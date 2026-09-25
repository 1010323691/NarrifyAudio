"""Test-only in-memory task executor.

Mirrors the retired ``backend/core/tasks.py`` public surface (thread-backed
``TaskManager`` / ``Task`` / ``TaskHandle`` / ``TaskStatus``) for the engine
e2e tests that run engine functions through a real worker thread. It drops
everything the tests never touched: the SSE event bus, workspace binding,
pause/resume/retry, and the LLM display-only state (the matching
``TaskHandle`` methods are no-ops).

``TaskCancelled`` is re-exported from ``backend.core.task_control`` — the
production task-control layer both task runtimes share.
"""
from __future__ import annotations

import itertools
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from backend.core.task_control import TaskCancelled

__all__ = [
    "TERMINAL",
    "Task",
    "TaskCancelled",
    "TaskHandle",
    "TaskManager",
    "TaskStatus",
    "get_task_manager",
]


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL = {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED, TaskStatus.FAILED}


class TaskHandle:
    """Handed to engine code so it can report progress, log, and honour cancel
    requests cooperatively. Display-only LLM metrics are no-ops (the tests
    never read them back)."""

    def __init__(self, task: "Task"):
        self._t = task

    @property
    def cancelled(self) -> bool:
        return self._t.cancel_event.is_set()

    def progress(self, frac: float, current: str = "") -> None:
        self._t.progress = max(0.0, min(1.0, float(frac)))
        if current:
            self._t.current = current

    def phase(self, name: str) -> None:
        self._t.phase = name

    def log(self, msg: str, level: str = "INFO") -> None:
        self._t.logs.append({"level": level, "msg": msg, "t": time.time()})

    def llm_chunk(self, text: str) -> None:
        pass

    def llm_rate(self, chars: int, cps: float) -> None:
        pass

    def llm_chars(self, chars: int, secs: float) -> None:
        pass

    def segment_stats(self, done: int, total: int, chars_done: int, chars_total: int) -> None:
        pass

    def check(self) -> None:
        """Cooperative cancellation point. Call between units of work."""
        if self._t.cancel_event.is_set():
            raise TaskCancelled()


@dataclass
class Task:
    id: str
    module: str
    label: str
    seq: int = 0
    phase: str = ""
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    current: str = ""
    logs: deque = field(default_factory=lambda: deque(maxlen=1000))
    result: dict = field(default_factory=dict)
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _func: Optional[Callable] = field(default=None, repr=False)
    _args: tuple = field(default=(), repr=False)
    _kwargs: dict = field(default_factory=dict, repr=False)

    def log(self, msg: str, level: str = "INFO") -> None:
        """Append a log entry (same shape ``TaskHandle.log`` uses)."""
        self.logs.append({"level": level, "msg": msg, "t": time.time()})


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._seq = itertools.count(1)

    def create(self, module: str, label: str, func: Callable, *args,
               start: bool = True, **kwargs) -> Task:
        """Create a task and (by default) start its worker thread.

        ``start=False`` creates a PENDING shell with no thread — ``start()``
        launches it later."""
        task = Task(id=uuid.uuid4().hex[:12], module=module, label=label, seq=next(self._seq))
        task._func, task._args, task._kwargs = func, args, kwargs
        with self._lock:
            self._tasks[task.id] = task
            if start:
                task.started = time.time()
                task.status = TaskStatus.RUNNING
        if start:
            t = threading.Thread(target=self._run, args=(task,), daemon=True)
            t.start()
        return task

    def start(self, task_id: str) -> Task:
        """Start a PENDING shell; raises ValueError once it is no longer PENDING."""
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        with self._lock:
            if task.status is not TaskStatus.PENDING:
                raise ValueError(f"task {task_id} 非 PENDING（{task.status.value}），不能启动")
            task.started = time.time()
            task.status = TaskStatus.RUNNING
        t = threading.Thread(target=self._run, args=(task,), daemon=True)
        t.start()
        return task

    def _run(self, task: Task) -> None:
        handle = TaskHandle(task)
        task.log("任务开始", "INFO")
        try:
            result = task._func(handle, *task._args, **task._kwargs) or {}
            task.result = result
            task.progress = 1.0
            task.current = "完成"
            task.log("任务完成", "INFO")
            task.status = TaskStatus.SUCCEEDED
        except TaskCancelled:
            task.log("任务已取消", "WARNING")
            task.status = TaskStatus.CANCELLED
        except Exception as exc:  # noqa: BLE001 — isolate any failure
            task.error = str(exc)
            task.log(f"任务失败：{exc}", "ERROR")
            task.status = TaskStatus.FAILED
        finally:
            task.finished = time.time()

    # -- lookups -------------------------------------------------------------
    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda t: t.created, reverse=True)

    # -- controls ------------------------------------------------------------
    def control(self, task_id: str, action: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if action == "cancel":
            with self._lock:
                task.cancel_event.set()
                if task.status is TaskStatus.PENDING:
                    # A shell has no worker thread to observe cancel_event —
                    # finalize it in place or it would stay PENDING forever.
                    task.finished = time.time()
                    task.status = TaskStatus.CANCELLED
            task.log("任务已取消", "WARNING")
        else:
            raise ValueError(f"unknown action {action!r}")
        return task


_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    global _manager
    if _manager is None:
        _manager = TaskManager()
    return _manager
