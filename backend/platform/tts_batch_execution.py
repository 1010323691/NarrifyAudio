"""Restore one cross-chapter TTS pool while retaining per-chapter task records."""
from __future__ import annotations

from dataclasses import replace
import time
from sqlalchemy import select

from .pooled_task_execution import PooledTaskContext
from .task_contracts import TaskExecutionError
from .task_engine_support import engine_execution_context, engine_result_outcome
from .task_validation import legacy_task_payload_error


class TTSBatchContext(PooledTaskContext):
    def __init__(self, claims, finish, cancel):
        super().__init__(claims, finish, cancel,
                         lambda claim: (claim.payload.get("scripts") or [claim.payload["script"]])[0])
        self.progress_snapshots = {}
        self.saving_results = False
        self.preparing = True
        self._controls = {}
        self._controls_at = 0.0

    def _preparation_controls(self):
        from .database import SessionLocal
        from .models import Task, TaskAttempt, utcnow
        from .task_context import _as_utc
        if self._controls and time.monotonic() - self._controls_at < 0.5:
            return
        claims = [ctx.claim for ctx in self.contexts.values()]
        now = utcnow()
        with SessionLocal() as db:
            statuses = dict(db.execute(select(Task.id, Task.status).where(Task.id.in_([c.task_id for c in claims]))).all())
            attempts = {a.id: a for a in db.scalars(select(TaskAttempt).where(TaskAttempt.id.in_([c.attempt_id for c in claims])))}
        self._controls = {}
        for claim in claims:
            attempt = attempts.get(claim.attempt_id)
            valid = bool(attempt and attempt.task_id == claim.task_id and attempt.lease_token == claim.lease_token
                         and attempt.status == 'running' and _as_utc(attempt.lease_expires_at)
                         and _as_utc(attempt.lease_expires_at) > now)
            self._controls[claim.task_id] = (statuses.get(claim.task_id), valid)
        self._controls_at = time.monotonic()

    @property
    def cancelled(self):
        if not self.preparing:
            return super().cancelled
        self._preparation_controls()
        status, valid = self._controls[self.claim.task_id]
        return not valid or status in {'cancelling', 'cancelled', 'succeeded', 'failed', 'timeout', None}

    def check(self):
        if not self.preparing:
            return super().check()
        if self.cancelled:
            from .task_contracts import TaskCancelledError
            raise TaskCancelledError()
        for entry, ctx in self.contexts.items():
            status, _ = self._controls[ctx.claim.task_id]
            if entry not in self.completed and status in {'paused', 'queued'}:
                ctx.check()

    def entry_cancelled(self, entry):
        if not self.preparing:
            return super().entry_cancelled(entry)
        if self.cancelled:
            from .task_contracts import TaskCancelledError
            raise TaskCancelledError()
        if entry in self.completed:
            return True
        ctx = self.contexts[entry]
        status, valid = self._controls[ctx.claim.task_id]
        if not valid or status in {'cancelling', 'cancelled', 'failed', 'timeout', None}:
            self.cancel(ctx.claim)
            self.completed.add(entry)
            return True
        return False

    def finish_preparation(self):
        self.preparing = False

    def chapter_cancelled(self, name):
        return self.entry_cancelled(name)

    def chapter_progress(self, name, done, total, chars, chars_total):
        if name in self.completed or name in self.results:
            return
        snapshot = (done, total, chars, chars_total)
        if self.progress_snapshots.get(name) == snapshot:
            return
        ctx = self.contexts[name]
        label = (f"正在保存合成结果 · 已保存 {done}/{total} 段 · 待保存 {max(0, total - done)} 段"
                 if self.saving_results else f"已合成 {done}/{total} 段")
        if ctx.chapter_snapshot(done, total, chars, chars_total, label):
            self.progress_snapshots[name] = snapshot

    def chapter_progress_many(self, chapters):
        from .task_context import write_claim_events
        entries, snapshots = [], {}
        for name, done, total, chars, chars_total in chapters:
            if name in self.completed or name in self.results:
                continue
            snapshot = (done, total, chars, chars_total)
            if self.progress_snapshots.get(name) == snapshot:
                continue
            ctx = self.contexts[name]
            label = f"已合成 {done}/{total} 段"
            entries.append((ctx.claim, [("segments", {"done": done, "total": total, "chars_done": chars, "chars_total": chars_total}),
                                        ("progress", {"progress": round(done / max(1, total) * 100), "current": label})]))
            snapshots[ctx.claim.task_id] = (name, snapshot)
        for task_id in write_claim_events(entries):
            name, snapshot = snapshots[task_id]
            self.progress_snapshots[name] = snapshot

    def phase(self, name):
        from .task_context import write_claim_events
        self.saving_results = name == "正在保存合成结果"
        self.progress_snapshots.clear()
        write_claim_events([(ctx.claim, [("phase", {"phase": name, "current": name})]) for entry, ctx in self.contexts.items()
                            if entry not in self.completed and entry not in self.results])

    def progress(self, fraction, current=""):
        # Preparation/model loading is a phase, never completed-audio progress.
        if current in {"解析输入", "加载模型", "模型就绪"} and getattr(self, "startup_phase", None) != current:
            if current == '加载模型':
                self.model_started = time.monotonic()
            elif current == '模型就绪' and getattr(self, 'model_started', None) is not None:
                from ..core.observability import record_tts_stage
                record_tts_stage('model_load', (time.monotonic() - self.model_started) * 1000)
            self.startup_phase = current
            self.phase(current)

    def segment_stats(self, *args):
        # synthesize_multi supplies per-chapter counters through chapter_progress.
        # Sending the whole pool to one chapter would double count the page summary.
        pass

    def chapter_settled(self, result):
        name = result["files"][0]["script"]
        if name in self.completed or name in self.results or self.entry_cancelled(name):
            return
        self.results[name] = result
        ctx = self.contexts[name]
        failed = result.get("error") or (result["total"] > 0 and result["completed"] == 0)
        ctx.progress(1.0, "音频合成失败" if failed else
                     "音频合成部分完成" if result["failed"] else "音频合成已完成")
        if ctx.claim.task_id != self.claim.task_id and self.finish(ctx.claim, result):
            self.completed.add(name)


def execute_tts_batch(claims, finish, cancel):
    from ..engines.tts_batch import synthesize_multi
    from ..core.paths import get_or_prepare_layout

    primary = claims[0]
    payload = primary.payload
    error = legacy_task_payload_error(primary.task_type, payload)
    if error:
        raise TaskExecutionError("invalid_payload", error)
    handle = TTSBatchContext(claims, finish, cancel)
    try:
        with engine_execution_context(primary):
            handle.mark_workspace_checkpoint_directory(get_or_prepare_layout().audio_chunk)
            synthesize_multi(handle, list(handle.contexts), payload.get("concurrency"),
                             payload.get("seed"), payload.get("auto_concurrency"))
            for name, ctx in handle.contexts.items():
                if ctx.claim.task_id == primary.task_id or name in handle.completed:
                    continue
                if not handle.entry_cancelled(name):
                    while not finish(ctx.claim, handle.results[name]):
                        ctx.check()
                    handle.completed.add(name)
        result = handle.results[handle.entry_key(primary)]
        if result.get("error") or (result["total"] > 0 and result["completed"] == 0):
            raise TaskExecutionError("tts_chapter_failed", result.get("error") or "本章全部段落合成失败")
        return replace(engine_result_outcome(primary, result), publication_journal=handle.publication_journal)
    except BaseException as exc:
        handle.rollback_publications()
        result = handle.results.get(handle.entry_key(primary))
        if isinstance(exc, RuntimeError) and result and (result.get("error") or (result["total"] > 0 and result["completed"] == 0)):
            raise TaskExecutionError("tts_chapter_failed", result.get("error") or "本章全部段落合成失败") from None
        raise
