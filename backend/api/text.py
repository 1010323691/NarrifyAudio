"""Text-formatting endpoint (module: 文本排版).

Reads a TXT, formats it with the ported engine, writes the result to the
workspace's ``01_input/`` under a *new* name (``<原基名>_排版<扩展名>`` — the
original upload is preserved alongside it) and returns stats + a preview (the
preview pane is a deliberate enhancement over the source tool, which only showed
stats).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_config
from ..core.paths import get_layout
from ..engines.text import format_text
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context
from ..platform.legacy_files import catalog_managed_file
from ..platform.legacy_workspace import active_workspace, ensure_project
from ..platform.models import ProjectFile
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
    if isinstance(ctx, AuthContext):
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
    text, enc, src = _common.read_decoded_file(req.path)

    cfg = _common.partial_copy(get_config().text, req.config)
    result = format_text(text, cfg)

    # New name (``<基名>_排版<扩展名>``) so the original upload in 01_input/ is kept.
    out_path: Path = get_layout().input / f"{src.stem}_排版{src.suffix}"
    # write_bytes: keep the formatted text's line endings as-is (no CRLF translation).
    out_path.write_bytes(result["text"].encode("utf-8"))
    workspace = active_workspace(db, ctx.user, ctx.session)
    if workspace is None:
        raise HTTPException(409, "尚未设置工作空间")
    project = ensure_project(db, ctx.user, workspace)
    name = safe_display_name(out_path.name)
    object_key = f"{safe_display_name(ctx.user.username)}/{project.id}/01_input/{name}"
    data = out_path.read_bytes()
    item = db.scalar(select(ProjectFile).where(ProjectFile.object_key == object_key))
    if item is None:
        item = ProjectFile(
            project_id=project.id,
            owner_id=ctx.user.id,
            original_name=name,
            object_key=object_key,
            content_type="text/plain; charset=utf-8",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            kind="input",
        )
        db.add(item)
    else:
        item.original_name = name
        item.size_bytes = len(data)
        item.sha256 = hashlib.sha256(data).hexdigest()
        item.deleted_at = None
    db.commit()
    db.refresh(item)

    return {
        "source": str(src),
        "encoding": enc,
        "output_path": str(out_path),
        "stats": result["stats"],
        "preview": result["text"][:2000],
        "full_length": len(result["text"]),
        "file_id": item.id,
        "project_id": project.id,
    }
