from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_csrf, require_user
from ..platform.models import Project, Task, User, Workspace, new_id, utcnow
from ..platform.storage import configured_storage_root, safe_display_name, user_workspace_root

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


def _json(item: Workspace) -> dict:
    return {"id": item.id, "name": item.name, "directory_key": item.directory_key, "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat()}


def _owned(db: Session, user: User, workspace_id: str) -> Workspace:
    item = db.scalar(select(Workspace).where(Workspace.id == workspace_id, Workspace.owner_id == user.id, Workspace.deleted_at.is_(None)))
    if item is None:
        raise HTTPException(404, "工作空间不存在")
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
_ACTIVE_TASKS = ("pending", "queued", "running", "paused", "cancelling", "retrying")
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".zip"}
_SPLIT_VOLUME_NAME = re.compile(r"^第\s+\d+\s+章(?:\s|\.|$)")


def _safe_user_workspace(db: Session, user: User, item: Workspace) -> Path | None:
    """Resolve a managed workspace without following user-controlled symlinks."""
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


def _workspace_summary(root: Path, *, active: bool) -> dict:
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

    for directory, child_dirs, filenames in os.walk(root, topdown=True, followlinks=False):
        parent = Path(directory)
        child_dirs[:] = [name for name in child_dirs if not (parent / name).is_symlink()]
        for filename in filenames:
            path = parent / filename
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
                relative = path.relative_to(root)
            except (OSError, ValueError):
                continue
            category = relative.parts[0] if relative.parts else "other"
            if category not in _CATEGORY_LABELS:
                category = "other"
            bucket = totals.setdefault(category, {"count": 0, "size_bytes": 0})
            bucket["count"] += 1
            bucket["size_bytes"] += stat.st_size
            if category == "02_split_text" and _SPLIT_VOLUME_NAME.match(filename):
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


@router.get("")
def list_workspaces(user: User = Depends(require_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Workspace).where(Workspace.owner_id == user.id, Workspace.deleted_at.is_(None)).order_by(Workspace.updated_at.desc())).all()
    return [_json(item) for item in rows]


@router.post("", status_code=201)
def create_workspace(payload: WorkspaceCreate, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    name = payload.name.strip()
    if db.scalar(select(Workspace).where(Workspace.owner_id == user.id, Workspace.name == name, Workspace.deleted_at.is_(None))) is not None:
        raise HTTPException(409, "工作空间名称已存在")
    workspace_id = new_id()
    item = Workspace(id=workspace_id, owner_id=user.id, name=name, directory_key=f"{user.username}/{workspace_id}")
    db.add(item)
    db.add(Project(id=workspace_id, owner_id=user.id, name=name, description="工作空间对应项目"))
    try:
        user_workspace_root(db, user.username, workspace_id).mkdir(parents=True, exist_ok=True)
        db.commit()
    except OSError as exc:
        db.rollback()
        raise HTTPException(422, f"无法创建工作空间目录：{exc}") from exc
    return _json(item)


@router.delete("/{workspace_id}")
def delete_workspace(workspace_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, workspace_id)
    active_task = db.scalar(select(Task.id).where(
        Task.owner_id == user.id,
        Task.project_id == item.id,
        Task.status.in_(_ACTIVE_TASKS),
    ).limit(1))
    if active_task is not None:
        raise HTTPException(409, "项目仍有未完成任务，请先等待任务完成或取消任务后再删除。")

    item.deleted_at = utcnow()
    project = db.scalar(select(Project).where(
        Project.id == item.id,
        Project.owner_id == user.id,
        Project.deleted_at.is_(None),
    ))
    if project is not None:
        project.deleted_at = item.deleted_at
    db.commit()
    return {"ok": True, "directory_retained": True}


@router.get("/{workspace_id}/summary")
def get_workspace_summary(workspace_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, workspace_id)
    active = db.scalar(select(Task.id).where(
        Task.owner_id == user.id, Task.project_id == item.id, Task.status.in_(_ACTIVE_TASKS)
    ).limit(1)) is not None
    root = _safe_user_workspace(db, user, item)
    if root is None:
        raise HTTPException(409, "项目存储目录不可安全访问")
    return {
        "workspace_id": item.id,
        "name": item.name,
        "updated_at": item.updated_at.isoformat(),
        **_workspace_summary(root, active=active),
    }


@router.post("/{workspace_id}/cleanup-temp")
def cleanup_workspace_temp(workspace_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, workspace_id)
    active = db.scalar(select(Task.id).where(
        Task.owner_id == user.id, Task.project_id == item.id, Task.status.in_(_ACTIVE_TASKS)
    ).limit(1)) is not None
    if active:
        raise HTTPException(409, "项目仍有正在处理的任务，暂时不能清理临时文件")
    root = _safe_user_workspace(db, user, item)
    if root is None:
        raise HTTPException(409, "项目存储目录不可安全访问")
    cutoff = datetime.now().timestamp() - _CACHE_MAX_AGE.total_seconds()
    root_resolved = root.resolve()
    deleted_count = deleted_bytes = skipped_count = 0
    for cache_name in _CACHE_DIRS:
        cache_root = root / cache_name
        if cache_root.is_symlink() or not cache_root.is_dir():
            continue
        for directory, child_dirs, filenames in os.walk(cache_root, topdown=True, followlinks=False):
            parent = Path(directory)
            child_dirs[:] = [name for name in child_dirs if not (parent / name).is_symlink()]
            for filename in filenames:
                path = parent / filename
                if path.is_symlink():
                    continue
                try:
                    resolved = path.resolve()
                    stat = path.stat()
                    if not resolved.is_relative_to(root_resolved) or stat.st_mtime >= cutoff:
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
