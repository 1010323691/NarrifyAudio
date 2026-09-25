from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.config import settings
from ..platform.database import get_db
from ..platform.deps import require_csrf, require_authenticated_user
from ..platform.file_response import file_response
from ..platform.models import Project, ProjectFile, User, new_id, utcnow
from ..platform.deps import AuthContext, get_auth_context
from ..services.projects import ActiveProjectTasksError, create_project as create_project_record, rename_project, soft_delete_project
from ..platform.storage import configured_storage_root, object_path, project_object_key, safe_display_name, user_workspace_root
from ..core.paths import Layout, WORKSPACE_DIRS, WORKSPACE_DIR_NAMES
from ..core.request_context import bind_workspace

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)


class ActiveProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str | None = None


def _project_json(project: Project, user: User) -> dict:
    return {"id": project.id, "name": project.name, "description": project.description, "created_at": project.created_at.isoformat(), "updated_at": project.updated_at.isoformat(), "directory_key": project.directory_key}


def _owned_project(db: Session, user: User, project_id: str) -> Project:
    project = db.scalar(select(Project).where(Project.id == project_id, Project.owner_id == user.id, Project.deleted_at.is_(None)))
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


@router.get("")
def list_projects(user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> list[dict]:
    return [_project_json(item, user) for item in db.scalars(select(Project).where(Project.owner_id == user.id, Project.deleted_at.is_(None)).order_by(Project.updated_at.desc())).all()]


@router.post("", status_code=201)
def create_project(payload: ProjectCreate, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    name = payload.name.strip()
    if db.scalar(select(Project).where(Project.owner_id == user.id, Project.name == name, Project.deleted_at.is_(None))) is not None:
        raise HTTPException(409, "项目名称已存在")
    project = create_project_record(
        db, owner_id=user.id, username=user.username,
        name=name, description=payload.description.strip(),
    )
    try:
        user_workspace_root(db, user.username, project.id).mkdir(parents=True, exist_ok=True)
        db.commit()
    except OSError as exc:
        db.rollback()
        raise HTTPException(422, f"无法创建项目工作空间目录：{exc}") from exc
    db.refresh(project)
    return _project_json(project, user)


def _active_project(db: Session, ctx: AuthContext) -> Project | None:
    project = None
    if ctx.session.active_project_id:
        project = db.scalar(select(Project).where(
            Project.id == ctx.session.active_project_id,
            Project.owner_id == ctx.user.id,
            Project.deleted_at.is_(None),
        ))
    if project is None:
        project = db.scalar(select(Project).where(
            Project.owner_id == ctx.user.id, Project.deleted_at.is_(None),
        ).order_by(Project.last_selected_at.desc().nullslast(), Project.updated_at.desc()))
    return project


def _project_context(db: Session, ctx: AuthContext) -> dict:
    project = _active_project(db, ctx)
    if project is None:
        return {"set": False, "path": "", "exists": False, "is_default": True, "dirs": {}}
    path = user_workspace_root(db, ctx.user.username, project.id)
    return {
        "set": True, "path": str(path), "exists": path.exists(),
        "is_default": project.name == "默认工作空间", "project_id": project.id,
        "project_name": project.name, "dirs": Layout(path).dirs(),
    }


@router.get("/active")
def get_active_project(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> dict:
    return _project_context(db, ctx)


@router.put("/active")
def select_active_project(
    payload: ActiveProjectRequest, ctx: AuthContext = Depends(get_auth_context),
    _: User = Depends(require_csrf), db: Session = Depends(get_db),
) -> dict:
    project_id = payload.project_id
    if not project_id:
        ctx.session.active_project_id = None
        db.commit()
        return _project_context(db, ctx)
    project = _owned_project(db, ctx.user, str(project_id))
    path = user_workspace_root(db, ctx.user.username, project.id)
    try:
        for name in (*WORKSPACE_DIR_NAMES, "logs", "config"):
            (path / name).mkdir(parents=True, exist_ok=True)
        from ..core import config as core_config
        core_config.init_workspace_config(path)
    except OSError as exc:
        raise HTTPException(422, f"无法准备项目目录：{exc}") from exc
    ctx.session.active_project_id = project.id
    project.last_selected_at = utcnow()
    db.commit()
    bind_workspace(path)
    return _project_context(db, ctx)


@router.get("/{project_id}")
def get_project(project_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> dict:
    return _project_json(_owned_project(db, user, project_id), user)


@router.patch("/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    project = _owned_project(db, user, project_id)
    if payload.name is not None:
        name = payload.name.strip()
        duplicate = db.scalar(select(Project).where(Project.owner_id == user.id, Project.name == name, Project.id != project.id, Project.deleted_at.is_(None)))
        if duplicate is not None:
            raise HTTPException(409, "项目名称已存在")
        rename_project(db, project, name)
    if payload.description is not None:
        project.description = payload.description.strip()
    db.commit()
    db.refresh(project)
    return _project_json(project, user)


@router.delete("/{project_id}")
def delete_project(project_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    project = _owned_project(db, user, project_id)
    try:
        soft_delete_project(db, project)
    except ActiveProjectTasksError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return {"ok": True}


def _file_json(item: ProjectFile) -> dict:
    parts = item.object_key.split("/")
    modules = {name for _, name in WORKSPACE_DIRS}
    module = parts[2] if len(parts) >= 4 and parts[2] in modules else None
    return {"id": item.id, "name": item.original_name, "module": module, "kind": item.kind, "content_type": item.content_type, "size_bytes": item.size_bytes, "sha256": item.sha256, "created_at": item.created_at.isoformat()}


@router.get("/{project_id}/files")
def list_files(project_id: str, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)) -> list[dict]:
    _owned_project(db, user, project_id)
    rows = db.scalars(select(ProjectFile).where(ProjectFile.project_id == project_id, ProjectFile.owner_id == user.id, ProjectFile.deleted_at.is_(None)).order_by(ProjectFile.created_at.desc())).all()
    return [_file_json(row) for row in rows]


@router.post("/{project_id}/files", status_code=201)
async def upload_file(project_id: str, upload: UploadFile = File(...), user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    project = _owned_project(db, user, project_id)
    file_id = new_id()
    key = project_object_key(user.username, project.id, file_id, upload.filename or "upload.bin")
    destination = object_path(key, configured_storage_root(db))
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "文件超过大小限制")
                digest.update(chunk)
                handle.write(chunk)
    except Exception:
        if destination.exists():
            destination.unlink()
        raise
    item = ProjectFile(id=file_id, project_id=project.id, owner_id=user.id, original_name=safe_display_name(upload.filename or "upload.bin"), object_key=key, content_type=upload.content_type or "application/octet-stream", size_bytes=size, sha256=digest.hexdigest())
    db.add(item)
    db.commit()
    return _file_json(item)


@router.get("/{project_id}/files/{file_id}")
def download_file(project_id: str, file_id: str, request: Request, user: User = Depends(require_authenticated_user), db: Session = Depends(get_db)):
    _owned_project(db, user, project_id)
    item = db.scalar(select(ProjectFile).where(ProjectFile.id == file_id, ProjectFile.project_id == project_id, ProjectFile.owner_id == user.id, ProjectFile.deleted_at.is_(None)))
    if item is None:
        raise HTTPException(404, "文件不存在")
    path = object_path(item.object_key, configured_storage_root(db))
    if not path.is_file():
        raise HTTPException(410, "文件内容已丢失")
    return file_response(request, path, media_type=item.content_type, filename=item.original_name)
