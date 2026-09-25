from __future__ import annotations

import json
import io
import logging
import mimetypes
import os
import secrets
import shutil
import threading
import time
import zipfile
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import redis
from sqlalchemy import func, or_, select

from ..core.config import TextConfig
from ..core import config as core_config
from ..core.file_lock import exclusive_file_lock
from ..core.paths import WORKSPACE_DIRS, get_layout
from ..core.request_context import bind_workspace, reset_workspace
from ..core.tasks import TaskCancelled
from ..engines.book import (
    EXPECTED_CHAPTER_FORMAT,
    analyze_text,
    base_name,
    check_chapter_sequence,
    decode_buffer,
    make_chapter_filenames,
    make_whole_book_filename,
    make_smart_filenames,
    is_generated_split_output_name,
    chapter_content,
    smart_repair,
)
from ..engines.text import format_text
from ..engines import script as script_engine
from ..engines import audio as audio_engine
from .config import settings
from .artifact_publication import PublicationJournal, PublicationJournalBundle, publication_transaction
from .database import SessionLocal
from .models import (
    OutboxEvent,
    Project,
    ProjectFile,
    Task,
    TaskAttempt,
    TaskResult,
    User,
    UserQuotaAccount,
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
    user_workspace_root,
    lock_storage_migration,
    storage_migration,
)
from .task_types import LEGACY_ENGINE_TASK_TYPES, SUPPORTED_TASK_TYPES
from .task_state import (
    TERMINAL_TASK_STATUSES,
    append_task_event,
    release_reservation,
    settle_reservation,
    suppress_pending_dispatch,
)


WORKER_GROUP = os.getenv("NARRIFY_TASK_GROUP", "narrify-workers")
WORKSPACE_MUTATING_TASK_TYPES = {
    "voices.foundation", "voices.clone", "tts.batch", "tts.merge", "tts.reset",
    "bgm.analysis", "bgm.segment", "bgm.mix", "bgm.match", "audio.export",
}


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
class TaskFileOutcome:
    temp_path: Path
    output_name: str
    content_type: str
    size_bytes: int
    sha256: str
    publish_module: str | None = None


@dataclass(frozen=True)
class TaskSideEffectOutput:
    temp_path: Path
    final_path: Path


@dataclass(frozen=True)
class TaskOutcome:
    temp_path: Path
    output_name: str
    content_type: str
    size_bytes: int
    sha256: str
    metadata: dict[str, Any]
    publish_module: str | None = None
    additional_outputs: tuple[TaskFileOutcome, ...] = field(default_factory=tuple)
    side_effect_outputs: tuple[TaskSideEffectOutput, ...] = field(default_factory=tuple)
    side_effect_deletes: tuple[Path, ...] = field(default_factory=tuple)
    publication_journal: PublicationJournal | PublicationJournalBundle | None = None


class PersistentTaskHandle:
    """Adapter from the legacy engine callback contract to durable task events."""

    def __init__(self, claim: TaskClaim):
        self.claim = claim
        self._rate_lock = threading.Lock()
        self._rate_total = 0
        self._rate_samples: deque[tuple[float, int]] = deque()
        self._last_rate_event = 0.0
        self._publication_journal: PublicationJournal | None = None
        self._shared_publication_journal: PublicationJournal | None = None
        self._staged_workspace_paths: set[Path] = set()
        self._staged_workspace_directories: set[Path] = set()

    @property
    def cancelled(self) -> bool:
        return cancellation_requested(self.claim)

    def progress(self, fraction: float, current: str = "") -> None:
        self.progress_percent(fraction * 100, current)

    def progress_percent(self, percent: float, current: str = "") -> None:
        update_progress(self.claim, round(max(0.0, min(100.0, percent))), current)

    def phase(self, name: str) -> None:
        _append_claim_event(self.claim, "phase", {"phase": name})

    def log(self, message: str, level: str = "INFO") -> None:
        _append_claim_event(self.claim, "log", {"level": level, "msg": message})

    def llm_chunk(self, _text: str) -> None:
        # Raw model output is intentionally not persisted in TaskEvent history.
        # The durable result is the published JSON artifact.
        return

    def llm_rate(self, chars: int, cps: float) -> None:
        now = time.monotonic()
        with self._rate_lock:
            self._rate_total += max(0, int(chars))
            self._rate_samples.append((now, self._rate_total))
            while self._rate_samples and self._rate_samples[0][0] < now - 10:
                self._rate_samples.popleft()
            first_time, first_total = self._rate_samples[0]
            span = now - first_time
            cps10 = max(0.0, (self._rate_total - first_total) / span) if span > 0.001 else 0.0
            if cps > 0 and now - self._last_rate_event < 1.0:
                return
            self._last_rate_event = now
        _append_claim_event(self.claim, "llm_rate", {"cps": max(0.0, float(cps)), "cps10": cps10})

    def llm_chars(self, chars: int, secs: float) -> None:
        _append_claim_event(self.claim, "llm_chars", {"chars": max(0, int(chars)), "secs": max(0.0, float(secs))})

    def segment_stats(self, done: int, total: int, chars_done: int, chars_total: int) -> None:
        _append_claim_event(self.claim, "segments", {
            "done": max(0, int(done)), "total": max(0, int(total)),
            "chars_done": max(0, int(chars_done)), "chars_total": max(0, int(chars_total)),
        })

    @property
    def publication_journal(self) -> PublicationJournal | PublicationJournalBundle | None:
        journals = [journal for journal in (
            self._publication_journal, self._shared_publication_journal,
        ) if journal is not None]
        if not journals:
            return None
        return journals[0] if len(journals) == 1 else PublicationJournalBundle(journals)

    def _ensure_publication_journal(self) -> PublicationJournal:
        if self._publication_journal is None:
            with SessionLocal() as db:
                user = db.get(User, self.claim.owner_id)
                if user is None:
                    raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
                journal = PublicationJournal(
                    configured_storage_root(db),
                    task_attempt_path(
                        db, user.username, self.claim.project_id,
                        self.claim.task_id, self.claim.attempt_id, "publication.json",
                    ),
                )
            journal.prepare()
            self._publication_journal = journal
        return self._publication_journal

    def _ensure_shared_publication_journal(self) -> PublicationJournal:
        if self._shared_publication_journal is None:
            from ..core import paths as core_paths

            library_root = Path(core_paths.MUSIC_LIBRARY_DIR).resolve()
            journal = PublicationJournal(
                library_root,
                library_root / ".tasks" / self.claim.task_id / self.claim.attempt_id / "publication.json",
            )
            journal.prepare()
            self._shared_publication_journal = journal
        return self._shared_publication_journal

    def _workspace_stage_path(self, final_path: Path) -> Path:
        with SessionLocal() as db:
            user = db.get(User, self.claim.owner_id)
            if user is None:
                raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
            return task_attempt_path(
                db, user.username, self.claim.project_id, self.claim.task_id,
                self.claim.attempt_id, f"workspace-{secrets.token_hex(12)}-{safe_display_name(final_path.name)}",
            )

    def allocate_workspace_stage(self, final_path: Path) -> Path:
        staged = self._workspace_stage_path(final_path)
        staged.parent.mkdir(parents=True, exist_ok=True)
        self._staged_workspace_paths.add(staged)
        return staged

    def allocate_workspace_directory(self, final_directory: Path) -> Path:
        staged = self._workspace_stage_path(final_directory / "stage")
        staged.mkdir(parents=True, exist_ok=False)
        self._staged_workspace_directories.add(staged)
        return staged

    def discard_workspace_directory(self, staged: Path) -> None:
        resolved = staged.resolve()
        if resolved not in self._staged_workspace_directories:
            raise ValueError("Workspace staging directory was not allocated by this task")
        shutil.rmtree(resolved, ignore_errors=True)
        self._staged_workspace_directories.discard(resolved)

    def _publish_staged_workspace_file(self, final_path: Path, staged: Path) -> None:
        with SessionLocal() as db:
            user = db.get(User, self.claim.owner_id)
            if user is None:
                raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
            attempt_root = task_attempt_path(
                db, user.username, self.claim.project_id, self.claim.task_id,
                self.claim.attempt_id, "publication-check",
            ).parent.resolve()
        if not staged.resolve().is_relative_to(attempt_root):
            raise ValueError("Staged workspace output is outside the active task attempt")
        journal = self._ensure_publication_journal()
        try:
            journal.publish(journal.add(final_path), staged)
        except BaseException:
            staged.unlink(missing_ok=True)
            self._staged_workspace_paths.discard(staged)
            raise
        self._staged_workspace_paths.discard(staged)

    def publish_workspace_stage(self, final_path: Path, staged: Path) -> None:
        self._publish_staged_workspace_file(final_path, staged)

    def discard_workspace_stage(self, staged: Path) -> None:
        staged.unlink(missing_ok=True)
        self._staged_workspace_paths.discard(staged)

    def stage_workspace_file(self, final_path: Path, data: bytes) -> None:
        """Journal and publish one legacy workspace file while preserving live updates."""
        staged = self._workspace_stage_path(final_path)
        try:
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
            self._publish_staged_workspace_file(final_path, staged)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def stage_workspace_copy(self, final_path: Path, source_path: Path) -> None:
        staged = self._workspace_stage_path(final_path)
        try:
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, staged)
            self._publish_staged_workspace_file(final_path, staged)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def stage_shared_file(self, final_path: Path, data: bytes) -> None:
        """Publish a shared music-library JSON update under the attempt's recovery journal."""
        self.check()
        journal = self._ensure_shared_publication_journal()
        staged = journal.path.parent / f"staged-{secrets.token_hex(12)}-{safe_display_name(final_path.name)}"
        try:
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
            journal.publish(journal.add(final_path), staged)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def defer_workspace_delete(self, final_path: Path) -> None:
        self._ensure_publication_journal().remove(final_path)

    def rollback_publications(self) -> None:
        for staged in self._staged_workspace_paths:
            staged.unlink(missing_ok=True)
        self._staged_workspace_paths.clear()
        for staged in self._staged_workspace_directories:
            shutil.rmtree(staged, ignore_errors=True)
        self._staged_workspace_directories.clear()
        if self._publication_journal is not None:
            self._publication_journal.rollback()
        if self._shared_publication_journal is not None:
            self._shared_publication_journal.rollback()

    def check(self) -> None:
        if self.cancelled:
            raise TaskCancelled()


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


@contextmanager
def _workspace_engine_lock(claim: TaskClaim):
    """Serialize legacy engine mutations and publication within one project."""
    if claim.task_type not in WORKSPACE_MUTATING_TASK_TYPES:
        yield True
        return
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        if user is None:
            raise TaskExecutionError("owner_not_found", "Task owner is missing")
        workspace = user_workspace_root(db, user.username, claim.project_id)
    lock_path = workspace / ".tasks" / "workspace-engine.lock"
    try:
        with exclusive_file_lock(lock_path):
            with SessionLocal() as db:
                task, attempt = _attempt_is_current(db, claim)
                active = task is not None and attempt is not None and task.status != "cancelling"
                db.rollback()
            yield active
    except TimeoutError as exc:
        raise TaskExecutionError("workspace_busy", str(exc), retryable=True) from exc


def _reconcile_attempt_publication(db, task: Task, attempt: TaskAttempt) -> None:
    user = db.get(User, task.owner_id)
    if user is None:
        raise RuntimeError(f"Task owner is missing: {task.owner_id}")
    root = configured_storage_root(db)
    path = task_attempt_path(db, user.username, task.project_id, task.id, attempt.id, "publication.json")
    PublicationJournal.reconcile(root, path, committed=task.status == "succeeded")
    from ..core import paths as core_paths

    shared_root = Path(core_paths.MUSIC_LIBRARY_DIR).resolve()
    shared_path = shared_root / ".tasks" / task.id / attempt.id / "publication.json"
    PublicationJournal.reconcile(shared_root, shared_path, committed=task.status == "succeeded")


def claim_task(task_id: str, worker_id: str, *, lease_seconds: int | None = None) -> TaskClaim | None:
    """Atomically create one fenced attempt for a submitted task."""
    lease_seconds = lease_seconds or settings.task_lease_seconds
    now = utcnow()
    with SessionLocal() as db:
        if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
            db.rollback()
            return None
        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None or task.status in TERMINAL_TASK_STATUSES:
            return None
        if task.status == "retrying" and _as_utc(task.next_attempt_at) and _as_utc(task.next_attempt_at) > now:
            db.rollback()
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
            _reconcile_attempt_publication(db, task, active)
            active.status = "expired"
            active.finished_at = now
            active.error_message = "worker lease expired"
            append_task_event(db, task.id, "attempt_expired", {"attempt_id": active.id})
            from .quota import release_attempt_holds
            release_attempt_holds(task.id, active.id, db=db)

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
        task.next_attempt_at = None
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


def claim_fair_task(
    worker_id: str,
    *,
    lease_seconds: int | None = None,
    task_types: tuple[str, ...] | None = None,
) -> TaskClaim | None:
    """Claim the next task using a persistent per-user round-robin cursor.

    The account row is locked before selecting a task, so multiple Worker
    processes cannot repeatedly choose the same user.  The oldest task from
    the least recently scheduled user is then claimed through the same fenced
    claim path used by Redis-delivered messages.
    """
    lease_seconds = lease_seconds or settings.task_lease_seconds
    now = utcnow()
    with SessionLocal() as db:
        retry_ready = or_(Task.next_attempt_at.is_(None), Task.next_attempt_at <= now)
        eligible_tasks = Task.status.in_(["pending", "queued"]) | ((Task.status == "retrying") & retry_ready)
        if task_types:
            eligible_tasks = eligible_tasks & Task.task_type.in_(task_types)
        account = db.scalar(
            select(UserQuotaAccount)
            .join(Task, Task.owner_id == UserQuotaAccount.user_id)
            .where(eligible_tasks)
            .order_by(
                UserQuotaAccount.last_scheduled_at.asc(),
                Task.created_at.asc(),
                Task.id.asc(),
            )
            .limit(1)
            .with_for_update(skip_locked=True, of=UserQuotaAccount)
        )
        if account is None:
            db.rollback()
            return None
        user_tasks = (Task.owner_id == account.user_id) & eligible_tasks
        if task_types:
            user_tasks = user_tasks & Task.task_type.in_(task_types)
        task_id = db.scalar(
            select(Task.id)
            .where(user_tasks)
            .order_by(Task.created_at.asc(), Task.id.asc())
            .limit(1)
        )
        if task_id is None:
            db.rollback()
            return None

        # Advance the cursor before releasing the account lock. The next
        # worker therefore selects another user even though claim_task uses a
        # separate session for the task-row fence.
        account.last_scheduled_at = now
        db.commit()
    claim = claim_task(str(task_id), worker_id, lease_seconds=lease_seconds)
    return claim


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


def _append_claim_event(claim: TaskClaim, event_type: str, payload: dict[str, Any]) -> bool:
    """Append an event only while this attempt still owns the task lease."""
    with SessionLocal() as db:
        task, attempt = _attempt_is_current(db, claim)
        if task is None or attempt is None:
            db.rollback()
            return False
        append_task_event(db, task.id, event_type, payload)
        db.commit()
        return True


def cancellation_requested(claim: TaskClaim) -> bool:
    with SessionLocal() as db:
        task = db.get(Task, claim.task_id)
        attempt = db.get(TaskAttempt, claim.attempt_id)
        if task is None or task.status == "cancelling":
            return True
        return (
            attempt is None
            or attempt.task_id != claim.task_id
            or attempt.status != "running"
            or attempt.lease_token != claim.lease_token
            or _as_utc(attempt.lease_expires_at) is None
            or _as_utc(attempt.lease_expires_at) <= utcnow()
        )


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
    additional_outputs: list[tuple[str, str, bytes]] | None = None,
) -> TaskOutcome:
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        if user is None:
            raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
        temp_path = task_attempt_path(db, user.username, claim.project_id, claim.task_id, claim.attempt_id, output_name)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(data)
        extra: list[TaskFileOutcome] = []
        for index, (extra_name, extra_type, extra_data) in enumerate(additional_outputs or [], 1):
            extra_path = task_attempt_path(
                db,
                user.username,
                claim.project_id,
                claim.task_id,
                claim.attempt_id,
                f"{index:05d}-{extra_name}",
            )
            extra_path.parent.mkdir(parents=True, exist_ok=True)
            extra_path.write_bytes(extra_data)
            extra.append(
                TaskFileOutcome(
                    temp_path=extra_path,
                    output_name=safe_display_name(extra_name),
                    content_type=extra_type,
                    size_bytes=len(extra_data),
                    sha256=sha256_file(extra_path),
                    publish_module=publish_module,
                )
            )
    return TaskOutcome(
        temp_path=temp_path,
        output_name=safe_display_name(output_name),
        content_type=content_type,
        size_bytes=len(data),
        sha256=sha256_file(temp_path),
        metadata=metadata,
        publish_module=publish_module,
        additional_outputs=tuple(extra),
    )


def _display_chapters(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    displayed = []
    for index, chapter in enumerate(chapters, 1):
        item = dict(chapter)
        item["seq"] = index
        if item.get("final_num") is not None:
            item["num"] = item["final_num"]
            item["numStr"] = str(item["final_num"])
        displayed.append(item)
    return displayed


def _book_analysis_result(text: str, encoding: str, source_name: str) -> dict[str, Any]:
    raw = analyze_text(text)
    raw_chapters = raw["chapters"]
    raw_sequence = check_chapter_sequence(raw_chapters)
    chapters = raw_chapters
    filenames = make_chapter_filenames(base_name(source_name), chapters)
    repair_status = None
    if raw_chapters:
        repair = smart_repair(text, raw_chapters)
        if repair["status"] != "error":
            chapters = _display_chapters(repair["chapters"])
            filenames = make_smart_filenames(repair["chapters"])
            repair_status = repair["status"]
    result = {
        "source": source_name,
        "encoding": encoding,
        "base": base_name(source_name),
        "total_chars": raw["totalChars"],
        "chapters": [
            {
                "seq": chapter["seq"],
                "num": chapter.get("final_num", chapter.get("num")),
                "numStr": (
                    str(chapter["final_num"])
                    if chapter.get("final_num") is not None
                    else chapter["numStr"]
                ),
                "title": chapter["title"],
                "chars": chapter["chars"],
            }
            for chapter in chapters
        ],
        "chapter_count": len(chapters),
        "filenames": filenames,
        "expected_format": EXPECTED_CHAPTER_FORMAT,
        "sequence": check_chapter_sequence(chapters),
        "raw_chapter_count": len(raw_chapters),
        "raw_sequence": raw_sequence,
        "repair_status": repair_status,
        "error": None,
    }
    if not chapters:
        result["error"] = (
            f"未检测到章节（系统识别的格式：{EXPECTED_CHAPTER_FORMAT}）。"
            "可「不处理，按整本继续」（整本输出为单个文件），或重新上传原文。"
        )
    return result


def _execute_script_parse(claim: TaskClaim) -> TaskOutcome:
    from ..core.config import GenerationConfig, LLMConfig, PromptsConfig

    with SessionLocal() as db:
        user, _project, item, source_path = _input_file(db, claim)
        workspace = user_workspace_root(db, user.username, claim.project_id)
        output_name = safe_display_name(f"{Path(item.original_name).stem}.json")
        output_path = task_attempt_path(
            db, user.username, claim.project_id, claim.task_id, claim.attempt_id, output_name
        )

    snapshot = claim.payload.get("config")
    if isinstance(snapshot, dict):
        llm = LLMConfig.model_validate(snapshot.get("llm") or {})
        prompts = PromptsConfig.model_validate(snapshot.get("prompts") or {})
        generation = GenerationConfig.model_validate(snapshot.get("generation") or {})
    else:
        token = bind_workspace(workspace)
        try:
            config = core_config.get_config()
            llm, prompts, generation = config.llm, config.prompts, config.generation
        finally:
            reset_workspace(token)

    history_path = workspace / "config" / "spot_check_history.json"
    handle = PersistentTaskHandle(claim)
    token = bind_workspace(workspace)
    try:
        try:
            result = script_engine.parse_script_file(
                handle,
                source_path,
                llm,
                prompts,
                generation,
                output_path=output_path,
                spot_history_path=history_path,
            )
        except TaskCancelled as exc:
            raise TaskCancelledError() from exc
    finally:
        reset_workspace(token)

    if not output_path.is_file():
        raise TaskExecutionError("missing_output", "脚本解析未生成结果文件")
    data = output_path.read_bytes()
    metadata = dict(result)
    metadata.pop("output_path", None)
    metadata["engine"] = "script.parse"
    metadata["source_file_id"] = item.id
    metadata["output_name"] = output_name
    return TaskOutcome(
        temp_path=output_path,
        output_name=output_name,
        content_type="application/json",
        size_bytes=len(data),
        sha256=sha256_file(output_path),
        metadata=metadata,
        publish_module="03_parsed_json",
    )


def _execute_audio_silences(claim: TaskClaim) -> TaskOutcome:
    with SessionLocal() as db:
        user, _project, item, source_path = _input_file(db, claim)
        workspace = user_workspace_root(db, user.username, claim.project_id)
    handle = PersistentTaskHandle(claim)
    target = str(claim.payload.get("target") or "10:00")
    tolerance = int(claim.payload.get("tolerance") or audio_engine.DEFAULT_TOLERANCE)
    token = bind_workspace(workspace)
    try:
        ffmpeg = core_config.get_config().ffmpeg
    finally:
        reset_workspace(token)
    handle.progress_percent(2, "读取时长")
    duration, err = audio_engine.probe_duration(source_path, ffmpeg.ffprobe_path)
    if err or not (duration > 0):
        raise TaskExecutionError("probe_failed", f"无法读取音频时长：{err}")
    try:
        result = audio_engine.detect_silences(
            source_path,
            duration,
            ffmpeg.ffmpeg_path,
            on_progress=lambda value: handle.progress_percent(5 + value * 85, "检测停顿"),
            should_cancel=lambda: handle.cancelled,
            on_log=handle.log,
        )
    except TaskCancelled as exc:
        raise TaskCancelledError() from exc
    if not result["ok"]:
        raise TaskExecutionError("silence_detection_failed", result["reason"])
    plan = audio_engine.build_aligned_plan(duration, target, result["pauses"], tolerance)
    if not plan["valid"]:
        raise TaskExecutionError("invalid_plan", plan["reason"])
    payload = {
        "duration": duration,
        "pause_count": len(result["pauses"]),
        "pauses": result["pauses"],
        "count": plan["count"],
        "segments": plan["segments"],
        "snapped": plan.get("snapped", 0),
        "fallbacks": plan.get("fallbacks", 0),
        "aligned": plan.get("aligned", False),
        "shifts": plan.get("shifts", []),
    }
    handle.progress_percent(100, "完成")
    output_name = safe_display_name(f"{Path(item.original_name).stem}_silences.json")
    return _write_outcome(
        claim,
        output_name,
        "application/json",
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        {"engine": "audio.silences", "source_file_id": item.id, **payload},
    )


def _execute_audio_cut(claim: TaskClaim) -> TaskOutcome:
    with SessionLocal() as db:
        user, _project, item, source_path = _input_file(db, claim)
        output_dir = task_attempt_path(
            db, user.username, claim.project_id, claim.task_id, claim.attempt_id, "cut"
        )
        workspace = user_workspace_root(db, user.username, claim.project_id)
    handle = PersistentTaskHandle(claim)
    token = bind_workspace(workspace)
    try:
        ffmpeg = core_config.get_config().ffmpeg
    finally:
        reset_workspace(token)
    target = str(claim.payload.get("target") or "10:00")
    smart_align = bool(claim.payload.get("smart_align"))
    tolerance = int(claim.payload.get("tolerance") or audio_engine.DEFAULT_TOLERANCE)
    naming = str(claim.payload.get("naming") or "第 {} 集")
    start_number = str(claim.payload.get("start_number") or "1")
    segments = claim.payload.get("segments")
    try:
        handle.progress_percent(2, "读取时长")
        duration, err = audio_engine.probe_duration(source_path, ffmpeg.ffprobe_path)
        if err or not (duration > 0):
            raise TaskExecutionError("probe_failed", f"无法读取音频时长：{err}")
        if segments is None:
            if smart_align:
                silence = audio_engine.detect_silences(
                    source_path,
                    duration,
                    ffmpeg.ffmpeg_path,
                    on_progress=lambda value: handle.progress_percent(5 + value * 25, "检测停顿"),
                    should_cancel=lambda: handle.cancelled,
                    on_log=handle.log,
                )
                if not silence["ok"]:
                    raise TaskExecutionError("silence_detection_failed", silence["reason"])
                plan = audio_engine.build_aligned_plan(duration, target, silence["pauses"], tolerance)
            else:
                plan = audio_engine.build_plan(duration, target)
            if not plan["valid"]:
                raise TaskExecutionError("invalid_plan", plan["reason"])
            segments = plan["segments"]
        if not isinstance(segments, list) or not segments:
            raise TaskExecutionError("invalid_payload", "未提供有效的切割方案")
        if len(segments) > audio_engine.MAX_SEGMENTS:
            raise TaskExecutionError("invalid_payload", f"段数 {len(segments)} 超过上限 {audio_engine.MAX_SEGMENTS}")
        files = audio_engine.cut_segments(
            source_path,
            segments,
            output_dir,
            naming,
            start_number,
            ffmpeg.ffmpeg_path,
            audio_engine.get_extension(item.original_name),
            on_progress=lambda value: handle.progress_percent(32 + value * 66, "切割"),
            should_cancel=lambda: handle.cancelled,
            on_log=handle.log,
        )
    except TaskCancelled as exc:
        raise TaskCancelledError() from exc
    if not files:
        raise TaskExecutionError("missing_output", "音频切割未生成文件")
    outputs = [Path(file["path"]) for file in files]
    metadata_files = [
        {
            "name": file["name"],
            "size": file["size"],
            "duration": file["duration"],
        }
        for file in files
    ]
    handle.progress_percent(100, "完成")
    first = outputs[0]
    additional: list[TaskFileOutcome] = []
    for index, (file, path) in enumerate(zip(files[1:], outputs[1:]), 1):
        additional.append(
            TaskFileOutcome(
                temp_path=path,
                output_name=safe_display_name(file["name"]),
                content_type=mimetypes.guess_type(file["name"])[0] or "application/octet-stream",
                size_bytes=path.stat().st_size,
                sha256=sha256_file(path),
                publish_module="07_output",
            )
        )
    return TaskOutcome(
        temp_path=first,
        output_name=safe_display_name(files[0]["name"]),
        content_type=mimetypes.guess_type(files[0]["name"])[0] or "application/octet-stream",
        size_bytes=first.stat().st_size,
        sha256=sha256_file(first),
        metadata={
            "engine": "audio.cut",
            "source_file_id": item.id,
            "output_dir": "07_output",
            "file_count": len(files),
            "files": metadata_files,
            "duration": duration,
            "smart_align": smart_align,
        },
        publish_module="07_output",
        additional_outputs=tuple(additional),
    )


def _execute_load_simulation(claim: TaskClaim) -> TaskOutcome:
    if os.getenv("NARRIFY_LOAD_SIMULATION_ENABLED", "false").lower() != "true":
        raise TaskExecutionError("simulation_disabled", "当前 worker 未启用负载模拟")
    if claim.task_type not in {"script.parse", "tts.batch"}:
        raise TaskExecutionError("invalid_simulation_type", "负载模拟任务类型无效")

    expected_kind = "llm" if claim.task_type == "script.parse" else "tts"
    if claim.payload.get("_simulation_kind") != expected_kind:
        raise TaskExecutionError("invalid_simulation_kind", "负载模拟任务配置无效")
    duration = max(0.0, min(float(claim.payload.get("_simulation_seconds", 0)), 3600.0))
    started = time.monotonic()
    next_progress = 1
    while True:
        if cancellation_requested(claim):
            raise TaskCancelledError()
        elapsed = time.monotonic() - started
        if elapsed >= duration:
            break
        fraction = elapsed / duration if duration else 1.0
        progress_step = min(3, int(fraction * 4))
        if progress_step >= next_progress:
            update_progress(claim, progress_step * 25, f"模拟 {expected_kind.upper()} 处理")
            next_progress = progress_step + 1
        time.sleep(min(0.5, duration - elapsed))

    metadata = {
        "engine": claim.task_type,
        "simulated": True,
        "simulation_kind": expected_kind,
        "duration_seconds": duration,
    }
    return _write_outcome(
        claim,
        f"load-simulation-{claim.task_id}.json",
        "application/json",
        json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
        metadata,
    )


def execute_claim(claim: TaskClaim) -> TaskOutcome:
    """Execute one real deterministic engine behind the durable worker boundary."""
    if claim.payload.get("_load_simulation") is True:
        return _execute_load_simulation(claim)
    if claim.task_type not in SUPPORTED_TASK_TYPES:
        raise TaskExecutionError("unsupported_task_type", f"不支持的任务类型：{claim.task_type}")
    if claim.task_type in LEGACY_ENGINE_TASK_TYPES:
        from .legacy_task_executor import execute_legacy_engine

        return execute_legacy_engine(claim)
    if claim.task_type == "script.parse":
        return _execute_script_parse(claim)
    if claim.task_type == "audio.silences":
        return _execute_audio_silences(claim)
    if claim.task_type == "audio.cut":
        return _execute_audio_cut(claim)
    with SessionLocal() as db:
        _, _, item, source_path = _input_file(db, claim)
        if cancellation_requested(claim):
            raise TaskCancelledError()
        update_progress(claim, 10, "读取输入")
        source_text, encoding = decode_buffer(source_path.read_bytes())

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

    if claim.task_type == "book.analyze":
        analysis = _book_analysis_result(source_text, encoding, str(source_path))
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

    raw = analyze_text(source_text)
    chapters = raw["chapters"]
    base = str(claim.payload.get("base") or base_name(item.original_name)).strip() or base_name(item.original_name)
    outputs: list[tuple[str, str, bytes]] = []
    final_chapters: list[dict[str, Any]] = []
    repair_status: str | None = None
    repair_report: dict[str, Any] | None = None
    baseline_chars: int | None = None
    if bool(claim.payload.get("whole_book")):
        name = make_whole_book_filename(base)
        outputs.append((name, "text/plain; charset=utf-8", source_text.encode("utf-8")))
    else:
        if not bool(claim.payload.get("smart")):
            raise TaskExecutionError("invalid_payload", "分册任务必须启用 smart 或 whole_book")
        if not chapters:
            raise TaskExecutionError("no_chapters", "未检测到章节，无法分册")
        repair = smart_repair(source_text, chapters)
        if repair["status"] == "error":
            raise TaskExecutionError("invalid_structure", repair.get("error") or "章节结构修复失败")
        repair_status = repair["status"]
        repair_report = repair["report"]
        baseline_chars = repair.get("baseline_chars")
        repaired = repair["chapters"]
        names = make_smart_filenames(repaired)
        final_chapters = [
            {
                "seq": index,
                "num": chapter.get("final_num", chapter.get("num")),
                "numStr": str(chapter.get("final_num", chapter.get("numStr"))),
                "title": chapter["title"],
                "chars": chapter["chars"],
                "orig_num": chapter.get("repair", {}).get("orig_num", chapter.get("num")),
                "orig_numStr": chapter.get("repair", {}).get("orig_numStr", chapter.get("numStr", "")),
                "final_num": chapter.get("final_num", index),
                "actions": chapter.get("repair", {}).get("actions", ["kept"]),
                "confidence": chapter.get("repair", {}).get("confidence", "high"),
            }
            for index, chapter in enumerate(repaired, 1)
        ]
        outputs.extend(
            (
                name,
                "text/plain; charset=utf-8",
                chapter_content(raw, chapter).encode("utf-8"),
            )
            for name, chapter in zip(names, repaired)
        )
    update_progress(claim, 75, "完成分册")
    return _write_outcome(
        claim,
        outputs[0][0],
        outputs[0][1],
        outputs[0][2],
        {
            "engine": "book.split",
            "source_file_id": item.id,
            "file_count": len(outputs),
            "chapters": final_chapters,
            "status": repair_status or "ok",
            "report": repair_report or {"actions": [], "warnings": [], "removed": []},
            "baseline_chars": baseline_chars,
            "original_count": len(chapters),
            "expected_format": EXPECTED_CHAPTER_FORMAT,
            "files": [{"name": name, "chars": len(data.decode("utf-8").replace("\n", "").replace("\r", ""))} for name, _, data in outputs],
        },
        publish_module="02_split_text",
        additional_outputs=outputs[1:],
    )


def _cleanup_outcome(outcome: TaskOutcome) -> None:
    if outcome.publication_journal is not None:
        try:
            outcome.publication_journal.rollback()
        except Exception:
            logging.getLogger(__name__).exception("Could not roll back attempt artifact publication")
    outcome.temp_path.unlink(missing_ok=True)
    for extra in outcome.additional_outputs:
        extra.temp_path.unlink(missing_ok=True)
    for extra in outcome.side_effect_outputs:
        extra.temp_path.unlink(missing_ok=True)


@contextmanager
def _legacy_engine_context(claim: TaskClaim):
    """Bind the managed workspace and immutable config snapshot for one engine call.

    Several mature engines still obtain their settings through ``get_config``.
    Keeping the snapshot in the task payload makes a retry deterministic without
    changing every engine signature at once.  A Worker processes one claim at a
    time, so replacing this workspace's cache entry is scoped and restored in
    the ``finally`` block.
    """
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        if user is None:
            raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
        workspace = user_workspace_root(db, user.username, claim.project_id)
    snapshot = claim.payload.get("config")
    config_token = None
    if isinstance(snapshot, dict):
        config_token = core_config.bind_task_config(core_config.AppConfig.model_validate(snapshot))
    token = bind_workspace(workspace)
    try:
        yield workspace
    finally:
        reset_workspace(token)
        if config_token is not None:
            core_config.reset_task_config(config_token)


def _legacy_result_outcome(claim: TaskClaim, result: Any) -> TaskOutcome:
    """Persist an engine's JSON result as a durable task result artifact."""
    if isinstance(result, dict):
        metadata = dict(result)
    else:
        metadata = {"value": result}
    metadata["engine"] = claim.task_type
    data = json.dumps(metadata, ensure_ascii=False, default=str).encode("utf-8")
    output_name = f"{claim.task_type.replace('.', '_')}_{claim.attempt_id}.json"
    return _write_outcome(
        claim,
        output_name,
        "application/json",
        data,
        metadata,
    )


def complete_claim(claim: TaskClaim, outcome: TaskOutcome) -> bool:
    with SessionLocal() as db:
        task, attempt = _attempt_is_current(db, claim)
        if task is None or attempt is None or task.status == "cancelling":
            db.rollback()
            _cleanup_outcome(outcome)
            return False
        user = db.get(User, task.owner_id)
        if user is None:
            db.rollback()
            _cleanup_outcome(outcome)
            return False
        outputs = [
            TaskFileOutcome(
                temp_path=outcome.temp_path,
                output_name=outcome.output_name,
                content_type=outcome.content_type,
                size_bytes=outcome.size_bytes,
                sha256=outcome.sha256,
                publish_module=outcome.publish_module,
            ),
            *outcome.additional_outputs,
        ]
        module_names = {name for _, name in WORKSPACE_DIRS if name != "00_temp"}
        if any(item.publish_module and item.publish_module not in module_names for item in outputs):
            db.rollback()
            _cleanup_outcome(outcome)
            return False
        legacy_module_paths: list[tuple[str, Path]] = []
        stale_book_paths: list[Path] = []
        module_outputs = {item.publish_module for item in outputs if item.publish_module}
        for module in module_outputs:
            module_prefix = f"{safe_display_name(user.username)}/{task.project_id}/{module}/"
            for existing in db.scalars(
                select(ProjectFile).where(
                    ProjectFile.owner_id == task.owner_id,
                    ProjectFile.project_id == task.project_id,
                    ProjectFile.object_key.like(module_prefix + "%"),
                )
            ).all():
                relative_parts = existing.object_key.removeprefix(module_prefix).split("/")
                if len(relative_parts) == 2:
                    try:
                        UUID(relative_parts[0])
                    except ValueError:
                        continue
                    existing.deleted_at = utcnow()
                    legacy_module_paths.append((module, object_path(existing.object_key, configured_storage_root(db))))
            if claim.task_type == "book.split" and module == "02_split_text":
                output_names = {safe_display_name(item.output_name) for item in outputs if item.publish_module == module}
                for existing in db.scalars(
                    select(ProjectFile).where(
                        ProjectFile.owner_id == task.owner_id,
                        ProjectFile.project_id == task.project_id,
                        ProjectFile.kind == "artifact",
                        ProjectFile.deleted_at.is_(None),
                        ProjectFile.object_key.like(module_prefix + "%"),
                    )
                ).all():
                    relative_name = existing.object_key.removeprefix(module_prefix)
                    if "/" in relative_name or relative_name in output_names or not is_generated_split_output_name(relative_name):
                        continue
                    existing.deleted_at = utcnow()
                    stale_book_paths.append(object_path(existing.object_key, configured_storage_root(db)))
        journal = outcome.publication_journal or PublicationJournal(
            configured_storage_root(db),
            task_attempt_path(db, user.username, task.project_id, task.id, attempt.id, "publication.json"),
        )
        with publication_transaction(journal, prepared=outcome.publication_journal is not None):
            published: list[dict[str, Any]] = []
            for item in outputs:
                file_record = None
                if item.publish_module:
                    object_key = f"{safe_display_name(user.username)}/{task.project_id}/{item.publish_module}/{safe_display_name(item.output_name)}"
                    file_record = db.scalar(select(ProjectFile).where(ProjectFile.object_key == object_key))
                    output_id = file_record.id if file_record is not None else new_id()
                else:
                    output_id = new_id()
                    object_key = project_object_key(user.username, task.project_id, output_id, item.output_name)
                final_path = object_path(object_key, configured_storage_root(db))
                journal.publish(journal.add(final_path), item.temp_path)
                if item.publish_module and file_record is not None:
                    file_record.original_name = item.output_name
                    file_record.content_type = item.content_type
                    file_record.size_bytes = item.size_bytes
                    file_record.sha256 = item.sha256
                    file_record.kind = "artifact"
                    file_record.deleted_at = None
                else:
                    db.add(
                        ProjectFile(
                            id=output_id,
                            project_id=task.project_id,
                            owner_id=task.owner_id,
                            original_name=item.output_name,
                            object_key=object_key,
                            content_type=item.content_type,
                            size_bytes=item.size_bytes,
                            sha256=item.sha256,
                            kind="artifact",
                        )
                    )
                published.append(
                    {
                        "file_id": output_id,
                        "object_key": object_key,
                        "name": item.output_name,
                        "path": str(final_path) if item.publish_module else None,
                    }
                )
            for item in outcome.side_effect_outputs:
                journal.publish(journal.add(item.final_path), item.temp_path)
            for target in outcome.side_effect_deletes:
                journal.remove(target)
            for target in stale_book_paths:
                journal.remove(target)
            result_payload = {
                "file_id": published[0]["file_id"],
                "object_key": published[0]["object_key"],
                "name": published[0]["name"],
                **outcome.metadata,
            }
            if outcome.publish_module:
                result_payload["path"] = published[0]["path"]
            if len(published) > 1 or isinstance(outcome.metadata.get("files"), list):
                described = outcome.metadata.get("files") if isinstance(outcome.metadata.get("files"), list) else []
                result_payload["files"] = [
                    {**published[index], **(described[index] if index < len(described) and isinstance(described[index], dict) else {})}
                    for index in range(len(published))
                ]
            db.add(TaskResult(task_id=task.id, result=result_payload))
            attempt.status = "succeeded"
            attempt.finished_at = utcnow()
            attempt.lease_expires_at = None
            task.status = "succeeded"
            task.progress = 100
            task.finished_at = utcnow()
            task.updated_at = utcnow()
            from .quota import release_attempt_holds
            release_attempt_holds(task.id, attempt.id, db=db)
            settle_reservation(db, task, note=f"{claim.task_type} completed")
            append_task_event(db, task.id, "succeeded", {"attempt_id": attempt.id, "file_id": output_id})
            db.commit()
        storage_root = configured_storage_root(db) / safe_display_name(user.username) / task.project_id
        for module, legacy_path in legacy_module_paths:
            try:
                legacy_path.unlink(missing_ok=True)
            except OSError:
                continue
            module_root = storage_root / module
            parent = legacy_path.parent
            while parent != module_root and parent.is_relative_to(module_root):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
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
        from .quota import release_attempt_holds
        release_attempt_holds(task.id, attempt.id, db=db)
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
            task.next_attempt_at = utcnow() + timedelta(seconds=delay)
            db.add(
                OutboxEvent(
                    aggregate_type="task",
                    aggregate_id=task.id,
                    event_type="task.retry",
                    payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id},
                    available_at=task.next_attempt_at,
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


def _process_claim(claim: TaskClaim) -> str:
    from .quota import reset_quota_context, set_quota_context
    from .worker_registry import heartbeat as worker_heartbeat

    quota_token = set_quota_context(claim.owner_id, claim.task_id, claim.attempt_id)
    stop = threading.Event()

    def report_worker(status: str, task_id: str | None) -> None:
        try:
            worker_heartbeat(claim.worker_id, status=status, current_task_id=task_id)
        except Exception:
            # A registry update must not prevent the fenced attempt from running.
            pass

    report_worker("running", claim.task_id)

    def renew() -> None:
        interval = max(1.0, min(10.0, settings.task_lease_seconds / 3))
        while not stop.wait(interval):
            if not heartbeat_claim(claim):
                return
            report_worker("running", claim.task_id)

    heartbeat = threading.Thread(target=renew, name=f"lease-{claim.task_id[:8]}", daemon=True)
    heartbeat.start()
    try:
        with _workspace_engine_lock(claim) as active:
            if not active:
                if cancellation_requested(claim):
                    fail_claim(claim, TaskCancelledError())
                    return "cancelled"
                return "skipped"
            outcome = execute_claim(claim)
            if cancellation_requested(claim):
                _cleanup_outcome(outcome)
                raise TaskCancelledError()
            try:
                completed = complete_claim(claim, outcome)
            except Exception:
                _cleanup_outcome(outcome)
                raise
            if completed:
                return "succeeded"
            fail_claim(claim, TaskCancelledError())
            return "cancelled"
    except TaskExecutionError as exc:
        fail_claim(claim, exc)
        return exc.code
    except Exception as exc:
        from .quota import QuotaInsufficientError
        if isinstance(exc, QuotaInsufficientError):
            fail_claim(claim, TaskExecutionError("quota_insufficient", str(exc)))
            return "quota_insufficient"
        fail_claim(claim, TaskExecutionError("worker_error", str(exc), retryable=True))
        return "worker_error"
    finally:
        stop.set()
        heartbeat.join(timeout=2)
        report_worker("idle", None)
        reset_quota_context(quota_token)


def process_task_message(message: dict[str, Any], *, worker_id: str) -> str:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else message
    task_id = str(payload.get("task_id", ""))
    if not task_id:
        return "invalid"
    claim = claim_task(task_id, worker_id)
    if claim is None:
        return "skipped"
    return _process_claim(claim)


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
            .where(Task.status.in_(["pending", "queued", "retrying", "running", "cancelling"]))
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
            if task.status in {"running", "cancelling"} and attempt is not None and _as_utc(attempt.lease_expires_at) and _as_utc(attempt.lease_expires_at) > now:
                continue
            if attempt is not None and attempt.status == "running":
                _reconcile_attempt_publication(db, task, attempt)
                attempt.status = "expired"
                attempt.finished_at = now
                attempt.error_message = "worker lease expired during recovery"
                append_task_event(db, task.id, "attempt_expired", {"attempt_id": attempt.id, "recovered": True})
                from .quota import release_attempt_holds
                release_attempt_holds(task.id, attempt.id, db=db)
            if task.status == "cancelling":
                task.status = "cancelled"
                task.finished_at = now
                task.next_attempt_at = None
                release_reservation(db, task, note="cancelled during recovery")
                suppress_pending_dispatch(db, task.id)
                append_task_event(db, task.id, "cancelled", {"reason": "worker lease expired"})
                continue
            if attempt is not None and attempt.status == "expired":
                if attempt.attempt_no >= settings.task_max_attempts:
                    task.status = "failed"
                    task.error_code = "lease_expired"
                    task.error_message = "Worker 租约过期且已达到最大尝试次数"
                    task.finished_at = now
                    release_reservation(db, task, kind="release", note="lease expired")
                    append_task_event(db, task.id, "failed", {"code": task.error_code})
                    continue
                task.status = "retrying"
                task.next_attempt_at = now

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
            # Outbox ids are UUID-sized (VARCHAR(36)); use a deterministic UUID
            # so recovery remains idempotent without overflowing the column.
            event_id = str(uuid5(NAMESPACE_URL, f"recover:{task.id}:{attempt_number}"))
            if db.get(OutboxEvent, event_id) is None:
                db.add(
                    OutboxEvent(
                        id=event_id,
                        aggregate_type="task",
                        aggregate_id=task.id,
                        event_type="task.recovered",
                        payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id},
                        available_at=task.next_attempt_at or now,
                    )
                )
                append_task_event(db, task.id, "dispatch_recovered", {"attempt_no": attempt_number})
                recovered += 1
        db.commit()
    return recovered


def run_once(client: redis.Redis, *, worker_id: str, block_ms: int = 1000) -> str:
    ensure_consumer_group(client)
    fair_claim = claim_fair_task(worker_id)
    if fair_claim is not None:
        return _process_claim(fair_claim)
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
