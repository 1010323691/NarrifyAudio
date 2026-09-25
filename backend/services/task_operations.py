"""Single entry for durable task operations shared by the user and admin routes.

Ownership rules, the retry gate and module labels live here once; the two
API surfaces (v1 task routes, admin console) reuse this module instead of
carrying their own copies.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..platform.config import settings
from ..platform.models import Project, QuotaReservation, QuotaTransaction, Task, TaskAttempt


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
    ``services.tasks.requeue_task_record`` after a pass.
    """
    if task.status not in {"failed", "cancelled", "timeout"}:
        raise RetryNotAllowedError("任务当前不可重试")
    reservation = db.scalar(select(QuotaReservation).where(QuotaReservation.task_id == task.id))
    if reservation is not None and reservation.units:
        raise RetryNotAllowedError("带额度预留的任务暂不支持原任务重试")
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
