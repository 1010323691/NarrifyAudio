from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any

from .database import SessionLocal
from .models import WorkerHeartbeat, utcnow


# An unchanged status/task is rewritten at most this often. Idle slots beat every
# ~0.5 s, which made the registry the largest source of writes (is_stale() allows 30 s).
HEARTBEAT_MIN_INTERVAL_SECONDS = 5.0
STALE_ROW_RETENTION = timedelta(days=1)
_last_beat: dict[str, tuple[str, str | None, float]] = {}
_last_beat_lock = threading.Lock()


def heartbeat(
    worker_id: str,
    *,
    status: str,
    capabilities: dict[str, Any] | None = None,
    current_task_id: str | None = None,
) -> None:
    mono = time.monotonic()
    with _last_beat_lock:
        last = _last_beat.get(worker_id)
    if last and last[0] == status and last[1] == current_task_id and mono - last[2] < HEARTBEAT_MIN_INTERVAL_SECONDS:
        return
    _write_heartbeat(worker_id, status=status, capabilities=capabilities, current_task_id=current_task_id)
    with _last_beat_lock:
        _last_beat[worker_id] = (status, current_task_id, mono)


def _write_heartbeat(
    worker_id: str,
    *,
    status: str,
    capabilities: dict[str, Any] | None,
    current_task_id: str | None,
) -> None:
    now = utcnow()
    with SessionLocal.begin() as db:
        row = db.get(WorkerHeartbeat, worker_id)
        if row is None:
            db.add(
                WorkerHeartbeat(
                    worker_id=worker_id,
                    status=status,
                    capabilities=capabilities or {},
                    current_task_id=current_task_id,
                    started_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
            )
        else:
            row.status = status
            if capabilities is not None:
                row.capabilities = capabilities
            row.current_task_id = current_task_id
            row.last_seen_at = now
            row.updated_at = now


def prune_stale(now=None, keep: timedelta = STALE_ROW_RETENTION) -> int:
    """Drop rows of workers that have not beaten for ``keep`` (every restart adds new pool ids)."""
    from sqlalchemy import delete

    cutoff = (now or utcnow()) - keep
    with SessionLocal.begin() as db:
        result = db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at < cutoff))
        return int(result.rowcount or 0)


def mark_offline(worker_id: str) -> None:
    with _last_beat_lock:
        _last_beat.pop(worker_id, None)
    with SessionLocal.begin() as db:
        row = db.get(WorkerHeartbeat, worker_id)
        if row is not None:
            row.status = "offline"
            row.current_task_id = None
            row.last_seen_at = utcnow()
            row.updated_at = utcnow()


def is_stale(last_seen_at, *, timeout_seconds: int = 30) -> bool:
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=utcnow().tzinfo)
    return last_seen_at < utcnow() - timedelta(seconds=timeout_seconds)


def live_worker_pool(db) -> tuple[dict, bool]:
    """Slot totals across live heartbeats, plus whether a slot-aware simulator is online.

    The simulator's heartbeat is authoritative about its active slots; callers
    use the flag to reclassify buffered running leases as queued.
    """
    from sqlalchemy import select

    live = [row for row in db.scalars(select(WorkerHeartbeat)).all() if not is_stale(row.last_seen_at)]
    pool = {"online_workers": len(live), "total_slots": 0, "active_slots": 0, "idle_slots": 0, "llm_gate_active": 0}
    for worker in live:
        capabilities = worker.capabilities or {}
        pool["llm_gate_active"] += max(0, int(capabilities.get("llm_gate_active", 0) or 0))
        slots = max(1, int(capabilities.get("slots", 1) or 1))
        active_slots = min(slots, max(0, int(capabilities.get("active_slots", 1 if worker.current_task_id else 0) or 0)))
        pool["total_slots"] += slots
        pool["active_slots"] += active_slots
    pool["idle_slots"] = max(0, pool["total_slots"] - pool["active_slots"])
    return pool, any((row.capabilities or {}).get("simulation") is True for row in live)
