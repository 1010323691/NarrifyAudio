"""Shared HTTP adapters for durable task submission and retry operations."""
from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..platform.models import User
from ..platform.task_submission import (
    TaskSubmissionError,
    submit_task_record,
    task_dict,
)
from ..services.task_operations import (
    RetryNotAllowedError,
    cancel_task_record,
    check_retry_eligible,
    control_task_category,
    owned_task,
    requeue_task_record,
)


class TaskSubmit(BaseModel):
    project_id: str
    task_type: str = Field(min_length=1, max_length=80)
    payload: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=180)


class TaskBatchControl(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    category: str = Field(min_length=1, max_length=40)
    action: Literal["pause", "resume", "cancel"]


def submit_task(payload: TaskSubmit, *, user: User, db: Session) -> dict:
    """Submit a durable task and translate service errors to HTTP responses."""
    try:
        task = submit_task_record(
            db, user, project_id=payload.project_id, task_type=payload.task_type,
            payload=payload.payload,
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


def batch_control_tasks(payload: TaskBatchControl, *, user: User, db: Session) -> dict:
    try:
        tasks = control_task_category(db, user.id, payload.project_id, payload.category, payload.action)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return {"changed": len(tasks), "tasks": [task_dict(task) for task in tasks]}
