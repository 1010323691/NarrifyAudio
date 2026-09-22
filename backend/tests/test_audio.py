"""Tests for the audio-splitting engine (``backend/engines/audio.py``).

Split into two tiers:

* **Pure-math invariants** (no FFmpeg needed) — lock in the preserved contracts:
  even-split keeps every segment at/below the target and tiles ``[0, total]``;
  pause-snapping only moves *interior* boundaries, stays strictly monotonic, and
  never changes the segment count; duration parsing / naming / formatting match
  the source; silence-log parsing handles trailing-open, negative and unsorted
  input.
* **Native-FFmpeg integration** (skipped if ``ffmpeg``/``ffprobe`` are absent) —
  probe a real tone, detect a real silence gap, and cut losslessly with ``-c copy``.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.engines import audio as A

# =========================================================================== #
# Duration parsing / display formatting
# =========================================================================== #

def test_parse_duration_to_seconds():
    assert A.parse_duration_to_seconds("90") == 90
    assert A.parse_duration_to_seconds("1:30") == 90
    assert A.parse_duration_to_seconds("01:30") == 90
    assert A.parse_duration_to_seconds("1:00:00") == 3600
    assert A.parse_duration_to_seconds("0:01:30") == 90
    assert A.parse_duration_to_seconds("  45.5  ") == 45.5
    assert math.isnan(A.parse_duration_to_seconds(""))
    assert math.isnan(A.parse_duration_to_seconds("abc"))
    assert math.isnan(A.parse_duration_to_seconds("1:2:3:4"))
    assert math.isnan(A.parse_duration_to_seconds(None))


def test_format_duration():
    assert A.format_duration(0) == "0:00"
    assert A.format_duration(90) == "1:30"
    assert A.format_duration(61) == "1:01"
    assert A.format_duration(3599) == "59:59"
    assert A.format_duration(3600) == "1:00:00"
    assert A.format_duration(3661) == "1:01:01"
    assert A.format_duration(-5) == "0:00"
    assert A.format_duration(float("nan")) == "—"
    assert A.format_duration(None) == "—"


def test_format_bytes():
    assert A.format_bytes(0) == "0 B"
    assert A.format_bytes(500) == "500 B"
    assert A.format_bytes(1023) == "1023 B"
    assert A.format_bytes(1024) == "1 KB"
    assert A.format_bytes(1536) == "2 KB"       # i==0 → 0 decimals → rounds
    assert A.format_bytes(1048576) == "1.0 MB"
    assert A.format_bytes(1610612736) == "1.5 GB"
    assert A.format_bytes(float("nan")) == "—"
    assert A.format_bytes(None) == "—"


def test_get_extension():
    assert A.get_extension("song.mp3") == "mp3"
    assert A.get_extension("song.MP3") == "mp3"
    assert A.get_extension("song") == "mp3"
    assert A.get_extension("song.") == "mp3"
    assert A.get_extension("archive.tar.gz") == "gz"
    assert A.get_extension("") == "mp3"


# =========================================================================== #
# Even-split planning
# =========================================================================== #

def test_build_plan_even():
    p = A.build_plan(100, "30")
    assert p["valid"]
    assert p["count"] == 4
    assert abs(p["each"] - 25) < 1e-9
    assert [s["index"] for s in p["segments"]] == [0, 1, 2, 3]
    # tiles [0, total] with no gaps or overlaps
    assert p["segments"][0]["start"] == 0
    prev = 0.0
    for s in p["segments"]:
        assert abs(s["start"] - prev) < 1e-9
        prev = s["start"] + s["duration"]
    assert abs(prev - 100) < 1e-9
    # no segment exceeds the target
    assert max(s["duration"] for s in p["segments"]) <= 30 + 1e-6


def test_build_plan_exact_division():
    p = A.build_plan(90, "30")
    assert p["count"] == 3
    assert all(abs(s["duration"] - 30) < 1e-9 for s in p["segments"])


def test_build_plan_single_segment():
    p = A.build_plan(10, "30")
    assert p["count"] == 1
    assert p["segments"][0]["start"] == 0
    assert abs(p["segments"][0]["duration"] - 10) < 1e-9


def test_build_plan_invalid():
    assert not A.build_plan(0, "30")["valid"]
    assert not A.build_plan(-5, "30")["valid"]
    assert not A.build_plan(100, "0")["valid"]
    assert not A.build_plan(100, "abc")["valid"]
    assert not A.build_plan(float("nan"), "30")["valid"]


# =========================================================================== #
# Pause-snapping
# =========================================================================== #

def test_snap_boundaries_shifts_to_pause():
    # W = min(15, (30-0)/2 - 0.5) = 14.5
    r = A.snap_boundaries([0, 30, 60, 90],
                          [{"start": 20, "end": 26}, {"start": 55, "end": 65}],  # mids 23, 60
                          15)
    assert abs(r["bounds"][1] - 23) < 1e-9   # boundary 1 snapped to pause mid 23
    assert abs(r["bounds"][2] - 60) < 1e-9   # boundary 2 snapped to pause mid 60
    assert r["snapped"] == 2
    assert r["fallbacks"] == 0
    assert r["shifts"][1] == pytest.approx(-7)
    # strictly monotonic — boundaries can never cross
    assert all(b < c for b, c in zip(r["bounds"], r["bounds"][1:]))


def test_snap_boundaries_fallback_no_pause_in_window():
    # W = min(5, 15 - 0.5) = 5 → window [25, 35]; the only pause (mid 1) is far away
    r = A.snap_boundaries([0, 30, 60], [{"start": 0, "end": 2}], 5)
    assert r["bounds"] == [0, 30, 60]  # unchanged → even-split time
    assert r["snapped"] == 0
    assert r["fallbacks"] == 1


def test_snap_boundaries_window_too_small():
    # segment width 1s → W = (1/2) - 0.5 = 0 → everything falls back
    r = A.snap_boundaries([0, 1, 2], [{"start": 0, "end": 0.1}], 15)
    assert r == {"bounds": [0, 1, 2], "shifts": [0.0, 0.0, 0.0],
                 "snapped": 0, "fallbacks": 1}


def test_snap_boundaries_single_segment():
    r = A.snap_boundaries([0, 10], [{"start": 4, "end": 6}], 15)  # n = 1
    assert r["snapped"] == 0 and r["fallbacks"] == 0
    assert r["bounds"] == [0, 10]


def test_build_aligned_plan_count_invariant():
    total = 120.0
    pauses = [{"start": 38, "end": 42}, {"start": 78, "end": 82}]  # mids 40, 80
    even = A.build_plan(total, "30")            # count = 4
    aligned = A.build_aligned_plan(total, "30", pauses, 15)

    # segment count is unchanged by snapping (the preserved contract)
    assert aligned["count"] == even["count"] == 4
    assert aligned["aligned"] is True
    assert len(aligned["segments"]) == 4

    # still tiles [0, total]
    assert abs(sum(s["duration"] for s in aligned["segments"]) - total) < 1e-6
    starts = [s["start"] for s in aligned["segments"]]
    assert starts[0] == 0
    assert all(a < b for a, b in zip(starts, starts[1:]))  # monotonic
    assert abs(starts[-1] + aligned["segments"][-1]["duration"] - total) < 1e-6


def test_build_aligned_plan_single_segment():
    a = A.build_aligned_plan(10, "30", [{"start": 4, "end": 6}], 15)
    assert a["count"] == 1
    assert a["aligned"] is False
    assert a["segments"][0]["start"] == 0


# =========================================================================== #
# Output naming
# =========================================================================== #

def test_output_name_brace():
    # the naming format is the complete file name — no source-file prefix
    assert A.output_name(0, "第 {} 集", "1", "mp3") == "第 1 集.mp3"
    assert A.output_name(2, "第 {} 集", "1", "mp3") == "第 3 集.mp3"
    assert A.output_name(0, "重活了 第 {} 集", "1", "mp3") == "重活了 第 1 集.mp3"


def test_output_name_no_brace_appends():
    assert A.output_name(0, "EP", "1", "mp3") == "EP_1.mp3"
    assert A.output_name(0, "", "1", "mp3") == "1.mp3"


def test_output_name_non_numeric_start():
    assert A.output_name(0, "第 {} 集", "abc", "mp3") == "第 1 集.mp3"
    assert A.output_name(0, "第 {} 集", "10x", "mp3") == "第 10 集.mp3"


# =========================================================================== #
# Silence-log parsing (pure)
# =========================================================================== #

def test_parse_silence_log_basic():
    lines = [
        "[silencedetect @ 0x1] silence_start: 10.5",
        "[silencedetect @ 0x1] silence_end: 12.0 | silence_duration: 1.5",
        "some unrelated ffmpeg line",
        "[silencedetect @ 0x1] silence_start: 40.0",
        "[silencedetect @ 0x1] silence_end: 43.0 | silence_duration: 3.0",
    ]
    assert A.parse_silence_log(lines, 100) == [
        {"start": 10.5, "end": 12.0}, {"start": 40.0, "end": 43.0},
    ]


def test_parse_silence_log_trailing_open_capped_at_total():
    assert A.parse_silence_log(["silence_start: 95.0"], 100) == [{"start": 95.0, "end": 100.0}]


def test_parse_silence_log_trailing_beyond_total_dropped():
    assert A.parse_silence_log(["silence_start: 105.0"], 100) == []


def test_parse_silence_log_negative_start_ignored():
    lines = ["silence_start: -0.2", "silence_end: 0.5",
             "silence_start: 10", "silence_end: 11"]
    assert A.parse_silence_log(lines, 100) == [{"start": 10.0, "end": 11.0}]


def test_parse_silence_log_sorted_by_start():
    lines = ["silence_start: 50", "silence_end: 51",
             "silence_start: 10", "silence_end: 12"]
    ps = A.parse_silence_log(lines, 100)
    assert [p["start"] for p in ps] == [10.0, 50.0]


# =========================================================================== #
# Native FFmpeg integration (skipped when FFmpeg is unavailable)
# =========================================================================== #

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
HAS_FFMPEG = bool(FFMPEG and FFPROBE)


def _run_mp3(path, *inputs):
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", *inputs,
                    "-c:a", "libmp3lame", "-b:a", "128k", str(path)], check=True)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_probe_duration(tmp_path):
    p = tmp_path / "tone.mp3"
    _run_mp3(p, "-f", "lavfi", "-i", "sine=frequency=440:duration=5")
    dur, err = A.probe_duration(p, "")  # empty → PATH
    assert err == "", err
    assert 4.9 <= dur <= 5.1


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_detect_silences_finds_real_gap(tmp_path):
    p = tmp_path / "x.mp3"
    # 2s tone + 1s true silence + 2s tone
    _run_mp3(p,
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
             "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1")
    res = A.detect_silences(p, 5.0, "")
    assert res["ok"], res
    assert any(abs(pp["start"] - 2.0) < 0.5 and pp["end"] - pp["start"] >= 0.4
               for pp in res["pauses"]), res["pauses"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_cut_segments_lossless_copy(tmp_path):
    src = tmp_path / "src.mp3"
    _run_mp3(src, "-f", "lavfi", "-i", "sine=frequency=440:duration=6")
    segs = A.build_plan(6.0, "2")["segments"]  # 3 × 2s
    out = tmp_path / "out"
    results = A.cut_segments(src, segs, out, "重活了 第 {} 集", "1", "", "mp3")

    assert len(results) == 3
    for i, r in enumerate(results):
        assert r["name"] == f"重活了 第 {i + 1} 集.mp3"
        assert Path(r["path"]).exists()
        assert Path(r["path"]).stat().st_size > 0
