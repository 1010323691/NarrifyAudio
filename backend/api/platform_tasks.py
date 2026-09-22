from __future__ import annotations

import hashlib
import json
import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.database import SessionLocal, get_db
from ..platform.deps import require_csrf, require_user
from ..platform.models import OutboxEvent, Project, QuotaReservation, QuotaTransaction, Task, TaskEvent, TaskResult, User, UserQuotaAccount, utcnow
from ..platform.task_state import TERMINAL_TASK_STATUSES, append_task_event, release_reservation

router = APIRouter(prefix="/api/v1/tasks", tags=["persistent-tasks"])


class TaskSubmit(BaseModel):
    project_id: str
    task_type: str = Field(min_length=1, max_length=80)
    payload: dict = Field(default_factory=dict)
    estimated_units: int = Field(default=0, ge=0, le=10_000_000)
    idempotency_key: str = Field(min_length=8, max_length=180)


def _task_json(task: Task) -> dict:
    result = task.result.result if task.result is not None else None
    return {"id": task.id, "project_id": task.project_id, "task_type": task.task_type, "status": task.status, "progress": task.progress, "error_code": task.error_code, "error_message": task.error_message, "result": result, "created_at": task.created_at.isoformat(), "updated_at": task.updated_at.isoformat()}


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
    project = db.scalar(select(Project).where(Project.id == payload.project_id, Project.owner_id == user.id, Project.deleted_at.is_(None)))
    if project is None:
        raise HTTPException(404, "项目不存在")
    request_hash = hashlib.sha256(json.dumps(payload.model_dump(exclude={"idempotency_key"}), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    existing = db.scalar(select(Task).where(Task.owner_id == user.id, Task.idempotency_key == payload.idempotency_key))
    if existing is not None:
        if existing.payload.get("_request_hash") != request_hash:
            raise HTTPException(409, "幂等键对应的请求内容不同")
        return _task_json(existing)
    account = db.scalar(select(UserQuotaAccount).where(UserQuotaAccount.user_id == user.id).with_for_update())
    if account is None:
        account = UserQuotaAccount(user_id=user.id, available_units=0)
        db.add(account)
        db.flush()
    if account.available_units < payload.estimated_units:
        raise HTTPException(409, "额度不足")
    # The account row lock serializes submissions for one user. Re-check the
    # idempotency key after acquiring it so concurrent identical requests do
    # not race into the unique constraint.
    existing = db.scalar(select(Task).where(Task.owner_id == user.id, Task.idempotency_key == payload.idempotency_key))
    if existing is not None:
        if existing.payload.get("_request_hash") != request_hash:
            raise HTTPException(409, "幂等键对应的请求内容不同")
        db.rollback()
        return _task_json(existing)
    task = Task(owner_id=user.id, project_id=project.id, task_type=payload.task_type, payload={**payload.payload, "_request_hash": request_hash}, idempotency_key=payload.idempotency_key)
    db.add(task)
    db.flush()
    if payload.estimated_units:
        before_available = account.available_units
        before_reserved = account.reserved_units
        before_consumed = account.consumed_units
        account.available_units -= payload.estimated_units
        account.reserved_units += payload.estimated_units
        reservation = QuotaReservation(user_id=user.id, task_id=task.id, units=payload.estimated_units)
        db.add(reservation)
        db.flush()
        db.add(
            QuotaTransaction(
                user_id=user.id,
                task_id=task.id,
                reservation_id=reservation.id,
                amount=-payload.estimated_units,
                kind="reserve",
                idempotency_key=f"reserve:{task.id}",
                available_before=before_available,
                available_after=account.available_units,
                reserved_before=before_reserved,
                reserved_after=account.reserved_units,
                consumed_before=before_consumed,
                consumed_after=account.consumed_units,
            )
        )
    append_task_event(db, task.id, "submitted", {"status": "pending", "estimated_units": payload.estimated_units})
    db.add(OutboxEvent(aggregate_type="task", aggregate_id=task.id, event_type="task.submitted", payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id}))
    db.commit()
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
    task.updated_at = utcnow()
    append_task_event(db, task.id, "cancel_requested", {"status": task.status})
    db.commit()
    return _task_json(task)
