"""Book endpoints submit durable analysis and split tasks.

The worker publishes outputs to the managed ``02_split_text`` project module.
"""
from __future__ import annotations

import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context
from ..platform.legacy_files import catalog_managed_file
from .platform_tasks import TaskSubmit, submit_task
from . import _common

router = APIRouter(prefix="/api/book", tags=["book"])


class AnalyzeRequest(BaseModel):
    path: str


class SplitRequest(BaseModel):
    path: str
    base: str | None = None  # override the base (file) name
    whole_book: bool = False  # write the entire text as one ``<base> 全书.txt``
    smart: bool = False  # split by the SMART-REPAIRED structure (智能识别结果)


class SmartSplitRequest(BaseModel):
    path: str


@router.post("/analyze")
def analyze(
    req: AnalyzeRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    _common.require_workspace()
    source = _common.resolve_inbound_path(req.path, label="文本文件")
    if not source.is_file():
        raise HTTPException(400, f"不是一个文件：{req.path}")
    try:
        item = catalog_managed_file(source, ctx, db)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    task = submit_task(
        TaskSubmit(
            project_id=item.project_id,
            task_type="book.analyze",
            payload={"input_file_id": item.id},
            estimated_units=0,
            idempotency_key=f"book-analyze:{item.id}:{uuid.uuid4()}",
        ),
        user=ctx.user,
        db=db,
    )
    return {"task_id": task["id"], "project_id": item.project_id}


@router.post("/split")
def split(req: SplitRequest, ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> dict:
    _common.require_workspace()
    source = _common.resolve_inbound_path(req.path, label="文本文件")
    if not source.is_file():
        raise HTTPException(400, f"不是一个文件：{req.path}")
    try:
        item = catalog_managed_file(source, ctx, db)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    task = submit_task(
        TaskSubmit(
            project_id=item.project_id,
            task_type="book.split",
            payload={
                "input_file_id": item.id,
                "base": req.base,
                "whole_book": req.whole_book,
                "smart": req.smart,
            },
            estimated_units=0,
            idempotency_key=f"book-split:{item.id}:{req.base}:{req.whole_book}:{req.smart}:{uuid.uuid4()}",
        ),
        user=ctx.user,
        db=db,
    )
    return {"task_id": task["id"], "project_id": item.project_id}


@router.post("/smart-split")
def smart_split(req: SmartSplitRequest, ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> dict:
    """智能识别：mechanically repair the chapter structure (renumber 1..N in
    physical order, split abnormally long chapters, drop exact-duplicate
    chapters) and write one file per repaired chapter as ``第 NNN 章 标题.txt``
    into ``02_split_text/``. Always produces output when chapters exist —
    every inferred action is reported with a confidence level. Never rewrites
    file content; previously generated split files are cleared first."""
    _common.require_workspace()
    source = _common.resolve_inbound_path(req.path, label="文本文件")
    if not source.is_file():
        raise HTTPException(400, f"不是一个文件：{req.path}")
    try:
        item = catalog_managed_file(source, ctx, db)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    task = submit_task(
        TaskSubmit(
            project_id=item.project_id,
            task_type="book.split",
            payload={"input_file_id": item.id, "smart": True, "whole_book": False},
            estimated_units=0,
            idempotency_key=f"book-smart-split:{item.id}:{uuid.uuid4()}",
        ),
        user=ctx.user,
        db=db,
    )
    return {"task_id": task["id"], "project_id": item.project_id}
