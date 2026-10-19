"""Loopback-only UI for LinkedIn and Internshala readers; no extra dependencies."""
import argparse
import json
import math
import os
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import reader


def source_name(url):
    return "internshala" if urlsplit(url or "").hostname in reader.internshala.HOSTS else "linkedin"

BASE_DIR = Path(__file__).resolve().parent
APP_ID = "local-linkedin-reader"


def ensure_chrome():
    try:
        with urlopen("http://127.0.0.1:9222/json/version", timeout=2) as response:
            info = json.load(response)
            if info.get("webSocketDebuggerUrl") and "Chrome" in info.get("Browser", ""):
                return
    except (OSError, ValueError, URLError):
        pass
    if os.name != "nt":
        raise RuntimeError("Start debug-enabled Chrome on port 9222 before collecting.")
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(BASE_DIR / "launch_reader.ps1"), "-ChromeOnly"],
        cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode:
        raise RuntimeError("Chrome could not start. Check the latest log in logs/ and log into the dedicated profile.")


class Manager:
    def __init__(self, data_dir=None, execute=None, bootstrap=None):
        self.data_dir = Path(data_dir or BASE_DIR / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.execute = execute or reader.execute
        self.bootstrap = bootstrap or ensure_chrome
        self.lock = threading.Lock()
        self.thread = None
        self.stop_event = threading.Event()
        self.args = None
        self.phase = "idle"
        self.message = ""
        self.cache = {}
        self.token = secrets.token_urlsafe(32)

    def read_file(self, name):
        if not name or Path(name).name != name or not name.endswith(".json"):
            raise ValueError("Invalid result filename.")
        path = (self.data_dir / name).resolve()
        if path.parent != self.data_dir.resolve():
            raise ValueError("Result must be inside data/.")
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self.lock:
            cached = self.cache.get(name)
        if cached and cached[0] == stamp:
            return cached[1]
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value.get("jobs"), list) or not isinstance(value.get("errors"), list):
            raise ValueError("Not a job-result file.")
        with self.lock:
            self.cache[name] = (stamp, value)
        return value

    def history(self):
        rows = []
        for path in sorted(self.data_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                value = self.read_file(path.name)
            except (OSError, ValueError, TypeError):
                continue
            rows.append({"file": path.name, "count": len(value["jobs"]), "errors": len(value["errors"]),
                         "source": value.get("source") or source_name(value.get("search_url")),
                         "status": value.get("status", "unknown"), "updated_at": value.get("updated_at"),
                         "search_url": value.get("search_url", "")})
        return rows[:40]

    def settings(self):
        try:
            return json.loads((self.data_dir / "ui_settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            history = self.history()
            return {"url": history[0]["search_url"] if history else reader.DEFAULT_SEARCH, "limit": 1000, "delay": 3}

    def start(self, payload):
        url = reader.search_url(str(payload.get("url", "")).strip())
        limit = payload.get("limit", 1000)
        delay = payload.get("delay", 3)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Target must be a whole number from 1 to 1,000.")
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay) or delay < 1:
            raise ValueError("Delay must be at least one second.")
        fresh = payload.get("fresh", False)
        if not isinstance(fresh, bool):
            raise ValueError("Invalid resume setting.")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError("A collection is already running. Stop it before starting another.")
            self.stop_event = threading.Event()
            output = self.data_dir / reader.daily_output_path(url, fresh).name
            self.args = SimpleNamespace(url=url, limit=limit, delay=delay, fresh=fresh, output=output,
                                        profile=BASE_DIR / ".browser-profile", cdp="http://127.0.0.1:9222",
                                        launch=False, stop_event=self.stop_event)
            self.phase, self.message = "starting", "Preparing Chrome and loading the search..."
            (self.data_dir / "ui_settings.json").write_text(
                json.dumps({"url": url, "limit": limit, "delay": delay}, indent=2), encoding="utf-8")
            self.thread = threading.Thread(target=self._work, args=(self.args,), daemon=True)
            self.thread.start()

    def _work(self, args):
        try:
            self.bootstrap()
            if args.stop_event.is_set():
                with self.lock:
                    self.phase, self.message = "interrupted", "Stopped before collection began."
                return
            result, _ = self.execute(args)
            with self.lock:
                self.phase, self.message = result["status"], result.get("stop_reason") or ""
        except Exception as exc:
            with self.lock:
                self.phase, self.message = "failed", str(exc)

    def stop(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                self.stop_event.set()
                self.phase = "stopping"
                self.message = "Stopping after the current browser operation; saved jobs will be kept."

    def snapshot(self, selected=None):
        history = self.history()
        with self.lock:
            running = bool(self.thread and self.thread.is_alive())
            args, phase, message = self.args, self.phase, self.message
        active_file = args.output.name if args else None
        chosen = selected or active_file or (history[0]["file"] if history else None)
        value = {}
        if chosen:
            try:
                value = self.read_file(chosen)
            except (OSError, ValueError):
                if selected:
                    raise
        status = value.get("status", "idle")
        is_active = bool(args and chosen == active_file)
        if is_active:
            if running:
                status = "stopping" if phase == "stopping" else ("running" if status == "running" else "starting")
            else:
                status = phase
        progress = value.get("progress", {})
        jobs = value.get("jobs", [])
        errors = value.get("errors", [])
        if is_active and running and status == "running":
            message = "Collecting job descriptions. Each saved job is written to JSON automatically."
        return {"app": APP_ID, "token": self.token, "running": running, "active_file": active_file,
                "source": value.get("source") or source_name(value.get("search_url", args.url if args else "")),
                "file": chosen, "status": status, "message": message if is_active else value.get("stop_reason", ""),
                "skipped": value.get("duplicates_skipped", 0), "saved": len(jobs), "errors": len(errors), "attempted": len(progress.get("attempted_links", [])),
                "pending": len(progress.get("pending_links", [])), "target": value.get("requested_limit", args.limit if args else 1000),
                "updated_at": value.get("updated_at"), "search_url": value.get("search_url", args.url if args else ""),
                "jobs": [{k: job.get(k, "") for k in ("job_id", "title", "company", "link", "location_and_posting_info")}
                         for job in reversed(jobs[-20:])], "last_error": errors[-1] if errors else None,
                "history": history, "settings": self.settings()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send_bytes(self, code, body, kind, filename=None):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # A closing/reloading browser tab can cancel a polling request.

    def json_reply(self, code, value):
        self.send_bytes(code, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def do_GET(self):
        if not self.valid_host():
            return self.json_reply(403, {"error": "Localhost access only."})
        path = urlsplit(self.path)
        query = parse_qs(path.query)
        try:
            if path.path == "/api/health":
                return self.json_reply(200, {"app": APP_ID})
            if path.path == "/api/state":
                return self.json_reply(200, self.server.app.snapshot(query.get("file", [None])[0]))
            if path.path in {"/api/download", "/api/job"}:
                name = query.get("file", [""])[0]
                value = self.server.app.read_file(name)
                if path.path == "/api/download":
                    return self.send_bytes(200, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"),
                                           "application/json; charset=utf-8", name)
                job_id = query.get("id", [""])[0]
                job = next((job for job in value["jobs"] if job.get("job_id") == job_id), None)
                return self.json_reply(200 if job else 404, job or {"error": "Job not found."})
            assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            if path.path in assets:
                name, kind = assets[path.path]
                return self.send_bytes(200, (BASE_DIR / "web" / name).read_bytes(), kind + "; charset=utf-8")
            return self.json_reply(404, {"error": "Not found."})
        except (OSError, ValueError, TypeError) as exc:
            return self.json_reply(404, {"error": str(exc)})

    def do_POST(self):
        if not self.valid_host() or self.headers.get("X-Reader-Token") != self.server.app.token:
            return self.json_reply(403, {"error": "Reload the dashboard before trying again."})
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            return self.json_reply(403, {"error": "Cross-origin requests are not allowed."})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 16384 or not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ValueError("A small JSON request is required.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object.")
            if self.path == "/api/start":
                self.server.app.start(payload)
            elif self.path == "/api/stop":
                self.server.app.stop()
            else:
                return self.json_reply(404, {"error": "Not found."})
            self.json_reply(200, {"ok": True})
        except RuntimeError as exc:
            self.json_reply(409, {"error": str(exc)})
        except (ValueError, TypeError, argparse.ArgumentTypeError) as exc:
            self.json_reply(400, {"error": str(exc)})
        except OSError as exc:
            self.json_reply(500, {"error": str(exc)})


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(port=8765, manager=None):
    server = DashboardServer(("127.0.0.1", port), Handler)
    server.app = manager or Manager()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    try:
        with urlopen(url + "/api/health", timeout=1) as response:
            existing = json.load(response).get("app") == APP_ID
    except (OSError, ValueError):
        existing = False
    if existing:
        if args.open:
            webbrowser.open(url)
        print(f"Dashboard is already running: {url}", flush=True)
        return
    try:
        server = create_server(args.port)
    except OSError:
        try:
            with urlopen(url + "/api/health", timeout=2) as response:
                if json.load(response).get("app") != APP_ID:
                    raise ValueError("Another application uses this port.")
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Cannot start dashboard on {url}: {exc}")
        if args.open:
            webbrowser.open(url)
        print(f"Dashboard is already running: {url}", flush=True)
        return
    print(f"LinkedIn Reader dashboard: {url}\nKeep this process open. Ctrl+C stops the server.", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        server.app.stop()
    finally:
        if server.app.thread:
            server.app.thread.join(timeout=40)
        server.server_close()


if __name__ == "__main__":
    main()
