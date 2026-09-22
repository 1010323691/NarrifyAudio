"""Audio-splitting engine — ported (behavior-preserving) from ``mp3-cue``.

The pure logic (even-split planning, pause-snapping, duration parsing, naming,
display formatting) is a 1:1 port of ``src/lib/{cutPlanner,silence,media,format}.js``
plus the naming/cut orchestration from ``App.vue``.

The one deliberate change: the source ran **ffmpeg.wasm** in the browser (30 MB
first-run CDN download, whole file resident in MEMFS, >1 GB warning). Here the
same operations run **native FFmpeg / ffprobe** as subprocesses that stream from
disk — no memory ceiling, offline, faster. The command lines are the same the
wasm core used internally:

* duration    → ``ffprobe -show_entries format=duration``
* pauses      → ``ffmpeg -af silencedetect=noise=-30dB:d=0.5 -f null -``
* cut         → ``ffmpeg -ss S -i in -t D -c copy out``  (lossless stream copy)

The preserved invariants are the algorithmic ones: even split keeps every segment
at/below the target; pause-snapping only ever moves *interior* boundaries, stays
monotonic, and never changes the segment count; ``-c copy`` re-encodes nothing.

A second deliberate change (user-requested): the naming format is the *complete*
file name — the source tool prefixed the source file's base name, here a format
like ``重活了 第 {} 集`` yields ``重活了 第 001 集.mp3`` with no prefix.
"""
from __future__ import annotations

import math
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..core.tasks import TaskCancelled

# ============================ Constants ============================

SILENCE_NOISE_DB = -30
SILENCE_MIN_DURATION = 0.5
DEFAULT_TOLERANCE = 15
TOLERANCE_MIN = 5
TOLERANCE_MAX = 30
MAX_SEGMENTS = 1000

MIME = {
    "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "aac": "audio/aac",
    "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
    "flac": "audio/flac", "webm": "audio/webm",
}


# ============================ Pure helpers ============================

def _fmt_num(x) -> str:
    """Format a number the way JS ``String(x)`` would (drop a trailing ``.0``),
    so ``-ss`` / ``-t`` args match the original exactly."""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def get_extension(file_name: Optional[str]) -> str:
    """Extension (lower-cased, no dot); falls back to ``mp3``."""
    base = file_name or ""
    idx = base.rfind(".")
    if idx == -1 or idx == len(base) - 1:
        return "mp3"
    return base[idx + 1:].lower()


def format_bytes(b) -> str:
    if b is None or (isinstance(b, float) and math.isnan(b)):
        return "—"
    if b < 1024:
        return f"{b} B"
    units = ["KB", "MB", "GB", "TB"]
    n = float(b)
    i = -1
    while True:
        n /= 1024
        i += 1
        if not (n >= 1024 and i < len(units) - 1):
            break
    digits = 0 if (n >= 100 or i == 0) else 1
    return f"{n:.{digits}f} {units[i]}"


def format_duration(total_seconds) -> str:
    if total_seconds is None or (isinstance(total_seconds, float) and math.isnan(total_seconds)):
        return "—"
    s = max(0, round(total_seconds))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def parse_duration_to_seconds(value) -> float:
    """``MM:SS`` / ``HH:MM:SS`` / plain seconds → seconds; NaN when unparseable."""
    if value is None:
        return float("nan")
    s = str(value).strip()
    if not s:
        return float("nan")
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = s.split(":")
    if not (2 <= len(parts) <= 3):
        return float("nan")
    if any(p == "" or not re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        return float("nan")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


# ============================ Planning ============================

def build_plan(total_duration: float, target_input) -> dict:
    """Even-distribution plan: ``count = ceil(total/target)``, ``each = total/count``."""
    target = parse_duration_to_seconds(target_input)
    if not (total_duration > 0):
        return {"valid": False, "reason": "无法读取音频时长，暂时无法生成方案。"}
    if not (target > 0):
        return {"valid": False, "reason": "请输入有效的目标时长（需大于 0）。"}

    count = max(1, math.ceil(total_duration / target - 1e-6))
    each = total_duration / count

    segments = []
    for i in range(count):
        start = i * each
        end = total_duration if i == count - 1 else (i + 1) * each
        segments.append({"index": i, "start": start, "duration": end - start})

    return {"valid": True, "reason": "", "count": count, "each": each, "target": target, "segments": segments}


def snap_boundaries(ideals, pauses, tolerance, min_gap: float = 0.5) -> dict:
    """Snap even-split interior boundaries to the nearest pause midpoint within
    ±tolerance. First/last boundaries are fixed; a per-boundary clamp keeps the
    result monotonic (boundaries can never cross)."""
    n = len(ideals) - 1  # segment count
    bounds = list(ideals)
    shifts = [0.0] * len(ideals)

    if n < 2:  # single segment: nothing to snap
        return {"bounds": bounds, "shifts": shifts, "snapped": 0, "fallbacks": 0}

    # Clamp the window so a boundary can't drift past its neighbours' midpoint.
    W = max(0.0, min(tolerance, (ideals[1] - ideals[0]) / 2 - min_gap))
    if W <= 0 or not pauses:
        return {"bounds": bounds, "shifts": shifts, "snapped": 0, "fallbacks": n - 1}

    mids = [(p["start"] + p["end"]) / 2 for p in pauses]  # sorted, since pauses are

    snapped = 0
    fallbacks = 0
    prev = 0.0
    start_idx = 0  # first candidate not below the current lo (advances only)

    for i in range(1, n):  # interior boundaries 1..n-1
        ideal = ideals[i]
        lo = max(ideal - W, prev + min_gap)
        hi = min(ideal + W, ideals[i + 1] - W - min_gap)

        while start_idx < len(mids) and mids[start_idx] < lo:
            start_idx += 1

        best = None
        if hi > lo:
            for j in range(start_idx, len(mids)):
                c = mids[j]
                if c > hi:
                    break
                if best is None or abs(c - ideal) < abs(best - ideal):
                    best = c

        if best is None:
            fallbacks += 1
            prev = ideal
            continue  # no pause in window → stay at the even-split time

        snapped_at = min(hi, max(lo, best))
        bounds[i] = snapped_at
        shifts[i] = snapped_at - ideal
        snapped += 1
        prev = snapped_at

    return {"bounds": bounds, "shifts": shifts, "snapped": snapped, "fallbacks": fallbacks}


def build_aligned_plan(total_duration: float, target_input, pauses, tolerance) -> dict:
    """build_plan() + pause-snapping in one call. Segment count is unchanged."""
    base = build_plan(total_duration, target_input)
    if not base["valid"]:
        return {**base, "aligned": False}

    count = base["count"]
    if count < 2:
        return {**base, "aligned": False, "shifts": [], "snapped": 0, "fallbacks": 0}

    ideals = [s["start"] for s in base["segments"]] + [total_duration]
    r = snap_boundaries(ideals, pauses, tolerance)
    bounds = r["bounds"]

    snapped_segments = [
        {"index": i, "start": bounds[i], "duration": bounds[i + 1] - bounds[i]}
        for i in range(count)
    ]
    return {
        **base, "segments": snapped_segments, "shifts": r["shifts"],
        "snapped": r["snapped"], "fallbacks": r["fallbacks"], "aligned": True,
    }


# ============================ Naming ============================

def output_name(index: int, naming_format: str, start_number, ext: str) -> str:
    """The naming format is the complete file name: its ``{}`` is the (start+index)
    number, zero-padded to the width the user typed for the start number — the
    source file's base name is *not* prefixed (``重活了 第 {} 集`` →
    ``重活了 第 001 集.mp3``); without ``{}`` the number is appended so files
    never collide."""
    digits = re.sub(r"\D", "", str(start_number or ""))
    start = int(digits) if digits else 1
    num_str = str(start + index).rjust(max(1, len(digits)), "0")

    fmt = (naming_format or "").strip()
    if "{}" in fmt:
        base = re.sub(r"\{\s*\}", num_str, fmt)
    else:
        base = (fmt + "_" if fmt else "") + num_str

    return f"{base}.{ext}"


# ============================ Silence parsing (pure) ============================

# Permissive across ffmpeg 4.x/5.x/6.x; tolerates the ``[silencedetect @ 0x…]`` prefix.
RE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
RE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")
RE_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def parse_silence_log(lines, total_duration: float) -> list:
    """Turn silencedetect log lines into sorted pause intervals.

    A silence that runs to the very end never emits a ``silence_end`` — it is capped
    at ``total_duration``. Slightly-negative leading timestamps (encoder delay) are
    ignored. Pure — unit-testable without running ffmpeg.
    """
    pauses = []
    open_start = None
    for line in lines:
        s = RE_START.search(line)
        if s:
            v = float(s.group(1))
            if v >= 0:
                open_start = v
            continue
        e = RE_END.search(line)
        if e and open_start is not None:
            end = float(e.group(1))
            if end > open_start:
                pauses.append({"start": open_start, "end": end})
            open_start = None
    if open_start is not None and open_start < total_duration:
        pauses.append({"start": open_start, "end": total_duration})
    pauses.sort(key=lambda p: p["start"])
    return pauses


# ============================ Native FFmpeg ops ============================

def probe_duration(path, ffprobe_path: str = ""):
    """Duration in seconds via ``ffprobe`` (replaces the browser <audio>/Web-Audio path).
    Returns ``(duration, error)`` — ``nan`` on failure."""
    ffprobe = ffprobe_path or "ffprobe"
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=120)
    except FileNotFoundError:
        return float("nan"), f"找不到 ffprobe（{ffprobe}）。请安装 FFmpeg 或在设置中指定路径。"
    except subprocess.TimeoutExpired:
        return float("nan"), "读取音频时长超时。"
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return float("nan"), err or "ffprobe 读取失败。"
    val = (proc.stdout or b"").decode("utf-8", "replace").strip()
    try:
        return float(val), ""
    except ValueError:
        return float("nan"), f"无法解析时长：{val!r}"


def detect_silences(path, total_duration: float, ffmpeg_path: str = "",
                    on_progress: Optional[Callable] = None,
                    should_cancel: Optional[Callable] = None,
                    on_log: Optional[Callable] = None,
                    noise_db: int = SILENCE_NOISE_DB,
                    min_duration: float = SILENCE_MIN_DURATION) -> dict:
    """Run ``silencedetect`` in a single streaming pass (native ffmpeg).

    A reader thread streams stderr (feeding progress via ``time=`` and the log
    callback); the main loop polls for cancellation independently so a cancel is
    responsive even if ffmpeg buffers its stderr.
    """
    ffmpeg = ffmpeg_path or "ffmpeg"
    cmd = [ffmpeg, "-loglevel", "info", "-i", str(path), "-map", "0:a:0",
           "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}", "-f", "null", "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return {"ok": False, "reason": f"找不到 ffmpeg（{ffmpeg}）。请安装 FFmpeg 或在设置中指定路径。"}

    lines: list = []

    def reader() -> None:
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                lines.append(line)
                if on_log:
                    on_log(line)
                m = RE_TIME.search(line)
                if m and on_progress and total_duration > 0:
                    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    on_progress(min(1.0, secs / total_duration))
        except Exception:
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    cancelled = False
    try:
        while proc.poll() is None:
            if should_cancel and should_cancel():
                cancelled = True
                break
            time.sleep(0.1)
        if cancelled:
            proc.kill()
        proc.wait()
        t.join(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if cancelled:
        raise TaskCancelled()

    code = proc.returncode
    if code != 0:
        return {"ok": False, "reason": f"停顿检测失败（引擎退出码 {code}）"}

    pauses = parse_silence_log(lines, total_duration)
    if on_progress:
        on_progress(1.0)
    return {"ok": True, "pauses": pauses}


def cut_segments(path, segments, out_dir, naming_format: str, start_number,
                 ffmpeg_path: str = "", ext: str = "mp3",
                 on_progress: Optional[Callable] = None,
                 should_cancel: Optional[Callable] = None,
                 on_log: Optional[Callable] = None) -> list:
    """Cut each segment with ``-c copy`` (lossless) directly to its final name.

    Runs as one ffmpeg invocation per segment; cancellation is checked between
    segments (each copy is fast, so this stays responsive).
    """
    ffmpeg = ffmpeg_path or "ffmpeg"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    n = len(segments)
    for i, seg in enumerate(segments):
        if should_cancel and should_cancel():
            raise TaskCancelled()

        name = output_name(i, naming_format, start_number, ext)
        out_path = out_dir / name
        cmd = [ffmpeg, "-y",
               "-ss", _fmt_num(seg["start"]),
               "-i", str(path),
               "-t", _fmt_num(seg["duration"]),
               "-c", "copy",
               str(out_path)]

        if on_log:
            on_log(f"第 {i + 1}/{n} 段：-ss {_fmt_num(seg['start'])} -t {_fmt_num(seg['duration'])}")
        try:
            # stderr as bytes (not text=True): the locale codec is GBK on Windows, which
            # would choke on UTF-8 bytes in the log (e.g. a non-ASCII filename).
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise RuntimeError(f"找不到 ffmpeg（{ffmpeg}）。请安装 FFmpeg 或在设置中指定路径。")
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"第 {i + 1} 段切割失败（引擎退出码 {proc.returncode}）："
                               + (err[-300:] if err else "（无错误输出）"))
        results.append({
            "name": name, "path": str(out_path),
            "size": out_path.stat().st_size, "duration": seg["duration"],
        })
        if on_log:
            on_log(f"第 {i + 1}/{n} 段完成：{name}")
        if on_progress:
            on_progress((i + 1) / n)

    return results
