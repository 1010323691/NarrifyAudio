"""Task-view logic for the v1 ``/api/v1/tasks/stream`` aggregate endpoint
(S7/5a): adapts durable Task/TaskEvent rows to the UI's task-snapshot shape
(module/label/phase/logs/llm metrics) over one SSE connection carrying many
tasks. The legacy ``/api/tasks`` adapter that once shared this module is
retired (5a) — this is the only copy. Frame contract (all frames are
``data:`` lines, no ``event:`` field):

- ``{"type": "snapshot_all", "tasks": [...]}``` — replayed once on connect
- ``{"type": "progress"|"phase"|"log"|"segments", "task_id": ...}``
- ``{"type": "status", "status": ..., "task": <snapshot>, "task_id": ...}`` — terminal /
  lifecycle transitions (the snapshot already carries the mapped status), plus a
  one-shot catch-up when a row leaves the live window with a terminal transition
  the client never saw (a batch finishing past the newest-200 window)
- ``{"type": "superseded", "task_id": ...}`` — a terminal row left the visible
  view because a newer run of the same entry superseded it: the client drops
  the old terminal row (the list shows the new run, not 完成/失败 + 进行中
  side by side). The stream names rows it tracked and pruned, plus — right
  after the replay and whenever fresh rows enter — rows it never tracked
  because the client loaded them from /history beyond the live window
- ``{"type": "ping"}`` — idle keepalive (the stream never ends on its own)
"""
from __future__ import annotations

import anyio
import asyncio
import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.database import SessionLocal
from ..platform.models import Task as DurableTask
from ..platform.models import TaskEvent
from ..platform.security import session_is_valid_for_user
from ..platform.task_identity import superseded_ids_hidden_by, superseded_task_ids
from ..platform.task_lifecycle import TERMINAL_TASK_STATUSES
from .task_operations import task_module


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def epoch(value) -> float:
    return value.timestamp() if value is not None else 0.0


def legacy_status(status: str) -> str:
    """Durable status -> the legacy UI's status vocabulary (queued/retrying read
    as pending, cancelling as running) — the UI renders the old shapes."""
    if status in {"queued", "retrying"}:
        return "pending"
    if status == "cancelling":
        return "running"
    return status


def durable_label(task: DurableTask) -> str:
    return task_display_label(task.task_type, task.payload)


def task_display_label(task_type: str, payload: dict | None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("label"):
        return str(payload["label"])
    source = str(payload.get("source_name") or payload.get("output_name") or "")
    if task_type == 'project.progress':
        return '更新制作进度'
    if task_type == "script.parse":
        return f"文本解析（{source or '文件'}）"
    if task_type.startswith("resources."):
        return {"resources.scan": "资源清单扫描", "resources.package": "资源文件打包", "resources.cleanup": "过期缓存清理"}.get(task_type, "资源管理")
    return f"持久化任务：{task_type}"


def task_events(db: Session, task_id: str) -> list[TaskEvent]:
    """The task's recent events in ascending sequence (cap 1000 — enough for
    the live-log panel; the tail is what snapshots and the stream consume)."""
    latest = db.scalars(
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.sequence.desc())
        .limit(1000)
    ).all()
    return list(reversed(latest))


def task_snapshot(db: Session, task: DurableTask) -> dict:
    events = task_events(db, task.id)
    logs = []
    current = ""
    phase = ""
    segments: dict = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.event_type == "progress":
            current = str(payload.get("current") or current)
        elif event.event_type == "phase":
            phase = str(payload.get("phase") or phase)
        elif event.event_type == "log":
            logs.append(
                {
                    "level": str(payload.get("level") or "INFO"),
                    "msg": str(payload.get("msg") or ""),
                    "t": epoch(event.created_at),
                }
            )
        elif event.event_type == "segments":
            segments = payload
    if not segments:
        latest_segments = db.scalar(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.event_type == "segments")
            .order_by(TaskEvent.sequence.desc())
            .limit(1)
        )
        if latest_segments is not None and isinstance(latest_segments.payload, dict):
            segments = latest_segments.payload
    if not phase:
        last_phase = db.scalar(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.event_type == "phase")
            .order_by(TaskEvent.sequence.desc())
            .limit(1)
        )
        if last_phase is not None:
            phase = str((last_phase.payload or {}).get("phase") or "")
    if not current:
        last_progress = db.scalar(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id, TaskEvent.event_type == "progress")
            .order_by(TaskEvent.sequence.desc())
            .limit(1)
        )
        if last_progress is not None:
            current = str((last_progress.payload or {}).get("current") or "")
    result = task.result.result if task.result is not None else {}
    project = getattr(task, "project", None)
    return {
        "id": task.id,
        "project_id": getattr(task, "project_id", ""),
        "project_name": getattr(project, "name", None) or "已删除项目",
        "task_type": task.task_type,
        "module": task_module(task.task_type),
        "label": durable_label(task),
        "seq": int(task.created_at.timestamp() * 1000),
        "status": legacy_status(task.status),
        "phase": phase,
        "progress": max(0.0, min(1.0, task.progress / 100.0)),
        "current": current,
        "logs": logs,
        "llm_stream": "",
        "seg_done": int(segments.get("done") or 0),
        "seg_total": int(segments.get("total") or 0),
        "seg_chars_done": int(segments.get("chars_done") or 0),
        "seg_chars_total": int(segments.get("chars_total") or 0),
        "result": result,
        "error": task.error_message or "",
        "error_code": getattr(task, "error_code", "") or "",
        "created": epoch(task.created_at),
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat() if getattr(task, "updated_at", None) else "",
        "started": epoch(task.started_at),
        "finished": epoch(task.finished_at),
    }


def task_center_item(db: Session, task: DurableTask, progress_payload: dict | None = None) -> dict:
    """Compact history row for the cross-project task center (no log payloads)."""
    progress_payload = progress_payload if isinstance(progress_payload, dict) else {}
    project = getattr(task, "project", None)
    return {
        "id": task.id,
        "project_id": getattr(task, "project_id", ""),
        "project_name": getattr(project, "name", None) or "已删除项目",
        "task_type": task.task_type,
        "label": durable_label(task),
        "status": legacy_status(task.status),
        "progress": max(0.0, min(1.0, task.progress / 100.0)),
        "current": str(progress_payload.get("current") or ""),
        "error": task.error_message or "",
        "error_code": getattr(task, "error_code", "") or "",
        "created": epoch(task.created_at),
        "created_at": task.created_at.isoformat(),
    }


def _fresh_task_snapshot(db: Session, task: DurableTask) -> dict:
    # PostgreSQL READ COMMITTED can see a worker's commit in the event query
    # after rows_fn loaded an older Task into the identity map. Refresh AFTER
    # reading the events, including a result relationship cached as absent.
    db.refresh(task)
    db.expire(task, ["result"])
    return task_snapshot(db, task)


def event_frame(db: Session, task: DurableTask, event: TaskEvent) -> dict | None:
    """One durable event -> the legacy UI frame (``task_id`` already embedded),
    or ``None`` for event types the UI does not render."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    if event.event_type == "progress":
        return {
            "type": "progress",
            "task_id": task.id,
            "progress": task.progress / 100.0,
            "current": payload.get("current") or "",
        }
    if event.event_type == "phase":
        return {"type": "phase", "task_id": task.id, "phase": payload.get("phase") or "", "current": payload.get("current") or ""}
    if event.event_type == "log":
        return {
            "type": "log",
            "task_id": task.id,
            "level": payload.get("level") or "INFO",
            "msg": payload.get("msg") or "",
            "t": epoch(event.created_at),
        }
    if event.event_type == "segments":
        return {"type": "segments", "task_id": task.id, **payload}
    if event.event_type in {
        "succeeded", "failed", "cancelled",
        "submitted", "cancel_requested", "admin_cancel_requested",
        "retry_requested", "admin_retry_requested",
        "retry_scheduled", "attempt_expired", "dispatch_recovered", "attempt_started",
        "llm_unavailable",
    }:
        snapshot = _fresh_task_snapshot(db, task)
        if event.event_type == "submitted":
            return {"type": "snapshot", "task_id": task.id, "task": snapshot}
        # Use the same authoritative status inside and outside the snapshot;
        # an old terminal event may be replayed after the task was retried.
        return {"type": "status", "status": snapshot["status"], "task_id": task.id, "task": snapshot}
    return None


def _new_frames(rows_fn, seen: dict[str, int], delivered: dict[str, str]) -> list[dict]:
    """All not-yet-seen frames for the rows ``rows_fn`` returns, in row order.
    Rows that disappeared from the view are pruned from ``seen``; the ones
    hidden because a newer run of the same entry superseded them also get a
    ``superseded`` frame, so a long-lived client drops the old terminal row
    instead of keeping it next to the new run. Rows that enter the view fresh
    name their hidden predecessors the same way — including predecessors the
    window never tracked (the client loaded them from /history). ``delivered``
    tracks the last status the client was told per row; a pruned row whose
    terminal transition still differs from it gets ONE catch-up ``status``
    frame — its completion can land in the same poll tick it leaves the
    newest-200 window (a batch finishing past the window), and without the
    frame the client keeps showing it as running forever. The catch-up cost is
    bounded per row: pruning happens once per window exit (a single ``db.get``,
    plus one full snapshot only when the status actually differs), so even a
    bulk exit — e.g. trashing a project drops up to its 200 visible rows in
    one 0.5 s tick — costs at most one lookup per row. Visible rows also
    reconcile their durable status against ``delivered`` every tick, even
    when no new event exists, so an already-consumed transition self-heals."""
    emitted: list[dict] = []
    with SessionLocal() as db:
        rows = rows_fn(db)
        current_ids = {row.id for row in rows}
        fresh_ids = [row.id for row in rows if row.id not in seen]
        for task in rows:
            events = db.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == task.id, TaskEvent.sequence > seen.get(task.id, 0))
                .order_by(TaskEvent.sequence)
                .limit(1000)
            ).all()
            for event in events:
                seen[task.id] = max(seen.get(task.id, 0), event.sequence)
                frame = event_frame(db, task, event)
                if frame is not None:
                    # Only frames embedding a full snapshot tell the client a
                    # status — remember that one per row.
                    if "task" in frame:
                        delivered[task.id] = frame["task"]["status"]
                    emitted.append(frame)
            if delivered.get(task.id) != legacy_status(task.status):
                snapshot = _fresh_task_snapshot(db, task)
                delivered[task.id] = snapshot["status"]
                emitted.append({
                    "type": "status", "status": snapshot["status"],
                    "task_id": task.id, "task": snapshot,
                })
        pruned = [task_id for task_id in seen if task_id not in current_ids]
        for task_id in pruned:
            row = db.get(DurableTask, task_id)
            if row is not None and row.status in TERMINAL_TASK_STATUSES:
                status = legacy_status(row.status)
                if delivered.get(task_id) != status:
                    emitted.append({
                        "type": "status",
                        "status": status,
                        "task_id": task_id,
                        "task": task_snapshot(db, row),
                    })
            seen.pop(task_id, None)
            delivered.pop(task_id, None)
        superseded: set[str] = set()
        if pruned:
            superseded.update(superseded_task_ids(db, pruned))
        if fresh_ids:
            superseded.update(superseded_ids_hidden_by(db, fresh_ids))
        for task_id in sorted(superseded):
            emitted.append({"type": "superseded", "task_id": task_id})
    return emitted


def snapshot_payload(rows_fn) -> tuple[list[dict], dict[str, int], set[str], dict[str, str]]:
    """Initial replay on one self-opened session: the ``snapshot_all`` task
    snapshots, the per-task seen-sequence map the poll ticks resume from,
    the rows the replayed rows supersede that no stream ever tracked (client
    history beyond the newest-200 replay), and the per-task status the replay
    just told the client (the prune catch-up compares against it) — the
    superseded rows are named right after the replay so a reconnect drops
    them from the merged list too."""
    seen: dict[str, int] = {}
    delivered: dict[str, str] = {}
    snapshots: list[dict] = []
    with SessionLocal() as db:
        rows = rows_fn(db)
        for task in rows:
            events = task_events(db, task.id)
            seen[task.id] = events[-1].sequence if events else 0
            # Capture the cursor BEFORE refreshing the snapshot: any commit
            # after this read remains eligible for the next live poll.
            snapshot = _fresh_task_snapshot(db, task)
            snapshots.append(snapshot)
            delivered[task.id] = snapshot["status"]
        hidden = superseded_ids_hidden_by(db, [task.id for task in rows]) if rows else set()
        return snapshots, seen, hidden, delivered


def session_still_valid(auth_token: str, user_id: str) -> bool:
    with SessionLocal() as db:
        return session_is_valid_for_user(db, auth_token, user_id)


async def aggregate_stream(rows_fn, auth_token: str, user_id: str, is_disconnected, compact=False):
    """The shared aggregate SSE body: replay ``snapshot_all`` for every row
    ``rows_fn(db)`` returns (the caller scopes it — current project for the
    legacy surface, the whole user for v1), then stream each row's new events
    over the one connection, re-checking the session every 5 s. Never ends on
    its own; pings keep the connection warm. Async generator on purpose:
    Starlette runs sync generators by borrowing a thread-pool worker for EVERY
    iteration, so the 0.5 s poll would hold a worker per idle connection
    (a handful of open tabs exhausts the pool); an async generator sleeps on
    the event loop instead. The synchronous DB units (snapshot, auth check,
    poll) are handed to the pool via ``anyio.to_thread`` so a slow query
    cannot freeze the event loop for every other connection on it.
    """
    next_auth_check = 0.0
    idle_delay = 1.0
    snapshots, seen, hidden, delivered = await anyio.to_thread.run_sync(compact_snapshot_payload if compact else snapshot_payload, rows_fn)
    if compact:
        for offset in range(0, len(snapshots), 100):
            yield sse({"type": "snapshot_chunk", "tasks": snapshots[offset:offset + 100], "first": offset == 0})
        yield sse({"type": "snapshot_end"})
    else:
        yield sse({"type": "snapshot_all", "tasks": snapshots})
    for task_id in sorted(hidden):
        yield sse({"type": "superseded", "task_id": task_id})
    while True:
        if await is_disconnected():
            return
        now = time.monotonic()
        if now >= next_auth_check:
            if not await anyio.to_thread.run_sync(session_still_valid, auth_token, user_id):
                return
            next_auth_check = now + 5.0
        frames = await anyio.to_thread.run_sync(compact_new_frames if compact else _new_frames, rows_fn, seen, delivered)
        for frame in frames:
            yield sse(frame)
        if not frames:
            yield sse({"type": "ping"})
        idle_delay = 1.0 if frames else min(3.0, idle_delay + 0.5)
        await asyncio.sleep(idle_delay if compact else 0.5)


def compact_snapshots(db, rows):
    """Build snapshots with bounded collection queries, never per-task history reads."""
    from sqlalchemy import func
    states = {row.id: dict(row.ui_state or {}) for row in rows}
    old_ids = [row.id for row in rows if row.ui_state is None]
    for offset in range(0, len(old_ids), 500):
        ids = old_ids[offset:offset + 500]
        latest = select(TaskEvent.task_id, TaskEvent.event_type, TaskEvent.payload,
                        func.row_number().over(partition_by=(TaskEvent.task_id, TaskEvent.event_type),
                                               order_by=TaskEvent.sequence.desc()).label('rn')).where(
                            TaskEvent.task_id.in_(ids), TaskEvent.event_type.in_(['phase', 'progress', 'segments'])).subquery()
        for tid, kind, payload in db.execute(select(latest.c.task_id, latest.c.event_type, latest.c.payload).where(latest.c.rn == 1)):
            from ..platform.task_lifecycle import _update_ui_state
            _update_ui_state(states[tid], kind, payload)
    old_tts = [row.id for row in rows if row.ui_state is None and row.task_type == 'tts.batch']
    from ..platform.models import TaskResult
    from ..platform.task_lifecycle import compact_result
    for offset in range(0, len(old_tts), 500):
        for task_id, result in db.execute(select(TaskResult.task_id, TaskResult.result).where(TaskResult.task_id.in_(old_tts[offset:offset + 500]))):
            states[task_id]['result'] = compact_result(result)
    snapshots = []
    for task in rows:
        state = states[task.id]
        segments = state.get('segments', {})
        snapshot = {
            'id': task.id, 'project_id': task.project_id, 'project_name': task.project.name if task.project else '已删除项目',
            'task_type': task.task_type, 'module': task_module(task.task_type), 'label': durable_label(task),
            'seq': int(epoch(task.created_at) * 1000), 'status': legacy_status(task.status),
            'phase': state.get('phase', ''), 'current': state.get('current', ''), 'progress': task.progress / 100,
            'logs': [], 'llm_stream': '', 'result': state.get('result', {}),
            'seg_done': segments.get('done', 0), 'seg_total': segments.get('total', 0),
            'seg_chars_done': segments.get('chars_done', 0), 'seg_chars_total': segments.get('chars_total', 0),
            'error': task.error_message, 'error_code': task.error_code,
            'created': epoch(task.created_at), 'created_at': task.created_at.isoformat(),
            'updated_at': task.updated_at.isoformat(), 'started': epoch(task.started_at), 'finished': epoch(task.finished_at),
        }
        # Other workbenches still consume their existing small structured results.
        if task.task_type != 'tts.batch' and task.result is not None:
            snapshot['result'] = task.result.result
        snapshots.append(snapshot)
    return snapshots


def compact_snapshot_payload(rows_fn):
    with SessionLocal() as db:
        rows = rows_fn(db)
        snapshots = compact_snapshots(db, rows)
        return (snapshots, {row.id: row.event_sequence for row in rows},
                superseded_ids_hidden_by(db, [row.id for row in rows]) if rows else set(),
                {snapshot['id']: snapshot['status'] for snapshot in snapshots})


def compact_new_frames(rows_fn, seen, delivered):
    from sqlalchemy import and_, or_
    from sqlalchemy.orm import selectinload
    from ..platform.models import TaskResult
    frames = []
    with SessionLocal() as db:
        rows = rows_fn(db)
        by_id = {row.id: row for row in rows}
        fresh_ids = [row.id for row in rows if row.id not in seen]
        changed = [row for row in rows if row.event_sequence > seen.get(row.id, 0)]
        budget = 1000
        for offset in range(0, len(changed), 500):
            if budget <= 0:
                break
            chunk = changed[offset:offset + 500]
            events = db.scalars(select(TaskEvent).where(or_(*[
                and_(TaskEvent.task_id == row.id, TaskEvent.sequence > seen.get(row.id, 0),
                     TaskEvent.sequence <= row.event_sequence) for row in chunk
            ])).order_by(TaskEvent.created_at, TaskEvent.task_id, TaskEvent.sequence).limit(budget)).all()
            budget -= len(events)
            # A poll emits one latest ordinary progress per task, but retains logs.
            latest = {}
            for event in events:
                seen[event.task_id] = max(seen.get(event.task_id, 0), event.sequence)
                if event.event_type in {'progress', 'phase', 'segments'}:
                    latest[(event.task_id, event.event_type)] = event
                elif event.event_type == 'log':
                    frame = event_frame(db, by_id[event.task_id], event)
                    if frame:
                        frames.append(frame)
                elif event.event_type in {
                    'submitted', 'attempt_started', 'attempt_expired', 'dispatch_recovered',
                    'succeeded', 'failed', 'cancelled', 'cancel_requested', 'admin_cancel_requested',
                    'retry_requested', 'admin_retry_requested', 'retry_scheduled', 'llm_unavailable',
                    'paused', 'pause_requested', 'resume_requested', 'resumed',
                }:
                    # Preserve the audit transition even when several transitions
                    # happened before this poll. Status snapshots stay authoritative.
                    frames.append({'type': 'lifecycle', 'task_id': event.task_id, 'event_type': event.event_type,
                                   'sequence': event.sequence, 'payload': event.payload, 't': epoch(event.created_at)})
            for event in latest.values():
                task = by_id[event.task_id]
                state = task.ui_state
                if state is not None and event.event_type == 'progress':
                    frame = {'type': 'progress', 'task_id': task.id, 'progress': task.progress / 100,
                             'current': state.get('current', '')}
                elif state is not None and event.event_type == 'phase':
                    frame = {'type': 'phase', 'task_id': task.id, 'phase': state.get('phase', ''), 'current': state.get('current', '')}
                elif state is not None and event.event_type == 'segments':
                    frame = {'type': 'segments', 'task_id': task.id, **state.get('segments', {})}
                else:
                    frame = event_frame(db, task, event)
                if frame:
                    frames.append(frame)
        snapshots_needed = [row for row in rows if row.id not in delivered or delivered[row.id] != legacy_status(row.status)]
        fresh_snapshots = []
        for snapshot in compact_snapshots(db, snapshots_needed):
            tid = snapshot['id']; delivered[tid] = snapshot['status']
            if tid in fresh_ids:
                fresh_snapshots.append(snapshot)
            else:
                frames.append({'type': 'status', 'status': snapshot['status'], 'task_id': tid, 'task': snapshot})
        for offset in range(0, len(fresh_snapshots), 100):
            frames.append({'type': 'snapshot_add_chunk', 'tasks': fresh_snapshots[offset:offset + 100]})
        for tid in fresh_ids:
            seen.setdefault(tid, 0)
        pruned = [tid for tid in seen if tid not in by_id]
        for offset in range(0, len(pruned), 500):
            gone = db.scalars(select(DurableTask).options(selectinload(DurableTask.project), selectinload(DurableTask.result.and_(TaskResult.task_id.in_(select(DurableTask.id).where(DurableTask.task_type != 'tts.batch'))))).where(
                DurableTask.id.in_(pruned[offset:offset + 500]), DurableTask.status.in_(TERMINAL_TASK_STATUSES))).all()
            for snapshot in compact_snapshots(db, gone):
                tid = snapshot['id']
                if delivered.get(tid) != snapshot['status']:
                    frames.append({'type': 'status', 'task_id': tid, 'status': snapshot['status'], 'task': snapshot})
        for tid in pruned:
            seen.pop(tid, None); delivered.pop(tid, None)
        hidden = set()
        if pruned:
            hidden |= superseded_task_ids(db, pruned)
        if fresh_ids:
            hidden |= superseded_ids_hidden_by(db, fresh_ids)
        frames.extend({'type': 'superseded', 'task_id': tid} for tid in sorted(hidden))
    return frames
