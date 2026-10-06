"""Shared LLM task capacity, checked under the host-wide claim lock."""
from sqlalchemy import func, select

from .models import Task
from .system_config import parse_worker_concurrency
from .task_registry import TASK_TYPES


LLM_TASK_TYPES = tuple(name for name, spec in TASK_TYPES.items() if "LLM" in spec.gpu_stages)
LLM_TASK_MULTIPLIER = 2


def llm_task_capacity_available(db, *, exclude_task_id: str | None = None) -> bool:
    """Count whole tasks, including their mechanical stages and cancellation cleanup.

    Paused tasks free capacity; resuming them must reacquire it. Dead workers'
    tasks retain their position until recovery changes their visible status.
    Replacing an expired attempt of the same task reuses its existing position.
    """
    limit = parse_worker_concurrency(db=db) * LLM_TASK_MULTIPLIER
    statement = select(func.count()).select_from(Task).where(
        Task.task_type.in_(LLM_TASK_TYPES),
        Task.status.in_(("running", "cancelling")),
    )
    if exclude_task_id is not None:
        statement = statement.where(Task.id != exclude_task_id)
    active = db.scalar(statement) or 0
    return active < limit
