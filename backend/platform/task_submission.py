"""Durable task submission record operations shared by services and platform adapters."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_config
from .models import OutboxEvent, Project, ProjectFile, Task, User, UserQuotaAccount, utcnow
from .storage import lock_storage_migration, storage_migration
from .task_lifecycle import append_task_event
from .task_types import ADMIN_ONLY_TASK_TYPES, BILLABLE_TASK_TYPES, SUPPORTED_TASK_TYPES
from .task_validation import legacy_task_payload_error


class TaskSubmissionError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def task_dict(task: Task) -> dict:
    result = task.result.result if task.result is not None else None
    payload = task.payload if isinstance(task.payload, dict) else {}
    # 输入标识从提交数据（payload）读取而不是 result：失败 / 取消任务没有
    # result，UI 仍需要据此把任务关联回具体的源文件（如解析页的章节行）。
    return {
        "id": task.id, "project_id": task.project_id, "task_type": task.task_type,
        "status": task.status, "progress": task.progress,
        "error_code": task.error_code, "error_message": task.error_message,
        "result": result, "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "source_name": payload.get("source_name"),
        "source_file_id": payload.get("input_file_id"),
        "source_file_ids": payload.get("input_file_ids"),
    }


def submit_task_record(
    db: Session, user: User, *, project_id: str, task_type: str,
    payload: dict, idempotency_key: str, commit: bool = True,
) -> Task:
    if task_type not in SUPPORTED_TASK_TYPES:
        raise TaskSubmissionError(422, f"不支持的任务类型：{task_type}")
    if task_type in ADMIN_ONLY_TASK_TYPES and user.role != "admin":
        raise TaskSubmissionError(403, "需要管理员权限")
    error = legacy_task_payload_error(task_type, payload)
    if error:
        raise TaskSubmissionError(422, error)
    if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
        raise TaskSubmissionError(409, "存储根目录正在迁移，暂时无法提交任务")
    # ``estimated_units`` was removed from the submission surface, but it
    # stays in the idempotency hash PINNED to 0 — every request that ever
    # reached production sent 0, so replays of historical requests keep the
    # exact same hash (actual model output/input characters are metered by
    # the engines, never by this estimate).
    request_body = {
        "project_id": project_id, "task_type": task_type,
        "payload": payload, "estimated_units": 0,
    }
    request_hash = hashlib.sha256(json.dumps(request_body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    project = db.scalar(select(Project).where(
        Project.id == project_id, Project.owner_id == user.id, Project.deleted_at.is_(None),
    ).with_for_update())
    if project is None:
        raise TaskSubmissionError(404, "项目不存在")
    existing = db.scalar(select(Task).where(Task.owner_id == user.id, Task.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.payload.get("_request_hash") != request_hash:
            raise TaskSubmissionError(409, "幂等键对应的请求内容不同")
        return existing
    if payload.get("input_file_ids"):
        for file_id in payload["input_file_ids"]:
            source = db.scalar(select(ProjectFile).where(
                ProjectFile.id == file_id, ProjectFile.owner_id == user.id,
                ProjectFile.project_id == project_id, ProjectFile.deleted_at.is_(None),
            ))
            if source is None:
                raise TaskSubmissionError(404, "源文件不存在或已删除")
    from .resource_delivery import validate_delivery_sources, DeliveryDenied
    try:
        validate_delivery_sources(db, user, project_id, task_type, payload)
    except DeliveryDenied as exc:
        raise TaskSubmissionError(403, str(exc)) from exc
    if task_type.startswith("resources."):
        from .resource_inventory import ResourceError, owned_project, current_snapshot, split_resource_id

        try:
            references = payload.get("snapshots") or (payload.get("scope") or {}).get("snapshots") or []
            for reference in references:
                owned_project(db, user, reference["project_id"])
                current = current_snapshot(db, user.id, reference["project_id"])
                if current is None or current[1]["snapshot_id"] != reference["snapshot_id"]:
                    raise ResourceError("资源清单已变化，请刷新后重试")
            for item in payload.get("files") or []:
                selected_project, _ = split_resource_id(item["resource_id"])
                owned_project(db, user, selected_project)
                current = current_snapshot(db, user.id, selected_project)
                if current is None or current[1]["snapshot_id"] != item["snapshot_id"]:
                    raise ResourceError("资源清单已变化，请刷新后重试")
            for selected_project in payload.get("project_ids") or []:
                owned_project(db, user, selected_project)
            if task_type == "resources.package":
                from .resource_tasks import package_selection
                delivery_count = len(package_selection(db, user, payload))
        except ResourceError as exc:
            raise TaskSubmissionError(exc.status, str(exc)) from exc
        if task_type == "resources.scan":
            from .task_lifecycle import ACTIVE_TASK_STATUSES

            active_scan = db.scalar(select(Task).where(
                Task.owner_id == user.id, Task.project_id == project_id,
                Task.task_type == task_type, Task.status.in_(ACTIVE_TASK_STATUSES),
            ).order_by(Task.created_at.desc()))
            if active_scan is not None and active_scan.payload.get("project_ids") == payload.get("project_ids") and bool(active_scan.payload.get("include_trash")) == bool(payload.get("include_trash")):
                return active_scan
    account = db.scalar(select(UserQuotaAccount).where(UserQuotaAccount.user_id == user.id).with_for_update())
    if account is None:
        account = UserQuotaAccount(user_id=user.id, available_units=0)
        db.add(account)
        db.flush()
    if task_type in BILLABLE_TASK_TYPES and account.available_units <= 0:
        raise TaskSubmissionError(409, "额度不足")
    # Account row locking serializes concurrent submissions for this user.
    existing = db.scalar(select(Task).where(Task.owner_id == user.id, Task.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.payload.get("_request_hash") != request_hash:
            raise TaskSubmissionError(409, "幂等键对应的请求内容不同")
        if commit:
            db.rollback()
        return existing
    # Resolve shared policy only for a NEW task, after both idempotency checks.
    # Keep the original request hash independent of changing admin defaults.
    stored_payload = dict(payload)
    if task_type == "resources.scan":
        from .task_lifecycle import ACTIVE_TASK_STATUSES

        # The quota-account row serializes scans for this owner on PostgreSQL.
        # Overlapping scans cannot publish older pointers over newer snapshots.
        targets = set(payload.get("project_ids") or [project_id])
        for running in db.scalars(select(Task).where(
            Task.owner_id == user.id, Task.task_type == task_type,
            Task.status.in_(ACTIVE_TASK_STATUSES),
        )).all():
            running_targets = set(running.payload.get("project_ids") or [running.project_id])
            if targets & running_targets or payload.get("include_trash") and running.payload.get("include_trash"):
                if targets <= running_targets and (not payload.get("include_trash") or running.payload.get("include_trash")):
                    return running
                raise TaskSubmissionError(409, "部分项目正在更新资源，请在完成后重新读取")
        stored_payload["scan_scope"] = ",".join(sorted(payload.get("project_ids") or [project_id]))
    elif task_type == "resources.package":
        stored_payload["export_id"] = idempotency_key
        stored_payload["delivery_count"] = delivery_count
    elif task_type == "resources.cleanup":
        stored_payload["cleanup_id"] = idempotency_key
    if task_type == "book.split":
        split = get_config().split
        stored_payload["split_policy"] = {
            "smart_split_long_chapters": split.smart_split_long_chapters,
            "length_target": split.length_target,
        }
    task = Task(
        owner_id=user.id, project_id=project.id, task_type=task_type,
        payload={**stored_payload, "_request_hash": request_hash}, idempotency_key=idempotency_key,
    )
    db.add(task)
    db.flush()
    append_task_event(db, task.id, "submitted", {"status": "pending", "estimated_units": 0})
    db.add(OutboxEvent(
        aggregate_type="task", aggregate_id=task.id, event_type="task.submitted",
        payload={"task_id": task.id, "task_type": task_type, "project_id": project_id},
    ))
    if commit:
        db.commit()
    return task
