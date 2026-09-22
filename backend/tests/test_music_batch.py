"""Tests for the batch AI tag recognition dispatch (``api/music.py``):
ordered + prefetched dispatch of ``music-ai-tags`` PENDING shells over the REAL
process-wide shared LLM gate, the label contract (``AI 推荐标签：{name}`` —
backend regex ↔ frontend derivation ↔ pinned here), the per-track in-flight
409, the endpoint guards, and cancel convergence.

``suggest_tags_batch`` is called directly (the suite never goes through the
HTTP layer). The engine worker is monkeypatched with a controllable stub that
uses the REAL gate — the coordinator's decisions are driven by
``gate().active`` exactly as in production (test_bgm_batch precedent).
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi import HTTPException

from backend.api import music as api_music
from backend.api.bgm import BGM_PREFETCH_DEPTH
from backend.core import concurrency
from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core.tasks import TERMINAL, TaskCancelled, TaskStatus, get_task_manager
from backend.engines import music as music_engine

NAMES = [f"t{i}.mp3" for i in range(1, 9)]  # up to 8 tracks


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """Throwaway project root + workspace + music library (8 fake tracks) with
    a drained shared LLM gate after."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    ws.mkdir(parents=True)
    core_config.set_workspace_pointer(str(ws))
    core_config.update_config({"llm": {"model_name": "test-model"}})
    lib = tmp_path / "music_library"
    lib.mkdir(parents=True, exist_ok=True)
    for n in NAMES:
        (lib / n).write_bytes(b"music-bytes")
        music_engine.update_index(
            lambda idx, n=n: idx["tracks"].__setitem__(n, {
                "duration": 60.0, "enabled": True, "description": "",
                "tags": {"scene": [], "mood": [], "emotion": [], "custom": []},
                "added_at": ""}))
    limit = concurrency.gate().limit
    yield
    # A failed test must not leak gate slots / zombie stub threads into later tests:
    # cancel every still-active AI-tag task (stubs honour cooperative cancel and
    # release their slots), let the gate drain, restore its limit.
    mgr = get_task_manager()
    for t in list(mgr.list()):
        if t.module == "music-ai-tags" and t.status not in TERMINAL:
            try:
                mgr.control(t.id, "cancel")
            except KeyError:
                pass
    deadline = time.time() + 5
    stuck = []
    while time.time() < deadline:
        stuck = [t for t in mgr.list()
                 if t.module == "music-ai-tags" and t.status not in TERMINAL]
        if not stuck and concurrency.gate().active == 0:
            break
        time.sleep(0.05)
    assert not stuck, f"music-ai-tags tasks leaked: {[t.label for t in stuck]}"
    assert concurrency.gate().active == 0
    concurrency.set_concurrency(limit)
    core_config.reset_config_cache()


def _wait_until(pred, timeout: float = 8.0, step: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


class Ctl:
    """Per-test coordination points for the stub workers."""

    def __init__(self, names):
        self.lock = threading.Lock()
        self.order: list[str] = []
        self.holders: set[str] = set()
        self.done: set[str] = set()
        self.go = threading.Event()
        self.releases: dict[str, threading.Event] = {n: threading.Event() for n in names}

    def snapshot_holders(self) -> list[str]:
        with self.lock:
            return sorted(self.holders)


def _make_stub(ctl: Ctl):
    def stub(handle, name, llm_cfg):
        with ctl.lock:
            ctl.order.append(name)
        # Park BEFORE the gate (cancel-aware, like the real engine).
        while not handle.cancelled and not ctl.go.wait(0.05):
            pass
        if handle.cancelled:
            raise TaskCancelled()
        g = concurrency.gate()
        if not g.acquire(stop_check=lambda: handle.cancelled):
            raise TaskCancelled()
        with ctl.lock:
            ctl.holders.add(name)
        try:
            while not handle.cancelled and not ctl.releases[name].wait(0.05):
                pass
        finally:
            g.release()
            with ctl.lock:
                ctl.holders.discard(name)
        if handle.cancelled:
            raise TaskCancelled()
        with ctl.lock:
            ctl.done.add(name)
        return {"ok": True, "name": name}

    return stub


def _launch(monkeypatch, names, c: int, go: bool = True):
    """suggest_tags_batch with a controllable stub on the REAL shared LLM gate
    (limit c). The Ctl covers ALL tracks — non-conflicting tracks may be added
    to a running batch mid-test (the in-flight 409 tests do exactly that)."""
    core_config.update_config({"generation": {"max_concurrency": c}})
    ctl = Ctl(NAMES)
    if go:
        ctl.go.set()
    monkeypatch.setattr("backend.engines.music.suggest_track_tags", _make_stub(ctl))
    res = api_music.suggest_tags_batch(api_music.SuggestBatchReq(names=list(names)))
    return ctl, get_task_manager(), res


# -- PENDING shells in order + labels pinned -----------------------------------

def test_batch_creates_pending_shells_in_order(workspace, monkeypatch):
    # C=2, 6 tracks: the coordinator's first round dispatches exactly
    # BGM_PREFETCH_DEPTH (4); the rest stay PENDING shells.
    ctl, mgr, res = _launch(monkeypatch, NAMES[:6], 2, go=False)
    task_ids = res["task_ids"]
    assert [row["name"] for row in res["tracks"]] == NAMES[:6]  # request order
    assert len(task_ids) == 6 and len(set(task_ids)) == 6
    tasks = [mgr.get(tid) for tid in task_ids]
    assert all(t is not None for t in tasks)
    assert [t.seq for t in tasks] == sorted(t.seq for t in tasks)  # seq = request order
    # The label tail is the load-bearing contract shared with the frontend's F5
    # reattach and the in-flight 409 — pin BOTH label shapes.
    assert all(t.module == "music-ai-tags" for t in tasks)
    assert [t.label for t in tasks] == [f"AI 推荐标签：{n}" for n in NAMES[:6]]
    _wait_until(lambda: len(ctl.order) == 4)
    assert ctl.order == NAMES[:4]  # exactly the first 4 dispatched, in order
    assert all(mgr.get(tid).status is TaskStatus.RUNNING for tid in task_ids[:4])
    assert all(mgr.get(tid).status is TaskStatus.PENDING for tid in task_ids[4:])
    assert concurrency.gate().active == 0  # nothing acquired a slot yet

    ctl.go.set()
    for n in NAMES[:6]:
        ctl.releases[n].set()
    _wait_until(lambda: len(ctl.order) == 6 and all(
        mgr.get(tid).status in TERMINAL for tid in task_ids))
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    assert ctl.order == NAMES[:6]  # strict request order end to end


# -- prefetch cap + strict order ------------------------------------------------

def test_batch_coordinator_cap_and_order(workspace, monkeypatch):
    # C=2, 6 tracks: steady state = 2 holders + 4 slot-waiters, never more;
    # strict request order as the shared LLM gate frees.
    ctl, mgr, res = _launch(monkeypatch, NAMES[:6], 2)
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
        assert ctl.order == NAMES[:6]
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
        # the shared coordinator's prefetch depth (imported — tracks the constant)
        assert running <= 2 + BGM_PREFETCH_DEPTH, f"in-flight over-cap: {running}"


# -- the endpoint sizes the gate -------------------------------------------------

def test_batch_endpoint_sizes_shared_llm_gate(workspace, monkeypatch):
    # suggest_tags_batch calls set_concurrency(generation.max_concurrency) on
    # every request.
    _launch(monkeypatch, NAMES[:2], 3, go=False)
    assert concurrency.gate().limit == 3


# -- guards ------------------------------------------------------------------------

def test_batch_empty_400(workspace):
    with pytest.raises(HTTPException) as e:
        api_music.suggest_tags_batch(api_music.SuggestBatchReq(names=[]))
    assert e.value.status_code == 400
    assert "请选择要 AI 识别标签的音乐" in e.value.detail


def test_batch_traversal_400(workspace):
    for bad in ("../evil.mp3", "a/b.mp3", "song.ogg"):
        with pytest.raises(HTTPException) as e:
            api_music.suggest_tags_batch(api_music.SuggestBatchReq(names=[bad]))
        assert e.value.status_code == 400


def test_batch_missing_track_404(workspace):
    with pytest.raises(HTTPException) as e:
        api_music.suggest_tags_batch(api_music.SuggestBatchReq(names=["ghost.mp3"]))
    assert e.value.status_code == 404
    assert "ghost.mp3" in e.value.detail


def test_batch_model_empty_400(workspace):
    core_config.update_config({"llm": {"model_name": ""}})
    with pytest.raises(HTTPException) as e:
        api_music.suggest_tags_batch(api_music.SuggestBatchReq(names=["t1.mp3"]))
    assert e.value.status_code == 400
    assert "尚未配置 LLM 模型" in e.value.detail


def test_batch_dedupe_preserves_order(workspace, monkeypatch):
    ctl, mgr, res = _launch(monkeypatch, ["t1.mp3", "t2.mp3", "t1.mp3"], 3, go=False)
    assert [row["name"] for row in res["tracks"]] == ["t1.mp3", "t2.mp3"]
    assert len(res["task_ids"]) == 2
    for tid in res["task_ids"]:
        mgr.control(tid, "cancel")
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL for tid in res["task_ids"]))


# -- same-track in-flight 409 ------------------------------------------------------

def test_batch_inflight_same_track_409(workspace, monkeypatch):
    # t1 / t2 in flight (holding their slots): resubmitting t1 is 409; adding a
    # non-conflicting track is allowed; a mixed request short-circuits.
    ctl, mgr, res = _launch(monkeypatch, NAMES[:2], 2, go=False)
    ctl.go.set()
    _wait_until(lambda: len(ctl.holders) == 2)

    with pytest.raises(HTTPException) as e:
        api_music.suggest_tags_batch(api_music.SuggestBatchReq(names=["t1.mp3"]))
    assert e.value.status_code == 409
    assert "t1.mp3" in e.value.detail

    res3 = api_music.suggest_tags_batch(api_music.SuggestBatchReq(names=["t3.mp3"]))
    assert res3["tracks"][0]["name"] == "t3.mp3"

    with pytest.raises(HTTPException) as e2:
        api_music.suggest_tags_batch(api_music.SuggestBatchReq(names=["t1.mp3", "t3.mp3"]))
    assert e2.value.status_code == 409

    ctl.releases["t1.mp3"].set()
    ctl.releases["t2.mp3"].set()
    ctl.releases["t3.mp3"].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL
                            for tid in [*res["task_ids"], *res3["task_ids"]]),
                timeout=5)


# -- cancel all converges -----------------------------------------------------------

def _cancel_all_converges(ctl, mgr, task_ids):
    pending = [tid for tid in task_ids if mgr.get(tid).status is TaskStatus.PENDING]
    for tid in task_ids:
        mgr.control(tid, "cancel")
    _wait_until(lambda: all(mgr.get(tid).status is TaskStatus.CANCELLED for tid in task_ids),
                timeout=5)
    _wait_until(lambda: concurrency.gate().active == 0, timeout=5)
    for tid in pending:  # the not-yet-dispatched shells finalized immediately
        t = mgr.get(tid)
        assert t.status is TaskStatus.CANCELLED and t.finished > 0


def test_batch_cancel_all_converges(workspace, monkeypatch):
    # C=2, 8 tracks: 6 dispatched (2 holders + 4 waiters), 2 PENDING shells left.
    ctl, mgr, res = _launch(monkeypatch, NAMES, 2)
    _wait_until(lambda: len(ctl.order) == 6 and concurrency.gate().active == 2)
    _cancel_all_converges(ctl, mgr, res["task_ids"])
