import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import reader
from parallel_reader import ParallelDetailReader


class ParallelTests(unittest.TestCase):
    def test_poll_timeout_racing_with_completed_future_returns_result(self):
        worker = ParallelDetailReader("unused", workers=1)
        future = Mock()
        future.result.side_effect = [FutureTimeoutError("poll expired"), {"link": "one"}]
        future.done.return_value = True
        worker.pending["one"] = future
        try:
            self.assertEqual(worker.read("one"), {"link": "one"})
        finally:
            worker.close()

    def test_actual_future_timeout_error_is_not_swallowed(self):
        worker = ParallelDetailReader("unused", workers=1)
        future = Mock()
        future.result.side_effect = FutureTimeoutError("task failed")
        future.done.return_value = True
        worker.pending["one"] = future
        try:
            with self.assertRaisesRegex(FutureTimeoutError, "task failed"):
                worker.read("one")
        finally:
            worker.close()

    def test_invalid_worker_counts_are_rejected_before_starting_threads(self):
        for workers in (True, 2.0, None, 0, 4):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                ParallelDetailReader("unused", workers=workers)

    def test_three_readers_overlap_with_three_distinct_sessions(self):
        barrier = threading.Barrier(3)
        owners = set()
        @contextmanager
        def factory(stop):
            owner = threading.get_ident()
            def read(url):
                owners.add(owner)
                barrier.wait(timeout=3)
                return {"link": url}
            yield read
        worker = ParallelDetailReader("unused", workers=3, delay=0, session_factory=factory)
        try:
            worker.prime(["one", "two", "three"])
            self.assertEqual([worker.read(url)["link"] for url in ("one", "two", "three")], ["one", "two", "three"])
            self.assertEqual(len(owners), 3)
        finally:
            worker.close()

    def test_coordinator_alone_writes_and_prefetch_respects_target_and_history(self):
        from seen_postings import SeenPostings
        owner = threading.get_ident()
        urls = [f"https://www.linkedin.com/jobs/view/{i}/" for i in range(1, 5)]
        read_urls = []
        @contextmanager
        def factory(stop):
            def read(url):
                read_urls.append(url)
                return {"link": url, "title": "Analyst", "description": "Full description."}
            yield read
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as folder:
            output = Path(folder) / "jobs.json"
            with SeenPostings(Path(folder) / "history.sqlite3") as history:
                history.remember({"link": urls[0], "title": "Old", "description": "Already collected."}, "old.json")
                result = {"search_url": reader.DEFAULT_SEARCH, "jobs": [], "errors": []}
                args = SimpleNamespace(limit=2, delay=0, seen_store=history, retries=0)
                save, remember = reader.save_result, history.remember
                def save_on_coordinator(*args):
                    self.assertEqual(threading.get_ident(), owner)
                    return save(*args)
                def remember_on_coordinator(*args):
                    self.assertEqual(threading.get_ident(), owner)
                    return remember(*args)
                worker = ParallelDetailReader("unused", delay=0, session_factory=factory)
                try:
                    with patch.object(reader, "navigate"), patch.object(reader, "wait_for_content"), \
                            patch.object(reader, "collect_links", return_value=urls), \
                            patch.object(reader, "save_result", side_effect=save_on_coordinator), \
                            patch.object(history, "remember", side_effect=remember_on_coordinator):
                        reader.collect_jobs(Mock(url=reader.DEFAULT_SEARCH), worker, args, result, output)
                finally:
                    worker.close()
                self.assertEqual(set(read_urls), set(urls[1:3]))
                self.assertEqual(history.count(), 3)
                self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))["jobs"]), 2)
                self.assertIn(urls[3], result["progress"]["pending_links"])

    def test_two_sessions_overlap_and_cleanup_on_their_own_threads(self):
        barrier = threading.Barrier(2)
        opened, closed = [], []

        @contextmanager
        def factory(stop):
            owner = threading.get_ident()
            opened.append(owner)
            def read(url):
                self.assertEqual(owner, threading.get_ident())
                barrier.wait(timeout=3)
                return {"link": url}
            try:
                yield read
            finally:
                closed.append(threading.get_ident())

        worker = ParallelDetailReader("unused", delay=0, session_factory=factory)
        try:
            worker.prime(["one", "two"])
            self.assertEqual(worker.read("one"), {"link": "one"})
            self.assertEqual(worker.read("two"), {"link": "two"})
        finally:
            worker.close()
        self.assertEqual(len(set(opened)), 2)
        self.assertEqual(sorted(opened), sorted(closed))

    def test_block_stops_scheduling_and_surfaces_to_coordinator(self):
        @contextmanager
        def factory(stop):
            def read(url):
                raise reader.AccessBlocked("verification")
            yield read
        worker = ParallelDetailReader("unused", delay=0, session_factory=factory)
        try:
            worker.prime(["one", "two", "three"])
            with self.assertRaises(reader.AccessBlocked):
                worker.read("one")
            self.assertTrue(worker.abort.is_set())
            self.assertNotIn("three", worker.pending)
        finally:
            worker.close()

    def test_stop_keeps_unread_work_unconsumed(self):
        event = threading.Event()
        @contextmanager
        def factory(stop):
            yield lambda url: {"link": url}
        worker = ParallelDetailReader("unused", event, delay=0, session_factory=factory)
        try:
            event.set()
            with self.assertRaises(KeyboardInterrupt):
                worker.prime(["one", "two"])
            self.assertFalse(worker.pending)
        finally:
            worker.close()

    def test_failed_read_can_be_retried(self):
        attempts = []
        @contextmanager
        def factory(stop):
            def read(url):
                attempts.append(url)
                if len(attempts) == 1:
                    raise ValueError("first read failed")
                return {"link": url}
            yield read
        worker = ParallelDetailReader("unused", delay=0, workers=1, session_factory=factory)
        try:
            with self.assertRaises(ValueError):
                worker.read("one")
            self.assertEqual(worker.read("one"), {"link": "one"})
        finally:
            worker.close()


if __name__ == "__main__":
    unittest.main()
