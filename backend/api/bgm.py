"""BGM chapter analysis, matching, manual edits, and mixing endpoints.

Long-running operations are submitted as durable Worker tasks. Snapshot module
labels retain their legacy names for the existing task UI.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core import paths as core_paths
from ..core.config import get_config
from ..core.paths import get_or_prepare_layout, resolve_layout
from ..engines import bgm as Bgm
from ..engines import music as music_engine
from ..engines import tts_batch as TtsBatch
from ..engines.audio import probe_duration
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context
from ..platform.legacy_tasks import active_durable_payloads, active_durable_targets, submit_legacy_engine_task
from ..platform.task_validation import is_safe_bgm_stem
from . import _common

router = APIRouter(prefix="/api/bgm", tags=["bgm"])

# Durable task types dispatched by the task worker.
ANALYSIS_MODULE = "bgm-analysis"
MIX_MODULE = "bgm-mix"
SEGMENT_MODULE = "bgm-segment"
ANALYSIS_LABEL = "章节气氛分析"
MIX_LABEL = "背景音乐混音"
SEGMENT_LABEL = "段落分析"


# --------------------------------------------------------------------------- #
# Durable in-flight guards.
# --------------------------------------------------------------------------- #

def _durable_audio_conflicts(stems: list[str], ctx: AuthContext, db: Session) -> list[str]:
    """Find chapters whose active durable TTS batch or merge can change audio."""
    wanted = {TtsBatch.package_for(Path(f"{stem}.json")): stem for stem in stems}
    active_packages = active_durable_targets(
        task_type="tts.merge", payload_key="package", ctx=ctx, db=db,
    )
    batch_payloads = active_durable_payloads(task_type="tts.batch", ctx=ctx, db=db)
    batch_payload_targets: set[str] = set()
    has_unscoped_batch = False
    for payload in batch_payloads:
        scripts = payload.get("scripts")
        script = payload.get("script")
        if isinstance(scripts, list) and scripts:
            batch_payload_targets.update(str(value) for value in scripts)
        elif isinstance(script, str) and script:
            batch_payload_targets.add(script)
        else:
            # A missing script means "most recent" and cannot be mapped safely.
            has_unscoped_batch = True
    if has_unscoped_batch:
        return list(stems)
    active_packages.update(
        TtsBatch.package_for(Path(script)) for script in batch_payload_targets if script
    )
    return sorted(stem for package, stem in wanted.items() if package in active_packages)


def _validated_stems(layout, stems: list[str]) -> list[str]:
    """Guard + dedupe chapter stems: no traversal, the 02 file must exist."""
    out: list[str] = []
    for s in stems:
        if not is_safe_bgm_stem(s) or s != Path(s).name:
            raise HTTPException(400, f"非法章节名：{s!r}")
        if not (layout.split_text / f"{s}.txt").is_file():
            raise HTTPException(400, f"未找到章节文件（02_split_text/{s}.txt）。")
        if s not in out:
            out.append(s)
    return out


def _segment_validated_stems(layout, stems: list[str]) -> list[str]:
    """The paragraph-analysis stem guard: :func:`_validated_stems` + the 03
    script must exist (the analysis's input). Re-checking an already-validated
    list is cheap and idempotent, so both callers (explicit / default-all)
    funnel through here."""
    out = _validated_stems(layout, stems)
    for s in out:
        if not (layout.parsed_json / f"{s}.json").is_file():
            raise HTTPException(
                400,
                f"未找到脚本 JSON（03_parsed_json/{s}.json）——请先在「文本解析」生成脚本。")
    return out


# --------------------------------------------------------------------------- #
# rows (read-only — no 409; a missing workspace degrades to empty rows)
# --------------------------------------------------------------------------- #

@router.get("/chapters")
def list_chapters() -> dict:
    """One row per ``02_split_text`` stem (sorted), joined with the two 08_bgm
    JSON caches and the disk facts the frontend badges need:

    * ``narration_exists`` — 06 旁白 present (mixing's input);
    * ``mix_exists`` — 08_bgm/<stem>.mp3 present (a no-BGM chapter's copy2 counts);
    * ``music_missing`` — the assignment points at a music file that no longer
      exists in the library (frontend: 「⚠ 已删除」, mixing blocked, re-match allowed).
    """
    layout = resolve_layout()
    if layout.split_text is None:
        return {"chapters": [], "mode": "llm"}
    lib_dir = core_paths.MUSIC_LIBRARY_DIR
    analysis = (Bgm.load_analysis(layout).get("chapters") or {})
    seg_data = Bgm.load_segment_analysis(layout).get("chapters") or {}
    data = Bgm.load_assignments(layout)
    chapters = data.get("chapters") or {}
    rows = []
    for stem in Bgm.list_chapter_stems(layout):
        a = analysis.get(stem)
        a_out = None
        if isinstance(a, dict):
            a_out = {c: [t for t in (a.get(c) or []) if isinstance(t, str)]
                     for c in music_engine.TAG_CATEGORIES}
            a_out["analyzed_at"] = a.get("analyzed_at", "")
            a_out["edited"] = bool(a.get("edited", False))
        e = chapters.get(stem)
        e_out = None
        music_missing = False
        if isinstance(e, dict):
            tags = e.get("tags") or {}
            e_out = {
                "tags": {c: [t for t in (tags.get(c) or []) if isinstance(t, str)]
                         for c in music_engine.TAG_CATEGORIES},
                "music": e.get("music") if isinstance(e.get("music"), str) else None,
                "locked": bool(e.get("locked", False)),
                "manual": bool(e.get("manual", False)),
                "score": e.get("score"),
                "reason": e.get("reason", ""),
                "matched_at": e.get("matched_at", ""),
                # Keep the mode discriminator: paragraph-level assignments use
                # music=None intentionally and read their concrete tracks from
                # the timeline file.
                "segment": bool(e.get("segment", False)),
            }
            if e_out["music"] and not (lib_dir / e_out["music"]).is_file():
                music_missing = True
        # 段落级行字段（全部行都带；null/False = 该章没有段落分析/时间轴）。
        # stale = 分析指纹 vs 当前 03 条目（03 缺失/损坏 → 指纹必不匹配 → True）。
        sa = seg_data.get(stem)
        segment_analysis = None
        if isinstance(sa, dict) and (sa.get("blocks") or sa.get("entries")):
            try:
                stale = (sa.get("fingerprint")
                         != Bgm.segment_fingerprint(Bgm._load_parsed_entries(layout, stem)))
            except Exception:  # noqa: BLE001 — 03 缺失/损坏 = 分析已不可用
                stale = True
            segment_tags = {c: [] for c in music_engine.TAG_CATEGORIES}
            for block in sa.get("blocks") or sa.get("entries") or []:
                if not isinstance(block, dict) or block.get("extend"):
                    continue
                tags = block.get("music_tags") or block.get("tags") or {}
                if not isinstance(tags, dict):
                    continue
                for category in music_engine.TAG_CATEGORIES:
                    for tag in tags.get(category) or []:
                        if isinstance(tag, str) and tag not in segment_tags[category]:
                            segment_tags[category].append(tag)
            segment_analysis = {
                "analyzed_at": sa.get("analyzed_at", ""),
                "entry_count": sa.get("entry_count", 0),
                "stale": stale,
                # The full block cache stays on disk; the row only needs the
                # deduplicated tag union for immediate list rendering.
                "tags": segment_tags,
            }
        # 允许查看仍对应当前分析指纹的时间轴，即使一次章节级匹配把
        # assignment.segment 重置为 false。这样可恢复的缓存不会被误报为
        #“结果丢失”；混音入口与引擎也会用同一套有效性判断。
        timeline = None
        segment_music_missing = False
        timeline_visible = bool(
            isinstance(e, dict) and e.get("segment")
        ) or bool(segment_analysis is not None and not segment_analysis["stale"])
        if timeline_visible:
            tl = Bgm.load_timeline(layout, stem)
            if tl is not None:
                spans = tl.get("timeline") or []
                timeline = {
                    "sections": len(spans),
                    "duration": tl.get("duration"),
                    "generated_at": tl.get("generated_at", ""),
                }
                for sp in spans:
                    mid = sp.get("music_id") or ""
                    if not (lib_dir / mid).is_file():
                        segment_music_missing = True
                        break
        rows.append({
            "stem": stem,
            "narration_exists": Bgm._find_narration(layout, stem) is not None,
            "mix_exists": (layout.bgm / f"{stem}.mp3").is_file(),
            "analysis": a_out,
            "assignment": e_out,
            "music_missing": music_missing,
            "segment_analysis": segment_analysis,
            "timeline": timeline,
            "segment_music_missing": segment_music_missing,
        })
    return {"chapters": rows, "mode": data.get("mode", "llm")}


def _source_txt_base(layout) -> str:
    """Return the source TXT stem used as the package folder name.

    Formatting keeps the original upload and writes a sibling ``_排版`` file,
    so prefer an unformatted TXT when both are present.  The newest candidate
    is the best fallback for workspaces that contain more than one upload.
    """
    input_dir = layout.input
    candidates = [p for p in (input_dir.glob("*.txt") if input_dir else []) if p.is_file()]
    if not candidates:
        raise HTTPException(400, "未找到源 TXT 文档，无法确定打包文件夹名称。")
    originals = [p for p in candidates if not p.stem.endswith("_排版")]
    source = max(originals or candidates, key=lambda p: (p.stat().st_mtime_ns, p.name))
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", source.stem)
    base = re.sub(r"\s+", " ", base).strip()
    return base or "有声书"


# --------------------------------------------------------------------------- #
# Durable task submission and conflict guards.
# --------------------------------------------------------------------------- #

class AnalyzeRequest(BaseModel):
    chapters: list[str]


class PackageRequest(BaseModel):
    chapters: list[str] | None = None  # None = all existing chapters


@router.post("/analyze")
def run_analyze(
    req: AnalyzeRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Submit one durable analysis task per selected chapter."""
    _common.require_workspace()
    layout = get_or_prepare_layout()
    stems = _validated_stems(layout, req.chapters or [])
    if not stems:
        raise HTTPException(400, "请选择要分析的章节。")
    cfg = get_config()
    if not cfg.llm.model_name:
        raise HTTPException(400, "尚未配置 LLM 模型（设置 → LLM → model_name）。")
    active = active_durable_targets(task_type="bgm.analysis", payload_key="stem", ctx=ctx, db=db)
    conflicts = [stem for stem in stems if stem in active]
    active_matches = active_durable_targets(
        task_type="bgm.match", payload_key="chapters", ctx=ctx, db=db,
    )
    active_mixes = active_durable_targets(task_type="bgm.mix", payload_key="stem", ctx=ctx, db=db)
    conflicts.extend(
        stem for stem in stems
        if (stem in active_matches or stem in active_mixes) and stem not in conflicts
    )
    if conflicts:
        raise HTTPException(409, "以下章节已有分析任务在途：" + "、".join(conflicts))
    created = []
    for stem in stems:
        task = submit_legacy_engine_task(
            task_type="bgm.analysis",
            label=f"{ANALYSIS_LABEL}：{stem}",
            payload={"stem": stem, "config": cfg.model_dump(mode="json")},
            ctx=ctx,
            db=db,
            idempotency_prefix=f"bgm-analysis:{stem}",
        )
        created.append({"stem": stem, "task_id": task["id"]})
    return {"task_ids": [item["task_id"] for item in created], "chapters": created}


# --------------------------------------------------------------------------- #
# Durable task submission and conflict guards.
# --------------------------------------------------------------------------- #

class SegmentAnalyzeRequest(BaseModel):
    chapters: list[str]


@router.post("/analyze-segment")
def run_analyze_segment(
    req: SegmentAnalyzeRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Submit one durable paragraph-analysis task per selected chapter."""
    _common.require_workspace()
    layout = get_or_prepare_layout()
    stems = _segment_validated_stems(layout, req.chapters or [])
    if not stems:
        raise HTTPException(400, "请选择要段落分析的章节。")
    cfg = get_config()
    if not cfg.llm.model_name:
        raise HTTPException(400, "尚未配置 LLM 模型（设置 → LLM → model_name）。")
    active = active_durable_targets(task_type="bgm.segment", payload_key="stem", ctx=ctx, db=db)
    conflicts = [stem for stem in stems if stem in active]
    active_matches = active_durable_targets(
        task_type="bgm.match", payload_key="chapters", ctx=ctx, db=db,
    )
    active_mixes = active_durable_targets(task_type="bgm.mix", payload_key="stem", ctx=ctx, db=db)
    conflicts.extend(
        stem for stem in stems
        if (stem in active_matches or stem in active_mixes) and stem not in conflicts
    )
    if conflicts:
        raise HTTPException(409, "以下章节已有段落分析任务在途：" + "、".join(conflicts))
    audio_conflicts = _durable_audio_conflicts(stems, ctx, db)
    if audio_conflicts:
        raise HTTPException(
            409, "以下章节音频合成或合并任务在途：" + "、".join(audio_conflicts)
        )
    created = []
    for stem in stems:
        task = submit_legacy_engine_task(
            task_type="bgm.segment",
            label=f"{SEGMENT_LABEL}：{stem}",
            payload={"stem": stem, "config": cfg.model_dump(mode="json")},
            ctx=ctx,
            db=db,
            idempotency_prefix=f"bgm-segment:{stem}",
        )
        created.append({"stem": stem, "task_id": task["id"]})
    return {"task_ids": [item["task_id"] for item in created], "chapters": created}


# --------------------------------------------------------------------------- #
# 匹配（持久任务：LLM / random / 段落级时间轴重算）
# --------------------------------------------------------------------------- #

class MatchRequest(BaseModel):
    chapters: list[str] | None = None  # None = all existing 02 chapters
    mode: str = "llm"  # "llm" | "random" | "segment"（段落级时间轴重算，零 LLM）


@router.post("/match")
def run_match(
    req: MatchRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Submit durable chapter-level matching or paragraph timeline tasks."""
    _common.require_workspace()
    layout = get_or_prepare_layout()
    if req.mode == "segment":
        stems = (
            _segment_validated_stems(layout, req.chapters)
            if req.chapters is not None
            else _segment_validated_stems(layout, Bgm.list_chapter_stems(layout))
        )
    else:
        if req.mode not in ("llm", "random"):
            raise HTTPException(400, f"未知匹配模式：{req.mode!r}")
        stems = (
            _validated_stems(layout, req.chapters)
            if req.chapters is not None
            else _validated_stems(layout, Bgm.list_chapter_stems(layout))
        )
    if not stems:
        raise HTTPException(400, "请选择要匹配的章节。")

    active_matches = active_durable_targets(
        task_type="bgm.match", payload_key="chapters", ctx=ctx, db=db,
    )
    active_mixes = active_durable_targets(task_type="bgm.mix", payload_key="stem", ctx=ctx, db=db)
    mix_conflicts = [stem for stem in stems if stem in active_mixes]
    if mix_conflicts:
        raise HTTPException(409, "以下章节已有混音任务在途：" + "、".join(mix_conflicts))
    if req.mode == "segment":
        active_segments = active_durable_targets(
            task_type="bgm.segment", payload_key="stem", ctx=ctx, db=db,
        )
        conflicts = [stem for stem in stems if stem in active_segments or stem in active_matches]
        if conflicts:
            raise HTTPException(409, "以下章节段落分析或匹配任务在途：" + "、".join(conflicts))
        audio_conflicts = _durable_audio_conflicts(stems, ctx, db)
        if audio_conflicts:
            raise HTTPException(409, "以下章节音频合成或合并任务在途：" + "、".join(audio_conflicts))
    else:
        active_analyses = active_durable_targets(
            task_type="bgm.analysis", payload_key="stem", ctx=ctx, db=db,
        )
        conflicts = [stem for stem in stems if stem in active_analyses or stem in active_matches]
        if conflicts:
            raise HTTPException(409, "以下章节分析或匹配任务在途：" + "、".join(conflicts))

    task = submit_legacy_engine_task(
        task_type="bgm.match",
        label=f"BGM match ({req.mode}): {len(stems)} chapters",
        payload={"chapters": stems, "mode": req.mode, "config": get_config().model_dump(mode="json")},
        ctx=ctx,
        db=db,
        idempotency_prefix=f"bgm-match:{req.mode}",
    )
    return {"task_id": task["id"]}


class ChapterUpdateRequest(BaseModel):
    tags: dict[str, list[str]] | None = None
    # Absent = untouched; null = clear (music=None); string = manual pick.
    music: str | None = None
    locked: bool | None = None


@router.put("/chapters/{stem}")
def update_chapter(stem: str, req: ChapterUpdateRequest) -> dict:
    """Apply a manual edit to one chapter (sync write, not a task):

    * ``tags`` → the analysis entry (``edited: true`` + ``edited_at``; the
      original ``analyzed_at`` is kept) and the assignment's tag snapshot;
    * ``music`` (key present) → validated against the library (missing 400);
      ``null`` clears; sets ``manual: true`` / ``score: null`` / reason
      「手动指定」;
    * ``locked`` → the lock flag (locked chapters are skipped by any re-match).
    """
    _common.require_workspace()
    layout = get_or_prepare_layout()
    if not stem or stem != Path(stem).name:
        raise HTTPException(400, f"非法章节名：{stem!r}")
    idx = music_engine.load_index()
    lib_dir = core_paths.MUSIC_LIBRARY_DIR
    now = datetime.now().isoformat(timespec="seconds")

    tags_norm = None
    if req.tags is not None:
        tags_norm = music_engine.normalize_track_tags(req.tags, idx["tags"])

    music_set = "music" in req.model_fields_set
    if music_set:
        m = (req.music or "").strip()
        if m:
            if m != Path(m).name:
                raise HTTPException(400, f"非法音乐文件名：{m!r}")
            if not (lib_dir / m).is_file():
                raise HTTPException(400, f"音乐库中找不到 {m}。")
        # m == "" / None → clear

    if tags_norm is not None:
        def _mutate_a(a_data: dict) -> None:
            a_chapters = a_data.setdefault("chapters", {})
            prev = a_chapters.get(stem) if isinstance(a_chapters.get(stem), dict) else {}
            a_entry = {c: list(tags_norm.get(c) or [])
                       for c in music_engine.TAG_CATEGORIES}
            a_entry["analyzed_at"] = prev.get("analyzed_at", "")
            a_entry["edited"] = True
            a_entry["edited_at"] = now
            a_chapters[stem] = a_entry

        Bgm.update_analysis(layout, _mutate_a)

    # Fallback source for a missing assignment entry — read AFTER the tags
    # write above (same order as the old inline sequence).
    fallback_analysis = (Bgm.load_analysis(layout).get("chapters") or {}).get(stem) or {}

    def _mutate_d(data: dict) -> None:
        chapters = data.setdefault("chapters", {})
        e = chapters.get(stem)
        if not isinstance(e, dict):
            a = fallback_analysis
            e = {
                "tags": {c: [t for t in (a.get(c) or []) if isinstance(t, str)]
                         for c in music_engine.TAG_CATEGORIES},
                "music": None, "locked": False, "manual": False,
                "score": None, "reason": "未匹配", "matched_at": now,
            }
        if tags_norm is not None:
            e["tags"] = {c: list(tags_norm.get(c) or [])
                         for c in music_engine.TAG_CATEGORIES}
        if music_set:
            e["music"] = ((req.music or "").strip() or None)
            e["manual"] = True
            e["score"] = None
            e["reason"] = "手动指定"
            e["matched_at"] = now
        if req.locked is not None:
            e["locked"] = bool(req.locked)
        chapters[stem] = e

    data = Bgm.update_assignments(layout, _mutate_d)
    return data["chapters"][stem]


# --------------------------------------------------------------------------- #
# Durable task submission and conflict guards.
# --------------------------------------------------------------------------- #

class MixRequest(BaseModel):
    chapters: list[str]


@router.post("/mix")
def run_mix(
    req: MixRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Validate selected assignments and submit one durable mix task per chapter."""
    _common.require_workspace()
    layout = get_or_prepare_layout()
    stems = _validated_stems(layout, req.chapters or [])
    if not stems:
        raise HTTPException(400, "请选择要混音的章节。")
    data = Bgm.load_assignments(layout)
    chapters = data.get("chapters") or {}
    lib_dir = core_paths.MUSIC_LIBRARY_DIR
    cfg = get_config()
    for s in stems:
        e = chapters.get(s)
        if not isinstance(e, dict):
            raise HTTPException(400, f"该章从未匹配，请先匹配（{s}）。")
        narration = Bgm._find_narration(layout, s)
        if narration is None:
            raise HTTPException(400,
                f"未找到旁白音频（06_audio_merge/{s}.mp3），请先完成音频合并。")
        m = e.get("music")
        if m and not (lib_dir / m).is_file():
            raise HTTPException(400,
                f"音乐库中找不到 {m}，请先到「音乐库」页检查或重新匹配（{s}）。")
        # 段落级时间轴的前置守卫——不让「⚠ 行」发起必败任务：时间轴缺失/过期
        #（旁白时长变化）/ span 曲目出库 → 400（引擎侧同守卫）。章节级匹配
        # 若只重置了 assignment.segment，但段落分析和时间轴仍有效，也继续认这份结果。
        tl = Bgm.load_segment_timeline(layout, s, e)
        if e.get("segment") or tl is not None:
            if tl is None:
                raise HTTPException(
                    400, f"该章没有时间轴，请重新进行段落分析（{s}）。")
            d, _perr = probe_duration(narration, cfg.ffmpeg.ffprobe_path)
            if (d > 0
                    and abs(d - float(tl.get("duration") or 0.0)) > Bgm._STALE_TOLERANCE_S):
                raise HTTPException(
                    400, f"时间轴已过期（旁白时长变化），请重新进行段落分析（{s}）。")
            for sp in tl.get("timeline") or []:
                mid = sp.get("music_id") or ""
                if not (lib_dir / mid).is_file():
                    raise HTTPException(
                        400,
                        f"音乐库中找不到 {mid}，请先到「音乐库」页检查或重新匹配（{s}）。")
    audio_conflicts = _durable_audio_conflicts(stems, ctx, db)
    if audio_conflicts:
        raise HTTPException(
            409, "以下章节音频合成或合并任务在途：" + "、".join(audio_conflicts)
        )
    active = active_durable_targets(task_type="bgm.mix", payload_key="stem", ctx=ctx, db=db)
    active_analyses = active_durable_targets(
        task_type="bgm.analysis", payload_key="stem", ctx=ctx, db=db,
    )
    active_segments = active_durable_targets(
        task_type="bgm.segment", payload_key="stem", ctx=ctx, db=db,
    )
    active_matches = active_durable_targets(
        task_type="bgm.match", payload_key="chapters", ctx=ctx, db=db,
    )
    conflicts = [
        stem for stem in stems
        if stem in active or stem in active_analyses or stem in active_segments or stem in active_matches
    ]
    if conflicts:
        raise HTTPException(409, "以下章节已有混音任务在途：" + "、".join(conflicts))
    created = []
    for stem in stems:
        task = submit_legacy_engine_task(
            task_type="bgm.mix",
            label=f"{MIX_LABEL}：{stem}",
            payload={"stem": stem, "config": cfg.model_dump(mode="json")},
            ctx=ctx,
            db=db,
            idempotency_prefix=f"bgm-mix:{stem}",
        )
        created.append({"stem": stem, "task_id": task["id"]})
    return {"task_ids": [item["task_id"] for item in created], "chapters": created}


@router.post("/package")
def package_mixed_audio(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    req: PackageRequest | None = None,
) -> dict:
    """Submit a ``bgm.package`` durable task that zips the selected finished
    chapter mixes into a source-named ZIP.

    The archive contains a top-level folder named after the source TXT stem,
    with one final ``.mp3`` per chapter.  The ZIP is published under
    ``08_bgm/`` so the file download endpoint can serve the result.
    """
    _common.require_workspace()
    layout = get_or_prepare_layout()
    if req is None or req.chapters is None:
        stems = Bgm.list_chapter_stems(layout)
    else:
        stems = _validated_stems(layout, req.chapters)
    if not stems:
        raise HTTPException(400, "未找到任何章节文件，无法打包下载。")
    stems = [s for s in stems if (layout.bgm / f"{s}.mp3").is_file()]
    if not stems:
        raise HTTPException(400, "选中的章节尚未完成混音，没有可下载的音频。")

    base = _source_txt_base(layout)
    task = submit_legacy_engine_task(
        task_type="bgm.package",
        label=f"BGM 打包：{base}",
        payload={"chapters": stems, "base": base},
        ctx=ctx,
        db=db,
        idempotency_prefix=f"bgm-package:{base}",
    )
    return {"task_id": task["id"]}


# --------------------------------------------------------------------------- #
# 时间轴（只读）：BGM 页「时间轴」查看弹层的数据源
# --------------------------------------------------------------------------- #

@router.get("/timeline/{stem}")
def get_timeline(stem: str) -> dict:
    """Return the chapter's timeline file (``08_bgm/timelines/<stem>.json``)
    verbatim: ``{"timeline": {...}}``.

    Guards: stem traversal 400; no workspace → ``{"timeline": null}``
    (degrade, like /chapters); file missing/corrupt → 404 with the actionable
    wording (the chapter needs a paragraph analysis + a segment match first).
    """
    layout = resolve_layout()
    if layout.bgm is None:
        return {"timeline": None}
    if not stem or stem != Path(stem).name:
        raise HTTPException(400, f"非法章节名：{stem!r}")
    tl = Bgm.load_timeline(layout, stem)
    if tl is None:
        raise HTTPException(404, "该章没有时间轴，请先完成段落分析。")
    return {"timeline": tl}
