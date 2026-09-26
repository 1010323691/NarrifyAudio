"""Daily cleanup for projects that have remained in the trash for a month."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, text
from starlette.concurrency import run_in_threadpool

from ..platform.database import LockSessionLocal, SessionLocal
from ..platform.models import Project, User
from ..platform.storage import lock_storage_migration, safe_project_workspace_path, storage_migration
from .projects import add_calendar_month, as_utc, permanently_delete_project

logger = logging.getLogger(__name__)
_RETENTION_LOCK_ID = 7526202610


def purge_expired_projects() -> int:
    """Purge every safely accessible project whose calendar-month retention expired."""
    with SessionLocal() as db:
        if db.get_bind().dialect.name == "postgresql":
            with LockSessionLocal() as lock_db:
                if not lock_storage_migration(lock_db, shared=True) or storage_migration(lock_db) is not None:
                    return 0
                if not lock_db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _RETENTION_LOCK_ID}):
                    return 0
                return _purge_expired_with_session(db)

        if storage_migration(db) is not None:
            return 0
        return _purge_expired_with_session(db)


def _purge_expired_with_session(db) -> int:
    purged = 0
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(Project.id, User.username)
        .join(User, User.id == Project.owner_id)
        .where(Project.deleted_at.is_not(None))
        .order_by(Project.deleted_at.asc())
    ).all()
    for project_id, username in rows:
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


async def project_retention_loop() -> None:
    """Check on startup and then once per day for expired trash entries."""
    while True:
        try:
            await run_in_threadpool(purge_expired_projects)
        except Exception:
            logger.exception("Daily project trash cleanup failed")
        await asyncio.sleep(24 * 60 * 60)
