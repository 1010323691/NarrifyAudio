"""Explicit-project resource endpoints; no dependency on the active workspace."""
from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_authenticated_user
from ..platform.file_response import file_response
from ..platform.models import User
from ..platform.resource_inventory import ResourceError, list_entries, overview, resolve_resource
from ..platform.resource_tasks import cleanup_preview
from ..services.resource_center import export_file, preview_file

router = APIRouter(prefix="/api/v1/resources", tags=["user-resources"])


def _translate(operation):
    try:
        return operation()
    except ResourceError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(409, "文件已变化或暂时不可访问，请刷新资源后重试。") from exc


@router.get("")
def get_resources(user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    return _translate(lambda: overview(db, user))


@router.get("/entries")
def get_entries(
    project_id: str | None = None, category: str = "all", path: str = "",
    query: str = Query("", max_length=1024), extension: str = Query("", max_length=20),
    sort: str = "name", page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
    directory_mode: bool = False, role: str = "",
    user: User = Depends(require_authenticated_user), db: Session = Depends(get_db),
) -> dict:
    return _translate(lambda: list_entries(db, user, project_id=project_id, category=category, path=path,
        query=query, extension=extension, sort=sort, page=page, page_size=page_size, directory_mode=directory_mode, role=role))


@router.get("/cleanup-preview")
def get_cleanup_preview(
    project_ids: list[str] = Query(...), page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
    user: User = Depends(require_authenticated_user), db: Session = Depends(get_db),
) -> dict:
    if len(project_ids) > 1000:
        raise HTTPException(422, "项目数量超过限制")
    return _translate(lambda: cleanup_preview(db, user, project_ids, page=page, page_size=page_size))


@router.get("/exports/{task_id}/download")
def download_export(task_id: str, request: Request, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)):
    path, name = _translate(lambda: export_file(db, user, task_id))
    return file_response(request, path, media_type="application/zip", filename=name)


@router.get("/files/{resource_id}")
def get_file(resource_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    _path, description, _row = _translate(lambda: resolve_resource(db, user, resource_id))
    return description


@router.get("/files/{resource_id}/preview")
def get_preview(resource_id: str, request: Request, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)):
    path, description, _row = _translate(lambda: resolve_resource(db, user, resource_id))
    if description["preview_kind"] in {"audio", "image"}:
        return file_response(request, path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream", filename=path.name, inline=True)
    return _translate(lambda: preview_file(db, user, resource_id))


@router.get("/files/{resource_id}/download")
def download_resource(resource_id: str, request: Request, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)):
    path, description, _row = _translate(lambda: resolve_resource(db, user, resource_id))
    if not description["can_download"]:
        raise HTTPException(403, description["download_reason"])
    return file_response(request, path, media_type="application/octet-stream", filename=description["name"])
