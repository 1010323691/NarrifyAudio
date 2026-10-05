"""Conservative retention for platform-owned resource snapshots and exports."""
from __future__ import annotations

import json
import logging
import time

from sqlalchemy import select

from ..core.safe_filesystem import is_link_or_junction
from .database import SessionLocal
from .models import Task, TaskResult, User
from .resource_inventory import EXPORT_RETENTION_SECONDS, ResourceError, internal_path
from .storage import lock_storage_migration, storage_migration
from .task_lifecycle import ACTIVE_TASK_STATUSES

logger = logging.getLogger(__name__)


def purge_resource_artifacts() -> int:
    """Remove only known platform files; retry failures on the next Worker pass."""
    deleted = 0
    now = time.time()
    with SessionLocal() as db:
        if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
            return 0
        rows = db.execute(select(Task, TaskResult).join(TaskResult, TaskResult.task_id == Task.id).where(
            Task.task_type == "resources.package", Task.status == "succeeded",
        )).all()
        for task, record in rows:
            from datetime import datetime

            try:
                expiry = datetime.fromisoformat(record.result["expires_at"]).timestamp()
                if expiry > now:
                    continue
                path = internal_path(db, task.owner_id, "exports", task.id, "files.zip")
                if path.is_file():
                    path.unlink()
                    deleted += 1
            except (ResourceError, OSError, ValueError, KeyError):
                logger.warning("Could not purge resource export task=%s", task.id)
        for user in db.scalars(select(User)).all():
            try:
                protected = set()
                for active in db.scalars(select(Task).where(Task.owner_id == user.id, Task.status.in_(ACTIVE_TASK_STATUSES), Task.task_type.in_(["resources.package", "resources.cleanup"]))).all():
                    payload = active.payload
                    for reference in payload.get("snapshots") or (payload.get("scope") or {}).get("snapshots") or []:
                        protected.add((reference["project_id"], reference["snapshot_id"]))
                    for reference in payload.get("files") or []:
                        protected.add((reference["resource_id"].split(":", 1)[0], reference["snapshot_id"]))
                root = internal_path(db, user.id)
                if not root.is_dir():
                    continue
                for directory in root.iterdir():
                    if directory.name in {"attempts", "exports"} or not directory.is_dir() or is_link_or_junction(directory):
                        continue
                    pointer = directory / "current.json"
                    if is_link_or_junction(pointer):
                        continue
                    current = json.loads(pointer.read_text(encoding="utf-8")).get("snapshot_id") if pointer.is_file() else None
                    for snapshot in directory.glob("*.sqlite"):
                        if is_link_or_junction(snapshot) or snapshot.stem == current or (directory.name, snapshot.stem) in protected:
                            continue
                        # Old snapshots remain available for frozen exports.
                        if snapshot.stat().st_mtime < now - EXPORT_RETENTION_SECONDS:
                            snapshot.unlink()
                            deleted += 1
            except (ResourceError, OSError, ValueError):
                logger.warning("Could not purge resource indexes owner=%s", user.id)
    return deleted
