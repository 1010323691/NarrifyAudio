"""Durable outbox publisher and Redis Streams task worker entrypoint."""
from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from typing import Callable

import redis
from sqlalchemy import func, select
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as PoolTimeoutError

from .platform.outbox import publish_pending
from .platform.task_worker import recover_database_tasks, resume_llm_unavailable_tasks, run_once
from .platform.worker_registry import heartbeat, mark_offline
from .platform.database import SessionLocal
from .platform.models import Task, TaskAttempt
from .platform.task_types import SUPPORTED_TASK_TYPES
from .core.concurrency import merge_gate, set_concurrency
from .platform.task_worker import _run_claim_fenced, claim_fair_task
from .platform.system_config import parse_worker_concurrency
from .services.project_retention import purge_expired_projects
from .platform.resource_retention import purge_resource_artifacts
from .platform.gpu_scheduler.runtime import Scheduler
from .platform.gpu_scheduler.config import load_config as load_gpu_config
from .platform.gpu_scheduler.store import read_state as read_gpu_state
from .platform.task_registry import TASK_TYPES
from .platform.task_admission import LLM_TASK_MULTIPLIER, LLM_TASK_TYPES

PARSE_LLM_CONCURRENCY_MAX = 32
PARSE_WORKER_MAX = PARSE_LLM_CONCURRENCY_MAX * 2
PARSE_WORKER_MULTIPLIER = LLM_TASK_MULTIPLIER
PARSE_TASK_TYPES = ("script.parse",)
MERGE_TASK_TYPES = ("tts.merge", "bgm.mix")
GPU_TASK_TYPES = tuple(name for name, spec in TASK_TYPES.items() if spec.gpu_initial)
MECHANICAL_TASK_TYPES = tuple(name for name, spec in TASK_TYPES.items() if not spec.gpu_initial)
WORKER_LANES = {"mechanical": MECHANICAL_TASK_TYPES, "model": GPU_TASK_TYPES}


def _dispatch_excluded_types(lane: str, *, once: bool, managed: bool) -> tuple[str, ...]:
    excluded = set(SUPPORTED_TASK_TYPES) - set(WORKER_LANES[lane])
    if not once:
        excluded.update(MERGE_TASK_TYPES)
        excluded.update(GPU_TASK_TYPES if managed else
                        (name for name, spec in TASK_TYPES.items() if spec.gpu_initial == "LLM"))
    return tuple(sorted(excluded))


def _retry_database_operation(
    operation: Callable[[], object], stop: threading.Event, *, once: bool = False,
) -> bool:
    """Retry a dispatch unit, leaving any claimed attempt behind its lease fence."""
    delay = 1.0
    while not stop.is_set():
        try:
            operation()
            return True
        except (OperationalError, InterfaceError, PoolTimeoutError) as exc:
            code = getattr(getattr(exc, "orig", None), "sqlstate", None)
            transient = code is None or code.startswith(("08", "53")) or code in {
                "40001", "40P01", "57P01", "57P02", "57P03",
            }
            if once or not transient:
                raise
            # Log locally: writing an error heartbeat needs the unavailable DB.
            logging.getLogger("audiobook.worker").warning(
                "Database unavailable; retrying in %.1fs (%s)", delay, type(exc).__name__,
            )
            stop.wait(delay)
            delay = min(delay * 2, 30.0)
    return False


def _mark_offline_safely(worker_id: str) -> None:
    try:
        mark_offline(worker_id)
    except Exception:
        logging.getLogger("audiobook.worker").warning("Could not mark worker offline: %s", worker_id)


def _dispatch_loop(client, worker_id, capabilities, stop, *, interval, once, lane="mechanical"):
    def dispatch():
        heartbeat(worker_id, status="idle", capabilities=capabilities)
        recover_database_tasks()
        publish_pending()
        result = run_once(
            client,
            worker_id=worker_id,
            block_ms=100 if once else int(max(100, interval * 1000)),
            excluded_task_types=_dispatch_excluded_types(
                lane, once=once,
                managed=False if once else load_gpu_config().enabled or read_gpu_state()["managed"],
            ),
        )
        if result not in {"idle", "skipped"}:
            heartbeat(worker_id, status="idle", capabilities=capabilities)

    while _retry_database_operation(dispatch, stop, once=once):
        if once:
            return
        stop.wait(max(0.1, interval))


def _gpu_task_loop(worker_id: str, service: str, stop: threading.Event, slot: int = 0) -> None:
    """Dedicated channels allow a mixed task to wait without blocking the other side."""
    types = tuple(name for name, spec in TASK_TYPES.items() if spec.gpu_initial == service and name not in PARSE_TASK_TYPES)
    slot_id = f"{worker_id}-gpu-{service.lower()}-{slot}"
    logger = logging.getLogger("audiobook.worker")
    try:
        while not stop.is_set():
            try:
                if service == "LLM" or load_gpu_config().enabled or read_gpu_state()["managed"]:
                    heartbeat(slot_id, status="idle", capabilities={"task_types": list(types), "slots": 1})
                    claim = claim_fair_task(slot_id, task_types=types)
                    if claim:
                        _run_claim_fenced(claim)
                        continue
            except Exception:
                logger.exception("GPU task channel failed service=%s", service)
            stop.wait(0.5)
    finally:
        _mark_offline_safely(slot_id)


def _merge_worker_loop(worker_id: str, slot: int, stop: threading.Event) -> None:
    """Execute CPU audio jobs independently of the synchronous general queue."""
    slot_id = f"{worker_id}-merge-{slot:02d}"
    logger = logging.getLogger("audiobook.worker")
    try:
        while not stop.is_set():
            try:
                heartbeat(slot_id, status="idle", capabilities={"task_types": list(MERGE_TASK_TYPES), "slots": 1})
                claim = claim_fair_task(slot_id, task_types=MERGE_TASK_TYPES)
                if claim is not None:
                    _run_claim_fenced(claim)
                    continue
            except Exception:
                logger.exception("Merge worker slot failed; retrying slot=%s", slot)
            stop.wait(0.5)
    finally:
        _mark_offline_safely(slot_id)


def _start_merge_workers(worker_id: str, stop: threading.Event) -> list[threading.Thread]:
    workers = []
    for slot in range(1, merge_gate().limit + 1):
        thread = threading.Thread(
            target=_merge_worker_loop, args=(worker_id, slot, stop),
            name=f"audio-merge-{slot:02d}", daemon=True,
        )
        thread.start()
        workers.append(thread)
    return workers


def parse_worker_slot_count(llm_concurrency: int, parked_worker_count: int = 0) -> int:
    """Provision local LLM threads; claim admission enforces the global task cap."""
    configured_slots = max(1, int(llm_concurrency)) * PARSE_WORKER_MULTIPLIER
    return min(PARSE_WORKER_MAX, configured_slots + max(0, int(parked_worker_count)))


def _paused_parse_worker_count(worker_id: str) -> int:
    """Count this process's parked LLM attempts (historical parse worker IDs).

    Their threads preserve the in-memory execution stack, so the coordinator
    provisions replacement threads. The shared claim cap still bounds runnable
    tasks across all processes; other processes do not compensate for these threads.
    """
    with SessionLocal() as db:
        return int(db.scalar(
            select(func.count(TaskAttempt.id))
            .join(Task, Task.id == TaskAttempt.task_id)
            .where(
                Task.task_type.in_(LLM_TASK_TYPES),
                ((Task.status == "paused") & (Task.error_code == "manual_pause"))
                | ((Task.status == "queued") & (Task.error_code == "resume_waiting")),
                TaskAttempt.status == "running",
                TaskAttempt.worker_id.startswith(f"{worker_id}-parse-", autoescape=True),
            )
        ) or 0)


def _project_retention_loop(stop: threading.Event) -> None:
    """Check expiry immediately, retry startup contention, then run daily."""
    logger = logging.getLogger("audiobook.worker")
    startup_check = True
    while not stop.is_set():
        purged = 0
        try:
            purged = purge_expired_projects()
            purge_resource_artifacts()
        except Exception:
            logger.exception("Daily project trash cleanup failed")
        # The worker can start alongside recovery/migration activity. A second
        # near-startup pass ensures an initial lock/migration skip is not delayed
        # until tomorrow. A successful purge proceeds directly to the daily cadence.
        if startup_check and purged == 0:
            startup_check = False
            stop.wait(60)
            continue
        startup_check = False
        stop.wait(24 * 60 * 60)


def _parse_worker_loop(worker_id: str, slot: int, stop: threading.Event, slot_stop: threading.Event) -> None:
    logger = logging.getLogger("audiobook.worker")
    slot_id = f"{worker_id}-parse-{slot:02d}"
    idle_delay = 0.25
    try:
        while not stop.is_set() and not slot_stop.is_set():
            try:
                claim = claim_fair_task(slot_id, task_types=LLM_TASK_TYPES)
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
        _mark_offline_safely(slot_id)


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
            _mark_offline_safely(f"{worker_id}-parse-{slot:02d}")


def main() -> None:
    parser = argparse.ArgumentParser(description="NarrifyAudio durable task outbox dispatcher")
    parser.add_argument("--once", action="store_true", help="publish one batch and exit")
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")
    parser.add_argument("--worker-id", default=os.getenv("NARRIFY_WORKER_ID", f"worker-local-{os.getpid()}"))
    parser.add_argument(
        "--task-lane", choices=tuple(WORKER_LANES),
        default=os.getenv("NARRIFY_WORKER_LANE", "mechanical"),
        help="mechanical tasks only, or LLM/TTS model tasks only",
    )
    args = parser.parse_args()
    if args.task_lane not in WORKER_LANES:
        parser.error("NARRIFY_WORKER_LANE must be mechanical or model")
    client = redis.Redis.from_url(os.getenv("NARRIFY_REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    capabilities = {
        "task_types": [
            *sorted(WORKER_LANES[args.task_lane]),
        ],
        "queue": "narrify-tasks",
        "task_lane": args.task_lane,
    }
    stop = threading.Event()
    if not _retry_database_operation(
        lambda: heartbeat(args.worker_id, status="starting", capabilities=capabilities),
        stop, once=args.once,
    ):
        return
    parse_workers: list[threading.Thread] = []
    retention_worker: threading.Thread | None = None
    delivery_worker: threading.Thread | None = None
    gpu_workers: list[threading.Thread] = []
    merge_workers: list[threading.Thread] = []
    scheduler_thread: threading.Thread | None = None
    if not args.once:
        if args.task_lane == "mechanical":
            merge_workers = _start_merge_workers(args.worker_id, stop)
            retention_worker = threading.Thread(
                target=_project_retention_loop, args=(stop,),
                name="project-retention-cleanup", daemon=True,
            )
            retention_worker.start()
            from .platform.delivery_maintenance import run as maintain_deliveries
            delivery_worker = threading.Thread(target=maintain_deliveries, args=(stop,),
                name="delivery-index-maintenance", daemon=True)
            delivery_worker.start()
        if args.task_lane == "model":
            scheduler_thread = threading.Thread(target=Scheduler(stop).run, name="gpu-scheduler", daemon=False)
            scheduler_thread.start()
            # All LLM types share the coordinator below and the host-wide task
            # budget. TTS retains its independent single-channel execution path.
            thread = threading.Thread(target=_gpu_task_loop, args=(args.worker_id, "TTS", stop, 0),
                                      name="gpu-task-tts-0", daemon=True)
            thread.start()
            gpu_workers.append(thread)
            parse_workers.append(threading.Thread(
                target=_parse_worker_coordinator, args=(args.worker_id, stop),
                name="script-parse-coordinator", daemon=True,
            ))
            parse_workers[0].start()
            threading.Thread(
                target=_llm_recovery_probe_loop, args=(stop,),
                name="llm-recovery-probe", daemon=True,
            ).start()
    try:
        _dispatch_loop(client, args.worker_id, capabilities, stop, interval=args.interval, once=args.once, lane=args.task_lane)
    except Exception:
        try:
            heartbeat(args.worker_id, status="error", capabilities=capabilities)
        except Exception:
            logging.getLogger("audiobook.worker").warning("Could not write worker error heartbeat")
        raise
    finally:
        stop.set()
        if retention_worker is not None:
            retention_worker.join(timeout=5)
        if delivery_worker is not None:
            delivery_worker.join(timeout=5)
        for thread in parse_workers:
            thread.join(timeout=2)
        for thread in gpu_workers:
            thread.join(timeout=2)
        for thread in merge_workers:
            thread.join(timeout=2)
        if scheduler_thread:
            scheduler_thread.join(timeout=5)
        _mark_offline_safely(args.worker_id)


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
