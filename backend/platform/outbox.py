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
    if not 1 <= limit <= 100:
        raise ValueError("outbox batch must contain 1..100 events")
    client = redis.Redis.from_url(redis_url or os.getenv("NARRIFY_REDIS_URL", "redis://localhost:6379/0"),
                                 decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    try:
        return _publish_batch(client, limit)
    finally:
        client.close()


def _publish_batch(client, limit):
    published = 0
    with SessionLocal() as db:
        rows = db.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= utcnow())
            .order_by(OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        if not rows:
            return 0
        responses = []
        with client.pipeline(transaction=False) as pipeline:
            for event in rows:
                pipeline.xadd(STREAM_NAME, {"event": _payload(event)}, maxlen=100_000, approximate=True)
            try:
                responses = pipeline.execute(raise_on_error=False)
            except redis.RedisError:
                # Some XADDs may have arrived before the connection failed.
                # Keep every unknown row pending and replay its stable event id.
                pass
        if len(responses) != len(rows):
            responses = [None] * len(rows)
        now = utcnow()
        for event, response in zip(rows, responses):
            event.attempts += 1
            if isinstance(response, (str, bytes)) and response:
                event.published_at = now
                published += 1
        db.commit()
    return published
