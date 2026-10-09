"""HTTP adapters for persistent split-text parsing and batch cancellation.

Both routes submit / cancel through the v1 durable task surface
(``services.task_operations``) — the durable Worker is the only executor.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.config import PromptsConfig, get_config
from ..core.paths import get_or_prepare_layout
from ..engines.script_prompts import load_default_prompts
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context
from ..platform.project_context import active_project
from ..services.script_parse_state import ScriptParseError, submit_run
from .task_operations import cancel_task as cancel_durable_task
from . import _common

router = APIRouter(prefix="/api/script", tags=["script"])


def _resolved_prompts() -> PromptsConfig:
    """The configured prompts, with empty fields filled from the bundled defaults.

    Mirrors the source ``get_config`` behaviour. Returns a copy, so the shared
    in-memory config is never mutated as a side effect of a generate call.
    """
    prompts = get_config().prompts
    system_prompt, user_prompt = load_default_prompts()
    return prompts.model_copy(update={
        "system_prompt": prompts.system_prompt or system_prompt,
        "user_prompt": prompts.user_prompt or user_prompt,
    })


def _resolve_split_file(name: str) -> Path:
    """Resolve a ``02_split_text`` file name to an absolute path, rejecting traversal.

    The frontend sends bare file names (from the text-format chapter state);
    anything that resolves outside the ``02_split_text`` directory is refused.
    """
    base = get_or_prepare_layout().split_text
    if base is None:
        raise HTTPException(409, "尚未设置工作空间——请先在「开始」页选择文件夹。")
    candidate = base / name
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError:
        raise HTTPException(400, f"非法文件路径：{name}")
    if not candidate.is_file():
        raise HTTPException(400, f"文件不存在：{name}")
    return candidate


class ParseChecks(BaseModel):
    """User-selected parse-check switches, submitted WITH the task.

    Each field is optional (``None`` = not selected); only provided fields override
    the task's config snapshot, so a partial selection leaves the other switches at
    their configured values. The submitted values are the per-task authority — they
    win over both the workspace config and admin platform defaults because the merge
    happens AFTER the snapshot is built from ``get_config()``.
    """
    check_chunk_alignment: bool | None = None
    check_boundary_speakers: bool | None = None
    validate_instructs: bool | None = None
    revalidate_splits: bool | None = None
    check_long_paragraphs: bool | None = None
    spot_check_enabled: bool | None = None


class GenerateFilesRequest(BaseModel):
    files: list[str] = Field(max_length=1000)
    # 解析检查开关（用户解析页勾选）：随任务提交、固化进每个任务的配置快照；
    # 缺省 = 沿用当前生效配置（旧客户端兼容）。
    checks: ParseChecks | None = None


@router.post("/generate-files")
def generate_files(
    req: GenerateFilesRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=180)] = None,
) -> dict:
    """Submit one persistent parse task per selected split file.

    The response deliberately keeps the legacy ``{file, task_id}`` shape so the
    page can follow the tasks through the unified v1 task centre while
    execution happens only in the durable Worker.
    """
    _common.require_workspace()
    names = list(dict.fromkeys(req.files))
    if not names:
        raise HTTPException(400, "请选择要解析的文件。")
    project = active_project(db, ctx.user, ctx.session)
    if project is None:
        raise HTTPException(409, "尚未设置工作空间")
    try:
        receipt = submit_run(db, ctx.user, project.id,
            [{"name": name, "sha256": None} for name in names],
            req.checks.model_dump(exclude_none=True) or None if req.checks is not None else None,
            idempotency_key)
    except ScriptParseError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    return {**receipt, "files": [{"file": row["name"], "task_id": row["task_id"]} for row in receipt["files"]]}


class CancelBatchRequest(BaseModel):
    task_ids: list[str]


@router.post("/cancel-batch")
def cancel_batch(
    req: CancelBatchRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Cancel durable parse tasks using the same batch-shaped response."""
    cancelled = []
    for task_id in dict.fromkeys(req.task_ids):
        try:
            result = cancel_durable_task(task_id, user=ctx.user, db=db)
        except HTTPException as exc:
            if exc.status_code == 404:
                continue
            raise
        cancelled.append(result)
    return {"cancelled": cancelled, "batches_stopped": 0}
