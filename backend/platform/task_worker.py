from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import redis
from sqlalchemy import func, select

from ..core.config import TextConfig
from ..core.paths import WORKSPACE_DIRS
from ..engines.book import analyze_text, decode_buffer
from ..engines.text import format_text
from .config import settings
from .database import SessionLocal
from .models import (
    OutboxEvent,
    Project,
    ProjectFile,
    Task,
    TaskAttempt,
    TaskResult,
    User,
    utcnow,
    new_id,
)
from .outbox import STREAM_NAME
from .storage import (
    configured_storage_root,
    object_path,
    project_object_key,
    safe_display_name,
    sha256_file,
    task_attempt_path,
)
from .task_state import (
    TERMINAL_TASK_STATUSES,
    append_task_event,
    release_reservation,
    settle_reservation,
)


WORKER_GROUP = os.getenv("NARRIFY_TASK_GROUP", "narrify-workers")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class TaskExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class TaskCancelledError(TaskExecutionError):
    def __init__(self) -> None:
        super().__init__("cancelled", "任务已取消", retryable=False)


@dataclass(frozen=True)
class TaskClaim:
    task_id: str
    attempt_id: str
    attempt_no: int
    lease_token: str
    worker_id: str
    owner_id: str
    project_id: str
    task_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class TaskOutcome:
    temp_path: Path
    output_name: str
    content_type: str
    size_bytes: int
    sha256: str
    metadata: dict[str, Any]
    publish_module: str | None = None


def ensure_consumer_group(client: redis.Redis) -> None:
    try:
        client.xgroup_create(STREAM_NAME, WORKER_GROUP, id="0-0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _attempt_is_current(db, claim: TaskClaim) -> tuple[Task | None, TaskAttempt | None]:
    task = db.scalar(select(Task).where(Task.id == claim.task_id).with_for_update())
    attempt = db.scalar(
        select(TaskAttempt)
        .where(TaskAttempt.id == claim.attempt_id, TaskAttempt.task_id == claim.task_id)
        .with_for_update()
    )
    if task is None or attempt is None:
        return None, None
    if attempt.lease_token != claim.lease_token or attempt.status != "running":
        return task, None
    if (_as_utc(attempt.lease_expires_at) is None) or (_as_utc(attempt.lease_expires_at) <= utcnow()):
        return task, None
    if task.status in TERMINAL_TASK_STATUSES:
        return task, None
    return task, attempt


def claim_task(task_id: str, worker_id: str, *, lease_seconds: int | None = None) -> TaskClaim | None:
    """Atomically create one fenced attempt for a submitted task."""
    lease_seconds = lease_seconds or settings.task_lease_seconds
    now = utcnow()
    with SessionLocal() as db:
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            return None

        active = db.scalar(
            select(TaskAttempt)
            .where(TaskAttempt.task_id == task.id, TaskAttempt.status == "running")
            .order_by(TaskAttempt.attempt_no.desc())
            .with_for_update()
        )
        if active is not None:
            if _as_utc(active.lease_expires_at) is not None and _as_utc(active.lease_expires_at) > now:
                db.rollback()
                return None
            active.status = "expired"
            active.finished_at = now
            active.error_message = "worker lease expired"
            append_task_event(db, task.id, "attempt_expired", {"attempt_id": active.id})

        if task.status == "cancelling":
            task.status = "cancelled"
            task.finished_at = now
            release_reservation(db, task, note="cancelled before worker claim")
            append_task_event(db, task.id, "cancelled", {"reason": "cancel requested"})
            db.commit()
            return None

        latest_attempt = db.scalar(
            select(func.max(TaskAttempt.attempt_no)).where(TaskAttempt.task_id == task.id)
        ) or 0
        if latest_attempt >= settings.task_max_attempts:
            task.status = "failed"
            task.error_code = "max_attempts"
            task.error_message = "任务超过最大尝试次数"
            task.finished_at = now
            release_reservation(db, task, kind="release", note="maximum attempts exceeded")
            append_task_event(db, task.id, "failed", {"code": task.error_code})
            db.commit()
            return None

        attempt = TaskAttempt(
            task_id=task.id,
            attempt_no=int(latest_attempt) + 1,
            worker_id=worker_id,
            lease_token=secrets.token_urlsafe(32),
            status="running",
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        db.add(attempt)
        db.flush()
        task.status = "running"
        task.started_at = task.started_at or now
        task.updated_at = now
        task.error_code = ""
        task.error_message = ""
        append_task_event(
            db,
            task.id,
            "attempt_started",
            {"attempt_id": attempt.id, "attempt_no": attempt.attempt_no, "worker_id": worker_id},
        )
        db.commit()
        return TaskClaim(
            task_id=task.id,
            attempt_id=attempt.id,
            attempt_no=attempt.attempt_no,
            lease_token=attempt.lease_token,
            worker_id=worker_id,
            owner_id=task.owner_id,
            project_id=task.project_id,
            task_type=task.task_type,
            payload=dict(task.payload),
        )


def heartbeat_claim(claim: TaskClaim, *, lease_seconds: int | None = None) -> bool:
    lease_seconds = lease_seconds or settings.task_lease_seconds
    with SessionLocal() as db:
        attempt = db.scalar(
            select(TaskAttempt)
            .where(TaskAttempt.id == claim.attempt_id, TaskAttempt.task_id == claim.task_id)
            .with_for_update()
        )
        if attempt is None or attempt.status != "running" or attempt.lease_token != claim.lease_token:
            db.rollback()
            return False
        attempt.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
        db.commit()
        return True


def update_progress(claim: TaskClaim, progress: int, current: str) -> bool:
    with SessionLocal() as db:
        task, attempt = _attempt_is_current(db, claim)
        if task is None or attempt is None:
            db.rollback()
            return False
        task.progress = max(0, min(100, int(progress)))
        task.updated_at = utcnow()
        append_task_event(db, task.id, "progress", {"progress": task.progress, "current": current})
        db.commit()
        return True


def cancellation_requested(claim: TaskClaim) -> bool:
    with SessionLocal() as db:
        task = db.get(Task, claim.task_id)
        return task is None or task.status == "cancelling"


def _input_file(db, claim: TaskClaim) -> tuple[User, Project, ProjectFile, Path]:
    input_file_id = str(claim.payload.get("input_file_id", ""))
    if not input_file_id:
        raise TaskExecutionError("invalid_payload", "任务缺少 input_file_id")
    user = db.get(User, claim.owner_id)
    project = db.scalar(select(Project).where(Project.id == claim.project_id, Project.owner_id == claim.owner_id))
    item = db.scalar(
        select(ProjectFile).where(
            ProjectFile.id == input_file_id,
            ProjectFile.project_id == claim.project_id,
            ProjectFile.owner_id == claim.owner_id,
            ProjectFile.deleted_at.is_(None),
        )
    )
    if user is None or project is None or item is None:
        raise TaskExecutionError("input_not_found", "任务输入文件不存在")
    path = object_path(item.object_key, configured_storage_root(db))
    if not path.is_file():
        raise TaskExecutionError("input_missing", "任务输入文件内容已丢失")
    return user, project, item, path


def _write_outcome(
    claim: TaskClaim,
    output_name: str,
    content_type: str,
    data: bytes,
    metadata: dict[str, Any],
    *,
    publish_module: str | None = None,
) -> TaskOutcome:
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        if user is None:
            raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
        temp_path = task_attempt_path(db, user.username, claim.project_id, claim.task_id, claim.attempt_id, output_name)
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.write_bytes(data)
    return TaskOutcome(
        temp_path=temp_path,
        output_name=safe_display_name(output_name),
        content_type=content_type,
        size_bytes=len(data),
        sha256=sha256_file(temp_path),
        metadata=metadata,
        publish_module=publish_module,
    )


def execute_claim(claim: TaskClaim) -> TaskOutcome:
    """Execute one real deterministic engine behind the durable worker boundary."""
    if claim.task_type not in {"text.format", "book.analyze"}:
        raise TaskExecutionError("unsupported_task_type", f"不支持的任务类型：{claim.task_type}")
    with SessionLocal() as db:
        _, _, item, source_path = _input_file(db, claim)
        if cancellation_requested(claim):
            raise TaskCancelledError()
        update_progress(claim, 10, "读取输入")
        source_text, _ = decode_buffer(source_path.read_bytes())

    if claim.task_type == "text.format":
        config = TextConfig.model_validate(claim.payload.get("config") or {})
        result = format_text(source_text, config)
        update_progress(claim, 75, "完成排版")
        output_name = str(claim.payload.get("output_name") or f"{Path(item.original_name).stem}_formatted.txt")
        return _write_outcome(
            claim,
            output_name,
            "text/plain; charset=utf-8",
            result["text"].encode("utf-8"),
            {
                "engine": "text.format",
                "stats": result["stats"],
                "source_file_id": item.id,
                "preview": result["text"][:2000],
                "full_length": len(result["text"]),
            },
            publish_module=str(claim.payload.get("publish_module") or "") or None,
        )

    analysis = analyze_text(source_text)
    update_progress(claim, 75, "完成章节分析")
    output_name = str(claim.payload.get("output_name") or f"{Path(item.original_name).stem}_analysis.json")
    return _write_outcome(
        claim,
        output_name,
        "application/json",
        json.dumps(analysis, ensure_ascii=False, indent=2).encode("utf-8"),
        {"engine": "book.analyze", "source_file_id": item.id, "analysis": analysis},
        publish_module=str(claim.payload.get("publish_module") or "") or None,
    )


def complete_claim(claim: TaskClaim, outcome: TaskOutcome) -> bool:
    with SessionLocal() as db:
        task, attempt = _attempt_is_current(db, claim)
        if task is None or attempt is None or task.status == "cancelling":
            db.rollback()
            outcome.temp_path.unlink(missing_ok=True)
            return False
        user = db.get(User, task.owner_id)
        if user is None:
            db.rollback()
            outcome.temp_path.unlink(missing_ok=True)
            return False
        output_id = new_id()
        if outcome.publish_module:
            module_names = {name for _, name in WORKSPACE_DIRS if name != "00_temp"}
            if outcome.publish_module not in module_names:
                db.rollback()
                outcome.temp_path.unlink(missing_ok=True)
                return False
            object_key = f"{safe_display_name(user.username)}/{task.project_id}/{outcome.publish_module}/{output_id}/{safe_display_name(outcome.output_name)}"
        else:
            object_key = project_object_key(user.username, task.project_id, output_id, outcome.output_name)
        final_path = object_path(object_key, configured_storage_root(db))
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(outcome.temp_path, final_path)
        db.add(
            ProjectFile(
                id=output_id,
                project_id=task.project_id,
                owner_id=task.owner_id,
                original_name=outcome.output_name,
                object_key=object_key,
                content_type=outcome.content_type,
                size_bytes=outcome.size_bytes,
                sha256=outcome.sha256,
                kind="artifact",
            )
        )
        result_payload = {"file_id": output_id, "object_key": object_key, "name": outcome.output_name, **outcome.metadata}
        if outcome.publish_module:
            result_payload["path"] = str(final_path)
        db.add(TaskResult(task_id=task.id, result=result_payload))
        attempt.status = "succeeded"
        attempt.finished_at = utcnow()
        attempt.lease_expires_at = None
        task.status = "succeeded"
        task.progress = 100
        task.finished_at = utcnow()
        task.updated_at = utcnow()
        settle_reservation(db, task, note=f"{claim.task_type} completed")
        append_task_event(db, task.id, "succeeded", {"attempt_id": attempt.id, "file_id": output_id})
        db.commit()
        return True


def fail_claim(claim: TaskClaim, error: TaskExecutionError) -> str:
    with SessionLocal() as db:
        task, attempt = _attempt_is_current(db, claim)
        if task is None or attempt is None:
            db.rollback()
            return "stale"
        attempt.status = "failed" if error.code != "cancelled" else "cancelled"
        attempt.finished_at = utcnow()
        attempt.lease_expires_at = None
        attempt.error_message = str(error)
        task.error_code = error.code
        task.error_message = str(error)
        task.updated_at = utcnow()
        if error.code == "cancelled" or task.status == "cancelling":
            task.status = "cancelled"
            task.finished_at = utcnow()
            release_reservation(db, task, note="task cancelled")
            append_task_event(db, task.id, "cancelled", {"attempt_id": attempt.id})
            db.commit()
            return "cancelled"
        if error.retryable and attempt.attempt_no < settings.task_max_attempts:
            task.status = "retrying"
            delay = min(300, 2 ** max(0, attempt.attempt_no - 1))
            db.add(
                OutboxEvent(
                    aggregate_type="task",
                    aggregate_id=task.id,
                    event_type="task.retry",
                    payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id},
                    available_at=utcnow() + timedelta(seconds=delay),
                )
            )
            append_task_event(db, task.id, "retry_scheduled", {"attempt_id": attempt.id, "delay_seconds": delay})
            db.commit()
            return "retrying"
        task.status = "failed"
        task.finished_at = utcnow()
        release_reservation(db, task, kind="release", note=f"task failed: {error.code}")
        append_task_event(db, task.id, "failed", {"attempt_id": attempt.id, "code": error.code})
        db.commit()
        return "failed"


def process_task_message(message: dict[str, Any], *, worker_id: str) -> str:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else message
    task_id = str(payload.get("task_id", ""))
    if not task_id:
        return "invalid"
    claim = claim_task(task_id, worker_id)
    if claim is None:
        return "skipped"
    stop = threading.Event()

    def renew() -> None:
        interval = max(1.0, settings.task_lease_seconds / 3)
        while not stop.wait(interval):
            if not heartbeat_claim(claim):
                return

    heartbeat = threading.Thread(target=renew, name=f"lease-{claim.task_id[:8]}", daemon=True)
    heartbeat.start()
    try:
        outcome = execute_claim(claim)
        if cancellation_requested(claim):
            raise TaskCancelledError()
        if complete_claim(claim, outcome):
            return "succeeded"
        fail_claim(claim, TaskCancelledError())
        return "cancelled"
    except TaskExecutionError as exc:
        fail_claim(claim, exc)
        return exc.code
    except Exception as exc:  # unexpected errors are retryable up to the attempt cap
        fail_claim(claim, TaskExecutionError("worker_error", str(exc), retryable=True))
        return "worker_error"
    finally:
        stop.set()
        heartbeat.join(timeout=2)


def _decode_stream_event(fields: dict[str, Any]) -> dict[str, Any]:
    raw = fields.get("event", fields.get(b"event"))
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw if isinstance(raw, dict) else {}


def process_stream_entry(client: redis.Redis, entry_id: str, fields: dict[str, Any], *, worker_id: str) -> str:
    result = process_task_message(_decode_stream_event(fields), worker_id=worker_id)
    client.xack(STREAM_NAME, WORKER_GROUP, entry_id)
    return result


def recover_database_tasks(limit: int = 100) -> int:
    """Recreate dispatch events after Redis loss or a dead worker."""
    now = utcnow()
    recovered = 0
    with SessionLocal() as db:
        tasks = db.scalars(
            select(Task)
            .where(Task.status.in_(["pending", "queued", "retrying", "running"]))
            .order_by(Task.updated_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for task in tasks:
            attempt = db.scalar(
                select(TaskAttempt)
                .where(TaskAttempt.task_id == task.id, TaskAttempt.status == "running")
                .order_by(TaskAttempt.attempt_no.desc())
                .with_for_update()
            )
            if task.status == "running" and attempt is not None and _as_utc(attempt.lease_expires_at) and _as_utc(attempt.lease_expires_at) > now:
                continue
            if attempt is not None and attempt.status == "running":
                attempt.status = "expired"
                attempt.finished_at = now
                attempt.error_message = "worker lease expired during recovery"
                append_task_event(db, task.id, "attempt_expired", {"attempt_id": attempt.id, "recovered": True})
                if attempt.attempt_no >= settings.task_max_attempts:
                    task.status = "failed"
                    task.error_code = "lease_expired"
                    task.error_message = "Worker 租约过期且已达到最大尝试次数"
                    task.finished_at = now
                    release_reservation(db, task, kind="release", note="lease expired")
                    append_task_event(db, task.id, "failed", {"code": task.error_code})
                    continue
                task.status = "retrying"

            pending_event = db.scalar(
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.aggregate_type == "task",
                    OutboxEvent.aggregate_id == task.id,
                    OutboxEvent.published_at.is_(None),
                )
                .limit(1)
            )
            if pending_event is not None:
                continue
            attempt_number = (attempt.attempt_no + 1) if attempt is not None else 0
            event_id = f"recover:{task.id}:{attempt_number}"
            if db.get(OutboxEvent, event_id) is None:
                db.add(
                    OutboxEvent(
                        id=event_id,
                        aggregate_type="task",
                        aggregate_id=task.id,
                        event_type="task.recovered",
                        payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id},
                    )
                )
                append_task_event(db, task.id, "dispatch_recovered", {"attempt_no": attempt_number})
                recovered += 1
        db.commit()
    return recovered


def run_once(client: redis.Redis, *, worker_id: str, block_ms: int = 1000) -> str:
    ensure_consumer_group(client)
    try:
        result = client.xautoclaim(
            STREAM_NAME,
            WORKER_GROUP,
            worker_id,
            min_idle_time=max(1000, settings.task_lease_seconds * 1000),
            start_id="0-0",
            count=1,
        )
    except (redis.ResponseError, AttributeError):
        result = None
    if result:
        entries = result[1] if len(result) > 1 else []
        if entries:
            entry_id, fields = entries[0]
            return process_stream_entry(client, entry_id, fields, worker_id=worker_id)
    rows = client.xreadgroup(WORKER_GROUP, worker_id, {STREAM_NAME: ">"}, count=1, block=block_ms)
    if not rows:
        return "idle"
    _, entries = rows[0]
    entry_id, fields = entries[0]
    return process_stream_entry(client, entry_id, fields, worker_id=worker_id)
