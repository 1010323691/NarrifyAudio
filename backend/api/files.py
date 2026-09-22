"""File endpoints — list a module's output directory and serve files for
download / preview (fetched by the browser; also the source of preview text)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session

from ..core.paths import WORKSPACE_DIRS, get_layout, is_workspace_set
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context
from ..platform.file_response import file_response
from ..platform.legacy_workspace import active_workspace, ensure_project
from ..platform.models import ProjectFile, new_id
from ..platform.config import settings
from ..platform.storage import project_input_object_key, safe_display_name
from . import _common

router = APIRouter(prefix="/api/files", tags=["files"])

# The seven workspace artifact directories, addressable by their on-disk name
# (Layout attribute). ``00_temp`` is scratch space and intentionally not
# addressable here.
_MODULE_ATTRS = {name: attr for attr, name in WORKSPACE_DIRS if attr != "temp"}


def _module_dir(module: str) -> Path:
    layout = get_layout()
    attr = _MODULE_ATTRS.get(module)
    d = getattr(layout, attr, None) if attr else None
    if d is None:
        raise HTTPException(404, "未知模块")
    return d


@router.get("/list/{module}")
def list_module(module: str, recursive: bool = Query(False)) -> dict:
    if module not in _MODULE_ATTRS:
        raise HTTPException(404, "未知模块")
    if not is_workspace_set():
        # No workspace yet: nothing to list (the pipeline is locked).
        return {"path": "", "items": []}
    d = _module_dir(module)
    if not d.exists():
        return {"path": str(d), "items": []}
    paths = sorted(d.rglob("*") if recursive else d.iterdir())
    items = []
    for p in paths:
        resolved = p.resolve()
        if not resolved.is_relative_to(d.resolve()) or p.is_symlink():
            continue
        if recursive and p.is_dir():
            continue
        relative = p.relative_to(d).as_posix()
        items.append(
            {
                "name": relative if recursive else p.name,
                "is_dir": p.is_dir(),
                "size": (p.stat().st_size if p.is_file() else None),
            }
        )
    return {"path": str(d), "items": items}


@router.get("/download/{module}/{name:path}")
def download_file(module: str, name: str, request: Request):
    if module not in _MODULE_ATTRS:
        raise HTTPException(404, "未知模块")
    if not is_workspace_set():
        raise HTTPException(404, "尚未设置工作空间")
    d = _module_dir(module).resolve()
    p = (d / name).resolve()
    if not p.is_relative_to(d) or not p.is_file():
        raise HTTPException(400, "非法路径")
    return file_response(request, p, media_type="application/octet-stream", filename=p.name)


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
    layout = get_layout()
    layout.input.mkdir(parents=True, exist_ok=True)
    workspace = active_workspace(db, ctx.user, ctx.session)
    if workspace is None:
        raise HTTPException(409, "尚未设置工作空间")
    project = ensure_project(db, ctx.user, workspace)
    file_id = new_id()
    name = safe_display_name(Path(filename or file.filename or "upload.bin").name)
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
                original_name=name,
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
    return {"path": str(dest), "name": name, "size": digest_size, "id": file_id, "file_id": file_id, "project_id": project.id}
