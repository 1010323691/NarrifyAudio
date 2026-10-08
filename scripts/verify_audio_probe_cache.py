"""Isolated actual ffprobe count for 100/500/1000 unchanged audio inputs."""
import json
import os
from pathlib import Path
import resource
import shutil
import sys
import tempfile
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.engines import audio, bgm


def measure(count):
    with tempfile.TemporaryDirectory(prefix="narrify-probe-cache-") as temporary:
        root = Path(temporary)
        previous = os.environ.get("NARRIFY_PROBE_CACHE_DIR")
        os.environ["NARRIFY_PROBE_CACHE_DIR"] = str(root / "cache")
        original = audio._probe_duration_uncached
        launches = []
        def measured(*args, **kwargs):
            launches.append(True)
            return original(*args, **kwargs)
        audio._probe_duration_uncached = measured
        try:
            paths = []
            for i in range(count):
                path = root / f"segment-{i}.wav"
                with wave.open(str(path), "wb") as output:
                    output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    output.writeframes(b"\x00\x00" * 2400)
                paths.append(path)
            memory_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
            start = time.perf_counter()
            for path in paths:
                assert audio.probe_duration(path) == (0.1, "")
            first_seconds = time.perf_counter() - start
            first_count = len(launches)
            start = time.perf_counter()
            for path in paths:
                assert bgm.probe_duration(path) == (0.1, "")
            second_seconds = time.perf_counter() - start
            assert first_count == count and len(launches) == first_count
            print(json.dumps(dict(inputs=count, first_probe_processes=first_count, second_probe_processes=0,
                first_seconds=round(first_seconds, 3), second_seconds=round(second_seconds, 3),
                cache_bytes=(root / "cache" / "duration.sqlite").stat().st_size,
                extra_parent_peak_rss=max(0, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 - memory_before))), flush=True)
        finally:
            audio._probe_duration_uncached = original
            if previous is None:
                os.environ.pop("NARRIFY_PROBE_CACHE_DIR", None)
            else:
                os.environ["NARRIFY_PROBE_CACHE_DIR"] = previous


if __name__ == "__main__":
    if not shutil.which("ffprobe"):
        raise SystemExit("ffprobe is required")
    for count in (100, 500, 1000):
        measure(count)
