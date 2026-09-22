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
