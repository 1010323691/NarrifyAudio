from __future__ import annotations

import asyncio
import base64
import binascii
import json
from datetime import datetime

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..platform.database import SessionLocal, get_db
from ..platform.platform_settings import settings
from ..platform.deps import require_csrf, require_authenticated_user, require_stream_user
from ..platform.models import Project, Task, TaskBatch, TaskEvent, TaskResult, User
from ..platform.security import session_is_valid_for_user
from ..platform.task_identity import one_row_per_entry
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES, TERMINAL_TASK_STATUSES
from ..platform.task_submission import task_dict
from ..services import task_center, task_views
from ..services.project_overview import overview_tasks
from .task_operations import (
    TaskBatchControl,
    TaskSubmit,
    batch_control_tasks as batch_control_tasks_for_user,
    cancel_task as cancel_task_for_user,
    retry_task as retry_task_for_user,
    submit_task as submit_task_for_user,
)

router = APIRouter(prefix="/api/v1/tasks", tags=["persistent-tasks"])
TASK_HISTORY_PAGE_SIZE = 50


@router.get("/submissions/{key}")
def submission_receipt(key: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    from ..platform.tts_submission import batch_receipt
    batch = db.scalar(select(TaskBatch).where(TaskBatch.owner_id == user.id, TaskBatch.idempotency_key == key))
    if batch is None:
        raise HTTPException(404, "尚未查到提交记录，请使用原幂等键确认")
    rows = db.execute(select(Task.id, Task.status).where(Task.batch_id == batch.id)).all()
    return {**batch_receipt(batch), "project_id": batch.project_id, "task_type": batch.task_type,
            "statuses": dict(rows)}


def _task_json(task: Task) -> dict:
    return task_dict(task)


def _user_tasks(db: Session, user_id: str, project_id: str | None = None, *, compact=False) -> list[Task]:
    """Return every active task plus the newest 200 records for live UI state,
    with terminal rows superseded by a newer run of the same entry hidden."""
    trashed_project = select(Project.id).where(
        Project.id == Task.project_id,
        Project.owner_id == user_id,
        Project.deleted_at.is_not(None),
    ).exists()
    filters = [Task.owner_id == user_id, ~trashed_project]
    if project_id:
        filters.append(Task.project_id == project_id)
    recent_ids = select(Task.id).where(*filters).order_by(Task.created_at.desc(), Task.id.desc()).limit(200)
    results = Task.result.and_(TaskResult.task_id.in_(select(Task.id).where(Task.task_type != 'tts.batch'))) if compact else Task.result
    stmt = select(Task).options(selectinload(Task.project), selectinload(results)).where(
        *filters,
        or_(Task.status.in_(ACTIVE_TASK_STATUSES), Task.id.in_(recent_ids)),
        one_row_per_entry(),
    )
    return db.scalars(stmt.order_by(Task.created_at.desc(), Task.id.desc())).all()


@router.get("")
def list_tasks(project_id: str | None = None, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> list[dict]:
    if project_id:
        # Project-scoped listing (workbench 处理记录 / 恢复): active tasks plus
        # the recent window, with the same supersede semantics as the global list.
        return [_task_json(row) for row in _user_tasks(db, user.id, project_id)]
    trashed_project = select(Project.id).where(
        Project.id == Task.project_id,
        Project.owner_id == user.id,
        Project.deleted_at.is_not(None),
    ).exists()
    rows = db.scalars(
        select(Task)
        .where(
            Task.owner_id == user.id,
            ~trashed_project,
            one_row_per_entry(),
        )
        .order_by(Task.created_at.desc())
        .limit(200)
    ).all()
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


@router.get("/overview")
def get_overview_tasks(project_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    try:
        return overview_tasks(db, user.id, project_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error


@router.get("/history")
def list_task_history(
    cursor: str | None = None,
    include_cancelled: bool = False,
    user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict:
    """Read the caller's complete task history in stable, newest-first pages.
    Cancelled rows are excluded by default: bulk cancels leave hundreds of
    identical terminal rows that would otherwise occupy the newest-first pages
    (and the SSE replay window) and push completed / failed work out of view.
    Pass ``include_cancelled=true`` for the full record.
    Terminal rows superseded by a newer run of the same entry are hidden as
    well, so a re-run shows as one entry, not 失败 + 进行中/已完成."""
    stmt = select(Task).options(selectinload(Task.project)).where(Task.owner_id == user.id)
    if not include_cancelled:
        stmt = stmt.where(Task.status != "cancelled")
    stmt = stmt.where(one_row_per_entry())
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
    progress_by_task: dict[str, dict] = {}
    if page:
        latest_progress = (
            select(
                TaskEvent.task_id,
                TaskEvent.payload,
                func.row_number().over(
                    partition_by=TaskEvent.task_id,
                    order_by=TaskEvent.sequence.desc(),
                ).label("row_number"),
            )
            .where(
                TaskEvent.task_id.in_([task.id for task in page]),
                TaskEvent.event_type == "progress",
            )
            .subquery()
        )
        progress_by_task = {
            task_id: payload if isinstance(payload, dict) else {}
            for task_id, payload in db.execute(
                select(latest_progress.c.task_id, latest_progress.c.payload)
                .where(latest_progress.c.row_number == 1)
            ).all()
        }
    items = [
        task_views.task_center_item(db, task, progress_by_task.get(task.id, {}))
        for task in page
    ]
    next_cursor = _encode_task_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/stream")
def stream_user_tasks(
    request: Request,
    user: User = Depends(require_stream_user),
    project_id: str | None = None,
    compact: bool = False,
) -> StreamingResponse:
    """Aggregate task event stream: ONE connection carries the snapshots +
    lifecycle events of the user's tasks (all projects, or one when
    ``project_id`` is given), each frame tagged with ``task_id``. Replays
    ``snapshot_all`` on connect so a reconnect self-heals the full state.
    The per-task ``/{task_id}/events`` endpoint stays for direct consumers;
    500 ms polling should move here."""
    return StreamingResponse(
        task_views.aggregate_stream(
            lambda db: _user_tasks(db, user.id, project_id, compact=compact),
            request.cookies.get(settings.session_cookie),
            user.id,
            request.is_disconnected,
            compact=compact,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/center/summary")
def task_center_summary(user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    return task_center.summary(db, user.id)


@router.get("/center/groups")
def task_center_groups(category: str, page: int = Query(1, ge=1),
                       user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    try:
        return task_center.groups(db, user.id, category, page)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/center/items")
def task_center_items(category: str, project_id: str, filter: str = "all", page: int = Query(1, ge=1),
                      user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    try:
        return task_center.items(db, user.id, category, project_id, filter, page)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{task_id}")
def get_task(task_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    task = db.scalar(select(Task).where(Task.id == task_id, Task.owner_id == user.id))
    if task is None:
        raise HTTPException(404, "任务不存在")
    return _task_json(task)


@router.get("/{task_id}/logs")
def task_logs(task_id: str, after: int | None = None, before: int | None = None,
              limit: int = Query(100, ge=1, le=100), user: User = Depends(require_authenticated_user),
              db: Session = Depends(get_db)) -> dict:
    if db.scalar(select(Task.id).where(Task.id == task_id, Task.owner_id == user.id)) is None:
        raise HTTPException(404, "任务不存在")
    stmt = select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.event_type == "log")
    if after is not None:
        stmt = stmt.where(TaskEvent.sequence > after)
    if before is not None:
        stmt = stmt.where(TaskEvent.sequence < before)
    rows = db.scalars(stmt.order_by(TaskEvent.sequence.asc() if after is not None else TaskEvent.sequence.desc()).limit(limit)).all()
    if after is None:
        rows.reverse()
    return {"logs": [{"sequence": row.sequence, "t": task_views.epoch(row.created_at),
                      "level": row.payload.get("level", "INFO"), "msg": row.payload.get("msg", "")} for row in rows],
            "next_before": rows[0].sequence if len(rows) == limit else None}


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
    user: User = Depends(require_stream_user),
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


@router.post("/batch-control")
def batch_control_tasks(
    payload: TaskBatchControl, compact: bool = False, user: User = Depends(require_csrf), db: Session = Depends(get_db),
) -> dict:
    return batch_control_tasks_for_user(payload, user=user, db=db, compact=compact)
