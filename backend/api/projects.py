from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_csrf, require_authenticated_user
from ..platform.models import Project, ProjectFile, User, utcnow
from ..platform.project_context import active_project
from ..platform.deps import AuthContext, get_auth_context
from ..services.list_paging import project_page
from ..services.projects import (
    ActiveProjectTasksError,
    DEFAULT_WORKSPACE_NAME,
    as_utc,
    trash_expires_at,
    create_project as create_project_record,
    move_project_to_trash,
    rename_project,
    restore_project,
)
from ..platform.storage import project_workspace_path
from ..platform.system_config import project_retention, retention_started_at
from ..platform.workspace_layout import validate_project_name
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


def _owned_project(db: Session, user: User, project_id: str, *, lock: bool = False) -> Project:
    statement = select(Project).where(Project.id == project_id, Project.owner_id == user.id, Project.deleted_at.is_(None))
    if lock:
        statement = statement.with_for_update()
    project = db.scalar(statement)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


@router.get("")
def list_projects(user: User = Depends(require_authenticated_user), db: Session = Depends(get_db),
                  page: Annotated[int | None, Query(ge=1)] = None, page_size: Annotated[int, Query(ge=1, le=100)] = 10, q: str = ""):
    if page is not None: return project_page(db, user, page, page_size, q)
    return [_project_json(item, user) for item in db.scalars(select(Project).where(Project.owner_id == user.id, Project.deleted_at.is_(None)).order_by(Project.updated_at.desc())).all()]


@router.get("/trash")
def list_trashed_projects(user: User = Depends(require_authenticated_user), db: Session = Depends(get_db),
                         page: Annotated[int | None, Query(ge=1)] = None, page_size: Annotated[int, Query(ge=1, le=100)] = 10, q: str = "", filter: str = "all"):
    if page is not None: return project_page(db, user, page, page_size, q, True, filter)
    items = db.scalars(select(Project).where(
        Project.owner_id == user.id, Project.deleted_at.is_not(None),
    ).order_by(Project.deleted_at.desc())).all()
    trash_days, floor = project_retention(db)["trash_days"], retention_started_at(db)
    return [{
        **_project_json(item, user),
        "deleted_at": as_utc(item.deleted_at).isoformat(),
        "expires_at": trash_expires_at(item.deleted_at, trash_days, floor).isoformat(),
    } for item in items]


def _owned_trashed_project(db: Session, user: User, project_id: str) -> Project:
    project = db.scalar(select(Project).where(
        Project.id == project_id, Project.owner_id == user.id, Project.deleted_at.is_not(None),
    ).with_for_update())
    if project is None:
        raise HTTPException(404, "项目不在回收站中")
    return project


def _lock_project_name_scope(db: Session, user: User) -> None:
    """Serialize name checks and updates for one owner's active projects."""
    db.scalar(select(User.id).where(User.id == user.id).with_for_update())


@router.post("", status_code=201)
def create_project(payload: ProjectCreate, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    try:
        name = validate_project_name(payload.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if name == DEFAULT_WORKSPACE_NAME:
        raise HTTPException(422, "「默认工作空间」是系统保留名称")
    _lock_project_name_scope(db, user)
    if db.scalar(select(Project).where(Project.owner_id == user.id, Project.name == name, Project.deleted_at.is_(None))) is not None:
        raise HTTPException(409, "项目名称已存在")
    try:
        project = create_project_record(
            db, owner_id=user.id, username=user.username,
            name=name, description=payload.description.strip(),
        )
        project_workspace_path(db, user.username, project.id).mkdir(parents=True, exist_ok=True)
        db.commit()
    except (OSError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, f"无法创建项目工作空间目录：{exc}") from exc
    db.refresh(project)
    return _project_json(project, user)


def _active_project(db: Session, ctx: AuthContext) -> Project | None:
    return active_project(db, ctx.user, ctx.session)


def _project_context(db: Session, ctx: AuthContext) -> dict:
    project = _active_project(db, ctx)
    if project is None:
        return {"set": False, "path": "", "exists": False, "is_default": True, "dirs": {}}
    path = project_workspace_path(db, ctx.user.username, project.id)
    return {
        "set": True, "path": str(path), "exists": path.exists(),
        "is_default": project.name == "默认工作空间", "project_id": project.id,
        "project_name": project.name, "dirs": Layout(path).dirs(),
    }


@router.get("/active")
def get_active_project(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> dict:
    return _project_context(db, ctx)


@router.post("/{project_id}/restore")
def restore_trashed_project(project_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    _lock_project_name_scope(db, user)
    project = _owned_trashed_project(db, user, project_id)
    try:
        restore_project(db, project)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(410, str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "项目名称刚刚被其他项目占用，请刷新回收站后重试。") from exc
    except Exception:
        db.rollback()
        raise
    db.refresh(project)
    return _project_json(project, user)


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
    path = project_workspace_path(db, ctx.user.username, project.id)
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


@router.patch("/{project_id}")
def update_project(project_id: str, payload: ProjectUpdate, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    if payload.name is not None:
        _lock_project_name_scope(db, user)
    project = _owned_project(db, user, project_id, lock=True)
    if payload.name is not None:
        name = payload.name.strip()
        # The default workspace is exempt from expiry by this name, so it can neither be renamed nor claimed.
        if (name == DEFAULT_WORKSPACE_NAME) != (project.name == DEFAULT_WORKSPACE_NAME) and name != project.name:
            raise HTTPException(422, "「默认工作空间」是系统保留名称，不能重命名或被占用")
        duplicate = db.scalar(select(Project).where(Project.owner_id == user.id, Project.name == name, Project.id != project.id, Project.deleted_at.is_(None)))
        if duplicate is not None:
            raise HTTPException(409, "项目名称已存在")
        try:
            rename_project(db, project, validate_project_name(name))
        except (ValueError, OSError) as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc
    if payload.description is not None:
        project.description = payload.description.strip()
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(project)
    return _project_json(project, user)


@router.delete("/{project_id}")
def delete_project(project_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    project = _owned_project(db, user, project_id, lock=True)
    try:
        move_project_to_trash(db, project)
        db.commit()
    except ActiveProjectTasksError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
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
