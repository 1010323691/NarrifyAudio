"""Catalog files produced by the compatibility pipeline in the managed store."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .deps import AuthContext
from .project_context import active_project
from .models import ProjectFile
from .storage import configured_storage_root, safe_display_name, sha256_file


def catalog_managed_file(source: Path, ctx: AuthContext, db: Session) -> ProjectFile:
    """Return a ProjectFile for an existing file inside the caller's workspace.

    Legacy pages historically passed absolute paths. The durable task boundary
    accepts only a catalogued file id, so this function maps that path to the
    authenticated user's current project without trusting user-supplied owner or
    storage-root segments.
    """
    source = source.resolve()
    if not source.is_file():
        raise ValueError("不是一个文件")
    project = active_project(db, ctx.user, ctx.session)
    if project is None:
        raise ValueError("尚未设置工作空间")
    root = configured_storage_root(db).resolve()
    try:
        relative = source.relative_to(root)
    except ValueError as exc:
        raise ValueError("文件不属于当前用户的托管工作空间") from exc
    object_key = relative.as_posix()
    prefix = f"{safe_display_name(ctx.user.username)}/{project.id}/"
    if not object_key.startswith(prefix):
        raise ValueError("文件不属于当前项目")
    item = db.scalar(
        select(ProjectFile).where(
            ProjectFile.object_key == object_key,
            ProjectFile.project_id == project.id,
            ProjectFile.owner_id == ctx.user.id,
        )
    )
    item_values = {
        "original_name": safe_display_name(source.name),
        "size_bytes": source.stat().st_size,
        "sha256": sha256_file(source),
    }
    if item is None:
        item = ProjectFile(
            project_id=project.id,
            owner_id=ctx.user.id,
            object_key=object_key,
            content_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            kind="legacy",
            **item_values,
        )
        db.add(item)
        db.flush()
    else:
        for key, value in item_values.items():
            setattr(item, key, value)
        item.deleted_at = None
    return item
