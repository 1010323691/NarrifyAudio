"""Qwen3-TTS synthesis worker.

Runs in the shared project virtualenv (``.venv``) as a one-shot
subprocess, spawned by the app backend — see ``backend/engines/tts.py`` and the
sibling ``backend/engines/tts_batch.py`` / ``merge.py``. Keeping the heavy ML
stack in a child process is deliberate: ``qwen-tts`` pulls a large dependency tree,
and keeping model loading in a child preserves the FastAPI process's memory and
CUDA lifecycle boundaries.

Ported (minimal core, not the whole app) from ``alexandria-audiobook``:
  - model resolve / load      (tts.py  _resolve_local_model_path / _init_local_*)
  - custom inference          (tts.py  _local_generate_custom)
  - clone inference           (tts.py  _get_clone_prompt / _local_generate_clone)
  - design inference          (tts.py  generate_voice_design / generate_design_voice)
  - WAV save                  (tts.py  _save_wav)  + WAV->MP3 (project.py, via pydub)
  - merge timeline + combine  (combine_audio_with_pauses)

Modes (``--mode``) — every run is a backend-dispatched one-shot subprocess
----------------------------------------------------------------------
  batch              all segments in a file, one subprocess, needed models loaded once;
                     segments are padded into native tensor batches; --concurrency is only the
                     fixed row ceiling after sorting by length; no automatic length/VRAM
                     downshifting. Timeout recovery temporarily halves the cap and restores it later.
  design-batch       all VoiceDesign candidates in a file, one subprocess, the design model
                     loaded once; every candidate (short ref text + its own voice description)
                     runs as a native tensor sub-batch like batch mode — the 角色配音·克隆
                     engine; each settles through a [design] line, output is WAV per candidate
  merge              two-stage: per-segment files -> part WAVs (batches of
                     --merge-batch-size, staged in --tmp-dir) -> the final audiobook;
                     every stage reports live progress

Contract with the backend (all on STDOUT unless noted)
------------------------------------------------------
  - ``[progress] <0.0-1.0> <label>`` -> backend calls handle.progress(frac, label)
  - ``[result]   <absolute path>``    -> the primary file produced
  - ``[segment]  <index> ok <path>``  -> one batch segment succeeded
  - ``[segment]  <index> error <reason>`` -> one batch segment failed (batch continues)
    (``index`` = the row's segment-table position — in pooled multi-file runs that is the
    pool-global index; a row may additionally carry ``out_dir`` / ``file_index`` deciding
    its chapter package dir and file number, see _row_output_paths)
  - ``[design]   <index> ok <seed> <path>`` -> one design candidate rendered (design-batch;
    index = the row's position in the jobs file; seed = that sub-batch's seed)
  - ``[design]   <index> error <reason>`` -> one design candidate failed (run continues)
  - ``[watchdog] timeout batch=<n> indices=[...] elapsed=<s>`` -> a sub-batch produced no
    output within its budget; the process exits 124. The backend halves the cap and restarts.
  - ``[restore] cap=<N>`` -> two successful batches have completed at the temporary
    demoted cap; restore one pending demotion level and synchronize the backend.
  - any other line                    -> forwarded as a log entry
  - exit 0 on success; non-zero on failure, error on STDERR.

Batch mode returns 0 even if individual segments failed (they are reported via the
``[segment]`` lines); it returns non-zero only on a *fatal* setup error (missing /
unreadable voice config, empty segment list, model load failure, ...).
"""
from __future__ import annotations

import argparse
import contextlib
import gc
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

# Direct script execution puts only tts-engine on sys.path. The shared limits
# module is stdlib-only: importing it never loads API, database or model code.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.core.tts_batch_limits import AUTO_BATCH_MAX, AUTO_BATCH_POINTS
from backend.core.pcm_stream import StreamingMerge, WaveInput

# transformers reconfigures its root logger on first import: it resets the level to
# WARNING, attaches its own stderr handler and disables propagation — which stomps any
# ``logging.getLogger("transformers").setLevel(...)`` set earlier in this process (the
# ML stack is imported lazily, always after ``main()``). The TRANSFORMERS_VERBOSITY env
# var is read at exactly that reconfiguration moment, so it is the reliable way to keep
# the benign "Setting `pad_token_id` to `eos_token_id` ... for open-end generation"
# WARNING (emitted on every ``generate()``) out of the live log. ``setdefault`` lets an
# explicit value (e.g. "info" while debugging) still win.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

# Model ids. The three Qwen3-TTS 1.7B variants share the same loader; only the
# suffix (CustomVoice / Base / VoiceDesign) selects the behaviour.
DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
DEFAULT_BASE_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
DEFAULT_DESIGN_MODEL = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
DEFAULT_SPEAKER = "serena"
DEFAULT_LANGUAGE = "chinese"

SUPPORTED_TYPES = ("custom", "clone", "design")

# Per-row generation cap used by the production TTS modes.
MAX_NEW_TOKENS = 2048

# Length-proportional decode cap (``max_new_tokens_for_chars``). This qwen_tts build's
# talker does NOT stop individual rows early in a multi-row tensor batch: the per-row
# EOS is not emitted within the cap, so the whole batch runs the FULL max_new_tokens in
# lockstep (observed 2026-09-17: a 31-row batch of 2-6-char lines ran 2048 steps,
# 8-15 min, blowing the sub-batch watchdog budget and hanging one-click synthesis;
# single-row calls DO stop at EOS). The cap therefore scales with the sub-batch's
# longest row: a capped runaway still fails fast, and the model's own per-row EOS
# truncation keeps any audio the model did stop clean, while long rows (which the
# length bands already shrink to few-row batches) keep the full cap. Calibrated from
# real probes at 12 frames/s: legitimate length is ~2.7 frames/char (4.5 chars/s);
# 6/char leaves 2x headroom for slow delivery; the floor covers ultra-short lines.
# A row of >= ~342 chars maps to the full 2048 (behaviour unchanged for long rows).
FRAME_CAP_PER_CHAR = 6
FRAME_CAP_FLOOR = 128


def max_new_tokens_for_chars(chars: int) -> int:
    """The decode cap for a sub-batch whose longest row is ``chars`` chars long."""
    return min(MAX_NEW_TOKENS, max(FRAME_CAP_FLOOR, int(chars) * FRAME_CAP_PER_CHAR))

# Live "still generating" heartbeat (see run_with_watchdog): the first line ~FIRST seconds in,
# then one every INTERVAL seconds while a sub-batch decodes. It reports *measured* elapsed time
# against the watchdog budget — never a fabricated percentage (true fractional progress is unknown
# mid-batch) — so a long sub-batch doesn't go silent in the log and the timeout budget stays legible.
HEARTBEAT_FIRST = 10.0     # seconds before the first heartbeat (shorter batches finish before it)
HEARTBEAT_INTERVAL = 20.0  # seconds between subsequent heartbeats

# Sub-batch watchdog budget (see sub_batch_timeout_seconds): GPU batches use a conservative
# 100 chars/sec baseline. The floor keeps a small/contended batch alive; the cap bounds a
# pathological batch. (CPU keeps a looser, floor-dominated budget inside the function.)
GPU_TIMEOUT_CHARS_PER_SEC = 100
GPU_TIMEOUT_FLOOR_S = 60     # a small batch / contended startup still gets real decode time
GPU_TIMEOUT_CAP_S = 3600     # a pathological batch can't push the wait past an hour
FORCED_TIMEOUT_CHARS_PER_SEC = 50
FORCED_TIMEOUT_FLOOR_S = 120
WATCHDOG_VRAM_PRESSURE_FRAC = 0.95
RESTORE_AFTER_SUCCESSFUL_BATCHES = 2

# Two-stage merge (merge mode): per-batch part WAVs are staged first, then folded into
# the whole book, so merging thousands of segments reports live progress throughout
# instead of going silent. RE_FFMPEG_TIME is the port of backend/engines/audio.py's
# RE_TIME (permissive across ffmpeg 4.x/5.x/6.x).
RE_FFMPEG_TIME = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
ENCODE_PROGRESS_MIN_STEP_PCT = 1.0    # refresh the encode-progress line at most every ...
ENCODE_PROGRESS_MIN_INTERVAL_S = 2.0  # ... 1% of the encode or 2 seconds, whichever is later

# Mechanical post-processing (WAV write + MP3 encode) is deliberately conservative: ffmpeg
# is an external CPU process and several workers can otherwise oversubscribe both CPU and disk.
# The bounded queue is the backpressure mechanism between GPU production and this stage.
MECHANICAL_MAX_WORKERS = 4
MECHANICAL_QUEUE_MULTIPLIER = 2
MECHANICAL_QUEUE_MAX = 8
MECHANICAL_RETRIES = 1


def log(msg: str) -> None:
    """A plain status line -> backend forwards it as a log entry."""
    print(msg, flush=True)


def progress(frac: float, label: str) -> None:
    """A tagged progress line -> backend maps it to handle.progress()."""
    print(f"[progress] {max(0.0, min(1.0, frac)):.2f} {label}", flush=True)


def _segment_ok(index: int, path: str) -> None:
    print(f"[segment] {index} ok {path}", flush=True)


def _segment_error(index: int, reason: str) -> None:
    # Collapse whitespace so a traceback-ish reason stays on one log line.
    reason = " ".join(str(reason).split())
    print(f"[segment] {index} error {reason}", flush=True)


def _available_memory_bytes() -> int | None:
    """Best-effort physical-memory reading without adding a runtime dependency."""
    try:
        if os.name == "nt":
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
        else:
            pages = os.sysconf("SC_AVPHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return int(pages) * int(page_size)
    except (AttributeError, OSError, ValueError):
        return None
    return None


def mechanical_worker_count(*, cpu_count: int | None = None,
                            available_memory: int | None = None) -> int:
    """Choose a conservative post-processing worker count from live host capacity.

    Each consumer may launch ffmpeg and hold one decoded audio array, so this is capped at
    half the logical CPUs / four workers and reduced on low-memory hosts. Disk pressure is
    handled at runtime by the bounded queue: a slow encode/write stage fills it and blocks the
    GPU producer instead of allowing unbounded audio arrays to accumulate.
    """
    cpu = max(1, int(cpu_count if cpu_count is not None else (os.cpu_count() or 4)))
    workers = max(1, min(MECHANICAL_MAX_WORKERS, cpu // 2))
    memory = (available_memory if available_memory is not None
              else _available_memory_bytes())
    if memory is not None:
        if memory < 4 * 2**30:
            workers = 1
        elif memory < 8 * 2**30:
            workers = min(workers, 2)
    return workers


class _MechanicalPipeline:
    """Bounded producer-consumer pipeline for post-inference audio processing.

    The producer remains the model thread: once a whole tensor sub-batch returns, each row is
    placed into this queue and the next GPU sub-batch can start immediately. Consumers perform
    only file I/O / WAV encoding / MP3 encoding and report each settled row through the existing
    callback. ``close(drain=True)`` is the normal finish path; ``drain=False`` is reserved for
    an unrecoverable shutdown and discards queued-but-not-started work after joining consumers.
    """

    def __init__(self, report, *, worker_count: int | None = None, queue_size: int | None = None):
        self.report = report
        self.worker_count = worker_count or mechanical_worker_count()
        self.queue_size = queue_size or max(
            self.worker_count,
            min(MECHANICAL_QUEUE_MAX, self.worker_count * MECHANICAL_QUEUE_MULTIPLIER),
        )
        self._queue: queue.Queue = queue.Queue(maxsize=self.queue_size)
        self._stop = threading.Event()
        self._closed = False
        self._close_lock = threading.Lock()
        self._batch_lock = threading.Lock()
        self._batches: dict[str, dict] = {}
        self._threads = [
            threading.Thread(target=self._consume, name=f"tts-mechanical-{i + 1}", daemon=True)
            for i in range(self.worker_count)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, rows, results, *, out_dir, width, save_fn, batch_label,
                batch_seed, started, inference_seconds, total_chars, cuda_peaks) -> None:
        """Enqueue one generated sub-batch; block when consumers fall behind."""
        state = {
            "label": batch_label,
            "rows": len(rows),
            "chars": total_chars,
            "started": started,
            "inference_seconds": inference_seconds,
            "cuda_peaks": cuda_peaks,
            "remaining": len(rows),
        }
        with self._batch_lock:
            self._batches[batch_label] = state
        for row, result in zip(rows, results):
            self._queue.put((row, result, out_dir, width, save_fn, batch_seed, state))

    def _consume(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                if self._stop.is_set():
                    continue
                row, result, out_dir, width, save_fn, batch_seed, state = item
                try:
                    outcome = _save_one_with_retry(
                        save_fn, row, result, out_dir, width, batch_seed,
                    )
                    self.report(*outcome)
                except BaseException as exc:  # noqa: BLE001 — isolate unexpected consumer faults
                    try:
                        self.report(row["index"], False, f"机械处理失败：{exc}")
                    except BaseException:
                        pass
                finally:
                    self._finish_batch_item(state)
            finally:
                self._queue.task_done()

    def _finish_batch_item(self, state: dict) -> None:
        with self._batch_lock:
            state["remaining"] -= 1
            if state["remaining"] != 0:
                return
            self._batches.pop(state["label"], None)
        elapsed = max(0.0, time.perf_counter() - state["started"])
        print("[perf] " + json.dumps({
            "stage": "batch", "event": "end", "batch": state["label"],
            "rows": state["rows"], "chars": state["chars"],
            "inference_seconds": state["inference_seconds"],
            # Includes queue waiting plus the actual encode/write work. The explicit field
            # makes the producer-consumer wait visible instead of mislabelling it as inference.
            "encode_write_seconds": max(0.0, elapsed - state["inference_seconds"]),
            "queue_and_encode_seconds": max(0.0, elapsed - state["inference_seconds"]),
            "seconds": elapsed,
            "throughput_chars_per_sec": (state["chars"] / elapsed if elapsed > 0 else 0.0),
            **state["cuda_peaks"],
        }), flush=True)

    def close(self, *, drain: bool = True) -> None:
        """Stop consumers after draining normal work, or discard queued work on shutdown."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if not drain:
                self._stop.set()
                while True:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        break
                    else:
                        self._queue.task_done()
            for _ in self._threads:
                self._queue.put(None)
        self._queue.join()
        for thread in self._threads:
            thread.join(timeout=10)


@contextlib.contextmanager
def _silence_streams():
    """Temporarily redirect the process stdout/stderr (fd 1 & 2) to devnull.

    Importing the ML stack and calling ``from_pretrained`` spam benign,
    non-actionable notices that would otherwise clutter the live log: torchaudio's
    "SoX could not be found!" (→ stderr; SoX is never used — audio I/O goes through
    soundfile/pydub/ffmpeg) and qwen-tts' "flash-attn is not installed" (→ stdout;
    we simply take the slower manual-attention path). None of these indicate a fault,
    so they are dropped.

    Redirecting the *raw file descriptors* (not just ``sys.stdout``/``sys.stderr``)
    also swallows C-level writes (which is why the SoX notice, in GBK on zh-CN
    Windows, would otherwise reach the parent as mojibake). The descriptors are
    restored before any ``[progress]``/``[result]`` line is emitted, so real output —
    and real errors, which surface as Python exceptions — are never lost.
    """
    saved_out, saved_err = os.dup(1), os.dup(2)
    try:
        with open(os.devnull, "wb") as devnull:
            os.dup2(devnull.fileno(), 1)
            os.dup2(devnull.fileno(), 2)
            yield
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)


def resolve_device(pref: str) -> str:
    """Resolve an 'auto' (or explicit) device preference to a concrete device."""
    if pref and pref != "auto":
        return pref
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _resolve_local_model_path(model_id: str):
    """If the model snapshot is already in the HF cache, return its dir; else None."""
    from huggingface_hub import try_to_load_from_cache

    result = try_to_load_from_cache(model_id, "config.json")
    if isinstance(result, str):
        return os.path.dirname(result)
    return None


def load_model(model_id: str, device: str):
    """Load ``Qwen3TTSModel``, preferring a local cache hit; downloads on first run.

    The ``qwen_tts`` import and the ``from_pretrained`` call are the source of the
    benign library notices (SoX-missing, flash-attn-missing), so they run inside
    :func:`_silence_streams`. The human-readable status lines are emitted *before*
    the silenced block so they still reach the log.
    """
    import torch

    dtype = torch.bfloat16 if "cuda" in device else torch.float32
    load_kwargs = {"dtype": dtype}
    if device != "cpu":
        load_kwargs["device_map"] = device

    local_path = _resolve_local_model_path(model_id)
    if local_path:
        log(f"Loading model from local cache: {local_path}")
    else:
        log(f"Model not cached locally; downloading {model_id} (first run, a few GB)...")

    with _silence_streams():
        from qwen_tts import Qwen3TTSModel  # import-time notices (SoX) fire silenced
        if local_path:
            try:
                model = Qwen3TTSModel.from_pretrained(local_path, **load_kwargs)
            except Exception as e:  # incomplete snapshot -> fall back to a hub download
                log(f"Local cache load failed ({e}); retrying via model id (may download).")
                model = Qwen3TTSModel.from_pretrained(model_id, **load_kwargs)
        else:
            model = Qwen3TTSModel.from_pretrained(model_id, **load_kwargs)
    return model


def _save_wav(audio_array, sample_rate: int, output_path: str) -> None:
    import numpy as np
    import soundfile as sf

    if not isinstance(audio_array, np.ndarray):
        audio_array = np.array(audio_array)
    if audio_array.ndim > 1:
        audio_array = audio_array.flatten()
    sf.write(output_path, audio_array, sample_rate)


def _wav_to_mp3(wav_path: str, mp3_path: str) -> bool:
    """Convert WAV -> MP3 via pydub (needs ffmpeg on PATH). False if it can't."""
    from pydub import AudioSegment

    segment = AudioSegment.from_wav(wav_path)
    if len(segment) == 0:
        return False
    segment.export(mp3_path, format="mp3")
    # A broken ffmpeg (no libmp3lame) yields a tiny header-only file without raising.
    size = os.path.getsize(mp3_path) if os.path.exists(mp3_path) else 0
    if size < 1024:
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
        return False
    return True


def _add_ffmpeg_to_path(ffmpeg: str) -> None:
    """Let pydub find a user-provided ffmpeg (its directory must be on PATH)."""
    if ffmpeg:
        fdir = os.path.dirname(os.path.abspath(ffmpeg))
        if fdir:
            os.environ["PATH"] = fdir + os.pathsep + os.environ.get("PATH", "")


def _resolve_alias(speaker: str, voice_config: dict) -> str:
    """Voice hints never redirect synthesis to a different role."""
    return (speaker or "").strip()


def _build_clone_prompt(model, voice_data: dict, root: str, speaker: str):
    """Create (and the caller caches) a Base-model voice-clone prompt (port of
    ``TTSEngine._get_clone_prompt``). Raises if the reference is missing/invalid."""
    import soundfile as sf

    ref_audio_path = voice_data.get("ref_audio")
    ref_text = voice_data.get("ref_text")
    if not ref_audio_path or not ref_text:
        raise ValueError(f"Clone voice for '{speaker}' missing ref_audio or ref_text")
    if not os.path.isabs(ref_audio_path):
        ref_audio_path = os.path.join(root, ref_audio_path)
    if not os.path.exists(ref_audio_path):
        raise FileNotFoundError(f"Reference audio not found for '{speaker}': {ref_audio_path}")

    audio_array, sample_rate = sf.read(ref_audio_path)
    if audio_array.ndim > 1:  # ensure mono
        audio_array = audio_array.mean(axis=1)
    return model.create_voice_clone_prompt(
        ref_audio=(audio_array, sample_rate),
        ref_text=ref_text,
    )


def _needed_types(segments, voice_config: dict) -> set:
    """Which of the three models a batch actually requires (so we load only those)."""
    types = set()
    for seg in segments:
        canonical = _resolve_alias((seg.get("speaker") or "").strip(), voice_config)
        vd = voice_config.get(canonical) or {}
        if not vd:
            continue
        vt = vd.get("type", "custom")
        if vt in SUPPORTED_TYPES:
            types.add(vt)
    return types


def _load_models_for(needed: set, device: str, args):
    """Load exactly the models ``needed`` holds, each once, for the whole batch."""
    models = {}
    if "custom" in needed:
        log("Loading CustomVoice model…")
        models["custom"] = load_model(args.model, device)
    if "clone" in needed:
        log("Loading Base model (voice cloning)…")
        models["clone"] = load_model(args.base_model, device)
    if "design" in needed:
        log("Loading VoiceDesign model…")
        models["design"] = load_model(args.design_model, device)
    return models


# ---------------------------------------------------------------------------
# Batch: native tensor-batch planning + the per-sub-batch watchdog
# ---------------------------------------------------------------------------

def fixed_batches(rows, max_batch):
    """Keep the caller's length order; only the final batch may be underfilled."""
    size = max(1, int(max_batch))
    for start in range(0, len(rows), size):
        yield rows[start:start + size]




def next_fixed_batch(remaining, max_batch, restore_stack, restore_successes=None):
    """Plan one fixed batch and restore a demoted level after two successes.

    ``restore_successes`` mirrors ``restore_stack`` and is incremented by the caller only
    after a sub-batch completes successfully. The backend's restore records survive a
    watchdog restart, while this short-lived success counter deliberately resets: a fresh
    engine must prove the reduced size twice before returning to the old size. The caller
    pops/emits the record so the backend stays in sync.
    """
    rows = remaining[:max_batch]
    restored = None
    if restore_stack and restore_successes and restore_successes[-1] >= RESTORE_AFTER_SUCCESSFUL_BATCHES:
        _threshold, previous_cap = restore_stack[-1]
        trial = remaining[:previous_cap]
        if previous_cap > max_batch:
            rows, max_batch, restored = trial, previous_cap, previous_cap
    return rows, remaining[len(rows):], max_batch, restored




def sub_batch_timeout_seconds(device, total_chars, vtype="custom", speaker_count=1):
    """A sub-batch's watchdog budget: how long a hung batch may run before the worker
    sacrifices the process (``os._exit(124)``) (pure).

    GPU (cuda/mps): the budget is the batch's total text at the conservative decode rate of
    ``GPU_TIMEOUT_CHARS_PER_SEC`` (~100 chars/sec). It is floored (``GPU_TIMEOUT_FLOOR_S``) so a
    small or contended batch still gets real time, and capped (``GPU_TIMEOUT_CAP_S``) so a
    pathological batch cannot wait unboundedly. ``vtype`` does not change the GPU budget: short
    rows decode at roughly the same per-char rate whichever voice type renders them.

    CPU decode is far slower than GPU, so it keeps a much looser, floor-dominated budget — a
    slow-but-healthy CPU batch must not be killed. When one tensor sub-batch contains multiple
    speakers, the resulting budget is multiplied by that speaker count because each role carries
    its own clone / voice conditioning work.
    """
    total_chars = max(0, int(total_chars))
    speaker_count = max(1, int(speaker_count))
    if device == "cpu":
        base = max(600, min(10800, int(600 + 4 * total_chars)))
    else:
        base = max(GPU_TIMEOUT_FLOOR_S,
                   min(GPU_TIMEOUT_CAP_S, int(total_chars / GPU_TIMEOUT_CHARS_PER_SEC)))
    return base * speaker_count


def forced_sub_batch_timeout_seconds(total_chars):
    """Return the hard watchdog deadline after the normal timeout is tolerated."""
    return max(float(FORCED_TIMEOUT_FLOOR_S), max(0, int(total_chars)) /
               FORCED_TIMEOUT_CHARS_PER_SEC)


def _vram_usage_fraction(device):
    """Return total-device VRAM usage as a fraction, or ``None`` when unreadable."""
    total = _total_vram(device)
    free = _free_vram(device)
    if not total or free is None:
        return None
    return max(0.0, min(1.0, 1.0 - float(free) / float(total)))


def watchdog_timeout_reason(elapsed, normal_timeout, forced_timeout, vram_usage):
    """Classify a watchdog deadline without touching torch or process state."""
    if float(elapsed) < float(normal_timeout):
        return None
    if vram_usage is not None and float(vram_usage) > WATCHDOG_VRAM_PRESSURE_FRAC:
        return "vram"
    if float(elapsed) >= float(forced_timeout):
        return "forced"
    return "wait"






def auto_batch_cap_for_chars(n_chars):
    """Return the measured safety tier for a row's character count.

    Matching is upward: a row uses the first measured character ceiling that is
    greater than or equal to its length (for example, 10 -> 216 and 75 -> 114).
    Counts beyond the last measured ceiling use the last, most conservative tier.
    """
    n = max(0, int(n_chars))
    for char_ceiling, cap in AUTO_BATCH_POINTS:
        if n <= char_ceiling:
            return cap
    return AUTO_BATCH_POINTS[-1][1]


def _take_auto_batch(remaining, max_batch):
    """Take a prefix whose cap is selected by its eventual longest row."""
    limit = max(1, min(AUTO_BATCH_MAX, int(max_batch)))
    rows = []
    for row in remaining:
        if len(rows) >= limit:
            break
        longest = max((r.get("chars", 0) for r in rows), default=0)
        longest = max(longest, int(row.get("chars", 0)))
        if len(rows) >= auto_batch_cap_for_chars(longest):
            break
        rows.append(row)
    return rows or remaining[:1]


def next_auto_batch(remaining, max_batch, restore_stack, restore_successes=None):
    """Choose the next auto-tier batch, preserving watchdog recovery state."""
    rows = _take_auto_batch(remaining, max_batch)
    restored = None
    if restore_stack and restore_successes and restore_successes[-1] >= RESTORE_AFTER_SUCCESSFUL_BATCHES:
        _threshold, previous_cap = restore_stack[-1]
        if previous_cap > max_batch:
            rows, max_batch, restored = _take_auto_batch(remaining, previous_cap), previous_cap, previous_cap
    return rows, remaining[len(rows):], max_batch, restored


def restore_oom_cap_after_success(current_cap, restore_cap, successful):
    """Probe the pre-OOM ceiling once, after one completed reduced group."""
    if restore_cap > current_cap and successful > 0:
        print(f"[oom-restore] cap={restore_cap}", flush=True)
        return restore_cap, 0
    return current_cap, restore_cap


def order_speaker_groups(classified):
    """Per-key execution queues, ordered most-rows-first (pure, key-generic).

    ``classified`` maps ``key -> [row, ...]`` — in batch mode the key is
    ``(voice_type, canonical_speaker)``, so each role gets its own ordered queue. Queues
    are returned in the order each key FIRST appears (dict insertion order) for ties.
    Returns ``[(key, rows), ...]``:

      * queues are ordered by row count DESCENDING — the biggest queue runs first (the
        heaviest share of the run starts immediately: fastest visible progress); ties keep
        first-seen order (deterministic for a given input file);
      * each queue's rows are sorted by character count ascending, so a role's short rows
        run first and a normal sub-batch stays close in length. Only
        an explicitly collected tail candidate may add rows from another role.
    """
    groups = [(key, sorted(rows, key=lambda r: r["chars"]))
              for key, rows in classified.items() if rows]
    groups.sort(key=lambda g: -len(g[1]))  # stable: ties keep first-seen order
    return groups


def collect_mergeable_role_tails(queues, start, max_batch):
    """Collect same-type role tails that may share a multi-role sub-batch.

    The first queue is always the next role to process. Once it has fewer rows than the
    current planned cap, take only enough prefixes from later same-type queues to fill one
    planned batch. Later queues may be larger — only the current role must be at its tail.
    A merged candidate is sorted by length again; ordinary (non-tail) processing
    never crosses role boundaries.
    """
    cap = max(1, int(max_batch))
    if not 0 <= int(start) < len(queues):
        return None
    key, rows = queues[int(start)]
    if len(rows) >= cap:
        return None
    vtype = key[0]
    indices = [int(start)]
    merged = list(rows)
    slots = cap - len(rows)
    for index in range(int(start) + 1, len(queues)):
        other_key, other_rows = queues[index]
        if other_key[0] == vtype and other_rows and slots > 0:
            indices.append(index)
            merged.extend(other_rows[:slots])
            slots -= min(slots, len(other_rows))
            if slots == 0:
                break
    if len(indices) == 1:
        return None
    merged.sort(key=lambda row: row["chars"])
    return indices, merged


def count_batch_speakers(rows):
    """Return the number of distinct speakers represented by a planned sub-batch."""
    return len({row.get("speaker", "") for row in rows})


def planned_concurrency_for_rows(rows):
    """Halve the current planned row count only when the plan spans multiple speakers."""
    planned = len(rows)
    return max(1, planned // 2) if count_batch_speakers(rows) > 1 else planned




def run_with_watchdog(fn, timeout_s, batch_label, indices, *, n_rows=None, device=None,
                      forced_timeout_s=None):
    """Run ``fn()`` in a daemon thread; the main thread polls, beats, and kills on timeout.

    At the normal deadline, VRAM usage above 95% triggers the watchdog immediately. Lower or
    unavailable VRAM pressure extends the wait until ``forced_timeout_s``, which always triggers
    the watchdog if the generation is still running.

    ``fn`` runs out-of-line so a hung GPU kernel can't block this poll loop. While it runs, the
    (live) main thread emits a throttled *liveness heartbeat* — a measured "已用时 Xs / 预算 Ys"
    line, never a fabricated percentage (the true fractional progress is unknown mid-batch) — so a
    long sub-batch doesn't go silent in the log and the watchdog budget stays legible. On timeout
    we emit a ``[watchdog]`` line (flushed first, so the backend always sees it) and
    ``os._exit(124)`` — the process (and its CUDA context) dies, so the fault can't poison a later
    run; the backend sees exit 124 and shrinks the batch / restarts a fresh subprocess. A
    non-timeout fault (e.g. OOM) is re-raised so the caller can do its one in-process halving
    retry. Returns ``fn``'s result on success. ``n_rows`` (when given) is the segment count shown
    in each heartbeat.
    """
    box = {}
    start = time.monotonic()

    def _target():
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001 — surface any fault to the poller
            box["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    budget = max(1.0, float(timeout_s))
    forced_budget = max(budget, float(forced_timeout_s) if forced_timeout_s is not None else budget)
    deadline = start + budget
    forced_deadline = start + forced_budget
    heartbeat_budget = budget
    last_beat = -1.0  # sentinel: the first heartbeat is due at HEARTBEAT_FIRST, not last_beat+interval
    while t.is_alive():
        now = time.monotonic()
        if now >= deadline:
            usage = _vram_usage_fraction(device) if device else None
            reason = watchdog_timeout_reason(now - start, budget, forced_budget, usage)
            if reason == "wait":
                deadline = forced_deadline
                budget = forced_budget
                heartbeat_budget = forced_budget
                log(f"[watchdog] normal timeout batch={batch_label}，VRAM占用 "
                    f"{usage:.1%}，未超过 {WATCHDOG_VRAM_PRESSURE_FRAC:.0%}；"
                    f"继续等待至强制截止 {forced_budget:.0f}s")
                continue
            pressure = "unknown" if usage is None else f"{usage:.1%}"
            print(f"[watchdog] timeout batch={batch_label} indices={list(indices)} "
                  f"elapsed={now - start:.0f}s vram={pressure} reason={reason}", flush=True)
            os._exit(124)  # terminate the stalled process and its CUDA context
        # Real-time liveness heartbeat (measured elapsed, NOT a fake percentage).
        if n_rows:
            elapsed = now - start
            due = HEARTBEAT_FIRST if last_beat < 0 else last_beat + HEARTBEAT_INTERVAL
            if elapsed >= due:
                last_beat = elapsed
                log(f"子批 {batch_label}（{n_rows} 段）生成中… 已用时 {elapsed:.0f}s / 预算 {budget:.0f}s")
        time.sleep(0.5)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _start_vram_peak(device):
    if "cuda" not in str(device):
        return None
    try:
        import torch
        torch.cuda.reset_peak_memory_stats()
        return torch.cuda.memory_reserved()
    except Exception:
        return None




@contextlib.contextmanager
def bounded_vocoder(model, batch_size=0):
    """Decode independent utterances in smaller groups; do not split a waveform.

    Opt-in: padding shape changes can cause numerical differences. The wrapper is
    scoped to one single-threaded generation and never modifies site-packages.
    """
    tokenizer = getattr(getattr(model, "model", None), "speech_tokenizer", None)
    original = getattr(tokenizer, "decode", None)
    if batch_size <= 0 or not callable(original):
        yield
        return
    own = "decode" in getattr(tokenizer, "__dict__", {})
    def decode(encoded):
        if not isinstance(encoded, list) or len(encoded) <= batch_size:
            return original(encoded)
        # The talker has finished; release its unused allocator blocks before the
        # decoder requests its different workspace shapes. Under WDDM, waiting
        # for an allocation failure can allow paging before empty_cache runs.
        _clear_gpu_cache(str(getattr(tokenizer, "device", "cpu")))
        wavs, rate = [], None
        for start in range(0, len(encoded), batch_size):
            part, sr = original(encoded[start:start + batch_size])
            if len(part) != len(encoded[start:start + batch_size]):
                raise RuntimeError("Vocoder returned an incomplete batch")
            if rate is not None and rate != sr:
                raise RuntimeError("Vocoder sample rate changed between chunks")
            wavs.extend(part)
            rate = sr
        return wavs, rate
    tokenizer.decode = decode
    try:
        yield
    finally:
        if own:
            tokenizer.decode = original
        else:
            delattr(tokenizer, "decode")


def _clear_gpu_cache(device) -> None:
    """gc + ``torch.cuda.empty_cache()`` between sub-batches (CUDA only); a no-op off CUDA.

    Frees fragmented / cached memory so a later, larger batch can still allocate.
    """
    gc.collect()
    if "cuda" in device:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass




def _free_vram(device):
    """Currently free VRAM in bytes, or ``None`` off CUDA / on a torch fault."""
    if "cuda" not in device:
        return None
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        return int(torch.cuda.mem_get_info()[0])
    except Exception:  # noqa: BLE001
        return None


def _total_vram(device):
    """The GPU's total VRAM in bytes (0 off CUDA / on fault) for watchdog pressure checks."""
    if "cuda" not in device:
        return 0
    try:
        import torch
        if not torch.cuda.is_available():
            return 0
        return int(torch.cuda.mem_get_info()[1])
    except Exception:  # noqa: BLE001
        return 0


def _warmup(model, vtype, language, device) -> None:
    """One tiny generation before the first real batch (CUDA only) to absorb first-call
    overhead (kernels / allocator warm-up). Clone is
    skipped (it needs a prompt); a warm on any loaded model warms the shared GPU kernels."""
    if "cuda" not in device or vtype not in ("custom", "design"):
        return
    try:
        import torch
        if not torch.cuda.is_available():
            return
        with torch.no_grad():
            if vtype == "design":
                model.generate_voice_design(
                    text="你好", instruct="a clear, natural speaking voice", language=language,
                    non_streaming_mode=True, max_new_tokens=32)
            else:
                model.generate_custom_voice(
                    text="你好", language=language, speaker=DEFAULT_SPEAKER,
                    instruct="neutral", non_streaming_mode=True, max_new_tokens=32)
        log("warmup 完成（已吸收首次调用开销）")
    except Exception as e:  # noqa: BLE001 — warmup is best-effort, never fatal
        log(f"warmup 跳过（{e}）")


# ---------------------------------------------------------------------------
# Merge: two-stage plan + boundary pause (pure stdlib — unit-testable in the lean venv)
# ---------------------------------------------------------------------------

def plan_merge_batches(n_segments, batch_size):
    """Half-open (start, end) ranges over the segment list for the two-stage merge.

    ``n <= size`` yields a single batch (the fast path: one part, stage 2 is just a
    rename). Sizes clamp to >= 1 so a bogus batch size degrades to one-segment parts,
    never to an empty range or an infinite loop.
    """
    size = max(1, int(batch_size))
    n = max(0, int(n_segments))
    if n == 0:
        return []
    if n <= size:
        return [(0, n)]
    return [(k, min(k + size, n)) for k in range(0, n, size)]


def normalize_pause_ms(raw):
    """A ``pause_after`` value (ms) -> int, or None when absent / not numeric.

    A non-numeric stray ("" / "fast" / {}) degrades to "no override" (the speaker
    rule applies) instead of raising; negative values clamp to 0. Numeric values —
    the normal case — pass through unchanged, so behaviour on clean data is
    identical to the old raw-value handling.
    """
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def boundary_gap_ms(last_pause_after, last_speaker, first_speaker, pause_ms, same_ms):
    """The pause between two adjacent parts, computed from the same inputs a
    single-pass merge uses for that boundary: the earlier part's last segment's
    ``pause_after`` (if any) wins, else the same-speaker / speaker-change default
    applied to (last segment of part k, first segment of part k+1). Feeding these
    gaps back in as explicit overrides makes a two-stage merge insert exactly the
    gaps a one-pass merge would.
    """
    p = normalize_pause_ms(last_pause_after)
    if p is not None:
        return p
    return same_ms if first_speaker == last_speaker else pause_ms


def merge_stage1_frac(done, total):
    """0.05 -> 0.80 across stage 1 (decode + per-batch part export), by global
    segment index (clamped at the band end so the next band starts exactly where
    this one ends — float rounding can otherwise overshoot by a ulp)."""
    if total <= 0:
        return 0.05
    return min(0.80, 0.05 + 0.75 * (done / total))


def merge_stage2_frac(done, total):
    """0.80 -> 0.95 across stage 2 (part decode + whole-book combine), by part
    index (clamped at the band end)."""
    if total <= 0:
        return 0.80
    return min(0.95, 0.80 + 0.15 * (done / total))


def merge_encode_frac(frac):
    """0.95 -> 1.00 across the final MP3 encode, by ffmpeg's reported time fraction
    (clamped)."""
    f = max(0.0, min(1.0, float(frac)))
    return min(1.0, 0.95 + 0.05 * f)


# ---------------------------------------------------------------------------
# Merge: timeline + pause-aware combine (1:1 ports of tts.py)
# ---------------------------------------------------------------------------

def combine_audio_with_pauses(audio_segments, speakers, pause_ms=500,
                              same_speaker_pause_ms=250, pause_overrides=None):
    """Combine audio segments with pauses between them (port of ``tts.py``).

    ``pause_overrides[i]`` (ms, or None) is the pause inserted *after* segment i;
    the last entry is ignored. None falls back to the speaker-change default.
    """
    from pydub import AudioSegment

    if not audio_segments:
        return None

    combined = audio_segments[0]
    prev_speaker = speakers[0]

    for i, (segment, speaker) in enumerate(zip(audio_segments[1:], speakers[1:])):
        override = pause_overrides[i] if pause_overrides else None
        if override is not None:
            gap = AudioSegment.silent(duration=override)
        elif speaker == prev_speaker:
            gap = AudioSegment.silent(duration=same_speaker_pause_ms)
        else:
            gap = AudioSegment.silent(duration=pause_ms)
        combined += gap + segment
        prev_speaker = speaker

    return combined




# ---------------------------------------------------------------------------
# Mode implementations
# ---------------------------------------------------------------------------

def _workspace_root(args) -> str:
    """The root relative path values resolve against inside ``--voice-config`` /
    ``--segments-file``: the backend passes the live workspace root via ``--workspace``
    (the worker's own cwd is the project root, not the workspace). Legacy callers
    that pass nothing keep the old cwd-based behaviour.
    """
    ws = getattr(args, "workspace", "") or ""
    return os.path.abspath(ws) if ws else os.getcwd()


def _effective_instruct(r, vtype):
    """The instruct/description actually sent for a row (token counting must match generation)."""
    if vtype == "custom":
        return (r["instruct"] or (r["vd"].get("default_style") or "").strip() or "neutral")
    if vtype == "design":
        base = (r["vd"].get("description") or "").strip()
        if base and r["instruct"]:
            return f"{base}, {r['instruct']}"
        return base or r["instruct"] or "A clear, natural speaking voice"
    return ""  # clone: no instruct


def _generate_rows(model, vtype, rows, args, clone_prompts, batch_seed, *, force_do_sample=False):
    """The GPU generate for a sub-batch (the model's list API). Returns ``[(ok, payload), ...]``
    in row order. Runs in the watchdog's daemon thread; it never saves files or emits protocol
    lines (the producer hands the results to the mechanical pipeline after the watchdog returns;
    the pipeline serializes progress updates so the ``[segment]`` / ``[progress]`` stream stays
    monotonic).

    A sub-batch may mix CHARACTERS: the list API takes per-row speaker / instruct / clone-prompt
    values (the model builds each row's ICL prefill from its own reference — see
    ``modeling_qwen3_tts.generate``), so ``clone_prompts`` is a per-character cache and each
    row indexes its own character's prompt item. A single-character batch passes the same
    item on every row — the exact broadcast the old single-speaker code produced.

    ``force_do_sample`` is design-batch-only: sampling is forced on so a checkpoint whose
    generate_config disables it (greedy decode) cannot make identical-input candidates
    byte-identical. Batch mode never sets it — its behaviour is unchanged.
    """
    if batch_seed is not None:
        import torch

        torch.manual_seed(batch_seed)
    texts = [r["text"] for r in rows]
    # The decode cap scales with the batch's longest row (see max_new_tokens_for_chars):
    # in batch mode the model does not stop individual rows at EOS, so a fixed 2048 cap
    # turned short-row batches into full-cap runs (multi-minute hangs); rows that DO emit
    # EOS are truncated at it anyway, so a smaller cap only bounds the runaways.
    cap = max_new_tokens_for_chars(max((len(t) for t in texts), default=0))
    if vtype == "custom":
        speakers = [(r["vd"].get("voice") or args.speaker or DEFAULT_SPEAKER) for r in rows]
        instructs = [_effective_instruct(r, "custom") for r in rows]
        wavs, _sr = model.generate_custom_voice(
            text=texts, language=args.language, speaker=speakers, instruct=instructs,
            non_streaming_mode=True, max_new_tokens=cap)
    elif vtype == "clone":
        # per-row clone prompts: one tensor call may serve several characters, each row
        # conditioned on its own reference (the API matches prompt items to rows 1:1)
        prompt = [clone_prompts[r["speaker"]][0] for r in rows]
        wavs, _sr = model.generate_voice_clone(
            text=texts, voice_clone_prompt=prompt,
            non_streaming_mode=True, max_new_tokens=cap)
    else:  # design: each row carries its own description (a per-row instruct list —
        # batch mode's design sub-batches are size 1, where this is equivalent to the
        # old scalar; design-batch packs several rows per call, one shared forward)
        wavs, _sr = model.generate_voice_design(
            text=texts, instruct=[_effective_instruct(r, "design") for r in rows],
            language=args.language, non_streaming_mode=True, max_new_tokens=cap,
            **({"do_sample": True} if force_do_sample else {}))
    if not wavs:
        return [(False, "模型未返回音频")] * len(rows)
    return [(True, (w, _sr)) for w in wavs]


def _row_output_paths(r: dict, fallback_out_dir: str, width: int) -> tuple:
    """(mp3, wav) output paths for one row (pure).

    A row may carry its own save location for pooled multi-file runs: ``out_dir`` (the
    chapter package directory, absolute) and ``file_index`` (the line's position inside
    its chapter). Both are optional — when absent the row falls back to the whole-batch
    ``out_dir`` + the segment-table ``index``, so single-file runs stay byte-identical to
    the legacy behaviour. The file number is ``file_index + 1``; ``width`` only zero-pads
    it (zfill never truncates) — the manifest stores the real path, so file naming is
    cosmetic, never the attribution mechanism.
    """
    out_dir = r.get("out_dir") or fallback_out_dir
    num = int(r.get("file_index", r["index"])) + 1
    fname = str(num).zfill(width)
    return os.path.join(out_dir, fname + ".mp3"), os.path.join(out_dir, fname + ".wav")


def _save_and_report(rows, results, out_dir, width, report, batch_seed=None) -> None:
    """Save each generated row (wav -> mp3) and emit its ``[segment]`` line + progress.

    Runs synchronously after the watchdog returns when no pipeline is supplied, or in a
    mechanical consumer when the producer-consumer pipeline is enabled. A per-row save / encode
    fault is a recorded error, never a run abort (matching the old per-segment tolerance).
    ``batch_seed`` is unused here (the design-batch save variant records it per candidate).
    Save location per :func:`_row_output_paths` — a row's own ``out_dir`` / ``file_index``
    (pooled multi-file) wins over the whole-batch ``out_dir`` + segment index.
    """
    import numpy as np

    for i, r in enumerate(rows):
        index = r["index"]
        ok, payload = results[i]
        if not ok:
            report(index, False, payload if isinstance(payload, str) else str(payload))
            continue
        wav, sr = payload
        if not isinstance(wav, np.ndarray):
            wav = np.array(wav)
        if wav.size == 0:
            report(index, False, "模型返回空音频")
            continue
        out_mp3, wav_tmp = _row_output_paths(r, out_dir, width)
        parent = os.path.dirname(out_mp3)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            _save_wav(wav, sr, wav_tmp)
            if _wav_to_mp3(wav_tmp, out_mp3):
                produced = out_mp3
                try:
                    if os.path.exists(wav_tmp):
                        os.remove(wav_tmp)
                except OSError:
                    pass
            else:
                produced = wav_tmp  # MP3 unavailable -> keep the WAV
            report(index, True, produced)
        except Exception as e:  # noqa: BLE001 — one bad row must not kill the batch
            for p in (wav_tmp, out_mp3):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            report(index, False, str(e))


def _save_and_report_design(rows, results, out_dir, width, report, batch_seed) -> None:
    """Save each generated design candidate straight to its row's ``out`` path (WAV — the clone
    reference is consumed as WAV, no MP3 encode) and emit its ``[design]`` line + progress.

    The design-batch counterpart of :func:`_save_and_report`; same fault-tolerant contract (a
    per-row fault is a recorded error, never a run abort), with independent rows allowed to be
    processed concurrently by pipeline consumers. ``report`` gets
    ``(index, ok, payload)`` where payload is ``(batch_seed, abs_path)`` on success — the
    sub-batch's seed, recorded per candidate (candidates in one sub-batch share it) — or the
    reason string on failure. ``out_dir`` / ``width`` are unused (each row carries its own
    output path) but kept so the save hook matches one signature.
    """
    import numpy as np

    for r, (ok, payload) in zip(rows, results):
        index = r["index"]
        out = os.path.abspath(r["out"])
        if not ok:
            report(index, False, payload if isinstance(payload, str) else str(payload))
            continue
        wav, sr = payload
        if not isinstance(wav, np.ndarray):
            wav = np.array(wav)
        if wav.size == 0:
            report(index, False, "模型返回空音频")
            continue
        try:
            parent = os.path.dirname(out)
            if parent:
                os.makedirs(parent, exist_ok=True)
            _save_wav(wav, sr, out)
            report(index, True, (batch_seed, out))
        except Exception as e:  # noqa: BLE001 — one bad row must not kill the batch
            try:
                os.remove(out)
            except OSError:
                pass
            report(index, False, str(e))


def _generated_audio_is_nonempty(result) -> bool:
    """Whether a generated result is eligible for a transient save/encode retry."""
    if not result or not result[0]:
        return False
    try:
        audio = result[1][0]
        size = getattr(audio, "size", None)
        return int(size if size is not None else len(audio)) > 0
    except (IndexError, TypeError, ValueError):
        return False


def _save_one_with_retry(save_fn, row, result, out_dir, width, batch_seed):
    """Run an existing save hook for one row, retrying only transient mechanical failures."""
    retryable = _generated_audio_is_nonempty(result)
    last = (row["index"], False, "机械处理失败：未返回保存结果")
    for attempt in range(MECHANICAL_RETRIES + 1):
        outcomes = []

        def capture(index, ok, detail):
            outcomes.append((index, ok, detail))

        try:
            save_fn([row], [result], out_dir, width, capture, batch_seed)
        except BaseException as exc:  # noqa: BLE001 — isolate one consumer item
            outcomes = [(row["index"], False, f"机械处理失败：{exc}")]
        if outcomes:
            last = outcomes[-1]
        if last[1] or not retryable or attempt >= MECHANICAL_RETRIES:
            return last
        time.sleep(0.05 * (attempt + 1))
    return last


def _synth_sub_batch(model, vtype, rows, *, args, clone_prompts, device, seed, sub_counter,
                     out_dir, width, report, save=None, force_do_sample=False,
                     pipeline=None):
    """Generate + save + report a sub-batch, under its watchdog, with in-process halving retry.

    ``sub_counter`` is a mutable ``[int]`` (a global sequence shared across every sub-batch, so a
    fixed seed + fixed layout reproduces). On a non-timeout fault (e.g. OOM) the GPU cache is
    cleared and the batch split
    in half, and each half retried recursively (each under its own watchdog) down to size 1. A
    size-1 fault hands off to the backend (``os._exit(124)``) so the specific segment can be
    struck / isolated. Timeouts never retry in-process (a hang means the GPU context is suspect)
    — the watchdog already ``os._exit``'d.

    ``save`` is the post-watchdog save/report hook (``(rows, results, out_dir, width, report,
    batch_seed)``); it defaults to :func:`_save_and_report` (batch mode, wav -> mp3) — the
    design-batch mode passes :func:`_save_and_report_design` (WAV straight to each row's out
    path).
    """
    if model is None:
        for r in rows:
            report(r["index"], False, "所需模型未加载")
        return

    sub_counter[0] += 1
    label = f"{vtype}#{sub_counter[0]}"
    indices = [r["index"] for r in rows]
    batch_seed = (seed + sub_counter[0]) if seed >= 0 else None
    total_chars = sum(r["chars"] for r in rows)
    speaker_count = count_batch_speakers(rows)

    def _gen():
        with bounded_vocoder(model, getattr(args, "vocoder_batch_size", 0)):
            return _generate_rows(model, vtype, rows, args, clone_prompts, batch_seed,
                                  force_do_sample=force_do_sample)

    _start_vram_peak(device)
    started = time.perf_counter()
    print("[perf] " + json.dumps({"stage": "batch", "event": "start", "batch": label,
          "rows": len(rows), "chars": total_chars,
          "min_chars": min(r["chars"] for r in rows),
          "max_chars": max(r["chars"] for r in rows)}), flush=True)
    try:
        timeout_s = sub_batch_timeout_seconds(device, total_chars, vtype, speaker_count)
        forced_timeout_s = max(timeout_s, forced_sub_batch_timeout_seconds(total_chars))
        results = run_with_watchdog(
            _gen, timeout_s, label, indices, n_rows=len(rows), device=device,
            forced_timeout_s=forced_timeout_s)
    except Exception as e:  # noqa: BLE001 — a fault (e.g. OOM), not a timeout
        print("[perf] " + json.dumps({"stage": "batch", "event": "error",
              "batch": label, "error": str(e), "oom": "out of memory" in str(e).lower()}), flush=True)
        if getattr(args, "fixed_batch", False):
            raise
        if len(rows) == 1:
            # even a lone row failed -> hand off to the backend to strike / isolate it
            log(f"段 {indices[0] + 1} 生成失败（{e}）——交由后端隔离")
            _clear_gpu_cache(device)
            os._exit(124)
        log(f"子批 {label}（{len(rows)} 段）生成失败（{e}）——清空显存缓存并对半拆分重试")
        _clear_gpu_cache(device)
        mid = len(rows) // 2
        _synth_sub_batch(model, vtype, rows[:mid], args=args, clone_prompts=clone_prompts,
                         device=device, seed=seed, sub_counter=sub_counter,
                         out_dir=out_dir, width=width, report=report, save=save,
                         force_do_sample=force_do_sample, pipeline=pipeline)
        _synth_sub_batch(model, vtype, rows[mid:], args=args, clone_prompts=clone_prompts,
                         device=device, seed=seed, sub_counter=sub_counter,
                         out_dir=out_dir, width=width, report=report, save=save,
                         force_do_sample=force_do_sample, pipeline=pipeline)
        return

    inference_seconds = time.perf_counter() - started
    cuda_peaks = {}
    if "cuda" in str(device):
        try:
            import torch
            cuda_peaks = {"allocated_peak_bytes": torch.cuda.max_memory_allocated(),
                          "reserved_peak_bytes": torch.cuda.max_memory_reserved()}
        except Exception:
            pass
    audio_seconds = sum(len(payload[0]) / payload[1] for ok, payload in results if ok)
    save_fn = save if save is not None else _save_and_report
    print('[perf] {"stage":"encode_write","event":"start"}', flush=True)
    if pipeline is not None:
        pipeline.submit(
            rows, results, out_dir=out_dir, width=width, save_fn=save_fn,
            batch_label=label, batch_seed=batch_seed, started=started,
            inference_seconds=inference_seconds, total_chars=total_chars,
            cuda_peaks={"audio_seconds": audio_seconds, **cuda_peaks},
        )
        return
    save_fn(rows, results, out_dir, width, report, batch_seed)
    elapsed_seconds = time.perf_counter() - started
    print("[perf] " + json.dumps({"stage": "batch", "event": "end", "batch": label,
          "rows": len(rows), "chars": total_chars, "audio_seconds": audio_seconds,
          "inference_seconds": inference_seconds,
          "encode_write_seconds": elapsed_seconds - inference_seconds,
          "seconds": elapsed_seconds,
          "throughput_chars_per_sec": (total_chars / elapsed_seconds
                                        if elapsed_seconds > 0 else 0.0),
          **cuda_peaks}), flush=True)


def parse_restore_stack(raw: str) -> list:
    """``--restore-stack`` -> list of ``(timeout_chars, cap)`` pairs, oldest first (pure).

    Empty / omitted = no pending demotion record (the default). Each entry is
    ``<chars>:<cap>`` — the timed-out sub-batch's total chars and the per-batch cap that
    was in force just before that demotion — so the newest (LAST) record is the one a
    planned sub-batch is compared against first (LIFO one-step restore). Whitespace-tolerant.
    A malformed entry (missing / extra colon, non-integer, or a non-positive number) raises
    ``ValueError`` (the caller turns it into ``TTS_WORKER_ERROR`` + exit 2) — a typo must
    never silently drop a pending restore.
    """
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        fields = [f.strip() for f in part.split(":")]
        if len(fields) != 2:
            raise ValueError(f"malformed --restore-stack entry {part!r} (want <chars>:<cap>)")
        try:
            chars, cap = int(fields[0]), int(fields[1])
        except ValueError:
            raise ValueError(f"malformed --restore-stack entry {part!r} (want <chars>:<cap>)") from None
        if chars <= 0 or cap <= 0:
            raise ValueError(f"malformed --restore-stack entry {part!r} (both numbers must be positive)")
        out.append((chars, cap))
    return out


def run_planned_group(
    model, vtype, planned_rows, planned_concurrency, *,
    args, clone_prompts, device, seed, sub_counter, out_dir, width, report, pipeline,
    synth_sub_batch=_synth_sub_batch,
    clear_gpu_cache=_clear_gpu_cache,
    count_speakers=count_batch_speakers,
    log=log,
    planner=None,
) -> int:
    """Run one planner group, temporarily splitting multi-role groups if needed.

    Promoted from ``_run_batch`` for testability: synthesis, cache clearing, speaker
    counting, logging and the batch planner are injected (defaults are the real
    implementations, so production call sites are behavior-unchanged).
    """
    if not planned_rows:
        return 0
    if planner is None:
        planner = next_auto_batch if args.auto_batch else next_fixed_batch
    planned_speakers = count_speakers(planned_rows)
    if planned_speakers <= 1:
        log(f"子批（{vtype}）：{len(planned_rows)} 段（当前预定并发 {planned_concurrency}）")
        synth_sub_batch(
            model, vtype, planned_rows,
            args=args, clone_prompts=clone_prompts, device=device, seed=seed,
            sub_counter=sub_counter, out_dir=out_dir, width=width, report=report,
            pipeline=pipeline)
        clear_gpu_cache(device)
        return 1

    temporary_concurrency = planned_concurrency_for_rows(planned_rows)
    pending = list(planned_rows)
    successful_batches = 0
    while pending:
        half_rows, next_pending, _ignored_cap, _ignored_restore = planner(
            pending, temporary_concurrency, [])
        speaker_count = count_speakers(half_rows)
        log(f"子批（{vtype}）：{len(half_rows)} 段（当前预定并发 {planned_concurrency}，"
            f"临时实际并发 {temporary_concurrency}）"
            + (f"（{speaker_count} 个角色，超时预算 ×{speaker_count}）"
               if speaker_count > 1 else ""))
        try:
            synth_sub_batch(
                model, vtype, half_rows,
                args=args, clone_prompts=clone_prompts, device=device, seed=seed,
                sub_counter=sub_counter, out_dir=out_dir, width=width,
                report=report, pipeline=pipeline)
        except Exception:
            if len(half_rows) <= 1:
                raise
            previous = temporary_concurrency
            temporary_concurrency = max(1, temporary_concurrency // 2)
            pending = half_rows + next_pending
            log(f"警告：多角色子批失败：临时并发 {previous} → {temporary_concurrency}，"
                "保留未完成段继续重试")
            continue
        pending = next_pending
        successful_batches += 1
        clear_gpu_cache(device)
    log(f"多角色预定组已完成：恢复后续子批使用并发 {planned_concurrency}")
    return successful_batches










def _run_batch(args) -> int:
    """Synthesize every segment in ``--segments-file``, one subprocess.

    Loads only the models the batch needs, once; then runs the segments as **native tensor
    batches** (the model's list API pads a group of rows into one forward, so the GPU truly
    processes several segments at once) instead of the old one-segment-per-thread pool.
    ``--concurrency`` is the fixed batch ceiling. With ``--auto-batch``, the longest
    row selects a measured safety tier. A watchdog can temporarily halve a batch
    after a timeout and later restore its previous ceiling.

    Rows run in **per-role queues** keyed by ``(voice_type, canonical_speaker)``. The role
    with the most useful rows starts first, and each role's rows are sorted by character
    count ascending. When a role reaches a tail smaller than the current planned concurrency,
    a same-model role may fill that one tail batch; that multi-role batch uses half of the
    current planned concurrency and gets a watchdog budget multiplied by its speaker count.
    Files are still named by segment index, so generation order never affects the assembled
    audiobook. Each sub-batch runs under a watchdog that kills the process on a hang (exit 124)
    so the backend can shrink and restart. A per-segment fault (missing config, model error)
    is a recorded ``[segment]`` line, never a run abort.
    """
    import json as _json

    if not args.segments_file or not args.voice_config or not args.out_dir:
        print("TTS_WORKER_ERROR: batch mode needs --segments-file, --voice-config and --out-dir",
              file=sys.stderr, flush=True)
        return 2

    _add_ffmpeg_to_path(args.ffmpeg)

    try:
        with open(args.segments_file, "r", encoding="utf-8") as f:
            segments = _json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"TTS_WORKER_ERROR: cannot read segments file: {e}", file=sys.stderr, flush=True)
        return 2
    if not isinstance(segments, list) or not segments:
        print("TTS_WORKER_ERROR: segments file is empty or not a list", file=sys.stderr, flush=True)
        return 2

    try:
        with open(args.voice_config, "r", encoding="utf-8") as f:
            voice_config = _json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"TTS_WORKER_ERROR: cannot read voice config: {e}", file=sys.stderr, flush=True)
        return 2
    if not isinstance(voice_config, dict):
        print("TTS_WORKER_ERROR: voice config is not a JSON object", file=sys.stderr, flush=True)
        return 2

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    device = resolve_device(args.device)
    log(f"device = {device} · {len(segments)} 段待合成")

    needed = _needed_types(segments, voice_config)

    progress(0.02, "解析输入")
    if needed:
        log("需加载模型：" + "、".join(t for t in SUPPORTED_TYPES if t in needed))
    else:
        log("警告：没有任何角色匹配到声音配置——所有段都会失败。请先在「角色声音」页生成声音。")

    progress(0.04, "加载模型")
    models = _load_models_for(needed, device, args)
    progress(0.05, "模型就绪")
    log("模型就绪。")

    max_batch = max(1, min(AUTO_BATCH_MAX, int(args.concurrency)))
    seed = int(args.seed)
    log(f"{'自动' if args.auto_batch else '固定'}批内上限 {max_batch}；按角色段数从多到少、角色内字数从少到多组批，尾部不足时允许同类型角色合批")
    log("vocoder：每组 8 段，解码前释放闲置显存缓存")
    args.vocoder_batch_size = 8
    args.fixed_batch = True
    if seed >= 0:
        log(f"seed = {seed}（可复现：同输入 + 同 seed + 同批布局 → 相同结果）")

    # -- classify every segment: immediate errors, then per-role queues ------------------
    # The classification key is PER ROLE, (vtype, canonical): a role's rows share one voice
    # config entry, so its clone prompt is built once and reused across batches.
    # Scheduling keeps role queues separate except for tail filling.
    total = len(segments)
    # The backend precomputes the width from the LARGEST package's FULL segment count and
    # hands it via --width, so every package in a run shares one digit count and a resume /
    # watchdog restart re-derives the SAME width. (Deriving it from `total` = the pending
    # count instead made the width shrink as the job completed, mixing 000x/0000x names in
    # a package filled over several runs.) --width 0 (absent) falls back to the legacy
    # pending-based width for manual / other callers.
    width = int(args.width) if getattr(args, "width", 0) else max(4, len(str(total)))
    counts = {"completed": 0, "failed": 0}
    report_lock = threading.Lock()
    clone_prompts: dict = {}
    classified: dict = {}  # (vtype, canonical) -> [row, ...], first-seen order

    def report_result(index, ok, detail):
        """Emit the ``[segment]`` line + progress under one lock, keeping counters monotonic."""
        with report_lock:
            if ok:
                _segment_ok(index, detail)
                counts["completed"] += 1
            else:
                _segment_error(index, detail)
                counts["failed"] += 1
            done = counts["completed"] + counts["failed"]
            progress(0.05 + 0.95 * (done / total),
                     f"完成 {done}/{total} 段（成功 {counts['completed']} / 失败 {counts['failed']}）")

    for seg in segments:
        index = int(seg.get("index", 0))
        speaker = (seg.get("speaker") or "").strip()
        text = (seg.get("text") or "").strip()
        instruct = (seg.get("instruct") or "").strip()

        if not text:
            report_result(index, False, "空文本")
            continue

        canonical = _resolve_alias(speaker, voice_config)
        vd = voice_config.get(canonical) or {}
        if not vd:
            report_result(index, False,
                          f"缺少角色声音配置（{speaker or canonical}）——请先在「角色声音」页生成")
            continue
        vtype = vd.get("type", "custom")
        if vtype not in SUPPORTED_TYPES:
            report_result(index, False, f"不支持的声音类型：{vtype}")
            continue

        # Pooled multi-file runs tag each row with its chapter save location: ``out_dir``
        # (package dir) + ``file_index`` (line position in that chapter). Both optional —
        # single-file rows omit them and fall back to the whole-batch --out-dir + index.
        # ``index`` stays the row's scheduling identity (protocol / watchdog / filename
        # width); ``file_index`` only decides the file number and package.
        row = {"seg": seg, "index": index, "speaker": canonical, "vd": vd,
               "text": text, "instruct": instruct, "chars": len(text),
               "out_dir": (seg.get("out_dir") or None),
               "file_index": int(seg.get("file_index", index))}
        classified.setdefault((vtype, canonical), []).append(row)

    # -- per-character setup: clone prompts ----------------------
    # (classification stays per character — see the comment above the loop)
    for (vtype, canonical), rows in classified.items():
        if vtype == "clone":
            # each character's clone prompt is built once and reused across all of that
            # character's sub-batches — including the ones shared with other characters
            model = models.get("clone")
            if model is None:
                for r in rows:
                    report_result(r["index"], False, "Base 模型未加载")
                continue
            try:
                # ``voice_config[canonical]["ref_audio"]`` is workspace-relative in the new
                # format — resolve it against the workspace root the backend handed us.
                clone_prompts[canonical] = _build_clone_prompt(
                    model, voice_config[canonical], _workspace_root(args), canonical)
            except Exception as e:  # noqa: BLE001 — a bad reference poisons only this character's rows
                for r in rows:
                    report_result(r["index"], False, f"克隆提示构建失败：{e}")
                continue

    # -- the execution queues: roles with more useful rows first, each role shortest first
    # Different roles stay separate until a role's remaining tail is smaller than the current
    # planned concurrency; collect_mergeable_role_tails then permits a same-model tail merge.
    oom_restore_cap = max(0, int(getattr(args, "oom_restore_cap", 0)))
    queues = order_speaker_groups(classified)  # [((vtype, speaker), rows ascending), ...]

    if not queues:
        # nothing to generate (every segment was an immediate error) — report and finish
        print("[noop] tts", flush=True)
        progress(1.0, f"完成（成功 {counts['completed']} / 失败 {counts['failed']} / 共 {total}）")
        log(f"批量合成结束：成功 {counts['completed']}，失败 {counts['failed']}，共 {total} 段。"
            f"输出目录：{out_dir}")
        return 0

    (first_vtype, _first_speaker), _first_rows = queues[0]
    first_model = models.get(first_vtype)
    if first_model is not None:
        _warmup(first_model, first_vtype, args.language, device)
    print("[ready] tts", flush=True)

    sub_counter = [0]
    restore_stack = args.restore_stack
    # The demotion records survive a watchdog restart; the successful-batch counter does not.
    # A fresh engine must complete two reduced batches before probing the old concurrency.
    restore_successes = [0 for _ in restore_stack]
    pipeline = _MechanicalPipeline(report_result)
    log(f"机械后处理流水线：{pipeline.worker_count} 个消费者，队列上限 {pipeline.queue_size}；"
        "队列满时暂停继续推理")

    try:
        queue_states = [(key, list(rows)) for key, rows in queues]
        queue_pos = 0
        while queue_pos < len(queue_states):
            (vtype, _speaker), role_rows = queue_states[queue_pos]
            if not role_rows:
                queue_pos += 1
                continue
            model = models.get(vtype)
            merged = collect_mergeable_role_tails(queue_states, queue_pos, max_batch)
            if merged is not None:
                merged_indices, merged_rows = merged
                merged_ids = {row["index"] for row in merged_rows}
                for index in merged_indices:
                    key, rows = queue_states[index]
                    queue_states[index] = (
                        key, [row for row in rows if row["index"] not in merged_ids])
                remaining = merged_rows
                while remaining:
                    planner = next_auto_batch if args.auto_batch else next_fixed_batch
                    planned_rows, planned_remaining, _planned_cap, _planned_restore = planner(
                        remaining, max_batch, restore_stack, restore_successes)
                    successful = run_planned_group(
                        model, vtype, planned_rows, len(planned_rows),
                        args=args, clone_prompts=clone_prompts, device=device, seed=seed,
                        sub_counter=sub_counter, out_dir=out_dir, width=width,
                        report=report_result, pipeline=pipeline)
                    if restore_stack and restore_successes:
                        restore_successes[-1] += successful
                    if _planned_restore is not None:
                        restore_stack.pop()
                        restore_successes.pop()
                        max_batch = _planned_cap
                        print(f"[restore] cap={_planned_restore}", flush=True)
                    if oom_restore_cap > max_batch and successful > 0:
                        restore_stack.clear()
                        restore_successes.clear()
                    max_batch, oom_restore_cap = restore_oom_cap_after_success(
                        max_batch, oom_restore_cap, successful)
                    remaining = planned_remaining
                queue_pos += 1
                continue

            planner = next_auto_batch if args.auto_batch else next_fixed_batch
            rows_b, role_remaining, max_batch, restored = planner(
                role_rows, max_batch, restore_stack, restore_successes)
            queue_states[queue_pos] = ((vtype, _speaker), role_remaining)
            if restored is not None:
                restore_stack.pop()
                restore_successes.pop()
                print(f"[restore] cap={restored}", flush=True)
            successful = run_planned_group(
                model, vtype, rows_b, len(rows_b),
                args=args, clone_prompts=clone_prompts, device=device, seed=seed,
                sub_counter=sub_counter, out_dir=out_dir, width=width,
                report=report_result, pipeline=pipeline)
            if restore_stack and restore_successes:
                restore_successes[-1] += successful
            if oom_restore_cap > max_batch and successful > 0:
                restore_stack.clear()
                restore_successes.clear()
            max_batch, oom_restore_cap = restore_oom_cap_after_success(
                max_batch, oom_restore_cap, successful)
    finally:
        # Drain already-generated audio before the process settles. On an exceptional exit this
        # preserves completed files; the manifest/backend can safely resume anything not queued.
        pipeline.close(drain=True)

    progress(1.0, f"完成（成功 {counts['completed']} / 失败 {counts['failed']} / 共 {total}）")
    log(f"批量合成结束：成功 {counts['completed']}，失败 {counts['failed']}，共 {total} 段。"
        f"输出目录：{out_dir}")
    return 0


def _run_design_batch(args) -> int:
    """Render every VoiceDesign candidate in ``--segments-file``, one subprocess.

    The 角色配音·克隆 stage's counterpart of ``_run_batch``: the VoiceDesign model is loaded
    ONCE, then every candidate (short ref text + its own voice description) runs as native
    tensor sub-batches — unlike batch mode's design rows (book segments, one per sub-batch),
    candidates DO share a sub-batch, which is the point of this mode. The configured
    batch ceiling bounds one unified length-ascending queue across characters, so
    same-length candidates of
    different characters fill the cap together (short rows first: early progress + crash
    resilience); each row keeps its backend identity (``index`` = position in the jobs file)
    so the ``[design]`` lines and the watchdog's ``indices=`` map back after the sorting.

    Every candidate is seeded ``--seed + sub-batch#`` (rows in one sub-batch share the seed;
    the backend records it per candidate — the same layout-conditioned reproducibility as
    batch mode), and ``do_sample`` is forced on (a checkpoint whose generate_config disables
    sampling would make identical-input candidates byte-identical). Output is WAV per
    candidate (no MP3 encode) at the row's ``out`` path. Watchdog / in-process halving retry /
    exit-124 hand-off are the same as batch mode.

    Progress across backend watchdog restarts: the reported fraction starts at
    ``--done-offset`` / ``--total`` (the candidates already settled) and climbs to 1.0 as this
    attempt's rows settle — so a restart's model-load phase reports the settled level, not a
    backward jump to zero (failed rows re-queued by the backend still count as unsettled).
    """
    import json as _json

    if not args.segments_file or not args.out_dir:
        print("TTS_WORKER_ERROR: design-batch mode needs --segments-file and --out-dir",
              file=sys.stderr, flush=True)
        return 2

    _add_ffmpeg_to_path(args.ffmpeg)

    try:
        with open(args.segments_file, "r", encoding="utf-8") as f:
            jobs = _json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"TTS_WORKER_ERROR: cannot read jobs file: {e}", file=sys.stderr, flush=True)
        return 2
    if not isinstance(jobs, list) or not jobs:
        print("TTS_WORKER_ERROR: jobs file is empty or not a list", file=sys.stderr, flush=True)
        return 2

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    total = max(1, int(getattr(args, "total", 0) or len(jobs)))
    offset = max(0, min(int(getattr(args, "done_offset", 0)), total))
    base = offset / total  # fraction settled before this attempt (the restart-safe floor)
    done = 0  # rows settled so far in THIS attempt
    report_lock = threading.Lock()

    def report_result(index, ok, payload):
        """Emit the ``[design]`` line + progress under one lock."""
        nonlocal done
        with report_lock:
            done += 1
            settled = offset + done
            if ok:
                seed, path = payload
                print(f"[design] {index} ok {seed} {path}", flush=True)
            else:
                print(f"[design] {index} error {' '.join(str(payload).split())}", flush=True)
            progress(base + done / len(jobs) * (1.0 - base), f"完成 {settled}/{total} 候选")

    # Rows carry the backend's per-candidate identity (index = position in the jobs file) so
    # the protocol lines map back after the ascending-by-length sort below.
    rows = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        text = str(j.get("text") or "").strip()
        description = str(j.get("description") or "").strip() or "A clear, natural speaking voice"
        rows.append({"index": int(j.get("index", 0)), "sp": str(j.get("sp", "")),
                     "k": int(j.get("k", 1)), "vd": {"description": description},
                     "text": text, "instruct": "", "chars": len(text),
                     "out": str(j.get("out", ""))})

    progress(base, "解析输入")
    # Unrenderable rows are immediate errors, never a generation input (mirrors batch mode).
    gen_rows = []
    for r in rows:
        if r["text"] and r["out"]:
            gen_rows.append(r)
        else:
            report_result(r["index"], False, "空文本/缺输出路径")
    if not gen_rows:
        print("[noop] tts", flush=True)
        progress(1.0, f"完成（共 {total} 候选，无可渲染）")
        return 0

    device = resolve_device(args.device)
    log(f"device = {device} · {len(gen_rows)} 个候选待渲染")

    progress(base, "加载 VoiceDesign 模型")
    model = load_model(args.design_model, device)
    log("VoiceDesign 模型就绪。")
    _warmup(model, "design", args.language, device)
    print("[ready] tts", flush=True)

    max_batch = max(1, min(64, int(args.concurrency)))
    seed = int(args.seed)
    args.vocoder_batch_size = 8
    args.fixed_batch = True
    log(f"固定批内上限 {max_batch}；跨角色按长度排序后组批")
    remaining = sorted(gen_rows, key=lambda r: r["chars"])
    sub_counter = [offset]
    pipeline = _MechanicalPipeline(report_result)
    log(f"机械后处理流水线：{pipeline.worker_count} 个消费者，队列上限 {pipeline.queue_size}；"
        "队列满时暂停继续推理")
    try:
        for rows_b in fixed_batches(remaining, max_batch):
            log(f"子批（design）：{len(rows_b)} 行（批内上限 {max_batch}）")
            _synth_sub_batch(
                model, "design", rows_b,
                args=args, clone_prompts={}, device=device, seed=seed,
                sub_counter=sub_counter, out_dir=out_dir, width=max(4, len(str(total))),
                report=report_result, save=_save_and_report_design, force_do_sample=True,
                pipeline=pipeline)
            _clear_gpu_cache(device)
    finally:
        pipeline.close(drain=True)

    progress(1.0, f"完成（共 {total} 候选）")
    log(f"候选渲染结束：{offset + done}/{total} 候选已落定。输出目录：{out_dir}")
    return 0


def _new_windows_kill_job():
    """Best-effort Windows job object that kills its processes when this process's
    handles are closed — i.e. when the backend hard-kills the worker on cancel,
    which would otherwise leave the ffmpeg encoder orphaned (TerminateProcess does
    not reach children). Returns ``(job, kernel32)`` or ``(None, None)`` anywhere
    the setup fails or off-Windows; callers then rely on the explicit finally-kill
    alone, exactly as before. Never raises.
    """
    if os.name != "nt":
        return None, None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None, None

        class _BASIC_LIMIT(ctypes.Structure):
            # JOBOBJECT_BASIC_LIMIT_INFORMATION (64-bit layout, 40 bytes)
            _fields_ = [("ProcessMemoryLimit", ctypes.c_int64),
                        ("JobMemoryLimit", ctypes.c_int64),
                        ("BasicLimitInfo", ctypes.c_uint32),
                        ("_pad", ctypes.c_uint32),
                        ("PeakProcessMemoryUsed", ctypes.c_uint64),
                        ("PeakJobMemoryUsed", ctypes.c_uint64)]

        class _IO_COUNTERS(ctypes.Structure):
            # IO_COUNTERS (64-bit layout, 48 bytes)
            _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                        ("WriteOperationCount", ctypes.c_uint64),
                        ("OtherOperationCount", ctypes.c_uint64),
                        ("ReadTransferBytes", ctypes.c_uint64),
                        ("WriteTransferBytes", ctypes.c_uint64),
                        ("OtherTransferBytes", ctypes.c_uint64)]

        class _EXT_LIMIT(ctypes.Structure):
            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (64-bit layout, 120 bytes)
            _fields_ = [("Basic", _BASIC_LIMIT),
                        ("Io", _IO_COUNTERS),
                        ("MemoryLimit", ctypes.c_uint64),
                        ("ProcessMemoryLimit", ctypes.c_uint64),
                        ("PeakProcessMemoryUsed", ctypes.c_uint64),
                        ("PeakJobMemoryUsed", ctypes.c_uint64)]

        if ctypes.sizeof(_EXT_LIMIT) != 120:
            kernel32.CloseHandle(job)
            return None, None  # unexpected layout — a wrong-size call would fail anyway
        ext = _EXT_LIMIT()
        ext.Basic.BasicLimitInfo = 0x2  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        # 9 = JobObjectExtendedLimitInformation
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(ext),
                                                ctypes.sizeof(ext)):
            kernel32.CloseHandle(job)
            return None, None
        return job, kernel32
    except Exception:
        return None, None


@contextlib.contextmanager
def _merge_input(path, tmp_dir):
    """Read WAV directly; decode other supported media to disk, never a pipe buffer."""
    try:
        source = WaveInput(path)
    except ValueError:
        import tempfile
        from pydub import AudioSegment
        from pydub.utils import mediainfo_json
        info = mediainfo_json(path)
        streams = [stream for stream in info.get("streams", []) if stream.get("codec_type") == "audio"]
        if not streams:
            raise ValueError("No audio stream")
        stream = streams[0]
        bits = stream.get("bits_per_sample", 16)
        if stream.get("sample_fmt") == "fltp" and stream.get("codec_name") in ("mp3", "mp4", "aac", "webm", "ogg"):
            bits = 16
        codec = "pcm_u8" if bits == 8 else f"pcm_s{bits}le"
        with tempfile.TemporaryDirectory(prefix="decode-", dir=tmp_dir) as decoded:
            wav = os.path.join(decoded, "input.wav")
            errors = os.path.join(decoded, "ffmpeg.stderr")
            with open(errors, "wb") as stderr:
                completed = subprocess.run([AudioSegment.converter, "-y", "-i", path,
                                            "-acodec", codec, "-vn", "-f", "wav", wav],
                                           stdout=subprocess.DEVNULL, stderr=stderr)
            if completed.returncode:
                with open(errors, "rb") as stderr:
                    stderr.seek(max(0, os.path.getsize(errors) - 4096))
                    raise ValueError(stderr.read().decode(errors="replace"))
            yield WaveInput(wav)
    else:
        yield source


def _merge_stage1(segs, tmp_dir, pause_ms, same_ms, batch_size, total, root):
    """Stream per-segment PCM into bounded-memory batch WAVs."""
    plan = plan_merge_batches(total, batch_size)
    m = len(plan)
    parts = []
    skipped = 0
    for k, (start, end) in enumerate(plan, start=1):
        label = f"正在合并第 {k}/{m} 批"
        progress(merge_stage1_frac(start, total), label)
        log(f"{label}（段 {start + 1}–{end}，共 {end - start} 段）")
        part_path = os.path.join(tmp_dir, f"part_{k:03d}.wav")
        merged = StreamingMerge(part_path)
        first, last, count = None, None, 0
        try:
            for i in range(start, end):
                segment = segs[i]
                path = segment.get("path") or ""
                full = path if os.path.isabs(path) else os.path.join(root, path)
                if not path or not os.path.exists(full):
                    skipped += 1
                    continue
                # Enter separately: decoder failures retain legacy skip behavior;
                # a failure after append begins is fatal rather than a partial success.
                context = _merge_input(full, tmp_dir)
                try:
                    source = context.__enter__()
                except Exception as exc:
                    log(f"跳过无法读取的段 {segment.get('index')}（{os.path.basename(full)}）：{exc}")
                    skipped += 1
                    continue
                try:
                    gap = None if last is None else boundary_gap_ms(
                        last.get("pause_after"), last.get("speaker", ""), segment.get("speaker", ""), pause_ms, same_ms)
                    merged.append(source, gap)
                finally:
                    context.__exit__(None, None, None)
                if first is None:
                    first = segment
                last = segment
                count += 1
                if (i + 1) % 10 == 0 or i == end - 1:
                    progress(merge_stage1_frac(i + 1, total), f"{label} · 段 {i + 1}/{total}")
            if not count:
                log(f"第 {k}/{m} 批：整批跳过（无可读音频）")
                continue
            duration_ms = merged.finish()
        finally:
            merged.close()
        parts.append({"k": k, "path": part_path,
                      "first_speaker": first.get("speaker", ""),
                      "last_speaker": last.get("speaker", ""),
                      "last_pause_after": last.get("pause_after"),
                      "duration_ms": duration_ms, "n": count})
        progress(merge_stage1_frac(end, total), f"{label} · 段 {end}/{total}")
        log(f"第 {k}/{m} 批完成 → part_{k:03d}.wav（{duration_ms / 60000:.1f} 分钟，{count} 段）")
    if skipped:
        log(f"警告：{skipped} 段被跳过（文件缺失或无法读取）")
    if not parts:
        print("TTS_WORKER_ERROR: 没有可合并的音频段", file=sys.stderr, flush=True)
        return None
    return parts


def _merge_stage2(parts, tmp_dir, out_path, pause_ms, same_ms, m_plan):
    """Stream part WAVs, preserving each explicit inter-part boundary gap."""
    final_wav = os.path.join(tmp_dir, os.path.splitext(os.path.basename(out_path))[0] + ".wav")
    if len(parts) == 1:
        os.replace(parts[0]["path"], final_wav)
        log("单批完成：整书 WAV 已就绪")
        return final_wav, parts[0]["duration_ms"] / 1000.0
    log(f"开始最终合并：{len(parts)} 个分片 → 整书（批间停顿按边界段规则）")
    merged = StreamingMerge(final_wav)
    try:
        for index, part in enumerate(parts):
            gap = None if not index else boundary_gap_ms(
                parts[index - 1]["last_pause_after"], parts[index - 1]["last_speaker"],
                part["first_speaker"], pause_ms, same_ms)
            merged.append(WaveInput(part["path"]), gap)
            progress(merge_stage2_frac(index + 1, len(parts)), f"最终合并第 {part['k']}/{m_plan} 片")
            log(f"分片 {part['k']}/{m_plan} 就绪（{part['duration_ms'] / 60000:.1f} 分钟）")
        duration_ms = merged.finish()
    finally:
        merged.close()
    log(f"整书 WAV 已导出（{duration_ms / 60000:.1f} 分钟）")
    return final_wav, duration_ms / 1000.0


def _encode_mp3_streaming(wav_path, mp3_path, duration_s, ffmpeg="", threads=0):
    """WAV -> MP3 via a direct ffmpeg child with live progress.

    ``threads`` — when a positive int, a global ``-threads N`` bounds the
    encoder's thread count (ffmpeg otherwise spawns ALL cores per process; with
    several merges running in parallel that oversubscribes the box). ``0``
    omits the flag entirely (legacy behaviour: manual / old runs).

    Progress comes from two measured sources, both throttled to ~1 update / 2 s:
    ffmpeg's stderr ``time=`` lines (real-time on platforms whose stderr is not
    block-buffered) and — because Windows pipes block-buffer ffmpeg's stderr, so
    the time= lines can arrive in one burst only at the end — a ffprobe reading
    of the growing output file's *real* duration every 2 s while the encoder
    runs (measured, never estimated). If ffprobe is missing the label degrades
    to an elapsed-time tick, so the progress text still changes at least every
    2 s. Mirrors backend/engines/audio.py::detect_silences (reader thread over
    stderr + the time= regex) minus the task-cancellation hook — the backend
    kills the worker on cancel, and the finally below kills the encoder. The
    encoder args match the old _wav_to_mp3 (pydub default export) exactly:
    libmp3lame with NO explicit bitrate — pydub's export passes no -b:a, and an
    explicit 192k would clamp differently per sample rate (160k at 22.05/24 kHz)
    and change the size / quality of every merged file. Returns False (caller
    falls back to keeping the WAV) when ffmpeg is missing / fails, or the output
    is a broken header-only file (< 1 KiB).
    """
    if duration_s <= 0:
        return False
    ffmpeg = ffmpeg or "ffmpeg"
    cmd = [ffmpeg, "-y"]
    if threads:
        cmd += ["-threads", str(threads)]
    cmd += ["-loglevel", "info", "-i", wav_path,
            "-c:a", "libmp3lame", mp3_path]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return False

    # Best-effort: on Windows, bind the encoder to a kill-on-close job object so a
    # hard kill of this worker on cancel cannot orphan it (see _new_windows_kill_job).
    job, kernel32 = _new_windows_kill_job()
    if job is not None:
        try:
            # A failed assignment (returns 0, no exception) means nothing is in the
            # job yet, so dropping the handle is safe and falls back to finally-kill.
            if not kernel32.AssignProcessToJobObject(job, proc._handle):
                kernel32.CloseHandle(job)
                job = None
        except Exception:
            kernel32.CloseHandle(job)
            job = None

    state = {"pct": -1.0, "at": 0.0}

    def report(pct) -> None:
        pct = max(0.0, min(100.0, pct))
        now = time.time()
        if (pct - state["pct"] >= ENCODE_PROGRESS_MIN_STEP_PCT
                or now - state["at"] >= ENCODE_PROGRESS_MIN_INTERVAL_S):
            state["pct"] = pct
            state["at"] = now
            progress(merge_encode_frac(pct / 100.0), f"编码 MP3 {pct:.0f}%")

    def reader() -> None:
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace")
                m = RE_FFMPEG_TIME.search(line)
                if not m:
                    continue
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                report(min(100.0, secs / duration_s * 100.0))
        except Exception:
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    def measured_pct():
        """The output file's real duration so far / the whole book (ffprobe)."""
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", mp3_path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
            if r.returncode != 0:
                return None
            d = float(r.stdout.strip().split(b"\n")[0])
            return min(100.0, max(0.0, d / duration_s * 100.0))
        except Exception:
            return None

    t_start = time.time()
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        while proc.poll() is None:
            # On Windows the time= lines above sit in ffmpeg's stderr buffer until
            # the encoder exits — measure the real output duration instead, so the
            # progress text/bar changes at least every ENCODE_PROGRESS_MIN_INTERVAL_S.
            if time.time() - state["at"] >= ENCODE_PROGRESS_MIN_INTERVAL_S:
                pct = measured_pct()
                if pct is None:
                    # no ffprobe (or nothing measurable yet): keep the label alive
                    state["at"] = time.time()
                    frac = (merge_encode_frac(max(0.0, state["pct"]) / 100.0)
                            if state["pct"] >= 0 else 0.95)
                    progress(frac, f"编码 MP3… 已 {time.time() - t_start:.0f} 秒")
                else:
                    report(pct)
            time.sleep(0.1)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        t.join(timeout=5)
        if job is not None:
            kernel32.CloseHandle(job)  # safe: the encoder has exited (or been killed)

    if proc.returncode != 0:
        return False
    # A broken ffmpeg (no libmp3lame) yields a tiny header-only file without raising.
    size = os.path.getsize(mp3_path) if os.path.exists(mp3_path) else 0
    if size < 1024:
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
        return False
    return True


def _run_merge(args) -> int:
    """Two-stage merge: per-segment files -> part WAVs (batches of
    --merge-batch-size) in --tmp-dir -> the final audiobook, with live progress
    through every stage.

    ``[result]`` is emitted exactly once, for the final file only (part files are
    plain log lines); on an MP3-encode failure it points at the kept whole-book WAV
    inside --tmp-dir, which the backend relocates into 06_audio_merge. Exit codes:
    0 success, 1 combine failed, 2 setup error / nothing to merge.
    """
    import json as _json

    if not args.segments_file or not args.out or not args.tmp_dir:
        print("TTS_WORKER_ERROR: merge mode needs --segments-file, --out and --tmp-dir",
              file=sys.stderr, flush=True)
        return 2

    _add_ffmpeg_to_path(args.ffmpeg)

    try:
        with open(args.segments_file, "r", encoding="utf-8") as f:
            segs = _json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"TTS_WORKER_ERROR: cannot read segments file: {e}", file=sys.stderr, flush=True)
        return 2
    if not isinstance(segs, list) or not segs:
        print("TTS_WORKER_ERROR: no segments to merge", file=sys.stderr, flush=True)
        return 2

    pause_ms = int(args.pause_ms)
    same_ms = int(args.same_same_ms)
    total = len(segs)
    tmp_dir = os.path.abspath(args.tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)

    parts = _merge_stage1(segs, tmp_dir, pause_ms, same_ms, args.merge_batch_size,
                         total, _workspace_root(args))
    if parts is None:
        return 2
    m_plan = len(plan_merge_batches(total, args.merge_batch_size))

    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    final_wav, duration_s = _merge_stage2(parts, tmp_dir, out_path, pause_ms, same_ms, m_plan)
    if final_wav is None:
        return 1

    progress(0.95, "开始编码 MP3")
    log(f"开始编码 MP3（{duration_s / 60:.1f} 分钟，流式进度）")
    if _encode_mp3_streaming(final_wav, out_path, duration_s, args.ffmpeg, args.threads):
        produced = out_path
    else:
        log("MP3 编码不可用（缺少 ffmpeg？）；保留 WAV。")
        produced = final_wav

    log(f"合并完成：{duration_s / 60:.1f} 分钟，{sum(p['n'] for p in parts)} 段 → {produced}")
    print(f"[result] {produced}", flush=True)
    progress(1.0, "完成")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Qwen3-TTS synthesis worker")
    ap.add_argument("--mode", required=True,
                    choices=["batch", "design-batch", "merge"],
                    help="durable mode, always passed explicitly by the backend (the former "
                         "one-shot custom/design/clone modes are retired — A5)")
    # shared
    ap.add_argument("--out", default="", help="absolute path of the intended output file")
    ap.add_argument("--speaker", default=DEFAULT_SPEAKER)
    ap.add_argument("--language", default=DEFAULT_LANGUAGE)
    ap.add_argument("--device", default="auto", help="auto|cuda|cpu|mps")
    ap.add_argument("--ffmpeg", default="", help="path to ffmpeg (its dir is added to PATH)")
    ap.add_argument("--workspace", default="",
                    help="workspace root: relative path values in --voice-config / "
                         "--segments-file resolve against this (batch / merge); "
                         "empty = legacy cwd-based resolution")
    # model ids
    ap.add_argument("--model", default=DEFAULT_MODEL, help="CustomVoice model id")
    ap.add_argument("--base-model", default=DEFAULT_BASE_MODEL, help="Base (clone) model id")
    ap.add_argument("--design-model", default=DEFAULT_DESIGN_MODEL, help="VoiceDesign model id")
    # batch
    ap.add_argument("--segments-file", default="",
                    help="JSON list of segments (batch / design-batch jobs)")
    ap.add_argument("--voice-config", default="", help="voice_config.json path (batch)")
    ap.add_argument("--out-dir", default="", help="per-segment output dir (batch / design-batch)")
    ap.add_argument("--width", type=int, default=0,
                    help="zero-pad width for per-segment filenames (batch): the digits the "
                         "largest package needs, precomputed by the backend and stable across "
                         "resume / watchdog restart (0 = derive it from the loaded "
                         "segment-table length, as before)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="per-batch CEILING: the max rows in one tensor batch (batch / design-batch); "
                         "length-sorted fixed batches with timeout recovery (1 = sequential)")
    ap.add_argument("--oom-restore-cap", type=int, default=0,
                    help="probe this original batch ceiling once after one reduced group succeeds")
    ap.add_argument("--auto-batch", action="store_true",
                    help="select each batch cap by upward matching the longest row to measured safety tiers")
    ap.add_argument("--vocoder-batch-size", type=int, default=0, help="opt-in decoder chunk size; 0 keeps native decode")
    ap.add_argument("--restore-stack", default="",
                    help="pending watchdog-demotion records '<chars>:<cap>,…', oldest first (batch): "
                         "each is the timed-out sub-batch's total chars + the pre-demotion per-batch "
                         "cap; after two successful batches at the newest reduced cap, restore it "
                         "(empty = no pending restore)")
    ap.add_argument("--seed", type=int, default=-1,
                    help="reproducible seed offset per sub-batch (batch / design-batch; -1 = random)")
    ap.add_argument("--done-offset", type=int, default=0,
                    help="candidates already settled before this run (design-batch; keeps progress "
                         "continuous across watchdog restarts)")
    ap.add_argument("--total", type=int, default=0,
                    help="global candidate total (design-batch; 0 = the jobs file length)")
    # merge
    ap.add_argument("--pause-ms", type=int, default=500, help="pause between different speakers")
    ap.add_argument("--same-same-ms", type=int, default=250, help="pause for same speaker")
    ap.add_argument("--tmp-dir", default="",
                    help="staging dir for the part WAVs (merge; required — created and "
                         "cleaned by the backend)")
    ap.add_argument("--merge-batch-size", type=int, default=100,
                    help="segments per part WAV in the two-stage merge (merge)")
    ap.add_argument("--threads", type=int, default=0,
                    help="max encoder threads for the final MP3 encode (merge); "
                         "0/omitted = ffmpeg's own default (direct invocations)")
    args = ap.parse_args()

    if args.mode in ("batch", "design-batch"):
        try:
            args.restore_stack = parse_restore_stack(args.restore_stack)
        except ValueError as e:
            print(f"TTS_WORKER_ERROR: {e}", file=sys.stderr, flush=True)
            return 2

    if args.mode == "batch":
        return _run_batch(args)
    if args.mode == "design-batch":
        return _run_design_batch(args)
    if args.mode == "merge":
        return _run_merge(args)

    raise AssertionError(f"unreachable: mode {args.mode} not in choices")


if __name__ == "__main__":
    sys.exit(main())
