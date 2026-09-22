"""Backend-owned folder browser and folder management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..core import filesystem
from ..core import filesystem_shortcuts
from ..platform.deps import require_admin

# This is a desktop/operations compatibility surface that can enumerate the
# server filesystem.  Ordinary users use managed workspaces instead.
router = APIRouter(prefix="/api/filesystem", tags=["filesystem"], dependencies=[Depends(require_admin)])


class CreateFolderRequest(BaseModel):
    parent_path: str
    name: str = Field(min_length=1)


class RenameFolderRequest(BaseModel):
    path: str
    new_name: str = Field(min_length=1)


class DeleteFolderRequest(BaseModel):
    path: str
    recursive: bool = False
    confirmed: bool = False


class ShortcutRequest(BaseModel):
    path: str


def _error(exc: filesystem.FilesystemError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


@router.get("/drives")
def get_drives() -> dict:
    try:
        return {"drives": filesystem.list_drives()}
    except OSError as exc:
        raise HTTPException(503, "无法读取系统磁盘列表") from exc


@router.get("/directories")
def get_directories(path: str = Query(..., min_length=1)) -> dict:
    try:
        return filesystem.list_directories(path)
    except filesystem.FilesystemError as exc:
        raise _error(exc)


@router.post("/folders")
def post_folder(req: CreateFolderRequest) -> dict:
    try:
        return filesystem.create_folder(req.parent_path, req.name)
    except filesystem.FilesystemError as exc:
        raise _error(exc)


@router.patch("/folders")
def patch_folder(req: RenameFolderRequest) -> dict:
    try:
        return filesystem.rename_folder(req.path, req.new_name)
    except filesystem.FilesystemError as exc:
        raise _error(exc)


@router.delete("/folders")
def remove_folder(req: DeleteFolderRequest) -> dict:
    try:
        return filesystem.delete_folder(req.path, req.recursive, req.confirmed)
    except filesystem.FilesystemError as exc:
        raise _error(exc)


@router.get("/shortcuts")
def get_shortcuts() -> dict:
    return {"shortcuts": filesystem_shortcuts.list_shortcuts()}


@router.post("/shortcuts")
def add_shortcut(req: ShortcutRequest) -> dict:
    try:
        return filesystem_shortcuts.add(req.path)
    except filesystem.FilesystemError as exc:
        raise _error(exc)


@router.delete("/shortcuts")
def delete_shortcut(req: ShortcutRequest) -> dict:
    try:
        filesystem_shortcuts.remove(req.path)
        return {"shortcuts": filesystem_shortcuts.list_shortcuts()}
    except filesystem.FilesystemError as exc:
        raise _error(exc)
