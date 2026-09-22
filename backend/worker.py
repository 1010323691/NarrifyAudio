"""Durable outbox publisher and Redis Streams task worker entrypoint."""
from __future__ import annotations

import argparse
import os
import time

import redis

from .platform.outbox import publish_pending
from .platform.task_worker import recover_database_tasks, run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="NarrifyAudio durable task outbox dispatcher")
    parser.add_argument("--once", action="store_true", help="publish one batch and exit")
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")
    parser.add_argument("--worker-id", default=os.getenv("NARRIFY_WORKER_ID", "worker-local"))
    args = parser.parse_args()
    client = redis.Redis.from_url(os.getenv("NARRIFY_REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    while True:
        recover_database_tasks()
        publish_pending()
        run_once(client, worker_id=args.worker_id, block_ms=100 if args.once else int(max(100, args.interval * 1000)))
        if args.once:
            return
        time.sleep(max(0.1, args.interval))


if __name__ == "__main__":
    main()
