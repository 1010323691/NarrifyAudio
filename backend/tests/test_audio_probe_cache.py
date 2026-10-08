"""Stable durable probe results, real process sharing and failure invalidation."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import wave

import pytest
from backend.core import audio_probe_cache as cache


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")
    tool = tmp_path / "ffprobe"
    tool.write_bytes(b"tool")
    monkeypatch.setenv("NARRIFY_PROBE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(cache.shutil, "which", lambda command: str(tool))
    return source, tool


def test_stable_success_is_shared_by_audio_bgm_and_preview(inputs, monkeypatch):
    from backend.engines import audio, bgm
    from backend.api import tts
    source, _tool = inputs
    calls = []
    monkeypatch.setattr(audio, "_probe_duration_uncached", lambda *args: calls.append(True) or (2.123456, ""))
    assert audio.probe_duration(source) == (2.123456, "")
    assert bgm.probe_duration(source) == (2.123456, "")
    assert tts._cached_probe(source, "", source.stat().st_mtime_ns) == 2.123
    assert calls == [True]


def test_source_replacement_same_size_and_mtime_and_tool_change_invalidate(inputs):
    source, tool = inputs
    calls = []
    measure = lambda: calls.append(True) or (float(len(calls)), "")
    assert cache.cached_duration(source, "ffprobe", measure)[0] == 1
    stamp = source.stat()
    replacement = source.with_suffix(".tmp")
    replacement.write_bytes(b"other")
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    replacement.replace(source)
    assert source.stat().st_size == stamp.st_size
    assert cache.cached_duration(source, "ffprobe", measure)[0] == 2
    tool.write_bytes(b"new tool")
    assert cache.cached_duration(source, "ffprobe", measure)[0] == 3
    assert cache.cached_duration(source, "ffprobe", measure, parameters=("different",))[0] == 4
    assert len(calls) == 4


@pytest.mark.parametrize("result", [(float("nan"), "failure"), (float("inf"), ""), (0.0, ""), (-1.0, ""), (2.0, "error")])
def test_failures_and_invalid_durations_are_not_cached(inputs, result):
    source, _tool = inputs
    calls = []
    for _ in range(2):
        cache.cached_duration(source, "ffprobe", lambda: calls.append(True) or result)
    assert len(calls) == 2
    with sqlite3.connect(cache.cache_root() / "duration.sqlite") as db:
        assert db.execute("SELECT count(*) FROM probes").fetchone()[0] == 0


def test_source_change_during_probe_is_rejected_and_next_call_remeasures(inputs):
    source, _tool = inputs
    def changed():
        source.write_bytes(b"changed during probe")
        return 10.0, ""
    duration, error = cache.cached_duration(source, "ffprobe", changed)
    assert math.isnan(duration) and "已变化" in error
    assert cache.cached_duration(source, "ffprobe", lambda: (20.0, "")) == (20.0, "")


def test_single_flight_does_not_hold_sqlite_connection_during_measurement(inputs):
    source, _tool = inputs
    calls = []
    def measure():
        calls.append(True)
        # Another connection can acquire the write transaction while the probe
        # waits; there is no cache DB connection enclosing the child process.
        with sqlite3.connect(cache.cache_root() / "duration.sqlite", timeout=.1) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()
        time.sleep(.05)
        return 3.0, ""
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: cache.cached_duration(source, "ffprobe", measure), range(8)))
    assert results == [(3.0, "")] * 8 and len(calls) == 1
    assert len(list(cache.cache_root().glob("probe-*.lock"))) == 1


def test_corrupt_cache_is_quarantined_and_rebuilt_and_retention_is_bounded(inputs, monkeypatch):
    source, _tool = inputs
    root = cache.cache_root()
    root.mkdir()
    (root / "duration.sqlite").write_bytes(b"corrupt database")
    assert cache.cached_duration(source, "ffprobe", lambda: (3.0, "")) == (3.0, "")
    assert len(list(root.glob("duration.corrupt-*.sqlite"))) == 1
    assert cache.cached_duration(source, "ffprobe", lambda: pytest.fail("rebuilt success must be reused")) == (3.0, "")
    with sqlite3.connect(root / "duration.sqlite") as db:
        for i in range(5):
            db.execute("INSERT INTO probes VALUES (?,?,?)", (f"extra-{i}", 1.0, time.time()))
        db.execute("INSERT INTO probes VALUES (?,?,?)", ("expired", 1.0, 0))
    monkeypatch.setattr(cache, "MAX_ENTRIES", 3)
    cache.prune_cache()
    with sqlite3.connect(root / "duration.sqlite") as db:
        assert db.execute("SELECT count(*) FROM probes").fetchone()[0] == 3
    with closing(cache._connect(root)) as db:
        assert db.execute("PRAGMA max_page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0] <= 32 * 1024 * 1024


def test_measurement_exception_is_not_repeated_as_cache_fallback(inputs):
    source, _tool = inputs
    calls = []
    def failed():
        calls.append(True)
        raise OSError("child cannot start")
    with pytest.raises(OSError, match="child cannot start"):
        cache.cached_duration(source, "ffprobe", failed)
    assert calls == [True]


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="real ffprobe is required")
def test_real_ffprobe_runs_once_across_four_processes_and_restart(tmp_path):
    source = tmp_path / "actual.wav"
    with wave.open(str(source), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x00" * 24000)
    code = '''import json,sys,time
from pathlib import Path
from backend.core.audio_probe_cache import cached_duration
from backend.engines.audio import _probe_duration_uncached
source,counter=sys.argv[1:]
def measure():
    with Path(counter).open("a") as output: output.write("probe\\n")
    time.sleep(.1)
    return _probe_duration_uncached(source)
print(json.dumps(cached_duration(source,"ffprobe",measure)))
'''
    env = {**os.environ, "NARRIFY_PROBE_CACHE_DIR": str(tmp_path / "persistent")}
    counter = tmp_path / "counter"
    command = [sys.executable, "-c", code, str(source), str(counter)]
    processes = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        assert json.loads(stdout) == [1.0, ""]
    restarted = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert restarted.returncode == 0, restarted.stderr
    assert json.loads(restarted.stdout) == [1.0, ""]
    assert counter.read_text().splitlines() == ["probe"]


def test_empty_cache_override_uses_default_and_parameter_changes_invalidate(inputs, monkeypatch, tmp_path):
    source, _tool = inputs
    monkeypatch.setenv("NARRIFY_PROBE_CACHE_DIR", "")
    monkeypatch.setattr(cache, "PROJECT_ROOT", tmp_path)
    assert cache.cache_root() == tmp_path / ".narrify" / "audio-probes"
    calls = []
    measure = lambda: calls.append(True) or (1.0, "")
    cache.cached_duration(source, "ffprobe", measure)
    monkeypatch.setattr(cache, "PARAMETERS", ("new parameters",))
    cache.cached_duration(source, "ffprobe", measure)
    assert len(calls) == 2


def test_unusable_cache_configuration_falls_back_without_failing_probe(inputs, monkeypatch):
    source, _tool = inputs
    monkeypatch.setattr(cache, "_safe_root", lambda: (_ for _ in ()).throw(RuntimeError("unavailable cache root")))
    calls = []
    assert cache.cached_duration(source, "ffprobe", lambda: calls.append(True) or (4.0, "")) == (4.0, "")
    assert calls == [True]
