"""Unit tests for the LLM 吞吐量 10-second-window average (_cps10).

The 吞吐量 card shows a genuinely measured, smooth average of the LLM generation rate over the
retained (≤10 s) span — (chars generated within the span) / that span — computed in the backend
from the real streamed chars (never estimated / animated). These tests lock in the pure window
math: the empty/single-sample and zero-span guards, the per-span division, and the partial-span
averaging for tasks younger than the window.
"""
from collections import deque

from backend.platform.task_context import _cps10


def _samples(pairs) -> deque[tuple[float, int]]:
    return deque(pairs)


# -- empty window / single sample -----------------------------------------------------------
def test_cps10_needs_two_samples():
    assert _cps10(deque(), 0, 0.0) == 0.0
    assert _cps10(_samples([(0.0, 10)]), 10, 0.0) == 0.0  # a single sample has no span


def test_cps10_zero_span_is_zero():
    # Two samples almost coincident -> guard against a divide-by-~0 blow-up (the exact
    # per-flush jitter this window average is meant to eliminate).
    assert _cps10(_samples([(0.0, 10), (0.0005, 20)]), 20, 0.0005) == 0.0


def test_cps10_is_chars_over_span():
    # The cumulative total is monotonic, so (total - first_total) is the chars in the span.
    assert _cps10(_samples([(0.0, 100), (10.0, 600)]), 600, 10.0) == 50.0  # 500 chars / 10 s
    assert _cps10(_samples([(0.0, 0), (10.0, 5000)]), 5000, 10.0) == 500.0  # 5000 chars / 10 s


def test_cps10_uses_first_sample_as_span_start():
    # The average is over (now - first sample), not (last - first): a sample at t=8 plus a
    # fresh total at t=10 still divides by the full 10 s.
    assert _cps10(_samples([(0.0, 0), (8.0, 300)]), 400, 10.0) == 40.0  # 400 chars / 10 s


def test_cps10_partial_span_for_young_task():
    # A task younger than the window averages over however much time it has (still honest).
    assert _cps10(_samples([(0.0, 0), (3.0, 600)]), 600, 3.0) == 200.0  # 600 chars / 3 s
    assert _cps10(_samples([(0.0, 0), (1.0, 150)]), 150, 1.0) == 150.0
    assert _cps10(_samples([(2.0, 100), (6.0, 500)]), 500, 6.0) == 100.0  # 400 chars / 4 s
    assert _cps10(_samples([(0.0, 50), (0.5, 75)]), 75, 0.5) == 50.0  # 25 chars / 0.5 s
