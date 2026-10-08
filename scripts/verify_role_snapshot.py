"""Isolated preparation measurements for 100/500/1000 independent role jobs."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil
from backend.core.script_snapshot import open_snapshot
from backend.engines.voices import _select_target_bands, _window_block, pick_ref_text


def measure(count):
    with tempfile.TemporaryDirectory(prefix="narrify-roles-") as root:
        root = Path(root)
        paths = []
        for chapter in range(40):
            path = root / f"{chapter}.json"
            path.write_text(json.dumps([{"speaker": f"role-{index}", "text": f"角色{index}第{chapter}章自己的台词与取样上下文。"} for index in range(count)], ensure_ascii=False), encoding="utf-8")
            paths.append(path)
        reads, build_logs = [], []
        original = Path.read_text
        def read(path, *args, **kwargs):
            if path in paths:
                reads.append(path)
            return original(path, *args, **kwargs)
        process = psutil.Process()
        baseline, peak = process.memory_info().rss, [process.memory_info().rss]
        done = threading.Event()
        def sample():
            while not done.wait(.01):
                peak[0] = max(peak[0], process.memory_info().rss)
        sampler = threading.Thread(target=sample)
        sampler.start()
        started = time.monotonic()
        try:
            with patch.object(Path, "read_text", read):
                for index in range(count):
                    snapshot = open_snapshot(paths, root / "snapshots", speakers=[f"role-{index}"], log=lambda *args: build_logs.append(args))
                    assert snapshot.counts == {f"role-{index}": 40}
                    pairs = snapshot.samples[f"role-{index}"]
                    bands = _select_target_bands(pairs)
                    for band in bands:
                        for position in band:
                            assert f"★ role-{index}:" in _window_block(snapshot, position)
                    assert pick_ref_text(pairs.texts())
            assert len(reads) == len(paths), (len(reads), len(paths))
            assert len(build_logs) == 1
            assert len(list((root / "snapshots").glob("*.sqlite"))) == 1
            return {"roles": count, "chapters": len(paths), "script_source_reads": len(reads),
                    "snapshot_builds": len(build_logs), "total_seconds": time.monotonic() - started,
                    "extra_peak_RSS_bytes": peak[0] - baseline}
        finally:
            done.set()
            sampler.join()


if __name__ == "__main__":
    print(json.dumps([measure(count) for count in (100, 500, 1000)], indent=2))
