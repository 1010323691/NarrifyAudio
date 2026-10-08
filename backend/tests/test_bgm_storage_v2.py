"""Per-chapter IO, recoverable legacy migration and independent writers."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import multiprocessing
from types import SimpleNamespace

import pytest

from backend.engines import bgm_storage as storage


@pytest.fixture
def layout(tmp_path):
    return SimpleNamespace(bgm=tmp_path / "08_bgm")


def legacy(layout, name, chapters):
    layout.bgm.mkdir(parents=True, exist_ok=True)
    path = layout.bgm / name
    path.write_text(json.dumps({"version": 1, "chapters": chapters}, ensure_ascii=False), encoding="utf-8")
    return path


def test_read_only_legacy_access_does_not_migrate(layout):
    legacy(layout, storage.SEGMENT_ANALYSIS_NAME, {"甲": {"fingerprint": "v1"}})
    assert storage.load_segment_analysis(layout, ["甲"])["chapters"]["甲"]["fingerprint"] == "v1"
    assert not (layout.bgm / storage.STATE_DIR).exists()


def test_interrupted_migration_resumes_immutable_snapshot(layout, monkeypatch):
    path = legacy(layout, storage.ASSIGNMENTS_NAME, {"甲": {"music": "original.mp3"}, "乙": {"music": None}})
    original = storage._atomic_write_json
    def fail(target, data, handle=None):
        if target == storage.chapter_path(layout, storage.ASSIGNMENTS_NAME, "乙"):
            raise OSError("interrupted migration")
        original(target, data, handle)
    monkeypatch.setattr(storage, "_atomic_write_json", fail)
    with pytest.raises(OSError, match="interrupted"):
        storage.ensure_migrated(layout)
    assert not storage._complete(layout)
    path.write_text(json.dumps({"chapters": {"甲": {"music": "external.mp3"}}}), encoding="utf-8")
    monkeypatch.setattr(storage, "_atomic_write_json", original)
    storage.ensure_migrated(layout)
    assert storage.load_assignments(layout)["chapters"] == {"甲": {"music": "original.mp3"}, "乙": {"music": None}}
    assert json.loads(path.read_bytes())["chapters"]["甲"]["music"] == "external.mp3"  # legacy file retained


def test_deleted_chapter_never_falls_back_to_legacy(layout):
    path = legacy(layout, storage.ASSIGNMENTS_NAME, {"甲": {"music": "old.mp3"}})
    storage.ensure_migrated(layout)
    storage.update_assignments(layout, lambda data: data["chapters"].pop("甲"), stems=["甲"])
    assert storage.load_assignments(layout, ["甲"])["chapters"] == {}
    assert "甲" in json.loads(path.read_bytes())["chapters"]
    # Even a corrupt new cache cannot resurrect the older matched result.
    storage.chapter_path(layout, storage.ASSIGNMENTS_NAME, "甲").write_bytes(b"broken")
    assert storage.load_assignments(layout, ["甲"])["chapters"] == {}


def test_hash_paths_preserve_distinct_original_stems(layout):
    stems = ["甲/乙", "甲\\乙", "甲?乙", "甲*乙", "CHAPTER", "chapter"]
    storage.save_assignments(layout, {"version": 1, "chapters": {stem: {"music": stem} for stem in stems}})
    paths = [storage.chapter_path(layout, storage.ASSIGNMENTS_NAME, stem) for stem in stems]
    assert len(set(path.name.casefold() for path in paths)) == len(stems)
    assert all(path.parent == layout.bgm / storage.STATE_DIR / "assignments" for path in paths)
    assert set(storage.load_assignments(layout)["chapters"]) == set(stems)


@pytest.mark.parametrize("count", [100, 500, 1000])
def test_single_chapter_update_only_reads_and_writes_its_state(layout, monkeypatch, count):
    chapters = {str(index): {"fingerprint": "old", "blocks": [{"start": 0, "end": 5, "music_tags": {"mood": ["旧"]}}]} for index in range(count)}
    storage.save_segment_analysis(layout, {"version": 1, "model": "test", "chapters": chapters})
    reads, writes = [], []
    original_read, original_write = Path.read_bytes, storage._atomic_write_json
    def read(path):
        reads.append(path)
        return original_read(path)
    def write(path, data, handle=None):
        writes.append(path)
        return original_write(path, data, handle)
    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(storage, "_atomic_write_json", write)
    storage.update_segment_analysis(layout, lambda data: data["chapters"]["0"].update({"fingerprint": "new"}), stems=["0"])
    expected = storage.chapter_path(layout, storage.SEGMENT_ANALYSIS_NAME, "0")
    assert reads.count(expected) == 1
    assert len(reads) == 3  # one chapter, metadata read and its version update
    assert writes[:2] == [expected, storage.chapter_path(layout, storage.SEGMENT_ANALYSIS_NAME, "0", summary=True)]
    assert writes[2].name == "metadata.json"
    assert storage.load_segment_analysis(layout, ["1"])["chapters"]["1"]["fingerprint"] == "old"


def test_independent_chapter_writers_do_not_wait_for_slow_mutator(layout):
    storage.save_assignments(layout, {"chapters": {"甲": {"music": None}, "乙": {"music": None}}})
    entered, release = threading.Event(), threading.Event()
    def slow(data):
        entered.set()
        assert release.wait(5)
        data["chapters"]["甲"]["music"] = "a.mp3"
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(storage.update_assignments, layout, slow, stems=["甲"])
        try:
            assert entered.wait(5)
            second = pool.submit(storage.update_assignments, layout, lambda data: data["chapters"]["乙"].update({"music": "b.mp3"}), stems=["乙"])
            assert second.result(timeout=2)["chapters"]["乙"]["music"] == "b.mp3"
        finally:
            release.set()
        first.result(timeout=5)
    assert storage.load_assignments(layout)["chapters"] == {"甲": {"music": "a.mp3"}, "乙": {"music": "b.mp3"}}


def test_segment_summary_omits_blocks_but_retains_tag_union(layout):
    entry = {"fingerprint": "text-version", "entry_count": 100, "blocks": [
        {"music_tags": {"mood": ["紧张", "紧张"]}}, {"extend": True, "music_tags": {"mood": ["忽略"]}},
        {"music_tags": {"mood": ["平静"], "scene": ["夜间"]}}]}
    storage.save_segment_analysis(layout, {"chapters": {"甲": entry}})
    result = storage.load_segment_summaries(layout, ["甲"])["chapters"]["甲"]
    assert result["has_analysis"] and result["entry_count"] == 100
    assert result["tags"] == {"mood": ["紧张", "平静"], "scene": ["夜间"]}
    assert "blocks" not in result


def test_unmodified_metadata_does_not_overwrite_concurrent_mode_change(layout):
    storage.save_assignments(layout, {"mode": "random", "chapters": {"甲": {}, "乙": {}}})
    entered, release = threading.Event(), threading.Event()
    def slow(data):
        entered.set()
        assert release.wait(5)
        data["chapters"]["甲"] = {"music": "a.mp3"}
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(storage.update_assignments, layout, slow, stems=["甲"])
        try:
            assert entered.wait(5)
            storage.update_assignments(layout, lambda data: data.update({"mode": "segment"}), stems=["乙"])
        finally:
            release.set()
        first.result(timeout=5)
    assert storage.load_assignments(layout, ["甲"])["mode"] == "segment"


def _increment_chapter(root, repeats):
    layout = SimpleNamespace(bgm=Path(root))
    def increment(data):
        data["chapters"]["甲"]["count"] += 1
    for _ in range(repeats):
        storage.update_assignments(layout, increment, stems=["甲"])


def test_concurrent_processes_serialize_same_chapter_updates(layout):
    storage.save_assignments(layout, {"chapters": {"甲": {"count": 0}}})
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_increment_chapter, args=(str(layout.bgm), 10)) for _ in range(4)]
    try:
        for process in workers:
            process.start()
        for process in workers:
            process.join(timeout=15)
        assert all(process.exitcode == 0 for process in workers)
        assert storage.load_assignments(layout, ["甲"])["chapters"]["甲"]["count"] == 40
    finally:
        for process in workers:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def test_admin_usage_reads_v2_and_ignores_legacy_after_migration(layout):
    from backend.services.admin_storage import read_bgm_usage
    legacy(layout, storage.ASSIGNMENTS_NAME, {"甲": {"music": "old.mp3"}})
    storage.update_assignments(layout, lambda data: data["chapters"]["甲"].update({"music": "new.mp3"}), stems=["甲"])
    assert read_bgm_usage(layout.bgm.parent) == {"new.mp3": 1}


def test_summary_revision_changes_when_only_full_analysis_changes(layout):
    storage.save_segment_analysis(layout, {"chapters": {"甲": {"model": "first", "blocks": [{"music_tags": {"mood": ["紧张"]}}]}}})
    path = storage.chapter_path(layout, storage.SEGMENT_ANALYSIS_NAME, "甲", summary=True)
    before = json.loads(path.read_bytes())
    storage.update_segment_analysis(layout, lambda data: data["chapters"]["甲"].update({"model": "second"}), stems=["甲"])
    after = json.loads(path.read_bytes())
    assert before["entry"] == after["entry"]
    assert before["revision"] != after["revision"]
    assert before["source_version"] != after["source_version"]
    chapter = json.loads(storage.chapter_path(layout, storage.SEGMENT_ANALYSIS_NAME, "甲").read_bytes())
    assert (chapter["revision"], chapter["source_version"]) == (after["revision"], after["source_version"])


def test_downgrade_export_contains_current_state_and_retains_snapshots(layout):
    old = legacy(layout, storage.ASSIGNMENTS_NAME, {"甲": {"music": "old.mp3"}})
    storage.update_assignments(layout, lambda data: data["chapters"]["甲"].update({"music": "new.mp3"}), stems=["甲"])
    destination = layout.bgm.parent / "export"
    storage.export_legacy(layout, destination)
    assert json.loads((destination / storage.ASSIGNMENTS_NAME).read_bytes())["chapters"]["甲"]["music"] == "new.mp3"
    assert json.loads(old.read_bytes())["chapters"]["甲"]["music"] == "old.mp3"
    assert storage._complete(layout)
