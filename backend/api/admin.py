from __future__ import annotations

import os
import json
import shutil
import subprocess
from datetime import datetime, timedelta
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
from ..platform.models import AuditLog, OutboxEvent, Project, ProjectFile, QuotaReservation, QuotaTransaction, SystemConfig, Task, TaskAttempt, TaskEvent, User, UserQuotaAccount, UserSession, WorkerHeartbeat, Workspace, utcnow
from ..platform.outbox import STREAM_NAME
from ..platform.storage import configured_storage_root, safe_display_name
from ..platform.task_state import TERMINAL_TASK_STATUSES, append_task_event, release_reservation, suppress_pending_dispatch
from ..platform.worker_registry import is_stale
from ..core.observability import api_requests_today, api_snapshot
from ..core import config as core_config

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_ACTIVE_TASK_STATUSES = ("pending", "queued", "running", "paused", "cancelling", "retrying")
_TEMP_CLEANUP_AGE_DAYS = 7
_WORKSPACE_CATEGORY_LABELS = {
    "00_temp": "临时文件",
    "cache": "缓存文件",
    ".cache": "缓存文件",
    "01_input": "输入文件",
    "02_split_text": "章节文本",
    "03_parsed_json": "解析结果",
    "04_voice_profiles": "角色资料",
    "05_audio_chunk": "合成片段",
    "06_audio_merge": "合并音频",
    "07_output": "最终音频",
    "08_bgm": "BGM 缓存与混音",
    "logs": "工作空间日志",
    "config": "工作空间配置",
    "models": "模型文件",
    "other": "其他文件",
}


def _workspace_path(root: Path, username: str, workspace_id: str) -> Path | None:
    """Resolve an indexed workspace without following a user-controlled path."""
    resolved_root = root.resolve()
    candidate = root / safe_display_name(username) / workspace_id
    if candidate.is_symlink() or candidate.parent.is_symlink():
        return None
    try:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_root):
            return None
    except (OSError, RuntimeError):
        return None
    return candidate


def _read_bgm_usage(workspace: Path) -> dict[str, int]:
    """Count saved chapter assignments without reading audio or analysis content."""
    path = workspace / "08_bgm" / "bgm_assignments.json"
    try:
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    chapters = data.get("chapters") if isinstance(data, dict) else None
    if not isinstance(chapters, dict):
        return {}
    counts: dict[str, int] = {}
    for entry in chapters.values():
        if isinstance(entry, dict) and isinstance(entry.get("music"), str):
            name = entry["music"]
            counts[name] = counts.get(name, 0) + 1
    return counts


def _scan_workspace(workspace: Path, *, active: bool = False) -> dict:
    """Measure ordinary files, and identify old 00_temp files eligible for cleanup."""
    result = {"size_bytes": 0, "file_count": 0, "categories": {},
              "cleanup_count": 0, "cleanup_bytes": 0}
    if workspace.is_symlink() or not workspace.is_dir():
        return result
    cutoff = datetime.now().timestamp() - _TEMP_CLEANUP_AGE_DAYS * 24 * 60 * 60
    for directory, child_dirs, filenames in os.walk(workspace, topdown=True, followlinks=False):
        parent = Path(directory)
        child_dirs[:] = [name for name in child_dirs if not (parent / name).is_symlink()]
        for filename in filenames:
            path = parent / filename
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(workspace)
            key = relative.parts[0] if relative.parts else "other"
            category = key if key in _WORKSPACE_CATEGORY_LABELS else "other"
            bucket = result["categories"].setdefault(category, {"count": 0, "size_bytes": 0})
            bucket["count"] += 1
            bucket["size_bytes"] += stat.st_size
            result["file_count"] += 1
            result["size_bytes"] += stat.st_size
            if not active and category == "00_temp" and stat.st_mtime < cutoff:
                result["cleanup_count"] += 1
                result["cleanup_bytes"] += stat.st_size
    return result


def _music_use_counts(workspaces: list[tuple[Workspace, str]], root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for workspace, username in workspaces:
        path = _workspace_path(root, username, workspace.id)
        if path is None or not path.is_dir():
            continue
        for name, count in _read_bgm_usage(path).items():
            counts[name] = counts.get(name, 0) + count
    return counts


def _memory_usage(host_total_bytes: int, cgroup_root: Path = Path("/sys/fs/cgroup")) -> tuple[int, int]:
    """Prefer the current container's cgroup memory usage/limit when available."""
    for current_name, limit_name in (("memory.current", "memory.max"), ("memory/memory.usage_in_bytes", "memory/memory.limit_in_bytes")):
        try:
            current = int((cgroup_root / current_name).read_text().strip())
            raw_limit = (cgroup_root / limit_name).read_text().strip()
            limit = int(raw_limit) if raw_limit != "max" else host_total_bytes
            if current >= 0 and 0 < limit < host_total_bytes * 2:
                return min(current, limit), limit
        except (OSError, ValueError):
            continue
    import psutil
    memory = psutil.virtual_memory()
    return memory.used, memory.total


class UserState(BaseModel):
    is_active: bool | None = None
    role: Literal["user", "admin"] | None = None


class StorageRootUpdate(BaseModel):
    root_path: str


class InitialQuotaUpdate(BaseModel):
    units: int = Field(ge=0, le=10_000_000)


class RegistrationUpdate(BaseModel):
    enabled: bool


_FEATURE_CONFIG_SECTIONS = {
    "text", "audio", "tts", "llm", "prompts", "persona_prompts",
    "generation", "ffmpeg", "bgm",
}


@router.get("/settings/application")
def get_application_settings(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Return effective feature settings, including shared admin overrides."""
    config = core_config.get_config().model_dump()
    stored = db.get(SystemConfig, "application.features")
    return {"config": config, "source": "admin" if stored else "deployment-default"}


@router.patch("/settings/application")
def update_application_settings(payload: dict, actor: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    if actor.role != "admin":
        raise HTTPException(403, "闇€瑕佺鐞嗗憳鏉冮檺")
    if not isinstance(payload, dict) or not payload or not set(payload).issubset(_FEATURE_CONFIG_SECTIONS):
        raise HTTPException(422, "鍙兘鏇存柊鍔熻兘閰嶇疆鍒嗘")
    try:
        # Validate the submitted sections against the canonical schema, then
        # persist only those sections so platform defaults remain independent
        # of any admin workspace's UI and path settings.
        patch = {key: payload[key] for key in payload}
        merged = core_config.AppConfig.model_validate({**core_config.AppConfig().model_dump(), **patch})
    except Exception as exc:
        raise HTTPException(422, f"Invalid feature configuration: {exc}") from exc
    config = db.get(SystemConfig, "application.features")
    if config is None:
        config = SystemConfig(key="application.features", value={})
        db.add(config)
    config.value = {**(config.value if isinstance(config.value, dict) else {}), **{
        key: getattr(merged, key).model_dump() for key in patch
    }}
    db.add(AuditLog(actor_user_id=actor.id, action="admin.application_features_changed", target_type="system_config", target_id="application.features", metadata_json={"sections": sorted(patch)}))
    db.commit()
    core_config.set_platform_config_cache(config.value)
    return {"config": core_config.get_config().model_dump(), "source": "admin"}


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


@router.get("/settings/runtime")
def get_runtime_settings(_: User = Depends(require_admin)) -> dict:
    """Expose non-secret, deployment-owned runtime limits to administrators."""
    return {
        "limits": {
            "max_upload_bytes": settings.max_upload_bytes,
            "session_ttl_hours": settings.session_ttl_hours,
            "task_lease_seconds": settings.task_lease_seconds,
            "task_max_attempts": settings.task_max_attempts,
        },
        "source": "deployment-environment",
        "editable_in_console": False,
        "model_settings_scope": "platform",
    }


@router.get("/users")
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    projects = dict(db.execute(select(Project.owner_id, func.count()).where(Project.deleted_at.is_(None)).group_by(Project.owner_id)).all())
    last_seen = dict(db.execute(select(UserSession.user_id, func.max(UserSession.last_seen_at)).group_by(UserSession.user_id)).all())
    file_usage = {
        user_id: {"count": int(count), "size_bytes": int(size)}
        for user_id, count, size in db.execute(
            select(ProjectFile.owner_id, func.count(), func.coalesce(func.sum(ProjectFile.size_bytes), 0))
            .where(ProjectFile.deleted_at.is_(None)).group_by(ProjectFile.owner_id)
        ).all()
    }
    quota_accounts = {account.user_id: account for account in db.scalars(select(UserQuotaAccount)).all()}
    workspace_usage: dict[str, dict[str, int]] = {}
    root = configured_storage_root(db)
    workspace_rows = db.execute(
        select(Workspace, User.id, User.username).join(User, User.id == Workspace.owner_id)
        .where(Workspace.deleted_at.is_(None))
    ).all()
    for workspace, user_id, username in workspace_rows:
        usage = workspace_usage.setdefault(user_id, {"workspace_count": 0, "storage_bytes": 0, "workspace_file_count": 0})
        usage["workspace_count"] += 1
        path = _workspace_path(root, username, workspace.id)
        if path is not None:
            measured = _scan_workspace(path)
            usage["storage_bytes"] += measured["size_bytes"]
            usage["workspace_file_count"] += measured["file_count"]
    return [{"id": item.id, "email": item.email, "username": item.username, "display_name": item.display_name,
             "role": item.role, "is_active": item.is_active, "created_at": item.created_at.isoformat(),
             "last_seen_at": last_seen[item.id].isoformat() if item.id in last_seen else None,
             "project_count": projects.get(item.id, 0),
             "workspace_count": workspace_usage.get(item.id, {}).get("workspace_count", 0),
             "workspace_file_count": workspace_usage.get(item.id, {}).get("workspace_file_count", 0),
             "storage_bytes": workspace_usage.get(item.id, {}).get("storage_bytes", 0),
             "file_count": file_usage.get(item.id, {}).get("count", 0),
             "file_bytes": file_usage.get(item.id, {}).get("size_bytes", 0),
             "available_units": quota_accounts[item.id].available_units if item.id in quota_accounts else 0,
             "reserved_units": quota_accounts[item.id].reserved_units if item.id in quota_accounts else 0,
             "consumed_units": quota_accounts[item.id].consumed_units if item.id in quota_accounts else 0} for item in users]


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
def list_all_tasks(status: str = "all", search: str = "", limit: int = 50,
                   _: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    query = select(Task)
    status_groups = {
        "queued": ("pending", "queued", "retrying"),
        "running": ("running", "paused", "cancelling"),
        "completed": ("succeeded",),
        "failed": ("failed", "timeout"),
        "cancelled": ("cancelled",),
    }
    if status in status_groups:
        query = query.where(Task.status.in_(status_groups[status]))
    elif status != "all":
        query = query.where(Task.status == status)
    if search:
        term = f"%{search.strip()}%"
        query = query.where(Task.id.ilike(term) | Task.task_type.ilike(term) | Task.project_id.ilike(term) |
                            Task.owner_id.in_(select(User.id).where(User.username.ilike(term))))
    rows = db.scalars(query.order_by(Task.created_at.desc()).limit(max(1, min(limit, 100)))).all()
    owners = {item.id: item.username for item in db.scalars(select(User)).all()}
    attempts = {}
    if rows:
        for attempt in db.scalars(select(TaskAttempt).where(TaskAttempt.task_id.in_([row.id for row in rows])).order_by(TaskAttempt.attempt_no.desc())).all():
            attempts.setdefault(attempt.task_id, attempt)
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
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            "worker_id": attempts[item.id].worker_id if item.id in attempts else None,
            "attempt_no": attempts[item.id].attempt_no if item.id in attempts else 0,
        }
        for item in rows
    ]


@router.get("/task-metrics")
def task_metrics(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Return an aggregate view for the admin operations dashboard.

    This is intentionally separate from the recent-task table: the table is
    capped for responsiveness, while these grouped counts cover every
    persisted task type and status.
    """
    rows = db.execute(
        select(Task.task_type, Task.status, func.count())
        .group_by(Task.task_type, Task.status)
    ).all()
    by_type: dict[str, dict[str, int]] = {}
    status_counts: dict[str, int] = {}
    for task_type, status, count in rows:
        typed = by_type.setdefault(str(task_type), {})
        typed[str(status)] = int(count)
        status_counts[str(status)] = status_counts.get(str(status), 0) + int(count)

    def count_statuses(*statuses: str) -> int:
        return sum(status_counts.get(status, 0) for status in statuses)

    now = utcnow()
    window_start = now - timedelta(seconds=60)
    throughput = {
        "window_seconds": 60,
        "submitted": int(db.scalar(select(func.count()).select_from(Task).where(Task.created_at >= window_start)) or 0),
        "started": int(db.scalar(select(func.count()).select_from(Task).where(Task.started_at >= window_start)) or 0),
        "completed": int(db.scalar(select(func.count()).select_from(Task).where(Task.finished_at >= window_start, Task.status == "succeeded")) or 0),
        "failed": int(db.scalar(select(func.count()).select_from(Task).where(Task.finished_at >= window_start, Task.status.in_(("failed", "timeout")))) or 0),
    }
    live_workers = [row for row in db.scalars(select(WorkerHeartbeat)).all() if not is_stale(row.last_seen_at)]
    worker_pool = {"online_workers": len(live_workers), "total_slots": 0, "active_slots": 0, "idle_slots": 0}
    for worker in live_workers:
        capabilities = worker.capabilities or {}
        slots = max(1, int(capabilities.get("slots", 1) or 1))
        active_slots = min(slots, max(0, int(capabilities.get("active_slots", 1 if worker.current_task_id else 0) or 0)))
        worker_pool["total_slots"] += slots
        worker_pool["active_slots"] += active_slots
    worker_pool["idle_slots"] = max(0, worker_pool["total_slots"] - worker_pool["active_slots"])
    running_count = count_statuses("running", "cancelling")
    has_slot_aware_simulator = any((row.capabilities or {}).get("simulation") is True for row in live_workers)
    consuming_count = min(running_count, worker_pool["active_slots"]) if has_slot_aware_simulator else running_count
    queued_count = count_statuses("queued", "retrying", "paused")
    if has_slot_aware_simulator:
        # The simulator's heartbeat is authoritative about active slots; any
        # additional running leases are buffered claims and belong in the queue.
        queued_count += max(0, running_count - consuming_count)

    return {
        "total": sum(status_counts.values()),
        "status_counts": status_counts,
        "stage_counts": {
            "production": count_statuses("pending"),
            "queued": queued_count,
            "consuming": consuming_count,
            "completed": count_statuses("succeeded"),
            "attention": count_statuses("failed", "timeout", "cancelled"),
        },
        "throughput_60s": throughput,
        "worker_pool": worker_pool,
        "by_type": [
            {"task_type": task_type, "total": sum(statuses.values()), "statuses": statuses}
            for task_type, statuses in sorted(by_type.items())
        ],
        "generated_at": utcnow().isoformat(),
    }


@router.get("/task-activity")
def task_activity(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    """Return recent durable task lifecycle events, including terminal outcomes."""
    rows = db.execute(
        select(TaskEvent, Task, User.username)
        .join(Task, Task.id == TaskEvent.task_id)
        .join(User, User.id == Task.owner_id)
        .order_by(TaskEvent.created_at.desc(), TaskEvent.sequence.desc())
        .limit(30)
    ).all()
    return [
        {
            "task_id": task.id,
            "task_type": task.task_type,
            "owner_username": username,
            "status": task.status,
            "progress": task.progress,
            "event_type": event.event_type,
            "payload": event.payload or {},
            "created_at": event.created_at.isoformat(),
        }
        for event, task, username in rows
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


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, actor: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    """Requeue a failed, zero-cost task while preserving its event history."""
    if actor.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status not in {"failed", "cancelled", "timeout"}:
        raise HTTPException(409, "任务当前不可重试")
    reservation = db.scalar(select(QuotaReservation).where(QuotaReservation.task_id == task.id))
    if reservation is not None and reservation.units:
        raise HTTPException(409, "带额度预留的任务暂不支持原任务重试")
    attempts = int(db.scalar(select(func.count()).select_from(TaskAttempt).where(TaskAttempt.task_id == task.id)) or 0)
    if attempts >= settings.task_max_attempts:
        raise HTTPException(409, "任务已达到最大尝试次数")
    if task.result is not None:
        db.delete(task.result)
    previous_status = task.status
    task.status = "pending"
    task.progress = 0
    task.error_code = ""
    task.error_message = ""
    task.started_at = None
    task.finished_at = None
    task.updated_at = utcnow()
    append_task_event(db, task.id, "admin_retry_requested", {"actor_user_id": actor.id, "status": "pending"})
    db.add(OutboxEvent(
        aggregate_type="task",
        aggregate_id=task.id,
        event_type="task.submitted",
        payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id},
    ))
    db.add(AuditLog(actor_user_id=actor.id, action="admin.task_retried", target_type="task",
                    target_id=task.id, metadata_json={"previous_status": previous_status, "attempts": attempts}))
    db.commit()
    return {"id": task.id, "status": task.status, "attempt_no": attempts}


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


def _gpu_status() -> list[dict]:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return []
    try:
        result = subprocess.run(
            [binary, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if result.returncode != 0:
            result = subprocess.run(
                [binary, "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, check=True,
            )

        def numeric(value: str, *, integer: bool = True):
            try:
                parsed = float(value)
                return int(parsed) if integer else round(parsed, 1)
            except (TypeError, ValueError):
                return None

        rows = []
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",", 6)]
            if len(parts) not in {6, 7}:
                continue
            index = numeric(parts[0])
            if index is None:
                continue
            rows.append({
                "index": index,
                "name": parts[1],
                "utilization_percent": numeric(parts[2]),
                "memory_used_mb": numeric(parts[3]),
                "memory_total_mb": numeric(parts[4]),
                "temperature_c": numeric(parts[5]),
                "power_w": numeric(parts[6], integer=False) if len(parts) == 7 else None,
            })
        return rows
    except (OSError, subprocess.SubprocessError):
        return []


def _task_module(task_type: str) -> str:
    prefix = task_type.split(".", 1)[0]
    if prefix in {"script", "music"}:
        return "llm"
    if prefix in {"tts", "voices"}:
        return "tts"
    if prefix in {"audio", "bgm"}:
        return "audio"
    if prefix in {"book", "text"}:
        return "system"
    return "worker"


@router.get("/overview")
def overview(tz_offset_minutes: int = 0, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    now = utcnow()
    offset = timedelta(minutes=max(-840, min(tz_offset_minutes, 840)))
    today = (now - offset).replace(hour=0, minute=0, second=0, microsecond=0) + offset
    metrics = task_metrics(_, db)
    queue = queue_status(_)
    gpu = _gpu_status()
    workers = list_workers(_, db)
    live = [worker for worker in workers if worker["status"] != "offline"]
    def has_worker(prefix: str) -> bool:
        return any(any(str(kind).startswith(prefix) for kind in worker["capabilities"].get("task_types", [])) for worker in live)
    from ..engines.tts import resolve_engine
    try:
        resolve_engine()
        tts_installed = True
    except (OSError, RuntimeError, FileNotFoundError):
        tts_installed = False
    services = [
        {"key": "api", "name": "API", "status": "healthy", "detail": "当前请求已响应"},
        {"key": "database", "name": "数据库", "status": "healthy", "detail": "查询正常"},
        {"key": "queue", "name": "Redis / 队列", "status": "healthy" if queue["available"] else "error", "detail": "连接正常" if queue["available"] else queue.get("error", "连接失败")},
        {"key": "llm", "name": "LLM Worker", "status": "healthy" if has_worker("script.") else "warning", "detail": "在线 Worker 可处理解析任务" if has_worker("script.") else "无可用解析 Worker"},
        {"key": "tts", "name": "TTS Worker", "status": "healthy" if tts_installed and has_worker("tts.") else "warning", "detail": "引擎与 Worker 可用" if tts_installed and has_worker("tts.") else "引擎或 Worker 未就绪"},
        {"key": "audio", "name": "FFmpeg", "status": "healthy" if shutil.which("ffmpeg") else "warning", "detail": "命令可用" if shutil.which("ffmpeg") else "未在 PATH 中找到"},
        {"key": "gpu", "name": "GPU", "status": "healthy" if gpu else "unknown", "detail": "已检测到 GPU" if gpu else "未采集到 GPU 数据"},
    ]
    recent_failures = db.scalars(select(Task).where(Task.status.in_(("failed", "timeout"))).order_by(Task.updated_at.desc()).limit(5)).all()
    api_metrics = api_snapshot()
    system_metrics = {"cpu_percent": None, "ram_used_bytes": None, "ram_total_bytes": None}
    try:
        import psutil
        ram_used, ram_total = _memory_usage(psutil.virtual_memory().total)
        system_metrics.update(cpu_percent=psutil.cpu_percent(interval=0.1),
                              ram_used_bytes=ram_used, ram_total_bytes=ram_total)
    except ImportError:
        pass
    recent_errors = [
        {"id": task.id, "time": task.updated_at.isoformat(), "module": _task_module(task.task_type),
         "type": task.error_code or task.status, "message": task.error_message or task.status}
        for task in recent_failures
    ]
    recent_errors.extend(
        {"id": f"api-{index}-{item['time']}", "time": item["time"], "module": "api",
         "type": f"HTTP {item['status']}", "message": f"{item['method']} {item['route']} 返回 {item['status']}"}
        for index, item in enumerate(api_metrics["recent_errors"])
    )
    return {
        "generated_at": now.isoformat(), "services": services,
        "today": {
            "completed": int(db.scalar(select(func.count()).select_from(Task).where(Task.finished_at >= today, Task.status == "succeeded")) or 0),
            "failed": int(db.scalar(select(func.count()).select_from(Task).where(Task.finished_at >= today, Task.status.in_(("failed", "timeout")))) or 0),
            "active_users": int(db.scalar(select(func.count(func.distinct(UserSession.user_id))).where(
                UserSession.revoked_at.is_(None), UserSession.expires_at > now,
                UserSession.last_seen_at >= now - timedelta(minutes=15))) or 0),
            "api_requests": api_requests_today(tz_offset_minutes),
            "llm_tokens": None,
            "tts_characters": None,
            "api_requests_scope": "当前 API 进程 · 控制台时区日期；进程重启后清零",
        },
        "tasks": {
            "running": metrics["stage_counts"]["consuming"],
            "queued": metrics["stage_counts"]["production"] + metrics["stage_counts"]["queued"],
            "failed": metrics["status_counts"].get("failed", 0) + metrics["status_counts"].get("timeout", 0),
        },
        "workers": metrics["worker_pool"], "queue": queue, "api": api_metrics, "system": system_metrics,
        "gpu": gpu, "user_count": int(db.scalar(select(func.count()).select_from(User)) or 0),
        "recent_errors": sorted(recent_errors, key=lambda item: item["time"], reverse=True)[:5],
    }


@router.get("/performance")
def performance(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    import os as platform_os
    disk = shutil.disk_usage(configured_storage_root(db))
    resource: dict = {"disk_total_bytes": disk.total, "disk_used_bytes": disk.used, "disk_free_bytes": disk.free,
                      "cpu_percent": None, "ram_used_bytes": None, "ram_total_bytes": None, "uptime_seconds": None}
    try:
        import psutil
        ram_used, ram_total = _memory_usage(psutil.virtual_memory().total)
        resource.update(cpu_percent=psutil.cpu_percent(interval=0.1), ram_used_bytes=ram_used,
                        ram_total_bytes=ram_total, uptime_seconds=int(utcnow().timestamp() - psutil.boot_time()))
    except ImportError:
        if hasattr(platform_os, "getloadavg"):
            resource["load_average_1m"] = platform_os.getloadavg()[0]
    return {"generated_at": utcnow().isoformat(), "system": resource, "gpu": _gpu_status(),
            "tasks": task_metrics(_, db), "workers": list_workers(_, db), "queue": queue_status(_),
            "api": api_snapshot(),
            "unavailable_metrics": ["LLM Token/TTFT/吞吐", "TTS 字符/实时倍率", "磁盘 I/O/网络"]}


@router.get("/resources")
def resources(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    from ..core.paths import MUSIC_LIBRARY_DIR
    root = configured_storage_root(db)
    disk = shutil.disk_usage(root)
    file_rows = db.execute(select(ProjectFile.kind, func.count(), func.coalesce(func.sum(ProjectFile.size_bytes), 0)).where(ProjectFile.deleted_at.is_(None)).group_by(ProjectFile.kind)).all()
    registered = {
        username: {"count": int(count), "size_bytes": int(size)}
        for username, count, size in db.execute(
            select(User.username, func.count(ProjectFile.id), func.coalesce(func.sum(ProjectFile.size_bytes), 0))
            .join(ProjectFile, ProjectFile.owner_id == User.id)
            .where(ProjectFile.deleted_at.is_(None)).group_by(User.username)
        ).all()
    }
    workspaces = db.execute(
        select(Workspace, User.username).join(User, User.id == Workspace.owner_id)
        .where(Workspace.deleted_at.is_(None))
    ).all()
    active_project_ids = set(db.scalars(
        select(Task.project_id).where(Task.status.in_(_ACTIVE_TASK_STATUSES)).distinct()
    ).all())
    users: dict[str, dict] = {}
    category_totals: dict[str, dict[str, int]] = {}
    cleanup_count = cleanup_bytes = 0
    for workspace, username in workspaces:
        row = users.setdefault(username, {"workspace_count": 0, "size_bytes": 0, "file_count": 0})
        row["workspace_count"] += 1
        path = _workspace_path(root, username, workspace.id)
        measured = _scan_workspace(path, active=workspace.id in active_project_ids) if path is not None else {
            "size_bytes": 0, "file_count": 0, "categories": {}, "cleanup_count": 0, "cleanup_bytes": 0,
        }
        row["size_bytes"] += measured["size_bytes"]
        row["file_count"] += measured["file_count"]
        cleanup_count += measured["cleanup_count"]
        cleanup_bytes += measured["cleanup_bytes"]
        for category, values in measured["categories"].items():
            total = category_totals.setdefault(category, {"count": 0, "size_bytes": 0})
            total["count"] += values["count"]
            total["size_bytes"] += values["size_bytes"]
    user_rows = [
        {"username": username, **values, "registered_file_count": registered.get(username, {}).get("count", 0),
         "registered_file_bytes": registered.get(username, {}).get("size_bytes", 0)}
        for username, values in sorted(users.items(), key=lambda item: item[1]["size_bytes"], reverse=True)[:20]
    ]
    music_usage = _music_use_counts(workspaces, root)
    music_files = [path for path in MUSIC_LIBRARY_DIR.iterdir() if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".mp3", ".wav", ".flac"}] if MUSIC_LIBRARY_DIR.is_dir() else []
    return {"root_path": str(root), "disk_total_bytes": disk.total, "disk_used_bytes": disk.used, "disk_free_bytes": disk.free,
            "workspaces": int(db.scalar(select(func.count()).select_from(Workspace).where(Workspace.deleted_at.is_(None))) or 0),
            "projects": int(db.scalar(select(func.count()).select_from(Project).where(Project.deleted_at.is_(None))) or 0),
            "files": [{"kind": kind, "count": count, "size_bytes": size} for kind, count, size in file_rows],
            "users": user_rows,
            "workspace_storage": {
                "size_bytes": sum(row["size_bytes"] for row in users.values()),
                "file_count": sum(row["file_count"] for row in users.values()),
                "categories": [
                    {"kind": key, "label": _WORKSPACE_CATEGORY_LABELS[key], **values}
                    for key, values in sorted(category_totals.items(), key=lambda item: item[1]["size_bytes"], reverse=True)
                ],
                "cleanup_candidates": {"count": cleanup_count, "size_bytes": cleanup_bytes,
                                       "older_than_days": _TEMP_CLEANUP_AGE_DAYS},
            },
            "music_library": {"count": len(music_files), "size_bytes": sum(path.stat().st_size for path in music_files),
                              "assigned_chapters": sum(music_usage.values())},
            "scope": "工作空间目录按只读文件扫描（跳过符号链接）；用户的缓存、临时文件、音频、日志均计入。模型位于工作空间之外时不计入此处。"}


@router.post("/resources/cleanup-temp")
def cleanup_stale_temp(actor: User = Depends(require_csrf), db: Session = Depends(get_db)) -> dict:
    """Delete only old regular files in inactive workspaces' 00_temp folders."""
    if actor.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    root = configured_storage_root(db)
    workspaces = db.execute(
        select(Workspace, User.username).join(User, User.id == Workspace.owner_id)
        .where(Workspace.deleted_at.is_(None))
    ).all()
    active_project_ids = set(db.scalars(
        select(Task.project_id).where(Task.status.in_(_ACTIVE_TASK_STATUSES)).distinct()
    ).all())
    cutoff = datetime.now().timestamp() - _TEMP_CLEANUP_AGE_DAYS * 24 * 60 * 60
    removed_count = removed_bytes = skipped = 0
    root_resolved = root.resolve()
    for workspace, username in workspaces:
        if workspace.id in active_project_ids:
            continue
        workspace_path = _workspace_path(root, username, workspace.id)
        if workspace_path is None:
            continue
        temp_dir = workspace_path / "00_temp"
        if temp_dir.is_symlink() or not temp_dir.is_dir():
            continue
        for directory, child_dirs, filenames in os.walk(temp_dir, topdown=True, followlinks=False):
            parent = Path(directory)
            child_dirs[:] = [name for name in child_dirs if not (parent / name).is_symlink()]
            for filename in filenames:
                path = parent / filename
                if path.is_symlink():
                    continue
                try:
                    resolved = path.resolve()
                    stat = path.stat()
                    if not resolved.is_relative_to(root_resolved) or stat.st_mtime >= cutoff:
                        continue
                    path.unlink()
                    removed_count += 1
                    removed_bytes += stat.st_size
                except OSError:
                    skipped += 1
    db.add(AuditLog(actor_user_id=actor.id, action="admin.temp_cleanup", target_type="storage",
                    target_id=str(root), metadata_json={"files": removed_count, "bytes": removed_bytes,
                                                       "age_days": _TEMP_CLEANUP_AGE_DAYS, "skipped": skipped}))
    db.commit()
    return {"deleted_count": removed_count, "deleted_bytes": removed_bytes, "skipped_count": skipped,
            "older_than_days": _TEMP_CLEANUP_AGE_DAYS}


@router.get("/events")
def admin_events(level: str = "all", module: str = "all", search: str = "", limit: int = 50, since_hours: int = 24,
                 _: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    limit = max(1, min(limit, 100))
    cutoff = utcnow() - timedelta(hours=max(1, min(since_hours, 24 * 30)))
    task_rows = db.scalars(select(Task).where(Task.status.in_(("failed", "timeout")), Task.updated_at >= cutoff).order_by(Task.updated_at.desc()).limit(100)).all()
    audit_rows = db.scalars(select(AuditLog).where(AuditLog.created_at >= cutoff).order_by(AuditLog.created_at.desc()).limit(100)).all()
    rows = ([{"id": task.id, "time": task.updated_at.isoformat(), "level": "error", "module": _task_module(task.task_type),
              "type": task.error_code or task.status, "message": task.error_message or task.status} for task in task_rows] +
            [{"id": audit.id, "time": audit.created_at.isoformat(), "level": "info", "module": "system",
              "type": audit.action, "message": f"{audit.target_type} {audit.target_id}"} for audit in audit_rows])
    rows.extend(
        {"id": f"api-{item['time']}-{item['route']}", "time": item["time"], "level": "error", "module": "api",
         "type": f"HTTP {item['status']}", "message": f"{item['method']} {item['route']} 返回 {item['status']}"}
        for item in api_snapshot(window_seconds=min(300, max(60, since_hours * 60)))["recent_errors"]
    )
    if level != "all":
        rows = [row for row in rows if row["level"] == level]
    if module != "all":
        rows = [row for row in rows if row["module"] == module]
    if search:
        term = search.casefold()
        rows = [row for row in rows if term in f'{row["type"]} {row["message"]} {row["id"]}'.casefold()]
    return sorted(rows, key=lambda row: row["time"], reverse=True)[:limit]
