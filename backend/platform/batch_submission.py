"""Atomic, bounded admission for independent parse, voice and BGM tasks.

The client intent identifies a replay before mutable preflight is repeated;
the durable request hash also binds the resolved inputs and configuration.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import timedelta, timezone
import hashlib
import json
import uuid

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from ..core.filenames import filename_aliases, workspace_audio_identity
from .gpu_scheduler.store import host_lock
from .models import OutboxEvent, Project, ProjectFile, Task, TaskBatch, TaskEvent, UserQuotaAccount, utcnow
from .storage import lock_storage_migration, storage_migration
from .task_lifecycle import ACTIVE_TASK_STATUSES
from .task_registry import BILLABLE_TASK_TYPES
from .task_submission import TaskSubmissionError
from .task_validation import legacy_task_payload_error

MAX_BATCH_TASKS = 1000
BATCH_TYPES = frozenset({"script.parse", "voices.foundation", "voices.clone", "bgm.segment", "bgm.match", "bgm.mix"})


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def batch_receipt(batch):
    result = {"batch_id": batch.id, "task_ids": list(batch.task_ids)}
    result.update(batch.config.get("_batch_receipt") or {})
    if len(batch.task_ids) == 1:
        result["task_id"] = batch.task_ids[0]
    return result


def _replay(db, owner_id, key, intent):
    batch = db.scalar(select(TaskBatch).where(TaskBatch.owner_id == owner_id, TaskBatch.idempotency_key == key))
    if batch is None:
        return None
    if batch.config.get("_batch_intent") != intent:
        raise TaskSubmissionError(409, "幂等键对应的请求内容不同")
    return batch_receipt(batch)


def _targets(kind, payload):
    if kind == "script.parse":
        file_id = payload.get("input_file_id")
        return {"id:" + file_id} if file_id else {"name:" + name for name in filename_aliases(payload.get("source_name") or "")}
    if kind.startswith("voices."):
        # One voice profile is shared across scripts in this project.
        return set(payload.get("speakers") or [])
    return set(payload.get("chapters") or ([payload["stem"]] if payload.get("stem") else []))


def _validate_inputs_and_conflicts(db, user, project, kind, entries):
    wanted = set()
    for entry in entries:
        payload = entry["payload"]
        error = legacy_task_payload_error(kind, payload)
        if error:
            raise TaskSubmissionError(422, error)
        targets = _targets(kind, payload)
        if not targets or wanted & targets:
            raise TaskSubmissionError(422, "批次包含无效或重复的任务目标")
        wanted.update(targets)
    if kind == "script.parse":
        ids = [entry["payload"].get("input_file_id") for entry in entries]
        files = {item.id: item for item in db.scalars(select(ProjectFile).where(
            ProjectFile.id.in_(ids), ProjectFile.owner_id == user.id,
            ProjectFile.project_id == project.id, ProjectFile.deleted_at.is_(None))) }
        if len(files) != len(ids):
            raise TaskSubmissionError(404, "源文件不存在或已删除")
        aliases = set()
        for entry in entries:
            payload = entry["payload"]
            source = files[payload["input_file_id"]]
            expected = payload.get("input_sha256")
            if expected and expected != source.sha256:
                raise TaskSubmissionError(409, "分册文本已变更，请刷新后重新选择。")
            payload["input_sha256"] = source.sha256
            aliases.update("name:" + name for name in filename_aliases(source.original_name))
        wanted.update(aliases)
    types = ({"voices.foundation", "voices.clone"} if kind.startswith("voices.") else
             {"bgm.segment", "bgm.match", "bgm.mix"} if kind.startswith("bgm.") else {kind})
    active = db.execute(select(Task.id, Task.task_type, Task.payload).where(
        Task.owner_id == user.id, Task.project_id == project.id,
        Task.task_type.in_(types), Task.status.in_(ACTIVE_TASK_STATUSES))).all()
    conflicts = []
    for tid, task_type, payload in active:
        targets = _targets(task_type, payload)
        if task_type == "script.parse":
            targets.update("name:" + name for name in filename_aliases(payload.get("source_name") or ""))
        if not targets or wanted & targets:
            conflicts.append(tid)
    if conflicts:
        raise TaskSubmissionError(409, "所选目标已有在途任务，请等待其结束")


def _batch_base_time(db, owner_id):
    """Start after the owner's newest task so batches never interleave.

    Rows inside one batch get ``base + index`` microseconds. Starting from the
    wall clock alone would let two batches submitted in the same instant (or
    within the batch's own span) sort by random ids. The owner's newest
    ``created_at`` is the previous batch's last row; project locks serialize
    submissions, so the next batch always begins after it.
    """
    now = utcnow()
    latest = db.scalar(select(func.max(Task.created_at)).where(Task.owner_id == owner_id))
    if latest is None:
        return now
    if latest.tzinfo is None:  # SQLite returns naive datetimes
        latest = latest.replace(tzinfo=timezone.utc)
    return max(now, latest + timedelta(microseconds=1))


def submit_task_batch(*, db, user, project_id, task_type, request, prepare,
                      idempotency_key=None, receipt_field=None):
    """Run preparation only for new intent, under the same admission transaction.

    ``prepare`` returns (entries, configuration); each entry has label/payload
    and may carry receipt fields. It must not commit. Every exception rolls
    back file catalog additions as well as all tasks/events/the receipt.
    """
    if task_type not in BATCH_TYPES:
        raise TaskSubmissionError(422, "不支持的批量任务类型")
    key = idempotency_key or str(uuid.uuid4())
    if not isinstance(key, str) or not 1 <= len(key) <= 180 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise TaskSubmissionError(422, "无效的幂等键")
    intent = _hash({"project": project_id, "type": task_type, "request": request})
    # SQLite has no row locks; serialize test/local admissions without holding
    # a host-wide lock during PostgreSQL preparation for unrelated projects.
    guard = host_lock() if db.get_bind().dialect.name == "sqlite" else nullcontext()
    with guard:
        try:
            if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
                raise TaskSubmissionError(409, "存储根目录正在迁移，暂时无法提交任务")
            project = db.scalar(select(Project).where(Project.id == project_id,
                Project.owner_id == user.id, Project.deleted_at.is_(None)).with_for_update())
            if project is None:
                raise TaskSubmissionError(404, "项目不存在")
            replay = _replay(db, user.id, key, intent)
            if replay is not None:
                db.rollback()
                return replay
            entries, config = prepare()
            minimum = 0 if task_type.startswith("voices.") else 1
            if not minimum <= len(entries) <= MAX_BATCH_TASKS:
                raise TaskSubmissionError(422, "一次请选择 1 至 1000 个任务目标")
            db.flush()  # catalogued legacy inputs need identities before validation
            entries = [{**entry, "payload": {k: v for k, v in entry["payload"].items() if k != "config"}}
                       for entry in entries]
            _validate_inputs_and_conflicts(db, user, project, task_type, entries)
            if task_type in BILLABLE_TASK_TYPES and entries:
                account = db.scalar(select(UserQuotaAccount).where(UserQuotaAccount.user_id == user.id).with_for_update())
                if account is None or account.available_units <= 0:
                    raise TaskSubmissionError(409, "额度不足")
            config = dict(config)
            config.pop("_batch_intent", None)
            config.pop("_batch_receipt", None)
            digest = _hash({"intent": intent, "entries": entries, "config": config})
            batch_id = str(uuid.uuid4())
            ids = [str(uuid.uuid4()) for _ in entries]
            response = {receipt_field: [{**entry.get("receipt", {}), "task_id": tid}
                        for entry, tid in zip(entries, ids)]} if receipt_field else {}
            batch = TaskBatch(id=batch_id, owner_id=user.id, project_id=project.id,
                task_type=task_type, idempotency_key=key, request_hash=digest,
                config={**config, "_batch_intent": intent, "_batch_receipt": response}, task_ids=ids)
            db.add(batch)
            db.flush()
            now, rows, events = _batch_base_time(db, user.id), [], []
            for index, (tid, entry) in enumerate(zip(ids, entries)):
                # Claims and lists order by (created_at, id); ids are random UUIDs,
                # so rows need distinct, entry-ordered timestamps (see _batch_base_time).
                created = now + timedelta(microseconds=index)
                payload = {k: v for k, v in entry["payload"].items() if k != "config"}
                payload.update(label=entry["label"], _batch_config_id=batch_id, _request_hash=digest)
                payload.pop("_audio_identity", None)
                identity = workspace_audio_identity(task_type, payload)
                if identity:
                    payload["_audio_identity"] = identity
                if task_type == "voices.clone":
                    payload["execution_batch"] = batch_id
                rows.append(dict(id=tid, owner_id=user.id, project_id=project.id, task_type=task_type,
                    payload=payload, batch_id=batch_id, event_sequence=1, ui_state={}, admission_units=1,
                    status="pending", progress=0, idempotency_key=f"batch:{batch_id}:{tid}", created_at=created, updated_at=now))
                events.append(dict(id=str(uuid.uuid4()), task_id=tid, sequence=1, event_type="submitted",
                    payload={"status": "pending", "estimated_units": 0}, created_at=created))
            for offset in range(0, len(rows), 100):
                db.execute(insert(Task), rows[offset:offset + 100])
                db.execute(insert(TaskEvent), events[offset:offset + 100])
            if ids:
                db.add(OutboxEvent(aggregate_type="task", aggregate_id=ids[0], event_type="task.submitted",
                    payload={"task_id": ids[0], "task_type": task_type, "project_id": project.id, "batch_id": batch_id}))
            response = batch_receipt(batch)
            db.commit()
            return response
        except IntegrityError:
            db.rollback()
            # Cross-project submissions can contend for an owner-scoped key.
            replay = _replay(db, user.id, key, intent)
            if replay is None:
                raise
            db.rollback()
            return replay
        except BaseException:
            db.rollback()
            raise
