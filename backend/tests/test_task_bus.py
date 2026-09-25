"""Tests for the retired in-process task manager's event bus utilities."""
import time
from backend.core.tasks import Task, TaskManager


def _fast_task(handle, msg: str) -> dict:
    handle.log(msg)
    return {"ok": True}


# -- bus forwarding -------------------------------------------------------------
def test_bus_receives_task_events_as_tuples():
    mgr = TaskManager()
    q = mgr.subscribe_all()
    task = mgr.create("script", "bus-test", _fast_task, "hello-bus")
    got = []
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            item = q.get(timeout=0.1)
        except Exception:
            break
        got.append(item)
        if any(e.get("type") == "final" for _t, e in got):
            break
    mgr.unsubscribe_all(q)

    tasks_in = [t for t, _e in got]
    events = [e for _t, e in got]
    assert all(t is task for t in tasks_in)  # every tuple carries the task object
    types = [e["type"] for e in events]
    assert "log" in types and "final" in types
    assert any(e.get("msg") == "hello-bus" for e in events if e["type"] == "log")
    assert task.status.value == "succeeded"


def test_unsubscribed_bus_queue_stays_empty():
    mgr = TaskManager()
    q = mgr.subscribe_all()
    mgr.unsubscribe_all(q)
    task = mgr.create("script", "bus-quiet", _fast_task, "x")
    deadline = time.time() + 2
    while task.status.value not in ("succeeded", "failed"):
        if time.time() > deadline:
            break
        time.sleep(0.01)
    assert q.empty()  # no delivery after unsubscribe


def test_standalone_task_has_no_broadcast():
    # A Task not created via the manager emits only to its own listeners.
    t = Task(id="solo", module="m", label="l")
    assert t._broadcast is None
    q = t.subscribe()
    t.log("only-listeners")
    entry = q.get_nowait()
    assert entry["type"] == "log" and entry["level"] == "INFO" and entry["msg"] == "only-listeners"


# -- queue-pressure drop policy ---------------------------------------------------
def test_full_bus_drops_display_only_events():
    mgr = TaskManager()
    q = mgr.subscribe_all()
    t = Task(id="p", module="m", label="l")
    cap = q.maxsize
    for i in range(cap):
        mgr._bus_event(t, {"type": "llm_rate", "cps": i})
    assert q.full()
    mgr._bus_event(t, {"type": "llm_chunk", "data": "zzz"})  # display-only → dropped
    assert q.full()
    assert all(e["type"] == "llm_rate" for _task, e in list(q.queue))


def test_full_bus_keeps_critical_events_by_evicting_display_only():
    mgr = TaskManager()
    q = mgr.subscribe_all()
    t = Task(id="p", module="m", label="l")
    cap = q.maxsize
    for i in range(cap):
        mgr._bus_event(t, {"type": "llm_rate", "cps": i})
    assert q.full()

    mgr._bus_event(t, {"type": "log", "level": "INFO", "msg": "must-survive"})

    items = list(q.queue)
    assert len(items) == 1  # display-only backlog evicted, the log kept
    task_in, event = items[0]
    assert task_in is t
    assert event["type"] == "log" and event["msg"] == "must-survive"


def test_full_bus_keeps_critical_events_when_backlog_is_critical():
    mgr = TaskManager()
    q = mgr.subscribe_all()
    t = Task(id="p", module="m", label="l")
    cap = q.maxsize
    for i in range(cap):
        mgr._bus_event(t, {"type": "log", "level": "INFO", "msg": f"old-{i}"})
    assert q.full()

    # Nothing display-only to evict: the new critical event is dropped rather
    # than clobbering older critical ones (better to lose one than reorder/lose all).
    mgr._bus_event(t, {"type": "log", "level": "INFO", "msg": "newest"})
    items = list(q.queue)
    assert len(items) == cap
    assert all(e["msg"].startswith("old-") for _task, e in items)
