"""Persisted 30-second platform resource samples for the admin console.

Every Worker process runs the sampler loop; ``SystemMetricSample.bucket`` is
the primary key, so the first process to write a bucket wins and the others
skip it without any leader election.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import SystemMetricSample, Task, utcnow
from .storage import configured_storage_root
from .system_config import parse_worker_concurrency
from .task_admission import llm_task_limit
from .system_probe import database_metrics, gpu_status, host_metrics, queue_status
from .tts_resource_budget import tts_batch_activity
from .task_registry import WORKER_GROUP_NAMES, task_worker_group
from .worker_registry import live_worker_pool

SAMPLE_INTERVAL_SECONDS = 30
RETENTION_DAYS = 7
_RUNNING = ("running", "cancelling")
_QUEUED = ("pending", "queued", "retrying")

logger = logging.getLogger("audiobook.metrics")


def sample_bucket(moment: datetime) -> int:
    return int(moment.timestamp() // SAMPLE_INTERVAL_SECONDS)


def collect_frame(db: Session) -> dict:
    groups = {name: {"running": 0, "queued": 0} for name in WORKER_GROUP_NAMES}
    for task_type, status, count in db.execute(
        select(Task.task_type, Task.status, func.count())
        .where(Task.status.in_(_RUNNING + _QUEUED)).group_by(Task.task_type, Task.status)
    ).all():
        bucket = groups.setdefault(task_worker_group(str(task_type)), {"running": 0, "queued": 0})
        bucket["running" if status in _RUNNING else "queued"] += int(count)
    pool, _ = live_worker_pool(db)
    try:
        root = configured_storage_root(db)
    except Exception:
        root = None
    return {
        "tasks": groups,
        "tts_batch": tts_batch_activity(db),
        "workers": pool,
        "llm_limit": parse_worker_concurrency(db=db),
        "llm_task_limit": llm_task_limit(db),
        "db": database_metrics(db),
        "redis": queue_status(include_info=True),
        "host": host_metrics(root, cpu_interval=0.5),
        "gpu": gpu_status(),
    }


def sample_once(now: datetime | None = None) -> bool:
    """Write the current bucket's frame unless another process already did."""
    moment = now or utcnow()
    bucket = sample_bucket(moment)
    with SessionLocal() as db:
        if db.get(SystemMetricSample, bucket) is not None:
            return False
        frame = collect_frame(db)
        db.rollback()  # end the read snapshot before the insert transaction
        db.add(SystemMetricSample(bucket=bucket, sampled_at=moment, data=frame))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
    return True


def prune(now: datetime | None = None, keep_days: int = RETENTION_DAYS) -> int:
    cutoff = (now or utcnow()) - timedelta(days=keep_days)
    with SessionLocal.begin() as db:
        result = db.execute(delete(SystemMetricSample).where(SystemMetricSample.sampled_at < cutoff))
        return int(result.rowcount or 0)


def run(stop) -> None:
    """Sampler loop for the Worker entry point: sample every 30 s, prune hourly."""
    last_prune = 0.0
    while not stop.is_set():
        try:
            sample_once()
            if time.monotonic() - last_prune > 3600:
                prune()
                last_prune = time.monotonic()
        except SQLAlchemyError:
            logger.warning("Metrics sample skipped: database unavailable", exc_info=True)
        except Exception:
            logger.exception("Metrics sample failed")
        stop.wait(SAMPLE_INTERVAL_SECONDS - (utcnow().timestamp() % SAMPLE_INTERVAL_SECONDS) + 0.5)
