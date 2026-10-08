"""Daily cleanup for projects that have remained in the trash for a month."""
from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..platform.database import LockSessionLocal, SessionLocal
from ..platform.models import Project, User, utcnow
from ..platform.storage import lock_storage_migration, safe_project_workspace_path, storage_migration
from .projects import add_calendar_month, as_utc, permanently_delete_project

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
        if add_calendar_month(deleted_at) > now:
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
    return purged
