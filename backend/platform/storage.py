from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import settings
from .models import SystemConfig


_SAFE_NAME = re.compile(r"[^\w.()\- ]+", re.UNICODE)


def lock_storage_migration(db: Session, *, shared: bool = False) -> bool:
    """Try to enter storage operations without queuing behind another request.

    HTTP middleware and task admission can hold shared locks on separate
    connections. A blocking exclusive waiter between them would deadlock.
    Callers must reject/retry when the lock is unavailable.
    """
    if db.get_bind().dialect.name == "postgresql":
        function = "pg_try_advisory_xact_lock_shared" if shared else "pg_try_advisory_xact_lock"
        return bool(db.scalar(text(f"SELECT {function}(7526202601)")))
    return True


def storage_migration(db: Session) -> SystemConfig | None:
    return db.get(SystemConfig, "storage.migration")


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


def project_workspace_path(db: Session | None, username: str, project_id: str) -> Path:
    root = configured_storage_root(db)
    return (root / safe_display_name(username) / project_id).resolve()


def object_path(object_key: str, root: Path | None = None) -> Path:
    root = (root or settings.storage_root).resolve()
    candidate = (root / object_key).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("非法存储路径")
    return candidate


def project_object_key(username: str, project_id: str, file_id: str, name: str) -> str:
    # Username is resolved from the authenticated user, never from the path input.
    return f"{safe_display_name(username)}/{project_id}/{file_id}/{safe_display_name(name)}"


def project_input_object_key(username: str, project_id: str, file_id: str, name: str) -> str:
    """Stable object key for an input also consumed by legacy workspace paths."""
    return f"{safe_display_name(username)}/{project_id}/01_input/{file_id}/{safe_display_name(name)}"


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


# Compatibility alias for scripts that still use the workspace-era name.
user_workspace_root = project_workspace_path
