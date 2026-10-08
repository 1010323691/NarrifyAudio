"""Atomic merge receipts: cheap admission, one configuration and one commit."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from ..core.config import get_config
from ..core.filenames import workspace_audio_identity
from ..core.request_context import bind_workspace, reset_workspace
from .models import OutboxEvent, Project, Task, TaskBatch, TaskEvent, utcnow
from .project_context import active_project
from .storage import lock_storage_migration, project_workspace_path, storage_migration
from .task_lifecycle import ACTIVE_TASK_STATUSES
from .task_validation import legacy_task_payload_error, is_safe_path_component

MAX_MERGE_PACKAGES = 1000
log = logging.getLogger(__name__)


def merge_receipt(batch, packages):
    return {"batch_id": batch.id, "task_ids": list(batch.task_ids),
            "packages": [{"package": name, "task_id": tid}
                         for name, tid in zip(packages, batch.task_ids)]}


def submit_merge_tasks(*, packages, ctx, db, idempotency_key=None, project_id=None, preflight=None):
    packages = list(dict.fromkeys(packages))
    start = time.monotonic()
    lock_ms = write_ms = 0.0
    outcome = "failed"
    if not packages:
        raise HTTPException(400, "请选择要合并的音频包。")
    if len(packages) > MAX_MERGE_PACKAGES:
        raise HTTPException(422, "一次最多提交 1000 个音频包")
    for name in packages:
        error = legacy_task_payload_error("tts.merge", {"package": name})
        if error or not is_safe_path_component(name):
            raise HTTPException(400, error or "音频包名称无效")
    key = idempotency_key or str(uuid.uuid4())
    if not isinstance(key, str) or not 1 <= len(key) <= 180 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise HTTPException(422, "无效的幂等键")
    digest = None
    try:
        if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
            raise HTTPException(409, "存储根目录正在迁移，暂时无法提交任务")
        project = (db.scalar(select(Project).where(Project.id == project_id, Project.owner_id == ctx.user.id,
                    Project.deleted_at.is_(None))) if project_id else active_project(db, ctx.user, ctx.session))
        if project is None:
            raise HTTPException(409, "尚未设置工作空间")
        request = {"project": project.id, "type": "tts.merge", "packages": packages}
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        lock_start = time.monotonic()
        # All admissions to this project use this row; no quota-account lock is needed.
        project = db.scalar(select(Project).where(Project.id == project.id, Project.owner_id == ctx.user.id,
                    Project.deleted_at.is_(None)).with_for_update())
        lock_ms = (time.monotonic() - lock_start) * 1000
        if project is None:
            raise HTTPException(404, "项目不存在")
        existing = db.scalar(select(TaskBatch).where(TaskBatch.owner_id == ctx.user.id,
                                                      TaskBatch.idempotency_key == key))
        if existing is not None:
            if existing.request_hash != digest:
                raise HTTPException(409, "幂等键对应的请求内容不同")
            response = merge_receipt(existing, packages)
            db.rollback()
            outcome = "replayed"
            return response
        active = db.scalars(select(Task.payload["package"].as_string()).where(
            Task.owner_id == ctx.user.id, Task.project_id == project.id, Task.task_type == "tts.merge",
            Task.status.in_(ACTIVE_TASK_STATUSES))).all()
        conflicts = sorted(set(packages) & set(active))
        if conflicts or None in active:  # Legacy unspecified-package merges are exclusive.
            raise HTTPException(409, "以下包已有合并任务在途：" + "、".join(conflicts or packages))
        token = bind_workspace(project_workspace_path(db, ctx.user.username, project.id))
        try:
            if preflight is not None:
                preflight(packages)
            snapshot = get_config().model_dump(mode="json")
        finally:
            reset_workspace(token)
        write_start = time.monotonic()
        batch_id = str(uuid.uuid4())
        ids = [str(uuid.uuid4()) for _ in packages]
        batch = TaskBatch(id=batch_id, owner_id=ctx.user.id, project_id=project.id,
                          task_type="tts.merge", idempotency_key=key, request_hash=digest,
                          config=snapshot, task_ids=ids)
        db.add(batch)
        db.flush()
        now = utcnow()
        rows, events = [], []
        for name, tid in zip(packages, ids):
            payload = {"package": name, "label": "merge-audio: " + name,
                       "_batch_config_id": batch_id, "_request_hash": digest}
            payload["_audio_identity"] = workspace_audio_identity("tts.merge", payload)
            rows.append(dict(id=tid, owner_id=ctx.user.id, project_id=project.id, task_type="tts.merge",
                             payload=payload, batch_id=batch_id, event_sequence=1, ui_state={},
                             admission_units=1, status="pending", progress=0,
                             idempotency_key=f"merge:{batch_id}:{tid}", created_at=now, updated_at=now))
            events.append(dict(id=str(uuid.uuid4()), task_id=tid, sequence=1, event_type="submitted",
                               payload={"status": "pending", "estimated_units": 0}, created_at=now))
        for offset in range(0, len(rows), 100):
            db.execute(insert(Task), rows[offset:offset + 100])
            db.execute(insert(TaskEvent), events[offset:offset + 100])
        db.add(OutboxEvent(aggregate_type="task", aggregate_id=ids[0], event_type="task.submitted",
                          payload={"task_id": ids[0], "task_type": "tts.merge",
                                   "project_id": project.id, "batch_id": batch_id}))
        db.commit()
        write_ms = (time.monotonic() - write_start) * 1000
        outcome = "submitted"
        return merge_receipt(batch, packages)
    except IntegrityError:
        # The owner/key uniqueness fence also covers requests for different projects.
        db.rollback()
        existing = db.scalar(select(TaskBatch).where(TaskBatch.owner_id == ctx.user.id,
                                                      TaskBatch.idempotency_key == key))
        if existing is not None and digest is not None:
            if existing.request_hash != digest:
                raise HTTPException(409, "幂等键对应的请求内容不同") from None
            outcome = "replayed"
            return merge_receipt(existing, packages)
        raise
    except BaseException:
        db.rollback()
        raise
    finally:
        log.info("合并提交：章节 %d · 状态 %s · 锁等待 %.1fms · 入库 %.1fms · 总耗时 %.1fms",
                 len(packages), outcome, lock_ms, write_ms, (time.monotonic() - start) * 1000)
