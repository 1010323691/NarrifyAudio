"""Adapters that execute legacy engines behind the durable task boundary."""
from __future__ import annotations

import os
import shutil
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from ..core import config as core_config
from ..core.paths import get_or_prepare_layout
from ..core.task_control import TaskCancelled
from ..core.safe_filesystem import file_identity
from ..platform.database import SessionLocal
from ..platform.models import User
from ..platform.storage import safe_display_name, task_attempt_path
from ..core.filenames import unique_filename
from ..platform.task_registry import TASK_TYPES
from ..platform.task_types import LEGACY_ENGINE_TASK_TYPES
from ..platform.task_validation import legacy_task_payload_error
from .task_contracts import (
    TaskCancelledError,
    TaskClaim,
    TaskExecutionError,
    TaskOutcome,
    TaskSideEffectOutput,
)
from .task_context import EngineExecutionContext, cancellation_requested, update_progress
from .task_engine_support import (engine_execution_context, engine_result_outcome,
                                  write_task_outcome, task_outcome_file, file_task_outcome)
from .resource_delivery import POLICY_VERSION
from ..core.input_versions import validating_inputs
from .task_input_validation import validate_task_inputs


def _validate_deliveries(claim, payload):
    from .resource_delivery import validate_delivery_sources, DeliveryDenied
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        try:
            return validate_delivery_sources(db, user, claim.project_id, claim.task_type, payload)
        except DeliveryDenied as exc:
            raise TaskExecutionError("delivery_denied", str(exc)) from exc


def _check_delivery_identity(path, record):
    try:
        unchanged = list(file_identity(path.stat())) == record["identity"]
    except OSError:
        unchanged = False
    if not unchanged:
        raise TaskExecutionError("delivery_changed", "成品在导出期间发生变化，请重新制作或选择成品。")


def _revalidate_deliveries(claim, payload, expected):
    current = _validate_deliveries(claim, payload)
    if any(name not in current or current[name].get("task_id") != record.get("task_id")
           or current[name].get("identity") != record.get("identity") for name, record in expected.items()):
        raise TaskExecutionError("delivery_changed", "成品资格或版本在导出期间已变化，请重新选择。")

def _voice_entry_result(payload: dict, result: dict) -> dict:
    """Single-role records must not repeat the whole book's character list."""
    speakers = payload.get("speakers")
    if isinstance(speakers, list) and len(speakers) == 1:
        return {**result, "speakers": speakers}
    return result


def _run_voices_foundation(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    from ..engines import voices
    result = voices.prepare_foundations(
        handle,
        payload.get("speakers"),
        bool(payload.get("new_only")),
        payload.get("overrides") or {},
        payload.get("script"),
    )
    return _voice_entry_result(payload, result)


def _run_voices_clone(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    from ..engines import voices
    result = voices.generate_voice_candidates(
        handle,
        payload.get("speakers"),
        bool(payload.get("new_only")),
        payload.get("concurrency"),
        payload.get("script"),
        payload.get("candidate_count"),
    )
    return _voice_entry_result(payload, result)


def _run_tts_batch(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    from ..engines import tts_batch
    # Audio segments, cached voice versions and manifests are resume checkpoints,
    # not artifacts that should disappear when the whole batch is interrupted.
    handle.mark_workspace_checkpoint_directory(get_or_prepare_layout().audio_chunk)
    scripts = payload.get("scripts") or []
    if len(scripts) > 1:
        return tts_batch.synthesize_multi(
            handle,
            scripts,
            payload.get("concurrency"),
            payload.get("seed"),
            payload.get("auto_concurrency"),
        )
    return tts_batch.synthesize(
        handle,
        payload.get("indices"),
        scripts[0] if scripts else payload.get("script"),
        payload.get("concurrency"),
        payload.get("seed"),
        payload.get("auto_concurrency"),
    )


def _run_tts_merge(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    from ..engines import merge as merge_engine
    return merge_engine.merge_audio_package(
        handle,
        str(payload.get("package") or ""),
    )


def _run_tts_preview_render(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    from ..engines import tts_batch
    return tts_batch.render_preview(
        handle,
        str(payload.get("script") or ""),
        payload.get("render") or [],
        payload.get("concurrency"),
        payload.get("seed"),
    )


def _run_bgm_segment(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    from ..engines import bgm as bgm_engine
    cfg = core_config.get_config()
    return bgm_engine.analyze_segment_chapter(handle, str(payload["stem"]), cfg.llm, cfg.bgm)


def _run_bgm_mix(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    from ..engines import bgm as bgm_engine
    from .resource_delivery import delivery_records
    cfg = core_config.get_config()
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        relative = f"06_audio_merge/{payload['stem']}.mp3"
        source_complete = relative in delivery_records(db, user, claim.project_id, [relative])
    result = bgm_engine.mix_chapter(handle, str(payload["stem"]), cfg.bgm, cfg.ffmpeg)
    if source_complete:
        with SessionLocal() as db:
            user = db.get(User, claim.owner_id)
            source_complete = relative in delivery_records(db, user, claim.project_id, [relative])
    result["complete"] = source_complete
    return result


def _run_music_suggest_tags(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    from ..engines import music as music_engine
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        if user is None or not user.is_active or user.role != "admin":
            raise TaskExecutionError("permission_revoked", "该任务现在需要管理员权限")
    cfg = core_config.get_config()
    return music_engine.suggest_track_tags(
        handle,
        str(payload["name"]),
        cfg.llm,
        payload.get("description"),
    )


def _run_bgm_match(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    from ..engines import bgm as bgm_engine
    cfg = core_config.get_config()
    stems = [str(value) for value in (payload.get("chapters") or [])]
    mode = str(payload.get("mode") or "random")
    # 升级前提交、升级后执行的在途任务 payload 可能带退役的 "llm" 模式：
    # 执行前归一为 random，防止落入无标签评分分支静默退化、并回写退役 mode 值。
    if mode not in ("random", "segment"):
        mode = "random"
    if mode == "segment":
        return bgm_engine.recompute_segment_timelines(
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
    result = bgm_engine.match_stems(
        get_or_prepare_layout(), stems, mode, cfg.bgm.min_match_score,
        handle=handle,
    )

    if len(stems) == 1 and isinstance(result.get("assignments"), dict):
        assignments = result["assignments"]
        chapters = assignments.get("chapters") or {}
        result = {**result, "assignments": {**assignments, "chapters": {
            stem: chapters[stem] for stem in stems if stem in chapters
        }}}
    return result


def _archive_source(output, source, name, record, claim):
    """Copy a delivery in bounded chunks, with cancellation and open-file checks."""
    def check():
        if cancellation_requested(claim):
            raise TaskCancelledError()
    check()
    _check_delivery_identity(source, record)
    info = zipfile.ZipInfo.from_file(source, arcname=name)
    info.compress_type = zipfile.ZIP_STORED
    with source.open("rb") as reader, output.open(info, "w", force_zip64=True) as writer:
        if list(file_identity(os.fstat(reader.fileno()))) != record["identity"]:
            raise TaskExecutionError("delivery_changed", "成品在导出期间发生变化")
        while chunk := reader.read(1024 * 1024):
            check()
            writer.write(chunk)
        if list(file_identity(os.fstat(reader.fileno()))) != record["identity"]:
            raise TaskExecutionError("delivery_changed", "成品在导出期间发生变化")
    _check_delivery_identity(source, record)
    check()


def _package_check(claim):
    if cancellation_requested(claim):
        raise TaskCancelledError()


def _run_bgm_package(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    deliveries = _validate_deliveries(claim, payload)
    layout = get_or_prepare_layout()
    stems = [str(value) for value in (payload.get("chapters") or [])]
    base = safe_display_name(str(payload.get("base") or "bgm"))
    with task_outcome_file(claim, f"{base}.zip") as archive:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED, allowZip64=True) as output:
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
                record = deliveries[f"08_bgm/{stem}.mp3"]
                _revalidate_deliveries(claim, {**payload, "chapters": [stem]}, {f"08_bgm/{stem}.mp3": record})
                _archive_source(output, resolved_source, f"{base}/{stem}.mp3", record, claim)
                update_progress(claim, int(index * 90 / max(1, len(stems))), f"打包 {index}/{len(stems)}")
        _revalidate_deliveries(claim, payload, deliveries)
        result = file_task_outcome(
            archive, f"{base}.zip", "application/zip",
            {"engine": "bgm.package", "base": base, "file_count": len(stems), "delivery_policy": POLICY_VERSION},
            publish_module="08_bgm", check=lambda: _package_check(claim),
        )
        handle.progress_percent(100, "完成")
    return result


def _run_audio_zip(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    deliveries = _validate_deliveries(claim, payload)
    workspace = get_or_prepare_layout().workspace
    if workspace is None:
        raise TaskExecutionError("workspace_missing", "尚未设置工作空间")
    base = safe_display_name(str(payload.get("base") or "audio"))
    files = payload.get("files") or []
    reserved_names: set[str] = set()
    with task_outcome_file(claim, f"{base}.zip") as archive:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED, allowZip64=True) as output:
            for index, item in enumerate(files, 1):
                if cancellation_requested(claim):
                    raise TaskCancelledError()
                if not isinstance(item, dict):
                    raise TaskExecutionError("invalid_payload", "打包文件参数无效")
                relative = Path(str(item.get("relative_path") or ""))
                source = (workspace / relative).resolve()
                if not source.is_relative_to(workspace.resolve()) or not source.is_file():
                    raise TaskExecutionError("input_missing", "待打包文件不存在")
                name = unique_filename(str(item.get("name") or source.name), reserved_names)
                reserved_names.add(name)
                record = deliveries[relative.as_posix()]
                _revalidate_deliveries(claim, {**payload, "files": [item]}, {relative.as_posix(): record})
                _archive_source(output, source, name, record, claim)
                update_progress(claim, int(index * 90 / max(1, len(files))), f"打包 {index}/{len(files)}")
        # Closing the archive writes its central directory before publication.
        _revalidate_deliveries(claim, payload, deliveries)
        result = file_task_outcome(
            archive, f"{base}.zip", "application/zip",
            {"engine": "audio.zip", "file_count": len(files), "delivery_policy": POLICY_VERSION},
            publish_module="07_output", check=lambda: _package_check(claim),
        )
        handle.progress_percent(100, "完成")
    return result


def _run_audio_export(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    deliveries = _validate_deliveries(claim, payload)
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
        record = deliveries[relative.as_posix()]
        _check_delivery_identity(input_path, record)
        shutil.copy2(input_path, staged)
        try:
            _check_delivery_identity(input_path, record)
        except TaskExecutionError:
            staged.unlink(missing_ok=True)
            raise
        side_effect_outputs.append(TaskSideEffectOutput(staged, target))
        written.append({"name": name, "path": str(target)})
        update_progress(claim, int(index * 90 / max(1, len(files))), f"导出 {index}/{len(files)}")
    _revalidate_deliveries(claim, payload, deliveries)
    return {"engine": "audio.export", "dest_dir": str(destination), "file_count": len(written), "files": written}


def _run_tts_reset(handle, claim: TaskClaim, payload: dict, side_effect_outputs, side_effect_deletes) -> Any:
    from ..engines import tts_batch
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
    return {"engine": "tts.reset", "ok": True, "removed": removed}


# S1：13 个 legacy 引擎分支的显式绑定（注册表按名查表；不用装饰器隐式注册）。
ENGINE_BRANCHES: dict[str, Callable] = {
    "voices.foundation": _run_voices_foundation,
    "voices.clone": _run_voices_clone,
    "tts.batch": _run_tts_batch,
    "tts.merge": _run_tts_merge,
    "tts.preview_render": _run_tts_preview_render,
    "bgm.segment": _run_bgm_segment,
    "bgm.mix": _run_bgm_mix,
    "music.suggest_tags": _run_music_suggest_tags,
    "bgm.match": _run_bgm_match,
    "bgm.package": _run_bgm_package,
    "audio.zip": _run_audio_zip,
    "audio.export": _run_audio_export,
    "tts.reset": _run_tts_reset,
}


def execute_engine_task(claim: TaskClaim) -> TaskOutcome:
    """Run an existing business engine inside the durable Worker boundary."""
    handle = EngineExecutionContext(claim)
    payload = claim.payload
    if claim.task_type in LEGACY_ENGINE_TASK_TYPES:
        validation_error = legacy_task_payload_error(claim.task_type, payload)
        if validation_error:
            raise TaskExecutionError("invalid_payload", validation_error)
    side_effect_outputs: list[TaskSideEffectOutput] = []
    side_effect_deletes: list[Path] = []
    with engine_execution_context(claim):
        try:
            spec = TASK_TYPES.get(claim.task_type)
            if spec is None or not spec.legacy_engine:
                raise TaskExecutionError("unsupported_task_type", f"不支持的任务类型：{claim.task_type}")
            runner = ENGINE_BRANCHES[claim.task_type]
            with validating_inputs(lambda: validate_task_inputs(payload)):
                result = runner(handle, claim, payload, side_effect_outputs, side_effect_deletes)
            if isinstance(result, TaskOutcome):
                return result
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
