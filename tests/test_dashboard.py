import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import dashboard
import reader


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=reader.BASE_DIR)
        self.manager = dashboard.Manager(self.temp.name, execute=self.fake_run, bootstrap=lambda: None)
        sample = {"schema_version": 2, "status": "failed", "stop_reason": "Pagination stopped.",
                  "search_url": reader.search_url(reader.DEFAULT_SEARCH), "requested_limit": 1000,
                  "jobs": [{"job_id": "123", "link": "https://www.linkedin.com/jobs/view/123/",
                            "title": "Analyst", "company": "Fixture", "description": "SQL and Python"}],
                  "errors": [], "progress": {"pending_links": [], "attempted_links": ["123"]}}
        reader.save_result(Path(self.temp.name) / "jobs_sample.json", sample)
        self.server = dashboard.create_server(0, self.manager)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.manager.stop()
        if self.manager.thread:
            self.manager.thread.join(timeout=3)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def fake_run(self, args):
        args.stop_event.wait(3)
        result = {"status": "interrupted", "stop_reason": "Stopped by user.", "jobs": [], "errors": [], "search_url": args.url}
        reader.save_result(args.output, result)
        return result, 130

    def request(self, path, payload=None, headers=None):
        values = {"Content-Type": "application/json", "X-Reader-Token": self.manager.token}
        values.update(headers or {})
        request = Request(self.url + path, data=json.dumps(payload).encode() if payload is not None else None, headers=values)
        with urlopen(request, timeout=5) as response:
            return response.status, response.read()

    def test_existing_run_counts_and_description(self):
        _, data = self.request("/api/state")
        state = json.loads(data)
        self.assertEqual(state["saved"], 1)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["jobs"][0]["title"], "Analyst")
        _, description = self.request("/api/job?file=jobs_sample.json&id=123")
        self.assertEqual(json.loads(description)["description"], "SQL and Python")

    def test_job_filter_pagination_and_quality_counts(self):
        sample = {"jobs": [{"job_id": str(i), "title": f"Analyst {i}", "company": "Fixture",
                           "description": "Partial ... more" if i == 0 else "Full responsibilities.",
                           "link": f"https://www.linkedin.com/jobs/view/{i}/"} for i in range(25)], "errors": []}
        reader.save_result(Path(self.temp.name) / "many.json", sample)
        _, body = self.request("/api/state?file=many.json&page=2")
        state = json.loads(body)
        self.assertEqual(state["jobs_pages"], 2)
        self.assertEqual(len(state["jobs"]), 5)
        self.assertEqual(state["incomplete"], 1)
        _, body = self.request("/api/state?file=many.json&quality=suspected_truncated")
        self.assertEqual(json.loads(body)["jobs_matching"], 1)
        _, body = self.request("/api/state?file=many.json&q=Analyst%2024")
        self.assertEqual(json.loads(body)["jobs_matching"], 1)

    def test_history_reads_at_most_forty_results_and_bounds_cache(self):
        for i in range(60):
            reader.save_result(Path(self.temp.name) / f"history_{i}.json", {"jobs": [], "errors": []})
        with patch.object(self.manager, "read_file", wraps=self.manager.read_file) as read:
            self.assertEqual(len(self.manager.history()), 40)
        self.assertLessEqual(read.call_count, 40)
        for path in Path(self.temp.name).glob("history_*.json"):
            self.manager.read_file(path.name)
        self.assertLessEqual(len(self.manager.cache), 45)

    def test_malformed_local_files_do_not_break_polling(self):
        folder = Path(self.temp.name)
        (folder / "ui_settings.json").write_text("null", encoding="utf-8")
        for index, bad in enumerate((dict(jobs=[], errors=[], progress=[]),
                                     dict(jobs=[{"title": 42}], errors=[]),
                                     dict(jobs=[], errors=[], search_url=[]))):
            (folder / f"bad_{index}.json").write_text(json.dumps(bad), encoding="utf-8")
        _, body = self.request("/api/state")
        state = json.loads(body)
        self.assertEqual(state["saved"], 1)
        self.assertIsInstance(state["settings"], dict)
        self.assertEqual(len(state["history"]), 1)

    def test_download_preserves_saved_records(self):
        _, data = self.request("/api/download?file=jobs_sample.json")
        self.assertEqual(len(json.loads(data)["jobs"]), 1)

    def test_invalid_search_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request("/api/start", {"url": "https://example.com/", "limit": 10})
        self.assertEqual(error.exception.code, 400)

    def test_duplicate_start_and_graceful_stop(self):
        payload = {"url": reader.DEFAULT_SEARCH, "limit": 10, "delay": 1}
        self.request("/api/start", payload)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/start", payload)
        self.assertEqual(error.exception.code, 409)
        self.request("/api/stop", {})
        self.manager.thread.join(timeout=3)
        _, raw = self.request("/api/state")
        value = json.loads(raw)
        self.assertFalse(value["running"])
        self.assertEqual(value["status"], "interrupted")
        self.assertEqual(self.manager.settings()["limit"], 10)

    def test_cross_origin_and_missing_token_cannot_start(self):
        for headers in ({"X-Reader-Token": ""}, {"Origin": "https://example.com"}):
            with self.subTest(headers=headers), self.assertRaises(HTTPError) as error:
                self.request("/api/start", {"url": reader.DEFAULT_SEARCH}, headers)
            self.assertEqual(error.exception.code, 403)

    def test_private_files_and_traversal_are_not_served(self):
        for path in ("/.browser-profile/Local%20State", "/reader.py", "/api/download?file=../reader.py"):
            with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, 404)

    def test_static_dashboard_is_available(self):
        _, body = self.request("/")
        self.assertIn(b"Collection setup", body)
        _, script = self.request("/app.js")
        self.assertNotIn(b"innerHTML", script)

    def test_second_launch_reuses_existing_dashboard(self):
        with patch('sys.argv', ['dashboard.py', '--port', str(self.server.server_port), '--open']), \
                patch.object(dashboard.webbrowser, 'open') as opened:
            dashboard.main()
        opened.assert_called_once_with(self.url)

    def test_second_server_cannot_claim_same_port(self):
        with self.assertRaises(OSError):
            dashboard.create_server(self.server.server_port)


if __name__ == "__main__":
    unittest.main()
