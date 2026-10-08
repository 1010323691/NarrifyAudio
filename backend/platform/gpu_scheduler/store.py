"""Host-wide atomic state and logical queues over existing durable tasks."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import timezone

from sqlalchemy import select

from ..database import SessionLocal
from ..models import GPURequest, GPUSchedulerState, Task, TaskAttempt, utcnow
from ..task_registry import TASK_TYPES
from ...core.file_lock import exclusive_file_lock
from ...core.paths import PROJECT_ROOT
from ...core.managed_process import identity_alive
from .policy import QueueStats

_lock = threading.RLock()
_held = threading.local()


def stamp(value=None) -> float:
    value = value or utcnow()
    return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()


def initial_state() -> dict:
    return {"state": "IDLE", "phase": "", "current": None, "managed": False,
            "heartbeat": 0, "active_since": 0, "last_switch": 0, "idle_since": 0,
            "served": False, "reason": "scheduler disabled", "error": "", "llm_process": {},
            "recover_requested": False, "service_config": {}}


@contextmanager
def host_lock():
    # A file lock also makes SQLite tests obey exactly the production atomicity.
    with _lock:
        if getattr(_held, "active", False):
            yield
        else:
            with exclusive_file_lock(PROJECT_ROOT / ".narrify" / "gpu-state.lock"):
                _held.active = True
                try:
                    yield
                finally:
                    _held.active = False


@contextmanager
def transaction():
    with host_lock():
        with SessionLocal.begin() as db:
            row = db.get(GPUSchedulerState, "local")
            if row is None:
                row = GPUSchedulerState(id="local", value=initial_state())
                db.add(row)
            state = {**initial_state(), **row.value}
            yield db, state
            row.value = state


def guarded_claim(function):
    from functools import wraps
    @wraps(function)
    def wrapped(*args, **kwargs):
        with host_lock():
            return function(*args, **kwargs)
    return wrapped


def read_state() -> dict:
    with SessionLocal() as db:
        row = db.get(GPUSchedulerState, "local")
        return {**initial_state(), **(row.value if row else {})}


def process_reap(db, state):
    """Never expire a running permit merely because a task lease expired."""
    for request in db.scalars(select(GPURequest)).all():
        owner = request.process.get("owner", {})
        if identity_alive(owner):
            continue
        child = request.process.get("child", {})
        llm_alive = request.service == "LLM" and identity_alive(state.get("llm_process", {}))
        if request.status == "running" and (identity_alive(child) or llm_alive):
            state.update(state="ERROR", phase="", error="GPU 调用所属 Worker 已退出，尚不能确认 GPU 工作已结束", reason="orphaned GPU request")
            continue
        db.delete(request)
    db.flush()


class QueueMonitor:
    def snapshot(self, db=None) -> dict[str, QueueStats]:
        if db is None:
            with SessionLocal() as session:
                return self.snapshot(session)
        now = stamp()
        waiting = {"LLM": {}, "TTS": {}}
        running = {"LLM": set(), "TTS": set()}
        tasks = {task.id: task for task in db.execute(select(Task.id, Task.task_type, Task.status, Task.error_code, Task.next_attempt_at, Task.created_at).where(Task.status.in_(
            ["pending", "queued", "retrying", "running", "cancelling", "paused"]))).all()}
        represented = set()
        for request in db.scalars(select(GPURequest)).all():
            task = tasks.get(request.task_id)
            key = request.task_id or request.id
            if request.status == "running":
                running[request.service].add(request.id)
                represented.add(key)
            elif request.status == "waiting" and (not request.task_id or (task and task.status == "running")):
                waiting[request.service][key] = min(waiting[request.service].get(key, now), stamp(request.created_at))
                represented.add(key)
        for task in tasks.values():
            if task.id in represented:
                continue
            eligible = task.status in {"pending", "queued"} or (task.status == "retrying" and (
                task.next_attempt_at is None or stamp(task.next_attempt_at) <= now))
            # Existing service-unavailable pauses are automatic, unlike manual pauses.
            eligible |= task.status == "paused" and task.error_code == "llm_unavailable"
            spec = TASK_TYPES.get(task.task_type)
            if eligible and spec and spec.gpu_initial:
                waiting[spec.gpu_initial][task.id] = stamp(task.created_at)
        return {side: QueueStats(len(waiting[side]), len(running[side]),
                                max(0, now - min(waiting[side].values())) if waiting[side] else 0)
                for side in waiting}
