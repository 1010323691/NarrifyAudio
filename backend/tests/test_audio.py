"""Tests for the audio duration probe (``backend/engines/audio.py``).

The cache layer around ``probe_duration`` is covered by ``test_audio_probe_cache``;
here we pin the ffprobe-facing behaviour: a real tone reports its duration
(skipped when FFmpeg is absent) and a missing ffprobe binary degrades to a
``nan`` duration with a readable message instead of raising.
"""
from __future__ import annotations

import math
import shutil
import subprocess

import pytest

from backend.engines import audio as A

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
HAS_FFMPEG = bool(FFMPEG and FFPROBE)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH")
def test_probe_duration(tmp_path):
    p = tmp_path / "tone.mp3"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                    "-c:a", "libmp3lame", "-b:a", "128k", str(p)], check=True)
    dur, err = A.probe_duration(p, "")  # empty → PATH
    assert err == "", err
    assert 4.9 <= dur <= 5.1


def test_probe_duration_reports_missing_ffprobe(tmp_path):
    p = tmp_path / "tone.mp3"
    p.write_bytes(b"not audio")
    dur, err = A._probe_duration_uncached(p, str(tmp_path / "no-such-ffprobe"))
    assert math.isnan(dur)
    assert "ffprobe" in err
