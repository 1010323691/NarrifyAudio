import json
import os
import time
from pathlib import Path

import pytest

from backend.core.script_snapshot import open_snapshot
from backend.engines import voices


@pytest.mark.parametrize("count", [100, 500, 1000])
def test_role_snapshot_is_built_once_and_reads_only_selected_role(tmp_path, monkeypatch, count):
    paths, rows = [], []
    for chapter in range(3):
        path = tmp_path / f"{chapter}.json"
        data = [{"speaker": f"role-{index}", "text": f"{chapter}-{index} 足够长的角色台词用于参考文本取样。"} for index in range(count)]
        path.write_text(json.dumps(data), encoding="utf-8")
        paths.append(path)
        rows.extend(data)
    directory = tmp_path / "snapshots"
    first = open_snapshot(paths, directory, speakers=["role-0"])
    assert first.counts == {"role-0": 3}
    original = Path.read_text
    def read(path, *args, **kwargs):
        assert path not in paths, "warm role task reread script source"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    for role in ["role-1", f"role-{count - 1}"]:
        snapshot = open_snapshot(paths, directory, speakers=[role])
        assert snapshot.path == first.path
        assert list(snapshot.counts) == [role]
        pairs = snapshot.samples[role]
        expected = [(index, row["text"]) for index, row in enumerate(rows) if row["speaker"] == role]
        assert list(pairs) == expected
        assert voices._select_target_bands(pairs) == voices._select_target_bands(expected)
        assert voices._window_block(snapshot, expected[1][0]) == voices._window_block(rows, expected[1][0])


def test_large_role_sampling_and_reference_text_preserve_old_semantics(tmp_path):
    rows = [{"speaker": "A" if index % 3 else "B", "text": f"line {index}" if index < 60 else f"足够长的角色台词{index}用于选择参考文本。"} for index in range(200)]
    path = tmp_path / "script.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    snapshot = open_snapshot([path], tmp_path / "snapshots")
    old_samples, order = voices._collect_samples(rows)
    samples, new_order = voices._collect_samples(snapshot)
    assert order == new_order
    for role in order:
        assert voices._select_target_bands(samples[role]) == voices._select_target_bands(old_samples[role])
        assert voices.pick_ref_text(samples[role].texts()) == voices.pick_ref_text([text for _, text in old_samples[role]])


def test_script_change_creates_new_immutable_snapshot(tmp_path):
    path = tmp_path / "script.json"
    path.write_text(json.dumps([{"speaker": "A", "text": "old"}]), encoding="utf-8")
    first = open_snapshot([path], tmp_path / "snapshots")
    path.write_text(json.dumps([{"speaker": "B", "text": "new"}]), encoding="utf-8")
    second = open_snapshot([path], tmp_path / "snapshots")
    assert second.path != first.path
    assert first[0] == {"speaker": "A", "text": "old"}
    assert second[0] == {"speaker": "B", "text": "new"}


def test_source_change_during_build_does_not_publish_snapshot(tmp_path):
    path = tmp_path / "script.json"
    path.write_text(json.dumps([{"speaker": "A", "text": "old"}]), encoding="utf-8")
    directory = tmp_path / "snapshots"
    def mutate():
        path.write_text(json.dumps([{"speaker": "B", "text": "changed"}]), encoding="utf-8")
        return False
    with pytest.raises(RuntimeError, match="发生变化"):
        open_snapshot([path], directory, cancelled=mutate)
    assert not list(directory.glob("*.sqlite"))


def test_expected_version_rejects_any_source_change_immediately(tmp_path):
    """The input-change check is a completeness contract, not a cache:
    external edits carry no epoch signal, so no time window may mask them."""
    from backend.core.script_snapshot import open_snapshot, source_version
    from backend.core.workspace_epochs import managed_mutation
    workspace = tmp_path / "workspace"
    (workspace / "00_temp" / "script-snapshots").mkdir(parents=True)
    (workspace / "03_parsed_json").mkdir()
    paths = []
    for chapter in range(4):
        path = workspace / "03_parsed_json" / f"{chapter:02d}.json"
        path.write_text(json.dumps([{"speaker": "A", "text": f"第{chapter}章台词"}]), encoding="utf-8")
        paths.append(path)
    version, _ = source_version(paths)
    directory = workspace / "00_temp" / "script-snapshots"
    first = open_snapshot(paths, directory, expected_version=version)
    # External (unmanaged) edit, no epoch bump: the very next open must reject.
    paths[1].write_text(json.dumps([{"speaker": "A", "text": "外部编辑"}]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="剧本输入已变更"):
        open_snapshot(paths, directory, expected_version=version)
    second_version, _ = source_version(paths)
    rebuilt = open_snapshot(paths, directory, expected_version=second_version)
    assert rebuilt.path != first.path
    # Managed write bumps the module epoch and is rejected the same way.
    with managed_mutation(paths[2]):
        paths[2].write_text(json.dumps([{"speaker": "A", "text": "受管改动"}]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="剧本输入已变更"):
        open_snapshot(paths, directory, expected_version=second_version)


def test_snapshot_prune_is_budgeted_per_round_and_interval(tmp_path):
    from backend.core.script_snapshot import prune_snapshots
    directory = tmp_path / "snapshots"
    directory.mkdir()
    names = []
    now = time.time()
    for index in range(8):
        path = directory / (f"{'%064x' % (index + 1)}.sqlite")
        path.write_bytes(b"")
        age = (8 * index + 8) * 86400
        os.utime(path, (now - age, now - age))
        names.append(path.name)
    assert prune_snapshots(directory, limit=1) == 1
    assert prune_snapshots(directory, limit=1) == 0  # interval budget
    (directory / ".prune-at").unlink()
    assert prune_snapshots(directory, limit=1) == 1
    (directory / ".prune-at").unlink()
    # With the budget restored the two remaining stale versions are removed;
    # the recent four survive regardless of age.
    assert prune_snapshots(directory, limit=10) == 2
    assert {path.name for path in directory.glob("*.sqlite")} == set(names[:4])


def test_snapshot_prune_ignores_foreign_files_and_stale_stages(tmp_path):
    from backend.core.script_snapshot import prune_snapshots
    directory = tmp_path / "snapshots"
    directory.mkdir()
    foreign = directory / "not-a-snapshot.sqlite"
    foreign.write_bytes(b"")
    staged = directory / ("." + "a" * 64 + ".b" * 32 + ".sqlite")
    staged.write_bytes(b"")
    os.utime(staged, (time.time() - 10, time.time() - 10))
    stray = directory / "readme.json"
    stray.write_bytes(b"{}")
    pruned = prune_snapshots(directory, max_age=0, limit=10)
    assert pruned == 1
    assert foreign.exists()
    assert stray.exists()


def test_bands_tolerate_tiny_per_and_use_a_single_connection(tmp_path, monkeypatch):
    from backend.core import script_snapshot
    rows = [{"speaker": "A", "text": f"line {index}"} for index in range(50)]
    path = tmp_path / "script.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    pairs = open_snapshot([path], tmp_path / "snapshots").samples["A"]
    assert pairs.bands(1) == pairs.bands(2)
    connects = []
    original = script_snapshot._connect
    monkeypatch.setattr(script_snapshot, "_connect", lambda *a, **k: connects.append(1) or original(*a, **k))
    front, middle, back = pairs.bands(8)
    assert len(front) == len(middle) == len(back) == 8 and len(connects) <= 2


def test_prune_skips_a_snapshot_whose_version_lock_is_held(tmp_path):
    import os
    from backend.core.file_lock import exclusive_file_lock
    from backend.core.script_snapshot import prune_snapshots
    names = [f"{index:064x}" for index in range(3)]
    for index, name in enumerate(names):
        path = tmp_path / f"{name}.sqlite"
        path.write_bytes(b"x")
        os.utime(path, (1000 + index, 1000 + index))
    with exclusive_file_lock(tmp_path / f"{names[0]}.lock"):
        assert prune_snapshots(tmp_path, keep=1, max_age=1, interval=0) == 1
    assert (tmp_path / f"{names[0]}.sqlite").exists()
    assert not (tmp_path / f"{names[1]}.sqlite").exists()


def test_reused_snapshot_is_refreshed_and_pruned_lock_is_removed(tmp_path):
    import os
    from backend.core.script_snapshot import open_snapshot, prune_snapshots
    source = tmp_path / "03_parsed_json" / "a.json"
    source.parent.mkdir()
    source.write_text('[{"speaker": "A", "text": "hi"}]', encoding="utf-8")
    cache = tmp_path / "cache"
    snapshot = open_snapshot([source], cache)
    sqlite = snapshot.path
    os.utime(sqlite, (1000, 1000))
    open_snapshot([source], cache)  # reuse refreshes the age gate
    assert sqlite.stat().st_mtime > 1000
    assert prune_snapshots(cache, keep=0, max_age=60, interval=0) == 0
    os.utime(sqlite, (1000, 1000))
    assert prune_snapshots(cache, keep=0, max_age=60, interval=0) == 1
    assert not sqlite.exists() and not sqlite.with_suffix(".lock").exists()
