"""Tests for the global LLM concurrency gate (``core/concurrency.py``).

The gate bounds how many parse tasks may run their LLM job at once; the rest of a
batch queue behind it. Fresh :class:`ConcurrencyGate` instances are used so the tests
never disturb the process's shared gate — except one wiring check, which restores the
previous limit on its way out.
"""
from __future__ import annotations

import threading
import time

from backend.core import concurrency


def test_limit_clamps_to_at_least_one():
    g = concurrency.ConcurrencyGate()
    g.set_limit(3)
    assert g.limit == 3
    g.set_limit(0)  # clamped: there is no "unlimited" mode
    assert g.limit == 1
    g.set_limit(-5)
    assert g.limit == 1
    g.set_limit(2)
    assert g.limit == 2


def test_acquire_release_stays_balanced():
    g = concurrency.ConcurrencyGate()
    g.set_limit(3)
    assert g.active == 0
    g.acquire()
    g.acquire()
    assert g.active == 2
    g.release()
    assert g.active == 1
    g.release()
    assert g.active == 0
    # Releasing below zero is a guarded no-op (never goes negative).
    g.release()
    assert g.active == 0


def test_slots_up_to_the_limit_acquire_immediately():
    g = concurrency.ConcurrencyGate()
    g.set_limit(2)
    g.acquire()
    g.acquire()  # both fit within the limit — neither blocks
    assert g.active == 2
    g.release()
    g.release()
    assert g.active == 0


def test_excess_acquire_blocks_until_release():
    g = concurrency.ConcurrencyGate()
    g.set_limit(1)
    g.acquire()  # hold the only slot
    got = threading.Event()

    def worker():
        g.acquire()
        got.set()
        g.release()

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.1)  # give the worker a chance to reach the (blocked) acquire
    assert not got.is_set(), "second acquire must block while the slot is held"
    g.release()  # free the slot; the queued worker must now proceed
    t.join(timeout=5)
    assert not t.is_alive(), "worker should have acquired after the release"
    assert got.is_set()


def test_set_concurrency_sizes_the_shared_gate():
    before = concurrency.gate().limit
    try:
        concurrency.set_concurrency(4)
        assert concurrency.gate().limit == 4
        concurrency.set_concurrency(0)  # clamped
        assert concurrency.gate().limit == 1
    finally:
        concurrency.set_concurrency(before)  # restore for any other test


# -- cooperative (stop_check) acquire -------------------------------------------------

def test_acquire_stop_check_aborts_wait_without_slot():
    g = concurrency.ConcurrencyGate()
    g.set_limit(1)
    g.acquire()  # hold the only slot
    result = {}
    stop = threading.Event()

    def worker():
        result["ok"] = g.acquire(stop_check=stop.is_set)

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.1)  # worker is now blocked in the cooperative wait
    assert t.is_alive() and "ok" not in result
    stop.set()
    t.join(timeout=2)  # must abort within one 0.2 s poll — not block forever
    assert not t.is_alive(), "stop_check never aborted the wait"
    assert result["ok"] is False  # aborted WITHOUT taking a slot
    assert g.active == 1  # the waiter took nothing; only the original holder remains
    g.release()
    assert g.active == 0


def test_acquire_stop_check_blocks_until_release_when_not_stopped():
    # stop_check stays False → behaves like the old blocking acquire: the worker
    # proceeds once the slot frees.
    g = concurrency.ConcurrencyGate()
    g.set_limit(1)
    g.acquire()  # hold the only slot
    got = threading.Event()

    def worker():
        assert g.acquire(stop_check=lambda: False) is True
        got.set()
        g.release()

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.1)
    assert not got.is_set()  # still blocked while the slot is held
    g.release()
    t.join(timeout=5)
    assert not t.is_alive() and got.is_set()
    assert g.active == 0  # balanced: holder + one (later) waiter, two releases


def test_acquire_stop_check_none_keeps_old_semantics():
    # ``stop_check=None`` returns True and never consults any predicate — the
    # pre-change call shape, whose return value existing callers ignore.
    g = concurrency.ConcurrencyGate()
    g.set_limit(1)
    assert g.acquire() is True
    g.release()
    assert g.active == 0
