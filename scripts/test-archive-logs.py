"""Run with .venv/bin/python scripts/test-archive-logs.py; uses temporary data only."""
from contextlib import redirect_stdout
import gzip
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_logs as logs


class LogArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "user" / "项目" / "logs"
        self.directory.mkdir(parents=True)

    def log(self, name="tts_batch_old.log", content=b"repeated diagnostic\n" * 10000):
        path = self.directory / name
        path.write_bytes(content)
        os.utime(path, (time.time() - 7200,) * 2)
        info = path.stat()
        return logs.Candidate(path, self.root, logs.identity(info), logs.allocated_bytes(info))

    def scan(self, busy=None):
        folders = logs.log_directories(self.root, "user", None)
        return list(logs.candidates(self.root, folders, cutoff=time.time() - 3600,
                                    min_bytes=1024, busy=busy or set()))

    def test_verified_archive_restores_exact_bytes_and_reports_disk_saving(self):
        item = self.log()
        before = item.path.read_bytes()
        saved, _ = logs.archive(item, lambda: set())
        target = item.path.with_name(item.path.name + ".gz")
        self.assertEqual(gzip.decompress(target.read_bytes()), before)
        self.assertFalse(item.path.exists())
        self.assertEqual(saved, item.allocated - logs.allocated_bytes(target.stat()))
        self.assertFalse(list(self.directory.glob(".archive-log-*")))

    def test_dry_run_does_not_write_or_delete(self):
        item = self.log()
        before = item.path.read_bytes()
        with patch.object(logs, "open_logs", return_value=set()), redirect_stdout(io.StringIO()):
            self.assertEqual(logs.main(["--root", str(self.root), "--username", "user"]), 0)
        self.assertEqual(item.path.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [item.path])

    def test_current_logs_recent_files_audio_and_recovery_files_are_excluded(self):
        old = self.log()
        live = self.log("app.log")
        opened = self.log("busy.log")
        recent = self.log("recent.log")
        recent.path.touch()
        for name in ["publication.json", "publication-backup-0.bin", "audio.mp3", "archived.log.gz"]:
            self.log(name)
        self.assertEqual([item.path for item in self.scan({logs.path_key(opened.path)})], [old.path])
        self.assertTrue(live.path.exists())

    def test_busy_file_is_not_archived(self):
        item = self.log()
        self.assertEqual(logs.archive(item, lambda: {logs.path_key(item.path)})[0], 0)
        self.assertTrue(item.path.exists())

    def test_existing_archive_is_never_overwritten(self):
        item = self.log()
        target = item.path.with_name(item.path.name + ".gz")
        target.write_bytes(b"existing archive")
        self.assertEqual(logs.archive(item, lambda: set())[0], 0)
        self.assertEqual(target.read_bytes(), b"existing archive")
        self.assertTrue(item.path.exists())

    def test_change_during_compression_retains_source(self):
        item = self.log()
        calls = 0
        def busy():
            nonlocal calls
            calls += 1
            if calls == 2:
                item.path.write_bytes(b"new content")
            return set()
        self.assertEqual(logs.archive(item, busy)[0], 0)
        self.assertEqual(item.path.read_bytes(), b"new content")
        self.assertFalse(item.path.with_name(item.path.name + ".gz").exists())

    def test_open_during_archive_install_removes_archive_and_preserves_source(self):
        item = self.log()
        calls = 0
        def busy():
            nonlocal calls
            calls += 1
            return {logs.path_key(item.path)} if calls == 3 else set()
        self.assertEqual(logs.archive(item, busy)[0], 0)
        self.assertTrue(item.path.exists())
        self.assertFalse(item.path.with_name(item.path.name + ".gz").exists())
        self.assertFalse(list(self.directory.glob(".archive-log-*")))

    def test_compression_verification_failure_retains_source(self):
        item = self.log()
        with patch.object(logs.gzip, "open", return_value=io.BytesIO(b"corrupt output")):
            with self.assertRaises(OSError):
                logs.archive(item, lambda: set())
        self.assertTrue(item.path.exists())
        self.assertFalse(list(self.directory.glob(".archive-log-*")))

    def test_uncompressible_file_is_kept(self):
        item = self.log(content=os.urandom(200000))
        self.assertEqual(logs.archive(item, lambda: set())[0], 0)
        self.assertTrue(item.path.exists())

    def test_file_and_directory_links_are_not_followed(self):
        item = self.log()
        try:
            (self.directory / "linked.log").symlink_to(item.path)
            (self.root / "other").symlink_to(self.root / "user", target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertEqual([entry.path for entry in self.scan()], [item.path])
        self.assertEqual(list(logs.log_directories(self.root, "other", None)), [])

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            list(logs.log_directories(self.root, "../outside", None))

    def test_actual_open_file_is_detected(self):
        item = self.log()
        with item.path.open("rb"):
            self.assertIn(logs.path_key(item.path), logs.open_logs())


if __name__ == "__main__":
    unittest.main()
