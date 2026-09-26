"""Durable outbox publisher and Redis Streams task worker entrypoint."""
from __future__ import annotations

import argparse
import logging
import os
import threading
import time

import redis
from sqlalchemy import func, select

from .platform.outbox import publish_pending
from .platform.task_worker import recover_database_tasks, resume_llm_unavailable_tasks, run_once
from .platform.worker_registry import heartbeat, mark_offline
from .platform.database import SessionLocal
from .platform.models import Task, TaskAttempt
from .platform.task_types import SUPPORTED_TASK_TYPES
from .core.concurrency import set_concurrency
from .platform.task_worker import _run_claim_fenced, claim_fair_task
from .platform.system_config import parse_worker_concurrency

PARSE_LLM_CONCURRENCY_MAX = 32
PARSE_WORKER_MAX = PARSE_LLM_CONCURRENCY_MAX * 2
PARSE_WORKER_MULTIPLIER = 2
PARSE_TASK_TYPES = ("script.parse",)


def parse_worker_slot_count(llm_concurrency: int, parked_worker_count: int = 0) -> int:
    """Keep the configured active queue slots available when tasks are parked."""
    configured_slots = max(1, int(llm_concurrency)) * PARSE_WORKER_MULTIPLIER
    return min(PARSE_WORKER_MAX, configured_slots + max(0, int(parked_worker_count)))


def _paused_parse_worker_count(worker_id: str) -> int:
    """Count this process's live, manually paused parse attempts.

    Their threads preserve the in-memory execution stack, so the coordinator
    provisions replacement workers while keeping the number of runnable slots
    at the configured limit. Other worker processes do not compensate for them.
    """
    with SessionLocal() as db:
        return int(db.scalar(
            select(func.count(TaskAttempt.id))
            .join(Task, Task.id == TaskAttempt.task_id)
            .where(
                Task.task_type.in_(PARSE_TASK_TYPES),
                Task.status == "paused",
                Task.error_code == "manual_pause",
                TaskAttempt.status == "running",
                TaskAttempt.worker_id.startswith(f"{worker_id}-parse-", autoescape=True),
            )
        ) or 0)


def _parse_worker_loop(worker_id: str, slot: int, stop: threading.Event, slot_stop: threading.Event) -> None:
    logger = logging.getLogger("audiobook.worker")
    slot_id = f"{worker_id}-parse-{slot:02d}"
    idle_delay = 0.25
    try:
        while not stop.is_set() and not slot_stop.is_set():
            try:
                claim = claim_fair_task(slot_id, task_types=PARSE_TASK_TYPES)
                if claim is not None:
                    idle_delay = 0.25
                    _run_claim_fenced(claim)
                else:
                    slot_stop.wait(idle_delay)
                    idle_delay = min(idle_delay * 2, 3.0)
            except Exception:
                logger.exception("Parse worker slot failed; retrying slot=%s", slot)
                slot_stop.wait(1.0)
                idle_delay = 0.25
    finally:
        mark_offline(slot_id)


def _parse_worker_coordinator(worker_id: str, stop: threading.Event) -> None:
    logger = logging.getLogger("audiobook.worker")
    workers: dict[int, tuple[threading.Thread, threading.Event]] = {}
    next_slot = 1
    paused_worker_count = 0
    last_pause_count_check = 0.0
    try:
        while not stop.is_set():
            try:
                llm_limit = parse_worker_concurrency(maximum=PARSE_LLM_CONCURRENCY_MAX)
                now = time.monotonic()
                if now - last_pause_count_check >= 1.0:
                    paused_worker_count = _paused_parse_worker_count(worker_id)
                    last_pause_count_check = now
                worker_limit = parse_worker_slot_count(llm_limit, paused_worker_count)
                set_concurrency(llm_limit)
                for slot, (thread, slot_stop) in list(workers.items()):
                    if not thread.is_alive():
                        workers.pop(slot, None)
                    elif slot > worker_limit:
                        slot_stop.set()
                occupied = set(workers)
                while len(workers) < worker_limit and not stop.is_set():
                    slot = next((item for item in range(next_slot, PARSE_WORKER_MAX + 1) if item not in occupied), None)
                    if slot is None:
                        slot = next((item for item in range(1, next_slot) if item not in occupied), None)
                    if slot is None:
                        break
                    next_slot = slot % PARSE_WORKER_MAX + 1
                    slot_stop = threading.Event()
                    thread = threading.Thread(
                        target=_parse_worker_loop,
                        args=(worker_id, slot, stop, slot_stop),
                        name=f"script-parse-{slot:02d}",
                        daemon=True,
                    )
                    workers[slot] = (thread, slot_stop)
                    occupied.add(slot)
                    thread.start()
            except Exception:
                logger.exception("Parse worker coordinator iteration failed; retrying")
            stop.wait(0.2)
    finally:
        for _thread, slot_stop in workers.values():
            slot_stop.set()
        for slot in workers:
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
    parse_workers: list[threading.Thread] = []
    if not args.once:
        parse_workers.append(threading.Thread(
            target=_parse_worker_coordinator,
            args=(args.worker_id, stop),
            name="script-parse-coordinator",
            daemon=True,
        ))
        parse_workers[0].start()
        threading.Thread(
            target=_llm_recovery_probe_loop, args=(stop,),
            name="llm-recovery-probe", daemon=True,
        ).start()
    try:
        while True:
            heartbeat(args.worker_id, status="idle", capabilities=capabilities)
            recover_database_tasks()
            publish_pending()
            result = run_once(
                client,
                worker_id=args.worker_id,
                block_ms=100 if args.once else int(max(100, args.interval * 1000)),
                excluded_task_types=() if args.once else PARSE_TASK_TYPES,
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


def _llm_recovery_probe_loop(stop: threading.Event) -> None:
    logger = logging.getLogger("audiobook.worker")
    while not stop.is_set():
        try:
            resumed = resume_llm_unavailable_tasks()
            if resumed:
                logger.info("LLM service recovered; redispatched %d paused tasks", resumed)
        except Exception:
            logger.exception("LLM recovery probe failed; next check will retry")
        stop.wait(60.0)


if __name__ == "__main__":
    main()
