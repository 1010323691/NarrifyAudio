"""Unified task endpoints for legacy pages and PostgreSQL-backed tasks.

The UI still uses the original task snapshot/SSE shape. This module adapts durable
Task/TaskEvent rows to that response contract while keeping ownership checks in the
database, so tasks remain visible across API and Worker restarts.
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ..platform.database import SessionLocal
from ..platform.config import settings
from ..platform.deps import AuthContext, get_auth_context
from ..platform.project_context import active_project
from ..platform.models import Task as DurableTask, TaskEvent
from ..platform.security import session_is_valid_for_user
from ..platform.task_lifecycle import TERMINAL_TASK_STATUSES
from ..services.tasks import cancel_task_record
from .task_submission import retry_task as retry_durable_task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _epoch(value) -> float:
    return value.timestamp() if value is not None else 0.0


def _legacy_status(status: str) -> str:
    if status in {"queued", "retrying"}:
        return "pending"
    if status == "cancelling":
        return "running"
    return status


def _durable_module(task_type: str) -> str:
    return {
        "voices.foundation": "voices-foundation",
        "voices.clone": "voices-clone",
        "tts.batch": "tts-batch",
        "tts.merge": "merge",
        "bgm.analysis": "bgm-analysis",
        "bgm.segment": "bgm-segment",
        "bgm.mix": "bgm-mix",
        "music.suggest_tags": "music-ai-tags",
    }.get(task_type, task_type.split(".", 1)[0])


def _durable_label(task: DurableTask) -> str:
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


def _durable_events(db, task_id: str) -> list[TaskEvent]:
    latest = db.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.sequence.desc())
        .limit(1000)
    ).all()
    return list(reversed(latest))


def _durable_snapshot(db, task: DurableTask) -> dict:
    events = _durable_events(db, task.id)
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
                    "t": _epoch(event.created_at),
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
        "module": _durable_module(task.task_type),
        "label": _durable_label(task),
        "seq": int(task.created_at.timestamp() * 1000),
        "status": _legacy_status(task.status),
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
        "created": _epoch(task.created_at),
        "started": _epoch(task.started_at),
        "finished": _epoch(task.finished_at),
    }


def _current_project_id(db, ctx: AuthContext) -> str | None:
    project = active_project(db, ctx.user, ctx.session)
    return project.id if project is not None else None


def _durable_rows(db, ctx: AuthContext) -> list[DurableTask]:
    project_id = _current_project_id(db, ctx)
    if project_id is None:
        return []
    return db.scalars(
        select(DurableTask)
        .where(DurableTask.owner_id == ctx.user.id, DurableTask.project_id == project_id)
        .order_by(DurableTask.created_at.desc())
        .limit(200)
    ).all()


def _project_task_snapshots(db, ctx: AuthContext) -> list[dict]:
    return [_durable_snapshot(db, task) for task in _durable_rows(db, ctx)]


def _durable_owned(
    db, ctx: AuthContext, task_id: str, *, lock: bool = False,
) -> DurableTask | None:
    project_id = _current_project_id(db, ctx)
    if project_id is None:
        return None
    statement = select(DurableTask).where(
        DurableTask.id == task_id,
        DurableTask.owner_id == ctx.user.id,
        DurableTask.project_id == project_id,
    )
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _durable_event_payload(db, task: DurableTask, event: TaskEvent) -> dict | None:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "progress":
        return {
            "type": "progress",
            "progress": task.progress / 100.0,
            "current": payload.get("current") or "",
        }
    if event.event_type == "phase":
        return {"type": "phase", "phase": payload.get("phase") or ""}
    if event.event_type == "log":
        return {
            "type": "log",
            "level": payload.get("level") or "INFO",
            "msg": payload.get("msg") or "",
            "t": _epoch(event.created_at),
        }
    if event.event_type in {"llm_rate", "llm_chars", "segments"}:
        return {"type": event.event_type, **payload}
    if event.event_type in {"succeeded", "failed", "cancelled"}:
        status = _legacy_status("cancelled" if event.event_type == "cancelled" else event.event_type)
        return {"type": "status", "status": status, "task": _durable_snapshot(db, task)}
    if event.event_type in {
        "submitted", "cancel_requested", "admin_cancel_requested",
        "retry_requested", "admin_retry_requested",
        "retry_scheduled", "attempt_expired", "dispatch_recovered", "attempt_started",
    }:
        if event.event_type != "submitted":
            return {
                "type": "status",
                "status": _legacy_status(task.status),
                "task": _durable_snapshot(db, task),
            }
        return {"type": "snapshot", "task": _durable_snapshot(db, task)}
    return None


def _persistent_events(ctx: AuthContext, seen: dict[str, int]) -> list[tuple[str, dict]]:
    emitted: list[tuple[str, dict]] = []
    with SessionLocal() as db:
        rows = _durable_rows(db, ctx)
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
                mapped = _durable_event_payload(db, task, event)
                if mapped is not None:
                    emitted.append((task.id, mapped))
        for task_id in list(seen):
            if task_id not in current_ids:
                seen.pop(task_id, None)
    return emitted


@router.get("")
def list_tasks(ctx: AuthContext = Depends(get_auth_context)) -> list[dict]:
    with SessionLocal() as db:
        return _project_task_snapshots(db, ctx)


@router.get("/stream")
def stream_all_tasks(request: Request, ctx: AuthContext = Depends(get_auth_context)):
    """Stream snapshots and lifecycle events for the current project's tasks."""

    async def gen():
        # Async generator on purpose: Starlette runs sync generators by
        # borrowing a thread-pool worker for EVERY iteration, so the 0.5 s
        # poll below would hold a worker per idle connection (a handful of
        # open tabs exhausts the pool). An async generator sleeps on the
        # event loop instead — no worker is occupied while waiting.
        seen: dict[str, int] = {}
        token = request.cookies.get(settings.session_cookie)
        next_auth_check = 0.0
        with SessionLocal() as db:
            for task in _durable_rows(db, ctx):
                events = _durable_events(db, task.id)
                seen[task.id] = events[-1].sequence if events else 0
            yield _sse({"type": "snapshot_all", "tasks": _project_task_snapshots(db, ctx)})
        while True:
            now = time.monotonic()
            if now >= next_auth_check:
                with SessionLocal() as auth_db:
                    if not session_is_valid_for_user(auth_db, token, ctx.user.id):
                        return
                next_auth_check = now + 5.0
            events = _persistent_events(ctx, seen)
            for task_id, event in events:
                event["task_id"] = task_id
                yield _sse(event)
            if not events:
                yield _sse({"type": "ping"})
            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{task_id}")
def get_task(task_id: str, ctx: AuthContext = Depends(get_auth_context)) -> dict:
    with SessionLocal() as db:
        durable = _durable_owned(db, ctx, task_id)
        if durable is None:
            raise HTTPException(404, "任务不存在")
        return _durable_snapshot(db, durable)


@router.post("/{task_id}/{action}")
def control_task(task_id: str, action: str, ctx: AuthContext = Depends(get_auth_context)) -> dict:
    if action == "retry":
        with SessionLocal() as db:
            durable = _durable_owned(db, ctx, task_id, lock=True)
            if durable is None:
                raise HTTPException(404, "任务不存在")
            retry_durable_task(task_id, user=ctx.user, db=db)
            return _durable_snapshot(db, durable)
    if action != "cancel":
        raise HTTPException(400, "持久化任务当前只支持取消或重试")
    with SessionLocal() as db:
        durable = _durable_owned(db, ctx, task_id, lock=True)
        if durable is None:
            raise HTTPException(404, "任务不存在")
        if durable.status not in TERMINAL_TASK_STATUSES:
            cancel_task_record(db, durable)
            db.commit()
        return _durable_snapshot(db, durable)
