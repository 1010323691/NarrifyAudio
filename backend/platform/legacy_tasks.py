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

from ..api.platform_tasks import TaskSubmit, submit_task
from .deps import AuthContext
from .legacy_workspace import active_workspace, ensure_project
from .models import Task


ACTIVE_TASK_STATUSES = {"pending", "queued", "running", "paused", "cancelling", "retrying"}


def estimate_legacy_units(task_type: str, payload: dict[str, Any]) -> int:
    """Estimate billable work for legacy endpoints before dispatch.

    Units deliberately describe work items rather than wall-clock seconds:
    one chapter, script, package, or track is one unit.  Pure file packaging
    and reset operations remain free.  The estimate is persisted in the task's
    quota reservation and later settled or released transactionally.
    """
    if task_type in {"bgm.analysis", "bgm.segment", "bgm.mix", "voices.foundation", "voices.clone", "tts.stress", "music.suggest_tags", "audio.silences", "audio.cut"}:
        for key in ("speakers", "rows", "names", "chapters", "scripts", "segments"):
            value = payload.get(key)
            if isinstance(value, list):
                return max(1, len(value))
        return 1
    if task_type == "bgm.match":
        chapters = payload.get("chapters")
        return max(1, len(chapters)) if isinstance(chapters, list) else 1
    if task_type == "tts.batch":
        scripts = payload.get("scripts")
        return max(1, len(scripts)) if isinstance(scripts, list) else 1
    if task_type == "tts.merge":
        return 1
    # audio.zip/audio.export, bgm.package and tts.reset only arrange or
    # remove already-produced files and do not consume generation quota.
    return 0


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
            estimated_units=estimate_legacy_units(task_type, payload),
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
    values: set[str] = set()
    for row in rows:
        if not isinstance(row.payload, dict):
            continue
        value = row.payload.get(payload_key)
        if isinstance(value, list):
            values.update(str(item) for item in value)
        elif value is not None:
            values.add(str(value))
    return values
