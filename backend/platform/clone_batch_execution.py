"""One isolated model run for independently leased character task records."""
from __future__ import annotations

from dataclasses import replace

from .pooled_task_execution import PooledTaskContext
from .task_contracts import TaskExecutionError
from .task_engine_support import engine_execution_context, engine_result_outcome
from .task_validation import legacy_task_payload_error


class CloneBatchContext(PooledTaskContext):
    batch_checkpoints = True

    def __init__(self, claims, finish, cancel):
        super().__init__(claims, finish, cancel, lambda claim: claim.payload["speakers"][0])

    def speaker_cancelled(self, speaker):
        return self.entry_cancelled(speaker)

    def speaker_progress(self, speaker, done, total):
        if speaker not in self.completed and speaker not in self.results:
            self.contexts[speaker].progress(done / max(1, total), f"候选 {done}/{total}")

    def speaker_settled(self, result):
        speaker = result["speaker"]
        self.results[speaker] = result
        ctx = self.contexts[speaker]
        ctx.progress(1.0, "克隆音频已完成" if result["ok"] else "克隆音频失败")
        ctx.log(f"{speaker} 克隆音频{'完成' if result['ok'] else '失败'}")
        if ctx.claim.task_id != self.claim.task_id:
            if self.finish(ctx.claim, self._result(speaker)):
                self.completed.add(speaker)

    def _result(self, speaker):
        result = self.results.get(speaker)
        return {"count": int(result is not None), "ok": int(bool(result and result["ok"])),
                "failed": int(bool(result and not result["ok"])), "speakers": [speaker],
                "results": [result] if result else [], **self.output_paths}


def execute_clone_batch(claims, finish, cancel):
    from ..engines.voices import generate_voice_candidates
    from ..core.paths import get_or_prepare_layout

    handle = CloneBatchContext(claims, finish, cancel)
    primary = claims[0]
    payload = primary.payload
    validation_error = legacy_task_payload_error(primary.task_type, payload)
    if validation_error:
        raise TaskExecutionError("invalid_payload", validation_error)
    try:
        with engine_execution_context(primary):
            # Settled characters are durable checkpoints. A later character's
            # cancellation or engine failure must not undo an already completed
            # task's WAVs, voice_config or output invalidation.
            handle.mark_workspace_checkpoint_directory(get_or_prepare_layout().voice_profiles)
            layout = get_or_prepare_layout()
            handle.output_paths = {"voice_config_path": str(layout.voice_profiles / "voice_config.json"),
                                   "output_dir": str(layout.voice_profiles / "designed_voices")}
            generate_voice_candidates(
                handle, list(handle.contexts), bool(payload.get("new_only")),
                payload.get("concurrency"), payload.get("script"), payload.get("candidate_count"),
            )
            for speaker, ctx in handle.contexts.items():
                if speaker not in handle.completed and ctx.claim.task_id != primary.task_id:
                    if not handle.speaker_cancelled(speaker):
                        while not finish(ctx.claim, handle._result(speaker)):
                            ctx.check()
                        handle.completed.add(speaker)
        outcome = engine_result_outcome(primary, handle._result(primary.payload["speakers"][0]))
        return replace(outcome, publication_journal=handle.publication_journal)
    except BaseException:
        handle.rollback_publications()
        raise
