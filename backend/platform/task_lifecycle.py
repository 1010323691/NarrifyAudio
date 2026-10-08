from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import OutboxEvent, Task, TaskEvent, utcnow


TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled", "timeout"}
# Every status that is not terminal: a task in one of these can still change state.
ACTIVE_TASK_STATUSES = {"pending", "queued", "running", "paused", "cancelling", "retrying"}


def append_task_event(db: Session, task_id: str, event_type: str, payload: dict) -> TaskEvent:
    """Append an ordered event while the caller holds the task row lock."""
    task = db.get(Task, task_id)
    last_sequence = task.event_sequence or 0
    state = dict(task.ui_state or {})
    if task.ui_state is None:
        # Old rows are hydrated once; new rows have no history to read.
        last_sequence = max(last_sequence, db.scalar(select(func.max(TaskEvent.sequence)).where(TaskEvent.task_id == task_id)) or 0)
        latest = select(func.max(TaskEvent.sequence)).where(
            TaskEvent.task_id == task_id, TaskEvent.event_type.in_(['progress', 'phase', 'segments'])
        ).group_by(TaskEvent.event_type)
        for old in db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.sequence.in_(latest))):
            _update_ui_state(state, old.event_type, old.payload)
    task.event_sequence = int(last_sequence) + 1
    _update_ui_state(state, event_type, payload)
    if event_type == "succeeded" and 'result' not in state and task.result is not None:
        state["result"] = compact_result(task.result.result)
    task.ui_state = state
    event = TaskEvent(
        task_id=task_id,
        sequence=int(last_sequence) + 1,
        event_type=event_type,
        payload=payload,
    )
    db.add(event)
    # SessionLocal disables autoflush. Recovery can append multiple events in
    # one transaction, so persist this sequence before the next max() lookup.
    db.flush()
    return event


def _update_ui_state(state: dict, event_type: str, payload: dict) -> None:
    if event_type in {'retry_requested', 'admin_retry_requested'}:
        state.pop('result', None)
        state['current'] = state['phase'] = '等待启动'
    elif event_type == "progress":
        if payload.get("current"):
            state["current"] = payload["current"]
    elif event_type == "phase":
        state["phase"] = payload.get("phase", "")
        if payload.get("current"):
            state["current"] = payload["current"]
    elif event_type == "segments":
        state["segments"] = payload


def hydrate_task_ui_states(db: Session, tasks) -> None:
    """Preserve migrated chapter counters when attaching a pooled execution slot."""
    old = {task.id: task for task in tasks if task.ui_state is None}
    if not old:
        return
    latest = select(TaskEvent.task_id, TaskEvent.event_type, TaskEvent.payload,
        func.row_number().over(partition_by=(TaskEvent.task_id, TaskEvent.event_type),
                               order_by=TaskEvent.sequence.desc()).label('rn')).where(
        TaskEvent.task_id.in_(old), TaskEvent.event_type.in_(['progress', 'phase', 'segments'])).subquery()
    states = {task_id: {} for task_id in old}
    for task_id, kind, payload in db.execute(select(latest.c.task_id, latest.c.event_type, latest.c.payload).where(latest.c.rn == 1)):
        _update_ui_state(states[task_id], kind, payload)
    for task_id, task in old.items():
        task.ui_state = states[task_id]


def compact_result(result: dict) -> dict:
    summary = {key: value for key, value in result.items() if isinstance(value, (int, float, bool)) or isinstance(value, str) and len(value) <= 1024}
    if isinstance(result.get('failed'), list):
        summary['failed_count'] = len(result['failed'])
    return summary


def suppress_pending_dispatch(db: Session, task_id: str) -> int:
    """Suppress not-yet-published dispatch events for a cancelled task."""
    result = db.query(OutboxEvent).filter(
        OutboxEvent.aggregate_type == "task",
        OutboxEvent.aggregate_id == task_id,
        OutboxEvent.published_at.is_(None),
    ).update({OutboxEvent.published_at: utcnow()}, synchronize_session=False)
    return int(result or 0)
