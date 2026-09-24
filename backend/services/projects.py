"""Shared lifecycle rules for Project rows with optional managed Workspaces."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.models import Project, Task, Workspace, new_id, utcnow


ACTIVE_TASK_STATUSES = ("pending", "queued", "running", "paused", "cancelling", "retrying")


class ActiveProjectTasksError(ValueError):
    pass


def create_project_workspace(
    db: Session, *, owner_id: str, username: str, name: str, description: str = "",
) -> tuple[Project, Workspace]:
    """Create the two persistent views of one user project with the same ID."""
    project_id = new_id()
    project = Project(id=project_id, owner_id=owner_id, name=name, description=description)
    workspace = Workspace(
        id=project_id, owner_id=owner_id, name=name,
        directory_key=f"{username}/{project_id}",
    )
    db.add_all((project, workspace))
    db.flush()
    return project, workspace


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
    workspace = db.scalar(select(Workspace).where(
        Workspace.id == project.id, Workspace.owner_id == project.owner_id,
        Workspace.deleted_at.is_(None),
    ))
    if workspace is not None:
        workspace.name = name


def soft_delete_project(db: Session, project: Project) -> None:
    ensure_project_idle(db, project)
    deleted_at = utcnow()
    project.deleted_at = deleted_at
    workspace = db.scalar(select(Workspace).where(
        Workspace.id == project.id, Workspace.owner_id == project.owner_id,
        Workspace.deleted_at.is_(None),
    ))
    if workspace is not None:
        workspace.deleted_at = deleted_at


def soft_delete_workspace(db: Session, workspace: Workspace) -> None:
    project = db.scalar(select(Project).where(
        Project.id == workspace.id, Project.owner_id == workspace.owner_id,
        Project.deleted_at.is_(None),
    ))
    if project is not None:
        soft_delete_project(db, project)
    else:
        ensure_scope_idle(db, workspace.owner_id, workspace.id)
        workspace.deleted_at = utcnow()
