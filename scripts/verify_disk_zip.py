"""Isolated actual legacy-package executor measurement; no application DB/data."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
import psutil
from backend.platform import engine_task_executor as executor
from backend.core.safe_filesystem import file_identity

size = int(sys.argv[1]) * 1024 ** 3
process = psutil.Process()
baseline = process.memory_info().rss
peak = [baseline]
done = threading.Event()
def sample():
    while not done.wait(.01):
        peak[0] = max(peak[0], process.memory_info().rss)
thread = threading.Thread(target=sample)
thread.start()
try:
    with tempfile.TemporaryDirectory(prefix='narrify-zip-probe-') as root:
        root = Path(root)
        source = root / 'chapter.mp3'
        with source.open('wb') as writer:
            writer.truncate(size)
        record = {'identity': list(file_identity(source.stat()))}
        start = time.monotonic()
        with patch.object(executor, '_validate_deliveries', lambda *_: {source.name: record}), \
             patch.object(executor, 'get_or_prepare_layout', lambda: SimpleNamespace(workspace=root)), \
             patch.object(executor, 'task_outcome_file', lambda *_: nullcontext(root / 'book.zip')), \
             patch.object(executor, 'cancellation_requested', lambda *_: False), \
             patch.object(executor, 'update_progress', lambda *_: None):
            result = executor._run_audio_zip(SimpleNamespace(progress_percent=lambda *_: None), None,
                {'base': 'book', 'files': [{'relative_path': source.name}]}, [], [])
        elapsed = time.monotonic() - start
        with zipfile.ZipFile(result.temp_path) as archive:
            member = archive.getinfo(source.name)
            assert member.file_size == size
            assert member.extract_version >= 45
            assert archive.testzip() is None
        digest = hashlib.sha256()
        with result.temp_path.open('rb') as reader:
            while chunk := reader.read(1024 * 1024):
                digest.update(chunk)
        assert result.sha256 == digest.hexdigest()
        delta = peak[0] - baseline
        assert delta <= 128 * 1024 ** 2, delta
        print(json.dumps({'source_GiB': size // 1024 ** 3, 'zip_bytes': result.size_bytes,
              'execution_seconds': elapsed, 'extra_peak_RSS_bytes': delta,
              'CRC_verified': True, 'SHA256_verified': True}), flush=True)
finally:
    done.set()
    thread.join()
