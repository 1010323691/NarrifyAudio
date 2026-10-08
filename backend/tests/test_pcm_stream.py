"""Sample parity against the previous two-stage pydub merge."""
import importlib.util
from pathlib import Path
import random
import shutil
import subprocess
import wave

import pytest

from backend.core import pcm_stream


def worker():
    path = Path(__file__).resolve().parents[2] / "tts-engine" / "tts_worker.py"
    spec = importlib.util.spec_from_file_location("pcm_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def audio_file(path, channels, rate, width):
    random_source = random.Random(1729 + rate + width)
    # Enough data to exercise resampler continuity across several PCM blocks.
    data = random_source.randbytes(int(rate * 2.1) * channels * width)
    with wave.open(str(path), "wb") as writer:
        writer.setparams((channels, width, rate, 0, "NONE", "not compressed"))
        writer.writeframes(data)


@pytest.mark.parametrize("formats", [
    [(1, 24000, 2)] * 7,
    [(1, 8000, 1), (1, 24000, 3), (2, 48000, 2), (1, 16000, 4), (1, 8000, 2)],
    [(1, 48000, 4), (1, 22050, 2), (2, 24000, 2), (2, 48000, 4)],
    [(1, 24000, 2), (4, 24000, 2), (1, 24000, 2)],
])
@pytest.mark.parametrize("batch_size", [1, 3, 100])
def test_streamed_two_stage_merge_matches_legacy_samples(tmp_path, formats, batch_size):
    from pydub import AudioSegment
    tw = worker()
    segments = []
    for index, fmt in enumerate(formats):
        path = tmp_path / f"segment-{index}.wav"
        audio_file(path, *fmt)
        segments.append({"index": index, "path": str(path), "speaker": "A" if index % 3 else "B",
                         "pause_after": [0, 123, None, "bad"][index % 4]})
    # Reproduce the original implementation, including format conversions at
    # both levels. A single-pass reference changes mixed-format resampling.
    old_parts = []
    old_metadata = []
    for start in range(0, len(segments), batch_size):
        batch = segments[start:start + batch_size]
        result = tw.combine_audio_with_pauses(
            [AudioSegment.from_file(item["path"]) for item in batch],
            [item["speaker"] for item in batch], 500, 250,
            [tw.normalize_pause_ms(item["pause_after"]) for item in batch])
        old_parts.append(result)
        old_metadata.append((batch[0], batch[-1]))
    overrides = [tw.boundary_gap_ms(last["pause_after"], last["speaker"], old_metadata[i + 1][0]["speaker"], 500, 250)
                 for i, (_, last) in enumerate(old_metadata[:-1])] + [None]
    expected = tw.combine_audio_with_pauses(old_parts, [first["speaker"] for first, _ in old_metadata], 500, 250, overrides)
    parts = tw._merge_stage1(segments, str(tmp_path), 500, 250, batch_size, len(segments), str(tmp_path))
    path, duration = tw._merge_stage2(parts, str(tmp_path), str(tmp_path / "book.mp3"), 500, 250, len(parts))
    actual = AudioSegment.from_file(path)
    assert (actual.channels, actual.sample_width, actual.frame_rate) == (expected.channels, expected.sample_width, expected.frame_rate)
    assert actual.raw_data == expected.raw_data
    assert duration == len(expected) / 1000
    assert not list(tmp_path.glob("*.pcm"))


def test_missing_batch_keeps_surviving_boundary_override(tmp_path):
    from pydub import AudioSegment
    tw = worker()
    first, last = tmp_path / "first.wav", tmp_path / "last.wav"
    audio_file(first, 1, 24000, 2)
    audio_file(last, 1, 24000, 2)
    rows = [{"path": str(first), "speaker": "A", "pause_after": 700},
            {"path": str(tmp_path / "missing.wav"), "speaker": "B"},
            {"path": str(last), "speaker": "A"}]
    parts = tw._merge_stage1(rows, str(tmp_path), 500, 250, 1, 3, str(tmp_path))
    path, _ = tw._merge_stage2(parts, str(tmp_path), "book.mp3", 500, 250, 3)
    expected = tw.combine_audio_with_pauses([AudioSegment.from_file(first), AudioSegment.from_file(last)], ["A", "A"], 500, 250, [700, None])
    assert AudioSegment.from_file(path).raw_data == expected.raw_data


def test_truncated_wave_is_rejected_before_appending(tmp_path):
    path = tmp_path / "truncated.wav"
    audio_file(path, 1, 24000, 2)
    with path.open("r+b") as writer:
        writer.truncate(100)
    with pytest.raises(ValueError, match="Truncated"):
        pcm_stream.WaveInput(path)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required for media decoding")
def test_compressed_input_decodes_to_disk_with_legacy_sample_parity(tmp_path):
    from pydub import AudioSegment
    tw = worker()
    wav = tmp_path / "input.wav"
    mp3 = tmp_path / "input.mp3"
    audio_file(wav, 1, 24000, 2)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-codec:a", "libmp3lame", str(mp3)], check=True)
    rows = [{"path": str(mp3), "speaker": "A"}, {"path": str(wav), "speaker": "B"}]
    expected = tw.combine_audio_with_pauses([AudioSegment.from_file(mp3), AudioSegment.from_file(wav)], ["A", "B"])
    parts = tw._merge_stage1(rows, str(tmp_path), 500, 250, 100, 2, str(tmp_path))
    path, _ = tw._merge_stage2(parts, str(tmp_path), "book.mp3", 500, 250, 1)
    assert AudioSegment.from_file(path).raw_data == expected.raw_data
    assert not list(tmp_path.glob("decode-*"))
