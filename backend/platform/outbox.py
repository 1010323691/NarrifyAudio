from __future__ import annotations

import json
import os

import redis
from sqlalchemy import select

from .database import SessionLocal
from .models import OutboxEvent, utcnow


STREAM_NAME = os.getenv("NARRIFY_TASK_STREAM", "narrify:task-events")


def _payload(event: OutboxEvent) -> str:
    return json.dumps(
        {
            "event_id": event.id,
            "event_type": event.event_type,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "payload": event.payload,
            "created_at": event.created_at.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def publish_pending(redis_url: str | None = None, limit: int = 100) -> int:
    """Publish committed outbox rows to Redis Streams.

    The database row is marked only after ``XADD`` succeeds.  If the process
    dies between those operations, the same event can be emitted again; the
    event id is stable and consumers must deduplicate it before applying a
    business effect.  This is the intentional at-least-once boundary.
    """
    client = redis.Redis.from_url(redis_url or os.getenv("NARRIFY_REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    published = 0
    with SessionLocal() as db:
        rows = db.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= utcnow())
            .order_by(OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for event in rows:
            try:
                client.xadd(STREAM_NAME, {"event": _payload(event)}, maxlen=100_000, approximate=True)
            except redis.RedisError:
                event.attempts += 1
                db.commit()
                continue
            event.attempts += 1
            event.published_at = utcnow()
            db.commit()
            published += 1
    return published
