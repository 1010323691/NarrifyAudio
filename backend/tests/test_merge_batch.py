"""Tests for the 音频合并 batch dispatch (``api/tts.py``): ordered + prefetched
coordinator over the REAL process-wide merge gate, the per-package in-flight 409,
the endpoint guards, and the read-only ``GET /merge-status`` rows.

``run_merge`` is called directly (the suite never goes through the HTTP layer);
``backend.engines.merge.run`` is monkeypatched with a controllable stub that uses the
REAL ``merge_gate()`` — the coordinator's decisions are driven by
``merge_gate().active`` exactly as in production. The endpoint sizes the gate from the
CPU formula on every call, so the tests pin the formula input (``os.cpu_count``) to
steer the limit.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.api import tts as api_tts
from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core import concurrency
from backend.core.tasks import TERMINAL, TaskCancelled, TaskStatus, get_task_manager
import backend.engines.merge as merge_engine

PKGS = [f"pkg{i}" for i in range(1, 9)]  # up to 8 packages


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """Throwaway project root + workspace, plus a drained merge gate afterwards."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    ws.mkdir(parents=True)  # require_workspace (a write guard) checks the folder exists
    core_config.set_workspace_pointer(str(ws))
    gate_limit = concurrency.merge_gate().limit
    yield ws
    # A failed test must not leak gate slots / zombie stub threads into later tests:
    # cancel every still-active merge task (the stubs honour cooperative cancel and
    # release their slots), then let the merge gate drain before restoring its limit.
    mgr = get_task_manager()
    for t in list(mgr.list()):
        if t.module == "merge" and t.status not in TERMINAL:
            try:
                mgr.control(t.id, "cancel")
            except KeyError:
                pass
    deadline = time.time() + 5
    stuck = []
    while time.time() < deadline:
        stuck = [t for t in mgr.list()
                 if t.module == "merge" and t.status not in TERMINAL]
        if not stuck and concurrency.merge_gate().active == 0:
            break
        time.sleep(0.05)
    assert not stuck and concurrency.merge_gate().active == 0, (
        f"merge gate / tasks leaked: {[t.label for t in stuck]}, active="
        f"{concurrency.merge_gate().active}")
    concurrency.set_merge_concurrency(gate_limit)
    core_config.reset_config_cache()


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
        self.order: list[str] = []      # packages whose stub body started (in start order)
        self.holders: set[str] = set()  # packages currently holding a merge slot
        self.done: set[str] = set()     # packages whose stub returned
        self.go = threading.Event()     # park point BEFORE the gate acquire
        self.releases: dict[str, threading.Event] = {p: threading.Event() for p in PKGS}

    def snapshot_holders(self) -> list[str]:
        with self.lock:
            return sorted(self.holders)


def _make_stub(ctl: Ctl):
    def stub(handle, m4b, package):
        with ctl.lock:
            ctl.order.append(package)
        # Test-controlled park point: no gate access until released. Cancel-aware, like
        # the real engine — a teardown cancel must finalize the task instead of leaking
        # a RUNNING task parked on the event forever.
        while not handle.cancelled and not ctl.go.wait(0.05):
            pass
        if handle.cancelled:
            raise TaskCancelled()
        g = concurrency.merge_gate()
        # Like the real engine: cooperative wait — a cancel while queued must abort
        # (no slot taken) within one stop_check poll (0.2 s).
        if not g.acquire(stop_check=lambda: handle.cancelled):
            raise TaskCancelled()
        with ctl.lock:
            ctl.holders.add(package)
        try:
            # Keep the slot until the test releases this package (or a cancel lands —
            # the real engine honours it inside run_worker's pump loop).
            while not handle.cancelled and not ctl.releases[package].wait(0.05):
                pass
        finally:
            g.release()
            with ctl.lock:
                ctl.holders.discard(package)
        if handle.cancelled:
            raise TaskCancelled()
        with ctl.lock:
            ctl.done.add(package)
        return {"ok": True, "package": package}

    return stub


def _launch(monkeypatch, pkgs, concurrency_limit: int, go: bool = True):
    """Create the packages' PENDING shells via the real endpoint + start the stub.

    ``go=False`` leaves the stubs parked BEFORE their gate acquire, so the post-launch
    state (dispatched vs PENDING, gate occupancy) is fully deterministic. The endpoint
    sizes the merge gate from the CPU formula, so the formula's input is pinned to
    ``2 * concurrency_limit`` (the formula divides by 2, cap 4).
    Returns (ctl, mgr, response dict).
    """
    ctl = Ctl()
    if go:
        ctl.go.set()
    monkeypatch.setattr("backend.engines.merge.run", _make_stub(ctl))
    monkeypatch.setattr(merge_engine.os, "cpu_count", lambda: 2 * concurrency_limit)
    mgr = get_task_manager()
    res = api_tts.run_merge(api_tts.MergeRequest(packages=list(pkgs)))
    return ctl, mgr, res


# -- PENDING shells in order ------------------------------------------------------

def test_merge_creates_pending_shells_in_order(workspace, monkeypatch):
    # C=2, 6 packages, stubs parked before the gate: the coordinator's first round
    # dispatches exactly MERGE_PREFETCH_DEPTH packages (waiting = 0 → starts until
    # waiting = 4); the rest stay PENDING shells (registered, visible, non-terminal).
    ctl, mgr, res = _launch(monkeypatch, PKGS[:6], 2, go=False)
    task_ids = res["task_ids"]
    assert [row["package"] for row in res["packages"]] == PKGS[:6]  # request order
    assert len(task_ids) == 6 and len(set(task_ids)) == 6
    tasks = [mgr.get(tid) for tid in task_ids]
    assert all(t is not None for t in tasks)
    assert [t.seq for t in tasks] == sorted(t.seq for t in tasks)  # seq = request order
    # The label tail is the load-bearing contract shared with the frontend's F5
    # reattach and the in-flight 409 — pin it.
    assert all(t.module == "merge" for t in tasks)
    assert [t.label for t in tasks] == [f"合并音频（Merge）：{p}" for p in PKGS[:6]]
    # First coordinator round has landed; the state is then STABLE (the 4 parked
    # tasks count as slot-waiters → waiting = 4 = cap → nothing more is dispatched).
    _wait_until(lambda: len(ctl.order) == 4)
    assert ctl.order == PKGS[:4]  # exactly the first 4 dispatched, in order
    assert all(mgr.get(tid).status is TaskStatus.RUNNING for tid in task_ids[:4])
    assert all(mgr.get(tid).status is TaskStatus.PENDING for tid in task_ids[4:])
    assert concurrency.merge_gate().active == 0  # nothing acquired a slot yet

    # Release the park: the 4 proceed (2 hold, 2 queue), the coordinator dispatches
    # 5 / 6 (waiting drops to 2 < 4); everything then completes.
    ctl.go.set()
    for p in PKGS[:6]:
        ctl.releases[p].set()
    _wait_until(lambda: len(ctl.order) == 6 and all(
        mgr.get(tid).status in TERMINAL for tid in task_ids))
    assert all(mgr.get(tid).status is TaskStatus.SUCCEEDED for tid in task_ids)
    assert ctl.order == PKGS[:6]  # strict request order end to end


# -- prefetch cap + strict order ---------------------------------------------------

def test_merge_coordinator_cap_and_order(workspace, monkeypatch):
    # C=2, 6 packages: steady state = 2 holders + 4 slot-waiters (= C+4), never more;
    # every package is dispatched in strict request order as the gate frees. A
    # monitor samples the invariants through the whole run.
    ctl, mgr, res = _launch(monkeypatch, PKGS[:6], 2)
    task_ids = res["task_ids"]
    samples: list[tuple[int, int]] = []
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.02):
            running = sum(
                1 for tid in task_ids
                if mgr.get(tid).status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
            )
            samples.append((concurrency.merge_gate().active, running))

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    try:
        _wait_until(lambda: len(ctl.order) == 6)  # all 6 dispatched
        assert ctl.order == PKGS[:6]  # strict request order, despite C=2
        # Release the holders round by round; the gate + coordinator refill. (Same
        # "not-yet-released" wave condition as the parse batch tests — a plain size
        # check races the discard/add of consecutive waves.)
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
        assert running <= 2 + api_tts.MERGE_PREFETCH_DEPTH, f"in-flight over-cap: {running}"


# -- the endpoint sizes the gate from the CPU formula ------------------------------

def test_merge_endpoint_sizes_gate_from_cpu(workspace, monkeypatch):
    # The endpoint calls set_merge_concurrency(Merge.concurrency_limit()) on every
    # request: cpu_count=6 -> 6//2 = 3 (under the 4 cap) lands in the real gate.
    ctl, mgr, res = _launch(monkeypatch, PKGS[:2], 3, go=False)
    assert concurrency.merge_gate().limit == 3


# -- endpoint guards ----------------------------------------------------------------

def test_merge_endpoint_empty_400(workspace, monkeypatch):
    with pytest.raises(HTTPException) as e:
        api_tts.run_merge(api_tts.MergeRequest(packages=[]))
    assert e.value.status_code == 400
    # Both fields absent: no "most recent package" fallback — an explicit selection is
    # the only way to start a merge.
    with pytest.raises(HTTPException) as e2:
        api_tts.run_merge(api_tts.MergeRequest())
    assert e2.value.status_code == 400


def test_merge_endpoint_legacy_field_400(workspace, monkeypatch):
    # The old single-select field is refused with an actionable (rebuild the frontend)
    # message — a stale pre-built dist must never hit the new multi-select path.
    with pytest.raises(HTTPException) as e:
        api_tts.run_merge(api_tts.MergeRequest(packages=["pkg1"], package="pkg1"))
    assert e.value.status_code == 400
    assert "前端版本过旧" in e.value.detail


def test_merge_endpoint_bad_name_400(workspace, monkeypatch):
    for bad in (["../etc"], ["a/b"], [""]):
        with pytest.raises(HTTPException) as e:
            api_tts.run_merge(api_tts.MergeRequest(packages=bad))
        assert e.value.status_code == 400


def test_merge_endpoint_dedupes(workspace, monkeypatch):
    # Duplicate package names collapse (order-preserving) before any task is created.
    ctl, mgr, res = _launch(monkeypatch, ["pkg1", "pkg1", "pkg2"], 2, go=False)
    assert [row["package"] for row in res["packages"]] == ["pkg1", "pkg2"]
    assert len(res["task_ids"]) == 2 and len(set(res["task_ids"])) == 2


# -- same-package in-flight 409 ------------------------------------------------------

def test_merge_inflight_same_package_409(workspace, monkeypatch):
    # pkg1 / pkg2 in flight (holding their slots): resubmitting pkg1 is 409; adding a
    # non-conflicting package is allowed (its task queues behind the gate); a mixed
    # request short-circuits all-or-nothing on the conflict (no partial creation).
    ctl, mgr, res = _launch(monkeypatch, PKGS[:2], 2, go=False)
    ctl.go.set()
    _wait_until(lambda: len(ctl.holders) == 2)

    with pytest.raises(HTTPException) as e:
        api_tts.run_merge(api_tts.MergeRequest(packages=["pkg1"]))
    assert e.value.status_code == 409
    assert "pkg1" in e.value.detail

    res3 = api_tts.run_merge(api_tts.MergeRequest(packages=["pkg3"]))
    assert res3["packages"][0]["package"] == "pkg3"

    with pytest.raises(HTTPException) as e2:
        api_tts.run_merge(api_tts.MergeRequest(packages=["pkg1", "pkg3"]))
    assert e2.value.status_code == 409

    # Drain: release everything so every task converges (fast teardown).
    for p in PKGS[:3]:
        ctl.releases[p].set()
    _wait_until(lambda: all(mgr.get(tid).status in TERMINAL
                            for tid in [*res["task_ids"], *res3["task_ids"]]),
                timeout=5)


# -- 【取消全部】= per-task cancel converges the whole batch --------------------------

def test_merge_cancel_all_converges(workspace, monkeypatch):
    # C=2, 8 packages: steady state = 2 holders + 4 slot-waiters dispatched; packages
    # 7 / 8 remain undispatched PENDING shells. Per-task cancel (the frontend's 取消全部
    # — merge has no cancel-batch endpoint) must finalize those in place, cancel the
    # in-flight (holders at their next poll, waiters via stop_check), and drain the gate.
    ctl, mgr, res = _launch(monkeypatch, PKGS, 2)
    task_ids = res["task_ids"]
    _wait_until(lambda: len(ctl.order) == 6 and concurrency.merge_gate().active == 2)
    pending = [tid for tid in task_ids if mgr.get(tid).status is TaskStatus.PENDING]
    assert len(pending) == 2  # packages 7 / 8 must stay PENDING until cancelled

    for tid in task_ids:
        mgr.control(tid, "cancel")

    _wait_until(lambda: all(mgr.get(tid).status is TaskStatus.CANCELLED for tid in task_ids),
                timeout=5)
    # Queued acquirers bailed via stop_check and the holders released: gate is clean.
    _wait_until(lambda: concurrency.merge_gate().active == 0, timeout=5)
    # The not-yet-dispatched shells were finalized immediately (no slot, no worker).
    for tid in pending:
        t = mgr.get(tid)
        assert t.status is TaskStatus.CANCELLED and t.finished > 0


# -- GET /merge-status ----------------------------------------------------------------

def _seed_package(ws, pkg: str, n_segments: int, n_ok_manifest: int, missing=()) -> None:
    """A source parsed JSON of ``n_segments`` lines + a manifest of ``n_ok_manifest``
    ok entries (files for non-missing indices created on disk)."""
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    data = [{"speaker": "NARRATOR", "text": f"台词 {i}。", "instruct": ""}
            for i in range(n_segments)]
    (ws / "03_parsed_json" / f"{pkg}.json").write_bytes(
        json.dumps(data, ensure_ascii=False).encode("utf-8"))
    pkg_dir = ws / "05_audio_chunk" / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for i in range(n_ok_manifest):
        f = pkg_dir / f"{i:04d}.mp3"
        if i not in missing:
            f.write_bytes(b"0" * 32)
        entries.append({"index": i, "speaker": "N", "text": f"t{i}",
                        "path": str(f), "ok": True, "reason": ""})
    if entries:
        (pkg_dir / "manifest.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def test_merge_status_counts_json_total(workspace):
    # total = the source parsed JSON's synthesizable segment count (the /batch-status
    # rule), not the manifest length.
    _seed_package(workspace, "p1", n_segments=10, n_ok_manifest=10)
    row = api_tts.merge_status(packages=["p1"])["packages"][0]
    assert row == {"name": "p1", "total": 10, "completed": 10,
                   "remaining": 0, "complete": True}


def test_merge_status_partial_manifest_not_ready(workspace):
    # The key fix: a synthesis cancelled mid-way leaves a manifest shorter than the
    # source JSON — total must come from the JSON, so the package is NOT 已就绪 and a
    # half book can never be merged silently.
    _seed_package(workspace, "p1", n_segments=100, n_ok_manifest=60)
    row = api_tts.merge_status(packages=["p1"])["packages"][0]
    assert row["total"] == 100
    assert row["completed"] == 60
    assert row["remaining"] == 40
    assert row["complete"] is False


def test_merge_status_missing_source_falls_back(workspace):
    # No source JSON -> degrade to the manifest length (older projects / manual setups).
    _seed_package(workspace, "p1", n_segments=0, n_ok_manifest=3)
    row = api_tts.merge_status(packages=["p1"])["packages"][0]
    assert row["total"] == 3
    assert row["completed"] == 3
    assert row["complete"] is True


def test_merge_status_missing_segment_file_not_done(workspace):
    # completed = ok AND file still on disk (Batch.is_done): a vanished file counts as
    # remaining, so the row reports 已合成 2/3 instead of 已就绪.
    _seed_package(workspace, "p1", n_segments=3, n_ok_manifest=3, missing={2})
    row = api_tts.merge_status(packages=["p1"])["packages"][0]
    assert row["total"] == 3
    assert row["completed"] == 2
    assert row["remaining"] == 1
    assert row["complete"] is False


def test_merge_status_zero_row(workspace):
    row = api_tts.merge_status(packages=["ghost"])["packages"][0]
    assert row == {"name": "ghost", "total": 0, "completed": 0,
                   "remaining": 0, "complete": False}


def test_merge_status_traversal_400(workspace, monkeypatch):
    with pytest.raises(HTTPException) as e:
        api_tts.merge_status(packages=["../evil"])
    assert e.value.status_code == 400


def test_merge_status_no_workspace_zero_rows(monkeypatch, tmp_path):
    # Read-only endpoint: no workspace -> all-zero rows (degrade, never 409).
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        res = api_tts.merge_status(packages=["p1", "p2"])
        assert res == {"packages": [
            {"name": "p1", "total": 0, "completed": 0, "remaining": 0, "complete": False},
            {"name": "p2", "total": 0, "completed": 0, "remaining": 0, "complete": False},
        ]}
    finally:
        core_config.reset_config_cache()
