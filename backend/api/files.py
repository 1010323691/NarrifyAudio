"""File endpoints — upload, and serve a module's files for download / preview
(fetched by the browser; also the source of preview text)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.paths import WORKSPACE_DIRS, get_or_prepare_layout, resolve_layout, is_workspace_set
from ..platform.database import get_db
from ..platform.file_response import file_response
from ..platform.project_context import active_project
from ..platform.models import Project, ProjectFile, new_id
from ..platform.deps import AuthContext, get_auth_context
from ..platform.platform_settings import settings
from ..platform.storage import project_input_object_key, safe_display_name, available_file_name, project_workspace_path
from . import _common
from ..platform.resource_delivery import require_delivery, DeliveryDenied
from ..core.safe_filesystem import safe_regular_path
from ..core.filenames import safe_filename, legacy_storage_name

router = APIRouter(prefix="/api/files", tags=["files"])

# The seven workspace artifact directories, addressable by their on-disk name
# (Layout attribute). ``00_temp`` is scratch space and intentionally not
# addressable here.
_MODULE_ATTRS = {name: attr for attr, name in WORKSPACE_DIRS if attr != "temp"}


def _module_dir(module: str) -> Path:
    layout = resolve_layout()
    attr = _MODULE_ATTRS.get(module)
    d = getattr(layout, attr, None) if attr else None
    if d is None:
        raise HTTPException(404, "未知模块")
    return d


@router.get("/download/{module}/{name:path}")
def download_file(module: str, name: str, request: Request, ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)):
    return _serve_file(module, name, request, ctx, db, download=True)


@router.get("/preview/{module}/{name:path}")
def preview_file(module: str, name: str, request: Request, ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)):
    return _serve_file(module, name, request, ctx, db, download=False)


def _serve_file(module, name, request, ctx, db, *, download):
    if module not in _MODULE_ATTRS:
        raise HTTPException(404, "未知模块")
    if not is_workspace_set():
        raise HTTPException(404, "尚未设置工作空间")
    d = _module_dir(module).resolve()
    p = (d / name).resolve()
    if not p.is_relative_to(d) or not p.is_file():
        # Exact names win; legacy aliases are usable only when unambiguous.
        candidates = set()
        for sanitize in (safe_filename, legacy_storage_name):
            fallback = "/".join(sanitize(seg) for seg in name.split("/"))
            p2 = (d / fallback).resolve()
            if p2.is_relative_to(d) and p2.is_file():
                candidates.add(p2)
        if len(candidates) > 1:
            raise HTTPException(409, "文件名匹配多个历史文件，请使用文件列表中的实际名称")
        if candidates:
            p = candidates.pop()
    if not p.is_relative_to(d) or not p.is_file():
        raise HTTPException(400, "非法路径")
    try:
        p = safe_regular_path(_module_dir(module), p.relative_to(d).as_posix())
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(400, "非法路径") from exc
    project = active_project(db, ctx.user, ctx.session)
    if project is None:
        raise HTTPException(404, "项目不存在")
    if download:
        try:
            require_delivery(db, ctx.user, project.id, module + "/" + p.relative_to(d).as_posix())
        except DeliveryDenied as exc:
            raise HTTPException(403, str(exc)) from exc
    elif p.suffix.lower() not in {".txt", ".md", ".json", ".log", ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        raise HTTPException(403, "此格式仅提供资料详情，不提供原始文件预览。")
    import mimetypes
    return file_response(request, p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream", filename=p.name, inline=not download)


@router.post("/upload")
def upload_file(
    file: UploadFile = File(...),
    filename: str | None = Form(None),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """File selection: save the uploaded file into ``01_input/`` and return its
    path. The frontend's hidden ``<input type=file>`` hands the file here, so this
    is the entry point into the ``01_input/`` directory."""
    _common.require_workspace()
    project = active_project(db, ctx.user, ctx.session)
    if project is None:
        raise HTTPException(409, "尚未设置工作空间")
    # A rename and an upload must agree on one directory throughout publication.
    project = db.scalar(select(Project).where(Project.id == project.id, Project.deleted_at.is_(None)).with_for_update().execution_options(populate_existing=True))
    if project is None:
        raise HTTPException(409, "项目已删除")
    from ..core.paths import Layout
    layout = Layout(project_workspace_path(db, ctx.user.username, project.id))
    layout.input.mkdir(parents=True, exist_ok=True)
    file_id = new_id()
    # Preserve the user's basename for display and recovery. Storage names are
    # independently sanitized; never overwrite original_name with that alias.
    original_name = (filename or file.filename or "upload.bin").replace("\\", "/").rsplit("/", 1)[-1] or "upload.bin"
    name = safe_display_name(original_name)
    dest = (layout.input / available_file_name(layout.input, name)).resolve()
    if not dest.is_relative_to(layout.input.resolve()):
        raise HTTPException(400, "非法文件名")
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest_size = 0
    import hashlib
    digest = hashlib.sha256()
    try:
        # Exclusive creation also arbitrates concurrent uploads with the same name.
        while True:
            try:
                handle = dest.open("xb")
                break
            except FileExistsError:
                dest = layout.input / available_file_name(layout.input, name)
        with handle:
            # FastAPI runs this synchronous endpoint in its worker pool. Keep
            # row-lock waits and disk reads there, never on the event loop.
            while chunk := file.file.read(1024 * 1024):
                digest_size += len(chunk)
                if digest_size > settings.max_upload_bytes:
                    raise HTTPException(413, "文件超过大小限制")
                digest.update(chunk)
                handle.write(chunk)
        if digest_size == 0:
            raise HTTPException(400, "上传内容为空。")
        object_key = project_input_object_key(ctx.user.username, project.id, file_id, dest.name, db=db)
        db.add(
            ProjectFile(
                id=file_id,
                project_id=project.id,
                owner_id=ctx.user.id,
                original_name=original_name,
                object_key=object_key,
                content_type=file.content_type or "application/octet-stream",
                size_bytes=digest_size,
                sha256=digest.hexdigest(),
                kind="input",
            )
        )
        db.commit()
    except Exception:
        if dest.exists():
            dest.unlink()
        db.rollback()
        raise
    return {"path": str(dest), "name": original_name, "size": digest_size, "id": file_id, "file_id": file_id, "project_id": project.id}
