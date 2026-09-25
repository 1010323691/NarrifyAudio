"""Shared HTTP adapters for durable task submission and retry operations."""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..platform.config import settings
from ..platform.models import QuotaReservation, QuotaTransaction, Task, TaskAttempt, User
from ..services.tasks import (
    TaskSubmissionError,
    cancel_task_record,
    requeue_task_record,
    submit_task_record,
    task_dict,
)


class TaskSubmit(BaseModel):
    project_id: str
    task_type: str = Field(min_length=1, max_length=80)
    payload: dict = Field(default_factory=dict)
    estimated_units: int = Field(default=0, ge=0, le=10_000_000)
    idempotency_key: str = Field(min_length=8, max_length=180)


def submit_task(payload: TaskSubmit, *, user: User, db: Session) -> dict:
    """Submit a durable task and translate service errors to HTTP responses."""
    try:
        task = submit_task_record(
            db, user, project_id=payload.project_id, task_type=payload.task_type,
            payload=payload.payload, estimated_units=payload.estimated_units,
            idempotency_key=payload.idempotency_key,
        )
    except TaskSubmissionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    return task_dict(task)


def cancel_task(task_id: str, *, user: User, db: Session) -> dict:
    """Cancel an owned task and return its durable-task representation."""
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id).with_for_update())
    if task is None:
        raise HTTPException(404, "任务不存在")
    cancel_task_record(db, task)
    db.commit()
    return task_dict(task)


def retry_task(task_id: str, *, user: User, db: Session) -> dict:
    """Retry an eligible zero-cost task without depending on a route module."""
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
    requeue_task_record(db, task, event_type="retry_requested", event_payload={"status": "pending"})
    db.commit()
    return task_dict(task)
