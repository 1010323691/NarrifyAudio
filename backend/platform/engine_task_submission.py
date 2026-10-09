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
from .project_context import active_project, submission_project
from .models import Task
from .task_lifecycle import ACTIVE_TASK_STATUSES


def submit_engine_batch(*, task_type, request, prepare, ctx, db,
                        idempotency_key=None, project_id=None, receipt_field=None):
    from .batch_submission import submit_task_batch
    from .models import Project
    project = (db.scalar(select(Project).where(Project.id == project_id,
        Project.owner_id == ctx.user.id, Project.deleted_at.is_(None)))
        if project_id else active_project(db, ctx.user, ctx.session))
    if project is None:
        raise HTTPException(409, "尚未设置工作空间")
    from ..core.request_context import bind_workspace, reset_workspace
    from .storage import project_workspace_path
    def prepare_in_project():
        token = bind_workspace(project_workspace_path(db, ctx.user.username, project.id))
        try:
            with submission_project(project.id):
                return prepare()
        finally:
            reset_workspace(token)
    try:
        return submit_task_batch(db=db, user=ctx.user, project_id=project.id, task_type=task_type,
            request=request, prepare=prepare_in_project, idempotency_key=idempotency_key, receipt_field=receipt_field)
    except TaskSubmissionError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc


def submit_legacy_engine_task(
    *,
    task_type: str,
    label: str,
    payload: dict,
    ctx: AuthContext,
    db: Session,
    idempotency_prefix: str,
    commit: bool = True,
    idempotency_key: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Create one durable task owned by the caller's active workspace."""
    if task_type == "tts.reset":
        from .tts_submission import submit_tts_tasks
        receipt = submit_tts_tasks(task_type=task_type, entries=[{"label": label, "payload": payload}],
                                   ctx=ctx, db=db, idempotency_key=idempotency_key, project_id=project_id)
        return {"id": receipt["task_id"], **receipt}
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
    idempotency_key: str | None = None,
    project_id: str | None = None,
) -> dict:
    """Submit independent records atomically; clone and TTS records share a GPU execution batch."""
    if not entries:
        return {"task_ids": []}
    if task_type == "tts.batch":
        from .tts_submission import submit_tts_tasks
        return submit_tts_tasks(task_type=task_type, entries=entries, ctx=ctx, db=db,
                                idempotency_key=idempotency_key, project_id=project_id)
    from .batch_submission import BATCH_TYPES
    if task_type in BATCH_TYPES:
        snapshots = [entry["payload"].get("config", {}) for entry in entries]
        if any(snapshot != snapshots[0] for snapshot in snapshots):
            raise HTTPException(422, "同一批次的配置必须一致")
        return submit_engine_batch(task_type=task_type,
            request=[{k: v for k, v in entry["payload"].items() if k != "config"} for entry in entries],
            prepare=lambda: (entries, snapshots[0]), ctx=ctx, db=db,
            idempotency_key=idempotency_key, project_id=project_id)
    created = []
    execution_batch = uuid.uuid4().hex if task_type in {"voices.clone", "tts.batch"} else None
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
