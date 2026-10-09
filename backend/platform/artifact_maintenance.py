"""Bounded retirement of superseded UUID-layout artifacts, outside completion."""
from pathlib import Path
from uuid import UUID
from sqlalchemy import select
from ..core.paths import WORKSPACE_DIRS
from ..core.object_keys import object_key_lookup_key
from ..core.safe_filesystem import file_identity, safe_regular_path
from .models import ProjectFile, utcnow
from . import storage


def retire_legacy_artifacts(db, cursor="", limit=100):
    if not 1 <= limit <= 100:
        raise ValueError("artifact maintenance chunk must be 1..100")
    root = storage.configured_storage_root(db)
    rows = db.scalars(select(ProjectFile).where(ProjectFile.kind == "artifact", ProjectFile.id > cursor)
                      .order_by(ProjectFile.id).limit(limit)).all()
    retired = []
    modules = {name for _, name in WORKSPACE_DIRS if name != "00_temp"}
    for item in rows:
        parts = item.object_key.split("/")
        if len(parts) != 5 or parts[2] not in modules:
            continue
        try:
            UUID(parts[3])
        except ValueError:
            continue
        # Retain old-only data: retire only an actual flat replacement in the
        # same project, with the same original name and a safely accessible file.
        key = "/".join([*parts[:3], parts[4]])
        replacements = db.scalars(select(ProjectFile).where(ProjectFile.project_id == item.project_id,
            ProjectFile.owner_id == item.owner_id, ProjectFile.deleted_at.is_(None),
            ProjectFile.object_key_normalized == object_key_lookup_key(key))).all()
        if len(replacements) != 1 or replacements[0].original_name != item.original_name or replacements[0].kind != "artifact":
            continue
        try:
            replacement = safe_regular_path(root, replacements[0].object_key)
            replacement_identity = file_identity(replacement.stat())
            if not replacement_identity[0] or replacement_identity[0] != replacements[0].size_bytes:
                continue
            if storage.sha256_file(replacement) != replacements[0].sha256 or file_identity(replacement.stat()) != replacement_identity:
                continue
            path = safe_regular_path(root, item.object_key)
            identity = file_identity(path.stat())
            if identity[0] != item.size_bytes or storage.sha256_file(path) != item.sha256 or file_identity(path.stat()) != identity:
                continue
        except (OSError, ValueError, RuntimeError):
            continue
        item.deleted_at = item.deleted_at or utcnow()
        retired.append((path, identity))
    return rows[-1].id if rows else "", retired


def remove_retired_files(retired):
    for path, identity in retired:
        try:
            # A replacement during the short commit window belongs to another
            # writer and must remain untouched.
            if path.is_symlink() or file_identity(path.stat()) != identity:
                continue
            path.unlink()
            path.parent.rmdir()
        except (OSError, ValueError):
            continue
