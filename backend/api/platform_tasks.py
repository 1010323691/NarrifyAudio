from __future__ import annotations

import json
import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..platform.database import SessionLocal, get_db
from ..platform.deps import require_csrf, require_user
from ..platform.models import OutboxEvent, QuotaReservation, QuotaTransaction, Task, TaskAttempt, TaskEvent, User, utcnow
from ..platform.config import settings
from ..platform.task_state import TERMINAL_TASK_STATUSES, append_task_event, release_reservation, suppress_pending_dispatch
from ..services.tasks import TaskSubmissionError, submit_task_record, task_dict

router = APIRouter(prefix="/api/v1/tasks", tags=["persistent-tasks"])


class TaskSubmit(BaseModel):
    project_id: str
    task_type: str = Field(min_length=1, max_length=80)
    payload: dict = Field(default_factory=dict)
    estimated_units: int = Field(default=0, ge=0, le=10_000_000)
    idempotency_key: str = Field(min_length=8, max_length=180)


def _task_json(task: Task) -> dict:
    return task_dict(task)


@router.get("")
def list_tasks(user: User = Depends(require_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Task).where(Task.owner_id == user.id).order_by(Task.created_at.desc()).limit(200)).all()
    return [_task_json(row) for row in rows]


@router.get("/{task_id}")
def get_task(task_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id))
    if task is None:
        raise HTTPException(404, "任务不存在")
    return _task_json(task)


@router.get("/{task_id}/events")
def stream_task_events(
    task_id: str,
    request: Request,
    user: User = Depends(require_user),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    try:
        after = max(0, int(last_event_id or "0"))
    except ValueError:
        after = 0

    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id))
    if task is None:
        raise HTTPException(404, "任务不存在")

    async def generate():
        sequence = after
        while True:
            if await request.is_disconnected():
                return
            with SessionLocal() as db:
                current = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id))
                if current is None:
                    return
                events = db.scalars(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id, TaskEvent.sequence > sequence)
                    .order_by(TaskEvent.sequence)
                    .limit(100)
                ).all()
                terminal = current.status in TERMINAL_TASK_STATUSES
            for event in events:
                sequence = event.sequence
                yield f"id: {sequence}\ndata: {json.dumps({'type': event.event_type, 'task_id': task_id, 'sequence': sequence, 'payload': event.payload}, ensure_ascii=False)}\n\n"
            if terminal and not events:
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("", status_code=201)
def submit_task(payload: TaskSubmit, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    try:
        task = submit_task_record(
            db, user, project_id=payload.project_id, task_type=payload.task_type,
            payload=payload.payload, estimated_units=payload.estimated_units,
            idempotency_key=payload.idempotency_key,
        )
    except TaskSubmissionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    return _task_json(task)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id).with_for_update())
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status in TERMINAL_TASK_STATUSES:
        return _task_json(task)
    if task.status in {"running", "cancelling"}:
        task.status = "cancelling"
    else:
        task.status = "cancelled"
        release_reservation(db, task, kind="release", note="user cancelled before execution")
        suppress_pending_dispatch(db, task.id)
    task.updated_at = utcnow()
    append_task_event(db, task.id, "cancel_requested", {"status": task.status})
    db.commit()
    return _task_json(task)


@router.post("/{task_id}/retry")
def retry_task(task_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    """Requeue a failed zero-cost task without losing its audit history."""
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id).with_for_update())
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status not in {"failed", "cancelled", "timeout"}:
        raise HTTPException(409, "任务当前不可重试")
    reservation = db.scalar(select(QuotaReservation).where(QuotaReservation.task_id == task.id))
    if reservation is not None and reservation.units:
        raise HTTPException(409, "带额度预留的任务暂不支持原任务重试")
    metered = db.scalar(select(QuotaTransaction.id).where(
        QuotaTransaction.task_id == task.id,
        QuotaTransaction.resource_type.in_(["LLM", "TTS"]),
        QuotaTransaction.kind == "consume",
    ).limit(1))
    if metered:
        raise HTTPException(409, "已有模型消费的任务请重新提交，以创建新的计费操作")
    attempts = db.scalar(select(func.count()).select_from(TaskAttempt).where(TaskAttempt.task_id == task.id)) or 0
    if attempts >= settings.task_max_attempts:
        raise HTTPException(409, "任务已达到最大尝试次数")
    if task.result is not None:
        db.delete(task.result)
    task.status = "pending"
    task.progress = 0
    task.error_code = ""
    task.error_message = ""
    task.started_at = None
    task.finished_at = None
    task.updated_at = utcnow()
    append_task_event(db, task.id, "retry_requested", {"status": "pending"})
    db.add(
        OutboxEvent(
            aggregate_type="task",
            aggregate_id=task.id,
            event_type="task.submitted",
            payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id},
        )
    )
    db.commit()
    return _task_json(task)
