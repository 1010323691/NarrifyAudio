"""Process-local concurrency gates for LLM-bound and merge work.

These gates limit concurrent callers within one Python process. They do not
configure cross-task parallelism in the durable queue; that depends on the
number of Worker processes.

A4 (批次 4): the process-wide resize surface (``set_concurrency`` /
``set_merge_concurrency``) had zero production callers and hid a permanent
limit=1 behind a fake knob, so it is retired — in production both gates run
at one permit (deliberately conservative serialization of LLM / merge work).
:class:`ConcurrencyGate` is kept as the permit-gate building block; its
``set_limit``/``limit`` remain only as a test seam for exercising the
multi-permit mechanics. If a tunable production limit is ever wanted,
reintroduce the resize surface as an explicit feature.
"""
from __future__ import annotations

import threading
from typing import Callable


class ConcurrencyGate:
    """A permit gate with a configurable limit (production gates stay at 1).

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
            return True

    def release(self) -> None:
        """Free the slot this thread took (guarded against underflow)."""
        with self._cond:
            if self._active > 0:
                self._active -= 1
            self._cond.notify_all()


# Module-level singletons shared by callers in this process. Both run at the
# default limit of 1 (A4: the resize surface is retired — no production caller).
_gate = ConcurrencyGate()


def gate() -> ConcurrencyGate:
    """The process-wide LLM gate (fixed limit 1 in production)."""
    return _gate


# A second, independent gate for CPU/ffmpeg/disk-bound merge work.
_merge_gate = ConcurrencyGate()


def merge_gate() -> ConcurrencyGate:
    """The process-wide merge gate (fixed limit 1 in production)."""
    return _merge_gate
