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
    ChapterReviewMark, OutboxEvent, Project, ProjectFile, QuotaHold, QuotaTransaction,
    Task, TaskAttempt, TaskEvent, TaskResult, TextFormatFlow, UserSession,
    WorkerHeartbeat, new_id, utcnow,
)
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from ..platform.workspace_layout import (
    ProjectDirectoryConflict, named_directory_key, relocate_project, ensure_project_directories,
)
from ..platform.storage import project_workspace_path


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
    key = named_directory_key(db, username, name)
    project_id = new_id()
    project = Project(id=project_id, owner_id=owner_id, name=name, description=description,
                      directory_key=key)
    db.add(project)
    db.flush()
    ensure_project_directories(project_workspace_path(db, username, project.id))
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
    key = named_directory_key(db, project.owner.username, name, project_id=project.id)
    if key != project.directory_key:
        relocate_project(db, project, key)
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
    number = 1
    while True:
        try:
            key = named_directory_key(db, project.owner.username, f"{project.name[:140]}（回收站 {number}）", project_id=project.id)
            break
        except ValueError:
            if number >= 1000:
                raise
            number += 1
    relocate_project(db, project, key)
    project.deleted_at = utcnow()
    db.execute(update(UserSession).where(UserSession.active_project_id == project.id).values(active_project_id=None))


def restore_project(db: Session, project: Project) -> None:
    """Restore a trashed project before its one-calendar-month expiry."""
    if project.deleted_at is None or add_calendar_month(as_utc(project.deleted_at)) <= utcnow():
        raise ValueError("项目已超过回收期限")
    occupied_names = {
        name.casefold() for name in db.scalars(select(Project.name).where(
            Project.owner_id == project.owner_id,
            Project.id != project.id,
            Project.deleted_at.is_(None),
        ))
    }
    base_name = project.name
    candidate = base_name
    number = 0
    while True:
        if candidate.casefold() not in occupied_names:
            try:
                key = named_directory_key(db, project.owner.username, candidate, project_id=project.id)
                break
            except ProjectDirectoryConflict:
                pass
        number += 1
        suffix = "（恢复）" if number == 1 else f"（恢复 {number}）"
        candidate = f"{base_name[:160 - len(suffix)]}{suffix}"
    project.name = candidate
    relocate_project(db, project, key)
    project.deleted_at = None


def permanently_delete_project(db: Session, project: Project, workspace_path: Path) -> None:
    """Permanently remove expired project data and its managed workspace.

    Failed or interrupted directory removals remain in a deterministic staging
    location. The expired project row stays in the trash so the next worker pass
    can resume cleanup; partially removed data is never moved back as if it were
    recoverable.
    """
    ensure_project_idle(db, project)
    if workspace_path.is_symlink():
        raise OSError("Project workspace must not be a symlink")
    staged_paths = list(workspace_path.parent.glob(f".{project.id}.deleting-*"))
    if workspace_path.exists():
        if not workspace_path.is_dir():
            raise OSError("Project workspace is not a directory")
        staged_path = workspace_path.with_name(f".{project.id}.deleting-{uuid.uuid4().hex}")
        workspace_path.rename(staged_path)
        staged_paths.append(staged_path)
    for staged_path in staged_paths:
        if staged_path.is_symlink() or not staged_path.is_dir():
            raise OSError("Staged project workspace is not a safe directory")

    task_ids = select(Task.id).where(Task.owner_id == project.owner_id, Task.project_id == project.id)
    try:
        from ..platform.models import CurrentDelivery, DeliveryIndexState, ProjectProgress, ProjectProgressRefresh
        db.execute(delete(ProjectProgressRefresh).where(ProjectProgressRefresh.project_id == project.id))
        db.execute(delete(ProjectProgress).where(ProjectProgress.project_id == project.id))
        db.execute(delete(CurrentDelivery).where(CurrentDelivery.project_id == project.id))
        db.execute(delete(DeliveryIndexState).where(DeliveryIndexState.project_id == project.id))
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
        from ..platform.models import TaskBatch
        db.execute(delete(TaskBatch).where(TaskBatch.owner_id == project.owner_id, TaskBatch.project_id == project.id))
        db.execute(delete(ProjectFile).where(
            ProjectFile.project_id == project.id, ProjectFile.owner_id == project.owner_id,
        ))
        # These workflow records point directly at the project and have no
        # database-level cascade. Remove them before deleting the project row.
        db.execute(delete(ChapterReviewMark).where(
            ChapterReviewMark.project_id == project.id, ChapterReviewMark.owner_id == project.owner_id,
        ))
        db.execute(delete(TextFormatFlow).where(
            TextFormatFlow.project_id == project.id, TextFormatFlow.owner_id == project.owner_id,
        ))
        db.execute(update(UserSession).where(UserSession.active_project_id == project.id).values(active_project_id=None))
        db.delete(project)
        db.flush()

        for staged_path in staged_paths:
            shutil.rmtree(staged_path, onerror=_remove_readonly)
        db.commit()
    except Exception:
        db.rollback()
        raise
