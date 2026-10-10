"""Character voice-prep engine (port of source ``app/generate_personas.py``).

Splits voice preparation into two independent phases so the LLM and the TTS engine
never share the GPU at once (each can then run at its own max concurrency):

**Phase 1 — ``prepare_foundations`` (LLM only, no TTS).** Detects every speaker in
the parsed script (in order of first appearance), keeps alias hints for display only
without inferring character identity from names, then — in parallel, bounded by the host-wide LLM concurrency limit —
asks the LLM for each character's voice *foundation*: a ``description`` + a
multi-sentence ``ref_text`` seed, reasoned from the character's own lines sampled
across the book (front / middle / back), each carrying its ±window local context
(surrounding narration and other characters). The foundation is persisted to
``voice_config.json`` as ``type: "foundation"``. No TTS runs in this phase.

**Phase 2 — ``generate_voice_candidates`` (TTS only, no LLM).** Reads back the persisted
foundations and renders every character's clone *candidate* seed WAVs in ONE
long-lived shared ``.venv`` subprocess (the worker's ``design-batch`` mode: the
VoiceDesign model is loaded once, and the candidates run as native tensor
sub-batches whose size a measured VRAM governor sets at runtime — the caller's
``concurrency`` is only the per-batch *ceiling*). Each candidate is an independent,
differently-seeded render (seeded by its sub-batch), so they can be auditioned and
the best kept. The entry stores the candidate list (``candidates``) plus the
user's pick (``selected_audio_id``); the top-level ``ref_audio`` always mirrors the
*active* candidate (the selected one, or the first when none is picked) so the
downstream 音频合成 stage needs no changes. A partial failure still yields a usable
clone (the successful candidates); a total failure falls back to a **design** voice
(``type: design``) and keeps the last-known ``ref_audio`` for previewing.
A hung / OOM-killed child (the worker's watchdog, exit 124) is recovered by
shrinking the per-batch cap and restarting a fresh subprocess that adopts the
candidates already rendered on disk (recorded with seed ``-1``); on cancel the
worker's process tree is killed so GPU memory is freed at once.

The book's per-line synthesis remains the separate 音频合成 (``tts_batch``) stage, which
consumes the finished ``voice_config.json``. A single character's foundation / clone can
be (re)generated in isolation via ``speakers`` (+ an optional ``description`` override).

Both operations receive the durable task context, report progress and logs to task history,
and honour cooperative cancellation between characters. A per-character
failure is recorded (and, in phase 2, that character falls back to ``design``) — it never
aborts the whole run; only a *fatal* error (no script, no engine) raises.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import time
import uuid
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from ..core.input_versions import check_bound_inputs
from contextvars import copy_context
from pathlib import Path

from ..core import pathio
from ..core.concurrency import gate
from ..core.config import get_config
from ..core.file_lock import exclusive_file_lock
from ..core.role_hints import suggest_role_hints
from ..core.filenames import safe_filename
from ..core.task_control import TaskCancelled
from ..core.paths import ALL_PARSED_JSON, get_or_prepare_layout, resolve_parsed_json, resolve_parsed_json_all
from ..platform.quota import QuotaInsufficientError
from ..platform.system_config import parse_worker_concurrency
from .persona_prompts import load_persona_prompts
from .tts import WorkerWatchdogTimeout, resolve_engine, run_tts_subprocess
from .tts_batch import (
    _parse_watchdog_indices,
    clamp_concurrency,
    invalidate_speaker_outputs,
)


# How much local context (entries, on each side) is attached to each sampled target
# line, and how many target lines are drawn from each of the front / middle / back bands.
CONTEXT_WINDOW = 4
SAMPLES_PER_BAND = 8


# ---------------------------------------------------------------------------
# Pure helpers (1:1 ports of generate_personas.py / project.py)
# ---------------------------------------------------------------------------

def extract_json_object(text):
    """Find and parse the first JSON object in ``text`` (port of the source)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    end = None
    for i, ch in enumerate(text[start:], start):
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
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        return json.loads(text[start:end])
    except Exception:  # noqa: BLE001
        return None


def normalize_speaker_name(name):
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFKC", name).strip().casefold()
    s = re.sub(r"^(mr|mrs|ms|miss|dr|prof|sir|lady|lord)\.?\s+", "", s)
    s = "".join(c for c in s if c.isalnum() or c.isspace())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity on normalized name tokens."""
    norm_a, norm_b = normalize_speaker_name(a), normalize_speaker_name(b)
    if not norm_a or not norm_b:
        return 0.0
    tokens_a, tokens_b = set(norm_a.split()), set(norm_b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a.intersection(tokens_b)) / len(tokens_a.union(tokens_b))


def _resolve_to_canonical(raw_name: str, allowed, threshold=0.4):
    """Map a raw name to the closest canonical label, or None (exact → substring → Jaccard)."""
    if not raw_name:
        return None
    norm_raw = normalize_speaker_name(raw_name)
    if not norm_raw:
        return None
    for name in allowed:
        if normalize_speaker_name(name) == norm_raw:
            return name
    for name in allowed:
        norm_name = normalize_speaker_name(name)
        if norm_name and (norm_name in norm_raw or norm_raw in norm_name):
            return name
    best_name, best_score = None, 0.0
    for name in allowed:
        score = _token_jaccard(raw_name, name)
        if score > best_score:
            best_score, best_name = score, name
    return best_name if best_score >= threshold else None


def _entry_speaker(entry):
    return (entry.get("speaker") or entry.get("type") or "").strip()


def _entry_text(entry):
    return (entry.get("text") or "").strip()


def _select_target_bands(pairs, per=SAMPLES_PER_BAND):
    """Split a character's own lines into (front, middle, back) bands of script indices.

    ``pairs`` is a list of ``(script_index, text)`` for the character's lines, in script
    order. ``front`` = the first ``per``; ``back`` = the last ``per``; ``middle`` =
    ``per`` lines spread evenly through the middle region. When the character has too few
    lines to fill three bands, all of them are returned as ``front`` (none dropped), so a
    small cast still gets every line with its context. Returns three lists of indices.
    """
    if hasattr(pairs, "bands"):
        return pairs.bands(per)
    n = len(pairs)
    if n == 0:
        return [], [], []
    idxs = [i for i, _t in pairs]
    if n <= 3 * per:
        return idxs, [], []
    front = idxs[:per]
    back = idxs[-per:]
    region = idxs[per:n - per]
    if len(region) <= per:
        middle = list(region)
    else:
        step = (len(region) - 1) / (per - 1)
        middle = [region[round(k * step)] for k in range(per)]
    return front, middle, back


def _window_block(script, idx, window=CONTEXT_WINDOW):
    """One target line with its ±``window`` surrounding entries (any speaker) as a block.

    The surrounding entries keep script order; the target line is marked ``★`` and the
    rest are indented. Entries with empty text are dropped, and the window clamps at the
    start / end of the script (fewer than ``window`` neighbours are taken when present).
    """
    total = len(script)
    lo = max(0, idx - window)
    hi = min(total, idx + window + 1)
    lines = []
    for j, entry in enumerate(script[lo:hi], lo):
        txt = _entry_text(entry)
        if not txt:
            continue
        spk = _entry_speaker(entry) or "(?)"
        marker = "★ " if j == idx else "   "
        lines.append(f"{marker}{spk}: {txt}")
    return "\n".join(lines)


def pick_ref_text(lines):
    """First line long enough to be a good clone reference; else the first non-empty line."""
    for ln in lines:
        if ln and len(ln.strip()) >= 12:
            return ln.strip()
    return next((ln.strip() for ln in lines if ln and ln.strip()), "")


#: Voice description used when the LLM is unavailable or unparseable (no gender word,
#: so the gender badge stays 未定).
FALLBACK_DESCRIPTION = "音色清晰自然，音高适中，语速平稳，适合有声书朗读。"


def _fallback_ref_text(speaker):
    """Last-resort reference-clip text (≈40 字 of calm Chinese) when the character has
    no usable line: the clip seeds the whole book's clone, so it must be speakable."""
    if speaker == "NARRATOR":
        return "夜色渐渐深了，街上的行人越来越少，只有路灯还静静地亮着，故事就从这里开始。"
    # Only a short all-CJK name is read out; Latin/digit/over-long names would break the
    # 40–60 字 budget or read as a mixed-language clip.
    who = f"我是{speaker}。" if re.fullmatch(r"[\u4e00-\u9fff]{1,6}", speaker or "") else ""
    return f"你好，{who}今天天气不错，我们一边走一边慢慢聊吧，有什么想说的都可以告诉我。"


def _fallback_persona(speaker, lines):
    """A minimal, always-valid persona if the LLM is unavailable or unparseable."""
    return FALLBACK_DESCRIPTION, pick_ref_text(lines), ""


def _normalize_gender(value) -> str:
    """Coerce an LLM gender answer to ``"male"`` / ``"female"`` / ``""`` (unknown)."""
    v = str(value or "").strip().lower()
    if v in ("male", "男", "男人", "男性"):
        return "male"
    if v in ("female", "女", "女人", "女性"):
        return "female"
    return ""


def _gender_from_description(description: str) -> str:
    """Gender fallback for prompts that don't emit a ``gender`` key: the description
    itself is required to state the voice's gender ("青年男性" / "少女女声"), so scan it."""
    d = str(description or "")
    if re.search(r"女性|女声", d):
        return "female"
    if re.search(r"男性|男声", d):
        return "male"
    return ""


def _sanitize(name):
    return re.sub(r"[^\w\-]", "_", name or "unknown").lower()


# ---------------------------------------------------------------------------
# LLM + worker bridges
# ---------------------------------------------------------------------------

def _llm_persona(handle, llm, system, user_template, speaker, script, bands):
    """Ask the LLM for a character's voice ``description`` + a ``ref_text`` seed.

    Uses the shared stdlib-urllib LLM transport. The prompt feeds the
    character's own lines sampled across the book (front / middle / back) with each line's
    ±window local context (see :func:`_select_target_bands` / :func:`_window_block`), so
    the model judges the voice from the character's full range of delivery, not just the
    intro. One retry, then the caller falls back to :func:`_fallback_persona`. Returns
    ``(description, ref_text, gender)`` — ``gender`` is ``"male"`` / ``"female"`` / ``""``
    (unknown): the explicit ``gender`` key wins, else the description's own gender words
    (a prompt without the key still states it there).
    """
    from .llm_transport import LLMUnavailableError, cache_kwargs, request_chat_completion as _llm_chat_completion

    if not (llm.model_name or "").strip():
        raise RuntimeError("未配置 LLM 模型名称（在「文本解析」页填写模型）。")

    front, middle, back = bands

    def band_lines(indices, title):
        if not indices:
            return []
        out = [f"【{title}】"]
        for k, idx in enumerate(indices, 1):
            out.append(f"── 台词 {k} ──")
            out.append(_window_block(script, idx, CONTEXT_WINDOW))
        out.append("")
        return out

    parts = []
    parts += band_lines(front, f"开场 · {speaker} 的前 {len(front)} 句台词")
    parts += band_lines(middle, f"中段 · {speaker} 的中 {len(middle)} 句台词")
    parts += band_lines(back, f"结尾 · {speaker} 的后 {len(back)} 句台词")
    line_windows = "\n".join(parts).strip() or "（该角色没有可用的台词样本。）"

    # str.replace (not str.format) so a user-edited template holding other braces can't
    # raise; {line_windows} is filled before {speaker} (the windows already carry the name).
    user_prompt = (user_template
                   .replace("{line_windows}", line_windows)
                   .replace("{speaker}", speaker))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    gen = get_config().generation
    for attempt in range(2):
        try:
            # 1024 (not 400): the answer now carries a multi-sentence ref_text, and a
            # thinking model spends part of the budget on reasoning before the JSON.
            text, _finish, _usage = _llm_chat_completion(
                llm.base_url, llm.api_key, llm.model_name, messages,
                temperature=0.3, top_p=gen.top_p, presence_penalty=gen.presence_penalty,
                max_tokens=1024, top_k=gen.top_k, min_p=gen.min_p,
                banned_tokens=gen.banned_tokens, **cache_kwargs(llm),
            )
        except LLMUnavailableError:
            raise
        except Exception as e:  # noqa: BLE001 — a failed call retries, then falls back
            handle.log(f"  调用 LLM 出错（{speaker}, 第 {attempt + 1} 次）：{e}", "ERROR")
            continue
        parsed = extract_json_object(text)
        if parsed:
            desc = str(parsed.get("description", "") or "").strip()
            ref = str(parsed.get("ref_text", "") or "").strip()
            if desc:
                from ..platform.quota import consume_llm_output
                consume_llm_output(text, "voices.foundation", operation_key=f"voices.foundation:{speaker}")
                gender = _normalize_gender(parsed.get("gender")) or _gender_from_description(desc)
                return desc, ref, gender
        handle.log(f"  LLM 响应无法解析为 persona（第 {attempt + 1} 次）", "WARNING")
    return "", "", ""


# ---------------------------------------------------------------------------
# Shared preparation helpers (used by both phase workers)
# ---------------------------------------------------------------------------

def _load_script(handle, script_name, speakers=None):
    """Lazily construct one immutable chapter-wise snapshot shared by role tasks."""
    from ..core.script_snapshot import bound_reference, open_snapshot, reference_paths
    layout = get_or_prepare_layout()
    reference = bound_reference()
    if reference is not None:
        if reference.get("script") != script_name:
            raise RuntimeError("任务剧本与共享快照引用不一致。")
        paths = reference_paths(reference, layout.workspace)
    else:
        paths = resolve_parsed_json_all() if script_name == ALL_PARSED_JSON else [resolve_parsed_json(script_name)]
    return open_snapshot(paths, layout.temp / "script-snapshots", check=handle.check,
                         log=handle.log, skip_invalid=script_name == ALL_PARSED_JSON, speakers=speakers,
                         cancelled=lambda: getattr(handle, "cancelled", False),
                         expected_version=reference["version"] if reference else None)


def _collect_samples(script):
    """Each character's OWN lines as (script_index, text), in order of first appearance.

    The index lets the persona prompt attach each sampled line's ±window local context
    (surrounding narration and other characters). Returns ``(samples, order)``.
    """
    if hasattr(script, "samples"):
        return script.samples, list(script.counts)
    samples: dict = {}
    order: list = []
    for i, entry in enumerate(script):
        sp = _entry_speaker(entry)
        if not sp:
            continue
        if sp not in samples:
            samples[sp] = []
            order.append(sp)
        samples[sp].append((i, _entry_text(entry)))
    return samples, order


def _load_voice_config(handle):
    """Load the existing voice_config.json (preserving any hand-edited entries).

    Legacy absolute ``ref_audio`` values that point inside the workspace are
    migrated to the workspace-relative form on load (and the file rewritten),
    so the config keeps working after the workspace folder moves.
    """
    layout = get_or_prepare_layout()
    vc_path = layout.voice_profiles / "voice_config.json"
    voice_config = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception as e:  # noqa: BLE001
            handle.log(f"现有 voice_config.json 无法解析（{e}），将重建。", "WARNING")
        if voice_config:
            _n, migrated = pathio.migrate_entries_in(vc_path, layout.workspace, "dict", ("ref_audio",))
            if migrated is not None:
                voice_config = migrated
            if _n:
                handle.log(f"voice_config.json 已迁移 {_n} 个旧绝对路径为工作目录相对路径。")
    return vc_path, voice_config


def _fold_aliases(handle, selected, voice_config):
    """Suggest role associations without changing any role's independent processing."""
    hints = suggest_role_hints(list(dict.fromkeys([*voice_config, *selected])), voice_config)
    hints = {sp: target for sp, target in hints.items() if sp in selected}
    return list(selected), hints


def _has_foundation(entry) -> bool:
    """Whether a stored entry already carries a voice foundation (a non-empty description)."""
    return bool(entry) and bool((entry.get("description") or "").strip())


def _clone_done(entry) -> bool:
    """Whether a stored entry already holds a usable clone (``type: clone`` + ``ref_audio``)."""
    return bool(entry) and entry.get("type") == "clone" and bool(entry.get("ref_audio"))


def auto_candidate_count(lines: int) -> int:
    """Candidate count in AUTO mode — absolute log-scale bands, no project ratio.

    Budgeting relative to the script's largest line count fails on real books: the
    旁白 routinely carries 10×+ the leads' lines and would flatten every lead to the
    two-candidate floor. Each character is budgeted by its OWN line count on a log
    scale (≈ +3 candidates per order of magnitude, saturating at 8):

    - ``lines < 20`` (cameo / 龙套) → 1 (auto-used; the UI disables manual selection)
    - 20+ → at least 2, then ``int(-2.1 + 3·log10(lines))`` clamped at 8:
      ~100 lines → 3, ~200 → 4, ~500 → 5, ~1000 → 6, ~2000 → 7, ~2300+ → 8
    - always clamped to 1..8
    """
    if lines < 20:
        return 1
    return max(2, min(8, int(-2.1 + 3.0 * math.log10(lines) + 1e-9)))


def _canonical_of(sp, voice_config) -> str:
    """Display hints do not change the role used for synthesis."""
    return sp


def _effective_line_counts(order, samples, voice_config) -> dict:
    """Budget candidates from each role's own lines, ignoring display hints."""
    return {sp: len(samples.get(sp, [])) for sp in order}


def _seed_of(value, default: int = -1) -> int:
    """Best-effort int coercion for a stored seed (display/record only)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def effective_candidates(entry) -> list:
    """The character's usable clone candidates as ``[{id, ref_audio, seed}, ...]``.

    New-format entries keep their ``candidates`` list (items lacking an ``id`` /
    ``ref_audio`` are skipped); legacy entries (no ``candidates`` key) that already hold
    a usable clone are synthesised as a single candidate so the UI stays coherent;
    anything else yields ``[]``. Shared by the /voices listing and the select endpoint
    so the two views can never disagree.
    """
    entry = entry if isinstance(entry, dict) else {}
    raw = entry.get("candidates")
    if isinstance(raw, list) and raw:
        out = []
        for c in raw:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "").strip()
            ref = c.get("ref_audio")
            if not cid or not isinstance(ref, str) or not ref.strip():
                continue
            out.append({"id": cid, "ref_audio": ref.strip(), "seed": _seed_of(c.get("seed"))})
        return out
    if _clone_done(entry):
        return [{"id": "1", "ref_audio": entry["ref_audio"], "seed": _seed_of(entry.get("seed"))}]
    return []


def _clone_have(entry) -> int:
    """How many usable clone candidates an entry already holds (the new_only gate)."""
    return len(effective_candidates(entry))


# ---------------------------------------------------------------------------
# The two-phase Task workers
# ---------------------------------------------------------------------------

def prepare_foundations(handle, speakers=None, new_only=False, overrides=None, script_name=None) -> dict:
    """Phase 1 (LLM only): generate each character's voice *foundation* and persist it.

    The first argument is the durable task context. ``speakers`` is an optional allowlist (single-
    character regeneration); ``new_only`` regenerates only characters without a foundation
    yet; ``overrides`` maps a speaker → a user description (skips the LLM for that
    character); ``script_name`` selects which parsed JSON to read (None → most recent).

    The persona LLM calls run in parallel (bounded by the host-wide LLM concurrency limit) so
    the LLM runs at full concurrency on its own. **No TTS is started** — the VoiceDesign
    seed render is Phase 2 (``generate_voice_candidates``). A per-character failure is recorded; only a
    *fatal* error (no script) raises.
    """
    overrides = overrides or {}

    script = _load_script(handle, script_name, speakers)
    samples, order = _collect_samples(script)
    handle.log(f"检测到 {len(order)} 个角色：{'、'.join(order)}")

    cfg = get_config()
    llm = cfg.llm
    pp = cfg.persona_prompts
    default_system, default_user = load_persona_prompts()  # mtime-cached: file edits apply without restart
    persona_system = pp.system_prompt or default_system
    persona_user = pp.user_prompt or default_user
    if not (llm.model_name or "").strip() and not overrides:
        handle.log("警告：未配置 LLM 模型——未提供提示词的角色将使用兜底描述。", "WARNING")

    layout = get_or_prepare_layout()
    foundation_lock = layout.temp / "tasks" / "foundation-publication.lock"
    with exclusive_file_lock(foundation_lock):
        vc_path, voice_config = _load_voice_config(handle)

    # Characters to (re)generate a foundation for: everyone, or (new_only) only those
    # without a foundation yet; an explicit ``speakers`` allowlist narrows it further.
    selected = [s for s in order if not (new_only and _has_foundation(voice_config.get(s)))]
    if speakers:
        allow = {s for s in speakers if s}
        selected = [s for s in selected if s in allow]
    if not selected:
        handle.log("没有需要生成基础的角色。")
        return {"count": 0, "aliases": 0, "speakers": order,
                "voice_config_path": str(vc_path), "results": []}

    handle.log(f"本次为 {len(selected)} 个角色生成语音推理基础（仅 LLM，不启动 TTS）。")

    unique_speakers = list(selected)

    n = len(unique_speakers)
    # Bound by the host-wide LLM limit (the same admin setting every LLM task shares),
    # not a per-feature knob; each call additionally takes a process gate permit below.
    max_workers = parse_worker_concurrency()
    handle.log(f"LLM 并发 {max_workers}（全机 LLM 并发上限）。")
    results = []
    done = 0

    def gen_one(sp):
        # Generate the foundation and charge its accepted LLM output. The coordinator
        # alone updates the shared voice_config, so worker results cannot race on it.
        handle.log(f"[{sp}] 开始生成语音推理基础（LLM 推理）…")
        pairs = samples.get(sp, [])
        lines = pairs.texts() if hasattr(pairs, "texts") else [t for _i, t in pairs]  # texts only, for ref-text selection / fallback
        bands = _select_target_bands(pairs)
        # Description + ref text: an override wins, else the LLM, else a fallback.
        # ``gender`` (male/female/"") is a PRE-FILL only — the user's badge pick is the
        # source of truth, so an existing value is never overwritten (the coordinator
        # applies it only when the entry has none yet).
        description = (overrides.get(sp) or "").strip()
        ref_text = ""
        gender = ""
        if description:
            handle.log(f"  [{sp}] 使用自定义提示词：{description[:60]}")
            ref_text = pick_ref_text(lines)
            gender = _gender_from_description(description)
        else:
            if not gate().acquire(stop_check=lambda: handle.cancelled):
                raise TaskCancelled()  # cancelled while queued — no permit taken
            try:
                try:
                    description, ref_text, gender = _llm_persona(
                        handle, llm, persona_system, persona_user, sp, script, bands,
                    )
                finally:
                    gate().release()
            except QuotaInsufficientError:
                raise
            except Exception as e:  # noqa: BLE001
                from .llm_transport import LLMUnavailableError
                if isinstance(e, LLMUnavailableError):
                    raise
                handle.log(f"  [{sp}] LLM 生成描述失败：{e}（改用兜底）", "WARNING")
                description, ref_text, gender = "", "", ""
        if not description:
            description, ref_text, _g = _fallback_persona(sp, lines)
            handle.log(f"  [{sp}] 使用兜底描述。", "WARNING")
            gender = gender or _g
        if not ref_text:
            ref_text = pick_ref_text(lines) or _fallback_ref_text(sp)
        return {
            "speaker": sp,
            "ok": bool(description),
            "type": "foundation",
            "description": description,
            "ref_text": ref_text,
            "gender": gender,
            "foundation_status": "done" if description else "failed",
        }

    def persist(result=None):
        # Completed foundations are durable checkpoints, including on cancellation.
        # Only merge this completion into the latest file: another character's
        # task may have published since this task loaded its initial snapshot.
        # A paused task may park indefinitely. Never park while holding the
        # publication lock needed by the other characters in this project.
        handle.check()
        with exclusive_file_lock(foundation_lock):
            if getattr(handle, "cancelled", False):
                raise TaskCancelled()
            check_bound_inputs()
            _, latest = _load_voice_config(handle)
            # Alias hints are display metadata. Do not rewrite
            # them from this task's snapshot or restore a concurrently removed link.
            if result is not None:
                entry = latest.setdefault(result["speaker"], {})
                entry.update({key: result[key] for key in (
                    "type", "description", "ref_text", "foundation_status")})
                entry.setdefault("seed", -1)
                if result.get("gender") and not entry.get("gender"):
                    entry["gender"] = result["gender"]
                # Invalidation is part of the checkpoint, so a failed sibling task
                # must never restore a shared manifest or stale merged output.
                invalidate_speaker_outputs([result["speaker"]], layout)
            vc_path.parent.mkdir(parents=True, exist_ok=True)
            pending = vc_path.with_name(f".{vc_path.name}.{uuid.uuid4().hex}.tmp")
            try:
                pending.write_bytes(json.dumps(latest, indent=2, ensure_ascii=False).encode("utf-8"))
                from ..core.workspace_epochs import managed_mutation
                with managed_mutation(vc_path):
                    os.replace(pending, vc_path)
            finally:
                pending.unlink(missing_ok=True)
            voice_config.clear()
            voice_config.update(latest)

    handle.progress(0.02, "启动 LLM（并行）")
    ex = ThreadPoolExecutor(max_workers=max_workers)
    futs = {ex.submit(copy_context().run, gen_one, sp): sp for sp in unique_speakers}
    cancelled = False
    quota_failed = False
    try:
        for fut in as_completed(futs):
            sp = futs[fut]
            try:
                r = fut.result()
            except QuotaInsufficientError:
                quota_failed = True
                raise
            except Exception as e:
                from .llm_transport import LLMUnavailableError
                if isinstance(e, LLMUnavailableError):
                    quota_failed = True
                    raise
                handle.log(f"  {sp} 生成基础失败：{e}", "ERROR")
                r = {"speaker": sp, "ok": False, "type": "foundation", "description": "",
                     "ref_text": "", "foundation_status": "failed"}
            persist(r)
            results.append({"speaker": r["speaker"], "ok": r["ok"], "type": r["type"],
                            "description": r["description"]})
            done += 1
            if r["ok"]:
                handle.log(f"  ✓ [{done}/{n}] {sp} 语音推理基础完成。")
            else:
                handle.log(f"  ✗ [{done}/{n}] {sp} 语音推理基础失败（无可用描述）。", "ERROR")
            handle.progress(0.02 + 0.98 * (done / (n or 1)), f"[{done}/{n}] 语音推理基础：{sp}")
            handle.check()  # cooperative cancel between completions
    except BaseException:
        cancelled = True
        raise
    finally:
        # On cancel, don't wait for in-flight (uninterruptible) LLM calls — drop them.
        if cancelled:
            ex.shutdown(wait=quota_failed, cancel_futures=True)
        else:
            ex.shutdown(wait=True)

    # Final persist also covers the early-cancel / no-completion case.
    persist()
    handle.log(f"voice_config 已保存：{vc_path}")

    handle.progress(1.0, "完成")
    handle.log(f"语音推理基础生成完成：{len(unique_speakers)} 个角色。角色关联提示可在角色列表查看。")
    return {
        "count": len(unique_speakers),
        "aliases": 0,
        "hints_deferred": True,
        "speakers": order,
        "voice_config_path": str(vc_path),
        "results": results,
    }


def generate_voice_candidates(handle, speakers=None, new_only=False, concurrency=None, script_name=None,
                candidate_count: int | None = None) -> dict:
    """Phase 2 (TTS only): render each foundation-bearing character's clone candidates.

    The first argument is the durable task context. Reads back the foundations persisted by Phase 1
    and, for every in-scope character that has a foundation, renders its clone
    *candidate* seed WAVs — all candidates in ONE long-lived shared ``.venv`` subprocess
    (the worker's ``design-batch`` mode: the VoiceDesign model loads once, the candidates
    run as native tensor sub-batches, each candidate seeded by its sub-batch) — stored as
    the entry's ``candidates`` list. The entry's top-level ``ref_audio`` always mirrors
    the active candidate (the first one until the user picks another via the select
    endpoint), so downstream synthesis is unaffected. A partial failure still yields a
    usable clone; a total failure falls back to a ``design`` voice and keeps the
    last-known ``ref_audio``.

    ``candidate_count`` is the fixed per-character candidate count (None = auto: the
    :func:`auto_candidate_count` ladder — absolute log-scale bands on each character's
    own line count, so the 旁白's 10×+ line count can't demote the leads); ``concurrency`` is the *per-batch ceiling* — the most
    candidates that may share one tensor batch (omitted → ``config.tts.batch_concurrency``;
    clamped to [1, 64]; the worker sets the actual size at runtime from the length bands
    and a measured VRAM governor); ``new_only`` limits the run to characters whose
    candidate count falls short of this run's target; ``speakers`` restricts to an
    allowlist (single-character remake). A hung / OOM-killed child (worker watchdog,
    exit 124) is not fatal: the run shrinks the ceiling (halving, floor 1) and restarts
    a fresh subprocess that adopts the candidates already rendered on disk (seed -1);
    at ceiling 1 a repeat timeout strikes the in-flight candidate, two strikes isolate
    it as a recorded failure. On cancel the worker's process tree is killed so GPU
    memory frees at once. **No LLM is used.**
    """
    script = _load_script(handle, script_name, speakers)
    samples, order = _collect_samples(script)
    handle.log(f"检测到 {len(order)} 个角色：{'、'.join(order)}")

    vc_path, voice_config = _load_voice_config(handle)
    layout = get_or_prepare_layout()
    ws = layout.workspace

    # Every role with a foundation can make its own voice, including hinted roles.
    selected = [s for s in order if _has_foundation(voice_config.get(s))]
    no_foundation = [s for s in order if not _has_foundation(voice_config.get(s))]
    if no_foundation:
        handle.log(f"提示：{len(no_foundation)} 个角色尚无语音推理基础，本次跳过——请先运行阶段 1。", "WARNING")
    # Per-character candidate target: the fixed count, or the AUTO ladder — absolute
    # log-scale bands on each character's own line count. No project-wide denominator: a 20k-line 旁白 must not demote a
    # 2k-line lead (the old lines/max-lines ratio did exactly that). The new_only /
    # speakers filters below can't shift the budget, so a single-character remake lands
    # on the same budget as a full run.
    eff = _effective_line_counts(order, samples, voice_config)

    def _target(sp):
        return candidate_count if candidate_count else auto_candidate_count(eff.get(sp, 0))

    if new_only:
        selected = [s for s in selected if _clone_have(voice_config.get(s)) < _target(s)]
    if speakers:
        allow = {s for s in speakers if s}
        selected = [s for s in selected if s in allow]
    if not selected:
        handle.log("没有可制作克隆音频的角色（请先运行阶段 1 生成语音推理基础）。", "WARNING")
        return {"count": 0, "ok": 0, "failed": 0, "speakers": order,
                "voice_config_path": str(vc_path),
                "output_dir": str(get_or_prepare_layout().voice_profiles / "designed_voices"),
                "results": []}

    n = len(selected)
    total = sum(_target(sp) for sp in selected)

    cfg = get_config()
    t = cfg.tts
    # 批内行数上限（仅上限）: the request's value, else the persisted default
    # (config.tts.batch_concurrency); clamped to [1, 64] so a stray value can't spawn a
    # degenerate / unbounded cap (the worker sets the actual per-batch size at runtime).
    rows_cap = min(64, clamp_concurrency(concurrency if concurrency else t.batch_concurrency))
    mode = "自动" if not candidate_count else f"固定 {candidate_count}"
    handle.log(f"本次为 {n} 个角色制作 {total} 段候选克隆音频（批内行数上限 {rows_cap}，备选数{mode}；"
               f"请确保已关闭 LLM 以释放显存）。")
    handle.log(f"备选计划：{'、'.join(f'{sp}={_target(sp)}' for sp in selected)}（{mode}）")

    # One namespace per character (keeps two characters' _sanitize names from colliding)
    # and one random seed base per run — candidate k sits in the sub-batch seeded
    # base + 子批序号 (rows of one sub-batch share the seed), so candidates differ from
    # each other and repeat runs differ from each other. The namespace is stable across
    # this run's watchdog restarts, which is what makes breakpoint adoption possible.
    namespace = time.time_ns()
    ns_map = {sp: namespace + index for index, sp in enumerate(selected)}
    base_seed = secrets.randbelow(2 ** 31)

    # One job per (character, candidate k): the character's short ref text + its voice
    # description, rendered to a per-candidate WAV. description / ref_text are read once
    # per character (the two phases are mutually exclusive, so the entry can't change
    # mid-run).
    jobs = []
    for sp in selected:
        entry = voice_config.get(sp, {})
        description = (entry.get("description") or "").strip()
        ref_text = (entry.get("ref_text") or "").strip()
        if not ref_text:
            ref_text = pick_ref_text([t for _i, t in samples.get(sp, [])]) \
                or _fallback_ref_text(sp)
        for k in range(1, _target(sp) + 1):
            final_out = layout.voice_profiles / "designed_voices" / safe_filename(f"{_sanitize(sp)}_{ns_map[sp]}_c{k}.wav")
            allocate_workspace_stage = getattr(handle, "allocate_workspace_stage", None)
            staged_out = allocate_workspace_stage(final_out) if callable(allocate_workspace_stage) else final_out
            job = {"sp": sp, "k": k, "description": description, "ref_text": ref_text, "out": str(staged_out)}
            if callable(allocate_workspace_stage):
                job["final_out"] = str(final_out)
            jobs.append(job)

    results = []
    ok = failed = 0
    job_results: dict = {}   # (sp, k) -> result (this run)
    pending: dict = {sp: {} for sp in selected}  # sp -> {k: result}; a character settles when full
    settled: set = set()

    def persist():
        # Single-writer persist: the whole file is written the moment a character's full
        # candidate set settles, so the 角色配音 list (status / preview / candidates)
        # refreshes in real time.
        data = json.dumps(voice_config, indent=2, ensure_ascii=False).encode("utf-8")
        stage_workspace_file = getattr(handle, "publish_workspace_bytes", None) or getattr(handle, "stage_workspace_file", None)
        if callable(stage_workspace_file):
            stage_workspace_file(vc_path, data)
        else:
            vc_path.parent.mkdir(parents=True, exist_ok=True)
            vc_path.write_bytes(data)

    def _settle(sp):
        # All of this character's candidates have settled: apply them as ONE unit on the
        # single task thread (characters still in flight keep their pre-run entry).
        nonlocal ok, failed
        check_bound_inputs()
        speaker_cancelled = getattr(handle, "speaker_cancelled", None)
        if callable(speaker_cancelled) and speaker_cancelled(sp):
            discard_stage = getattr(handle, "discard_workspace_stage", None)
            if callable(discard_stage):
                for job in jobs:
                    if job["sp"] == sp and job.get("final_out"):
                        discard_stage(Path(job["out"]))
            settled.add(sp)
            return
        num = f"[{len(settled) + 1}/{n}]"
        entry = voice_config.get(sp, {})
        good = sorted((r for r in pending[sp].values() if r["ok"]), key=lambda r: r["k"])
        if good:
            jobs_by_candidate = {(job["sp"], job["k"]): job for job in jobs}
            publish_workspace_stage = getattr(handle, "publish_workspace_stage", None)
            if callable(publish_workspace_stage) and all(jobs_by_candidate[(sp, r["k"])].get("final_out") for r in good):
                for result in good:
                    job = jobs_by_candidate[(sp, result["k"])]
                    staged_out = Path(job["out"])
                    final_out = Path(job["final_out"])
                    if staged_out.exists():
                        publish_workspace_stage(final_out, staged_out)
                    result["preview"] = str(final_out)
            cands = [{"id": str(i),
                      "ref_audio": pathio.to_workspace_relative(r["preview"], ws) or r["preview"],
                      "seed": r["seed"]}
                     for i, r in enumerate(good, 1)]
            # The top-level ref_audio mirrors the ACTIVE candidate (the first one until
            # the user picks another): workspace-relative (the worker resolves it against
            # the job's absolute out path) so the config survives the workspace folder
            # moving; an out-of-workspace file would keep its absolute path.
            entry.update({
                "type": "clone",
                "ref_audio": cands[0]["ref_audio"],
                "candidates": cands,
                # A re-render replaces the whole set, so any earlier pick is void.
                "selected_audio_id": None,
                "ref_text": good[0]["ref_text"],
                "description": good[0]["description"],
                "character_style": good[0]["description"],
                "clone_status": "done",
                "seed": entry.get("seed", -1),
            })
            voice_config[sp] = entry
            persist()
            invalidate_speaker_outputs([sp], layout,
                                       handle=None if getattr(handle, "batch_checkpoints", False) else handle)
            results.append({"speaker": sp, "ok": True, "type": "clone",
                            "preview": good[0]["preview"], "candidates": len(good)})
            ok += 1
            handle.log(f"  ✓ {num} {sp} 候选克隆音频就绪（{len(good)}/{_target(sp)}）。")
        else:
            # Total failure: fall back to a design voice, clear the (absent)
            # candidates, and keep the last-known ref_audio so the old take stays
            # audible in the preview column.
            entry.update({"type": "design", "candidates": [], "selected_audio_id": None,
                          "clone_status": "failed"})
            voice_config[sp] = entry
            persist()
            invalidate_speaker_outputs([sp], layout,
                                       handle=None if getattr(handle, "batch_checkpoints", False) else handle)
            first_reason = next((r["reason"] for r in pending[sp].values() if not r["ok"]), "")
            results.append({"speaker": sp, "ok": False, "type": "design", "preview": "",
                            "candidates": 0, "reason": first_reason})
            failed += 1
            handle.log(f"  ✗ {num} {sp} 候选全部生成失败（{first_reason}），改用 design 兜底。", "ERROR")
        discard_workspace_stage = getattr(handle, "discard_workspace_stage", None)
        if callable(discard_workspace_stage):
            for job in jobs:
                if job["sp"] == sp and job.get("final_out"):
                    discard_workspace_stage(Path(job["out"]))
        settled.add(sp)
        speaker_settled = getattr(handle, "speaker_settled", None)
        if callable(speaker_settled):
            speaker_settled(results[-1])

    # Breakpoint adoption: a candidate WAV already on disk (rendered by an earlier
    # attempt of THIS run, before a watchdog restart) counts as done — its seed is
    # recorded as -1 (the sub-batch seed is unrecoverable) — instead of re-rendering.
    for job in jobs:
        p = job["out"]
        if os.path.exists(p) and os.path.getsize(p) >= 1024:
            r = {"sp": job["sp"], "k": job["k"], "ok": True, "type": "clone",
                 "preview": p, "seed": -1, "description": job["description"],
                 "ref_text": job["ref_text"], "clone_status": "done"}
            job_results[(job["sp"], job["k"])] = r
            pending[job["sp"]][job["k"]] = r
    if job_results:
        handle.log(f"断点采纳：{len(job_results)} 个候选已在盘上（本次运行先前产物，不重渲染）")
    # Characters whose whole set is already adopted settle now (before the engine runs).
    for sp in selected:
        if sp not in settled and len(pending[sp]) >= _target(sp):
            _settle(sp)

    to_render = [j for j in jobs if (j["sp"], j["k"]) not in job_results]
    if not to_render:
        # Everything was adopted (a complete prior attempt): settle and stop without
        # spawning the engine (no wasted model load).
        persist()
        handle.log(f"克隆音频制作完成：成功 {ok} / 失败 {failed} / 共 {n} 个角色（全部断点采纳，未启动引擎）。")
        handle.progress(1.0, "完成")
        return {
            "count": n,
            "ok": ok,
            "failed": failed,
            "speakers": order,
            "voice_config_path": str(vc_path),
            "output_dir": str(get_or_prepare_layout().voice_profiles / "designed_voices"),
            "results": results,
        }

    from ..platform.quota import QuotaInsufficientError, reserve_tts_quota
    if not reserve_tts_quota(sum(len(job.get("ref_text") or "") for job in to_render), "voices.clone"):
        raise QuotaInsufficientError("TTS 输入字数超过可用额度")

    # One subprocess per attempt: a fresh ``design-batch`` run (the model loads once per
    # attempt) whose candidates settle through [design] lines; a watchdog restart adopts
    # whatever already landed on disk.
    job_file = layout.temp / f"design_jobs_{uuid.uuid4().hex[:12]}.json"
    out_dir = layout.voice_profiles / "designed_voices"
    out_dir.mkdir(parents=True, exist_ok=True)

    python, worker = resolve_engine()
    handle.log(f"引擎：.venv（一次性子进程，模型只加载一次）· 批内上限 {rows_cap} 行")
    # A persistent per-run transcript: selected events persist in task history; raw child output is also mirrored to this file (one per run,
    # appended per restart attempt) — a run that dies mid-batch leaves its exact
    # batch / watchdog / error trail on disk for diagnosis.
    run_log = layout.logs / f"tts_clone_{time.strftime('%Y%m%d_%H%M%S')}.log"
    handle.log(f"运行日志（排障用，含每次重启的完整引擎输出）：{run_log}")

    by_pos: dict = {}         # this attempt's row position -> job (rebuilt every attempt)
    in_flight: set = set()    # row indices the current child was generating ([watchdog] line)
    excluded: set = set()     # (sp, k) isolated after two strikes
    struck: dict = {}

    def _handle_design(line: str) -> None:
        # ``[design] <index> ok <seed> <path>`` / ``[design] <index> error <reason>`` —
        # index is the row's position in this attempt's jobs file.
        parts = line[len("[design]"):].split(None, 3)
        if len(parts) < 3:
            handle.log(line, "WARNING")
            return
        try:
            index = int(parts[0])
        except ValueError:
            handle.log(line, "WARNING")
            return
        job = by_pos.get(index)
        if job is None:
            handle.log(line, "WARNING")
            return
        sp, k = job["sp"], job["k"]
        if parts[1] == "ok":
            if len(parts) < 4:
                handle.log(line, "WARNING")
                return
            from ..platform.quota import consume_tts_input
            consume_tts_input(len(job.get("ref_text") or ""), "voices.clone", f"{sp}:{k}")
            seed = _seed_of(parts[2])
            r = {"sp": sp, "k": k, "ok": True, "type": "clone", "preview": parts[3],
                 "seed": seed, "description": job["description"], "ref_text": job["ref_text"],
                 "clone_status": "done"}
            handle.log(f"  [{sp}] 候选 {k} 就绪（seed {seed}）")
        else:
            r = {"sp": sp, "k": k, "ok": False, "type": "design", "preview": "",
                 "reason": parts[2], "seed": -1, "description": job["description"],
                 "ref_text": job["ref_text"], "clone_status": "failed"}
            handle.log(f"  [{sp}] 候选 {k} 渲染失败（{parts[2]}）", "ERROR")
        job_results[(sp, k)] = r
        pending[sp][k] = r
        speaker_progress = getattr(handle, "speaker_progress", None)
        if callable(speaker_progress):
            speaker_progress(sp, len(pending[sp]), _target(sp))
        if sp not in settled and len(pending[sp]) >= _target(sp):
            _settle(sp)

    def on_line(line: str) -> None:
        if line.startswith("[design]"):
            _handle_design(line)
        elif line.startswith("[watchdog]"):
            # The child names the sub-batch that hung before it exits 124: remember the
            # in-flight row indices so a strike at ceiling 1 targets the right candidate(s).
            in_flight.update(_parse_watchdog_indices(line))
            handle.log(line, "WARNING")
        else:
            handle.log(line)

    # -- the watchdog / restart loop ----------------------------------------------
    # A hung or OOM-killed child (exit 124) is not a fatal task failure: shrink the
    # per-batch ceiling and restart a fresh subprocess (adoption skips the already-rendered,
    # a re-run covers the not-yet-settled), down to ceiling 1, where a repeat timeout
    # strikes the in-flight candidate; two strikes isolate it as a recorded failure (the
    # run continues without it). Candidates of an already-settled character are never
    # re-run (its entry is final) — that keeps re-renders from orphaning the file on disk.
    MAX_ATTEMPTS = 8
    attempt = 0
    while True:
        if attempt > MAX_ATTEMPTS:
            raise RuntimeError(
                f"角色克隆引擎反复超时（{MAX_ATTEMPTS} 次缩批重试后仍未完成）——已完成进度已保住，"
                f"请调小「批内行数」后重试。"
            )
        remaining = [j for j in to_render
                     if (j["sp"], j["k"]) not in excluded
                     and j["sp"] not in settled
                     and not (job_results.get((j["sp"], j["k"])) or {}).get("ok")]
        if not remaining:
            break
        by_pos = {i: j for i, j in enumerate(remaining)}
        # The jobs file rows carry their in-file position as ``index`` (the protocol lines
        # and the watchdog's indices= both key on it).
        rows = [{"index": i, "sp": j["sp"], "k": j["k"], "text": j["ref_text"],
                 "description": j["description"], "out": j["out"]}
                for i, j in enumerate(remaining)]
        job_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        # offset = candidates settled (ok or isolated) before this attempt — the worker's
        # progress floor, so a restart reports the settled level, never dips back to zero.
        offset = total - len(remaining)
        cmd = [
            str(python), str(worker),
            "--mode", "design-batch",
            "--segments-file", str(job_file),
            "--out-dir", str(out_dir),
            "--concurrency", str(rows_cap),
            "--seed", str(base_seed),
            "--done-offset", str(offset),
            "--total", str(total),
            "--language", t.language or "chinese",
            "--device", t.device or "auto",
        ]
        if t.design_model:
            cmd += ["--design-model", t.design_model]
        if cfg.ffmpeg.ffmpeg_path:
            cmd += ["--ffmpeg", cfg.ffmpeg.ffmpeg_path]
        cmd += ["--vocoder-batch-size", "8"]

        in_flight.clear()  # a fresh child starts with an empty in-flight set
        try:
            from ..platform.quota import require_quota
            require_quota("TTS", "voices.clone")
            run_tts_subprocess(cmd, handle, on_line, temp_files=(job_file,),
                       fail_prefix="角色克隆引擎", watchdog_code=124, log_file=run_log,
                       interrupt_on_pause=True)
            break  # a clean exit (0)
        except WorkerWatchdogTimeout:
            attempt += 1
            if rows_cap > 1:
                rows_cap = max(1, rows_cap // 2)
                handle.log(f"看门狗触发（批内行超时）→ 批内上限缩到 {rows_cap} 行，重启引擎", "WARNING")
                continue
            # rows_cap == 1: strike the in-flight candidates; two strikes isolate a poison
            # candidate (its character settles without it).
            newly = []
            for i in list(in_flight):
                job = by_pos.get(i)
                if job is None:
                    continue
                sk = (job["sp"], job["k"])
                struck[sk] = struck.get(sk, 0) + 1
                if struck[sk] >= 2:
                    excluded.add(sk)
                    r = {"sp": job["sp"], "k": job["k"], "ok": False, "type": "design",
                         "preview": "", "reason": "超时（已隔离）", "seed": -1,
                         "description": job["description"], "ref_text": job["ref_text"],
                         "clone_status": "failed"}
                    job_results[sk] = r
                    pending[job["sp"]][job["k"]] = r
                    newly.append(sk)
            if newly:
                names = "、".join(f"{sp} 候选 {k}" for sp, k in sorted(newly))
                handle.log(f"{names} 连续两次超时 → 隔离为失败，其余候选继续", "WARNING")
                for sp, _k in newly:
                    if sp not in settled and len(pending[sp]) >= _target(sp):
                        _settle(sp)
            else:
                handle.log("看门狗触发（单候选超时，首次记罚）→ 重启引擎重试", "WARNING")
            continue

    # Safety net: settle any character whose set never completed (e.g. the engine exited
    # before reporting every row) — the same per-character accounting as the [design] path.
    for sp in selected:
        if sp not in settled and pending[sp]:
            _settle(sp)

    # Belt-and-braces re-write after the normal loop (each settlement already persisted).
    persist()
    handle.log(f"voice_config 已保存：{vc_path}")

    handle.progress(1.0, "完成")
    handle.log(f"克隆音频制作完成：成功 {ok} / 失败 {failed} / 共 {n} 个角色。")
    return {
        "count": n,
        "ok": ok,
        "failed": failed,
        "speakers": order,
        "voice_config_path": str(vc_path),
        "output_dir": str(get_or_prepare_layout().voice_profiles / "designed_voices"),
        "results": results,
    }
