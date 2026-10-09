"""Host-wide atomic state and logical queues over existing durable tasks."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import timezone

from sqlalchemy import and_, exists, func, or_, select

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


def _waiting_request_metrics(db, side: str) -> tuple[int, object]:
    """Waiting requests whose task is not the one being served (or has none),
    deduped to one per task like the historical in-memory keying."""
    count, oldest = db.execute(
        select(
            func.count(func.distinct(func.coalesce(GPURequest.task_id, GPURequest.id))),
            func.min(GPURequest.created_at),
        )
        .select_from(GPURequest)
        .outerjoin(Task, Task.id == GPURequest.task_id)
        .where(
            GPURequest.status == "waiting",
            GPURequest.service == side,
            or_(GPURequest.task_id.is_(None), Task.status == "running"),
        )
    ).one()
    return int(count or 0), oldest


def _eligible_task_metrics(db, side: str, task_types: list[str]) -> tuple[int, object]:
    """Eligible tasks no request already represents (earliest-queued wins)."""
    has_request = exists(
        select(GPURequest.id).where(
            GPURequest.task_id == Task.id,
            GPURequest.status.in_(["running", "waiting"]),
        )
    )
    eligible = or_(
        Task.status.in_(["pending", "queued"]),
        and_(Task.status == "retrying", or_(Task.next_attempt_at.is_(None), Task.next_attempt_at <= utcnow())),
        # Existing service-unavailable pauses are automatic, unlike manual pauses.
        and_(Task.status == "paused", Task.error_code == "llm_unavailable"),
    )
    count, oldest = db.execute(
        select(func.count(Task.id), func.min(Task.created_at))
        .where(eligible, Task.task_type.in_(task_types), ~has_request)
    ).one()
    return int(count or 0), oldest


class QueueMonitor:
    def snapshot(self, db=None) -> dict[str, QueueStats]:
        if db is None:
            with SessionLocal() as session:
                return self.snapshot(session)
        now = stamp()
        stats = {}
        for side in ("LLM", "TTS"):
            # Aggregate per side in SQL: full task/request tables never
            # materialize just to count a queue.
            task_types = [t for t, spec in TASK_TYPES.items() if spec.gpu_initial == side]
            request_waiting, request_oldest = _waiting_request_metrics(db, side)
            task_waiting, task_oldest = _eligible_task_metrics(db, side, task_types)
            running = int(db.scalar(select(func.count(GPURequest.id)).where(
                GPURequest.status == "running", GPURequest.service == side)) or 0)
            oldest = min(value for value in (request_oldest, task_oldest) if value is not None) if (
                request_oldest is not None or task_oldest is not None) else None
            stats[side] = QueueStats(
                request_waiting + task_waiting,
                running,
                max(0, now - stamp(oldest)) if oldest is not None else 0,
            )
        return stats
