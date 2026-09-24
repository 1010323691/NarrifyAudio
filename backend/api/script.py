"""HTTP adapters for persistent split-text parsing and batch cancellation."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import PromptsConfig, get_config
from ..core.paths import get_layout
from ..engines.script_prompts import load_default_prompts
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context
from ..platform.legacy_files import catalog_managed_file
from .platform_tasks import TaskSubmit, cancel_task as cancel_durable_task, submit_task
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

    The frontend sends bare file names (from ``GET /api/files/list/02_split_text``);
    anything that resolves outside the ``02_split_text`` directory is refused.
    """
    base = get_layout().split_text
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


class GenerateFilesRequest(BaseModel):
    files: list[str]  # 02_split_text 下的文件名（不含路径）


@router.post("/generate-files")
def generate_files(
    req: GenerateFilesRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Submit one persistent parse task per selected split file."""
    return generate_files_durable(req, ctx=ctx, db=db)


@router.post("/generate-files-durable")
def generate_files_durable(
    req: GenerateFilesRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Submit one PostgreSQL-backed parse task per selected split file.

    The response deliberately keeps the legacy ``{file, task_id}`` shape so the
    existing page can use the unified task centre while execution happens only
    in the durable Worker.
    """
    _common.require_workspace()
    names = list(dict.fromkeys(req.files))
    if not names:
        raise HTTPException(400, "请选择要解析的文件。")
    cfg = get_config()
    prompts = _resolved_prompts()
    snapshot = {
        "llm": cfg.llm.model_dump(mode="json"),
        "prompts": prompts.model_dump(mode="json"),
        "generation": cfg.generation.model_dump(mode="json"),
    }
    created = []
    for name in names:
        path = _resolve_split_file(name)
        try:
            item = catalog_managed_file(path, ctx, db)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        task = submit_task(
            TaskSubmit(
                project_id=item.project_id,
                task_type="script.parse",
                payload={
                    "input_file_id": item.id,
                    "source_name": item.original_name,
                    "config": snapshot,
                },
                estimated_units=0,
                idempotency_key=f"script-parse:{item.id}:{uuid.uuid4()}",
            ),
            user=ctx.user,
            db=db,
        )
        created.append({"file": name, "task_id": task["id"]})
    return {"task_ids": [item["task_id"] for item in created], "files": created}


class CancelBatchRequest(BaseModel):
    task_ids: list[str]


@router.post("/cancel-batch")
def cancel_batch(
    req: CancelBatchRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Cancel selected persistent parse tasks."""
    return cancel_batch_durable(req, ctx=ctx, db=db)


@router.post("/cancel-batch-durable")
def cancel_batch_durable(
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
