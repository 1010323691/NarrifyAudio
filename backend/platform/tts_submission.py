"""Bounded, atomic TTS submissions. No script or audio contents are read here."""
from __future__ import annotations

import hashlib
import json
import uuid
from sqlalchemy import func, insert, select
from fastapi import HTTPException

from ..core.filenames import workspace_audio_identity, package_stem
from ..core.observability import timed_tts_stage
from .models import Project, SystemConfig, Task, TaskBatch, TaskEvent, OutboxEvent, UserQuotaAccount, utcnow
from .project_context import active_project
from .storage import lock_storage_migration, storage_migration
from .task_lifecycle import ACTIVE_TASK_STATUSES
from .task_validation import legacy_task_payload_error
from .gpu_scheduler.store import host_lock
from .platform_settings import settings

MAX_BATCH_CHAPTERS = 500
MAX_USER_PENDING = 1000
MAX_GLOBAL_PENDING = 10000


def batch_receipt(batch):
    result = {"batch_id": batch.id, "task_ids": list(batch.task_ids)}
    if len(batch.task_ids) == 1:
        result["task_id"] = batch.task_ids[0]
    return result


def _packages(payload):
    names = payload.get("scripts") or ([payload["script"]] if payload.get("script") else [])
    return {package_stem(name.removesuffix(".json").removesuffix("_checked")).casefold() for name in names}


@timed_tts_stage('submission')
def submit_tts_tasks(*, task_type, entries, ctx, db, idempotency_key=None, project_id=None):
    if not 1 <= len(entries) <= MAX_BATCH_CHAPTERS:
        raise HTTPException(422, "一次最多提交 500 章")
    for entry in entries:
        error = legacy_task_payload_error(task_type, entry["payload"])
        if error:
            raise HTTPException(422, error)
    if sum(len(e['payload'].get('scripts') or ([e['payload']['script']] if e['payload'].get('script') else [])) for e in entries) > MAX_BATCH_CHAPTERS:
        raise HTTPException(422, '一次最多提交 500 章')
    packages = [_packages(e['payload']) for e in entries]
    names_count = sum(len(e['payload'].get('scripts') or ([e['payload']['script']] if e['payload'].get('script') else [])) for e in entries)
    if names_count != len(set().union(*packages)):
        raise HTTPException(422, '所选章节指向重复的音频包')
    key = idempotency_key or str(uuid.uuid4())
    if not isinstance(key, str) or not 1 <= len(key) <= 180 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise HTTPException(422, "无效的幂等键")
    project = db.scalar(select(Project).where(Project.id == project_id, Project.owner_id == ctx.user.id,
                                              Project.deleted_at.is_(None))) if project_id else active_project(db, ctx.user, ctx.session)
    if project is None:
        raise HTTPException(409, "尚未设置工作空间")
    request = {"project": project.id, "type": task_type,
               "entries": [{k: v for k, v in e["payload"].items() if k != "config"} for e in entries]}
    digest = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # Covers SQLite tests too; PostgreSQL row locks remain the durable fence.
    with host_lock():
        try:
            if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
                raise HTTPException(409, "存储根目录正在迁移，暂时无法提交任务")
            # Serialize capacity checks across users without scanning full payloads.
            guard = db.get(SystemConfig, "tts.submission.capacity", with_for_update=True)
            if guard is None:
                guard = SystemConfig(key="tts.submission.capacity", value={})
                db.add(guard)
                db.flush()
            db.scalar(select(Project).where(Project.id == project.id).with_for_update())
            account = db.scalar(select(UserQuotaAccount).where(UserQuotaAccount.user_id == ctx.user.id).with_for_update())
            existing = db.scalar(select(TaskBatch).where(TaskBatch.owner_id == ctx.user.id, TaskBatch.idempotency_key == key))
            if existing:
                if existing.request_hash != digest:
                    raise HTTPException(409, "幂等键对应的请求内容不同")
                response = batch_receipt(existing)
                db.rollback()
                return response
            if not settings.tts_submissions_enabled:
                raise HTTPException(503, '合成提交暂时停用，请稍后重试', headers={'Retry-After': '10'})
            if task_type == "tts.batch" and (account is None or account.available_units <= 0):
                raise HTTPException(409, "额度不足")
            targets = set().union(*(_packages(e["payload"]) for e in entries))
            active = db.execute(select(Task.id, Task.payload).where(
                Task.owner_id == ctx.user.id, Task.project_id == project.id,
                Task.task_type.in_(("tts.batch", "tts.reset")), Task.status.in_(ACTIVE_TASK_STATUSES),
            )).all()
            conflicts = [tid for tid, payload in active if targets & _packages(payload)
                         or task_type == 'tts.reset' and not targets or not _packages(payload)]
            if conflicts:
                raise HTTPException(409, {"message": "所选章节已有在途任务，请等待其结束", "task_ids": conflicts, "code": "active_tts_conflict"})
            if task_type == "tts.batch":
                counts = db.execute(select(Task.owner_id, func.sum(Task.admission_units)).where(
                    Task.task_type == "tts.batch", Task.status.in_(ACTIVE_TASK_STATUSES),
                ).group_by(Task.owner_id)).all()
                units = sum(max(1, len(_packages(e['payload']))) for e in entries)
                if sum(count for _, count in counts) + units > MAX_GLOBAL_PENDING or dict(counts).get(ctx.user.id, 0) + units > MAX_USER_PENDING:
                    raise HTTPException(429, "待合成队列已满，请稍后重试", headers={"Retry-After": "10"})
            batch_id = str(uuid.uuid4())
            ids = [str(uuid.uuid4()) for _ in entries]
            batch = TaskBatch(id=batch_id, owner_id=ctx.user.id, project_id=project.id,
                              task_type=task_type, idempotency_key=key, request_hash=digest,
                              config=entries[0]["payload"].get("config", {}), task_ids=ids)
            if task_type == "tts.batch" and project_id:
                from ..core.request_context import bind_workspace, reset_workspace
                from ..core.config import get_config
                from .storage import project_workspace_path
                token = bind_workspace(project_workspace_path(db, ctx.user.username, project.id))
                try:
                    batch.config = get_config().model_dump(mode="json")
                finally:
                    reset_workspace(token)
            db.add(batch)
            db.flush()
            now = utcnow()
            rows, events = [], []
            for tid, entry in zip(ids, entries):
                payload = {k: v for k, v in entry["payload"].items() if k != "config"}
                payload.update(label=entry["label"], execution_batch=batch_id, _batch_config_id=batch_id, _request_hash=digest)
                identity = workspace_audio_identity(task_type, payload)
                if identity:
                    payload["_audio_identity"] = identity
                rows.append(dict(id=tid, owner_id=ctx.user.id, project_id=project.id, task_type=task_type,
                                 payload=payload, batch_id=batch_id, event_sequence=1, ui_state={},
                                 admission_units=max(1, len(_packages(payload))),
                                 status="pending", progress=0, idempotency_key=f"tts:{batch_id}:{tid}",
                                 created_at=now, updated_at=now))
                events.append(dict(id=str(uuid.uuid4()), task_id=tid, sequence=1, event_type="submitted",
                                   payload={"status": "pending", "estimated_units": 0}, created_at=now))
            for offset in range(0, len(rows), 100):
                db.execute(insert(Task), rows[offset:offset + 100])
                db.execute(insert(TaskEvent), events[offset:offset + 100])
            # One wake-up is sufficient: fair selection reads all committed tasks.
            db.add(OutboxEvent(aggregate_type="task", aggregate_id=ids[0], event_type="task.submitted",
                               payload={"task_id": ids[0], "task_type": task_type, "project_id": project.id, "batch_id": batch_id}))
            db.commit()
            return batch_receipt(batch)
        except BaseException:
            db.rollback()
            raise
