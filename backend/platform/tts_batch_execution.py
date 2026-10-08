"""Restore one cross-chapter TTS pool while retaining per-chapter task records."""
from __future__ import annotations

from dataclasses import replace

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

    def phase(self, name):
        self.saving_results = name == "正在保存合成结果"
        self.progress_snapshots.clear()
        for entry, ctx in self.contexts.items():
            if entry not in self.completed and entry not in self.results:
                ctx.phase(name)

    def progress(self, fraction, current=""):
        # The subprocess reports whole-pool progress; each visible task owns
        # its chapter counters and completion marker instead.
        pass

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
