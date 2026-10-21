import argparse
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import reader


def job(number):
    return f"https://www.linkedin.com/jobs/view/{number}/"


class ReaderTests(unittest.TestCase):
    def test_search_retains_filters_and_removes_pinned_job(self):
        value = reader.search_url(
            "https://www.linkedin.com/jobs/search-results/?keywords=Data%20Analyst"
            "&geoId=102713980&f_TPR=r86400&currentJobId=123&start=75"
            "&originToLandingJobPostings=123&sortBy=R")
        self.assertIn("keywords=Data+Analyst", value)
        self.assertIn("f_TPR=r86400", value)
        self.assertIn("geoId=102713980", value)
        self.assertIn("sortBy=DD", value)
        self.assertIn("start=0", value)
        self.assertNotIn("currentJobId", value)
        self.assertNotIn("originToLanding", value)

    def test_foreign_or_non_search_urls_are_rejected(self):
        for value in ("https://linkedin.com.example.com/jobs/search/",
                      "https://example.com/jobs/search/", "https://www.linkedin.com/feed/",
                      "https://user:password@www.linkedin.com/jobs/search/"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                reader.search_url(value)

    def test_job_url_normalization(self):
        self.assertEqual(reader.canonical_job_url(job(123) + "?trackingId=test"), job(123))
        self.assertEqual(reader.canonical_job_url("https://www.linkedin.com/jobs/view/data-analyst-123/"), job(123))
        self.assertEqual(reader.canonical_job_url("https://www.linkedin.com/jobs/search-results/?currentJobId=123"), job(123))
        self.assertIsNone(reader.canonical_job_url("https://example.com/jobs/view/123/"))

    def test_verification_and_rate_limit_stop_immediately(self):
        page = Mock(url="https://www.linkedin.com/checkpoint/challenge")
        page.is_closed.return_value = False
        with self.assertRaises(reader.AccessBlocked):
            reader.wait_for_content(page, "h1")
        page = Mock(url=job(123))
        page.is_closed.return_value = False
        page.locator.return_value.count.return_value = 0
        page.goto.return_value.status = 429
        with self.assertRaises(reader.AccessBlocked):
            reader.navigate(page, job(123))

    def test_redirect_cannot_capture_a_different_job(self):
        page = Mock(url=job(456))
        page.is_closed.return_value = False
        page.locator.return_value.count.return_value = 0
        page.goto.return_value.status = 200
        with self.assertRaises(ValueError):
            reader.navigate(page, job(123))

    def test_repeated_links_end_scrolling(self):
        page = Mock(url=reader.DEFAULT_SEARCH)
        with patch.object(reader, "check_access"), patch.object(reader, "job_links", return_value=[job(123)]) as links:
            self.assertEqual(reader.collect_links(page, 1000), [job(123)])
            self.assertEqual(links.call_count, 4)

    def test_collection_deduplicates_across_pages_and_honors_limit(self):
        args = SimpleNamespace(limit=3, delay=1)
        result = {"search_url": reader.DEFAULT_SEARCH, "jobs": [], "errors": []}
        page = Mock()
        with patch.object(reader, "navigate"), patch.object(reader, "wait_for_content"), \
                patch.object(reader, "collect_links", side_effect=[[job(1), job(2)], [job(2), job(3), job(4)]]), \
                patch.object(reader, "next_results", return_value=True) as next_page, \
                patch.object(reader, "save_result"), \
                patch.object(reader, "read_job", side_effect=lambda detail, url: {"link": url}) as read:
            reader.collect_jobs(page, SimpleNamespace(read=lambda url: reader.read_job(None, url)), args, result, Path("unused.json"))
        self.assertEqual([value[0][1] for value in read.call_args_list], [job(1), job(2), job(3)])
        self.assertEqual(result["status"], "completed")
        next_page.assert_called_once()

    def test_five_failed_descriptions_stop_requests(self):
        args = SimpleNamespace(limit=10, delay=1)
        result = {"search_url": reader.DEFAULT_SEARCH, "jobs": [], "errors": []}
        with patch.object(reader, "navigate"), patch.object(reader, "wait_for_content"), \
                patch.object(reader, "collect_links", return_value=[job(i) for i in range(10)]), \
                patch.object(reader, "save_result"), \
                patch.object(reader, "read_job", side_effect=ValueError("missing description")) as read:
            with self.assertRaisesRegex(RuntimeError, "Five consecutive"):
                reader.collect_jobs(Mock(), SimpleNamespace(read=lambda url: reader.read_job(None, url)), args, result, Path("unused.json"))
        self.assertEqual(read.call_count, 5)
        self.assertEqual(len(result["errors"]), 5)

    def test_failed_replace_preserves_previous_json(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            path = Path(folder) / "jobs.json"
            reader.save_result(path, {"jobs": [{"description": "Delhi \u20b9"}]})
            original = path.read_text(encoding="utf-8")
            with patch.object(Path, "replace", side_effect=PermissionError("locked")), patch.object(reader.time, "sleep"):
                with self.assertRaises(PermissionError):
                    reader.save_result(path, {"jobs": []})
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_main_saves_partial_work_on_verification(self):
        def blocked(args, result, output):
            args.output_ready = True  # Simulate an initialized checkpoint, not a startup failure.
            result["jobs"].append({"link": job(123)})
            raise reader.AccessBlocked("verification")

        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            output = Path(folder) / "jobs.json"
            with patch("sys.argv", ["reader.py", "--output", str(output)]), \
                    patch.object(reader, "run_browser", side_effect=blocked):
                code = reader.main()
            saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["jobs"], [{"link": job(123)}])

    def test_disabled_pager_is_not_clicked(self):
        page = Mock(url=reader.DEFAULT_SEARCH)
        page.evaluate.return_value = []
        page.get_by_role.return_value.all.return_value = []
        button = Mock()
        page.locator.return_value.all.return_value = [button]
        page.locator.return_value.count.return_value = 1
        button.is_visible.return_value = True
        button.is_enabled.return_value = False
        self.assertFalse(reader.next_results(page))
        button.click.assert_not_called()

    def test_closed_detail_tab_is_recreated_once(self):
        context = Mock()
        context.browser.is_connected.return_value = True
        first, second = Mock(), Mock()
        first.is_closed.return_value = False
        second.is_closed.return_value = False
        context.new_page.side_effect = [first, second]
        owned = []
        worker = reader.DetailReader(context, owned)
        with patch.object(reader, "read_job", side_effect=[
                reader.PlaywrightError("Target page, context or browser has been closed"), {"link": job(1)}]):
            self.assertEqual(worker.read(job(1)), {"link": job(1)})
        self.assertEqual(owned, [first, second])

    def test_disconnected_browser_does_not_spend_job_queue(self):
        context = Mock()
        context.browser.is_connected.return_value = False
        worker = reader.DetailReader(context, [])
        with patch.object(reader, "read_job", side_effect=reader.PlaywrightError(
                "Target page, context or browser has been closed")):
            with self.assertRaises(reader.BrowserStopped):
                worker.read(job(1))
        self.assertEqual(context.new_page.call_count, 1)

    def test_daily_output_resumes_saved_jobs_and_pending_queue(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            output = Path(folder) / "daily.json"
            search = reader.search_url(reader.DEFAULT_SEARCH)
            args = SimpleNamespace(output=output, fresh=False, limit=10)
            result = {"jobs": [], "errors": []}
            reader.prepare_output(args, result, search)
            result["jobs"].append({"link": job(1)})
            result["progress"]["pending_links"] = [job(2)]
            result["progress"]["attempted_links"] = [job(1)]
            reader.save_result(output, result)
            resumed = {}
            reader.prepare_output(args, resumed, search)
            self.assertEqual(resumed["jobs"], [{"link": job(1)}])
            self.assertEqual(resumed["progress"]["pending_links"], [job(2)])

    def test_incompatible_checkpoint_is_preserved(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            output = Path(folder) / "daily.json"
            output.write_text('{"schema_version": 1}', encoding="utf-8")
            args = SimpleNamespace(output=output, fresh=False, limit=10)
            with self.assertRaises(ValueError):
                reader.prepare_output(args, {}, reader.search_url(reader.DEFAULT_SEARCH))
            self.assertFalse(args.output_ready)
            self.assertEqual(output.read_text(encoding="utf-8"), '{"schema_version": 1}')

    def test_restart_reads_pending_job_without_repeating_saved_job(self):
        args = SimpleNamespace(limit=2, delay=1)
        result = {"search_url": reader.DEFAULT_SEARCH, "jobs": [], "errors": []}
        worker = Mock()
        worker.read.side_effect = [{"link": job(1)}, reader.BrowserStopped("connection closed")]
        page = Mock(url=reader.DEFAULT_SEARCH)
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            output = Path(folder) / "jobs.json"
            with patch.object(reader, "navigate"), patch.object(reader, "wait_for_content"), \
                    patch.object(reader, "collect_links", return_value=[job(1), job(2)]):
                with self.assertRaises(reader.BrowserStopped):
                    reader.collect_jobs(page, worker, args, result, output)
            resumed = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(resumed["progress"]["pending_links"], [job(2)])
            self.assertEqual(resumed["jobs"], [{"link": job(1)}])
            worker.read.side_effect = None
            worker.read.return_value = {"link": job(2)}
            worker.read.reset_mock()
            with patch.object(reader, "navigate"), patch.object(reader, "wait_for_content"), \
                    patch.object(reader, "collect_links") as discover:
                reader.collect_jobs(page, worker, args, resumed, output)
            worker.read.assert_called_once_with(job(2))
            discover.assert_not_called()
            self.assertEqual(resumed["status"], "completed")

    def test_restart_after_pagination_failure_advances_past_completed_page(self):
        args = SimpleNamespace(limit=2, delay=1)
        result = {"search_url": reader.DEFAULT_SEARCH, "jobs": [{"link": job(1)}], "errors": [],
                  "progress": {"results_page_url": reader.DEFAULT_SEARCH, "pending_links": [],
                               "attempted_links": [job(1)], "page_complete": True}}
        worker = Mock()
        worker.read.return_value = {"link": job(2)}
        with patch.object(reader, "navigate"), patch.object(reader, "wait_for_content"), \
                patch.object(reader, "collect_links", side_effect=[[job(1)], [job(2)]]), \
                patch.object(reader, "next_results", return_value=True) as advance, \
                patch.object(reader, "save_result"):
            reader.collect_jobs(Mock(url=reader.DEFAULT_SEARCH), worker, args, result, Path("unused.json"))
        advance.assert_called_once()
        worker.read.assert_called_once_with(job(2))
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
