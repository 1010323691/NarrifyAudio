from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_csrf, require_authenticated_user
from ..platform.models import Project, Task, User
from ..platform.storage import configured_storage_root, safe_display_name
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from ..services.project_filesystem import iter_regular_project_files

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _owned(db: Session, user: User, project_id: str) -> Project:
    item = db.scalar(select(Project).where(Project.id == project_id, Project.owner_id == user.id, Project.deleted_at.is_(None)))
    if item is None:
        raise HTTPException(404, "项目不存在")
    return item


_CATEGORY_LABELS = {
    "00_temp": "临时缓存",
    "01_input": "原始文件",
    "02_split_text": "章节文本",
    "03_parsed_json": "解析结果",
    "04_voice_profiles": "角色资料",
    "05_audio_chunk": "合成音频",
    "06_audio_merge": "合并音频",
    "07_output": "最终成品",
    "08_bgm": "背景音乐",
    "config": "项目配置",
    "logs": "项目日志",
    "other": "其他文件",
}
_CACHE_DIRS = {"00_temp", ".cache", "cache"}
_CACHE_MAX_AGE = timedelta(days=7)
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".zip"}
_SPLIT_VOLUME_NAME = re.compile(r"^第\s+\d+\s+章(?:\s|\.|$)")


def _safe_project_directory(db: Session, user: User, item: Project) -> Path | None:
    """Resolve a managed project directory without following user-controlled symlinks."""
    root = configured_storage_root(db).resolve()
    user_root = root / safe_display_name(user.username)
    candidate = user_root / item.id
    if user_root.is_symlink() or candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            return None
    except (OSError, RuntimeError):
        return None
    return resolved


def _project_summary(root: Path, *, active: bool) -> dict:
    """Collect file metadata only; never follows links or reads file contents."""
    totals: dict[str, dict[str, int]] = {}
    files: list[dict] = []
    cleanup_count = cleanup_bytes = split_volume_count = 0
    cutoff = datetime.now().timestamp() - _CACHE_MAX_AGE.total_seconds()
    if not root.is_dir() or root.is_symlink():
        return {
            "file_count": 0, "size_bytes": 0, "split_volume_count": 0,
            "categories": [], "recent_files": [],
            "recent_outputs": [],
            "cleanup_candidates": {"count": 0, "size_bytes": 0, "older_than_days": 7,
                                   "blocked_by_active_tasks": active},
        }

    for path, relative, stat in iter_regular_project_files(root):
        category = relative.parts[0] if relative.parts else "other"
        if category not in _CATEGORY_LABELS:
            category = "other"
        bucket = totals.setdefault(category, {"count": 0, "size_bytes": 0})
        bucket["count"] += 1
        bucket["size_bytes"] += stat.st_size
        if category == "02_split_text" and _SPLIT_VOLUME_NAME.match(path.name):
            split_volume_count += 1
        if not active and relative.parts and relative.parts[0] in _CACHE_DIRS and stat.st_mtime < cutoff:
            cleanup_count += 1
            cleanup_bytes += stat.st_size
        files.append({
            "name": path.name,
            "relative_path": relative.as_posix(),
            "module": category,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        })

    files.sort(key=lambda row: row["modified_at"], reverse=True)
    outputs = [
        row for row in files
        if Path(row["name"]).suffix.lower() in _AUDIO_EXTENSIONS
        and row["module"] in {"05_audio_chunk", "06_audio_merge", "07_output", "08_bgm"}
    ]
    return {
        "file_count": sum(bucket["count"] for bucket in totals.values()),
        "size_bytes": sum(bucket["size_bytes"] for bucket in totals.values()),
        "split_volume_count": split_volume_count,
        "categories": [
            {"key": key, "label": _CATEGORY_LABELS[key], **value}
            for key, value in sorted(totals.items(), key=lambda pair: pair[1]["size_bytes"], reverse=True)
        ],
        "recent_files": files[:100],
        "recent_outputs": outputs[:12],
        "cleanup_candidates": {
            "count": cleanup_count, "size_bytes": cleanup_bytes, "older_than_days": 7,
            "blocked_by_active_tasks": active,
        },
    }


@router.get("/{project_id}/summary")
def get_project_summary(project_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, project_id)
    active = db.scalar(select(Task.id).where(
        Task.owner_id == user.id, Task.project_id == item.id, Task.status.in_(ACTIVE_TASK_STATUSES)
    ).limit(1)) is not None
    root = _safe_project_directory(db, user, item)
    if root is None:
        raise HTTPException(409, "项目存储目录不可安全访问")
    return {
        "project_id": item.id,
        "name": item.name,
        "updated_at": item.updated_at.isoformat(),
        **_project_summary(root, active=active),
    }


@router.post("/{project_id}/cleanup-temp")
def cleanup_project_temp(project_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, project_id)
    active = db.scalar(select(Task.id).where(
        Task.owner_id == user.id, Task.project_id == item.id, Task.status.in_(ACTIVE_TASK_STATUSES)
    ).limit(1)) is not None
    if active:
        raise HTTPException(409, "项目仍有正在处理的任务，暂时不能清理临时文件")
    root = _safe_project_directory(db, user, item)
    if root is None:
        raise HTTPException(409, "项目存储目录不可安全访问")
    cutoff = datetime.now().timestamp() - _CACHE_MAX_AGE.total_seconds()
    root_resolved = root.resolve()
    deleted_count = deleted_bytes = skipped_count = 0
    for cache_name in _CACHE_DIRS:
        cache_root = root / cache_name
        if cache_root.is_symlink() or not cache_root.is_dir():
            continue
        for path, _relative, stat in iter_regular_project_files(cache_root):
            try:
                if not path.resolve().is_relative_to(root_resolved) or stat.st_mtime >= cutoff:
                    continue
                path.unlink()
                deleted_count += 1
                deleted_bytes += stat.st_size
            except OSError:
                skipped_count += 1
    return {
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "skipped_count": skipped_count,
        "older_than_days": 7,
    }
