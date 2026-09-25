from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import OutboxEvent, Task, TaskEvent, utcnow


TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled", "timeout"}
# Every status that is not terminal: a task in one of these can still change state.
ACTIVE_TASK_STATUSES = {"pending", "queued", "running", "paused", "cancelling", "retrying"}


def append_task_event(db: Session, task_id: str, event_type: str, payload: dict) -> TaskEvent:
    """Append an ordered event while the caller holds the task row lock."""
    last_sequence = db.scalar(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task_id)
    ) or 0
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


def suppress_pending_dispatch(db: Session, task_id: str) -> int:
    """Suppress not-yet-published dispatch events for a cancelled task."""
    result = db.query(OutboxEvent).filter(
        OutboxEvent.aggregate_type == "task",
        OutboxEvent.aggregate_id == task_id,
        OutboxEvent.published_at.is_(None),
    ).update({OutboxEvent.published_at: utcnow()}, synchronize_session=False)
    return int(result or 0)
