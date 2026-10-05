from __future__ import annotations

import json
import logging
import mimetypes
import os
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

import redis
from sqlalchemy import func, or_, select

from ..core.config import TextConfig
from ..core import config as core_config
from ..core.file_lock import exclusive_file_lock
from ..core.paths import WORKSPACE_DIRS
from ..core.request_context import bind_workspace, reset_workspace
from ..core.task_control import TaskCancelled
from ..engines.book import (
    DEFAULT_LENGTH_TARGET_CHARS,
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
    split_by_length,
)
from ..engines.text import format_text
from ..engines import script as script_engine
from ..engines import audio as audio_engine
from .platform_settings import settings
from .artifact_publication import PublicationJournal, publication_transaction
from .database import SessionLocal
from .models import (
    OutboxEvent,
    Project,
    ProjectFile,
    Task,
    TaskAttempt,
    TaskEvent,
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
    project_workspace_path,
    lock_storage_migration,
    storage_migration,
)
from .task_registry import TASK_TYPES
from .resource_tasks import _execute_resource_scan, _execute_resource_package, _execute_resource_cleanup
from .resource_inventory import ResourceError
from .task_context import (
    EngineExecutionContext,
    PersistentTaskHandle,
    _as_utc,
    _attempt_is_current,
    cancellation_requested,
    update_progress,
)
from .task_engine_support import write_task_outcome
from .task_contracts import (
    TaskCancelledError,
    TaskClaim,
    TaskExecutionError,
    TaskFileOutcome,
    TaskOutcome,
)
from .task_lifecycle import (
    TERMINAL_TASK_STATUSES,
    append_task_event,
    suppress_pending_dispatch,
)



_write_outcome = write_task_outcome

WORKER_GROUP = os.getenv("NARRIFY_TASK_GROUP", "narrify-workers")
from .gpu_scheduler.admission import claim_allowed, allowed_task_types, bind_claim, reset_claim
from .gpu_scheduler.store import guarded_claim
WORKSPACE_MUTATING_TASK_TYPES = {
    "voices.foundation", "voices.clone", "tts.batch", "tts.merge", "tts.reset",
    "bgm.segment", "bgm.mix", "bgm.match", "audio.export",
}


def ensure_consumer_group(client: redis.Redis) -> None:
    try:
        client.xgroup_create(STREAM_NAME, WORKER_GROUP, id="0-0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


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
        workspace = project_workspace_path(db, user.username, claim.project_id)
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
    checkpoint_directory = (
        project_workspace_path(db, user.username, task.project_id) / "05_audio_chunk"
        if task.task_type == "tts.batch" else None
    )
    PublicationJournal.reconcile(root, path, committed=task.status == "succeeded",
                                 checkpoint_directory=checkpoint_directory)
    from ..core import paths as core_paths

    shared_root = Path(core_paths.MUSIC_LIBRARY_DIR).resolve()
    shared_path = shared_root / ".tasks" / task.id / attempt.id / "publication.json"
    PublicationJournal.reconcile(shared_root, shared_path, committed=task.status == "succeeded")


@guarded_claim
def claim_task(
    task_id: str,
    worker_id: str,
    *,
    lease_seconds: int | None = None,
    excluded_task_types: tuple[str, ...] = (),
) -> TaskClaim | None:
    """Atomically create one fenced attempt for a submitted task."""
    lease_seconds = lease_seconds or settings.task_lease_seconds
    now = utcnow()
    with SessionLocal() as db:
        if not lock_storage_migration(db, shared=True) or storage_migration(db) is not None:
            db.rollback()
            return None
        task_stmt = select(Task).where(Task.id == task_id)
        if excluded_task_types:
            task_stmt = task_stmt.where(Task.task_type.not_in(excluded_task_types))
        task = db.scalar(task_stmt.with_for_update())
        if task is None or task.status in TERMINAL_TASK_STATUSES or task.status == "paused":
            return None
        if not claim_allowed(task.task_type, db):
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
            suppress_pending_dispatch(db, task.id)
            append_task_event(db, task.id, "cancelled", {"reason": "cancel requested"})
            db.commit()
            return None

        latest_attempt = db.scalar(
            select(func.max(TaskAttempt.attempt_no)).where(TaskAttempt.task_id == task.id)
        ) or 0
        if latest_attempt >= settings.task_max_attempts and task.error_code != "llm_unavailable":
            task.status = "failed"
            task.error_code = "max_attempts"
            task.error_message = "任务超过最大尝试次数"
            task.finished_at = now
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
    excluded_task_types: tuple[str, ...] = (),
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
        eligible_tasks = eligible_tasks & Task.task_type.in_(allowed_task_types(db))
        if task_types:
            eligible_tasks = eligible_tasks & Task.task_type.in_(task_types)
        if excluded_task_types:
            eligible_tasks = eligible_tasks & Task.task_type.not_in(excluded_task_types)
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
        if excluded_task_types:
            user_tasks = user_tasks & Task.task_type.not_in(excluded_task_types)
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


def _book_analysis_result(text: str, encoding: str, source_name: str, *, on_progress: Callable[[float], None] | None = None) -> dict[str, Any]:
    raw = analyze_text(text, on_progress=(lambda value: on_progress(value * 0.7)) if on_progress else None)
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
    if on_progress:
        on_progress(1.0)
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
            f"可「按字数分册」（约 {_length_target_from_payload({})} 字/册、字数平均、不切段落、不截断句子）"
            "、「不处理，按整本继续」（整本输出为单个文件），或重新上传原文。"
        )
    return result


def _execute_script_parse(claim: TaskClaim) -> TaskOutcome:
    from ..core.config import GenerationConfig, LLMConfig, PromptsConfig

    with SessionLocal() as db:
        user, _project, item, source_path = _input_file(db, claim)
        workspace = project_workspace_path(db, user.username, claim.project_id)
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
    handle = EngineExecutionContext(claim)
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
    # 输入内容指纹：解析页据此判断「同名文件被重新分册覆盖后，旧解析结果已过期」。
    # 取引擎实际读到的字节（source_path），而不是 DB 行快照。
    metadata["source_sha256"] = sha256_file(source_path)
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
        workspace = project_workspace_path(db, user.username, claim.project_id)
    handle = EngineExecutionContext(claim)
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
    return write_task_outcome(
        claim,
        output_name,
        "application/json",
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        {"engine": "audio.silences", "source_file_id": item.id, **payload},
    )


def _execute_audio_cut(claim: TaskClaim) -> TaskOutcome:
    from ..core.safe_filesystem import file_identity, safe_regular_path
    from .resource_delivery import delivery_records

    with SessionLocal() as db:
        user, _project, item, source_path = _input_file(db, claim)
        output_dir = task_attempt_path(
            db, user.username, claim.project_id, claim.task_id, claim.attempt_id, "cut"
        )
        workspace = project_workspace_path(db, user.username, claim.project_id)
        source_relative = source_path.relative_to(workspace).as_posix()
        source_path = safe_regular_path(workspace, source_relative)
        source_identity = file_identity(source_path.stat())
        # Uploaded audio is a valid standalone input. Production intermediates
        # inherit their source's delivery eligibility rather than gaining it by cutting.
        source_complete = source_relative.startswith("01_input/") or source_relative in delivery_records(db, user, claim.project_id)
    handle = EngineExecutionContext(claim)
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
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        source_complete = source_complete and file_identity(safe_regular_path(workspace, source_relative).stat()) == source_identity
        if not source_relative.startswith("01_input/"):
            source_complete = source_complete and source_relative in delivery_records(db, user, claim.project_id)
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
            "complete": source_complete,
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
    return write_task_outcome(
        claim,
        f"load-simulation-{claim.task_id}.json",
        "application/json",
        json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
        metadata,
    )


def _prepare_text_claim(claim: TaskClaim) -> tuple[ProjectFile, str, str, Path]:
    """Shared preamble for the text-based platform tasks: load + decode the input file."""
    with SessionLocal() as db:
        _, _, item, source_path = _input_file(db, claim)
        if cancellation_requested(claim):
            raise TaskCancelledError()
        update_progress(claim, 10, "读取输入")
        source_text, encoding = decode_buffer(source_path.read_bytes())
    return item, source_text, encoding, source_path


def _text_progress_reporter(claim: TaskClaim, start: int, end: int, label: str) -> Callable[[float], None]:
    """Report actual work without writing one database event per input line."""
    last_value = start - 1
    last_time = 0.0

    def report(fraction: float) -> None:
        nonlocal last_value, last_time
        value = start + int(max(0.0, min(1.0, fraction)) * (end - start))
        now = time.monotonic()
        if value <= last_value or (value != end and value - last_value < 5 and now - last_time < 0.5):
            return
        if cancellation_requested(claim):
            raise TaskCancelledError()
        update_progress(claim, value, label)
        last_value, last_time = value, now

    return report


def _execute_text_format(claim: TaskClaim) -> TaskOutcome:
    item, source_text, _encoding, _source_path = _prepare_text_claim(claim)
    config = TextConfig.model_validate(claim.payload.get("config") or {})
    result = format_text(source_text, config, on_progress=_text_progress_reporter(claim, 10, 75, "排版文本"))
    update_progress(claim, 75, "完成排版")
    output_name = str(claim.payload.get("output_name") or f"{Path(item.original_name).stem}_formatted.txt")
    return write_task_outcome(
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
        on_progress=_text_progress_reporter(claim, 75, 95, "写入排版结果"),
    )


def _execute_book_analyze(claim: TaskClaim) -> TaskOutcome:
    item, source_text, encoding, source_path = _prepare_text_claim(claim)
    analysis = _book_analysis_result(source_text, encoding, str(source_path), on_progress=_text_progress_reporter(claim, 10, 75, "分析章节"))
    update_progress(claim, 75, "完成章节分析")
    output_name = str(claim.payload.get("output_name") or f"{Path(item.original_name).stem}_analysis.json")
    return write_task_outcome(
        claim,
        output_name,
        "application/json",
        json.dumps(analysis, ensure_ascii=False, indent=2).encode("utf-8"),
        {"engine": "book.analyze", "source_file_id": item.id, "analysis": analysis},
        publish_module=str(claim.payload.get("publish_module") or "") or None,
        on_progress=_text_progress_reporter(claim, 75, 95, "写入章节分析"),
    )


def _length_target_from_payload(payload: dict[str, Any]) -> int:
    """Effective by-length target for the by_length branch: an explicit valid
    ``payload["length_target"]`` overrides the admin-configured platform
    default (``split.length_target``); otherwise that configured value (3000
    when unset/invalid)."""
    raw = payload.get("length_target")
    if not isinstance(raw, bool):
        try:
            target = int(raw)
        except (TypeError, ValueError):
            pass
        else:
            if target > 0:
                return target
    try:
        target = core_config.get_config().split.length_target
    except Exception:  # noqa: BLE001 — a config read failure falls back to the code default
        target = 0
    return target if target > 0 else DEFAULT_LENGTH_TARGET_CHARS


def _execute_book_split(claim: TaskClaim) -> TaskOutcome:
    item, source_text, _encoding, _source_path = _prepare_text_claim(claim)
    raw = analyze_text(source_text, on_progress=_text_progress_reporter(claim, 10, 40, "识别分册章节"))
    chapters = raw["chapters"]
    base = str(claim.payload.get("base") or base_name(item.original_name)).strip() or base_name(item.original_name)
    outputs: list[tuple[str, str, bytes]] = []
    final_chapters: list[dict[str, Any]] = []
    repair_status: str | None = None
    repair_report: dict[str, Any] | None = None
    baseline_chars: int | None = None
    length_target: int | None = None
    split_policy: dict | None = None
    if bool(claim.payload.get("whole_book")):
        name = make_whole_book_filename(base)
        outputs.append((name, "text/plain; charset=utf-8", source_text.encode("utf-8")))
    elif bool(claim.payload.get("by_length")):
        # 零章节兜底：未识别出章节结构时按目标字数平均分册，切点只落
        # 段落/句子边界（不切段落、不截断句子）。
        length_target = _length_target_from_payload(claim.payload)
        result = split_by_length(source_text, length_target)
        if result["status"] == "error":
            raise TaskExecutionError("invalid_structure", result.get("error") or "按字数拆分失败")
        segments = result["segments"]
        names = make_smart_filenames([{"final_num": s["seq"], "title": ""} for s in segments])
        final_chapters = [
            {
                "seq": s["seq"],
                "num": s["seq"],
                "numStr": str(s["seq"]),
                "title": "",
                "chars": s["chars"],
                "orig_num": None,
                "orig_numStr": "",
                "final_num": s["seq"],
                "actions": ["length_split"],
                "reasons": ["length_split"],
                "confidence": "high",
            }
            for s in segments
        ]
        outputs.extend(
            (name, "text/plain; charset=utf-8", source_text[s["start"]:s["end"]].encode("utf-8"))
            for name, s in zip(names, segments)
        )
        repair_report = {
            "actions": [
                {
                    "seq": s["seq"],
                    "orig_num": None,
                    "orig_numStr": "",
                    "orig_title": "",
                    "final_num": s["seq"],
                    "actions": ["length_split"],
                    "confidence": "high",
                }
                for s in segments
            ],
            "warnings": result["warnings"],
            "removed": [],
        }
    else:
        if not bool(claim.payload.get("smart")):
            raise TaskExecutionError("invalid_payload", "分册任务必须启用 smart、by_length 或 whole_book")
        if not chapters:
            raise TaskExecutionError("no_chapters", "未检测到章节，无法分册")
        policy = claim.payload.get("split_policy") or {}
        repair = smart_repair(
            source_text, chapters,
            split_long_chapters=policy.get("smart_split_long_chapters") is True,
            length_target=policy.get("length_target", DEFAULT_LENGTH_TARGET_CHARS),
        )
        if repair["status"] == "error":
            raise TaskExecutionError("invalid_structure", repair.get("error") or "章节结构修复失败")
        repair_status = repair["status"]
        repair_report = repair["report"]
        baseline_chars = repair.get("baseline_chars")
        split_policy = repair.get("split_policy")
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
                "reasons": chapter.get("repair", {}).get("reasons") or chapter.get("repair", {}).get("actions", ["kept"]),
                "confidence": chapter.get("repair", {}).get("confidence", "high"),
                **({"source_chapter_id": chapter["source_chapter_id"]} if "source_chapter_id" in chapter else {}),
                **({"long_split": chapter["long_split"]} if "long_split" in chapter else {}),
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
    return write_task_outcome(
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
            "length_target": length_target,
            "split_policy": split_policy,
            "expected_format": EXPECTED_CHAPTER_FORMAT,
            "files": [{"name": name, "chars": len(data.decode("utf-8").replace("\n", "").replace("\r", ""))} for name, _, data in outputs],
        },
        publish_module="02_split_text",
        additional_outputs=outputs[1:],
        on_progress=_text_progress_reporter(claim, 75, 95, "写入分册文件"),
    )


# S1：6 个平台直连执行器的显式绑定（注册表按名查表；不用装饰器隐式注册）。
DIRECT_EXECUTORS: dict[str, Callable[[TaskClaim], TaskOutcome]] = {
    "text.format": _execute_text_format,
    "book.analyze": _execute_book_analyze,
    "book.split": _execute_book_split,
    "script.parse": _execute_script_parse,
    "audio.silences": _execute_audio_silences,
    "audio.cut": _execute_audio_cut,
    "resources.scan": _execute_resource_scan,
    "resources.package": _execute_resource_package,
    "resources.cleanup": _execute_resource_cleanup,
}


def execute_claim(claim: TaskClaim) -> TaskOutcome:
    """Execute one real deterministic engine behind the durable worker boundary."""
    if claim.payload.get("_load_simulation") is True:
        return _execute_load_simulation(claim)
    spec = TASK_TYPES.get(claim.task_type)
    if spec is None:
        raise TaskExecutionError("unsupported_task_type", f"不支持的任务类型：{claim.task_type}")
    if spec.legacy_engine:
        from .engine_task_executor import execute_engine_task

        return execute_engine_task(claim)
    try:
        return DIRECT_EXECUTORS[claim.task_type](claim)
    except ResourceError as exc:
        raise TaskExecutionError("resource_changed", str(exc)) from exc


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


def complete_claim(claim: TaskClaim, outcome: TaskOutcome) -> bool | None:
    with SessionLocal() as db:
        task, attempt = _attempt_is_current(db, claim)
        if task is not None and attempt is not None and task.status == "paused":
            db.rollback()
            return None
        if task is None or attempt is None or task.status == "cancelling":
            db.rollback()
            _cleanup_outcome(outcome)
            return False
        user = db.get(User, task.owner_id)
        if user is None:
            db.rollback()
            _cleanup_outcome(outcome)
            raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
        output_id = None
        outputs = [] if outcome.result_only else [
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
            raise TaskExecutionError("invalid_publish_module", "任务产物目录配置无效")
        legacy_module_paths: list[tuple[str, Path]] = []
        stale_book_paths: list[Path] = []
        module_outputs = {item.publish_module for item in outputs if item.publish_module}
        for module in module_outputs:
            module_prefix = f"{safe_display_name(user.username)}/{task.project_id}/{module}/"
            for existing in db.scalars(
                select(ProjectFile).where(
                    ProjectFile.owner_id == task.owner_id,
                    ProjectFile.project_id == task.project_id,
                    ProjectFile.kind == "artifact",
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
            result_payload = dict(outcome.metadata) if outcome.result_only else {
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
            from .resource_delivery import capture_deliveries
            if task.task_type in {"tts.merge", "bgm.mix", "audio.cut", "audio.export", "audio.zip", "bgm.package"}:
                result_payload["deliveries"] = capture_deliveries(
                    db, user, task.project_id, task.task_type, result_payload,
                    [str(item.final_path) for item in outcome.side_effect_outputs],
                )
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
        if outcome.result_only:
            outcome.temp_path.unlink(missing_ok=True)
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
            append_task_event(db, task.id, "cancelled", {"attempt_id": attempt.id})
            db.commit()
            return "cancelled"
        if error.code == "llm_unavailable":
            task.status = "paused"
            task.finished_at = None
            task.next_attempt_at = None
            append_task_event(db, task.id, "llm_unavailable", {
                "attempt_id": attempt.id,
                "message": "LLM 服务暂不可用，任务已暂停；系统每分钟检查并在服务恢复后自动重试。",
            })
            db.commit()
            return "paused"
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
        append_task_event(db, task.id, "failed", {"attempt_id": attempt.id, "code": error.code})
        db.commit()
        return "failed"


def _run_claim_fenced(claim: TaskClaim) -> str:
    from .quota import reset_quota_context, set_quota_context
    from .worker_registry import heartbeat as worker_heartbeat

    quota_token = set_quota_context(claim.owner_id, claim.task_id, claim.attempt_id)
    gpu_token = bind_claim(claim)
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
            try:
                if not heartbeat_claim(claim):
                    return
            except Exception:
                # A transient DB hiccup must not kill the lease-renewal thread: a
                # dead heartbeat is exactly how a live task loses its lease and gets
                # re-claimed (progress reset to zero) mid-execution.
                continue
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
                while True:
                    completed = complete_claim(claim, outcome)
                    if completed is not None:
                        break
                    EngineExecutionContext(claim).check()
            except Exception:
                _cleanup_outcome(outcome)
                raise
            if completed:
                return "succeeded"
            fail_claim(claim, TaskCancelledError())
            return "cancelled"
    except TaskCancelled:
        fail_claim(claim, TaskCancelledError())
        return "cancelled"
    except TaskExecutionError as exc:
        fail_claim(claim, exc)
        return exc.code
    except Exception as exc:
        from ..engines.llm_transport import LLMHTTPError, LLMUnavailableError
        if isinstance(exc, LLMUnavailableError):
            fail_claim(claim, TaskExecutionError("llm_unavailable", str(exc)))
            return "paused"
        if isinstance(exc, LLMHTTPError) and exc.status in {400, 401, 403, 422}:
            fail_claim(claim, TaskExecutionError("llm_configuration_error", str(exc)))
            return "llm_configuration_error"
        if isinstance(exc, LLMHTTPError) and exc.status == 404:
            # model_not_found usually means the configured model is not loaded yet
            # (server restarted mid-batch, model still loading): wait for the
            # recovery probe to confirm the model is back, instead of parking the
            # task as misconfigured.
            fail_claim(claim, TaskExecutionError("llm_unavailable", str(exc)))
            return "paused"
        from .quota import QuotaInsufficientError
        if isinstance(exc, QuotaInsufficientError):
            fail_claim(claim, TaskExecutionError("quota_insufficient", str(exc)))
            return "quota_insufficient"
        logging.getLogger("audiobook.worker").exception("Task execution failed task=%s type=%s", claim.task_id, claim.task_type)
        fail_claim(claim, TaskExecutionError("worker_error", str(exc), retryable=True))
        return "worker_error"
    finally:
        stop.set()
        heartbeat.join(timeout=2)
        report_worker("idle", None)
        reset_quota_context(quota_token)
        reset_claim(gpu_token)


def process_task_message(
    message: dict[str, Any],
    *,
    worker_id: str,
    excluded_task_types: tuple[str, ...] = (),
) -> str:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else message
    task_id = str(payload.get("task_id", ""))
    if not task_id:
        return "invalid"
    claim = claim_task(task_id, worker_id, excluded_task_types=excluded_task_types)
    if claim is None:
        return "skipped"
    return _run_claim_fenced(claim)


def _decode_stream_event(fields: dict[str, Any]) -> dict[str, Any]:
    raw = fields.get("event", fields.get(b"event"))
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw if isinstance(raw, dict) else {}


def process_stream_entry(
    client: redis.Redis,
    entry_id: str,
    fields: dict[str, Any],
    *,
    worker_id: str,
    excluded_task_types: tuple[str, ...] = (),
) -> str:
    result = process_task_message(
        _decode_stream_event(fields), worker_id=worker_id, excluded_task_types=excluded_task_types,
    )
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
                suppress_pending_dispatch(db, task.id)
                append_task_event(db, task.id, "cancelled", {"reason": "worker lease expired"})
                continue
            if attempt is not None and attempt.status == "expired":
                if attempt.attempt_no >= settings.task_max_attempts:
                    task.status = "failed"
                    task.error_code = "lease_expired"
                    task.error_message = "Worker 租约过期且已达到最大尝试次数"
                    task.finished_at = now
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


# 恢复探测通过后重派仍连续失败（如端点 /models 与 chat 矛盾）时的终止阈值：
# 每个循环至少含一次失败尝试 + 一个 60s 探针周期，30 ≈ 30 分钟，足够覆盖任何
# 合法的模型加载窗口，又能终止病态端点上的无限 pause/resume 循环。
LLM_RECOVERY_MAX_CYCLES = 30


def _resume_llm_config(row: Any, workspace_llm: dict | None) -> dict | None:
    """暂停任务重派后实际执行的 LLM 配置：优先取提交时写进 payload 的配置
    快照（script.parse 执行器回放的就是该快照），缺省回退活动工作区配置。

    若按活动配置探活，用户暂停期间改 model_name（最自然的修复动作）后探针会
    通过，但任务仍按旧快照模型重派 → 再次 404 → 每 60s 无限循环。
    """
    payload = row.payload
    if isinstance(payload, dict):
        snapshot = payload.get("config")
        if isinstance(snapshot, dict):
            llm = snapshot.get("llm")
            if isinstance(llm, dict) and str(llm.get("base_url") or "").strip():
                return llm
    return workspace_llm


def resume_llm_unavailable_tasks(limit: int = 500) -> int:
    """Probe paused LLM tasks and redispatch them once their configured endpoint responds."""
    from ..engines.llm_transport import llm_server_is_alive
    from .gpu_scheduler.config import load_config, platform_llm
    from .gpu_scheduler.store import read_state
    managed_gpu = load_config().enabled
    if managed_gpu and read_state()["state"] != "LLM_ACTIVE":
        return 0

    with SessionLocal() as db:
        rows = db.execute(
            select(Task.id, Task.task_type, Task.owner_id, Task.project_id, Task.payload)
            .where(Task.status == "paused", Task.error_code == "llm_unavailable")
            .order_by(Task.updated_at.asc())
            .limit(limit)
        ).all()
        owner_ids = {row.owner_id for row in rows}
        users = {user.id: user for user in db.scalars(select(User).where(User.id.in_(owner_ids))).all()}
        workspaces = {}
        for row in rows:
            user = users.get(row.owner_id)
            if user is None:
                continue
            key = (row.owner_id, row.project_id)
            if key not in workspaces:
                try:
                    workspaces[key] = project_workspace_path(db, user.username, row.project_id)
                except Exception:
                    logging.getLogger("audiobook.worker").exception(
                        "Unable to resolve paused task workspace owner_id=%s project_id=%s",
                        row.owner_id, row.project_id,
                    )

    configs = {}
    for key, workspace in workspaces.items():
        token = bind_workspace(workspace)
        try:
            configs[key] = core_config.get_config().llm.model_dump(mode="json")
        except Exception:
            logging.getLogger("audiobook.worker").exception(
                "Unable to read paused task LLM config owner_id=%s project_id=%s", *key,
            )
        finally:
            reset_workspace(token)

    candidates: list[tuple[str, str, str, str]] = []
    for row in rows:
        llm_config = platform_llm().model_dump() if managed_gpu else _resume_llm_config(row, configs.get((row.owner_id, row.project_id)))
        if not llm_config:
            continue
        base_url = str(llm_config.get("base_url") or "").strip()
        if base_url:
            model = str(llm_config.get("model_name") or "").strip()
            candidates.append((str(row.id), base_url, str(llm_config.get("api_key") or ""), model))

    liveness: dict[tuple[str, str, str], bool] = {}
    for _task_id, base_url, api_key, model in candidates:
        key = (base_url, api_key, model)
        if key not in liveness:
            liveness[key] = llm_server_is_alive(base_url, api_key, model_name=model or None)

    resumed = 0
    for task_id, base_url, api_key, model in candidates:
        if not liveness.get((base_url, api_key, model), False):
            continue
        now = utcnow()
        with SessionLocal() as db:
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None or task.status != "paused" or task.error_code != "llm_unavailable":
                db.rollback()
                continue
            cycles = db.scalar(
                select(func.count(TaskEvent.id))
                .where(
                    TaskEvent.task_id == task.id,
                    TaskEvent.event_type == "llm_unavailable",
                )
            ) or 0
            if cycles >= LLM_RECOVERY_MAX_CYCLES:
                # 端点自报健康但任务重派后仍持续失败（病态 /models 与 chat 矛盾）：
                # 终态化以终止无限 pause/resume 循环，用户重新提交即可恢复。
                task.status = "failed"
                task.error_message = (
                    f"LLM 恢复探测通过后任务仍失败（累计 {cycles} 次），已停止自动重试；"
                    "请检查 LLM 配置与服务状态后重新提交。"
                )
                task.finished_at = now
                task.updated_at = now
                append_task_event(db, task.id, "llm_recovery_exhausted", {"cycles": cycles})
                db.commit()
                logging.getLogger("audiobook.worker").warning(
                    "LLM recovery exhausted for task %s after %d cycles; task failed",
                    task.id, cycles,
                )
                continue
            pending_event = db.scalar(
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.aggregate_type == "task",
                    OutboxEvent.aggregate_id == task.id,
                    OutboxEvent.published_at.is_(None),
                )
                .limit(1)
            )
            if pending_event is None:
                latest_attempt = db.scalar(
                    select(func.max(TaskAttempt.attempt_no)).where(TaskAttempt.task_id == task.id)
                ) or 0
                event_id = str(uuid5(NAMESPACE_URL, f"llm-resume:{task.id}:{latest_attempt}"))
                if db.get(OutboxEvent, event_id) is None:
                    db.add(OutboxEvent(
                        id=event_id,
                        aggregate_type="task",
                        aggregate_id=task.id,
                        event_type="task.llm_resumed",
                        payload={"task_id": task.id, "task_type": task.task_type, "project_id": task.project_id},
                        available_at=now,
                    ))
            task.status = "retrying"
            task.next_attempt_at = now
            task.finished_at = None
            task.error_message = "LLM 服务已恢复，任务重新排队"
            task.updated_at = now
            append_task_event(db, task.id, "retry_scheduled", {
                "reason": "LLM service recovered",
                "message": task.error_message,
            })
            db.commit()
            resumed += 1
    return resumed


def run_once(
    client: redis.Redis,
    *,
    worker_id: str,
    block_ms: int = 1000,
    excluded_task_types: tuple[str, ...] = (),
) -> str:
    ensure_consumer_group(client)
    fair_claim = claim_fair_task(worker_id, excluded_task_types=excluded_task_types)
    if fair_claim is not None:
        return _run_claim_fenced(fair_claim)
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
            return process_stream_entry(
                client, entry_id, fields, worker_id=worker_id,
                excluded_task_types=excluded_task_types,
            )
    rows = client.xreadgroup(WORKER_GROUP, worker_id, {STREAM_NAME: ">"}, count=1, block=block_ms)
    if not rows:
        return "idle"
    _, entries = rows[0]
    entry_id, fields = entries[0]
    return process_stream_entry(
        client, entry_id, fields, worker_id=worker_id,
        excluded_task_types=excluded_task_types,
    )
