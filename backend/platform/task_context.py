"""Durable task execution context and fenced attempt event operations."""
from __future__ import annotations

import secrets
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..core.task_control import TaskCancelled
from .artifact_publication import PublicationJournal, PublicationJournalBundle
from .database import SessionLocal
from .models import Task, TaskAttempt, User, utcnow
from .storage import configured_storage_root, safe_display_name, task_attempt_path
from .task_contracts import (
    TaskClaim, TaskExecutionError,
)
from .task_state import TERMINAL_TASK_STATUSES, append_task_event


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _cps10(samples: deque[tuple[float, int]], total: int, now: float) -> float:
    """Average chars/s over the retained (≤10 s) span of rate samples.

    ``samples`` holds (monotonic_ts, cumulative_chars) oldest-first; the cumulative total is
    monotonic, so (total - first_total) is exactly the chars generated within the span and
    (total - first_total) / (now - first_ts) is the true average rate over it — no bias from
    how the flushes are spaced. Empty window or a near-zero span yields 0.
    """
    if not samples:
        return 0.0
    first_time, first_total = samples[0]
    span = now - first_time
    if span <= 0.001:
        return 0.0
    return max(0.0, (total - first_total) / span)



class EngineTaskContext:
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
            cps10 = _cps10(self._rate_samples, self._rate_total, now)
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




PersistentTaskHandle = EngineTaskContext
