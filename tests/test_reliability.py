import argparse
import contextlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import reader
from description_quality import assess_description, MORE_LABEL
from posting_identity import canonical_job_url, posting_key
from seen_postings import SeenPostings
from pagination import advance


JOB = "https://www.linkedin.com/jobs/view/123/"


def record(description="Full responsibilities and qualifications."):
    return {"link": JOB, "title": "Analyst", "description": description, "company": "Fixture"}


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=reader.BASE_DIR)
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_startup_failures_preserve_existing_output_byte_for_byte(self):
        output = self.folder / "jobs.json"
        for error in (RuntimeError("CDP unavailable"), sqlite3.OperationalError("locked"),
                      argparse.ArgumentTypeError("bad checkpoint URL")):
            with self.subTest(error=error):
                original = b'{"jobs":[{"title":"Saved"}],"schema_version":2}'
                output.write_bytes(original)
                args = SimpleNamespace(output=output, url=reader.DEFAULT_SEARCH, limit=10)
                with patch.object(reader, "run_browser", side_effect=error), contextlib.redirect_stdout(io.StringIO()):
                    _, code = reader.execute(args)
                self.assertEqual(code, 1)
                self.assertEqual(output.read_bytes(), original)

    def test_corrupt_and_wrong_filter_checkpoints_are_preserved(self):
        output = self.folder / "jobs.json"
        search = reader.search_url(reader.DEFAULT_SEARCH)
        for content in ('{broken', json.dumps({"schema_version": 2, "search_url": search,
                        "jobs": [], "errors": [], "progress": {"results_page_url": search.replace("Delhi", "Mumbai"),
                        "pending_links": [], "attempted_links": []}})):
            output.write_text(content, encoding="utf-8")
            args = SimpleNamespace(output=output, fresh=False, limit=10)
            with self.assertRaises((ValueError, argparse.ArgumentTypeError)):
                reader.prepare_output(args, {}, search)
            self.assertFalse(args.output_ready)
            self.assertEqual(output.read_text(encoding="utf-8"), content)

    def test_strict_identity_rejects_invalid_urls(self):
        for url in (JOB.replace("https:", "http:"), JOB.replace("www.", "u:p@www."),
                    JOB.replace("linkedin.com", "linkedin.com:444"), "https://www.linkedin.com:bad/jobs/view/123/"):
            self.assertIsNone(posting_key(url))
            self.assertIsNone(canonical_job_url(url))

    def test_ellipsis_expansion_labels_and_quality(self):
        for label in ("\u2026 more", "\u2026more", "... more", "Show more", "See more"):
            self.assertIsNotNone(MORE_LABEL.search(label))
            self.assertEqual(assess_description("Some content " + label), "suspected_truncated")
        self.assertEqual(assess_description(""), "missing")
        self.assertEqual(assess_description("An ordinary complete sentence."), "complete_candidate")

    def test_quality_repair_upgrades_identity_and_does_not_downgrade(self):
        with SeenPostings(self.folder / "history.sqlite3") as history:
            history.remember(record("Partial ... more"), "old.json")
            self.assertFalse(history.contains(JOB))
            self.assertTrue(history.was_seen(JOB))
            self.assertEqual(len(history.retry_candidates("linkedin")), 1)
            history.remember(record(), "repair.json")
            history.remember(record("Later bad ... more"), "later.json")
            self.assertTrue(history.contains(JOB))
            self.assertEqual(history.count(), 1)
            self.assertEqual(history.retry_candidates("linkedin"), [])

    def test_legacy_database_is_backed_up_and_reaudited_once(self):
        path = self.folder / "history.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE seen_postings(source TEXT, posting_id TEXT, link TEXT, title TEXT, company TEXT, first_seen_at TEXT, first_output TEXT, PRIMARY KEY(source,posting_id))")
            db.execute("INSERT INTO seen_postings VALUES (?,?,?,?,?,?,?)", ("linkedin", "123", JOB, "Analyst", "Fixture", "old", "old.json"))
        (self.folder / "old.json").write_text(json.dumps({"jobs": [record("Partial ... more")]}), encoding="utf-8")
        with SeenPostings(path) as history:
            history.import_directory(self.folder)
            self.assertEqual(history.count(), 1)
            self.assertFalse(history.contains(JOB))
            self.assertTrue((self.folder / "history_before_quality_v2.sqlite3").exists())
        with SeenPostings(path) as history:
            self.assertEqual(history.import_directory(self.folder)["files_imported"], 0)

    def test_transient_retry_once_then_success(self):
        worker = Mock()
        worker.read.side_effect = [reader.PlaywrightTimeoutError("slow"), record()]
        result = {}
        with patch.object(reader, "interruptible_wait"):
            saved = reader.read_with_retry(worker, JOB, SimpleNamespace(retries=1), result, Mock())
        self.assertEqual(saved["extraction_attempts"], 2)
        self.assertEqual(result["retried"], 1)

    def test_block_and_data_errors_are_not_retried(self):
        for error in (reader.AccessBlocked("verification"), ValueError("layout changed")):
            worker = Mock()
            worker.read.side_effect = error
            with self.assertRaises(type(error)):
                reader.read_with_retry(worker, JOB, SimpleNamespace(retries=3), {}, Mock())
            worker.read.assert_called_once()

    def test_cancel_during_backoff(self):
        event = threading.Event()
        page = Mock()
        page.wait_for_timeout.side_effect = lambda _: event.set()
        with self.assertRaises(KeyboardInterrupt):
            reader.interruptible_wait(page, 5000, event)
        page.wait_for_timeout.assert_called_once()

    def test_json_failure_does_not_mark_history(self):
        with SeenPostings(self.folder / "history.sqlite3") as history:
            with patch.object(reader, "save_result", side_effect=OSError("disk full")), self.assertRaises(OSError):
                reader.persist_record({"jobs": []}, record(), self.folder / "jobs.json", history)
            self.assertFalse(history.was_seen(JOB))

    def test_repair_updates_existing_id_and_preserves_original_file(self):
        original = self.folder / "original.json"
        original.write_text(json.dumps({"jobs": [record("Old ... more")]}), encoding="utf-8")
        before = original.read_bytes()
        with SeenPostings(self.folder / "history.sqlite3") as history:
            history.import_directory(self.folder)
            args = SimpleNamespace(seen_store=history, limit=10, retries=1, delay=1)
            result = {"search_url": reader.DEFAULT_SEARCH, "jobs": [], "errors": [],
                      "progress": {"pending_links": [], "attempted_links": []}}
            worker = Mock()
            worker.read.return_value = record()
            with patch.object(reader, "interruptible_wait"):
                reader.collect_repairs(Mock(), worker, args, result, self.folder / "repair.json")
            self.assertEqual(history.count(), 1)
            self.assertTrue(history.contains(JOB))
            self.assertEqual(original.read_bytes(), before)

    def test_missing_pager_is_not_reported_as_last_page(self):
        group = Mock()
        group.all.return_value = []
        outcome = advance(Mock(url=reader.DEFAULT_SEARCH), [group], lambda _: [JOB])
        self.assertEqual(outcome.status, "missing_control")
        self.assertFalse(outcome)

    def test_failed_checkpoint_url_is_requeued_on_resume(self):
        output = self.folder / "jobs.json"
        search = reader.search_url(reader.DEFAULT_SEARCH)
        args = SimpleNamespace(output=output, fresh=False, limit=10)
        result = {"jobs": [], "errors": []}
        reader.prepare_output(args, result, search)
        result["errors"] = [{"link": JOB, "error": "timeout"}]
        result["progress"]["attempted_links"] = [JOB]
        reader.save_result(output, result)
        reader.prepare_output(args, {}, search)
        loaded = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(loaded["progress"]["pending_links"], [JOB])
        self.assertEqual(loaded["progress"]["attempted_links"], [])

    def test_wrong_posting_returned_by_adapter_is_rejected(self):
        worker = Mock()
        worker.read.return_value = dict(record(), link=JOB.replace("123", "456"))
        with self.assertRaisesRegex(ValueError, "different"):
            reader.read_with_retry(worker, JOB, SimpleNamespace(retries=1), {}, Mock())

    def test_same_id_with_two_slugs_is_read_once(self):
        url = "https://internshala.com/internship/detail/analyst123"
        alias = "https://internshala.com/internship/detail/new-analyst123"
        worker = Mock()
        worker.read.return_value = dict(record(), link=url)
        args = SimpleNamespace(limit=10, delay=1)
        result = {"search_url": reader.internshala.DEFAULT_SEARCH, "jobs": [], "errors": []}
        with patch.object(reader, "navigate"), patch.object(reader, "wait_for_content"), \
                patch.object(reader, "collect_links", return_value=[url, alias]), \
                patch.object(reader, "next_results", return_value=False), patch.object(reader, "interruptible_wait"):
            reader.collect_jobs(Mock(url=result["search_url"]), worker, args, result, self.folder / "jobs.json")
        worker.read.assert_called_once_with(url)

    def test_search_redirect_with_changed_filters_is_rejected(self):
        page = Mock(url=reader.DEFAULT_SEARCH.replace("Delhi", "Mumbai"))
        page.is_closed.return_value = False
        page.goto.return_value.status = 200
        page.locator.return_value.count.return_value = 0
        with self.assertRaisesRegex(ValueError, "different filters"):
            reader.navigate(page, reader.DEFAULT_SEARCH)

    def test_collection_lock_blocks_a_second_collector_and_releases(self):
        from run_lock import CollectionLock
        path = self.folder / "collection.lock"
        with CollectionLock(path):
            with self.assertRaisesRegex(RuntimeError, "Another reader"):
                with CollectionLock(path):
                    pass
        with CollectionLock(path):
            pass

    def test_dead_first_control_does_not_hide_second(self):
        page = Mock(url=reader.DEFAULT_SEARCH)
        first, second, group = Mock(), Mock(), Mock()
        for control in (first, second):
            control.is_visible.return_value = control.is_enabled.return_value = True
            control.get_attribute.return_value = ""
            control.inner_text.return_value = "Next"
            control.locator.return_value.count.return_value = 0
        first.click.side_effect = reader.PlaywrightError("element detached")
        group.all.return_value = [first, second]
        links = Mock(side_effect=[[JOB], [JOB.replace("123", "456")]])
        with patch.object(reader, "check_access"):
            outcome = advance(page, [group], links)
        self.assertTrue(outcome)
        second.click.assert_called_once()
        self.assertEqual(len(outcome.next_controls), 2)
