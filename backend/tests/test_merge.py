"""Offline tests for the two-stage merge orchestration (``backend/engines/merge.py``).

``run()`` hands the ordered per-segment manifest to the isolated worker — stubbed
here, mirroring the test_tts_batch.py pattern (monkeypatched resolve_engine /
run_worker, no real shared .venv subprocess) — and owns the staging dir's cleanup. The
worker's two-stage part-merge behaviour itself is pinned by the pure-function tests
in test_tts_worker.py plus manual runs against the real shared .venv.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

import pytest

from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core.concurrency import ConcurrencyGate
import backend.engines.merge as merge


class _Handle:
    """A minimal TaskHandle stand-in: records log/progress, never cancels or pauses."""

    def __init__(self):
        self.logs = []
        self.progresses = []
        self.cancelled = False  # stop_check lambda (queue-cancel) reads this

    def log(self, msg, level="INFO"):
        self.logs.append((level, msg))

    def progress(self, frac, current=""):
        self.progresses.append((frac, current))

    def check(self):
        pass


@pytest.fixture(autouse=True)
def fresh_gate(monkeypatch):
    """A fresh merge gate per test (injected in place of the process-wide singleton) so
    one test's held / leaked slot can never bleed into another test's ``run()``."""
    gate = ConcurrencyGate()
    monkeypatch.setattr(merge, "merge_gate", lambda: gate)
    yield gate


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """A throwaway project root + workspace (no parsed script — merge reads the
    batch manifest directly, seeded per test)."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    core_config.set_workspace_pointer(str(ws))
    yield ws
    core_config.reset_config_cache()


def _seed_manifest(ws, n, package="pkg", missing=(), top_level=False):
    """A batch manifest of n ok segments; files for non-missing indices are created."""
    pkg_dir = ws / "05_audio_chunk" if top_level else ws / "05_audio_chunk" / package
    pkg_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for i in range(n):
        f = pkg_dir / f"{i:04d}.mp3"
        if i not in missing:
            f.write_bytes(b"0" * 32)  # a dummy segment file
        entries.append({
            "index": i,
            "speaker": "A" if i % 2 == 0 else "B",
            "text": f"t{i}",
            "pause_after": 900 if i == 1 else None,
            "path": str(f),
            "ok": True,
            "reason": "",
        })
    (pkg_dir / "manifest.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return entries


def _fake_run_worker(captured, behaviour="ok"):
    """A run_worker stand-in: records the cmd and simulates the child's [result]
    (ok: the mp3 at --out; wav-fallback: the whole-book WAV inside --tmp-dir)."""

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        if behaviour == "fail":
            raise RuntimeError("engine exploded")
        out = cmd[cmd.index("--out") + 1]
        if behaviour == "wav-fallback":
            tmp_dir = cmd[cmd.index("--tmp-dir") + 1]
            path = os.path.join(tmp_dir, os.path.splitext(os.path.basename(out))[0] + ".wav")
        else:
            path = out
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"0" * 2048)
        on_line(f"[result] {path}")
        if behaviour == "plain-lines":
            on_line("一条普通日志行")
        return deque()

    return run_worker


def _stub_engine(monkeypatch, captured, behaviour="ok"):
    """Point the engine at fakes so no real shared .venv subprocess is spawned."""
    monkeypatch.setattr(merge, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(merge, "run_tts_subprocess", _fake_run_worker(captured, behaviour))


def _cmd_flag(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _log_msgs(handle):
    return [msg for _level, msg in handle.logs]


# --------------------------------------------------------------------------- #
# run() — the two-stage command + staging dir lifecycle
# --------------------------------------------------------------------------- #

def test_run_cmd_includes_tmp_dir_and_batch_size(workspace, monkeypatch):
    _seed_manifest(workspace, 250)
    captured = {}
    _stub_engine(monkeypatch, captured)
    merge.merge_audio_package(_Handle(), False, "pkg")
    tmp_dir = _cmd_flag(captured["cmd"], "--tmp-dir")
    assert tmp_dir.startswith(str(workspace / "00_temp") + os.sep)
    assert _cmd_flag(captured["cmd"], "--merge-batch-size") == "100"
    # the final-encode thread bound is always sent (flag+value pair; value >= 1)
    assert int(_cmd_flag(captured["cmd"], "--threads")) == merge.thread_budget()


def test_run_logs_two_stage_plan(workspace, monkeypatch):
    _seed_manifest(workspace, 250)
    captured = {}
    _stub_engine(monkeypatch, captured)
    handle = _Handle()
    merge.merge_audio_package(handle, False, "pkg")
    assert "两阶段合并：250 段 → 3 批（每批 100 段）→ 整书" in _log_msgs(handle)

    # a single batch (<= MERGE_BATCH_SIZE segments) needs no plan line
    _seed_manifest(workspace, 50, package="small")
    handle2 = _Handle()
    merge.merge_audio_package(handle2, False, "small")
    assert not any("两阶段合并" in m for m in _log_msgs(handle2))


def test_run_success_result(workspace, monkeypatch):
    _seed_manifest(workspace, 120)
    captured = {}
    _stub_engine(monkeypatch, captured)
    handle = _Handle()
    result = merge.merge_audio_package(handle, False, "pkg")
    assert result["file"] == "pkg.mp3"
    assert result["path"] == str(workspace / "06_audio_merge" / "pkg.mp3")
    assert result["segments"] == 120
    assert result["size"] == 2048
    assert Path(result["path"]).exists()
    assert handle.progresses and handle.progresses[-1][0] == 1.0


def test_run_cleans_tmp_dir_on_success(workspace, monkeypatch):
    _seed_manifest(workspace, 120)
    captured = {}
    _stub_engine(monkeypatch, captured)
    merge.merge_audio_package(_Handle(), False, "pkg")
    assert list((workspace / "00_temp").glob("merge_tmp_*")) == []


def test_run_cleans_tmp_dir_on_failure(workspace, monkeypatch):
    _seed_manifest(workspace, 120)
    captured = {}
    _stub_engine(monkeypatch, captured, behaviour="fail")
    with pytest.raises(RuntimeError):
        merge.merge_audio_package(_Handle(), False, "pkg")
    assert list((workspace / "00_temp").glob("merge_tmp_*")) == []


def test_run_relocates_wav_fallback(workspace, monkeypatch):
    _seed_manifest(workspace, 120)
    captured = {}
    _stub_engine(monkeypatch, captured, behaviour="wav-fallback")
    result = merge.merge_audio_package(_Handle(), False, "pkg")
    target = workspace / "06_audio_merge" / "pkg.wav"
    assert result["file"] == "pkg.wav"
    assert result["path"] == str(target)
    assert target.exists()
    assert list((workspace / "00_temp").glob("merge_tmp_*")) == []


# --------------------------------------------------------------------------- #
# run() — error paths & log plumbing (no regression)
# --------------------------------------------------------------------------- #

def test_run_missing_manifest_raises(workspace, monkeypatch):
    captured = {}
    _stub_engine(monkeypatch, captured)
    with pytest.raises(RuntimeError, match="未找到合成结果清单"):
        merge.merge_audio_package(_Handle(), False, "nope")


def test_run_empty_manifest_raises(workspace, monkeypatch):
    (workspace / "05_audio_chunk" / "pkg").mkdir(parents=True, exist_ok=True)
    (workspace / "05_audio_chunk" / "pkg" / "manifest.json").write_text("[]", encoding="utf-8")
    captured = {}
    _stub_engine(monkeypatch, captured)
    with pytest.raises(RuntimeError, match="manifest.json 为空"):
        merge.merge_audio_package(_Handle(), False, "pkg")


def test_run_no_ok_segments_raises(workspace, monkeypatch):
    _seed_manifest(workspace, 3, missing={0, 1, 2})  # ok=True but every file missing
    captured = {}
    _stub_engine(monkeypatch, captured)
    with pytest.raises(RuntimeError, match="没有可合并的音频"):
        merge.merge_audio_package(_Handle(), False, "pkg")


def test_run_rejects_segments_rendered_with_old_voice(workspace, monkeypatch):
    entries = _seed_manifest(workspace, 2)
    entries[0]["voice_used"] = {"canonical": "A", "type": "clone"}
    (workspace / "05_audio_chunk" / "pkg" / "manifest.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8",
    )
    vc = workspace / "04_voice_profiles"
    vc.mkdir(parents=True, exist_ok=True)
    (vc / "voice_config.json").write_text(json.dumps({
        "A": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav"},
        "B": {"type": "clone", "ref_audio": "04_voice_profiles/b.wav"},
    }), encoding="utf-8")
    captured = {}
    _stub_engine(monkeypatch, captured)

    with pytest.raises(RuntimeError, match="角色声音已变更"):
        merge.merge_audio_package(_Handle(), False, "pkg")
    assert "cmd" not in captured


def test_run_rejects_legacy_segments_when_voice_config_exists(workspace, monkeypatch):
    _seed_manifest(workspace, 2)
    vc = workspace / "04_voice_profiles"
    vc.mkdir(parents=True, exist_ok=True)
    (vc / "voice_config.json").write_text(json.dumps({
        "A": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav"},
        "B": {"type": "clone", "ref_audio": "04_voice_profiles/b.wav"},
    }), encoding="utf-8")
    captured = {}
    _stub_engine(monkeypatch, captured)

    with pytest.raises(RuntimeError, match="角色声音已变更"):
        merge.merge_audio_package(_Handle(), False, "pkg")
    assert "cmd" not in captured


def test_run_skips_missing_files_with_warning(workspace, monkeypatch):
    _seed_manifest(workspace, 5, missing={1, 3})
    captured = {}
    _stub_engine(monkeypatch, captured)
    handle = _Handle()
    result = merge.merge_audio_package(handle, False, "pkg")
    assert result["segments"] == 3
    assert any(level == "WARNING" and "2 段成功记录的文件缺失" in msg
               for level, msg in handle.logs)


def test_run_legacy_output_name(workspace, monkeypatch):
    _seed_manifest(workspace, 3, top_level=True)
    captured = {}
    _stub_engine(monkeypatch, captured)
    result = merge.merge_audio_package(_Handle())  # no package -> most recent / legacy top-level
    assert result["file"] == "cloned_audiobook.mp3"


def test_run_plain_lines_go_to_log(workspace, monkeypatch):
    _seed_manifest(workspace, 3)
    captured = {}
    _stub_engine(monkeypatch, captured, behaviour="plain-lines")
    handle = _Handle()
    merge.merge_audio_package(handle, False, "pkg")
    assert "一条普通日志行" in _log_msgs(handle)


# --------------------------------------------------------------------------- #
# run() — the process-wide merge gate (batch-merge concurrency scope)
# --------------------------------------------------------------------------- #

def test_run_gate_acquired_after_fast_fail(workspace, monkeypatch, fresh_gate):
    """A fast-fail validation must not block on — or take — a merge slot: with the only
    slot held elsewhere, a missing manifest still fails immediately and leaves the
    gate untouched (the acquire sits after every fast-fail, before any file is written)."""
    fresh_gate.acquire()  # an outside holder takes the only slot
    try:
        captured = {}
        _stub_engine(monkeypatch, captured)
        t0 = time.monotonic()
        with pytest.raises(RuntimeError, match="未找到合成结果清单"):
            merge.merge_audio_package(_Handle(), False, "nope")
        assert time.monotonic() - t0 < 1.0  # did not sit down waiting for the slot
        assert fresh_gate.active == 1  # unchanged — never acquired
    finally:
        fresh_gate.release()


def test_run_spawns_only_after_slot(workspace, monkeypatch, fresh_gate):
    """With the only slot held, a valid merge waits at the gate: nothing is spawned
    (no engine, no staging file) until the slot frees — then it runs to completion and
    releases the slot again (balanced)."""
    _seed_manifest(workspace, 30)
    fresh_gate.acquire()  # an outside holder takes the only slot
    captured = {}
    _stub_engine(monkeypatch, captured)
    handle = _Handle()
    t = threading.Thread(target=merge.merge_audio_package, args=(handle, False, "pkg"), daemon=True)
    t.start()
    time.sleep(0.5)  # plenty of cooperative-poll cycles for the run to reach the gate
    assert "cmd" not in captured  # nothing spawned while the slot is held
    fresh_gate.release()  # frees the slot -> the queued run acquires it and spawns
    t.join(timeout=5)
    assert not t.is_alive()
    assert "cmd" in captured  # spawned only after the slot freed
    assert (workspace / "06_audio_merge" / "pkg.mp3").exists()
    assert fresh_gate.active == 0  # the run released its slot on the way out


def test_run_release_balanced(workspace, monkeypatch, fresh_gate):
    """Every exit path releases the slot it took exactly once (no leak, no underflow)."""
    _seed_manifest(workspace, 10)
    _stub_engine(monkeypatch, {})
    merge.merge_audio_package(_Handle(), False, "pkg")
    assert fresh_gate.active == 0  # success path balances
    _stub_engine(monkeypatch, {}, behaviour="fail")
    with pytest.raises(RuntimeError):
        merge.merge_audio_package(_Handle(), False, "pkg")
    assert fresh_gate.active == 0  # engine-failure path balances too


def test_run_cancel_while_queued_zero_output(workspace, monkeypatch, fresh_gate):
    """Cancel while waiting for the slot: aborts within one poll cycle (<= ~1s) WITHOUT
    taking the slot, without spawning the engine, and with zero file residue."""
    _seed_manifest(workspace, 30)
    fresh_gate.acquire()  # an outside holder keeps the only slot
    try:
        captured = {}
        _stub_engine(monkeypatch, captured)
        handle = _Handle()

        def worker():
            try:
                merge.merge_audio_package(handle, False, "pkg")
            except Exception as e:  # noqa: BLE001 — a daemon thread swallows it; record
                handle.error = e

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        time.sleep(0.3)
        handle.cancelled = True
        t.join(timeout=2)
        assert not t.is_alive()
        assert isinstance(getattr(handle, "error", None), merge.TaskCancelled)
        assert "cmd" not in captured  # never spawned
        assert fresh_gate.active == 1  # the outside holder's slot was never touched
        assert list((workspace / "00_temp").glob("merge_segments_*")) == []
        assert list((workspace / "00_temp").glob("merge_tmp_*")) == []
        out = workspace / "06_audio_merge"
        assert not out.exists() or list(out.iterdir()) == []
    finally:
        fresh_gate.release()


def test_run_success_leaves_no_wav_in_output(workspace, monkeypatch, fresh_gate):
    """Requirement pin: the merge result is MP3 directly — the two-stage whole-book WAV
    lives only in the 00_temp staging dir and dies with it; no big WAV lingers in
    06_audio_merge on the success path (the encode-failure WAV is the only kept one)."""
    _seed_manifest(workspace, 120)
    _stub_engine(monkeypatch, {})  # behaviour="ok" -> MP3 written at --out
    result = merge.merge_audio_package(_Handle(), False, "pkg")
    assert result["file"].endswith(".mp3")
    assert list((workspace / "06_audio_merge").glob("*.wav")) == []
    assert list((workspace / "00_temp").glob("merge_tmp_*")) == []


def test_concurrency_limit_formula(monkeypatch):
    """Parallelism = logical CPU count / 2, clamped to [1, 4] (constant policy)."""
    for cores, expected in ((2, 1), (3, 1), (4, 2), (8, 4), (16, 4), (64, 4), (None, 2)):
        monkeypatch.setattr(merge.os, "cpu_count", lambda c=cores: c)
        assert merge.concurrency_limit() == expected


def test_thread_budget_formula(monkeypatch):
    """Per-ffmpeg threads = max(1, (cpu // 2) // limit): the whole gate (mix + merge
    share it) occupies at most half the cores, never fewer than 1 thread."""
    cases = (
        # (cpu, limit, expected)
        (8, 4, 1), (16, 4, 2), (4, 2, 1), (2, 1, 1), (1, 1, 1), (32, 4, 4),
        (3, 2, 0),   # 3//2=1 // 2 = 0 -> clamped to 1
        (5, 1, 2),   # 5//2=2 // 1 = 2
    )
    for cpu, limit, expected in cases:
        got = merge.thread_budget(limit=limit, cpu=cpu)
        assert got == max(1, expected), (cpu, limit, got)
    # degenerate limit values clamp to 1 (no zero-division / zero-threads)
    assert merge.thread_budget(limit=0, cpu=8) == merge.thread_budget(limit=1, cpu=8)
    assert merge.thread_budget(limit=-3, cpu=8) >= 1
    # default args = live os.cpu_count() policy: >= 1 and == the explicit formula
    for cores in (2, 8, 16):
        with monkeypatch.context() as m:
            m.setattr(merge.os, "cpu_count", lambda c=cores: c)
            assert merge.thread_budget() == max(1, (cores // 2) // merge.concurrency_limit())
