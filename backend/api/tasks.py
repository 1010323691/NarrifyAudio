"""Unified task endpoints for legacy pages and PostgreSQL-backed tasks.

The UI still uses the original task snapshot/SSE shape. This module adapts durable
Task/TaskEvent rows to that response contract while keeping ownership checks in the
database, so tasks remain visible across API and Worker restarts. The snapshot /
event-mapping / aggregate-stream machinery is shared with the v1 aggregate endpoint
(``services/task_views.py``) — 5a deletes this file once the views run on the v1 surface.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ..platform.database import SessionLocal
from ..platform.config import settings
from ..platform.deps import AuthContext, get_auth_context
from ..platform.project_context import active_project
from ..platform.models import Task as DurableTask
from ..platform.task_lifecycle import TERMINAL_TASK_STATUSES
from ..services import task_views
from ..services.task_operations import owned_task
from ..services.tasks import cancel_task_record
from .task_operations import retry_task as retry_durable_task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _current_project_id(db, ctx: AuthContext) -> str | None:
    project = active_project(db, ctx.user, ctx.session)
    return project.id if project is not None else None


def _durable_rows(db, ctx: AuthContext) -> list[DurableTask]:
    project_id = _current_project_id(db, ctx)
    if project_id is None:
        return []
    return db.scalars(
        select(DurableTask)
        .where(DurableTask.owner_id == ctx.user.id, DurableTask.project_id == project_id)
        .order_by(DurableTask.created_at.desc())
        .limit(200)
    ).all()


def _durable_owned(
    db, ctx: AuthContext, task_id: str, *, lock: bool = False,
) -> DurableTask | None:
    project_id = _current_project_id(db, ctx)
    if project_id is None:
        return None
    return owned_task(db, ctx.user.id, task_id, project_id=project_id, lock=lock)


@router.get("")
def list_tasks(ctx: AuthContext = Depends(get_auth_context)) -> list[dict]:
    with SessionLocal() as db:
        return [task_views.task_snapshot(db, task) for task in _durable_rows(db, ctx)]


@router.get("/stream")
def stream_all_tasks(request: Request, ctx: AuthContext = Depends(get_auth_context)):
    """Stream snapshots and lifecycle events for the current project's tasks."""
    return StreamingResponse(
        task_views.aggregate_stream(
            lambda db: _durable_rows(db, ctx),
            request.cookies.get(settings.session_cookie),
            ctx.user.id,
            request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{task_id}")
def get_task(task_id: str, ctx: AuthContext = Depends(get_auth_context)) -> dict:
    with SessionLocal() as db:
        durable = _durable_owned(db, ctx, task_id)
        if durable is None:
            raise HTTPException(404, "任务不存在")
        return task_views.task_snapshot(db, durable)


@router.post("/{task_id}/{action}")
def control_task(task_id: str, action: str, ctx: AuthContext = Depends(get_auth_context)) -> dict:
    if action == "retry":
        with SessionLocal() as db:
            durable = _durable_owned(db, ctx, task_id, lock=True)
            if durable is None:
                raise HTTPException(404, "任务不存在")
            retry_durable_task(task_id, user=ctx.user, db=db)
            return task_views.task_snapshot(db, durable)
    if action != "cancel":
        raise HTTPException(400, "持久化任务当前只支持取消或重试")
    with SessionLocal() as db:
        durable = _durable_owned(db, ctx, task_id, lock=True)
        if durable is None:
            raise HTTPException(404, "任务不存在")
        if durable.status not in TERMINAL_TASK_STATUSES:
            cancel_task_record(db, durable)
            db.commit()
        return task_views.task_snapshot(db, durable)
