from __future__ import annotations

import asyncio
import base64
import binascii
import json
from datetime import datetime

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from ..platform.database import SessionLocal, get_db
from ..platform.platform_settings import settings
from ..platform.deps import require_csrf, require_authenticated_user
from ..platform.models import Task, TaskEvent, User
from ..platform.security import session_is_valid_for_user
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES, TERMINAL_TASK_STATUSES
from ..platform.task_submission import task_dict
from ..services import task_views
from .task_operations import (
    TaskSubmit,
    cancel_task as cancel_task_for_user,
    retry_task as retry_task_for_user,
    submit_task as submit_task_for_user,
)

router = APIRouter(prefix="/api/v1/tasks", tags=["persistent-tasks"])
TASK_HISTORY_PAGE_SIZE = 50


def _task_json(task: Task) -> dict:
    return task_dict(task)


def _user_tasks(db: Session, user_id: str, project_id: str | None = None) -> list[Task]:
    """Return every active task plus the newest 200 records for live UI state."""
    filters = [Task.owner_id == user_id]
    if project_id:
        filters.append(Task.project_id == project_id)
    recent_ids = select(Task.id).where(*filters).order_by(Task.created_at.desc(), Task.id.desc()).limit(200)
    stmt = select(Task).where(
        *filters,
        or_(Task.status.in_(ACTIVE_TASK_STATUSES), Task.id.in_(recent_ids)),
    )
    return db.scalars(stmt.order_by(Task.created_at.desc(), Task.id.desc())).all()


@router.get("")
def list_tasks(user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Task).where(Task.owner_id == user.id).order_by(Task.created_at.desc()).limit(200)).all()
    return [_task_json(row) for row in rows]


def _encode_task_cursor(created_at: datetime, task_id: str) -> str:
    raw = json.dumps([created_at.isoformat(), task_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_task_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at, task_id = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        value = datetime.fromisoformat(created_at)
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("invalid task id")
        return value, task_id
    except (ValueError, TypeError, binascii.Error) as exc:
        raise HTTPException(422, "无效的任务历史游标") from exc


@router.get("/history")
def list_task_history(
    cursor: str | None = None,
    user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict:
    """Read the caller's complete task history in stable, newest-first pages."""
    stmt = select(Task).options(selectinload(Task.project)).where(Task.owner_id == user.id)
    if cursor:
        created_at, task_id = _decode_task_cursor(cursor)
        stmt = stmt.where(or_(
            Task.created_at < created_at,
            and_(Task.created_at == created_at, Task.id < task_id),
        ))
    rows = db.scalars(
        stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(TASK_HISTORY_PAGE_SIZE + 1)
    ).all()
    has_more = len(rows) > TASK_HISTORY_PAGE_SIZE
    page = rows[:TASK_HISTORY_PAGE_SIZE]
    items = [task_views.task_center_item(db, task) for task in page]
    next_cursor = _encode_task_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/stream")
def stream_user_tasks(
    request: Request,
    user: User = Depends(require_authenticated_user),
    project_id: str | None = None,
) -> StreamingResponse:
    """Aggregate task event stream: ONE connection carries the snapshots +
    lifecycle events of the user's tasks (all projects, or one when
    ``project_id`` is given), each frame tagged with ``task_id``. Replays
    ``snapshot_all`` on connect so a reconnect self-heals the full state.
    The per-task ``/{task_id}/events`` endpoint stays for direct consumers;
    500 ms polling should move here."""
    return StreamingResponse(
        task_views.aggregate_stream(
            lambda db: _user_tasks(db, user.id, project_id),
            request.cookies.get(settings.session_cookie),
            user.id,
            request.is_disconnected,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{task_id}")
def get_task(task_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id))
    if task is None:
        raise HTTPException(404, "任务不存在")
    return _task_json(task)


def _stream_session_valid(cookie: str | None, user_id: str) -> bool:
    with SessionLocal() as db:
        return session_is_valid_for_user(db, cookie, user_id)


def _stream_poll(task_id: str, user_id: str, after: int) -> tuple[Task | None, list[TaskEvent], bool]:
    """One poll tick's data on a single self-opened session: the task
    (``None`` once the row is gone), its events after ``after``, and whether
    it is terminal."""
    with SessionLocal() as db:
        current = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user_id))
        if current is None:
            return None, [], False
        events = db.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.sequence > after)
            .order_by(TaskEvent.sequence)
            .limit(100)
        ).all()
        return current, events, current.status in TERMINAL_TASK_STATUSES


@router.get("/{task_id}/events")
def stream_task_events(
    task_id: str,
    request: Request,
    user: User = Depends(require_authenticated_user),
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
        next_auth_check = 0.0
        while True:
            if await request.is_disconnected():
                return
            now = asyncio.get_running_loop().time()
            if now >= next_auth_check:
                if not await anyio.to_thread.run_sync(
                    _stream_session_valid, request.cookies.get(settings.session_cookie), user.id,
                ):
                    return
                next_auth_check = now + 5.0
            current, events, terminal = await anyio.to_thread.run_sync(_stream_poll, task_id, user.id, sequence)
            if current is None:
                return
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
    return submit_task_for_user(payload, user=user, db=db)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    return cancel_task_for_user(task_id, user=user, db=db)


@router.post("/{task_id}/retry")
def retry_task(task_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    return retry_task_for_user(task_id, user=user, db=db)
