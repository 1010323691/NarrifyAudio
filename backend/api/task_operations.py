"""Shared HTTP adapters for durable task submission and retry operations."""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..platform.models import User
from ..services.task_operations import RetryNotAllowedError, check_retry_eligible, owned_task
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
    task = owned_task(db, user.id, task_id, lock=True)
    if task is None:
        raise HTTPException(404, "任务不存在")
    cancel_task_record(db, task)
    db.commit()
    return task_dict(task)


def retry_task(task_id: str, *, user: User, db: Session) -> dict:
    """Retry an eligible zero-cost task without depending on a route module."""
    task = owned_task(db, user.id, task_id, lock=True)
    if task is None:
        raise HTTPException(404, "任务不存在")
    try:
        check_retry_eligible(db, task)
    except RetryNotAllowedError as exc:
        raise HTTPException(409, exc.message) from exc
    requeue_task_record(db, task, event_type="retry_requested", event_payload={"status": "pending"})
    db.commit()
    return task_dict(task)
