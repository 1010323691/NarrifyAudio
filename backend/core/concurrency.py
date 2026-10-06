"""Process-local concurrency gates for LLM-bound and merge work.

These gates limit concurrent callers within one Python process. Text-parse
Worker threads resize the LLM gate from the administrator-configured per-process
parse concurrency; merge work shares a CPU-sized gate with BGM mixing.
"""
from __future__ import annotations

import os
import threading
from typing import Callable


class ConcurrencyGate:
    """A permit gate with a configurable limit.

    ``set_limit(n)`` clamps to ``>= 1`` and wakes any waiters. ``acquire``
    blocks until a slot is free; ``release`` frees a slot and wakes one
    waiter. ``active`` reports how many slots are currently held. There is
    deliberately no "unlimited" mode — the whole point is to cap in-flight
    work — so ``acquire``/``release`` stay perfectly balanced.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._limit = 1  # always >= 1 (no "unlimited" mode)
        self._active = 0
        self._local = threading.local()

    def set_limit(self, n: int) -> None:
        """Set the max number of concurrent holders (clamped to ``>= 1``)."""
        with self._cond:
            self._limit = max(1, int(n or 1))
            self._cond.notify_all()

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @property
    def active(self) -> int:
        """How many slots are currently held (useful for tests / UI)."""
        with self._cond:
            return self._active

    def acquire(self, stop_check: Callable[[], bool] | None = None) -> bool:
        """Block until a slot is free, then take it (returns ``True``).

        When ``stop_check`` is supplied the wait is cooperative: the predicate is
        re-checked every 0.2 s while blocked, and once it returns ``True`` the wait is
        abandoned WITHOUT taking a slot (returns ``False``). The caller must then abort
        (e.g. raise ``TaskCancelled``) and must NOT call ``release`` — no slot was
        taken. ``stop_check=None`` keeps the notify-driven blocking semantics
        (also returns ``True``).
        """
        with self._cond:
            while self._active >= self._limit:
                if stop_check is not None:
                    if stop_check():
                        return False
                    self._cond.wait(0.2)
                else:
                    self._cond.wait()
            self._active += 1
            self._local.held = getattr(self._local, "held", 0) + 1
            return True

    def release(self) -> None:
        """Free one slot held by this thread (guarded against underflow)."""
        held = getattr(self._local, "held", 0)
        if held <= 0:
            return
        with self._cond:
            if self._active > 0:
                self._active -= 1
                self._local.held = held - 1
            self._cond.notify_all()

    def suspend_current_thread(self) -> int:
        """Temporarily free every permit held by this thread and return its count."""
        held = getattr(self._local, "held", 0)
        for _ in range(held):
            self.release()
        return held

    def restore_current_thread(self, count: int, stop_check: Callable[[], bool]) -> bool:
        """Reacquire a suspended permit set, rolling back if the wait is interrupted."""
        acquired = 0
        for _ in range(max(0, count)):
            if not self.acquire(stop_check=stop_check):
                for _ in range(acquired):
                    self.release()
                return False
            acquired += 1
        return True


# Module-level singletons shared by callers in this process.
_gate = ConcurrencyGate()


def gate() -> ConcurrencyGate:
    """The process-wide LLM gate used by text parsing."""
    return _gate


def set_concurrency(n: int) -> None:
    """Set the process-local text-parse LLM concurrency limit."""
    _gate.set_limit(n)


# A second, independent gate for CPU/ffmpeg/disk-bound merge work.
def merge_concurrency_limit() -> int:
    """Reserve CPU and memory headroom: half the logical CPUs, at most four jobs."""
    return max(1, min(4, (os.cpu_count() or 4) // 2))


_merge_gate = ConcurrencyGate()
_merge_gate.set_limit(merge_concurrency_limit())


def merge_gate() -> ConcurrencyGate:
    """The process-wide CPU-sized gate shared by merges and BGM mixes."""
    return _merge_gate
