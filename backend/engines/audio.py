"""Audio duration probing (native ``ffprobe``).

Merge, music-library and BGM code ask for a file's duration through
:func:`probe_duration`: successful results are cached across API/Worker
processes (``core.audio_probe_cache``) and the subprocess honours the Worker's
task-cancellation registry when a claim is bound.
"""
from __future__ import annotations

import subprocess


def probe_duration(path, ffprobe_path: str = "", timeout: float = 120.0):
    """Stable successful ffprobe durations, persisted across API/Worker processes."""
    from ..core.audio_probe_cache import cached_duration
    return cached_duration(path, ffprobe_path,
        lambda: _probe_duration_uncached(path, ffprobe_path, timeout), timeout=timeout)


def _probe_duration_uncached(path, ffprobe_path: str = "", timeout: float = 120.0):
    """Duration in seconds via ``ffprobe``.
    Returns ``(duration, error)`` — ``nan`` on failure. ``timeout`` bounds the ffprobe
    subprocess (default 120s); callers on a hot synchronous path may pass a shorter cap."""
    ffprobe = ffprobe_path or "ffprobe"
    from ..core.audio_probe_cache import PARAMETERS
    cmd = [ffprobe, *PARAMETERS, str(path)]
    try:
        from ..platform.mechanical_audio import has_bound_claim, run_registered
        runner = run_registered if has_bound_claim() else subprocess.run
        proc = runner(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
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
