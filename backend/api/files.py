"""File endpoints — list a module's output directory and serve files for
download / preview (fetched by the browser; also the source of preview text)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.paths import WORKSPACE_DIRS, get_or_prepare_layout, resolve_layout, is_workspace_set
from ..platform.database import get_db
from ..platform.file_response import file_response
from ..platform.project_context import active_project
from ..platform.models import ProjectFile, new_id
from ..platform.deps import AuthContext, get_auth_context
from ..platform.platform_settings import settings
from ..platform.storage import configured_storage_root, project_input_object_key, safe_display_name
from . import _common
from ..platform.resource_delivery import require_delivery, DeliveryDenied
from ..core.safe_filesystem import safe_regular_path

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


@router.get("/list/{module}")
def list_module(
    module: str,
    recursive: bool = Query(False),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    if module not in _MODULE_ATTRS:
        raise HTTPException(404, "未知模块")
    if not is_workspace_set():
        # No workspace yet: nothing to list (the pipeline is locked).
        return {"path": "", "items": []}
    d = _module_dir(module)
    if not d.exists():
        return {"path": str(d), "items": []}
    paths = sorted(d.rglob("*") if recursive else d.iterdir())
    catalog: dict[str, ProjectFile] = {}
    project = active_project(db, ctx.user, ctx.session)
    if project is not None:
        prefix = f"{safe_display_name(ctx.user.username)}/{project.id}/{module}/"
        catalog = {
            item.object_key: item
            for item in db.scalars(
                select(ProjectFile).where(
                    ProjectFile.owner_id == ctx.user.id,
                    ProjectFile.project_id == project.id,
                    ProjectFile.object_key.like(prefix + "%"),
                    ProjectFile.deleted_at.is_(None),
                )
            ).all()
        }
    storage_root = configured_storage_root(db).resolve()
    items = []
    for p in paths:
        resolved = p.resolve()
        if not resolved.is_relative_to(d.resolve()) or p.is_symlink():
            continue
        if recursive and p.is_dir():
            continue
        relative = p.relative_to(d).as_posix()
        item = {
            "name": relative if recursive else p.name,
            "is_dir": p.is_dir(),
            "size": (p.stat().st_size if p.is_file() else None),
        }
        if p.is_file():
            try:
                object_key = p.resolve().relative_to(storage_root).as_posix()
            except ValueError:
                object_key = ""
            cataloged = catalog.get(object_key)
            if cataloged is not None:
                item.update({"id": cataloged.id, "file_id": cataloged.id, "project_id": cataloged.project_id})
        items.append(item)
    return {"path": str(d), "items": items}


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
    if (not p.is_relative_to(d) or not p.is_file()) and safe_display_name(name) != name:
        # 存储侧清洗漂移回退：文件表存引擎原始名（保留 ，/—），磁盘名经
        # safe_display_name 清洗（→_）——精确名未命中时按清洗名（逐段，与发布
        # 时逐文件清洗一致）再解析一次。
        fallback = "/".join(safe_display_name(seg) for seg in name.split("/"))
        p2 = (d / fallback).resolve()
        if p2.is_relative_to(d) and p2.is_file():
            p = p2
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
async def upload_file(
    file: UploadFile = File(...),
    filename: str | None = Form(None),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """File selection: save the uploaded file into ``01_input/`` and return its
    path. The frontend's hidden ``<input type=file>`` hands the file here, so this
    is the entry point into the ``01_input/`` directory."""
    _common.require_workspace()
    layout = get_or_prepare_layout()
    layout.input.mkdir(parents=True, exist_ok=True)
    project = active_project(db, ctx.user, ctx.session)
    if project is None:
        raise HTTPException(409, "尚未设置工作空间")
    file_id = new_id()
    # Preserve the user's basename for display and recovery. Storage names are
    # independently sanitized; never overwrite original_name with that alias.
    original_name = (filename or file.filename or "upload.bin").replace("\\", "/").rsplit("/", 1)[-1] or "upload.bin"
    name = safe_display_name(original_name)
    dest = (layout.input / file_id / name).resolve()
    if not dest.is_relative_to(layout.input.resolve()):
        raise HTTPException(400, "非法文件名")
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest_size = 0
    import hashlib
    digest = hashlib.sha256()
    try:
        with dest.open("xb") as handle:
            while chunk := await file.read(1024 * 1024):
                digest_size += len(chunk)
                if digest_size > settings.max_upload_bytes:
                    raise HTTPException(413, "文件超过大小限制")
                digest.update(chunk)
                handle.write(chunk)
        if digest_size == 0:
            raise HTTPException(400, "上传内容为空。")
        object_key = project_input_object_key(ctx.user.username, project.id, file_id, name)
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
