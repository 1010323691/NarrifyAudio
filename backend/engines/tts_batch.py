"""Batch TTS engine — synthesize every script line (subprocess orchestrator).

One long-running Task drives the worker's ``batch`` mode: a single shared ``.venv``
subprocess loads the needed model(s) once and synthesizes all segments in JSON
order, so a full book runs in one process (models loaded once) while the 3.14
backend still never imports torch. The child's stdout is pumped line-by-line into
the task's progress/log — the real-time "第 i/N 段 · 角色：X · 正在生成" stream — and
each per-segment ``[segment]`` line is recorded, so a single failure never freezes
the run. A manifest of what was produced is written for the Merge stage. The task
log is SSE-only (it vanishes with the session), so the child's full transcript is
also mirrored to ``<workspace>/logs/tts_batch_<timestamp>.log`` — the persistent
forensic trail for a run that dies mid-batch.

``synthesize`` is a Task worker (first arg is a :class:`TaskHandle`), mirroring
``engines/tts.py``: it streams progress/log, honours cooperative cancel (killing
the child), and marks the task FAILED only on a *fatal* error (no segments, engine
down, or every segment failed) — a per-segment failure is a recorded, non-fatal line.
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..core import pathio
from ..core.config import get_config
from ..core.paths import get_layout, resolve_parsed_json
from ..core.tasks import TaskCancelled
from .tts import DEFAULT_LANGUAGE, DEFAULT_MODEL, WorkerWatchdogTimeout, resolve_engine, run_worker

IMPLEMENTED = True

# 一键合成「批内段数」上限的上下界（前端输入与后端钳制共用）。这只是上限：worker 运行时按
# 长度排序后按上限切批；仅超时触发临时减半及后续恢复。
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 128
# The automatic curve's global ceiling is the measured 5-char anchor; it does not
# extrapolate beyond the recorded range.
AUTO_MAX_CONCURRENCY = 340

# 增量 manifest 的落盘节流：内存态逐条更新，整份 JSON 重写最多每 2 秒一次（取消 / 引擎失败 /
# 看门狗重启 / 收尾仍强制落盘）。每行都整份重写 1MB 会在磁盘 / 杀软扫描负载下拖住行处理主
# 循环（任务日志与进度条逐行蠕动，而 worker 实际在全速张量批）。
MANIFEST_FLUSH_INTERVAL = 2.0

# 进度指标（已合成/总段数 · 已合成/总字数 → 合成页「开始音频合成」按钮下方）的 SSE 推送节流：
# 每段完成都触发一次上报，但推送最多每 1 秒一次——一个子批的段行往往同秒内成串到达，而每条
# 事件都携带全量累计值，节流不丢信息（启动时与收尾各强制推一次；终态快照/结果事件带最终值兜底）。
STATS_FLUSH_INTERVAL = 1.0


def _canonical_voice_name(speaker: str, voice_config: dict) -> str:
    """Resolve the same alias chain that the TTS worker uses."""
    name = (speaker or "").strip()
    seen = set()
    for _ in range(8):
        if not name or name in seen:
            break
        seen.add(name)
        entry = voice_config.get(name) or {}
        alias = entry.get("alias_of") or entry.get("alias")
        if not isinstance(alias, str) or not alias.strip() or alias == name:
            break
        name = alias.strip()
    return name


def _canonical_voice_path(value) -> str:
    """Normalize an in-workspace voice path before hashing it.

    ``voice_config.json`` is lazily migrated from absolute paths to workspace-relative
    paths. The representation change must not look like a new voice after a restart or
    workspace move, while the path below ``04_voice_profiles`` must remain part of the
    identity so two different reference recordings do not collide.
    """
    if not isinstance(value, str):
        return ""
    path = value.strip().replace("\\", "/")
    marker = "/04_voice_profiles/"
    folded = path.casefold()
    marker_at = folded.find(marker)
    if marker_at >= 0:
        path = "04_voice_profiles/" + path[marker_at + len(marker):]
    while path.startswith("./"):
        path = path[2:]
    return path.casefold() if os.name == "nt" else path


def voice_params(speaker: str, voice_config: dict) -> dict:
    """Return the effective JSON voice parameters used for one script speaker.

    This object is persisted in each successful manifest entry as ``voice_used``.  It is
    intentionally made from synthesis inputs only; UI-only state such as the gender badge
    and clone progress markers must not invalidate already rendered speech.
    """
    canonical = _canonical_voice_name(speaker, voice_config)
    entry = voice_config.get(canonical) or {}
    return {
        "canonical": canonical,
        "type": entry.get("type", ""),
        "ref_audio": _canonical_voice_path(entry.get("ref_audio", "")),
        "ref_text": entry.get("ref_text", ""),
        "description": entry.get("description", ""),
        "voice": entry.get("voice", ""),
        "instruct": entry.get("instruct", ""),
    }


def voice_signature(speaker: str, voice_config: dict) -> str:
    """Return the legacy stable hash for the effective voice parameters.

    New manifests persist :func:`voice_params` itself.  The hash remains in manifests for
    compatibility with older tooling and for migrating old tests/projects.
    """
    payload = voice_params(speaker, voice_config)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def segment_voice_params(segments, voice_config: dict) -> dict[int, dict]:
    """Map each segment index to the exact effective voice JSON for that segment."""
    return {s["index"]: voice_params(s.get("speaker", ""), voice_config)
            for s in segments}


def segment_voice_signatures(segments, voice_config: dict) -> dict[int, str]:
    """Map each segment index to the effective voice signature for that segment."""
    return {s["index"]: voice_signature(s.get("speaker", ""), voice_config)
            for s in segments}


def _voice_signature_matches(entry: dict, expected: str | None) -> bool:
    """Check that a manifest entry was rendered with the current voice inputs.

    Once a workspace has a voice configuration, an entry without a signature is
    deliberately stale. Treating it as complete would let a process restart
    resurrect legacy progress and silently reuse audio rendered before a voice edit.
    ``expected is None`` remains the compatibility path for projects without any
    voice configuration.
    """
    return expected is None or entry.get("voice_signature") == expected


def _voice_params_match(entry: dict, expected: dict | None) -> bool:
    """Compare the current target JSON with the voice JSON actually used by the segment."""
    return expected is None or entry.get("voice_used") == expected


def _resolved_existing_path(value, workspace):
    """Resolve a manifest path and return it only when the generated file still exists."""
    if not value:
        return None
    try:
        path = pathio.resolve_path(value, workspace, strict=False) if workspace is not None else Path(value)
    except (OSError, TypeError, pathio.PathOutsideWorkspace, pathio.PathNotFoundError):
        return None
    if path is None:
        return None
    try:
        return path if path.exists() else None
    except OSError:
        return None


def _archive_voice_version(entry: dict, workspace) -> bool:
    """Keep the current generated file under a voice-specific name before invalidating it."""
    path = _resolved_existing_path(entry.get("path"), workspace)
    signature = entry.get("voice_signature") or ""
    used = entry.get("voice_used")
    if path is None or (not signature and (not isinstance(used, dict) or not used)):
        return False
    versions = entry.setdefault("voice_versions", [])
    if not isinstance(versions, list):
        versions = []
        entry["voice_versions"] = versions
    for version in versions:
        if not isinstance(version, dict):
            continue
        same_voice = (
            signature and version.get("voice_signature") == signature
            or isinstance(used, dict) and used and version.get("voice_used") == used
        )
        if same_voice and _resolved_existing_path(version.get("path"), workspace) is not None:
            return False
    token = signature[:16] if signature else hashlib.sha256(
        json.dumps(used, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    archived = path.with_name(f"{path.stem}.voice-{token}{path.suffix}")
    try:
        if not archived.exists():
            shutil.copy2(path, archived)
    except OSError:
        return False
    versions.append({
        "path": _store_path(str(archived), workspace),
        "voice_signature": signature,
        **({"voice_used": used} if isinstance(used, dict) else {}),
    })
    return True


def _restore_cached_voice_versions(entries: dict, expected_voice_params: dict | None,
                                   expected_voice_signatures: dict | None,
                                   workspace) -> int:
    """Restore a previously synthesized voice version into the active manifest entry."""
    if expected_voice_params is None:
        return 0
    restored = 0
    for index, entry in entries.items():
        if not isinstance(entry, dict) or _voice_params_match(entry, expected_voice_params.get(index)):
            continue
        expected_params = expected_voice_params.get(index)
        expected_signature = ((expected_voice_signatures or {}).get(index)
                              if expected_voice_signatures is not None else None)
        versions = entry.get("voice_versions")
        if not isinstance(versions, list):
            continue
        for version in reversed(versions):
            if not isinstance(version, dict):
                continue
            if version.get("voice_used") is not None:
                matches = version.get("voice_used") == expected_params
            else:
                matches = bool(expected_signature) and version.get("voice_signature") == expected_signature
            if not matches:
                continue
            path = _resolved_existing_path(version.get("path"), workspace)
            if path is None:
                continue
            entry["path"] = version["path"]
            entry["ok"] = True
            entry["reason"] = ""
            entry["voice_used"] = expected_params
            if expected_signature is not None:
                entry["voice_signature"] = expected_signature
            restored += 1
            break
    return restored


def _merged_output_paths(layout, package: str):
    """Generated merge outputs for a package (only exact, derived file names)."""
    safe = "".join("_" if c in '\\/:*?"<>|' else c for c in package).strip() or "audiobook"
    return [layout.audio_merge / f"{safe}.mp3", layout.audio_merge / f"{safe}.wav"]


def invalidate_speaker_outputs(speakers, layout=None) -> int:
    """Mark old per-segment audio for changed speakers as stale and drop merged output.

    This is intentionally limited to generated ``05_audio_chunk/<package>`` manifests
    and the exact derived merge files.  Audio files remain on disk for inspection, but
    their manifest entries can no longer satisfy a resume or merge-readiness check.
    """
    layout = layout or get_layout()
    if layout.audio_chunk is None:
        return 0
    names = {str(s).strip() for s in (speakers or []) if str(s).strip()}
    if not names:
        return 0
    changed = 0
    for manifest_path in layout.audio_chunk.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        touched = False
        for entry in data:
            if isinstance(entry, dict) and (entry.get("speaker") or "").strip() in names:
                if entry.get("voice_used") != {} or entry.get("voice_signature") != "":
                    _archive_voice_version(entry, layout.workspace)
                    entry["voice_used"] = {}
                    entry["voice_signature"] = ""
                    touched = True
                    changed += 1
        if touched:
            pathio.rewrite_json_file(manifest_path, data)
            outputs = _merged_output_paths(layout, manifest_path.parent.name)
            if layout.bgm is not None:
                safe = "".join("_" if c in '\\/:*?"<>|' else c
                                for c in manifest_path.parent.name).strip() or "audiobook"
                outputs.append(layout.bgm / f"{safe}.mp3")
            for output in outputs:
                try:
                    output.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
    return changed


def clamp_concurrency(n, *, maximum: int = MAX_CONCURRENCY) -> int:
    """Clamp a requested concurrency to ``[MIN_CONCURRENCY, MAX_CONCURRENCY]``.

    ``None`` / non-integer / out-of-range values collapse to a safe in-range int, so a
    stray config value or request can never spawn a degenerate cap (0) or an unbounded
    one (e.g. 999).
    """
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = MIN_CONCURRENCY
    return max(MIN_CONCURRENCY, min(max(MIN_CONCURRENCY, int(maximum)), v))


def timeout_demotion_cap(current_cap: int, actual_rows: int) -> int:
    """Choose the next cap from the sub-batch that actually timed out.

    The worker's cap is only an upper bound: length/type grouping can produce a
    smaller actual sub-batch.  Demoting from that actual size avoids leaving the
    next retry effectively unchanged (for example, 96 timed-out rows under a
    cap of 340 should demote to 48, not 170).
    """
    cap = max(MIN_CONCURRENCY, int(current_cap))
    rows = int(actual_rows or 0)
    if rows <= 0:
        rows = cap
    return max(MIN_CONCURRENCY, min(cap, rows) // 2)


def encode_restore_stack(stack) -> str:
    """The pending demotion records as the worker's ``--restore-stack`` value (pure).

    ``stack`` is the LIFO list of ``(timeout_chars, cap)`` pairs, OLDEST first — each entry
    is the total chars of the sub-batch that timed out and the per-batch cap in force just
    before that demotion. The worker counts successful batches at the NEWEST (last)
    demoted cap and restores that cap after two successes. ``""`` for an empty
    stack (the flag is then omitted from the command entirely — the exact legacy command).
    """
    return ",".join(f"{chars}:{cap}" for chars, cap in stack)


def filename_width(full_count: int) -> int:
    """Zero-pad width for per-segment MP3 filenames.

    The digits the LARGEST package in a run needs (its FULL segment count), floored at 4.
    The backend precomputes this and hands it to the worker (``--width``) so that every
    package in a run shares ONE digit count, and the width is a function of the full
    segment count — stable across resume runs and watchdog restarts. Deriving the width
    from the pending (remaining) count instead made it shrink as the job completed, so a
    package filled across several runs ended up with mixed ``000x`` / ``0000x`` names.
    """
    return max(4, len(str(max(0, int(full_count)))))


def _build_cmd(python, worker, seg_file, vc_path, out_dir, *, language, device,
               model, base_model, design_model, ffmpeg_path, concurrency, seed,
               workspace=None, restore_stack: str = "",
               width: int = 0, auto_concurrency: bool = False) -> list:
    """Fixed row ceiling and decoder groups, with timeout-demotion recovery records."""
    cmd = [
        str(python), str(worker),
        "--mode", "batch",
        "--segments-file", str(seg_file),
        "--voice-config", str(vc_path),
        "--out-dir", str(out_dir),
        "--language", language or DEFAULT_LANGUAGE,
        "--device", device or "auto",
        "--concurrency", str(concurrency),
        "--seed", str(seed),
    ]
    if workspace:
        cmd += ["--workspace", str(workspace)]
    if model:
        cmd += ["--model", model]
    if base_model:
        cmd += ["--base-model", base_model]
    if design_model:
        cmd += ["--design-model", design_model]
    if ffmpeg_path:
        cmd += ["--ffmpeg", ffmpeg_path]
    if restore_stack:
        cmd += ["--restore-stack", restore_stack]
    if width:
        cmd += ["--width", str(width)]
    if auto_concurrency:
        cmd.append("--auto-batch")
    cmd += ["--vocoder-batch-size", "8"]
    return cmd


def _load_script(p):
    if not p.exists():
        raise RuntimeError("未找到脚本 JSON（03_parsed_json/）——请先在「文本解析」生成脚本。")
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{p.name} 无法解析：{e}")
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"{p.name} 为空——请先生成脚本。")
    return data


class _SegmentLogBuffer:
    """Aggregate per-segment results before sending a task-log entry.

    ``[segment]`` lines are an internal worker protocol and can arrive in large bursts.
    They still update manifests and progress immediately, but the user-facing log gets one
    summary per worker sub-batch instead of one event per completed row.
    """

    def __init__(self, handle, total: int):
        self.handle = handle
        self.total = total
        self.completed = 0
        self.pending_completed = 0
        self.pending_failed = 0
        self.pending_failure_details: list[str] = []

    def add(self, result: dict | None) -> None:
        if not result:
            return
        if result["ok"]:
            self.completed += 1
            self.pending_completed += 1
        else:
            self.pending_failed += 1
            if len(self.pending_failure_details) < 3:
                index = result.get("index")
                label = result.get("label") or (
                    f"第 {index + 1} 段" if isinstance(index, int) else "未知段"
                )
                detail = result.get("detail") or "未知原因"
                self.pending_failure_details.append(f"{label}：{detail}")

    def flush(self) -> None:
        if not self.pending_completed and not self.pending_failed:
            return
        message = (
            f"合成进度：本批新增成功 {self.pending_completed} 段、失败 {self.pending_failed} 段；"
            f"累计完成 {self.completed}/{self.total} 段"
        )
        if self.pending_failure_details:
            extra = "；".join(self.pending_failure_details)
            remaining = self.pending_failed - len(self.pending_failure_details)
            if remaining > 0:
                extra += f"；另有 {remaining} 条失败"
            message += f"；失败示例：{extra}"
        self.handle.log(message, "WARNING" if self.pending_failed else "INFO")
        self.pending_completed = 0
        self.pending_failed = 0
        self.pending_failure_details.clear()


def _handle_segment(line: str, by_index: dict, total: int, seg_results: dict, handle) -> dict | None:
    """Parse a ``[segment] <index> ok|error <detail>`` line into a result."""
    parts = line[len("[segment]"):].split(None, 2)
    if len(parts) < 2:
        handle.log(line, "WARNING")
        return
    try:
        index = int(parts[0])
    except ValueError:
        handle.log(line, "WARNING")
        return
    status = parts[1]
    detail = parts[2] if len(parts) > 2 else ""
    speaker = (by_index.get(index) or {}).get("speaker") or "(未知)"
    if status == "ok":
        seg_results[index] = {"ok": True, "path": detail, "reason": ""}
        return {"ok": True, "index": index, "label": f"[{index + 1}/{total}] {speaker}"}
    else:
        seg_results[index] = {"ok": False, "path": "", "reason": detail}
        return {
            "ok": False,
            "index": index,
            "label": f"[{index + 1}/{total}] {speaker}",
            "detail": detail,
        }


def _format_batch_performance(line: str) -> str | None:
    """Turn a worker ``[perf]`` batch-completion event into a UI log line.

    The worker emits a structured event internally. The task log and the persistent run log
    both receive a concise human-readable line instead, so the per-batch metrics are useful
    during a run and when inspecting the workspace afterwards.
    """
    if not line.startswith("[perf] "):
        return None
    try:
        event = json.loads(line[len("[perf] "):])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(event, dict):
        return None
    if event.get("stage") != "batch" or event.get("event") != "end":
        return None
    try:
        rows = int(event["rows"])
        chars = int(event["chars"])
        seconds = float(event["seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    if rows < 0 or chars < 0 or seconds < 0:
        return None
    throughput = event.get("throughput_chars_per_sec")
    try:
        throughput = float(throughput)
    except (TypeError, ValueError):
        throughput = chars / seconds if seconds > 0 else 0.0
    if throughput < 0:
        return None
    return (f"性能：批内数量 {rows} · 总字数 {chars} · 总耗时 {seconds:.2f} 秒 · "
            f"吞吐量 {throughput:.2f} 字/秒")


def _format_batch_log_line(line: str) -> str | None:
    """Mirror readable worker summaries, not the per-segment protocol, to the run log."""
    if line.startswith("[segment]"):
        return None
    if line.startswith("[perf] "):
        return _format_batch_performance(line)
    return line


def _parse_watchdog_indices(line: str):
    """The in-flight segment indices named in a ``[watchdog] … indices=[…]`` line (a no-op list
    if the marker / list is absent or unparseable). Used to target a strike at workers==1."""
    i = line.find("indices=[")
    if i < 0:
        return []
    j = line.find("]", i)
    if j < 0:
        return []
    out = []
    for tok in line[i + len("indices=["):j].split(","):
        tok = tok.strip()
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return out


def _parse_restore_cap(line: str):
    """The restored per-batch cap named in a ``[restore] cap=<N>`` line (``None`` if the
    marker / value is absent or unparseable — the caller logs the raw line and carries on)."""
    for tok in line.split():
        if tok.startswith("cap="):
            try:
                return int(tok[len("cap="):])
            except ValueError:
                return None
    return None


def _build_segments(script, indices=None):
    """Build the ordered per-line synthesis segments (pure).

    ``index`` is the line's position in the *full* script, so per-segment filenames
    and the merged order always match the JSON. Lines with empty text are skipped;
    ``indices`` (line positions) optionally restricts which lines are included.
    """
    want = set(int(i) for i in indices) if indices else None
    segments = []
    for i, entry in enumerate(script):
        if want is not None and i not in want:
            continue
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        segments.append({
            "index": i,
            "speaker": (entry.get("speaker") or entry.get("type") or "").strip(),
            "text": text,
            "instruct": (entry.get("instruct") or "").strip(),
            "pause_after": entry.get("pause_after"),
        })
    return segments


@dataclass
class _PooledFile:
    """One chapter file's bookkeeping inside a pooled multi-file run.

    ``pending`` / ``by_index`` / ``seg_results`` are keyed by the *chapter-local* index
    (the line's position in this file's JSON); the pool-global index only exists on the
    pool rows and is mapped back here through the run's ``pool_map``. ``error`` is set
    only for prep-stage fatals (missing / corrupt / empty script, package collision) —
    engine-level failures are expressed by the top-level ``failed`` list and this
    package's manifest, never by ``error``.
    """

    name: str
    src: Path | None = None
    pkg: str = ""
    out_dir: Path | None = None
    manifest_path: Path | None = None
    all_segments: list = field(default_factory=list)
    by_index: dict = field(default_factory=dict)
    old_entries: dict = field(default_factory=dict)
    voice_params: dict[int, dict] | None = None
    voice_signatures: dict[int, str] | None = None
    seg_results: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    # Pre-run completion snapshot (the manifest's done set, and its total chars) — the
    # baseline the 进度指标 (已合成/总段数 · 字数) counts on top of this run's completions.
    done_set: set = field(default_factory=set)
    done_count: int = 0
    done_chars: int = 0
    all_count: int = 0
    error: str | None = None
    dirty: bool = False


def _build_pool_rows(files, pool_start: int = 0) -> tuple:
    """Expand the files' pending segments into the unified pool rows (pure).

    Returns ``(pool_rows, pool_owners)`` — parallel lists, one row per pending segment in
    request order (files, then chapter-local index order). Each row is the chapter's
    segment dict plus the pooled-run fields the worker reads: ``index`` = the row's
    pool-global position (its scheduling identity: protocol lines, watchdog, file-number
    width) numbered consecutively from ``pool_start``; ``out_dir`` = the chapter's package
    dir (absolute); ``file_index`` = the line's position inside its chapter (the manifest
    key and the file number). Files with a prep fatal or nothing pending contribute no
    rows. (On a watchdog restart the remaining rows are filtered in place and keep their
    original pool indices — never renumbered.)
    """
    pool_rows: list[dict] = []
    pool_owners: list = []
    for f in files:
        if f.error or not f.pending:
            continue
        for local in f.pending:
            row = dict(f.by_index[local])
            row["index"] = pool_start
            row["out_dir"] = str(f.out_dir)
            row["file_index"] = local
            pool_rows.append(row)
            pool_owners.append(f)
            pool_start += 1
    return pool_rows, pool_owners


def _handle_segment_pool(line: str, pool_map: dict, pool_total: int, handle) -> dict | None:
    """Route a ``[segment] <pool-index> ok|error <detail>`` line to its owning chapter.

    ``pool_index`` is the row's pool-global segment-table position (the worker's scheduling
    identity); ``pool_map`` is the single source mapping it back to ``(file, local index)``
    — the pool index is NEVER used as a chapter-internal index. Malformed lines and unknown
    indices are logged as warnings and dropped, exactly like the single-file
    :func:`_handle_segment`.
    """
    parts = line[len("[segment]"):].split(None, 2)
    if len(parts) < 2:
        handle.log(line, "WARNING")
        return
    try:
        pool_index = int(parts[0])
    except ValueError:
        handle.log(line, "WARNING")
        return
    status = parts[1]
    detail = parts[2] if len(parts) > 2 else ""
    owner = pool_map.get(pool_index)
    if owner is None:
        handle.log(f"未知段索引 {pool_index}（池共 {pool_total} 段）：{line}", "WARNING")
        return
    f, local = owner
    speaker = (f.by_index.get(local) or {}).get("speaker") or "(未知)"
    if status == "ok":
        f.seg_results[local] = {"ok": True, "path": detail, "reason": ""}
        f.dirty = True
        return {
            "ok": True,
            "index": pool_index,
            "label": f"[{pool_index + 1}/{pool_total}] {f.name} · {speaker}",
        }
    else:
        f.seg_results[local] = {"ok": False, "path": "", "reason": detail}
        f.dirty = True
        return {
            "ok": False,
            "index": pool_index,
            "label": f"[{pool_index + 1}/{pool_total}] {f.name} · {speaker}",
            "detail": detail,
        }


def _settle_pool(handle, files) -> dict:
    """Aggregate the pooled run's final result (the same shape the legacy multi run returned).

    ``files`` is in request order and so is the result's ``files`` list; the top-level
    ``failed`` list is ordered file order → chapter-local index order (the worker's
    cross-chapter execution order never leaks into any result or manifest ordering). Per
    file: a prep fatal is all-zeros + ``error``; a no-pending file reports its cumulative
    completion; a pooled file reports ``total = len(pending)`` and the count of this run's
    ok results. The failure test (``attempted`` — files that pooled or errored — with
    ``completed == 0``) mirrors the single-file fatal rule: a selection where every
    actually-pooled segment failed (or every file was a prep fatal) fails the task; an
    all-complete / all-empty / one-chapter-sinks-others-succeed selection succeeds.
    """
    per_file: list[dict] = []
    failed_all: list[dict] = []
    for f in files:
        if f.error:
            per_file.append({
                "script": f.name, "total": 0, "completed": 0, "failed": 0,
                "output_dir": "", "manifest_path": "", "done_count": 0, "all_count": 0,
                "error": f.error,
            })
            continue
        done = count_completion(
            f.all_segments, load_manifest(f.out_dir),
            expected_voice_signatures=f.voice_signatures,
            expected_voice_params=f.voice_params,
        )
        # completed = this run's ok results; a no-pending file reports its cumulative
        # completion (the legacy zero-short-circuit shape).
        completed = (sum(1 for i in f.pending if (f.seg_results.get(i) or {}).get("ok"))
                     if f.pending else done["completed"])
        file_failed = 0
        for local in f.pending:
            r = f.seg_results.get(local)
            if r and r.get("ok"):
                continue
            file_failed += 1
            failed_all.append({
                "index": local,
                "speaker": (f.by_index.get(local) or {}).get("speaker", ""),
                "reason": (r or {}).get("reason") or "（引擎未返回结果）",
                "script": f.name,
            })
        per_file.append({
            "script": f.name,
            # total = this run's pooled rows; a no-pending file reports its whole segment
            # count (the legacy zero-short-circuit shape: all-done / empty files).
            "total": len(f.pending) if f.pending else f.all_count,
            "completed": completed,
            "failed": file_failed,
            "output_dir": str(f.out_dir),
            "manifest_path": str(f.manifest_path),
            "done_count": done["completed"],
            "all_count": done["total"],
            "error": None,
        })
    result = {
        "total": sum(r["total"] for r in per_file),
        "completed": sum(r["completed"] for r in per_file),
        "failed": failed_all,
        "output_dir": "",  # pooled run: each chapter has its own package (see ``files``)
        "manifest_path": "",
        "done_count": sum(r["done_count"] for r in per_file),
        "all_count": sum(r["all_count"] for r in per_file),
        "files": per_file,
    }
    attempted = [f for f in files if f.error or f.pending]
    if not attempted or result["completed"] > 0:
        handle.progress(1.0, "完成")
        return result
    if result["total"] > 0:
        raise RuntimeError(f"全部 {result['total']} 段合成失败（各章节原因见任务日志与结果 files 字段）。")
    raise RuntimeError(f"所选 {len(files)} 个文件全部无法合成（原因见任务日志与结果 files 字段）。")


def package_for(src: Path) -> str:
    """The package (sub-folder in ``05_audio_chunk/``) a source JSON's batch output lands in.

    Named after the source's base stem so 音频合并 can list & pick a package; a base and its
    ``_checked`` variant share the same package.
    """
    stem = src.stem
    if stem.endswith("_checked"):
        stem = stem[: -len("_checked")]
    return stem or "batch"


def _migrate_legacy_voice_used(data: list, layout) -> int:
    """Recover ``voice_used`` for manifests written before the per-segment JSON field.

    A legacy entry is safe to migrate only when its stored signature exactly matches the
    current target parameters.  A mismatched or signature-less entry remains stale and must
    be synthesized again.
    """
    vc_path = layout.voice_profiles / "voice_config.json"
    if not vc_path.exists():
        return 0
    try:
        voice_config = json.loads(vc_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(voice_config, dict):
        return 0
    migrated = 0
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("ok") or "voice_used" in entry:
            continue
        speaker = entry.get("speaker", "")
        expected = voice_signature(speaker, voice_config)
        if entry.get("voice_signature") == expected:
            entry["voice_used"] = voice_params(speaker, voice_config)
            migrated += 1
    return migrated


def load_manifest(out_dir) -> dict:
    """The package's cumulative manifest as ``{index: entry}`` (``{}`` if absent / unreadable).

    One entry per segment the batch has ever reported — the source of truth for what is already
    synthesized, so a cancel (or a later re-run) can resume without re-doing finished work.
    """
    p = Path(out_dir) / "manifest.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt manifest just means "start fresh"
        return {}
    if not isinstance(data, list):
        return {}
    # Lazy migration (single read): legacy manifests stored absolute paths (the workspace's
    # location at write time). Convert any that still point inside the workspace to the
    # relative form and rewrite the file, so the project keeps working after the workspace
    # moves. ``migrate_entries`` + ``rewrite_json_file`` on the already-parsed list is the
    # in-memory form of ``migrate_entries_in`` (which would re-read the same file).
    layout = get_layout()
    n = pathio.migrate_entries(data, layout.workspace, ("path",))
    voice_migrated = _migrate_legacy_voice_used(data, layout)
    by_index = {}
    for e in data:
        if isinstance(e, dict) and "index" in e:
            try:
                by_index[int(e["index"])] = e
            except (TypeError, ValueError):
                pass
    expected_params = {}
    expected_signatures = {}
    vc_path = layout.voice_profiles / "voice_config.json"
    if vc_path.exists():
        try:
            voice_config = json.loads(vc_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            voice_config = None
        if isinstance(voice_config, dict):
            for index, entry in by_index.items():
                if isinstance(entry, dict):
                    speaker = entry.get("speaker", "")
                    expected_params[index] = voice_params(speaker, voice_config)
                    expected_signatures[index] = voice_signature(speaker, voice_config)
    restored = _restore_cached_voice_versions(
        by_index, expected_params or None, expected_signatures or None, layout.workspace,
    )
    if n or voice_migrated or restored:
        pathio.rewrite_json_file(p, data)
    return by_index


def is_done(entry, expected_voice_signature: str | None = None,
            expected_voice_params: dict | None = None) -> bool:
    """Whether a manifest entry is a *completed* segment: ``ok`` AND its file still on disk.

    A missing file (deleted, or a run killed between writing the file and its line being
    drained) means the segment is not truly done, so a resume re-synthesizes it. The path
    value is resolved against the *current* workspace root (relative form; a legacy
    absolute value still works, and one invalidated by a workspace move is recovered
    best-effort) — never against a fixed location.
    """
    if not entry or not entry.get("ok"):
        return False
    if not _voice_signature_matches(entry, expected_voice_signature):
        return False
    if not _voice_params_match(entry, expected_voice_params):
        return False
    path = entry.get("path")
    if not path:
        return False
    ws = get_layout().workspace
    try:
        p = pathio.resolve_path(path, ws, strict=False) if ws is not None else Path(path)
    except (OSError, TypeError, pathio.PathOutsideWorkspace, pathio.PathNotFoundError):
        return False
    if p is None:
        return False
    try:
        return p.exists()
    except (OSError, TypeError):
        return False


def done_indices(old_entries: dict, out_dir, ws,
                 expected_voice_signatures: dict[int, str] | None = None,
                 expected_voice_params: dict[int, dict] | None = None) -> set[int]:
    """The done manifest entries (``ok`` AND the file still on disk) as a set of indices.

    The same judgment as :func:`is_done`, batched for a whole package: entries whose stored
    path lives directly inside ``out_dir`` (the normal relative form
    ``05_audio_chunk/<pkg>/NNNN.mp3``) are answered from ONE lazy directory listing instead
    of one stat per entry; every other shape (legacy absolute, external, ``..``, another
    directory, or no workspace) falls back to the exact per-entry :func:`is_done`, so the
    result is identical entry-for-entry. A missing ``out_dir`` yields ``{}`` (nothing
    exists — the same as per-entry ``exists()`` on a gone directory).
    """
    done: set[int] = set()

    def current(i, entry) -> bool:
        expected = ((expected_voice_signatures or {}).get(i)
                    if expected_voice_signatures is not None else None)
        voice = ((expected_voice_params or {}).get(i)
                 if expected_voice_params is not None else None)
        return is_done(entry, expected, voice)

    rel_out = pathio.to_workspace_relative(str(out_dir), ws) if ws is not None else None
    if rel_out is None:
        for i, e in old_entries.items():
            if current(i, e):
                done.add(i)
        return done
    # NTFS is case-insensitive (exists() folds case); POSIX is not.
    fold = str.casefold if os.name == "nt" else (lambda s: s)
    prefix = rel_out + "/"
    names: set | None = None  # lazy: one os.listdir for the whole package
    for i, e in old_entries.items():
        if not (isinstance(e, dict) and e.get("ok")):
            continue
        expected = ((expected_voice_signatures or {}).get(i)
                    if expected_voice_signatures is not None else None)
        voice = ((expected_voice_params or {}).get(i)
                 if expected_voice_params is not None else None)
        if not _voice_signature_matches(e, expected) or not _voice_params_match(e, voice):
            continue
        v = e.get("path")
        if not isinstance(v, str) or not v.strip():
            continue
        if not pathio._is_abs(v):
            vn = pathio._norm(v)
            if not pathio._escapes(vn) and vn.startswith(prefix):
                base = vn[len(prefix):]
                if base and "/" not in base:
                    if names is None:
                        try:
                            # No is_file filter on purpose: Path.exists() is True for a
                            # directory named like an mp3 too — the set must match it.
                            names = {fold(n) for n in os.listdir(out_dir)}
                        except OSError:
                            names = set()
                    if fold(base) in names:
                        done.add(i)
                    continue
        if current(i, e):  # legacy / external / escaping value: the exact per-entry rule
            done.add(i)
    return done


def plan_to_synthesize(all_indices, done_set, indices=None):
    """Which line indices a run should synthesize (a subset of ``all_indices``).

    An explicit ``indices`` wins (synthesise exactly those, intersected with the valid set);
    otherwise the default is a *resume* — only the not-yet-done lines (``all - done``).
    Re-doing everything is NOT a planning mode: the caller deletes the package folder first
    (``POST /api/tts/batch-reset``), after which an ordinary resume has nothing to skip.
    """
    all_set = set(all_indices)
    if indices:
        return {int(i) for i in indices} & all_set
    return all_set - set(done_set)


def _store_path(path, root):
    """The manifest's on-disk form of an audio path: workspace-relative when the file
    lives inside the workspace (the location-independent form), the value unchanged when
    it does not (an external resource keeps its absolute path)."""
    if not path or root is None:
        return path
    rel = pathio.to_workspace_relative(path, root)
    return path if rel is None else rel


def _preserve_voice_versions(item: dict, old) -> dict:
    """Carry the per-voice audio cache through every incremental manifest rewrite."""
    if isinstance(old, dict) and isinstance(old.get("voice_versions"), list):
        item["voice_versions"] = old["voice_versions"]
    return item


def build_manifest(all_segments, old_entries, run_results, root=None,
                   expected_voice_signatures: dict[int, str] | None = None,
                   expected_voice_params: dict[int, dict] | None = None):
    """Rebuild the package manifest: one entry per non-empty segment, in index order.

    For each segment this run's result wins; else a prior *done* entry is preserved (its existing
    audio path, paired with the current script's speaker/text/pause); else a not-done entry
    (``ok: false``, empty path) reusing a prior failure's reason when there is one. The same
    function powers both the incremental (per-segment) writes and the final write, so the file
    always holds the cumulative state and a cancel never loses finished work.

    ``root`` (the workspace root) gives every stored path the location-independent,
    workspace-relative form; with ``None`` (or for out-of-workspace paths) the value is
    stored as given. Passing the live root also migrates any legacy absolute value a
    preserved old entry still carries.
    """
    manifest = []
    for s in sorted(all_segments, key=lambda x: x["index"]):
        index = s["index"]
        base = {
            "index": index,
            "speaker": s["speaker"],
            "text": s["text"],
            "pause_after": s["pause_after"],
        }
        if expected_voice_signatures is not None:
            base["voice_signature"] = expected_voice_signatures.get(index, "")
        r = run_results.get(index)
        if r is not None:
            if r.get("ok"):
                item = {**base, "path": _store_path(r.get("path", ""), root),
                        "ok": True, "reason": ""}
                if expected_voice_params is not None:
                    item["voice_used"] = expected_voice_params.get(index, {})
                manifest.append(_preserve_voice_versions(item, old_entries.get(index)))
            else:
                item = {**base, "path": "", "ok": False, "reason": r.get("reason", "")}
                old = old_entries.get(index)
                if isinstance(old, dict) and "voice_used" in old:
                    item["voice_used"] = old["voice_used"]
                manifest.append(_preserve_voice_versions(item, old_entries.get(index)))
        else:
            old = old_entries.get(index)
            if old is not None and is_done(
                old,
                expected_voice_signatures.get(index) if expected_voice_signatures is not None else None,
                expected_voice_params.get(index) if expected_voice_params is not None else None,
            ):
                item = {**base, "path": _store_path(old.get("path", ""), root),
                        "ok": True, "reason": ""}
                if "voice_used" in old:
                    item["voice_used"] = old["voice_used"]
                manifest.append(_preserve_voice_versions(item, old))
            elif old is not None:
                item = {**base, "path": "", "ok": False, "reason": old.get("reason", "")}
                if "voice_used" in old:
                    item["voice_used"] = old["voice_used"]
                manifest.append(_preserve_voice_versions(item, old))
            else:
                manifest.append({**base, "path": "", "ok": False, "reason": ""})
    return manifest


def count_completion(all_segments, manifest_by_index, out_dir=None,
                     expected_voice_signatures: dict[int, str] | None = None,
                     expected_voice_params: dict[int, dict] | None = None) -> dict:
    """``{total, completed, remaining}`` for a script — completed = done (ok + file exists).

    ``total`` is the number of non-empty (synthesizable) segments; the ``synthesize`` result and
    the read-only ``/batch-status`` endpoint both derive their numbers from this.

    ``out_dir`` (the package dir) switches the existence check to the batched
    :func:`done_indices` (one directory listing for every ``ok`` entry instead of one stat per
    entry — identical judgment); omitted, the per-entry :func:`is_done` loop runs verbatim.
    """
    total = len(all_segments)
    if out_dir is not None:
        done = done_indices(manifest_by_index, out_dir, get_layout().workspace,
                            expected_voice_signatures, expected_voice_params)
        completed = sum(1 for s in all_segments if s["index"] in done)
    else:
        completed = sum(
            1 for s in all_segments
            if is_done(
                manifest_by_index.get(s["index"]),
                expected_voice_signatures.get(s["index"])
                if expected_voice_signatures is not None else None,
                expected_voice_params.get(s["index"])
                if expected_voice_params is not None else None,
            )
        )
    return {"total": total, "completed": completed, "remaining": total - completed}


def _write_manifest_file(manifest_path, manifest) -> None:
    """Write the (list) manifest to disk (UTF-8, pretty-printed; JSON tolerates the CRLF)."""
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _synthesize_one(handle, indices=None, script=None, concurrency=None, seed=None,
                    auto_concurrency: bool = False) -> dict:
    """Task worker: synthesize ONE script's lines (default = resume: only the not-yet-done).

    The single-file body behind :func:`synthesize` (the legacy API path and the tests).
    The multi-file shape does NOT call this: ``synthesize_multi`` pools every chapter's
    pending segments into one engine subprocess (see there) instead of running one
    subprocess per file.

    ``concurrency`` is only the *per-batch ceiling* (the most segments that may share one GPU
    tensor batch), applied after length sorting. Only timeout recovery temporarily lowers
    this ceiling. When omitted it falls back to the
    persisted default (``config.tts.batch_concurrency``); either way it is clamped to
    ``[1, 128]`` before being handed to the worker. ``seed`` (>=0) makes a run reproducible; when
    omitted it uses ``config.tts.batch_seed`` (-1 = random).

    A hung / OOM-killed child (the worker's watchdog, exit 124) is *not* a fatal failure: the run
    shrinks the batch (halving the cap, floor 1) and restarts a fresh subprocess (resume semantics
    skip the already-done segments), down to batch 1, where a repeat timeout strikes the in-flight
    segment and two strikes isolate it as a recorded failure (the run continues without it).

    The package ``manifest.json`` is the cumulative source of truth: the in-memory state updates
    after every segment, and the disk rewrite is throttled (at most once per
    ``MANIFEST_FLUSH_INTERVAL``) with a forced flush on cancel / engine failure / watchdog restart
    / completion — a cancel keeps whatever finished; only a hard kill of the backend can lose up
    to the interval's worth of segments (their files stay on disk, so a resume re-does only what
    the manifest still lacks). A run is a *resume* — it skips segments already done
    (``ok`` + file on disk); a resume with nothing left short-circuits without spawning the
    engine. Re-doing everything is not a mode here: the caller deletes the package folder
    first (``POST /api/tts/batch-reset``), after which an ordinary resume has nothing to skip.
    """
    src = resolve_parsed_json(script)
    script = _load_script(src)

    layout = get_layout()
    ws = layout.workspace

    # voice_config is optional here — a character missing from it becomes a clear
    # per-segment error (the run continues), not a crash.
    vc_path = layout.voice_profiles / "voice_config.json"
    voice_config = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception as e:  # noqa: BLE001
            handle.log(f"voice_config.json 无法解析（{e}）——相关角色将失败。", "WARNING")
        # Lazy migration of legacy absolute ref_audio values (the worker resolves the
        # relative form against --workspace, so the file must be rewritten before spawn).
        _n, migrated_vc = pathio.migrate_entries_in(vc_path, ws, "dict", ("ref_audio",))
        if isinstance(migrated_vc, dict):
            voice_config = migrated_vc

    out_dir = layout.audio_chunk / package_for(src)
    manifest_path = out_dir / "manifest.json"

    # The full set of synthesizable segments (the manifest always describes exactly these) and
    # the subset this run will actually synthesize (a resume = the not-yet-done ones).
    all_segments = _build_segments(script)
    voice_params_by_index = (
        segment_voice_params(all_segments, voice_config)
        if vc_path.exists() else None
    )
    voice_signatures = (
        segment_voice_signatures(all_segments, voice_config)
        if vc_path.exists() else None
    )
    all_indices = {s["index"] for s in all_segments}
    old_entries = load_manifest(out_dir)
    _restore_cached_voice_versions(
        old_entries, voice_params_by_index, voice_signatures, ws,
    )
    done_set = done_indices(
        old_entries, out_dir, ws, voice_signatures, voice_params_by_index,
    ) & all_indices
    to_do = plan_to_synthesize(all_indices, done_set, indices)
    segments = [s for s in all_segments if s["index"] in to_do]
    run_total = len(segments)
    stale_speakers = sorted({
        s["speaker"] for s in all_segments
        if s["index"] not in done_set
        and old_entries.get(s["index"], {}).get("ok")
        and old_entries.get(s["index"], {}).get("path")
    })
    if stale_speakers:
        handle.log(f"检测到角色声音已变更，将重新合成这些角色的全部台词：{'、'.join(stale_speakers)}", "WARNING")
        for output in _merged_output_paths(layout, out_dir.name):
            try:
                output.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    # Stable filename width: the digits this file's FULL segment count needs (not the
    # pending subset) — a resume / watchdog restart re-derives the same width, so the
    # package never ends up with mixed 000x / 0000x names.
    width = filename_width(len(all_segments))

    if indices is None and done_set:
        handle.log(f"续合：已完成 {len(done_set)} 段，本次合成剩余 {run_total} 段（共 {len(all_segments)} 段）")
    else:
        handle.log(f"开始音频合成：{run_total} 段")

    speakers_in_batch = sorted({s["speaker"] for s in segments if s["speaker"]})
    if speakers_in_batch:
        handle.log(f"涉及角色：{'、'.join(speakers_in_batch)}")

    # Nothing left to do (a resume that is already complete) — persist the full manifest and stop
    # without spawning the engine (no wasted model load).
    if run_total == 0:
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest_file(manifest_path, build_manifest(
            all_segments, old_entries, {}, root=ws,
            expected_voice_signatures=voice_signatures,
            expected_voice_params=voice_params_by_index,
        ))
        done = count_completion(all_segments, old_entries,
                                expected_voice_signatures=voice_signatures,
                                expected_voice_params=voice_params_by_index)["completed"]
        handle.log(f"已全部完成，无需合成（{done}/{len(all_segments)} 段）。")
        handle.progress(1.0, "完成")
        return {
            "total": len(all_segments),
            "completed": done,
            "failed": [],
            "output_dir": str(out_dir),
            "manifest_path": str(manifest_path),
            "done_count": done,
            "all_count": len(all_segments),
        }

    if not voice_config:
        handle.log("警告：未找到 voice_config.json——请先在「角色配音」页生成角色声音，否则所有段都会失败。", "WARNING")

    seg_file = layout.temp / f"batch_segments_{uuid.uuid4().hex[:12]}.json"
    python, worker = resolve_engine()
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = get_config()
    t = cfg.tts
    # 批内段数（仅上限）: the request's value, else the persisted default
    # (config.tts.batch_concurrency); clamped to [1, 128] so a stray value can't spawn a
    # degenerate / unbounded cap (the worker sets the actual per-batch size at runtime).
    workers = clamp_concurrency(
        AUTO_MAX_CONCURRENCY if auto_concurrency else (concurrency if concurrency else t.batch_concurrency),
        maximum=AUTO_MAX_CONCURRENCY if auto_concurrency else MAX_CONCURRENCY,
    )
    # seed: the request's value, else the persisted default (config.tts.batch_seed); -1 = random.
    seed = seed if seed is not None else t.batch_seed
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1

    handle.log(f"引擎：.venv（一次性子进程，模型只加载一次）· "
               f"{'自动' if auto_concurrency else '手动'}批内上限 {workers} 段")
    # A persistent per-run transcript: the task log is SSE-only and vanishes with the session,
    # so every line the engine emits is also mirrored to this file (one per run, appended per
    # restart attempt) — a run that dies mid-batch leaves its exact batch / watchdog / error
    # trail on disk for diagnosis.
    run_log = layout.logs / f"tts_batch_{time.strftime('%Y%m%d_%H%M%S')}.log"
    handle.log(f"运行日志（排障用，含每次重启的完整引擎输出）：{run_log}")
    handle.progress(0.02, "启动引擎")

    seg_results: dict = {}  # index -> {ok, path, reason} (this run)
    by_index = {s["index"]: s for s in segments}
    segment_log = _SegmentLogBuffer(handle, run_total)
    in_flight: set = set()  # indices the current child was generating (from its [watchdog] line)

    # 进度指标（合成页「开始音频合成」按钮下方）：累计已合成 = 运行前 manifest 已完成
    # ∪ 本次运行 ok 的段（union 口径，显式 indices 重做已完成段时不会重复计数），对
    # 整表总数（全部可合成段 / 其 strip 后字数之和）。每段完成触发一次上报（SSE
    # 「segments」 事件），推送节流至最多每 STATS_FLUSH_INTERVAL 秒一次；启动时与收尾
    # 各强制推一次（终态快照 / 结果事件携带最终值兜底）。
    all_by_index = {s["index"]: s for s in all_segments}
    seg_total = len(all_segments)
    chars_total = sum(len(s["text"]) for s in all_segments)
    last_stats = [0.0]  # time.monotonic() of the last stats push

    def _report_stats(force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last_stats[0] < STATS_FLUSH_INTERVAL:
            return
        last_stats[0] = now
        done = chars = 0
        for i in all_indices:
            if i in done_set or (seg_results.get(i) or {}).get("ok"):
                done += 1
                chars += len(all_by_index[i]["text"])
        handle.segment_stats(done, seg_total, chars, chars_total)

    _report_stats(force=True)  # the baseline (a resume run shows its pre-run progress at once)

    # In-memory truth updates on every [segment] line, but the full-file disk rewrite is
    # throttled to at most once per MANIFEST_FLUSH_INTERVAL: under disk / AV-scanner load a
    # 1MB rewrite per line stalled this line-processing loop (task log and progress bar crept
    # one line at a time while the worker itself was tensor-batching at full speed). The first
    # line always flushes (last_flush starts at 0); forced flushes still fire on cancel, engine
    # failure, watchdog restart and completion — a cancel loses nothing, a hard kill of the
    # backend loses at most the interval's worth of segments (their files stay on disk, so a
    # resume re-does only what the manifest still lacks).
    last_flush = [0.0]  # time.monotonic() of the last manifest write to disk

    def _write_manifest(force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last_flush[0] < MANIFEST_FLUSH_INTERVAL:
            return
        _write_manifest_file(manifest_path, build_manifest(
            all_segments, old_entries, seg_results, root=ws,
            expected_voice_signatures=voice_signatures,
            expected_voice_params=voice_params_by_index,
        ))
        last_flush[0] = now

    def on_line(line: str) -> None:
        nonlocal workers  # a confirmed restore re-syncs the per-batch cap
        if line.startswith("[segment]"):
            segment_log.add(_handle_segment(line, by_index, run_total, seg_results, handle))
            _write_manifest()
            _report_stats()
        elif line.startswith("[perf] "):
            performance = _format_batch_performance(line)
            if performance is not None:
                segment_log.flush()
                handle.log(performance)
        elif line.startswith("[watchdog]"):
            # The child names the batch that hung before it exits 124: remember the in-flight
            # indices so a strike at workers==1 targets the right segment(s).
            in_flight.update(_parse_watchdog_indices(line))
            handle.log(line, "WARNING")
        elif line.startswith("[restore]"):
            # The child completed two successful batches at the newest demoted cap, restored
            # the pre-demotion cap and popped that record from its mirror
            # stack. This line is the only source of the workers bookkeeping after a
            # restore, so re-sync from it; a desync (unparseable / no pending record) is a
            # WARNING, not fatal — the worker's value still wins.
            cap = _parse_restore_cap(line)
            if cap is None:
                handle.log(line, "WARNING")
            else:
                if restore_stack:
                    _chars, expected = restore_stack.pop()
                    if expected != cap:
                        handle.log(f"[restore] 上报挡位 {cap} 与降档记录 {expected} 不一致——按 worker 上报同步", "WARNING")
                else:
                    handle.log("收到 [restore] 行但没有待恢复的降档记录——按 worker 上报同步", "WARNING")
                workers = cap
                handle.log(f"子批总字数低于降档阈值 → 批内上限恢复为 {workers} 段")
        else:
            handle.log(line)

    # -- the watchdog / restart loop ----------------------------------------------
    # A hung or OOM-killed child (exit 124) is not a fatal task failure: shrink the batch and
    # restart a fresh subprocess (resume semantics skip the done), down to workers==1, where a
    # repeat timeout strikes the in-flight segment; two strikes isolate it as a recorded failure.
    MAX_ATTEMPTS = 8
    excluded: set = set()
    struck: dict = {}
    restore_stack: list = []  # LIFO demotion records (oldest first), handed to every restart
    attempt = 0
    try:
        while True:
            if attempt > MAX_ATTEMPTS:
                raise RuntimeError(
                    f"音频合成引擎反复超时（{MAX_ATTEMPTS} 次缩批重试后仍未完成）——已完成进度已保住，"
                    f"请调小「批内段数」后重试。"
                )
            remaining = [s for s in segments
                         if s["index"] not in excluded
                         and not (seg_results.get(s["index"]) or {}).get("ok")]
            if not remaining:
                break
            seg_file.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
            cmd = _build_cmd(
                python, worker, seg_file, vc_path, out_dir,
                language=t.language, device=t.device,
                model=t.model, base_model=t.base_model, design_model=t.design_model,
                ffmpeg_path=cfg.ffmpeg.ffmpeg_path, concurrency=workers, seed=seed,
                workspace=ws,
                restore_stack=encode_restore_stack(restore_stack),
                width=width, auto_concurrency=auto_concurrency,
            )
            in_flight.clear()  # a fresh child starts with an empty in-flight set
            try:
                run_worker(cmd, handle, on_line, temp_files=(seg_file,),
                           fail_prefix="音频合成引擎", watchdog_code=124, log_file=run_log,
                           log_line=_format_batch_log_line)
                segment_log.flush()
                break  # a clean exit (0)
            except WorkerWatchdogTimeout:
                segment_log.flush()
                attempt += 1
                if workers > 1:
                    # Record this demotion (the timed-out sub-batch's total chars + the cap
                    # it ran at) BEFORE the demotion: two successful batches at the new gear
                    # restore the old gear (LIFO, one demotion per restore). An empty /
                    # unparseable in-flight set records nothing — a zero-chars record would
                    # be inert and would block the records behind it in the LIFO stack.
                    # (chars = stripped code points — _build_segments already strips the
                    # text, so this matches the worker's per-row chars exactly.)
                    timeout_chars = sum(len(by_index[i]["text"]) for i in in_flight if i in by_index)
                    if timeout_chars > 0:
                        restore_stack.append((timeout_chars, workers))
                    timeout_rows = len(in_flight)
                    workers = timeout_demotion_cap(workers, timeout_rows)
                    handle.log(f"看门狗触发（批内段超时）→ 实际批内 {timeout_rows} 段，下一次批内上限 {workers} 段"
                               + (f"（记录本批 {timeout_chars} 字，后续更小的子批将自动恢复）" if timeout_chars else "（未解析到实际批量，按原上限减半）")
                               + "，重启引擎", "WARNING")
                else:
                    # workers == 1: strike the in-flight segment; two strikes isolate a poison
                    # segment
                    newly = []
                    for i in list(in_flight):
                        struck[i] = struck.get(i, 0) + 1
                        if struck[i] >= 2:
                            excluded.add(i)
                            seg_results[i] = {"ok": False, "path": "", "reason": "超时（已隔离）"}
                            newly.append(i)
                    if newly:
                        names = "、".join(f"第 {i + 1} 段" for i in sorted(newly))
                        handle.log(f"{names} 连续两次超时 → 隔离为失败，其余段继续", "WARNING")
                    else:
                        handle.log("看门狗触发（单段超时，首次记罚）→ 重启引擎重试", "WARNING")
                # The restart rebuilds its segment table from in-memory state (a throttled
                # manifest can't cause re-synthesis) — flush anyway so a backend crash in the
                # gap can't make a later resume re-do the last ~2s.
                _write_manifest(force=True)
                continue
    except Exception:
        # Cancel / engine failure / attempt cap: flush whatever finished since the last
        # throttled write, then let the exception settle the task as before.
        segment_log.flush()
        _write_manifest(force=True)
        _report_stats(force=True)  # the terminal snapshot carries the exact final counters
        raise

    # Final manifest (the throttled writes may lag up to the interval; this one is
    # authoritative — also a safety net in case the child exits before its last line is
    # drained).
    _write_manifest(force=True)
    # The last 进度指标 push (the terminal snapshot / result events carry the same
    # final counters, so the page's card stays correct after the task settles).
    _report_stats(force=True)

    completed = 0
    failed = []
    for s in segments:
        r = seg_results.get(s["index"])
        if r and r.get("ok"):
            completed += 1
        else:
            failed.append({"index": s["index"], "speaker": s["speaker"],
                           "reason": (r or {}).get("reason") or "（引擎未返回结果）"})

    done = count_completion(all_segments, load_manifest(out_dir),
                            expected_voice_signatures=voice_signatures,
                            expected_voice_params=voice_params_by_index)
    handle.log(
        f"音频合成结束：本次成功 {completed} / 失败 {len(failed)} / 共 {run_total} 段；"
        f"累计已合成 {done['completed']}/{done['total']} 段。清单：{manifest_path.name}"
    )

    if completed == 0:
        first = failed[0]["reason"] if failed else "无"
        raise RuntimeError(f"本次 {run_total} 段全部合成失败。首个原因：{first}")

    handle.progress(1.0, "完成")
    return {
        "total": run_total,
        "completed": completed,
        "failed": failed,
        "output_dir": str(out_dir),
        "manifest_path": str(manifest_path),
        "done_count": done["completed"],
        "all_count": done["total"],
    }


def synthesize(handle, indices=None, script=None, concurrency=None, seed=None,
               auto_concurrency: bool = False) -> dict:
    """Single-file entry (the legacy ``POST /api/tts/batch`` path and the tests): delegates
    verbatim to :func:`_synthesize_one`. Signature kept identical so positional callers work."""
    return _synthesize_one(handle, indices, script, concurrency, seed, auto_concurrency)


def synthesize_multi(handle, scripts, concurrency=None, seed=None,
                     auto_concurrency: bool = False) -> dict:
    """Task worker: synthesize several parsed JSON files (chapters) in ONE task — pooled.

    Every selected chapter's *pending* segments (resume: the not-yet-done lines) are read
    into one unified TTS task pool and a SINGLE engine subprocess schedules the whole pool:
    the worker's speaker grouping / ordering / lazy sub-batch / VRAM governance now run
    across chapter boundaries (the model set loads once; a short chapter's tail no longer
    starves the batch). Each pool row carries its chapter attribution (``out_dir`` = the
    chapter's package dir, ``file_index`` = the line's position inside that chapter) so
    every result lands back in the right chapter package — the pool's scheduling order is
    decoupled from the chapter order, but results and manifests keep the chapter-local
    order. 章节是数据组织单位，不是 GPU 调度单位。

    Prep-stage fatals (missing / corrupt / empty script, a package collision) are
    *isolated* per file: logged, recorded on that file's ``error`` entry, and the run
    continues with the other files. An engine-level "all of a file's segments failed"
    does NOT set ``error`` — it is expressed by the top-level ``failed`` list (each entry
    tagged with its ``script``) and the chapter's own manifest ``ok: false`` entries (one
    bad chapter never sinks the pool — the same principle as 角色配音's per-character
    failure isolation). The task fails only when every actually-pooled segment failed
    (or every file was a prep fatal); an all-complete / all-empty selection settles as a
    success without spawning the engine.

    Progress is driven directly by the worker's ``[progress]`` lines (0→1 over the whole
    pool — the backend emits no per-file progress windows). Cancellation propagates:
    ``TaskCancelled`` is re-raised (NEVER swallowed by the per-file error isolation); a
    forced manifest flush covers every pooled file first, so a cancel keeps whatever
    finished.
    """
    if not scripts:
        raise RuntimeError("没有可合成的文件——请先在「待合成」列表勾选解析 JSON。")
    n = len(scripts)
    layout = get_layout()
    ws = layout.workspace

    # voice_config is optional here — a character missing from it becomes a clear
    # per-segment error (the run continues), not a crash. Read + lazily migrated ONCE for
    # the whole pool (same logic as _synthesize_one; the worker resolves the relative
    # ref_audio against --workspace, so the rewrite happens before any spawn).
    vc_path = layout.voice_profiles / "voice_config.json"
    voice_config = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception as e:  # noqa: BLE001
            handle.log(f"voice_config.json 无法解析（{e}）——相关角色将失败。", "WARNING")
        _n, migrated_vc = pathio.migrate_entries_in(vc_path, ws, "dict", ("ref_audio",))
        if isinstance(migrated_vc, dict):
            voice_config = migrated_vc

    # -- per-file prep (request order): fatal files are isolated, the rest join the pool --
    files: list[_PooledFile] = []
    pkg_owner: dict = {}  # package -> first file claiming it (collision defence)
    for i, name in enumerate(scripts):
        handle.check()  # cancel / pause point before any work on file i
        f = _PooledFile(name=name)
        files.append(f)
        handle.log(f"文件 {i + 1}/{n}：{name}")
        try:
            src = resolve_parsed_json(name)
            script = _load_script(src)
            f.src = src
            pkg = package_for(src)
            # API-layer defence: the UI already filters _checked names, but a direct API
            # call could pass both x.json and x_checked.json — they map to ONE package and
            # would write the same files / manifest, so fail the second clearly instead of
            # corrupting silently.
            if pkg in pkg_owner:
                raise RuntimeError(f"包目录 {pkg} 与 {pkg_owner[pkg]} 冲突（同一包不可被两个文件合成）")
            pkg_owner[pkg] = name
            f.pkg = pkg
            f.out_dir = layout.audio_chunk / pkg
            f.manifest_path = f.out_dir / "manifest.json"
            f.all_segments = _build_segments(script)
            f.by_index = {s["index"]: s for s in f.all_segments}
            f.voice_signatures = (
                segment_voice_signatures(f.all_segments, voice_config)
                if vc_path.exists() else None
            )
            f.voice_params = (
                segment_voice_params(f.all_segments, voice_config)
                if vc_path.exists() else None
            )
            f.old_entries = load_manifest(f.out_dir)
            all_indices = {s["index"] for s in f.all_segments}
            _restore_cached_voice_versions(
                f.old_entries, f.voice_params, f.voice_signatures, ws,
            )
            done_set = done_indices(
                f.old_entries, f.out_dir, ws, f.voice_signatures, f.voice_params,
            ) & all_indices
            stale_speakers = sorted({
                s["speaker"] for s in f.all_segments
                if s["index"] not in done_set
                and f.old_entries.get(s["index"], {}).get("ok")
                and f.old_entries.get(s["index"], {}).get("path")
            })
            if stale_speakers:
                handle.log(
                    f"检测到 {name} 中角色声音已变更，将重新合成这些角色的全部台词：{'、'.join(stale_speakers)}",
                    "WARNING",
                )
                for output in _merged_output_paths(layout, f.pkg):
                    try:
                        output.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
            f.pending = sorted(plan_to_synthesize(all_indices, done_set))
            f.all_count = len(f.all_segments)
            # Pre-run completion snapshot (进度指标 baseline: this file's done 段/字).
            f.done_set = done_set
            f.done_count = len(done_set)
            f.done_chars = sum(len(f.by_index[i]["text"]) for i in done_set)
            if f.pending:
                if done_set:
                    handle.log(f"续合：已完成 {len(done_set)} 段，待合成 {len(f.pending)} 段（共 {f.all_count} 段）")
                else:
                    handle.log(f"待合成 {len(f.pending)} 段（共 {f.all_count} 段）")
            else:
                # Nothing left (all done, or an empty script) — rewrite the engine-owned
                # manifest and contribute no rows to the pool (no wasted model work).
                f.out_dir.mkdir(parents=True, exist_ok=True)
                _write_manifest_file(f.manifest_path,
                                     build_manifest(
                                         f.all_segments, f.old_entries, {}, root=ws,
                                         expected_voice_signatures=f.voice_signatures,
                                         expected_voice_params=f.voice_params,
                                     ))
                handle.log("无待合成段，跳过" if not f.all_count else f"已全部完成，跳过（0/{f.all_count} 段待合成）")
        except TaskCancelled:
            raise  # cancel is a task-level outcome — never "file failed, keep going"
        except Exception as e:  # noqa: BLE001 — one bad file is isolated, the pool continues
            f.error = str(e)
            handle.log(f"文件 {name} 准备失败（跳过，继续其余文件）：{e}", "ERROR")

    if not voice_config:
        handle.log("警告：未找到 voice_config.json——请先在「角色配音」页生成角色声音，否则所有段都会失败。", "WARNING")

    # -- build the unified pool ----------------------------------------------------------
    pool_rows, pool_owners = _build_pool_rows(files)
    pool_total = len(pool_rows)
    # The single source mapping pool-global index -> (chapter file, chapter-local index).
    pool_map = {row["index"]: (f, row["file_index"]) for row, f in zip(pool_rows, pool_owners)}

    # -- engine prep (once; the model set loads once for the whole pool) ------------------
    seg_file = layout.temp / f"batch_segments_{uuid.uuid4().hex[:12]}.json"
    cfg = get_config()
    t = cfg.tts
    # 批内段数（仅上限）: the request's value, else the persisted default
    # (config.tts.batch_concurrency); clamped to [1, 128] — ONE cap for the whole pool.
    workers = clamp_concurrency(
        AUTO_MAX_CONCURRENCY if auto_concurrency else (concurrency if concurrency else t.batch_concurrency),
        maximum=AUTO_MAX_CONCURRENCY if auto_concurrency else MAX_CONCURRENCY,
    )
    # seed: the request's value, else the persisted default (config.tts.batch_seed); -1 = random.
    seed = seed if seed is not None else t.batch_seed
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1

    # Multi-manifest throttled flush: one shared 2s clock, every pooled (dirty, non-fatal)
    # file's manifest rewritten together. Forced flushes (cancel / engine failure /
    # watchdog restart / completion) cover ALL pooled files — a cancel keeps whatever
    # finished in every chapter, not just the last one touched.
    last_flush = [0.0]

    def flush_manifests(force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last_flush[0] < MANIFEST_FLUSH_INTERVAL:
            return
        for f in files:
            if f.error or not f.pending:
                continue
            if not force and not f.dirty:
                continue
            f.dirty = False
            _write_manifest_file(f.manifest_path,
                                 build_manifest(
                                     f.all_segments, f.old_entries, f.seg_results, root=ws,
                                     expected_voice_signatures=f.voice_signatures,
                                     expected_voice_params=f.voice_params,
                                 ))
        last_flush[0] = now

    # In-flight POOL indices the current child was generating (from its [watchdog] line) —
    # shared with on_line below; a strike at workers==1 targets these, mapped back to their
    # chapters for the manifest write.
    in_flight: set = set()
    segment_log = _SegmentLogBuffer(handle, pool_total)

    def on_line(line: str) -> None:
        nonlocal workers  # a confirmed restore re-syncs the per-batch cap
        if line.startswith("[segment]"):
            segment_log.add(_handle_segment_pool(line, pool_map, pool_total, handle))
            flush_manifests()
            _report_stats()  # resolved at call time (defined before the loop below)
        elif line.startswith("[perf] "):
            performance = _format_batch_performance(line)
            if performance is not None:
                segment_log.flush()
                handle.log(performance)
        elif line.startswith("[watchdog]"):
            in_flight.update(_parse_watchdog_indices(line))
            handle.log(line, "WARNING")
        elif line.startswith("[restore]"):
            # The child completed two successful batches at the newest demoted cap, restored
            # the pre-demotion cap and popped that record from its mirror
            # stack. This line is the only source of the workers bookkeeping after a
            # restore, so re-sync from it; a desync (unparseable / no pending record) is a
            # WARNING, not fatal — the worker's value still wins.
            cap = _parse_restore_cap(line)
            if cap is None:
                handle.log(line, "WARNING")
            else:
                if restore_stack:
                    _chars, expected = restore_stack.pop()
                    if expected != cap:
                        handle.log(f"[restore] 上报挡位 {cap} 与降档记录 {expected} 不一致——按 worker 上报同步", "WARNING")
                else:
                    handle.log("收到 [restore] 行但没有待恢复的降档记录——按 worker 上报同步", "WARNING")
                workers = cap
                handle.log(f"子批总字数低于降档阈值 → 批内上限恢复为 {workers} 段")
        else:
            handle.log(line)

    run_log = layout.logs / f"tts_batch_{time.strftime('%Y%m%d_%H%M%S')}.log"

    # No pool (everything done / empty, or every file a prep fatal): settle without the
    # engine — all-fatals raise inside _settle_pool (nothing was synthesized).
    if pool_total == 0:
        handle.log(f"无待合成段（{n} 个文件），不启动引擎")
        return _settle_pool(handle, files)

    # Resolve the external TTS environment only after validation has produced
    # actual work.  Corrupt or empty inputs should report their per-file errors
    # even on a machine that has not installed the optional TTS environment.
    python, worker = resolve_engine()

    # The pooled rows' own out_dir decides their save location; the whole-batch --out-dir is
    # just the worker's required fallback (constraint 3) — the first pooled file's package.
    for f in files:
        if f.pending and not f.error:
            f.out_dir.mkdir(parents=True, exist_ok=True)  # the manifest flush needs the dir
    out_dir_fallback = next(f.out_dir for f in files if f.pending and not f.error)
    handle.log(f"引擎：.venv（一次性子进程，全池统一调度，模型只加载一次）· "
               f"{'自动' if auto_concurrency else '手动'}批内上限 {workers} 段")
    handle.log(f"运行日志（排障用，含每次重启的完整引擎输出）：{run_log}")

    # 进度指标（合成页「开始音频合成」按钮下方）：全池累计已合成 = 各文件运行前已完成
    # ∪ 本次运行 ok 的段（union 口径），对全池总数（prep fatal 的文件不进总数——
    # 它们没有可合成段）。每段完成触发一次上报（SSE 「segments」 事件），推送节流至
    # 最多每 STATS_FLUSH_INTERVAL 秒一次；启动时与收尾各强制推一次。
    seg_total = sum(f.all_count for f in files if not f.error)
    chars_total = sum(len(s["text"]) for f in files if not f.error for s in f.all_segments)
    # Stable filename width for the WHOLE pool: the digits the LARGEST package's full
    # segment count needs, applied uniformly to every package (one digit count across the
    # run). Full counts (not pending) keep it stable across watchdog restarts, so a pool
    # filled over several runs never mixes 000x / 0000x names.
    width = filename_width(max((f.all_count for f in files if not f.error), default=0))
    last_stats = [0.0]  # time.monotonic() of the last stats push

    def _report_stats(force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last_stats[0] < STATS_FLUSH_INTERVAL:
            return
        last_stats[0] = now
        done = chars = 0
        for f in files:
            if f.error:
                continue
            for i, seg in f.by_index.items():
                if i in f.done_set or (f.seg_results.get(i) or {}).get("ok"):
                    done += 1
                    chars += len(seg["text"])
        handle.segment_stats(done, seg_total, chars, chars_total)

    _report_stats(force=True)  # the baseline (resume runs show their pre-run progress at once)

    MAX_ATTEMPTS = 8
    excluded: set = set()  # pool indices isolated after two strikes
    struck: dict = {}
    restore_stack: list = []  # LIFO demotion records (oldest first), handed to every restart
    attempt = 0
    try:
        while True:
            if attempt > MAX_ATTEMPTS:
                raise RuntimeError(
                    f"音频合成引擎反复超时（{MAX_ATTEMPTS} 次缩批重试后仍未完成）——已完成进度已保住，"
                    f"请调小「批内段数」后重试。"
                )
            # The restart keeps the remaining rows' ORIGINAL pool indices (no renumbering):
            # the manifest / result mapping and the workers' protocol stay stable across
            # restarts.
            remaining = []
            for row in pool_rows:
                if row["index"] in excluded:
                    continue
                f, local = pool_map[row["index"]]
                if (f.seg_results.get(local) or {}).get("ok"):
                    continue
                remaining.append(row)
            if not remaining:
                break
            seg_file.write_bytes(json.dumps(remaining, ensure_ascii=False).encode("utf-8"))
            cmd = _build_cmd(
                python, worker, seg_file, vc_path, out_dir_fallback,
                language=t.language, device=t.device,
                model=t.model, base_model=t.base_model, design_model=t.design_model,
                ffmpeg_path=cfg.ffmpeg.ffmpeg_path, concurrency=workers, seed=seed,
                workspace=ws,
                restore_stack=encode_restore_stack(restore_stack),
                width=width, auto_concurrency=auto_concurrency,
            )
            in_flight.clear()  # a fresh child starts with an empty in-flight set
            try:
                run_worker(cmd, handle, on_line, temp_files=(seg_file,),
                           fail_prefix="音频合成引擎", watchdog_code=124, log_file=run_log,
                           log_line=_format_batch_log_line)
                segment_log.flush()
                break  # a clean exit (0)
            except WorkerWatchdogTimeout:
                segment_log.flush()
                attempt += 1
                if workers > 1:
                    # Record this demotion (the timed-out sub-batch's total chars + the cap
                    # it ran at) BEFORE the demotion: two successful batches at the new gear
                    # restore the old gear (LIFO, one demotion per restore). The in-flight
                    # set holds POOL-global indices — the rows' own table carries the text.
                    # An empty / unparseable set records nothing (a zero-chars record would
                    # block the LIFO stack behind it).
                    timeout_chars = sum(len(r["text"]) for r in pool_rows if r["index"] in in_flight)
                    if timeout_chars > 0:
                        restore_stack.append((timeout_chars, workers))
                    timeout_rows = len(in_flight)
                    workers = timeout_demotion_cap(workers, timeout_rows)
                    handle.log(f"看门狗触发（批内段超时）→ 实际批内 {timeout_rows} 段，下一次批内上限 {workers} 段"
                               + (f"（记录本批 {timeout_chars} 字，后续更小的子批将自动恢复）" if timeout_chars else "（未解析到实际批量，按原上限减半）")
                               + "，重启引擎", "WARNING")
                else:
                    # workers == 1: strike the in-flight POOL rows; two strikes isolate a
                    # poison row — mapped back to its chapter for the manifest write.
                    newly = []
                    for pi in sorted(in_flight):
                        struck[pi] = struck.get(pi, 0) + 1
                        if struck[pi] >= 2:
                            excluded.add(pi)
                            f, local = pool_map[pi]
                            f.seg_results[local] = {"ok": False, "path": "", "reason": "超时（已隔离）"}
                            f.dirty = True
                            newly.append((f, local, pi))
                    if newly:
                        names = "、".join(f"{f.name} 第 {local + 1} 段（池内第 {pi + 1}）"
                                          for f, local, pi in sorted(newly, key=lambda t_: t_[2]))
                        handle.log(f"{names} 连续两次超时 → 隔离为失败，其余段继续", "WARNING")
                    else:
                        handle.log("看门狗触发（单段超时，首次记罚）→ 重启引擎重试", "WARNING")
                # The restart rebuilds its segment table from in-memory state (a throttled
                # manifest can't cause re-synthesis) — flush every pooled file anyway so a
                # backend crash in the gap can't make a later resume re-do the last ~2s.
                flush_manifests(force=True)
                continue
    except Exception:
        # Cancel / engine failure / attempt cap: force-flush every pooled file's manifest,
        # then let the exception settle the task (TaskCancelled re-raised verbatim).
        segment_log.flush()
        flush_manifests(force=True)
        _report_stats(force=True)  # the terminal snapshot carries the exact final counters
        raise

    # Final authoritative manifests (the throttled writes may lag up to the interval; also a
    # safety net in case the child exits before its last line is drained).
    flush_manifests(force=True)
    # The last 进度指标 push (the terminal snapshot / result events carry the same final
    # counters, so the page's card stays correct after the task settles).
    _report_stats(force=True)
    return _settle_pool(handle, files)
