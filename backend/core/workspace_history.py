"""Persistent recent-workspace records.

This is application-level state, deliberately separate from a workspace's
``config/app.json`` so switching projects never copies the history into a
project.  The file is small, human-readable JSON and is written atomically.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths as core_paths

log = logging.getLogger(__name__)
_lock = threading.RLock()


def history_file() -> Path:
    return core_paths.PROJECT_ROOT / "config" / "recent_workspaces.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_path(value: str | os.PathLike[str]) -> str:
    """Return a stable absolute path suitable for display and persistence."""
    raw = os.fspath(value).strip()
    if not raw:
        return ""
    return os.path.normpath(os.path.abspath(os.path.expanduser(raw)))


def path_key(value: str | os.PathLike[str]) -> str:
    """Return a platform-aware identity (case-insensitive on Windows)."""
    return os.path.normcase(normalize_path(value))


def display_name(value: str | os.PathLike[str]) -> str:
    path = Path(normalize_path(value))
    return path.name or path.anchor or str(path)


def _write(records: list[dict[str, Any]]) -> None:
    target = history_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".recent-workspaces-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"recent_workspaces": records}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read() -> list[dict[str, Any]]:
    target = history_file()
    if not target.exists():
        _write([])
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        raw = data.get("recent_workspaces", []) if isinstance(data, dict) else []
        if not isinstance(raw, list):
            raise ValueError("recent_workspaces must be a list")
    except Exception as exc:
        log.warning("无法读取最近工作空间配置，将使用空列表：%s", exc)
        try:
            backup = target.with_name(f"{target.stem}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}{target.suffix}")
            os.replace(target, backup)
        except OSError:
            pass
        try:
            _write([])
        except OSError:
            pass
        return []

    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        path = normalize_path(item["path"])
        if not path or path_key(path) in seen:
            continue
        seen.add(path_key(path))
        created = item.get("created_at") if isinstance(item.get("created_at"), str) else now_iso()
        last_used = item.get("last_used_at") if isinstance(item.get("last_used_at"), str) else created
        cleaned.append({
            "path": path,
            "display_name": item.get("display_name") or display_name(path),
            "last_used_at": last_used,
            "created_at": created,
        })
    return cleaned


def list_records(current_path: str = "") -> list[dict[str, Any]]:
    with _lock:
        records = _read()
        current_key = path_key(current_path) if current_path else ""
        enriched = [
            {
                **item,
                "exists": Path(item["path"]).is_dir(),
                "is_current": bool(current_key and path_key(item["path"]) == current_key),
            }
            for item in records
        ]
        enriched.sort(key=lambda item: item["last_used_at"], reverse=True)
        return enriched


def upsert(path: str) -> dict[str, Any]:
    normalized = normalize_path(path)
    if not normalized:
        raise ValueError("工作空间路径不能为空")
    with _lock:
        records = _read()
        key = path_key(normalized)
        timestamp = now_iso()
        found = next((item for item in records if path_key(item["path"]) == key), None)
        if found is None:
            found = {
                "path": normalized,
                "display_name": display_name(normalized),
                "created_at": timestamp,
            }
            records.append(found)
        else:
            found["path"] = normalized
            found["display_name"] = display_name(normalized)
        found["last_used_at"] = timestamp
        _write(records)
        return dict(found)


def remove(path: str) -> bool:
    key = path_key(path)
    with _lock:
        records = _read()
        remaining = [item for item in records if path_key(item["path"]) != key]
        if len(remaining) == len(records):
            return False
        _write(remaining)
        return True
