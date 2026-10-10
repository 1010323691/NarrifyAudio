"""Next-day verification that a permanently deleted project left nothing behind.

Every permanent deletion writes a ``ProjectPurgeRecord`` receipt. On a later
daily pass this module re-checks the workspace, the staged ``.deleting-*``
copies, the platform-owned resource directories and every project-linked
table; anything found (for example files a still-running worker wrote after the
deletion) is removed and recorded on the receipt, and the receipt is marked
verified only once a re-check comes back clean."""
from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..platform.database import SessionLocal
from ..platform.models import (
    ChapterReviewMark, CurrentDelivery, DeliveryIndexState, Project, ProjectFile, ProjectProgress,
    ProjectProgressRefresh, ProjectPurgeRecord, Task, TaskBatch, TextFormatFlow, UserSession, utcnow,
)
from ..platform.resource_inventory import ResourceError, internal_path
from ..platform.storage import configured_storage_root, lock_storage_migration, storage_migration
from .projects import _remove_readonly, purge_project_rows

logger = logging.getLogger(__name__)
VERIFIED_RECORD_KEEP = timedelta(days=30)
_PROJECT_ID_TABLES = (
    ("projects", Project, Project.id), ("project_files", ProjectFile, ProjectFile.project_id),
    ("tasks", Task, Task.project_id), ("task_batches", TaskBatch, TaskBatch.project_id),
    ("text_format_flows", TextFormatFlow, TextFormatFlow.project_id),
    ("chapter_review_marks", ChapterReviewMark, ChapterReviewMark.project_id),
    ("project_progress", ProjectProgress, ProjectProgress.project_id),
    ("project_progress_refresh", ProjectProgressRefresh, ProjectProgressRefresh.project_id),
    ("current_deliveries", CurrentDelivery, CurrentDelivery.project_id),
    ("delivery_index_state", DeliveryIndexState, DeliveryIndexState.project_id),
    ("user_sessions.active_project_id", UserSession, UserSession.active_project_id),
)


def local_midnight(now: datetime | None = None) -> datetime:
    """Start of the current local day (the daily pass boundary)."""
    moment = (now or utcnow()).astimezone()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _key_in_use(db: Session, directory_key: str) -> bool:
    return db.scalar(select(Project.id).where(Project.directory_key == directory_key).limit(1)) is not None


def _candidate_paths(db: Session, record: ProjectPurgeRecord) -> list[Path]:
    root = configured_storage_root(db)
    workspace = root / record.directory_key
    # Directory keys are name-based: a project created (or trashed) later may now own this very
    # directory, and it must never be mistaken for the deleted project's leftovers.
    paths = [] if _key_in_use(db, record.directory_key) else [workspace]
    paths += workspace.parent.glob(f".{record.project_id}.deleting-*")
    try:
        paths.append(internal_path(db, record.owner_id, record.project_id))
        paths += [internal_path(db, record.owner_id, "exports", task_id) for task_id in record.export_task_ids or []]
    except ResourceError:
        logger.warning("Resource storage not safely accessible while verifying project %s", record.project_id)
    return [path for path in paths if path.is_symlink() or path.exists()]


def find_traces(db: Session, record: ProjectPurgeRecord) -> dict:
    """Everything still attributable to the deleted project: {"paths": [...], "rows": {table: count}}."""
    rows = {}
    for name, model, column in _PROJECT_ID_TABLES:
        count = db.scalar(select(func.count()).select_from(model).where(column == record.project_id)) or 0
        if count:
            rows[name] = int(count)
    return {"paths": [str(path) for path in _candidate_paths(db, record)], "rows": rows}


def _clear_traces(db: Session, record: ProjectPurgeRecord, traces: dict) -> None:
    root = configured_storage_root(db).resolve()
    workspace = str(configured_storage_root(db) / record.directory_key)
    for raw in traces["paths"]:
        path = Path(raw)
        if raw == workspace and _key_in_use(db, record.directory_key):
            continue   # a project claimed this directory since the scan
        if not path.parent.resolve().is_relative_to(root):
            raise OSError(f"Refusing to remove a path outside the storage root: {path}")
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path, onerror=_remove_readonly)
    if traces["rows"]:
        purge_project_rows(db, record.owner_id, record.project_id)
        db.execute(delete(Project).where(Project.id == record.project_id))
        db.commit()


def verify_purged_projects(limit: int = 50, *, now: datetime | None = None) -> int:
    """Verify receipts from before today; return how many were handled (bounded round)."""
    moment = now or utcnow()
    boundary = local_midnight(moment).astimezone(timezone.utc)  # compare in UTC (sqlite stores naive UTC)
    handled = 0
    with SessionLocal() as db:
        if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
            return 0
        records = db.scalars(select(ProjectPurgeRecord).where(
            ProjectPurgeRecord.verified_at.is_(None), ProjectPurgeRecord.purged_at < boundary,
        ).order_by(ProjectPurgeRecord.purged_at).limit(limit)).all()
        for record in records:
            handled += 1
            record.attempts += 1
            traces = find_traces(db, record)
            if traces["paths"] or traces["rows"]:
                logger.warning("Deleted project %s left traces behind: %s; removing them", record.project_id, traces)
                try:
                    _clear_traces(db, record, traces)
                except Exception:
                    db.rollback()
                    logger.exception("Could not clean traces of deleted project %s; will retry tomorrow", record.project_id)
                    record = db.merge(record)
                    record.leftovers = traces
                    db.commit()
                    continue
                record = db.merge(record)
                record.leftovers = traces
                traces = find_traces(db, record)
            if not (traces["paths"] or traces["rows"]):
                record.verified_at = moment
            db.commit()
        db.execute(delete(ProjectPurgeRecord).where(
            ProjectPurgeRecord.verified_at.is_not(None), ProjectPurgeRecord.verified_at < moment - VERIFIED_RECORD_KEEP,
        ))
        db.commit()
    return handled
