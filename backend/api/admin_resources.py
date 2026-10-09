"""Administrative project-storage inventory and cleanup endpoints."""
from __future__ import annotations

from datetime import datetime
import shutil

from fastapi import APIRouter, Depends, Query
from typing import Annotated
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.paths import MUSIC_LIBRARY_DIR
from ..platform.database import get_db
from ..platform.deps import require_admin, require_admin_csrf
from ..platform.models import AuditLog, Project, ProjectFile, Task, User
from ..platform.storage import configured_storage_root
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from ..services.project_filesystem import iter_regular_project_files
from ..services.admin_storage import (
    TEMP_CLEANUP_AGE_DAYS,
    PROJECT_CATEGORY_LABELS,
    music_use_counts,
    scan_project_directory,
    project_storage_path,
)

from ..services.admin_lists import resource_page, user_usage_page
from ..services.list_paging import page_meta

router = APIRouter(prefix="/api/v1/admin", tags=["admin-resources"])


@router.get("/resources")
def resources(_: User = Depends(require_admin), db: Session = Depends(get_db), light: bool = False, users_only: bool = False,
              page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 10) -> dict:
    root = configured_storage_root(db)
    disk = shutil.disk_usage(root)
    if light: return resource_page(db, root, disk, MUSIC_LIBRARY_DIR, page, page_size)
    # The full inventory pages its user table too: `users_only` re-scans just this page's users (page flips are cheap),
    # while the first full request also scans everything for the aggregate cards.
    page_rows, user_total = user_usage_page(db, page, page_size)
    page_usernames = [row[0] for row in page_rows]
    file_rows = db.execute(select(ProjectFile.kind, func.count(), func.coalesce(func.sum(ProjectFile.size_bytes), 0)).where(ProjectFile.deleted_at.is_(None)).group_by(ProjectFile.kind)).all()
    registered = {
        username: {"count": int(count), "size_bytes": int(size)}
        for username, count, size in db.execute(
            select(User.username, func.count(ProjectFile.id), func.coalesce(func.sum(ProjectFile.size_bytes), 0))
            .join(ProjectFile, ProjectFile.owner_id == User.id)
            .where(ProjectFile.deleted_at.is_(None)).group_by(User.username)
        ).all()
    }
    project_query = select(Project, User.username).join(User, User.id == Project.owner_id).where(Project.deleted_at.is_(None))
    if users_only:
        project_query = project_query.where(User.username.in_(page_usernames))
    projects = db.execute(project_query).all()
    active_project_ids = set(db.scalars(
        select(Task.project_id).where(Task.status.in_(ACTIVE_TASK_STATUSES)).distinct()
    ).all())
    users: dict[str, dict] = {}
    category_totals: dict[str, dict[str, int]] = {}
    cleanup_count = cleanup_bytes = 0
    for project, username in projects:
        row = users.setdefault(username, {"project_count": 0, "size_bytes": 0, "file_count": 0})
        row["project_count"] += 1
        path = project_storage_path(root, username, project.directory_key)
        measured = scan_project_directory(path, active=project.id in active_project_ids) if path is not None else {
            "size_bytes": 0, "file_count": 0, "categories": {}, "cleanup_count": 0, "cleanup_bytes": 0,
        }
        row["size_bytes"] += measured["size_bytes"]
        row["file_count"] += measured["file_count"]
        cleanup_count += measured["cleanup_count"]
        cleanup_bytes += measured["cleanup_bytes"]
        for category, values in measured["categories"].items():
            total = category_totals.setdefault(category, {"count": 0, "size_bytes": 0})
            total["count"] += values["count"]
            total["size_bytes"] += values["size_bytes"]
    empty = {"project_count": 0, "size_bytes": 0, "file_count": 0}
    user_rows = [
        {"username": username, **users.get(username, empty), "registered_file_count": registered.get(username, {}).get("count", 0),
         "registered_file_bytes": registered.get(username, {}).get("size_bytes", 0)}
        for username in page_usernames
    ]
    if users_only:
        return {"users": user_rows, "pagination": page_meta(user_total, page, page_size)}
    music_usage = music_use_counts(projects, root)
    music_files = [path for path in MUSIC_LIBRARY_DIR.iterdir() if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".mp3", ".wav", ".flac"}] if MUSIC_LIBRARY_DIR.is_dir() else []
    return {
        "root_path": str(root), "disk_total_bytes": disk.total, "disk_used_bytes": disk.used,
        "disk_free_bytes": disk.free,
        "projects": int(db.scalar(select(func.count()).select_from(Project).where(Project.deleted_at.is_(None))) or 0),
        "files": [{"kind": kind, "count": count, "size_bytes": size} for kind, count, size in file_rows],
        "users": user_rows, "pagination": page_meta(user_total, page, page_size),
        "project_storage": {
            "size_bytes": sum(row["size_bytes"] for row in users.values()),
            "file_count": sum(row["file_count"] for row in users.values()),
            "categories": [
                {"kind": key, "label": PROJECT_CATEGORY_LABELS[key], **values}
                for key, values in sorted(category_totals.items(), key=lambda item: item[1]["size_bytes"], reverse=True)
            ],
            "cleanup_candidates": {"count": cleanup_count, "size_bytes": cleanup_bytes,
                                   "older_than_days": TEMP_CLEANUP_AGE_DAYS},
        },
        "music_library": {"count": len(music_files), "size_bytes": sum(path.stat().st_size for path in music_files),
                          "assigned_chapters": sum(music_usage.values())},
        "scope": "项目目录按只读文件扫描（跳过符号链接）；用户的缓存、临时文件、音频、日志均计入。模型位于项目目录之外时不计入此处。",
    }


@router.post("/resources/cleanup-temp")
def cleanup_stale_temp(actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
    root = configured_storage_root(db)
    projects = db.execute(
        select(Project, User.username).join(User, User.id == Project.owner_id)
        .where(Project.deleted_at.is_(None))
    ).all()
    active_project_ids = set(db.scalars(
        select(Task.project_id).where(Task.status.in_(ACTIVE_TASK_STATUSES)).distinct()
    ).all())
    cutoff = datetime.now().timestamp() - TEMP_CLEANUP_AGE_DAYS * 24 * 60 * 60
    removed_count = removed_bytes = skipped = 0
    root_resolved = root.resolve()
    for project, username in projects:
        if project.id in active_project_ids:
            continue
        project_path = project_storage_path(root, username, project.directory_key)
        if project_path is None:
            continue
        temp_dir = project_path / "00_temp"
        if temp_dir.is_symlink() or not temp_dir.is_dir():
            continue
        for path, _relative, stat in iter_regular_project_files(temp_dir):
            try:
                if not path.resolve().is_relative_to(root_resolved) or stat.st_mtime >= cutoff:
                    continue
                path.unlink()
                removed_count += 1
                removed_bytes += stat.st_size
            except OSError:
                skipped += 1
    db.add(AuditLog(actor_user_id=actor.id, action="admin.temp_cleanup", target_type="storage",
                    target_id=str(root), metadata_json={"files": removed_count, "bytes": removed_bytes,
                                                       "age_days": TEMP_CLEANUP_AGE_DAYS, "skipped": skipped}))
    db.commit()
    return {"deleted_count": removed_count, "deleted_bytes": removed_bytes, "skipped_count": skipped,
            "older_than_days": TEMP_CLEANUP_AGE_DAYS}
