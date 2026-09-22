"""Persistent user-mounted folder shortcuts for the filesystem browser."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import filesystem
from . import paths as core_paths

_lock = threading.RLock()


def shortcuts_file() -> Path:
    return core_paths.PROJECT_ROOT / "config" / "filesystem_shortcuts.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write(records: list[dict[str, Any]]) -> None:
    target = shortcuts_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".filesystem-shortcuts-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"shortcuts": records}, handle, ensure_ascii=False, indent=2)
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
    target = shortcuts_file()
    if not target.exists():
        _write([])
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        raw = data.get("shortcuts", []) if isinstance(data, dict) else []
        if not isinstance(raw, list):
            raise ValueError("shortcuts must be a list")
    except Exception:
        # Shortcuts are optional UI state; a corrupt file must never block the app.
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
        try:
            path = str(filesystem.normalize_path(item["path"]))
        except filesystem.FilesystemError:
            continue
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        created = item.get("created_at") if isinstance(item.get("created_at"), str) else _now()
        last_used = item.get("last_used_at") if isinstance(item.get("last_used_at"), str) else created
        cleaned.append({
            "path": path,
            "display_name": item.get("display_name") or Path(path).name or Path(path).anchor,
            "created_at": created,
            "last_used_at": last_used,
        })
    return cleaned


def list_shortcuts() -> list[dict[str, Any]]:
    with _lock:
        records = _read()
        return [
            {**item, "exists": Path(item["path"]).is_dir()}
            for item in records
        ]


def add(path: str) -> dict[str, Any]:
    normalized = str(filesystem.normalize_path(path))
    native = filesystem.io_path(Path(normalized))
    if not native.exists():
        raise filesystem.FilesystemError(404, "目录不存在或磁盘不可用")
    if not native.is_dir():
        raise filesystem.FilesystemError(400, "快捷入口只能挂载文件夹")
    with _lock:
        records = _read()
        key = os.path.normcase(normalized)
        found = next((item for item in records if os.path.normcase(item["path"]) == key), None)
        timestamp = _now()
        if found is None:
            found = {
                "path": normalized,
                "display_name": Path(normalized).name or Path(normalized).anchor,
                "created_at": timestamp,
            }
            records.append(found)
        found["last_used_at"] = timestamp
        _write(records)
        return {**found, "exists": True}


def remove(path: str) -> bool:
    normalized = str(filesystem.normalize_path(path))
    key = os.path.normcase(normalized)
    with _lock:
        records = _read()
        remaining = [item for item in records if os.path.normcase(item["path"]) != key]
        if len(remaining) == len(records):
            return False
        _write(remaining)
        return True
