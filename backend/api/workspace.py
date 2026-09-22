"""Compatibility workspace endpoints backed by managed user workspaces."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import config as core_config
from ..core.paths import Layout, WORKSPACE_DIR_NAMES
from ..core.request_context import bind_workspace
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context, require_csrf
from ..platform.models import User, Workspace, utcnow
from ..platform.storage import configured_storage_root, safe_display_name, user_workspace_root

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


class WorkspaceRequest(BaseModel):
    path: str = ""
    workspace_id: str | None = None


class RecentWorkspaceRequest(BaseModel):
    path: str


def _owned(db: Session, user: User, workspace_id: str) -> Workspace:
    item = db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.owner_id == user.id,
            Workspace.deleted_at.is_(None),
        )
    )
    if item is None:
        raise HTTPException(404, "工作空间不存在")
    return item


def _active(db: Session, ctx: AuthContext) -> Workspace | None:
    if ctx.session.active_workspace_id:
        item = db.scalar(
            select(Workspace).where(
                Workspace.id == ctx.session.active_workspace_id,
                Workspace.owner_id == ctx.user.id,
                Workspace.deleted_at.is_(None),
            )
        )
        if item is not None:
            return item
    return db.scalar(
        select(Workspace)
        .where(Workspace.owner_id == ctx.user.id, Workspace.deleted_at.is_(None))
        .order_by(Workspace.updated_at.desc())
    )


def _info(db: Session, ctx: AuthContext) -> dict:
    item = _active(db, ctx)
    if item is None:
        return {"set": False, "path": "", "exists": False, "is_default": True, "dirs": {}}
    path = user_workspace_root(db, ctx.user.username, item.id)
    return {
        "set": True,
        "path": str(path),
        "exists": path.exists(),
        "is_default": item.name == "默认工作空间",
        "workspace_id": item.id,
        "workspace_name": item.name,
        "dirs": Layout(path).dirs(),
    }


@router.get("")
def get_workspace(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> dict:
    return _info(db, ctx)


@router.get("/recent")
def get_recent_workspaces(ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(
        select(Workspace)
        .where(Workspace.owner_id == ctx.user.id, Workspace.deleted_at.is_(None))
        .order_by(Workspace.updated_at.desc())
    ).all()
    current = _active(db, ctx)
    return {
        "workspaces": [
            {
                "path": str(user_workspace_root(db, ctx.user.username, item.id)),
                "name": item.name,
                "workspace_id": item.id,
                "exists": user_workspace_root(db, ctx.user.username, item.id).is_dir(),
                "is_current": current is not None and item.id == current.id,
            }
            for item in rows
        ]
    }


@router.delete("/recent")
def delete_recent_workspace(
    req: RecentWorkspaceRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    current = _active(db, ctx)
    if current is not None:
        path = user_workspace_root(db, ctx.user.username, current.id)
        if Path(req.path).expanduser().resolve() == path:
            ctx.session.active_workspace_id = None
            db.commit()
    return get_recent_workspaces(ctx, db)


@router.put("")
def set_workspace(
    req: WorkspaceRequest,
    ctx: AuthContext = Depends(get_auth_context),
    _: User = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    if not req.path.strip() and not req.workspace_id:
        ctx.session.active_workspace_id = None
        db.commit()
        bind_workspace(None)
        return _info(db, ctx)

    if req.workspace_id:
        item = _owned(db, ctx.user, req.workspace_id)
    else:
        raw = Path(req.path).expanduser()
        if not raw.is_absolute():
            raise HTTPException(400, "工作空间必须由系统管理，不能使用相对路径")
        root = (configured_storage_root(db) / safe_display_name(ctx.user.username)).resolve()
        resolved = raw.resolve()
        if not resolved.is_relative_to(root) or len(resolved.relative_to(root).parts) != 1:
            raise HTTPException(400, "工作空间必须位于管理员设置的用户根目录下")
        item = _owned(db, ctx.user, resolved.relative_to(root).parts[0])

    path = user_workspace_root(db, ctx.user.username, item.id)
    try:
        for name in (*WORKSPACE_DIR_NAMES, "logs", "config"):
            (path / name).mkdir(parents=True, exist_ok=True)
        core_config.init_workspace_config(path)
    except OSError as exc:
        raise HTTPException(422, f"无法准备工作空间目录：{exc}") from exc
    ctx.session.active_workspace_id = item.id
    item.updated_at = utcnow()
    db.commit()
    bind_workspace(path)
    return _info(db, ctx)
