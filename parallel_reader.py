"""Two independent Playwright workers; results are committed by the coordinator."""
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future, TimeoutError
from contextlib import ExitStack, contextmanager


class StopSignal:
    def __init__(self, external, abort):
        self.external, self.abort = external, abort

    def is_set(self):
        return self.abort.is_set() or (self.external is not None and self.external.is_set())


@contextmanager
def browser_session(cdp, stop):
    import reader
    from playwright.sync_api import sync_playwright
    owned = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp, timeout=10000)
        if not browser.contexts:
            raise reader.BrowserStopped("Chrome has no available context.")
        worker = reader.DetailReader(browser.contexts[0], owned, stop)
        try:
            yield worker.read
        finally:
            for page in owned:
                try:
                    if not page.is_closed():
                        page.close()
                except reader.PlaywrightError:
                    pass
            # Stopping this thread's Playwright connection leaves Chrome open.


class ParallelDetailReader:
    def __init__(self, cdp, stop_event=None, delay=3, workers=2, session_factory=None):
        self.abort = threading.Event()
        self.stop = StopSignal(stop_event, self.abort)
        self.external_stop = stop_event
        self.delay = delay
        self.workers = workers
        self.factory = session_factory or (lambda stop: browser_session(cdp, stop))
        self.tasks = queue.Queue()
        self.pending = {}
        self.backlog = deque()
        self.blocked = None
        self.threads = [threading.Thread(target=self._worker, name=f"detail-reader-{i+1}") for i in range(workers)]
        for thread in self.threads:
            thread.start()

    def _worker(self):
        import reader
        with ExitStack() as stack:
            read = None
            ready_at = 0
            while True:
                task = self.tasks.get()
                if task is None:
                    return
                url, future = task
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    while time.monotonic() < ready_at and not self.stop.is_set():
                        self.abort.wait(min(0.1, ready_at - time.monotonic()))
                    reader.check_cancel(self.stop)
                    if read is None:
                        read = stack.enter_context(self.factory(self.stop))
                    record = read(url)
                    ready_at = time.monotonic() + self.delay
                    future.set_result(record)
                except BaseException as exc:
                    if isinstance(exc, (reader.AccessBlocked, reader.BrowserStopped)):
                        self.blocked = exc
                        self.abort.set()
                    ready_at = time.monotonic() + self.delay
                    future.set_exception(exc)

    def _check(self):
        import reader
        if self.blocked is not None:
            raise self.blocked
        reader.check_cancel(self.stop)

    def _submit(self, url):
        future = Future()
        self.pending[url] = future
        self.tasks.put((url, future))

    def _fill(self):
        if self.stop.is_set():
            return
        while self.backlog and len(self.pending) < self.workers:
            self._submit(self.backlog.popleft())

    def prime(self, urls):
        self._check()
        self.backlog = deque(dict.fromkeys(urls))
        self._fill()

    def read(self, url):
        self._check()
        if url not in self.pending:
            self._submit(url)  # Bounded retries of a failed URL use the same workers.
        future = self.pending[url]
        try:
            while True:
                self._check()
                try:
                    return future.result(timeout=0.1)
                except TimeoutError:
                    if future.done():
                        raise  # A real TimeoutError raised by the detail reader.
        finally:
            self.pending.pop(url, None)
            self._fill()

    def close(self):
        self.abort.set()
        self.backlog.clear()
        for future in self.pending.values():
            future.cancel()
        for _ in self.threads:
            self.tasks.put(None)
        for thread in self.threads:
            thread.join()
