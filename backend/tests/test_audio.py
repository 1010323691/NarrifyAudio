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


def test_get_extension():
    for args, expected in [
        (('song.mp3',), 'mp3'),
        (('song.MP3',), 'mp3'),
        (('song',), 'mp3'),
        (('song.',), 'mp3'),
        (('archive.tar.gz',), 'gz'),
        (('',), 'mp3'),
    ]:
        assert A.get_extension(*args) == expected, args


# =========================================================================== #
# Even-split planning
# =========================================================================== #

def test_build_plan_boundaries():
    # build plan even
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

    # build plan exact division
    p = A.build_plan(90, "30")
    assert p["count"] == 3
    assert all(abs(s["duration"] - 30) < 1e-9 for s in p["segments"])

    # build plan single segment
    p = A.build_plan(10, "30")
    assert p["count"] == 1
    assert p["segments"][0]["start"] == 0
    assert abs(p["segments"][0]["duration"] - 10) < 1e-9

    # build plan invalid
    assert not A.build_plan(0, "30")["valid"]
    assert not A.build_plan(-5, "30")["valid"]
    assert not A.build_plan(100, "0")["valid"]
    assert not A.build_plan(100, "abc")["valid"]
    assert not A.build_plan(float("nan"), "30")["valid"]


# =========================================================================== #
# Pause-snapping
# =========================================================================== #

def test_snap_boundaries_contract():
    # snap boundaries shifts to pause
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

    # snap boundaries fallback no pause in window
    # W = min(5, 15 - 0.5) = 5 → window [25, 35]; the only pause (mid 1) is far away
    r = A.snap_boundaries([0, 30, 60], [{"start": 0, "end": 2}], 5)
    assert r["bounds"] == [0, 30, 60]  # unchanged → even-split time
    assert r["snapped"] == 0
    assert r["fallbacks"] == 1

    # snap boundaries window too small
    # segment width 1s → W = (1/2) - 0.5 = 0 → everything falls back
    r = A.snap_boundaries([0, 1, 2], [{"start": 0, "end": 0.1}], 15)
    assert r == {"bounds": [0, 1, 2], "shifts": [0.0, 0.0, 0.0],
                 "snapped": 0, "fallbacks": 1}

    # snap boundaries single segment
    r = A.snap_boundaries([0, 10], [{"start": 4, "end": 6}], 15)  # n = 1
    assert r["snapped"] == 0 and r["fallbacks"] == 0
    assert r["bounds"] == [0, 10]


def test_aligned_plan_contract():
    # build aligned plan count invariant
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

    # build aligned plan single segment
    a = A.build_aligned_plan(10, "30", [{"start": 4, "end": 6}], 15)
    assert a["count"] == 1
    assert a["aligned"] is False
    assert a["segments"][0]["start"] == 0


# =========================================================================== #
# Output naming
# =========================================================================== #

def test_output_name_contract():
    for args, expected in [
        ((0, '第 {} 集', '1', 'mp3',), '第 1 集.mp3'),
        ((2, '第 {} 集', '1', 'mp3',), '第 3 集.mp3'),
        ((0, '重活了 第 {} 集', '1', 'mp3',), '重活了 第 1 集.mp3'),
        ((0, 'EP', '1', 'mp3',), 'EP_1.mp3'),
        ((0, '', '1', 'mp3',), '1.mp3'),
        ((0, '第 {} 集', 'abc', 'mp3',), '第 1 集.mp3'),
        ((0, '第 {} 集', '10x', 'mp3',), '第 10 集.mp3'),
    ]:
        assert A.output_name(*args) == expected, args


# =========================================================================== #
# Silence-log parsing (pure)
# =========================================================================== #

def test_silence_log_contract():
    # parse silence log basic
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

    # parse silence log trailing open capped at total
    assert A.parse_silence_log(["silence_start: 95.0"], 100) == [{"start": 95.0, "end": 100.0}]

    # parse silence log trailing beyond total dropped
    assert A.parse_silence_log(["silence_start: 105.0"], 100) == []

    # parse silence log negative start ignored
    lines = ["silence_start: -0.2", "silence_end: 0.5",
             "silence_start: 10", "silence_end: 11"]
    assert A.parse_silence_log(lines, 100) == [{"start": 10.0, "end": 11.0}]

    # parse silence log sorted by start
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
