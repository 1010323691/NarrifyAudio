"""Adapters that execute legacy engines behind the durable task boundary."""
from __future__ import annotations

import io
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

from ..core import config as core_config
from ..core.paths import get_or_prepare_layout
from ..core.task_control import TaskCancelled
from ..platform.database import SessionLocal
from ..platform.models import User
from ..platform.storage import safe_display_name, task_attempt_path
from ..platform.task_types import LEGACY_ENGINE_TASK_TYPES
from ..platform.task_validation import legacy_task_payload_error
from .task_contracts import (
    TaskCancelledError,
    TaskClaim,
    TaskExecutionError,
    TaskOutcome,
    TaskSideEffectOutput,
)
from .task_context import EngineTaskContext, cancellation_requested, update_progress
from .task_engine_support import engine_execution_context, engine_result_outcome, write_task_outcome

def execute_engine_task(claim: TaskClaim) -> TaskOutcome:
    """Run an existing business engine inside the durable Worker boundary."""
    from ..engines import bgm as bgm_engine
    from ..engines import merge as merge_engine
    from ..engines import music as music_engine
    from ..engines import tts_batch
    from ..engines import voices

    handle = EngineTaskContext(claim)
    payload = claim.payload
    if claim.task_type in LEGACY_ENGINE_TASK_TYPES:
        validation_error = legacy_task_payload_error(claim.task_type, payload)
        if validation_error:
            raise TaskExecutionError("invalid_payload", validation_error)
    side_effect_outputs: list[TaskSideEffectOutput] = []
    side_effect_deletes: list[Path] = []
    with engine_execution_context(claim):
        try:
            if claim.task_type == "voices.foundation":
                result = voices.prepare_foundations(
                    handle,
                    payload.get("speakers"),
                    bool(payload.get("new_only")),
                    payload.get("overrides") or {},
                    payload.get("script"),
                )
            elif claim.task_type == "voices.clone":
                result = voices.generate_voice_candidates(
                    handle,
                    payload.get("speakers"),
                    bool(payload.get("new_only")),
                    payload.get("concurrency"),
                    payload.get("script"),
                    payload.get("candidate_count"),
                )
            elif claim.task_type == "tts.batch":
                scripts = payload.get("scripts") or []
                if len(scripts) > 1:
                    result = tts_batch.synthesize_multi(
                        handle,
                        scripts,
                        payload.get("concurrency"),
                        payload.get("seed"),
                        payload.get("auto_concurrency"),
                    )
                else:
                    result = tts_batch.synthesize(
                        handle,
                        payload.get("indices"),
                        scripts[0] if scripts else payload.get("script"),
                        payload.get("concurrency"),
                        payload.get("seed"),
                        payload.get("auto_concurrency"),
                    )
            elif claim.task_type == "tts.merge":
                result = merge_engine.merge_audio_package(
                    handle,
                    bool(payload.get("m4b")),
                    str(payload.get("package") or ""),
                )
            elif claim.task_type == "bgm.analysis":
                cfg = core_config.get_config()
                result = bgm_engine.analyze_chapter(handle, str(payload["stem"]), cfg.llm, cfg.bgm)
            elif claim.task_type == "bgm.segment":
                cfg = core_config.get_config()
                result = bgm_engine.analyze_segment_chapter(handle, str(payload["stem"]), cfg.llm, cfg.bgm)
            elif claim.task_type == "bgm.mix":
                cfg = core_config.get_config()
                result = bgm_engine.mix_chapter(handle, str(payload["stem"]), cfg.bgm, cfg.ffmpeg)
            elif claim.task_type == "music.suggest_tags":
                with SessionLocal() as db:
                    user = db.get(User, claim.owner_id)
                    if user is None or not user.is_active or user.role != "admin":
                        raise TaskExecutionError("permission_revoked", "该任务现在需要管理员权限")
                cfg = core_config.get_config()
                result = music_engine.suggest_track_tags(
                    handle,
                    str(payload["name"]),
                    cfg.llm,
                    payload.get("description"),
                )
            elif claim.task_type == "bgm.match":
                cfg = core_config.get_config()
                stems = [str(value) for value in (payload.get("chapters") or [])]
                mode = str(payload.get("mode") or "llm")
                if mode == "segment":
                    result = bgm_engine.recompute_segment_timelines(
                        get_or_prepare_layout(),
                        stems,
                        cfg.bgm.min_match_score,
                        ffprobe_path=cfg.ffmpeg.ffprobe_path,
                        pause_ms=cfg.tts.pause_between_speakers_ms or 500,
                        same_ms=cfg.tts.pause_same_speaker_ms or 250,
                        volume_base=cfg.bgm.volume,
                        volume_tiers=cfg.bgm.segment_volume_tiers,
                        handle=handle,
                    )
                else:
                    result = bgm_engine.match_stems(
                        get_or_prepare_layout(), stems, mode, cfg.bgm.min_match_score,
                        handle=handle,
                    )
            elif claim.task_type == "bgm.package":
                layout = get_or_prepare_layout()
                stems = [str(value) for value in (payload.get("chapters") or [])]
                base = safe_display_name(str(payload.get("base") or "bgm"))
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as output:
                    for index, stem in enumerate(stems, 1):
                        if cancellation_requested(claim):
                            raise TaskCancelledError()
                        if (
                            not stem
                            or stem in {".", ".."}
                            or "/" in stem
                            or "\\" in stem
                            or "\x00" in stem
                        ):
                            raise TaskExecutionError("invalid_payload", "BGM 章节参数无效")
                        source = layout.bgm / f"{stem}.mp3"
                        try:
                            resolved_source = source.resolve(strict=True)
                            resolved_bgm = layout.bgm.resolve(strict=True)
                            inside_bgm = resolved_source.is_relative_to(resolved_bgm)
                        except (OSError, RuntimeError):
                            inside_bgm = False
                        if not inside_bgm or not resolved_source.is_file():
                            raise TaskExecutionError("input_missing", f"BGM 文件不存在：{stem}")
                        output.write(resolved_source, arcname=f"{base}/{stem}.mp3")
                        update_progress(claim, int(index * 90 / max(1, len(stems))), f"打包 {index}/{len(stems)}")
                result = write_task_outcome(
                    claim,
                    f"{base}.zip",
                    "application/zip",
                    archive.getvalue(),
                    {"engine": "bgm.package", "base": base, "file_count": len(stems)},
                    publish_module="08_bgm",
                )
                handle.progress_percent(100, "完成")
                return result
            elif claim.task_type == "audio.zip":
                workspace = get_or_prepare_layout().workspace
                if workspace is None:
                    raise TaskExecutionError("workspace_missing", "尚未设置工作空间")
                base = safe_display_name(str(payload.get("base") or "audio"))
                archive = io.BytesIO()
                files = payload.get("files") or []
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as output:
                    for index, item in enumerate(files, 1):
                        if cancellation_requested(claim):
                            raise TaskCancelledError()
                        if not isinstance(item, dict):
                            raise TaskExecutionError("invalid_payload", "打包文件参数无效")
                        relative = Path(str(item.get("relative_path") or ""))
                        source = (workspace / relative).resolve()
                        if not source.is_relative_to(workspace.resolve()) or not source.is_file():
                            raise TaskExecutionError("input_missing", "待打包文件不存在")
                        name = safe_display_name(str(item.get("name") or source.name))
                        output.write(source, arcname=name)
                        update_progress(claim, int(index * 90 / max(1, len(files))), f"打包 {index}/{len(files)}")
                    result = write_task_outcome(
                    claim,
                    f"{base}.zip",
                    "application/zip",
                    archive.getvalue(),
                    {"engine": "audio.zip", "file_count": len(files)},
                    publish_module="07_output",
                )
                handle.progress_percent(100, "完成")
                return result
            elif claim.task_type == "audio.export":
                workspace = get_or_prepare_layout().workspace
                if workspace is None:
                    raise TaskExecutionError("workspace_missing", "尚未设置工作空间")
                source_relative = Path(str(payload.get("source_relative") or ""))
                source = (workspace / source_relative).resolve()
                if not source.is_relative_to(workspace.resolve()) or not source.is_file():
                    raise TaskExecutionError("input_missing", "源音频不存在")
                destination = source.parent / "分集"
                written = []
                files = payload.get("files") or []
                for index, item in enumerate(files, 1):
                    if cancellation_requested(claim):
                        raise TaskCancelledError()
                    if not isinstance(item, dict):
                        raise TaskExecutionError("invalid_payload", "导出文件参数无效")
                    relative = Path(str(item.get("relative_path") or ""))
                    input_path = (workspace / relative).resolve()
                    if not input_path.is_relative_to(workspace.resolve()) or not input_path.is_file():
                        raise TaskExecutionError("input_missing", "待导出文件不存在")
                    name = safe_display_name(str(item.get("name") or input_path.name))
                    target = destination / name
                    with SessionLocal() as db:
                        user = db.get(User, claim.owner_id)
                        if user is None:
                            raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
                        staged = task_attempt_path(
                            db, user.username, claim.project_id, claim.task_id, claim.attempt_id,
                            f"audio-export-{index:05d}-{name}",
                        )
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(input_path, staged)
                    side_effect_outputs.append(TaskSideEffectOutput(staged, target))
                    written.append({"name": name, "path": str(target)})
                    update_progress(claim, int(index * 90 / max(1, len(files))), f"导出 {index}/{len(files)}")
                result = {"engine": "audio.export", "dest_dir": str(destination), "file_count": len(written), "files": written}
            elif claim.task_type == "tts.reset":
                layout = get_or_prepare_layout()
                scripts = [str(value) for value in (payload.get("scripts") or [])]
                removed = []
                for index, name in enumerate(scripts, 1):
                    if cancellation_requested(claim):
                        raise TaskCancelledError()
                    package = tts_batch.package_for(Path(name))
                    target = (layout.audio_chunk / package).resolve()
                    if not target.is_relative_to(layout.audio_chunk.resolve()):
                        raise TaskExecutionError("invalid_payload", "合成包路径无效")
                    if target.exists():
                        side_effect_deletes.append(target)
                        removed.append(package)
                    update_progress(claim, int(index * 90 / max(1, len(scripts))), f"重置 {index}/{len(scripts)}")
                result = {"engine": "tts.reset", "ok": True, "removed": removed}
            else:
                raise TaskExecutionError("unsupported_task_type", f"不支持的任务类型：{claim.task_type}")
        except TaskCancelled as exc:
            for item in side_effect_outputs:
                item.temp_path.unlink(missing_ok=True)
            handle.rollback_publications()
            raise TaskCancelledError() from exc
        except BaseException:
            for item in side_effect_outputs:
                item.temp_path.unlink(missing_ok=True)
            handle.rollback_publications()
            raise
    if isinstance(result, TaskOutcome):
        return replace(result, publication_journal=handle.publication_journal)
    handle.progress_percent(100, "完成")
    try:
        outcome = engine_result_outcome(claim, result)
    except BaseException:
        for item in side_effect_outputs:
            item.temp_path.unlink(missing_ok=True)
        raise
    return replace(
        outcome,
        side_effect_outputs=tuple(side_effect_outputs),
        side_effect_deletes=tuple(side_effect_deletes),
        publication_journal=handle.publication_journal,
    )
