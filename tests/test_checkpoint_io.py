import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reader


class CheckpointIOTests(unittest.TestCase):
    def test_temporary_windows_lock_recovers_without_truncating_target(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as directory:
            output = Path(directory) / "jobs.json"
            reader.save_result(output, {"jobs": [{"title": "Old"}]})
            before = output.read_bytes()
            replace = Path.replace
            attempts = []
            def temporarily_locked(source, target):
                attempts.append(source)
                if len(attempts) <= 2:
                    self.assertEqual(output.read_bytes(), before)
                    raise PermissionError("WinError 5")
                return replace(source, target)
            with patch.object(Path, "replace", temporarily_locked), patch.object(reader.time, "sleep") as sleep:
                reader.save_result(output, {"jobs": [{"title": "New"}]})
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["jobs"][0]["title"], "New")
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_persistent_lock_preserves_old_file_and_recovery_snapshot(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as directory:
            output = Path(directory) / "jobs.json"
            reader.save_result(output, {"jobs": [{"title": "Old"}]})
            before = output.read_bytes()
            with patch.object(Path, "replace", side_effect=PermissionError("locked")) as replace, \
                    patch.object(reader.time, "sleep"), self.assertRaisesRegex(PermissionError, "snapshot retained"):
                reader.save_result(output, {"jobs": [{"title": "New"}]})
            self.assertEqual(replace.call_count, 7)
            self.assertEqual(output.read_bytes(), before)
            snapshots = list(Path(directory).glob("*.tmp"))
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(json.loads(snapshots[0].read_text(encoding="utf-8"))["jobs"][0]["title"], "New")

    def test_failed_fsync_preserves_target_and_discards_partial_snapshot(self):
        with tempfile.TemporaryDirectory(dir=reader.BASE_DIR) as directory:
            output = Path(directory) / "jobs.json"
            reader.save_result(output, {"jobs": []})
            before = output.read_bytes()
            with patch.object(reader.os, "fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
                reader.save_result(output, {"jobs": [{"title": "New"}]})
            self.assertEqual(output.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
