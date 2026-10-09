from __future__ import annotations

import csv
import io
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Literal, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only

from ..platform.platform_settings import settings
from ..platform.database import SessionLocal, get_db, pool_status
from ..platform.deps import AuthContext, get_auth_context, require_admin, require_admin_csrf
from ..platform.models import AuditLog, Project, ProjectFile, QuotaTransaction, SystemConfig, Task, TaskAttempt, TaskEvent, User, UserQuotaAccount, UserSession, WorkerHeartbeat, utcnow
from ..platform.security import revoke_session
from ..platform.system_config import parse_worker_concurrency
from ..platform.task_admission import llm_task_limit
from ..platform.tts_resource_budget import tts_batch_activity
from ..platform.system_probe import database_metrics, gpu_status, host_metrics, queue_status
from ..platform.task_registry import TASK_TYPES
from ..platform.worker_registry import live_worker_pool
from ..platform.storage import configured_storage_root, lock_storage_migration, storage_username, storage_migration
from ..platform.system_config import client_logs_enabled, update_feature_defaults_cache
from ..platform.task_lifecycle import ACTIVE_TASK_STATUSES, TERMINAL_TASK_STATUSES
from ..platform.worker_registry import is_stale
from ..services.task_operations import RetryNotAllowedError, check_retry_eligible, task_worker_group
from ..services.task_operations import cancel_task_record, requeue_task_record
from ..core.observability import api_requests_today, api_snapshot
from ..core import config as core_config
from ..engines import llm_transport
from ..engines.script_prompts import load_default_prompts
from ..services.admin_storage import (
    scan_project_directory,
    project_storage_path,
)



from ..services.admin_analytics import today_chars, user_counts, user_daily_usage
from ..services.admin_lists import event_page, event_union, filtered_events
from ..services.list_paging import page_meta
from ..services.user_provisioning import ProvisioningError, provision_user

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

class UserState(BaseModel):
    is_active: bool | None = None
    role: Literal["user", "admin"] | None = None


class StorageRootUpdate(BaseModel):
    root_path: str


class InitialQuotaUpdate(BaseModel):
    units: int = Field(ge=0, le=10_000_000)


class ClientLogsUpdate(BaseModel):
    enabled: bool = Field(strict=True)


@router.patch("/settings/client-logs")
def update_client_logs(payload: ClientLogsUpdate, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
    row = db.get(SystemConfig, "client.logs")
    if row is None:
        row = SystemConfig(key="client.logs", value={})
        db.add(row)
    row.value = {"enabled": payload.enabled}
    db.add(AuditLog(actor_user_id=actor.id, action="admin.client_logs_changed",
                    target_type="system_config", target_id="client.logs", metadata_json=row.value))
    db.commit()
    return {"enabled": client_logs_enabled(db)}


class RegistrationUpdate(BaseModel):
    enabled: bool


_FEATURE_CONFIG_SECTIONS = {
    "text", "audio", "tts", "llm", "prompts", "persona_prompts",
    "generation", "ffmpeg", "bgm", "split",
}


@router.get("/settings/gpu-scheduler")
def get_gpu_scheduler_settings(_: User = Depends(require_admin)) -> dict:
    from ..platform.gpu_scheduler.config import load_config
    return {"config": load_config().model_dump()}


@router.patch("/settings/gpu-scheduler")
def update_gpu_scheduler_settings(payload: dict, actor: User = Depends(require_admin_csrf)) -> dict:
    from ..platform.gpu_scheduler.config import GPUConfig, load_config, validate_enabled
    from ..platform.gpu_scheduler.store import transaction
    with transaction() as (db, _state):
        try:
            config = GPUConfig.model_validate({**load_config(db).model_dump(), **payload})
            validate_enabled(config)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        row = db.get(SystemConfig, "gpu_scheduler")
        if row is None:
            row = SystemConfig(key="gpu_scheduler", value={})
            db.add(row)
        row.value = config.model_dump()
        db.add(AuditLog(actor_user_id=actor.id, action="admin.gpu_scheduler_changed",
                        target_type="system_config", target_id="gpu_scheduler", metadata_json={"fields": sorted(payload)}))
    return {"config": config.model_dump()}


@router.get("/gpu-scheduler/status")
def get_gpu_scheduler_status(_: User = Depends(require_admin)) -> dict:
    from ..platform.gpu_scheduler.runtime import status_snapshot
    return status_snapshot()


@router.post("/gpu-scheduler/recover", status_code=202)
def recover_gpu_scheduler(actor: User = Depends(require_admin_csrf)) -> dict:
    from ..platform.gpu_scheduler.store import transaction
    with transaction() as (db, state):
        if state["state"] != "ERROR":
            raise HTTPException(409, "调度器当前不处于 ERROR 状态")
        state["recover_requested"] = True
        db.add(AuditLog(actor_user_id=actor.id, action="admin.gpu_scheduler_recovery_requested",
                        target_type="gpu_scheduler", target_id="local", metadata_json={}))
    return {"accepted": True}

# 6 个解析检查开关由用户在「文本解析」页勾选并随任务参数提交（api/script.py 的
# ParseChecks）——用户专属。不得持久化成平台功能默认：get_config() 会把
# application.features 深合并**覆盖**工作区值，平台一旦有值就会压掉用户存进项目
# 配置的「上次选择」。持久化时剔除这些键（已存在的旧值在下一次保存 generation
# 段时被整段替换清掉）。
_USER_OWNED_CHECKS = {
    "check_chunk_alignment", "check_boundary_speakers", "validate_instructs",
    "revalidate_splits", "check_long_paragraphs", "spot_check_enabled",
}


def _application_config_with_prompt_defaults() -> dict:
    """Echo bundled parsing prompts when the platform config leaves them blank."""
    config = core_config.get_config().model_dump()
    prompts = config.setdefault("prompts", {})
    system_prompt, user_prompt = load_default_prompts()
    if not prompts.get("system_prompt"):
        prompts["system_prompt"] = system_prompt
    if not prompts.get("user_prompt"):
        prompts["user_prompt"] = user_prompt
    return config


@router.get("/settings/application")
def get_application_settings(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Return effective feature settings, including shared admin overrides."""
    config = _application_config_with_prompt_defaults()
    stored = db.get(SystemConfig, "application.features")
    return {"config": config, "source": "admin" if stored else "deployment-default"}


@router.patch("/settings/application")
def update_application_settings(payload: dict, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
    if not isinstance(payload, dict) or not payload or not set(payload).issubset(_FEATURE_CONFIG_SECTIONS):
        raise HTTPException(422, "只能更新功能配置分段")
    try:
        # Validate the submitted sections against the canonical schema, then
        # persist only those sections so platform defaults remain independent
        # of any admin workspace's UI and path settings.
        patch = {key: payload[key] for key in payload}
        merged = core_config.AppConfig.model_validate({**core_config.AppConfig().model_dump(), **patch})
    except Exception as exc:
        raise HTTPException(422, f"Invalid feature configuration: {exc}") from exc
    if "llm" in patch:
        from ..platform.gpu_scheduler.config import load_config, validate_enabled
        from ..platform.gpu_scheduler.store import read_state
        try:
            gpu_config = load_config(db)
            if read_state()["managed"] and not gpu_config.enabled:
                gpu_config = gpu_config.model_copy(update={"enabled": True})
            validate_enabled(gpu_config, merged.llm)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    config = db.get(SystemConfig, "application.features")
    if config is None:
        config = SystemConfig(key="application.features", value={})
        db.add(config)
    sections = {}
    for key in patch:
        value = getattr(merged, key).model_dump()
        if key == "generation":
            value = {k: v for k, v in value.items() if k not in _USER_OWNED_CHECKS}
        if key == "text":
            value.pop("split_long_continuous_chapters", None)
        sections[key] = value
    config.value = {**(config.value if isinstance(config.value, dict) else {}), **sections}
    db.add(AuditLog(actor_user_id=actor.id, action="admin.application_features_changed", target_type="system_config", target_id="application.features", metadata_json={"sections": sorted(patch)}))
    db.commit()
    update_feature_defaults_cache(config.value)
    return {"config": _application_config_with_prompt_defaults(), "source": "admin"}


@router.get("/llm/models")
def get_llm_models(base_url: str = "", api_key: str = "",
                   _: User = Depends(require_admin)) -> dict:
    """List the model names the LLM endpoint exposes (its ``/models`` payload).

    ``base_url`` / ``api_key`` let the console probe form values that are not
    saved yet; when ``base_url`` is empty the effective platform config is
    probed instead (read-only, so no CSRF — same surface as the other GETs).
    """
    if base_url:
        target_url, target_key = base_url, api_key
    else:
        llm = core_config.get_config().llm
        target_url, target_key = llm.base_url, llm.api_key
    if not target_url:
        raise HTTPException(422, "请先填写 LLM 服务地址（API 地址）")
    try:
        models = llm_transport.list_llm_models(target_url, target_key)
    except llm_transport.LLMModelsFetchError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"models": models}


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
    migration = storage_migration(db)
    return {
        "root_path": str(configured_storage_root(db)), "source": source,
        "migration": migration.value if migration is not None else None,
    }


@router.patch("/settings/storage")
def update_storage_settings(payload: StorageRootUpdate, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
    # Keep this transaction independent from the durable migration marker's
    # commits: only one administrator may move/compensate directories at a time.
    with SessionLocal() as guard:
        if not lock_storage_migration(guard):
            raise HTTPException(409, "存储正在使用或迁移，请稍后重试")
        return _update_storage_settings(payload, actor, db)


def _update_storage_settings(payload: StorageRootUpdate, actor: User, db: Session) -> dict:
    raw = payload.root_path.strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(422, "存储根目录必须是绝对路径")
    resolved = path.resolve()
    previous = configured_storage_root(db)
    migration = storage_migration(db)
    if migration is not None:
        details = migration.value if isinstance(migration.value, dict) else {}
        if details.get("source") != str(previous) or details.get("target") != str(resolved):
            raise HTTPException(409, f"已有未完成的存储迁移，请先恢复至 {details.get('target') or '原目标'}")
    if resolved == previous and migration is None:
        return {"root_path": str(resolved), "source": "admin" if db.get(SystemConfig, "storage.root") else "deployment-default"}
    if resolved.is_relative_to(previous) or previous.is_relative_to(resolved):
        raise HTTPException(422, "新旧存储根目录不能互相嵌套")
    new_migration = migration is None
    if new_migration:
        active_task = db.scalar(select(Task.id).where(Task.status.in_(ACTIVE_TASK_STATUSES)).limit(1))
        if active_task is not None:
            raise HTTPException(409, "仍有未完成任务，存储迁移需在任务排空后进行")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(422, f"无法创建存储根目录：{exc}") from exc
    moved: list[tuple[Path, Path]] = []
    directory_keys = {item.directory_key for item in db.scalars(select(Project)).all()}
    directory_keys.update(
        f"{storage_username(username)}/{project.id}"
        for project, username in db.execute(
            select(Project, User.username).join(User, User.id == Project.owner_id)
        ).all()
    )
    # Validate all paths before making the migration durable. A typo or a
    # conflicting target must not leave every write endpoint disabled.
    for directory_key in directory_keys:
        source = (previous / directory_key).resolve()
        target = (resolved / directory_key).resolve()
        if not source.is_relative_to(previous) or not target.is_relative_to(resolved):
            raise HTTPException(422, "工作空间目录键越界")
        if source.exists() and (source.is_symlink() or target.exists()):
            raise HTTPException(409, f"工作空间目录迁移冲突：{directory_key}")
    if new_migration:
        migration = SystemConfig(
            key="storage.migration", value={"source": str(previous), "target": str(resolved)},
        )
        db.add(migration)
        db.commit()

    def compensate():
        db.rollback()
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
        # Retain the marker if a cross-volume move left a partial target, or
        # this request resumed moves performed by an earlier process.
        if new_migration and all(not (resolved / key).exists() for key in directory_keys):
            marker = storage_migration(db)
            if marker is not None:
                db.delete(marker)
                db.commit()
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
        compensate()
        raise
    except OSError as exc:
        compensate()
        raise HTTPException(422, f"工作空间目录迁移失败：{exc}") from exc
    config = db.get(SystemConfig, "storage.root")
    if config is None:
        config = SystemConfig(key="storage.root", value={"path": str(resolved)})
        db.add(config)
    else:
        config.value = {"path": str(resolved)}
    db.add(AuditLog(actor_user_id=actor.id, action="admin.storage_root_changed", target_type="system_config", target_id="storage.root", metadata_json={"path": str(resolved)}))
    db.delete(migration)
    try:
        db.commit()
    except Exception:
        compensate()
        raise
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
def update_quota_settings(payload: InitialQuotaUpdate, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
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
def update_registration_settings(payload: RegistrationUpdate, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
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
def list_users(_: User = Depends(require_admin), db: Session = Depends(get_db),
               page: Annotated[int | None, Query(ge=1)] = None, page_size: Annotated[int, Query(ge=1, le=100)] = 20,
               search: str = "", role: str = "all", state: str = "all", sort: str = "default"):
    statement = select(User)
    if search.strip():
        term = "%" + search.strip() + "%"
        statement = statement.where(User.username.ilike(term) | User.display_name.ilike(term) | User.email.ilike(term))
    if role != "all": statement = statement.where(User.role == role)
    if state != "all": statement = statement.where(User.is_active.is_(state == "active"))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    if sort == "name": statement = statement.order_by(User.username, User.id)
    elif sort == "storage":
        usage = select(ProjectFile.owner_id, func.sum(ProjectFile.size_bytes).label("size")).where(ProjectFile.deleted_at.is_(None)).group_by(ProjectFile.owner_id).subquery()
        statement = statement.outerjoin(usage, usage.c.owner_id == User.id).order_by(func.coalesce(usage.c.size, 0).desc(), User.id)
    else: statement = statement.order_by(User.created_at.desc(), User.id)
    if page is not None: statement = statement.offset((page-1)*page_size).limit(page_size)
    users = db.scalars(statement).all()
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
    if page is not None:
        # The paged list reports catalog counts, never walks audio workspaces.
        workspace_usage = {user_id: {"storage_bytes": value["size_bytes"], "project_file_count": value["count"]}
                           for user_id, value in file_usage.items()}
    else:
        root = configured_storage_root(db)
        workspace_rows = db.execute(
            select(Project, User.id, User.username).join(User, User.id == Project.owner_id)
            .where(Project.deleted_at.is_(None), User.id.in_([u.id for u in users]))
        ).all()
        for workspace, user_id, username in workspace_rows:
            usage = workspace_usage.setdefault(user_id, {"storage_bytes": 0, "project_file_count": 0})
            path = project_storage_path(root, username, workspace.id)
            if path is not None:
                measured = scan_project_directory(path)
                usage["storage_bytes"] += measured["size_bytes"]
                usage["project_file_count"] += measured["file_count"]
    items = [{"id": item.id, "email": item.email, "username": item.username, "display_name": item.display_name,
             "role": item.role, "is_active": item.is_active, "created_at": item.created_at.isoformat(),
             "last_seen_at": last_seen[item.id].isoformat() if item.id in last_seen else None,
             "project_count": projects.get(item.id, 0),
             "project_file_count": workspace_usage.get(item.id, {}).get("project_file_count", 0),
             "storage_bytes": workspace_usage.get(item.id, {}).get("storage_bytes", 0),
             "file_count": file_usage.get(item.id, {}).get("count", 0),
             "file_bytes": file_usage.get(item.id, {}).get("size_bytes", 0),
             "available_units": quota_accounts[item.id].available_units if item.id in quota_accounts else 0,
             "reserved_units": quota_accounts[item.id].reserved_units if item.id in quota_accounts else 0,
             "consumed_units": quota_accounts[item.id].consumed_units if item.id in quota_accounts else 0} for item in users]

    return {"items": items, "pagination": page_meta(total, page, page_size, {"all": total, "active_admins": db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))) or 0, **user_counts(db)})} if page is not None else items


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: UserState, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
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


class UserCreate(BaseModel):
    email: str = Field(max_length=320)
    username: str = Field(min_length=6, max_length=20)
    password: str = Field(max_length=256)
    display_name: str = Field(default="", max_length=120)
    role: Literal["user", "admin"] = "user"


@router.post("/users", status_code=201)
def create_user(payload: UserCreate, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
    """Administrator provisioning; works whether or not self-registration is open."""
    try:
        user, _project = provision_user(db, email=payload.email, username=payload.username, password=payload.password,
                                        display_name=payload.display_name, role=payload.role)
    except ProvisioningError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.message) from exc
    db.add(AuditLog(actor_user_id=actor.id, action="admin.user_created", target_type="user", target_id=user.id,
                    metadata_json={"username": user.username, "role": user.role}))
    try:
        db.commit()
    except IntegrityError as exc:  # a concurrent request took the same email or username
        db.rollback()
        raise HTTPException(409, "邮箱或用户名已被占用，请刷新后重试") from exc
    return {"id": user.id, "email": user.email, "username": user.username, "display_name": user.display_name,
            "role": user.role, "is_active": user.is_active, "created_at": user.created_at.isoformat()}


def _active_sessions(db: Session, user_id: str):
    return select(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None),
                                     UserSession.expires_at > utcnow())


@router.get("/users/{user_id}")
def user_detail(user_id: str, tz_offset_minutes: int = 0, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "用户不存在")
    account = db.get(UserQuotaAccount, user_id)
    sessions = db.scalars(_active_sessions(db, user_id)).all()
    statuses = dict(db.execute(select(Task.status, func.count()).where(Task.owner_id == user_id).group_by(Task.status)).all())
    recent = db.scalars(select(Task).options(load_only(Task.id, Task.task_type, Task.status, Task.progress, Task.created_at,
                                                       Task.finished_at, raiseload=True))
                        .where(Task.owner_id == user_id).order_by(Task.created_at.desc(), Task.id.desc()).limit(10)).all()
    last_seen = max((row.last_seen_at for row in sessions), default=None)
    return {
        "id": user.id,
        "quota": _quota_json(account) if account else None,
        "sessions": {"active": len(sessions), "last_seen_at": last_seen.isoformat() if last_seen else None},
        "task_statuses": {str(key): int(value) for key, value in statuses.items()},
        "recent_tasks": [{"id": row.id, "task_type": row.task_type, "status": row.status, "progress": row.progress,
                          "created_at": row.created_at.isoformat(),
                          "finished_at": row.finished_at.isoformat() if row.finished_at else None} for row in recent],
        "daily_usage": user_daily_usage(db, user_id, 30, tz_offset_minutes),
    }


@router.get("/users/{user_id}/quota-transactions")
def user_quota_transactions(user_id: str, page: Annotated[int, Query(ge=1)] = 1,
                            page_size: Annotated[int, Query(ge=1, le=100)] = 20,
                            _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    if db.get(User, user_id) is None:
        raise HTTPException(404, "用户不存在")
    statement = select(QuotaTransaction).where(QuotaTransaction.user_id == user_id)
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.scalars(statement.order_by(QuotaTransaction.created_at.desc(), QuotaTransaction.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"id": row.id, "time": row.created_at.isoformat(), "kind": row.kind, "amount": row.amount,
                       "resource_type": row.resource_type, "operation_type": row.operation_type, "char_count": row.char_count,
                       "available_after": row.available_after, "consumed_after": row.consumed_after,
                       "task_id": row.task_id, "note": row.note} for row in rows],
            "pagination": page_meta(total, page, page_size)}


@router.post("/users/{user_id}/sessions/revoke")
def revoke_user_sessions(user_id: str, ctx: AuthContext = Depends(get_auth_context),
                         actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
    """Force sign-out everywhere. An administrator's own current session is kept."""
    if db.get(User, user_id) is None:
        raise HTTPException(404, "用户不存在")
    revoked = 0
    for session in db.scalars(_active_sessions(db, user_id).with_for_update()).all():
        if session.id == ctx.session.id:
            continue
        revoke_session(session)
        revoked += 1
    db.add(AuditLog(actor_user_id=actor.id, action="admin.sessions_revoked", target_type="user", target_id=user_id,
                    metadata_json={"revoked": revoked}))
    db.commit()
    return {"id": user_id, "revoked": revoked}


def _quota_json(account: UserQuotaAccount) -> dict:
    return {
        "user_id": account.user_id,
        "available_units": account.available_units,
        "reserved_units": account.reserved_units,
        "frozen_units": account.frozen_units,
        "consumed_units": account.consumed_units,
    }


@router.post("/users/{user_id}/quota/adjust")
def adjust_user_quota(user_id: str, payload: QuotaAdjustment, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
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
                   page: Annotated[int | None, Query(ge=1)] = None, group: str = "all", task_type: str = "all",
                   _: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict] | dict:
    query = select(Task).options(load_only(Task.id, Task.owner_id, Task.project_id, Task.task_type,
        Task.status, Task.progress, Task.error_code, Task.error_message, Task.created_at, Task.updated_at,
        Task.started_at, Task.finished_at, raiseload=True))
    status_groups = {
        "queued": ("pending", "queued", "retrying"),
        "running": ("running", "cancelling"),
        "completed": ("succeeded",),
        "failed": ("failed", "timeout"),
        "cancelled": ("cancelled",),
    }
    if status in status_groups:
        query = query.where(Task.status.in_(status_groups[status]))
    elif status != "all":
        query = query.where(Task.status == status)
    if task_type != "all":
        query = query.where(Task.task_type == task_type)
    if group != "all":
        query = query.where(Task.task_type.in_([name for name in TASK_TYPES if task_worker_group(name) == group]))
    if search:
        term = f"%{search.strip()}%"
        query = query.where(Task.id.ilike(term) | Task.task_type.ilike(term) | Task.project_id.ilike(term) |
                            Task.owner_id.in_(select(User.id).where(User.username.ilike(term))))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    size = max(1, min(limit, 100))
    rows = db.scalars(query.order_by(Task.created_at.desc(), Task.id.desc()).offset(((page or 1)-1)*size).limit(size)).all()
    owners = {item.id: item.username for item in db.scalars(select(User)).all()}
    attempts = {}
    if rows:
        for attempt in db.scalars(select(TaskAttempt).where(TaskAttempt.task_id.in_([row.id for row in rows])).order_by(TaskAttempt.attempt_no.desc())).all():
            attempts.setdefault(attempt.task_id, attempt)
    items = [
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
            "group": task_worker_group(item.task_type),
        }
        for item in rows
    ]


    return {"items": items, "pagination": page_meta(total, page, size)} if page is not None else items


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
    worker_pool, has_slot_aware_simulator = live_worker_pool(db)
    running_count = count_statuses("running", "cancelling")
    consuming_count = min(running_count, worker_pool["active_slots"]) if has_slot_aware_simulator else running_count
    queued_count = count_statuses("queued", "retrying")
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


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status in TERMINAL_TASK_STATUSES:
        return {"id": task.id, "status": task.status}
    previous_status = task.status
    changed = cancel_task_record(db, task, admin=True, actor_user_id=actor.id)
    if changed:
        db.add(AuditLog(actor_user_id=actor.id, action="admin.task_cancelled", target_type="task", target_id=task.id, metadata_json={"previous_status": previous_status}))
    db.commit()
    return {"id": task.id, "status": task.status}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: str, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
    """Requeue a failed, zero-cost task while preserving its event history."""
    task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise HTTPException(404, "任务不存在")
    try:
        attempts = check_retry_eligible(db, task)
    except RetryNotAllowedError as exc:
        raise HTTPException(409, exc.message) from exc
    previous_status = task.status
    requeue_task_record(
        db, task, event_type="admin_retry_requested",
        event_payload={"actor_user_id": actor.id, "status": "pending"},
    )
    db.add(AuditLog(actor_user_id=actor.id, action="admin.task_retried", target_type="task",
                    target_id=task.id, metadata_json={"previous_status": previous_status, "attempts": attempts}))
    db.commit()
    return {"id": task.id, "status": task.status, "attempt_no": attempts}


def _event_summary(payload: dict) -> str:
    """One line per task event; payloads may hold large results, so only short scalars."""
    if not isinstance(payload, dict):
        return ""
    parts = []
    for key in ("status", "stage", "progress", "message", "error", "error_code", "worker_id", "reason"):
        value = payload.get(key)
        if value not in (None, "", [], {}) and isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}={value}" if key not in {"message", "error"} else str(value))
    return " · ".join(parts)[:300]


@router.get("/tasks/{task_id}")
def task_detail(task_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    owner = db.get(User, task.owner_id)
    project = db.get(Project, task.project_id)
    attempts = db.scalars(select(TaskAttempt).where(TaskAttempt.task_id == task_id).order_by(TaskAttempt.attempt_no)).all()
    events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.sequence.desc()).limit(100)).all()
    iso = lambda value: value.isoformat() if value else None  # noqa: E731
    return {
        "id": task.id, "task_type": task.task_type, "group": task_worker_group(task.task_type), "status": task.status,
        "progress": task.progress, "error_code": task.error_code, "error_message": task.error_message,
        "owner_id": task.owner_id, "owner_username": owner.username if owner else "",
        "project_id": task.project_id, "project_name": project.name if project else None,
        "created_at": iso(task.created_at), "updated_at": iso(task.updated_at), "started_at": iso(task.started_at),
        "finished_at": iso(task.finished_at), "next_attempt_at": iso(task.next_attempt_at), "batch_id": task.batch_id,
        "attempts": [{"attempt_no": row.attempt_no, "worker_id": row.worker_id, "status": row.status,
                      "started_at": iso(row.started_at), "finished_at": iso(row.finished_at),
                      "error_message": row.error_message} for row in attempts],
        "events": [{"sequence": row.sequence, "type": row.event_type, "time": iso(row.created_at),
                    "summary": _event_summary(row.payload)} for row in reversed(events)],
    }


class BulkTaskAction(BaseModel):
    action: Literal["cancel", "retry"]
    ids: list[str] = Field(min_length=1, max_length=100)


@router.post("/tasks/bulk")
def bulk_task_action(payload: BulkTaskAction, actor: User = Depends(require_admin_csrf), db: Session = Depends(get_db)) -> dict:
    """Cancel or requeue several tasks; each task is decided (and committed) on its own."""
    results = []
    for task_id in dict.fromkeys(payload.ids):
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None:
            results.append({"id": task_id, "ok": False, "reason": "任务不存在"})
            continue
        previous_status = task.status
        if payload.action == "cancel":
            if task.status in TERMINAL_TASK_STATUSES:
                db.rollback()
                results.append({"id": task_id, "ok": False, "reason": "任务已结束"})
                continue
            changed = cancel_task_record(db, task, admin=True, actor_user_id=actor.id)
            if changed:
                db.add(AuditLog(actor_user_id=actor.id, action="admin.task_cancelled", target_type="task", target_id=task.id,
                                metadata_json={"previous_status": previous_status, "bulk": True}))
            db.commit()
            results.append({"id": task_id, "ok": True, "changed": bool(changed), "status": task.status})
            continue
        try:
            attempts = check_retry_eligible(db, task)
        except RetryNotAllowedError as exc:
            db.rollback()
            results.append({"id": task_id, "ok": False, "reason": exc.message})
            continue
        requeue_task_record(db, task, event_type="admin_retry_requested",
                            event_payload={"actor_user_id": actor.id, "status": "pending"})
        db.add(AuditLog(actor_user_id=actor.id, action="admin.task_retried", target_type="task", target_id=task.id,
                        metadata_json={"previous_status": previous_status, "attempts": attempts, "bulk": True}))
        db.commit()
        results.append({"id": task_id, "ok": True, "changed": True, "status": task.status})
    return {"action": payload.action, "succeeded": sum(1 for row in results if row["ok"]), "results": results}


def _list_workers(db: Session) -> list[dict]:
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


@router.get("/overview")
def overview(tz_offset_minutes: int = 0, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    now = utcnow()
    offset = timedelta(minutes=max(-840, min(tz_offset_minutes, 840)))
    today = (now - offset).replace(hour=0, minute=0, second=0, microsecond=0) + offset
    metrics = task_metrics(_, db)
    queue = queue_status()
    gpu = gpu_status()
    from ..platform.mechanical_audio import status as audio_status
    audio_admission = audio_status(db)
    workers = _list_workers(db)
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
        {"key": "audio_admission", "name": "音频任务调度",
         "status": "error" if audio_admission["error"] else "warning" if audio_admission["memory_paused"] else "healthy",
         "detail": audio_admission["error"] or ("可用内存不足，等待恢复" if audio_admission["memory_paused"] else
                    f"全机执行 {audio_admission['active']}/{audio_admission['limit']} 个合并/混音任务")},
    ]
    recent_failures = db.scalars(select(Task).where(Task.status.in_(("failed", "timeout"))).order_by(Task.updated_at.desc()).limit(5)).all()
    api_metrics = api_snapshot()
    host = host_metrics(cpu_interval=0.1)
    recent_errors = [
        {"id": task.id, "time": task.updated_at.isoformat(), "module": task_worker_group(task.task_type),
         "type": task.error_code or task.status, "message": task.error_message or task.status}
        for task in recent_failures
    ]
    recent_errors.extend(
        {"id": f"api-{index}-{item['time']}", "time": item["time"], "module": "api",
         "type": f"HTTP {item['status']}", "message": f"{item['method']} {item['route']} 返回 {item['status']}"}
        for index, item in enumerate(api_metrics["recent_errors"])
    )
    chars = today_chars(db, today)
    database = database_metrics(db)
    return {
        "generated_at": now.isoformat(), "services": services,
        "today": {
            "completed": int(db.scalar(select(func.count()).select_from(Task).where(Task.finished_at >= today, Task.status == "succeeded")) or 0),
            "failed": int(db.scalar(select(func.count()).select_from(Task).where(Task.finished_at >= today, Task.status.in_(("failed", "timeout")))) or 0),
            "submitted": int(db.scalar(select(func.count()).select_from(Task).where(Task.created_at >= today)) or 0),
            "active_users": int(db.scalar(select(func.count(func.distinct(UserSession.user_id))).where(
                UserSession.revoked_at.is_(None), UserSession.expires_at > now,
                UserSession.last_seen_at >= now - timedelta(minutes=15))) or 0),
            "api_requests": api_requests_today(tz_offset_minutes),
            "tts_chars": chars["tts"],
            "llm_chars": chars["llm"],
            "api_requests_scope": "当前 API 进程 · 控制台时区日期；不含状态检查；重启后清零",
        },
        "tasks": {
            "running": metrics["stage_counts"]["consuming"],
            "queued": metrics["stage_counts"]["production"] + metrics["stage_counts"]["queued"],
            "failed": metrics["status_counts"].get("failed", 0) + metrics["status_counts"].get("timeout", 0),
        },
        "workers": metrics["worker_pool"], "queue": queue, "api": api_metrics,
        "system": {"cpu_percent": host["cpu_percent"], "ram_used_bytes": host["ram_used_bytes"], "ram_total_bytes": host["ram_total_bytes"]},
        "database": database["connections"] if database else None,
        "gpu": gpu, "audio_admission": audio_admission, "user_count": int(db.scalar(select(func.count()).select_from(User)) or 0),
        "recent_errors": sorted(recent_errors, key=lambda item: item["time"], reverse=True)[:5],
    }


@router.get("/performance")
def performance(_: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    return {"generated_at": utcnow().isoformat(), "system": host_metrics(configured_storage_root(db)), "gpu": gpu_status(),
            "tasks": task_metrics(_, db), "workers": _list_workers(db), "queue": queue_status(include_info=True),
            "api": api_snapshot(), "api_pool": pool_status(), "database": database_metrics(db),
            "llm_limit": parse_worker_concurrency(db=db), "llm_task_limit": llm_task_limit(db),
            "tts_batch": tts_batch_activity(db)}


def _recent_api_errors(since_hours: int) -> list[dict]:
    return api_snapshot(window_seconds=min(300, max(60, since_hours * 60)))["recent_errors"]


@router.get("/events")
def admin_events(level: str = "all", module: str = "all", search: str = "", limit: int = 50, since_hours: int = 24,
                 page: Annotated[int | None, Query(ge=1)] = None,
                 _: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict] | dict:
    limit = max(1, min(limit, 100))
    if page is not None:
        return event_page(db, page, limit, level, module, search, since_hours, _recent_api_errors(since_hours))
    cutoff = utcnow() - timedelta(hours=max(1, min(since_hours, 24 * 30)))
    task_rows = db.scalars(select(Task).where(Task.status.in_(("failed", "timeout")), Task.updated_at >= cutoff).order_by(Task.updated_at.desc()).limit(100)).all()
    audit_rows = db.scalars(select(AuditLog).where(AuditLog.created_at >= cutoff).order_by(AuditLog.created_at.desc()).limit(100)).all()
    rows = ([{"id": task.id, "time": task.updated_at.isoformat(), "level": "error", "module": task_worker_group(task.task_type),
              "type": task.error_code or task.status, "message": task.error_message or task.status} for task in task_rows] +
            [{"id": audit.id, "time": audit.created_at.isoformat(), "level": "info", "module": "system",
              "type": audit.action, "message": f"{audit.target_type} {audit.target_id}"} for audit in audit_rows])
    rows.extend(
        {"id": f"api-{item['time']}-{item['route']}", "time": item["time"], "level": "error", "module": "api",
         "type": f"HTTP {item['status']}", "message": f"{item['method']} {item['route']} 返回 {item['status']}"}
        for item in _recent_api_errors(since_hours)
    )
    if level != "all":
        rows = [row for row in rows if row["level"] == level]
    if module != "all":
        rows = [row for row in rows if row["module"] == module]
    if search:
        term = search.casefold()
        rows = [row for row in rows if term in f'{row["type"]} {row["message"]} {row["id"]}'.casefold()]
    return sorted(rows, key=lambda row: row["time"], reverse=True)[:limit]


_EXPORT_LIMIT = 5000


def _csv_cell(value):
    """Neutralise spreadsheet formulas: summaries can carry user-controlled text."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/events/export")
def export_events(level: str = "all", module: str = "all", search: str = "", since_hours: int = 24,
                  _: User = Depends(require_admin), db: Session = Depends(get_db)) -> StreamingResponse:
    """CSV of the current filter (newest first, at most 5000 rows), BOM-prefixed for Excel."""
    events = event_union(since_hours, _recent_api_errors(since_hours))
    statement = filtered_events(events, level, module, search).order_by(events.c.time.desc(), events.c.id.desc()).limit(_EXPORT_LIMIT)
    buffer = io.StringIO()
    buffer.write("\ufeff")
    writer = csv.writer(buffer)
    writer.writerow(["时间", "级别", "模块", "类型", "摘要", "ID"])
    for row in db.execute(statement).mappings():
        writer.writerow([row["time"].isoformat(), *(_csv_cell(row[key]) for key in ("level", "module", "type", "message")), row["id"]])
    filename = f"narrify-events-{utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    return StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/events/audit/{audit_id}")
def audit_event(audit_id: str, _: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    audit = db.get(AuditLog, audit_id)
    if audit is None:
        raise HTTPException(404, "审计记录不存在")
    actor = db.get(User, audit.actor_user_id) if audit.actor_user_id else None
    return {"id": audit.id, "action": audit.action, "target_type": audit.target_type, "target_id": audit.target_id,
            "metadata": audit.metadata_json or {}, "created_at": audit.created_at.isoformat(),
            "actor": {"id": actor.id, "username": actor.username} if actor else None}
