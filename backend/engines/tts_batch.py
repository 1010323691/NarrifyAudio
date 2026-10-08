"""Batch TTS engine — synthesize every script line (subprocess orchestrator).

One long-running Task drives the worker's ``batch`` mode: a single shared ``.venv``
subprocess loads the needed model(s) once and synthesizes all segments in JSON
order, so a full book runs in one process (models loaded once) while the 3.14
backend still never imports torch. The child's stdout is pumped line-by-line into
progress counters and manifests, so a single failure never freezes the run.
Task history and ``<workspace>/logs/tts_batch_<timestamp>.log`` retain one
summary per batch, transitions and bounded failure diagnostics. DEBUG retains
internal protocol output as well.

``synthesize`` is a durable engine operation (first arg is the task context), mirroring
``engines/tts.py``: it streams progress/log, honours cooperative cancel (killing
the child), and marks the task FAILED only on a *fatal* error (no segments, engine
down, or every segment failed) — a per-segment failure is a recorded, non-fatal line.
"""
from __future__ import annotations

import json
import hashlib
import time

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..core.tts_batch_limits import AUTO_BATCH_CAPS, AUTO_BATCH_MAX
from ..core import pathio
from ..core.config import get_config
from ..core.paths import get_or_prepare_layout, resolve_parsed_json
from ..core.task_control import TaskCancelled
from .tts import DEFAULT_LANGUAGE, WorkerOutOfMemory, WorkerWatchdogTimeout, resolve_engine, run_tts_subprocess
from .tts_manifest import (
    voice_params,
    voice_signature,
    segment_voice_params,
    segment_voice_signatures,
    restore_cached_voice_versions,
    merged_output_paths,
    defer_or_delete,
    migrate_voice_config,
    invalidate_speaker_outputs,
    load_manifest,
    package_for,
    read_manifest,
    migrate_manifest,
    is_done,
    done_indices,
    plan_to_synthesize,
    build_manifest,
    count_completion,
    write_manifest_file,
)

# 一键合成「批内段数」上限的上下界（前端输入与后端钳制共用）。这只是上限：worker 运行时按
# 长度排序后按上限切批；仅超时触发临时减半及后续恢复。
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 128
# The automatic curve's global ceiling is the measured 5-char anchor; it does not
# extrapolate beyond the recorded range.
AUTO_MAX_CONCURRENCY = AUTO_BATCH_MAX

# 增量 manifest 的落盘节流：内存态逐条更新，整份 JSON 重写最多每 2 秒一次（取消 / 引擎失败 /
# 看门狗重启 / 收尾仍强制落盘）。每行都整份重写 1MB 会在磁盘 / 杀软扫描负载下拖住行处理主
# 循环（任务日志与进度条逐行蠕动，而 worker 实际在全速张量批）。
MANIFEST_FLUSH_INTERVAL = 2.0

# 进度指标（已合成/总段数 · 已合成/总字数 → 合成页「开始音频合成」按钮下方）的 SSE 推送节流：
# 每段完成都触发一次上报，但推送最多每 1 秒一次——一个子批的段行往往同秒内成串到达，而每条
# 事件都携带全量累计值，节流不丢信息（启动时与收尾各强制推一次；终态快照/结果事件带最终值兜底）。
STATS_FLUSH_INTERVAL = 1.0


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


def oom_demotion_cap(current_cap: int, actual_rows: int, auto: bool) -> int:
    """Step below the actual failed batch, never restoring an OOM ceiling."""
    ceiling = min(current_cap, actual_rows) if actual_rows > 0 else current_cap
    if auto:
        for tier in AUTO_BATCH_CAPS:
            if tier < ceiling:
                return tier
    return max(1, ceiling // 2)


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
               width: int = 0, auto_concurrency: bool = False,
               oom_restore_cap: int = 0) -> list:
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
        # 跨层术语映射：worker 侧「design」声线模型，后端任务 voices.clone 即 worker 的
        # design-batch 模式（模式与行协议见 tts-engine/tts_worker.py 头部注释）。
        cmd += ["--design-model", design_model]
    if ffmpeg_path:
        cmd += ["--ffmpeg", ffmpeg_path]
    if restore_stack:
        cmd += ["--restore-stack", restore_stack]
    if width:
        cmd += ["--width", str(width)]
    if oom_restore_cap:
        cmd += ["--oom-restore-cap", str(oom_restore_cap)]
    if auto_concurrency:
        cmd.append("--auto-batch")
    cmd += ["--vocoder-batch-size", "8"]
    return cmd


def _load_script(script_path):
    if not script_path.exists():
        raise RuntimeError("未找到脚本 JSON（03_parsed_json/）——请先在「文本解析」生成脚本。")
    try:
        data = json.loads(script_path.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{script_path.name} 无法解析：{e}")
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"{script_path.name} 为空——请先生成脚本。")
    return data


class _BatchRunLogHandle:
    """Write the same concise task messages to the run's diagnostic file."""

    def __init__(self, handle, path: Path):
        self.handle = handle
        self.path = path
        self.quota_inputs = []
        self.quota_started = 0.0
        self.quota_seconds = 0.0
        self.manifest_seconds = 0.0
        self.events_seconds = 0.0
        self.stage_dirs = []
        self.expected_audio = 0
        self.last_metrics = time.monotonic()
        self.last_published = 0

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def queue_tts_input(self, count, operation, key):
        if not self.quota_inputs:
            self.quota_started = time.monotonic()
        self.quota_inputs.append((count, operation, key))
        self.flush_workspace_stages()

    def flush_workspace_stages(self, *, force=False):
        flush = getattr(self.handle, "flush_workspace_stages", None)
        if callable(flush):
            flush(force=force)
        if self.quota_inputs and (force or len(self.quota_inputs) >= 128
                                  or time.monotonic() - self.quota_started >= 0.25):
            # Never declare counters/manifest complete before audio and quota are durable.
            if callable(flush):
                flush(force=True)
            from ..platform.quota import consume_tts_inputs
            started = time.monotonic()
            consume_tts_inputs(self.quota_inputs)
            self.quota_seconds += time.monotonic() - started
            self.quota_inputs.clear()
        self.report_publication()

    def report_publication(self, *, force=False):
        now = time.monotonic()
        if not self.expected_audio or (not force and now - self.last_metrics < 30):
            return
        journal = getattr(self.handle, "_publication_journal", None)
        metrics = getattr(journal, "metrics", {})
        if not isinstance(metrics, dict):
            return
        published = metrics.get("published_audio", 0)
        pending = sum(1 for directory in self.stage_dirs for _ in directory.glob("*.mp3"))
        elapsed = max(0.001, now - self.last_metrics)
        rate = max(0, published - self.last_published) / elapsed
        self.last_metrics, self.last_published = now, published
        self.log(
            f"发布统计：已生成文件 {published + pending}/{self.expected_audio} 段 · "
            f"已发布 {published} 段 · 暂存待发布 {pending} 段 · "
            f"发布速率 {rate:.1f} 段/秒 · 恢复记录写入 {metrics.get('journal_bytes', 0)} 字节 · "
            f"累计耗时：记录 {metrics.get('journal_seconds', 0):.2f}s、"
            f"移动 {metrics.get('move_seconds', 0):.2f}s、额度 {self.quota_seconds:.2f}s、"
            f"清单 {self.manifest_seconds:.2f}s、进度事件 {self.events_seconds:.2f}s"
        )

    def log(self, message: str, level: str = "INFO") -> None:
        self.handle.log(message, level)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(f"{time.strftime('%H:%M:%S')} [{level}] {message}\n")
        except OSError:
            pass  # logging must not abort an otherwise recoverable synthesis


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
        self.last_summary: str | None = None

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

    def flush(self, performance: str | None = None) -> None:
        if not performance and not self.pending_completed and not self.pending_failed:
            return
        counts = (f"成功 {self.pending_completed} 段、失败 {self.pending_failed} 段；"
                  f"累计完成 {self.completed}/{self.total} 段")
        message = f"{performance} · {counts}" if performance else f"合成进度：{counts}"
        if self.pending_failure_details:
            extra = "；".join(self.pending_failure_details)
            remaining = self.pending_failed - len(self.pending_failure_details)
            if remaining > 0:
                extra += f"；另有 {remaining} 条失败"
            message += f"；失败示例：{extra}"
        self.last_summary = message
        self.handle.log(message, "WARNING" if self.pending_failed else "INFO")
        self.pending_completed = 0
        self.pending_failed = 0
        self.pending_failure_details.clear()


    def mirror(self, line: str) -> str | None:
        # Human summaries/transitions are already mirrored by the handle.
        # DEBUG additionally retains internal protocol diagnostics.
        return line if get_config().log.level.upper() == "DEBUG" else None


def _published_segment_path(handle, detail: str, stage_dir, final_dir) -> str:
    if stage_dir is None or final_dir is None:
        return detail
    staged_root = Path(stage_dir).resolve()
    staged_file = Path(detail).resolve()
    if not staged_file.is_relative_to(staged_root) or not staged_file.is_file():
        raise RuntimeError(f"TTS worker returned an invalid staged audio path: {detail}")
    final_file = Path(final_dir) / staged_file.relative_to(staged_root)
    publish = getattr(handle, "queue_workspace_stage", handle.publish_workspace_stage)
    publish(final_file, staged_file)
    return str(final_file)


def _flush_audio(handle, *, force: bool = True) -> None:
    flush = getattr(handle, "flush_workspace_stages", None)
    if callable(flush):
        flush(force=force)


def _handle_segment(line: str, by_index: dict, total: int, seg_results: dict, handle,
                    stage_dir=None, final_dir=None) -> dict | None:
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
        detail = _published_segment_path(handle, detail, stage_dir, final_dir)
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
    return (f"性能：批次 {event.get('batch', '?')} · 批内数量 {rows} · 总字数 {chars} · 总耗时 {seconds:.2f} 秒 · "
            f"吞吐量 {throughput:.2f} 字/秒")


def _format_batch_log_line(line: str) -> str | None:
    """Keep batch summaries and meaningful transitions; protocol remains internal."""
    if get_config().log.level.upper() == "DEBUG":
        return line
    if line.startswith(("[segment]", "[progress]")):
        return None
    if line.startswith("[perf]"):
        return _format_batch_performance(line)
    if line.startswith(("子批（", "[watchdog]", "[restore]", "[oom-restore]")):
        return None  # handled as an explicit transition/warning by the caller
    if "生成中… 已用时" in line or line.startswith(("[perf]", "机械后处理流水线", "vocoder：")):
        return None
    if line.startswith(("device =", "需加载模型", "Loading", "自动批内上限")):
        return None
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


def build_segments(script, indices=None):
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
    stage_out_dir: Path | None = None
    manifest_path: Path | None = None
    all_segments: list = field(default_factory=list)
    by_index: dict = field(default_factory=dict)
    old_entries: dict = field(default_factory=dict)
    voice_params: dict[int, dict] | None = None
    voice_signatures: dict[int, str] | None = None
    seg_results: dict = field(default_factory=dict)
    pending: list = field(default_factory=list)
    # Seed counters from the resume snapshot, then update them only when a
    # segment changes completion state (including retries and watchdog errors).
    done_set: set = field(default_factory=set)
    done_count: int = 0
    done_chars: int = 0
    all_count: int = 0
    all_chars: int = 0
    error: str | None = None
    dirty: bool = False
    cancelled: bool = False
    quota_operation: str = "tts.batch"


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
        if f.error or f.cancelled or not f.pending:
            continue
        for local in f.pending:
            row = dict(f.by_index[local])
            row["index"] = pool_start
            row["out_dir"] = str(f.stage_out_dir or f.out_dir)
            row["file_index"] = local
            pool_rows.append(row)
            pool_owners.append(f)
            pool_start += 1
    return pool_rows, pool_owners


def _update_pool_result(f, local: int, result: dict) -> None:
    previous = local in f.done_set or bool((f.seg_results.get(local) or {}).get("ok"))
    completed = local in f.done_set or bool(result.get("ok"))
    delta = int(completed) - int(previous)
    f.done_count += delta
    f.done_chars += delta * len(f.by_index[local]["text"])
    f.seg_results[local] = result
    f.dirty = True


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
    chapter_cancelled = getattr(handle, "chapter_cancelled", None)
    if callable(chapter_cancelled) and chapter_cancelled(f.name):
        if f.name not in getattr(handle, "results", {}):
            f.cancelled = True
        return
    speaker = (f.by_index.get(local) or {}).get("speaker") or "(未知)"
    if status == "ok":
        detail = _published_segment_path(handle, detail, f.stage_out_dir, f.out_dir)
        _update_pool_result(f, local, {"ok": True, "path": detail, "reason": ""})
        return {
            "ok": True,
            "index": pool_index,
            "label": f"[{pool_index + 1}/{pool_total}] {f.name} · {speaker}",
        }
    else:
        _update_pool_result(f, local, {"ok": False, "path": "", "reason": detail})
        return {
            "ok": False,
            "index": pool_index,
            "label": f"[{pool_index + 1}/{pool_total}] {f.name} · {speaker}",
            "detail": detail,
        }


def _settle_pool(handle, files, *, allow_failure: bool = False) -> dict:
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
            f.all_segments, migrate_manifest(f.out_dir, handle),
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
    if allow_failure:
        if len(files) == 1 and files[0].error:
            result["error"] = files[0].error
        return result
    attempted = [f for f in files if f.error or f.pending]
    if not attempted or result["completed"] > 0:
        handle.progress(1.0, "完成")
        return result
    if result["total"] > 0:
        raise RuntimeError(f"全部 {result['total']} 段合成失败（各章节原因见任务日志与结果 files 字段）。")
    raise RuntimeError(f"所选 {len(files)} 个文件全部无法合成（原因见任务日志与结果 files 字段）。")


def _synthesize_one(handle, indices=None, script=None, concurrency=None, seed=None,
                    auto_concurrency: bool | None = None) -> dict:
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

    layout = get_or_prepare_layout()
    ws = layout.workspace
    run_log = layout.logs / f"tts_batch_{time.strftime('%Y%m%d_%H%M%S')}.log"
    handle = _BatchRunLogHandle(handle, run_log)

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
        migrate_voice_config(handle, vc_path, ws, voice_config)

    out_dir = layout.audio_chunk / package_for(src)
    manifest_path = out_dir / "manifest.json"

    # The full set of synthesizable segments (the manifest always describes exactly these) and
    # the subset this run will actually synthesize (a resume = the not-yet-done ones).
    all_segments = build_segments(script)
    voice_params_by_index = (
        segment_voice_params(all_segments, voice_config)
        if vc_path.exists() else None
    )
    voice_signatures = (
        segment_voice_signatures(all_segments, voice_config)
        if vc_path.exists() else None
    )
    all_indices = {s["index"] for s in all_segments}
    old_entries = migrate_manifest(out_dir, handle)
    restore_cached_voice_versions(
        old_entries, voice_params_by_index, voice_signatures, ws,
    )
    done_set = done_indices(
        old_entries, out_dir, ws, voice_signatures, voice_params_by_index,
    ) & all_indices
    to_do = plan_to_synthesize(all_indices, done_set, indices)
    segments = [s for s in all_segments if s["index"] in to_do]
    run_total = len(segments)
    allocate_directory = getattr(handle, "allocate_workspace_directory", None)
    stage_out_dir = allocate_directory(out_dir) if run_total and callable(allocate_directory) else None
    stale_speakers = sorted({
        s["speaker"] for s in all_segments
        if s["index"] not in done_set
        and old_entries.get(s["index"], {}).get("ok")
        and old_entries.get(s["index"], {}).get("path")
    })
    if stale_speakers:
        handle.log(f"检测到角色声音已变更，将重新合成这些角色的全部台词：{'、'.join(stale_speakers)}", "WARNING")
        for output in merged_output_paths(layout, out_dir.name):
            defer_or_delete(handle, output)
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
        write_manifest_file(manifest_path, build_manifest(
            all_segments, old_entries, {}, root=ws,
            expected_voice_signatures=voice_signatures,
            expected_voice_params=voice_params_by_index,
        ), handle)
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

    from ..platform.quota import QuotaInsufficientError, reserve_tts_quota
    if not reserve_tts_quota(sum(len(segment["text"]) for segment in segments), "tts.batch"):
        raise QuotaInsufficientError("TTS 输入字数超过可用额度")

    if not voice_config:
        handle.log("警告：未找到 voice_config.json——请先在「角色配音」页生成角色声音，否则所有段都会失败。", "WARNING")

    seg_file = layout.temp / f"batch_segments_{uuid.uuid4().hex[:12]}.json"
    python, worker = resolve_engine()
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = get_config()
    t = cfg.tts
    auto_concurrency = t.batch_auto if auto_concurrency is None else bool(auto_concurrency)
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
    # Batch summaries and transitions persist in both task history and the run log.
    handle.log(f"运行日志（批次摘要与异常）：{run_log}")
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
        _flush_audio(handle)
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
        _flush_audio(handle)
        write_manifest_file(manifest_path, build_manifest(
            all_segments, old_entries, seg_results, root=ws,
            expected_voice_signatures=voice_signatures,
            expected_voice_params=voice_params_by_index,
        ), handle)
        last_flush[0] = now

    def on_line(line: str) -> None:
        nonlocal workers, oom_restore_cap  # keep restart caps in sync with the child
        if line.startswith("[segment]"):
            outcome = _handle_segment(
                line, by_index, run_total, seg_results, handle, stage_out_dir, out_dir,
            )
            if outcome and outcome.get("ok"):
                from ..platform.quota import consume_tts_input
                segment_index = outcome["index"]
                consume_tts_input(len(by_index[segment_index]["text"]), "tts.batch", str(segment_index))
            segment_log.add(outcome)
            _write_manifest()
            _report_stats()
            _flush_audio(handle, force=False)
        elif line.startswith("[perf] "):
            performance = _format_batch_performance(line)
            if performance is not None:
                segment_log.flush(performance)
        elif line.startswith("[watchdog]"):
            # The child names the batch that hung before it exits 124: remember the in-flight
            # indices so a strike at workers==1 targets the right segment(s).
            in_flight.update(_parse_watchdog_indices(line))
            handle.log(line, "WARNING")
        elif line.startswith("[oom-restore]"):
            cap = _parse_restore_cap(line)
            if cap is not None and cap == oom_restore_cap:
                workers = cap
                oom_restore_cap = 0
                restore_stack.clear()
                handle.log(f"降档批次已完成，尝试恢复批内上限 {workers} 段。")
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
            message = _format_batch_log_line(line)
            if message is not None:
                handle.log(message)

    # -- the watchdog / restart loop ----------------------------------------------
    # A hung or OOM-killed child (exit 124) is not a fatal task failure: shrink the batch and
    # restart a fresh subprocess (resume semantics skip the done), down to workers==1, where a
    # repeat timeout strikes the in-flight segment; two strikes isolate it as a recorded failure.
    MAX_ATTEMPTS = 8
    excluded: set = set()
    struck: dict = {}
    restore_stack: list = []  # LIFO demotion records (oldest first), handed to every restart
    attempt = 0
    oom_seen = False
    oom_restore_cap = 0
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
                python, worker, seg_file, vc_path, stage_out_dir or out_dir,
                language=t.language, device=t.device,
                model=t.model, base_model=t.base_model, design_model=t.design_model,
                ffmpeg_path=cfg.ffmpeg.ffmpeg_path, concurrency=workers, seed=seed,
                workspace=ws,
                restore_stack=encode_restore_stack(restore_stack),
                width=width, auto_concurrency=auto_concurrency,
                oom_restore_cap=oom_restore_cap,
            )
            in_flight.clear()  # a fresh child starts with an empty in-flight set
            try:
                from ..platform.quota import require_quota
                require_quota("TTS", "tts.batch")
                run_tts_subprocess(cmd, handle, on_line, temp_files=(seg_file,),
                           fail_prefix="音频合成引擎", watchdog_code=124, log_file=run_log,
                           log_line=segment_log.mirror, interrupt_on_pause=True, private_errors=True)
                segment_log.flush()
                break  # a clean exit (0)
            except WorkerOutOfMemory as exc:
                segment_log.flush()
                if workers <= 1 or exc.rows == 1:
                    raise RuntimeError("音频合成显存不足，已保留完成进度；请释放显存后重试。") from None
                oom_restore_cap = workers if not oom_seen else 0
                oom_seen = True
                workers = oom_demotion_cap(workers, exc.rows, auto_concurrency)
                restore_stack.clear()
                policy = "完成一批后尝试恢复原档位" if oom_restore_cap else "本次任务保持降档执行"
                handle.log(f"显存不足，批内上限已降为 {workers} 段，正在自动重试；{policy}。", "WARNING")
                _write_manifest(force=True)
                continue
            except WorkerWatchdogTimeout:
                segment_log.flush()
                attempt += 1
                if workers > 1:
                    # Record this demotion (the timed-out sub-batch's total chars + the cap
                    # it ran at) BEFORE the demotion: two successful batches at the new gear
                    # restore the old gear (LIFO, one demotion per restore). An empty /
                    # unparseable in-flight set records nothing — a zero-chars record would
                    # be inert and would block the records behind it in the LIFO stack.
                    # (chars = stripped code points — build_segments already strips the
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

    done = count_completion(all_segments, migrate_manifest(out_dir, handle),
                            expected_voice_signatures=voice_signatures,
                            expected_voice_params=voice_params_by_index)
    discard_directory = getattr(handle, "discard_workspace_directory", None)
    if stage_out_dir is not None and callable(discard_directory):
        discard_directory(stage_out_dir)
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
               auto_concurrency: bool | None = None) -> dict:
    """Single-file entry (the legacy ``POST /api/tts/batch`` path and the tests): delegates
    verbatim to :func:`_synthesize_one`. Signature kept identical so positional callers work."""
    return _synthesize_one(handle, indices, script, concurrency, seed, auto_concurrency)


# ---------------------------------------------------------------------------
# 整章预览：单句重渲染（产物落暂存区，绝不触碰正式 05 / manifest / 03）
# ---------------------------------------------------------------------------

def preview_lock_path(layout, package: str) -> Path:
    """章节级跨进程锁文件：预览保存（``/apply``）与 ``tts.merge`` 执行期共用同一把锁。

    防止合并读到保存过程中新旧混合的 05 文件。锁等待超时（``TimeoutError``）由 Worker 统一转
    ``worker_error(retryable=True)`` 退避重试——apply 持锁仅秒级，实际几乎总是等待后取到。
    """
    return layout.temp / "locks" / "chapter_preview" / f"{package}.lock"


def preview_staging_dir(layout, package: str) -> Path:
    """预览产物暂存区（``00_temp/chapter_preview/<pkg>/``）：重渲染音频 + ``state.json``。"""
    return layout.temp / "chapter_preview" / package


def _preview_state_path(layout, package: str) -> Path:
    return preview_staging_dir(layout, package) / "state.json"


def _preview_state_read(layout, package: str) -> dict:
    """The chapter's preview state, degraded to an empty shape when absent / corrupt."""
    p = _preview_state_path(layout, package)
    if not p.exists():
        return {"script": "", "updated_at": 0.0, "lines": {}}
    try:
        data = json.loads(p.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — 损坏的暂存状态 = 无暂存（重渲染会整体覆盖该行）
        return {"script": "", "updated_at": 0.0, "lines": {}}
    if not isinstance(data, dict):
        return {"script": "", "updated_at": 0.0, "lines": {}}
    if not isinstance(data.get("lines"), dict):
        data["lines"] = {}
    data.setdefault("script", "")
    data.setdefault("updated_at", 0.0)
    return data


def _preview_fingerprint(text: str, speaker: str, instruct: str) -> str:
    """Stable hash of the rendered triple (the line's re-render gate key)."""
    raw = json.dumps({"text": text, "speaker": speaker, "instruct": instruct},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def render_preview(handle, script, render, concurrency=None, seed=None) -> dict:
    """Task worker (``tts.preview_render``): re-render preview lines into the STAGING area.

    ``render`` is an array of ``{index, text?, speaker?, instruct?}`` (entries carry only the
    fields the caller changed; the JSON task payload never drifts key types). The fields are
    overridden on an IN-MEMORY copy of the script (``type`` / ``pause_after`` and any other
    per-line fields are preserved), the target lines go through the same one-shot TTS worker
    (``build_segments`` + ``run_tts_subprocess``), and the worker writes its audio into
    ``00_temp/chapter_preview/<pkg>/`` itself — file number ``file_index + 1`` (1-based,
    zfill'd), and the ``[segment] ok <path>`` line reports the ACTUAL produced file (an MP3,
    or the kept WAV when the MP3 encode failed). That reported name is what gets recorded in
    ``state.json`` (``file``) and is what the save gate / audition use.

    Line-level failures (a watchdog two-strike, a worker error line) are recorded in
    ``state.json`` as ``ok=false, reason`` and the task STILL ends in its normal success
    terminal — deliberately different from :func:`_synthesize_one`, whose all-failed
    ``raise`` would turn a line failure into a retryable task crash. A FATAL engine error
    (a non-watchdog non-zero exit) fails the task as usual.

    Nothing in this function touches ``05_audio_chunk``, the package manifest, or the script
    on disk — the save endpoint (``/apply``) is the only writer of those, under the chapter
    lock and with backup/rollback. Quota is billed like formal synthesis (operation name
    ``"tts.batch"``; the ContextVar is bound by the Worker, and a caller without one — the
    unit tests — is a no-op).

    ``concurrency`` is accepted for executor-signature symmetry and ignored: a preview run
    renders one line per task, so the per-batch ceiling is always 1.
    """
    src = resolve_parsed_json(script)
    lines = _load_script(src)
    total = len(lines)

    indices: list[int] = []
    for item in render or []:
        index = int(item["index"])
        if index >= total:
            raise RuntimeError(f"第 {index + 1} 句越界（本章共 {total} 行）。")
        if index not in indices:
            indices.append(index)
    indices.sort()
    if not indices:
        raise RuntimeError("没有要渲染的句子。")
    for item in render:
        row = lines[int(item["index"])]
        for key in ("text", "speaker", "instruct"):
            if item.get(key) is not None:
                row[key] = item[key]
    for index in indices:
        if not (lines[index].get("text") or "").strip():
            raise RuntimeError(f"第 {index + 1} 句文本为空，无法渲染。")

    layout = get_or_prepare_layout()
    ws = layout.workspace
    pkg = package_for(src)
    staging = preview_staging_dir(layout, pkg)
    staging.mkdir(parents=True, exist_ok=True)

    if not (layout.voice_profiles / "voice_config.json").exists():
        handle.log("警告：未找到 voice_config.json——请先在「角色配音」页生成角色声音，否则所有段都会失败。", "WARNING")

    segments = build_segments(lines, indices)
    from ..platform.quota import QuotaInsufficientError, reserve_tts_quota
    if not reserve_tts_quota(sum(len(segment["text"]) for segment in segments), "tts.batch"):
        raise QuotaInsufficientError("TTS 输入字数超过可用额度")

    cfg = get_config()
    t = cfg.tts
    seed = seed if seed is not None else t.batch_seed
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1
    # Width is a function of the chapter's FULL line count (same rule as the batch run) so
    # the staging files match the formal package's digit count.
    width = filename_width(total)

    seg_file = layout.temp / f"preview_segments_{uuid.uuid4().hex[:12]}.json"
    python, worker = resolve_engine()
    handle.log(f"整章预览渲染：{len(segments)} 句（{pkg}）→ 暂存 {staging.name}")
    handle.progress(0.05, "启动引擎")

    seg_results: dict = {}  # index -> {ok, path, reason}
    by_index = {s["index"]: s for s in segments}

    def on_line(line: str) -> None:
        if line.startswith("[segment]"):
            outcome = _handle_segment(line, by_index, len(segments), seg_results, handle)
            if outcome and outcome.get("ok"):
                from ..platform.quota import consume_tts_input
                consume_tts_input(len(by_index[outcome["index"]]["text"]), "tts.batch",
                                  str(outcome["index"]))
        else:
            handle.log(line)

    # Watchdog: the per-batch ceiling is 1, so an exit-124 is a strike on this line — one
    # strike restarts the engine once, two strikes record the line failed（"超时（已隔离）"）
    # and the task still ends normally (the frontend shows the line failed + a retry).
    MAX_STRIKES = 2
    strikes = 0
    while True:
        seg_file.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        cmd = _build_cmd(
            python, worker, seg_file, layout.voice_profiles / "voice_config.json", staging,
            language=t.language, device=t.device,
            model=t.model, base_model=t.base_model, design_model=t.design_model,
            ffmpeg_path=cfg.ffmpeg.ffmpeg_path, concurrency=1, seed=seed,
            workspace=ws, width=width,
        )
        try:
            run_tts_subprocess(cmd, handle, on_line, temp_files=(seg_file,),
                               fail_prefix="整章预览渲染引擎", watchdog_code=124)
            break  # a clean exit (0): every line reported via its [segment] line
        except WorkerWatchdogTimeout:
            strikes += 1
            handle.log(f"预览渲染看门狗触发（超时，第 {strikes}/{MAX_STRIKES} 次）", "WARNING")
            if strikes >= MAX_STRIKES:
                for index in by_index:
                    if not (seg_results.get(index) or {}).get("ok"):
                        seg_results[index] = {"ok": False, "path": "", "reason": "超时（已隔离）"}
                handle.log("连续两次超时 → 该句记为失败（可重试）。", "WARNING")
                break
            handle.log("重启引擎重试", "WARNING")
    try:
        seg_file.unlink(missing_ok=True)
    except OSError:
        pass

    # 结果落盘：按行并入 state.json（同章节多句各自独立累积；原子写）
    state = _preview_state_read(layout, pkg)
    state["script"] = script
    state["updated_at"] = time.time()
    lines_state = state["lines"]
    for seg in segments:
        index = seg["index"]
        r = seg_results.get(index) or {"ok": False, "path": "", "reason": "（引擎未返回结果）"}
        ok = bool(r.get("ok"))
        lines_state[str(index)] = {
            "ok": ok,
            "reason": "" if ok else str(r.get("reason") or "未知原因"),
            # The worker-reported ACTUAL produced file (an .mp3, or the kept .wav fallback) —
            # the save gate and the audition both key off it, never an assumed extension.
            "file": Path(r.get("path") or "").name if ok else "",
            "text": seg["text"],
            "speaker": seg["speaker"],
            "instruct": seg["instruct"],
            "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "fingerprint": _preview_fingerprint(seg["text"], seg["speaker"], seg["instruct"]),
        }
    pathio.rewrite_json_file(_preview_state_path(layout, pkg), state)

    completed = sum(1 for s in segments if (seg_results.get(s["index"]) or {}).get("ok"))
    handle.log(f"整章预览渲染结束：成功 {completed} / 共 {len(segments)} 句。")
    handle.progress(1.0, "完成")
    return {
        "script": script,
        "package": pkg,
        "total": len(segments),
        "completed": completed,
        "failed": [
            {"index": s["index"], "speaker": s["speaker"],
             "reason": (seg_results.get(s["index"]) or {}).get("reason", "")}
            for s in segments if not (seg_results.get(s["index"]) or {}).get("ok")
        ],
        "state_path": str(_preview_state_path(layout, pkg)),
    }


def synthesize_multi(handle, scripts, concurrency=None, seed=None,
                     auto_concurrency: bool | None = None) -> dict:
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
    layout = get_or_prepare_layout()
    ws = layout.workspace
    run_log = layout.logs / f"tts_batch_{time.strftime('%Y%m%d_%H%M%S')}.log"
    handle = _BatchRunLogHandle(handle, run_log)

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
        migrate_voice_config(handle, vc_path, ws, voice_config)

    # -- per-file prep (request order): fatal files are isolated, the rest join the pool --
    files: list[_PooledFile] = []
    pkg_owner: dict = {}  # package -> first file claiming it (collision defence)
    for i, name in enumerate(scripts):
        handle.check()  # cancel / pause point before any work on file i
        f = _PooledFile(name=name)
        files.append(f)
        if get_config().log.level.upper() == "DEBUG":
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
            f.all_segments = build_segments(script)
            f.by_index = {s["index"]: s for s in f.all_segments}
            f.voice_signatures = (
                segment_voice_signatures(f.all_segments, voice_config)
                if vc_path.exists() else None
            )
            f.voice_params = (
                segment_voice_params(f.all_segments, voice_config)
                if vc_path.exists() else None
            )
            f.old_entries = migrate_manifest(f.out_dir, handle)
            all_indices = {s["index"] for s in f.all_segments}
            restore_cached_voice_versions(
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
                for output in merged_output_paths(layout, f.pkg):
                    defer_or_delete(handle, output)
            f.pending = sorted(plan_to_synthesize(all_indices, done_set))
            f.dirty = bool(f.pending) and not f.manifest_path.exists()
            allocate_directory = getattr(handle, "allocate_workspace_directory", None)
            if f.pending and callable(allocate_directory):
                f.stage_out_dir = allocate_directory(f.out_dir)
            f.all_count = len(f.all_segments)
            f.all_chars = sum(len(seg["text"]) for seg in f.all_segments)
            # Pre-run completion snapshot (进度指标 baseline: this file's done 段/字).
            f.done_set = done_set
            f.done_count = len(done_set)
            f.done_chars = sum(len(f.by_index[i]["text"]) for i in done_set)
            if f.pending and get_config().log.level.upper() == "DEBUG":
                if done_set:
                    handle.log(f"续合：已完成 {len(done_set)} 段，待合成 {len(f.pending)} 段（共 {f.all_count} 段）")
                else:
                    handle.log(f"待合成 {len(f.pending)} 段（共 {f.all_count} 段）")
            elif not f.pending:
                # Nothing left (all done, or an empty script) — rewrite the engine-owned
                # manifest and contribute no rows to the pool (no wasted model work).
                f.out_dir.mkdir(parents=True, exist_ok=True)
                write_manifest_file(f.manifest_path,
                                     build_manifest(
                                         f.all_segments, f.old_entries, {}, root=ws,
                                         expected_voice_signatures=f.voice_signatures,
                                         expected_voice_params=f.voice_params,
                                     ), handle)
                if get_config().log.level.upper() == "DEBUG":
                    handle.log("无待合成段，跳过" if not f.all_count else f"已全部完成，跳过（0/{f.all_count} 段待合成）")
        except TaskCancelled:
            raise  # cancel is a task-level outcome — never "file failed, keep going"
        except Exception as e:  # noqa: BLE001 — one bad file is isolated, the pool continues
            f.error = str(e)
            handle.log(f"文件 {name} 准备失败（跳过，继续其余文件）：{e}", "ERROR")

    if not voice_config:
        handle.log("警告：未找到 voice_config.json——请先在「角色配音」页生成角色声音，否则所有段都会失败。", "WARNING")

    # Admit chapters in request order. Each chapter reserves its pending input before
    # entering the pool. After the first quota failure, report all following chapters too.
    from ..platform.quota import QuotaInsufficientError, reserve_tts_quota
    chapter_cancelled = getattr(handle, "chapter_cancelled", None)
    if callable(chapter_cancelled):
        for f in files:
            f.cancelled = chapter_cancelled(f.name)
    quota_exhausted = False
    for chapter_index, f in enumerate(files, 1):
        if f.error or f.cancelled or not f.pending:
            continue
        f.quota_operation = f"tts.batch.chapter.{chapter_index}.{hashlib.sha1(f.name.encode('utf-8')).hexdigest()[:8]}"
        required_chars = sum(len(f.by_index[index]["text"]) for index in f.pending)
        if quota_exhausted or not reserve_tts_quota(required_chars, f.quota_operation):
            quota_exhausted = True
            f.error = f"额度不足：本章需要 {required_chars} 字，当前可用额度不足"
            handle.log(f"{f.name}：{f.error}，跳过本章及后续章节", "ERROR")
        else:
            if get_config().log.level.upper() == "DEBUG":
                handle.log(f"{f.name}：已预留本章 TTS 输入 {required_chars} 字")

    # -- build the unified pool ----------------------------------------------------------
    pool_rows, pool_owners = _build_pool_rows(files)
    pool_total = len(pool_rows)
    handle.expected_audio = pool_total
    handle.stage_dirs = [f.stage_out_dir for f in files if f.stage_out_dir is not None]
    # The single source mapping pool-global index -> (chapter file, chapter-local index).
    pool_map = {row["index"]: (f, row["file_index"]) for row, f in zip(pool_rows, pool_owners)}

    # -- engine prep (once; the model set loads once for the whole pool) ------------------
    seg_file = layout.temp / f"batch_segments_{uuid.uuid4().hex[:12]}.json"
    cfg = get_config()
    t = cfg.tts
    auto_concurrency = t.batch_auto if auto_concurrency is None else bool(auto_concurrency)
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

    def flush_file(f):
        if f.error or not f.pending or not f.dirty:
            return
        _flush_audio(handle)
        started = time.monotonic()
        write_manifest_file(f.manifest_path,
                            build_manifest(f.all_segments, f.old_entries, f.seg_results, root=ws,
                                           expected_voice_signatures=f.voice_signatures,
                                           expected_voice_params=f.voice_params), handle)
        handle.manifest_seconds += time.monotonic() - started
        f.dirty = False

    def flush_manifests(force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last_flush[0] < MANIFEST_FLUSH_INTERVAL:
            return
        _flush_audio(handle)
        for f in files:
            if f.dirty:
                flush_file(f)
        last_flush[0] = time.monotonic()

    notified = set()
    chapter_settled = getattr(handle, "chapter_settled", None)
    chapter_progress = getattr(handle, "chapter_progress", None)

    def report_chapter(f):
        if not callable(chapter_progress) or f.error or f.cancelled:
            return
        chapter_progress(f.name, f.done_count, f.all_count, f.done_chars, f.all_chars)

    def settle_chapter(f, force=False):
        if not callable(chapter_settled) or f.name in notified or f.cancelled:
            return
        if not force and not f.error and len(f.seg_results) < len(f.pending):
            return
        _flush_audio(handle)
        flush_file(f)
        report_chapter(f)
        result = _settle_pool(handle, [f], allow_failure=True)
        chapter_settled(result)
        notified.add(f.name)

    # In-flight POOL indices the current child was generating (from its [watchdog] line) —
    # shared with on_line below; a strike at workers==1 targets these, mapped back to their
    # chapters for the manifest write.
    in_flight: set = set()
    segment_log = _SegmentLogBuffer(handle, pool_total)

    def on_line(line: str) -> None:
        nonlocal workers, oom_restore_cap  # keep restart caps in sync with the child
        if line.startswith("[segment]"):
            outcome = _handle_segment_pool(line, pool_map, pool_total, handle)
            if outcome and outcome.get("ok"):
                file, local_index = pool_map[outcome["index"]]
                handle.queue_tts_input(
                    len(file.by_index[local_index]["text"]), file.quota_operation,
                    f"{file.name}:{local_index}",
                )
            if outcome:
                file, _local = pool_map[outcome["index"]]
                stats_dirty.add(file.name)
                settle_chapter(file)
            segment_log.add(outcome)
            flush_manifests()
            _report_stats()  # resolved at call time (defined before the loop below)
            _flush_audio(handle, force=False)
        elif line.startswith("[perf] "):
            performance = _format_batch_performance(line)
            if performance is not None:
                segment_log.flush(performance)
        elif line.startswith("[watchdog]"):
            in_flight.update(_parse_watchdog_indices(line))
            handle.log(line, "WARNING")
        elif line.startswith("[oom-restore]"):
            cap = _parse_restore_cap(line)
            if cap is not None and cap == oom_restore_cap:
                workers = cap
                oom_restore_cap = 0
                restore_stack.clear()
                handle.log(f"降档批次已完成，尝试恢复批内上限 {workers} 段。")
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
            message = _format_batch_log_line(line)
            if message is not None:
                handle.log(message)

    # No pool (everything done / empty, or every file a prep fatal): settle without the
    # engine — all-fatals raise inside _settle_pool (nothing was synthesized).
    for f in files:
        settle_chapter(f)

    if pool_total == 0:
        if quota_exhausted:
            raise QuotaInsufficientError("TTS 输入字数额度不足，未合成任何章节")
        handle.log(f"无待合成段（{n} 个文件），不启动引擎")
        handle.report_publication(force=True)
        return _settle_pool(handle, [f for f in files if not f.cancelled])

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
    handle.log(f"开始音频合成：{n} 章，{pool_total} 段待合成")
    handle.log(f"引擎：.venv（一次性子进程，全池统一调度，模型只加载一次）· "
               f"{'自动' if auto_concurrency else '手动'}批内上限 {workers} 段")
    handle.log(f"运行日志（批次摘要与异常）：{run_log}")

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
    last_stats = [0.0]
    stats_dirty = {f.name for f in files}

    def _report_stats(force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - last_stats[0] < STATS_FLUSH_INTERVAL:
            return
        _flush_audio(handle)
        started = time.monotonic()
        for f in files:
            if force or f.name in stats_dirty:
                report_chapter(f)
        stats_dirty.clear()
        handle.segment_stats(sum(f.done_count for f in files if not f.error), seg_total,
                             sum(f.done_chars for f in files if not f.error), chars_total)
        handle.events_seconds += time.monotonic() - started
        last_stats[0] = time.monotonic()

    _report_stats(force=True)  # the baseline (resume runs show their pre-run progress at once)

    MAX_ATTEMPTS = 8
    excluded: set = set()  # pool indices isolated after two strikes
    struck: dict = {}
    restore_stack: list = []  # LIFO demotion records (oldest first), handed to every restart
    attempt = 0
    oom_seen = False
    oom_restore_cap = 0
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
                if f.cancelled or (f.seg_results.get(local) or {}).get("ok"):
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
                oom_restore_cap=oom_restore_cap,
            )
            in_flight.clear()  # a fresh child starts with an empty in-flight set
            try:
                from ..platform.quota import require_quota
                require_quota("TTS", "tts.batch")
                run_tts_subprocess(cmd, handle, on_line, temp_files=(seg_file,),
                           fail_prefix="音频合成引擎", watchdog_code=124, log_file=run_log,
                           log_line=segment_log.mirror, interrupt_on_pause=True, private_errors=True)
                segment_log.flush()
                break  # a clean exit (0)
            except WorkerOutOfMemory as exc:
                segment_log.flush()
                if workers <= 1 or exc.rows == 1:
                    raise RuntimeError("音频合成显存不足，已保留完成进度；请释放显存后重试。") from None
                oom_restore_cap = workers if not oom_seen else 0
                oom_seen = True
                workers = oom_demotion_cap(workers, exc.rows, auto_concurrency)
                restore_stack.clear()
                policy = "完成一批后尝试恢复原档位" if oom_restore_cap else "本次任务保持降档执行"
                handle.log(f"显存不足，批内上限已降为 {workers} 段，正在自动重试；{policy}。", "WARNING")
                flush_manifests(force=True)
                continue
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
                            _update_pool_result(f, local, {"ok": False, "path": "", "reason": "超时（已隔离）"})
                            stats_dirty.add(f.name)
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
    discard_directory = getattr(handle, "discard_workspace_directory", None)
    if callable(discard_directory):
        for f in files:
            if f.stage_out_dir is not None:
                discard_directory(f.stage_out_dir)
    for f in files:
        settle_chapter(f, force=True)
    handle.report_publication(force=True)
    return _settle_pool(handle, [f for f in files if not f.cancelled])
