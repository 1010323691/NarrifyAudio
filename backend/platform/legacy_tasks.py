"""Submit legacy engine calls through the durable task boundary.

The original pages still use the legacy task labels and response shapes.  This
adapter gives those pages a PostgreSQL-backed task id while keeping the engine
arguments as plain JSON so a Worker can reconstruct the call after an API
restart.
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api.platform_tasks import TaskSubmit, submit_task
from .deps import AuthContext
from .legacy_workspace import active_workspace, ensure_project
from .models import Task


ACTIVE_TASK_STATUSES = {"pending", "queued", "running", "paused", "cancelling", "retrying"}


def submit_legacy_engine_task(
    *,
    task_type: str,
    label: str,
    payload: dict,
    ctx: AuthContext,
    db: Session,
    idempotency_prefix: str,
) -> dict:
    """Create one durable task owned by the caller's active workspace."""
    workspace = active_workspace(db, ctx.user, ctx.session)
    if workspace is None:
        raise HTTPException(409, "尚未设置工作空间")
    try:
        project = ensure_project(db, ctx.user, workspace)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return submit_task(
        TaskSubmit(
            project_id=project.id,
            task_type=task_type,
            payload={"label": label, **payload},
            estimated_units=0,
            idempotency_key=f"{idempotency_prefix}:{uuid.uuid4()}",
        ),
        user=ctx.user,
        db=db,
    )


def active_durable_targets(
    *,
    task_type: str,
    payload_key: str,
    ctx: AuthContext,
    db: Session,
) -> set[str]:
    """Return active target values for one user's current project."""
    workspace = active_workspace(db, ctx.user, ctx.session)
    if workspace is None:
        return set()
    rows = db.scalars(
        select(Task).where(
            Task.owner_id == ctx.user.id,
            Task.project_id == workspace.id,
            Task.task_type == task_type,
            Task.status.in_(ACTIVE_TASK_STATUSES),
        )
    ).all()
    return {
        str(row.payload.get(payload_key))
        for row in rows
        if isinstance(row.payload, dict) and row.payload.get(payload_key) is not None
    }
