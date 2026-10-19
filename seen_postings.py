"""Persistent, local identity index for successfully saved postings."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from posting_identity import posting_key
from description_quality import annotate


class SeenPostings:
    UPSERT = """INSERT INTO seen_postings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, posting_id) DO UPDATE SET
        link=excluded.link, title=excluded.title, company=excluded.company,
        description_status=excluded.description_status, record_json=excluded.record_json,
        last_collected_at=excluded.last_collected_at
        WHERE seen_postings.description_status != 'complete_candidate'
           OR (excluded.description_status='complete_candidate' AND
               COALESCE(excluded.last_collected_at,'') > COALESCE(seen_postings.last_collected_at,''))"""
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        if self.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='seen_postings'").fetchone() and self.connection.execute("PRAGMA user_version").fetchone()[0] < 2:
            backup = path.with_name(path.stem + "_before_quality_v2.sqlite3")
            if not backup.exists():
                destination = sqlite3.connect(backup)
                try:
                    self.connection.backup(destination)
                finally:
                    destination.close()
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
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(seen_postings)")}
        with self.connection:
            for name, definition in (("description_status", "TEXT NOT NULL DEFAULT 'legacy_unknown'"),
                                     ("record_json", "TEXT"), ("last_collected_at", "TEXT")):
                if name not in columns:
                    self.connection.execute(f"ALTER TABLE seen_postings ADD COLUMN {name} {definition}")
            if self.connection.execute("PRAGMA user_version").fetchone()[0] < 2:
                # Re-read existing JSON once to assess legacy entries; never delete identities.
                self.connection.execute("DELETE FROM imported_files")
                self.connection.execute("PRAGMA user_version=2")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self):
        self.connection.close()

    def contains(self, url):
        key = posting_key(url)
        return bool(key and self.connection.execute(
            "SELECT 1 FROM seen_postings WHERE source=? AND posting_id=? AND description_status IN ('complete_candidate','legacy_unknown')", key).fetchone())

    def was_seen(self, url):
        key = posting_key(url)
        return bool(key and self.connection.execute(
            "SELECT 1 FROM seen_postings WHERE source=? AND posting_id=?", key).fetchone())

    def retry_candidates(self, source, limit=1000):
        rows = self.connection.execute(
            "SELECT record_json FROM seen_postings WHERE source=? AND description_status IN ('suspected_truncated','missing') AND record_json IS NOT NULL ORDER BY first_seen_at LIMIT ?",
            (source, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def quality_counts(self):
        return dict(self.connection.execute("SELECT description_status, COUNT(*) FROM seen_postings GROUP BY description_status"))

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
        record = annotate(dict(record))
        return (*key, record["link"], record["title"], str(record.get("company") or ""),
                record.get("collected_at") or datetime.now(timezone.utc).isoformat(), str(output), record["description_status"], json.dumps(record, ensure_ascii=False),
                record.get("collected_at") or datetime.now(timezone.utc).isoformat())

    def remember(self, record, output):
        row = self.row(record, output)
        if row is None:
            return False
        existed = self.was_seen(record["link"])
        with self.connection:
            self.connection.execute(self.UPSERT, row)
        return not existed

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
                before = self.count()
                with self.connection:
                    self.connection.executemany(self.UPSERT, rows)
                    report["postings_added"] += self.count() - before
                    self.connection.execute("INSERT OR REPLACE INTO imported_files VALUES (?, ?, ?)", (name, *stamp))
                report["files_imported"] += 1
            except (OSError, ValueError, TypeError):
                report["unreadable_files"] += 1
        return report
