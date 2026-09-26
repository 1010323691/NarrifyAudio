"""Durable outbox publisher and Redis Streams task worker entrypoint."""
from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import redis

from .platform.outbox import publish_pending
from .platform import system_config  # registers core.config's feature-defaults provider
from .platform.task_worker import recover_database_tasks, resume_llm_unavailable_tasks, run_once
from .platform.worker_registry import heartbeat, mark_offline
from .platform.task_types import SUPPORTED_TASK_TYPES
from .core.config import get_config
from .core.concurrency import set_concurrency
from .platform.task_worker import _run_claim_fenced, claim_fair_task

PARSE_WORKER_MAX = 32
PARSE_TASK_TYPES = ("script.parse",)
NON_PARSE_TASK_TYPES = ("script.parse",)


def _parse_worker_loop(worker_id: str, slot: int, stop: threading.Event) -> None:
    slot_id = f"{worker_id}-parse-{slot:02d}"
    claim = claim_fair_task(slot_id, task_types=PARSE_TASK_TYPES)
    if claim is not None:
        _run_claim_fenced(claim)
    else:
        stop.wait(0.2)


def _parse_worker_coordinator(worker_id: str, stop: threading.Event) -> None:
    active: dict[object, int] = {}
    next_slot = 1
    pool = ThreadPoolExecutor(max_workers=PARSE_WORKER_MAX, thread_name_prefix="script-parse")
    try:
        while not stop.is_set():
            limit = max(1, min(PARSE_WORKER_MAX, int(get_config().generation.parse_worker_concurrency or 1)))
            set_concurrency(limit)
            while len(active) < limit:
                occupied = set(active.values())
                slot = next((item for item in range(next_slot, PARSE_WORKER_MAX + 1) if item not in occupied), None)
                if slot is None:
                    slot = next((item for item in range(1, next_slot) if item not in occupied), None)
                if slot is None:
                    break
                next_slot = slot % PARSE_WORKER_MAX + 1
                active[pool.submit(_parse_worker_loop, worker_id, slot, stop)] = slot
            if active:
                completed, _ = wait(tuple(active), timeout=0.2, return_when=FIRST_COMPLETED)
                for future in completed:
                    slot = active.pop(future)
                    try:
                        future.result()
                    finally:
                        mark_offline(f"{worker_id}-parse-{slot:02d}")
            else:
                stop.wait(0.2)
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        for slot in active.values():
            mark_offline(f"{worker_id}-parse-{slot:02d}")


def main() -> None:
    parser = argparse.ArgumentParser(description="NarrifyAudio durable task outbox dispatcher")
    parser.add_argument("--once", action="store_true", help="publish one batch and exit")
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")
    parser.add_argument("--worker-id", default=os.getenv("NARRIFY_WORKER_ID", f"worker-local-{os.getpid()}"))
    args = parser.parse_args()
    client = redis.Redis.from_url(os.getenv("NARRIFY_REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    capabilities = {
        "task_types": [
            *sorted(SUPPORTED_TASK_TYPES),
        ],
        "queue": "narrify-tasks",
    }
    heartbeat(args.worker_id, status="starting", capabilities=capabilities)
    stop = threading.Event()
    next_llm_probe = 0.0
    parse_workers: list[threading.Thread] = []
    if not args.once:
        parse_workers.append(threading.Thread(
            target=_parse_worker_coordinator,
            args=(args.worker_id, stop),
            name="script-parse-coordinator",
            daemon=True,
        ))
        parse_workers[0].start()
    try:
        while True:
            heartbeat(args.worker_id, status="idle", capabilities=capabilities)
            recover_database_tasks()
            now = time.monotonic()
            if now >= next_llm_probe:
                resumed = resume_llm_unavailable_tasks()
                if resumed:
                    logging.getLogger("audiobook.worker").info(
                        "LLM 服务恢复，重新排队 %d 个暂停任务", resumed,
                    )
                next_llm_probe = now + 60.0
            publish_pending()
            result = run_once(
                client,
                worker_id=args.worker_id,
                block_ms=100 if args.once else int(max(100, args.interval * 1000)),
                excluded_task_types=() if args.once else NON_PARSE_TASK_TYPES,
            )
            if result not in {"idle", "skipped"}:
                heartbeat(args.worker_id, status="idle", capabilities=capabilities)
            if args.once:
                return
            time.sleep(max(0.1, args.interval))
    except Exception:
        heartbeat(args.worker_id, status="error", capabilities=capabilities)
        raise
    finally:
        stop.set()
        for thread in parse_workers:
            thread.join(timeout=2)
        mark_offline(args.worker_id)


if __name__ == "__main__":
    main()
