"""Durable outbox publisher and Redis Streams task worker entrypoint."""
from __future__ import annotations

import argparse
import os
import time

import redis

from .platform.outbox import publish_pending
from .platform.task_worker import recover_database_tasks, run_once
from .platform.worker_registry import heartbeat, mark_offline


def main() -> None:
    parser = argparse.ArgumentParser(description="NarrifyAudio durable task outbox dispatcher")
    parser.add_argument("--once", action="store_true", help="publish one batch and exit")
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")
    parser.add_argument("--worker-id", default=os.getenv("NARRIFY_WORKER_ID", "worker-local"))
    args = parser.parse_args()
    client = redis.Redis.from_url(os.getenv("NARRIFY_REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    capabilities = {
        "task_types": [
            "text.format", "book.analyze", "book.split", "script.parse",
            "audio.silences", "audio.cut", "voices.foundation", "voices.clone",
            "tts.batch", "tts.stress", "tts.merge", "bgm.analysis", "bgm.segment",
            "bgm.mix", "bgm.match", "bgm.package", "music.suggest_tags",
            "audio.zip", "audio.export", "tts.reset",
        ],
        "queue": "narrify-tasks",
    }
    heartbeat(args.worker_id, status="starting", capabilities=capabilities)
    try:
        while True:
            heartbeat(args.worker_id, status="idle", capabilities=capabilities)
            recover_database_tasks()
            publish_pending()
            result = run_once(client, worker_id=args.worker_id, block_ms=100 if args.once else int(max(100, args.interval * 1000)))
            if result not in {"idle", "skipped"}:
                heartbeat(args.worker_id, status="idle", capabilities=capabilities)
            if args.once:
                return
            time.sleep(max(0.1, args.interval))
    except Exception:
        heartbeat(args.worker_id, status="error", capabilities=capabilities)
        raise
    finally:
        mark_offline(args.worker_id)


if __name__ == "__main__":
    main()
