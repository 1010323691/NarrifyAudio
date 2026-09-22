from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_csrf, require_user
from ..platform.models import User, Workspace, new_id, utcnow
from ..platform.storage import user_workspace_root

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


def _json(item: Workspace) -> dict:
    return {"id": item.id, "name": item.name, "directory_key": item.directory_key, "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat()}


def _owned(db: Session, user: User, workspace_id: str) -> Workspace:
    item = db.scalar(select(Workspace).where(Workspace.id == workspace_id, Workspace.owner_id == user.id, Workspace.deleted_at.is_(None)))
    if item is None:
        raise HTTPException(404, "工作空间不存在")
    return item


@router.get("")
def list_workspaces(user: User = Depends(require_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Workspace).where(Workspace.owner_id == user.id, Workspace.deleted_at.is_(None)).order_by(Workspace.updated_at.desc())).all()
    return [_json(item) for item in rows]


@router.post("", status_code=201)
def create_workspace(payload: WorkspaceCreate, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    name = payload.name.strip()
    if db.scalar(select(Workspace).where(Workspace.owner_id == user.id, Workspace.name == name, Workspace.deleted_at.is_(None))) is not None:
        raise HTTPException(409, "工作空间名称已存在")
    workspace_id = new_id()
    item = Workspace(id=workspace_id, owner_id=user.id, name=name, directory_key=f"{user.username}/{workspace_id}")
    db.add(item)
    try:
        user_workspace_root(db, user.username, workspace_id).mkdir(parents=True, exist_ok=True)
        db.commit()
    except OSError as exc:
        db.rollback()
        raise HTTPException(422, f"无法创建工作空间目录：{exc}") from exc
    return _json(item)


@router.delete("/{workspace_id}")
def delete_workspace(workspace_id: str, user: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    item = _owned(db, user, workspace_id)
    item.deleted_at = utcnow()
    db.commit()
    return {"ok": True, "directory_retained": True}

