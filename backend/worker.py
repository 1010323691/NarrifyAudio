"""Durable queue dispatcher entrypoint.

Run ``python -m backend.worker`` next to the API.  It only publishes committed
Outbox rows; execution handlers will be added as each legacy engine is moved
behind an explicit TaskContext.  Keeping this process independent from
FastAPI prevents a worker crash from taking down authentication or queries.
"""
from __future__ import annotations

import argparse
import time

from .platform.outbox import publish_pending


def main() -> None:
    parser = argparse.ArgumentParser(description="NarrifyAudio durable task outbox dispatcher")
    parser.add_argument("--once", action="store_true", help="publish one batch and exit")
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")
    args = parser.parse_args()
    while True:
        publish_pending()
        if args.once:
            return
        time.sleep(max(0.1, args.interval))


if __name__ == "__main__":
    main()

