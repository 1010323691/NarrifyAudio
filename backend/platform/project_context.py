"""Resolve a user's active Project for compatibility pipeline adapters."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project, User, UserSession

_submission_project = ContextVar("narrify_submission_project", default=None)


@contextmanager
def submission_project(project_id):
    """Scope admission helpers without changing the persisted UI selection."""
    token = _submission_project.set(project_id)
    try:
        yield
    finally:
        _submission_project.reset(token)


def active_project(db: Session, user: User, session: UserSession) -> Project | None:
    selected = _submission_project.get()
    if selected is not None:
        return db.scalar(select(Project).where(Project.id == selected,
            Project.owner_id == user.id, Project.deleted_at.is_(None)))
    if session.active_project_id:
        item = db.scalar(select(Project).where(
            Project.id == session.active_project_id,
            Project.owner_id == user.id,
            Project.deleted_at.is_(None),
        ))
        if item is not None:
            return item
    return db.scalar(select(Project).where(
        Project.owner_id == user.id,
        Project.deleted_at.is_(None),
    ).order_by(Project.last_selected_at.desc().nullslast(), Project.updated_at.desc()))
