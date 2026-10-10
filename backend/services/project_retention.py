"""Cleanup of projects past their administrator-set lifetime.

Two deadlines, both configured in the admin console (``retention.projects``):
trashed projects are purged ``trash_days`` after deletion, and live projects
are purged ``project_ttl_days`` after creation (0 = never)."""
from __future__ import annotations

import logging

from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..platform.database import LockSessionLocal, SessionLocal
from ..platform.models import Project, Task, User, utcnow
from ..platform.storage import lock_storage_migration, safe_project_workspace_path, storage_migration
from ..platform.system_config import project_retention
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from .projects import as_utc, permanently_delete_project, trash_expires_at

DEFAULT_WORKSPACE_NAME = "默认工作空间"  # provisioned for every account; never expires on its own

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
    retention = project_retention(db)
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
        if trash_expires_at(deleted_at, retention["trash_days"]) > now:
            continue
        workspace_path = safe_project_workspace_path(db, username, project.id)
        if workspace_path is None:
            logger.error("Skipping expired project with unsafe storage path: %s", project.id)
            continue
        try:
            permanently_delete_project(db, project, workspace_path)
            purged += 1
        except Exception:
            db.rollback()
            logger.exception("Failed to purge expired project: %s", project.id)
    if purged < limit:
        purged += _purge_naturally_expired(db, limit - purged, now, retention["project_ttl_days"])
    return purged


def _purge_naturally_expired(db: Session, limit: int, now, ttl_days: int) -> int:
    """Permanently delete live projects older than ``ttl_days`` (idle ones only)."""
    if ttl_days <= 0:
        return 0
    cutoff = now - timedelta(days=ttl_days)
    rows = db.execute(
        select(Project.id, User.username)
        .join(User, User.id == Project.owner_id)
        .where(Project.deleted_at.is_(None), Project.created_at < cutoff, Project.name != DEFAULT_WORKSPACE_NAME)
        .order_by(Project.created_at.asc())
        .limit(limit * 3)  # busy projects are skipped, so look past them
    ).all()
    purged = 0
    for project_id, username in rows:
        if purged >= limit:
            break
        project = db.scalar(select(Project).where(
            Project.id == project_id, Project.deleted_at.is_(None),
        ).with_for_update(skip_locked=True))
        if project is None:
            continue
        busy = db.scalar(select(Task.id).where(
            Task.owner_id == project.owner_id, Task.project_id == project.id, Task.status.in_(ACTIVE_TASK_STATUSES),
        ).limit(1))
        if busy is not None:
            db.rollback()  # release the row lock; retried on a later pass
            continue
        workspace_path = safe_project_workspace_path(db, username, project.id)
        if workspace_path is None:
            logger.error("Skipping expired project with unsafe storage path: %s", project.id)
            db.rollback()
            continue
        try:
            permanently_delete_project(db, project, workspace_path)
            purged += 1
            logger.info("Purged expired project %s (older than %s days)", project.id, ttl_days)
        except Exception:
            db.rollback()
            logger.exception("Failed to purge expired project: %s", project.id)
    return purged
