"""Shared lifecycle rules for user-owned projects and their storage directories."""
from __future__ import annotations

import calendar
import os
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..platform.models import (
    OutboxEvent, Project, ProjectFile, QuotaHold, QuotaTransaction, Task,
    TaskAttempt, TaskEvent, TaskResult, UserSession, WorkerHeartbeat, new_id,
)
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES


class ActiveProjectTasksError(ValueError):
    pass


def _remove_readonly(func, path, _exc_info) -> None:
    """Allow managed project cleanup to remove read-only Windows files."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


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


def add_calendar_month(value: datetime) -> datetime:
    """Return the same time one calendar month later, clamping the day."""
    month = value.month % 12 + 1
    year = value.year + (1 if value.month == 12 else 0)
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def move_project_to_trash(db: Session, project: Project) -> None:
    """Hide a project while retaining its files and database records for recovery."""
    ensure_project_idle(db, project)
    project.deleted_at = datetime.now(timezone.utc)
    db.execute(update(UserSession).where(UserSession.active_project_id == project.id).values(active_project_id=None))


def restore_project(db: Session, project: Project) -> None:
    """Restore a trashed project before its one-calendar-month expiry."""
    if project.deleted_at is None or add_calendar_month(as_utc(project.deleted_at)) <= datetime.now(timezone.utc):
        raise ValueError("项目已超过回收期限")
    duplicate = db.scalar(select(Project.id).where(
        Project.owner_id == project.owner_id,
        Project.name == project.name,
        Project.id != project.id,
        Project.deleted_at.is_(None),
    ).limit(1))
    if duplicate is not None:
        base_name = project.name
        number = 1
        while True:
            suffix = "（恢复）" if number == 1 else f"（恢复 {number}）"
            candidate = f"{base_name[:160 - len(suffix)]}{suffix}"
            duplicate = db.scalar(select(Project.id).where(
                Project.owner_id == project.owner_id,
                Project.name == candidate,
                Project.deleted_at.is_(None),
            ).limit(1))
            if duplicate is None:
                project.name = candidate
                break
            number += 1
    project.deleted_at = None


def permanently_delete_project(db: Session, project: Project, workspace_path: Path) -> None:
    """Permanently remove expired project data and its managed workspace."""
    ensure_project_idle(db, project)
    staged_path: Path | None = None
    if workspace_path.is_symlink():
        raise OSError("Project workspace must not be a symlink")
    if workspace_path.exists():
        if not workspace_path.is_dir():
            raise OSError("Project workspace is not a directory")
        staged_path = workspace_path.with_name(f".{project.id}.deleting-{uuid.uuid4().hex}")
        workspace_path.rename(staged_path)

    task_ids = select(Task.id).where(Task.owner_id == project.owner_id, Task.project_id == project.id)
    try:
        db.execute(delete(QuotaHold).where(QuotaHold.task_id.in_(task_ids)))
        db.execute(delete(QuotaTransaction).where(QuotaTransaction.task_id.in_(task_ids)))
        db.execute(delete(OutboxEvent).where(
            OutboxEvent.aggregate_type == "task", OutboxEvent.aggregate_id.in_(task_ids),
        ))
        db.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.current_task_id.in_(task_ids)).values(current_task_id=None))
        db.execute(delete(TaskResult).where(TaskResult.task_id.in_(task_ids)))
        db.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(task_ids)))
        db.execute(delete(TaskAttempt).where(TaskAttempt.task_id.in_(task_ids)))
        db.execute(delete(Task).where(Task.id.in_(task_ids)))
        db.execute(delete(ProjectFile).where(
            ProjectFile.project_id == project.id, ProjectFile.owner_id == project.owner_id,
        ))
        db.execute(update(UserSession).where(UserSession.active_project_id == project.id).values(active_project_id=None))
        db.delete(project)
        db.flush()

        if staged_path is not None:
            shutil.rmtree(staged_path, onerror=_remove_readonly)
        db.commit()
    except Exception:
        db.rollback()
        if staged_path is not None and staged_path.exists() and not workspace_path.exists():
            staged_path.rename(workspace_path)
        raise
