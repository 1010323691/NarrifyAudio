from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_csrf, require_user
from ..platform.models import OutboxEvent, Project, QuotaReservation, QuotaTransaction, Task, TaskEvent, User, UserQuotaAccount, new_id, utcnow

router = APIRouter(prefix="/api/v1/tasks", tags=["persistent-tasks"])


class TaskSubmit(BaseModel):
    project_id: str
    task_type: str = Field(min_length=1, max_length=80)
    payload: dict = Field(default_factory=dict)
    estimated_units: int = Field(default=0, ge=0, le=10_000_000)
    idempotency_key: str = Field(min_length=8, max_length=180)


def _task_json(task: Task) -> dict:
    return {"id": task.id, "project_id": task.project_id, "task_type": task.task_type, "status": task.status, "progress": task.progress, "error_code": task.error_code, "error_message": task.error_message, "created_at": task.created_at.isoformat(), "updated_at": task.updated_at.isoformat()}


@router.get("")
def list_tasks(user: User = Depends(require_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Task).where(Task.owner_id == user.id).order_by(Task.created_at.desc()).limit(200)).all()
    return [_task_json(row) for row in rows]


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
    task = Task(owner_id=user.id, project_id=project.id, task_type=payload.task_type, payload={**payload.payload, "_request_hash": request_hash}, idempotency_key=payload.idempotency_key)
    db.add(task)
    db.flush()
    if payload.estimated_units:
        account.available_units -= payload.estimated_units
        account.reserved_units += payload.estimated_units
        db.add(QuotaReservation(user_id=user.id, task_id=task.id, units=payload.estimated_units))
        db.add(QuotaTransaction(user_id=user.id, task_id=task.id, amount=-payload.estimated_units, kind="reserve", idempotency_key=f"reserve:{task.id}"))
    db.add(TaskEvent(task_id=task.id, sequence=1, event_type="submitted", payload={"status": "pending"}))
    db.add(OutboxEvent(aggregate_type="task", aggregate_id=task.id, event_type="task.submitted", payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id}))
    db.commit()
    return _task_json(task)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id))
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status in {"succeeded", "failed", "cancelled", "timeout"}:
        return _task_json(task)
    task.status = "cancelling" if task.status in {"running", "queued"} else "cancelled"
    task.updated_at = utcnow()
    db.add(TaskEvent(task_id=task.id, sequence=len(task.events) + 1, event_type="cancel_requested", payload={"status": task.status}))
    db.commit()
    return _task_json(task)

