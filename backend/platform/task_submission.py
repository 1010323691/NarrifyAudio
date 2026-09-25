"""Durable task submission record operations shared by services and platform adapters."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import OutboxEvent, Project, Task, User, UserQuotaAccount, utcnow
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
    return {
        "id": task.id, "project_id": task.project_id, "task_type": task.task_type,
        "status": task.status, "progress": task.progress,
        "error_code": task.error_code, "error_message": task.error_message,
        "result": result, "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def submit_task_record(
    db: Session, user: User, *, project_id: str, task_type: str,
    payload: dict, estimated_units: int, idempotency_key: str,
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
    # Estimated task size remains part of the historical idempotency contract;
    # actual model output/input characters are metered by the engines.
    request_body = {
        "project_id": project_id, "task_type": task_type,
        "payload": payload, "estimated_units": estimated_units,
    }
    request_hash = hashlib.sha256(json.dumps(request_body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    project = db.scalar(select(Project).where(
        Project.id == project_id, Project.owner_id == user.id, Project.deleted_at.is_(None),
    ))
    if project is None:
        raise TaskSubmissionError(404, "项目不存在")
    existing = db.scalar(select(Task).where(Task.owner_id == user.id, Task.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.payload.get("_request_hash") != request_hash:
            raise TaskSubmissionError(409, "幂等键对应的请求内容不同")
        return existing
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
        db.rollback()
        return existing
    task = Task(
        owner_id=user.id, project_id=project.id, task_type=task_type,
        payload={**payload, "_request_hash": request_hash}, idempotency_key=idempotency_key,
    )
    db.add(task)
    db.flush()
    append_task_event(db, task.id, "submitted", {"status": "pending", "estimated_units": 0})
    db.add(OutboxEvent(
        aggregate_type="task", aggregate_id=task.id, event_type="task.submitted",
        payload={"task_id": task.id, "task_type": task_type, "project_id": project_id},
    ))
    db.commit()
    return task
