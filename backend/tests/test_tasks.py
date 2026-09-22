"""Tests for the TaskManager's deferred-start (PENDING shell) semantics.

The 文本解析 batch endpoint creates every file's task as a PENDING shell
(``create(..., start=False)``) so the response can carry all task_ids at once, then a
coordinator thread starts them in order via ``start(task_id)``. These tests lock in
the shell lifecycle: a shell is registered (visible / counted non-terminal) but has no
thread; ``start`` runs it exactly like the default path; cancelling a shell finalizes
it in place (no worker thread would ever observe the cancel_event); and the
start / cancel race cannot hang or leak a PENDING task.
"""
import threading
import time

from backend.core.tasks import (
    TERMINAL,
    Task,
    TaskCancelled,
    TaskManager,
    TaskStatus,
)


def _worker_ok(handle) -> dict:
    return {"ok": True}


def _worker_cancel(handle) -> dict:
    # Simulate an engine that honours cooperative cancel mid-work.
    handle.check()
    return {"ok": True}


def _wait_terminal(task: Task, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while task.status not in TERMINAL:
        if time.time() > deadline:
            raise AssertionError(f"task {task.id} still {task.status.value} after {timeout}s")
        time.sleep(0.01)


# -- PENDING shell creation -------------------------------------------------------

def test_create_start_false_stays_pending():
    mgr = TaskManager()
    q = mgr.subscribe_all()
    task = mgr.create("script", "文本解析（a.txt）", _worker_ok, start=False)

    assert task.status is TaskStatus.PENDING
    assert task._thread is None
    assert task.started == 0.0
    # Registered: visible to lookups / the API list, counted non-terminal (SSE keep-alive).
    assert mgr.get(task.id) is task
    assert any(t.id == task.id for t in mgr.list())
    assert task.status not in TERMINAL
    # No worker ran: no running status event reached the bus.
    deadline = time.time() + 0.5
    running_seen = False
    while time.time() < deadline:
        try:
            _t, e = q.get(timeout=0.1)
        except Exception:
            break
        if e.get("type") == "status" and e.get("status") == "running":
            running_seen = True
    mgr.unsubscribe_all(q)
    assert not running_seen
    assert task.status is TaskStatus.PENDING


def test_create_default_unchanged():
    # The default path (start=True) behaves exactly as before: RUNNING + thread + events.
    mgr = TaskManager()
    task = mgr.create("script", "默认启动", _worker_ok)
    assert task._thread is not None
    _wait_terminal(task)
    assert task.status is TaskStatus.SUCCEEDED
    assert task.started > 0.0
    assert task.result == {"ok": True}
    assert "任务开始" in [e["msg"] for e in task.logs]
    assert "任务完成" in [e["msg"] for e in task.logs]


# -- start(task_id) ----------------------------------------------------------------

def test_start_pending_task_runs_and_succeeds():
    # A held worker makes the RUNNING window observable: an instant worker can
    # reach SUCCEEDED before the test's next statement (status is not asserted
    # on a race).
    entered = threading.Event()
    release = threading.Event()

    def _worker_hold(handle) -> dict:
        entered.set()  # worker thread is up and running
        release.wait(10)
        return {"ok": True}

    mgr = TaskManager()
    task = mgr.create("script", "文本解析（b.txt）", _worker_hold, start=False)
    mgr.start(task.id)
    assert entered.wait(5), "start() never ran the worker thread"
    assert task.status is TaskStatus.RUNNING
    assert task.started > 0.0
    release.set()
    _wait_terminal(task)
    assert task.status is TaskStatus.SUCCEEDED
    assert task.result == {"ok": True}


def test_start_non_pending_raises():
    mgr = TaskManager()
    task = mgr.create("script", "already running", _worker_ok)
    try:
        mgr.start(task.id)
        raise AssertionError("start on a non-PENDING task must raise ValueError")
    except ValueError:
        pass
    _wait_terminal(task)
    # Unknown id raises KeyError (the coordinator swallows both as benign races).
    try:
        mgr.start("nope")
        raise AssertionError("start on an unknown id must raise KeyError")
    except KeyError:
        pass


# -- cancelling a shell finalizes it in place --------------------------------------

def test_cancel_pending_task_finalizes_immediately():
    mgr = TaskManager()
    q = mgr.subscribe_all()
    task = mgr.create("script", "文本解析（c.txt）", _worker_cancel, start=False)

    mgr.control(task.id, "cancel")

    # No worker thread exists for a shell — the cancel must finalize it right now,
    # or it would sit PENDING forever (and hold the SSE stream open).
    assert task.status is TaskStatus.CANCELLED
    assert task.finished > 0.0
    assert task.cancel_event.is_set()
    assert "任务已取消（未启动）" in [e["msg"] for e in task.logs]

    # Bus saw the terminal status (with snapshot) and a final event — the same
    # lifecycle a running task's cancel produces.
    got: list = []
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            got.append(q.get(timeout=0.1))
        except Exception:
            break
        if any(e.get("type") == "final" for _t, e in got):
            break
    mgr.unsubscribe_all(q)
    types = [e["type"] for _t, e in got]
    assert "final" in types
    status_ev = [e for _t, e in got if e.get("type") == "status" and e.get("status") == "cancelled"]
    assert status_ev and status_ev[0].get("task", {}).get("id") == task.id

    # A late start after the cancel is a benign race → ValueError (coordinator moves on).
    try:
        mgr.start(task.id)
        raise AssertionError("start after cancel must raise ValueError")
    except ValueError:
        pass
    assert task.status is TaskStatus.CANCELLED  # unchanged

    # retry revives the shell (terminal-state retry path): it re-runs to success.
    mgr.control(task.id, "retry")
    _wait_terminal(task)
    assert task.status is TaskStatus.SUCCEEDED
    assert task.result == {"ok": True}


# -- start / cancel race -------------------------------------------------------------

def test_start_cancel_race_no_hang():
    # Hammer start vs cancel concurrently: whatever wins, the task must end in a
    # terminal state (CANCELLED, or SUCCEEDED if the worker won the race and ran) —
    # never a leaked PENDING — and no thread may block forever on the lock.
    mgr = TaskManager()
    for _ in range(25):
        task = mgr.create("script", "race", _worker_ok, start=False)
        def starter():
            try:
                mgr.start(task.id)
            except (ValueError, KeyError):
                pass

        t = threading.Thread(target=starter)
        t.start()
        mgr.control(task.id, "cancel")  # may race the start
        t.join(timeout=5)
        assert not t.is_alive(), "start/cancel race deadlocked"
        _wait_terminal(task)
        assert task.status in TERMINAL, f"leaked {task.status.value}"
