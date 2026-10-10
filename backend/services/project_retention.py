"""Cleanup of projects past their administrator-set lifetime.

Two deadlines, both configured in the admin console (``retention.projects``):
trashed projects are purged ``trash_days`` after deletion, and live projects
are purged ``project_ttl_days`` after creation (0 = never)."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..platform.database import LockSessionLocal, SessionLocal
from ..platform.models import Project, Task, User, utcnow
from ..platform.storage import lock_storage_migration, safe_project_workspace_path, storage_migration
from ..platform.system_config import project_retention, retention_started_at
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from .projects import DEFAULT_WORKSPACE_NAME, as_utc, permanently_delete_project, trash_expires_at
from .task_operations import cancel_task_record


logger = logging.getLogger(__name__)
_RETENTION_LOCK_ID = 7526202610


def purge_expired_projects(limit: int = 20) -> int:
    """Purge up to ``limit`` safely accessible projects whose retention expired.

    The bound is the maintenance coordinator's single-round work budget: a
    large trash cannot delay task dispatch for hours, and a round that hits
    the budget continues on the coordinator's next pass (rows that fail to
    purge stay in place and keep their ordering)."""
    if not 1 <= limit <= 100:
        raise ValueError("purge budget must contain 1..100 projects")
    with SessionLocal() as db:
        if db.get_bind().dialect.name == "postgresql":
            with LockSessionLocal() as lock_db:
                if not lock_storage_migration(lock_db, shared=True) or storage_migration(lock_db) is not None:
                    return 0
                if not lock_db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _RETENTION_LOCK_ID}):
                    return 0
                return _purge_expired_with_session(db, limit)

        if storage_migration(db) is not None:
            return 0
        return _purge_expired_with_session(db, limit)


def _purge_expired_with_session(db: Session, limit: int) -> int:
    purged = 0
    now = utcnow()
    retention, floor = project_retention(db), retention_started_at(db)
    rows = db.execute(
        select(Project.id, User.username)
        .join(User, User.id == Project.owner_id)
        .where(Project.deleted_at.is_not(None))
        .order_by(Project.deleted_at.asc())
        .limit(limit + 1)
    ).all()
    for project_id, username in rows[:limit]:
        project = db.scalar(select(Project).where(
            Project.id == project_id, Project.deleted_at.is_not(None),
        ).with_for_update(skip_locked=True))
        if project is None:
            continue
        deleted_at = as_utc(project.deleted_at)
        if trash_expires_at(deleted_at, retention["trash_days"], floor) > now:
            continue
        workspace_path = safe_project_workspace_path(db, username, project.id)
        if workspace_path is None:
            logger.error("Skipping expired project with unsafe storage path: %s", project.id)
            continue
        try:
            permanently_delete_project(db, project, workspace_path, reason="trash")
            purged += 1
        except Exception:
            db.rollback()
            logger.exception("Failed to purge expired project: %s", project.id)
    if purged < limit:
        purged += _purge_naturally_expired(db, limit - purged, now, retention["project_ttl_days"], floor)
    return purged


CANCEL_GRACE_SECONDS = 30.0   # how long running tasks get to notice their cancel before the project is deleted
_CANCEL_POLL_SECONDS = 1.0
_RUNNING_STATES = ("running", "cancelling")


def _cancel_unfinished(db: Session, project: Project) -> int:
    running = db.scalars(select(Task).where(
        Task.owner_id == project.owner_id, Task.project_id == project.id,
        Task.status.in_(ACTIVE_TASK_STATUSES - {"cancelling"}),
    ).with_for_update()).all()
    for task in running:
        cancel_task_record(db, task)
    db.commit()
    return len(running)


def _wait_for_cancellation(db: Session, project_ids: list[str]) -> None:
    """Give running tasks a bounded grace period to stop; deletion proceeds regardless."""
    deadline = time.monotonic() + CANCEL_GRACE_SECONDS
    while time.monotonic() < deadline:
        db.rollback()
        if not db.scalar(select(Task.id).where(
            Task.project_id.in_(project_ids), Task.status.in_(_RUNNING_STATES),
        ).limit(1)):
            return
        time.sleep(_CANCEL_POLL_SECONDS)


def _purge_naturally_expired(db: Session, limit: int, now, ttl_days: int, floor=None) -> int:
    """Permanently delete live projects older than ``ttl_days``, even with unfinished tasks.

    Unfinished tasks are asked to cancel first, given a short grace to stop, and
    the project is then deleted regardless; the next day's verification
    (``project_purge_audit``) sweeps whatever a lingering worker wrote after that."""
    if ttl_days <= 0:
        return 0
    cutoff = now - timedelta(days=ttl_days)
    if floor is not None and floor >= cutoff:
        return 0   # projects are aged from no earlier than the retention start: nothing can be due yet
    rows = db.execute(
        select(Project.id, User.username)
        .join(User, User.id == Project.owner_id)
        .where(Project.deleted_at.is_(None), Project.created_at < cutoff, Project.name != DEFAULT_WORKSPACE_NAME)
        .order_by(Project.created_at.asc())
        .limit(limit)
    ).all()
    cancelled = 0
    for project_id, _username in rows:
        project = db.scalar(select(Project).where(Project.id == project_id, Project.deleted_at.is_(None)).with_for_update(skip_locked=True))
        if project is not None:
            cancelled += _cancel_unfinished(db, project)
    if cancelled:
        logger.info("Expired projects: cancelled %d unfinished tasks before deletion", cancelled)
        _wait_for_cancellation(db, [project_id for project_id, _ in rows])
    purged = 0
    for project_id, username in rows:
        project = db.scalar(select(Project).where(
            Project.id == project_id, Project.deleted_at.is_(None),
        ).with_for_update(skip_locked=True))
        if project is None:
            continue
        workspace_path = safe_project_workspace_path(db, username, project.id)
        if workspace_path is None:
            logger.error("Skipping expired project with unsafe storage path: %s", project.id)
            db.rollback()
            continue
        try:
            permanently_delete_project(db, project, workspace_path, force=True, reason="expired")
            purged += 1
            logger.info("Purged expired project %s (older than %s days)", project.id, ttl_days)
        except Exception:
            db.rollback()
            logger.exception("Failed to purge expired project: %s", project.id)
    return purged
