"""Audio-splitting endpoints (module: 音频分集).

Two fast, synchronous endpoints and two long-running tasks:

* ``POST /probe``    — ``ffprobe`` duration/size/extension (sync, quick).
* ``POST /plan``     — even-distribution plan (sync, pure math after a probe).
* ``POST /silences`` — pause detection + pause-aligned plan (**task**: long,
  cancellable, streamed) for the "智能对齐" preview.
* ``POST /cut``      — lossless ``-c copy`` cut to the workspace's ``07_output/``
  (**task**: the main work; re-detects pauses when smart-align is on unless a
  plan is passed in).

FFmpeg/ffprobe come from ``config.ffmpeg`` (empty → resolved from PATH).
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.config import get_config
from ..core.paths import get_layout
from ..core.tasks import get_task_manager
from ..engines import audio as A
from . import _common

router = APIRouter(prefix="/api/audio", tags=["audio"])


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


# ============================ Long-running task workers =====================

def _silences_worker(handle, path, target, tolerance, ffmpeg_path, ffprobe_path) -> dict:
    """Probe → detect pauses (streamed, cancellable) → pause-aligned plan."""
    try:
        p = _common.resolve_inbound_path(path, label="音频文件")
    except HTTPException as e:
        raise RuntimeError(str(e.detail))
    if not p.is_file():
        raise RuntimeError("输入不是一个文件。")
    handle.progress(0.02, "读取时长")
    duration, err = A.probe_duration(p, ffprobe_path)
    if err or not (duration > 0):
        raise RuntimeError(f"无法读取音频时长：{err}")

    handle.log(f"时长 {A.format_duration(duration)}，开始检测停顿")
    res = A.detect_silences(
        p, duration, ffmpeg_path,
        on_progress=lambda f: handle.progress(0.05 + f * 0.88, "检测停顿"),
        should_cancel=handle.cancelled,
        on_log=lambda m: handle.log(m),
    )
    if not res["ok"]:
        raise RuntimeError(res["reason"])
    pauses = res["pauses"]
    handle.log(f"检测到 {len(pauses)} 处停顿")

    plan = A.build_aligned_plan(duration, target, pauses, tolerance)
    if not plan["valid"]:
        raise RuntimeError(plan["reason"])

    handle.progress(1.0, "完成")
    return {
        "duration": duration,
        "pause_count": len(pauses),
        "pauses": pauses,
        "count": plan["count"],
        "segments": plan["segments"],
        "snapped": plan.get("snapped", 0),
        "fallbacks": plan.get("fallbacks", 0),
        "aligned": plan.get("aligned", False),
        "shifts": plan.get("shifts", []),
    }


def _cut_worker(handle, path, target, smart_align, tolerance, naming, start_number,
                ffmpeg_path, ffprobe_path, ext, segments=None) -> dict:
    """Cut to the workspace's ``07_output/``. Reuses a client-supplied plan if given;
    otherwise probes (and re-detects pauses for smart-align) to build one."""
    try:
        p = _common.resolve_inbound_path(path, label="音频文件")
    except HTTPException as e:
        raise RuntimeError(str(e.detail))
    if not p.is_file():
        raise RuntimeError("输入不是一个文件。")

    handle.progress(0.02, "读取时长")
    duration, err = A.probe_duration(p, ffprobe_path)
    if err or not (duration > 0):
        raise RuntimeError(f"无法读取音频时长：{err}")
    handle.log(f"时长 {A.format_duration(duration)}（{A.format_bytes(p.stat().st_size)}）")

    if segments is None:
        handle.progress(0.05, "生成切割方案")
        if smart_align:
            res = A.detect_silences(
                p, duration, ffmpeg_path,
                on_progress=lambda f: handle.progress(0.05 + f * 0.25, "检测停顿"),
                should_cancel=handle.cancelled,
                on_log=lambda m: handle.log(m),
            )
            if not res["ok"]:
                raise RuntimeError(res["reason"])
            pauses = res["pauses"]
            handle.log(f"检测到 {len(pauses)} 处停顿")
            plan = A.build_aligned_plan(duration, target, pauses, tolerance)
        else:
            plan = A.build_plan(duration, target)
        if not plan["valid"]:
            raise RuntimeError(plan["reason"])
        segments = plan["segments"]
    else:
        if not segments:
            raise RuntimeError("未提供有效的切割方案。")

    if len(segments) > A.MAX_SEGMENTS:
        raise RuntimeError(f"段数 {len(segments)} 超过上限 {A.MAX_SEGMENTS}。")

    base = p.stem  # 原文件名（去扩展名）= 包名；切集产物落到 07_output/<包名>/
    out_dir = get_layout().output / base
    handle.progress(0.32, "开始切割")
    files = A.cut_segments(
        p, segments, out_dir, naming, start_number,
        ffmpeg_path, ext,
        on_progress=lambda f: handle.progress(0.32 + f * 0.66, f"切割 {int(f * 100)}%"),
        should_cancel=handle.cancelled,
        on_log=lambda m: handle.log(m),
    )

    handle.progress(1.0, "完成")
    return {
        "output_dir": str(out_dir),
        "file_count": len(files),
        "files": files,
        "duration": duration,
        "smart_align": smart_align,
    }


# ============================ Long-running endpoints ========================

class SilencesRequest(BaseModel):
    path: str
    target_duration: str | None = None
    align_tolerance: int | None = None


@router.post("/silences")
def silences(req: SilencesRequest) -> dict:
    cfg = get_config()
    a = cfg.audio
    target = req.target_duration or a.target_duration
    tol = req.align_tolerance if req.align_tolerance is not None else a.align_tolerance
    task = get_task_manager().create(
        "audio", f"停顿检测：{Path(req.path).name}",
        _silences_worker,
        req.path, target, tol,
        cfg.ffmpeg.ffmpeg_path, cfg.ffmpeg.ffprobe_path,
    )
    return {"task_id": task.id}


class CutRequest(BaseModel):
    path: str
    target_duration: str | None = None
    smart_align: bool | None = None
    align_tolerance: int | None = None
    naming_format: str | None = None
    start_number: str | None = None
    segments: list | None = None  # pre-computed plan (list of {index,start,duration})


@router.post("/cut")
def cut(req: CutRequest) -> dict:
    _common.require_workspace()
    cfg = get_config()
    a = cfg.audio
    target = req.target_duration or a.target_duration
    smart = req.smart_align if req.smart_align is not None else a.smart_align
    tol = req.align_tolerance if req.align_tolerance is not None else a.align_tolerance
    naming = req.naming_format if req.naming_format is not None else a.naming_format
    start = req.start_number if req.start_number is not None else a.start_number
    ext = A.get_extension(Path(req.path).name)
    task = get_task_manager().create(
        "audio", f"音频分集：{Path(req.path).name}",
        _cut_worker,
        req.path, target, smart, tol, naming, start,
        cfg.ffmpeg.ffmpeg_path, cfg.ffmpeg.ffprobe_path, ext,
        segments=req.segments,
    )
    return {"task_id": task.id}


# ============================ Sync: post-cut packaging / export =============

class ZipFileSpec(BaseModel):
    name: str
    path: str


class ZipRequest(BaseModel):
    base: str | None = None  # source-audio stem -> becomes the zip's file name
    files: list[ZipFileSpec]


@router.post("/zip")
def zip_files(req: ZipRequest) -> dict:
    """Package already-cut files into a STORE zip under the workspace's
    ``07_output/`` so the browser can download it (mirrors the book module's
    optional ``.zip`` output)."""
    _common.require_workspace()
    if not req.files:
        raise HTTPException(400, "没有可打包的文件。")
    layout = get_layout()
    base = (req.base or "").strip() or "audio"
    entries = []
    for spec in req.files:
        p = _common.resolve_inbound_path(spec.path, label=f"文件 {spec.name or ''}".strip())
        if not p.is_file():
            raise HTTPException(400, f"不是一个文件：{spec.name or p.name}")
        entries.append((spec.name or p.name, p))
    # 打包 zip 与分集产物同处一个按源命名的子文件夹（07_output/<base>/）。
    out_dir = layout.output / base
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{base}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for name, p in entries:
            zf.write(p, arcname=name)  # STORED: no re-encode, matches book build_zip
    return {"zip_path": str(zip_path), "file_count": len(entries)}


class ExportRequest(BaseModel):
    source_path: str
    files: list[ZipFileSpec]


@router.post("/export")
def export_to_source(req: ExportRequest) -> dict:
    """Copy the cut files into a ``分集`` folder created beside the source audio, so
    the results land in the user's own folder next to the original file."""
    _common.require_workspace()
    src = _common.resolve_inbound_path(req.source_path, label="源音频文件")
    if not src.is_file():
        raise HTTPException(400, "源音频不是一个文件。")
    if not req.files:
        raise HTTPException(400, "没有可输出的文件。")
    dest_dir = src.parent / "分集"
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for spec in req.files:
        p = _common.resolve_inbound_path(spec.path, label=f"文件 {spec.name or ''}".strip())
        if not p.is_file():
            raise HTTPException(400, f"不是一个文件：{spec.name or p.name}")
        name = spec.name or p.name
        if Path(name).name != name:  # a crafted name must not escape 分集/
            raise HTTPException(400, f"非法文件名：{name}")
        dest = dest_dir / name
        shutil.copy2(p, dest)  # plain copy — the files are already losslessly cut
        written.append({"name": name, "path": str(dest)})
    return {"dest_dir": str(dest_dir), "file_count": len(written), "files": written}
