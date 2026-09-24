"""Durable task submission independent of the HTTP route module."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..platform.models import OutboxEvent, Project, Task, User, UserQuotaAccount
from ..platform.task_state import append_task_event
from ..platform.storage import lock_storage_migration, storage_migration


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
    lock_storage_migration(db, shared=True)
    if storage_migration(db) is not None:
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
    if task_type in {"script.parse", "voices.foundation", "voices.clone", "tts.batch", "bgm.analysis", "bgm.segment", "music.suggest_tags"} and account.available_units <= 0:
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
        payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id},
    ))
    db.commit()
    return task
