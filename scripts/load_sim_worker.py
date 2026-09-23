"""Bounded synthetic LLM/TTS workers for the local admin load demonstration."""
from __future__ import annotations

import argparse
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from backend.platform.task_worker import _process_claim, claim_fair_task
from backend.platform.worker_registry import heartbeat, mark_offline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm-slots", type=int, default=4)
    parser.add_argument("--llm-seconds", type=float, default=10)
    parser.add_argument("--tts-slots", type=int, default=96)
    parser.add_argument("--tts-seconds", type=float, default=50)
    args = parser.parse_args()
    if min(args.llm_slots, args.tts_slots) < 1:
        parser.error("slot counts must be positive")
    if min(args.llm_seconds, args.tts_seconds) < 0:
        parser.error("durations cannot be negative")

    stopping = threading.Event()
    active = {"llm": 0, "tts": 0}
    active_lock = threading.Lock()
    pools = {
        "llm": {"task_type": "script.parse", "slots": args.llm_slots, "seconds": args.llm_seconds},
        "tts": {"task_type": "tts.batch", "slots": args.tts_slots, "seconds": args.tts_seconds},
    }

    def stop(_signum, _frame) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def consume(kind: str, claim, slots_available: threading.BoundedSemaphore) -> None:
        worker_id = f"load-sim-{kind}"
        with active_lock:
            active[kind] += 1
        try:
            _process_claim(claim)
        finally:
            with active_lock:
                active[kind] -= 1
            slots_available.release()

    def dispatch(kind: str, executor: ThreadPoolExecutor, slots_available: threading.BoundedSemaphore) -> None:
        worker_id = f"load-sim-{kind}"
        task_type = str(pools[kind]["task_type"])
        lease_seconds = max(600, int(float(pools[kind]["seconds"]) * 3))
        while not stopping.is_set():
            if not slots_available.acquire(timeout=0.2):
                continue
            try:
                claim = claim_fair_task(worker_id, lease_seconds=lease_seconds, task_types=(task_type,))
            except Exception:
                slots_available.release()
                if stopping.wait(0.5):
                    break
                continue
            if claim is None:
                slots_available.release()
                stopping.wait(0.5)
                continue
            executor.submit(consume, kind, claim, slots_available)

    executors: list[ThreadPoolExecutor] = []
    dispatchers: list[threading.Thread] = []
    try:
        for kind, config in pools.items():
            slots = int(config["slots"])
            capabilities = {
                "task_types": [config["task_type"]],
                "simulation": True,
                "slots": slots,
                "duration_seconds": config["seconds"],
            }
            heartbeat(f"load-sim-{kind}", status="idle", capabilities=capabilities)
            executor = ThreadPoolExecutor(max_workers=slots, thread_name_prefix=f"load-{kind}")
            executors.append(executor)
            slots_available = threading.BoundedSemaphore(slots)
            dispatcher = threading.Thread(
                target=dispatch,
                args=(kind, executor, slots_available),
                name=f"dispatch-{kind}",
                daemon=True,
            )
            dispatchers.append(dispatcher)
            dispatcher.start()

        while not stopping.wait(2):
            for kind, config in pools.items():
                with active_lock:
                    count = active[kind]
                heartbeat(
                    f"load-sim-{kind}",
                    status="processing" if count else "idle",
                    capabilities={
                        "task_types": [config["task_type"]],
                        "simulation": True,
                        "slots": int(config["slots"]),
                        "active_slots": count,
                        "duration_seconds": config["seconds"],
                    },
                )
    finally:
        stopping.set()
        for dispatcher in dispatchers:
            dispatcher.join(timeout=2)
        for executor in executors:
            executor.shutdown(wait=True, cancel_futures=True)
        for kind in pools:
            mark_offline(f"load-sim-{kind}")


if __name__ == "__main__":
    main()
