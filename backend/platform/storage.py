from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy.orm import Session

from .config import settings
from .models import SystemConfig


_SAFE_NAME = re.compile(r"[^\w.()\- ]+", re.UNICODE)


def safe_display_name(name: str) -> str:
    candidate = Path(name or "upload.bin").name
    candidate = _SAFE_NAME.sub("_", candidate).strip(" .")
    return candidate[:180] or "upload.bin"


def configured_storage_root(db: Session | None = None) -> Path:
    """Return the administrator-selected root, or the deployment default."""
    if db is not None:
        config = db.get(SystemConfig, "storage.root")
        if config and isinstance(config.value, dict) and config.value.get("path"):
            return Path(str(config.value["path"])).expanduser().resolve()
    return settings.storage_root.resolve()


def user_workspace_root(db: Session | None, username: str, workspace_id: str) -> Path:
    root = configured_storage_root(db)
    return (root / safe_display_name(username) / workspace_id).resolve()


def object_path(object_key: str, root: Path | None = None) -> Path:
    root = (root or settings.storage_root).resolve()
    candidate = (root / object_key).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("非法存储路径")
    return candidate


def project_object_key(username: str, project_id: str, file_id: str, name: str) -> str:
    # Username is resolved from the authenticated user, never from the path input.
    return f"{safe_display_name(username)}/{project_id}/{file_id}/{safe_display_name(name)}"


def task_attempt_path(
    db: Session | None,
    username: str,
    project_id: str,
    task_id: str,
    attempt_id: str,
    name: str,
) -> Path:
    """Return an isolated temporary output path for one durable attempt."""
    root = configured_storage_root(db)
    relative = Path(safe_display_name(username)) / project_id / ".tasks" / task_id / attempt_id / safe_display_name(name)
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("非法任务临时路径")
    return candidate


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
