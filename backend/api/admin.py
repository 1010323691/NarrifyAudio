from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import redis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import require_admin, require_csrf
from ..platform.models import AuditLog, QuotaTransaction, SystemConfig, Task, User, UserQuotaAccount, WorkerHeartbeat
from ..platform.outbox import STREAM_NAME
from ..platform.storage import configured_storage_root
from ..platform.worker_registry import is_stale

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class UserState(BaseModel):
    is_active: bool | None = None
    role: Literal["user", "admin"] | None = None


class StorageRootUpdate(BaseModel):
    root_path: str


class QuotaAdjustment(BaseModel):
    amount: int
    idempotency_key: str = Field(min_length=8, max_length=180)
    note: str = Field(default="", max_length=500)


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
    next_role = payload.role or user.role
    next_active = user.is_active if payload.is_active is None else payload.is_active
    if user.role == "admin" and (next_role != "admin" or not next_active):
        active_admins = db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))) or 0
        if active_admins <= 1:
            raise HTTPException(409, "不能禁用或降权最后一个有效管理员")
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.role is not None:
        user.role = payload.role
    db.add(AuditLog(actor_user_id=actor.id, action="admin.user_state_changed", target_type="user", target_id=user.id, metadata_json={"is_active": user.is_active, "role": user.role}))
    db.commit()
    return {"id": user.id, "is_active": user.is_active, "role": user.role}


def _quota_json(account: UserQuotaAccount) -> dict:
    return {
        "user_id": account.user_id,
        "available_units": account.available_units,
        "reserved_units": account.reserved_units,
        "frozen_units": account.frozen_units,
        "consumed_units": account.consumed_units,
    }


@router.get("/users/{user_id}/quota")
def get_user_quota(user_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    account = db.get(UserQuotaAccount, user_id)
    if account is None:
        raise HTTPException(404, "额度账户不存在")
    return _quota_json(account)


@router.post("/users/{user_id}/quota/adjust")
def adjust_user_quota(user_id: str, payload: QuotaAdjustment, actor: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    if actor.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    if payload.amount == 0:
        raise HTTPException(422, "额度调整不能为 0")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    existing = db.scalar(select(QuotaTransaction).where(QuotaTransaction.idempotency_key == payload.idempotency_key))
    if existing is not None:
        if existing.user_id != user_id or existing.amount != payload.amount:
            raise HTTPException(409, "幂等键对应的额度调整不同")
        account = db.get(UserQuotaAccount, user_id)
        if account is None:
            raise HTTPException(409, "额度账户不存在")
        return _quota_json(account)
    account = db.scalar(select(UserQuotaAccount).where(UserQuotaAccount.user_id == user_id).with_for_update())
    if account is None:
        account = UserQuotaAccount(user_id=user_id)
        db.add(account)
        db.flush()
    if payload.amount < 0 and account.available_units < -payload.amount:
        raise HTTPException(409, "可用额度不足，不能扣减")
    account.available_units += payload.amount
    db.add(
        QuotaTransaction(
            user_id=user_id,
            amount=payload.amount,
            kind="admin_adjust",
            idempotency_key=payload.idempotency_key,
            note=payload.note or "administrator adjustment",
        )
    )
    db.add(
        AuditLog(
            actor_user_id=actor.id,
            action="admin.quota_adjusted",
            target_type="user",
            target_id=user_id,
            metadata_json={"amount": payload.amount, "note": payload.note},
        )
    )
    db.commit()
    return _quota_json(account)


@router.get("/tasks")
def list_all_tasks(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(Task).order_by(Task.created_at.desc()).limit(500)).all()
    owners = {item.id: item.username for item in db.scalars(select(User)).all()}
    return [
        {
            "id": item.id,
            "owner_id": item.owner_id,
            "owner_username": owners.get(item.owner_id, ""),
            "project_id": item.project_id,
            "task_type": item.task_type,
            "status": item.status,
            "progress": item.progress,
            "error_code": item.error_code,
            "error_message": item.error_message,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }
        for item in rows
    ]


@router.get("/workers")
def list_workers(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc())).all()
    return [
        {
            "worker_id": item.worker_id,
            "status": "offline" if is_stale(item.last_seen_at) else item.status,
            "capabilities": item.capabilities,
            "current_task_id": item.current_task_id,
            "started_at": item.started_at.isoformat(),
            "last_seen_at": item.last_seen_at.isoformat(),
        }
        for item in rows
    ]


@router.get("/queue")
def queue_status(_: User = Depends(require_admin)) -> dict:
    client = redis.Redis.from_url(
        os.getenv("NARRIFY_REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
    )
    try:
        client.ping()
        length = int(client.xlen(STREAM_NAME))
        pending = 0
        try:
            summary = client.xpending(STREAM_NAME, os.getenv("NARRIFY_TASK_GROUP", "narrify-workers"))
            pending = int(summary.get("pending", 0)) if isinstance(summary, dict) else int(summary[0] or 0)
        except redis.ResponseError:
            pass
        return {"available": True, "stream": STREAM_NAME, "length": length, "pending": pending}
    except Exception as exc:  # Redis is an operational dependency, not a request crash.
        return {"available": False, "stream": STREAM_NAME, "length": 0, "pending": 0, "error": str(exc)}
