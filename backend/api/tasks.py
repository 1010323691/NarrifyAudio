"""Unified task-system endpoints (requirement #2): list / detail / control / SSE.

The SSE stream (``GET /api/tasks/{id}/stream``) is how the task centre gets live
progress + logs. It replays the current snapshot on connect, then forwards each
event until the task reaches a terminal state.
"""
from __future__ import annotations

import json
import queue as _queue

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..core.tasks import TERMINAL, get_task_manager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("")
def list_tasks() -> list[dict]:
    return [t.snapshot() for t in get_task_manager().list()]


@router.get("/stream")
def stream_all_tasks():
    """Multiplexed SSE: ONE connection streams the events of ALL tasks.

    Every event carries ``task_id``; the client dispatches on it. The UI holds
    exactly one such stream per tab — browsers cap simultaneous HTTP/1.1
    connections per host at ~6, so one connection *per task* (the
    ``/{task_id}/stream`` endpoint below) is exhausted by a few parallel parses:
    every EventSource beyond the cap never connects and its window shows no logs
    at all while the backend runs fine.

    On connect it replays ``snapshot_all`` (a snapshot of every task, so a late
    or reconnecting client starts with the full picture — and a reconnect
    self-heals anything missed), then forwards every event until the client
    disconnects. The stream never ends on its own (tasks are created over time);
    a 15 s ``ping`` keeps idle connections alive.
    """

    def gen():
        mgr = get_task_manager()
        # Subscribe AFTER snapshotting: an event emitted in between lands in the
        # queue and is forwarded once (no replay/live overlap for the same event).
        q = mgr.subscribe_all()
        try:
            yield _sse({"type": "snapshot_all", "tasks": [t.snapshot() for t in mgr.list()]})
            while True:
                try:
                    task, event = q.get(timeout=15)
                except _queue.Empty:
                    yield _sse({"type": "ping"})  # keep-alive
                    continue
                payload = dict(event)
                payload["task_id"] = task.id
                yield _sse(payload)
        finally:
            mgr.unsubscribe_all(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{task_id}")
def get_task(task_id: str) -> dict:
    task = get_task_manager().get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task.snapshot()


@router.post("/{task_id}/{action}")
def control_task(task_id: str, action: str) -> dict:
    """action ∈ {cancel, pause, resume, retry}."""
    try:
        task = get_task_manager().control(task_id, action)
    except KeyError:
        raise HTTPException(404, "任务不存在")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return task.snapshot()


@router.get("/{task_id}/stream")
def stream_task(task_id: str):
    task = get_task_manager().get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")

    def gen():
        # Snapshot BEFORE subscribing so the replayed state and the live events that
        # follow never overlap: a chunk logged after the snapshot is forwarded once as
        # an event, rather than appearing both in the snapshot and again as an event.
        # (The lost-event window between the snapshot and the subscribe is a few
        # instructions wide — negligible, and self-heals on the next snapshot/final.)
        initial = task.snapshot()
        q = task.subscribe()
        try:
            # Replay state so a late subscriber starts with the full picture.
            yield _sse({"type": "snapshot", "task": initial})
            if task.status in TERMINAL:
                return
            while True:
                try:
                    event = q.get(timeout=15)
                except _queue.Empty:
                    yield _sse({"type": "ping"})  # keep-alive
                    continue
                yield _sse(event)
                if event.get("type") == "final":
                    break
        finally:
            task.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
