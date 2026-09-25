"""Single entry for durable task operations shared by the user and admin routes.

Ownership rules, the retry gate, module labels and the cancel/requeue state
transitions live here once; the two API surfaces (v1 task routes, admin
console) reuse this module instead of carrying their own copies.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..platform.models import OutboxEvent, Project, QuotaTransaction, Task, TaskAttempt, utcnow
from ..platform.platform_settings import settings
from ..platform.task_lifecycle import (
    TERMINAL_TASK_STATUSES,
    append_task_event,
    suppress_pending_dispatch,
)


class RetryNotAllowedError(ValueError):
    """The retry gate rejected the task; ``message`` is the user-facing reason."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def owned_task(
    db: Session, user_id: str, task_id: str,
    *, project_id: str | None = None, lock: bool = False,
) -> Task | None:
    """Task owned by this user; None when absent or not owned.

    ``lock`` requests the row lock needed for state transitions. When
    ``project_id`` is given, tasks outside that project are not owned.
    """
    statement = select(Task).where(Task.id == task_id, Task.owner_id == user_id)
    if project_id is not None:
        statement = statement.where(Task.project_id == project_id)
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def owned_project(db: Session, user_id: str, project_id: str) -> Project | None:
    """Live project owned by this user (soft-deleted projects are not owned)."""
    return db.scalar(select(Project).where(
        Project.id == project_id,
        Project.owner_id == user_id,
        Project.deleted_at.is_(None),
    ))


# Fine-grained display label per task type for the user task list.
_MODULE_LABELS = {
    "voices.foundation": "voices-foundation",
    "voices.clone": "voices-clone",
    "tts.batch": "tts-batch",
    "tts.merge": "merge",
    "bgm.analysis": "bgm-analysis",
    "bgm.segment": "bgm-segment",
    "bgm.mix": "bgm-mix",
    "music.suggest_tags": "music-ai-tags",
}


def task_module(task_type: str) -> str:
    """Fine-grained module label shown in the user task list."""
    return _MODULE_LABELS.get(task_type, task_type.split(".", 1)[0])


# Coarse execution group per task prefix for the admin console.
# A different axis from task_module (group of workers, not display label).
_WORKER_GROUPS = {
    "script": "llm",
    "music": "llm",
    "tts": "tts",
    "voices": "tts",
    "audio": "audio",
    "bgm": "audio",
    "book": "system",
    "text": "system",
}


def task_worker_group(task_type: str) -> str:
    """Coarse worker group (llm/tts/audio/system/worker) for the admin console."""
    return _WORKER_GROUPS.get(task_type.split(".", 1)[0], "worker")


def check_retry_eligible(db: Session, task: Task) -> int:
    """Single retry gate for the user and admin paths; returns the attempt count.

    The caller must hold the task row lock (``owned_task(lock=True)`` on the
    user side, the admin route's own lock) and requeue via
    ``requeue_task_record`` after a pass.
    """
    if task.status not in {"failed", "cancelled", "timeout"}:
        raise RetryNotAllowedError("任务当前不可重试")
    metered = db.scalar(select(QuotaTransaction.id).where(
        QuotaTransaction.task_id == task.id,
        QuotaTransaction.resource_type.in_(["LLM", "TTS"]),
        QuotaTransaction.kind == "consume",
    ).limit(1))
    if metered:
        raise RetryNotAllowedError("已有模型消费的任务请重新提交，以创建新的计费操作")
    attempts = int(db.scalar(select(func.count()).select_from(TaskAttempt).where(TaskAttempt.task_id == task.id)) or 0)
    if attempts >= settings.task_max_attempts:
        raise RetryNotAllowedError("任务已达到最大尝试次数")
    return attempts


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
