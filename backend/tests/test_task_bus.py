"""Tests for the process-wide task event bus (core.tasks) that feeds the
multiplexed ``GET /api/tasks/stream`` SSE endpoint.

One such connection per browser tab must carry the events of ALL tasks: browsers
cap simultaneous HTTP/1.1 connections per host at ~6, so the old
one-EventSource-per-task design was exhausted by a few parallel parses (every
window beyond the cap showed no logs while the backend ran fine). These tests
lock in the bus semantics: every manager-created task forwards its events to
bus subscribers as ``(task, event)`` tuples, display-only events
(``llm_chunk`` / ``llm_rate``) are dropped under queue pressure while critical
events (``log`` / ``progress`` / ``status`` / ``final``) are kept, and the
multiplexed route replays a ``snapshot_all`` before going live.
"""
import asyncio
import json
import time

from backend.api.tasks import stream_all_tasks
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


# -- the multiplexed SSE route ----------------------------------------------------
def _slow_task(handle, msg: str) -> dict:
    # Log AFTER the stream has subscribed (the multiplexed route subscribes on
    # its first iteration, which is milliseconds after task creation).
    time.sleep(0.5)
    handle.log(msg)
    return {"ok": True}


def test_stream_all_route_replays_snapshot_then_streams_tagged_events():
    # The route reads the process-wide singleton — create the task there too.
    from backend.core.tasks import get_task_manager

    task = get_task_manager().create("script", "stream-all", _slow_task, "stream-hello")

    resp = stream_all_tasks()
    body = resp.body_iterator  # async iterator (sync gen wrapped in the threadpool)

    async def pull(n: int):
        out = []
        for _ in range(n):
            out.append(await body.__anext__())
        return out

    frames = asyncio.run(pull(2))
    e0 = json.loads(frames[0][len("data: "):].strip())
    e1 = json.loads(frames[1][len("data: "):].strip())

    assert e0["type"] == "snapshot_all"
    assert any(t["id"] == task.id for t in e0["tasks"])  # the fresh task is replayed
    assert e1["task_id"] == task.id  # live events are tagged for client dispatch
    assert e1["type"] in ("log", "progress", "status")
    assert resp.media_type == "text/event-stream"
