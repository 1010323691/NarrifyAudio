"""Bridge the desktop workspace identity to the durable project model.

The legacy pages still address a selected workspace.  During migration that
workspace uses the same stable identifier as its platform Project, so uploaded
files and durable tasks can share one ownership boundary without trusting a
client-supplied path.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Project, User, UserSession, Workspace


def active_workspace(db: Session, user: User, session: UserSession) -> Workspace | None:
    if session.active_workspace_id:
        item = db.scalar(
            select(Workspace).where(
                Workspace.id == session.active_workspace_id,
                Workspace.owner_id == user.id,
                Workspace.deleted_at.is_(None),
            )
        )
        if item is not None:
            return item
    return db.scalar(
        select(Workspace)
        .where(Workspace.owner_id == user.id, Workspace.deleted_at.is_(None))
        .order_by(Workspace.updated_at.desc())
    )


def ensure_project(db: Session, user: User, workspace: Workspace) -> Project:
    project = db.get(Project, workspace.id)
    if project is not None:
        if project.owner_id != user.id or project.deleted_at is not None:
            raise ValueError("工作空间对应的项目归属不一致")
        return project
    project = Project(
        id=workspace.id,
        owner_id=user.id,
        name=workspace.name,
        description="由兼容工作空间自动建立",
    )
    db.add(project)
    db.flush()
    return project

