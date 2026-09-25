"""Audio endpoints for probing, planning, splitting, packaging, and export.

Probe and plan are quick synchronous operations; silences, cut, zip, and export
run as durable tasks.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import get_config
from ..core.paths import get_or_prepare_layout
from ..engines import audio as A
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context
from ..platform.legacy_files import catalog_managed_file
from ..platform.engine_task_submission import estimate_legacy_units, submit_legacy_engine_task
from ..platform.models import ProjectFile
from .task_submission import TaskSubmit, submit_task
from . import _common

router = APIRouter(prefix="/api/audio", tags=["audio"])


def _register_legacy_input(path: str, ctx: AuthContext, db: Session) -> ProjectFile:
    """Catalog a legacy workspace file before submitting a durable task.

    The old audio picker returns a path rather than a ProjectFile id.  Resolve it
    inside the authenticated managed workspace, then register that existing file
    under the same user/project object-key namespace used by durable uploads.
    """
    _common.require_workspace()
    source = _common.resolve_inbound_path(path, label="音频文件")
    if not source.is_file():
        raise HTTPException(400, "不是一个文件。")
    try:
        return catalog_managed_file(source, ctx, db)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# ============================ Sync: probe / plan ============================

class ProbeRequest(BaseModel):
    path: str


@router.post("/probe")
def probe(req: ProbeRequest) -> dict:
    cfg = get_config()
    p = _common.resolve_inbound_path(req.path, label="音频文件")
    if not p.is_file():
        raise HTTPException(400, "不是一个文件。")
    duration, err = A.probe_duration(p, cfg.ffmpeg.ffprobe_path)
    if err or not (duration > 0):
        raise HTTPException(400, f"无法读取音频时长：{err}")
    ext = A.get_extension(p.name)
    return {
        "ok": True,
        "name": p.name,
        "path": str(p),
        "duration": duration,
        "size": p.stat().st_size,
        "ext": ext,
        "mime": A.MIME.get(ext, "application/octet-stream"),
    }


class PlanRequest(BaseModel):
    path: str
    target_duration: str | None = None


@router.post("/plan")
def plan(req: PlanRequest) -> dict:
    """Quick even-split preview (the aligned plan comes from the ``/silences`` task)."""
    cfg = get_config()
    target = req.target_duration or cfg.audio.target_duration
    p = _common.resolve_inbound_path(req.path, label="音频文件")
    if not p.is_file():
        raise HTTPException(400, "不是一个文件。")
    duration, err = A.probe_duration(p, cfg.ffmpeg.ffprobe_path)
    if err or not (duration > 0):
        raise HTTPException(400, f"无法读取时长：{err}")
    result = A.build_plan(duration, target)
    if not result["valid"]:
        raise HTTPException(400, result["reason"])
    return {
        "duration": duration,
        "count": result["count"],
        "each": result["each"],
        "target": result["target"],
        "segments": result["segments"],
    }


# ============================ Long-running endpoints ========================

class SilencesRequest(BaseModel):
    path: str
    target_duration: str | None = None
    align_tolerance: int | None = None


@router.post("/silences")
def silences(
    req: SilencesRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    cfg = get_config()
    a = cfg.audio
    target = req.target_duration or a.target_duration
    tol = req.align_tolerance if req.align_tolerance is not None else a.align_tolerance
    item = _register_legacy_input(req.path, ctx, db)
    task = submit_task(
        TaskSubmit(
            project_id=item.project_id,
            task_type="audio.silences",
            payload={"input_file_id": item.id, "source_name": item.original_name, "target": target, "tolerance": tol},
            estimated_units=estimate_legacy_units("audio.silences", {"input_file_id": item.id}),
            idempotency_key=f"audio-silences:{item.id}:{uuid.uuid4()}",
        ),
        user=ctx.user,
        db=db,
    )
    return {"task_id": task["id"]}


class CutRequest(BaseModel):
    path: str
    target_duration: str | None = None
    smart_align: bool | None = None
    align_tolerance: int | None = None
    naming_format: str | None = None
    start_number: str | None = None
    segments: list | None = None  # pre-computed plan (list of {index,start,duration})


@router.post("/cut")
def cut(
    req: CutRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    cfg = get_config()
    a = cfg.audio
    target = req.target_duration or a.target_duration
    smart = req.smart_align if req.smart_align is not None else a.smart_align
    tol = req.align_tolerance if req.align_tolerance is not None else a.align_tolerance
    naming = req.naming_format if req.naming_format is not None else a.naming_format
    start = req.start_number if req.start_number is not None else a.start_number
    item = _register_legacy_input(req.path, ctx, db)
    task = submit_task(
        TaskSubmit(
            project_id=item.project_id,
            task_type="audio.cut",
            payload={
                "input_file_id": item.id,
                "source_name": item.original_name,
                "target": target,
                "smart_align": smart,
                "tolerance": tol,
                "naming": naming,
                "start_number": start,
                "segments": req.segments,
            },
            estimated_units=estimate_legacy_units("audio.cut", {"segments": req.segments}),
            idempotency_key=f"audio-cut:{item.id}:{uuid.uuid4()}",
        ),
        user=ctx.user,
        db=db,
    )
    return {"task_id": task["id"]}


# ============================ Sync: post-cut packaging / export =============

class ZipFileSpec(BaseModel):
    name: str
    path: str


class ZipRequest(BaseModel):
    base: str | None = None  # source-audio stem -> becomes the zip's file name
    files: list[ZipFileSpec]


@router.post("/zip")
def zip_files(
    req: ZipRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Submit an ``audio.zip`` durable task that packages already-cut files
    into a STORE zip under the workspace's ``07_output/`` so the browser can
    download it."""
    _common.require_workspace()
    if not req.files:
        raise HTTPException(400, "没有可打包的文件。")
    layout = get_or_prepare_layout()
    base = (req.base or "").strip() or "audio"
    workspace = layout.workspace
    if workspace is None:
        raise HTTPException(409, "尚未设置工作空间")
    entries = []
    for spec in req.files:
        path = _common.resolve_inbound_path(spec.path, label=f"文件 {spec.name or ''}".strip())
        if not path.is_file():
            raise HTTPException(400, f"不是一个文件：{spec.name or path.name}")
        entries.append({
            "name": Path(spec.name or path.name).name,
            "relative_path": path.relative_to(workspace).as_posix(),
        })
    task = submit_legacy_engine_task(
        task_type="audio.zip",
        label=f"音频打包：{base}",
        payload={"base": base, "files": entries},
        ctx=ctx,
        db=db,
        idempotency_prefix=f"audio-zip:{base}",
    )
    return {"task_id": task["id"]}


class ExportRequest(BaseModel):
    source_path: str
    files: list[ZipFileSpec]


@router.post("/export")
def export_to_source(
    req: ExportRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Copy the cut files into a ``分集`` folder created beside the source audio, so
    the results land in the user's own folder next to the original file."""
    _common.require_workspace()
    src = _common.resolve_inbound_path(req.source_path, label="源音频文件")
    if not src.is_file():
        raise HTTPException(400, "源音频不是一个文件。")
    if not req.files:
        raise HTTPException(400, "没有可输出的文件。")
    workspace = get_or_prepare_layout().workspace
    if workspace is None:
        raise HTTPException(409, "尚未设置工作空间")
    entries = []
    for spec in req.files:
        path = _common.resolve_inbound_path(spec.path, label=f"文件 {spec.name or ''}".strip())
        if not path.is_file():
            raise HTTPException(400, f"不是一个文件：{spec.name or path.name}")
        entries.append({
            "name": Path(spec.name or path.name).name,
            "relative_path": path.relative_to(workspace).as_posix(),
        })
    source = _common.resolve_inbound_path(req.source_path, label="源音频文件")
    if not source.is_file():
        raise HTTPException(400, "源音频不是一个文件。")
    task = submit_legacy_engine_task(
        task_type="audio.export",
        label=f"音频导出：{source.name}",
        payload={
            "source_relative": source.relative_to(workspace).as_posix(),
            "files": entries,
        },
        ctx=ctx,
        db=db,
        idempotency_prefix=f"audio-export:{source.name}",
    )
    return {"task_id": task["id"]}
