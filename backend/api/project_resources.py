from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_csrf, require_authenticated_user
from ..platform.models import Project, Task, User
from ..platform.storage import safe_project_workspace_path
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES
from ..services.project_filesystem import iter_regular_project_files
from ..services.project_completion import COMPLETION_SECTIONS
from ..services.project_progress import progress_summary
from ..services.task_operations import owned_project

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _owned(db: Session, user: User, project_id: str) -> Project:
    item = owned_project(db, user.id, project_id)
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
_PROGRESS_CATEGORIES = (
    "02_split_text", "03_parsed_json", "04_voice_profiles", "05_audio_chunk",
    "06_audio_merge", "07_output", "08_bgm",
)


def _safe_project_directory(db: Session, user: User, item: Project) -> Path | None:
    """Resolve a managed project directory without following user-controlled symlinks."""
    return safe_project_workspace_path(db, user.username, item.id)


def _project_summary(root: Path, *, active: bool) -> dict:
    """Collect full file metadata for the project detail view."""
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


def _stage_has_file(stage_root: Path, *, count_split_volumes: bool = False) -> tuple[bool, int]:
    """Find stage output without statting and resolving every project file."""
    if stage_root.is_symlink() or not stage_root.is_dir():
        return False, 0
    resolved_root = stage_root.resolve()
    pending = [stage_root]
    found = False
    split_volume_count = 0
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        child = Path(entry.path)
                        if not child.is_symlink() and child.resolve().is_relative_to(resolved_root):
                            pending.append(child)
                    elif entry.is_file(follow_symlinks=False):
                        if count_split_volumes:
                            if _SPLIT_VOLUME_NAME.match(entry.name):
                                split_volume_count += 1
                                found = True
                        else:
                            return True, 0
                except (OSError, RuntimeError, ValueError):
                    continue
    return found, split_volume_count


def _project_progress(root: Path) -> dict:
    """Return only stage presence for project cards using short-circuit scans."""
    if root.is_symlink() or not root.is_dir():
        return {"stage_keys": [], "split_volume_count": 0}
    stage_keys = []
    split_volume_count = 0
    for key in _PROGRESS_CATEGORIES:
        has_output, split_count = _stage_has_file(
            root / key, count_split_volumes=(key == "02_split_text"),
        )
        if has_output:
            stage_keys.append(key)
        if key == "02_split_text":
            split_volume_count = split_count
    return {"stage_keys": stage_keys, "split_volume_count": split_volume_count}


@router.get("/{project_id}/summary")
def get_project_summary(project_id: str, progress: bool = False, section: str | None = None, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, project_id)
    root = _safe_project_directory(db, user, item)
    if root is None:
        raise HTTPException(409, "项目存储目录不可安全访问")
    if section is not None and (not progress or section not in COMPLETION_SECTIONS):
        raise HTTPException(422, "无效的进度区域")
    if progress:
        return {
            "project_id": item.id,
            "name": item.name,
            "updated_at": item.updated_at.isoformat(),
            "stage_keys": [], "split_volume_count": 0,
            "stage_completion": progress_summary(db, user, item.id, root, section),
        }
    active = db.scalar(select(Task.id).where(
        Task.owner_id == user.id, Task.project_id == item.id, Task.status.in_(ACTIVE_TASK_STATUSES)
    ).limit(1)) is not None
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
