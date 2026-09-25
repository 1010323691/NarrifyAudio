"""Text-formatting endpoint backed by the durable task worker.

The task preserves the original upload and publishes the formatted result in
``01_input/`` under a new name.
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import get_config
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context
from ..platform.legacy_files import catalog_managed_file
from ..platform.storage import safe_display_name
from .platform_tasks import TaskSubmit, submit_task
from . import _common

router = APIRouter(prefix="/api/text", tags=["text"])


class FormatRequest(BaseModel):
    path: str
    config: dict = {}  # partial overrides for the 10 toggles


@router.post("/format")
def format_text_endpoint(req: FormatRequest, ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db)) -> dict:
    _common.require_workspace()
    source = _common.resolve_inbound_path(req.path, label="文本文件")
    if not source.is_file():
        raise HTTPException(400, f"不是一个文件：{req.path}")
    try:
        item = catalog_managed_file(source, ctx, db)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    output_name = safe_display_name(f"{source.stem}_排版{source.suffix}")
    task = submit_task(
        TaskSubmit(
            project_id=item.project_id,
            task_type="text.format",
            payload={
                "input_file_id": item.id,
                "config": _common.partial_copy(get_config().text, req.config).model_dump(mode="json"),
                "publish_module": "01_input",
                "output_name": output_name,
            },
            estimated_units=0,
            idempotency_key=f"text-format:{item.id}:{output_name}:{hashlib.sha256(repr(req.config).encode()).hexdigest()}",
        ),
        user=ctx.user,
        db=db,
    )
    return {"task_id": task["id"], "file_id": item.id, "project_id": item.project_id}
