from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from .platform_settings import settings
from .models import SystemConfig, Project
from ..core.safe_filesystem import is_link_or_junction


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
    return candidate[:180].rstrip(" .") or "upload.bin"


def configured_storage_root(db: Session | None = None) -> Path:
    """Return the administrator-selected root, or the deployment default."""
    if db is not None:
        config = db.get(SystemConfig, "storage.root")
        if config and isinstance(config.value, dict) and config.value.get("path"):
            return Path(str(config.value["path"])).expanduser().resolve()
    return settings.storage_root.resolve()


def project_workspace_path(db: Session | None, username: str, project_id: str) -> Path:
    root = configured_storage_root(db)
    return object_path(project_directory_key(db, username, project_id), root)


def safe_project_workspace_path(db: Session | None, username: str, project_id: str) -> Path | None:
    """Resolve a project directory only when it stays inside managed storage."""
    if not project_id or Path(project_id).name != project_id or project_id in {".", ".."}:
        return None
    root = configured_storage_root(db).resolve()
    user_root = root / safe_display_name(username)
    try:
        candidate = root / project_directory_key(db, username, project_id)
    except ValueError:
        return None
    if is_link_or_junction(user_root) or is_link_or_junction(candidate):
        return None
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            return None
    except (OSError, RuntimeError):
        return None
    return resolved


def object_path(object_key: str, root: Path | None = None) -> Path:
    root = (root or settings.storage_root).resolve()
    candidate = (root / object_key).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("非法存储路径")
    return candidate


def project_directory_key(db: Session | None, username: str, project_id: str) -> str:
    project = db.get(Project, project_id) if db is not None else None
    key = project.directory_key if project is not None else f"{safe_display_name(username)}/{project_id}"
    parts = Path(key).parts
    if len(parts) != 2 or parts[0] != safe_display_name(username) or any(part in {".", ".."} for part in parts):
        raise ValueError("非法项目目录")
    return key


def artifact_module(task_type: str, name: str = "") -> str:
    if task_type.startswith(("bgm.", "music.")):
        return "08_bgm"
    if task_type.startswith("voices."):
        return "04_voice_profiles"
    if task_type == "tts.merge":
        return "06_audio_merge"
    if task_type.startswith("tts."):
        return "05_audio_chunk"
    if task_type == "text.format" or task_type == "book.split":
        return "02_split_text"
    if task_type.startswith(("book.", "script.")):
        return "03_parsed_json"
    if task_type.startswith("audio."):
        return "07_output" if Path(name).suffix.lower() != ".json" else "03_parsed_json"
    return "07_output"


def available_file_name(directory: Path, name: str, *, reserved: set[str] | None = None) -> str:
    """Preserve all versions with readable numeric suffixes, including on Windows."""
    name = safe_display_name(name)
    occupied = {entry.name.casefold() for entry in directory.iterdir()} if directory.is_dir() else set()
    occupied.update(value.casefold() for value in (reserved or set()))
    candidate = name
    number = 2
    while candidate.casefold() in occupied:
        suffix = f" ({number})"
        extension = Path(name).suffix
        stem = Path(name).stem
        if len(extension) + len(suffix) >= 180:
            # Extremely long extensions cannot fit alongside a disambiguator.
            stem, extension = name, ""
        stem_limit = 180 - len(suffix) - len(extension)
        candidate = f"{stem[:stem_limit]}{suffix}{extension}"
        number += 1
    return candidate


def project_object_key(username: str, project_id: str, file_id: str, name: str,
                       *, db: Session | None = None, task_type: str = "") -> str:
    key = project_directory_key(db, username, project_id)
    module = artifact_module(task_type, name)
    return f"{key}/{module}/{safe_display_name(name)}"


def project_input_object_key(username: str, project_id: str, file_id: str, name: str,
                             *, db: Session | None = None) -> str:
    return f"{project_directory_key(db, username, project_id)}/01_input/{safe_display_name(name)}"


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
    relative = Path(project_directory_key(db, username, project_id)) / "00_temp" / "tasks" / task_id / attempt_id / safe_display_name(name)
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


