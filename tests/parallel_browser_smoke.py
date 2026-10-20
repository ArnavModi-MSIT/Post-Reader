"""Exercise production CDP workers against one isolated, temporary Chrome."""
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
import reader
from parallel_reader import ParallelDetailReader


def main():
    chrome = Path(os.environ.get("ProgramFiles", "/nonexistent")) / "Google/Chrome/Application/chrome.exe"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, executable_path=str(chrome) if chrome.exists() else None,
            headless=True, args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1"])
        original = context.pages[0]
        barrier = threading.Barrier(2)
        owners = set()
        guard = threading.Lock()

        def fixture_read(page, url):
            with guard:
                owners.add(threading.get_ident())
            page.set_content("<h1>Local fixture</h1>")
            barrier.wait(timeout=10)
            page.wait_for_timeout(100)
            return {"link": url, "title": page.locator("h1").inner_text(), "description": "Fixture description."}

        worker = None
        try:
            with patch.object(reader, "read_job", side_effect=fixture_read):
                worker = ParallelDetailReader(f"http://127.0.0.1:{port}", delay=0)
                urls = [f"https://www.linkedin.com/jobs/view/{number}/" for number in (123, 456)]
                worker.prime(urls)
                records = [worker.read(url) for url in urls]
                assert [row["link"] for row in records] == urls
                assert len(owners) == 2, owners
                worker.close()
                worker = None
            original.wait_for_timeout(100)
            assert not original.is_closed()
            assert len(context.pages) == 1, len(context.pages)
            print("Parallel CDP fixture passed: two owning threads overlap, correct records returned, worker tabs cleaned up, original tab preserved. No job-site requests.")
        finally:
            if worker:
                worker.close()
            context.close()


if __name__ == "__main__":
    main()
