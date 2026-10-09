from __future__ import annotations

from datetime import timedelta
from typing import Any

from .database import SessionLocal
from .models import WorkerHeartbeat, utcnow


def heartbeat(
    worker_id: str,
    *,
    status: str,
    capabilities: dict[str, Any] | None = None,
    current_task_id: str | None = None,
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


def mark_offline(worker_id: str) -> None:
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
