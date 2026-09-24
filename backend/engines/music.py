"""The global music library engine (背景音乐系统 · 音乐库).

The library lives at the PROJECT ROOT (``core_paths.MUSIC_LIBRARY_DIR``), OUTSIDE
any workspace: music files + one shared index (``music_index.json``) are
project-level resources shared by every workspace. It is deliberately a fixed
code constant — config is per-workspace, so a per-project setting that pinned a
global path would be self-contradictory.

Design notes
------------
* **Single index, not per-track sidecars**: tag rename / delete / batch ops all
  need to read-then-atomically-write every track at once; a sidecar mid-failure
  would leave inconsistent state. At library scale (tens to hundreds of
  tracks) one small JSON file is a non-issue.
* **Tags are 4-bucketed** (scene / mood / emotion / custom): the built-in
  vocabulary contains a name in two buckets (悲伤 = mood + emotion), so bucket
  identity — not name identity — disambiguates at match time. Weights come from
  the bucket (mood 3 > scene 2 > emotion 1, custom +1). New/rename still 409 on
  a name that already exists in *any* bucket (no third occurrence); the
  built-in cross-bucket pair is grandfathered.
* **Atomic writes**: ``write_bytes`` + ``os.replace`` under a module lock (the
  voice_config.json precedent). Reads never write; a missing index degrades to
  the DEFAULT_TAGS-seeded empty index in memory (not persisted), a corrupt one
  degrades with a WARNING.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from backend.core import paths as core_paths
from backend.core.concurrency import gate
from backend.core.file_lock import exclusive_file_lock
from backend.core.tasks import TaskCancelled
from backend.engines.llm_transport import request_chat_completion as _llm_chat_completion
from backend.engines.script import (
    LLMJSONRetryExhausted,
    llm_json_with_retry,
)
from backend.engines.voices import extract_json_object

log = logging.getLogger("audiobook.music")

# -- tag model --------------------------------------------------------------- #

#: The four tag categories (buckets), in display order.
TAG_CATEGORIES = ("scene", "mood", "emotion", "custom")

#: Match weights per category (气氛 > 场景 > 情绪; custom participates at +1).
TAG_WEIGHTS = {"mood": 3, "scene": 2, "emotion": 1, "custom": 1}

#: Built-in tag vocabulary (plan.md §三). Note: 悲伤 intentionally appears in
#: both mood and emotion — bucketing disambiguates; new/rename 409s only on a
#: name already present in ANY bucket.
DEFAULT_TAGS: dict[str, list[str]] = {
    "scene": ["日常", "战斗", "冒险", "旅行", "宫廷", "城市", "森林", "夜晚", "宴会", "爱情"],
    "mood": ["轻松", "温馨", "欢快", "平静", "神秘", "紧张", "压抑", "悲伤", "恐怖", "庄严", "热血", "史诗"],
    "emotion": ["希望", "喜悦", "愤怒", "悲伤", "孤独", "浪漫", "激动", "绝望", "沉重"],
    "custom": [],
}

#: Uploadable audio extensions (lowercase, with dot).
ALLOWED_EXTS = {".mp3", ".wav", ".flac"}

#: Max length for a user-created folder name (folders are INDEX METADATA —
#: there is no filesystem entity — but the name still guards the API route
#: and the UI, so keep it tight).
FOLDER_NAME_MAX = 50

_INDEX_NAME = "music_index.json"

# Module-level lock for the read-modify-write transactions on the index.
_INDEX_LOCK = threading.RLock()


def _empty_track_tags() -> dict[str, list[str]]:
    return {c: [] for c in TAG_CATEGORIES}


# -- pure functions ---------------------------------------------------------- #

def normalize_track_tags(raw: dict | None, registry: dict[str, list[str]]) -> dict[str, list[str]]:
    """Normalise a client-supplied tag set into the four buckets.

    For each non-custom bucket only names present in that bucket's registry
    vocabulary are kept (first-appearance order, de-duplicated); everything
    else (out-of-vocabulary names, garbage, the client's own custom list) is
    folded into ``custom``. A non-dict / missing input yields four empty
    buckets. The registry is the live index's ``tags`` section.
    """
    out = _empty_track_tags()
    if not isinstance(raw, dict):
        return out
    for cat in TAG_CATEGORIES:
        vals = raw.get(cat)
        if not isinstance(vals, list):
            continue
        vocab = registry.get(cat) if isinstance(registry.get(cat), list) else []
        for v in vals:
            if not isinstance(v, str):
                continue
            name = v.strip()
            if not name:
                continue
            if cat == "custom" or name in vocab:
                if name not in out[cat]:
                    out[cat].append(name)
            elif name not in out["custom"]:
                out["custom"].append(name)
    return out


def is_generic_track(track: dict) -> bool:
    """A usable fallback: enabled with ALL FOUR buckets empty (「通用音乐」)."""
    if not isinstance(track, dict) or not track.get("enabled", False):
        return False
    tags = track.get("tags")
    if not isinstance(tags, dict):
        return False
    return all(
        not [t for t in (tags.get(c) or []) if isinstance(t, str) and t.strip()]
        for c in TAG_CATEGORIES
    )


def find_tag_category(registry: dict[str, list[str]], name: str) -> str | None:
    """The FIRST category (in :data:`TAG_CATEGORIES` order) whose registry
    bucket contains ``name`` — or ``None`` when the name is not registered."""
    for cat in TAG_CATEGORIES:
        vals = registry.get(cat)
        if isinstance(vals, list) and name in vals:
            return cat
    return None


def all_tag_names(registry: dict[str, list[str]]) -> set[str]:
    """Every registered tag name (across all four buckets)."""
    names: set[str] = set()
    for cat in TAG_CATEGORIES:
        vals = registry.get(cat)
        if isinstance(vals, list):
            names.update(v for v in vals if isinstance(v, str) and v.strip())
    return names


def apply_tag_rename(
    index: dict, category: str, old: str, new: str
) -> int:
    """Rename a registered tag ``old`` -> ``new`` IN ``category`` (mutates
    ``index`` in place; the caller holds the lock / persists).

    Propagates to the registry bucket and to every track's ``tags[category]``.
    Returns the number of tracks whose tag list changed. Pre-conditions (old
    present, new absent globally) are the API layer's job.
    """
    bucket = index.get("tags", {}).get(category)
    if isinstance(bucket, list) and old in bucket:
        bucket[bucket.index(old)] = new
    tracks = index.get("tracks")
    if not isinstance(tracks, dict):
        return 0
    affected = 0
    for tr in tracks.values():
        if not isinstance(tr, dict):
            continue
        ttags = tr.get("tags")
        if not isinstance(ttags, dict):
            continue
        lst = ttags.get(category)
        if isinstance(lst, list) and old in lst:
            ttags[category] = [new if v == old else v for v in lst]
            affected += 1
    return affected


def apply_tag_delete(index: dict, category: str, name: str) -> int:
    """Remove a tag from the registry bucket and every track (mutates
    ``index`` in place). Returns the number of tracks whose tag list changed.
    Never touches music files (plan.md: 删除标签仅移除标签)."""
    bucket = index.get("tags", {}).get(category)
    if isinstance(bucket, list) and name in bucket:
        bucket.remove(name)
    tracks = index.get("tracks")
    if not isinstance(tracks, dict):
        return 0
    affected = 0
    for tr in tracks.values():
        if not isinstance(tr, dict):
            continue
        ttags = tr.get("tags")
        if not isinstance(ttags, dict):
            continue
        lst = ttags.get(category)
        if isinstance(lst, list) and name in lst:
            ttags[category] = [v for v in lst if v != name]
            affected += 1
    return affected


def validate_music_name(name: str) -> str:
    """Guard a music file name: no traversal, allowed extension.

    Returns the bare name; raises ``ValueError`` (-> HTTP 400) otherwise.
    """
    if not isinstance(name, str):
        raise ValueError("非法音乐文件名。")
    bare = Path(name).name
    if not name or bare != name or not bare.strip():
        raise ValueError(f"非法音乐文件名：{name!r}")
    ext = Path(bare).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"仅支持 MP3 / WAV / FLAC（收到 {ext or '无扩展名'} 文件）。")
    return bare


# -- folders (index metadata — the library dir stays FLAT on disk) ---------- #
#
# Folders are a user-organisation layer over the track index: ``index["folders"]``
# maps name -> {"created_at"}, each track carries ``folder`` ("" = uncategorised,
# i.e. "uploaded to the library root"). Deliberately NOT physical subdirectories:
# the bare file name is the GLOBAL reference key (BGM assignments, preview route,
# AI suggestion cache), so same-named files must stay globally unique and the
# flat library + same-name 409 semantics are load-bearing. BGM matching reads
# only ``enabled`` + ``tags``, so folder membership never affects it.

def validate_folder_name(name: str) -> str:
    """Guard a user-created folder name (-> stripped name, else ValueError 400).

    Folders are metadata (no filesystem entity), but the name guards the API
    route (``{folder}`` path param) and the UI, so: non-empty after strip, no
    ``/`` / ``\\``, max :data:`FOLDER_NAME_MAX` characters.
    """
    if not isinstance(name, str):
        raise ValueError("非法文件夹名称。")
    bare = name.strip()
    if not bare:
        raise ValueError("文件夹名称不能为空。")
    if "/" in bare or "\\" in bare:
        raise ValueError(f"文件夹名称不能包含 / 或 \\（收到 {name!r}）。")
    if len(bare) > FOLDER_NAME_MAX:
        raise ValueError(f"文件夹名称过长（最多 {FOLDER_NAME_MAX} 字）。")
    return bare


def folder_counts(index: dict) -> dict[str, int]:
    """Track count per folder key (folders with zero tracks included).

    Tracks whose ``folder`` points at a key missing from ``index["folders"]``
    cannot occur after :func:`_coerce_index` (orphan references degrade to
    ""), but are defensively counted as uncategorised (ignored here).
    """
    folders = index.get("folders")
    names = list(folders) if isinstance(folders, dict) else []
    counts = {n: 0 for n in names}
    tracks = index.get("tracks")
    if isinstance(tracks, dict):
        for tr in tracks.values():
            if not isinstance(tr, dict):
                continue
            f = tr.get("folder")
            if isinstance(f, str) and f in counts:
                counts[f] += 1
    return counts


def create_folder(index: dict, name: str) -> None:
    """Register a new folder (mutates ``index`` in place; the caller holds the
    lock / persists). The duplicate-name precondition is the API layer's job
    (409 under the same transaction)."""
    index.setdefault("folders", {})[name] = {
        "created_at": datetime.now().isoformat(timespec="seconds")
    }


def apply_folder_rename(index: dict, old: str, new: str) -> int:
    """Rename folder ``old`` -> ``new`` (mutates ``index`` in place),
    propagating to every track's ``folder`` field (mirror of
    :func:`apply_tag_rename`). ``created_at`` is preserved. Returns the number
    of tracks whose field changed. Pre-conditions (old present, new absent)
    are the API layer's job."""
    folders = index.get("folders")
    if not isinstance(folders, dict) or old not in folders:
        return 0
    entry = folders.pop(old)
    folders[new] = entry
    tracks = index.get("tracks")
    if not isinstance(tracks, dict):
        return 0
    affected = 0
    for tr in tracks.values():
        if isinstance(tr, dict) and tr.get("folder") == old:
            tr["folder"] = new
            affected += 1
    return affected


def delete_folder(index: dict, name: str) -> bool:
    """Remove a folder key (mutates ``index`` in place). The EMPTY precondition
    is the API layer's job (409 non-empty); track files are never touched.
    Returns True when the key existed."""
    folders = index.get("folders")
    if isinstance(folders, dict) and name in folders:
        folders.pop(name)
        return True
    return False


def apply_track_move(index: dict, names: list[str], folder: str) -> tuple[list[str], list[str]]:
    """Set the ``folder`` field on the given tracks (mutates ``index`` in
    place; ``folder = ""`` = move to uncategorised / root). Returns
    ``(moved, missing)`` — missing names are reported, not an error (batch
    endpoint precedent). The target-folder existence precondition is the API
    layer's job."""
    tracks = index.get("tracks")
    moved: list[str] = []
    missing: list[str] = []
    for n in names:
        tr = tracks.get(n) if isinstance(tracks, dict) else None
        if not isinstance(tr, dict):
            missing.append(n)
            continue
        tr["folder"] = folder
        moved.append(n)
    return moved, missing


# -- index IO (module lock + atomic os.replace; reads never write) ----------- #

def _library_dir() -> Path:
    # Module-attribute access at call time: tests monkeypatch
    # ``core_paths.MUSIC_LIBRARY_DIR`` and it must take effect here.
    return core_paths.MUSIC_LIBRARY_DIR


def _index_path() -> Path:
    return _library_dir() / _INDEX_NAME


def _default_index() -> dict:
    return {
        "version": 1,
        "tags": copy.deepcopy(DEFAULT_TAGS),
        "folders": {},
        "tracks": {},
    }


def _coerce_duration(v) -> float:
    """A finite float, else 0.0 (a corrupt duration string must not nuke the
    whole index — per-field degradation)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return float(v)
    return 0.0


def _coerce_index(data) -> dict:
    """Coerce parsed JSON into a well-formed index (degrading corrupt shapes).

    LOAD-TIME INVARIANT (folders): a track's ``folder`` is either a key of
    ``index["folders"]`` or ``""`` (uncategorised) — an orphan reference
    (folder key missing, corrupt hand-edit) degrades to "" so the UI never
    shows a row pointing at a non-existent folder.
    """
    if not isinstance(data, dict):
        return _default_index()
    idx = _default_index()
    if isinstance(data.get("tags"), dict):
        for cat in TAG_CATEGORIES:
            vals = data["tags"].get(cat)
            if isinstance(vals, list):
                idx["tags"][cat] = [v for v in vals if isinstance(v, str) and v.strip()]
        # Preserve any registered names we know how to bucket; unknown
        # category keys are dropped (the schema is fixed at four buckets).
    folders = data.get("folders")
    if isinstance(folders, dict):
        for fname, entry in folders.items():
            if not isinstance(fname, str) or not fname:
                continue
            created = entry.get("created_at") if isinstance(entry, dict) else None
            idx["folders"][fname] = {
                "created_at": created if isinstance(created, str) else "",
            }
    tracks = data.get("tracks")
    if isinstance(tracks, dict):
        known_folders = set(idx["folders"])
        for name, tr in tracks.items():
            if not isinstance(name, str) or not isinstance(tr, dict):
                continue
            tags = tr.get("tags")
            folder = tr.get("folder")
            idx["tracks"][name] = {
                "duration": _coerce_duration(tr.get("duration")),
                "enabled": bool(tr.get("enabled", True)),
                "description": tr.get("description") if isinstance(tr.get("description"), str) else "",
                "tags": normalize_track_tags(tags, idx["tags"]),
                "added_at": tr.get("added_at") if isinstance(tr.get("added_at"), str) else "",
                "folder": folder if isinstance(folder, str) and folder in known_folders else "",
            }
    return idx


def load_index() -> dict:
    """Read the index. Missing -> DEFAULT_TAGS-seeded empty index (NOT written
    to disk — reads never write). Corrupt -> the same degraded index + WARNING."""
    p = _index_path()
    if not p.exists():
        return _default_index()
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
        return _coerce_index(data)
    except Exception as e:
        log.warning("音乐库索引损坏，降级为空索引：%s", e)
        return _default_index()


def save_index(index: dict, handle=None) -> None:
    """Atomically write the index (write_bytes + os.replace). Caller holds the
    lock (or accepts concurrent writers — os.replace is atomic either way)."""
    d = _library_dir()
    payload = json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")
    stage_file = getattr(handle, "stage_shared_file", None)
    if callable(stage_file):
        stage_file(_index_path(), payload)
        return
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".music_index_", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, _index_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_index(mutator, handle=None) -> dict:
    """Atomic read -> mutate -> write transaction on the index (shared by
    upload / tag edits / batch ops / tag management).

    ``mutator(index)`` may raise to abort the transaction (nothing is written;
    the file, if already written by the mutator, is the mutator's concern).
    Returns the (saved) index.
    """
    with _INDEX_LOCK:
        with exclusive_file_lock(_library_dir() / ".music_index.lock"):
            idx = load_index()
            mutator(idx)
            save_index(idx, handle)
            return idx


# --------------------------------------------------------------------------- #
# AI tag suggestions cache (music_tag_suggestions.json — candidates only)
#
# Batch AI tag recognition (one Task per track, module ``music-ai-tags``)
# writes its CANDIDATE tags here. On success the worker AUTO-ADOPTS the
# candidate into the index when the track is still untagged (no manual tag
# decision — 2026-09: the one-click batch path must not force a manual
# 编辑-采用 step); auto-adopted candidates are consumed right away, so a
# successful run on an untagged track normally leaves no entry behind. A
# track the user tagged meanwhile KEEPS its candidate for the「AI 推荐采用」
# button (POST /tracks/apply-suggestions) or the tag editor (PUT /tracks/{name}
# with tags) — manual decisions are NEVER overwritten, and confirming either
# way consumes the entry. Orphan entries (track deleted afterwards) are
# harmless dead weight — the UI joins rows against the index and the API
# filters them out of GET /library (same orphan policy as BGM assignments).
# --------------------------------------------------------------------------- #

_SUGGESTIONS_NAME = "music_tag_suggestions.json"

_SUGGESTIONS_LOCK = threading.RLock()


def _suggestions_path() -> Path:
    return _library_dir() / _SUGGESTIONS_NAME


def _default_suggestions() -> dict:
    return {"version": 1, "tracks": {}}


def _coerce_suggestions(data) -> dict:
    """Coerce parsed JSON into a well-formed suggestions cache (degrading
    corrupt shapes)."""
    if not isinstance(data, dict):
        return _default_suggestions()
    out = _default_suggestions()
    tracks = data.get("tracks")
    if isinstance(tracks, dict):
        for name, entry in tracks.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            out["tracks"][name] = {
                "tags": _coerce_suggestion_tags(entry.get("tags")),
                "suggested_at": entry.get("suggested_at") if isinstance(entry.get("suggested_at"), str) else "",
                "model": entry.get("model") if isinstance(entry.get("model"), str) else "",
            }
    return out


def _coerce_suggestion_tags(tags) -> dict[str, list[str]]:
    """The three LLM buckets (scene/mood/emotion), lists of strings. The
    suggestions cache holds the LLM's RAW candidates — no registry filtering
    here (the registry may legitimately outdate a cached suggestion; the
    vocabulary filter already happened at LLM reply time)."""
    out: dict[str, list[str]] = {}
    for cat in ("scene", "mood", "emotion"):
        vals = tags.get(cat) if isinstance(tags, dict) else None
        out[cat] = [v for v in vals if isinstance(v, str) and v.strip()] if isinstance(vals, list) else []
    return out


def load_suggestions() -> dict:
    """Read the suggestions cache. Missing -> empty cache in memory (NOT
    written to disk — reads never write). Corrupt -> degraded + WARNING."""
    p = _suggestions_path()
    if not p.exists():
        return _default_suggestions()
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
        return _coerce_suggestions(data)
    except Exception as e:
        log.warning("AI 标签推荐缓存损坏，降级为空缓存：%s", e)
        return _default_suggestions()


def save_suggestions(data: dict, handle=None) -> None:
    """Atomically write the suggestions cache (write_bytes + os.replace)."""
    d = _library_dir()
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    stage_file = getattr(handle, "stage_shared_file", None)
    if callable(stage_file):
        stage_file(_suggestions_path(), payload)
        return
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".music_suggest_", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, _suggestions_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_suggestions(mutator, handle=None) -> dict:
    """Atomic read -> mutate -> write transaction on the suggestions cache
    (parallel AI tasks each rewrite the whole file — the voice_config.json /
    08_bgm analysis precedent)."""
    with _SUGGESTIONS_LOCK:
        with exclusive_file_lock(_library_dir() / ".music_suggestions.lock"):
            data = load_suggestions()
            mutator(data)
            save_suggestions(data, handle)
            return data


def clear_suggestion(name: str, handle=None) -> None:
    """Drop one track's candidate entry (the user made a tag decision).
    No-op when absent — reads never write, the file is only touched when an
    entry actually exists."""
    if name in (load_suggestions().get("tracks") or {}):
        update_suggestions(lambda d: d["tracks"].pop(name, None), handle)


def track_has_manual_tags(track: dict) -> bool:
    """Whether the user already made a tag decision (ANY of the four buckets
    non-empty). Such tracks are never overwritten by AI candidates — the
    shared criterion of the「AI 推荐采用」endpoint and the worker auto-adoption
    (a non-dict track has no decision to protect)."""
    tags = track.get("tags") if isinstance(track, dict) else None
    if not isinstance(tags, dict):
        return False
    return any(tags.get(c) for c in TAG_CATEGORIES)


def auto_apply_suggestion(name: str, cand: dict, handle=None) -> bool:
    """Auto-adopt an AI candidate into the track's tags (2026-09：批量一键
    识别成功后，未手动打标的曲目直接落标签，不再要求用户手动「编辑-采用」).

    The check AND the write happen inside ONE ``update_index`` transaction:
    when the user already tagged the track (any bucket non-empty) nothing is
    touched (a manual edit landing microseconds earlier still wins — the same
    no-overwrite rule ``POST /tracks/apply-suggestions`` enforces at write
    time). Names out of the CURRENT registry are folded into custom (the same
    normalize criterion as a user-confirmed PUT). Returns True when the tags
    were adopted (the caller then consumes the candidate).
    """
    applied = False

    def _mutate(idx: dict) -> None:
        nonlocal applied
        tr = idx["tracks"].get(name)
        if not isinstance(tr, dict) or track_has_manual_tags(tr):
            return
        tr["tags"] = normalize_track_tags(cand, idx["tags"])
        applied = True

    update_index(_mutate, handle)
    return applied


# --------------------------------------------------------------------------- #
# AI tag recommendation prompts / parsing (shared by the sync single-track
# endpoint and the per-track Task worker — text-only: filename + description
# + vocabulary, the LLM never reads the audio)
# --------------------------------------------------------------------------- #

#: Per-bucket caps for LLM-suggested tags (防 LLM 造词/过量).
SUGGESTION_CAPS = {"scene": 2, "mood": 3, "emotion": 2}


def build_suggestion_prompts(stem: str, description: str,
                             registry: dict[str, list[str]]) -> tuple[str, str]:
    """(system, user) prompt for tag suggestion from the file NAME (``stem``)
    + user DESCRIPTION + the tag vocabulary."""
    vocab = {c: registry.get(c, []) for c in TAG_CATEGORIES if c != "custom"}
    vocab_text = "\n".join(f"{c}: {'、'.join(v)}" for c, v in vocab.items())
    system = (
        "你是有声书背景音乐标签助手。根据音乐文件名和用户描述，从给定词表中选择标签。"
        "只能从词表中选择，禁止创造新词。输出 JSON：{\"scene\": [...], \"mood\": [...], "
        "\"emotion\": [...]}，scene 最多 2 个、mood 最多 3 个、emotion 最多 2 个，"
        "选不出就留空数组。只输出 JSON，不要解释。"
    )
    user = f"文件名：{stem}\n用户描述：{description or '（无）'}\n\n词表：\n{vocab_text}"
    return system, user


def parse_suggestion_reply(content: str, registry: dict[str, list[str]]) -> dict | None:
    """Parse an LLM suggestion reply (a JSON OBJECT — ``extract_json_object``)
    into the three tag buckets, keeping ONLY in-vocabulary names per category
    and capping each bucket (anti word-coining / overflow). ``None`` when the
    reply is not a JSON object (the caller retries)."""
    data = extract_json_object(content)
    if not isinstance(data, dict):
        return None
    out: dict[str, list[str]] = {}
    for cat in ("scene", "mood", "emotion"):
        vals = data.get(cat)
        vocab = [v for v in (registry.get(cat) or []) if isinstance(v, str)]
        keep: list[str] = []
        if isinstance(vals, list):
            for v in vals:
                if isinstance(v, str) and v.strip() and v.strip() in vocab and v.strip() not in keep:
                    keep.append(v.strip())
        out[cat] = keep[: SUGGESTION_CAPS[cat]]
    return out


# --------------------------------------------------------------------------- #
# Task worker: one-track AI tag recognition (module ``music-ai-tags``)
# --------------------------------------------------------------------------- #

def suggest_track_tags(handle, name: str, llm_cfg, description: str | None = None) -> dict:
    """Task worker: LLM-recommend tags for ONE track (2 attempts with
    parse-error feedback retry, same prompt semantics as the sync single-track
    endpoint).

    Slot scope = the whole task (no check phase): the LLM call holds one shared
    LLM slot (``gate()``); a cancel while queued aborts without taking a slot.
    A total failure fails the TASK (the UI offers 重试).

    On success the candidate is AUTO-ADOPTED into the track's tags when the
    track has no manual tags yet (2026-09：一键识别后未手动打标的条目直接生效)
    and its candidate entry is consumed (incl. any stale one) — no entry is
    written in that case. When the track is already tagged (or the candidate
    has no tags at all) the candidate lands in
    ``music_tag_suggestions.json`` (only this track's entry is rewritten) for
    the user to confirm in the UI.
    """
    handle.check()
    lib_dir = _library_dir()
    if not (lib_dir / name).is_file():
        raise RuntimeError(f"音乐库中找不到 {name}。")
    if not llm_cfg.model_name:
        raise RuntimeError("尚未配置 LLM 模型（设置 → LLM → model_name）。")

    idx = load_index()
    tr = idx["tracks"].get(name)
    description = ((tr or {}).get("description") if description is None else description or "").strip()
    system, user = build_suggestion_prompts(Path(name).stem, description, idx["tags"])

    handle.progress(0.05, "排队中（等待并发槽位）")
    if not gate().acquire(stop_check=lambda: handle.cancelled):
        raise TaskCancelled()  # 排队中被取消——未取槽，不进入 try、不 release
    try:
        try:
            parsed, _attempts = llm_json_with_retry(
                llm_cfg, system, user,
                lambda c: parse_suggestion_reply(c, idx["tags"]),
                handle=handle, llm_call=_llm_chat_completion,
                max_attempts=2,
                # 思考模型（如 LM Studio 的 *-mtp 系列）的推理 token 与正文
                # 共用 max_tokens 预算——关掉思考（严格网关拒绝该键时传输层
                # 自动退化为不带该键的单次重试）；2048 = 服务端忽略该键时的
                # 安全网（300 在思考模型上会耗尽预算 → content 为空）。
                max_tokens=2048,
                extra_body={"enable_thinking": False},
                format_hint='{"scene": [...], "mood": [...], "emotion": [...]}',
                operation_type="music.suggest_tags",
            )
        except LLMJSONRetryExhausted as e:
            raise RuntimeError(
                f"AI 推荐失败（{e.last_err}），请重试或手动打标。") from e

        parts = [f"{c} {', '.join(parsed[c])}" for c in ("scene", "mood", "emotion") if parsed[c]]
        if any(parsed.get(c) for c in ("scene", "mood", "emotion")):
            if auto_apply_suggestion(name, parsed, handle):
                # 未手动打标 → AI 结果直接落曲目标签并消费候选（含陈旧条目）；
                # 已手动打标绝不覆盖——走 update_suggestions 保留候选待确认。
                clear_suggestion(name, handle)
                handle.log("识别完成（未手动打标，已自动采用）：" + "、".join(parts))
                handle.progress(1.0, "完成")
                return parsed
        update_suggestions(
            lambda d: d["tracks"].__setitem__(name, {
                "tags": parsed,
                "suggested_at": datetime.now().isoformat(timespec="seconds"),
                "model": llm_cfg.model_name,
            }),
            handle,
        )
        handle.log("识别完成：" + ("、".join(parts) if parts else "（无标签）"))
        handle.progress(1.0, "完成")
        return parsed
    finally:
        gate().release()  # acquire 成功才进入 try——排队中被取消的路径未取槽、不到这里
