from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_admin, require_csrf
from ..platform.models import AuditLog, SystemConfig, User
from ..platform.storage import configured_storage_root

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class UserState(BaseModel):
    is_active: bool


class StorageRootUpdate(BaseModel):
    root_path: str


@router.get("/settings/storage")
def get_storage_settings(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    config = db.get(SystemConfig, "storage.root")
    source = "admin"
    if config is None:
        source = "deployment-default"
    return {"root_path": str(configured_storage_root(db)), "source": source}


@router.patch("/settings/storage")
def update_storage_settings(payload: StorageRootUpdate, actor: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    if actor.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    raw = payload.root_path.strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(422, "存储根目录必须是绝对路径")
    resolved = path.resolve()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(422, f"无法创建存储根目录：{exc}") from exc
    config = db.get(SystemConfig, "storage.root")
    if config is None:
        config = SystemConfig(key="storage.root", value={"path": str(resolved)})
        db.add(config)
    else:
        config.value = {"path": str(resolved)}
    db.add(AuditLog(actor_user_id=actor.id, action="admin.storage_root_changed", target_type="system_config", target_id="storage.root", metadata_json={"path": str(resolved)}))
    db.commit()
    return {"root_path": str(resolved), "source": "admin"}


@router.get("/users")
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return [{"id": item.id, "email": item.email, "username": item.username, "display_name": item.display_name, "role": item.role, "is_active": item.is_active, "created_at": item.created_at.isoformat()} for item in users]


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: UserState, actor: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    if actor.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    if user.role == "admin" and not payload.is_active:
        active_admins = db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))) or 0
        if active_admins <= 1:
            raise HTTPException(409, "不能禁用最后一个有效管理员")
    user.is_active = payload.is_active
    db.add(AuditLog(actor_user_id=actor.id, action="admin.user_state_changed", target_type="user", target_id=user.id, metadata_json={"is_active": payload.is_active}))
    db.commit()
    return {"id": user.id, "is_active": user.is_active}
