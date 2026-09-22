"""Tests for the 文本解析 batch dispatch (``api/script.py``): ordered + prefetched
coordinator, the slot-scope handoff (a task that reaches the check stages runs
slot-free), and the 【取消全部】 endpoint.

``generate_files`` is called directly (the suite never goes through the HTTP layer);
``backend.engines.script.generate_file`` is monkeypatched with a controllable stub
that uses the REAL shared concurrency gate — the coordinator's decisions are driven
by ``gate().active`` + ``task.phase`` exactly as in production. Slot completion is
signalled per file (``releases[name]``) so the test controls completion order without
relying on the gate's (non-FIFO) ``notify_all`` wake order.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from backend.api import script as api_script
from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core import concurrency
from backend.core.tasks import TERMINAL, TaskCancelled, TaskStatus, get_task_manager

NAMES = [f"第 {i:03d} 章 章名{i}.txt" for i in range(1, 9)]  # up to 8 files


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """Throwaway project root + workspace with all source files (mirrors
    ``test_script.py``), plus a clean batch registry afterwards."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    core_config.set_workspace_pointer(str(ws))
    (ws / "02_split_text").mkdir(parents=True)
    for name in NAMES:
        (ws / "02_split_text" / name).write_bytes("夜色像潮水一样漫进街巷。\n".encode("utf-8"))
    gate_limit = concurrency.gate().limit
    yield ws
    # A failed test must not leak gate slots / zombie stub threads into later
    # tests: cancel every still-active task (the stubs honour cooperative cancel
    # and release their slots), then let the shared gate drain before restoring
    # its limit. Without this, one stuck stub pins gate.active at the limit and
    # every subsequent test's acquirers block forever.
    mgr = get_task_manager()
    for t in list(mgr.list()):
        if t.module == "script" and t.status not in TERMINAL:
            try:
                mgr.control(t.id, "cancel")
            except KeyError:
                pass
    deadline = time.time() + 5
    while concurrency.gate().active > 0 and time.time() < deadline:
        time.sleep(0.05)
    concurrency.set_concurrency(gate_limit)  # restore the shared gate's limit
    core_config.reset_config_cache()
    api_script._BATCHES.clear()
    api_script._TASK_BATCH.clear()


def _wait_until(pred, timeout: float = 8.0, step: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


class Ctl:
    """Per-test coordination points for the stub engine."""

    def __init__(self):
        self.lock = threading.Lock()
        self.order: list[str] = []      # files whose stub body started (in start order)
        self.holders: set[str] = set()  # files currently holding a gate slot
        self.done: set[str] = set()     # files whose stub returned
        self.mode = "hold"              # "hold" = keep the slot; "check" = release + phase("check")
        self.go = threading.Event()     # park point BEFORE the gate acquire
        self.check_done = threading.Event()
        self.releases: dict[str, threading.Event] = {n: threading.Event() for n in NAMES}

    def snapshot_holders(self) -> list[str]:
        with self.lock:
            return sorted(self.holders)


def _make_stub(ctl: Ctl):
    def stub(handle, path, llm, prompts, generation):
        name = Path(path).name
        with ctl.lock:
            ctl.order.append(name)
        ctl.go.wait()  # test-controlled park point: no gate access until released
        g = concurrency.gate()
        # Like the real engine: cooperative wait — a cancel while queued must abort
        # (no slot taken) within one stop_check poll (0.2 s).
        if not g.acquire(stop_check=lambda: handle.cancelled):
            raise TaskCancelled()
        with ctl.lock:
            ctl.holders.add(name)
        try:
            if ctl.mode == "check":
                # Reached the mechanical check stages: the slot is released and the
                # task reports phase("check") — the coordinator's refill signal.
                # The park point is cancel-aware (like the hold-mode loop) so a
                # teardown cancel finalizes the task instead of leaking a RUNNING
                # task parked on the event forever.
                g.release()
                handle.phase("check")
                while not handle.cancelled and not ctl.check_done.wait(0.1):
                    pass
                if handle.cancelled:
                    raise TaskCancelled()
                return {"ok": True, "name": name}
            # "hold": keep the slot until the test releases this file (or a cancel
            # lands — the real engine honours it at the next chunk boundary).
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


def _launch(monkeypatch, names, concurrency_limit: int, mode: str = "hold",
            go: bool = True):
    """Create the N files' PENDING shells via the real endpoint + start the stub.

    ``go=False`` leaves the stubs parked BEFORE their gate acquire, so the post-launch
    state (dispatched vs PENDING, gate occupancy) is fully deterministic.
    Returns (ctl, mgr, task_ids in request order).
    """
    ctl = Ctl()
    ctl.mode = mode
    if go:
        ctl.go.set()
    monkeypatch.setattr("backend.engines.script.generate_file", _make_stub(ctl))
    core_config.update_config({"generation": {"max_concurrency": concurrency_limit}})
    mgr = get_task_manager()
    res = api_script.generate_files(api_script.GenerateFilesRequest(files=names))
    return ctl, mgr, res["task_ids"]


# -- PENDING shells in order ------------------------------------------------------

def test_generate_files_creates_pending_shells_in_order(workspace, monkeypatch):
    # C=2, 6 files, stubs parked before the gate: the coordinator's first round
    # dispatches exactly PREFETCH_DEPTH files (waiting = 0 → starts until waiting = 4);
    # with the 4 parked tasks counted as slot-waiters, no further file is dispatched.
    ctl, mgr, task_ids = _launch(monkeypatch, NAMES[:6], 2, go=False)
    tasks = [mgr.get(tid) for tid in task_ids]
    assert all(t is not None for t in tasks)
    assert [t.seq for t in tasks] == sorted(t.seq for t in tasks)  # seq = request order
    # First coordinator round has landed; the state is then STABLE (the 4 parked
    # tasks count as slot-waiters → waiting = 4 = cap → nothing more is dispatched).
    _wait_until(lambda: len(ctl.order) == 4)
    assert ctl.order == NAMES[:4]  # exactly the first 4 dispatched, in order
    assert all(mgr.get(tid).status is TaskStatus.RUNNING for tid in task_ids[:4])
    assert all(mgr.get(tid).status is TaskStatus.PENDING for tid in task_ids[4:])
    assert concurrency.gate().active == 0  # nothing acquired a slot yet
    # Batch registry: one batch, ordered == request order, reverse map complete.
    batches = list(api_script._BATCHES.values())
    assert len(batches) == 1
    assert batches[0].ordered == task_ids
    assert [api_script._TASK_BATCH[tid] for tid in task_ids] == [batches[0].id] * len(task_ids)

    # Release the park: the 4 proceed (2 hold, 2 queue), the coordinator dispatches
    # 5 / 6 (waiting drops to 2 < 4); everything then completes.
    ctl.go.set()
    for n in NAMES[:6]:
        ctl.releases[n].set()
    _wait_until(lambda: len(ctl.order) == 6 and all(
        mgr.get(tid).status in TERMINAL for tid in task_ids))
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    # Coordinator finished and deregistered the batch.
    _wait_until(lambda: not api_script._BATCHES)
    assert not any(tid in api_script._TASK_BATCH for tid in task_ids)
    assert ctl.order == NAMES[:6]  # strict selection order end to end


# -- prefetch cap + strict order ---------------------------------------------------

def test_coordinator_prefetch_cap_and_order(workspace, monkeypatch):
    # C=2, 6 files, hold mode: steady state = 2 holders + 4 slot-waiters (= C+4),
    # never more; every file is dispatched in strict selection order as the gate
    # frees. A monitor samples the invariants through the whole run.
    ctl, mgr, task_ids = _launch(monkeypatch, NAMES[:6], 2)
    samples: list[tuple[int, int]] = []
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.02):
            running = sum(
                1 for tid in task_ids
                if mgr.get(tid).status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
            )
            samples.append((concurrency.gate().active, running))

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    try:
        _wait_until(lambda: len(ctl.order) == 6)  # all 6 dispatched
        assert ctl.order == NAMES[:6]  # strict selection order, despite C=2
        # Release the holders round by round; the gate + coordinator refill.
        # The wave condition must be "two holders NOT yet released": a plain
        # size==2 check is ambiguous — right after a wave's files free the gate
        # they can still be visible in holders() for a moment (their discard races
        # the next wave's add), so a stale wave could satisfy the wait and the
        # round would re-release the old files (no-op) while the new wave waits
        # forever (observed as cross-test gate-slot leaks under process load).
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
    _wait_until(lambda: not api_script._BATCHES)
    for active, running in samples:
        assert active <= 2, f"gate over-cap: {active}"
        assert running <= 2 + api_script.PREFETCH_DEPTH, f"in-flight over-cap: {running}"


# -- check phase releases prefetch headroom -----------------------------------------

def test_check_phase_releases_prefetch_headroom(workspace, monkeypatch):
    # C=2, 6 files, check mode: each file takes its slot, immediately releases it and
    # reports phase("check"), then blocks. With ZERO completions the coordinator must
    # still dispatch ALL 6 — check-phase tasks hold no slot AND claim no prefetch slot
    # (the core invariant: waiting ≤ 4, not in-flight ≤ C + 4 — under the latter
    # formulation these 4 would block the last 2 files and idle the gate).
    ctl, mgr, task_ids = _launch(monkeypatch, NAMES[:6], 2, mode="check")
    _wait_until(lambda: len(ctl.order) == 6)
    assert ctl.order == NAMES[:6]
    _wait_until(lambda: all(mgr.get(tid).phase == "check" for tid in task_ids))
    _wait_until(lambda: concurrency.gate().active == 0)  # all slots handed back
    assert all(mgr.get(tid).status is TaskStatus.RUNNING for tid in task_ids)
    # The coordinator is still alive (batch registered) while everyone sits in check.
    assert api_script._BATCHES
    ctl.check_done.set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL for tid in task_ids))
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    _wait_until(lambda: not api_script._BATCHES)


# -- 【取消全部】 stops dispatch + cancels the whole batch ---------------------------

def test_cancel_batch_stops_dispatch(workspace, monkeypatch):
    # C=2, 8 files: steady state = 2 holders + 4 slot-waiters dispatched; files 7 / 8
    # remain PENDING shells the coordinator has NOT dispatched yet. The cancel must
    # finalize those in place AND cancel the 6 in-flight (holders at their next chunk
    # boundary, waiters via the gate's stop_check within ~0.2 s).
    ctl, mgr, task_ids = _launch(monkeypatch, NAMES, 2)
    _wait_until(lambda: len(ctl.order) == 6 and concurrency.gate().active == 2)
    pending = [tid for tid in task_ids if mgr.get(tid).status is TaskStatus.PENDING]
    assert len(pending) == 2  # files 7 / 8 must stay PENDING until cancelled
    # Hold the state a beat so the coordinator can't dispatch them first: with the
    # 4 slot-waiters in place (waiting = 4 = cap) it won't, but assert it anyway.
    time.sleep(0.3)
    assert len([tid for tid in task_ids if mgr.get(tid).status is TaskStatus.PENDING]) == 2

    api_script.cancel_batch(api_script.CancelBatchRequest(task_ids=task_ids))

    _wait_until(lambda: all(mgr.get(tid).status is TaskStatus.CANCELLED for tid in task_ids),
                timeout=5)
    # Queued acquirers bailed via stop_check and the holders released: gate is clean.
    _wait_until(lambda: concurrency.gate().active == 0, timeout=5)
    # The batch is deregistered (coordinator saw stop / all-terminal and exited).
    _wait_until(lambda: not api_script._BATCHES)
    assert not any(tid in api_script._TASK_BATCH for tid in task_ids)
    # The not-yet-dispatched shells were finalized immediately (no slot, no worker).
    for tid in pending:
        t = mgr.get(tid)
        assert t.status is TaskStatus.CANCELLED and t.finished > 0


def test_cancel_batch_ignores_terminal_and_unknown(workspace, monkeypatch):
    ctl, mgr, task_ids = _launch(monkeypatch, NAMES[:2], 2)
    for n in NAMES[:2]:
        ctl.releases[n].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL for tid in task_ids))
    _wait_until(lambda: not api_script._BATCHES)
    # Idempotent: terminal + unknown ids are ignored, nothing raised.
    res = api_script.cancel_batch(
        api_script.CancelBatchRequest(task_ids=[*task_ids, "nope-nope"]))
    assert res["cancelled"] == [] and res["batches_stopped"] == 0
    # A fresh batch: cancel touches only its own (non-terminal) ids.
    ctl2, mgr2, task_ids2 = _launch(monkeypatch, NAMES[:2], 2)
    _wait_until(lambda: len(ctl2.order) >= 1)
    res2 = api_script.cancel_batch(api_script.CancelBatchRequest(task_ids=task_ids2))
    assert len(res2["cancelled"]) == 2 and res2["batches_stopped"] == 1
    _wait_until(lambda: all(mgr2.get(tid).status is TaskStatus.CANCELLED for tid in task_ids2))
