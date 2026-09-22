"""Unified asynchronous task system.

Long-running work (audio silence-detection / cutting, and future TTS) runs as a
:class:`Task` in a worker thread. Each task exposes status, progress, a live log
buffer and start / pause / resume / cancel / retry controls, and streams events
to the UI over SSE. A failing task is marked failed and isolated — it never takes
the console down (requirement #7).
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL = {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED, TaskStatus.FAILED}

# Max chars of raw LLM stream retained per task (the 「流式反馈」 panel's source of truth).
# Tasks are kept in memory for the life of the process, so this bounds how much a long
# parse can accumulate; the oldest chunks are dropped first. Lower it if you're memory
# sensitive, raise it for more reviewable history.
LLM_STREAM_CAP = 128 * 1024

# Length of the 文本解析 吞吐量 card's moving window (seconds). The backend keeps, per task,
# the real streamed chars sampled against a monotonic clock and reports the average rate over
# the most recent ``RATE_WINDOW`` seconds — a genuinely measured, smooth 10-second average
# (vs. the instantaneous per-flush rate, which jitters). 10 s is long enough to straddle the
# per-chunk gaps yet short enough to still read as "now".
RATE_WINDOW = 10.0


class TaskCancelled(Exception):
    """Raised by engine code to abort a task cleanly."""


class TaskHandle:
    """Handed to engine code so it can report progress, log, and honour
    pause / cancel requests cooperatively."""

    def __init__(self, task: "Task"):
        self._t = task

    @property
    def cancelled(self) -> bool:
        return self._t.cancel_event.is_set()

    def progress(self, frac: float, current: str = "") -> None:
        self._t.set_progress(frac, current)

    def phase(self, name: str) -> None:
        """Report the current work stage (e.g. ``"parse"`` / ``"check"``).

        The 文本解析 batch coordinator reads ``Task.phase`` to tell which tasks have
        already released their concurrency slot (the mechanical check stages run
        slot-free) so it can keep admitting prefetched files. Display-neutral: the
        frontend store ignores the SSE event, and ``current`` stays the user-facing
        step text.
        """
        self._t.set_phase(name)

    def log(self, msg: str, level: str = "INFO") -> None:
        self._t.log(msg, level)

    def llm_chunk(self, text: str) -> None:
        """Forward a (coalesced) slice of the raw LLM stream to the 「流式反馈」 panel.

        Display-only: the engine calls this as the model streams so the UI can show the
        model's working state live. It is never read back by the parse/JSON pipeline.
        """
        self._t.log_llm(text)

    def llm_rate(self, chars: int, cps: float) -> None:
        """Report one generation-rate sample to the UI.

        ``chars`` is the streamed chars (content + reasoning) this flush produced and ``cps``
        the instantaneous rate over the flush window; the task accumulates ``chars`` into a
        monotonic total and samples it against the clock so it can report both the
        instantaneous rate (per-window ``字/s`` gauge) and the 吞吐量 card's 10-second average.
        Like ``llm_chunk`` it is display-only — never the parse/JSON pipeline. Real streamed
        chars, never tokens.
        """
        self._t.record_llm_rate(chars, cps)

    def llm_chars(self, chars: int, secs: float) -> None:
        """Report cumulative original-text chars processed + the processing time up to the
        most recently completed chunk (per chunk).

        The 文本解析 engine calls this after each text chunk is fully processed, so the
        page's 处理速度 gauge (Σ chars ÷ Σ processing-time) steps up on every chunk and
        stays *stable* in between — the time base is frozen at each chunk's completion
        (reported by the backend), not a live clock, so the value never decays while the
        next chunk is still generating. Display-only, like ``llm_rate`` — real chars,
        never tokens.
        """
        self._t.set_llm_chars(chars, secs)

    def segment_stats(self, done: int, total: int, chars_done: int, chars_total: int) -> None:
        """Report cumulative 已合成段数 / 字数 against the run's totals (音频合成 metrics).

        The tts_batch engine calls this (throttled, at most once a second) as segments
        complete, so the 合成页's 已合成/总段数 · 已合成/总字数 card below the
        开始音频合成 button updates per finished sub-batch purely from these events
        (no polling). Display-only, like ``llm_rate``. ``done`` / ``chars_done`` are
        cumulative (the pre-run manifest completions + this run's) against the run's
        full segment table (``total`` / ``chars_total``); every event carries the full
        cumulative state, so a dropped event self-heals on the next one or the snapshot.
        """
        self._t.set_segment_stats(done, total, chars_done, chars_total)

    def check(self) -> None:
        """Cooperative cancellation + pause point. Call between units of work."""
        if self._t.cancel_event.is_set():
            raise TaskCancelled()
        while self._t.pause_event.is_set() and not self._t.cancel_event.is_set():
            time.sleep(0.1)
        if self._t.cancel_event.is_set():
            raise TaskCancelled()


@dataclass
class Task:
    id: str
    module: str
    label: str
    # Monotonic creation order (1-based, assigned by the TaskManager). Unlike
    # ``created`` (wall clock, can tie within a tight create loop) it is a
    # guaranteed ordering key — the 文本解析 page rebuilds a batch's file order
    # after a page reload by sorting non-terminal tasks on this.
    seq: int = 0
    # Engine-reported work stage (e.g. ``"parse"`` / ``"check"``, set via
    # :meth:`set_phase`) — consumed in-process by the 文本解析 batch coordinator
    # (which tasks have already released their concurrency slot). Empty for
    # stages the engine doesn't report.
    phase: str = ""
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    current: str = ""
    logs: deque = field(default_factory=lambda: deque(maxlen=1000))
    # Raw LLM stream (the 「流式反馈」 panel). A char-capped buffer of the coalesced
    # deltas, oldest dropped first when over ``LLM_STREAM_CAP`` — see ``log_llm``.
    # Kept on the Task so the on-connect snapshot / terminal events can replay it and
    # the panel survives a page reload (restored via ``GET /api/tasks``).
    llm_chunks: deque = field(default_factory=deque)
    llm_len: int = 0
    # Live LLM generation rate (chars/s) for the 文本解析 吞吐量 / per-window gauge.
    # Updated from the streamed text and replayed in the snapshot / terminal events, so
    # a reconnect or a finished window still carries its most recent rate.
    llm_cps: float = 0.0
    # Cumulative original-text chars processed so far — stepped up per completed chunk by
    # the 文本解析 engine (the 处理速度 gauge's numerator). Real chars, never tokens.
    # Kept on the Task so the snapshot / terminal events replay it (survives a reload).
    llm_chars: int = 0
    # Cumulative processing time (s) up to the last completed chunk — the 处理速度
    # gauge's denominator. Frozen at each chunk's completion (not a live clock), so the
    # gauge holds steady between chunks instead of decaying.
    llm_secs: float = 0.0
    # 10-second-window average of the LLM generation rate (chars/s) for the 文本解析 吞吐量
    # card — (chars generated over the last ``RATE_WINDOW`` s) / that span, computed in the
    # backend from the real streamed chars (see ``record_llm_rate``) and replayed in the
    # snapshot / SSE events. The thin client sums this over running tasks for the page total.
    llm_cps_10s: float = 0.0
    # Cumulative streamed chars (content + reasoning) for this task — a monotonic running
    # total that ``record_llm_rate`` advances on every flush (reset on retry). Backs the
    # 10-second window diff below; not sent to the client on its own.
    llm_gen_total: int = 0
    # Cumulative 已合成段数 / 字数 against the run's full segment table (音频合成: the
    # 合成页 button-below 已合成/总段数 · 已合成/总字数 card). Advanced by the tts_batch
    # engine on every completed segment (``set_segment_stats``) and replayed in the
    # snapshot / terminal events, so the card survives a page reload and stays correct
    # after the task settles. 0 for tasks that don't report segments.
    seg_done: int = 0
    seg_total: int = 0
    seg_chars_done: int = 0
    seg_chars_total: int = 0
    # (monotonic_ts, cumulative_gen_chars) samples retained within the last ``RATE_WINDOW``
    # seconds — the 吞吐量 window's source. Oldest-first; samples older than the window are
    # evicted on each update, so the diff (last - first) / (t_last - t_first) is the true
    # average rate over "as much of the last 10 s as we have samples for" (full 10 s once
    # steady, less for a young task, empty → 0 when the task has been idle past the window).
    llm_gen_hist: deque = field(default_factory=deque)
    result: dict = field(default_factory=dict)
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    # Manager-level broadcast hook (set by ``TaskManager.create``): every event emitted
    # here is also forwarded to the process-wide bus that feeds the multiplexed
    # ``GET /api/tasks/stream`` SSE endpoint (one connection per browser tab covers
    # ALL tasks — see ``TaskManager._bus_event``). None for standalone tasks.
    _broadcast: Optional[Callable[["Task", dict], None]] = field(default=None, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _func: Optional[Callable] = field(default=None, repr=False)
    _args: tuple = field(default=(), repr=False)
    _kwargs: dict = field(default_factory=dict, repr=False)
    _listeners: set = field(default_factory=set, repr=False)

    # -- state updates (notify SSE listeners) -------------------------------
    def set_progress(self, frac: float, current: str = "") -> None:
        self.progress = max(0.0, min(1.0, float(frac)))
        if current:
            self.current = current
        self._emit({"type": "progress", "progress": self.progress, "current": self.current})

    def set_phase(self, phase: str) -> None:
        """Set the engine-reported work stage (see ``Task.phase``) and forward it.

        The event rides the usual bus; the multiplexed SSE stream passes it through
        and the frontend store ignores unknown event types — the consumers are
        in-process (the batch coordinator polls ``Task.phase`` directly).
        """
        self.phase = phase
        self._emit({"type": "phase", "phase": phase})

    def log(self, msg: str, level: str = "INFO") -> None:
        # Append in chronological order (oldest → newest). The UI renders the newest
        # line at the bottom, and the snapshot replays this same order, so the live
        # stream and a reconnect's replayed log always agree on ordering.
        entry = {"level": level, "msg": msg, "t": time.time()}
        self.logs.append(entry)
        self._emit({"type": "log", **entry})

    def log_llm(self, text: str) -> None:
        """Append a (coalesced) slice of the raw LLM stream and forward it over SSE.

        The 文本解析 engine calls this as the model streams, so the 「流式反馈」 panel
        fills in real time. The text is also kept on the Task (char-capped, oldest
        dropped first) so the on-connect snapshot / terminal events can replay the whole
        stream — the panel survives a page reload and self-heals any dropped live event.
        """
        self.llm_chunks.append(text)
        self.llm_len += len(text)
        while self.llm_len > LLM_STREAM_CAP and self.llm_chunks:
            self.llm_len -= len(self.llm_chunks.popleft())
        self._emit({"type": "llm_chunk", "data": text})

    def record_llm_rate(self, chars: int, cps: float) -> None:
        """Record one generation-rate sample and forward the instantaneous + 10 s rates.

        ``chars`` advances the monotonic streamed-char total; a (timestamp, total) sample is
        appended and anything older than ``RATE_WINDOW`` is evicted, so the retained span is
        the last 10 s (or less for a young task). ``llm_cps`` is the instantaneous rate
        (per-window gauge, unchanged); ``llm_cps_10s`` is (chars over that span) / (span) — a
        real, unbiased 10-second average the 吞吐量 card sums across running tasks. One
        ``llm_rate`` SSE event carries both (``cps`` + ``cps10``).
        """
        now = time.monotonic()
        self.llm_cps = max(0.0, float(cps))
        self.llm_gen_total += max(0, int(chars))
        self.llm_gen_hist.append((now, self.llm_gen_total))
        cutoff = now - RATE_WINDOW
        while self.llm_gen_hist and self.llm_gen_hist[0][0] < cutoff:
            self.llm_gen_hist.popleft()
        self.llm_cps_10s = self._window_rate(self.llm_gen_hist)
        self._emit({"type": "llm_rate", "cps": self.llm_cps, "cps10": self.llm_cps_10s})

    @staticmethod
    def _window_rate(hist: deque) -> float:
        """Average chars/s over the retained (≤ ``RATE_WINDOW``) span of samples.

        ``hist`` holds (monotonic_ts, cumulative_chars) oldest-first; the cumulative total is
        monotonic, so (c_last - c_first) is exactly the chars generated within the span and
        (c_last - c_first) / (t_last - t_first) is the true average rate over it — no bias
        from how the flushes are spaced in time, and no estimation.
        """
        if len(hist) < 2:
            return 0.0
        ts0, c0 = hist[0]
        ts1, c1 = hist[-1]
        dt = ts1 - ts0
        if dt <= 1e-3:  # ~zero span (only a flat reset sample, or a brand-new task) → no rate
            return 0.0
        return max(0.0, (c1 - c0) / dt)

    def set_llm_chars(self, chars: int, secs: float) -> None:
        """Set cumulative original-text chars + processing time and forward over SSE.

        Both advance together (one event per completed chunk) so the client's
        处理速度 = Σ chars ÷ Σ secs is always internally consistent and stable between
        chunks (the time base is whatever the backend measured at that chunk's end).
        """
        self.llm_chars = max(0, int(chars))
        self.llm_secs = max(0.0, float(secs))
        self._emit({"type": "llm_chars", "chars": self.llm_chars, "secs": self.llm_secs})

    def set_segment_stats(self, done: int, total: int, chars_done: int, chars_total: int) -> None:
        """Set the 音频合成 cumulative 段数 / 字数 progress and forward over SSE.

        Every event carries the FULL cumulative state (``done``/``total``,
        ``chars_done``/``chars_total``), so the client just overwrites — and a dropped
        event (display-only, see ``TaskManager._DISPLAY_ONLY``) self-heals on the next
        event or the snapshot / terminal replay.
        """
        self.seg_done = max(0, int(done))
        self.seg_total = max(0, int(total))
        self.seg_chars_done = max(0, int(chars_done))
        self.seg_chars_total = max(0, int(chars_total))
        self._emit({
            "type": "segments",
            "done": self.seg_done,
            "total": self.seg_total,
            "chars_done": self.seg_chars_done,
            "chars_total": self.seg_chars_total,
        })

    def _set_status(self, status: TaskStatus) -> None:
        self.status = status
        if status in TERMINAL:
            # A terminal status is the instant consumers act on "task done", so the
            # event also carries the full snapshot (result / error). Otherwise a client
            # reacting to the status change reads a stale, empty result — the result
            # would otherwise arrive only in the later ``final`` event, by which point
            # the view has already captured (and detached on) the empty one.
            self._emit({"type": "status", "status": status.value, "task": self.snapshot()})
        else:
            self._emit({"type": "status", "status": status.value})

    def _emit(self, event: dict) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait(event)
            except queue.Full:
                pass
        if self._broadcast is not None:
            self._broadcast(self, event)

    # -- snapshot for the API ------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "module": self.module,
            "label": self.label,
            "seq": self.seq,
            "status": self.status.value,
            "progress": self.progress,
            "current": self.current,
            "logs": list(self.logs),
            "llm_stream": "".join(self.llm_chunks),
            "llm_cps": self.llm_cps,
            "llm_cps_10s": self.llm_cps_10s,
            "llm_chars": self.llm_chars,
            "llm_secs": self.llm_secs,
            "seg_done": self.seg_done,
            "seg_total": self.seg_total,
            "seg_chars_done": self.seg_chars_done,
            "seg_chars_total": self.seg_chars_total,
            "result": self.result,
            "error": self.error,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
        }

    # -- SSE subscription ----------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        self._listeners.discard(q)


class TaskManager:
    # Event types that are display-only and self-healing: a dropped slice is
    # repaired by the next ``snapshot`` / ``snapshot_all`` / ``final`` (which
    # replays the authoritative, backend-capped LLM stream) or the next event of
    # the same kind (``segments`` carries the full cumulative counters, so any
    # single drop is made up by the following push). ``log`` / ``progress`` /
    # ``status`` / ``final`` are NOT in this set — they must survive queue
    # pressure (see ``_bus_put_one``).
    _DISPLAY_ONLY = ("llm_chunk", "llm_rate", "segments")

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        # Monotonic creation counter (thread-safe): a guaranteed-total ordering
        # for tasks created back-to-back (``created`` wall-clock timestamps can
        # tie within a tight loop).
        self._seq = itertools.count(1)
        # Process-wide event bus feeding the multiplexed ``GET /api/tasks/stream``
        # SSE endpoint. Browsers cap simultaneous HTTP/1.1 connections per host at
        # ~6, so the UI holds exactly ONE such connection per tab (covering all
        # tasks, every event carrying ``task_id``) instead of one per task — one
        # connection per task is exhausted by a few parallel parses and every
        # EventSource beyond the cap never connects (its window shows no logs
        # while the backend runs fine).
        self._bus: set = set()
        self._bus_lock = threading.Lock()

    def create(self, module: str, label: str, func: Callable, *args,
               start: bool = True, **kwargs) -> Task:
        """Create a task and (by default) start its worker thread.

        ``start=False`` creates a **PENDING shell**: registered in ``self._tasks``
        (visible to ``GET /api/tasks`` / ``snapshot_all`` / page-reload reattach and
        counted as non-terminal for the SSE keep-alive) but with NO thread started —
        the 文本解析 batch endpoint creates all of a batch's file shells up front
        (returning every task_id so the UI binds rows immediately) and a coordinator
        thread calls :meth:`start` on each in selection order as capacity opens.
        """
        task = Task(id=uuid.uuid4().hex[:12], module=module, label=label, seq=next(self._seq))
        task._func, task._args, task._kwargs = func, args, kwargs
        task._broadcast = self._bus_event
        with self._lock:
            self._tasks[task.id] = task
            if start:
                task.started = time.time()
                task._set_status(TaskStatus.RUNNING)
        if start:
            t = threading.Thread(target=self._run, args=(task,), daemon=True)
            task._thread = t
            t.start()
        return task

    def start(self, task_id: str) -> Task:
        """Start a PENDING shell (created via ``create(..., start=False)``).

        The PENDING → RUNNING transition happens under the manager lock so it cannot
        race with ``control("cancel")`` finalizing the shell; when the task is no
        longer PENDING (e.g. cancelled meanwhile) this raises ``ValueError`` — the
        文本解析 coordinator treats that as a benign race and moves on to the next
        file.
        """
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        with self._lock:
            if task.status is not TaskStatus.PENDING:
                raise ValueError(f"task {task_id} 非 PENDING（{task.status.value}），不能启动")
            task.started = time.time()
            task._set_status(TaskStatus.RUNNING)
        t = threading.Thread(target=self._run, args=(task,), daemon=True)
        task._thread = t
        t.start()
        return task

    def _run(self, task: Task) -> None:
        handle = TaskHandle(task)
        task.log("任务开始", "INFO")
        try:
            result = task._func(handle, *task._args, **task._kwargs) or {}
            task.result = result
            task.set_progress(1.0, "完成")
            task.log("任务完成", "INFO")
            task._set_status(TaskStatus.SUCCEEDED)
        except TaskCancelled:
            task.log("任务已取消", "WARNING")
            task._set_status(TaskStatus.CANCELLED)
        except Exception as exc:  # noqa: BLE001 — isolate any failure
            task.error = str(exc)
            task.log(f"任务失败：{exc}", "ERROR")
            task._set_status(TaskStatus.FAILED)
        finally:
            task.finished = time.time()
            task._emit({"type": "final", "task": task.snapshot()})

    # -- lookups -------------------------------------------------------------
    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda t: t.created, reverse=True)

    # -- process-wide bus (multiplexed SSE) ----------------------------------
    def subscribe_all(self) -> queue.Queue:
        """Subscribe to the events of ALL tasks (current and future).

        Receives ``(task, event)`` tuples; larger ``maxsize`` than a per-task
        listener because one consumer now drains every task's traffic.
        """
        q: queue.Queue = queue.Queue(maxsize=3000)
        self._bus.add(q)
        return q

    def unsubscribe_all(self, q: queue.Queue) -> None:
        self._bus.discard(q)

    def _bus_event(self, task: Task, event: dict) -> None:
        """Forward one task event to every bus subscriber (called from ``Task._emit``)."""
        for q in list(self._bus):
            self._bus_put_one(q, task, event)

    def _bus_put_one(self, q: queue.Queue, task: Task, event: dict) -> None:
        try:
            q.put_nowait((task, event))
            return
        except queue.Full:
            pass
        # Queue full. A display-only event can simply be dropped (self-healing,
        # see ``_DISPLAY_ONLY``); a critical one (log / progress / status /
        # final) is kept — evict the oldest display-only items first, retry.
        if event.get("type") in self._DISPLAY_ONLY:
            return
        with self._bus_lock:
            items: list = []
            while True:
                try:
                    items.append(q.get_nowait())
                except queue.Empty:
                    break
            for t_old, e_old in items:
                if e_old.get("type") not in self._DISPLAY_ONLY:
                    q.put_nowait((t_old, e_old))
            try:
                q.put_nowait((task, event))
            except queue.Full:
                pass

    # -- controls ------------------------------------------------------------
    def control(self, task_id: str, action: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if action == "cancel":
            finalized_shell = False
            with self._lock:
                task.cancel_event.set()
                if task.status is TaskStatus.PENDING:
                    # A shell has no worker thread to observe cancel_event — finalize
                    # it in place or it would hang PENDING forever (and the SSE
                    # keep-alive would keep the stream open). The terminal status
                    # event below carries the full snapshot, like _run's path.
                    task.finished = time.time()
                    task._set_status(TaskStatus.CANCELLED)
                    finalized_shell = True
            if finalized_shell:
                # _run's finally never runs for a shell — emit the terminal log line
                # and the final event here so SSE consumers see the same lifecycle.
                task.log("任务已取消（未启动）", "WARNING")
                task._emit({"type": "final", "task": task.snapshot()})
            else:
                task.log("收到取消请求", "WARNING")
        elif action == "pause":
            if task.status == TaskStatus.RUNNING:
                task.pause_event.set()
                task._set_status(TaskStatus.PAUSED)
                task.log("任务已暂停", "INFO")
        elif action == "resume":
            if task.status == TaskStatus.PAUSED:
                task.pause_event.clear()
                task._set_status(TaskStatus.RUNNING)
                task.log("任务已恢复", "INFO")
        elif action == "retry":
            if task.status in TERMINAL:
                task.cancel_event.clear()
                task.pause_event.clear()
                task.error = ""
                task.progress = 0.0
                task.phase = ""  # a rerun starts from scratch — a stale "check" phase
                # would make the batch coordinator think the slot was already released
                task.logs.clear()
                task.llm_chunks.clear()
                task.llm_len = 0
                task.llm_cps = 0.0
                task.llm_cps_10s = 0.0
                task.llm_gen_total = 0
                task.llm_gen_hist.clear()
                task.llm_chars = 0
                task.llm_secs = 0.0
                task.seg_done = 0
                task.seg_total = 0
                task.seg_chars_done = 0
                task.seg_chars_total = 0
                task.result = {}
                task.started = time.time()
                task.finished = 0.0
                task._set_status(TaskStatus.RUNNING)
                t = threading.Thread(target=self._run, args=(task,), daemon=True)
                task._thread = t
                t.start()
        else:
            raise ValueError(f"unknown action {action!r}")
        return task


_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    global _manager
    if _manager is None:
        _manager = TaskManager()
    return _manager
