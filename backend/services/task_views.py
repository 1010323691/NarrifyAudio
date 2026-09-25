"""Task-view logic for the v1 ``/api/v1/tasks/stream`` aggregate endpoint
(S7/5a): adapts durable Task/TaskEvent rows to the UI's task-snapshot shape
(module/label/phase/logs/llm metrics) over one SSE connection carrying many
tasks. The legacy ``/api/tasks`` adapter that once shared this module is
retired (5a) — this is the only copy. Frame contract (all frames are
``data:`` lines, no ``event:`` field):

- ``{"type": "snapshot_all", "tasks": [...]}``` — replayed once on connect
- ``{"type": "progress"|"phase"|"log"|"llm_rate"|"llm_chars"|"segments", "task_id": ...}``
- ``{"type": "status", "status": ..., "task": <snapshot>, "task_id": ...}`` — terminal /
  lifecycle transitions (the snapshot already carries the mapped status)
- ``{"type": "ping"}`` — idle keepalive (the stream never ends on its own)
"""
from __future__ import annotations

import asyncio
import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.database import SessionLocal
from ..platform.models import Task as DurableTask
from ..platform.models import TaskEvent
from ..platform.security import session_is_valid_for_user
from .task_operations import task_module


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def epoch(value) -> float:
    return value.timestamp() if value is not None else 0.0


def legacy_status(status: str) -> str:
    """Durable status -> the legacy UI's status vocabulary (queued/retrying read
    as pending, cancelling as running) — the UI renders the old shapes."""
    if status in {"queued", "retrying"}:
        return "pending"
    if status == "cancelling":
        return "running"
    return status


def durable_label(task: DurableTask) -> str:
    payload = task.payload if isinstance(task.payload, dict) else {}
    if payload.get("label"):
        return str(payload["label"])
    source = str(payload.get("source_name") or payload.get("output_name") or "")
    if task.task_type == "script.parse":
        return f"文本解析（{source or '文件'}）"
    if task.task_type == "audio.silences":
        return f"停顿检测：{source or '音频'}"
    if task.task_type == "audio.cut":
        return f"音频分集：{source or '音频'}"
    return f"持久化任务：{task.task_type}"


def task_events(db: Session, task_id: str) -> list[TaskEvent]:
    """The task's recent events in ascending sequence (cap 1000 — enough for
    the live-log panel; the tail is what snapshots and the stream consume)."""
    latest = db.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.sequence.desc())
        .limit(1000)
    ).all()
    return list(reversed(latest))


def task_snapshot(db: Session, task: DurableTask) -> dict:
    events = task_events(db, task.id)
    logs = []
    current = ""
    phase = ""
    metrics: dict[str, dict] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.event_type == "progress":
            current = str(payload.get("current") or current)
        elif event.event_type == "phase":
            phase = str(payload.get("phase") or phase)
        elif event.event_type == "log":
            logs.append(
                {
                    "level": str(payload.get("level") or "INFO"),
                    "msg": str(payload.get("msg") or ""),
                    "t": epoch(event.created_at),
                }
            )
        elif event.event_type in {"llm_rate", "llm_chars", "segments"}:
            metrics[event.event_type] = payload
    for metric_type in ("llm_rate", "llm_chars", "segments"):
        if metric_type not in metrics:
            latest_metric = db.scalar(
                select(TaskEvent)
                .where(TaskEvent.task_id == task.id, TaskEvent.event_type == metric_type)
                .order_by(TaskEvent.sequence.desc())
                .limit(1)
            )
            if latest_metric is not None and isinstance(latest_metric.payload, dict):
                metrics[metric_type] = latest_metric.payload
    if not phase:
        last_phase = db.scalar(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.event_type == "phase")
            .order_by(TaskEvent.sequence.desc())
            .limit(1)
        )
        if last_phase is not None:
            phase = str((last_phase.payload or {}).get("phase") or "")
    if not current:
        last_progress = db.scalar(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.event_type == "progress")
            .order_by(TaskEvent.sequence.desc())
            .limit(1)
        )
        if last_progress is not None:
            current = str((last_progress.payload or {}).get("current") or "")
    result = task.result.result if task.result is not None else {}
    return {
        "id": task.id,
        "module": task_module(task.task_type),
        "label": durable_label(task),
        "seq": int(task.created_at.timestamp() * 1000),
        "status": legacy_status(task.status),
        "phase": phase,
        "progress": max(0.0, min(1.0, task.progress / 100.0)),
        "current": current,
        "logs": logs,
        "llm_stream": "",
        "llm_cps": float(metrics.get("llm_rate", {}).get("cps") or 0),
        "llm_cps_10s": float(metrics.get("llm_rate", {}).get("cps10") or 0),
        "llm_chars": int(metrics.get("llm_chars", {}).get("chars") or 0),
        "llm_secs": float(metrics.get("llm_chars", {}).get("secs") or 0),
        "seg_done": int(metrics.get("segments", {}).get("done") or 0),
        "seg_total": int(metrics.get("segments", {}).get("total") or 0),
        "seg_chars_done": int(metrics.get("segments", {}).get("chars_done") or 0),
        "seg_chars_total": int(metrics.get("segments", {}).get("chars_total") or 0),
        "result": result,
        "error": task.error_message or "",
        "created": epoch(task.created_at),
        "started": epoch(task.started_at),
        "finished": epoch(task.finished_at),
    }


def event_frame(db: Session, task: DurableTask, event: TaskEvent) -> dict | None:
    """One durable event -> the legacy UI frame (``task_id`` already embedded),
    or ``None`` for event types the UI does not render."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "progress":
        return {
            "type": "progress",
            "task_id": task.id,
            "progress": task.progress / 100.0,
            "current": payload.get("current") or "",
        }
    if event.event_type == "phase":
        return {"type": "phase", "task_id": task.id, "phase": payload.get("phase") or ""}
    if event.event_type == "log":
        return {
            "type": "log",
            "task_id": task.id,
            "level": payload.get("level") or "INFO",
            "msg": payload.get("msg") or "",
            "t": epoch(event.created_at),
        }
    if event.event_type in {"llm_rate", "llm_chars", "segments"}:
        return {"type": event.event_type, "task_id": task.id, **payload}
    if event.event_type in {"succeeded", "failed", "cancelled"}:
        status = legacy_status("cancelled" if event.event_type == "cancelled" else event.event_type)
        return {"type": "status", "status": status, "task_id": task.id, "task": task_snapshot(db, task)}
    if event.event_type in {
        "submitted", "cancel_requested", "admin_cancel_requested",
        "retry_requested", "admin_retry_requested",
        "retry_scheduled", "attempt_expired", "dispatch_recovered", "attempt_started",
    }:
        if event.event_type != "submitted":
            return {
                "type": "status",
                "status": legacy_status(task.status),
                "task_id": task.id,
                "task": task_snapshot(db, task),
            }
        return {"type": "snapshot", "task_id": task.id, "task": task_snapshot(db, task)}
    return None


def _new_frames(rows_fn, seen: dict[str, int]) -> list[dict]:
    """All not-yet-seen frames for the rows ``rows_fn`` returns, in row order.
    Rows that disappeared from the view are pruned from ``seen``."""
    emitted: list[dict] = []
    with SessionLocal() as db:
        rows = rows_fn(db)
        current_ids = {row.id for row in rows}
        for task in rows:
            events = db.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == task.id, TaskEvent.sequence > seen.get(task.id, 0))
                .order_by(TaskEvent.sequence)
                .limit(1000)
            ).all()
            for event in events:
                seen[task.id] = max(seen.get(task.id, 0), event.sequence)
                frame = event_frame(db, task, event)
                if frame is not None:
                    emitted.append(frame)
        for task_id in list(seen):
            if task_id not in current_ids:
                seen.pop(task_id, None)
    return emitted


async def aggregate_stream(rows_fn, auth_token: str, user_id: str, is_disconnected):
    """The shared aggregate SSE body: replay ``snapshot_all`` for every row
    ``rows_fn(db)`` returns (the caller scopes it — current project for the
    legacy surface, the whole user for v1), then stream each row's new events
    over the one connection, re-checking the session every 5 s. Never ends on
    its own; pings keep the connection warm. Async generator on purpose:
    Starlette runs sync generators by borrowing a thread-pool worker for EVERY
    iteration, so the 0.5 s poll would hold a worker per idle connection
    (a handful of open tabs exhausts the pool); an async generator sleeps on
    the event loop instead.
    """
    seen: dict[str, int] = {}
    next_auth_check = 0.0
    with SessionLocal() as db:
        rows = rows_fn(db)
        for task in rows:
            events = task_events(db, task.id)
            seen[task.id] = events[-1].sequence if events else 0
        yield sse({"type": "snapshot_all", "tasks": [task_snapshot(db, t) for t in rows]})
    while True:
        if await is_disconnected():
            return
        now = time.monotonic()
        if now >= next_auth_check:
            with SessionLocal() as auth_db:
                if not session_is_valid_for_user(auth_db, auth_token, user_id):
                    return
            next_auth_check = now + 5.0
        frames = _new_frames(rows_fn, seen)
        for frame in frames:
            yield sse(frame)
        if not frames:
            yield sse({"type": "ping"})
        await asyncio.sleep(0.5)
