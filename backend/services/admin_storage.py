"""Filesystem inventory helpers shared by administrative API routes."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..platform.models import Project
from ..platform.storage import safe_display_name
from .project_filesystem import iter_regular_project_files


ACTIVE_TASK_STATUSES = ("pending", "queued", "running", "paused", "cancelling", "retrying")
TEMP_CLEANUP_AGE_DAYS = 7
PROJECT_CATEGORY_LABELS = {
    "00_temp": "临时文件",
    "cache": "缓存文件",
    ".cache": "缓存文件",
    "01_input": "输入文件",
    "02_split_text": "章节文本",
    "03_parsed_json": "解析结果",
    "04_voice_profiles": "角色资料",
    "05_audio_chunk": "合成片段",
    "06_audio_merge": "合并音频",
    "07_output": "最终音频",
    "08_bgm": "BGM 缓存与混音",
    "logs": "工作空间日志",
    "config": "工作空间配置",
    "models": "模型文件",
    "other": "其他文件",
}


def project_storage_path(root: Path, username: str, project_id: str) -> Path | None:
    """Resolve an indexed workspace without following a user-controlled path."""
    resolved_root = root.resolve()
    candidate = root / safe_display_name(username) / project_id
    if candidate.is_symlink() or candidate.parent.is_symlink():
        return None
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_root):
            return None
    except (OSError, RuntimeError):
        return None
    return candidate


def read_bgm_usage(workspace: Path) -> dict[str, int]:
    """Count saved chapter assignments without reading audio or analysis content."""
    path = workspace / "08_bgm" / "bgm_assignments.json"
    try:
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    chapters = data.get("chapters") if isinstance(data, dict) else None
    if not isinstance(chapters, dict):
        return {}
    counts: dict[str, int] = {}
    for entry in chapters.values():
        if isinstance(entry, dict) and isinstance(entry.get("music"), str):
            name = entry["music"]
            counts[name] = counts.get(name, 0) + 1
    return counts


def scan_project_directory(workspace: Path, *, active: bool = False) -> dict:
    """Measure ordinary files, and identify old 00_temp files eligible for cleanup."""
    result = {"size_bytes": 0, "file_count": 0, "categories": {},
              "cleanup_count": 0, "cleanup_bytes": 0}
    if workspace.is_symlink() or not workspace.is_dir():
        return result
    cutoff = datetime.now().timestamp() - TEMP_CLEANUP_AGE_DAYS * 24 * 60 * 60
    for _path, relative, stat in iter_regular_project_files(workspace):
        key = relative.parts[0] if relative.parts else "other"
        category = key if key in PROJECT_CATEGORY_LABELS else "other"
        bucket = result["categories"].setdefault(category, {"count": 0, "size_bytes": 0})
        bucket["count"] += 1
        bucket["size_bytes"] += stat.st_size
        result["file_count"] += 1
        result["size_bytes"] += stat.st_size
        if not active and category == "00_temp" and stat.st_mtime < cutoff:
            result["cleanup_count"] += 1
            result["cleanup_bytes"] += stat.st_size
    return result


def music_use_counts(workspaces: list[tuple[Project, str]], root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for workspace, username in workspaces:
        path = project_storage_path(root, username, workspace.id)
        if path is None or not path.is_dir():
            continue
        for name, count in read_bgm_usage(path).items():
            counts[name] = counts.get(name, 0) + count
    return counts


