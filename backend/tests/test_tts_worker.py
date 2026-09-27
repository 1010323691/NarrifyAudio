"""Offline tests for production TTS batching, watchdog recovery, and audio output.

The worker imports without the ML stack, so pure helpers and protocol behavior can be
checked from the backend suite.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import types
from types import SimpleNamespace

import pytest

from backend.core.paths import PROJECT_ROOT

WORKER_PATH = PROJECT_ROOT / "tts-engine" / "tts_worker.py"


def _load_worker():
    """Load ``tts-engine/tts_worker.py`` as a fresh module (stdlib-only top level; no torch)."""
    spec = importlib.util.spec_from_file_location("tts_worker_under_test", WORKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(chars, **kw):
    base = {"chars": chars, "text": "字" * chars, "instruct": "", "vd": {}}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# --restore-stack — watchdog demotion records and the per-batch auto-restore
# ---------------------------------------------------------------------------

def test_parse_restore_stack_empty_and_valid():
    tw = _load_worker()
    assert tw.parse_restore_stack("") == []
    assert tw.parse_restore_stack(None) == []
    assert tw.parse_restore_stack("  ") == []
    # empty parts are skipped (a trailing comma is harmless)
    assert tw.parse_restore_stack("12000:4,") == [(12000, 4)]
    assert tw.parse_restore_stack("12000:4,,5000:2") == [(12000, 4), (5000, 2)]
    # whitespace-tolerant, order pinned (oldest first)
    assert tw.parse_restore_stack(" 12000:4 , 5000:2 ") == [(12000, 4), (5000, 2)]


def test_parse_restore_stack_malformed_raises():
    tw = _load_worker()
    for bad in ("12000", "12000:4:9", "abc:4", "0:4", "12000:-4"):
        with pytest.raises(ValueError) as exc:
            tw.parse_restore_stack(bad)
        # the error names the offender (the caller surfaces it as TTS_WORKER_ERROR +
        # exit 2 — a typo must never silently drop a pending restore)
        assert bad in str(exc.value)


# --------------------------------------------------------------------------- #
# order_speaker_groups — role queues, most lines first, shortest role lines first
# --------------------------------------------------------------------------- #

def test_order_speaker_groups_most_lines_first():
    tw = _load_worker()
    classified = {
        ("clone", "龙套"): [_row(5)] * 2,
        ("clone", "主角"): [_row(8)] * 5,
        ("custom", "旁白"): [_row(30)] * 9,
    }
    groups = tw.order_speaker_groups(classified)
    # the most-line character (旁白, 9 lines) runs first, then 5, then 2
    assert [key for key, _rows in groups] == [
        ("custom", "旁白"), ("clone", "主角"), ("clone", "龙套")]


def test_order_speaker_groups_ties_keep_first_seen():
    tw = _load_worker()
    classified = {
        ("clone", "甲"): [_row(4)],
        ("clone", "乙"): [_row(6), _row(2)],
        ("clone", "丙"): [_row(3), _row(7)],
    }
    groups = tw.order_speaker_groups(classified)
    # equal counts -> first-seen (insertion) order, deterministic for a given input
    assert [key for key, _rows in groups] == [
        ("clone", "乙"), ("clone", "丙"), ("clone", "甲")]


def test_order_speaker_groups_rows_length_ascending():
    tw = _load_worker()
    classified = {("custom", "旁白"): [_row(40), _row(3), _row(200), _row(12)]}
    _key, rows = tw.order_speaker_groups(classified)[0]
    # rows come back length-ascending so a sub-batch drawn from them stays homogeneous
    assert [r["chars"] for r in rows] == [3, 12, 40, 200]


def test_order_speaker_groups_never_mixes_keys():
    tw = _load_worker()
    classified = {
        ("custom", "A"): [_row(5), _row(9)],
        ("custom", "B"): [_row(50)],
        ("clone", "C"): [_row(2), _row(3), _row(4)],
    }
    groups = tw.order_speaker_groups(classified)
    # each group is exactly one KEY's rows — all of them, length-ascending, no cross-key
    # pooling (at the vtype keys the batch mode actually uses, a custom row can never
    # enter a clone queue — the three vtypes are three different models)
    for key, rows in groups:
        assert [r["chars"] for r in rows] == \
            sorted(r["chars"] for r in classified[key])
    assert {key for key, _rows in groups} == set(classified)


def test_order_speaker_groups_empty():
    tw = _load_worker()
    assert tw.order_speaker_groups({}) == []


def test_collect_mergeable_role_tails_only_merges_small_same_type_queues():
    tw = _load_worker()
    queues = [
        (("clone", "A"), [_row(8, speaker="A"), _row(2, speaker="A")]),
        (("clone", "B"), [_row(6, speaker="B")]),
        (("custom", "C"), [_row(1, speaker="C")]),
        (("clone", "D"), [_row(4, speaker="D")] * 4),
    ]
    merged = tw.collect_mergeable_role_tails(queues, 0, 4)
    assert merged is not None
    indices, rows = merged
    assert indices == [0, 1, 3]
    assert [row["chars"] for row in rows] == [2, 4, 6, 8]
    assert tw.collect_mergeable_role_tails(queues, 3, 4) is None


def test_multi_speaker_concurrency_halves_current_planned_count():
    tw = _load_worker()
    one_role = [_row(20, speaker="A") for _ in range(96)]
    two_roles = ([_row(20, speaker="A") for _ in range(48)]
                 + [_row(20, speaker="B") for _ in range(48)])
    assert tw.planned_concurrency_for_rows(one_role) == 96
    assert tw.planned_concurrency_for_rows(two_roles) == 48


# --------------------------------------------------------------------------- #
# sub_batch_timeout_seconds — the device-scaled watchdog budget
# --------------------------------------------------------------------------- #

def test_timeout_cuda_floor_cap_and_scaling():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("cuda", 0) == 60                 # floor
    assert tw.sub_batch_timeout_seconds("cuda", 1000) == 60               # floor: chars / 100
    assert tw.sub_batch_timeout_seconds("cuda", 600000) == 3600           # cap


def test_timeout_cpu_is_looser():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("cpu", 0) == 600            # floor (much looser)
    assert tw.sub_batch_timeout_seconds("cpu", 30000) == 10800      # cap
    assert tw.sub_batch_timeout_seconds("cpu", 1000) == 600 + 4 * 1000


def test_timeout_gpu_formula_for_mps():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("mps", 0) == 60  # any non-cpu device uses the GPU budget


def test_timeout_clone_formula_floor_scale_cap():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("cuda", 0, vtype="clone") == 60                 # floor
    assert tw.sub_batch_timeout_seconds("cuda", 1000, vtype="clone") == 60               # chars / 100
    assert tw.sub_batch_timeout_seconds("cuda", 600000, vtype="clone") == 3600           # cap


def test_timeout_gpu_budget_is_uniform_across_vtypes():
    tw = _load_worker()
    # The GPU budget no longer differentiates by voice type: all types share the current
    # conservative 100 chars/sec baseline, with the same floor and cap.
    for chars in (0, 100, 500, 2000, 10000, 60000):
        assert tw.sub_batch_timeout_seconds("cuda", chars, vtype="clone") == \
            tw.sub_batch_timeout_seconds("cuda", chars, vtype="design") == \
            tw.sub_batch_timeout_seconds("cuda", chars)  # default vtype="custom"


def test_timeout_budget_tracks_measured_decode_rate():
    tw = _load_worker()
    # The budget tracks the conservative GPU decode rate (~100 chars/sec).
    assert tw.sub_batch_timeout_seconds("cuda", 15000, vtype="clone") == 150


def test_timeout_budget_multiplies_by_speaker_count():
    tw = _load_worker()
    assert tw.sub_batch_timeout_seconds("cuda", 15000, speaker_count=2) == 300
    assert tw.sub_batch_timeout_seconds("cpu", 0, speaker_count=3) == 1800


def test_forced_timeout_uses_the_new_hard_deadline():
    tw = _load_worker()
    assert tw.forced_sub_batch_timeout_seconds(0) == 120
    assert tw.forced_sub_batch_timeout_seconds(6000) == 120
    assert tw.forced_sub_batch_timeout_seconds(10000) == 200


def test_watchdog_timeout_requires_vram_pressure_until_forced_deadline():
    tw = _load_worker()
    assert tw.watchdog_timeout_reason(59, 60, 120, 0.99) is None
    assert tw.watchdog_timeout_reason(60, 60, 120, 0.951) == "vram"
    assert tw.watchdog_timeout_reason(60, 60, 120, 0.95) == "wait"
    assert tw.watchdog_timeout_reason(119, 60, 120, None) == "wait"
    assert tw.watchdog_timeout_reason(120, 60, 120, 0.50) == "forced"


def test_vram_usage_fraction_uses_total_device_memory(monkeypatch):
    tw = _load_worker()
    monkeypatch.setattr(tw, "_total_vram", lambda _device: 1000)
    monkeypatch.setattr(tw, "_free_vram", lambda _device: 40)
    assert tw._vram_usage_fraction("cuda") == 0.96
    monkeypatch.setattr(tw, "_free_vram", lambda _device: 50)
    assert tw._vram_usage_fraction("cuda") == 0.95
    monkeypatch.setattr(tw, "_total_vram", lambda _device: 0)
    assert tw._vram_usage_fraction("cuda") is None


# --------------------------------------------------------------------------- #
# run_with_watchdog — the non-timeout paths
# (the timeout path calls os._exit(124), which would kill the test runner, so it is not exercised
# in-process; these pin that a fast ``fn`` returns its result and a fault is re-raised)
# --------------------------------------------------------------------------- #

def test_run_with_watchdog_returns_result():
    tw = _load_worker()
    assert tw.run_with_watchdog(lambda: 42, 5.0, "custom#1", []) == 42


def test_run_with_watchdog_reraises_fault():
    tw = _load_worker()

    def boom():
        raise ValueError("simulated OOM")

    with pytest.raises(ValueError):
        tw.run_with_watchdog(boom, 5.0, "custom#1", [])


# --------------------------------------------------------------------------- #
# Two-stage merge planning (pure)
# --------------------------------------------------------------------------- #

def test_plan_merge_batches_single():
    tw = _load_worker()
    assert tw.plan_merge_batches(0, 100) == []
    assert tw.plan_merge_batches(1, 100) == [(0, 1)]
    assert tw.plan_merge_batches(80, 100) == [(0, 80)]
    assert tw.plan_merge_batches(100, 100) == [(0, 100)]


def test_plan_merge_batches_coverage():
    tw = _load_worker()
    plan = tw.plan_merge_batches(250, 100)
    assert plan == [(0, 100), (100, 200), (200, 250)]
    flat = [i for start, end in plan for i in range(start, end)]
    assert flat == list(range(250))  # full coverage, no overlap, in order


def test_plan_merge_batches_clamps_size():
    tw = _load_worker()
    # a bogus size degrades to one-segment parts, never to an empty range
    assert tw.plan_merge_batches(3, 0) == [(0, 1), (1, 2), (2, 3)]
    assert tw.plan_merge_batches(2, -5) == [(0, 1), (1, 2)]


def test_normalize_pause_ms():
    tw = _load_worker()
    assert tw.normalize_pause_ms(None) is None
    assert tw.normalize_pause_ms(900) == 900
    assert tw.normalize_pause_ms("900") == 900
    assert tw.normalize_pause_ms(900.6) == 900
    assert tw.normalize_pause_ms("abc") is None
    assert tw.normalize_pause_ms({}) is None
    assert tw.normalize_pause_ms(-5) == 0


def test_boundary_gap_override_wins():
    tw = _load_worker()
    assert tw.boundary_gap_ms(900, "A", "B", 500, 250) == 900
    assert tw.boundary_gap_ms(0, "A", "A", 500, 250) == 0  # an explicit 0 is still an override


def test_boundary_gap_speaker_rule():
    tw = _load_worker()
    assert tw.boundary_gap_ms(None, "A", "A", 500, 250) == 250
    assert tw.boundary_gap_ms(None, "A", "B", 500, 250) == 500
    assert tw.boundary_gap_ms("bad", "A", "A", 500, 250) == 250  # dirty -> speaker rule


def test_boundary_gap_matches_single_pass():
    """The two-stage merge must insert exactly the gaps a single-pass merge would.
    The single-pass rule for the gap after segment i is: its pause_after (if any),
    else the same/different-speaker default against segment i+1. With batch size 2
    the part boundary falls exactly on the override-bearing gap below."""
    tw = _load_worker()
    segs = [("A", None), ("A", 900), ("B", None), ("A", None)]
    # single-pass gaps for the three inter-segment boundaries (pause_ms=500, same_ms=250):
    single_pass = [250, 900, 500]  # 0->1 same speaker; 1->2 override; 2->3 speaker change

    def single_pass_gap(i):
        # the reference rule, inlined (what one combine pass over the whole sequence does)
        override = segs[i][1]
        if override is not None:
            return int(override)
        return 250 if segs[i + 1][0] == segs[i][0] else 500

    assert [single_pass_gap(i) for i in range(3)] == single_pass

    # two-stage: the part boundary (after segment 1, the last of part [0,1]) is
    # boundary_gap_ms(last_pause_after, last_speaker, first_speaker, ...) of the two parts
    assert tw.boundary_gap_ms(segs[1][1], segs[1][0], segs[2][0], 500, 250) == single_pass[1]
    # ...and the intra-part boundaries are the same rule the single pass applies there
    assert tw.boundary_gap_ms(segs[0][1], segs[0][0], segs[1][0], 500, 250) == single_pass[0]
    assert tw.boundary_gap_ms(segs[2][1], segs[2][0], segs[3][0], 500, 250) == single_pass[2]


def test_boundary_gap_across_skipped_segments():
    """When segments are skipped (missing files), the single-pass gap falls between
    the last valid segment before and the first valid segment after the skip — the
    part boundary must use exactly those two (plus any override on the earlier one)."""
    tw = _load_worker()
    assert tw.boundary_gap_ms(None, "A", "C", 500, 250) == 500  # A -> C across the skip
    assert tw.boundary_gap_ms(700, "A", "C", 500, 250) == 700    # override still wins


def test_merge_frac_bands_contiguous():
    tw = _load_worker()
    assert tw.merge_stage1_frac(0, 3000) == pytest.approx(0.05)
    assert tw.merge_stage1_frac(3000, 3000) == pytest.approx(0.80)
    assert tw.merge_stage2_frac(0, 30) == pytest.approx(0.80)
    assert tw.merge_stage2_frac(30, 30) == pytest.approx(0.95)
    assert tw.merge_encode_frac(0.0) == pytest.approx(0.95)
    assert tw.merge_encode_frac(1.0) == pytest.approx(1.0)
    assert tw.merge_encode_frac(5.0) == pytest.approx(1.0)  # clamped at the top
    seq = ([tw.merge_stage1_frac(i, 100) for i in range(101)]
           + [tw.merge_stage2_frac(i, 3) for i in range(1, 4)]
           + [tw.merge_encode_frac(f) for f in (0.0, 0.5, 1.0)])
    assert all(a <= b for a, b in zip(seq, seq[1:]))  # one monotone run, 0.05 -> 1.0


def test_encode_mp3_streaming_threads_flag(tmp_path, monkeypatch):
    """``-threads N`` follows ``-y`` in the final-encode command (bounds the encoder's
    thread count — ffmpeg otherwise spawns ALL cores per process); 0/omitted leaves the
    legacy command byte-identical (manual / old runs)."""
    tw = _load_worker()
    wav = tmp_path / "b.wav"
    wav.write_bytes(b"x" * 16)
    mp3 = tmp_path / "b.mp3"
    mp3.write_bytes(b"A" * 2048)  # a finished (>= 1 KiB) output file

    captured = {}

    class _FakeProc:
        returncode = 0

        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.stderr = io.BytesIO(b"")

        def poll(self):
            return 0

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(tw.subprocess, "Popen", _FakeProc)

    # threads=0 (the default) -> no flag, the legacy command shape
    assert tw._encode_mp3_streaming(str(wav), str(mp3), 10.0) is True
    assert captured["cmd"] == [
        "ffmpeg", "-y", "-loglevel", "info", "-i", str(wav),
        "-c:a", "libmp3lame", str(mp3),
    ]
    # threads=2 -> `-threads 2` right after `-y`
    assert tw._encode_mp3_streaming(str(wav), str(mp3), 10.0, threads=2) is True
    assert captured["cmd"][:4] == ["ffmpeg", "-y", "-threads", "2"]
    assert captured["cmd"][4:] == [
        "-loglevel", "info", "-i", str(wav), "-c:a", "libmp3lame", str(mp3),
    ]


# --------------------------------------------------------------------------- #
# design-batch — the 角色配音·克隆 stage (shared planning / generate / save path)
# --------------------------------------------------------------------------- #


def test_generate_rows_design_uses_per_row_instruct_and_do_sample_switch():
    tw = _load_worker()

    class _Model:
        def __init__(self):
            self.calls = []

        def generate_voice_design(self, **kw):
            self.calls.append(kw)
            return [[1.0] * 8] * len(kw["text"]), 24000

    model = _Model()
    args = SimpleNamespace(language="chinese")
    rows = [
        {"index": 0, "text": "t0", "instruct": "", "vd": {"description": "deep voice"}},
        {"index": 1, "text": "t1", "instruct": "", "vd": {"description": "bright voice"}},
    ]
    # Default (batch mode): no do_sample override — its design behaviour is unchanged.
    results = tw._generate_rows(model, "design", rows, args, {}, None)
    assert results == [(True, ([1.0] * 8, 24000))] * 2
    kw = model.calls[0]
    # Each row keeps its own description (a per-row instruct list, matching the
    # token counting / VRAM estimate).
    assert kw["instruct"] == ["deep voice", "bright voice"]
    assert kw["text"] == ["t0", "t1"]
    assert "do_sample" not in kw
    # design-batch: sampling forced on (identical-input candidates stay distinct even on
    # a checkpoint whose generate_config disables sampling).
    tw._generate_rows(model, "design", rows, args, {}, None, force_do_sample=True)
    assert model.calls[1].get("do_sample") is True


def test_generate_rows_clone_passes_per_row_prompt_items():
    tw = _load_worker()

    class _Model:
        def __init__(self):
            self.calls = []

        def generate_voice_clone(self, **kw):
            self.calls.append(kw)
            return [[1.0] * 8] * len(kw["text"]), 24000

    model = _Model()
    args = SimpleNamespace(language="chinese")
    # One tensor call serving TWO characters: each row is conditioned on its own
    # character's reference (the model's list API matches prompt items to rows 1:1).
    rows = [
        {"index": 0, "speaker": "甲", "text": "t0", "instruct": "", "vd": {}},
        {"index": 1, "speaker": "乙", "text": "t1", "instruct": "", "vd": {}},
        {"index": 2, "speaker": "甲", "text": "t2", "instruct": "", "vd": {}},
    ]
    prompts = {"甲": ["prompt-甲"], "乙": ["prompt-乙"]}
    results = tw._generate_rows(model, "clone", rows, args, prompts, None)
    assert results == [(True, ([1.0] * 8, 24000))] * 3
    kw = model.calls[0]
    assert kw["voice_clone_prompt"] == ["prompt-甲", "prompt-乙", "prompt-甲"]
    # a single-character batch passes the same item on every row (the old broadcast)
    tw._generate_rows(model, "clone", [rows[0], rows[2]], args, prompts, None)
    assert model.calls[1]["voice_clone_prompt"] == ["prompt-甲", "prompt-甲"]


class TestLengthProportionalDecodeCap:
    """In a multi-row tensor batch the model does not stop individual rows at EOS
    (model-side behaviour), so the decode cap scales with the sub-batch's longest row
    instead of always being MAX_NEW_TOKENS — a short-row batch must no longer be able
    to run the full 2048-step cap (the 2026-09-17 one-click-synthesis hang: 31 rows of
    2-6 chars ran the full cap and blew the sub-batch watchdog budget)."""

    def test_short_rows_get_the_floor(self):
        tw = _load_worker()
        assert tw.max_new_tokens_for_chars(0) == tw.FRAME_CAP_FLOOR
        for chars in (1, 2, 6, 21):
            assert tw.max_new_tokens_for_chars(chars) == tw.FRAME_CAP_FLOOR

    def test_proportional_mid_range(self):
        tw = _load_worker()
        per_char = tw.FRAME_CAP_PER_CHAR
        for chars in (30, 50, 100, 200):
            assert tw.max_new_tokens_for_chars(chars) == chars * per_char

    def test_long_rows_keep_the_full_cap(self):
        tw = _load_worker()
        # the floor of proportionality: 341*6 = 2046 < 2048; 342*6 = 2052 clamps to 2048
        assert tw.max_new_tokens_for_chars(341) == 341 * tw.FRAME_CAP_PER_CHAR
        for chars in (342, 1000, 2500):
            assert tw.max_new_tokens_for_chars(chars) == tw.MAX_NEW_TOKENS

    def test_monotonic(self):
        tw = _load_worker()
        caps = [tw.max_new_tokens_for_chars(c) for c in range(0, 400)]
        assert caps == sorted(caps)

    def test_generate_rows_passes_the_proportional_cap(self):
        tw = _load_worker()

        class _Model:
            def __init__(self):
                self.calls = []

            def generate_voice_clone(self, **kw):
                self.calls.append(kw)
                return [[1.0] * 8] * len(kw["text"]), 24000

        model = _Model()
        args = SimpleNamespace(language="chinese")
        # The incident shape: a few ultra-short rows in one sub-batch.
        rows = [
            {"index": 0, "speaker": "NARRATOR", "text": "啪！", "instruct": "", "vd": {}},
            {"index": 1, "speaker": "NARRATOR", "text": "侍女抱了拳。", "instruct": "", "vd": {}},
        ]
        tw._generate_rows(model, "clone", rows, args, {"NARRATOR": ["prompt"]}, None)
        # 6-char longest row -> the floor, NOT the full 2048 cap (the hang signature)
        assert model.calls[0]["max_new_tokens"] == tw.max_new_tokens_for_chars(6)
        assert model.calls[0]["max_new_tokens"] < tw.MAX_NEW_TOKENS

        # A ~105-char row -> proportional, still under the full cap.
        long_rows = [{
            "index": 2, "speaker": "NARRATOR",
            "text": "风从街口穿过来，带着一股淡淡的柴火气味。" * 5,
            "instruct": "", "vd": {},
        }]
        tw._generate_rows(model, "clone", long_rows, args, {"NARRATOR": ["prompt"]}, None)
        cap = model.calls[1]["max_new_tokens"]
        assert cap == tw.max_new_tokens_for_chars(len(long_rows[0]["text"]))
        assert tw.FRAME_CAP_FLOOR < cap < tw.MAX_NEW_TOKENS


def test_save_and_report_design_protocol_and_fault_tolerance(tmp_path, monkeypatch):
    tw = _load_worker()

    # The save path imports numpy/soundfile lazily — stub them to keep this unit test
    # independent of the shared ML environment.
    class _Nd:  # the fake ndarray type: no real value is an instance of it
        pass

    class _Arr:
        def __init__(self, x):
            self.x = x
            self.size = len(x)
            self.ndim = 1

        def __len__(self):
            return self.size

    def _np_array(x):
        return x if isinstance(x, _Arr) else _Arr(x)  # idempotent, like the real one

    fake_np = types.SimpleNamespace(array=_np_array, ndarray=_Nd)
    written = []

    class _FakeSF:
        @staticmethod
        def write(path, data, sr):
            if path.endswith("bad.wav"):
                raise RuntimeError("boom")
            written.append((path, len(data), sr))
            with open(path, "wb") as f:
                f.write(b"RIFF" + bytes(100))

    monkeypatch.setitem(sys.modules, "numpy", fake_np)
    monkeypatch.setitem(sys.modules, "soundfile", _FakeSF)

    out = tmp_path / "dv"
    rows = [
        {"index": 10, "out": str(out / "ok.wav")},
        {"index": 11, "out": str(out / "empty.wav")},
        {"index": 12, "out": str(out / "bad.wav")},
        {"index": 13, "out": str(out / "lost.wav")},
    ]
    results = [
        (True, ([1.0] * 8, 24000)),   # ok: WAV straight to the row's out path
        (True, ([], 24000)),          # empty audio: recorded error, not an abort
        (True, ([1.0] * 8, 24000)),   # save fault -> recorded error, run continues
        (False, "生成失败"),            # generation fault: reported as-is
    ]
    reports = []
    tw._save_and_report_design(rows, results, str(out), 4,
                               lambda i, ok, p: reports.append((i, ok, p)), 12345)
    assert reports == [
        (10, True, (12345, os.path.abspath(str(out / "ok.wav")))),
        (11, False, "模型返回空音频"),
        (12, False, "boom"),
        (13, False, "生成失败"),
    ]
    # Exactly the ok row produced a file (no MP3 encode in this mode).
    assert written and written[0][0] == os.path.abspath(str(out / "ok.wav"))
    assert (out / "ok.wav").exists()
    assert not (out / "empty.wav").exists() and not (out / "bad.wav").exists()


# --------------------------------------------------------------------------- #
# _row_output_paths — per-row chapter save location (pooled multi-file runs)
# --------------------------------------------------------------------------- #

def test_row_output_paths_fallback_and_override():
    tw = _load_worker()
    fb = r"C:\ws\05_audio_chunk\pool_fallback"
    # Legacy rows (no out_dir / file_index) fall back to the whole-batch dir + segment
    # index — byte-identical to the pre-pooling formula.
    assert tw._row_output_paths({"index": 3}, fb, 4) == (
        os.path.join(fb, "0004.mp3"), os.path.join(fb, "0004.wav"))
    # Pooled rows carry their own chapter dir + local line number (file number =
    # file_index + 1, zero-padded to the pool width). The segment-table index is
    # never used for the file number once file_index is present. Joined with
    # os.path.join like the implementation, so the expectation adapts to the
    # runner platform (byte-identical on Windows, the production platform).
    assert tw._row_output_paths({"index": 12, "out_dir": r"C:\ws\05_audio_chunk\t",
                                 "file_index": 37}, fb, 5) == (
        os.path.join(r"C:\ws\05_audio_chunk\t", "00038.mp3"),
        os.path.join(r"C:\ws\05_audio_chunk\t", "00038.wav"))
    # zfill pads but never truncates: a number wider than `width` keeps its digits.
    assert tw._row_output_paths({"index": 0, "file_index": 99999}, fb, 4) == (
        os.path.join(fb, "100000.mp3"), os.path.join(fb, "100000.wav"))
    # Empty-string out_dir also falls back (truthiness, not presence).
    assert tw._row_output_paths({"index": 0, "out_dir": ""}, fb, 4) == (
        os.path.join(fb, "0001.mp3"), os.path.join(fb, "0001.wav"))


def test_save_and_report_pool_rows_land_in_own_packages(tmp_path, monkeypatch):
    tw = _load_worker()

    # The save path imports numpy/soundfile lazily — stub them to keep this unit test
    # independent of the shared ML environment, and stub the pydub-based WAV->MP3 encode.
    class _Nd:  # the fake ndarray type: no real value is an instance of it
        pass

    class _Arr:
        def __init__(self, x):
            self.x = x
            self.size = len(x)
            self.ndim = 1

        def __len__(self):
            return self.size

    def _np_array(x):
        return x if isinstance(x, _Arr) else _Arr(x)  # idempotent, like the real one

    fake_np = types.SimpleNamespace(array=_np_array, ndarray=_Nd)
    encoded = []

    class _FakeSF:
        @staticmethod
        def write(path, data, sr):
            with open(path, "wb") as f:
                f.write(b"RIFF" + bytes(100))

    def _fake_wav_to_mp3(wav_path, mp3_path):
        assert os.path.exists(wav_path)
        with open(mp3_path, "wb") as f:
            f.write(b"ID3" + bytes(1200))  # >= 1024B, so the encode counts as good
        encoded.append(mp3_path)
        return True

    monkeypatch.setitem(sys.modules, "numpy", fake_np)
    monkeypatch.setitem(sys.modules, "soundfile", _FakeSF)
    monkeypatch.setattr(tw, "_wav_to_mp3", _fake_wav_to_mp3)

    pkg_a = str(tmp_path / "s")  # chapter package s
    pkg_b = str(tmp_path / "t")  # chapter package t
    fallback = str(tmp_path / "fallback")
    rows = [
        {"index": 0, "out_dir": pkg_a, "file_index": 3},   # pooled -> s/0004.mp3
        {"index": 1, "out_dir": pkg_b, "file_index": 0},   # pooled -> t/0001.mp3
        {"index": 2, "file_index": 7},                     # legacy fallback -> fallback/0008.mp3
    ]
    results = [
        (True, ([1.0] * 8, 24000)),  # ok
        (True, ([], 24000)),         # empty audio -> recorded error, no file touched
        (True, ([1.0] * 8, 24000)),  # ok, falls back to the batch out_dir
    ]
    reports = []
    tw._save_and_report(rows, results, fallback, 4,
                        lambda i, ok, p: reports.append((i, ok, p)))
    # Reports always carry the segment-table index (pool-global in a pooled run),
    # never the file_index.
    assert reports == [
        (0, True, os.path.join(pkg_a, "0004.mp3")),
        (1, False, "模型返回空音频"),
        (2, True, os.path.join(fallback, "0008.mp3")),
    ]
    # Files land in their own chapter packages; the file number is file_index + 1.
    assert sorted(encoded) == sorted([os.path.join(pkg_a, "0004.mp3"),
                                      os.path.join(fallback, "0008.mp3")])
    assert (tmp_path / "s" / "0004.mp3").exists()
    assert not (tmp_path / "t" / "0001.mp3").exists()      # empty-audio row wrote nothing
    assert not (tmp_path / "s" / "0001.mp3").exists()      # index 0 was NOT used as a number
    # No stray WAV left behind (the successful encode removes it).
    assert not list((tmp_path / "s").glob("*.wav")) and not list((tmp_path / "fallback").glob("*.wav"))


# --------------------------------------------------------------------------- #
# producer-consumer mechanical post-processing pipeline
# --------------------------------------------------------------------------- #

def test_mechanical_worker_count_respects_cpu_and_memory():
    tw = _load_worker()
    assert tw.mechanical_worker_count(cpu_count=8, available_memory=16 * 2**30) == 4
    assert tw.mechanical_worker_count(cpu_count=8, available_memory=7 * 2**30) == 2
    assert tw.mechanical_worker_count(cpu_count=8, available_memory=3 * 2**30) == 1
    assert tw.mechanical_worker_count(cpu_count=1, available_memory=16 * 2**30) == 1


def test_mechanical_pipeline_is_bounded_and_retries_save_faults(capsys):
    tw = _load_worker()
    reports = []
    calls = {}

    def save_fn(rows, results, out_dir, width, report, batch_seed):
        index = rows[0]["index"]
        calls[index] = calls.get(index, 0) + 1
        if index == 0 and calls[index] == 1:
            report(index, False, "disk busy")
        else:
            report(index, True, f"out-{index}")

    pipeline = tw._MechanicalPipeline(lambda *outcome: reports.append(outcome),
                                       worker_count=1, queue_size=1)
    rows = [{"index": i} for i in range(3)]
    results = [(True, ([1.0], 24000)) for _ in rows]
    pipeline.submit(rows, results, out_dir="", width=4, save_fn=save_fn,
                    batch_label="clone#1", batch_seed=None, started=tw.time.perf_counter(),
                    inference_seconds=0.0, total_chars=3, cuda_peaks={})
    pipeline.close()

    assert pipeline.queue_size == 1
    assert calls[0] == 2  # transient mechanical failure retried once
    assert sorted(reports) == [(0, True, "out-0"), (1, True, "out-1"), (2, True, "out-2")]
    assert '"event": "end"' in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The worker module loads in the lean backend (stdlib-only top level)
# --------------------------------------------------------------------------- #

def test_worker_module_loads_without_torch():
    # The worker's top level is stdlib-only, so it imports without loading torch.
    tw = _load_worker()
    # Production batching and watchdog helpers remain importable.
    for name in ("order_speaker_groups", "next_fixed_batch",
                 "sub_batch_timeout_seconds", "forced_sub_batch_timeout_seconds",
                 "watchdog_timeout_reason", "_vram_usage_fraction",
                 "run_with_watchdog", "_clear_gpu_cache", "_free_vram", "_total_vram", "_warmup",
                 "_synth_sub_batch",
                 "_run_design_batch", "_save_and_report_design", "_generate_rows",
                 "_row_output_paths", "_save_and_report",
                 "plan_merge_batches", "normalize_pause_ms", "boundary_gap_ms",
                 "merge_stage1_frac", "merge_stage2_frac", "merge_encode_frac",
                 "parse_restore_stack",
                 "mechanical_worker_count", "_MechanicalPipeline"):
        assert callable(getattr(tw, name)), f"missing {name}"
    for const in ("GPU_TIMEOUT_CHARS_PER_SEC", "GPU_TIMEOUT_FLOOR_S"):
        assert getattr(tw, const) > 0, f"missing {const}"
    # ...and the tokenizer is OUT of the planning path (char counts are the length metric)...
    assert not hasattr(tw, "_row_tokens")
    assert not hasattr(tw, "_tensor_tokens")
    # ...and the old thread-pool scheduler is gone...
    assert not hasattr(tw, "run_bounded")
    # ...with its stdlib dependencies intact (no concurrent.futures).
    assert tw.threading is not None
    assert not hasattr(tw, "ThreadPoolExecutor")


def test_bounded_vocoder_preserves_order_and_restores_on_failure(monkeypatch):
    tw = _load_worker()
    cleared = []
    monkeypatch.setattr(tw, '_clear_gpu_cache', cleared.append)
    class Tokenizer:
        device = 'cuda'
        def __init__(self):
            self.sizes = []
        def decode(self, encoded):
            self.sizes.append(len(encoded))
            return list(encoded), 24000
    tokenizer = Tokenizer()
    model = SimpleNamespace(model=SimpleNamespace(speech_tokenizer=tokenizer))
    with tw.bounded_vocoder(model, 8):
        wavs, sr = tokenizer.decode(list(range(19)))
    assert wavs == list(range(19)) and sr == 24000
    assert tokenizer.sizes == [8, 8, 3]
    assert cleared == ['cuda']
    assert 'decode' not in tokenizer.__dict__
    with pytest.raises(RuntimeError), tw.bounded_vocoder(model, 8):
        raise RuntimeError('inference failed')
    assert 'decode' not in tokenizer.__dict__


def test_production_command_has_only_row_cap_and_fixed_decoder():
    from backend.engines.tts_batch import _build_cmd
    cmd = _build_cmd('python', 'worker', 'segments', 'voices', 'out', language='', device='cuda',
        model='', base_model='', design_model='', ffmpeg_path='', concurrency=80, seed=42)
    assert cmd[cmd.index('--concurrency')+1] == '80'
    assert cmd[cmd.index('--vocoder-batch-size')+1] == '8'
    assert not {'--disabled-checks', '--max-batch-chars', '--length-ratio', '--restore-stack'} & set(cmd)


def test_auto_production_command_enables_length_curve():
    from backend.engines.tts_batch import _build_cmd
    cmd = _build_cmd('python', 'worker', 'segments', 'voices', 'out', language='', device='cuda',
        model='', base_model='', design_model='', ffmpeg_path='', concurrency=340, seed=42,
        auto_concurrency=True)
    assert '--auto-batch' in cmd
    assert cmd[cmd.index('--concurrency') + 1] == '340'


def test_fixed_batches_do_not_shrink_for_length_or_total_chars():
    tw = _load_worker()
    rows = [dict(index=i, chars=1 if i < 40 else 200) for i in range(161)]
    batches = list(tw.fixed_batches(rows, 80))
    assert [len(b) for b in batches] == [80, 80, 1]
    assert [r for b in batches for r in b] == rows


def test_auto_batch_uses_upward_matched_safety_tiers():
    tw = _load_worker()
    assert [tw.auto_batch_cap_for_chars(n) for n in (200, 150, 100, 50, 20, 5)] == \
        [80, 96, 128, 224, 272, 340]
    assert [tw.auto_batch_cap_for_chars(n) for n in (1, 10, 21, 35, 51, 75, 101, 151, 201)] == \
        [340, 272, 224, 224, 128, 128, 96, 80, 80]


def test_auto_batch_uses_longest_row_in_each_prefix():
    tw = _load_worker()
    rows = [dict(index=i, chars=5) for i in range(340)] + [dict(index=340, chars=20)]
    batch, remaining, cap, restored = tw.next_auto_batch(rows, 340, [])
    assert len(batch) == 340 and batch[-1]["chars"] == 5
    assert len(remaining) == 1 and cap == 340 and restored is None


def test_auto_batch_mixed_lengths_use_longest_row_tier():
    tw = _load_worker()
    rows = [dict(index=i, chars=10) for i in range(79)] + [dict(index=79, chars=200)]
    ordered = tw.order_speaker_groups({"clone": rows})[0][1]
    batch, remaining, cap, restored = tw.next_auto_batch(ordered, 340, [])
    assert len(batch) == 80 and max(row["chars"] for row in batch) == 200
    assert not remaining and cap == 340 and restored is None


def test_auto_batches_restore_after_two_successes():
    tw = _load_worker()
    rows = [dict(index=i, chars=10) for i in range(500)]
    stack = [(1, 340)]
    successes = [0]

    first, remaining, cap, restored = tw.next_auto_batch(rows, 80, stack, successes)
    assert len(first) == 80 and cap == 80 and restored is None
    successes[0] += 1
    second, remaining, cap, restored = tw.next_auto_batch(remaining, cap, stack, successes)
    assert len(second) == 80 and cap == 80 and restored is None
    successes[0] += 1

    third, remaining, cap, restored = tw.next_auto_batch(remaining, cap, stack, successes)
    assert len(third) == 272 and cap == restored == 340
    assert len(remaining) == 68


def test_fixed_batch_oom_does_not_retry_or_shrink(monkeypatch):
    tw = _load_worker()
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise RuntimeError('CUDA out of memory')
    monkeypatch.setattr(tw, '_generate_rows', fail)
    monkeypatch.setattr(tw, 'run_with_watchdog', lambda fn, *a, **kw: fn())
    with pytest.raises(RuntimeError, match='out of memory'):
        tw._synth_sub_batch(object(), 'clone', [dict(index=i, chars=200) for i in range(80)],
            args=SimpleNamespace(fixed_batch=True), clone_prompts={}, device='cpu', seed=0,
            sub_counter=[0], out_dir='', width=4, report=lambda *a: None)
    assert len(calls) == 1


def test_fixed_batches_restore_after_two_successes_even_when_char_threshold_is_exceeded():
    tw = _load_worker()
    rows = [dict(index=i, chars=190) for i in range(160)]
    stack = [(1, 80)]  # deliberately lower than every trial batch
    successes = [0]

    first, remaining, cap, restored = tw.next_fixed_batch(rows, 40, stack, successes)
    assert cap == 40 and len(first) == 40 and restored is None
    successes[0] += 1
    second, remaining, cap, restored = tw.next_fixed_batch(remaining, cap, stack, successes)
    assert cap == 40 and len(second) == 40 and restored is None
    successes[0] += 1

    third, remaining, cap, restored = tw.next_fixed_batch(remaining, cap, stack, successes)
    assert cap == restored == 80 and len(third) == 80
    assert not remaining
    assert first + second + third == rows
    assert stack == [(1, 80)]  # caller emits the recovery event and pops it


def test_fixed_batches_restore_one_timeout_level_at_a_time():
    tw = _load_worker()
    rows = [dict(index=i, chars=100) for i in range(120)]
    stack = [(16000, 80), (8000, 40)]
    successes = [0, 0]
    first, remaining, cap, restored = tw.next_fixed_batch(rows, 20, stack, successes)
    assert cap == 20 and restored is None
    successes[-1] += 1
    second, remaining, cap, restored = tw.next_fixed_batch(remaining, cap, stack, successes)
    assert cap == 20 and restored is None
    successes[-1] += 1
    third, remaining, cap, restored = tw.next_fixed_batch(remaining, cap, stack, successes)
    assert cap == restored == 40
    stack.pop()
    successes.pop()
    successes[-1] += 1
    fourth, remaining, cap, restored = tw.next_fixed_batch(remaining, cap, stack, successes)
    assert cap == 40 and restored is None
    successes[-1] += 1
    fifth, remaining, cap, restored = tw.next_fixed_batch(remaining, cap, stack, successes)
    assert cap == restored == 80
    assert first + second + third + fourth + fifth == rows and not remaining
