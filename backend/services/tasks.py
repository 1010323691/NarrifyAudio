"""Durable task cancellation/requeue transitions behind the HTTP operations layer."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..platform.models import OutboxEvent, Task, utcnow
from ..platform.task_lifecycle import (
    TERMINAL_TASK_STATUSES, append_task_event,
    suppress_pending_dispatch,
)


def cancel_task_record(
    db: Session, task: Task, *, admin: bool = False, actor_user_id: str | None = None,
) -> bool:
    """Apply the shared durable cancellation transition; return whether it changed state."""
    if task.status in TERMINAL_TASK_STATUSES:
        return False
    if task.status == "cancelling":
        return False
    if task.status == "running":
        task.status = "cancelling"
    else:
        task.status = "cancelled"
        task.finished_at = task.finished_at or utcnow()
        suppress_pending_dispatch(db, task.id)
    task.updated_at = utcnow()
    event_type = "admin_cancel_requested" if admin else "cancel_requested"
    payload = {"status": task.status}
    if admin:
        payload["actor_user_id"] = actor_user_id
    append_task_event(db, task.id, event_type, payload)
    return True


def requeue_task_record(
    db: Session, task: Task, *, event_type: str, event_payload: dict,
) -> None:
    """Reset a validated terminal task and add its durable submission event."""
    if task.result is not None:
        db.delete(task.result)
    task.status = "pending"
    task.progress = 0
    task.error_code = ""
    task.error_message = ""
    task.started_at = None
    task.finished_at = None
    task.updated_at = utcnow()
    append_task_event(db, task.id, event_type, event_payload)
    db.add(OutboxEvent(
        aggregate_type="task", aggregate_id=task.id, event_type="task.submitted",
        payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id},
    ))
