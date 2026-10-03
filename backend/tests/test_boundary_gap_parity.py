"""Cross-implementation parity for the inter-segment pause rule.

``tts-engine/tts_worker.py`` and ``backend/engines/merge.py`` each carry a
1:1 copy of ``boundary_gap_ms`` (the worker runs as a separate process and
cannot import the engine). The per-side value tests pin each copy's behavior,
but a change to only one side stays green until the other side's fixtures
look stale; this test imports both implementations and asserts they agree
over the documented decision branches, so either side diverging fails the
suite on its own.
"""
from __future__ import annotations

from backend.engines.merge import boundary_gap_ms as engine_gap
from backend.tests.test_tts_worker import _load_worker


def test_worker_and_engine_pause_rules_match():
    tw = _load_worker()
    # Grid spans every branch: override wins (explicit 0, negative, numeric
    # string, surrounding whitespace), dirty input falls back (stray strings
    # raise ValueError, unhashables like {} / [5] raise TypeError), exact
    # speaker match (None == None, "B " != "B") vs speaker change, pause/same
    # defaults.
    overrides = [None, 0, -30, 500, "500", " 700 ", "bad", "fast", {}, [5]]
    speakers = [None, "A", "B", "B "]
    pauses = [250, 500, 1000]
    for override in overrides:
        for last_speaker in speakers:
            for first_speaker in speakers:
                for pause_ms in pauses:
                    for same_ms in pauses:
                        expected = tw.boundary_gap_ms(override, last_speaker, first_speaker, pause_ms, same_ms)
                        actual = engine_gap(override, last_speaker, first_speaker, pause_ms, same_ms)
                        assert expected == actual, (
                            "boundary_gap_ms 镜像分叉: override={!r} last={!r} first={!r} "
                            "pause_ms={!r} same_ms={!r} → worker={} engine={}".format(
                                override, last_speaker, first_speaker, pause_ms, same_ms, expected, actual
                            )
                        )
