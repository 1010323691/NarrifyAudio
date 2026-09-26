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

import anyio
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
    project = getattr(task, "project", None)
    return {
        "id": task.id,
        "project_id": getattr(task, "project_id", ""),
        "project_name": getattr(project, "name", None) or "已删除项目",
        "task_type": task.task_type,
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
        "error_code": getattr(task, "error_code", "") or "",
        "created": epoch(task.created_at),
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat() if getattr(task, "updated_at", None) else "",
        "started": epoch(task.started_at),
        "finished": epoch(task.finished_at),
    }


def task_center_item(db: Session, task: DurableTask, progress_payload: dict | None = None) -> dict:
    """Compact history row for the cross-project task center (no log payloads)."""
    progress_payload = progress_payload if isinstance(progress_payload, dict) else {}
    project = getattr(task, "project", None)
    return {
        "id": task.id,
        "project_id": getattr(task, "project_id", ""),
        "project_name": getattr(project, "name", None) or "已删除项目",
        "task_type": task.task_type,
        "label": durable_label(task),
        "status": legacy_status(task.status),
        "progress": max(0.0, min(1.0, task.progress / 100.0)),
        "current": str(progress_payload.get("current") or ""),
        "error": task.error_message or "",
        "error_code": getattr(task, "error_code", "") or "",
        "created": epoch(task.created_at),
        "created_at": task.created_at.isoformat(),
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
        "llm_unavailable",
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


def snapshot_payload(rows_fn) -> tuple[list[dict], dict[str, int]]:
    """Initial replay on one self-opened session: the ``snapshot_all`` task
    snapshots and the per-task seen-sequence map the poll ticks resume from."""
    seen: dict[str, int] = {}
    with SessionLocal() as db:
        rows = rows_fn(db)
        for task in rows:
            events = task_events(db, task.id)
            seen[task.id] = events[-1].sequence if events else 0
        return [task_snapshot(db, t) for t in rows], seen


def session_still_valid(auth_token: str, user_id: str) -> bool:
    with SessionLocal() as db:
        return session_is_valid_for_user(db, auth_token, user_id)


async def aggregate_stream(rows_fn, auth_token: str, user_id: str, is_disconnected):
    """The shared aggregate SSE body: replay ``snapshot_all`` for every row
    ``rows_fn(db)`` returns (the caller scopes it — current project for the
    legacy surface, the whole user for v1), then stream each row's new events
    over the one connection, re-checking the session every 5 s. Never ends on
    its own; pings keep the connection warm. Async generator on purpose:
    Starlette runs sync generators by borrowing a thread-pool worker for EVERY
    iteration, so the 0.5 s poll would hold a worker per idle connection
    (a handful of open tabs exhausts the pool); an async generator sleeps on
    the event loop instead. The synchronous DB units (snapshot, auth check,
    poll) are handed to the pool via ``anyio.to_thread`` so a slow query
    cannot freeze the event loop for every other connection on it.
    """
    next_auth_check = 0.0
    snapshots, seen = await anyio.to_thread.run_sync(snapshot_payload, rows_fn)
    yield sse({"type": "snapshot_all", "tasks": snapshots})
    while True:
        if await is_disconnected():
            return
        now = time.monotonic()
        if now >= next_auth_check:
            if not await anyio.to_thread.run_sync(session_still_valid, auth_token, user_id):
                return
            next_auth_check = now + 5.0
        frames = await anyio.to_thread.run_sync(_new_frames, rows_fn, seen)
        for frame in frames:
            yield sse(frame)
        if not frames:
            yield sse({"type": "ping"})
        await asyncio.sleep(0.5)
