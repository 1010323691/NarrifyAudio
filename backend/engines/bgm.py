"""The BGM engine (背景音乐系统 · 阶段 3/4): chapter mood analysis (LLM),
deterministic tag matching, and the final per-chapter mix.

Artifacts (all under the workspace's ``08_bgm/``):
* ``chapter_music_analysis.json`` — LLM mood tags per chapter (a CACHE: tag
  edits / re-matching never re-call the LLM);
* ``segment_music_analysis.json`` — LLM paragraph-level BGM needs per chapter
  (a CACHE: the entry fingerprint anchors staleness; the timeline below can be
  re-derived from it with ZERO LLM calls after TTS durations change);
* ``timelines/<stem>.json`` — the paragraph-level BGM timeline (spans with
  start/end/music_id/volume) computed from the REAL 05_audio_chunk durations
  (ffprobe) and the merge pause rule — never from text length;
* ``bgm_assignments.json`` — per-chapter match result (music = a bare music
  library FILE NAME — the library lives outside the workspace, so it never
  enters the pathio migration chain);
* ``<stem>.mp3`` — the final mixed audio (chapters without BGM are a plain
  copy of the 06 narration — still a finished product).

The music library itself (``music_library/`` at the project root) is managed
by :mod:`backend.engines.music`; this module reads it (for matching) and
references it by file name (for mixing). The LLM analysis faces (chapter AND
paragraph) are the write paths here: out-of-vocabulary tags they find are
registered into the global tag registry (``music.update_index``) so later
chapters and manual track tagging can reuse them. The LLM only ever DESCRIBES
music needs (tags + intensity + a per-paragraph action) — it never picks a
music file; matching is mechanical (``score_track`` / ``match_chapter``), and
the timeline mix reads the Timeline only (no semantic judgement at mix time).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from backend.core import paths as core_paths
from backend.core.config import get_config
from backend.core.concurrency import gate, merge_gate
from backend.core.tasks import TaskCancelled
from backend.engines.audio import probe_duration
from backend.engines.book import decode_buffer
from backend.engines import music as music_engine
from backend.engines import tts_batch
from backend.engines.merge import boundary_gap_ms, collect_segments, thread_budget
from backend.engines.llm_transport import request_chat_completion as _llm_chat_completion
from backend.engines.script import (
    LLMJSONRetryExhausted,
    ParseRejected,
    llm_json_with_retry,
)
from backend.engines.voices import extract_json_object

log = logging.getLogger("audiobook.bgm")

# -- artifact file names (inside ``08_bgm/``) -------------------------------- #

ANALYSIS_NAME = "chapter_music_analysis.json"
ASSIGNMENTS_NAME = "bgm_assignments.json"
SEGMENT_ANALYSIS_NAME = "segment_music_analysis.json"
TIMELINE_DIR = "timelines"

#: Tolerance (seconds) for the narration-duration freshness check at mix time:
#: the timeline's recorded duration vs a fresh probe of the 06 file.
_STALE_TOLERANCE_S = 0.5

#: Per-bucket caps for LLM chapter-analysis tags (anti overflow).
_ANALYSIS_CAPS = {"scene": 2, "mood": 3, "emotion": 2, "custom": 2}

#: The exact JSON shape the analysis LLM must return (echoed into the prompt
#: and into every parse-failure retry).
_ANALYSIS_FORMAT_HINT = (
    '{"scene": [...], "mood": [...], "emotion": [...], "custom": [...]}'
)

#: The exact JSON shape the paragraph-analysis LLM must return (one array of
#: contiguous SCENE BLOCKS — several entries map to one scene, gaps between
#: blocks are intentional silence, and an ``extend`` block in a non-first
#: batch continues the previous batch's last scene).
_SEGMENT_FORMAT_HINT = (
    '[{"start_segment": 0, "end_segment": 9, "scene": "场景简述", "mood": "气氛简述", '
    '"music_tags": {"scene": [...], "mood": [...], "emotion": [...], "custom": [...]}, '
    '"intensity": 2, "reason": "换曲理由"}, '
    '{"start_segment": 10, "end_segment": 19, "extend": true}, ...]'
)

#: A scene span shorter than this (real 05 durations) is folded into a
#: neighbouring scene during the deterministic timeline validation (短段并入 —
#: too short to read as its own musical unit). Module constant by design (no
#: config item — the project's constant-policy pattern for this kind of rule).
_MIN_SPAN_S = 20.0

#: Match weights, heaviest first (reason strings list categories in this order).
_WEIGHT_ORDER = ("mood", "scene", "emotion", "custom")

# Module lock for the two 08_bgm JSON caches (parallel analysis tasks each
# rewrite the whole analysis file — the voice_config.json precedent).
_BGMS_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# pure functions (rng injectable — everything here is unit-testable)
# --------------------------------------------------------------------------- #

def sample_chapter_text(text: str, n: int) -> str:
    """The text handed to the LLM: the whole text when it fits in ``n`` chars,
    else head / middle / tail windows of ``n // 3`` joined by ``\\n……\\n``."""
    n = max(0, int(n))
    if len(text) <= n:
        return text
    w = max(1, n // 3)
    head = text[:w]
    mid_start = max(0, (len(text) - w) // 2)
    mid = text[mid_start:mid_start + w]
    tail = text[-w:]
    parts = [s for s in (head, mid, tail) if s]
    # a window may repeat when the text is barely longer than n — keep first-seen
    seen: set[str] = set()
    out: list[str] = []
    for s in parts:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return "\n……\n".join(out)


def parse_analysis_reply(content: str) -> dict | None:
    """Parse an LLM analysis reply into the four tag buckets.

    Returns ``{scene, mood, emotion, custom}`` (each capped at
    :data:`_ANALYSIS_CAPS`, de-duplicated, stripped) or ``None`` when the reply
    is not a JSON object (the caller retries). Out-of-vocabulary names are
    KEPT (chapter tags are free-form analysis the user can edit — unlike
    suggest-tags, which is strict in-vocabulary).
    """
    data = extract_json_object(content)
    if not isinstance(data, dict):
        return None
    out: dict[str, list[str]] = {}
    for cat in music_engine.TAG_CATEGORIES:
        vals = data.get(cat)
        if not isinstance(vals, list):
            out[cat] = []
            continue
        kept: list[str] = []
        for v in vals:
            if not isinstance(v, str):
                continue
            name = v.strip()
            if name and name not in kept:
                kept.append(name)
        out[cat] = kept[: _ANALYSIS_CAPS[cat]]
    return out


def score_track(chapter_tags: dict, track_tags: dict) -> tuple[int, str]:
    """Weighted tag-overlap score between a chapter's analysis and one track.

    Same-bucket intersection × category weight (mood 3 / scene 2 / emotion 1 /
    custom 1). The reason string is pinned: ``"mood 命中 紧张, 热血(+6)；scene
    命中 战斗(+2)"`` — one segment per hit category, heaviest first.
    """
    total = 0
    parts: list[str] = []
    if isinstance(chapter_tags, dict) and isinstance(track_tags, dict):
        for cat in _WEIGHT_ORDER:
            a = chapter_tags.get(cat)
            b = track_tags.get(cat)
            if not isinstance(a, list) or not isinstance(b, list):
                continue
            b_set = {t for t in b if isinstance(t, str) and t.strip()}
            hit = [t for t in dict.fromkeys(a) if isinstance(t, str) and t in b_set]
            if hit:
                pts = len(hit) * music_engine.TAG_WEIGHTS[cat]
                total += pts
                parts.append(f"{cat} 命中 {', '.join(hit)}(+{pts})")
    return total, "；".join(parts)


def match_chapter(
    chapter_tags: dict,
    tracks: list[tuple[str, dict]],
    min_score: int = 1,
    prev_music: str | None = None,
    next_music: str | None = None,
    rng: random.Random | None = None,
    mode: str = "llm",
) -> dict:
    """Pick one track (or None) for a chapter.

    ``tracks`` = ``[(name, track_entry), …]`` from the library index (enabled
    filtering is re-applied defensively here). Order:
    1. highest score (random among ties, ``rng``); a score below ``min_score``
       yields no tagged candidate;
    2. else the GENERIC pool (enabled + all four buckets empty), same
       adjacent-chapter de-dup;
    3. else ``music=None``.

    Adjacent de-dup: the ``prev_music`` / ``next_music`` file names are
    excluded from the candidate set; when that blocks the WHOLE set the
    exclusion is relaxed (and noted in the reason).

    ``mode="random"`` ignores tags entirely: every enabled track is a
    candidate with score 0, reason 「全章节随机」 (still adjacent-de-duped).
    """
    rng = rng or random.Random()
    exclude = {m for m in (prev_music, next_music) if m}
    enabled = [
        (name, t) for name, t in tracks
        if isinstance(t, dict) and t.get("enabled")
    ]

    def _pick(pool: list[str]) -> tuple[str | None, bool]:
        if not pool:
            return None, False
        ok = [n for n in pool if n not in exclude]
        if ok:
            return rng.choice(ok), False
        return rng.choice(pool), True  # fully blocked by neighbours — relax

    if mode == "random":
        music, relaxed = _pick([n for n, _ in enabled])
        if music is None:
            return {"music": None, "via": "none", "score": 0,
                    "reason": "无候选：音乐库中没有启用的曲目"}
        return {"music": music, "via": "random", "score": 0,
                "reason": ("全章节随机" if not relaxed else "全章节随机（相邻章去重放宽）")}

    scored = []
    for name, t in enabled:
        s, reason = score_track(chapter_tags, t.get("tags") or {})
        if s >= max(1, int(min_score)):
            scored.append((s, name, reason))
    if scored:
        best = max(s for s, _n, _r in scored)
        top = [n for s, n, _r in scored if s == best]
        reason = next(r for s, _n, r in scored if s == best)
        music, relaxed = _pick(top)
        if relaxed:
            reason += "（相邻章去重放宽）"
        return {"music": music, "via": "tags", "score": best, "reason": reason}

    generic = [n for n, t in enabled if music_engine.is_generic_track(t)]
    music, relaxed = _pick(generic)
    if music is not None:
        return {"music": music, "via": "generic", "score": 0,
                "reason": "无标签命中，使用通用音乐" + ("（相邻章去重放宽）" if relaxed else "")}
    return {"music": None, "via": "none", "score": 0,
            "reason": "无候选：无标签命中且无通用音乐"}


def _fmt_time(x: float) -> str:
    return f"{max(0.0, float(x)):.3f}"


def build_mix_cmd(
    ffmpeg: str,
    narration: Path,
    music: Path,
    out: Path,
    duration: float,
    cfg,
    music_duration: float,
    threads: int | None = None,
) -> list[str]:
    """The one-shot ffmpeg command that mixes one chapter (pinned element-wise).

    * both inputs are resampled to 44100 (mp3 44.1k / wav 48k — prevents an
      amix sample-rate conflict);
    * ``loop=True`` → ``-stream_loop -1`` (input-level repeat; aloop's size
      parameter is in samples and ambiguous), ``loop=False`` → ``-stream_loop -0``;
    * the music is trimmed to the narration duration with BOTH fades clamped
      ``min(fade, duration/2)`` (short chapters must never produce overlapping
      fades / an out-of-range ``st``);
    * ``amix duration=first`` — the narration length wins;
    * ``amix normalize=0`` — NO automatic 1/N scaling (the default would halve
      EVERYTHING, incl. the narration, and would swallow half of the user's
      ``volume`` gain — under the old UI cap of 1 the BGM could never reach
      the music file's own level, i.e. it was "pinned" small). With
      normalization off, ``volume`` maps 1:1 to the BGM gain over the full UI
      range 0~2 (1 = the music file's own level, 2 = 2× it, +6 dB) and the
      narration keeps its full level (also prevents the level pop when a
      non-looped track ends: the narration never jumps from half to full);
    * ``threads`` — when a positive int, a global ``-threads N`` is inserted
      right after ``-y`` bounding the encoder's thread count (ffmpeg otherwise
      spawns ALL cores per process; with several mixes gated at once that
      oversubscribes the box — see ``merge.thread_budget``). ``None``/``0``
      omits the flag entirely (legacy behaviour; manual callers).
    """
    duration = float(duration)
    fade_in = round(min(float(cfg.fade_in), duration / 2), 3)
    fade_out = round(min(float(cfg.fade_out), duration / 2), 3)
    st = round(duration - fade_out, 3)
    vol = float(cfg.volume)
    loop_arg = "-1" if cfg.loop else "-0"
    filter_complex = (
        "[0:a]aresample=44100[nar];"
        f"[1:a]aresample=44100,atrim=0:{_fmt_time(duration)},"
        f"afade=t=in:d={_fmt_time(fade_in)},"
        f"afade=t=out:st={_fmt_time(st)}:d={_fmt_time(fade_out)},"
        f"volume={vol:g}[bgm];"
        "[nar][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]"
    )
    cmd = [ffmpeg, "-y"]
    if threads:
        cmd += ["-threads", str(threads)]
    cmd += [
        "-i", str(narration), "-stream_loop", loop_arg, "-i", str(music),
        "-filter_complex", filter_complex, "-map", "[out]", "-c:a", "libmp3lame", str(out),
    ]
    return cmd


# --------------------------------------------------------------------------- #
# paragraph-level BGM (segment mode) — pure functions
# --------------------------------------------------------------------------- #

def _load_parsed_entries(layout, stem: str) -> list[dict]:
    """The chapter's 03 entries: ``03_parsed_json/<stem>.json`` (the base file is
    named after the 02 stem). Error wording mirrors ``tts_batch._load_script``."""
    p = layout.parsed_json / f"{stem}.json"
    if not p.is_file():
        raise RuntimeError(
            f"未找到脚本 JSON（03_parsed_json/{stem}.json）——请先在「文本解析」生成脚本。"
        )
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{p.name} 无法解析：{e}") from e
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"{p.name} 为空——请先生成脚本。")
    return data


def segment_fingerprint(entries: list) -> str:
    """The staleness anchor of a segment analysis: sha256 over the entry COUNT
    plus the entry texts (TEXT ONLY, deliberately — the music need depends on
    content, not speakers; pauses are re-derived from the manifest at recompute
    time, so speaker/pause edits never invalidate the LLM cache).

    The ``v2:`` salt bumps the fingerprint when the LLM output format changed
    (per-entry ``music_action`` records → scene blocks): every pre-upgrade
    cache entry reads as stale until the user re-runs the paragraph analysis
    (one-time migration; the old cache file is never deleted)."""
    texts = [str(e.get("text") or "") if isinstance(e, dict) else "" for e in entries]
    payload = f"v2:{len(texts)}\n" + "\n".join(texts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_fingerprint(value) -> str:
    """Stable hash for a persisted audio-stage snapshot.

    The snapshot is deliberately JSON based instead of hashing the whole audio
    file: it is used on every BGM mix preflight and must remain cheap while
    still detecting manifest/path/metadata changes.
    """
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def segment_manifest_fingerprint(manifest: dict) -> str:
    """Fingerprint the current 05 manifest, including its ordered metadata."""
    rows = []
    for index, entry in sorted((manifest or {}).items(),
                               key=lambda item: int(item[0])):
        if not isinstance(entry, dict):
            rows.append({"index": index, "entry": entry})
            continue
        rows.append({
            "index": int(index),
            "speaker": entry.get("speaker", ""),
            "text": entry.get("text", ""),
            "pause_after": entry.get("pause_after"),
            "path": entry.get("path", ""),
            "ok": bool(entry.get("ok")),
            "reason": entry.get("reason", ""),
        })
    return _canonical_fingerprint(rows)


def _file_snapshot(path: Path) -> dict:
    """Small identity snapshot for a generated audio artifact."""
    st = path.stat()
    return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def segment_audio_fingerprint(segs: list[dict], durs: dict[int, float]) -> str:
    """Fingerprint the surviving 05 files and their measured durations."""
    rows = []
    for seg in sorted(segs, key=lambda item: item["index"]):
        path = Path(seg["path"])
        try:
            stat = _file_snapshot(path)
        except OSError:
            stat = {"size": -1, "mtime_ns": -1}
        rows.append({
            "index": seg["index"],
            "duration": round(float(durs[seg["index"]]), 6),
            "file": stat,
        })
    return _canonical_fingerprint(rows)


def _manifest_matches_script(manifest: dict, expected: list[dict]) -> bool:
    """Check that 05 metadata still belongs to the current 03 script."""
    for segment in expected:
        actual = manifest.get(segment["index"])
        if not isinstance(actual, dict):
            return False
        if (actual.get("speaker", "") or "").strip() != segment["speaker"]:
            return False
        if (actual.get("text", "") or "").strip() != segment["text"]:
            return False
        if actual.get("pause_after") != segment.get("pause_after"):
            return False
    return True


def _extract_json_array(content: str):
    """Parse the top-level ``[...]`` array in ``content`` (the array analogue
    of ``voices.extract_json_object`` — the paragraph-batch reply is a single
    JSON array, and the object scanner would silently truncate it to the first
    record).

    The top-level JSON value must itself be an ARRAY: if the first structural
    character (outside any string literal) is ``{``, the LLM returned an object
    instead of a block array — a format violation (→ ``None`` → retry), NOT a
    silently-accepted empty batch (``{"scene": []}`` must not read as "silent").
    """
    # Locate the first structural char outside any string literal.
    i, n = 0, len(content)
    in_str = False
    esc = False
    start = None
    while i < n:
        ch = content[i]
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str and ch in "{[":
            start = i
            break
        i += 1
    if start is None or content[start] != "[":
        return None  # no array at all, or top-level is an object
    depth = 0
    in_str = False
    esc = False
    end = None
    for j, ch in enumerate(content[start:], start):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end is None:
        return None
    try:
        return json.loads(content[start:end])
    except Exception:  # noqa: BLE001
        return None


def _parse_scene_index(value) -> int | None:
    """A block's ``start_segment`` / ``end_segment``: an ``int`` (``bool``
    rejected — ``True`` would otherwise silently become 1) or a digit string;
    anything else → ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _parse_scene_tags(raw) -> dict | None:
    """Normalize a scene block's ``music_tags`` — the four capped buckets
    (capped at :data:`_ANALYSIS_CAPS`, stripped, de-duplicated, order kept;
    out-of-vocabulary names are KEPT — the chapter-analysis precedent).
    A non-dict → ``None`` (a new-scene block without usable tags cannot be
    matched downstream)."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, list[str]] = {}
    for cat in music_engine.TAG_CATEGORIES:
        vals = raw.get(cat)
        kept: list[str] = []
        if isinstance(vals, list):
            for v in vals:
                if not isinstance(v, str):
                    continue
                name = v.strip()
                if name and name not in kept:
                    kept.append(name)
        out[cat] = kept[: _ANALYSIS_CAPS[cat]]
    return out


def _parse_scene_intensity(raw) -> int:
    """A scene block's ``intensity``: an ``int`` (``bool`` rejected) or a
    digit string, else the default 2; clamped 1..3."""
    if isinstance(raw, bool):
        value: int | None = None
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError:
            value = None
    else:
        value = None
    if value is None:
        value = 2
    return max(1, min(3, value))


def parse_segment_blocks_reply_detailed(
    content: str, batch: list[int], prev_scene: dict | None
) -> tuple[list[dict] | None, str]:
    """Parse one paragraph-batch LLM reply into a list of SCENE BLOCKS.

    The reply is a JSON array of contiguous blocks over the batch's absolute
    entry range ``[b0, b1]``:

    * blocks are ascending and non-overlapping (``start > previous end``);
    * the GAP between two blocks is an intentional no-BGM stretch (the LLM's
      silence — it is never filled mechanically downstream);
    * a new-scene block carries ``music_tags`` (four capped buckets — a
      non-dict is a parse failure), optional ``scene`` / ``mood`` / ``reason``
      descriptions and a clamped ``intensity`` (default 2);
    * an ``extend`` block may appear ONLY as the FIRST block, only when
      ``prev_scene`` is not ``None`` (a scene is still open), with
      ``start == b0`` and ``prev_scene["end"] < b0`` — directly contiguous
      (``end == b0 - 1``) OR separated by fully-silent batches (a silent
      batch does not close the scene, so the open scene may end earlier);
      its descriptive/tag fields are dropped — the scene's fields stay the
      ORIGINAL scene's.

    A fully-silent batch is a VALID reply (the empty array).

    Returns ``(blocks, note)`` on success — ``note`` is ``""`` or a CLAMP
    notice: a block's ``end_segment`` past the batch end is clamped to the
    batch end rather than rejected — that is the model writing the scene's
    TRUE endpoint when the scene continues into the next batch (the open
    scene is picked up by the next batch's extend block; the clamp is a
    semantic repair, not data loss). 2026-09 incident: a deterministic model
    wrote the chapter-final index on a chapter-final batch → hard rejection →
    3× identical rejected retries → stable batch failure. ``start < b0`` is
    NOT clamped (it would silently steal previous-batch entries) — it is
    rejected with the extend hint.

    Returns ``(None, reason)`` on ANY other violation — ``reason`` is a
    specific, model-actionable Chinese description threaded into the 【重试】
    feedback via :class:`backend.engines.script.ParseRejected` (a bare
    ``None``-re-roll of a deterministic model is identical → useless).
    """
    data = _extract_json_array(content)
    if not isinstance(data, list):
        return None, "回复不是可解析的 JSON 数组"
    b0, b1 = int(batch[0]), int(batch[-1])
    out: list[dict] = []
    notes: list[str] = []
    prev_end = b0 - 1
    for block_no, item in enumerate(data):
        if not isinstance(item, dict):
            return None, f"第 {block_no + 1} 个块不是 JSON 对象"
        start = _parse_scene_index(item.get("start_segment"))
        end = _parse_scene_index(item.get("end_segment"))
        if start is None or end is None:
            field = "start_segment" if start is None else "end_segment"
            return None, (f"第 {block_no + 1} 个块的 {field}={item.get(field)!r} "
                          f"不是整数（须为本批范围内的全局条目编号）")
        if start > end:
            return None, (f"第 {block_no + 1} 个块 start_segment={start} > "
                          f"end_segment={end}（起点不得大于终点）")
        # 整块在本批之外（起点已越过本批末条）→ 拒收（本批回复只描述本批
        # 条目；钳制起点会吞掉下一批的条目）。
        if start > b1:
            return None, (f"第 {block_no + 1} 个块 start_segment={start} 超出本批上界 "
                          f"{b1}——块完全在本批之外（本批只含条目 {b0}~{b1}）")
        # end 越过本批上界 = 模型写出了场景的真实终点（场景延续到下一批）→
        # 钳制到本批末条而非拒收（见 docstring 事故注）；start 越下界不钳制
        # → 带 extend 提示拒收。
        if end > b1:
            notes.append(
                f"第 {block_no + 1} 个块 end_segment={end} 超出本批上界 {b1}，"
                f"已钳制到 {b1}（场景延续由下一批 extend 接住）")
            end = b1
        if start < b0:
            return None, (f"第 {block_no + 1} 个块 start_segment={start} 早于本批下界 "
                          f"{b0}——起点只能在本批内；上一批延续的场景须用 extend 块表达")
        if start <= prev_end:
            return None, (f"第 {block_no + 1} 个块 start_segment={start} 与前面块重叠"
                          f"（前一块止于 {prev_end}；块须严格升序、互不重叠）")
        if item.get("extend"):
            # prev_scene 须止于本批之前（end < b0）：直接邻接（== b0-1）或隔着
            # 全静音批（静音批不封闭场景 → open_scene 保持前值、end 更早）均
            # 合法——与提示词「仍开放，可用 extend 延续」同口径；end >= b0 =
            # 状态错乱 → 拒。
            if block_no != 0:
                return None, f"extend 块只能是数组首位（本块是第 {block_no + 1} 个）"
            p_end = prev_scene.get("end") if prev_scene is not None else None
            if not isinstance(p_end, int):
                return None, ("extend 块要求上一批末尾场景仍开放（本批没有可延续的"
                              "开放场景——请输出新场景块）")
            if start != b0:
                return None, (f"extend 块 start_segment 须等于本批首条 {b0}"
                              f"（实际 {start}）")
            if p_end >= b0:
                return None, (f"状态错乱：上一批开放场景止于 {p_end}，未止于本批之前"
                              f"（应 < {b0}）")
            out.append({"start": start, "end": end, "extend": True})
        else:
            tags = _parse_scene_tags(item.get("music_tags"))
            if tags is None:
                return None, (f"第 {block_no + 1} 个块的 music_tags 缺失或不是 "
                              f"JSON 对象（四桶 scene/mood/emotion/custom）")
            out.append({
                "start": start,
                "end": end,
                "scene": item.get("scene").strip()
                         if isinstance(item.get("scene"), str) else "",
                "mood": item.get("mood").strip()
                        if isinstance(item.get("mood"), str) else "",
                "music_tags": tags,
                "intensity": _parse_scene_intensity(item.get("intensity")),
                "reason": item.get("reason").strip()
                          if isinstance(item.get("reason"), str) else "",
            })
        prev_end = end
    return out, "；".join(notes)


def parse_segment_blocks_reply(
    content: str, batch: list[int], prev_scene: dict | None
) -> list[dict] | None:
    """Public wrapper (unchanged contract): the blocks, or ``None`` on any
    violation / clamp-note ignored."""
    blocks, _note = parse_segment_blocks_reply_detailed(content, batch, prev_scene)
    return blocks


def intensity_volume(intensity, base: float, tiers: list | None = None) -> float:
    """A span's BGM volume: ``base × tier``, tier = ``tiers[intensity-1]``
    (intensity 1/2/3 → default 0.5×/1×/1.5× of ``bgm.volume``; out-of-range or
    missing intensity falls to tier 2; the result is clamped ≤ 2.0 — the same
    ceiling as the chapter-level UI range)."""
    if not isinstance(tiers, list) or len(tiers) != 3:
        tiers = [0.5, 1.0, 1.5]
    try:
        i = int(intensity)
    except (TypeError, ValueError):
        i = 2
    if i < 1 or i > 3:
        i = 2
    try:
        mult = float(tiers[i - 1])
    except (TypeError, ValueError):
        mult = 1.0
    return min(2.0, float(base) * mult)


def build_timeline_mix_cmd(
    ffmpeg: str,
    narration: Path,
    out: Path,
    spans: list[dict],
    music_dir: Path,
    duration: float,
    cfg,
    threads: int | None = None,
) -> list[str]:
    """The one-shot ffmpeg command for a TIMELINE mix (pinned element-wise, the
    sibling of :func:`build_mix_cmd`).

    Input 0 is the narration; each span adds one ``-stream_loop … -i
    music_dir/music_id`` input. Per span the filter chain is: resample →
    ``atrim`` to the span length → per-span fades (clamped ``min(fade,
    span/2)``) → ``volume`` (the intensity-mapped span volume) → ``adelay`` (ms
    from the timeline start, ``all=1`` so every channel is delayed — the
    positioning is by delay, avoiding ``atrim=start`` PTS arithmetic on a
    looped input). ``amix inputs=K+1 … normalize=0`` — the 1/N division must
    stay off (the same load-bearing invariant as :func:`build_mix_cmd`: the
    narration keeps its full level, ``volume`` maps 1:1 to the BGM gain).
    Zero-span chapters never reach this function (the caller copies the
    narration instead).
    """
    duration = float(duration)
    loop_arg = "-1" if cfg.loop else "-0"
    filter_parts = ["[0:a]aresample=44100[nar]"]
    mix_inputs = ["[nar]"]
    cmd = [ffmpeg, "-y"]
    if threads:
        cmd += ["-threads", str(threads)]
    cmd += ["-i", str(narration)]
    for k, span in enumerate(spans):
        start = max(0.0, float(span.get("start") or 0.0))
        end = max(start, float(span.get("end") or 0.0))
        length = max(0.05, end - start)
        fi = round(min(float(cfg.fade_in), length / 2), 3)
        fo = round(min(float(cfg.fade_out), length / 2), 3)
        st = round(length - fo, 3)
        vol = float(span.get("volume") or 0.0)
        mid = int(round(start * 1000))
        filter_parts.append(
            f"[{k + 1}:a]aresample=44100,atrim=0:{_fmt_time(length)},"
            f"afade=t=in:d={_fmt_time(fi)},"
            f"afade=t=out:st={_fmt_time(st)}:d={_fmt_time(fo)},"
            f"volume={vol:g},adelay={mid}:all=1[m{k}]"
        )
        mix_inputs.append(f"[m{k}]")
        cmd += ["-stream_loop", loop_arg,
                "-i", str(music_dir / span.get("music_id"))]
    filter_complex = ";".join(filter_parts) + ";" + "".join(mix_inputs) + (
        f"amix=inputs={len(spans) + 1}:duration=first:"
        "dropout_transition=0:normalize=0[out]"
    )
    cmd += ["-filter_complex", filter_complex, "-map", "[out]",
            "-c:a", "libmp3lame", str(out)]
    return cmd


def _timeline_path(layout, stem: str) -> Path:
    return _bgm_dir(layout) / TIMELINE_DIR / f"{stem}.json"


def load_timeline(layout, stem: str) -> dict | None:
    """The chapter's timeline file (``08_bgm/timelines/<stem>.json``). Missing
    or corrupt → ``None`` (never raises — the caller decides the wording)."""
    p = _timeline_path(layout, stem)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# 08_bgm JSON caches (module lock + tmp + os.replace; reads never write)
# --------------------------------------------------------------------------- #

def _bgm_dir(layout) -> Path:
    return layout.bgm


def _default_analysis() -> dict:
    return {"version": 1, "model": "", "chapters": {}}


def _default_assignments() -> dict:
    return {"version": 1, "mode": "llm", "updated_at": "", "chapters": {}}


def _default_segment_analysis() -> dict:
    return {"version": 1, "model": "", "chapters": {}}


def _atomic_write_json(p: Path, data: dict, handle=None) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    stage_file = getattr(handle, "stage_workspace_file", None)
    if callable(stage_file):
        stage_file(p, payload)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".bgm_", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_cached(layout, name: str, default_factory) -> dict:
    p = _bgm_dir(layout) / name
    if not p.exists():
        return default_factory()
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        chapters = data.get("chapters")
        if not isinstance(chapters, dict):
            data["chapters"] = {}
        return data
    except Exception as e:
        log.warning("08_bgm 缓存 %s 损坏，降级为空：%s", name, e)
        return default_factory()


def load_analysis(layout) -> dict:
    return _load_cached(layout, ANALYSIS_NAME, _default_analysis)


def load_assignments(layout) -> dict:
    return _load_cached(layout, ASSIGNMENTS_NAME, _default_assignments)


def load_segment_analysis(layout) -> dict:
    return _load_cached(layout, SEGMENT_ANALYSIS_NAME, _default_segment_analysis)


def load_segment_timeline(layout, stem: str, assignment: dict | None = None) -> dict | None:
    """Return the timeline when it is authoritative for the chapter.

    A paragraph timeline normally carries ``assignment.segment``.  A previous
    chapter-level match can reset that marker while leaving a still-valid
    paragraph analysis and timeline on disk, though.  In that recoverable
    state the timeline is still the user's matched result and must remain the
    source for mixing.  Unmarked timelines without a current paragraph
    analysis remain ignored as legacy leftovers.
    """
    timeline = load_timeline(layout, stem)
    if timeline is None:
        return None
    segment = (load_segment_analysis(layout).get("chapters") or {}).get(stem)
    if isinstance(segment, dict) and (segment.get("blocks") or segment.get("entries")):
        try:
            entries = _load_parsed_entries(layout, stem)
        except Exception:
            return None
        if segment.get("fingerprint") != segment_fingerprint(entries):
            return None
        if (isinstance(assignment, dict) and assignment.get("segment")
                and timeline.get("fingerprint")
                and timeline.get("fingerprint") != segment.get("fingerprint")):
            return None
        return timeline
    # Legacy segment assignments remain visible so the strict mix preflight
    # can report a migration error instead of copying narration silently.
    return timeline if isinstance(assignment, dict) and assignment.get("segment") else None
def _segment_audio_inputs(layout, stem: str, ffprobe_path: str):
    """Load a complete, current paragraph audio snapshot for one chapter."""
    entries = _load_parsed_entries(layout, stem)
    expected = tts_batch._build_segments(entries)
    expected_indices = {s["index"] for s in expected}
    package = tts_batch.package_for(Path(f"{stem}.json"))
    manifest = tts_batch.read_manifest(layout.audio_chunk / package)
    if not manifest:
        raise RuntimeError(
            f"{stem}：未找到合成结果清单（05_audio_chunk/{package}/manifest.json）——"
            "请先完成音频合成。"
        )
    segs, _missing = collect_segments(list(manifest.values()), layout.workspace)
    actual_indices = [s["index"] for s in segs]
    if not _manifest_matches_script(manifest, expected):
        raise RuntimeError(
            f"{stem}：05 音频清单与当前 03 脚本不一致，请重新完成音频合成。"
        )
    if (len(actual_indices) != len(set(actual_indices))
            or set(actual_indices) != expected_indices
            or len(actual_indices) != len(expected_indices)):
        missing = sorted(expected_indices - set(actual_indices))
        detail = f"：{'、'.join(map(str, missing[:10]))}" if missing else ""
        raise RuntimeError(
            f"{stem}：音频合成尚未完成（缺少 {len(missing)} 段{detail}），"
            "请等待音频合成完成后再生成段落时间轴。"
        )
    durs: dict[int, float] = {}
    for s in segs:
        d, err = probe_duration(Path(s["path"]), ffprobe_path)
        if not (d > 0):
            raise RuntimeError(
                f"{stem}：无法探测第 {s['index']} 段时长（{err or 'ffprobe 失败'}）。"
            )
        durs[s["index"]] = d
    narration = _find_narration(layout, stem)
    if narration is None:
        raise RuntimeError(
            f"未找到旁白音频（06_audio_merge/{stem}.mp3），请先完成音频合并。"
        )
    total, terr = probe_duration(narration, ffprobe_path)
    if not (total > 0):
        raise RuntimeError(f"无法探测旁白时长（{terr or 'ffprobe 失败'}）。")
    return entries, manifest, segs, durs, narration, total


def _segment_source_snapshot(entries, manifest, segs, durs, narration,
                             pause_ms: int, same_ms: int) -> dict:
    """Build the consistency contract stored beside a generated timeline."""
    return {
        "script_fingerprint": segment_fingerprint(entries),
        "manifest_fingerprint": segment_manifest_fingerprint(manifest),
        "audio_fingerprint": segment_audio_fingerprint(segs, durs),
        "pause_between_ms": int(pause_ms),
        "pause_same_ms": int(same_ms),
        "narration": _file_snapshot(narration),
    }


def validate_segment_timeline(layout, stem: str, timeline: dict,
                              ffprobe_path: str, pause_ms: int,
                              same_ms: int):
    """Validate that a segment timeline still describes the current audio."""
    if not isinstance(timeline, dict) or not isinstance(timeline.get("source"), dict):
        raise RuntimeError(
            f"{stem}：段落时间轴缺少音频一致性快照，请重新“匹配”（段落级，不调用 LLM）。"
        )
    inputs = _segment_audio_inputs(layout, stem, ffprobe_path)
    entries, manifest, segs, durs, narration, total = inputs
    source = _segment_source_snapshot(
        entries, manifest, segs, durs, narration, pause_ms, same_ms,
    )
    saved = timeline["source"]
    for key in ("script_fingerprint", "manifest_fingerprint", "audio_fingerprint",
                "pause_between_ms", "pause_same_ms", "narration"):
        if saved.get(key) != source[key]:
            raise RuntimeError(
                f"{stem}：段落时间轴已过期（{key} 发生变化），请重新“匹配”（段落级，不调用 LLM）。"
            )
    if abs(total - float(timeline.get("duration") or 0.0)) > _STALE_TOLERANCE_S:
        raise RuntimeError(
            f"{stem}：时间轴已过期（旁白时长变化），请重新“匹配”（段落级，不调用 LLM）。"
        )
    return inputs


def _copy_narration_after_merge_gate(handle, layout, stem: str, out: Path,
                                     ffmpeg_cfg, expected_timeline: dict | None,
                                     pause_ms: int, same_ms: int) -> None:
    """Copy a no-BGM result only after the shared merge gate is acquired."""
    if not merge_gate().acquire(stop_check=lambda: handle.cancelled):
        raise TaskCancelled()
    try:
        latest_data = load_assignments(layout)
        latest_entry = (latest_data.get("chapters") or {}).get(stem)
        if not isinstance(latest_entry, dict):
            raise RuntimeError("混音输入在等待期间已变化，请重新发起混音。")
        latest_timeline = load_segment_timeline(layout, stem, latest_entry)
        if expected_timeline is not None:
            if latest_timeline is None:
                raise RuntimeError(
                    "段落时间轴缺失或已过期，请重新“匹配”（段落级，不调用 LLM）。"
                )
            latest_cfg = get_config()
            latest_pause_ms = latest_cfg.tts.pause_between_speakers_ms or 500
            latest_same_ms = latest_cfg.tts.pause_same_speaker_ms or 250
            validate_segment_timeline(
                layout, stem, latest_timeline, ffmpeg_cfg.ffprobe_path,
                latest_pause_ms, latest_same_ms,
            )
        elif latest_entry.get("music"):
            raise RuntimeError("混音输入在等待期间已变化，请重新发起混音。")
        narration = _find_narration(layout, stem)
        if narration is None:
            raise RuntimeError(
                f"未找到旁白音频（06_audio_merge/{stem}.mp3）。"
            )
        layout.bgm.mkdir(parents=True, exist_ok=True)
        shutil.copy2(narration, out)
    finally:
        merge_gate().release()


def save_analysis(layout, data: dict, handle=None) -> None:
    with _BGMS_LOCK:
        _atomic_write_json(_bgm_dir(layout) / ANALYSIS_NAME, data, handle)


def save_assignments(layout, data: dict, handle=None) -> None:
    with _BGMS_LOCK:
        _atomic_write_json(_bgm_dir(layout) / ASSIGNMENTS_NAME, data, handle)


def save_segment_analysis(layout, data: dict, handle=None) -> None:
    with _BGMS_LOCK:
        _atomic_write_json(_bgm_dir(layout) / SEGMENT_ANALYSIS_NAME, data, handle)


def update_analysis(layout, mutator, handle=None) -> dict:
    """Atomic read → mutate → write on the analysis cache.

    Mirrors :func:`backend.engines.music.update_index`: the module lock is
    held across load → mutator → save, so parallel analyze tasks (each
    rewriting the whole file) cannot lose each other's entries. ``mutator``
    may raise to abort — nothing is written. Returns the saved data.
    """
    with _BGMS_LOCK:
        data = load_analysis(layout)
        mutator(data)
        save_analysis(layout, data, handle)
        return data


def update_assignments(layout, mutator, handle=None) -> dict:
    """Atomic read → mutate → write on the assignments file (same pattern as
    :func:`update_analysis` — concurrent matches on disjoint stems can't
    lose each other's entries)."""
    with _BGMS_LOCK:
        data = load_assignments(layout)
        mutator(data)
        save_assignments(layout, data, handle)
        return data


def update_segment_analysis(layout, mutator, handle=None) -> dict:
    """Atomic read → mutate → write on the segment-analysis cache (same pattern
    as :func:`update_analysis` — parallel paragraph-analysis tasks on disjoint
    chapters can't lose each other's entries)."""
    with _BGMS_LOCK:
        data = load_segment_analysis(layout)
        mutator(data)
        save_segment_analysis(layout, data, handle)
        return data


def list_chapter_stems(layout) -> list[str]:
    """Sorted stems of ``02_split_text/*.txt`` (includes the whole-book file)."""
    d = layout.split_text
    if d is None or not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.txt") if p.is_file())


# --------------------------------------------------------------------------- #
# Task worker 1: LLM chapter mood analysis (module ``bgm-analysis``)
# --------------------------------------------------------------------------- #

def _vocab_text(tags: dict[str, list[str]]) -> str:
    return "\n".join(
        f"{c}: {'、'.join(tags.get(c) or [])}" for c in music_engine.TAG_CATEGORIES
    )


def _analysis_prompts(sample: str, registry: dict[str, list[str]]) -> tuple[str, str]:
    """(system, user) for the chapter-analysis LLM call.

    The system prompt embeds the FULL global tag vocabulary — all four
    buckets INCLUDING custom, so tags auto-registered from earlier chapters
    are reusable — plus the reuse-first / new-tag rules; the reply must be a
    single strict JSON object in ``_ANALYSIS_FORMAT_HINT`` shape.
    """
    system = (
        "你是有声书章节气氛分析助手。阅读下面的章节文本节选，为这一章选择气氛标签。\n\n"
        "现有标签词表（scene=场景 / mood=气氛 / emotion=情绪 / custom=自定义）：\n"
        f"{_vocab_text(registry)}\n\n"
        "选择规则：\n"
        "1. 优先原样复用词表中已有的标签（与词表逐字一致）；只有当词表中确实没有任何"
        "标签能描述本章时，才允许给出新标签；\n"
        "2. 新标签必须简短（2~6 字）、含义明确，且与词表中所有已有标签（包括近义标签）"
        "语义明显不同——禁止给出与已有标签同义、近义或仅措辞不同的标签；\n"
        "3. 每个标签放入正确的分类：scene=故事发生的地点/情境，mood=章节整体气氛基调，"
        "emotion=主要情绪，custom=以上三类都不合适的补充；\n"
        "4. 数量上限：scene 最多 2 个、mood 最多 3 个、emotion 最多 2 个、custom 最多 2 个；"
        "某类选不出就留空数组。\n\n"
        f"输出格式：{_ANALYSIS_FORMAT_HINT}\n"
        "只输出这一个 JSON 对象，不要解释、不要前后缀、不要代码围栏。"
    )
    user = f"章节文本节选：\n{sample}"
    return system, user


def register_analysis_tags(parsed: dict) -> list[str]:
    """Register the chapter reply's out-of-vocabulary tag names into the GLOBAL
    music tag registry (``music_library/music_index.json``) — one atomic
    :func:`backend.engines.music.update_index` per call.

    * a name already present in ANY bucket is skipped (the global
      unique-name invariant; the analysis entry keeps it as a free-form tag);
    * a new name lands in the FIRST bucket (``TAG_CATEGORIES`` order) that
      contains it in the reply — one reply can never create a new
      cross-bucket pair;
    * the presence re-check runs under ``music._INDEX_LOCK`` (parallel analyze
      tasks adding the same name serialize; the second skips).

    Returns the names actually added (``[]`` when nothing new — the index file
    is not rewritten at all in that case).
    """
    idx = music_engine.load_index()
    known = music_engine.all_tag_names(idx["tags"])
    if not any(n not in known for c in music_engine.TAG_CATEGORIES
               for n in (parsed.get(c) or [])):
        return []
    added: list[str] = []

    def _mutate(i: dict) -> None:
        for cat in music_engine.TAG_CATEGORIES:
            for name in parsed.get(cat) or []:
                if name in music_engine.all_tag_names(i["tags"]) or name in added:
                    continue
                i["tags"].setdefault(cat, []).append(name)
                added.append(name)

    music_engine.update_index(_mutate)
    if added:
        log.info("章节气氛分析 新增全局标签：%s", "、".join(added))
    return added


def _scene_tag_line(tags: dict[str, list[str]]) -> str:
    """The compact tag line used in the prev-batch boundary note and the
    chapter-reference line (non-empty buckets in ``TAG_CATEGORIES`` order,
    bucket name prefixed, ``；``-separated)."""
    parts = [f"{c}={'、'.join(tags.get(c) or [])}"
             for c in music_engine.TAG_CATEGORIES if tags.get(c)]
    return "；".join(parts) if parts else "（无）"


def _segment_prompts(chunk: list[tuple[int, dict]], prev_ctx: dict | None,
                     registry: dict[str, list[str]],
                     meta: dict) -> tuple[str, str]:
    """(system, user) for one paragraph-batch analysis LLM call.

    The LLM identifies CONTINUOUS SCENE BLOCKS over the batch (several
    entries per scene — it never picks a concrete track: matching is
    mechanical downstream). The system embeds the FULL global tag vocabulary
    plus the cut / no-cut rules and the quantity reference; when the chapter
    duration is known the reference number ``K = max(3, ceil(分钟数))`` is
    the SAME number the mechanical timeline cap uses
    (:func:`recompute_segment_timelines`), so the prompt and the mechanical
    rule can never contradict. ``prev_ctx`` (non-first batches) carries the
    previous batch's OPEN scene (extend-able) and its last 3 raw texts as
    read-only boundary context, so scene continuity survives batch
    boundaries. ``meta`` = ``{"n", "minutes"(float|None), "bi", "batches",
    "chapter_tags"(dict|None)}``.
    """
    minutes = meta.get("minutes")
    k = max(3, math.ceil(minutes)) if minutes else 3
    system = (
        "你是中文有声书的背景音乐场景规划器。阅读下面的章节段落（每行 = 一条旁白/台词条目，"
        "index 为其在本章中的全局序号），把它们识别为若干个连续的【场景块】——同一场景的多个"
        "段落合并成一个场景块，块给出标签与强度；绝不选择、命名或暗示任何具体音乐文件"
        "（选曲由下游机械流程完成）。\n\n"
        f"现有标签词表（scene=场景 / mood=气氛 / emotion=情绪 / custom=自定义）：\n"
        f"{_vocab_text(registry)}\n\n"
        "核心原则：宁可少切，不要频繁切——音乐只应在真实的场景/剧情变化处切换。\n\n"
        "只在以下情形切分：\n"
        "1. 地点/场所变化；\n"
        "2. 时间/环境明显变化（昼夜、前后、内外等）；\n"
        "3. 故事阶段变化（铺垫→冲突爆发、危机→解决等）；\n"
        "4. 情绪基调显著转变；\n"
        "5. 叙述方式变化（独白→激烈对白、回忆→现实等）。\n\n"
        "以下情形不切分：\n"
        "1. 说话人不同但场景/气氛未变——多人同场对话是同一场景；\n"
        "2. 同场景的语气起伏、台词间的情绪波动；\n"
        "3. 一两句的过渡/插叙/闪回；\n"
        "4. 凑数切分（为凑数量而切）；\n"
        "5. 无法从文字确认的猜测性切分。\n\n"
        "数量控制：常规章节整体约 3-7 个场景块；实际数量一律以场景变化为准——宁少勿多，"
        "绝不凑数。相邻场景的氛围/风格与上一场景高度相似时，应延续上一场景；"
        "块之间未覆盖的段落是有意静音（平静日常对话、纯过渡可以整体静音）。\n\n"
        "标签规则：\n"
        "1. 只使用词表内的词（与词表逐字一致）；确有必要时最多 1 个新词"
        "（简短 2~6 字、含义明确、与已有标签语义明显不同，会自动入库）；\n"
        "2. 每个标签放入正确的分类（scene=地点/情境，mood=气氛基调，emotion=主要情绪，"
        "custom=以上三类都不合适的补充）；数量上限：scene 最多 2 个、mood 最多 3 个、"
        "emotion 最多 2 个、custom 最多 2 个；无合适标签 → 空列表；\n"
        "3. intensity = 音乐强度：1=轻柔铺底、2=常规、3=强烈烘托；缺省 2。\n\n"
        f"输出格式：{_SEGMENT_FORMAT_HINT}\n"
        "只输出一个 JSON 数组（不要解释、不要前后缀、不要代码围栏）：start_segment / "
        "end_segment 是全局条目编号（含端点），块升序、互不重叠、起点大于前一块终点；"
        "仅当上一批末尾的场景延续到本批开头时，数组首位允许一个 extend 块 "
        '{"start_segment": 本批首条, "end_segment": 连续到的条目, "extend": true}'
        "（不携带其他字段），其余一律是新场景块；本批若全部静音则输出空数组 []。"
        "每个块的 start_segment / end_segment 必须落在本批条目范围内（不得越过本批"
        "末条）——某场景若实际延续到本批之后，end_segment 只写到本批末条即可，"
        "下一批会用 extend 块接住。"
    )
    system += (
        "\n\n原文可能包含成人或其他敏感内容。只做抽象场景分类，不复述细节，不评价内容，"
        "也不要因敏感内容拒绝分析。若本批存在明显场景，必须返回至少一个场景块；"
        "只有确实没有 BGM 的过渡段才返回 []。"
    )
    b0, b1 = int(chunk[0][0]), int(chunk[-1][0])
    head = f"本章共 {meta['n']} 段"
    if minutes:
        head += f"，旁白约 {minutes:.2f} 分钟"
    head += f"，本批是第 {meta['bi']}/{meta['batches']} 批（条目 {b0}~{b1}）。"
    user_parts: list[str] = [head]
    if minutes:
        user_parts.append(
            f"理论曲目数参考约 {k} 首（即平均约 1 曲/分钟）——实际数量一律以场景变化为准，"
            "宁少勿多，绝不凑数。"
        )
    else:
        user_parts.append(
            "章节时长未知，请按常规 3-7 个场景块把握——实际数量一律以场景变化为准，"
            "宁少勿多，绝不凑数。"
        )
    chapter_tags = meta.get("chapter_tags")
    if chapter_tags and any(chapter_tags.get(c) for c in music_engine.TAG_CATEGORIES):
        user_parts.append(f"全章气氛参考（来自章节分析，仅供参考）：{_scene_tag_line(chapter_tags)}")
    if prev_ctx:
        sc = prev_ctx["scene"]
        lines = [f"[{i}] {t}" for i, t in prev_ctx["texts"]]
        user_parts.append(
            f"上一批结束时的场景（仍开放，可用 extend 延续）：止于 index {sc['end']}，"
            f"场景={sc.get('scene') or ''}，气氛={sc.get('mood') or ''}，"
            f"标签={_scene_tag_line(sc.get('music_tags') or {})}，"
            f"强度={sc.get('intensity', 2)}。\n"
            "上一批末尾段落（只读，不要为这些条目输出）：\n"
            + "\n".join(lines)
        )
    user_parts.append(
        "本批段落：\n"
        + "\n".join(
            f"[{i}] {str(e.get('speaker') or 'NARRATOR').strip()}："
            f"{str(e.get('text') or '').strip()}"
            for i, e in chunk
        )
    )
    return system, "\n\n".join(user_parts)


def _log_segment_llm_exchange(stem: str, batch_no: int, attempt_no: int,
                              messages: list[dict], *, content: str | None,
                              finish: str | None, usage, error: Exception | None = None) -> None:
    """Persist one paragraph-analysis LLM exchange for postmortem diagnosis.

    The paragraph parser intentionally accepts ``[]`` as a valid all-silent
    batch, so the original prompt and reply are needed to distinguish that
    semantic result from a model refusal or other degraded response.  This is
    written through the normal ``audiobook.bgm`` logger, which follows the
    active workspace's ``logs/app.log``.
    """
    system = messages[0].get("content", "") if messages else ""
    user = messages[1].get("content", "") if len(messages) > 1 else ""
    raw = content if content is not None else "<无原始回复>"
    log.info(
        "段落分析 LLM 原始交换（%s，第 %d 批，第 %d 次）\n"
        "===== system prompt =====\n%s\n"
        "===== user prompt =====\n%s\n"
        "===== raw reply =====\n%s\n"
        "===== meta =====\nfinish_reason=%r\nusage=%r\nerror=%r",
        stem, batch_no, attempt_no, system, user, raw, finish, usage, error,
    )


def analyze_chapter(handle, stem: str, llm_cfg, bgm_cfg) -> dict:
    """Task worker: LLM-analyze ONE chapter's mood tags, register any new tags
    into the global music tag registry, then auto-match the chapter
    (mechanical, no LLM) — one-click analyze leaves the chapter ready to mix.

    Slot scope = the whole task (no check phase): the LLM call holds one shared
    LLM slot; a cancel while queued aborts without taking a slot.

    The LLM reply must be a strict JSON object (four tag buckets); parse
    failures are retried with the parse error fed back (up to 3 calls). A
    TOTAL failure fails the task with a clear error and writes NOTHING
    (analysis / registry / assignments untouched) — the user retries, edits
    tags by hand, or matches manually; a chapter is never hard-blocked.
    """
    handle.check()
    layout = core_paths.get_layout()
    if layout.split_text is None:
        raise RuntimeError("未设置工作空间。")
    src = layout.split_text / f"{stem}.txt"
    if not src.is_file():
        raise RuntimeError(f"未找到章节文件（02_split_text/{stem}.txt）。")
    if not llm_cfg.model_name:
        raise RuntimeError("尚未配置 LLM 模型（设置 → LLM → model_name）。")

    text = decode_buffer(src.read_bytes())[0]
    sample = sample_chapter_text(text, bgm_cfg.analysis_chars)
    system, user = _analysis_prompts(sample, music_engine.load_index()["tags"])

    handle.progress(0.05, "排队中（等待并发槽位）")
    if not gate().acquire(stop_check=lambda: handle.cancelled):
        raise TaskCancelled()  # 排队中被取消——未取槽，不进入 try、不 release
    try:
        try:
            parsed, _attempts = llm_json_with_retry(
                llm_cfg, system, user, parse_analysis_reply,
                handle=handle, llm_call=_llm_chat_completion,
                max_attempts=3,
                # 同音乐库 AI 推荐：思考模型关思考 + 加大预算作安全网
                # （传输层对拒绝额外键的严格网关自动单次退化重试）。
                max_tokens=2048,
                extra_body={"enable_thinking": False},
                format_hint=_ANALYSIS_FORMAT_HINT,
                operation_type="bgm.analysis",
            )
        except LLMJSONRetryExhausted as e:
            # 全败 → 任务失败、零落盘（旧的「3 败 → 空标签记录 + 成功」语义已移除）
            raise RuntimeError(f"章节气氛分析失败：{e}") from e

        entry = {
            "scene": parsed.get("scene", []),
            "mood": parsed.get("mood", []),
            "emotion": parsed.get("emotion", []),
            "custom": parsed.get("custom", []),
            "analyzed_at": datetime.now().isoformat(timespec="seconds"),
            "edited": False,
        }
        parts = [f"{c} {', '.join(entry[c])}"
                 for c in music_engine.TAG_CATEGORIES if entry[c]]
        handle.log("分析完成：" + ("、".join(parts) if parts else "（无标签）"))

        handle.progress(0.8, "标签入库")
        added = register_analysis_tags(parsed)
        if added:
            handle.log("音乐库新增标签：" + "、".join(added))
        else:
            handle.log("标签均已在音乐库词表中")

        update_analysis(layout, lambda d: (
            d.update({"model": llm_cfg.model_name}),
            d["chapters"].__setitem__(stem, entry),
        ), handle=handle)

        handle.progress(0.9, "匹配音乐")
        mode = load_assignments(layout).get("mode") or "llm"
        mres = match_stems(
            layout, [stem], mode, max(1, int(bgm_cfg.min_match_score)),
            handle=handle,
        )
        match_entry = mres["assignments"]["chapters"].get(stem)
        skipped_locked = mres["skipped_locked"] > 0
        if skipped_locked:
            handle.log("该章已锁定，保留原匹配结果")
        handle.progress(1.0, "完成")
        return {
            "stem": stem,
            "analysis": entry,
            "new_tags": added,
            "skipped_locked": skipped_locked,
            "match": match_entry,  # 该章 assignment 条目（锁定章 = 原样保留的条目）
        }
    finally:
        gate().release()  # acquire 成功才进入 try——排队中被取消的路径未取槽、不到这里


def analyze_segment_chapter(handle, stem: str, llm_cfg, bgm_cfg) -> dict:
    """Task worker (module ``bgm-segment``): LLM-analyze ONE chapter's
    paragraphs into CONTINUOUS SCENE BLOCKS (batches of
    ``bgm_cfg.segment_batch_size`` entries), register any new tags into the
    global registry, then best-effort generate the chapter's timeline (pure,
    no LLM).

    The LLM only DESCRIBES the scenes (tags + intensity per scene block) —
    it never picks a track; the quantity reference in the prompt (K) is the
    SAME number the mechanical timeline cap uses, and the timeline validation
    (same-track merge / short-span merge / cap merge) is the only quantity
    control.
    Slot scope = the WHOLE task (like :func:`analyze_chapter`): one shared LLM
    slot across all batches; a cancel while queued aborts without taking a
    slot. Writes happen ONLY after the LAST batch succeeds (atomicity — a
    failed batch writes nothing: analysis cache / registry / timelines /
    assignments all untouched). The timeline recompute is best-effort: missing
    05/06 inputs leave the task SUCCEEDED with a note — the analysis is the
    durable LLM artifact and the timeline can be re-derived ANY time with zero
    LLM calls (``POST /api/bgm/match`` mode="segment").
    """
    handle.check()
    layout = core_paths.get_layout()
    if layout.parsed_json is None:
        raise RuntimeError("未设置工作空间。")
    if not llm_cfg.model_name:
        raise RuntimeError("尚未配置 LLM 模型（设置 → LLM → model_name）。")
    entries = _load_parsed_entries(layout, stem)
    fingerprint = segment_fingerprint(entries)
    n = len(entries)
    batch_size = max(1, int(bgm_cfg.segment_batch_size or 20))
    batches = [list(range(k, min(n, k + batch_size))) for k in range(0, n, batch_size)]
    registry = music_engine.load_index()["tags"]
    total_batches = len(batches)
    # 全章气氛参考（章节分析缓存，若存在）——给场景规划一点整章背景。
    chapter_tags = load_analysis(layout).get("chapters", {}).get(stem)
    if not isinstance(chapter_tags, dict):
        chapter_tags = None

    # 06 旁白时长探测（槽外——快速失败守卫与探测都不占槽）：时长进提示词的
    # 数量参考（K = max(3, ceil(分钟数))，与机械时间轴上限同一数值）。任何
    # 异常/缺失 → minutes=None 降级（「章节时长未知」提示词行），绝不杀死分析。
    minutes: float | None = None
    try:
        narration = _find_narration(layout, stem)
        if narration is not None:
            d, _perr = probe_duration(narration, get_config().ffmpeg.ffprobe_path)
            if d and d > 0:
                minutes = d / 60.0
    except Exception:  # noqa: BLE001 — 探测 hiccup 不得杀死分析
        log.warning("段落分析 06 旁白时长探测失败（%s）", stem, exc_info=True)

    handle.progress(0.05, f"排队中（等待并发槽位 · 共 {n} 段 / {total_batches} 批）")
    if not gate().acquire(stop_check=lambda: handle.cancelled):
        raise TaskCancelled()  # 排队中被取消——未取槽，不进入 try、不 release
    try:
        # open_scene 状态机：上一批末尾的场景可能仍开放（LLM 可用 extend 块
        # 延续到下一批开头）。批以空隙结尾（末块止于批末条之前）→ 场景封闭，
        # 下批不得 extend；全静默批（空回复）= 对场景边界无陈述 → 保持
        # open_scene 不变。
        blocks: list[dict] = []
        open_scene: dict | None = None
        for bi, idxs in enumerate(batches, 1):
            handle.check()
            chunk = [(i, entries[i]) for i in idxs]
            prev_ctx = None
            if bi > 1 and open_scene is not None:
                prev_idxs = batches[bi - 2]
                prev_ctx = {
                    "scene": open_scene,
                    "texts": [(i, str(entries[i].get("text") or "").strip())
                              for i in prev_idxs[-3:]],
                }
            system, user = _segment_prompts(chunk, prev_ctx, registry, {
                "n": n,
                "minutes": minutes,
                "bi": bi,
                "batches": total_batches,
                "chapter_tags": chapter_tags,
            })
            def _parse(c, _b=idxs, _ps=open_scene, _bi=bi):
                # 违规 → ParseRejected（具体原因经 llm_json_with_retry 进
                # last_err + 【重试】反馈，确定性模型可自我纠正）；end 钳制
                # 成功 → 两通道各留一行（终端/app.log + 任务日志）：该场景
                # 由下一批 extend 接住。
                got, note = parse_segment_blocks_reply_detailed(c, _b, _ps)
                if got is None:
                    return ParseRejected(note)
                if note:
                    log.info("段落分析（%s，第 %d/%d 批）%s",
                             stem, _bi, total_batches, note)
                    handle.log(f"第 {_bi}/{total_batches} 批 {note}")
                return got

            try:
                attempt_no = 0

                def _logged_call(base_url, api_key, model, messages,
                                 temperature, top_p, presence_penalty,
                                 max_tokens, **kwargs):
                    nonlocal attempt_no
                    attempt_no += 1
                    try:
                        result = _llm_chat_completion(
                            base_url, api_key, model, messages,
                            temperature, top_p, presence_penalty, max_tokens,
                            **kwargs,
                        )
                    except Exception as e:  # noqa: BLE001
                        _log_segment_llm_exchange(
                            stem, bi, attempt_no, messages,
                            content=None, finish=None, usage=None, error=e,
                        )
                        raise
                    content, finish, usage = result
                    _log_segment_llm_exchange(
                        stem, bi, attempt_no, messages,
                        content=content, finish=finish, usage=usage,
                    )
                    return result

                got, _attempts = llm_json_with_retry(
                    llm_cfg, system, user, _parse,
                    handle=handle, llm_call=_logged_call,
                    max_attempts=3,
                    # 段落批（≤20 条 × ≤200 字）比章节摘要长，预算取 4096；
                    # 思考模型关思考 + 传输层对拒绝额外键的严格网关自动单次退化重试。
                    max_tokens=4096,
                    extra_body={"enable_thinking": False},
                    format_hint=_SEGMENT_FORMAT_HINT,
                    operation_type="bgm.segment",
                )
            except LLMJSONRetryExhausted as e:
                raise RuntimeError(
                    f"段落分析失败（第 {bi}/{total_batches} 批）：{e}"
                ) from e
            for blk in got:
                blocks.append(blk)
                if blk.get("extend"):
                    open_scene["end"] = blk["end"]  # 原地延展——字段保持原场景的
                else:
                    # 拷贝而非别名：延展只改 open_scene["end"]，但别名会让延展
                    # 改写缓存里的原始块（把块与延展端点之间的静音间隙吞进块
                    # 区间），时间轴阶段会把 LLM 的有意静音铺上音乐。
                    open_scene = dict(blk)
            if got and open_scene is not None and open_scene["end"] < idxs[-1]:
                open_scene = None  # 批以空隙结尾 → 场景封闭

            handle.progress(0.10 + 0.70 * bi / total_batches,
                            f"段落分析 {bi}/{total_batches} 批")

        # 全部批次成功 → 原子写（此前的任何失败都是零落盘）。
        # 新标签注册面 = 全部新场景块的 music_tags 并集（extend 块不携带标签）。
        if not blocks:
            message = (
                f"{stem}：所有段落分析批次均返回空数组（[]），"
                "疑似 LLM 拒绝或返回无效结果；未写入分析缓存，"
                "请重试或切换支持该文本的 LLM 模型。"
            )
            handle.log(message, "WARNING")
            raise RuntimeError(message)

        union: dict[str, list[str]] = {c: [] for c in music_engine.TAG_CATEGORIES}
        for blk in blocks:
            if blk.get("extend"):
                continue
            for c in music_engine.TAG_CATEGORIES:
                for t in blk.get("music_tags", {}).get(c) or []:
                    if t not in union[c]:
                        union[c].append(t)
        handle.progress(0.85, "标签入库")
        added = register_analysis_tags(union)
        if added:
            handle.log("音乐库新增标签：" + "、".join(added))
        else:
            handle.log("标签均已在音乐库词表中")

        entry = {
            "fingerprint": fingerprint,
            "entry_count": n,
            "blocks": blocks,
            "model": llm_cfg.model_name,
            "analyzed_at": datetime.now().isoformat(timespec="seconds"),
            "edited": False,
        }
        update_segment_analysis(layout, lambda d: (
            d.update({"model": llm_cfg.model_name}),
            d["chapters"].__setitem__(stem, entry),
        ), handle=handle)
        handle.log(f"段落分析完成：{n} 段（{total_batches} 批）")

        handle.progress(0.95, "生成时间轴")
        timeline_ok, timeline_note = _try_auto_timeline(layout, stem, bgm_cfg, handle)
        if timeline_ok:
            handle.log("时间轴已生成（05 实测时长 + 停顿规则，零 LLM）")
        handle.progress(1.0, "完成")
        return {
            "stem": stem,
            "blocks": blocks,
            "new_tags": added,
            "timeline": timeline_ok,
            "timeline_note": timeline_note,
        }
    finally:
        gate().release()  # acquire 成功才进入 try——排队中被取消的路径未取槽、不到这里


def _try_auto_timeline(layout, stem: str, bgm_cfg, handle=None) -> tuple[bool, str]:
    """Best-effort timeline recompute at the tail of a paragraph analysis (the
    task STILL SUCCEEDS when 05/06 are missing — the timeline is a derived,
    zero-LLM artifact). Returns ``(ok, note)``."""
    cfg = get_config()
    try:
        recompute_segment_timelines(
            layout, [stem], max(1, int(bgm_cfg.min_match_score)),
            ffprobe_path=cfg.ffmpeg.ffprobe_path,
            pause_ms=cfg.tts.pause_between_speakers_ms or 500,
            same_ms=cfg.tts.pause_same_speaker_ms or 250,
            volume_base=cfg.bgm.volume,
            volume_tiers=cfg.bgm.segment_volume_tiers,
            handle=handle,
        )
        return True, ""
    except Exception as e:  # noqa: BLE001 — a timeline hiccup never fails the analysis
        log.warning("段落分析后自动生成时间轴失败（%s）：%s", stem, e)
        return False, f"时间轴未生成（{e}）——可稍后在 BGM 页执行「匹配（时间轴）」。"


def recompute_segment_timelines(
    layout,
    stems: list[str],
    min_score: int,
    ffprobe_path: str = "",
    pause_ms: int = 500,
    same_ms: int = 250,
    volume_base: float = 0.18,
    volume_tiers: list | None = None,
    rng: random.Random | None = None,
    handle=None,
) -> dict:
    """Regenerate the paragraph-level timelines for the given chapters — PURE
    (synchronous, LLM-free): the cached LLM scene blocks + the REAL 05
    durations (ffprobe) + the merge pause rule (``boundary_gap_ms`` — the
    same gaps the two-stage merge inserts) walk a cursor; the scene blocks
    become time spans (span start = first present entry's audio start, span
    END = last present entry's audio END — the boundary pause falls into the
    inter-span gap), matched mechanically (``match_chapter`` with NO
    adjacent de-dup; identical tags short-circuit to the previous pick),
    merged (adjacent same-track scenes; validation A folds spans shorter
    than ``_MIN_SPAN_S`` into a neighbour; validation B merges down to the
    cap ``max(3, ceil(分钟数))`` — the SAME number the prompt's K reference
    shows), then write ``08_bgm/timelines/<stem>.json`` (version 2) + ONE
    ``update_assignments`` transaction (``mode="segment"``; per-stem
    ``segment: true`` marker; locked chapters keep their entry verbatim).

    Any chapter error aborts the WHOLE call before ANY write (zero drop): the
    LLM cache, the timelines, and the assignments are all untouched. Returns
    ``{"mode": "segment", matched, no_bgm, skipped_locked}`` (same keys as
    chapter-level matching — the frontend toast wording is unchanged).
    """
    idx = music_engine.load_index()
    tracks = list(idx["tracks"].items())
    seg_data = load_segment_analysis(layout).get("chapters") or {}
    now = datetime.now().isoformat(timespec="seconds")
    timeline_writes: list[tuple[str, dict]] = []
    assign_entries: dict[str, dict] = {}
    matched = 0
    no_bgm = 0

    for stem in sorted(set(stems)):
        # 1) the cached LLM analysis (blocks + fingerprint vs the CURRENT 03
        # entries — the v2-salted fingerprint forces a one-time re-analysis
        # after the per-entry → scene-blocks upgrade; a pre-upgrade
        # ``entries``-format cache reads as stale HERE, checked BEFORE the
        # fingerprint so the wording is the actionable one).
        a = seg_data.get(stem)
        if not isinstance(a, dict) or not a.get("blocks"):
            raise RuntimeError(f"{stem}：段落分析缺失或已失效，请重新段落分析。")
        entries = _load_parsed_entries(layout, stem)
        if a.get("fingerprint") != segment_fingerprint(entries):
            raise RuntimeError(
                f"{stem}：段落分析已失效（03 脚本变化），请重新段落分析。"
            )
        entry_count = len(entries)

        # 2) the real 05 durations (manifest + ffprobe). collect_segments keeps
        # only ok segments whose file still exists, sorted by index — the same
        # core merge.run feeds the worker (missing segments are skipped; the
        # cursor walk spans across them, exactly like the merged audio does).
        package = tts_batch.package_for(Path(f"{stem}.json"))
        manifest = tts_batch.read_manifest(layout.audio_chunk / package)
        if not manifest:
            raise RuntimeError(
                f"{stem}：未找到合成结果清单（05_audio_chunk/{package}/manifest.json）"
                "——请先运行「音频合成」。"
            )
        segs, _missing = collect_segments(list(manifest.values()), layout.workspace)
        expected_segments = tts_batch._build_segments(entries)
        expected_indices = {s["index"] for s in expected_segments}
        actual_indices = [s["index"] for s in segs]
        for s in segs:
            if s["index"] is None or not (0 <= s["index"] < entry_count):
                raise RuntimeError(
                    f"{stem}：合成清单与段落分析不一致（条目数变化），请重新段落分析。"
                )
        if not _manifest_matches_script(manifest, expected_segments):
            raise RuntimeError(
                f"{stem}：05 音频清单与当前 03 脚本不一致，请重新完成音频合成。"
            )
        if (len(actual_indices) != len(set(actual_indices))
                or set(actual_indices) != expected_indices
                or len(actual_indices) != len(expected_indices)):
            missing = sorted(expected_indices - set(actual_indices))
            detail = f"：{'、'.join(map(str, missing[:10]))}" if missing else ""
            raise RuntimeError(
                f"{stem}：音频合成尚未完成（缺少 {len(missing)} 段{detail}），"
                "请等待音频合成完成后再生成段落时间轴。"
            )
        if not segs:
            raise RuntimeError(f"{stem}：没有可计算时间轴的音频（合成段落全部缺失）。")
        for s in segs:
            if s["index"] is None or not (0 <= s["index"] < entry_count):
                raise RuntimeError(
                    f"{stem}：合成清单与段落分析不一致（条目数变化），请重新段落分析。"
                )
        durs: dict[int, float] = {}
        for s in segs:
            d, err = probe_duration(Path(s["path"]), ffprobe_path)
            if not (d > 0):
                raise RuntimeError(
                    f"{stem}：无法探测第 {s['index']} 段时长（{err or 'ffprobe 失败'}）。"
                )
            durs[s["index"]] = d
        narration = _find_narration(layout, stem)
        if narration is None:
            raise RuntimeError(
                f"未找到旁白音频（06_audio_merge/{stem}.mp3），请先完成音频合并。"
            )
        total, terr = probe_duration(narration, ffprobe_path)
        if not (total > 0):
            raise RuntimeError(f"无法探测旁白时长（{terr or 'ffprobe 失败'}）。")

        # 3) the cursor walk (the worker's compute_timeline ported to seconds).
        starts: dict[int, float] = {}
        cursor = 0.0
        prev: dict | None = None
        for s in segs:  # already sorted by index
            if prev is not None:
                cursor += boundary_gap_ms(
                    prev["pause_after"], prev["speaker"],
                    s["speaker"], pause_ms, same_ms,
                ) / 1000.0
            starts[s["index"]] = cursor
            cursor += durs[s["index"]]
            prev = s

        # 4) scene assembly (the same open-scene state machine as
        # analyze_segment_chapter): a new-scene block opens a scene; a
        # CONTIGUOUS ``extend`` block (``e1 + 1 == start``) extends the open
        # scene in place; a NON-contiguous extend (a fully-silent batch
        # between the scene's end and this batch — the silent batch does not
        # close the scene) reopens the scene's identity at the extend block:
        # the silent gap stays silent (never covered) and the resumed scene
        # is a SEPARATE scene, so the entry-adjacent same-track merge cannot
        # bridge the gap. An orphan extend with no open scene is dropped
        # (defensive — the parser rejects every other malformation).
        scenes: list[dict] = []
        open_sc: dict | None = None
        for blk in a["blocks"]:
            if not isinstance(blk, dict) \
                    or not isinstance(blk.get("start"), int) \
                    or not isinstance(blk.get("end"), int):
                continue  # 畸形块防御性跳过
            if blk.get("extend"):
                if open_sc is not None:
                    if open_sc["e1"] + 1 == blk["start"]:
                        open_sc["e1"] = blk["end"]
                    else:
                        # 隔静音批的延展：以原场景身份开新场景（间隙保留）。
                        scenes.append(open_sc)
                        open_sc = {
                            "e0": blk["start"], "e1": blk["end"],
                            "tags": open_sc["tags"],
                            "intensity": open_sc["intensity"],
                            "scene": open_sc["scene"], "mood": open_sc["mood"],
                            "reason": open_sc["reason"],
                        }
            else:
                if open_sc is not None:
                    scenes.append(open_sc)
                open_sc = {
                    "e0": blk["start"],
                    "e1": blk["end"],
                    "tags": blk.get("music_tags") or {},
                    "intensity": blk.get("intensity") or 2,
                    "scene": blk.get("scene") or "",
                    "mood": blk.get("mood") or "",
                    "reason": blk.get("reason") or "",
                }
        if open_sc is not None:
            scenes.append(open_sc)

        # 5) time mapping (span start = the scene's FIRST present entry's
        # audio start; span END = the scene's LAST present entry's audio END
        # — the boundary pause falls into the inter-span gap; clamped to the
        # narration total to absorb any 06/05 drift). A scene whose entries
        # all lack audio is dropped.
        timed: list[dict] = []
        for sc in scenes:
            present = [i for i in range(sc["e0"], sc["e1"] + 1) if i in starts]
            if not present:
                continue
            sc["start"] = starts[present[0]]
            sc["end"] = min(starts[present[-1]] + durs[present[-1]], total)
            timed.append(sc)
        scenes = timed

        # 6) mechanical matching (NO adjacent de-dup — that amplifier forced
        # adjacent similar scenes onto different tracks; identical tags
        # short-circuit to the previous pick verbatim — deterministic track
        # reuse, no rng consumption).
        base = max(1, int(min_score))
        picks: list[dict] = []
        for k, sc in enumerate(scenes):
            if k > 0 and sc["tags"] == scenes[k - 1]["tags"]:
                picks.append(picks[k - 1])
            else:
                picks.append(match_chapter(sc["tags"], tracks, base,
                                           None, None, rng=rng, mode="llm"))

        # 7) merge adjacent same-track scenes (one span: no overlapping
        # fades; volume = the run's MAX intensity; tags = the union,
        # kept-first) — keyed on ENTRY-RANGE adjacency (``_e1 + 1 == _e0``),
        # NEVER time contiguity: the boundary pause always separates
        # adjacent blocks in time, and an entry-range gap = intentional
        # silence, never covered.
        spans: list[dict] = []
        for sc, pick in zip(scenes, picks):
            if pick["music"] is None:
                continue
            if (spans and spans[-1]["music_id"] == pick["music"]
                    and spans[-1]["_e1"] + 1 == sc["e0"]):
                last = spans[-1]
                last["end"] = round(min(sc["end"], total), 3)
                last["_run"] += 1
                last["intensity"] = max(last["intensity"], sc["intensity"])
                last["volume"] = intensity_volume(
                    last["intensity"], volume_base, volume_tiers)
                for c in music_engine.TAG_CATEGORIES:
                    for t in sc["tags"].get(c) or []:
                        if t not in last["tags"][c]:
                            last["tags"][c].append(t)
                last["_e1"] = sc["e1"]
            else:
                spans.append({
                    "start": round(sc["start"], 3),
                    "end": round(sc["end"], 3),
                    "music_id": pick["music"],
                    "intensity": sc["intensity"],
                    "volume": intensity_volume(sc["intensity"], volume_base, volume_tiers),
                    "tags": {c: list(sc["tags"].get(c) or [])
                             for c in music_engine.TAG_CATEGORIES},
                    "score": pick["score"],
                    "reason": pick["reason"],
                    "scene_desc": sc["scene"],
                    "mood_desc": sc["mood"],
                    "switch_reason": sc["reason"],
                    "_base_reason": pick["reason"],
                    "_run": 1,
                    "_short": 0,
                    "_e0": sc["e0"],
                    "_e1": sc["e1"],
                })

        # 8) validation A — 短段并入 (a span shorter than _MIN_SPAN_S folds
        # into a neighbour: most-similar tags first, then the LONGER
        # neighbour, then the earlier one. Time range = the union when
        # entry-adjacent, else the NEIGHBOUR's range — the short interval
        # becomes silence. A lone short span is kept).
        while True:
            target_k = None
            for k, s in enumerate(spans):
                if s["end"] - s["start"] < _MIN_SPAN_S \
                        and (k > 0 or k + 1 < len(spans)):
                    target_k = k
                    break
            if target_k is None:
                break
            s = spans[target_k]
            neighbours = []
            if target_k > 0:
                neighbours.append(target_k - 1)
            if target_k + 1 < len(spans):
                neighbours.append(target_k + 1)

            def _nb_key(j: int) -> tuple:
                nb = spans[j]
                return (
                    -score_track(s["tags"], nb["tags"])[0],
                    -(nb["end"] - nb["start"]),
                    j,
                )

            j = min(neighbours, key=_nb_key)
            nb = spans[j]
            adjacent = (j == target_k - 1 and nb["_e1"] + 1 == s["_e0"]) or \
                       (j == target_k + 1 and s["_e1"] + 1 == nb["_e0"])
            if adjacent:
                nb["start"] = round(min(nb["start"], s["start"]), 3)
                nb["end"] = round(min(max(nb["end"], s["end"]), total), 3)
                nb["_run"] += s["_run"]
            for c in music_engine.TAG_CATEGORIES:
                for t in s["tags"].get(c) or []:
                    if t not in nb["tags"][c]:
                        nb["tags"][c].append(t)
            nb["intensity"] = max(nb["intensity"], s["intensity"])
            nb["volume"] = intensity_volume(
                nb["intensity"], volume_base, volume_tiers)
            nb["_short"] += 1
            del spans[target_k]

        # 9) validation B — 时长上限 (cap = max(3, ceil(分钟数)) — the SAME
        # number as the prompt's K reference; over the cap, merge the
        # highest tag-overlap adjacent pair (tie → entry-adjacent pairs,
        # tie → earliest), keeping the LONGER span (tie → earlier). Never
        # pads to a lower bound — few scenes stay few; B only EXTENDS time
        # ranges, so it cannot create new short spans (no A/B loop).
        cap = max(3, math.ceil(total / 60.0))
        while len(spans) > cap:
            best_k = None
            best_key: tuple | None = None
            for k in range(len(spans) - 1):
                sa, sb = spans[k], spans[k + 1]
                key = (
                    -score_track(sa["tags"], sb["tags"])[0],
                    not (sa["_e1"] + 1 == sb["_e0"]),
                    k,
                )
                if best_key is None or key < best_key:
                    best_key, best_k = key, k
            if best_k is None:
                break
            sa, sb = spans[best_k], spans[best_k + 1]
            kept, absorbed = (sa, sb) if (sa["end"] - sa["start"]) >= \
                                    (sb["end"] - sb["start"]) else (sb, sa)
            absorbed_idx = best_k + 1 if kept is sa else best_k
            kept["start"] = round(min(sa["start"], sb["start"]), 3)
            kept["end"] = round(min(max(sa["end"], sb["end"]), total), 3)
            kept["_run"] += absorbed["_run"]
            for c in music_engine.TAG_CATEGORIES:
                for t in absorbed["tags"].get(c) or []:
                    if t not in kept["tags"][c]:
                        kept["tags"][c].append(t)
            kept["intensity"] = max(kept["intensity"], absorbed["intensity"])
            kept["volume"] = intensity_volume(
                kept["intensity"], volume_base, volume_tiers)
            del spans[absorbed_idx]

        # 10) finalize (drop the bookkeeping keys; reason = the first
        # pick's reason + the merge notes).
        for s in spans:
            s["reason"] = s["_base_reason"] \
                + (f"（合并 {s['_run']} 段）" if s["_run"] > 1 else "") \
                + ("（短段并入）" if s["_short"] > 0 else "")
            s.pop("_base_reason", None)
            s.pop("_run", None)
            s.pop("_short", None)
            s.pop("_e0", None)
            s.pop("_e1", None)

        # 11) the timeline file (version 2: spans carry the scene description
        # + the switch reason for the 时间轴 dialog; v1 files stay playable —
        # build_timeline_mix_cmd reads only start/end/music_id/volume).
        timeline = {
            "version": 2,
            "stem": stem,
            "generated_at": now,
            "model": a.get("model", ""),
            "fingerprint": a.get("fingerprint", ""),
            "entry_count": entry_count,
            "duration": round(total, 3),
            "source": _segment_source_snapshot(
                entries, manifest, segs, durs, narration, pause_ms, same_ms,
            ),
            "timeline": spans,
        }
        timeline_writes.append((stem, timeline))

        # 12) the assignment entry (written in ONE transaction after all
        # stems). tags = the union over ALL assembled scenes (pre-validation).
        union = {c: [] for c in music_engine.TAG_CATEGORIES}
        for sc in scenes:
            for c in music_engine.TAG_CATEGORIES:
                for t in sc["tags"].get(c) or []:
                    if t not in union[c]:
                        union[c].append(t)
        reason = (f"段落级时间轴（{len(spans)} 段 BGM）" if spans
                  else "段落级（全章无 BGM）")
        assign_entries[stem] = {
            "tags": union,
            "music": None,
            "segment": True,
            "locked": False,
            "manual": False,
            "score": None,
            "reason": reason,
            "matched_at": now,
        }
        if spans:
            matched += 1
        else:
            no_bgm += 1

    def _mutate(data: dict) -> None:
        chapters = data.setdefault("chapters", {})
        data["mode"] = "segment"
        for stem, e in assign_entries.items():
            cur = chapters.get(stem)
            if isinstance(cur, dict) and cur.get("locked"):
                continue  # locked — the entry is preserved verbatim (counted below)
            chapters[stem] = e
        data["updated_at"] = now

    cur_chapters = load_assignments(layout).get("chapters") or {}
    skipped_locked = sum(
        1 for stem in assign_entries
        if isinstance(cur_chapters.get(stem), dict) and cur_chapters[stem].get("locked")
    )
    if timeline_writes:
        for stem, tl in timeline_writes:
            _atomic_write_json(_timeline_path(layout, stem), tl, handle)
        update_assignments(layout, _mutate, handle=handle)
    return {"mode": "segment", "matched": matched, "no_bgm": no_bgm,
            "skipped_locked": skipped_locked}


# --------------------------------------------------------------------------- #
# Task worker 2: final mix (module ``bgm-mix``)
# --------------------------------------------------------------------------- #

def _find_narration(layout, stem: str) -> Path | None:
    """06 旁白：``<stem>.mp3``（``.wav`` 兜底）。"""
    d = layout.audio_merge
    if d is None or not d.exists():
        return None
    for ext in (".mp3", ".wav"):
        p = d / f"{stem}{ext}"
        if p.is_file():
            return p
    return None


def mix_chapter(handle, stem: str, bgm_cfg, ffmpeg_cfg) -> dict:
    """Task worker: mix ONE chapter (06 narration + the matched music →
    ``08_bgm/<stem>.mp3``).

    * ``music=None`` (matched but no BGM) → plain ``shutil.copy2`` of the 06
      narration (still a finished product);
    * the music slot holds the process-wide MERGE gate for the whole ffmpeg run
      (ffmpeg/CPU/disk bound — same gate as audio merge);
    * cancel → kill ffmpeg + TaskCancelled (a partial output stays on disk —
      a rerun with ``-y`` self-heals, the merge precedent);
    * ffmpeg's stderr is drained by a PUMP THREAD the whole time the process
      runs — the Windows pipe capacity is 4KB and a timeline mix (K+1 inputs +
      ``filter_complex``) dumps more startup output than that, so without a
      concurrent reader ffmpeg blocks on its stderr write before it ever opens
      the output file (progress frozen at 5%, task never finishes). The pump
      keeps only the last 4KB as the error-report tail (same idiom as
      ``audio.detect_silences``).
    """
    handle.check()
    layout = core_paths.get_layout()
    if layout.bgm is None:
        raise RuntimeError("未设置工作空间。")
    narration = _find_narration(layout, stem)
    if narration is None:
        raise RuntimeError(
            f"未找到旁白音频（06_audio_merge/{stem}.mp3），请先完成音频合并。"
        )

    data = load_assignments(layout)
    entry = (data.get("chapters") or {}).get(stem)
    if not isinstance(entry, dict):
        raise RuntimeError("该章从未匹配，请先匹配。")
    music_name = entry.get("music")

    final_out = layout.bgm / f"{stem}.mp3"
    allocate_workspace_stage = getattr(handle, "allocate_workspace_stage", None)
    durable_stage = callable(allocate_workspace_stage)
    out = allocate_workspace_stage(final_out) if durable_stage else final_out

    def publish_output() -> Path:
        if durable_stage:
            handle.publish_workspace_stage(final_out, out)
            return final_out
        return out
    requested_music_name = music_name
    cfg_now = get_config()
    pause_ms = cfg_now.tts.pause_between_speakers_ms or 500
    same_ms = cfg_now.tts.pause_same_speaker_ms or 250

    # 段落级时间轴分支：span 的曲目/定位/音量/淡入淡出全部由「匹配（时间轴）」
    # 阶段算好，这里不做任何语义判断。章节级匹配如果只重置了 assignment.segment，
    # 但当前段落分析和时间轴仍有效，也要恢复使用这份已匹配结果；真正遗留的时间轴
    # 仍会被 load_segment_timeline 忽略。
    tl = load_segment_timeline(layout, stem, entry)
    if entry.get("segment") and tl is None:
        raise RuntimeError(
            "段落时间轴缺失或已过期，请重新“匹配”（段落级，不调用 LLM）。"
        )
    cmd = None
    dur = 0.0
    if tl is not None:
        inputs = validate_segment_timeline(
            layout, stem, tl, ffmpeg_cfg.ffprobe_path,
            pause_ms, same_ms,
        )
        narration = inputs[4]
        ffprobe = ffmpeg_cfg.ffprobe_path
        nar_dur, perr = probe_duration(narration, ffprobe)
        if not (nar_dur > 0):
            raise RuntimeError(f"无法探测旁白时长（{perr or 'ffprobe 失败'}）。")
        # 新鲜度锚点：时间轴记录的 06 时长 vs 现探时长（0.5s 容差）。
        if abs(nar_dur - float(tl.get("duration") or 0.0)) > _STALE_TOLERANCE_S:
            raise RuntimeError(
                "时间轴已过期（旁白时长变化），请重新「匹配」（段落级，不调用 LLM）。")
        lib_dir = core_paths.MUSIC_LIBRARY_DIR
        for s in tl.get("timeline") or []:
            mid = s.get("music_id") or ""
            if not (lib_dir / mid).is_file():
                raise RuntimeError(
                    f"音乐库中找不到 {mid}，请先到「音乐库」页检查或重新匹配（{stem}）。")
        spans = tl.get("timeline") or []
        if not spans:
            # 全章无 BGM（零 span）：直接复制旁白（成品，与 music=None 同径）。
            handle.log("该章段落级时间轴无 BGM 段，直接复制旁白音频。")
            layout.bgm.mkdir(parents=True, exist_ok=True)
            _copy_narration_after_merge_gate(
                handle, layout, stem, out, ffmpeg_cfg, tl, pause_ms, same_ms,
            )
            handle.progress(1.0, "完成")
            published = publish_output()
            return {"stem": stem, "file": published.name, "path": str(published),
                    "duration": None, "music": None}
        cmd = build_timeline_mix_cmd(
            ffmpeg_cfg.ffmpeg_path or "ffmpeg", narration, out, spans, lib_dir,
            nar_dur, bgm_cfg, threads=thread_budget(),
        )
        dur = nar_dur
        music_name = f"段落级时间轴（{len(spans)} 段 BGM）"
    elif not music_name:
        # 无 BGM 章：直接复制旁白（成品已生成，mix_exists=True）。
        handle.log("该章无 BGM，直接复制旁白音频。")
        layout.bgm.mkdir(parents=True, exist_ok=True)
        _copy_narration_after_merge_gate(
            handle, layout, stem, out, ffmpeg_cfg, tl, pause_ms, same_ms,
        )
        handle.progress(1.0, "完成")
        published = publish_output()
        return {"stem": stem, "file": published.name, "path": str(published),
                "duration": None, "music": None}

    if cmd is None:
        lib_dir = core_paths.MUSIC_LIBRARY_DIR
        music_path = lib_dir / music_name
        if not music_path.is_file():
            raise RuntimeError(f"音乐库中找不到 {music_name}，请先到「音乐库」页检查。")
        ffprobe = ffmpeg_cfg.ffprobe_path
        dur, err = probe_duration(narration, ffprobe)
        if not (dur > 0):
            raise RuntimeError(f"无法探测旁白时长（{err or 'ffprobe 失败'}）。")
        m_dur, _m_err = probe_duration(music_path, ffprobe)
        if not (m_dur > 0):
            raise RuntimeError(f"无法探测音乐时长（{_m_err or 'ffprobe 失败'}）。")
        cmd = build_mix_cmd(
            ffmpeg_cfg.ffmpeg_path or "ffmpeg", narration, music_path, out,
            dur, bgm_cfg, m_dur,
            # Bound the encoder's threads — merge.py sizes the budget so the
            # whole merge_gate (shared with audio merge) occupies at most half
            # the cores.
            threads=thread_budget(),
        )
    handle.log("开始混音：" + " ".join(cmd[:4]) + " …")

    if not merge_gate().acquire(stop_check=lambda: handle.cancelled):
        raise TaskCancelled()  # 排队中被取消——未取闸，下面 finally 不得 release
    acquired = True
    proc = None
    stderr_tail_box: list[bytes] = [b""]
    try:
        # Revalidate after waiting for merge_gate: files, manifest, pauses,
        # assignment and timeline must still describe the same mix.
        latest_data = load_assignments(layout)
        latest_entry = (latest_data.get("chapters") or {}).get(stem)
        if not isinstance(latest_entry, dict):
            raise RuntimeError("混音输入在等待期间已变化，请重新发起混音。")
        latest_timeline = load_segment_timeline(layout, stem, latest_entry)
        if tl is not None:
            if latest_timeline is None:
                raise RuntimeError(
                    "段落时间轴缺失或已过期，请重新“匹配”（段落级，不调用 LLM）。"
                )
            latest_cfg = get_config()
            latest_pause_ms = latest_cfg.tts.pause_between_speakers_ms or 500
            latest_same_ms = latest_cfg.tts.pause_same_speaker_ms or 250
            latest_inputs = validate_segment_timeline(
                layout, stem, latest_timeline, ffmpeg_cfg.ffprobe_path,
                latest_pause_ms, latest_same_ms,
            )
            narration = latest_inputs[4]
            dur = latest_inputs[5]
            spans = latest_timeline.get("timeline") or []
            if not spans:
                layout.bgm.mkdir(parents=True, exist_ok=True)
                shutil.copy2(narration, out)
                handle.progress(1.0, "完成")
                published = publish_output()
                return {"stem": stem, "file": published.name, "path": str(published),
                        "duration": None, "music": None}
            lib_dir = core_paths.MUSIC_LIBRARY_DIR
            for span in spans:
                mid = span.get("music_id") or ""
                if not (lib_dir / mid).is_file():
                    raise RuntimeError(
                        f"音乐库中找不到 {mid}，请先检查音乐库或重新匹配（{stem}）。"
                    )
            cmd = build_timeline_mix_cmd(
                ffmpeg_cfg.ffmpeg_path or "ffmpeg", narration, out, spans,
                lib_dir, dur, bgm_cfg, threads=thread_budget(),
            )
            music_name = f"段落级时间轴（{len(spans)} 段 BGM）"
        else:
            if latest_entry.get("music") != requested_music_name:
                raise RuntimeError("混音输入在等待期间已变化，请重新发起混音。")
            narration = _find_narration(layout, stem)
            if narration is None:
                raise RuntimeError(
                    f"未找到旁白音频（06_audio_merge/{stem}.mp3）。"
                )
            lib_dir = core_paths.MUSIC_LIBRARY_DIR
            music_path = lib_dir / requested_music_name
            if not music_path.is_file():
                raise RuntimeError(
                    f"音乐库中找不到 {requested_music_name}，请先检查音乐库。"
                )
            dur, err = probe_duration(narration, ffmpeg_cfg.ffprobe_path)
            if not (dur > 0):
                raise RuntimeError(f"无法探测旁白时长（{err or 'ffprobe 失败'}）。")
            m_dur, m_err = probe_duration(music_path, ffmpeg_cfg.ffprobe_path)
            if not (m_dur > 0):
                raise RuntimeError(f"无法探测音乐时长（{m_err or 'ffprobe 失败'}）。")
            cmd = build_mix_cmd(
                ffmpeg_cfg.ffmpeg_path or "ffmpeg", narration, music_path, out,
                dur, bgm_cfg, m_dur, threads=thread_budget(),
            )
        handle.progress(0.05, "混音中")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            cwd=str(core_paths.PROJECT_ROOT),
        )
        # stderr 必须被并发抽干（见 docstring）：泵线程持续读取、只保留最后
        # 4KB 作为错误报告尾；进程退出 → 管道 EOF → 泵自行收尾并关管道。
        def _stderr_pump() -> None:
            tail = b""
            try:
                while True:
                    chunk = proc.stderr.read(4096)
                    if not chunk:
                        break
                    tail = (tail + chunk)[-4096:]
                stderr_tail_box[0] = tail
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    proc.stderr.close()
                except Exception:  # noqa: BLE001
                    pass

        pump = threading.Thread(target=_stderr_pump, daemon=True)
        pump.start()
        start = time.monotonic()
        last_size = 0.0
        # 0.2 s 轮询：取消响应 + 进度粗估（输出文件增长 0.05→0.95）。
        while proc.poll() is None:
            handle.check()  # TaskCancelled → finally kill
            elapsed = time.monotonic() - start
            if out.exists():
                size = out.stat().st_size
                if size > last_size and dur > 0:
                    last_size = size
                    frac = 0.05 + 0.90 * min(1.0, elapsed / max(1.0, dur))
                    handle.progress(min(0.95, frac), "混音中")
            time.sleep(0.2)
        rc = proc.poll()
        proc.wait(timeout=10)
        pump.join(timeout=5)  # 进程已退出（EOF）→ 泵必然已读完并收尾
        stderr_tail = stderr_tail_box[0].decode("utf-8", "replace")
        if rc != 0:
            raise RuntimeError(f"混音失败（ffmpeg 退出码 {rc}）：{stderr_tail.strip()[-300:]}")
        if not out.exists() or out.stat().st_size < 1024:
            raise RuntimeError(
                f"混音输出异常（缺失或小于 1KiB）：{stderr_tail.strip()[-300:]}"
            )
        handle.progress(1.0, "完成")
        published = publish_output()
        return {"stem": stem, "file": published.name, "path": str(published),
                "duration": round(dur, 3), "music": music_name}
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
        if acquired:
            merge_gate().release()


# --------------------------------------------------------------------------- #
# matching (deterministic, synchronous — no task)
# --------------------------------------------------------------------------- #

def match_stems(
    layout,
    stems: list[str],
    mode: str,
    min_score: int,
    rng: random.Random | None = None,
    handle=None,
) -> dict:
    """Re-match the given chapters (sorted), writing the assignments file ONCE.

    The whole read-modify-write runs inside one ``update_assignments``
    transaction (the module lock is held across load → mutate → save), so
    concurrent matches on disjoint stems cannot lose each other's entries.

    Locked chapters are skipped wholesale (their entry is preserved verbatim,
    ``matched_at`` untouched). The ``prev`` neighbour is the previous 02
    chapter's FRESH result when it is part of this call, else its EXISTING
    assignment (single-chapter re-match boundary — neighbours are never
    touched); ``next`` is always the next chapter's existing value.
    """
    idx = music_engine.load_index()
    tracks = list(idx["tracks"].items())
    analysis = load_analysis(layout).get("chapters") or {}
    chapter_stems = list_chapter_stems(layout)
    stem_positions = {stem: i for i, stem in enumerate(chapter_stems)}
    rng = rng or random.Random()
    counts: dict[str, int] = {}

    def _mutate(data: dict) -> None:
        chapters = data.setdefault("chapters", {})
        data["mode"] = mode
        matched = 0
        no_bgm = 0
        skipped_locked = 0
        fresh: dict[str, str | None] = {}  # stem → music this call (locked = existing)
        now = datetime.now().isoformat(timespec="seconds")
        for stem in sorted(set(stems)):
            cur = chapters.get(stem)
            if isinstance(cur, dict) and cur.get("locked"):
                skipped_locked += 1
                fresh[stem] = cur.get("music")  # a locked neighbour still constrains
                continue
            cur_tags = analysis.get(stem) or {}
            stem_index = stem_positions.get(stem)
            prev_key = (
                chapter_stems[stem_index - 1]
                if stem_index is not None and stem_index > 0
                else ""
            )
            next_key = (
                chapter_stems[stem_index + 1]
                if stem_index is not None and stem_index + 1 < len(chapter_stems)
                else ""
            )
            prev_music = None
            if prev_key:
                if prev_key in fresh:
                    prev_music = fresh[prev_key]  # 上一章本次已处理 → 用新鲜结果
                else:
                    p = chapters.get(prev_key)
                    prev_music = p.get("music") if isinstance(p, dict) else None
            nxt = chapters.get(next_key)
            next_music = nxt.get("music") if isinstance(nxt, dict) else None
            res = match_chapter(
                cur_tags, tracks, max(1, int(min_score)),
                prev_music, next_music, rng=rng, mode=mode,
            )
            entry = {
                "tags": {c: list(cur_tags.get(c) or [])
                         for c in music_engine.TAG_CATEGORIES},
                "music": res["music"],
                # 章节模式匹配把该章的任何遗留时间轴作废（文件留盘不删；
                # 混音/行字段只认 segment=true 的条目 → 陈旧时间轴惰性）。
                "segment": False,
                "locked": False,
                "manual": False,
                "score": res["score"],
                "reason": res["reason"],
                "matched_at": now,
            }
            if res["music"] is None:
                entry["score"] = 0
            chapters[stem] = entry
            if res["music"] is None:
                no_bgm += 1
            else:
                matched += 1
            fresh[stem] = res["music"]
        data["updated_at"] = now
        counts.update(matched=matched, no_bgm=no_bgm, skipped_locked=skipped_locked)

    data = update_assignments(layout, _mutate, handle=handle)
    return {"mode": mode, "matched": counts["matched"], "no_bgm": counts["no_bgm"],
            "skipped_locked": counts["skipped_locked"], "assignments": data}
