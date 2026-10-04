"""TTS engine base — local Qwen3-TTS (worker helpers + shared orchestrator).

The heavy ML stack (torch + qwen-tts) lives in the shared project virtualenv
(``.venv``) but still runs as a one-shot subprocess; the backend process never
imports torch. Keeping the worker in a child process preserves the important
CUDA/model isolation without requiring a second Python environment. The engine
core is ``tts-engine/tts_worker.py`` (prepared by ``install_tts_env.ps1``).

This module is the shared base of the TTS-family stages (``tts_batch`` /
``merge``): interpreter + child-env resolution, and :func:`run_tts_subprocess` — the
one-shot subprocess orchestration every synthesis stage is built on (pump
threads, progress/log streaming, cooperative cancel/pause, child + temp cleanup).
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from ..core.paths import PROJECT_ROOT
from ..platform.gpu_scheduler.admission import gpu_permit, release_paused_tts
from ..platform.gpu_scheduler.config import load_config
from ..platform.gpu_scheduler.manager import GPUServiceManager

READY = True
NOT_READY_MSG = "TTS 引擎未就绪：请先运行 install_tts_env.ps1 安装共享的 .venv 环境。"

DEFAULT_LANGUAGE = "chinese"


def _venv_python() -> Path:
    """Interpreter of the shared project env (Windows ``Scripts`` / posix ``bin``)."""
    if os.name == "nt":
        return PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    return PROJECT_ROOT / ".venv" / "bin" / "python"


def resolve_engine() -> tuple[Path, Path]:
    """Return ``(python_exe, worker_script)``. Env overrides (``AUDIOTTS_PYTHON`` /
    ``AUDIOTTS_WORKER``) let tests point at a stub. Raises an actionable error if the
    engine is missing."""
    python = Path(os.environ.get("AUDIOTTS_PYTHON") or _venv_python())
    worker = Path(os.environ.get("AUDIOTTS_WORKER") or (PROJECT_ROOT / "tts-engine" / "tts_worker.py"))
    if not python.exists():
        raise RuntimeError("未找到 TTS 引擎解释器（.venv）。请先运行 install_tts_env.ps1 安装共享环境。")
    if not worker.exists():
        raise RuntimeError("未找到 TTS 引擎脚本 tts-engine/tts_worker.py。")
    return python, worker


def _kill_worker_tree(proc: subprocess.Popen) -> None:
    """Terminate the worker **and** everything it spawned.

    A plain ``proc.kill()`` (TerminateProcess on Windows) reaches only the worker
    itself; on a cancel mid-encode the worker's ffmpeg child would be orphaned
    and keep running (and holding its temp files) until the encode finished on
    its own. On Windows this therefore does a process-tree kill
    (``taskkill /F /T``) and falls back to the plain kill if that fails. POSIX
    behaviour is unchanged (plain kill of the child).
    """
    GPUServiceManager.cancel_tts(proc)


def _child_env() -> dict:
    """Environment for the isolated TTS child (see :func:`resolve_engine`).

    Inherits the parent environment but forces UTF-8 stdio. A separate CPython
    child would otherwise default its stdout to the Windows ANSI codepage (GBK on
    zh-CN), which mangles any non-ASCII text it prints — CJK character names in
    ``[progress]`` lines and CJK output paths in ``[result]``/``[segment]`` lines —
    before the parent decodes it as UTF-8. Forcing UTF-8 (and, via UTF-8 mode, the
    filesystem encoding) also keeps the path the child *reports* identical to the
    file it actually *wrote*, so the parent's existence check finds it.
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class WorkerWatchdogTimeout(RuntimeError):
    """The one-shot worker died on its *watchdog* exit code (a sub-batch produced no output
    within its budget and the process ``os._exit``'d). Distinct from a plain failure so a stage
    (``tts_batch``) can shrink the batch and restart a fresh subprocess instead of failing the
    whole task. ``run_tts_subprocess`` raises this when ``watchdog_code`` is set and matches the exit.
    """


class _SubprocessPaused(Exception):
    """The child was stopped for pause and should be relaunched after resume."""


class TTSStartupError(RuntimeError):
    """No synthesis was admitted; it is safe to retry model initialization."""


def run_tts_subprocess(*args, **kwargs) -> deque:
    """Run or restart a child across pauses, retaining its input files until exit."""
    cmd = args[0] if args else kwargs["cmd"]
    gpu = "--mode" in cmd and cmd[cmd.index("--mode") + 1] in {"batch", "design-batch"}
    temp_files = kwargs.pop("temp_files", ())
    startup_attempts = 0
    try:
        while True:
            try:
                if gpu:
                    handle = args[1] if len(args) > 1 else kwargs["handle"]
                    with gpu_permit("TTS", handle) as request_id:
                        return _run_tts_subprocess_once(*args, **kwargs, gpu_request_id=request_id)
                return _run_tts_subprocess_once(*args, **kwargs)
            except _SubprocessPaused:
                continue
            except TTSStartupError as exc:
                startup_attempts += 1
                config = load_config()
                if startup_attempts >= config.startup_retry_count:
                    from ..platform.gpu_scheduler.store import transaction
                    with transaction() as (_db, state):
                        state.update(state="ERROR", error=str(exc), reason="TTS startup attempts exhausted")
                    raise
    finally:
        for file in temp_files:
            try:
                Path(file).unlink(missing_ok=True)
            except OSError:
                pass


def _run_tts_subprocess_once(cmd: list, handle, on_line, *, temp_files=(), fail_prefix: str = "TTS 引擎",
              watchdog_code: int | None = None,
              log_file: Path | None = None, log_line=None,
              interrupt_on_pause: bool = False, gpu_request_id: str | None = None) -> deque:
    """Run a one-shot shared-``.venv`` worker and stream its output into a Task.

    Shared orchestration for the TTS-family stages (batch synthesis / merge):
    spawn the child (UTF-8 forced, project root as cwd), pump stdout/stderr from
    reader threads into the main loop, honour cooperative cancel/pause via
    ``handle.check()`` (the child is killed in ``finally``), mirror stderr into the
    task log at WARNING level, then clean up the child and any ``temp_files``.

    Cancel is checked **per line** as the output drains (not only once per drain
    cycle): on a loaded machine (AV scan / disk contention) each line's processing
    is slow, so the queues can hold a large backlog while the worker runs at full
    speed — a cancel clicked meanwhile must not wait for that backlog to drain
    (minutes) before being seen; the per-line check bounds the cancel latency to a
    single line's worth of processing. A pause halts the drain at the next line
    likewise, and cancel still wins (``check()`` re-tests it every 0.1 s). The run
    mirror (``log_file``) is block-buffered — a line-buffered mirror would pay one
    OS write (plus the AV-scan tax) per line on the hot path — and flushes at the
    attempt's end.

    * ``[progress] <frac> <label>`` stdout lines are reported via
      ``handle.progress`` here; every other non-empty stdout line is passed to
      ``on_line`` (decoded, stripped) for stage-specific parsing.
    * When ``log_file`` is given, the whole transcript is mirrored to that file
      (append mode, line-buffered) — stdout lines as ``[out] …``, stderr lines as
      ``[err] …``, bracketed by ``=== attempt started/ended ===`` markers — so a
      run leaves a persistent, on-disk trail (the task events remain queryable in the durable task history). ``None`` (the default) changes nothing.
    * ``log_line``, when supplied, transforms stdout lines before they are mirrored to
      ``log_file``; returning ``None`` filters a line. The Task still receives the original
      line through ``on_line`` so a stage can parse structured worker events without exposing
      them in the user-facing log.
    * A non-zero exit raises ``RuntimeError(f"{fail_prefix}失败（退出码 N）…")`` — except that,
      when ``watchdog_code`` is set and the child exits with exactly that code, a
      :class:`WorkerWatchdogTimeout` is raised instead (so a batch stage can shrink the batch
      and restart a fresh subprocess rather than failing the whole task).

    Returns the rolling stderr tail for post-run validation.
    """
    try:
        proc = GPUServiceManager.spawn_tts(cmd, gpu_request_id, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          cwd=str(PROJECT_ROOT), env=_child_env())
    except FileNotFoundError:
        raise RuntimeError(f"无法启动 TTS 引擎：{cmd[0]}")

    out_q: "queue.Queue" = queue.Queue()
    err_q: "queue.Queue" = queue.Queue()
    stderr_tail: deque = deque(maxlen=40)

    # Persistent run mirror: the task history persists selected events, while this file keeps the raw child transcript
    # for diagnosis after a failed run. When a stage names a log file, every stdout line
    # ([out]), stderr line ([err]) and the attempt boundary markers are also appended there
    # (line-buffered, append mode) — each restart attempt appends its own section, so the
    # file is the forensic record of what the engine actually said before it died.
    # Block-buffered (NOT line-buffered): the mirror sits on the hot path — every line the
    # worker emits is written here while the main loop drains the queues. Line buffering made
    # that one OS write (and one real-time-AV-scan hit) per line, which on a loaded machine
    # stalled the loop that must stay responsive to cancel. The block flushes at the attempt's
    # end (the close in finally); a hard backend kill loses at most the buffer — the same
    # order of loss as the undrained queues already have.
    run_log = None
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            run_log = open(log_file, "a", encoding="utf-8", buffering=64 * 1024)
            run_log.write(f"\n=== attempt started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            run_log.write("cmd: " + " ".join(str(c) for c in cmd) + "\n")
        except OSError:
            run_log = None

    def _pump(stream, q: "queue.Queue") -> None:
        try:
            for raw in iter(stream.readline, b""):
                q.put(raw)
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.put(None)  # EOF sentinel — guarantees the reader loop terminates

    threading.Thread(target=_pump, args=(proc.stdout, out_q), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr, err_q), daemon=True).start()

    out_done = err_done = False
    pause_requested = False
    pause_retry = False
    require_ready = gpu_request_id is not None and load_config().enabled
    ready = not require_ready
    ready_deadline = time.monotonic() + load_config().startup_timeout if require_ready else 0

    def check() -> None:
        nonlocal pause_requested, pause_retry
        if interrupt_on_pause and hasattr(handle, "check_interruptible"):
            def stop_for_pause() -> None:
                nonlocal pause_requested
                pause_requested = True
                if proc.poll() is None:
                    _kill_worker_tree(proc)
                GPUServiceManager.finish_tts(proc)
                release_paused_tts(gpu_request_id)
            handle.check_interruptible(stop_for_pause)
            if pause_requested:
                pause_retry = True
                raise _SubprocessPaused()
        else:
            handle.check()

    try:
        while True:
            check()  # cooperative cancel/pause
            if not ready and time.monotonic() >= ready_deadline:
                raise TTSStartupError("TTS 模型加载及 warmup 就绪检查超时")
            try:
                while True:
                    raw = out_q.get_nowait()
                    if raw is None:
                        out_done = True
                        break
                    # Per-line cancel point: the drain runs until the queue is empty, and the
                    # top-of-loop check below is only reached AFTER the whole backlog is gone —
                    # on a loaded machine that can be minutes, which made a clicked cancel
                    # look like "it never stops". Checking per line bounds the latency to one
                    # line (a pause parked here likewise; cancel still wins the busy-wait).
                    check()
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    if line in {"[ready] tts", "[noop] tts"}:
                        ready = True
                        GPUServiceManager.tts_ready(gpu_request_id)
                    elif line.startswith("[progress]"):
                        parts = line.split(None, 2)
                        try:
                            frac = float(parts[1])
                        except (ValueError, IndexError):
                            frac = 0.0
                        handle.progress(frac, parts[2] if len(parts) > 2 else "")
                    else:
                        on_line(line)
                        if run_log:
                            mirrored = log_line(line) if log_line is not None else line
                            if mirrored is not None:
                                run_log.write(f"[out] {mirrored}\n")
            except queue.Empty:
                pass
            try:
                while True:
                    raw = err_q.get_nowait()
                    if raw is None:
                        err_done = True
                        break
                    check()  # per-line cancel point (same rationale as the stdout drain)
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        stderr_tail.append(line)
                        handle.log(line, "WARNING")
                        if run_log:
                            run_log.write(f"[err] {line}\n")
            except queue.Empty:
                pass
            if proc.poll() is not None and out_done and err_done:
                break
            time.sleep(0.15)
    finally:
        if proc.poll() is None:
            _kill_worker_tree(proc)
        GPUServiceManager.finish_tts(proc)
        if run_log:
            run_log.write(
                f"=== attempt ended rc={proc.returncode} "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
            run_log.close()
        for p in (proc.stdout, proc.stderr):
            try:
                p.close()
            except Exception:  # noqa: BLE001
                pass
        if not pause_retry:
            for f in temp_files:
                try:
                    Path(f).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass

    if proc.returncode != 0:
        tail = " | ".join(stderr_tail)[-500:]
        msg = (f"{fail_prefix}失败（退出码 {proc.returncode}）"
               + (f"：{tail}" if tail else "（无错误输出）"))
        if not ready:
            raise TTSStartupError(msg)
        if watchdog_code is not None and proc.returncode == watchdog_code:
            raise WorkerWatchdogTimeout(msg)
        raise RuntimeError(msg)
    if not ready:
        raise TTSStartupError("TTS 子进程退出前未报告模型就绪")
    return stderr_tail

