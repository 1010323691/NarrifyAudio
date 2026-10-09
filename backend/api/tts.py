"""TTS + character-voice + batch + merge endpoints (modules: TTS / 角色配音 / 音频合成 / 音频合并).

``GET /status`` reports readiness. The stage endpoints (``POST /prepare-foundations``,
``POST /make-clones``, ``POST /batch``, ``POST /merge``) submit independent
durable tasks for the selected characters or chapters. Workers drive the isolated
Qwen3-TTS engine (see
``backend/engines/tts.py`` / ``voices.py`` / ``tts_batch.py`` / ``merge.py``) and
return ``{"task_ids": [...]}``; single-entry responses also carry ``task_id``.
The UI streams each task's progress/logs over SSE and plays
the resulting audio via the shared ``GET /api/files/download/05_audio_chunk/{name}``
route. Any failing task is marked failed and isolated — it never takes the console
down (requirement #7).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import threading
import time
from functools import wraps
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..core import pathio
from ..core.config import get_config
from ..core.bounded_json import read_json, is_script_data
from ..core.bounded_cache import BoundedCache
from ..core.file_lock import exclusive_file_lock
from ..core.paths import ALL_PARSED_JSON, get_or_prepare_layout, resolve_layout, resolve_parsed_json, resolve_parsed_json_all, merged_audio_filename
from ..engines.book import parse_chapter_number
from ..engines import bgm as Bgm
from ..engines import tts as T
from ..engines import tts_batch as Batch
from ..engines import voices as V
from ..core.role_hint_cache import cached_role_hints
from ..core.role_hints import collect_cooccurrence
from ..core.script_snapshot import capture_reference, source_version
from ..services.list_paging import entry_states, page_enriched, page_meta, page_slice
from ..services.chapter_display import chapter_display_name, chapter_source_path
from ..engines.audio import probe_duration
from ..engines.merge import boundary_gap_ms
from ..platform.merge_submission import submit_merge_tasks
from ..platform.database import get_db
from ..platform.deps import AuthContext, get_auth_context
from ..platform.engine_task_submission import (
    active_durable_payloads,
    active_durable_targets,
    has_active_durable_tasks,
    submit_legacy_engine_task,
    submit_legacy_engine_tasks,
    submit_engine_batch,
)
from ..platform.file_response import file_response
from . import _common

router = APIRouter(prefix="/api/tts", tags=["tts"])


@router.get("/status")
def status() -> dict:
    """Report the worker substrate without loading model packages or weights."""
    ready = T.READY
    if ready:
        try:
            T.resolve_engine()
        except (RuntimeError, OSError):
            ready = False
    return {
        # 线协议字段名 "implemented" 被前端与测试消费，本轮不改
        "implemented": T.READY,
        "ready": ready,
        "message": (
            "TTS 工作进程文件可用；模型依赖和权重将在任务启动时检查。"
            if ready else T.NOT_READY_MSG
        ),
    }


# ---------------------------------------------------------------------------
# 角色配音（voice preparation）
# ---------------------------------------------------------------------------

class PrepareFoundationsRequest(BaseModel):
    project_id: str | None = Field(default=None, max_length=36)
    # Phase 1 (LLM only): None -> every character; a list -> only those (single-char regen).
    speakers: list[str] | None = Field(default=None, max_length=1000)
    # True -> regenerate only characters without a foundation yet.
    new_only: bool = False
    # speaker -> user-supplied voice description (skips the LLM for that character).
    overrides: dict[str, str] | None = None
    # Which parsed JSON (in 03_parsed_json/) to read; None -> most recent (resolve_parsed_json).
    script: str | None = None


class MakeClonesRequest(BaseModel):
    project_id: str | None = Field(default=None, max_length=36)
    # Phase 2 (TTS only): None -> every foundation-bearing character; a list -> only those.
    speakers: list[str] | None = Field(default=None, max_length=1000)
    # True -> limit to characters not yet holding a usable clone (also retries failed ones).
    new_only: bool = False
    # 批内行数（上限，1..64，不是并发进程数）：单个长驻 design-batch 子进程内的 GPU 张量批
    # 上限；None -> config.tts.batch_concurrency。
    concurrency: int | None = None
    # Which parsed JSON to read for the character set; None -> most recent.
    script: str | None = None
    # Per-character clone-candidate count: None = auto (the absolute log-scale ladder on
    # each character's OWN line count — see voices.auto_candidate_count); else 2/4/6/8.
    candidate_count: int | None = None

    @field_validator("candidate_count")
    @classmethod
    def _check_candidate_count(cls, v):
        if v is not None and v not in (2, 4, 6, 8):
            raise ValueError("candidate_count 须为 null（自动）或 2/4/6/8 之一")
        return v


def _voice_task_speakers(script: str | None, speakers: list[str] | None,
                        new_only: bool, *, clone: bool = False,
                        candidate_count: int | None = None) -> list[str]:
    """Resolve the selection once; each durable row names a real character."""
    rows = list_voices(script)["speakers"]
    allow = set(speakers) if speakers else None
    counts = {row["name"]: row["line_count"] for row in rows}
    targets = []
    for row in rows:
        name = row["name"]
        if allow is not None and name not in allow:
            continue
        if clone:
            if row["foundation_status"] != "done":
                continue
            target = candidate_count or V.auto_candidate_count(counts[name])
            if new_only and len(row["candidates"]) >= target:
                continue
        elif new_only and row["foundation_status"] == "done":
            continue
        targets.append(name)
    return targets


@router.post("/prepare-foundations")
def prepare_foundations(
    req: PrepareFoundationsRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=180)] = None,
) -> dict:
    """Submit one foundation task per selected character."""
    _common.require_workspace()
    return submit_engine_batch(task_type="voices.foundation", request=req.model_dump(),
        prepare=lambda: _prepare_voice_foundations(req), ctx=ctx, db=db, idempotency_key=idempotency_key, project_id=req.project_id)


def _prepare_voice_foundations(req):
    script = req.script or resolve_parsed_json(None).name
    paths = resolve_parsed_json_all() if script == ALL_PARSED_JSON else [resolve_parsed_json(script)]
    reference = capture_reference(paths, get_or_prepare_layout().workspace, script)
    targets = _voice_task_speakers(script, req.speakers, req.new_only)
    if source_version(paths)[0] != reference["version"]:
        raise HTTPException(409, "剧本在提交期间发生变化，请重新选择角色。")
    config = get_config().model_dump(mode="json")
    config["_script_inputs"] = reference
    return ([{
            "label": f"语音推理基础 · {speaker}",
            "payload": {"speakers": [speaker], "new_only": req.new_only,
                        "overrides": {speaker: req.overrides[speaker]} if req.overrides and speaker in req.overrides else {},
                        "script": script},
        } for speaker in targets], config)


@router.post("/make-clones")
def make_clones(
    req: MakeClonesRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=180)] = None,
) -> dict:
    """Submit one clone task per selected foundation-bearing character."""
    _common.require_workspace()
    return submit_engine_batch(task_type="voices.clone", request=req.model_dump(),
        prepare=lambda: _prepare_voice_clones(req), ctx=ctx, db=db, idempotency_key=idempotency_key, project_id=req.project_id)


def _prepare_voice_clones(req):
    script = req.script or resolve_parsed_json(None).name
    paths = resolve_parsed_json_all() if script == ALL_PARSED_JSON else [resolve_parsed_json(script)]
    reference = capture_reference(paths, get_or_prepare_layout().workspace, script)
    targets = _voice_task_speakers(script, req.speakers, req.new_only,
                                   clone=True, candidate_count=req.candidate_count)
    if source_version(paths)[0] != reference["version"]:
        raise HTTPException(409, "剧本在提交期间发生变化，请重新选择角色。")
    config = get_config().model_dump(mode="json")
    config["_script_inputs"] = reference
    return ([{
            "label": f"克隆音频 · {speaker}",
            "payload": {"speakers": [speaker], "new_only": req.new_only,
                        "concurrency": req.concurrency, "script": script,
                        "candidate_count": req.candidate_count},
        } for speaker in targets], config)


def _voice_usable(entry: dict) -> bool:
    """Whether a stored voice entry actually yields a usable voice at synthesis time."""
    vtype = entry.get("type", "")
    if vtype == "clone":
        return bool(entry.get("ref_audio"))
    if vtype == "design":
        return bool((entry.get("description") or "").strip())
    if vtype == "custom":
        return True  # uses a named preset / default voice
    return False


def _foundation_status(entry: dict) -> str:
    """The character's voice-foundation state: ``none`` | ``done`` | ``failed``.

    Prefers the explicit ``foundation_status`` written by Phase 1; otherwise infers it so
    entries created before the two-phase split (a stored description ⇒ a foundation exists)
    still report sensibly.
    """
    entry = entry or {}
    st = entry.get("foundation_status")
    if st in ("done", "failed"):
        return st
    if (entry.get("description") or "").strip():
        return "done"
    return "none"


def _clone_status(entry: dict) -> str:
    """The character's clone-audio state: ``none`` | ``done`` | ``failed``.

    Prefers the explicit ``clone_status`` written by Phase 2; otherwise a stored
    ``type: clone`` + ``ref_audio`` (a usable clone seed) infers ``done``.
    """
    st = entry.get("clone_status")
    if st in ("done", "failed"):
        return st
    if entry.get("type") == "clone" and entry.get("ref_audio"):
        return "done"
    return "none"


def _fold_script(order: list[str], counts: dict[str, int], data, pairs: dict | None = None) -> bool:
    """Fold one parsed script (a list of entries) into the shared ``order``/``counts``.

    Dedupes by speaker name (falling back to ``type``), sums line counts, and keeps
    first-appearance order — the same folding the single-file path did inline, now shared
    with the whole-book aggregate. Returns True when the file held a non-empty list (the
    ``has_script`` signal).
    """
    if not isinstance(data, list) or not data:
        return False
    sequence = []
    for entry in data:
        sp = (entry.get("speaker") or entry.get("type") or "").strip()
        if not sp:
            continue
        if sp not in counts:
            counts[sp] = 0
            order.append(sp)
        counts[sp] += 1
        sequence.append(sp)
    if pairs is not None:
        collect_cooccurrence(sequence, pairs)
    return True


_SPEAKER_CACHE = BoundedCache(64 * 1024 * 1024, 2048)
_SPEAKER_CACHE_LOCK = threading.Lock()


def _script_speakers(path):
    try:
        stat = path.stat()
    except OSError:
        return False, [], {}, {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _SPEAKER_CACHE_LOCK:
        if key in _SPEAKER_CACHE:
            _SPEAKER_CACHE.move_to_end(key)
            return _SPEAKER_CACHE[key]
    order, counts, pairs = [], {}, {}
    try:
        has_script = _fold_script(order, counts, read_json(path), pairs)
    except (OSError, ValueError, UnicodeError):
        return False, [], {}, {}
    value = (has_script, order, counts, pairs)
    with _SPEAKER_CACHE_LOCK:
        _SPEAKER_CACHE[key] = value
        while len(_SPEAKER_CACHE) > 2048: _SPEAKER_CACHE.popitem(last=False)
    return value


_SUMMARY_CACHE = BoundedCache(8 * 1024 * 1024, 32)
_SUMMARY_LOCK = threading.Lock()


def _cache_voice_summary(function):
    @wraps(function)
    def cached(*args, **kwargs):
        if not kwargs.get("summary_only"):
            return function(*args, **kwargs)
        layout = resolve_layout()
        if layout.parsed_json is None:
            return function(*args, **kwargs)
        script = kwargs.get("script", args[0] if args else None)
        paths = resolve_parsed_json_all() if script == ALL_PARSED_JSON else [resolve_parsed_json(script)]
        paths = [*paths, layout.voice_profiles / "voice_config.json"]
        fingerprints = []
        for path in paths:
            try:
                stat = path.stat()
                fingerprints.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                fingerprints.append((str(path), None, None))
        key = (str(layout.workspace), tuple(fingerprints), repr(args), repr(sorted(kwargs.items())))
        # Coalesce simultaneous summary polling. The cache contains counts only,
        # never script text, config credentials or complete role inventories.
        with _SUMMARY_LOCK:
            hit = _SUMMARY_CACHE.get(key)
            if hit and time.monotonic() - hit[0] < 30:
                _SUMMARY_CACHE.move_to_end(key)
                return hit[1]
            value = function(*args, **kwargs)
            _SUMMARY_CACHE[key] = (time.monotonic(), value)
            while len(_SUMMARY_CACHE) > 32:
                _SUMMARY_CACHE.popitem(last=False)
            return value
    return cached


@router.get("/voices")
@_cache_voice_summary
def list_voices(script: str | None = None, page: Annotated[int | None, Query(ge=1)] = None,
                page_size: Annotated[int, Query(ge=1, le=100)] = 10,
                q: str = "", filter: str = "all", summary_only: bool = False, keys_only: bool = False) -> dict:
    """Detected characters + their voice-config state (ready/pending) + preview paths.

    ``script`` names the parsed JSON (in ``03_parsed_json/``) to read; when omitted the
    most recently written one is used (see ``resolve_parsed_json``). With no workspace
    set it degrades to an empty result. ``preview`` is a path relative to
    ``04_voice_profiles/`` so the UI can play it through the shared
    ``download/04_voice_profiles/{name}`` route; empty when there is nothing to preview.
    ``speakers`` is sorted by ``line_count`` descending (stable — ties keep
    first-appearance order), so the leads top the page list.
    """
    layout = resolve_layout()
    if layout.parsed_json is None:  # no workspace: nothing to read (read-only, degrades)
        return {"has_script": False, "script_path": "", "voice_config_path": "", "speakers": []}
    out_voices = layout.voice_profiles
    vc_path = out_voices / "voice_config.json"

    # Which script(s) to read: every parsed JSON (the whole-book "all files" request) or
    # the single named / most-recent one. ``script_path_out`` is only a display label.
    if script == ALL_PARSED_JSON:
        script_paths = resolve_parsed_json_all()
        script_path_out = ""
    else:
        script_paths = [resolve_parsed_json(script)]
        script_path_out = str(script_paths[0])

    has_script = False
    order: list[str] = []
    counts: dict[str, int] = {}
    cooccur: dict = {}
    for sp in script_paths:
        found, file_order, file_counts, file_pairs = _script_speakers(sp)
        has_script = has_script or found
        for pair, n in file_pairs.items():
            cooccur[pair] = cooccur.get(pair, 0) + n
        for name in file_order:
            if name not in counts: order.append(name)
            counts[name] = counts.get(name, 0) + file_counts[name]

    voice_config: dict = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception:  # noqa: BLE001
            voice_config = {}
        # Lazy migration of legacy absolute ref_audio values (the file is rewritten in the
        # workspace-relative form on first read after the upgrade).
        _n, migrated = pathio.migrate_entries_in(vc_path, layout.workspace, "dict", ("ref_audio",))
        if migrated is not None:
            voice_config = migrated

    # Display order: line count descending (leads first, cameos last) — a stable sort, so
    # ties keep first-appearance (or voice_config) order.
    names = sorted(order if has_script else list(voice_config.keys()),
                   key=lambda sp: -counts.get(sp, 0))
    hints = cached_role_hints(names, voice_config, counts, layout.temp / "role-hints", cooccur)

    ready_names = {n for n in names if _voice_ready(n, voice_config)}
    counts_out = {"all": len(names), "ready": len(ready_names), "pending": len(names) - len(ready_names),
                  "non_alias": len(names),
                  "foundation": sum(_foundation_status(voice_config.get(n, {})) == "done" for n in names),
                  "clone": sum(_clone_status(voice_config.get(n, {})) == "done" for n in names)}
    matching = [n for n in names if (filter == "all" or n not in ready_names) and
                (not q.strip() or q.strip().casefold() in (n + " " + hints.get(n, "")).casefold())]
    pagination = page_meta(len(matching), page or 1, page_size, counts_out)
    if keys_only:
        return {"has_script": has_script, "script_path": script_path_out, "voice_config_path": str(vc_path),
                "speakers": [{"name": n, "line_count": counts.get(n, 0), "status": "ready" if n in ready_names else "pending", "alias_of": hints.get(n, "")} for n in (page_slice(matching, page, page_size) if page is not None else matching)], "pagination": pagination}
    names = [] if summary_only else page_slice(matching, page, page_size) if page is not None else matching

    def _preview_of(ref: str) -> str:
        """A stored reference-audio path, relative to 04_voice_profiles/ (the UI plays it
        through the shared download route); '' when there is nothing to preview."""
        if not ref:
            return ""
        # Resolved against the current workspace root (relative form; a legacy absolute
        # value still works) so the preview keeps working after the workspace moves.
        try:
            p = pathio.resolve_path(ref, layout.workspace, strict=False)
        except pathio.PathOutsideWorkspace:
            p = None
        if p is None or not p.exists():
            return ""
        try:
            return str(p.relative_to(out_voices)).replace(os.sep, "/")
        except ValueError:
            return str(p)

    speakers: list[dict] = []
    for sp in names:
        entry = voice_config.get(sp, {})
        vtype = entry.get("type", "")
        alias_of = hints.get(sp, "")
        ready = _voice_ready(sp, voice_config)
        # Every clone candidate the character has (legacy entries synthesise one from
        # their clone reference), plus the user's pick (None = the default first one).
        cands = V.effective_candidates(entry)
        speakers.append({
            "name": sp,
            "line_count": counts.get(sp, 0),
            "status": "ready" if ready else "pending",
            "foundation_status": _foundation_status(entry),
            "clone_status": _clone_status(entry),
            "type": vtype,
            "alias_of": alias_of,
            # Gender badge: "male" / "female" / "" (unknown). Pre-filled by Phase 1's
            # persona inference; the user's badge pick is the source of truth.
            "gender": entry.get("gender", ""),
            "description": entry.get("description", ""),
            "preview": _preview_of(entry.get("ref_audio", "")),
            "candidates": [{"id": c["id"], "preview": _preview_of(c["ref_audio"]), "seed": c["seed"]}
                           for c in cands],
            "selected_audio_id": entry.get("selected_audio_id") or None,
        })

    return {
        "has_script": has_script,
        "script_path": script_path_out,
        "voice_config_path": str(vc_path),
        "speakers": speakers,
        **({"pagination": pagination} if page is not None or summary_only else {}),
    }


class SelectVoiceRequest(BaseModel):
    speaker: str
    # Candidate id to make active; None/empty = clear the pick (the default first
    # candidate becomes active again).
    audio_id: str | None = None


def _phase_task_active(ctx: AuthContext | None, db: Session | None) -> bool:
    """Whether a voices-foundation / voices-clone task is in flight. Both rewrite
    ``voice_config.json`` as a whole file, so a concurrent selection write could be
    clobbered (or vice versa) — refuse the write while one runs."""
    if not isinstance(ctx, AuthContext) or not isinstance(db, Session):
        return False
    for task_type in ("voices.foundation", "voices.clone"):
        if has_active_durable_tasks(task_type=task_type, ctx=ctx, db=db):
            return True
    return False


@router.put("/voices/select")
def select_voice(
    req: SelectVoiceRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Record the user's clone-candidate pick for a character.

    Synchronous (no Task): the chosen candidate becomes the ACTIVE clone reference —
    ``selected_audio_id`` plus the top-level ``ref_audio`` are updated together in one
    file rewrite, so every downstream stage (音频合成 / worker) uses the picked take.
    """
    _common.require_workspace()
    if _phase_task_active(ctx, db):
        raise HTTPException(409, "配音任务进行中，请待其结束后再选择音色。")
    layout = get_or_prepare_layout()
    vc_path = layout.voice_profiles / "voice_config.json"
    if not vc_path.exists():
        raise HTTPException(404, "未找到声音配置，请先运行阶段 1 / 阶段 2。")
    try:
        voice_config = json.loads(vc_path.read_text("utf-8"))
        if not isinstance(voice_config, dict):
            raise ValueError
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "声音配置已损坏，无法更新选择。")
    # Lazy migration of legacy absolute ref_audio values (same as the other read paths).
    _n, migrated = pathio.migrate_entries_in(vc_path, layout.workspace, "dict", ("ref_audio",))
    if isinstance(migrated, dict):
        voice_config = migrated
    entry = voice_config.get(req.speaker)
    if not isinstance(entry, dict):
        raise HTTPException(404, f"声音配置中没有角色：{req.speaker}")
    cands = V.effective_candidates(entry)
    if not cands:
        raise HTTPException(400, "该角色没有可选择的候选音频，请先在阶段 2 生成。")
    aid = (req.audio_id or "").strip() or None
    chosen = None
    if aid is not None:
        chosen = next((c for c in cands if c["id"] == aid), None)
        if chosen is None:
            raise HTTPException(400, f"无效的候选编号：{aid}")
    active = chosen or cands[0]
    entry["selected_audio_id"] = aid
    # Keep the active reference in sync so downstream synthesis uses the picked take.
    entry["ref_audio"] = active["ref_audio"]
    pathio.rewrite_json_file(vc_path, voice_config)
    Batch.invalidate_speaker_outputs([req.speaker], layout)
    return {"ok": True, "speaker": req.speaker, "selected_audio_id": aid,
            "ref_audio": active["ref_audio"]}


class SetGenderRequest(BaseModel):
    speaker: str
    # "male" / "female" — set the badge; "" / None = clear it (back to unknown).
    gender: str = ""


@router.post("/voices/gender")
def set_gender(
    req: SetGenderRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Record the user's gender pick for a character (the badge next to the name).

    Synchronous (no Task): upserts ``gender`` on the character's ``voice_config.json``
    entry — a character without any entry yet gets a minimal one (script-detected names
    are valid targets too). A running phase task is refused (409): it rewrites the whole
    file and would clobber the pick, same guard as the candidate selection.
    """
    _common.require_workspace()
    if _phase_task_active(ctx, db):
        raise HTTPException(409, "配音任务进行中，请待其结束后再设置性别。")
    sp = (req.speaker or "").strip()
    if not sp:
        raise HTTPException(400, "角色名不能为空。")
    g = (req.gender or "").strip()
    if g not in ("male", "female", ""):
        raise HTTPException(400, "无效的性别值。")
    layout = get_or_prepare_layout()
    vc_path = layout.voice_profiles / "voice_config.json"
    voice_config: dict = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError
            voice_config = loaded
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "声音配置已损坏，无法设置性别。")
        _n, migrated = pathio.migrate_entries_in(vc_path, layout.workspace, "dict", ("ref_audio",))
        if isinstance(migrated, dict):
            voice_config = migrated
    entry = voice_config.get(sp)
    if not isinstance(entry, dict):
        entry = {}
    if g:
        entry["gender"] = g
    else:
        entry.pop("gender", None)
    voice_config[sp] = entry
    vc_path.parent.mkdir(parents=True, exist_ok=True)
    pathio.rewrite_json_file(vc_path, voice_config)
    return {"ok": True, "speaker": sp, "gender": g}


class MergeSpeakersRequest(BaseModel):
    # The character being merged away (its lines + voice-config entry disappear).
    source: str
    # The character every source line is re-assigned to.
    target: str
    # Same semantics as ``list_voices``' script: None/'' -> most recent base file; a file
    # name -> only that file; "``__all__``" -> every base file (the whole-book view).
    script: str | None = None


@router.post("/voices/merge-speakers")
def merge_speakers(
    req: MergeSpeakersRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Merge one character into another by rewriting the parsed source data in place.

    Synchronous (no Task): pure deterministic JSON surgery — every entry whose identity
    is ``source`` (``speaker`` first, ``type`` as fallback — the ``_fold_script`` rule)
    is re-assigned to ``target`` directly in ``03_parsed_json/*.json`` (no LLM call), the
    ``source`` entry is removed from ``voice_config.json`` (its candidate WAVs stay on
    disk — user data is never deleted), and aliases pointing at ``source`` are
    redirected to ``target``. Only files that actually changed are rewritten (a rewrite
    refreshes mtime, which would perturb the most-recent-file / ``__all__`` ordering).
    """
    _common.require_workspace()
    if _phase_task_active(ctx, db):
        raise HTTPException(409, "配音任务进行中，请待其结束后再合并角色。")
    src = (req.source or "").strip()
    tgt = (req.target or "").strip()
    if not src or not tgt:
        raise HTTPException(400, "角色名不能为空。")
    if src == tgt:
        raise HTTPException(400, "源角色与目标角色相同。")
    layout = get_or_prepare_layout()

    # Which script(s) to read/rewrite (mirrors list_voices' scope resolution).
    if req.script == ALL_PARSED_JSON:
        script_paths = [p for p in resolve_parsed_json_all() if p.exists()]
    else:
        script_paths = [resolve_parsed_json(req.script)]

    # voice_config: absent is legal (nothing to clean up); corrupt is not.
    vc_path = layout.voice_profiles / "voice_config.json"
    voice_config: dict = {}
    vc_exists = False
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
                vc_exists = True
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "声音配置已损坏，无法合并角色。")
        # Lazy migration of legacy absolute ref_audio values (same as the other paths).
        _n, migrated = pathio.migrate_entries_in(vc_path, layout.workspace, "dict", ("ref_audio",))
        if isinstance(migrated, dict):
            voice_config = migrated

    # Collect the identity set across the scope files. A corrupt file is a hard error on
    # this write endpoint (silently skipping it would leave the file unmerged).
    file_speakers: set[str] = set()
    for sp in script_paths:
        if not sp.exists():
            continue
        try:
            data = json.loads(sp.read_text("utf-8"))
            if not isinstance(data, list):
                raise ValueError
        except Exception:  # noqa: BLE001
            raise HTTPException(400, f"解析文件已损坏，无法合并：{sp.name}")
        for e in data:
            if isinstance(e, dict):
                name = (e.get("speaker") or e.get("type") or "").strip()
                if name:
                    file_speakers.add(name)

    if src not in file_speakers and src not in voice_config:
        raise HTTPException(404, f"未找到角色：{src}")
    if tgt not in file_speakers and tgt not in voice_config:
        raise HTTPException(400, f"目标角色不在当前范围：{tgt}")

    # Rewrite the parsed source data: replace the source identity file by file.
    replaced = 0
    changed_files: list[str] = []
    for sp in script_paths:
        if not sp.exists():
            continue
        data = json.loads(sp.read_text("utf-8"))  # validity already checked above
        changed = 0
        for e in data:
            if not isinstance(e, dict):
                continue
            spk = (e.get("speaker") or "").strip()
            tp = (e.get("type") or "").strip()
            if spk == src:
                e["speaker"] = tgt
                changed += 1
            elif not spk and tp == src:
                # ``type`` only stands in for the identity when ``speaker`` is absent.
                e["type"] = tgt
                changed += 1
        if changed:
            pathio.rewrite_json_file(sp, data)
            changed_files.append(sp.name)
            replaced += changed

    # Sync voice_config: drop the merged-away entry; re-point aliases at the target.
    if vc_exists:
        voice_config.pop(src, None)
        for name, e in voice_config.items():
            if not isinstance(e, dict):
                continue
            if e.get("alias_of") == src:
                e["alias_of"] = tgt
            if e.get("alias") == src:  # legacy field (still recognised downstream)
                e["alias"] = tgt
        pathio.rewrite_json_file(vc_path, voice_config)

    # The source lines now use the target voice.  Mark both identities stale so an
    # already-complete package cannot silently retain audio rendered before this edit.
    Batch.invalidate_speaker_outputs([src, tgt], layout)

    return {"ok": True, "source": src, "target": tgt, "replaced": replaced, "files": changed_files}


# ---------------------------------------------------------------------------
# 音频合成 + 音频合并
# ---------------------------------------------------------------------------

class BatchRequest(BaseModel):
    project_id: str | None = None
    # None -> every script line; a list of line indices -> only those (single file only).
    indices: list[int] | None = None
    # Which parsed JSON (in 03_parsed_json/) to synthesize; None -> most recent.
    script: str | None = None
    # Multi-file run (the 待合成 card's selection): parsed-JSON file names, synthesized
    # as independent durable tasks (each file = its own package).
    # Takes precedence over ``script``; an empty list falls back to ``script`` / most recent.
    scripts: list[str] | None = Field(default=None, max_length=500)


@router.post("/batch")
def run_batch(
    req: BatchRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(max_length=180)] = None,
) -> dict:
    _common.require_workspace()
    if req.script == ALL_PARSED_JSON or any(s == ALL_PARSED_JSON for s in (req.scripts or [])):
        raise HTTPException(status_code=400, detail="音频合成仅支持逐个解析 JSON（“全部”只用于「角色配音」）。")
    scripts = req.scripts or ([req.script] if req.script else [])
    if len(scripts) > 1 and req.indices:
        raise HTTPException(status_code=400, detail="按段选择（indices）仅支持单个文件。")
    scripts = list(dict.fromkeys(scripts))
    if not scripts:
        scripts = [resolve_parsed_json(None).name]
    config = get_config().model_dump(mode="json")
    label = f"音频合成（{len(req.indices)} 段）" if req.indices else "音频合成"
    return submit_legacy_engine_tasks(
        task_type="tts.batch",
        entries=[{
            "label": f"{label} · {script}",
            "payload": {"indices": req.indices, "script": script,
                        "scripts": [script], "config": config},
        } for script in scripts],
        ctx=ctx, db=db, idempotency_prefix="tts-batch", idempotency_key=idempotency_key, project_id=req.project_id,
    )


class ResetBatchRequest(BaseModel):
    project_id: str | None = None
    # Parsed-JSON file names (03_parsed_json/). Each one's synthesis package — the folder
    # ``05_audio_chunk/<包名>/`` with its mp3s and manifest.json — is deleted, so the
    # following ordinary run (default resume) re-synthesizes every segment.
    scripts: list[str] = Field(max_length=500)




@router.post("/batch-reset")
def reset_batch(
    req: ResetBatchRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(max_length=180)] = None,
) -> dict:
    """Queue package removal before the ordinary synthesis task recreates each segment.

    The task deletes the selected parsed JSON files' synthesis packages from
    ``05_audio_chunk/<包名>/``; the following resume-style synthesis then
    regenerates all segments.

    Deleting generated, regenerable output is the app's only deliberate delete path —
    user-initiated here, and confined by construction to ``05_audio_chunk/<包名>/``: the
    package name is the file name's stem minus ``_checked`` (never a separator), so it can
    not escape the directory. Guards: no workspace 409 (write guard) -> ``__all__`` /
    empty list 400 -> in-flight tts-batch task 409 (its engine is writing those folders).
    """
    _common.require_workspace()
    if any(s == ALL_PARSED_JSON for s in req.scripts):
        raise HTTPException(status_code=400, detail="“全部文件”只用于「角色配音」——请逐个列出解析 JSON。")
    if not req.scripts:
        raise HTTPException(status_code=400, detail="没有要重置的文件。")
    task = submit_legacy_engine_task(
        task_type="tts.reset",
        label=f"重新合成：{len(req.scripts)} 个文件",
        payload={"scripts": req.scripts},
        ctx=ctx,
        db=db,
        idempotency_prefix="tts-reset",
        idempotency_key=idempotency_key,
        project_id=req.project_id,
    )
    return {"task_id": task["id"]}


def _read_voice_config(layout) -> dict:
    """The workspace ``voice_config.json`` as a dict (``{}`` when absent / corrupt) — a read-only
    snapshot (no lazy migration: this endpoint is polled, so it must not write)."""
    vc = layout.voice_profiles / "voice_config.json"
    if not vc.exists():
        return {}
    try:
        loaded = json.loads(vc.read_text("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _voice_ready(speaker: str, voice_config: dict) -> bool:
    return _voice_usable(voice_config.get(speaker) or {})


def _expected_voice_params(layout, segments, voice_config: dict):
    """Expected per-segment voice JSON, or ``None`` for legacy workspaces."""
    vc_path = layout.voice_profiles / "voice_config.json"
    if not vc_path.exists():
        return None
    return Batch.segment_voice_params(segments, voice_config)


# -- per-package batch-status digest cache --------------------------------------
# The 待合成 list is polled every 3 s with ALL rows in ONE request, and each row is a pure
# function of the four files behind it — so cache the computed row keyed by their
# ``(mtime_ns, size)``: the source parsed JSON (rewritten only by re-parsing), the package
# manifest (a live run rewrites it at least every 2 s), the package dir (NTFS bumps a
# directory's mtime on any child add/remove — a new mp3, a user-deleted one, a batch-reset
# rmtree), and voice_config.json (rewritten per character by the 角色配音 stages). The
# workspace string in the key keeps two workspaces with identical package names/stats from
# sharing entries. A hit returns a copy (the fresh-dict contract); a miss computes the row
# exactly as before (the state judgment is unchanged) and pays it only once per real change.
_STATUS_CACHE = BoundedCache(32 * 1024 * 1024, 512)
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE_MAX = 512


def reset_batch_status_cache() -> None:
    """Drop the per-package batch-status cache (a test / debug seam — the file stats already
    invalidate on every real change). Mirrors ``core.config.reset_config_cache``."""
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE.clear()
    with _MERGE_STATUS_CACHE_LOCK:
        _MERGE_STATUS_CACHE.clear()
        _MERGE_INPUT_CACHE.clear()


def _stat_key(path) -> tuple | None:
    """A path's ``(mtime_ns, size)``, or ``None`` when it cannot be stat'ed (a file that
    vanishes between keying and computing degrades to a miss on the next poll, never a crash)."""
    try:
        st = Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _chapter_source_path(name: str, layout) -> Path | None:
    if not name or Path(name).name != name:
        return None
    return chapter_source_path(Path(name).stem, layout)


def _chapter_display_name(name: str, layout) -> str:
    return chapter_display_name(Path(name).stem, layout) if Path(name).name == name else Path(name).stem


def _chapter_completion(segments, manifest, package_dir, expected_params):
    """Shared source-based completion rule for synthesis and merge readiness."""
    return Batch.count_completion(segments, manifest, out_dir=package_dir, expected_voice_params=expected_params)


def _file_batch_status(name: str, layout, voice_config: dict, out_dir: Path | None = None) -> dict:
    """One row of the multi-file 待合成 list (``GET /batch-status?scripts=…``).

    Per file: segment completion (same rule as the single-file endpoint — ``completed`` = a
    manifest entry ``ok`` whose file is still on disk) plus character readiness (the same
    ready rule as ``/voices``: the role's own usable voice entry). ``complete`` marks a file
    whose every synthesizable segment is done (the 已合成 badge; a file with no synthesizable
    segments never gets it). Degrades to a zero row when the file is missing / corrupt /
    empty, or no workspace is set. ``is_script`` distinguishes script arrays
    (including empty ones) from analysis reports and unreadable files.

    ``out_dir`` (the package dir) switches the completion count to the batched
    ``Batch.count_completion(out_dir=…)`` existence check; omitted, the per-entry rule runs.
    """
    out = {"name": name, "display_name": _chapter_display_name(name, layout),
           "total": 0, "completed": 0, "remaining": 0,
           "complete": False, "speakers": 0, "ready": 0, "missing": [],
           "is_script": False}
    src = resolve_parsed_json(name)
    if not src.exists():
        return out
    try:
        data = read_json(src)
    except Exception:  # noqa: BLE001 — a corrupt / empty script just reports zeros
        return out
    # Legacy book-analysis reports share this directory with scripts. Identify
    # scripts by their contents, not by a filename suffix or their segment count.
    if not is_script_data(data):
        return out
    out["is_script"] = True
    if not data:
        return out
    segs = Batch.build_segments(data)
    pkg_dir = out_dir or layout.audio_chunk / Batch.package_for(src)
    expected_params = _expected_voice_params(layout, segs, voice_config)
    manifest = Batch.read_manifest(pkg_dir, voice_config=voice_config)
    c = _chapter_completion(segs, manifest, pkg_dir, expected_params)
    out["total"], out["completed"], out["remaining"] = c["total"], c["completed"], c["remaining"]
    out["complete"] = c["total"] > 0 and c["completed"] == c["total"]
    order: list[str] = []
    _fold_script(order, {}, data)  # distinct speakers, first-appearance order (incl. NARRATOR)
    ready = [sp for sp in order if _voice_ready(sp, voice_config)]
    out["speakers"] = len(order)
    out["ready"] = len(ready)
    out["missing"] = [sp for sp in order if sp not in set(ready)]
    if expected_params is not None:
        stale = sorted({
            s["speaker"] for s in segs
            if (manifest.get(s["index"]) or {}).get("ok")
            and manifest[s["index"]].get("voice_used") != expected_params.get(s["index"])
        })
        if stale:
            out["stale_speakers"] = stale
    return out


def _cached_file_batch_status(name: str, layout, voice_config: dict) -> dict:
    """``_file_batch_status`` with the (mtime_ns, size)-keyed digest cache (see module note)."""
    if layout.audio_chunk is None:  # no workspace: the row degrades to zeros — nothing to key
        return _file_batch_status(name, layout, voice_config)
    src = resolve_parsed_json(name)
    pkg_dir = layout.audio_chunk / Batch.package_for(src)
    chapter_source = _chapter_source_path(name, layout)
    key = (str(layout.workspace) or "", name,
           _stat_key(src),
           _stat_key(chapter_source) if chapter_source is not None else None,
           _stat_key(pkg_dir / "manifest.json"),
           _stat_key(pkg_dir),
           _stat_key(layout.voice_profiles / "voice_config.json"))
    with _STATUS_CACHE_LOCK:
        row = _STATUS_CACHE.get(key)
        if row is not None:
            return {**row, "missing": list(row["missing"])}
    row = _file_batch_status(name, layout, voice_config, out_dir=pkg_dir)
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE[key] = row
        if len(_STATUS_CACHE) > _STATUS_CACHE_MAX:  # evict the oldest inserted (dict order)
            _STATUS_CACHE.pop(next(iter(_STATUS_CACHE)))
    # A MISS returns a copy too: the stored row must stay pristine (the pre-cache contract
    # was a fresh dict per call — a caller mutating the first row must not poison the cache).
    return {**row, "missing": list(row["missing"])}


class BatchStatusRequest(BaseModel):
    scripts: list[str] = Field(max_length=100)


@router.post("/batch-status")
def batch_status_files(req: BatchStatusRequest) -> dict:
    """Read multi-file progress without putting an entire book in the URL."""
    return batch_status(scripts=req.scripts) if req.scripts else {"files": []}


# ``scripts`` MUST be declared as a QUERY param: in this FastAPI version a bare
# ``list[...]`` default is treated as a JSON request body, and the repeated ``?scripts=``
# params are silently ignored (the 待合成 rows would all stay zero). ``Annotated`` keeps
# the plain ``None`` default, so direct (test) calls still work without going through FastAPI.
@router.post("/batch-list")
@router.get("/batch-list")
def batch_list(page: Annotated[int, Query(ge=1)] = 1,
               page_size: Annotated[int, Query(ge=1, le=100)] = 10,
               q: str = "", filter: str = "all", keys_only: bool = False,
               ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db),
               selected: Annotated[list[str] | None, Body(max_length=10000)] = None) -> dict:
    layout = resolve_layout()
    def order(name):
        match = re.search(r"第\s*([0-9一二三四五六七八九十百千零两]+)\s*章", name)
        number = parse_chapter_number(match.group(1)) if match else None
        return (number if number is not None else float("inf"), name)
    names = sorted((p.name for p in resolve_parsed_json_all()), key=order) if layout.parsed_json is not None else []
    vc = _read_voice_config(layout) if names else {}
    states = entry_states(db, ctx, ["tts.batch"])
    def state(row):
        if states.get(row["name"]) in {"active", "failed"}:
            return states[row["name"]]
        return "done" if row.get("complete") else "blocked" if row.get("missing") else "stale" if row.get("stale_speakers") else "pending"
    def enrich(name):
        row = _cached_file_batch_status(name, layout, vc)
        stem = Path(name).stem
        return {**row, "merged": (layout.audio_merge / merged_audio_filename(stem)).is_file(), "mixed": (layout.bgm / (stem + ".mp3")).is_file()}
    result = page_enriched(names, enrich, state,
                           page, page_size, q, filter, keys_only)
    result["files"] = result.pop("items")
    available = set(names)
    result["missing_selected"] = [n for n in selected or [] if n not in available]
    return result


@router.post("/merge-list")
@router.get("/merge-list")
def merge_list(page: Annotated[int, Query(ge=1)] = 1,
               page_size: Annotated[int, Query(ge=1, le=100)] = 10,
               q: str = "", filter: str = "all", keys_only: bool = False,
               ctx: AuthContext = Depends(get_auth_context), db: Session = Depends(get_db),
               selected: Annotated[list[str] | None, Body(max_length=10000)] = None) -> dict:
    started = time.monotonic()
    metrics = {"hits": 0, "misses": 0, "verify_ms": 0.0}
    layout = resolve_layout()
    names = sorted(p.name for p in layout.audio_chunk.iterdir() if p.is_dir()) if layout.audio_chunk and layout.audio_chunk.exists() else []
    voice_config = _read_voice_config(layout) if names else {}
    def enrich(name):
        row = _cached_package_merge_status(name, layout, voice_config, metrics)
        target = layout.audio_merge / merged_audio_filename(name)
        return {**row, "merged_filename": target.name if row.get("complete") and target.is_file() else None}
    states = entry_states(db, ctx, ["tts.merge"])
    result = page_enriched(names, enrich, lambda r: "running" if states.get(r["name"]) == "active" else "failed" if states.get(r["name"]) == "failed" else "done" if r["merged_filename"] else "ready" if r.get("complete") else "blocked", page, page_size, q, filter, keys_only)
    result["packages"] = result.pop("items")
    available = set(names)
    result["missing_selected"] = [n for n in selected or [] if n not in available]
    if keys_only:
        _merge_log.info("合并选择：匹配 %d 章 · 命中 %d · 核验 %d · 核验耗时 %.1fms · 总耗时 %.1fms",
                        len(result["packages"]), metrics["hits"], metrics["misses"], metrics["verify_ms"],
                        (time.monotonic() - started) * 1000)
    return result


@router.get("/batch-status")
def batch_status(script: str | None = None,
    scripts: Annotated[list[str] | None, Query(max_length=100)] = None) -> dict:
    """Synthesis progress of the chosen script(s).

    Single file (``?script=``; neither param -> the most recent one):
    ``{total, completed, remaining}``. ``completed`` counts segments already synthesized (a
    manifest entry ``ok`` whose file is still on disk) — the number behind the 待合成 list's
    per-row 【已合成 / 总段落】, refreshed live while a run streams (the manifest is written
    incrementally).

    Multi-file (repeated ``?scripts=`` params — the 待合成 list): ``{"files": [one row per
    name, in request order]}``; each row additionally carries ``complete`` (every segment
    done — the 已合成 badge), and ``speakers`` / ``ready`` / ``missing`` (character readiness,
    the same rule as ``/voices``). ``"__all__"`` is rejected (400), as in ``POST /batch``.
    Degrades to zeros with no workspace / no files, like ``/voices``.
    """
    layout = resolve_layout()
    if scripts:
        if any(s == ALL_PARSED_JSON for s in scripts):
            raise HTTPException(status_code=400, detail="“全部文件”只用于「角色配音」——请逐个列出解析 JSON。")
        vc = _read_voice_config(layout) if layout.parsed_json is not None else {}
        return {"files": [_cached_file_batch_status(n, layout, vc) for n in scripts]}
    if layout.parsed_json is None:  # no workspace: nothing to read (read-only, degrades)
        return {"total": 0, "completed": 0, "remaining": 0}
    src = resolve_parsed_json(script)
    if not src.exists():
        return {"total": 0, "completed": 0, "remaining": 0}
    try:
        data = json.loads(src.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt / empty script just reports nothing
        return {"total": 0, "completed": 0, "remaining": 0}
    if not isinstance(data, list) or not data:
        return {"total": 0, "completed": 0, "remaining": 0}
    out_dir = layout.audio_chunk / Batch.package_for(src)
    segs = Batch.build_segments(data)
    return Batch.count_completion(
        segs, Batch.read_manifest(out_dir), out_dir=out_dir,
        expected_voice_params=_expected_voice_params(
            layout, segs, _read_voice_config(layout),
        ),
    )


# ---------------------------------------------------------------------------
# Durable package merge task submission.
# cancel-batch 端点，取消 = 前端逐任务 control；壳被 cancel 就地终结后自然掉出投放搜索）
# ---------------------------------------------------------------------------

# Same prefetch invariant as the parse batch: tasks waiting for a merge slot stay ≤ 4.




class MergeRequest(BaseModel):
    packages: list[str] | None = Field(default=None, max_length=10000)
    project_id: str | None = None


@router.post("/merge")
def run_merge(
    req: MergeRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    """Submit one durable Worker task per selected merge package.

    Duplicate package names are collapsed in request order. A package already
    being merged in the current project is rejected so two workers cannot write
    the same output concurrently.
    """
    _common.require_workspace()
    pkgs = list(dict.fromkeys(req.packages or []))  # dedupe, preserving order
    if not pkgs:
        raise HTTPException(400, "请选择要合并的音频包。")
    for p in pkgs:
        if not p or p != Path(p).name:
            raise HTTPException(400, f"非法包名：{p}")
    return submit_merge_tasks(
        packages=pkgs, ctx=ctx, db=db, idempotency_key=idempotency_key,
        project_id=req.project_id, preflight=_merge_preflight,
    )


def _merge_preflight(packages):
    layout = resolve_layout()
    for package in packages:
        if _chapter_preview_lock_held(layout, package):
            raise HTTPException(409, f"章节 {package} 有保存操作进行中，请稍后再合并。")


def _package_merge_status(name: str, layout, voice_config: dict | None = None, *, probe=None) -> dict:
    """One row of the merge page's package list (``GET /merge-status?packages=…``).

    ``total`` = the source parsed JSON's synthesizable segment count — the same rule as
    ``/batch-status`` (a manifest-length total would mark a mid-cancelled package "ready"
    and silently merge a half book). Only when the source JSON is missing / corrupt /
    empty does the row degrade to the manifest length. ``completed`` = ok manifest entries
    whose file is still on disk (``Batch.is_done``); ``complete`` = every segment done —
    the 已就绪 badge.
    """
    out = {"name": name, "display_name": chapter_display_name(name, layout),
           "total": 0, "completed": 0, "remaining": 0, "complete": False}
    if voice_config is None:
        voice_config = _read_voice_config(layout)
    manifest = Batch.read_manifest(layout.audio_chunk / name, voice_config=voice_config)
    if probe is not None:
        # Version selection depends on file existence, including nested directories.
        # Re-normalize these manifests after the short existence cache expires.
        probe["dynamic_versions"] = any(entry.get("voice_versions") for entry in manifest.values())
    src = layout.parsed_json / f"{name}.json"
    if src.exists():
        try:
            data = json.loads(src.read_text("utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt source just degrades to the manifest
            data = None
        if isinstance(data, list) and data:
            segs = Batch.build_segments(data)
            expected = _expected_voice_params(layout, segs,
                _read_voice_config(layout) if voice_config is None else voice_config)
            c = _chapter_completion(segs, manifest, layout.audio_chunk / name, expected)
            if probe is not None:
                indices = {segment["index"] for segment in segs}
                probe["candidates"] = [(index, entry["path"]) for index, entry in manifest.items()
                    if index in indices and entry.get("ok") and entry.get("path")
                    and (expected is None or entry.get("voice_used") == expected.get(index))]
            out["total"] = c["total"]
            out["completed"] = c["completed"]
            out["remaining"] = c["remaining"]
            out["complete"] = c["total"] > 0 and c["completed"] == c["total"]
            return out
    if probe is not None:
        probe["candidates"] = [(index, entry["path"]) for index, entry in manifest.items()
                               if entry.get("ok") and entry.get("path")]
    out["total"] = len(manifest)  # degrade: source JSON missing / corrupt / empty
    out["completed"] = len(Batch.done_indices(manifest, layout.audio_chunk / name,
                                              layout.workspace))
    out["remaining"] = out["total"] - out["completed"]
    out["complete"] = out["total"] > 0 and out["completed"] == out["total"]
    return out


_MERGE_STATUS_CACHE = BoundedCache(32 * 1024 * 1024, 2048)
_MERGE_STATUS_CACHE_LOCK = threading.Lock()
# No audio/text bodies: only the eligible path/index probe and its source-derived row.
_MERGE_INPUT_CACHE = BoundedCache(64 * 1024 * 1024, 2048)
# Fixed stripes bound synchronization memory even during large cold requests.
_MERGE_STATUS_STRIPES = [threading.Lock() for _ in range(64)]
_MERGE_STATUS_TTL = 2.0
_merge_log = logging.getLogger(__name__)


def _cached_package_merge_status(name, layout, voice_config, metrics=None):
    package_dir = layout.audio_chunk / name
    title_source = _chapter_source_path(f"{name}.json", layout)
    key = (str(layout.workspace), name,
           _stat_key(layout.parsed_json / f"{name}.json"),
           _stat_key(package_dir / "manifest.json"), _stat_key(package_dir),
           _stat_key(layout.voice_profiles / "voice_config.json"),
           _stat_key(title_source) if title_source else None)
    def lookup():
        with _MERGE_STATUS_CACHE_LOCK:
            cached = _MERGE_STATUS_CACHE.get(key)
            if cached is not None:
                expires, row = cached
                if time.monotonic() < expires:
                    if metrics is not None:
                        metrics["hits"] += 1
                    return dict(row)
                _MERGE_STATUS_CACHE.pop(key)
        return None
    row = lookup()
    if row is not None:
        return row
    with _MERGE_STATUS_STRIPES[hash(key) % len(_MERGE_STATUS_STRIPES)]:
        row = lookup()
        if row is not None:
            return row
        started = time.monotonic()
        # Directory changes and expiry require fresh existence checks, not re-parsing
        # unchanged scripts, titles, manifests and effective voice parameters.
        input_key = key[:4] + key[5:]
        with _MERGE_STATUS_CACHE_LOCK:
            inputs = _MERGE_INPUT_CACHE.get(input_key)
        if inputs is None:
            probe = {"candidates": []}
            row = _package_merge_status(name, layout, voice_config, probe=probe)
            if not probe.get("dynamic_versions"):
                with _MERGE_STATUS_CACHE_LOCK:
                    _MERGE_INPUT_CACHE[input_key] = (dict(row), probe["candidates"])
        else:
            base, candidates = inputs
            minimal = {index: {"ok": True, "path": path} for index, path in candidates}
            completed = len(Batch.done_indices(minimal, package_dir, layout.workspace))
            row = {**base, "completed": completed, "remaining": base["total"] - completed,
                   "complete": base["total"] > 0 and completed == base["total"]}
        if metrics is not None:
            metrics["misses"] += 1
            metrics["verify_ms"] += (time.monotonic() - started) * 1000
        with _MERGE_STATUS_CACHE_LOCK:
            _MERGE_STATUS_CACHE[key] = (time.monotonic() + _MERGE_STATUS_TTL, dict(row))
        return row


class MergeStatusRequest(BaseModel):
    packages: list[str]


@router.post("/merge-status")
def merge_status_packages(req: MergeStatusRequest) -> dict:
    """Read package progress with a bounded URL, including for large books."""
    return merge_status(packages=req.packages)


# Keep the legacy repeated query parameters explicit for existing GET callers.
@router.get("/merge-status")
def merge_status(packages: Annotated[list[str] | None, Query()] = None) -> dict:
    """Merge readiness of the chosen audio package(s) — the merge page's package rows.

    Each row is ``{name, total, completed, remaining, complete}``: ``total`` is the
    source parsed JSON's synthesizable segment count (see ``_package_merge_status``), so a
    package whose synthesis was cancelled mid-way is NOT reported 已就绪. Degrades to zero
    rows with no workspace (read-only, like ``/batch-status``); illegal (traversal) names
    are 400. Rows come back in request order.
    """
    names = list(dict.fromkeys(packages or []))  # dedupe, preserving order
    for p in names:
        if not p or p != Path(p).name:
            raise HTTPException(400, f"非法包名：{p}")
    layout = resolve_layout()
    if layout.audio_chunk is None:  # no workspace: nothing to read (read-only, degrades)
        return {"packages": [
            {"name": p, "display_name": chapter_display_name(p, layout),
             "total": 0, "completed": 0, "remaining": 0, "complete": False}
            for p in names
        ]}
    voice_config = _read_voice_config(layout) if names else {}
    return {"packages": [_cached_package_merge_status(p, layout, voice_config) for p in names]}


# ---------------------------------------------------------------------------
# 整章预览：章节最终合成的逐句检视与单句微调（暂存重渲染 → /apply 提交）
# ---------------------------------------------------------------------------

def _is_safe_script_name(name: str) -> bool:
    """A bare filename component (the /merge-style check, minus the empty case)."""
    return bool(name) and name == Path(name).name and name not in {".", ".."}


# 预览编辑/应用抢章节排他锁的等待上限；提成常量只为测试可缩短，生产值不变。
PREVIEW_LOCK_TIMEOUT = 5.0


def _chapter_preview_lock_held(layout, package: str) -> bool:
    """Non-blocking probe of the chapter lock (held during a preview save or merge execution)."""
    if layout.temp is None:
        return False
    try:
        with exclusive_file_lock(Batch.preview_lock_path(layout, package), timeout=0.1):
            return False
    except TimeoutError:
        return True


def _preview_staged_view(state: dict) -> dict:
    """state.json → the per-line response shape (str-keyed; the worker-reported ``file`` included —
    the audition URL and the save gate both key off the ACTUAL produced file)."""
    lines: dict[str, dict] = {}
    for key, value in (state.get("lines") or {}).items():
        if not isinstance(value, dict):
            continue
        lines[str(key)] = {
            "ok": bool(value.get("ok")),
            "reason": value.get("reason") or "",
            "text": value.get("text") or "",
            "speaker": value.get("speaker") or "",
            "instruct": value.get("instruct") or "",
            "rendered_at": value.get("rendered_at") or "",
            "fingerprint": value.get("fingerprint") or "",
            "file": value.get("file") or "",
        }
    return lines


def _preview_inflight_conflicts(script: str, pkg: str, ctx: AuthContext, db: Session) -> str | None:
    """The in-flight guard shared by line-rerender / apply (bgm._durable_audio_conflicts pattern):
    409 reason when any hits, else ``None``. Guards the state.json race and a merge reading an
    in-flight chapter."""
    for payload in active_durable_payloads(task_type="tts.batch", ctx=ctx, db=db):
        targets = payload.get("scripts")
        if not isinstance(targets, list) or not targets:
            targets = [payload.get("script")] if payload.get("script") else []
        if script in {str(value) for value in targets}:
            return "该章节有合成任务进行中，请待其结束后再操作。"
    if script in active_durable_targets(
        task_type="tts.preview_render", payload_key="script", ctx=ctx, db=db,
    ):
        return "该章节有其它句子仍在重渲染，请待其结束后再操作。"
    if pkg in active_durable_targets(
        task_type="tts.merge", payload_key="package", ctx=ctx, db=db,
    ):
        return "该章节有合并任务进行中，请待其结束后再操作。"
    return None


# 单句/章节音频时长的进程级 LRU 缓存：detail 是事件驱动（打开/保存/刷新/任务终态），
# 批量重渲染会随任务逐个终态多次重拉——文件未变即命中，避免重复起 ffprobe 子进程。
# key 包含完整文件身份、工具身份和参数；只缓存成功值。
# probe_duration 的持久化缓存供其它进程和 BGM 共享；本层仅保留小型预览 memo。
_DURATION_CACHE: OrderedDict = OrderedDict()
_DURATION_CACHE_LOCK = threading.Lock()
_DURATION_CACHE_MAX = 512
_DURATION_MISSING = object()


def _cached_probe(path: Path, ffprobe_path: str, mtime_ns: int | None,
                  timeout: float = 120.0) -> float | None:
    """Small preview memo above the shared persistent probe cache; successes only."""
    from ..core.audio_probe_cache import input_identity, tool_identity, PARAMETERS
    source, tool = input_identity(path), tool_identity(ffprobe_path)
    if source is None:
        return None
    key = (source, tool or ("unresolved", ffprobe_path), PARAMETERS)
    with _DURATION_CACHE_LOCK:
        hit = _DURATION_CACHE.get(key, _DURATION_MISSING)
        if hit is not _DURATION_MISSING:
            _DURATION_CACHE.move_to_end(key)
            if source == input_identity(path) and tool == tool_identity(ffprobe_path):
                return hit
            return None
    duration, _err = probe_duration(path, ffprobe_path, timeout=timeout)  # 锁外：起子进程
    if source != input_identity(path) or tool != tool_identity(ffprobe_path) or _err or not math.isfinite(duration) or duration <= 0:
        return None
    value = round(duration, 3)
    with _DURATION_CACHE_LOCK:
        _DURATION_CACHE[key] = value
        _DURATION_CACHE.move_to_end(key)
        if len(_DURATION_CACHE) > _DURATION_CACHE_MAX:
            _DURATION_CACHE.popitem(last=False)
    return value


@router.get("/preview/chapter/{name}")
def preview_chapter(name: str) -> dict:
    """One chapter's preview detail: per-line script + formal 05 audio + staged re-render state
    + downstream artifact status (06 merge / 08 mix / 08 timeline / segment-analysis stale).

    The backend does NOT compute a ``preview_ready`` field (it does not know the frontend
    draft, and a comparison against the on-disk 03 is backwards — the disk 03 is precisely
    the pre-edit value). It returns only the staged fields; the frontend derives previewReady
    against its own draft, and ``/apply`` is the sole authoritative server-side gate.
    F5 restore: for lines whose staged.ok content differs from the disk 03, the frontend
    restores its draft from staged (no re-render needed before saving).
    """
    if not _is_safe_script_name(name):
        raise HTTPException(400, f"非法脚本名：{name}")
    layout = resolve_layout()
    if layout.parsed_json is None:  # read-only: degrade like /batch-status
        return {"name": name, "package": "", "lines": [], "chapter_audio": None,
                "timeline_exists": False,
                "downstream": {"merged": False, "mixed": False, "timeline": False,
                               "segment_stale": False}}
    src = layout.parsed_json / name
    if not src.exists():
        raise HTTPException(404, f"剧本不存在：{name}")
    try:
        data = json.loads(src.read_text("utf-8"))
    except Exception:
        raise HTTPException(400, f"剧本无法解析：{name}")
    if not isinstance(data, list) or not data:
        raise HTTPException(400, f"剧本为空：{name}")
    pkg = Batch.package_for(src)
    manifest = Batch.read_manifest(layout.audio_chunk / pkg)
    staged = _preview_staged_view(Batch._preview_state_read(layout, pkg))
    cfg = get_config()

    # 章节级音频（06 存在则 ffprobe；path 为 workspace 相对形式，前端走 files/download 播放）
    chapter_audio = None
    for candidate in Batch.merged_output_paths(layout, pkg):
        if candidate.is_file():
            try:
                _mtime = candidate.stat().st_mtime_ns
            except OSError:
                continue
            chapter_audio = {
                "path": f"06_audio_merge/{candidate.name}",
                "duration": _cached_probe(candidate, cfg.ffmpeg.ffprobe_path, _mtime, timeout=10.0),
            }
            break

    # 句级近似起点（章节试听从选中句开播）：时间轴口径与 merge 完全一致——
    # 按 03 顺序拼接「存在 05 文件」的句子 + 句间 boundary gap（pause_after 覆盖 >
    # 同人 same_ms > 换人 pause_ms），缺失文件跳过且不贡献间隔。05 被改动后未重新
    # 合并时偏移按当前 05 计算、与 06 存在漂移——试听级近似，不是精确时间轴。
    ffprobe_path = cfg.ffmpeg.ffprobe_path
    pause_ms = cfg.tts.pause_between_speakers_ms or 500
    same_ms = cfg.tts.pause_same_speaker_ms or 250
    lines_out = []
    offset = 0.0
    prev_speaker: str | None = None
    prev_pause_after: object = None
    for i, row in enumerate(data):
        entry = manifest.get(i) or {}
        ok = bool(entry.get("ok"))
        audio = ""
        audio_mtime_ns = None
        line_file: Path | None = None
        if ok:
            p = entry.get("path") or ""
            if p:
                candidate = Path(p)
                if not candidate.is_absolute():
                    candidate = (layout.workspace / p) if layout.workspace else Path(p)
                try:
                    if candidate.is_file():
                        audio = p
                        audio_mtime_ns = candidate.stat().st_mtime_ns
                        line_file = candidate
                    else:
                        ok = False
                except OSError:
                    ok = False
            else:
                ok = False
        start_offset = None
        # 单句真实时长（与 chapter_audio 解耦：未合并也算，供列表行/Inspector 显示；
        # 同时供下方 start_offset 累加）。走 mtime 缓存，批量重渲染多次重拉只首次 probe。
        duration_out: float | None = None
        if ok and line_file is not None:
            # 详情是用户直接等待的同步接口：单文件 ffprobe 卡死时把上限压到 10s，
            # 避免一个坏文件拖住整章（默认 120s 只用于后台/引擎路径）。
            duration_out = _cached_probe(line_file, ffprobe_path, audio_mtime_ns, timeout=10.0)
        if chapter_audio is not None and ok and line_file is not None:
            speaker = (entry.get("speaker") or row.get("speaker") or row.get("type") or "").strip()
            if prev_speaker is not None:
                offset += boundary_gap_ms(prev_pause_after, prev_speaker, speaker, pause_ms, same_ms) / 1000.0
            start_offset = round(offset, 3)
        lines_out.append({
            "index": i,
            "speaker": (row.get("speaker") or row.get("type") or "").strip(),
            "text": (row.get("text") or "").strip(),
            "instruct": (row.get("instruct") or "").strip(),
            "audio": audio,
            "audio_mtime_ns": audio_mtime_ns,
            "duration": duration_out,
            "ok": ok,
            "reason": "" if ok else str(entry.get("reason") or ""),
            "staged": staged.get(str(i)),
            "start_offset": start_offset,
        })
        if start_offset is not None:
            offset += 0.0 if duration_out is None else duration_out
            prev_speaker = speaker
            prev_pause_after = row.get("pause_after")

    from ..core.filenames import package_aliases
    stems = package_aliases(pkg)
    timeline_exists = bool(layout.bgm and any((layout.bgm / "timelines" / f"{stem}.json").is_file() for stem in stems))
    seg_data = Bgm.load_segment_analysis(layout, [pkg]).get("chapters") or {}
    sa = seg_data.get(pkg)
    segment_stale = False
    if isinstance(sa, dict) and (sa.get("blocks") or sa.get("entries")):
        try:
            segment_stale = (sa.get("fingerprint")
                             != Bgm.segment_fingerprint(Bgm._load_parsed_entries(layout, pkg)))
        except Exception:  # noqa: BLE001 — 03 缺失/损坏 = 分析已不可用
            segment_stale = True
    # segment_stale：BGM 段落分析是否已失效（03 变更 → 分析指纹不再匹配）。
    # 本页暂不消费（BGM 页有独立分析态入口），预留供将来「BGM 段落分析已过期」提示。
    downstream = {
        "merged": chapter_audio is not None,
        "mixed": bool(layout.bgm and any((layout.bgm / f"{stem}.mp3").is_file() for stem in stems)),
        "timeline": timeline_exists,
        "segment_stale": segment_stale,
    }
    return {
        "name": name,
        "package": pkg,
        "lines": lines_out,
        "chapter_audio": chapter_audio,
        "timeline_exists": timeline_exists,
        "downstream": downstream,
    }


class PreviewLineRerenderRequest(BaseModel):
    script: str
    index: int
    text: str | None = None
    speaker: str | None = None
    instruct: str | None = None

    @field_validator("index")
    @classmethod
    def _check_index(cls, v):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError("index 须为非负整数")
        return v


@router.post("/preview/line-rerender")
def preview_line_rerender(
    req: PreviewLineRerenderRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Submit a single-line re-render task (``tts.preview_render``) — the artifact lands in the
    staging area only; the formal 05 / manifest / 03 stay untouched until ``/apply``."""
    _common.require_workspace()
    if not _is_safe_script_name(req.script):
        raise HTTPException(400, f"非法脚本名：{req.script}")
    layout = get_or_prepare_layout()
    src = layout.parsed_json / req.script
    if not src.exists():
        raise HTTPException(404, f"剧本不存在：{req.script}")
    try:
        data = json.loads(src.read_text("utf-8"))
    except Exception:
        raise HTTPException(400, f"剧本无法解析：{req.script}")
    if not isinstance(data, list) or not data:
        raise HTTPException(400, f"剧本为空：{req.script}")
    if req.index >= len(data):
        raise HTTPException(400, f"第 {req.index + 1} 句越界（本章共 {len(data)} 行）")
    if req.text is not None and not req.text.strip():
        raise HTTPException(400, "台词文本不能为空")

    conflict = _preview_inflight_conflicts(req.script, Batch.package_for(src), ctx, db)
    if conflict:
        raise HTTPException(409, conflict)
    render_item = {"index": req.index}
    for key in ("text", "speaker", "instruct"):
        value = getattr(req, key)
        if value is not None:
            render_item[key] = value
    task = submit_legacy_engine_task(
        task_type="tts.preview_render",
        label=f"整章预览·单句重渲染：{req.script}·第{req.index + 1}句",
        payload={
            "script": req.script,
            "index": req.index,
            "render": [render_item],
            "config": get_config().model_dump(mode="json"),
        },
        ctx=ctx,
        db=db,
        idempotency_prefix="tts-preview",
    )
    return {"task_id": task["id"]}


@router.get("/preview/audio/{name:path}")
def preview_audio(name: str, request: Request):
    """Stream a staged preview audio (``00_temp/chapter_preview/`` is not addressable via the
    files API). ``name`` must resolve inside that root with a ``.mp3`` / ``.wav`` extension."""
    layout = resolve_layout()
    if layout.temp is None:
        raise HTTPException(404, "尚未设置工作空间")
    root = layout.temp / "chapter_preview"
    if not name or ".." in Path(name).parts:
        raise HTTPException(400, "非法路径")
    p = (root / name).resolve()
    if not p.is_relative_to(root.resolve()):
        raise HTTPException(400, "非法路径")
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    if p.suffix.lower() not in {".mp3", ".wav"}:
        raise HTTPException(400, "预览音频仅支持 .mp3 / .wav")
    media_type = "audio/mpeg" if p.suffix.lower() == ".mp3" else "audio/wav"
    return file_response(request, p, media_type=media_type, filename=p.name, inline=True)


class PreviewEdit(BaseModel):
    index: int
    text: str | None = None
    speaker: str | None = None
    instruct: str | None = None

    @field_validator("index")
    @classmethod
    def _check_index(cls, v):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError("index 须为非负整数")
        return v


class PreviewApplyRequest(BaseModel):
    script: str
    # Partial triples: each edit carries only the changed fields.
    edits: list[PreviewEdit] = []


def _preview_effective_triple(row: dict, edit: PreviewEdit) -> tuple[str, str, str]:
    """The line's post-edit (text, speaker, instruct) — the disk 03 line with this edit's
    fields overridden, in the worker's own (stripped, type-fallback) shape, so the comparison
    against the staged state is apples-to-apples (state.json records build_segments' output)."""
    text = edit.text if edit.text is not None else (row.get("text") or "")
    speaker = edit.speaker if edit.speaker is not None else (row.get("speaker") or row.get("type") or "")
    instruct = edit.instruct if edit.instruct is not None else (row.get("instruct") or "")
    return text.strip(), speaker.strip(), instruct.strip()


def _preview_downstream_deletions(layout, pkg: str, failures: list[dict]) -> list[str]:
    """Delete this chapter's downstream artifacts (06 merge / 08 mix / 08 timeline). Each
    failure is recorded, never raised — the caller surfaces it as ``downstream_dirty``."""
    from ..core.filenames import package_aliases
    targets = [
        ("merged", p) for p in Batch.merged_output_paths(layout, pkg)
    ]
    targets += [target for stem in package_aliases(pkg) for target in (
        ("mixed", layout.bgm / f"{stem}.mp3"),
        ("timeline", layout.bgm / "timelines" / f"{stem}.json"),
    )]
    invalidated = []
    for artifact, path in targets:
        try:
            if path.is_file():
                path.unlink()
                invalidated.append(artifact)
        except OSError as e:
            failures.append({"stage": "downstream", "artifact": artifact, "error": str(e)})
    return invalidated


def _cleanup_backup_parent(backup_root: Path) -> None:
    """Remove the shared backup parent once it is empty (after a save's subdir went away) —
    only when empty: a concurrent save of another chapter may still hold a subdir there."""
    parent = backup_root.parent
    try:
        if not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def _preview_apply_commit(layout, src, lines, edits_by_index, pkg) -> tuple[bool, dict]:
    """The locked save body: backup → 05 replace → manifest update → 03 update → (success:
    drop backup + delete downstream) or (failure: byte-level restore + 500 shape).

    Returns ``(ok, result)``: ``ok=True`` carries the 200 response body (possibly with
    ``downstream_dirty``); ``ok=False`` carries the 500 body (the formal three-piece set is
    byte-identical to pre-save after the restore).
    """
    total = len(lines)
    staging = Batch.preview_staging_dir(layout, pkg)
    state = Batch._preview_state_read(layout, pkg)
    staged_lines = state.get("lines") or {}
    out_dir = layout.audio_chunk / pkg
    manifest_path = out_dir / "manifest.json"
    width = Batch.filename_width(total)

    failures: list[dict] = []
    backup_root = layout.temp / "preview_apply_backup" / f"{pkg}_{uuid.uuid4().hex[:12]}"

    # The replace plan (per edited line): the staged artifact, the formal target name (the line's
    # ORIGINAL 1-based number, width-stable, with the artifact's ACTUAL extension — a .wav
    # fallback lands as a .wav), and the manifest entry's pre-save path (removed when it
    # differs, so a .mp3→.wav switch never leaves a stale file behind).
    plan: dict[int, dict] = {}
    entries_before = Batch.read_manifest(out_dir)
    for index in sorted(edits_by_index):
        st = staged_lines.get(str(index)) or {}
        file_name = st.get("file") or ""
        dst = out_dir / f"{index + 1:0{width}}{Path(file_name).suffix}"
        old = entries_before.get(index) or {}
        old_path = old.get("path") or ""
        old_abs = None
        if old_path:
            candidate = Path(old_path)
            if not candidate.is_absolute():
                candidate = (layout.workspace / old_path) if layout.workspace else candidate
            if candidate.is_file():
                old_abs = candidate
        plan[index] = {
            "staged_file": staging / file_name,
            "dst": dst,
            "old_abs": old_abs,
        }

    # 1. Backup: the whole package dir (manifest + 05 files) + the script, same-disk copies.
    #    Restoring these two directories is byte-exact and covers every replace permutation.
    backup_root.mkdir(parents=True, exist_ok=True)
    if out_dir.exists():
        shutil.copytree(out_dir, backup_root / "audio_chunk_pkg", dirs_exist_ok=False)
    (backup_root / "script.json").write_bytes(src.read_bytes())

    # 2. 05 替换
    def _step_replace() -> None:
        from ..core.workspace_epochs import managed_mutation
        with managed_mutation(out_dir):
            out_dir.mkdir(parents=True, exist_ok=True)
            for index, item in plan.items():
                staged_file, dst, old_abs = item["staged_file"], item["dst"], item["old_abs"]
                if not staged_file.is_file():
                    raise RuntimeError(f"暂存产物缺失：{item['staged_file'].name}")
                shutil.copy2(staged_file, dst)
                if old_abs is not None and old_abs.resolve() != dst.resolve():
                    try:
                        old_abs.unlink()
                    except OSError as e:
                        raise RuntimeError(f"旧音频删除失败：{old_abs.name}（{e}）")

    # 3. manifest 更新（只动被改行；voice_versions / pause_after 等未变字段保留）
    def _step_manifest() -> None:
        entries = Batch.read_manifest(out_dir)
        vc_path = layout.voice_profiles / "voice_config.json"
        voice_config = {}
        vc_exists = False
        if vc_path.exists():
            try:
                loaded = json.loads(vc_path.read_text("utf-8"))
                if isinstance(loaded, dict):
                    voice_config = loaded
                    vc_exists = True
            except Exception:  # noqa: BLE001
                voice_config = {}
        for index in sorted(edits_by_index):
            st = staged_lines.get(str(index)) or {}
            old = entries.get(index) or {}
            entry = {
                "index": index,
                "speaker": st.get("speaker") or "",
                "text": st.get("text") or "",
                "pause_after": old.get("pause_after", lines[index].get("pause_after")),
                "path": f"05_audio_chunk/{pkg}/{plan[index]['dst'].name}",
                "ok": True,
                "reason": "",
            }
            if vc_exists:
                entry["voice_used"] = Batch.voice_params(entry["speaker"], voice_config)
                entry["voice_signature"] = Batch.voice_signature(entry["speaker"], voice_config)
            else:
                for key in ("voice_used", "voice_signature"):
                    if key in old:
                        entry[key] = old[key]
            if isinstance(old.get("voice_versions"), list):
                entry["voice_versions"] = old["voice_versions"]
            entries[index] = entry
        ordered = [entries[i] for i in sorted(entries)]
        Batch.write_manifest_file(manifest_path, ordered)

    # 4. 03 剧本（字段级就地改，其余字段原样保留；写 strip 后的值与 staged 口径一致）
    def _step_script() -> None:
        for index, edit in sorted(edits_by_index.items()):
            row = lines[index]
            if edit.text is not None:
                row["text"] = edit.text.strip()
            if edit.speaker is not None:
                row["speaker"] = edit.speaker.strip()
            if edit.instruct is not None:
                row["instruct"] = edit.instruct.strip()
        pathio.rewrite_json_file(src, lines)

    steps = (
        ("replace_05", _step_replace),
        ("update_manifest", _step_manifest),
        ("update_script", _step_script),
    )
    for stage, step in steps:
        try:
            step()
        except Exception as e:  # noqa: BLE001 — 任一步失败 → 字节级还原 → 500
            _restore = []
            try:
                from ..core.workspace_epochs import managed_mutation
                with managed_mutation(out_dir):
                    shutil.rmtree(out_dir, ignore_errors=True)
                    if (backup_root / "audio_chunk_pkg").exists():
                        shutil.copytree(backup_root / "audio_chunk_pkg", out_dir)
            except Exception as re_:  # noqa: BLE001
                _restore.append(f"05 包还原失败：{re_}")
            try:
                with managed_mutation(src):
                    src.write_bytes((backup_root / "script.json").read_bytes())
            except Exception as re_:  # noqa: BLE001
                _restore.append(f"剧本还原失败：{re_}")
            shutil.rmtree(backup_root, ignore_errors=True)
            _cleanup_backup_parent(backup_root)
            return False, {
                "ok": False,
                "stage": stage,
                "error": str(e),
                "restore_failures": _restore,
                "message": ("保存失败，正式状态已还原（与保存前一致），可重试保存。"
                            if not _restore else
                            "保存失败，且还原不完整——状态可能不一致，需人工核对。"),
            }

    # ②③④ 全部成功 → 删备份 → 下游失效（失败只记录，不回滚 ②③④——此时保存已成立）
    shutil.rmtree(backup_root, ignore_errors=True)
    _cleanup_backup_parent(backup_root)
    invalidated = _preview_downstream_deletions(layout, pkg, failures)
    downstream_dirty = bool(failures)
    if not downstream_dirty:
        # 暂存清理：保存成功后删除整章暂存目录（state.json + 全部行暂存文件）。前端保存总
        # 覆盖所有 dirty 句，正常路径无丢失；整目录删除比逐句修剪更简单（规避 state.json/
        # 文件一致性边缘）。已知局限：「重渲染 A 句 → 撤销 A 句 → 保存其他句」场景下，A 句
        # 暂存音频（已花 TTS 成本）随目录删除——损失对象是暂存产物（正式 05 不动），后续
        # 需要时重新渲染即可。（部分下游失败时保留目录供排查/重试。）
        shutil.rmtree(staging, ignore_errors=True)
    return True, {
        "ok": True,
        "edited": sorted(edits_by_index),
        "invalidated": invalidated,
        "failures": failures,
        "downstream_dirty": downstream_dirty,
    }


@router.post("/preview/apply")
def apply_preview_edits(
    req: PreviewApplyRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Save the edits (the sole authoritative gate): per-line staged state must match the
    effective triple (disk 03 line + this edit's fields), the staged ``file`` must exist in
    the staging dir — else 409. Then, under the chapter lock (mutually exclusive with
    tts.merge): backup → 05 replace → manifest → 03, with byte-level restore + 500 on any
    failure, followed by this chapter's downstream invalidation (06 / 08 mix / 08 timeline).
    """
    _common.require_workspace()
    if not _is_safe_script_name(req.script):
        raise HTTPException(400, f"非法脚本名：{req.script}")
    layout = get_or_prepare_layout()
    src = layout.parsed_json / req.script
    if not src.exists():
        raise HTTPException(404, f"剧本不存在：{req.script}")
    try:
        lines = json.loads(src.read_text("utf-8"))
    except Exception:
        raise HTTPException(400, f"剧本无法解析：{req.script}")
    if not isinstance(lines, list) or not lines:
        raise HTTPException(400, f"剧本为空：{req.script}")
    total = len(lines)

    edits_by_index: dict[int, PreviewEdit] = {}
    for edit in req.edits:
        if edit.index >= total:
            raise HTTPException(400, f"第 {edit.index + 1} 句越界（本章共 {total} 行）")
        if edit.text is not None and not edit.text.strip():
            raise HTTPException(400, f"第 {edit.index + 1} 句台词不能为空")
        edits_by_index[edit.index] = edit
    if not edits_by_index:
        raise HTTPException(400, "没有要保存的修改。")

    pkg = Batch.package_for(src)
    conflict = _preview_inflight_conflicts(req.script, pkg, ctx, db)
    if conflict:
        raise HTTPException(409, conflict)

    # 逐句门禁：staged（ok + file 存在 + 三元组 == 有效三元组）——唯一权威
    staging = Batch.preview_staging_dir(layout, pkg)
    state = Batch._preview_state_read(layout, pkg)
    staged_lines = state.get("lines") or {}
    gate_errors = []
    for index in sorted(edits_by_index):
        st = staged_lines.get(str(index))
        effective = _preview_effective_triple(lines[index], edits_by_index[index])
        file_exists = (
            isinstance(st, dict) and bool(st.get("file")) and (staging / st["file"]).is_file()
        )
        if (
            not isinstance(st, dict) or st.get("ok") is not True or not file_exists
            or st.get("text") != effective[0]
            or st.get("speaker") != effective[1]
            or st.get("instruct") != effective[2]
        ):
            gate_errors.append(f"第 {index + 1} 句修改后尚未完成重渲染")
    if gate_errors:
        raise HTTPException(409, "；".join(gate_errors))

    # 章节锁（apply 取不到 → 409；tts.merge 执行期持同一把锁，超时由 Worker 重试）
    lock = exclusive_file_lock(Batch.preview_lock_path(layout, pkg), timeout=PREVIEW_LOCK_TIMEOUT)
    try:
        lock.__enter__()
    except TimeoutError:
        raise HTTPException(409, "该章节有保存或合并操作进行中，请稍后再试。")
    try:
        ok, result = _preview_apply_commit(layout, src, lines, edits_by_index, pkg)
    finally:
        lock.__exit__(None, None, None)
    if not ok:
        raise HTTPException(500, result)
    return result


class PreviewPurgeRequest(BaseModel):
    script: str


@router.post("/preview/purge-stale")
def purge_preview_stale(
    req: PreviewPurgeRequest,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict:
    """Retry the downstream invalidation after a partial save failure (``downstream_dirty``) —
    the same deletions, under the chapter lock."""
    _common.require_workspace()
    if not _is_safe_script_name(req.script):
        raise HTTPException(400, f"非法脚本名：{req.script}")
    layout = get_or_prepare_layout()
    if not (layout.parsed_json / req.script).exists():
        raise HTTPException(404, f"剧本不存在：{req.script}")
    pkg = Batch.package_for(layout.parsed_json / req.script)
    conflict = _preview_inflight_conflicts(req.script, pkg, ctx, db)
    if conflict:
        raise HTTPException(409, conflict)
    failures: list[dict] = []
    lock = exclusive_file_lock(Batch.preview_lock_path(layout, pkg), timeout=PREVIEW_LOCK_TIMEOUT)
    try:
        lock.__enter__()
    except TimeoutError:
        raise HTTPException(409, "该章节有保存或合并操作进行中，请稍后再试。")
    try:
        invalidated = _preview_downstream_deletions(layout, pkg, failures)
    finally:
        lock.__exit__(None, None, None)
    return {
        "ok": True,
        "invalidated": invalidated,
        "failures": failures,
        "downstream_dirty": bool(failures),
    }
