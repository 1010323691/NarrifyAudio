"""Shared lifecycle rules for user-owned projects and their storage directories."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.models import Project, Task, new_id, utcnow


ACTIVE_TASK_STATUSES = ("pending", "queued", "running", "paused", "cancelling", "retrying")


class ActiveProjectTasksError(ValueError):
    pass


def create_project(
    db: Session, *, owner_id: str, username: str, name: str, description: str = "",
) -> Project:
    """Create one project record with a stable key for its storage directory."""
    project_id = new_id()
    project = Project(id=project_id, owner_id=owner_id, name=name, description=description,
                      directory_key=f"{username}/{project_id}")
    db.add(project)
    db.flush()
    return project


def ensure_scope_idle(db: Session, owner_id: str, project_id: str) -> None:
    active = db.scalar(select(Task.id).where(
        Task.owner_id == owner_id,
        Task.project_id == project_id,
        Task.status.in_(ACTIVE_TASK_STATUSES),
    ).limit(1))
    if active is not None:
        raise ActiveProjectTasksError("项目仍有未完成任务，请先等待任务完成或取消任务后再删除。")


def ensure_project_idle(db: Session, project: Project) -> None:
    ensure_scope_idle(db, project.owner_id, project.id)


def rename_project(db: Session, project: Project, name: str) -> None:
    project.name = name


def soft_delete_project(db: Session, project: Project) -> None:
    ensure_project_idle(db, project)
    deleted_at = utcnow()
    project.deleted_at = deleted_at
