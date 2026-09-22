"""Tests for the BGM batch dispatch (``api/bgm.py``): ordered + prefetched
coordinators over the REAL process-wide gates (shared LLM gate for analysis,
``merge_gate()`` for mix), the per-chapter in-flight 409s, the endpoint guards,
the synchronous ``POST /match`` write, and the read-only ``GET /chapters`` rows.

``run_analyze`` / ``run_mix`` / ``run_match`` / ``list_chapters`` /
``update_chapter`` are called directly (the suite never goes through the HTTP
layer). The engine workers are monkeypatched with controllable stubs that use
the REAL gates — the coordinators' decisions are driven by ``gate().active`` /
``merge_gate().active`` exactly as in production.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.api import bgm as api_bgm
from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core import concurrency
from backend.core.tasks import TERMINAL, TaskCancelled, TaskStatus, get_task_manager
from backend.engines import music as music_engine
import backend.engines.bgm as bgm_engine
import backend.engines.merge as merge_engine
import backend.engines.tts_batch as tts_batch

STEMS = [f"ch{i}" for i in range(1, 9)]  # up to 8 chapters


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """Throwaway project root + workspace (8 02 chapter files, one library
    track, one 06 narration + assignment per chapter) and drained gates after."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    (ws / "02_split_text").mkdir(parents=True)
    for s in STEMS:
        (ws / "02_split_text" / f"{s}.txt").write_bytes("本章内容。".encode("utf-8"))
    core_config.set_workspace_pointer(str(ws))
    core_config.update_config({"llm": {"model_name": "test-model"}})
    # one enabled, tagged library track
    lib = tmp_path / "music_library"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "t1.mp3").write_bytes(b"music-bytes")
    music_engine.update_index(
        lambda idx: idx["tracks"].update({
            "t1.mp3": {"duration": 120.0, "enabled": True, "description": "",
                       "tags": {"scene": [], "mood": ["紧张"], "emotion": [], "custom": []},
                       "added_at": ""}}))
    _seed_mix(ws, STEMS)
    limits = (concurrency.gate().limit, concurrency.merge_gate().limit)
    yield ws
    # A failed test must not leak gate slots / zombie stub threads into later tests:
    # cancel every still-active BGM task (stubs honour cooperative cancel and release
    # their slots), let BOTH gates drain, restore their limits.
    mgr = get_task_manager()
    for t in list(mgr.list()):
        if t.module in ("bgm-analysis", "bgm-segment", "bgm-mix") and t.status not in TERMINAL:
            try:
                mgr.control(t.id, "cancel")
            except KeyError:
                pass
    deadline = time.time() + 5
    stuck = []
    while time.time() < deadline:
        stuck = [t for t in mgr.list()
                 if t.module in ("bgm-analysis", "bgm-segment", "bgm-mix")
                 and t.status not in TERMINAL]
        if not stuck and concurrency.gate().active == 0 and concurrency.merge_gate().active == 0:
            break
        time.sleep(0.05)
    assert not stuck, f"bgm tasks leaked: {[t.label for t in stuck]}"
    assert concurrency.gate().active == 0 and concurrency.merge_gate().active == 0
    concurrency.set_concurrency(limits[0])
    concurrency.set_merge_concurrency(limits[1])
    core_config.reset_config_cache()


def _seed_mix(ws, stems: list[str], music: str | None = "t1.mp3") -> None:
    """06 narration mp3s + a (matched) assignment entry per stem."""
    (ws / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    for s in stems:
        (ws / "06_audio_merge" / f"{s}.mp3").write_bytes(b"NARR" * 16)
    layout = core_paths.get_layout()
    data = bgm_engine.load_assignments(layout)
    for s in stems:
        data["chapters"][s] = {"tags": {}, "music": music, "locked": False,
                               "manual": False, "score": 3, "reason": "r",
                               "matched_at": "t0"}
    bgm_engine.save_assignments(layout, data)


# -- 段落级（bgm-segment）seeding helpers -----------------------------------------

def _segment_entries(n: int, speakers=None) -> list[dict]:
    speakers = list(speakers) if speakers else ["老道"] * n
    return [{"speaker": speakers[i % len(speakers)], "text": f"段落{i}的文本。"}
            for i in range(n)]


def _seed_scripts(ws, stems: list[str], n: int = 6) -> dict[str, list[dict]]:
    """03 脚本 JSON（n 条）per stem — the paragraph-analysis input."""
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    out: dict[str, list[dict]] = {}
    for s in stems:
        e = _segment_entries(n)
        (ws / "03_parsed_json" / f"{s}.json").write_bytes(
            json.dumps(e, ensure_ascii=False).encode("utf-8"))
        out[s] = e
    return out


def _seed_segment_chapter(ws, stem: str, entries: list[dict], with_05: bool = True) -> None:
    """Full recompute input for one stem: 03 script + 05 manifest (ws-relative
    paths) + fake segment mp3s + 06 narration (the fixture already writes 06 —
    probes are stubbed, the rewrite is inert)."""
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    (ws / "03_parsed_json" / f"{stem}.json").write_bytes(
        json.dumps(entries, ensure_ascii=False).encode("utf-8"))
    if with_05:
        package = tts_batch.package_for(Path(f"{stem}.json"))
        pkg_dir = ws / "05_audio_chunk" / package
        pkg_dir.mkdir(parents=True, exist_ok=True)
        manifest = []
        for i, e in enumerate(entries):
            fname = f"{i + 1:04d}.mp3"
            (pkg_dir / fname).write_bytes(b"SEG" * 100)
            manifest.append({"index": i, "speaker": e["speaker"], "text": e["text"],
                             "pause_after": None,
                             "path": f"05_audio_chunk/{package}/{fname}",
                             "ok": True, "reason": ""})
        (pkg_dir / "manifest.json").write_bytes(
            json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    (ws / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    (ws / "06_audio_merge" / f"{stem}.mp3").write_bytes(b"NARR" * 256)


def _segment_block(start: int, end: int, intensity: int = 2, **tags) -> dict:
    """一个缓存的场景块（新场景形态：四桶标签 + 强度）。"""
    return {
        "start": start, "end": end, "scene": "", "mood": "", "reason": "",
        "music_tags": {c: list(tags.get(c) or [])
                       for c in music_engine.TAG_CATEGORIES},
        "intensity": intensity,
    }


def _seed_segment_analysis(stem: str, entries, blocks,
                           fingerprint: str | None = None) -> None:
    layout = core_paths.get_layout()
    data = bgm_engine.load_segment_analysis(layout)
    data["chapters"][stem] = {
        "fingerprint": fingerprint or bgm_engine.segment_fingerprint(entries),
        "entry_count": len(entries),
        "blocks": blocks,
        "model": "test-model",
        "analyzed_at": "t0",
        "edited": False,
    }
    bgm_engine.save_segment_analysis(layout, data)


def _fake_probe(segs_dur: dict, total: float):
    """probe_duration stand-in: 05 segment files (``000X.mp3``) per ``segs_dur``
    (key = segment index); every other path (the 06 narration) returns ``total``."""
    def probe(path, ffprobe=""):
        m = re.fullmatch(r"(\d{4})\.mp3", Path(path).name)
        if m:
            return segs_dur.get(int(m.group(1)) - 1, float("nan")), None
        return total, None
    return probe


def _write_timeline(stem: str, spans: list[dict], duration: float) -> dict:
    layout = core_paths.get_layout()
    for sp in spans:  # v2 时间轴的三个描述字段（缺省空串，旧调用方无需感知）
        sp.setdefault("scene_desc", "")
        sp.setdefault("mood_desc", "")
        sp.setdefault("switch_reason", "")
    tl = {"version": 2, "stem": stem, "generated_at": "t0", "model": "test-model",
          "fingerprint": "fp", "entry_count": 6,
          "duration": duration, "timeline": spans}
    bgm_engine._atomic_write_json(bgm_engine._timeline_path(layout, stem), tl)
    return tl


def _wait_until(pred, timeout: float = 8.0, step: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


class Ctl:
    """Per-test coordination points for the stub workers."""

    def __init__(self, stems):
        self.lock = threading.Lock()
        self.order: list[str] = []
        self.holders: set[str] = set()
        self.done: set[str] = set()
        self.go = threading.Event()
        self.releases: dict[str, threading.Event] = {s: threading.Event() for s in stems}

    def snapshot_holders(self) -> list[str]:
        with self.lock:
            return sorted(self.holders)


def _make_stub(ctl: Ctl, gate_fn):
    def stub(handle, stem, *args, **kw):
        with ctl.lock:
            ctl.order.append(stem)
        # Park BEFORE the gate (cancel-aware, like the real engine).
        while not handle.cancelled and not ctl.go.wait(0.05):
            pass
        if handle.cancelled:
            raise TaskCancelled()
        g = gate_fn()
        if not g.acquire(stop_check=lambda: handle.cancelled):
            raise TaskCancelled()
        with ctl.lock:
            ctl.holders.add(stem)
        try:
            while not handle.cancelled and not ctl.releases[stem].wait(0.05):
                pass
        finally:
            g.release()
            with ctl.lock:
                ctl.holders.discard(stem)
        if handle.cancelled:
            raise TaskCancelled()
        with ctl.lock:
            ctl.done.add(stem)
        return {"ok": True, "stem": stem}

    return stub


def _launch_analyze(monkeypatch, stems, c: int, go: bool = True):
    """run_analyze with a controllable stub on the REAL shared LLM gate (limit c).

    The Ctl covers ALL stems — non-conflicting chapters may be added to a running
    batch mid-test (the in-flight 409 tests launch extra stems after the fact).
    """
    core_config.update_config({"generation": {"max_concurrency": c}})
    ctl = Ctl(STEMS)
    if go:
        ctl.go.set()
    monkeypatch.setattr("backend.engines.bgm.analyze_chapter", _make_stub(ctl, concurrency.gate))
    mgr = get_task_manager()
    res = api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=list(stems)))
    return ctl, mgr, res


def _launch_mix(monkeypatch, stems, cpu: int, go: bool = True):
    """run_mix with a controllable stub on the REAL merge gate (limit = cpu // 2)."""
    ctl = Ctl(STEMS)
    if go:
        ctl.go.set()
    monkeypatch.setattr("backend.engines.bgm.mix_chapter", _make_stub(ctl, concurrency.merge_gate))
    monkeypatch.setattr(merge_engine.os, "cpu_count", lambda: cpu)
    mgr = get_task_manager()
    res = api_bgm.run_mix(api_bgm.MixRequest(chapters=list(stems)))
    return ctl, mgr, res


def _launch_segment(monkeypatch, stems, c: int, go: bool = True):
    """run_analyze_segment with a controllable stub on the REAL shared LLM gate
    (limit c). The stems' 03 scripts must exist first (the endpoint guard)."""
    core_config.update_config({"generation": {"max_concurrency": c}})
    ctl = Ctl(STEMS)
    if go:
        ctl.go.set()
    monkeypatch.setattr("backend.engines.bgm.analyze_segment_chapter",
                        _make_stub(ctl, concurrency.gate))
    mgr = get_task_manager()
    res = api_bgm.run_analyze_segment(
        api_bgm.SegmentAnalyzeRequest(chapters=list(stems)))
    return ctl, mgr, res


# -- PENDING shells in order + labels pinned -----------------------------------

def test_analyze_creates_pending_shells_in_order(workspace, monkeypatch):
    # C=2 (shared LLM gate), 6 chapters: the coordinator's first round dispatches
    # exactly BGM_PREFETCH_DEPTH; the rest stay PENDING shells.
    ctl, mgr, res = _launch_analyze(monkeypatch, STEMS[:6], 2, go=False)
    task_ids = res["task_ids"]
    assert [row["stem"] for row in res["chapters"]] == STEMS[:6]  # request order
    assert len(task_ids) == 6 and len(set(task_ids)) == 6
    tasks = [mgr.get(tid) for tid in task_ids]
    assert all(t is not None for t in tasks)
    assert [t.seq for t in tasks] == sorted(t.seq for t in tasks)  # seq = request order
    # The label tail is the load-bearing contract shared with the frontend's F5
    # reattach and the in-flight 409 — pin BOTH label shapes.
    assert all(t.module == "bgm-analysis" for t in tasks)
    assert [t.label for t in tasks] == [f"章节气氛分析：{s}" for s in STEMS[:6]]
    _wait_until(lambda: len(ctl.order) == 4)
    assert ctl.order == STEMS[:4]  # exactly the first 4 dispatched, in order
    assert all(mgr.get(tid).status is TaskStatus.RUNNING for tid in task_ids[:4])
    assert all(mgr.get(tid).status is TaskStatus.PENDING for tid in task_ids[4:])
    assert concurrency.gate().active == 0  # nothing acquired a slot yet

    ctl.go.set()
    for s in STEMS[:6]:
        ctl.releases[s].set()
    _wait_until(lambda: len(ctl.order) == 6 and all(
        mgr.get(tid).status in TERMINAL for tid in task_ids))
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    assert ctl.order == STEMS[:6]  # strict request order end to end


def test_mix_creates_pending_shells_in_order(workspace, monkeypatch):
    # cpu=4 → merge gate limit 2, 6 chapters: same prefetch shape on merge_gate.
    ctl, mgr, res = _launch_mix(monkeypatch, STEMS[:6], 4, go=False)
    task_ids = res["task_ids"]
    assert [row["stem"] for row in res["chapters"]] == STEMS[:6]
    assert len(task_ids) == 6 and len(set(task_ids)) == 6
    tasks = [mgr.get(tid) for tid in task_ids]
    assert all(t.module == "bgm-mix" for t in tasks)
    assert [t.label for t in tasks] == [f"背景音乐混音：{s}" for s in STEMS[:6]]
    _wait_until(lambda: len(ctl.order) == 4)
    assert ctl.order == STEMS[:4]
    assert all(mgr.get(tid).status is TaskStatus.PENDING for tid in task_ids[4:])
    assert concurrency.merge_gate().active == 0

    ctl.go.set()
    for s in STEMS[:6]:
        ctl.releases[s].set()
    _wait_until(lambda: len(ctl.order) == 6 and all(
        mgr.get(tid).status in TERMINAL for tid in task_ids))
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    assert ctl.order == STEMS[:6]


def test_segment_creates_pending_shells_in_order(workspace, monkeypatch):
    # C=2 (shared LLM gate), 6 chapters: same prefetch shape as the chapter
    # analysis over the same gate + coordinator; module/label contract pinned.
    _seed_scripts(workspace, STEMS[:6])
    ctl, mgr, res = _launch_segment(monkeypatch, STEMS[:6], 2, go=False)
    task_ids = res["task_ids"]
    assert [row["stem"] for row in res["chapters"]] == STEMS[:6]
    assert len(task_ids) == 6 and len(set(task_ids)) == 6
    tasks = [mgr.get(tid) for tid in task_ids]
    assert [t.seq for t in tasks] == sorted(t.seq for t in tasks)  # seq = request order
    assert all(t.module == "bgm-segment" for t in tasks)
    assert [t.label for t in tasks] == [f"段落分析：{s}" for s in STEMS[:6]]
    _wait_until(lambda: len(ctl.order) == 4)
    assert ctl.order == STEMS[:4]  # exactly the first 4 dispatched, in order
    assert all(mgr.get(tid).status is TaskStatus.RUNNING for tid in task_ids[:4])
    assert all(mgr.get(tid).status is TaskStatus.PENDING for tid in task_ids[4:])
    assert concurrency.gate().active == 0  # nothing acquired a slot yet

    ctl.go.set()
    for s in STEMS[:6]:
        ctl.releases[s].set()
    _wait_until(lambda: len(ctl.order) == 6 and all(
        mgr.get(tid).status in TERMINAL for tid in task_ids))
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    assert ctl.order == STEMS[:6]  # strict request order end to end


# -- prefetch cap + strict order ------------------------------------------------

def test_analyze_coordinator_cap_and_order(workspace, monkeypatch):
    # C=2, 6 chapters: steady state = 2 holders + 4 slot-waiters, never more;
    # strict request order as the shared LLM gate frees.
    ctl, mgr, res = _launch_analyze(monkeypatch, STEMS[:6], 2)
    task_ids = res["task_ids"]
    samples: list[tuple[int, int]] = []
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.02):
            running = sum(
                1 for tid in task_ids
                if mgr.get(tid).status in (TaskStatus.RUNNING, TaskStatus.PAUSED))
            samples.append((concurrency.gate().active, running))

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    try:
        _wait_until(lambda: len(ctl.order) == 6)
        assert ctl.order == STEMS[:6]
        released: set[str] = set()

        def new_wave_count() -> int:
            return sum(1 for h in ctl.snapshot_holders() if h not in released)

        for _ in range(3):
            _wait_until(lambda: new_wave_count() == 2)
            for h in ctl.snapshot_holders():
                if h not in released:
                    released.add(h)
                    ctl.releases[h].set()
        _wait_until(lambda: len(ctl.done) == 6)
    finally:
        stop.set()
        mon.join(timeout=2)
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    for active, running in samples:
        assert active <= 2, f"LLM gate over-cap: {active}"
        assert running <= 2 + api_bgm.BGM_PREFETCH_DEPTH, f"in-flight over-cap: {running}"


def test_mix_coordinator_cap_and_order(workspace, monkeypatch):
    # cpu=4 → C=2, 6 chapters: same invariants on the merge gate.
    ctl, mgr, res = _launch_mix(monkeypatch, STEMS[:6], 4)
    task_ids = res["task_ids"]
    samples: list[tuple[int, int]] = []
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.02):
            running = sum(
                1 for tid in task_ids
                if mgr.get(tid).status in (TaskStatus.RUNNING, TaskStatus.PAUSED))
            samples.append((concurrency.merge_gate().active, running))

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    try:
        _wait_until(lambda: len(ctl.order) == 6)
        assert ctl.order == STEMS[:6]
        released: set[str] = set()

        def new_wave_count() -> int:
            return sum(1 for h in ctl.snapshot_holders() if h not in released)

        for _ in range(3):
            _wait_until(lambda: new_wave_count() == 2)
            for h in ctl.snapshot_holders():
                if h not in released:
                    released.add(h)
                    ctl.releases[h].set()
        _wait_until(lambda: len(ctl.done) == 6)
    finally:
        stop.set()
        mon.join(timeout=2)
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    for active, running in samples:
        assert active <= 2, f"merge gate over-cap: {active}"
        assert running <= 2 + api_bgm.BGM_PREFETCH_DEPTH, f"in-flight over-cap: {running}"


# -- the endpoints size their gates ----------------------------------------------

def test_analyze_endpoint_sizes_shared_llm_gate(workspace, monkeypatch):
    # run_analyze calls set_concurrency(generation.max_concurrency) on every request.
    ctl, mgr, res = _launch_analyze(monkeypatch, STEMS[:2], 3, go=False)
    assert concurrency.gate().limit == 3


def test_mix_endpoint_sizes_merge_gate_from_cpu(workspace, monkeypatch):
    # run_mix calls set_merge_concurrency(Merge.concurrency_limit()) = cpu // 2 (cap 4).
    ctl, mgr, res = _launch_mix(monkeypatch, STEMS[:2], 6, go=False)
    assert concurrency.merge_gate().limit == 3


def test_segment_endpoint_sizes_shared_llm_gate(workspace, monkeypatch):
    # run_analyze_segment calls set_concurrency(generation.max_concurrency) per request.
    _seed_scripts(workspace, STEMS[:2])
    ctl, mgr, res = _launch_segment(monkeypatch, STEMS[:2], 3, go=False)
    assert concurrency.gate().limit == 3


# -- endpoint guards ---------------------------------------------------------------

def test_analyze_endpoint_guards(workspace, monkeypatch):
    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=[]))
    assert e.value.status_code == 400
    assert "请选择要分析的章节" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["../evil"]))
    assert e.value.status_code == 400
    assert "非法章节名" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ghost"]))
    assert e.value.status_code == 400
    assert "未找到章节文件" in e.value.detail

    core_config.update_config({"llm": {"model_name": ""}})
    try:
        with pytest.raises(HTTPException) as e:
            api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ch1"]))
        assert e.value.status_code == 400
        assert "尚未配置 LLM 模型" in e.value.detail
    finally:
        core_config.update_config({"llm": {"model_name": "test-model"}})


def test_mix_endpoint_guards(workspace, monkeypatch):
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=[]))
    assert e.value.status_code == 400

    # never matched (no assignment entry) → 400 before any task is created
    data = bgm_engine.load_assignments(core_paths.get_layout())
    data["chapters"].pop("ch2")
    bgm_engine.save_assignments(core_paths.get_layout(), data)
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch2"]))
    assert e.value.status_code == 400
    assert "从未匹配" in e.value.detail

    # narration missing → 400
    (workspace / "06_audio_merge" / "ch3.mp3").unlink()
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch3"]))
    assert e.value.status_code == 400
    assert "未找到旁白音频" in e.value.detail

    # music deleted from the library → 400 (the ⚠ 已删除 row may NOT mix)
    (core_paths.MUSIC_LIBRARY_DIR / "t1.mp3").unlink()
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch4"]))
    assert e.value.status_code == 400
    assert "音乐库中找不到 t1.mp3" in e.value.detail
    (core_paths.MUSIC_LIBRARY_DIR / "t1.mp3").write_bytes(b"music-bytes")

    # a matched-but-no-BGM chapter (music=None) is ALLOWED — the copy2 path
    layout = core_paths.get_layout()
    data = bgm_engine.load_assignments(layout)
    data["chapters"]["ch5"] = {"tags": {}, "music": None, "locked": False,
                               "manual": False, "score": 0, "reason": "r",
                               "matched_at": "t0"}
    bgm_engine.save_assignments(layout, data)
    ctl, mgr, res = _launch_mix(monkeypatch, ["ch5"], 4, go=False)
    assert res["chapters"][0]["stem"] == "ch5"  # the copy2 path is allowed
    ctl.go.set()
    ctl.releases["ch5"].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL for tid in res["task_ids"]))


def test_mix_segment_guards(workspace, monkeypatch):
    """segment=true chapter pre-flight guards (per stem, before ANY task):
    missing timeline / stale timeline (narration drift) / span track deleted →
    400; fresh timeline + track present → allowed."""
    layout = core_paths.get_layout()
    data = bgm_engine.load_assignments(layout)
    data["chapters"]["ch1"] = {"tags": {}, "music": None, "segment": True,
                               "locked": False, "manual": False, "score": None,
                               "reason": "r", "matched_at": "t0"}
    bgm_engine.save_assignments(layout, data)
    # the 06 narration is fake bytes → stub the probe the guard runs
    monkeypatch.setattr(api_bgm, "probe_duration",
                        lambda path, ffprobe="": (42.0, None))

    def _span(music: str) -> list[dict]:
        return [{"start": 0.0, "end": 40.0, "music_id": music, "intensity": 2,
                 "volume": 0.18, "tags": {}, "score": 3, "reason": "r"}]

    # ① no timeline file
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1"]))
    assert e.value.status_code == 400
    assert "该章没有时间轴" in e.value.detail

    # ② stale timeline (probe 42.0 vs stored 100.0 > _STALE_TOLERANCE_S)
    _write_timeline("ch1", _span("t1.mp3"), duration=100.0)
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1"]))
    assert e.value.status_code == 400
    assert "时间轴已过期" in e.value.detail

    # ③ span's track deleted from the library (timeline fresh again)
    _write_timeline("ch1", _span("ghost.mp3"), duration=42.0)
    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1"]))
    assert e.value.status_code == 400
    assert "音乐库中找不到 ghost.mp3" in e.value.detail

    # ④ fresh timeline + track present → guard passes, the task is created
    _write_timeline("ch1", _span("t1.mp3"), duration=42.0)
    ctl, mgr, res = _launch_mix(monkeypatch, ["ch1"], 4, go=False)
    assert res["chapters"][0]["stem"] == "ch1"
    ctl.go.set()
    ctl.releases["ch1"].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL for tid in res["task_ids"]))


def test_mix_accepts_fresh_timeline_after_segment_marker_reset(workspace, monkeypatch):
    """A chapter-level match may reset only the assignment marker; a current
    segment analysis + timeline still represents a valid paragraph match and
    must remain mixable."""
    entries = _seed_scripts(workspace, ["ch1"])["ch1"]
    _seed_segment_analysis("ch1", entries, [_segment_block(0, 0, mood=["紧张"])])
    _write_timeline("ch1", [{"start": 0.0, "end": 40.0, "music_id": "t1.mp3",
                             "intensity": 2, "volume": 0.18, "tags": {},
                             "score": 3, "reason": "r"}], duration=42.0)
    data = bgm_engine.load_assignments(core_paths.get_layout())
    data["chapters"]["ch1"]["music"] = None
    data["chapters"]["ch1"]["segment"] = False
    bgm_engine.save_assignments(core_paths.get_layout(), data)
    assert bgm_engine.load_segment_timeline(
        core_paths.get_layout(), "ch1", data["chapters"]["ch1"]
    ) is not None
    monkeypatch.setattr(api_bgm, "probe_duration",
                        lambda path, ffprobe="": (42.0, None))

    ctl, mgr, res = _launch_mix(monkeypatch, ["ch1"], 4, go=False)
    assert res["chapters"][0]["stem"] == "ch1"
    ctl.go.set()
    ctl.releases["ch1"].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL for tid in res["task_ids"]))


def test_mix_endpoint_dedupes(workspace, monkeypatch):
    ctl, mgr, res = _launch_mix(monkeypatch, ["ch1", "ch1", "ch2"], 4, go=False)
    assert [row["stem"] for row in res["chapters"]] == ["ch1", "ch2"]
    assert len(res["task_ids"]) == 2 and len(set(res["task_ids"])) == 2
    ctl.go.set()
    for s in ("ch1", "ch2"):
        ctl.releases[s].set()
    _wait_until(lambda: len(ctl.done) == 2)


def test_segment_endpoint_guards(workspace, monkeypatch):
    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=[]))
    assert e.value.status_code == 400
    assert "请选择要段落分析的章节" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=["../evil"]))
    assert e.value.status_code == 400
    assert "非法章节名" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=["ghost"]))
    assert e.value.status_code == 400
    assert "未找到章节文件" in e.value.detail

    # 02 exists (fixture) but no 03 script yet → the segment-specific 400
    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=["ch1"]))
    assert e.value.status_code == 400
    assert "未找到脚本 JSON" in e.value.detail

    # the 03 script is the model check's precondition (guards run in order)
    _seed_scripts(workspace, ["ch2"])
    core_config.update_config({"llm": {"model_name": ""}})
    try:
        with pytest.raises(HTTPException) as e:
            api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=["ch2"]))
        assert e.value.status_code == 400
        assert "尚未配置 LLM 模型" in e.value.detail
    finally:
        core_config.update_config({"llm": {"model_name": "test-model"}})


# -- same-chapter in-flight 409 ----------------------------------------------------

def test_analyze_inflight_same_chapter_409(workspace, monkeypatch):
    # ch1 / ch2 in flight (holding their slots): resubmitting ch1 is 409; adding a
    # non-conflicting chapter is allowed; a mixed request short-circuits.
    ctl, mgr, res = _launch_analyze(monkeypatch, STEMS[:2], 2, go=False)
    ctl.go.set()
    _wait_until(lambda: len(ctl.holders) == 2)

    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ch1"]))
    assert e.value.status_code == 409
    assert "ch1" in e.value.detail

    res3 = api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ch3"]))
    assert res3["chapters"][0]["stem"] == "ch3"

    with pytest.raises(HTTPException) as e2:
        api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ch1", "ch3"]))
    assert e2.value.status_code == 409

    ctl.releases["ch1"].set()
    ctl.releases["ch2"].set()
    ctl.releases["ch3"].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL
                            for tid in [*res["task_ids"], *res3["task_ids"]]),
                timeout=5)


def test_mix_inflight_same_chapter_409(workspace, monkeypatch):
    ctl, mgr, res = _launch_mix(monkeypatch, STEMS[:2], 4, go=False)
    ctl.go.set()
    _wait_until(lambda: len(ctl.holders) == 2)

    with pytest.raises(HTTPException) as e:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1"]))
    assert e.value.status_code == 409
    assert "ch1" in e.value.detail

    res3 = api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch3"]))
    assert res3["chapters"][0]["stem"] == "ch3"

    for s in ("ch1", "ch2", "ch3"):
        ctl.releases[s].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL
                            for tid in [*res["task_ids"], *res3["task_ids"]]),
                timeout=5)


def test_segment_inflight_same_chapter_409(workspace, monkeypatch):
    _seed_scripts(workspace, STEMS[:3])
    ctl, mgr, res = _launch_segment(monkeypatch, STEMS[:2], 2, go=False)
    ctl.go.set()
    _wait_until(lambda: len(ctl.holders) == 2)

    with pytest.raises(HTTPException) as e:
        api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=["ch1"]))
    assert e.value.status_code == 409
    assert "ch1" in e.value.detail

    res3 = api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=["ch3"]))
    assert res3["chapters"][0]["stem"] == "ch3"

    with pytest.raises(HTTPException) as e2:
        api_bgm.run_analyze_segment(
            api_bgm.SegmentAnalyzeRequest(chapters=["ch1", "ch3"]))
    assert e2.value.status_code == 409

    ctl.releases["ch1"].set()
    ctl.releases["ch2"].set()
    ctl.releases["ch3"].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL
                            for tid in [*res["task_ids"], *res3["task_ids"]]),
                timeout=5)


# -- POST /match (synchronous) ------------------------------------------------------

def test_match_unknown_mode_400(workspace):
    # mode="segment" is no longer a placeholder 400 — it is the synchronous
    # timeline recompute (covered by the tests below).
    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(mode="weird"))
    assert e.value.status_code == 400
    assert "未知匹配模式" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=[]))
    assert e.value.status_code == 400


def test_match_writes_assignments_locked_skipped(workspace):
    # ch1 locked with t1.mp3 → preserved verbatim (matched_at untouched, counted);
    # ch2 re-matched (its tags hit 紧张 → t1.mp3, but prev = ch1's locked t1 → the
    # only candidate is blocked → relaxed re-pick with the note).
    layout = core_paths.get_layout()
    analysis = bgm_engine.load_analysis(layout)
    for s in ("ch1", "ch2"):
        analysis["chapters"][s] = {"scene": [], "mood": ["紧张"], "emotion": [],
                                   "custom": [], "analyzed_at": "t0", "edited": False}
    bgm_engine.save_analysis(layout, analysis)
    data = bgm_engine.load_assignments(layout)
    data["chapters"]["ch1"]["locked"] = True
    data["chapters"]["ch1"]["matched_at"] = "old-t"
    bgm_engine.save_assignments(layout, data)

    res = api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1", "ch2"]))
    assert res["skipped_locked"] == 1 and res["matched"] == 1 and res["no_bgm"] == 0
    kept = res["assignments"]["chapters"]["ch1"]
    assert kept["music"] == "t1.mp3" and kept["matched_at"] == "old-t" and kept["locked"]
    rematched = res["assignments"]["chapters"]["ch2"]
    assert rematched["music"] == "t1.mp3" and "放宽" in rematched["reason"]
    assert rematched["locked"] is False and rematched["manual"] is False
    # mode persisted + the neighbours outside the selection are untouched
    on_disk = bgm_engine.load_assignments(layout)
    assert on_disk["mode"] == "llm"
    assert on_disk["chapters"]["ch3"]["matched_at"] == "t0"  # not in the selection


def test_match_analysis_inflight_409_mix_inflight_ok(workspace, monkeypatch):
    # an analysis task on ch1 blocks /match (would read a half-written analysis);
    # a mix task on ch2 does NOT block it.
    ctl_a, mgr, res_a = _launch_analyze(monkeypatch, ["ch1"], 2, go=False)
    ctl_m, mgr2, res_m = _launch_mix(monkeypatch, ["ch2"], 4, go=False)
    ctl_a.go.set()
    ctl_m.go.set()
    _wait_until(lambda: len(ctl_a.holders) == 1 and len(ctl_m.holders) == 1)

    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"]))
    assert e.value.status_code == 409
    assert "分析任务在途" in e.value.detail

    res = api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch2"]))
    assert res["matched"] + res["no_bgm"] == 1

    ctl_a.releases["ch1"].set()
    ctl_m.releases["ch2"].set()
    _wait_until(lambda: all(
        mgr.get(tid).status in TERMINAL for tid in [*res_a["task_ids"], *res_m["task_ids"]]),
        timeout=5)


def test_match_segment_recompute_writes_timelines(workspace, monkeypatch):
    # Synchronous recompute: cached analysis + stubbed 05 durations + the pause
    # rule → timeline files + segment-marked assignments, ZERO LLM calls.
    e1 = _seed_scripts(workspace, ["ch1"])["ch1"]
    e2 = _seed_scripts(workspace, ["ch2"])["ch2"]
    _seed_segment_chapter(workspace, "ch1", e1)
    _seed_segment_chapter(workspace, "ch2", e2)
    # ch1: one scene block covering entries 0..4 (entry 5 = intentional
    # silence) → t1.mp3 (mood 紧张, +3). Same speaker throughout → 250ms
    # gaps; span end = the block's last entry's audio END = 4·2.25 + 2.0 = 11.0.
    _seed_segment_analysis("ch1", e1, [
        _segment_block(0, 4, intensity=2, mood=["紧张"]),
    ])
    # ch2: empty tags → score 0 < min_score and t1 is tagged (not generic)
    # → no candidate → the no-BGM chapter.
    _seed_segment_analysis("ch2", e2, [
        _segment_block(0, 4),
    ])
    monkeypatch.setattr(bgm_engine, "probe_duration",
                        _fake_probe({i: 2.0 for i in range(6)}, 14.25))
    calls: list = []
    monkeypatch.setattr(bgm_engine, "_llm_chat_completion",
                        lambda *a, **k: calls.append(1))

    layout = core_paths.get_layout()
    before = bgm_engine.load_assignments(layout)
    res = api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1", "ch2"],
                                                 mode="segment"))
    assert calls == []  # pure recompute — the LLM path is never touched
    assert res == {"mode": "segment", "matched": 1, "no_bgm": 1,
                   "skipped_locked": 0}

    tl1 = bgm_engine.load_timeline(layout, "ch1")
    assert tl1["version"] == 2 and tl1["duration"] == 14.25
    assert tl1["entry_count"] == 6
    assert tl1["timeline"] == [
        {"start": 0.0, "end": 11.0, "music_id": "t1.mp3", "intensity": 2,
         "volume": 0.18,
         "tags": {"scene": [], "mood": ["紧张"], "emotion": [], "custom": []},
         "score": 3, "reason": "mood 命中 紧张(+3)",
         "scene_desc": "", "mood_desc": "", "switch_reason": ""}]
    tl2 = bgm_engine.load_timeline(layout, "ch2")
    assert tl2["timeline"] == []  # the no-BGM chapter still gets a (empty) timeline

    on_disk = bgm_engine.load_assignments(layout)
    assert on_disk["mode"] == "segment"
    e1d = on_disk["chapters"]["ch1"]
    assert e1d["segment"] is True and e1d["music"] is None
    assert e1d["reason"] == "段落级时间轴（1 段 BGM）"
    assert e1d["tags"]["mood"] == ["紧张"] and e1d["locked"] is False
    assert on_disk["chapters"]["ch2"]["reason"] == "段落级（全章无 BGM）"
    # the unselected chapters' entries are untouched (only data["mode"] moved)
    assert on_disk["chapters"]["ch3"]["music"] == "t1.mp3"
    assert on_disk["chapters"]["ch3"]["matched_at"] == "t0"
    assert "segment" not in on_disk["chapters"]["ch3"]
    # the pre-existing (chapter-mode) entries were replaced, not merged
    assert before["chapters"]["ch1"]["music"] == "t1.mp3"


def test_match_segment_error_wordings_zero_drop(workspace, monkeypatch):
    """Any engine error → 400 with the engine's actionable wording, and NOTHING
    was written (timeline absent + assignments byte-identical)."""
    layout = core_paths.get_layout()
    before = (layout.bgm / bgm_engine.ASSIGNMENTS_NAME).read_bytes()

    # ① no 03 at all → the per-stem validation dies on the missing script
    # (the default-all path skips per-stem validation and surfaces the
    # engine wording instead — cases ②..④ cover that route).
    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"], mode="segment"))
    assert e.value.status_code == 400
    assert "未找到脚本 JSON（03_parsed_json/ch1.json）" in e.value.detail

    e1 = _seed_scripts(workspace, ["ch1"])["ch1"]
    # 03 + 06 only — the 05 synthesis stays absent for case ④
    _seed_segment_chapter(workspace, "ch1", e1, with_05=False)

    # ② the LLM analysis was never run
    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"], mode="segment"))
    assert e.value.status_code == 400
    assert "段落分析缺失或已失效" in e.value.detail

    # ③ 03 changed after the analysis (fingerprint mismatch)
    _seed_segment_analysis("ch1", e1, [_segment_block(0, 0)],
                           fingerprint="deadbeef")
    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"], mode="segment"))
    assert e.value.status_code == 400
    assert "段落分析已失效（03 脚本变化）" in e.value.detail

    # ④ the 05 synthesis is missing
    _seed_segment_analysis("ch1", e1, [_segment_block(0, 0)])
    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"], mode="segment"))
    assert e.value.status_code == 400
    assert "未找到合成结果清单" in e.value.detail

    # a failed recompute wrote NOTHING: no timeline, assignments byte-identical
    assert not (layout.bgm / bgm_engine.TIMELINE_DIR / "ch1.json").exists()
    assert (layout.bgm / bgm_engine.ASSIGNMENTS_NAME).read_bytes() == before


def test_match_segment_inflight_guard_mix_ok(workspace, monkeypatch):
    # a bgm-segment task on ch1 blocks /match segment (half-written analysis);
    # a bgm-mix task on ch2 does NOT block it (mix reads assignments later,
    # the recompute writes them — the guard is deliberately one-directional).
    e1 = _seed_scripts(workspace, ["ch1"])["ch1"]
    e2 = _seed_scripts(workspace, ["ch2"])["ch2"]
    _seed_segment_chapter(workspace, "ch2", e2)
    _seed_segment_analysis("ch2", e2, [
        _segment_block(0, 4, intensity=2, mood=["紧张"]),
    ])
    ctl_s, mgr, res_s = _launch_segment(monkeypatch, ["ch1"], 2, go=False)
    ctl_m, mgr2, res_m = _launch_mix(monkeypatch, ["ch2"], 4, go=False)
    ctl_s.go.set()
    ctl_m.go.set()
    _wait_until(lambda: len(ctl_s.holders) == 1 and len(ctl_m.holders) == 1)

    monkeypatch.setattr(bgm_engine, "probe_duration",
                        _fake_probe({i: 2.0 for i in range(6)}, 14.25))
    with pytest.raises(HTTPException) as e:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"], mode="segment"))
    assert e.value.status_code == 409
    assert "段落分析任务在途" in e.value.detail

    res = api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch2"], mode="segment"))
    assert res["matched"] == 1 and res["no_bgm"] == 0

    ctl_s.releases["ch1"].set()
    ctl_m.releases["ch2"].set()
    _wait_until(lambda: all(
        mgr.get(tid).status in TERMINAL for tid in [*res_s["task_ids"],
                                                    *res_m["task_ids"]]),
        timeout=5)


# -- 【取消全部】= per-task cancel converges the whole batch ------------------------

def _cancel_all_converges(ctl, mgr, task_ids, gate):
    pending = [tid for tid in task_ids if mgr.get(tid).status is TaskStatus.PENDING]
    for tid in task_ids:
        mgr.control(tid, "cancel")
    _wait_until(lambda: all(mgr.get(tid).status is TaskStatus.CANCELLED for tid in task_ids),
                timeout=5)
    _wait_until(lambda: gate().active == 0, timeout=5)
    for tid in pending:  # the not-yet-dispatched shells finalized immediately
        t = mgr.get(tid)
        assert t.status is TaskStatus.CANCELLED and t.finished > 0


def test_analyze_cancel_all_converges(workspace, monkeypatch):
    # C=2, 8 chapters: 6 dispatched (2 holders + 4 waiters), 2 PENDING shells left.
    ctl, mgr, res = _launch_analyze(monkeypatch, STEMS, 2)
    _wait_until(lambda: len(ctl.order) == 6 and concurrency.gate().active == 2)
    _cancel_all_converges(ctl, mgr, res["task_ids"], concurrency.gate)


def test_mix_cancel_all_converges(workspace, monkeypatch):
    ctl, mgr, res = _launch_mix(monkeypatch, STEMS, 4)
    _wait_until(lambda: len(ctl.order) == 6 and concurrency.merge_gate().active == 2)
    _cancel_all_converges(ctl, mgr, res["task_ids"], concurrency.merge_gate)


def test_segment_cancel_all_converges(workspace, monkeypatch):
    _seed_scripts(workspace, STEMS)
    ctl, mgr, res = _launch_segment(monkeypatch, STEMS, 2)
    _wait_until(lambda: len(ctl.order) == 6 and concurrency.gate().active == 2)
    _cancel_all_converges(ctl, mgr, res["task_ids"], concurrency.gate)


# -- GET /chapters -------------------------------------------------------------------

def test_chapters_rows(workspace):
    ws = workspace
    layout = core_paths.get_layout()
    # ch1: full pipeline state (06 + 08 + analysis + assignment with live music)
    (ws / "08_bgm").mkdir(parents=True, exist_ok=True)
    (ws / "08_bgm" / "ch1.mp3").write_bytes(b"MIXED")
    analysis = bgm_engine.load_analysis(layout)
    analysis["chapters"]["ch1"] = {"scene": ["战斗"], "mood": ["紧张"], "emotion": [],
                                   "custom": [], "analyzed_at": "t0", "edited": True}
    bgm_engine.save_analysis(layout, analysis)
    # ch2: narration is a .wav (fallback) + assignment points at a deleted music
    (ws / "06_audio_merge" / "ch2.mp3").unlink()
    (ws / "06_audio_merge" / "ch2.wav").write_bytes(b"WAVNARR")
    data = bgm_engine.load_assignments(layout)
    data["chapters"]["ch2"]["music"] = "ghost.mp3"
    data["chapters"]["ghost"] = data["chapters"]["ch1"].copy()  # orphan (no 02 file)
    bgm_engine.save_assignments(layout, data)

    res = api_bgm.list_chapters()
    rows = {r["stem"]: r for r in res["chapters"]}
    assert list(rows) == STEMS  # the 02 files are the row basis (orphan hidden)
    assert "ghost" not in rows

    r1 = rows["ch1"]
    assert r1["narration_exists"] is True and r1["mix_exists"] is True
    assert r1["music_missing"] is False
    assert r1["analysis"]["mood"] == ["紧张"] and r1["analysis"]["edited"] is True
    assert r1["assignment"]["music"] == "t1.mp3" and r1["assignment"]["score"] == 3

    r2 = rows["ch2"]
    assert r2["narration_exists"] is True  # the .wav fallback counts
    assert r2["mix_exists"] is False
    assert r2["music_missing"] is True     # ⚠ 已删除
    assert r2["analysis"] is None

    r3 = rows["ch3"]
    assert r3["narration_exists"] is True  # fixture seed
    assert r3["mix_exists"] is False and r3["analysis"] is None
    assert r3["assignment"]["music"] == "t1.mp3" and r3["music_missing"] is False
    assert res["mode"] == "llm"


def test_chapters_segment_row_fields(workspace):
    # segment_analysis: null / fresh (stale=False) / stale (03 deleted after the
    # analysis — the fingerprint must mismatch); a fresh cached timeline remains
    # viewable after a chapter-mode assignment reset, while stale leftovers stay
    # hidden; segment_music_missing = a span's track left the library.
    layout = core_paths.get_layout()
    e1 = _seed_scripts(workspace, ["ch1"])["ch1"]
    e3 = _seed_scripts(workspace, ["ch3"])["ch3"]
    _seed_segment_analysis("ch1", e1, [_segment_block(0, 0, mood=["紧张"])])
    _write_timeline("ch1", [{"start": 0.0, "end": 10.0, "music_id": "t1.mp3",
                             "intensity": 2, "volume": 0.18, "tags": {},
                             "score": 3, "reason": "r"}], duration=14.25)
    # ch3: the 03 script is deleted AFTER the analysis → stale; a leftover
    # timeline file must NOT surface (the entry is not segment-marked)
    _seed_segment_analysis("ch3", e3, [_segment_block(0, 0)])
    (workspace / "03_parsed_json" / "ch3.json").unlink()
    _write_timeline("ch3", [{"start": 0.0, "end": 5.0, "music_id": "t1.mp3",
                             "intensity": 2, "volume": 0.18, "tags": {},
                             "score": 3, "reason": "r"}], duration=9.0)
    # ch5: segment entry + a timeline spanning a track that is not in the library
    data = bgm_engine.load_assignments(layout)
    for s in ("ch1", "ch5"):
        data["chapters"][s] = {"tags": {}, "music": None, "segment": s == "ch5",
                               "locked": False, "manual": False, "score": None,
                               "reason": "r", "matched_at": "t0"}
    bgm_engine.save_assignments(layout, data)
    _write_timeline("ch5", [{"start": 0.0, "end": 8.0, "music_id": "ghost.mp3",
                             "intensity": 2, "volume": 0.18, "tags": {},
                             "score": 0, "reason": "r"}], duration=9.0)

    rows = {r["stem"]: r for r in api_bgm.list_chapters()["chapters"]}
    r1 = rows["ch1"]
    assert r1["segment_analysis"] == {
        "analyzed_at": "t0", "entry_count": 6, "stale": False,
        "tags": {"scene": [], "mood": ["紧张"], "emotion": [], "custom": []},
    }
    assert r1["timeline"] == {"sections": 1, "duration": 14.25,
                              "generated_at": "t0"}
    assert r1["segment_music_missing"] is False
    assert r1["assignment"]["segment"] is False

    r3 = rows["ch3"]
    assert r3["segment_analysis"]["stale"] is True  # 03 已删 → 指纹必不匹配
    assert r3["timeline"] is None  # 遗留时间轴惰性（entry 非 segment）
    assert r3["segment_music_missing"] is False

    r5 = rows["ch5"]
    assert r5["segment_analysis"] is None
    assert r5["timeline"] is not None
    assert r5["segment_music_missing"] is True  # ghost.mp3 已出库

    r2 = rows["ch2"]
    assert r2["segment_analysis"] is None and r2["timeline"] is None
    assert r2["segment_music_missing"] is False


def test_chapters_no_workspace_empty(monkeypatch, tmp_path):
    # Read-only endpoint: no workspace -> empty rows (degrade, never 409).
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        assert api_bgm.list_chapters() == {"chapters": [], "mode": "llm"}
    finally:
        core_config.reset_config_cache()


# -- GET /timeline/{stem} ----------------------------------------------------------

def test_timeline_endpoint(workspace):
    _write_timeline("ch1", [{"start": 0.0, "end": 10.0, "music_id": "t1.mp3",
                             "intensity": 2, "volume": 0.18, "tags": {},
                             "score": 3, "reason": "r"}], duration=14.25)
    res = api_bgm.get_timeline("ch1")
    assert res["timeline"]["version"] == 2
    assert res["timeline"]["stem"] == "ch1"
    assert res["timeline"]["duration"] == 14.25
    assert len(res["timeline"]["timeline"]) == 1
    sp = res["timeline"]["timeline"][0]
    # v2 三个描述字段原样往返（_write_timeline 缺省空串）
    assert sp["scene_desc"] == "" and sp["mood_desc"] == ""
    assert sp["switch_reason"] == ""

    with pytest.raises(HTTPException) as e:
        api_bgm.get_timeline("ch2")
    assert e.value.status_code == 404
    assert "该章没有时间轴" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.get_timeline("../evil")
    assert e.value.status_code == 400
    assert "非法章节名" in e.value.detail


def test_timeline_no_workspace(monkeypatch, tmp_path):
    # Read-only endpoint: no workspace -> {"timeline": None} (degrade, never 409).
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        assert api_bgm.get_timeline("ch1") == {"timeline": None}
    finally:
        core_config.reset_config_cache()


# -- PUT /chapters/{stem} -------------------------------------------------------------

def test_update_chapter_tags(workspace):
    ws = workspace
    layout = core_paths.get_layout()
    analysis = bgm_engine.load_analysis(layout)
    analysis["chapters"]["ch1"] = {"scene": [], "mood": ["旧标签"], "emotion": [],
                                   "custom": [], "analyzed_at": "t0", "edited": False}
    bgm_engine.save_analysis(layout, analysis)

    res = api_bgm.update_chapter(
        "ch1", api_bgm.ChapterUpdateRequest(tags={"mood": ["紧张"], "custom": ["我的"]}))
    assert res["locked"] is False and res["manual"] is False
    a = bgm_engine.load_analysis(layout)["chapters"]["ch1"]
    assert a["edited"] is True and a["edited_at"]
    assert a["analyzed_at"] == "t0"  # the original analysis time is kept
    assert a["mood"] == ["紧张"] and a["custom"] == ["我的"]  # 紧张 = vocab mood; 我的 → custom
    e = bgm_engine.load_assignments(layout)["chapters"]["ch1"]
    assert e["tags"]["mood"] == ["紧张"]  # the snapshot followed the edit
    assert e["music"] == "t1.mp3"        # music untouched (key absent)
    assert e["reason"] == "r"


def test_update_chapter_music_and_lock(workspace):
    # manual pick
    e = api_bgm.update_chapter(
        "ch1", api_bgm.ChapterUpdateRequest(music="t1.mp3", locked=True))
    assert e["music"] == "t1.mp3" and e["manual"] is True and e["locked"] is True
    assert e["score"] is None and e["reason"] == "手动指定" and e["matched_at"]
    # clear (music present, value null)
    e2 = api_bgm.update_chapter("ch1", api_bgm.ChapterUpdateRequest(music=None))
    assert e2["music"] is None and e2["manual"] is True and e2["reason"] == "手动指定"
    assert e2["locked"] is True  # the lock survived (key absent)
    # an unmatched chapter gets an entry created on demand
    layout = core_paths.get_layout()
    data = bgm_engine.load_assignments(layout)
    data["chapters"].pop("ch3")
    bgm_engine.save_assignments(layout, data)
    e3 = api_bgm.update_chapter("ch3", api_bgm.ChapterUpdateRequest(music="t1.mp3"))
    assert e3["music"] == "t1.mp3" and e3["manual"] is True and e3["locked"] is False


def test_update_chapter_guard_400s(workspace):
    with pytest.raises(HTTPException) as e:
        api_bgm.update_chapter("../evil", api_bgm.ChapterUpdateRequest(locked=True))
    assert e.value.status_code == 400

    with pytest.raises(HTTPException) as e:
        api_bgm.update_chapter("ch1",
                               api_bgm.ChapterUpdateRequest(music="a/b.mp3"))
    assert e.value.status_code == 400
    assert "非法音乐文件名" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.update_chapter("ch1",
                               api_bgm.ChapterUpdateRequest(music="ghost.mp3"))
    assert e.value.status_code == 400
    assert "音乐库中找不到 ghost.mp3" in e.value.detail
    # a failed update must not have touched anything
    e0 = bgm_engine.load_assignments(core_paths.get_layout())["chapters"]["ch1"]
    assert e0["music"] == "t1.mp3" and e0["reason"] == "r"


def test_write_endpoints_no_workspace_409(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        for call in (
            lambda: api_bgm.run_analyze(api_bgm.AnalyzeRequest(chapters=["ch1"])),
            lambda: api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=["ch1"])),
            lambda: api_bgm.run_match(api_bgm.MatchRequest()),
            lambda: api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1"])),
            lambda: api_bgm.update_chapter("ch1", api_bgm.ChapterUpdateRequest(locked=True)),
        ):
            with pytest.raises(HTTPException) as e:
                call()
            assert e.value.status_code == 409
    finally:
        core_config.reset_config_cache()
