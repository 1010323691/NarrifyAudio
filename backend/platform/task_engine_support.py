"""Engine execution helpers shared by the durable Worker and engine adapter."""
from __future__ import annotations

import json
import hashlib
from contextlib import contextmanager
from typing import Any, Callable

from ..core import config as core_config
from ..core.request_context import bind_workspace, reset_workspace
from ..core.script_snapshot import bind_reference, reset_reference
from ..core.input_versions import bind_metadata, reset_metadata
from .database import SessionLocal
from .models import User
from .storage import sha256_file, task_attempt_path, project_workspace_path
from .task_contracts import TaskClaim, TaskExecutionError, TaskFileOutcome, TaskOutcome


@contextmanager
def task_outcome_file(claim: TaskClaim, output_name: str):
    """Reserve an attempt-owned disk artifact; remove incomplete writes on error."""
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        if user is None:
            raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
        path = task_attempt_path(db, user.username, claim.project_id,
                                 claim.task_id, claim.attempt_id, output_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def file_task_outcome(path, output_name: str, content_type: str,
                      metadata: dict[str, Any], *, publish_module=None,
                      check: Callable[[], None] | None = None) -> TaskOutcome:
    """Describe a closed attempt artifact without materializing its contents."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            if check:
                check()
            size += len(chunk)
            digest.update(chunk)
    if check:
        check()
    return TaskOutcome(temp_path=path, output_name=output_name,
                       content_type=content_type, size_bytes=size,
                       sha256=digest.hexdigest(), metadata=metadata,
                       publish_module=publish_module)


def write_task_outcome(
    claim: TaskClaim,
    output_name: str,
    content_type: str,
    data: bytes,
    metadata: dict[str, Any],
    *,
    publish_module: str | None = None,
    additional_outputs: list[tuple[str, str, bytes]] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> TaskOutcome:
    created_paths = []
    try:
        with SessionLocal() as db:
            user = db.get(User, claim.owner_id)
            if user is None:
                raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
            temp_path = task_attempt_path(db, user.username, claim.project_id, claim.task_id, claim.attempt_id, output_name)
            extra_paths = [
                task_attempt_path(db, user.username, claim.project_id, claim.task_id,
                                  claim.attempt_id, f"{index:05d}-{name}")
                for index, (name, _, _) in enumerate(additional_outputs or [], 1)
            ]
        # File writes/hashes and progress callbacks must not pin a connection.
        # A callback uses its own short transaction; nesting it here can exhaust
        # every slot when several Worker threads publish at the same time.
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        created_paths.append(temp_path)
        temp_path.write_bytes(data)
        output_count = 1 + len(extra_paths)
        if on_progress:
            on_progress(1 / output_count)
        extra: list[TaskFileOutcome] = []
        for index, ((extra_name, extra_type, extra_data), extra_path) in enumerate(
            zip(additional_outputs or [], extra_paths), 1,
        ):
            extra_path.parent.mkdir(parents=True, exist_ok=True)
            created_paths.append(extra_path)
            extra_path.write_bytes(extra_data)
            if on_progress:
                on_progress((index + 1) / output_count)
            extra.append(TaskFileOutcome(
                temp_path=extra_path, output_name=extra_name,
                content_type=extra_type, size_bytes=len(extra_data),
                sha256=sha256_file(extra_path), publish_module=publish_module,
            ))
        return TaskOutcome(
            temp_path=temp_path,
            output_name=output_name,
            content_type=content_type,
            size_bytes=len(data),
            sha256=sha256_file(temp_path),
            metadata=metadata,
            publish_module=publish_module,
            additional_outputs=tuple(extra),
        )
    except BaseException:
        for path in reversed(created_paths):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        for parent in {path.parent for path in created_paths}:
            try:
                parent.rmdir()
            except OSError:
                pass
        raise





@contextmanager
def engine_execution_context(claim: TaskClaim):
    """Bind the managed workspace and immutable config snapshot for one engine call.

    Several mature engines still obtain their settings through ``get_config``.
    Keeping the snapshot in the task payload makes a retry deterministic without
    changing every engine signature at once. ContextVar bindings isolate each
    concurrent task's workspace and config and are reset in the ``finally`` block.
    """
    with SessionLocal() as db:
        user = db.get(User, claim.owner_id)
        if user is None:
            raise TaskExecutionError("owner_not_found", "任务所属用户不存在")
        workspace = project_workspace_path(db, user.username, claim.project_id)
    snapshot = task_config_snapshot(claim.payload, claim.owner_id, claim.project_id)
    config_token = None
    if isinstance(snapshot, dict):
        config_token = core_config.bind_task_config(core_config.AppConfig.model_validate(snapshot))
    token = bind_workspace(workspace)
    reference_token = bind_reference(snapshot.get("_script_inputs") if isinstance(snapshot, dict) else None)
    metadata_token = bind_metadata(snapshot if isinstance(snapshot, dict) else {})
    try:
        yield workspace
    finally:
        reset_metadata(metadata_token)
        reset_reference(reference_token)
        reset_workspace(token)
        if config_token is not None:
            core_config.reset_task_config(config_token)


def task_config_snapshot(payload, owner_id, project_id, *, db=None):
    snapshot = payload.get("config")
    if isinstance(snapshot, dict) or not payload.get("_batch_config_id"):
        return snapshot
    from .models import TaskBatch
    if db is None:
        with SessionLocal() as session:
            return task_config_snapshot(payload, owner_id, project_id, db=session)
    batch = db.get(TaskBatch, payload["_batch_config_id"])
    if batch is None or batch.owner_id != owner_id or batch.project_id != project_id:
        raise TaskExecutionError("batch_not_found", "提交配置不存在")
    return batch.config


def engine_result_outcome(claim: TaskClaim, result: Any) -> TaskOutcome:
    """Persist an engine's JSON result as a durable task result artifact."""
    if isinstance(result, dict):
        metadata = dict(result)
    else:
        metadata = {"value": result}
    metadata["engine"] = claim.task_type
    data = json.dumps(metadata, ensure_ascii=False, default=str).encode("utf-8")
    output_name = f"{claim.task_type.replace('.', '_')}_{claim.attempt_id}.json"
    return write_task_outcome(
        claim,
        output_name,
        "application/json",
        data,
        metadata,
    )
