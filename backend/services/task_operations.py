"""Single entry for durable task operations shared by the user and admin routes.

Ownership rules, the retry gate and module labels live here once; the two
API surfaces (v1 task routes, admin console) reuse this module instead of
carrying their own copies.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.models import Project, Task


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
