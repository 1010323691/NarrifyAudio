"""Durable task execution context and fenced attempt event operations."""
from __future__ import annotations

import secrets
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..core.concurrency import gate, merge_gate
from ..core.task_control import TaskCancelled
from .artifact_publication import PublicationJournal, PublicationJournalBundle
from .database import SessionLocal
from .models import Task, TaskAttempt, TaskEvent, User, utcnow
from .storage import configured_storage_root, safe_display_name, task_attempt_path
from .task_contracts import (
    TaskClaim, TaskExecutionError,
)
from .task_lifecycle import TERMINAL_TASK_STATUSES, append_task_event
from .task_admission import llm_task_capacity_available, LLM_TASK_TYPES
from .gpu_scheduler.store import host_lock


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class EngineExecutionContext:
    """Adapter from the legacy engine callback contract to durable task events."""

    def __init__(self, claim: TaskClaim):
        self.claim = claim
        self._publication_journal: PublicationJournal | None = None
        self._shared_publication_journal: PublicationJournal | None = None
        self._staged_workspace_paths: set[Path] = set()
        self._staged_workspace_directories: set[Path] = set()
        self._guarded_workspace_paths: set[Path] = set()
        self._workspace_checkpoint_directories: set[Path] = set()
        self._rollback_lock: tuple | None = None
        self._published_workspace_stages: set[Path] = set()
        self._pending_audio_stages: list[tuple[Path, Path]] = []
        self._audio_stage_started = 0.0
        self._attempt_directory: Path | None = None

    @property
    def cancelled(self) -> bool:
        return cancellation_requested(self.claim)

    def progress(self, fraction: float, current: str = "") -> None:
        if getattr(self.claim, "task_type", None) == "tts.batch" and current in {"启动引擎", "解析输入", "加载模型", "模型就绪"}:
            self.phase(current)
            return
        self.progress_percent(fraction * 100, current)

    def progress_percent(self, percent: float, current: str = "") -> None:
        update_progress(self.claim, round(max(0.0, min(100.0, percent))), current)

    def chapter_snapshot(self, done: int, total: int, chars: int, chars_total: int, current: str) -> bool:
        return update_chapter_snapshot(self.claim, done, total, chars, chars_total, current)

    def phase(self, name: str) -> None:
        payload = {"phase": name}
        if getattr(self.claim, "task_type", None) == "tts.batch":
            payload["current"] = name
        _append_claim_event(self.claim, "phase", payload)

    def log(self, message: str, level: str = "INFO") -> None:
        _append_claim_event(self.claim, "log", {"level": level, "msg": message})

    def llm_chunk(self, _text: str) -> None:
        # Raw model output is intentionally not persisted in TaskEvent history.
        # The durable result is the published JSON artifact.
        return

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
        # The worker holds the storage/project fence for this immutable attempt.
        # Resolving its root again for every chapter adds hundreds of DB reads.
        if self._attempt_directory is None:
            with SessionLocal() as db:
                user = db.get(User, self.claim.owner_id)
                if user is None:
                    raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
                self._attempt_directory = task_attempt_path(
                    db, user.username, self.claim.project_id, self.claim.task_id,
                    self.claim.attempt_id, "stage",
                ).parent
        return self._attempt_directory / f"workspace-{secrets.token_hex(12)}-{safe_display_name(final_path.name)}"

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

    def mark_workspace_checkpoint_directory(self, directory: Path) -> None:
        """Keep completed incremental outputs here on failure and crash recovery."""
        self._workspace_checkpoint_directories.add(directory.resolve())

    def discard_workspace_directory(self, staged: Path) -> None:
        resolved = staged.resolve()
        if resolved not in self._staged_workspace_directories:
            raise ValueError("Workspace staging directory was not allocated by this task")
        shutil.rmtree(resolved, ignore_errors=True)
        self._staged_workspace_directories.discard(resolved)

    def _publish_staged_workspace_file(self, final_path: Path, staged: Path) -> None:
        # A manifest/metadata checkpoint must never precede its completed audio.
        self.flush_workspace_stages(force=True)
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
            # Guarded entries (see mark_workspace_guarded) are restored on
            # rollback only if still untouched by a concurrent writer.
            resolved = final_path.resolve()
            checkpoint = any(resolved.is_relative_to(directory)
                             for directory in self._workspace_checkpoint_directories)
            journal.publish(journal.add(
                final_path, guard=resolved in self._guarded_workspace_paths,
                checkpoint=checkpoint,
            ), staged)
        except BaseException:
            staged.unlink(missing_ok=True)
            self._staged_workspace_paths.discard(staged)
            raise
        self._published_workspace_stages.add(resolved)
        self._staged_workspace_paths.discard(staged)

    def workspace_stage_published(self, final_path: Path) -> bool:
        """Confirm this attempt moved new bytes; existing originals are not evidence."""
        return final_path.resolve() in self._published_workspace_stages

    def publish_workspace_stages(self, stages: list[tuple[Path, Path]]) -> None:
        """Durably register completed audio together before moving any file."""
        if not stages:
            return
        with SessionLocal() as db:
            user = db.get(User, self.claim.owner_id)
            if user is None:
                raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
            attempt_root = task_attempt_path(
                db, user.username, self.claim.project_id, self.claim.task_id,
                self.claim.attempt_id, "publication-check",
            ).parent.resolve()
        for _final, staged in stages:
            if not staged.resolve().is_relative_to(attempt_root) or not staged.is_file():
                raise ValueError("Staged workspace output is outside the active task attempt")
        journal = self._ensure_publication_journal()
        finals = [final.resolve() for final, _staged in stages]
        indices = journal.add_many(
            finals,
            guards=[final in self._guarded_workspace_paths for final in finals],
            checkpoints=[any(final.is_relative_to(directory)
                             for directory in self._workspace_checkpoint_directories)
                         for final in finals],
        )
        for index, (_final, staged) in zip(indices, stages):
            journal.publish(index, staged)
            self._published_workspace_stages.add(journal.entries[index][0])
            self._staged_workspace_paths.discard(staged)

    def queue_workspace_stage(self, final_path: Path, staged: Path) -> None:
        # Only completed incremental audio may wait briefly for batch registration.
        if not any(final_path.resolve().is_relative_to(directory)
                   for directory in self._workspace_checkpoint_directories):
            self.publish_workspace_stage(final_path, staged)
            return
        if not self._pending_audio_stages:
            self._audio_stage_started = time.monotonic()
        self._pending_audio_stages.append((final_path, staged))
        self.flush_workspace_stages()

    def flush_workspace_stages(self, *, force: bool = False) -> None:
        pending = self._pending_audio_stages
        if not pending or (not force and len(pending) < 128
                           and time.monotonic() - self._audio_stage_started < 0.25):
            return
        # Clear before publication: on an I/O error the recovery journal owns
        # partial moves; never retry a moved source or register it twice.
        self._pending_audio_stages = []
        self.publish_workspace_stages(pending)

    def publish_workspace_stage(self, final_path: Path, staged: Path) -> None:
        self._publish_staged_workspace_file(final_path, staged)

    def discard_workspace_stage(self, staged: Path) -> None:
        staged.unlink(missing_ok=True)
        self._staged_workspace_paths.discard(staged)

    def publish_workspace_bytes(self, final_path: Path, data: bytes) -> None:
        """Journal and publish workspace bytes while preserving live updates."""
        staged = self._workspace_stage_path(final_path)
        try:
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
            self._publish_staged_workspace_file(final_path, staged)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def stage_workspace_file(self, final_path: Path, data: bytes) -> None:
        """Compatibility alias for engines using the former method name."""
        self.publish_workspace_bytes(final_path, data)

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
            # guard：回滚仅在文件仍是本任务所发布的字节时才还原，避免覆盖并发任务/手动编辑写入的标签。
            journal.publish(journal.add(final_path, guard=True), staged)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def mark_workspace_guarded(self, final_path: Path) -> None:
        """Engines call this before publishing a workspace file that is shared
        across processes (the 08_bgm JSON caches). Rollback then restores the
        entry ONLY while its bytes still match what this task published, so a
        concurrent writer (API tag propagation, manual edit, another worker)
        is never clobbered by a late failure rollback."""
        self._guarded_workspace_paths.add(final_path.resolve())

    def set_rollback_lock(self, lock_cm_factory, *args) -> None:
        """Register the cross-process lock (a context-manager factory, e.g.
        ``bgm_storage.storage_lock(layout)``) that rollback acquires around
        the guarded compare-and-restore — the same lock the writers hold, so
        the restore cannot interleave with an in-flight read-modify-write."""
        self._rollback_lock = (lock_cm_factory, args)

    def defer_workspace_delete(self, final_path: Path) -> None:
        self._ensure_publication_journal().remove(final_path)

    def rollback_publications(self) -> None:
        # Cooperative failure/cancel retains completed incremental audio, even
        # when it arrived just before the batching deadline.
        try:
            self.flush_workspace_stages(force=True)
        except Exception:
            logging.getLogger(__name__).exception("Could not publish buffered audio before rollback")
        for staged in self._staged_workspace_paths:
            staged.unlink(missing_ok=True)
        self._staged_workspace_paths.clear()
        for staged in self._staged_workspace_directories:
            shutil.rmtree(staged, ignore_errors=True)
        self._staged_workspace_directories.clear()
        if self._publication_journal is not None:
            lock = None
            if self._rollback_lock is not None:
                factory, args = self._rollback_lock
                lock = factory(*args)
            self._publication_journal.rollback(guarded_lock=lock)
        if self._shared_publication_journal is not None:
            from ..core.file_lock import exclusive_file_lock
            library_root = self._shared_publication_journal.root
            self._shared_publication_journal.rollback(
                guarded_lock=exclusive_file_lock(library_root / ".music_index.lock"))

    def _paused(self) -> bool:
        with SessionLocal() as db:
            task = db.get(Task, self.claim.task_id)
            resume_waiting = task is not None and task.status == "queued" and task.error_code == "resume_waiting"
            if not resume_waiting:
                return task is not None and task.status == "paused"
        # Use the same lock as claim_task, and hold it through commit so two
        # resumed/new tasks cannot both take the last available task position.
        with host_lock(), SessionLocal.begin() as db:
            task, attempt = _attempt_is_current(db, self.claim)
            if task is None or attempt is None:
                return False
            if task.status != "queued" or task.error_code != "resume_waiting":
                return task.status == "paused"
            if task.task_type in LLM_TASK_TYPES and not llm_task_capacity_available(db):
                return True
            if task.task_type == "tts.batch":
                from .tts_resource_budget import tts_capacity_available, set_tts_parked
                if not tts_capacity_available(db, resume_claim=self.claim):
                    return True
                set_tts_parked(db, self.claim, False)
            task.status = "running"
            task.error_code = ""
            task.error_message = ""
            task.updated_at = utcnow()
            append_task_event(db, task.id, "resumed", {"same_attempt": True})
            return False

    def _pause(self, on_pause=None) -> None:
        # Free scarce permits while parked so other tasks can make progress.
        permits = [(resource, resource.suspend_current_thread()) for resource in (gate(), merge_gate())]
        if on_pause is not None:
            on_pause()
        from . import mechanical_audio
        audio_parked = getattr(self.claim, "task_type", None) in mechanical_audio.AUDIO_TASK_TYPES and mechanical_audio.release(self.claim)
        if getattr(self.claim, "task_type", None) == "tts.batch":
            from .tts_resource_budget import set_tts_parked
            with host_lock(), SessionLocal.begin() as db:
                set_tts_parked(db, self.claim, True)
        delay = 0.25
        while self._paused():
            if self.cancelled:
                raise TaskCancelled()
            time.sleep(delay)
            delay = min(delay * 2, 5.0)
        if self.cancelled:
            raise TaskCancelled()
        if audio_parked:
            mechanical_audio.resume(self.claim, lambda: self.cancelled)
        restored = []
        for resource, count in permits:
            if not resource.restore_current_thread(count, stop_check=lambda: self.cancelled):
                for restored_resource, restored_count in reversed(restored):
                    for _ in range(restored_count):
                        restored_resource.release()
                raise TaskCancelled()
            restored.append((resource, count))

    def check_interruptible(self, on_pause) -> None:
        """Interrupt a child on pause, park, then return so its caller can restart it."""
        if self._paused():
            self._pause(on_pause)
        if self.cancelled:
            raise TaskCancelled()

    def check(self) -> None:
        # Back off to cap database load while a task remains paused.
        if self._paused():
            self._pause()
        if self.cancelled:
            raise TaskCancelled()




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


def update_chapter_snapshot(claim: TaskClaim, done: int, total: int, chars: int,
                            chars_total: int, current: str) -> bool:
    """Publish counters and the visible task progress under a single lease fence."""
    with SessionLocal() as db:
        task, attempt = _attempt_is_current(db, claim)
        if task is None or attempt is None:
            db.rollback()
            return False
        done, total = max(0, int(done)), max(0, int(total))
        task.progress = round(max(0, min(1, done / max(1, total))) * 100)
        task.updated_at = utcnow()
        append_task_event(db, task.id, "segments", {
            "done": done, "total": total, "chars_done": max(0, int(chars)),
            "chars_total": max(0, int(chars_total)),
        })
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
        if task is None or task.status in TERMINAL_TASK_STATUSES | {"cancelling"}:
            return True
        return (
            attempt is None
            or attempt.task_id != claim.task_id
            or attempt.status != "running"
            or attempt.lease_token != claim.lease_token
            or _as_utc(attempt.lease_expires_at) is None
            or _as_utc(attempt.lease_expires_at) <= utcnow()
        )




PersistentTaskHandle = EngineExecutionContext


def write_claim_events(entries):
    """Fence and persist a bounded collection of chapter events in one transaction."""
    from sqlalchemy import insert
    from .task_lifecycle import _update_ui_state
    from .models import new_id
    if not entries:
        return set()
    now = utcnow()
    with SessionLocal() as db:
        tasks = {row.id: row for row in db.scalars(select(Task).where(Task.id.in_([claim.task_id for claim, _ in entries]))
                                                  .order_by(Task.id).with_for_update())}
        attempts = {row.id: row for row in db.scalars(select(TaskAttempt).where(
            TaskAttempt.id.in_([claim.attempt_id for claim, _ in entries])).order_by(TaskAttempt.id).with_for_update())}
        accepted, events = set(), []
        for claim, updates in entries:
            task, attempt = tasks.get(claim.task_id), attempts.get(claim.attempt_id)
            if (task is None or attempt is None or attempt.task_id != task.id or attempt.status != 'running'
                    or attempt.lease_token != claim.lease_token or _as_utc(attempt.lease_expires_at) is None
                    or _as_utc(attempt.lease_expires_at) <= now
                    or task.status in TERMINAL_TASK_STATUSES):
                continue
            state = dict(task.ui_state or {})
            for kind, payload in updates:
                task.event_sequence = (task.event_sequence or 0) + 1
                _update_ui_state(state, kind, payload)
                if kind == 'progress':
                    task.progress = max(0, min(100, int(payload['progress'])))
                events.append(dict(id=new_id(), task_id=task.id, sequence=task.event_sequence,
                                   event_type=kind, payload=payload, created_at=now))
            task.ui_state = state; task.updated_at = now
            accepted.add(task.id)
        for offset in range(0, len(events), 100):
            db.execute(insert(TaskEvent), events[offset:offset + 100])
        db.commit()
        return accepted
