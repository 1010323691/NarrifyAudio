"""Offline tests for the batch TTS segment ordering (``backend/engines/tts_batch.py``)
and the voice-config usability check (``backend/api/tts.py``).

The segment list is what the worker synthesizes and what Merge later reorders, so its
index assignment, ordering, empty-text skipping and index filtering are load-bearing
invariants worth pinning. ``_voice_usable`` decides the ready/pending badge shown per
character on the 角色配音 page.
"""
from __future__ import annotations

from backend.api.tts import _voice_usable
from backend.engines.tts_batch import build_segments


def _entry(speaker: str, text: str, instruct: str = "", pause_after=None, **extra):
    e = {"speaker": speaker, "text": text, "instruct": instruct}
    if pause_after is not None:
        e["pause_after"] = pause_after
    e.update(extra)
    return e


# --------------------------------------------------------------------------- #
# build_segments — ordering & index assignment
# --------------------------------------------------------------------------- #

def testbuild_segments_in_json_order():
    script = [_entry("A", "a"), _entry("B", "b"), _entry("A", "c")]
    segs = build_segments(script)
    assert [s["index"] for s in segs] == [0, 1, 2]
    assert [s["speaker"] for s in segs] == ["A", "B", "A"]
    assert [s["text"] for s in segs] == ["a", "b", "c"]


def testbuild_segments_index_is_full_position():
    # A filtered run must keep each segment's index = its position in the full script.
    script = [_entry("A", "a"), _entry("B", "b"), _entry("C", "c")]
    segs = build_segments(script, indices=[2, 0])
    assert [s["index"] for s in segs] == [0, 2]  # re-sorted back to JSON order
    assert [s["speaker"] for s in segs] == ["A", "C"]


def testbuild_segments_skips_empty_text():
    script = [_entry("A", "  "), _entry("B", "b"), _entry("C", "")]
    segs = build_segments(script)
    assert [s["index"] for s in segs] == [1]
    assert segs[0]["speaker"] == "B"


def testbuild_segments_ignores_unknown_indices():
    script = [_entry("A", "a"), _entry("B", "b")]
    assert build_segments(script, indices=[5, 99]) == []


def testbuild_segments_falls_back_to_type_for_speaker():
    # A script entry with only ``type`` (no ``speaker``) still yields a speaker label.
    script = [{"type": "NARRATOR", "text": "n", "instruct": ""}]
    segs = build_segments(script)
    assert segs[0]["speaker"] == "NARRATOR"


def testbuild_segments_copies_pause_after_and_instruct():
    script = [_entry("A", "a", instruct="warm", pause_after=900)]
    segs = build_segments(script)
    assert segs[0]["instruct"] == "warm"
    assert segs[0]["pause_after"] == 900


def testbuild_segments_empty_script():
    assert build_segments([]) == []


# --------------------------------------------------------------------------- #
# _voice_usable
# --------------------------------------------------------------------------- #

def test_voice_usability_contract():
    for args, expected in [
        (({'type': 'clone', 'ref_audio': '/x/preview.wav'},), True),
        (({'type': 'clone'},), False),
        (({'type': 'design', 'description': 'a warm voice'},), True),
        (({'type': 'design', 'description': '   '},), False),
        (({'type': 'custom'},), True),
        (({'type': 'lora'},), False),
        (({},), False),
    ]:
        assert _voice_usable(*args) is expected, args


# --------------------------------------------------------------------------- #
# synthesize → the worker command carries the (clamped) --concurrency
# --------------------------------------------------------------------------- #

import json  # noqa: E402
import os  # noqa: E402
from collections import deque  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from backend.core import config as core_config  # noqa: E402
from backend.core import paths as core_paths  # noqa: E402
import backend.engines.tts_batch as tts_batch  # noqa: E402
from backend.engines.tts import WorkerWatchdogTimeout  # noqa: E402


class _Handle:
    """A minimal TaskHandle stand-in: records log/progress/segment stats, never cancels or pauses."""

    def __init__(self):
        self.logs = []
        self.progresses = []
        self.stats = []  # (done, total, chars_done, chars_total) — every segment_stats call

    def log(self, msg, level="INFO"):
        self.logs.append((level, msg))

    def progress(self, frac, current=""):
        self.progresses.append((frac, current))

    def check(self):
        pass

    def segment_stats(self, done, total, chars_done, chars_total):
        self.stats.append((done, total, chars_done, chars_total))


def test_segment_publication_moves_audio_to_workspace_and_records_final_path(tmp_path):
    class _JournalHandle(_Handle):
        def publish_workspace_stage(self, final_path, staged_path):
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_path, final_path)

    stage_dir = tmp_path / "attempt" / "staged"
    final_dir = tmp_path / "workspace" / "05_audio_chunk" / "book"
    stage_dir.mkdir(parents=True)
    staged_audio = stage_dir / "0001.mp3"
    staged_audio.write_bytes(b"audio")
    results = {}

    outcome = tts_batch._handle_segment(
        f"[segment] 0 ok {staged_audio}", {0: {"speaker": "A"}}, 1,
        results, _JournalHandle(), stage_dir, final_dir,
    )

    assert outcome["ok"] is True
    assert results[0]["path"] == str(final_dir / "0001.mp3")
    assert (final_dir / "0001.mp3").read_bytes() == b"audio"
    assert not staged_audio.exists()


def test_manifest_writer_uses_workspace_publication_handle(tmp_path):
    writes = []

    class _JournalHandle(_Handle):
        def stage_workspace_file(self, final_path, data):
            writes.append((final_path, data))

    path = tmp_path / "manifest.json"
    manifest = [{"index": 0, "ok": True}]
    tts_batch.write_manifest_file(path, manifest, _JournalHandle())

    assert not path.exists()
    assert writes[0][0] == path
    assert json.loads(writes[0][1]) == manifest


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """A throwaway project root + workspace so get_or_prepare_layout()/resolve_parsed_json() resolve.

    Seeds one parsed script: two non-empty lines + one empty (which ``build_segments``
    skips). No voice_config is written — the run still completes because ``run_worker``
    is stubbed to report every segment ok.
    """
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    core_paths.reset_layout_cache()  # module state outlives the monkeypatched TEMPLATE_FILE
    ws = tmp_path / "Book"
    core_config.set_workspace_pointer(str(ws))
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    (ws / "03_parsed_json" / "s.json").write_text(
        json.dumps([
            {"speaker": "A", "text": "hello"},
            {"speaker": "B", "text": "world"},
            {"speaker": "A", "text": ""},  # empty -> skipped by build_segments
        ]),
        encoding="utf-8",
    )
    yield ws
    core_config.reset_config_cache()
    core_paths.reset_layout_cache()


def _fake_run_worker(captured):
    """A run_worker stand-in: records the cmd and simulates a fully-successful child."""

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    return run_worker


def _stub_engine(monkeypatch, captured):
    """Point the engine at fakes so no real shared .venv subprocess is spawned."""
    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", _fake_run_worker(captured))


def _cmd_flag(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_timeout_demotion_uses_actual_subbatch_size():
    for args, expected in [
        ((340, 96,), 48),
        ((4, 2,), 1),
        ((4, 0,), 2),
    ]:
        assert tts_batch.timeout_demotion_cap(*args) == expected, args


def test_oom_demotion_steps_below_actual_batch():
    for cap, rows, auto, expected in [
        (340, 224, True, 128), (340, 340, True, 272),
        (128, 96, True, 80), (80, 80, True, 40),
        (340, 2, True, 1), (80, 0, True, 40), (128, 80, False, 40),
    ]:
        assert tts_batch.oom_demotion_cap(cap, rows, auto) == expected, (cap, rows, auto, expected,)


@pytest.mark.parametrize("pooled", [False, True])
@pytest.mark.parametrize("auto", [False, True])
def test_oom_restart_keeps_completed_segments(workspace, monkeypatch, pooled, auto):
    if pooled:
        _seed_second_file(workspace)
    calls = []
    pending = []

    def run_worker(cmd, handle, on_line, **kw):
        assert kw["private_errors"] is True
        calls.append(cmd)
        rows = json.loads(Path(_cmd_flag(cmd, "--segments-file")).read_text("utf-8"))
        pending.append([row["index"] for row in rows])
        for row in rows[:1] if len(calls) == 1 else rows:
            out = Path(row.get("out_dir") or _cmd_flag(cmd, "--out-dir"))
            out.mkdir(parents=True, exist_ok=True)
            audio = out / f"{row.get('file_index', row['index']) + 1:04d}.mp3"
            audio.write_bytes(b"fake")
            on_line(f"[segment] {row['index']} ok {audio}")
        if len(calls) == 1:
            raise tts_batch.WorkerOutOfMemory(224)
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("python"), Path("worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    handle = _Handle()
    if pooled:
        tts_batch.synthesize_multi(handle, ["s.json", "t.json"], 128, auto_concurrency=auto)
    else:
        tts_batch.synthesize(handle, None, "s.json", 128, auto_concurrency=auto)
    assert [_cmd_flag(cmd, "--concurrency") for cmd in calls] == (
        ["340", "128"] if auto else ["128", "64"]
    )
    assert pending[1] == pending[0][1:]
    assert "--restore-stack" not in calls[1]
    assert _cmd_flag(calls[1], "--oom-restore-cap") == ("340" if auto else "128")
    assert any("正在自动重试" in msg for _, msg in handle.logs)


@pytest.mark.parametrize("pooled", [False, True])
def test_second_oom_after_restore_locks_task_to_reduced_cap(workspace, monkeypatch, pooled):
    calls = []
    completed = _fake_run_worker_pool([])

    def run_worker(cmd, handle, on_line, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            raise tts_batch.WorkerOutOfMemory(224)
        if len(calls) == 2:
            assert _cmd_flag(cmd, "--oom-restore-cap") == "340"
            on_line("[oom-restore] cap=340")
            raise tts_batch.WorkerOutOfMemory(224)
        assert "--oom-restore-cap" not in cmd
        return completed(cmd, handle, on_line, **kw)

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("python"), Path("worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    handle = _Handle()
    if pooled:
        _seed_second_file(workspace)
        tts_batch.synthesize_multi(handle, ["s.json", "t.json"], auto_concurrency=True)
    else:
        tts_batch.synthesize(handle, None, "s.json", auto_concurrency=True)
    assert [_cmd_flag(cmd, "--concurrency") for cmd in calls] == ["340", "128", "128"]
    assert any("本次任务保持降档执行" in msg for _, msg in handle.logs)


@pytest.mark.parametrize("pooled", [False, True])
def test_oom_at_single_row_stops_with_safe_message(workspace, monkeypatch, pooled):
    calls = []
    def run_worker(cmd, handle, on_line, **kw):
        calls.append(cmd)
        raise tts_batch.WorkerOutOfMemory(1)
    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("python"), Path("worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    with pytest.raises(RuntimeError, match="显存不足，已保留完成进度"):
        if pooled:
            tts_batch.synthesize_multi(_Handle(), ["s.json"], auto_concurrency=True)
        else:
            tts_batch.synthesize(_Handle(), None, "s.json", auto_concurrency=True)
    assert len(calls) == 1


def test_synthesize_passes_request_concurrency(workspace, monkeypatch):
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", 4)
    assert _cmd_flag(captured["cmd"], "--concurrency") == "4"


def test_synthesize_defaults_concurrency_from_config(workspace, monkeypatch):
    # concurrency=None -> the configured default (batch_concurrency = 80).
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", None)
    assert _cmd_flag(captured["cmd"], "--concurrency") == "80"


def test_synthesize_zero_concurrency_falls_back_to_default(workspace, monkeypatch):
    # 0 requests the config default (80), never a degenerate pool.
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", 0)
    assert _cmd_flag(captured["cmd"], "--concurrency") == "80"


def test_synthesize_clamps_concurrency_to_max(workspace, monkeypatch):
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", 999)
    assert _cmd_flag(captured["cmd"], "--concurrency") == "128"


def test_synthesize_clamps_concurrency_to_min(workspace, monkeypatch):
    # -5 is truthy (so it is used, not the default) but clamps down to 1.
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", -5)
    assert _cmd_flag(captured["cmd"], "--concurrency") == "1"


def test_synthesize_reports_effective_concurrency_in_log(workspace, monkeypatch):
    captured = {}
    _stub_engine(monkeypatch, captured)
    h = _Handle()
    tts_batch.synthesize(h, None, "s.json", 3)
    assert any("批内上限 3 段" in msg for _lvl, msg in h.logs)


# --------------------------------------------------------------------------- #
# the watchdog / restart loop — shrink, isolate, and the attempt cap
# --------------------------------------------------------------------------- #

def test_synthesize_watchdog_shrinks_and_restarts(workspace, monkeypatch):
    """A hung child (exit 124) shrinks the batch and restarts; a clean 2nd run finishes the job."""
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if len(calls) == 1:  # first run hangs: name the in-flight batch, then exit 124
            on_line("[watchdog] timeout batch=custom#1 indices=[0, 1] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        # second run is clean: synthesize whatever is still remaining
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "s.json", 4)
    assert len(calls) == 2  # one restart, not a fatal failure
    assert _cmd_flag(calls[0], "--concurrency") == "4"
    assert _cmd_flag(calls[1], "--concurrency") == "1"  # actual timed-out rows: 2 -> 1
    assert result["completed"] == 2 and result["failed"] == []  # the task still succeeds
    assert any("实际批内 2 段，下一次批内上限 1 段" in msg for _lvl, msg in h.logs)


def test_synthesize_watchdog_isolates_poison_segment_at_workers_one(workspace, monkeypatch):
    """At workers==1 a segment that times out twice is isolated as a failure; the rest complete."""
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if len(calls) in (1, 2):  # poison segment 0 times out twice (strike, then isolate)
            on_line("[watchdog] timeout batch=custom#1 indices=[0] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        # third run: only the healthy remaining segment (index 1)
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "s.json", 1)
    assert len(calls) == 3
    assert all(_cmd_flag(c, "--concurrency") == "1" for c in calls)  # never grows back
    assert result["completed"] == 1  # the healthy segment (index 1) completed
    assert len(result["failed"]) == 1 and result["failed"][0]["index"] == 0
    assert "隔离" in result["failed"][0]["reason"]  # the poison segment is a recorded failure
    # and it lands in the manifest as not-ok, so a later default resume would retry it
    by_index = {e["index"]: e for e in json.loads(
        (workspace / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))}
    assert by_index[0]["ok"] is False and by_index[1]["ok"] is True


def test_synthesize_watchdog_attempt_cap_raises(workspace, monkeypatch):
    """If the engine keeps timing out (and nothing is ever isolated), the run gives up -> FAILED."""
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        # Always time out, always naming an out-of-range index that can never be isolated,
        # so `remaining` never shrinks and the attempt cap is the only thing that ends the loop.
        calls.append(cmd)
        on_line("[watchdog] timeout batch=custom#1 indices=[99] elapsed=181.2")
        raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    with pytest.raises(RuntimeError):
        tts_batch.synthesize(h, None, "s.json", 1)
    assert len(calls) == 9  # MAX_ATTEMPTS (8) shrink-retries, then the 9th attempt trips the cap


# --------------------------------------------------------------------------- #
# --restore-stack — a timeout demotion records its chars; a smaller later
# sub-batch restores the pre-demotion gear (LIFO)
# --------------------------------------------------------------------------- #

def _seed_long_script(ws, name="long.json"):
    """1000 + 2000 char lines: the char metric (stripped text length) is pinned by real text."""
    (ws / "03_parsed_json" / name).write_text(json.dumps([
        {"speaker": "A", "text": "字" * 1000},
        {"speaker": "B", "text": "字" * 2000},
    ]), encoding="utf-8")


def _seed_four_segment_script(ws, name="long.json"):
    rows = [
        {"speaker": "A", "text": "x" * 1000},
        {"speaker": "B", "text": "x" * 2000},
        {"speaker": "C", "text": "x" * 3000},
        {"speaker": "D", "text": "x" * 4000},
    ]
    (ws / "03_parsed_json" / name).write_text(json.dumps(rows), encoding="utf-8")


def test_synthesize_default_cmd_omits_restore_stack(workspace, monkeypatch):
    """No demotion history -> the flag is omitted: the default command is byte-identical
    to the legacy one."""
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", 4)
    assert "--restore-stack" not in captured["cmd"]


def test_synthesize_watchdog_records_timeout_chars(workspace, monkeypatch):
    """A timeout demotion records the timed-out sub-batch's total chars (the pre-demotion
    cap): the restart carries --restore-stack '3000:4' (1000 + 2000 chars, cap 4) and
    demotes from the actual two-row sub-batch to --concurrency '1'."""
    ws = workspace
    _seed_long_script(ws)
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if len(calls) == 1:  # first run hangs: name the in-flight batch, then exit 124
            on_line("[watchdog] timeout batch=custom#1 indices=[0, 1] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "long.json", 4)
    assert len(calls) == 2
    assert "--restore-stack" not in calls[0]  # no demotion yet -> legacy command
    assert _cmd_flag(calls[1], "--restore-stack") == "3000:4"
    assert _cmd_flag(calls[1], "--concurrency") == "1"  # actual timed-out rows: 2 -> 1
    assert result["completed"] == 2 and result["failed"] == []
    # the demotion log carries the recorded char count (the user-visible half of the restore)
    assert any("记录本批 3000 字" in msg for _lvl, msg in h.logs)


def test_synthesize_restore_line_pops_before_next_record(workspace, monkeypatch):
    """A [restore] cap=N line re-syncs the backend's workers and pops the record: the NEXT
    demotion records the restored cap (4), not the demoted one (1)."""
    ws = workspace
    _seed_long_script(ws)
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if len(calls) == 1:
            on_line("[watchdog] timeout batch=custom#1 indices=[0, 1] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        if len(calls) == 2:
            # a small batch fits below the threshold: the child restores cap 4, then the
            # next (restored-gear) batch hangs again — in-flight = index 0 (1000 chars)
            on_line("[restore] cap=4")
            on_line("[watchdog] timeout batch=custom#1 indices=[0] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "long.json", 4)
    assert len(calls) == 3
    assert _cmd_flag(calls[1], "--restore-stack") == "3000:4"
    assert any("恢复为 4 段" in msg for _lvl, msg in h.logs)
    # the next demotion records the RESTORED cap (4) — the pop happened before the push
    assert _cmd_flag(calls[2], "--restore-stack") == "1000:4"
    assert _cmd_flag(calls[2], "--concurrency") == "1"  # actual timed-out rows: 1 -> 1
    assert result["completed"] == 2


def test_synthesize_cascade_demotions_encode_oldest_first(workspace, monkeypatch):
    """Actual sub-batches 4 -> 2 demote the cap 4 -> 2 -> 1 and preserve LIFO history."""
    ws = workspace
    _seed_four_segment_script(ws)
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if len(calls) == 1:
            on_line("[watchdog] timeout batch=custom#1 indices=[0, 1, 2, 3] elapsed=181.2")
            raise WorkerWatchdogTimeout()
        elif len(calls) == 2:
            on_line("[watchdog] timeout batch=custom#1 indices=[0, 1] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "long.json", 4)
    assert len(calls) == 3
    assert _cmd_flag(calls[0], "--concurrency") == "4"
    assert "--restore-stack" not in calls[0]
    assert _cmd_flag(calls[1], "--concurrency") == "2"
    assert _cmd_flag(calls[1], "--restore-stack") == "10000:4"
    assert _cmd_flag(calls[2], "--concurrency") == "1"
    assert _cmd_flag(calls[2], "--restore-stack") == "10000:4,3000:2"  # oldest first
    assert result["completed"] == 4


def test_synthesize_strike_at_one_records_nothing(workspace, monkeypatch):
    """Strikes at workers==1 do not push a no-op record after two real demotions."""
    ws = workspace
    _seed_four_segment_script(ws)
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if len(calls) == 1:  # actual rows 4 -> cap 2
            on_line("[watchdog] timeout batch=custom#1 indices=[0, 1, 2, 3] elapsed=181.2")
            raise WorkerWatchdogTimeout()
        if len(calls) == 2:  # actual rows 2 -> cap 1
            on_line("[watchdog] timeout batch=custom#1 indices=[0, 1] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        if len(calls) in (3, 4):  # strikes at workers==1 (first strike, then isolation)
            on_line("[watchdog] timeout batch=custom#1 indices=[0] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        # fifth run: the healthy remaining segments
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "long.json", 4)
    assert len(calls) == 5
    assert "--restore-stack" not in calls[0]
    assert _cmd_flag(calls[1], "--restore-stack") == "10000:4"
    assert _cmd_flag(calls[2], "--restore-stack") == "10000:4,3000:2"
    # the two strikes pushed nothing: the stack is identical across them
    assert _cmd_flag(calls[3], "--restore-stack") == "10000:4,3000:2"
    assert _cmd_flag(calls[4], "--restore-stack") == "10000:4,3000:2"
    assert result["completed"] == 3 and result["failed"][0]["index"] == 0


def test_synthesize_unknown_inflight_index_records_nothing(workspace, monkeypatch):
    """A timeout naming only an out-of-range index means zero known chars: the demotion
    uses the parsed actual row count, but nothing is recorded (a 0-chars record would be inert and
    would block the LIFO stack behind it)."""
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if len(calls) == 1:
            on_line("[watchdog] timeout batch=custom#1 indices=[99] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "s.json", 4)
    assert len(calls) == 2
    assert "--restore-stack" not in calls[1]  # demoted, but nothing to restore to
    assert _cmd_flag(calls[1], "--concurrency") == "1"
    assert result["completed"] == 2


def test_synthesize_restore_line_without_record_still_syncs(workspace, monkeypatch):
    """A [restore] line with no pending record (a desync) must not fail the run: log a
    WARNING and still take the worker's value (the worker's value always wins)."""
    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if len(calls) == 1:
            # no [watchdog] line before the exit: the backend has no in-flight set -> no record
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        # second run: a restore line the backend has no record for, then a clean finish
        on_line("[restore] cap=4")
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "s.json", 4)
    assert len(calls) == 2
    assert "--restore-stack" not in calls[1]  # nothing was recorded, so nothing is sent
    assert any(lvl == "WARNING" and "没有待恢复" in msg for lvl, msg in h.logs)
    assert result["completed"] == 2  # the desync is a warning, not a failure


# --------------------------------------------------------------------------- #
# the persistent per-run transcript (a forensic trail for a mid-batch death)
# --------------------------------------------------------------------------- #

def test_synthesize_aggregates_segment_logs(workspace, monkeypatch):
    captured = {}
    _stub_engine(monkeypatch, captured)
    h = _Handle()
    tts_batch.synthesize(h, None, "s.json", 4)

    segment_logs = [msg for _level, msg in h.logs if msg.startswith("合成进度：")]
    assert len(segment_logs) == 1
    assert "新增成功 2 段" in segment_logs[0]
    assert not any(msg.startswith("[1/2]") or msg.startswith("[2/2]") for _level, msg in h.logs)


def test_synthesize_persists_run_log(workspace, monkeypatch):
    """synthesize mirrors the child transcript to a per-run log file under <workspace>/logs
    (the durable task event stream carries UI progress; a run that dies mid-batch must still
    leave the child transcript on disk for diagnosis)."""
    captured = {}

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        captured["log_file"] = kw.get("log_file")
        log_file = kw.get("log_file")
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("=== attempt started ===\n[out] 引擎就绪\n[err] simulated fault\n"
                        "=== attempt ended rc=0 ===\n")
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok "
                    f"{os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine",
                        lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    tts_batch.synthesize(h, None, "s.json", 4)

    log_file = captured["log_file"]
    assert log_file is not None
    assert log_file.parent == workspace / "logs"
    assert log_file.name.startswith("tts_batch_") and log_file.name.endswith(".log")
    text = log_file.read_text(encoding="utf-8")
    assert "[out] 引擎就绪" in text and "[err] simulated fault" in text
    assert any("运行日志" in msg for _lvl, msg in h.logs)  # the user can find the file


def test_synthesize_logs_batch_performance_to_task_and_workspace_log(workspace, monkeypatch):
    """A completed worker batch is readable in the task log and remains in the run log."""
    captured = {}

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["log_file"] = kw["log_file"]
        line = "[perf] " + json.dumps({
            "stage": "batch", "event": "end", "batch": "clone#1",
            "rows": 2, "chars": 10, "seconds": 2.5,
            "throughput_chars_per_sec": 4.0,
        })
        on_line(line)
        mirrored = kw["log_line"](line)
        captured["log_file"].parent.mkdir(parents=True, exist_ok=True)
        if mirrored is not None:
            with open(captured["log_file"], "a", encoding="utf-8") as f:
                f.write(f"[out] {mirrored}\n")
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok "
                    f"{os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine",
                        lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    tts_batch.synthesize(h, None, "s.json", 4)

    assert any(
        msg == "性能：批内数量 2 · 总字数 10 · 总耗时 2.50 秒 · 吞吐量 4.00 字/秒"
        for _level, msg in h.logs
    )
    text = captured["log_file"].read_text(encoding="utf-8")
    assert "性能：批内数量 2 · 总字数 10 · 总耗时 2.50 秒 · 吞吐量 4.00 字/秒" in text
    assert "[out] [perf]" not in text


def test_run_worker_filters_per_segment_protocol_from_log_file(tmp_path):
    """run_worker keeps internal per-segment protocol lines out of the persistent log."""
    import sys

    from backend.engines import tts as tts_eng

    log_file = tmp_path / "logs" / "unit_run.log"
    code = ("import sys; "
            "print('[segment] 0 ok /x.mp3', flush=True); "
            "print('boom-line', file=sys.stderr, flush=True)")
    h = _Handle()
    tail = tts_eng.run_tts_subprocess([sys.executable, "-c", code], h, lambda line: None,
                              log_file=log_file, log_line=tts_batch._format_batch_log_line)
    text = log_file.read_text(encoding="utf-8")
    assert "=== attempt started" in text
    assert "[out] [segment] 0 ok /x.mp3" not in text
    assert "[err] boom-line" in text
    assert "=== attempt ended rc=0" in text
    assert "boom-line" in " | ".join(tail)  # the stderr tail still feeds the error message


def test_run_worker_can_transform_stdout_in_persistent_log(tmp_path):
    """A stage can hide internal structured events while keeping a readable summary on disk."""
    import sys

    from backend.engines import tts as tts_eng

    log_file = tmp_path / "logs" / "unit_run.log"
    code = "print('[perf] {\\\"internal\\\": true}', flush=True)"
    seen = []
    h = _Handle()
    tts_eng.run_tts_subprocess(
        [sys.executable, "-c", code], h, seen.append, log_file=log_file,
        log_line=lambda line: "性能摘要" if line.startswith("[perf]") else line,
    )
    text = log_file.read_text(encoding="utf-8")
    assert seen == ['[perf] {"internal": true}']
    assert "性能摘要" in text
    assert "[out] [perf]" not in text


def test_run_worker_restarts_child_after_pause_and_keeps_temp_input(tmp_path):
    import sys
    import time

    from backend.engines import tts as tts_eng

    launches = tmp_path / "launches.txt"
    temp_input = tmp_path / "segments.json"
    temp_input.write_text("{}", encoding="utf-8")
    code = (
        "import pathlib, sys, time; "
        f"p=pathlib.Path({str(launches)!r}); "
        "n=int(p.read_text() or '0') if p.exists() else 0; "
        "p.write_text(str(n+1)); "
        "time.sleep(30) if n == 0 else print('resumed', flush=True)"
    )

    class _PauseOnce(_Handle):
        paused = False

        def check_interruptible(self, on_pause):
            if not self.paused:
                self.paused = True
                deadline = time.monotonic() + 2
                while not launches.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert launches.exists()
                on_pause()

    seen = []
    tail = tts_eng.run_tts_subprocess(
        [sys.executable, "-c", code], _PauseOnce(), seen.append,
        temp_files=(temp_input,), interrupt_on_pause=True,
    )
    assert launches.read_text(encoding="utf-8") == "2"
    assert seen == ["resumed"]
    assert list(tail) == []
    assert not temp_input.exists()


def test_run_worker_cancelled_while_paused_cleans_temp_input(tmp_path):
    import sys

    from backend.core.task_control import TaskCancelled
    from backend.engines import tts as tts_eng

    temp_input = tmp_path / "segments.json"
    temp_input.write_text("{}", encoding="utf-8")

    class _CancelOnPause(_Handle):
        def check_interruptible(self, on_pause):
            on_pause()
            raise TaskCancelled()

    with pytest.raises(TaskCancelled):
        tts_eng.run_tts_subprocess(
            [sys.executable, "-c", "import time; time.sleep(30)"], _CancelOnPause(),
            lambda _line: None, temp_files=(temp_input,), interrupt_on_pause=True,
        )
    assert not temp_input.exists()


def test_run_worker_cancel_not_stalled_by_backlog(tmp_path):
    """A cancel must be honoured within one line, NOT after the whole output backlog drains.

    On a loaded machine (AV scan + disk / GPU contention) each line's processing is slow, so
    the main loop falls far behind the full-speed worker and a large backlog queues up. The
    old code checked cancel only once per full-drain cycle, so a cancel clicked mid-run waited
    for the ENTIRE backlog (minutes) before the child was killed — the user saw the log keep
    scrolling and thought "取消停不下来". The per-line check bounds the latency to one line.

    Simulated here: the child spews 1500 lines in well under a second (they all queue up), the
    consumer digests 20 ms/line (1500 × 20 ms = 30 s of draining), and the cancel lands at line
    120 (~2.5 s in). Pre-fix the recognition came with the next top-of-loop check, ~30 s away;
    the bound below pins the post-fix behaviour.
    """
    import sys
    import threading
    import time

    from backend.core.task_control import TaskCancelled
    from backend.engines import tts as tts_eng

    class _LateCancel(_Handle):
        """A stand-in that sets its cancel flag once the (slow) consumer digested N lines."""

        def __init__(self, after: int):
            super().__init__()
            self.cancel_event = threading.Event()
            self.after = after
            self.seen = 0

        def check(self):
            if self.cancel_event.is_set():
                raise TaskCancelled()

        def note_line(self):
            self.seen += 1
            if self.seen >= self.after:
                self.cancel_event.set()

    code = ("import time\n"
            "for i in range(1500):\n"
            "    print(f'seg-{i}', flush=True)\n"
            "time.sleep(300)\n")
    h = _LateCancel(after=120)
    t0 = time.monotonic()
    with pytest.raises(TaskCancelled):
        tts_eng.run_tts_subprocess([sys.executable, "-c", code], h,
                           lambda line: (time.sleep(0.02), h.note_line()))
    assert time.monotonic() - t0 < 10, "cancel stalled behind the output backlog"


# --------------------------------------------------------------------------- #
# resume / incremental manifest / batch-status
# --------------------------------------------------------------------------- #

def _seed_done_index0(ws):
    """Pre-seed the package with index 0 already synthesized (manifest entry + file on disk)."""
    out_dir = ws / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "0001.mp3").write_bytes(b"fake")
    (out_dir / "manifest.json").write_text(json.dumps([
        {"index": 0, "speaker": "A", "text": "hello", "path": str(out_dir / "0001.mp3"),
         "ok": True, "reason": "", "pause_after": None},
    ]), encoding="utf-8")
    return out_dir


def _segments_written(captured):
    """The per-line segments the (fake) worker was asked to synthesize, as ``[index, ...]``."""
    seg_file = captured["cmd"][captured["cmd"].index("--segments-file") + 1]
    with open(seg_file, encoding="utf-8") as f:
        return [s["index"] for s in json.load(f)]


def test_synthesize_resume_skips_done(workspace, monkeypatch):
    _seed_done_index0(workspace)
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", None)  # default = resume
    assert _segments_written(captured) == [1]  # only the not-yet-done segment is synthesized


def test_voice_signature_change_resynthesizes_only_changed_speaker(workspace):
    """A changed active reference invalidates every old segment for that speaker."""
    out_dir = workspace / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = {}
    config = {
        "A": {"type": "clone", "ref_audio": "04_voice_profiles/a-1.wav"},
        "B": {"type": "clone", "ref_audio": "04_voice_profiles/b.wav"},
    }
    segments = [
        {"index": 0, "speaker": "A", "text": "a", "pause_after": None},
        {"index": 1, "speaker": "B", "text": "b", "pause_after": None},
    ]
    for segment in segments:
        path = out_dir / f"{segment['index'] + 1:04d}.mp3"
        path.write_bytes(b"audio")
        entries[segment["index"]] = {
            **segment, "path": str(path), "ok": True, "reason": "",
            "voice_signature": tts_batch.voice_signature(segment["speaker"], config),
        }

    changed = {**config, "A": {**config["A"], "ref_audio": "04_voice_profiles/a-2.wav"}}
    expected = tts_batch.segment_voice_signatures(segments, changed)
    done = tts_batch.done_indices(entries, out_dir, workspace, expected)

    assert done == {1}


def test_manifest_compares_actual_voice_json_per_segment(workspace):
    """A persisted used-voice JSON is the restart-safe source of truth."""
    out_dir = workspace / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "A": {"type": "clone", "ref_audio": "04_voice_profiles/a-1.wav",
              "ref_text": "a sample"},
        "B": {"type": "clone", "ref_audio": "04_voice_profiles/b.wav"},
    }
    segments = [
        {"index": 0, "speaker": "A", "text": "a", "pause_after": None},
        {"index": 1, "speaker": "B", "text": "b", "pause_after": None},
    ]
    entries = {}
    used = tts_batch.segment_voice_params(segments, config)
    for segment in segments:
        path = out_dir / f"{segment['index'] + 1:04d}.mp3"
        path.write_bytes(b"audio")
        entries[segment["index"]] = {
            **segment, "path": str(path), "ok": True, "reason": "",
            "voice_used": used[segment["index"]],
        }

    changed = {**config, "A": {**config["A"], "ref_text": "changed sample"}}
    expected = tts_batch.segment_voice_params(segments, changed)
    assert entries[0]["voice_used"] == used[0]
    assert tts_batch.done_indices(
        entries, out_dir, workspace, expected_voice_params=expected,
    ) == {1}


def test_voice_signature_ignores_absolute_vs_relative_workspace_path(workspace):
    absolute = str(workspace / "04_voice_profiles" / "a.wav")
    relative = "04_voice_profiles/a.wav"
    assert tts_batch.voice_signature("A", {
        "A": {"type": "clone", "ref_audio": absolute},
    }) == tts_batch.voice_signature("A", {
        "A": {"type": "clone", "ref_audio": relative},
    })


def test_legacy_manifest_without_voice_signature_is_stale(workspace):
    """A restart must not resurrect pre-signature audio as resumable progress."""
    out_dir = workspace / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = out_dir / "0001.mp3"
    audio.write_bytes(b"audio")
    config = {"A": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav"}}
    _seed_voice_config(workspace, config)
    entry = {"index": 0, "speaker": "A", "text": "a", "path": str(audio),
             "ok": True, "reason": "", "pause_after": None}
    (out_dir / "manifest.json").write_text(json.dumps([entry]), encoding="utf-8")

    expected = tts_batch.segment_voice_signatures([entry], config)
    assert tts_batch.done_indices({0: entry}, out_dir, workspace, expected) == set()


def test_legacy_signature_manifest_migrates_used_voice_json(workspace):
    """Existing audio from the signature-only manifest remains resumable after migration."""
    out_dir = workspace / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = out_dir / "0001.mp3"
    audio.write_bytes(b"audio")
    config = {"A": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav"}}
    _seed_voice_config(workspace, config)
    entry = {
        "index": 0, "speaker": "A", "text": "a", "path": str(audio),
        "ok": True, "reason": "", "pause_after": None,
        "voice_signature": tts_batch.voice_signature("A", config),
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps([entry]), encoding="utf-8")

    loaded = tts_batch.load_manifest(out_dir)
    expected = tts_batch.segment_voice_params([entry], config)
    assert loaded[0]["voice_used"] == expected[0]
    assert tts_batch.done_indices(
        loaded, out_dir, workspace, expected_voice_params=expected,
    ) == {0}

    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted[0]["voice_used"] == expected[0]


def test_read_manifest_normalizes_legacy_data_without_rewriting(workspace):
    out_dir = workspace / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = out_dir / "0001.mp3"
    audio.write_bytes(b"audio")
    config = {"A": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav"}}
    _seed_voice_config(workspace, config)
    entry = {
        "index": 0, "speaker": "A", "text": "a", "path": str(audio),
        "ok": True, "voice_signature": tts_batch.voice_signature("A", config),
    }
    manifest_path = out_dir / "manifest.json"
    original = json.dumps([entry])
    manifest_path.write_text(original, encoding="utf-8")

    loaded = tts_batch.read_manifest(out_dir)

    assert loaded[0]["voice_used"] == tts_batch.voice_params("A", config)
    assert loaded[0]["path"] != str(audio)
    assert manifest_path.read_text(encoding="utf-8") == original


def test_hash_only_manifest_is_stale_when_voice_json_is_required(workspace):
    out_dir = workspace / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = out_dir / "0001.mp3"
    audio.write_bytes(b"audio")
    config = {"A": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav"}}
    entry = {
        "index": 0, "speaker": "A", "text": "a", "path": str(audio),
        "ok": True, "reason": "",
        "voice_signature": tts_batch.voice_signature("A", config),
    }
    expected = tts_batch.segment_voice_params([entry], config)
    assert tts_batch.done_indices(
        {0: entry}, out_dir, workspace, expected_voice_params=expected,
    ) == set()


def test_invalidate_speaker_outputs_marks_manifest_and_removes_merge(workspace):
    out_dir = workspace / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = out_dir / "0001.mp3"
    audio.write_bytes(b"audio")
    (workspace / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    merged = workspace / "06_audio_merge" / "s.mp3"
    merged.write_bytes(b"merged")
    bgm = workspace / "08_bgm" / "s.mp3"
    bgm.parent.mkdir(parents=True, exist_ok=True)
    bgm.write_bytes(b"bgm")
    manifest = [{
        "index": 0, "speaker": "A", "text": "a", "path": str(audio),
        "ok": True, "reason": "", "voice_signature": "old",
    }]
    (out_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert tts_batch.invalidate_speaker_outputs(["A"]) == 1
    current = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert current[0]["voice_signature"] == ""
    assert current[0]["voice_used"] == {}
    assert len(current[0]["voice_versions"]) == 1
    assert (workspace / current[0]["voice_versions"][0]["path"]).exists()
    assert not merged.exists()
    assert not bgm.exists()


def test_switching_back_restores_archived_voice_version(workspace):
    """A voice switch keeps the old audio and makes it resumable when switched back."""
    out_dir = workspace / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    audio = out_dir / "0001.mp3"
    audio.write_bytes(b"voice-a")
    config_a = {"A": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav"}}
    _seed_voice_config(workspace, config_a)
    entry = {
        "index": 0, "speaker": "A", "text": "a", "path": str(audio),
        "ok": True, "reason": "", "pause_after": None,
        "voice_used": tts_batch.voice_params("A", config_a),
        "voice_signature": tts_batch.voice_signature("A", config_a),
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps([entry]), encoding="utf-8")

    assert tts_batch.invalidate_speaker_outputs(["A"]) == 1
    old = tts_batch.load_manifest(out_dir)
    expected_params = tts_batch.segment_voice_params([entry], config_a)
    expected_signatures = tts_batch.segment_voice_signatures([entry], config_a)
    assert old[0]["voice_used"] == expected_params[0]
    assert (workspace / old[0]["path"]).read_bytes() == b"voice-a"
    assert tts_batch.done_indices(
        old, out_dir, workspace,
        expected_voice_signatures=expected_signatures,
        expected_voice_params=expected_params,
    ) == {0}


def test_synthesize_resume_manifest_is_cumulative(workspace, monkeypatch):
    out_dir = _seed_done_index0(workspace)
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", None)
    by_index = {e["index"]: e for e in json.loads((out_dir / "manifest.json").read_text("utf-8"))}
    assert set(by_index) == {0, 1}  # pre-done + newly-done: one entry per non-empty segment
    # The legacy absolute path of the preserved entry is migrated to the workspace-relative
    # form on load; this run's (absolute, worker-reported) output is stored in the same form.
    assert by_index[0]["ok"] is True and by_index[0]["path"] == "05_audio_chunk/s/0001.mp3"
    assert by_index[1]["ok"] is True and by_index[1]["path"] == "05_audio_chunk/s/0002.mp3"


def test_synthesize_writes_manifest_incrementally(workspace, monkeypatch):
    """In-memory state updates per line; the disk rewrite is throttled — but the FIRST line
    always flushes (so a cancel right after the first result loses nothing) and the run's end
    force-flushes (so the manifest is complete whenever the run settles)."""
    captured = {}

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        out = cmd[cmd.index("--out-dir") + 1]
        on_line(f"[segment] 0 ok {os.path.join(out, '0001.mp3')}")
        # After the first segment the manifest must already reflect it (the first line always
        # flushes, even under the throttle) — this is exactly what makes an early cancel lose
        # nothing.
        with open(os.path.join(out, "manifest.json"), encoding="utf-8") as f:
            mid = json.load(f)
        assert any(e["index"] == 0 and e["ok"] for e in mid), "manifest not flushed after first segment"
        on_line(f"[segment] 1 ok {os.path.join(out, '0002.mp3')}")

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    tts_batch.synthesize(_Handle(), None, "s.json", None)

    # The completion force-flush covers the throttled line: the manifest is complete at rest.
    by = {e["index"]: e for e in json.loads(
        (workspace / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))}
    assert all(by[i]["ok"] for i in (0, 1))


def test_synthesize_manifest_flush_is_throttled(workspace, monkeypatch):
    """A burst of [segment] lines inside the throttle window rewrites the file only once
    (the per-line updates stay in memory); the completion force-flush lands the complete
    manifest. Pins that the hot path is NOT a full-file rewrite per line."""
    captured = {}
    writes = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        out = cmd[cmd.index("--out-dir") + 1]
        for i in (0, 1):  # both lines land within the 2s throttle window
            on_line(f"[segment] {i} ok {os.path.join(out, f'{i + 1:04d}.mp3')}")

    real_write = tts_batch.write_manifest_file  # capture before the patch below

    def counting(path, manifest, handle=None):
        writes.append((len(manifest), sum(e["ok"] for e in manifest)))
        real_write(path, manifest, handle)

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    monkeypatch.setattr(tts_batch, "write_manifest_file", counting)
    tts_batch.synthesize(_Handle(), None, "s.json", None)

    # The manifest always describes every segment (done + pending), so each write carries 2
    # entries: the first-line flush sees 1 ok (segment 1 is still pending), the second line
    # triggers NO write (throttle window), and the completion force-flush sees both ok.
    # Exactly two disk writes for the whole run.
    assert writes == [(2, 1), (2, 2)]


def test_synthesize_resume_all_done_short_circuits(workspace, monkeypatch):
    ws = workspace
    out_dir = ws / "05_audio_chunk" / "s"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("0001.mp3", "0002.mp3"):
        (out_dir / name).write_bytes(b"fake")
    (out_dir / "manifest.json").write_text(json.dumps([
        {"index": 0, "speaker": "A", "text": "hello", "path": str(out_dir / "0001.mp3"),
         "ok": True, "reason": "", "pause_after": None},
        {"index": 1, "speaker": "B", "text": "world", "path": str(out_dir / "0002.mp3"),
         "ok": True, "reason": "", "pause_after": None},
    ]), encoding="utf-8")
    calls = []

    def run_worker(cmd, handle, on_line, **kw):
        calls.append(cmd)  # must never be called when a resume has nothing left

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "s.json", None)
    assert calls == []  # no engine spawned (no wasted model load)
    assert result["completed"] == 2 and result["failed"] == []
    assert result["done_count"] == 2 and result["all_count"] == 2
    assert any("已全部完成" in msg for _lvl, msg in h.logs)


# --------------------------------------------------------------------------- #
# 进度指标 (segment stats) — 合成页「开始音频合成」按钮下方的
# 已合成/总段数 · 已合成/总字数（SSE 「segments」 事件，节流推送）
# --------------------------------------------------------------------------- #

def test_synthesize_segment_stats_baseline_and_throttled_final(workspace, monkeypatch):
    """First push = the run baseline (fresh run: 0/2 段, 0/10 字); a whole sub-batch of
    segment lines landing inside the 1 s throttle window yields NO mid push; the final
    forced push carries the full cumulative state. The totals are the FULL segment table
    (2 segments, 5+5 stripped chars) — not just this run's rows."""
    captured = {}
    _stub_engine(monkeypatch, captured)
    h = _Handle()
    tts_batch.synthesize(h, None, "s.json", 4)
    assert h.stats == [(0, 2, 0, 10), (2, 2, 10, 10)]


def test_synthesize_segment_stats_pushes_per_segment(workspace, monkeypatch):
    """With the push throttle at 0 every completed segment pushes at once — the
    实时 path: the card steps up per finished segment/sub-batch. Cumulatives step by
    one segment and its stripped chars, monotonically, ending at the full table."""
    captured = {}
    _stub_engine(monkeypatch, captured)
    monkeypatch.setattr(tts_batch, "STATS_FLUSH_INTERVAL", 0.0)
    h = _Handle()
    tts_batch.synthesize(h, None, "s.json", 4)
    # baseline → one push per completed segment → the forced final push (same full state)
    assert h.stats == [
        (0, 2, 0, 10),
        (1, 2, 5, 10),
        (2, 2, 10, 10),
        (2, 2, 10, 10),
    ]


def test_synthesize_segment_stats_resume_baseline_counts_prior_done(workspace, monkeypatch):
    """A resume run's baseline already counts the pre-run completed work: segment 1 done
    in the manifest → first push (1, 2, 5, 10), not (0, …). The final push = the whole
    table done. (Union semantics: re-doing an already-done segment can never double-count.)
    """
    ws = workspace
    _seed_done_package(ws, "s", [(0, "A", "hello", "0001.mp3")])
    captured = {}
    _stub_engine(monkeypatch, captured)
    monkeypatch.setattr(tts_batch, "STATS_FLUSH_INTERVAL", 0.0)
    h = _Handle()
    tts_batch.synthesize(h, None, "s.json", 4)
    assert h.stats[0] == (1, 2, 5, 10)  # the baseline includes the pre-run done segment
    assert h.stats[-1] == (2, 2, 10, 10)  # the final = everything done
    for (d0, _t, c0, _tc), (d1, _t2, c1, _tc2) in zip(h.stats, h.stats[1:]):
        assert d1 >= d0 and c1 >= c0  # monotone


def test_synthesize_segment_stats_failed_segments_not_counted(workspace, monkeypatch):
    """A failed segment is NOT 已合成: it adds nothing to the done 段/字 cumulatives
    (it only shows up in the run's failed list / manifest ok:false). 1-of-2 ok still
    succeeds, and the final push counts exactly the one ok segment."""
    captured = {}
    monkeypatch.setattr(tts_batch, "STATS_FLUSH_INTERVAL", 0.0)

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        out_dir = cmd[cmd.index("--out-dir") + 1]
        on_line(f"[segment] 0 ok {os.path.join(out_dir, '0001.mp3')}")
        on_line("[segment] 1 error 引擎炸了")

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize(h, None, "s.json", 4)
    assert result["completed"] == 1 and len(result["failed"]) == 1  # the run succeeds
    assert (1, 2, 5, 10) in h.stats  # after segment 0 ok: 1 done 段 / 5 done 字
    assert h.stats[-1] == (1, 2, 5, 10)  # the failed segment added nothing; final = same


def test_synthesize_multi_segment_stats_pool_wide(workspace, monkeypatch):
    """The pooled run reports POOL-wide cumulatives: the totals span every healthy file
    (s: 2 段/10 字, t: 2 段/6 字); per-segment pushes step across chapter boundaries;
    the final = the whole pool done."""
    ws = workspace
    _seed_second_file(ws)
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    monkeypatch.setattr(tts_batch, "STATS_FLUSH_INTERVAL", 0.0)
    h = _Handle()
    tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    assert h.stats[0] == (0, 4, 0, 16)  # the baseline: both files' full tables
    assert h.stats[-1] == (4, 4, 16, 16)  # the final: the whole pool done
    assert len(h.stats) == 6  # baseline + 4 per-segment + the forced final
    # the four per-segment pushes step strictly (every pool row completes, in pool order);
    # the forced final repeats the last per-segment value (same full state)
    for (d0, _t, c0, _tc), (d1, _t2, c1, _tc2) in zip(h.stats[:4], h.stats[1:5]):
        assert d1 > d0 and c1 > c0
    assert h.stats[5] == h.stats[4]


def test_synthesize_multi_segment_stats_exclude_fatal_files(workspace, monkeypatch):
    """A prep-fatal file contributes nothing to the progress totals (it has no
    synthesizable segments): the totals cover only the healthy files, while the fatal
    file is still isolated per file (its result entry carries the error)."""
    ws = workspace
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    monkeypatch.setattr(tts_batch, "STATS_FLUSH_INTERVAL", 0.0)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "missing.json"], 4)
    assert h.stats[0] == (0, 2, 0, 10)  # only s.json's table (missing.json is fatal → excluded)
    assert h.stats[-1] == (2, 2, 10, 10)
    assert result["files"][1]["error"]  # the fatal file stays isolated (existing semantics)


def test_synthesize_multi_segment_stats_resume_baselines(workspace, monkeypatch):
    """Pooled resume: each file's pre-run done set is in its baseline (s: 1/2 done
    → 5/10 字; t: none) → the pool baseline is (1, 4, 5, 16)."""
    ws = workspace
    _seed_second_file(ws)
    _seed_done_package(ws, "s", [(0, "A", "hello", "0001.mp3")])
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    monkeypatch.setattr(tts_batch, "STATS_FLUSH_INTERVAL", 0.0)
    h = _Handle()
    tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    assert h.stats[0] == (1, 4, 5, 16)  # the pool baseline counts s's pre-run done segment
    assert h.stats[-1] == (4, 4, 16, 16)


def test_is_done_requires_ok_and_file(workspace):
    d = workspace / "x"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "a.mp3"
    f.write_bytes(b"fake")
    assert tts_batch.is_done({"ok": True, "path": str(f)}) is True
    assert tts_batch.is_done({"ok": True, "path": str(d / "missing.mp3")}) is False  # ok but file gone
    assert tts_batch.is_done({"ok": False, "path": str(f)}) is False  # a failure is not done
    assert tts_batch.is_done({"ok": True, "path": ""}) is False  # no path
    assert tts_batch.is_done(None) is False


# -- done_indices (the batched per-package done-set: one listdir, is_done parity) --

def test_done_indices_relative_fast_path(workspace):
    """Entries stored in the normal relative form (directly inside the package dir) are
    answered from the single lazy directory listing — same verdicts as per-entry is_done."""
    ws = workspace
    out = ws / "05_audio_chunk" / "s"
    out.mkdir(parents=True, exist_ok=True)
    (out / "0001.mp3").write_bytes(b"fake")
    (out / "0003.mp3").write_bytes(b"fake")
    entries = {
        0: {"ok": True, "path": "05_audio_chunk/s/0001.mp3"},   # file on disk -> done
        1: {"ok": True, "path": "05_audio_chunk/s/0002.mp3"},   # ok but file missing
        2: {"ok": True, "path": "05_audio_chunk/s/0003.mp3"},   # file on disk -> done
        3: {"ok": False, "path": "05_audio_chunk/s/0001.mp3"},  # a failure is not done
        4: {"ok": True, "path": ""},                            # no path
        5: None,
    }
    assert tts_batch.done_indices(entries, out, ws) == {0, 2}


def test_done_indices_legacy_absolute_fallback(workspace):
    """Absolute stored values (the pre-migration form) skip the fast path and fall back to
    the exact per-entry is_done rule."""
    ws = workspace
    out = ws / "05_audio_chunk" / "s"
    out.mkdir(parents=True, exist_ok=True)
    f = out / "0001.mp3"
    f.write_bytes(b"fake")
    entries = {
        0: {"ok": True, "path": str(f)},                   # inside the workspace -> done
        1: {"ok": True, "path": str(out / "missing.mp3")}, # file gone
    }
    assert tts_batch.done_indices(entries, out, ws) == {0}


def test_done_indices_external_absolute_fallback(workspace):
    """An absolute path OUTSIDE the workspace is judged as-is by is_done (a global /
    external resource), never dropped by the fast path's prefix rule."""
    ws = workspace
    out = ws / "05_audio_chunk" / "s"
    out.mkdir(parents=True, exist_ok=True)
    ext = ws.parent / "outside.mp3"  # the project root — outside the workspace
    ext.write_bytes(b"fake")
    entries = {
        0: {"ok": True, "path": str(ext)},                  # exists -> done
        1: {"ok": True, "path": str(ws.parent / "gone.mp3")},  # missing
    }
    assert tts_batch.done_indices(entries, out, ws) == {0}


def test_done_indices_escaping_relative_fallback(workspace):
    """Relative values that do not sit directly inside the package dir can never be
    fast-pathed (no prefix match) and take the exact per-entry is_done fallback:
    a ``..`` value escaping the workspace is rejected (PathOutsideWorkspace -> False),
    and a ``..`` value normalizing elsewhere in the workspace is judged by existence."""
    ws = workspace
    out = ws / "05_audio_chunk" / "s"
    out.mkdir(parents=True, exist_ok=True)
    (out / "0001.mp3").write_bytes(b"fake")
    entries = {
        0: {"ok": True, "path": "05_audio_chunk/s/../../0001.mp3"},   # -> ws/0001.mp3: absent
        1: {"ok": True, "path": "05_audio_chunk/../02_split_text/x.txt"},  # another dir: absent
        2: {"ok": True, "path": "../../outside.mp3"},                 # escapes the ws -> False
    }
    assert tts_batch.done_indices(entries, out, ws) == set()
    # The same ``..`` hop that lands on a REAL file elsewhere in the ws counts as done —
    # the fallback is is_done entry-for-entry, not a blanket rejection of ``..``.
    (ws / "0001.mp3").write_bytes(b"fake")
    assert tts_batch.done_indices({0: entries[0]}, out, ws) == {0}


def test_done_indices_directory_named_like_mp3_counts_as_done(workspace):
    """Path.exists() is True for a *directory* named like a segment file; the name-set
    fast path must agree (deliberately no is_file filter on the listing)."""
    ws = workspace
    out = ws / "05_audio_chunk" / "s"
    out.mkdir(parents=True, exist_ok=True)
    (out / "0001.mp3").mkdir()  # a DIRECTORY named like an mp3
    entries = {0: {"ok": True, "path": "05_audio_chunk/s/0001.mp3"}}
    assert tts_batch.done_indices(entries, out, ws) == {0}


def test_done_indices_missing_out_dir_is_empty(workspace):
    """A package dir that does not exist yields no done entries (nothing exists — the same
    as per-entry exists() all False), without raising."""
    ws = workspace
    out = ws / "05_audio_chunk" / "ghost"  # never created
    entries = {
        0: {"ok": True, "path": "05_audio_chunk/ghost/0001.mp3"},
        1: {"ok": True, "path": str(out / "0002.mp3")},
    }
    assert tts_batch.done_indices(entries, out, ws) == set()


def test_done_indices_falls_back_to_exact_checks_when_listing_fails(workspace, monkeypatch):
    from backend.engines import tts_manifest

    ws = workspace
    out = ws / "05_audio_chunk" / "s"
    out.mkdir(parents=True, exist_ok=True)
    (out / "0001.mp3").write_bytes(b"fake")
    monkeypatch.setattr(tts_manifest.os, "listdir", lambda _path: (_ for _ in ()).throw(PermissionError()))

    assert tts_batch.done_indices({0: {"ok": True, "path": "05_audio_chunk/s/0001.mp3"}}, out, ws) == {0}


def test_done_indices_no_workspace_uses_is_done_fallback(workspace):
    """With no workspace the fast path is unavailable: every entry takes the exact
    per-entry is_done rule (absolute values judged as-is)."""
    ws = workspace
    out = ws / "05_audio_chunk" / "s"
    out.mkdir(parents=True, exist_ok=True)
    f = out / "0001.mp3"
    f.write_bytes(b"fake")
    entries = {
        0: {"ok": True, "path": str(f)},
        1: {"ok": True, "path": "05_audio_chunk/s/0002.mp3"},  # relative + no ws -> not done
    }
    assert tts_batch.done_indices(entries, out, None) == {0}


def test_synthesis_plan_selection():
    # plan to synthesize resume skips done
    assert tts_batch.plan_to_synthesize({0, 1, 2}, {0, 1}) == {2}

    # plan to synthesize explicit indices intersect
    assert tts_batch.plan_to_synthesize({0, 1, 2}, {0}, indices=[1, 2, 9]) == {1, 2}


def test_batch_status_counts(workspace):
    from backend.api.tts import batch_status
    # No manifest yet -> the two non-empty lines, none done.
    assert batch_status("s.json") == {"total": 2, "completed": 0, "remaining": 2}
    _seed_done_index0(workspace)  # one done
    assert batch_status("s.json") == {"total": 2, "completed": 1, "remaining": 1}


def test_batch_status_missing_script_degrades(workspace):
    from backend.api.tts import batch_status
    assert batch_status("nope.json") == {"total": 0, "completed": 0, "remaining": 0}


# --------------------------------------------------------------------------- #
# multi-file run (synthesize_multi) — the 待合成 card's multi-select
# --------------------------------------------------------------------------- #

def _seed_second_file(ws, name="t.json"):
    """A second parsed script with its own speaker set (multi-file tests)."""
    (ws / "03_parsed_json" / name).write_text(
        json.dumps([{"speaker": "C", "text": "uno"}, {"speaker": "C", "text": "dos"}]),
        encoding="utf-8",
    )


def _seed_voice_config(ws, entries):
    (ws / "04_voice_profiles").mkdir(parents=True, exist_ok=True)
    (ws / "04_voice_profiles" / "voice_config.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _seed_done_package(ws, pkg, done):
    """Pre-seed a package with completed segments: manifest entries + files on disk.

    ``done`` = a list of ``(index, speaker, text, filename)`` tuples.
    """
    out_dir = ws / "05_audio_chunk" / pkg
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for index, speaker, text, fname in done:
        (out_dir / fname).write_bytes(b"fake")
        manifest.append({"index": index, "speaker": speaker, "text": text,
                         "path": str(out_dir / fname), "ok": True, "reason": "",
                         "pause_after": None})
    vc_path = ws / "04_voice_profiles" / "voice_config.json"
    if vc_path.exists():
        voice_config = json.loads(vc_path.read_text("utf-8"))
        for entry in manifest:
            entry["voice_signature"] = tts_batch.voice_signature(
                entry["speaker"], voice_config,
            )
            entry["voice_used"] = tts_batch.voice_params(
                entry["speaker"], voice_config,
            )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                           encoding="utf-8")
    return out_dir


def _fake_run_worker_pool(calls, errors=(), progress=False):
    """A run_worker stand-in for POOLED multi-file runs: records the cmd, then replays the
    pool rows per the REAL worker's save rules — file number = file_index + 1 (the row's
    chapter position; the segment-table index when absent), zero-padded to the run's stable
    width (the backend's --width; the loaded table's length in legacy runs without it),
    saved under the row's own out_dir (the cmd's --out-dir is only the fallback).
    ``errors`` = (package-dir-name, file_index) pairs that get an error line instead of a
    file; ``progress`` emits the worker's [progress] lines the way the real run_worker
    intercepts them (straight to handle.progress)."""

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        fallback = cmd[cmd.index("--out-dir") + 1]
        width = (int(cmd[cmd.index("--width") + 1]) if "--width" in cmd
                 else max(4, len(str(len(segs)))))
        if progress:
            handle.progress(0.05, "生成中")
        for s in segs:
            local = s.get("file_index", s["index"])
            num = int(local) + 1
            out = s.get("out_dir") or fallback
            if (Path(out).name, local) in errors:
                on_line(f"[segment] {s['index']} error 强制失败（fake worker）")
            else:
                (Path(out) / f"{num:0{width}d}.mp3").write_bytes(b"fake")
                on_line(f"[segment] {s['index']} ok {os.path.join(out, f'{num:0{width}d}.mp3')}")
        if progress:
            handle.progress(1.0, "完成")
        return deque()

    return run_worker


def _stub_engine_pool(monkeypatch, calls, errors=(), progress=False):
    """Point the engine at the pooled fake worker (no real shared .venv subprocess)."""
    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", _fake_run_worker_pool(calls, errors, progress))


def test_pool_rows_carry_per_row_out_dir_and_file_index():
    """_build_pool_rows (pure): pool indices consecutive from 0; file_index stays
    chapter-local; out_dir is each chapter's own package; fatal / no-pending files
    contribute no rows; the input segments are not mutated."""
    from backend.engines.tts_batch import _PooledFile, _build_pool_rows
    s_segs = [{"index": 0, "speaker": "A", "text": "a", "instruct": "", "pause_after": None},
              {"index": 1, "speaker": "B", "text": "b", "instruct": "", "pause_after": None}]
    t_segs = [{"index": 0, "speaker": "C", "text": "c", "instruct": "", "pause_after": None},
              {"index": 2, "speaker": "C", "text": "d", "instruct": "", "pause_after": None}]  # local gap kept
    fatal = _PooledFile(name="bad.json", error="无法解析")
    empty = _PooledFile(name="empty.json", pending=[])
    s = _PooledFile(name="s.json", out_dir=Path("C:/ws/05_audio_chunk/s"),
                    all_segments=s_segs, by_index={x["index"]: x for x in s_segs}, pending=[0, 1])
    t = _PooledFile(name="t.json", out_dir=Path("C:/ws/05_audio_chunk/t"),
                    all_segments=t_segs, by_index={x["index"]: x for x in t_segs}, pending=[0, 2])
    rows, owners = _build_pool_rows([s, fatal, t, empty])
    assert [r["index"] for r in rows] == [0, 1, 2, 3]  # pool-global, consecutive
    assert [r["file_index"] for r in rows] == [0, 1, 0, 2]  # chapter-local, gaps kept
    assert [Path(r["out_dir"]) for r in rows] == [
        Path("C:/ws/05_audio_chunk/s")] * 2 + [Path("C:/ws/05_audio_chunk/t")] * 2
    assert [o.name for o in owners] == ["s.json", "s.json", "t.json", "t.json"]
    assert rows[0]["speaker"] == "A" and rows[3]["text"] == "d"  # the chapter's fields ride along
    assert s_segs[0] == {"index": 0, "speaker": "A", "text": "a", "instruct": "", "pause_after": None}


def test_synthesize_multi_one_done_file_contributes_no_pool_rows(workspace, monkeypatch):
    """A fully-done file contributes no pool rows — one engine spawn for the pending file;
    the pool's rows carry per-row chapter attribution; results aggregate per chapter."""
    ws = workspace
    _seed_second_file(ws)
    _seed_done_package(ws, "s", [(0, "A", "hello", "0001.mp3"), (1, "B", "world", "0002.mp3")])
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)

    assert len(calls) == 1  # the done file short-circuits (no wasted model load)
    rows = json.loads(Path(calls[0][calls[0].index("--segments-file") + 1]).read_text("utf-8"))
    assert len(rows) == 2  # t's two rows only — s contributed none
    assert [r["index"] for r in rows] == [0, 1]  # pool-global positions, consecutive
    assert [r["file_index"] for r in rows] == [0, 1]  # t's chapter-local line positions
    assert all(r["out_dir"].endswith(os.path.join("05_audio_chunk", "t")) for r in rows)
    # --out-dir is the first POOLED file's package (fallback only, never a per-row dir here)
    assert _cmd_flag(calls[0], "--out-dir").endswith(os.path.join("05_audio_chunk", "t"))
    assert [f["script"] for f in result["files"]] == ["s.json", "t.json"]  # request order
    s, t = result["files"]
    assert s["total"] == 2 and s["completed"] == 2 and s["error"] is None  # no-rows short form
    assert t["total"] == 2 and t["completed"] == 2 and t["error"] is None
    assert result["total"] == 4 and result["completed"] == 4 and result["failed"] == []
    assert result["done_count"] == 4 and result["all_count"] == 4
    # the done file's cumulative manifest survives the run (rewritten in the no-rows path)
    m = {e["index"]: e for e in json.loads(
        (ws / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))}
    assert m[0]["ok"] and m[1]["ok"]


def test_synthesize_multi_all_done_no_engine(workspace, monkeypatch):
    """Everything already done → zero engine spawns and NO failure (resume semantics)."""
    ws = workspace
    _seed_done_package(ws, "s", [(0, "A", "hello", "0001.mp3"), (1, "B", "world", "0002.mp3")])
    calls = []

    def run_worker(cmd, handle, on_line, **kw):
        calls.append(cmd)

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json"], 4)
    assert calls == []
    assert result["completed"] == 2 and result["files"][0]["error"] is None


def test_synthesize_multi_zero_segment_files_succeed(workspace, monkeypatch):
    """A file with entries but no synthesizable segments takes the zero-short-circuit:
    success, no engine, no (misleading) 'all failed' error."""
    ws = workspace
    (ws / "03_parsed_json" / "empty.json").write_text(
        json.dumps([{"speaker": "A", "text": "   "}]), encoding="utf-8")
    calls = []

    def run_worker(cmd, handle, on_line, **kw):
        calls.append(cmd)

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["empty.json"], 4)
    assert calls == []
    assert result["completed"] == 0 and result["files"][0]["error"] is None
    assert result["files"][0]["total"] == 0


def test_synthesize_multi_failed_segments_tagged_with_script(workspace, monkeypatch):
    """One failed segment in chapter 2: the run succeeds; the failure carries its file name
    and LOCAL index; the chapter's error stays None (engine-level failures are NOT a
    prep fatal) and the package manifest records ok:false."""
    ws = workspace
    _seed_second_file(ws)
    calls = []
    _stub_engine_pool(monkeypatch, calls, errors={("t", 1)})
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    assert result["completed"] == 3  # 2 from s.json + 1 from t.json
    assert len(result["failed"]) == 1
    assert result["failed"][0] == {"index": 1, "speaker": "C",
                                    "reason": "强制失败（fake worker）", "script": "t.json"}
    assert result["files"][0]["failed"] == 0 and result["files"][1]["failed"] == 1
    assert result["files"][1]["error"] is None  # new semantics: engine-level ≠ error
    t = {e["index"]: e for e in json.loads(
        (ws / "05_audio_chunk" / "t" / "manifest.json").read_text("utf-8"))}
    assert t[0]["ok"] and t[1]["ok"] is False and t[1]["reason"] == "强制失败（fake worker）"


def test_synthesize_multi_progress_driven_by_worker_pool_span(workspace, monkeypatch):
    """The whole pool's progress is driven by the worker's [progress] lines (0→1); the
    backend's pooled path emits no per-file progress windows or labels."""
    ws = workspace
    _seed_second_file(ws)
    calls = []
    _stub_engine_pool(monkeypatch, calls, progress=True)
    h = _Handle()
    tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    fracs = [p[0] for p in h.progresses]
    assert fracs == [0.05, 1.0, 1.0]  # the worker's pool span, then the final settle
    assert all(a <= b + 1e-9 for a, b in zip(fracs, fracs[1:]))  # monotonic non-decreasing
    labels = [lbl for _f, lbl in h.progresses]
    assert not any("启动引擎" in lbl for lbl in labels)  # the backend never emits its own
    assert not any(lbl.startswith("s.json") or lbl.startswith("t.json") for lbl in labels)
    assert labels[-1] == "完成"


def test_synthesize_multi_per_file_fatal_error_isolated(workspace, monkeypatch):
    """A corrupt file 2 is a recorded PREP-stage error — file 1's success is kept, the bad
    file contributes no pool rows, no exception."""
    ws = workspace
    (ws / "03_parsed_json" / "bad.json").write_text("{ not json", encoding="utf-8")
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "bad.json"], 4)
    assert result["files"][0]["completed"] == 2 and result["files"][0]["error"] is None
    assert result["files"][1]["error"] and "无法解析" in result["files"][1]["error"]
    assert result["files"][1]["total"] == 0  # fatal files are all-zeros
    assert result["completed"] == 2  # the pool as a whole succeeded
    assert any(lvl == "ERROR" for lvl, _msg in h.logs)  # the isolation is logged
    assert len(calls) == 1
    rows = json.loads(Path(calls[0][calls[0].index("--segments-file") + 1]).read_text("utf-8"))
    assert all(Path(r["out_dir"]).name == "s" for r in rows)  # bad.json never joined the pool


@pytest.mark.parametrize("affordable_chapters", [1, 2])
def test_synthesize_multi_admits_chapters_until_character_quota_is_exhausted(
    workspace, monkeypatch, affordable_chapters,
):
    """Synthesize affordable chapters in order and report the rest as insufficient."""
    ws = workspace
    _seed_second_file(ws)
    (ws / "03_parsed_json" / "u.json").write_text(
        json.dumps([{"speaker": "D", "text": "tres"}]), encoding="utf-8",
    )
    reservations = []

    def reserve_chapter(char_count, operation_type):
        reservations.append((char_count, operation_type))
        return len(reservations) <= affordable_chapters

    monkeypatch.setattr("backend.platform.quota.reserve_tts_quota", reserve_chapter)
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    handle = _Handle()
    result = tts_batch.synthesize_multi(handle, ["s.json", "t.json", "u.json"], 4)

    assert len(reservations) == affordable_chapters + 1  # later chapters skip reservation
    pooled_chapters = [Path(row["out_dir"]).name for row in json.loads(
        Path(calls[0][calls[0].index("--segments-file") + 1]).read_text("utf-8")
    )]
    assert pooled_chapters == [name for chapter in ["s", "t"][:affordable_chapters]
                               for name in [chapter, chapter]]
    assert result["files"][0]["completed"] == 2
    if affordable_chapters == 2:
        assert result["files"][1]["completed"] == 2
    else:
        assert "额度不足" in result["files"][1]["error"]
    assert "额度不足" in result["files"][2]["error"]
    assert sum("额度不足" in message for _level, message in handle.logs) == 3 - affordable_chapters


def test_synthesize_multi_all_files_fail_raises(workspace):
    """Nothing got synthesized while files WERE attempted (prep fatals) → the task fails."""
    ws = workspace
    (ws / "03_parsed_json" / "bad.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="全部无法合成"):
        tts_batch.synthesize_multi(_Handle(), ["bad.json", "also-missing.json"], 4)


def test_synthesize_multi_cancel_in_prep_never_reaches_engine(workspace, monkeypatch):
    """A cancel at the prep-stage boundary propagates (never 'file failed, keep going') —
    the engine never spawns."""
    ws = workspace
    _seed_second_file(ws)
    from backend.core.task_control import TaskCancelled

    calls = []
    _stub_engine_pool(monkeypatch, calls)
    h = _Handle()
    n = 0

    def check():
        nonlocal n
        n += 1
        if n == 2:  # the boundary check before file 2's prep
            raise TaskCancelled()

    h.check = check
    with pytest.raises(TaskCancelled):
        tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    assert calls == []


def test_synthesize_multi_cancel_keeps_finished_pool_work(workspace, monkeypatch):
    """A cancel mid-run propagates; the finished work's manifests are force-flushed and
    kept."""
    ws = workspace
    _seed_second_file(ws)
    from backend.core.task_control import TaskCancelled

    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        fallback = cmd[cmd.index("--out-dir") + 1]
        width = max(4, len(str(len(segs))))
        for s in segs:
            local = s.get("file_index", s["index"])
            num = int(local) + 1
            out = s.get("out_dir") or fallback
            (Path(out) / f"{num:0{width}d}.mp3").write_bytes(b"fake")
            on_line(f"[segment] {s['index']} ok {os.path.join(out, f'{num:0{width}d}.mp3')}")
        handle.check()  # the cancel arrives while the child is still draining

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    n = 0

    def check():
        nonlocal n
        n += 1
        if n == 3:  # file 1 prep, file 2 prep, then the in-run check
            raise TaskCancelled()

    h.check = check
    with pytest.raises(TaskCancelled):
        tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    assert len(calls) == 1
    by = {e["index"]: e for e in json.loads(
        (ws / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))}
    assert by[0]["ok"] and by[1]["ok"]  # file 1's finished work is kept


def test_synthesize_multi_cancel_flushes_all_manifests(workspace, monkeypatch):
    """A cancel force-flushes EVERY pooled file's manifest — even a chapter whose rows the
    child never reported (its manifest is written with all rows not-done)."""
    ws = workspace
    _seed_second_file(ws)
    from backend.core.task_control import TaskCancelled

    calls = []

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        fallback = cmd[cmd.index("--out-dir") + 1]
        for s in segs:  # report ONLY s's rows before the cancel arrives
            out = s.get("out_dir") or fallback
            if Path(out).name != "s":
                continue
            local = s.get("file_index", s["index"])
            num = int(local) + 1
            (Path(out) / f"{num:04d}.mp3").write_bytes(b"fake")
            on_line(f"[segment] {s['index']} ok {os.path.join(out, f'{num:04d}.mp3')}")
        handle.check()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    n = 0

    def check():
        nonlocal n
        n += 1
        if n == 3:  # file 1 prep, file 2 prep, then the in-run check
            raise TaskCancelled()

    h.check = check
    with pytest.raises(TaskCancelled):
        tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    s = {e["index"]: e for e in json.loads(
        (ws / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))}
    assert s[0]["ok"] and s[1]["ok"]
    t = {e["index"]: e for e in json.loads(
        (ws / "05_audio_chunk" / "t" / "manifest.json").read_text("utf-8"))}
    assert t[0]["ok"] is False and t[1]["ok"] is False  # written anyway (not-done rows)


def test_synthesize_multi_resume_across_files(workspace, monkeypatch):
    """A pre-done segment in one chapter is excluded from the pool (resume) and its
    manifest path is preserved; the pool holds the rest — one engine spawn."""
    ws = workspace
    _seed_second_file(ws)
    _seed_done_package(ws, "s", [(0, "A", "hello", "0001.mp3")])  # s: index 0 done
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)

    assert len(calls) == 1
    rows = json.loads(Path(calls[0][calls[0].index("--segments-file") + 1]).read_text("utf-8"))
    # pool = s's remaining index-1 row + both of t's rows; file_index stays chapter-local
    assert [(Path(r["out_dir"]).name, r["file_index"]) for r in rows] == [
        ("s", 1), ("t", 0), ("t", 1)]
    assert [r["index"] for r in rows] == [0, 1, 2]  # pool-global, consecutive
    # the pre-done s/0001.mp3 keeps its original path (workspace-relative, forward-slash
    # form — the pathio on-disk convention)
    m = {e["index"]: e for e in json.loads(
        (ws / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))}
    assert m[0]["ok"] and m[0]["path"].endswith("05_audio_chunk/s/0001.mp3")
    assert m[1]["ok"] and m[1]["path"].endswith("05_audio_chunk/s/0002.mp3")
    s, t = result["files"]
    assert s["total"] == 1 and s["completed"] == 1  # only the pending row was pooled
    assert s["done_count"] == 2 and s["all_count"] == 2
    assert t["total"] == 2 and t["completed"] == 2


def test_synthesize_multi_watchdog_pool_index_mapping(workspace, monkeypatch):
    """A workers==1 strike targets the POOL index and lands on its owning chapter's
    manifest (never a chapter-internal index): pool index 2 is t's local 0, so the
    isolation lands in t's package only."""
    ws = workspace
    _seed_second_file(ws)
    POISON = 2  # the pool index of the poison row (t's local 0)
    kills = [1, 1]  # two whole-process watchdog kills, then a clean run

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        fallback = cmd[cmd.index("--out-dir") + 1]
        width = max(4, len(str(len(segs))))
        if kills:
            # the hung sub-batch holds POISON: the worker names its POOL indices in the
            # [watchdog] line, then the whole process dies (exit 124)
            kills.pop(0)
            on_line(f"[watchdog] timeout batch=1 indices=[{POISON}] elapsed=99s")
            raise WorkerWatchdogTimeout()
        for s in segs:
            local = s.get("file_index", s["index"])
            num = int(local) + 1
            out = s.get("out_dir") or fallback
            (Path(out) / f"{num:0{width}d}.mp3").write_bytes(b"fake")
            on_line(f"[segment] {s['index']} ok {os.path.join(out, f'{num:0{width}d}.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "t.json"], 1)

    # pool: s(0,1) then t(2,3) — the strike isolated pool index 2 = t's local 0
    assert result["completed"] == 3 and len(result["failed"]) == 1
    assert result["failed"][0] == {"index": 0, "speaker": "C",
                                    "reason": "超时（已隔离）", "script": "t.json"}
    t = {e["index"]: e for e in json.loads(
        (ws / "05_audio_chunk" / "t" / "manifest.json").read_text("utf-8"))}
    assert t[0]["ok"] is False and "隔离" in t[0]["reason"]
    assert t[1]["ok"] is True
    s = {e["index"]: e for e in json.loads(
        (ws / "05_audio_chunk" / "s" / "manifest.json").read_text("utf-8"))}
    assert s[0]["ok"] is True and s[1]["ok"] is True  # s's package untouched by the strike


def test_synthesize_multi_watchdog_records_and_restore(workspace, monkeypatch):
    """The pooled loop records demotions the same way: the chars are the POOL rows' text
    (pool-global in-flight indices -> rows), and a [restore] line re-syncs the pool loop's
    workers for the next restart's command."""
    ws = workspace
    _seed_second_file(ws)
    calls = []
    kills = [1]  # one whole-process watchdog kill, then a clean run

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls.append(cmd)
        if kills:
            # the hung sub-batch holds pool indices 0,1 (s's rows: 'hello' + 'world' = 10 chars)
            kills.pop(0)
            on_line("[watchdog] timeout batch=1 indices=[0, 1] elapsed=99s")
            raise WorkerWatchdogTimeout()
        # second run: a small batch restores cap 4, then the whole pool finishes cleanly
        on_line("[restore] cap=4")
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        fallback = cmd[cmd.index("--out-dir") + 1]
        width = max(4, len(str(len(segs))))
        for s in segs:
            local = s.get("file_index", s["index"])
            out = s.get("out_dir") or fallback
            (Path(out) / f"{local + 1:0{width}d}.mp3").write_bytes(b"fake")
            on_line(f"[segment] {s['index']} ok {os.path.join(out, f'{local + 1:0{width}d}.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)

    assert len(calls) == 2
    assert _cmd_flag(calls[0], "--concurrency") == "4"
    assert "--restore-stack" not in calls[0]  # no demotion yet -> legacy command
    # 10 = len('hello') + len('world') — the pool rows' own text, keyed by pool index
    assert _cmd_flag(calls[1], "--restore-stack") == "10:4"
    assert _cmd_flag(calls[1], "--concurrency") == "1"  # actual timed-out rows: 2 -> 1
    assert any("恢复为 4 段" in msg for _lvl, msg in h.logs)
    assert result["completed"] == 4 and result["failed"] == []


def test_synthesize_multi_one_file_engine_level_all_failed_isolated_in_failed_list(workspace, monkeypatch):
    """Every pooled segment of ONE chapter fails: the task still succeeds; the chapter's
    error stays None and its failures land in the top-level failed list + its manifest."""
    ws = workspace
    _seed_second_file(ws)
    calls = []
    _stub_engine_pool(monkeypatch, calls, errors={("t", 0), ("t", 1)})
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    assert result["completed"] == 2  # s.json's two rows
    s, t = result["files"]
    assert t["error"] is None and t["completed"] == 0 and t["failed"] == 2
    assert all(f["script"] == "t.json" for f in result["failed"])
    assert sorted(f["index"] for f in result["failed"]) == [0, 1]  # chapter-local indices
    m = {e["index"]: e for e in json.loads(
        (ws / "05_audio_chunk" / "t" / "manifest.json").read_text("utf-8"))}
    assert m[0]["ok"] is False and m[1]["ok"] is False
    assert h.progresses[-1] == (1.0, "完成")  # settled as a success


def test_synthesize_multi_all_pool_segments_fail_raises(workspace, monkeypatch):
    """Every pooled segment fails (engine-level) → the task fails; the manifests are still
    written (all ok:false)."""
    ws = workspace
    _seed_second_file(ws)
    calls = []
    _stub_engine_pool(monkeypatch, calls, errors={("s", 0), ("s", 1), ("t", 0), ("t", 1)})
    h = _Handle()
    with pytest.raises(RuntimeError, match="段合成失败"):
        tts_batch.synthesize_multi(h, ["s.json", "t.json"], 4)
    for pkg in ("s", "t"):
        m = {e["index"]: e for e in json.loads(
            (ws / "05_audio_chunk" / pkg / "manifest.json").read_text("utf-8"))}
        assert all(v["ok"] is False for v in m.values())


def test_synthesize_multi_result_shape_and_file_order(workspace, monkeypatch):
    """The result dict's shape is pinned (top-level 7 keys; files entries 9 keys) and the
    files order is the request order — including the prep-fatal placeholder."""
    ws = workspace
    _seed_second_file(ws)
    (ws / "03_parsed_json" / "bad.json").write_text("{ not json", encoding="utf-8")
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "bad.json", "t.json"], 4)
    assert set(result) == {"total", "completed", "failed", "output_dir", "manifest_path",
                           "done_count", "all_count", "files"}
    assert result["output_dir"] == "" and result["manifest_path"] == ""
    assert [f["script"] for f in result["files"]] == ["s.json", "bad.json", "t.json"]
    for f in result["files"]:
        assert set(f) == {"script", "total", "completed", "failed", "output_dir",
                          "manifest_path", "done_count", "all_count", "error"}
    bad = result["files"][1]
    assert bad["error"] and all(bad[k] == 0 for k in
                                ("total", "completed", "failed", "done_count", "all_count"))
    assert bad["output_dir"] == "" and bad["manifest_path"] == ""


def test_synthesize_multi_cmd_shape_unchanged_for_pool(workspace, monkeypatch):
    """The pooled run's worker command keeps the exact --flag value pairing (no new flags);
    --concurrency is ONE pool-level value."""
    ws = workspace
    _seed_second_file(ws)
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    tts_batch.synthesize_multi(_Handle(), ["s.json", "t.json"], 4)
    cmd = calls[0]
    assert "--design-batch" not in " ".join(cmd)  # sanity: it is a batch command
    assert cmd[cmd.index("--mode") + 1] == "batch"
    # every flag token is paired with a value: walk tokens, --* always has a successor
    tokens = cmd[2:]
    i = 0
    while i < len(tokens):
        assert tokens[i].startswith("--") and i + 1 < len(tokens), f"unpaired flag {tokens[i]}"
        i += 2
    assert _cmd_flag(cmd, "--concurrency") == "4"
    for flag in ("--segments-file", "--voice-config", "--out-dir", "--language", "--device",
                 "--seed", "--workspace"):
        assert flag in cmd


def test_synthesize_multi_package_collision_guarded(workspace, monkeypatch):
    """x.json + x_checked.json map to ONE package — the second is a prep fatal (clear
    error), not a silent overwrite of the first's files/manifest."""
    ws = workspace
    # s_checked.json is a distinct script resolving to the same package "s"
    (ws / "03_parsed_json" / "s_checked.json").write_text(
        json.dumps([{"speaker": "Z", "text": "z1"}, {"speaker": "Z", "text": "z2"}]),
        encoding="utf-8")
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    h = _Handle()
    result = tts_batch.synthesize_multi(h, ["s.json", "s_checked.json"], 4)
    first, second = result["files"]
    assert first["error"] is None and first["completed"] == 2
    assert second["error"] and "s" in second["error"] and "冲突" in second["error"]
    assert second["total"] == 0  # the colliding file never joined the pool
    rows = json.loads(Path(calls[0][calls[0].index("--segments-file") + 1]).read_text("utf-8"))
    assert all(Path(r["out_dir"]).name == "s" and r["file_index"] in (0, 1) for r in rows)
    assert [r["file_index"] for r in rows] == [0, 1]  # only s.json's rows (its local lines)


# --------------------------------------------------------------------------- #
# batch-status multi-file (the per-row 待合成 stats)
# --------------------------------------------------------------------------- #

def test_batch_status_multi_counts(workspace):
    from backend.api.tts import batch_status
    _seed_second_file(workspace)
    _seed_voice_config(workspace, {
        "A": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav"},
        # B deliberately absent from voice_config -> not ready
        "C": {"type": "clone", "ref_audio": "04_voice_profiles/c.wav"},
    })
    r = batch_status(None, ["s.json", "t.json", "nope.json"])
    assert [f["name"] for f in r["files"]] == ["s.json", "t.json", "nope.json"]  # request order
    s, t, z = r["files"]
    assert (s["total"], s["completed"], s["remaining"]) == (2, 0, 2)
    assert s["complete"] is False
    assert (s["speakers"], s["ready"]) == (2, 1) and s["missing"] == ["B"]
    assert (t["total"], t["completed"]) == (2, 0)
    assert (t["speakers"], t["ready"], t["missing"]) == (1, 1, [])
    assert z == {"name": "nope.json", "total": 0, "completed": 0, "remaining": 0,
                 "complete": False, "speakers": 0, "ready": 0, "missing": []}
    # a file whose every segment is done (ok + file on disk) earns the 已合成 flag
    _seed_done_package(workspace, "s", [(0, "A", "hello", "0001.mp3"),
                                        (1, "B", "world", "0002.mp3")])
    assert batch_status(None, ["s.json"])["files"][0]["complete"] is True


def test_batch_status_keeps_voice_change_stale_after_restart(workspace):
    """The persisted manifest/config comparison survives a status-cache reset."""
    from backend.api import tts as api_tts
    from backend.api.tts import batch_status

    config = {
        "A": {"type": "clone", "ref_audio": "04_voice_profiles/a-1.wav"},
        "B": {"type": "clone", "ref_audio": "04_voice_profiles/b.wav"},
    }
    _seed_voice_config(workspace, config)
    _seed_done_package(workspace, "s", [(0, "A", "hello", "0001.mp3"),
                                        (1, "B", "world", "0002.mp3")])
    assert batch_status(None, ["s.json"])["files"][0]["complete"] is True

    changed = {**config, "A": {**config["A"], "ref_audio": "04_voice_profiles/a-2.wav"}}
    _seed_voice_config(workspace, changed)
    api_tts.reset_batch_status_cache()  # simulate a backend restart
    row = batch_status(None, ["s.json"])["files"][0]

    assert (row["completed"], row["remaining"], row["complete"]) == (1, 1, False)
    assert row["stale_speakers"] == ["A"]


def test_batch_status_multi_digest_cache_hit_and_reset(workspace):
    """The multi-file rows are per-package digests keyed on (workspace, src, manifest,
    pkg dir, voice_config) file stats: an unchanged second call serves the cache, each
    call returns a FRESH row (mutating one must not leak into later calls), and
    ``reset_batch_status_cache`` drops the digest for a clean recompute."""
    from backend.api import tts as api_tts
    _seed_second_file(workspace)
    _seed_voice_config(workspace, {
        "A": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav"},
        "C": {"type": "clone", "ref_audio": "04_voice_profiles/c.wav"},
    })
    r1 = api_tts.batch_status(None, ["s.json", "t.json"])
    r1["files"][0]["missing"] = ["HACKED"]   # mutate the returned row …
    r1["files"][0]["completed"] = 99
    r2 = api_tts.batch_status(None, ["s.json", "t.json"])  # … a cache hit must not see it
    assert (r2["files"][0]["completed"], r2["files"][0]["missing"]) == (0, ["B"])
    assert (r2["files"][1]["total"], r2["files"][1]["completed"]) == (2, 0)
    api_tts.reset_batch_status_cache()  # the seam: force a clean recompute
    r3 = api_tts.batch_status(None, ["s.json", "t.json"])
    assert (r3["files"][0]["total"], r3["files"][0]["completed"],
            r3["files"][0]["missing"]) == (2, 0, ["B"])
    assert r3["files"][0] is not r2["files"][0]


def test_batch_status_multi_digest_cache_serves_unchanged_calls(workspace, monkeypatch):
    """The 3s-poll steady state: with no file change between calls, the expensive row
    builder runs ONCE per package — later calls are served from the digest cache."""
    from backend.api import tts as api_tts
    _seed_second_file(workspace)
    built: list[str] = []
    real = api_tts._file_batch_status

    def counting(name, layout, vc, out_dir=None):
        built.append(name)
        return real(name, layout, vc, out_dir=out_dir)

    monkeypatch.setattr(api_tts, "_file_batch_status", counting)
    for _ in range(3):  # three polls, nothing changes
        r = api_tts.batch_status(None, ["s.json", "t.json"])
        assert (r["files"][0]["total"], r["files"][0]["completed"]) == (2, 0)
    assert built == ["s.json", "t.json"]  # computed once, served twice from the cache
    # A real change (a done package lands on disk) invalidates the digest …
    _seed_done_package(workspace, "s", [(0, "A", "hello", "0001.mp3"),
                                        (1, "B", "world", "0002.mp3")])
    assert api_tts.batch_status(None, ["s.json"])["files"][0]["complete"] is True
    assert built[-1] == "s.json"  # … so exactly s.json's row was recomputed


def test_batch_status_multi_rejects_all(workspace):
    from fastapi import HTTPException

    from backend.api.tts import batch_status
    with pytest.raises(HTTPException) as ei:
        batch_status(None, ["__all__"])
    assert ei.value.status_code == 400


def test_batch_status_multi_no_workspace_degrades(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    core_paths.reset_layout_cache()
    try:
        from backend.api.tts import batch_status
        r = batch_status(None, ["a.json", "b.json"])
        assert r == {"files": [
            {"name": n, "total": 0, "completed": 0, "remaining": 0,
             "complete": False, "speakers": 0, "ready": 0, "missing": []}
            for n in ("a.json", "b.json")]}
    finally:
        core_config.reset_config_cache()


# --------------------------------------------------------------------------- #
# run_batch dispatch (the API layer)
# --------------------------------------------------------------------------- #

def _durable_ctx():
    from types import SimpleNamespace
    return SimpleNamespace(user=SimpleNamespace(id="user-1"), session=object())


def test_run_batch_submits_durable_task_with_selected_scripts(workspace, monkeypatch):
    import backend.api.tts as tts_api
    from backend.api.tts import BatchRequest, run_batch

    submitted = []
    def submit(**kwargs):
        submitted.append(kwargs)
        return {"id": "durable-task"}
    monkeypatch.setattr(tts_api, "submit_legacy_engine_task", submit)

    result = run_batch(
        BatchRequest(scripts=["s.json", "t.json"]), ctx=_durable_ctx(), db=object(),
    )

    assert result == {"task_id": "durable-task"}
    assert submitted[0]["task_type"] == "tts.batch"
    assert submitted[0]["payload"]["scripts"] == ["s.json", "t.json"]
    assert submitted[0]["payload"]["indices"] is None


def test_run_batch_keeps_single_file_and_index_validation(workspace, monkeypatch):
    import backend.api.tts as tts_api
    from backend.api.tts import BatchRequest, run_batch

    monkeypatch.setattr(tts_api, "submit_legacy_engine_task", lambda **kwargs: {"id": "task"})
    result = run_batch(
        BatchRequest(script="s.json", indices=[0, 2]), ctx=_durable_ctx(), db=object(),
    )
    assert result == {"task_id": "task"}
    with pytest.raises(Exception) as exc:
        run_batch(
            BatchRequest(scripts=["s.json", "t.json"], indices=[0]),
            ctx=_durable_ctx(), db=object(),
        )
    assert getattr(exc.value, "status_code", None) == 400


def test_run_batch_rejects_all_parsed_json_sentinel(workspace):
    from fastapi import HTTPException
    from backend.api.tts import BatchRequest, run_batch

    with pytest.raises(HTTPException) as exc:
        run_batch(BatchRequest(scripts=["__all__"]), ctx=_durable_ctx(), db=object())
    assert exc.value.status_code == 400


def test_reset_batch_submits_durable_task_and_blocks_active_synthesis(workspace, monkeypatch):
    import backend.api.tts as tts_api
    from fastapi import HTTPException
    from backend.api.tts import ResetBatchRequest, reset_batch

    submitted = []
    monkeypatch.setattr(tts_api, "has_active_durable_tasks", lambda **_kwargs: False)
    monkeypatch.setattr(
        tts_api, "submit_legacy_engine_task",
        lambda **kwargs: submitted.append(kwargs) or {"id": "reset-task"},
    )
    result = reset_batch(
        ResetBatchRequest(scripts=["s.json", "t_checked.json"]),
        ctx=_durable_ctx(), db=object(),
    )
    assert result == {"task_id": "reset-task"}
    assert submitted[0]["task_type"] == "tts.reset"
    assert submitted[0]["payload"]["scripts"] == ["s.json", "t_checked.json"]

    monkeypatch.setattr(tts_api, "has_active_durable_tasks", lambda **_kwargs: True)
    with pytest.raises(HTTPException) as exc:
        reset_batch(ResetBatchRequest(scripts=["s.json"]), ctx=_durable_ctx(), db=object())
    assert exc.value.status_code == 409


def test_reset_batch_rejects_empty_and_all_sentinel(workspace):
    from fastapi import HTTPException
    from backend.api.tts import ResetBatchRequest, reset_batch

    for scripts in ([], ["__all__"]):
        with pytest.raises(HTTPException) as exc:
            reset_batch(ResetBatchRequest(scripts=scripts), ctx=_durable_ctx(), db=object())
        assert exc.value.status_code == 400


def test_filename_width_helper():
    """The zero-pad width = the digits the LARGEST package's full count needs, floored at 4
    — the 10^k boundary is where a package's names grow a digit (9999 -> 4 digits,
    10000 -> 5)."""
    from backend.engines.tts_batch import filename_width
    assert filename_width(0) == 4
    assert filename_width(1) == 4
    assert filename_width(213) == 4
    assert filename_width(9999) == 4
    assert filename_width(10000) == 5
    assert filename_width(40798) == 5
    assert filename_width(99999) == 5
    assert filename_width(100000) == 6
    assert filename_width(-5) == 4  # negative clamps to the floor


def test_build_cmd_threads_width_flag():
    """_build_cmd appends --width only when the backend set one (0 = absent, so a legacy /
    manual run keeps the exact command it used to receive)."""
    from backend.engines.tts_batch import _build_cmd
    kw = dict(language="chinese", device="auto", model="", base_model="", design_model="",
              ffmpeg_path="", concurrency=4, seed=-1)
    cmd = _build_cmd("/py", "/w", "/seg.json", "/vc.json", "/out", width=5, **kw)
    assert cmd[cmd.index("--width") + 1] == "5"
    legacy = _build_cmd("/py", "/w", "/seg.json", "/vc.json", "/out", width=0, **kw)
    assert "--width" not in legacy


def test_single_file_width_is_full_count_not_pending(workspace, monkeypatch):
    """The width must be a function of the file's FULL segment count, not the pending
    subset — a pending-derived width shrinks as the job completes, which is exactly how a
    package filled across several runs ended up with mixed 000x / 0000x names."""
    ws = workspace
    _seed_done_package(ws, "s", [(0, "A", "hello", "0001.mp3")])  # 1 of 2 done -> 1 pending
    seen = []
    real = tts_batch.filename_width

    def spy(count):
        seen.append(count)
        return real(count)

    monkeypatch.setattr(tts_batch, "filename_width", spy)
    captured = {}
    _stub_engine(monkeypatch, captured)
    tts_batch.synthesize(_Handle(), None, "s.json", None)  # resume
    assert seen == [2]  # the full count, not the pending 1
    assert _cmd_flag(captured["cmd"], "--width") == str(real(2))


def test_multi_file_width_is_largest_package_full_count(workspace, monkeypatch):
    """Pooled run: ONE width for the whole pool = the digits the LARGEST package's FULL
    count needs — not the pool total, not each file's own count. Every package in the run
    gets the same digit count, so a chapter re-synthesized later in the run lands on names
    that line up with the chapters synthesized earlier."""
    ws = workspace
    _seed_second_file(ws)  # t.json: 2 full segments
    (ws / "03_parsed_json" / "t.json").write_text(  # grow t to 12 full segments
        json.dumps([{"speaker": "C", "text": f"line {i}"} for i in range(12)],
                   ensure_ascii=False), encoding="utf-8")
    _seed_done_package(ws, "t", [(0, "C", "line 0", "0001.mp3")])  # 11 of 12 pending
    seen = []
    real = tts_batch.filename_width

    def spy(count):
        seen.append(count)
        return real(count)

    monkeypatch.setattr(tts_batch, "filename_width", spy)
    calls = []
    _stub_engine_pool(monkeypatch, calls)
    tts_batch.synthesize_multi(_Handle(), ["s.json", "t.json"], 4)
    assert seen == [12]  # the largest package's full count — not 14 (pool total), not per-file
    assert len(calls) == 1  # one engine spawn for the whole pool -> one width
    assert _cmd_flag(calls[0], "--width") == str(real(12)) == "4"
    # both packages' fresh files carry the same digit count (the run-wide width)
    s_files = sorted(p.name for p in (ws / "05_audio_chunk" / "s").glob("*.mp3"))
    t_files = sorted(p.name for p in (ws / "05_audio_chunk" / "t").glob("*.mp3"))
    assert s_files == ["0001.mp3", "0002.mp3"]
    assert len(t_files) == 12 and t_files[0] == "0001.mp3" and t_files[-1] == "0012.mp3"


def test_single_file_width_stable_across_watchdog_restart(workspace, monkeypatch):
    """A watchdog restart re-writes the seg table as the (shrinking) remaining subset —
    the restart command must carry the SAME --width as the first (it is precomputed once,
    from the full count, before the run loop, and reused for every restart)."""
    ws = workspace
    _seed_long_script(ws)
    calls = []  # (cmd, seg-row indices AT CALL TIME — the file is rewritten in place on
    # restart, so it must be snapshotted per call, not read back afterwards)

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        calls.append((cmd, [s["index"] for s in segs]))
        if len(calls) == 1:
            # one segment settles, then the batch times out: the restart's pending table
            # is a strict subset of the first run's
            on_line(f"[segment] 0 ok {os.path.join(cmd[cmd.index('--out-dir') + 1], '0001.mp3')}")
            on_line("[watchdog] timeout batch=custom#1 indices=[1] elapsed=181.2")
            raise WorkerWatchdogTimeout("音频合成引擎失败（退出码 124）")
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            on_line(f"[segment] {s['index']} ok {os.path.join(out_dir, str(s['index'] + 1).zfill(4) + '.mp3')}")
        return deque()

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)
    result = tts_batch.synthesize(_Handle(), None, "long.json", 4)
    assert len(calls) == 2
    (cmd0, rows0), (cmd1, rows1) = calls
    assert rows0 == [0, 1]  # the first run sees the full table
    assert rows1 == [1]     # the restart's table is the shrinking remainder
    assert _cmd_flag(cmd0, "--width") == _cmd_flag(cmd1, "--width") == "4"
    assert result["completed"] == 2


def test_safe_package_name_snapshot():
    """Q5: merge-output and bgm side-file sanitizers collapsed into one function —
    behavior must match the two old inline copies exactly, including the deliberate
    differences from ``safe_display_name`` (no 180-char cap, dots preserved)."""
    from pathlib import Path

    from backend.engines.tts_manifest import merged_output_paths, _safe_package_name

    cases = {
        "my book": "my book",
        r'a/b:c*d?e"f<g>h|i\j': "a_b_c_d_e_f_g_h_i_j",
        "  padded  ": "padded",
        "   ": "audiobook",
        "": "audiobook",
        "中文 书名": "中文 书名",
        "dots..kept": "dots..kept",
        "long" * 50: "long" * 50,
    }
    for raw, expected in cases.items():
        assert _safe_package_name(raw) == expected, raw

    class _Layout:
        audio_merge = Path("/ws/06_audio_merge")

    assert [p.name for p in merged_output_paths(_Layout(), 'a/b:c')] == ["a_b_c.mp3", "a_b_c.wav"]


# ---------------------------------------------------------------------------
# 整章预览 render_preview（暂存区渲染，绝不触碰正式 05 / manifest / 03）
# ---------------------------------------------------------------------------

def _fake_preview_worker(captured):
    """A preview-mode child stand-in: writes the ACTUAL produced file (the worker's
    real behaviour) before emitting its ``[segment] ok`` line with the real path."""

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        captured["cmd"] = cmd
        with open(cmd[cmd.index("--segments-file") + 1], encoding="utf-8") as f:
            segs = json.load(f)
        captured["segments"] = segs
        out_dir = cmd[cmd.index("--out-dir") + 1]
        for s in segs:
            ext = captured.get("ext", ".mp3")
            path = os.path.join(out_dir, str(s["index"] + 1).zfill(4) + ext)
            with open(path, "wb") as f:
                f.write(b"0" * 16)
            on_line(f"[segment] {s['index']} ok {path}")
        return deque()

    return run_worker


def test_render_preview_writes_staging_only_and_records_state(workspace, monkeypatch):
    captured = {}
    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", _fake_preview_worker(captured))

    before_script = (workspace / "03_parsed_json" / "s.json").read_bytes()
    before_chunk = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))

    handle = _Handle()
    result = tts_batch.render_preview(
        handle, "s.json",
        [{"index": 0, "text": "你好啊", "speaker": "B", "instruct": "愤怒地"}],
    )

    # 产物在暂存区，文件名 1-based（worker 口径 file_index + 1）
    staging = workspace / "00_temp" / "chapter_preview" / "s"
    assert (staging / "0001.mp3").is_file()
    # state.json 记录实际产物名 + 覆盖后的三元组
    state = json.loads((staging / "state.json").read_text("utf-8"))
    line = state["lines"]["0"]
    assert line["ok"] is True
    assert line["file"] == "0001.mp3"
    assert line["text"] == "你好啊"
    assert line["speaker"] == "B"
    assert line["instruct"] == "愤怒地"
    assert line["fingerprint"] and line["rendered_at"]
    # worker 收到的段 = 覆盖后的值（含 1-based 无关的段表 index）
    seg = captured["segments"][0]
    assert (seg["index"], seg["text"], seg["speaker"], seg["instruct"]) == (0, "你好啊", "B", "愤怒地")
    # 单句运行：批内并发恒 1，宽度 = 全章行数口径（3 行 → 4）
    assert _cmd_flag(captured["cmd"], "--concurrency") == "1"
    assert _cmd_flag(captured["cmd"], "--width") == "4"
    assert _cmd_flag(captured["cmd"], "--out-dir") == str(staging)
    # 正式状态零改动：05 包内无任何文件（layout prepare 只建空目录）、03 字节一致
    assert not any((workspace / "05_audio_chunk").rglob("*"))
    assert (workspace / "03_parsed_json" / "s.json").read_bytes() == before_script
    after_chunk = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))
    new_paths = [p for p in after_chunk if p.startswith("05_audio_chunk/") and p not in before_chunk]
    assert not new_paths
    assert result["completed"] == 1 and result["failed"] == []


def test_render_preview_wav_fallback_records_actual_extension(workspace, monkeypatch):
    # MP3 编码失败时 worker 保留 WAV——state.json 的 file 必须记录实际扩展名（门禁/试听以此为据）
    captured = {"ext": ".wav"}
    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", _fake_preview_worker(captured))

    tts_batch.render_preview(_Handle(), "s.json", [{"index": 1, "instruct": "轻声"}])

    staging = workspace / "00_temp" / "chapter_preview" / "s"
    assert (staging / "0002.wav").is_file()
    state = json.loads((staging / "state.json").read_text("utf-8"))
    line = state["lines"]["1"]
    assert line["ok"] is True
    assert line["file"] == "0002.wav"
    # 未提供的字段不进入 render 条目（partial 三元组）
    assert captured["segments"] == [{"index": 1, "speaker": "B", "text": "world", "instruct": "轻声", "pause_after": None}]


def test_render_preview_watchdog_two_strikes_isolates_line_without_crashing(workspace, monkeypatch):
    # exit-124 两击 → 该行记 ok=false（超时已隔离），任务正常终态（不 raise）
    calls = {"n": 0}

    def run_worker(cmd, handle, on_line, *, temp_files=(), fail_prefix="TTS 引擎", **kw):
        calls["n"] += 1
        raise WorkerWatchdogTimeout("看门狗超时")

    monkeypatch.setattr(tts_batch, "resolve_engine", lambda: (Path("/fake/python"), Path("/fake/worker")))
    monkeypatch.setattr(tts_batch, "run_tts_subprocess", run_worker)

    result = tts_batch.render_preview(_Handle(), "s.json", [{"index": 0}])

    assert calls["n"] == 2
    staging = workspace / "00_temp" / "chapter_preview" / "s"
    state = json.loads((staging / "state.json").read_text("utf-8"))
    line = state["lines"]["0"]
    assert line["ok"] is False
    assert line["reason"] == "超时（已隔离）"
    assert line["file"] == ""
    assert result["completed"] == 0
    assert result["failed"] == [{"index": 0, "speaker": "A", "reason": "超时（已隔离）"}]


def test_render_preview_rejects_out_of_range_and_empty_render(workspace):
    with pytest.raises(RuntimeError):
        tts_batch.render_preview(_Handle(), "s.json", [{"index": 99}])
    with pytest.raises(RuntimeError):
        tts_batch.render_preview(_Handle(), "s.json", [])
    # 第 3 行（空文本行）被覆盖为空文本 → 明确报错
    with pytest.raises(RuntimeError):
        tts_batch.render_preview(_Handle(), "s.json", [{"index": 2, "text": "   "}])


def test_preview_lock_path_and_staging_dir_are_stable(workspace):
    layout = core_paths.get_or_prepare_layout()
    lock = tts_batch.preview_lock_path(layout, "s")
    staging = tts_batch.preview_staging_dir(layout, "s")
    assert lock == layout.temp / "locks" / "chapter_preview" / "s.lock"
    assert staging == layout.temp / "chapter_preview" / "s"
