import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import reader
from seen_postings import SeenPostings, posting_key


def job(number):
    return f"https://www.linkedin.com/jobs/view/{number}/"


def record(number):
    return {"link": job(number), "title": "Analyst", "description": "Job details"}


class HistoryTests(unittest.TestCase):
    def test_identity_ignores_slugs_tracking_and_search(self):
        for url in (job(123), job(123) + "?trackingId=x",
                    "https://linkedin.com/jobs/view/analyst-123/",
                    "https://www.linkedin.com/jobs/search/?currentJobId=123"):
            self.assertEqual(posting_key(url), ("linkedin", "123"))
        self.assertEqual(posting_key("https://internshala.com/internship/detail/analyst123"), ("internshala", "123"))
        self.assertIsNone(posting_key("https://example.com/jobs/view/123/"))

    def test_persists_after_reopen_and_separates_sites(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            path = Path(folder) / "history.sqlite3"
            with SeenPostings(path) as history:
                self.assertTrue(history.remember(record(123), "yesterday.json"))
                self.assertFalse(history.remember(record(123), "another-search.json"))
            with SeenPostings(path) as history:
                self.assertTrue(history.contains(job(123) + "?trackingId=new"))
                self.assertFalse(history.contains("https://internshala.com/internship/detail/analyst123"))
                self.assertEqual(history.count(), 1)

    def test_imports_successes_only_and_updates_changed_files(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            folder = Path(folder)
            output = folder / "old.json"
            output.write_text(json.dumps({"jobs": [record(1), record(1), {"link": job(2), "title": "Incomplete"}],
                                          "errors": [{"link": job(3)}]}), encoding="utf-8")
            (folder / "broken.json").write_text("{", encoding="utf-8")
            with SeenPostings(folder / "history.sqlite3") as history:
                self.assertEqual(history.import_directory(folder)["postings_added"], 1)
                self.assertEqual(history.import_directory(folder)["files_imported"], 0)
                self.assertFalse(history.contains(job(2)))
                self.assertFalse(history.contains(job(3)))
                output.write_text(json.dumps({"jobs": [record(1), record(44)]}), encoding="utf-8")
                self.assertEqual(history.import_directory(folder)["postings_added"], 1)
                output.unlink()
                self.assertTrue(history.contains(job(1)))
                self.assertTrue(history.contains(job(44)))

    def test_collect_skips_seen_page_then_saves_new_posting(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            folder = Path(folder)
            with SeenPostings(folder / "history.sqlite3") as history:
                history.remember(record(1), "yesterday-other-search.json")
                args = SimpleNamespace(limit=1, delay=1, fresh=True, seen_store=history)
                result = {"search_url": reader.DEFAULT_SEARCH, "jobs": [], "errors": []}
                worker = Mock()
                worker.read.return_value = record(2)
                with patch.object(reader, "navigate"), patch.object(reader, "wait_for_content"), \
                        patch.object(reader, "collect_links", side_effect=[[job(1)], [job(1), job(2)]]), \
                        patch.object(reader, "next_results", return_value=True) as advance:
                    reader.collect_jobs(Mock(url=reader.DEFAULT_SEARCH), worker, args, result, folder / "today.json")
                worker.read.assert_called_once_with(job(2))
                advance.assert_called_once()
                self.assertEqual(result["duplicates_skipped"], 1)
                self.assertTrue(history.contains(job(2)))
                self.assertEqual(json.loads((folder / "today.json").read_text(encoding="utf-8"))["jobs"], [record(2)])

    def test_failed_description_is_not_remembered(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            with SeenPostings(Path(folder) / "history.sqlite3") as history:
                args = SimpleNamespace(limit=1, delay=1, seen_store=history)
                result = {"search_url": reader.DEFAULT_SEARCH, "jobs": [], "errors": []}
                worker = Mock()
                worker.read.side_effect = ValueError("missing description")
                with patch.object(reader, "navigate"), patch.object(reader, "wait_for_content"), \
                        patch.object(reader, "collect_links", return_value=[job(1)]), \
                        patch.object(reader, "next_results", return_value=False):
                    reader.collect_jobs(Mock(url=reader.DEFAULT_SEARCH), worker, args, result, Path(folder) / "today.json")
                self.assertFalse(history.contains(job(1)))

    def test_repeated_seen_page_stops(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            with SeenPostings(Path(folder) / "history.sqlite3") as history:
                history.remember(record(1), "old.json")
                args = SimpleNamespace(limit=1, delay=1, seen_store=history)
                result = {"search_url": reader.DEFAULT_SEARCH, "jobs": [], "errors": []}
                worker = Mock()
                with patch.object(reader, "navigate"), patch.object(reader, "wait_for_content"), \
                        patch.object(reader, "collect_links", return_value=[job(1)]), \
                        patch.object(reader, "next_results", return_value=True) as advance:
                    reader.collect_jobs(Mock(url=reader.DEFAULT_SEARCH), worker, args, result, Path(folder) / "today.json")
                advance.assert_called_once()
                worker.read.assert_not_called()
                self.assertIn("repeated", result["stop_reason"])


if __name__ == "__main__":
    unittest.main()
