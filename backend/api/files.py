"""File endpoints — list a module's output directory and serve files for
download / preview (fetched by the browser; also the source of preview text)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..core.paths import WORKSPACE_DIRS, get_layout, is_workspace_set
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
def list_module(module: str) -> dict:
    if module not in _MODULE_ATTRS:
        raise HTTPException(404, "未知模块")
    if not is_workspace_set():
        # No workspace yet: nothing to list (the pipeline is locked).
        return {"path": "", "items": []}
    d = _module_dir(module)
    if not d.exists():
        return {"path": str(d), "items": []}
    items = [
        {
            "name": p.name,
            "is_dir": p.is_dir(),
            "size": (p.stat().st_size if p.is_file() else None),
        }
        for p in sorted(d.iterdir())
    ]
    return {"path": str(d), "items": items}


@router.get("/download/{module}/{name:path}")
def download_file(module: str, name: str):
    if module not in _MODULE_ATTRS:
        raise HTTPException(404, "未知模块")
    if not is_workspace_set():
        raise HTTPException(404, "尚未设置工作空间")
    d = _module_dir(module).resolve()
    p = (d / name).resolve()
    if not str(p).startswith(str(d)) or not p.is_file():
        raise HTTPException(400, "非法路径")
    return FileResponse(p, filename=p.name)


@router.post("/upload")
async def upload_file(file: UploadFile = File(...), filename: str | None = Form(None)) -> dict:
    """File selection: save the uploaded file into ``01_input/`` and return its
    path. The frontend's hidden ``<input type=file>`` hands the file here, so this
    is the entry point into the ``01_input/`` directory."""
    _common.require_workspace()
    layout = get_layout()
    layout.input.mkdir(parents=True, exist_ok=True)
    name = Path(filename or file.filename or "upload.bin").name  # strip any directory
    data = await file.read()
    if not data:
        raise HTTPException(400, "上传内容为空。")
    dest = layout.input / name
    dest.write_bytes(data)
    return {"path": str(dest), "name": name, "size": len(data)}
