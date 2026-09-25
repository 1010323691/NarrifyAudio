"""Unified task endpoints for legacy pages and PostgreSQL-backed tasks.

The original UI consumes the in-process task snapshot/SSE contract. During the
worker migration durable tasks must remain visible through that same contract,
otherwise a page appears idle after its work moves to Redis. This module adapts
durable Task/TaskEvent rows to the legacy snapshot shape while keeping ownership
checks in the database.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ..platform.database import SessionLocal
from ..platform.deps import AuthContext, get_auth_context
from ..platform.legacy_workspace import active_workspace
from ..platform.models import Task as DurableTask, TaskEvent, utcnow
from ..platform.task_state import TERMINAL_TASK_STATUSES, append_task_event, release_reservation, suppress_pending_dispatch
from .platform_tasks import retry_task as retry_durable_task

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
    workspace = active_workspace(db, ctx.user, ctx.session)
    return workspace.id if workspace is not None else None


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


def _combined_snapshot(db, ctx: AuthContext) -> list[dict]:
    return [_durable_snapshot(db, task) for task in _durable_rows(db, ctx)]


def _durable_owned(db, ctx: AuthContext, task_id: str) -> DurableTask | None:
    project_id = _current_project_id(db, ctx)
    if project_id is None:
        return None
    return db.scalar(
        select(DurableTask).where(
            DurableTask.id == task_id,
            DurableTask.owner_id == ctx.user.id,
            DurableTask.project_id == project_id,
        ).with_for_update()
    )


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
    if event.event_type == "submitted":
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
        return _combined_snapshot(db, ctx)


@router.get("/stream")
def stream_all_tasks(ctx: AuthContext = Depends(get_auth_context)):
    """Stream snapshots and lifecycle events for the current project's tasks."""

    def gen():
        seen: dict[str, int] = {}
        with SessionLocal() as db:
            for task in _durable_rows(db, ctx):
                events = _durable_events(db, task.id)
                seen[task.id] = events[-1].sequence if events else 0
            yield _sse({"type": "snapshot_all", "tasks": _combined_snapshot(db, ctx)})
        while True:
            events = _persistent_events(ctx, seen)
            for task_id, event in events:
                event["task_id"] = task_id
                yield _sse(event)
            if not events:
                yield _sse({"type": "ping"})
            time.sleep(0.5)

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
            return retry_durable_task(task_id, user=ctx.user, db=db)
    if action != "cancel":
        raise HTTPException(400, "持久化任务当前只支持取消或重试")
    with SessionLocal() as db:
        durable = _durable_owned(db, ctx, task_id)
        if durable is None:
            raise HTTPException(404, "任务不存在")
        if durable.status not in TERMINAL_TASK_STATUSES:
            now = utcnow()
            durable.status = "cancelling" if durable.status in {"running", "queued"} else "cancelled"
            if durable.status == "cancelled":
                durable.finished_at = now
                release_reservation(db, durable, kind="release", note="user cancelled before execution")
                suppress_pending_dispatch(db, durable.id)
            durable.updated_at = now
            append_task_event(db, durable.id, "cancel_requested", {"status": durable.status})
            db.commit()
        return _durable_snapshot(db, durable)


@router.get("/{task_id}/stream")
def stream_task(task_id: str, ctx: AuthContext = Depends(get_auth_context)):
    with SessionLocal() as db:
        durable = _durable_owned(db, ctx, task_id)
        if durable is None:
            raise HTTPException(404, "任务不存在")
        initial = _durable_snapshot(db, durable)
        events = _durable_events(db, task_id)
        sequence = events[-1].sequence if events else 0

    def durable_gen():
        nonlocal sequence
        yield _sse({"type": "snapshot", "task": initial})
        while True:
            with SessionLocal() as current_db:
                current = _durable_owned(current_db, ctx, task_id)
                if current is None:
                    return
                events = current_db.scalars(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id, TaskEvent.sequence > sequence)
                    .order_by(TaskEvent.sequence)
                    .limit(100)
                ).all()
                terminal = current.status in TERMINAL_TASK_STATUSES
                for event in events:
                    sequence = event.sequence
                    mapped = _durable_event_payload(current_db, current, event)
                    if mapped is not None:
                        yield _sse(mapped)
                if terminal and not events:
                    return
            if not events:
                yield _sse({"type": "ping"})
            time.sleep(0.5)

    return StreamingResponse(
        durable_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
