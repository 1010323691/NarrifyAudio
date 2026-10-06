"""Supervise independent mechanical and model worker processes."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger('audiobook.worker_pool')


def worker_command(lane: str, slot: int, interval: float, pool_id: str) -> list[str]:
    return [sys.executable, '-m', 'backend.worker', '--task-lane', lane,
            '--worker-id', f'{pool_id}-{lane}-{slot}', '--interval', str(interval)]


def stop_children(children: list[subprocess.Popen], timeout: float = 15) -> None:
    for child in children:
        if child.poll() is None:
            if os.name == 'posix':
                try:
                    child.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
            else:
                child.terminate()
    deadline = time.monotonic() + timeout
    for child in children:
        if child.poll() is None:
            try:
                child.wait(timeout=max(.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def supervise(counts: dict[str, int], *, interval: float, stop: threading.Event, pool_id: str) -> None:
    root = Path(__file__).resolve().parents[1]
    slots = {(lane, slot): {'process': None, 'restart_at': 0., 'delay': 1., 'started': 0.}
             for lane, count in counts.items() for slot in range(count)}
    children: list[subprocess.Popen] = []
    try:
        while not stop.is_set():
            now = time.monotonic()
            for (lane, slot), state in slots.items():
                process = state['process']
                if process is not None and process.poll() is not None:
                    if now-state['started'] >= 60:
                        state['delay'] = 1.
                    log.warning('%s worker %s exited (%s); restart in %.1fs', lane, slot, process.returncode, state['delay'])
                    state['restart_at'] = now+state['delay']
                    state['delay'] = min(state['delay']*2, 30.)
                    children.remove(process)
                    state['process'] = None
                if state['process'] is None and now >= state['restart_at']:
                    child = subprocess.Popen(worker_command(lane, slot, interval, pool_id), cwd=root)
                    children.append(child)
                    state.update(process=child, started=now)
                    log.info('Started %s worker %s pid=%s', lane, slot, child.pid)
            stop.wait(.5)
    finally:
        stop_children(children)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mechanical-workers', type=int, default=4)
    parser.add_argument('--model-workers', type=int, default=4)
    parser.add_argument('--interval', type=float, default=1.)
    args = parser.parse_args()
    if min(args.mechanical_workers, args.model_workers) < 1 or args.interval <= 0:
        parser.error('both worker counts and interval must be positive')
    logging.basicConfig(level=logging.INFO)
    stop = threading.Event()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, lambda *_: stop.set())
    try:
        supervise({'mechanical': args.mechanical_workers, 'model': args.model_workers},
                  interval=args.interval, stop=stop, pool_id=f'pool-{os.getpid()}')
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    main()
