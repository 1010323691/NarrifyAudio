from __future__ import annotations

import json
import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.database import SessionLocal, get_db
from ..platform.config import settings
from ..platform.deps import require_csrf, require_authenticated_user
from ..platform.models import Task, TaskEvent, User
from ..platform.security import session_is_valid_for_user
from ..platform.task_lifecycle import TERMINAL_TASK_STATUSES
from ..platform.task_submission import task_dict
from .task_operations import (
    TaskSubmit,
    cancel_task as cancel_task_for_user,
    submit_task as submit_task_for_user,
)

router = APIRouter(prefix="/api/v1/tasks", tags=["persistent-tasks"])


def _task_json(task: Task) -> dict:
    return task_dict(task)


@router.get("")
def list_tasks(user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Task).where(Task.owner_id == user.id).order_by(Task.created_at.desc()).limit(200)).all()
    return [_task_json(row) for row in rows]


@router.get("/{task_id}")
def get_task(task_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id))
    if task is None:
        raise HTTPException(404, "任务不存在")
    return _task_json(task)


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
                with SessionLocal() as db:
                    if not session_is_valid_for_user(
                        db, request.cookies.get(settings.session_cookie), user.id,
                    ):
                        return
                next_auth_check = now + 5.0
            with SessionLocal() as db:
                current = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id))
                if current is None:
                    return
                events = db.scalars(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id, TaskEvent.sequence > sequence)
                    .order_by(TaskEvent.sequence)
                    .limit(100)
                ).all()
                terminal = current.status in TERMINAL_TASK_STATUSES
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
