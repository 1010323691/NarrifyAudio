"""Engine execution helpers shared by the durable Worker and engine adapter."""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

from ..core import config as core_config
from ..core.request_context import bind_workspace, reset_workspace
from .database import SessionLocal
from .models import User
from .storage import safe_display_name, sha256_file, task_attempt_path, project_workspace_path
from .task_contracts import TaskClaim, TaskExecutionError, TaskFileOutcome, TaskOutcome


def write_task_outcome(
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


_write_outcome = write_task_outcome



@contextmanager
def engine_execution_context(claim: TaskClaim):
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
        workspace = project_workspace_path(db, user.username, claim.project_id)
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




# Compatibility aliases for existing worker integrations.
legacy_engine_context = engine_execution_context
legacy_result_outcome = engine_result_outcome
