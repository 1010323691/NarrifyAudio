"""Submit legacy engine calls through the durable task boundary.

The original pages still use the legacy task labels and response shapes.  This
adapter gives those pages a PostgreSQL-backed task id while keeping the engine
arguments as plain JSON so a Worker can reconstruct the call after an API
restart.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .task_submission import TaskSubmissionError, submit_task_record, task_dict
from .deps import AuthContext
from .project_context import active_project
from .models import Task
from .task_lifecycle import ACTIVE_TASK_STATUSES


def submit_legacy_engine_task(
    *,
    task_type: str,
    label: str,
    payload: dict,
    ctx: AuthContext,
    db: Session,
    idempotency_prefix: str,
    commit: bool = True,
) -> dict:
    """Create one durable task owned by the caller's active workspace."""
    project = active_project(db, ctx.user, ctx.session)
    if project is None:
        raise HTTPException(409, "尚未设置工作空间")
    try:
        task = submit_task_record(
            db, ctx.user, project_id=project.id, task_type=task_type,
            payload={"label": label, **payload},
            idempotency_key=f"{idempotency_prefix}:{uuid.uuid4()}",
            commit=commit,
        )
    except TaskSubmissionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    return task_dict(task)


def active_durable_targets(
    *,
    task_type: str,
    payload_key: str,
    ctx: AuthContext,
    db: Session,
) -> set[str]:
    """Return active target values for one user's current project."""
    values: set[str] = set()
    for payload in active_durable_payloads(task_type=task_type, ctx=ctx, db=db):
        value = payload.get(payload_key)
        if isinstance(value, list):
            values.update(str(item) for item in value)
        elif value is not None:
            values.add(str(value))
    return values


def active_durable_payloads(
    *, task_type: str, ctx: AuthContext, db: Session,
) -> list[dict[str, Any]]:
    """Return payloads for active tasks in the user's current project."""
    project = active_project(db, ctx.user, ctx.session)
    if project is None:
        return []
    rows = db.scalars(
        select(Task).where(
            Task.owner_id == ctx.user.id,
            Task.project_id == project.id,
            Task.task_type == task_type,
            Task.status.in_(ACTIVE_TASK_STATUSES),
        )
    ).all()
    return [row.payload for row in rows if isinstance(row.payload, dict)]


def has_active_durable_tasks(*, task_type: str, ctx: AuthContext, db: Session) -> bool:
    """Return whether the current project has any non-terminal task of this type."""
    project = active_project(db, ctx.user, ctx.session)
    if project is None:
        return False
    return db.scalar(
        select(Task.id).where(
            Task.owner_id == ctx.user.id,
            Task.project_id == project.id,
            Task.task_type == task_type,
            Task.status.in_(ACTIVE_TASK_STATUSES),
        ).limit(1)
    ) is not None


def submit_legacy_engine_tasks(
    *, task_type: str, entries: list[dict], ctx: AuthContext, db: Session,
    idempotency_prefix: str,
) -> dict:
    """Submit independent records atomically; clone records share a GPU execution batch."""
    if not entries:
        return {"task_ids": []}
    created = []
    execution_batch = uuid.uuid4().hex if task_type == "voices.clone" else None
    try:
        for entry in entries:
            task = submit_legacy_engine_task(
                task_type=task_type, label=entry["label"],
                payload={**entry["payload"], "execution_batch": execution_batch}
                if execution_batch else entry["payload"],
                ctx=ctx, db=db, idempotency_prefix=idempotency_prefix, commit=False,
            )
            created.append(task["id"])
        db.commit()
    except Exception:
        db.rollback()
        raise
    response = {"task_ids": created}
    if len(created) == 1:
        response["task_id"] = created[0]
    return response
