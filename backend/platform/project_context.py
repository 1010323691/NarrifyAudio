"""Resolve a user's active Project for compatibility pipeline adapters."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project, User, UserSession


def active_project(db: Session, user: User, session: UserSession) -> Project | None:
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
