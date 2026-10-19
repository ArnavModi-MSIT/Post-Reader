"""Persistent, local identity index for successfully saved postings."""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def posting_key(url):
    """Use site + numeric posting ID, independent of tracking parameters or slugs."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
            return None
        host = (parts.hostname or "").removeprefix("www.")
        if host == "linkedin.com":
            match = re.fullmatch(r"/jobs/view/(?:[^/]*-)?(\d+)/?", parts.path)
            job_id = match.group(1) if match else None
            if not job_id and parts.path.rstrip("/") in {"/jobs/search", "/jobs/search-results"}:
                job_id = parse_qs(parts.query).get("currentJobId", [None])[0]
            if job_id and job_id.isascii() and job_id.isdigit():
                return "linkedin", job_id
        if host == "internshala.com":
            match = re.fullmatch(r"/internship/detail/[^/]*?(\d+)/?", parts.path)
            if match:
                return "internshala", match.group(1)
    except ValueError:
        pass
    return None


class SeenPostings:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS seen_postings (
                source TEXT NOT NULL,
                posting_id TEXT NOT NULL,
                link TEXT NOT NULL,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                first_output TEXT NOT NULL,
                PRIMARY KEY (source, posting_id)
            );
            CREATE TABLE IF NOT EXISTS imported_files (
                path TEXT PRIMARY KEY,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL
            );
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self.connection.close()

    def contains(self, url):
        key = posting_key(url)
        return bool(key and self.connection.execute(
            "SELECT 1 FROM seen_postings WHERE source=? AND posting_id=?", key).fetchone())

    def count(self):
        return self.connection.execute("SELECT COUNT(*) FROM seen_postings").fetchone()[0]

    @staticmethod
    def row(record, output):
        if not isinstance(record, dict):
            return None
        key = posting_key(record.get("link"))
        # Empty/incomplete output and entries under `errors` must not suppress future reads.
        if not key or not all(isinstance(record.get(field), str) and record[field].strip()
                              for field in ("title", "description")):
            return None
        return (*key, record["link"], record["title"], str(record.get("company") or ""),
                record.get("collected_at") or datetime.now(timezone.utc).isoformat(), str(output))

    def remember(self, record, output):
        row = self.row(record, output)
        if row is None:
            return False
        with self.connection:
            cursor = self.connection.execute("INSERT OR IGNORE INTO seen_postings VALUES (?, ?, ?, ?, ?, ?, ?)", row)
        return cursor.rowcount == 1

    def import_directory(self, directory):
        """Import changed JSON results only; never modify or delete source files/history."""
        report = {"files_imported": 0, "postings_added": 0, "unreadable_files": 0}
        for path in sorted(Path(directory).glob("*.json")):
            try:
                stat = path.stat()
                name = str(path.resolve())
                stamp = (stat.st_mtime_ns, stat.st_size)
                cached = self.connection.execute("SELECT mtime_ns, size FROM imported_files WHERE path=?", (name,)).fetchone()
                if cached == stamp:
                    continue
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                    continue
                rows = [row for record in data["jobs"] if (row := self.row(record, name))]
                before = self.connection.total_changes
                with self.connection:
                    self.connection.executemany("INSERT OR IGNORE INTO seen_postings VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
                    report["postings_added"] += self.connection.total_changes - before
                    self.connection.execute("INSERT OR REPLACE INTO imported_files VALUES (?, ?, ?)", (name, *stamp))
                report["files_imported"] += 1
            except (OSError, ValueError, TypeError):
                report["unreadable_files"] += 1
        return report
