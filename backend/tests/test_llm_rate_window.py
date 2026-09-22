"""Unit tests for the 文本解析 吞吐量 10-second-window average (core.tasks).

The 吞吐量 card shows a genuinely measured, smooth 10-second average of the LLM generation
rate — (chars generated over the last ``RATE_WINDOW`` seconds) / that span — computed in the
backend from the real streamed chars (never estimated / animated). These tests lock in that
behavior: the pure window-rate math, the per-flush accumulation + SSE emission, and the
eviction of samples that fall outside the window.
"""
import time
from collections import deque

from backend.core.tasks import RATE_WINDOW, Task


def _task() -> Task:
    return Task(id="x", module="m", label="l")


# -- _window_rate: the pure (chars / span) math over the retained samples -----------
def test_window_rate_needs_two_samples():
    assert Task._window_rate(deque()) == 0.0
    assert Task._window_rate(deque([(0.0, 10)])) == 0.0  # a single sample has no span


def test_window_rate_zero_span_is_zero():
    # Two samples almost coincident -> guard against a divide-by-~0 blow-up (the exact
    # per-flush jitter this window average is meant to eliminate).
    assert Task._window_rate(deque([(0.0, 10), (0.0005, 20)])) == 0.0


def test_window_rate_is_chars_over_span():
    # The cumulative total is monotonic, so (c_last - c_first) is the chars in the span.
    assert Task._window_rate(deque([(0.0, 100), (10.0, 600)])) == 50.0  # 500 chars / 10 s
    assert Task._window_rate(deque([(0.0, 0), (10.0, 5000)])) == 500.0  # 5000 chars / 10 s


def test_window_rate_partial_span_for_young_task():
    # A task younger than the window averages over however much time it has (still honest).
    assert Task._window_rate(deque([(0.0, 0), (3.0, 600)])) == 200.0  # 600 chars / 3 s


# -- record_llm_rate: accumulation, both rates, and the SSE event --------------------
def test_record_llm_rate_accumulates_and_emits_both_rates():
    t = _task()
    q = t.subscribe()
    t.record_llm_rate(100, 50.0)
    t.record_llm_rate(50, 30.0)

    assert t.llm_gen_total == 150          # monotonic streamed-char total
    assert t.llm_cps == 30.0              # the instantaneous (per-window) rate
    assert t.llm_gen_hist[-1][1] == 150    # samples carry the cumulative total

    e1 = q.get_nowait()
    e2 = q.get_nowait()
    assert e1["type"] == "llm_rate" and e1["cps"] == 50.0 and "cps10" in e1
    assert e2["type"] == "llm_rate" and e2["cps"] == 30.0 and "cps10" in e2


def test_record_llm_rate_evicts_samples_older_than_window():
    t = _task()
    now = time.monotonic()
    # One sample already beyond the window and one inside it.
    t.llm_gen_hist.append((now - (RATE_WINDOW + 5.0), 0))  # 5 s past the window
    t.llm_gen_hist.append((now - 2.0, 400))                # inside the window
    t.llm_gen_total = 400

    t.record_llm_rate(100, 10.0)  # appends a fresh (now, 500) sample, evicts the old one

    assert t.llm_gen_total == 500
    assert len(t.llm_gen_hist) == 2  # the out-of-window sample was dropped
    ages = [time.monotonic() - ts for (ts, _c) in t.llm_gen_hist]
    assert all(0 <= age <= RATE_WINDOW for age in ages)
