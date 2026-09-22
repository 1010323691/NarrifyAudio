from __future__ import annotations

import json

import pytest

from backend.core import config as core_config
from backend.core import filesystem
from backend.core import filesystem_shortcuts
from backend.core import paths as core_paths
from backend.core import workspace_history


@pytest.fixture
def fs_root(tmp_path, monkeypatch):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path / "app")
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path / "app")
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    core_paths.reset_layout_cache()
    yield tmp_path
    core_config.reset_config_cache()
    core_paths.reset_layout_cache()


def test_folder_lifecycle_and_sorted_listing(fs_root):
    parent = fs_root / "中文 Projects"
    parent.mkdir()
    filesystem.create_folder(str(parent), "B")
    filesystem.create_folder(str(parent), "a")
    listing = filesystem.list_directories(str(parent))
    assert [item["name"] for item in listing["folders"]] == ["a", "B"]
    renamed = filesystem.rename_folder(str(parent / "a"), "改名")
    assert renamed["name"] == "改名"
    (parent / "B" / "nested").mkdir(parents=True)
    assert filesystem.delete_folder(str(parent / "B"), recursive=True, confirmed=True)["deleted"]
    assert not (parent / "B").exists()


def test_folder_names_and_conflicts_are_rejected(fs_root):
    parent = fs_root / "parent"
    parent.mkdir()
    filesystem.create_folder(str(parent), "same")
    with pytest.raises(filesystem.FilesystemError) as conflict:
        filesystem.create_folder(str(parent), "same")
    assert conflict.value.status == 409
    with pytest.raises(filesystem.FilesystemError) as invalid:
        filesystem.create_folder(str(parent), "bad/name")
    assert invalid.value.status == 400


def test_root_and_application_paths_are_protected(fs_root):
    with pytest.raises(filesystem.FilesystemError) as root_error:
        filesystem.create_folder(str(fs_root / "app"), "child")
    # The fixture's app root is protected, while its parent is not.
    assert root_error.value.status == 403


def test_history_deduplicates_and_marks_missing(fs_root):
    workspace_history.upsert(str(fs_root / "A"))
    workspace_history.upsert(str(fs_root / "a"))
    records = workspace_history.list_records(str(fs_root / "A"))
    assert len(records) == 1
    assert records[0]["is_current"] is True
    assert records[0]["exists"] is False
    assert workspace_history.remove(str(fs_root / "A")) is True
    assert workspace_history.list_records() == []


def test_corrupt_history_recovers(fs_root):
    target = workspace_history.history_file()
    target.parent.mkdir(parents=True)
    target.write_text("not-json", encoding="utf-8")
    assert workspace_history.list_records() == []
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == {"recent_workspaces": []}


def test_shortcuts_mount_dedupes_and_remove_does_not_delete_folder(fs_root):
    folder = fs_root / "挂载目录"
    folder.mkdir()
    first = filesystem_shortcuts.add(str(folder))
    second = filesystem_shortcuts.add(str(folder).replace("/", "\\"))
    assert first["path"] == second["path"]
    assert first["display_name"] == "挂载目录"
    assert len(filesystem_shortcuts.list_shortcuts()) == 1
    assert filesystem_shortcuts.remove(str(folder)) is True
    assert folder.is_dir()


def test_shortcuts_keep_missing_directory_status(fs_root):
    folder = fs_root / "稍后删除"
    folder.mkdir()
    filesystem_shortcuts.add(str(folder))
    folder.rmdir()
    rows = filesystem_shortcuts.list_shortcuts()
    assert rows[0]["exists"] is False
