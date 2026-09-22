"""TTS + character-voice + batch + merge endpoints (modules: TTS / 角色配音 / 音频合成 / 音频合并).

``GET /status`` reports readiness. The stage endpoints (``POST /prepare-foundations``,
``POST /make-clones``, ``GET /voices``, ``POST /batch``, ``POST /merge``) each start a
long-running
:class:`Task` that drives the isolated Qwen3-TTS engine (see
``backend/engines/tts.py`` / ``voices.py`` / ``tts_batch.py`` / ``merge.py``) and
return ``{"task_id"}``; the UI streams the task's progress/logs over SSE and plays
the resulting audio via the shared ``GET /api/files/download/05_audio_chunk/{name}``
route. Any failing task is marked failed and isolated — it never takes the console
down (requirement #7).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from ..core import pathio
from ..core.concurrency import merge_gate, set_merge_concurrency
from ..core.paths import ALL_PARSED_JSON, get_layout, resolve_parsed_json, resolve_parsed_json_all
from ..core.tasks import TERMINAL, TaskStatus, get_task_manager
from ..engines import merge as Merge
from ..engines import tts as T
from ..engines import tts_batch as Batch
from ..engines import tts_stress as Stress
from ..engines import voices as V
from . import _common

router = APIRouter(prefix="/api/tts", tags=["tts"])


@router.get("/status")
def status() -> dict:
    """Let the UI show an accurate badge (ready vs. engine-not-installed)."""
    return {
        "implemented": T.IMPLEMENTED,
        "message": T.NOT_READY_MSG if not T.IMPLEMENTED else "TTS 合成可用。",
    }


# ---------------------------------------------------------------------------
# 角色配音（voice preparation）
# ---------------------------------------------------------------------------

class PrepareFoundationsRequest(BaseModel):
    # Phase 1 (LLM only): None -> every character; a list -> only those (single-char regen).
    speakers: list[str] | None = None
    # True -> regenerate only characters without a foundation yet.
    new_only: bool = False
    # speaker -> user-supplied voice description (skips the LLM for that character).
    overrides: dict[str, str] | None = None
    # Which parsed JSON (in 03_parsed_json/) to read; None -> most recent (resolve_parsed_json).
    script: str | None = None


class MakeClonesRequest(BaseModel):
    # Phase 2 (TTS only): None -> every foundation-bearing character; a list -> only those.
    speakers: list[str] | None = None
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


def _scope_suffix(script: str | None) -> str:
    """A short task-label suffix naming the parsed-JSON scope ("" for the default, most-recent)."""
    return "（全部解析文件）" if script == ALL_PARSED_JSON else ""


@router.post("/prepare-foundations")
def prepare_foundations(req: PrepareFoundationsRequest) -> dict:
    """Start Phase 1: batch-generate every character's voice foundation (LLM only, no TTS)."""
    _common.require_workspace()
    suffix = _scope_suffix(req.script)
    if req.speakers:
        label = f"重新生成 {len(req.speakers)} 个角色语音推理基础{suffix}"
    elif req.new_only:
        label = f"生成新增角色语音推理基础{suffix}"
    else:
        label = f"生成所有角色语音推理基础{suffix}"
    task = get_task_manager().create(
        "voices-foundation", label,
        V.prepare_foundations,
        req.speakers, req.new_only, req.overrides or {}, req.script,
    )
    return {"task_id": task.id}


@router.post("/make-clones")
def make_clones(req: MakeClonesRequest) -> dict:
    """Start Phase 2: batch-render every character's clone seed WAV (TTS only, no LLM)."""
    _common.require_workspace()
    suffix = _scope_suffix(req.script)
    if req.speakers:
        label = f"重新制作 {len(req.speakers)} 个角色克隆音频{suffix}"
    elif req.new_only:
        label = f"制作新增角色克隆音频{suffix}"
    else:
        label = f"制作所有角色克隆音频{suffix}"
    label += " · 备选 自动" if req.candidate_count is None else f" · 备选 {req.candidate_count}"
    task = get_task_manager().create(
        "voices-clone", label,
        V.make_clones,
        req.speakers, req.new_only, req.concurrency, req.script, req.candidate_count,
    )
    return {"task_id": task.id}


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


def _fold_script(order: list[str], counts: dict[str, int], data) -> bool:
    """Fold one parsed script (a list of entries) into the shared ``order``/``counts``.

    Dedupes by speaker name (falling back to ``type``), sums line counts, and keeps
    first-appearance order — the same folding the single-file path did inline, now shared
    with the whole-book aggregate. Returns True when the file held a non-empty list (the
    ``has_script`` signal).
    """
    if not isinstance(data, list) or not data:
        return False
    for entry in data:
        sp = (entry.get("speaker") or entry.get("type") or "").strip()
        if not sp:
            continue
        if sp not in counts:
            counts[sp] = 0
            order.append(sp)
        counts[sp] += 1
    return True


@router.get("/voices")
def list_voices(script: str | None = None) -> dict:
    """Detected characters + their voice-config state (ready/pending) + preview paths.

    ``script`` names the parsed JSON (in ``03_parsed_json/``) to read; when omitted the
    most recently written one is used (see ``resolve_parsed_json``). With no workspace
    set it degrades to an empty result. ``preview`` is a path relative to
    ``04_voice_profiles/`` so the UI can play it through the shared
    ``download/04_voice_profiles/{name}`` route; empty when there is nothing to preview.
    ``speakers`` is sorted by ``line_count`` descending (stable — ties keep
    first-appearance order), so the leads top the page list.
    """
    layout = get_layout()
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
    for sp in script_paths:
        if not sp.exists():
            continue
        try:
            data = json.loads(sp.read_text("utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt file just contributes no speakers
            data = []
        if _fold_script(order, counts, data):
            has_script = True

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
        alias_of = entry.get("alias_of", "")
        ready = bool(alias_of) or _voice_usable(entry)
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
    }


class SelectVoiceRequest(BaseModel):
    speaker: str
    # Candidate id to make active; None/empty = clear the pick (the default first
    # candidate becomes active again).
    audio_id: str | None = None


def _phase_task_active() -> bool:
    """Whether a voices-foundation / voices-clone task is in flight. Both rewrite
    ``voice_config.json`` as a whole file, so a concurrent selection write could be
    clobbered (or vice versa) — refuse the write while one runs."""
    for t in get_task_manager().list():
        if t.module in ("voices-foundation", "voices-clone") and t.status not in TERMINAL:
            return True
    return False


@router.put("/voices/select")
def select_voice(req: SelectVoiceRequest) -> dict:
    """Record the user's clone-candidate pick for a character.

    Synchronous (no Task): the chosen candidate becomes the ACTIVE clone reference —
    ``selected_audio_id`` plus the top-level ``ref_audio`` are updated together in one
    file rewrite, so every downstream stage (音频合成 / worker) uses the picked take.
    """
    _common.require_workspace()
    if _phase_task_active():
        raise HTTPException(409, "配音任务进行中，请待其结束后再选择音色。")
    layout = get_layout()
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
    vc_path.write_bytes(json.dumps(voice_config, indent=2, ensure_ascii=False).encode("utf-8"))
    Batch.invalidate_speaker_outputs([req.speaker], layout)
    return {"ok": True, "speaker": req.speaker, "selected_audio_id": aid,
            "ref_audio": active["ref_audio"]}


class SetGenderRequest(BaseModel):
    speaker: str
    # "male" / "female" — set the badge; "" / None = clear it (back to unknown).
    gender: str = ""


@router.post("/voices/gender")
def set_gender(req: SetGenderRequest) -> dict:
    """Record the user's gender pick for a character (the badge next to the name).

    Synchronous (no Task): upserts ``gender`` on the character's ``voice_config.json``
    entry — a character without any entry yet gets a minimal one (script-detected names
    are valid targets too). A running phase task is refused (409): it rewrites the whole
    file and would clobber the pick, same guard as the candidate selection.
    """
    _common.require_workspace()
    if _phase_task_active():
        raise HTTPException(409, "配音任务进行中，请待其结束后再设置性别。")
    sp = (req.speaker or "").strip()
    if not sp:
        raise HTTPException(400, "角色名不能为空。")
    g = (req.gender or "").strip()
    if g not in ("male", "female", ""):
        raise HTTPException(400, "无效的性别值。")
    layout = get_layout()
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
def merge_speakers(req: MergeSpeakersRequest) -> dict:
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
    if _phase_task_active():
        raise HTTPException(409, "配音任务进行中，请待其结束后再合并角色。")
    src = (req.source or "").strip()
    tgt = (req.target or "").strip()
    if not src or not tgt:
        raise HTTPException(400, "角色名不能为空。")
    if src == tgt:
        raise HTTPException(400, "源角色与目标角色相同。")
    layout = get_layout()

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
    # None -> every script line; a list of line indices -> only those (single file only).
    indices: list[int] | None = None
    # Which parsed JSON (in 03_parsed_json/) to synthesize; None -> most recent.
    script: str | None = None
    # Multi-file run (the 待合成 card's selection): parsed-JSON file names, synthesized one
    # by one in a single task (each file = its own package; a per-file failure is isolated).
    # Takes precedence over ``script``; an empty list falls back to ``script`` / most recent.
    scripts: list[str] | None = None
    # Manual batch ceiling (1..128); auto_concurrency uses measured safety tiers instead.
    concurrency: int | None = None
    # Use measured upward-matched safety tiers for the per-batch row cap.
    auto_concurrency: bool = False
    # Reproducible seed for the run: >=0 seeds each sub-batch (seed + sub-batch seq); None ->
    # the persisted default (config.tts.batch_seed); -1 -> random.
    seed: int | None = None


@router.post("/batch")
def run_batch(req: BatchRequest) -> dict:
    _common.require_workspace()
    if req.script == ALL_PARSED_JSON or any(s == ALL_PARSED_JSON for s in (req.scripts or [])):
        raise HTTPException(status_code=400, detail="音频合成仅支持逐个解析 JSON（“全部”只用于「角色配音」）。")
    scripts = req.scripts or ([req.script] if req.script else [])
    if len(scripts) > 1 and req.indices:
        raise HTTPException(status_code=400, detail="按段选择（indices）仅支持单个文件。")
    if req.indices:
        label = f"音频合成（{len(req.indices)} 段）"
    else:
        label = "音频合成（续合）"
    if len(scripts) == 1:
        label += f" · {scripts[0]}"
    elif len(scripts) > 1:
        label += f" · {len(scripts)} 个文件"
    if req.concurrency:
        label += f" · 批内 {req.concurrency}"
    if req.auto_concurrency:
        label += " · 自动批量"
    if len(scripts) > 1:
        task_args = (scripts, req.concurrency, req.seed, req.auto_concurrency) \
            if req.auto_concurrency else (scripts, req.concurrency, req.seed)
        task = get_task_manager().create(
            "tts-batch", label,
            Batch.synthesize_multi, *task_args,
        )
    else:  # 0 or 1 file: the legacy single-file path, byte-identical behaviour
        task_args = (req.indices, scripts[0] if scripts else req.script,
                     req.concurrency, req.seed, req.auto_concurrency) \
            if req.auto_concurrency else (req.indices, scripts[0] if scripts else req.script,
                                          req.concurrency, req.seed)
        task = get_task_manager().create(
            "tts-batch", label,
            Batch.synthesize, *task_args,
        )
    return {"task_id": task.id}


class StressTestRequest(BaseModel):
    # 批内行数（--concurrency 上限，钳 [1,128]）：每轮要垫成一个张量批的行数（固定不变）。
    rows: int = 64
    # 起始每行字数（1..2500）。
    start_chars: int = 10
    # 每轮递增的每行字数（≥1）：每轮 = 上一轮 + step，逐轮跑下去直到某轮失败。
    step_chars: int = 10
    # 轮数上限；None/0 = 不限（跑到失败为止，硬上限 Stress.MAX_STRESS_ROUNDS 防病态循环）。
    max_rounds: int | None = None
    # 压测用的克隆音色角色名；None = 自动取第一个可用克隆音色（任意克隆都可以）。
    speaker: str | None = None
    # 可复现 seed；None = 用持久默认（config.tts.batch_seed）。
    seed: int | None = None


def _engine_task_active() -> bool:
    """Whether a task that spawns the shared .venv engine is in flight (音频合成 / 音频合并 /
    角色配音·克隆 / 压测). Two engine subprocesses would fight over the GPU, so the
    stress-test entry refuses to start while one runs."""
    for t in get_task_manager().list():
        if t.module in ("tts-batch", "merge", "voices-clone", "tts-stress") and t.status not in TERMINAL:
            return True
    return False


@router.post("/stress-test")
def run_stress_test(req: StressTestRequest) -> dict:
    """压测（临时测试入口）：机器自动生成自然语句（不借助 LLM、不需要解析脚本），固定批内
    行数、每行字数从起点每轮递增，用任意一个已有克隆音色逐轮跑下去，直到某轮未达吞吐标准
    （1 秒 10 字）/ 看门狗 / 引擎失败为止。逐轮报告（处理量 / 耗时 / 吞吐）写入工作空间
    stress_test/ 目录；临时产物全在 00_temp/、每轮用完即删。"""
    _common.require_workspace()
    if not 1 <= req.start_chars <= Stress.MAX_STRESS_CHARS:
        raise HTTPException(400, f"起始每行字数须为 1..{Stress.MAX_STRESS_CHARS}。")
    if req.step_chars < 1:
        raise HTTPException(400, "每轮递增须 ≥ 1。")
    if req.max_rounds is not None and req.max_rounds < 1:
        raise HTTPException(400, "轮数上限须 ≥ 1（留空 = 不限）。")
    if _engine_task_active():
        raise HTTPException(409, "引擎任务进行中（音频合成 / 合并 / 角色克隆 / 压测），请待其结束后再压测。")
    rows = Batch.clamp_concurrency(req.rows)
    label = f"压测（{rows} 行 · {req.start_chars}字 起 · 每轮 +{req.step_chars}）"
    task = get_task_manager().create(
        "tts-stress", label,
        Stress.stress_test, rows, req.start_chars, req.step_chars, req.max_rounds,
        req.speaker, req.seed,
    )
    return {"task_id": task.id}


class ResetBatchRequest(BaseModel):
    # Parsed-JSON file names (03_parsed_json/). Each one's synthesis package — the folder
    # ``05_audio_chunk/<包名>/`` with its mp3s and manifest.json — is deleted, so the
    # following ordinary run (default resume) re-synthesizes every segment.
    scripts: list[str]


def _batch_task_active() -> bool:
    """Whether a tts-batch (音频合成) task is in flight. Its engine writes the package folders
    while running, so deleting a package mid-run would tear out the files / manifest it is
    producing — refuse the reset while one runs (the same guard shape as voice selection)."""
    for t in get_task_manager().list():
        if t.module == "tts-batch" and t.status not in TERMINAL:
            return True
    return False


@router.post("/batch-reset")
def reset_batch(req: ResetBatchRequest) -> dict:
    """「重新全部合成」第一步（同步、非任务）：删除选中解析 JSON 的合成包
    （``05_audio_chunk/<包名>/``：逐行 mp3 + manifest.json），使随后的一键合成请求
    （与默认续合同一条线路、同一请求形状）从头重做全部段落。

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
    if _batch_task_active():
        raise HTTPException(409, "合成任务进行中，请待其结束后再重置。")
    layout = get_layout()
    removed = []
    for name in req.scripts:
        pkg_name = Batch.package_for(Path(name))
        pkg = layout.audio_chunk / pkg_name
        if pkg.exists():
            shutil.rmtree(pkg)
            removed.append(pkg_name)
    return {"ok": True, "removed": removed}


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
_STATUS_CACHE: dict = {}
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE_MAX = 512


def reset_batch_status_cache() -> None:
    """Drop the per-package batch-status cache (a test / debug seam — the file stats already
    invalidate on every real change). Mirrors ``core.config.reset_config_cache``."""
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE.clear()


def _stat_key(p) -> tuple | None:
    """A path's ``(mtime_ns, size)``, or ``None`` when it cannot be stat'ed (a file that
    vanishes between keying and computing degrades to a miss on the next poll, never a crash)."""
    try:
        st = Path(p).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _file_batch_status(name: str, layout, voice_config: dict, out_dir: Path | None = None) -> dict:
    """One row of the multi-file 待合成 list (``GET /batch-status?scripts=…``).

    Per file: segment completion (same rule as the single-file endpoint — ``completed`` = a
    manifest entry ``ok`` whose file is still on disk) plus character readiness (the same
    ready rule as ``/voices``: an alias or a usable voice entry). ``complete`` marks a file
    whose every synthesizable segment is done (the 已合成 badge; a file with no synthesizable
    segments never gets it). Degrades to a zero row when the file is missing / corrupt /
    empty, or no workspace is set.

    ``out_dir`` (the package dir) switches the completion count to the batched
    ``Batch.count_completion(out_dir=…)`` existence check; omitted, the per-entry rule runs.
    """
    out = {"name": name, "total": 0, "completed": 0, "remaining": 0,
           "complete": False, "speakers": 0, "ready": 0, "missing": []}
    src = resolve_parsed_json(name)
    if not src.exists():
        return out
    try:
        data = json.loads(src.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt / empty script just reports zeros
        return out
    if not isinstance(data, list) or not data:
        return out
    segs = Batch._build_segments(data)
    pkg_dir = out_dir or layout.audio_chunk / Batch.package_for(src)
    expected_params = _expected_voice_params(layout, segs, voice_config)
    manifest = Batch.load_manifest(pkg_dir)
    c = Batch.count_completion(
        segs, manifest, out_dir=pkg_dir,
        expected_voice_params=expected_params,
    )
    out["total"], out["completed"], out["remaining"] = c["total"], c["completed"], c["remaining"]
    out["complete"] = c["total"] > 0 and c["completed"] == c["total"]
    order: list[str] = []
    _fold_script(order, {}, data)  # distinct speakers, first-appearance order (incl. NARRATOR)
    ready = [sp for sp in order
             if (voice_config.get(sp) or {}).get("alias_of") or _voice_usable(voice_config.get(sp) or {})]
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
    key = (str(layout.workspace) or "", name,
           _stat_key(src),
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


# ``scripts`` MUST be declared as a QUERY param: in this FastAPI version a bare
# ``list[...]`` default is treated as a JSON request body, and the repeated ``?scripts=``
# params are silently ignored (the 待合成 rows would all stay zero). ``Annotated`` keeps
# the plain ``None`` default, so direct (test) calls still work without going through FastAPI.
@router.get("/batch-status")
def batch_status(script: str | None = None,
                 scripts: Annotated[list[str] | None, Query()] = None) -> dict:
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
    layout = get_layout()
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
    segs = Batch._build_segments(data)
    return Batch.count_completion(
        segs, Batch.load_manifest(out_dir), out_dir=out_dir,
        expected_voice_params=_expected_voice_params(
            layout, segs, _read_voice_config(layout),
        ),
    )


# ---------------------------------------------------------------------------
# 批量合并（batch merge）：PENDING 壳 + 协调者（无注册表 / 无 stop 标志 —— merge 无
# cancel-batch 端点，取消 = 前端逐任务 control；壳被 cancel 就地终结后自然掉出投放搜索）
# ---------------------------------------------------------------------------

# Same prefetch invariant as the parse batch: tasks waiting for a merge slot stay ≤ 4.
MERGE_PREFETCH_DEPTH = 4


def _inflight_merge_packages() -> set[str]:
    """Package names (the label tail ``：{package}``) of non-terminal merge tasks.

    The same-package in-flight guard: two merge tasks on one package would write the same
    ``06_audio_merge/<包>.mp3``. Non-conflicting packages may still join a running batch —
    their tasks simply queue behind the gate. (The label format is the load-bearing
    contract shared with the frontend's F5 reattach — change all sides together.)
    """
    out = set()
    for t in get_task_manager().list():
        if t.module == "merge" and t.status not in TERMINAL:
            m = re.search(r"：(.+)$", t.label)
            if m:
                out.add(m.group(1))
    return out


def _run_merge_coordinator(ordered: list[str]) -> None:
    """Dispatch a merge batch's PENDING shells in order, keeping the prefetch bounded.

    Invariant maintained each round: the number of batch tasks *waiting for a merge slot*
    (started but not yet slot-holding) is < MERGE_PREFETCH_DEPTH. Slot holders are read
    from the process-wide ``merge_gate().active`` (only merge workers hold merge slots),
    so the gate is the hard cap and this thread only paces the start-ups.
    """
    mgr = get_task_manager()
    while True:
        tasks = [mgr.get(tid) for tid in ordered]
        if all(t is None or t.status in TERMINAL for t in tasks):
            return
        no_slot = sum(
            1 for t in tasks
            if t is not None and t.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
        )
        waiting = max(0, no_slot - merge_gate().active)
        while waiting < MERGE_PREFETCH_DEPTH:
            next_tid = next(
                (tid for tid, t in zip(ordered, tasks)
                 if t is not None and t.status is TaskStatus.PENDING),
                None,
            )
            if next_tid is None:
                break
            try:
                mgr.start(next_tid)
            except (ValueError, KeyError):
                break  # raced with a cancel — it will no longer be PENDING
            tasks = [mgr.get(tid) for tid in ordered]
            no_slot = sum(
                1 for t in tasks
                if t is not None and t.status in (TaskStatus.RUNNING, TaskStatus.PAUSED)
            )
            waiting = max(0, no_slot - merge_gate().active)
        time.sleep(0.2)


class MergeRequest(BaseModel):
    m4b: bool = False  # M4B output is a later phase; MP3 is produced for now.
    # Multi-select: package names (sub-folder names in 05_audio_chunk/, one per source
    # JSON) to merge — one Task each, run in parallel under the process-wide merge gate.
    packages: list[str] | None = None
    # Legacy single-select field, kept ONLY to detect an old pre-built frontend: any
    # non-null value is refused with an actionable error (the new UI always sends
    # explicit package names; the "most recent package" fallback is gone).
    package: str | None = None


@router.post("/merge")
def run_merge(req: MergeRequest) -> dict:
    """Start one merge Task per selected package.

    All task shells are created up front (PENDING, in request order) so the response
    carries every task_id; a coordinator thread then starts them in order with a bounded
    prefetch, and each worker takes the process-wide merge gate (hard cap =
    ``Merge.concurrency_limit()``) around its whole engine run. Guards: no workspace 409
    → legacy ``package`` field 400 (old frontend) → empty selection 400 (no most-recent
    fallback) → illegal package name 400 (traversal) → same-package in-flight 409.
    Returns ``{"task_ids": [...], "packages": [{package, task_id}, ...]}``.
    """
    _common.require_workspace()
    if req.package is not None:
        raise HTTPException(400, "前端版本过旧（仍在发送单选 package 字段）——请重新构建前端（npm run build）后刷新页面。")
    pkgs = list(dict.fromkeys(req.packages or []))  # dedupe, preserving order
    if not pkgs:
        raise HTTPException(400, "请选择要合并的音频包。")
    for p in pkgs:
        if not p or p != Path(p).name:
            raise HTTPException(400, f"非法包名：{p}")
    conflicts = [p for p in pkgs if p in _inflight_merge_packages()]
    if conflicts:
        raise HTTPException(409, "以下包已有合并任务在途：" + "、".join(conflicts))
    set_merge_concurrency(Merge.concurrency_limit())
    mgr = get_task_manager()
    created = [
        {
            "package": p,
            "task_id": mgr.create(
                "merge",
                ("合并 M4B" if req.m4b else "合并音频（Merge）") + f"：{p}",
                Merge.run, req.m4b, p, start=False,
            ).id,
        }
        for p in pkgs
    ]
    threading.Thread(
        target=_run_merge_coordinator, args=([c["task_id"] for c in created],), daemon=True,
    ).start()
    return {"task_ids": [c["task_id"] for c in created], "packages": created}


def _package_merge_status(name: str, layout) -> dict:
    """One row of the merge page's package list (``GET /merge-status?packages=…``).

    ``total`` = the source parsed JSON's synthesizable segment count — the same rule as
    ``/batch-status`` (a manifest-length total would mark a mid-cancelled package "ready"
    and silently merge a half book). Only when the source JSON is missing / corrupt /
    empty does the row degrade to the manifest length. ``completed`` = ok manifest entries
    whose file is still on disk (``Batch.is_done``); ``complete`` = every segment done —
    the 已就绪 badge.
    """
    out = {"name": name, "total": 0, "completed": 0, "remaining": 0, "complete": False}
    manifest = Batch.load_manifest(layout.audio_chunk / name)
    src = layout.parsed_json / f"{name}.json"
    if src.exists():
        try:
            data = json.loads(src.read_text("utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt source just degrades to the manifest
            data = None
        if isinstance(data, list) and data:
            segs = Batch._build_segments(data)
            c = Batch.count_completion(
                segs, manifest, out_dir=layout.audio_chunk / name,
                expected_voice_params=_expected_voice_params(
                    layout, segs, _read_voice_config(layout),
                ),
            )
            out["total"] = c["total"]
            out["completed"] = c["completed"]
            out["remaining"] = c["remaining"]
            out["complete"] = c["total"] > 0 and c["completed"] == c["total"]
            return out
    out["total"] = len(manifest)  # degrade: source JSON missing / corrupt / empty
    out["completed"] = len(Batch.done_indices(manifest, layout.audio_chunk / name,
                                              layout.workspace))
    out["remaining"] = out["total"] - out["completed"]
    out["complete"] = out["total"] > 0 and out["completed"] == out["total"]
    return out


# ``packages`` MUST be declared as a QUERY param: in this FastAPI version a bare
# ``list[...]`` default is treated as a JSON request body and the repeated ``?packages=``
# params are silently ignored (same trap as ``/batch-status``). ``Annotated`` keeps the
# plain ``None`` default, so direct (test) calls still work without going through FastAPI.
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
    layout = get_layout()
    if layout.audio_chunk is None:  # no workspace: nothing to read (read-only, degrades)
        return {"packages": [
            {"name": p, "total": 0, "completed": 0, "remaining": 0, "complete": False}
            for p in names
        ]}
    return {"packages": [_package_merge_status(p, layout) for p in names]}
