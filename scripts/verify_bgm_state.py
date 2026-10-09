"""Isolated chapter-store IO measurement for 100/500/1000 chapters."""
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.engines import bgm_storage as storage


def measure(count):
    with tempfile.TemporaryDirectory(prefix="narrify-bgm-v2-") as root:
        layout = SimpleNamespace(bgm=Path(root) / "08_bgm")
        storage.ensure_migrated(layout)
        written, read_bytes, read_count = [0], [0], [0]
        atomic, read = storage._atomic_write_json, Path.read_bytes
        def measured_write(path, data, handle=None):
            written[0] += len(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            return atomic(path, data, handle)
        def measured_read(path):
            value = read(path)
            read_bytes[0] += len(value)
            read_count[0] += 1
            return value
        start = time.monotonic()
        with patch.object(storage, "_atomic_write_json", measured_write), patch.object(Path, "read_bytes", measured_read):
            for index in range(count):
                stem = str(index)
                entry = {"fingerprint": "text-version", "entry_count": 100,
                         "blocks": [{"start": block * 5, "end": block * 5 + 4,
                                     "music_tags": {"mood": ["紧张"], "scene": ["夜间"]},
                                     "scene_desc": "隔离负载场景"} for block in range(20)]}
                storage.update_segment_analysis(layout, lambda data: data["chapters"].update({stem: entry}), stems=[stem])
            elapsed = time.monotonic() - start
            before_bytes, before_count = read_bytes[0], read_count[0]
            selected = storage.load_segment_analysis(layout, ["0"])
            assert list(selected["chapters"]) == ["0"]
            last_reads, last_bytes = read_count[0] - before_count, read_bytes[0] - before_bytes
            assert last_reads == 2
        return {"chapters": count, "serialized_bytes": written[0], "read_bytes": read_bytes[0],
                "update_seconds": elapsed, "single_chapter_reads": last_reads, "single_chapter_read_bytes": last_bytes}


if __name__ == "__main__":
    print(json.dumps([measure(count) for count in (100, 500, 1000)], ensure_ascii=False, indent=2))
