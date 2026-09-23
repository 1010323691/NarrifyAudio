from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

import redis
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..platform.config import settings
from ..platform.database import get_db
from ..platform.deps import require_admin, require_csrf
from ..platform.models import AuditLog, Project, QuotaTransaction, SystemConfig, Task, User, UserQuotaAccount, WorkerHeartbeat, Workspace, utcnow
from ..platform.outbox import STREAM_NAME
from ..platform.storage import configured_storage_root, safe_display_name
from ..platform.task_state import TERMINAL_TASK_STATUSES, append_task_event, release_reservation, suppress_pending_dispatch
from ..platform.worker_registry import is_stale

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class UserState(BaseModel):
    is_active: bool | None = None
    role: Literal["user", "admin"] | None = None


class StorageRootUpdate(BaseModel):
    root_path: str


class InitialQuotaUpdate(BaseModel):
    units: int = Field(ge=0, le=10_000_000)


class RegistrationUpdate(BaseModel):
    enabled: bool


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
    previous = configured_storage_root(db)
    existing_config = db.get(SystemConfig, "storage.root")
    if resolved == previous and existing_config is not None:
        return {"root_path": str(resolved), "source": "admin" if db.get(SystemConfig, "storage.root") else "deployment-default"}
    if resolved.is_relative_to(previous) or previous.is_relative_to(resolved):
        raise HTTPException(422, "新旧存储根目录不能互相嵌套")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(422, f"无法创建存储根目录：{exc}") from exc
    moved: list[tuple[Path, Path]] = []
    directory_keys = {item.directory_key for item in db.scalars(select(Workspace)).all()}
    directory_keys.update(
        f"{safe_display_name(username)}/{project.id}"
        for project, username in db.execute(
            select(Project, User.username).join(User, User.id == Project.owner_id)
        ).all()
    )
    try:
        for directory_key in directory_keys:
            source = (previous / directory_key).resolve()
            target = (resolved / directory_key).resolve()
            if not source.is_relative_to(previous) or not target.is_relative_to(resolved):
                raise HTTPException(422, "工作空间目录键越界")
            if source == target or not source.exists():
                if not source.exists() and target.exists():
                    continue
                continue
            if source.is_symlink() or target.exists():
                raise HTTPException(409, f"工作空间目录迁移冲突：{directory_key}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            moved.append((source, target))
    except HTTPException:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
        raise
    except OSError as exc:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
        raise HTTPException(422, f"工作空间目录迁移失败：{exc}") from exc
    config = db.get(SystemConfig, "storage.root")
    if config is None:
        config = SystemConfig(key="storage.root", value={"path": str(resolved)})
        db.add(config)
    else:
        config.value = {"path": str(resolved)}
    db.add(AuditLog(actor_user_id=actor.id, action="admin.storage_root_changed", target_type="system_config", target_id="storage.root", metadata_json={"path": str(resolved)}))
    db.commit()
    return {"root_path": str(resolved), "source": "admin"}


@router.get("/settings/quota")
def get_quota_settings(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    config = db.get(SystemConfig, "quota.initial_units")
    if config is None:
        return {"initial_units": settings.initial_quota_units, "source": "deployment-default"}
    try:
        units = max(0, int(config.value.get("units", settings.initial_quota_units))) if isinstance(config.value, dict) else settings.initial_quota_units
    except (TypeError, ValueError):
        units = settings.initial_quota_units
    return {"initial_units": units, "source": "admin"}


@router.patch("/settings/quota")
def update_quota_settings(payload: InitialQuotaUpdate, actor: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    if actor.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    config = db.get(SystemConfig, "quota.initial_units")
    if config is None:
        config = SystemConfig(key="quota.initial_units", value={"units": payload.units})
        db.add(config)
    else:
        config.value = {"units": payload.units}
    db.add(AuditLog(actor_user_id=actor.id, action="admin.initial_quota_changed", target_type="system_config", target_id="quota.initial_units", metadata_json={"units": payload.units}))
    db.commit()
    return {"initial_units": payload.units, "source": "admin"}


@router.get("/settings/registration")
def get_registration_settings(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    config = db.get(SystemConfig, "registration.enabled")
    if config is None:
        return {"enabled": settings.registration_enabled, "source": "deployment-default"}
    enabled = bool(config.value.get("enabled", settings.registration_enabled)) if isinstance(config.value, dict) else settings.registration_enabled
    return {"enabled": enabled, "source": "admin"}


@router.patch("/settings/registration")
def update_registration_settings(payload: RegistrationUpdate, actor: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    if actor.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    config = db.get(SystemConfig, "registration.enabled")
    if config is None:
        db.add(SystemConfig(key="registration.enabled", value={"enabled": payload.enabled}))
    else:
        config.value = {"enabled": payload.enabled}
    db.add(AuditLog(actor_user_id=actor.id, action="admin.registration_changed", target_type="system_config", target_id="registration.enabled", metadata_json={"enabled": payload.enabled}))
    db.commit()
    return {"enabled": payload.enabled, "source": "admin"}


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
    before_available = account.available_units
    before_reserved = account.reserved_units
    before_consumed = account.consumed_units
    account.available_units += payload.amount
    db.add(
        QuotaTransaction(
            user_id=user_id,
            amount=payload.amount,
            kind="admin_adjust",
            idempotency_key=payload.idempotency_key,
            note=payload.note or "administrator adjustment",
            actor_user_id=actor.id,
            available_before=before_available,
            available_after=account.available_units,
            reserved_before=before_reserved,
            reserved_after=account.reserved_units,
            consumed_before=before_consumed,
            consumed_after=account.consumed_units,
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


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, actor: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    if actor.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status in TERMINAL_TASK_STATUSES:
        return {"id": task.id, "status": task.status}
    previous_status = task.status
    if task.status in {"running", "cancelling"}:
        task.status = "cancelling"
    else:
        task.status = "cancelled"
        task.finished_at = task.finished_at or utcnow()
        release_reservation(db, task, kind="release", note="administrator cancelled task")
        suppress_pending_dispatch(db, task.id)
    task.updated_at = utcnow()
    append_task_event(db, task.id, "admin_cancel_requested", {"actor_user_id": actor.id, "status": task.status})
    db.add(AuditLog(actor_user_id=actor.id, action="admin.task_cancelled", target_type="task", target_id=task.id, metadata_json={"previous_status": previous_status}))
    db.commit()
    return {"id": task.id, "status": task.status}


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
