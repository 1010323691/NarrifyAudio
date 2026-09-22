"""Tests for the single-root Layout (everything follows the user's workspace).

Every test points the layout/config at throwaway ``tmp_path`` directories (via
monkeypatched module globals + a pointer-less root ``app.json``), so the real
project's files are never touched.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from backend.core import config as core_config
from backend.core import paths as core_paths


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Redirect the project-root globals into ``tmp_path`` and start from a clean
    (pointer-less) root ``app.json``. The in-memory config cache is reset around
    each test so it re-resolves against the sandbox."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(
        json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8"
    )
    core_config.reset_config_cache()
    core_paths.reset_layout_cache()  # module state outlives the monkeypatched TEMPLATE_FILE
    yield tmp_path
    core_config.reset_config_cache()
    core_paths.reset_layout_cache()


@pytest.fixture
def set_pointer(sandbox):
    """Write the workspace pointer into the sandboxed root ``app.json``."""

    def _set(working_dir: str):
        core_config.set_workspace_pointer(working_dir)

    return _set


# -- Layout construction ------------------------------------------------------

def test_layout_maps_single_root(tmp_path):
    ws = tmp_path / "ws"
    layout = core_paths.Layout(ws)
    # The seven artifact dirs + scratch all follow the workspace:
    assert layout.input == ws / "01_input"
    assert layout.split_text == ws / "02_split_text"
    assert layout.parsed_json == ws / "03_parsed_json"
    assert layout.voice_profiles == ws / "04_voice_profiles"
    assert layout.audio_chunk == ws / "05_audio_chunk"
    assert layout.audio_merge == ws / "06_audio_merge"
    assert layout.output == ws / "07_output"
    assert layout.bgm == ws / "08_bgm"
    assert layout.temp == ws / "00_temp"
    # Config + log now live inside the workspace too:
    assert layout.logs == ws / "logs"
    assert layout.config == ws / "config"


def test_layout_unset_is_inert(tmp_path):
    layout = core_paths.Layout(None)
    assert layout.workspace is None
    assert layout.input is None
    assert layout.output is None
    assert layout.bgm is None
    assert layout.logs is None
    assert layout.config is None
    assert layout.dirs() == {}
    # ensure() must not raise or create anything while unset:
    layout.ensure()


def test_dirs_helper_lists_artifact_dirs(tmp_path):
    ws = tmp_path / "ws"
    d = core_paths.Layout(ws).dirs()
    assert set(d) == set(core_paths.WORKSPACE_DIR_NAMES)
    assert d["01_input"] == str(ws / "01_input")
    assert d["06_audio_merge"] == str(ws / "06_audio_merge")
    assert d["08_bgm"] == str(ws / "08_bgm")
    # 08_bgm 必须在 dirs() 里（files API 的 list/download 依赖 WORKSPACE_DIRS 派生）。
    assert "08_bgm" in core_paths.WORKSPACE_DIR_NAMES


def test_music_library_dir_is_project_root_level(sandbox, monkeypatch):
    # 音乐库 = 项目根级全局目录（工作空间外、跨工程共享），固定常量不可配置。
    # 常量在 import 时按真实 PROJECT_ROOT 求值，故测试直接 monkeypatch 该属性
    # （引擎侧一律模块属性访问 core_paths.MUSIC_LIBRARY_DIR，与此一致）。
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", sandbox / "music_library")
    assert core_paths.MUSIC_LIBRARY_DIR == sandbox / "music_library"
    # 它不是工作空间目录（不进 WORKSPACE_DIRS / Layout / pathio markers）。
    assert "music_library" not in core_paths.WORKSPACE_DIR_NAMES
    assert "music_library" not in core_paths.WORKSPACE_DIRS
    from backend.core import pathio as core_pathio

    assert "music_library" not in core_pathio.WORKSPACE_MARKERS


def test_ensure_creates_everything_and_is_idempotent(tmp_path):
    ws = tmp_path / "ws"
    core_paths.Layout(ws).ensure()
    for name in core_paths.WORKSPACE_DIR_NAMES:
        assert (ws / name).is_dir()
    assert (ws / "logs").is_dir()
    assert (ws / "config").is_dir()
    core_paths.Layout(ws).ensure()  # second pass must not raise


# -- get_layout resolution ----------------------------------------------------

def test_get_layout_unset_is_inert(sandbox, set_pointer):
    set_pointer("")
    layout = core_paths.get_layout()
    assert layout.workspace is None
    # Nothing is planted in the project directory while unset:
    for name in core_paths.WORKSPACE_DIR_NAMES:
        assert not (sandbox / name).exists()
    assert not (sandbox / "logs").exists()
    assert not (sandbox / "config").exists()


def test_get_layout_set_creates_workspace_dirs(sandbox, set_pointer):
    ws = sandbox / "MyBook"
    ws.mkdir()  # the workspace endpoint creates the root folder; get_layout fills in the rest
    set_pointer(str(ws))
    layout = core_paths.get_layout()
    assert layout.workspace == ws
    for name in core_paths.WORKSPACE_DIR_NAMES:
        assert (ws / name).is_dir()
    assert (ws / "logs").is_dir()
    assert (ws / "config").is_dir()
    # Second call is a no-op (exist_ok), not an error:
    core_paths.get_layout()


def test_get_layout_missing_root_is_inert(sandbox, set_pointer):
    """A pointer to a folder that no longer exists (the workspace was moved / deleted)
    must NOT resurrect an empty skeleton at the old location — the dashboard reports
    it (``exists: false``) and the write endpoints answer 409 until re-selection."""
    set_pointer(str(sandbox / "Gone"))
    layout = core_paths.get_layout()
    assert layout.workspace == sandbox / "Gone"  # still "set" …
    assert not (sandbox / "Gone").exists()       # … but nothing is planted on disk
    assert not (sandbox / "Gone" / "01_input").exists()
    assert not (sandbox / "Gone" / "logs").exists()
    assert not (sandbox / "Gone" / "config").exists()


def test_get_layout_relative_working_dir_resolves_against_project(sandbox, set_pointer):
    ws = sandbox / "rel" / "ws"
    ws.mkdir(parents=True)  # a relative pointer names a folder that (was) created by the endpoint
    set_pointer("rel/ws")
    layout = core_paths.get_layout()
    assert layout.workspace == ws
    assert (ws / "01_input").is_dir()


def test_get_layout_is_idempotent_across_calls(sandbox, set_pointer):
    ws = sandbox / "MyBook"
    set_pointer(str(ws))
    first = core_paths.get_layout()
    second = core_paths.get_layout()
    assert first.workspace == second.workspace
    assert first.input == second.input


def test_get_layout_cache_hit_does_not_replant_after_workspace_deleted(sandbox, set_pointer):
    """A cached hit must NOT re-run ``ensure()``: once the workspace folder is gone
    (moved / deleted), later calls stay inert and must not resurrect the skeleton at the
    old location (the pre-cache behavior was guarded by ``exists()``; the memoization keeps
    it via the ``ensured`` flag instead of re-statting + re-mkdir'ing on every call)."""
    ws = sandbox / "MyBook"
    ws.mkdir()
    set_pointer(str(ws))
    core_paths.get_layout()  # plants the skeleton and memoizes (ensured)
    for name in core_paths.WORKSPACE_DIR_NAMES:
        assert (ws / name).is_dir()
    shutil.rmtree(ws)  # the folder vanishes under the pointer
    for _ in range(3):  # repeated cached hits
        core_paths.get_layout()
    assert not (ws / "01_input").exists()  # no ghost skeleton replanted
    assert not (ws / "logs").exists()
    assert not (ws / "config").exists()


def test_get_layout_stale_pointer_folder_comeback_plants_skeleton_once(sandbox, set_pointer):
    """A cached stale-pointer entry (the folder was gone when it was memoized) plants the
    skeleton EXACTLY ONCE when the folder comes back — same skeleton-planting as an uncached
    call, but no mkdir probes on the subsequent hits."""
    ws = sandbox / "Gone"
    set_pointer(str(ws))  # pointer to a folder that does not exist
    layout = core_paths.get_layout()
    assert layout.workspace == ws
    assert not (ws / "01_input").exists()  # stale: nothing planted
    ws.mkdir()  # the folder comes back (the moved project was restored / recreated)
    layout2 = core_paths.get_layout()  # cached entry -> plants the skeleton once
    assert layout2 is layout  # the same memoized object, now ensured
    for name in core_paths.WORKSPACE_DIR_NAMES:
        assert (ws / name).is_dir()
    assert (ws / "logs").is_dir()
    assert (ws / "config").is_dir()
    # Further hits: still the same object, no exception, no re-planting.
    assert core_paths.get_layout() is layout


def test_get_layout_pointer_change_invalidates_cache(sandbox, set_pointer):
    """Every real pointer set/clear rewrites the root ``app.json`` — the new
    ``(mtime_ns, size)`` key must invalidate the memoized layout (no ``reset_*`` call)."""
    ws1 = sandbox / "one"
    ws1.mkdir()
    ws2 = sandbox / "another-longer-name"  # different length -> different root-file size too
    ws2.mkdir()
    set_pointer(str(ws1))
    first = core_paths.get_layout()
    assert first.workspace == ws1
    set_pointer(str(ws2))  # rewrites the root app.json -> a new cache key
    second = core_paths.get_layout()
    assert second.workspace == ws2
    for name in core_paths.WORKSPACE_DIR_NAMES:  # the new workspace's skeleton is planted
        assert (ws2 / name).is_dir()
    set_pointer("")  # clearing the pointer invalidates as well
    third = core_paths.get_layout()
    assert third.workspace is None
    assert core_paths.get_layout().workspace is None  # the inert hit stays inert


def test_reset_layout_cache_forces_reread(sandbox, set_pointer):
    ws = sandbox / "ws"
    ws.mkdir()
    set_pointer(str(ws))
    first = core_paths.get_layout()
    core_paths.reset_layout_cache()
    second = core_paths.get_layout()  # re-resolves from the pointer
    assert second.workspace == ws
    assert second.input == first.input


# -- predicates / invariants ---------------------------------------------------

def test_is_workspace_set_false_when_empty(sandbox, set_pointer):
    set_pointer("")
    assert not core_paths.is_workspace_set()


def test_is_workspace_set_true_when_set(sandbox, set_pointer):
    set_pointer(str(sandbox / "ws"))
    assert core_paths.is_workspace_set()


# -- resolve_parsed_json (which parsed JSON the downstream stages read) ---------

def test_resolve_parsed_json_unset_returns_placeholder(sandbox, set_pointer):
    set_pointer("")
    # No workspace -> an inert relative placeholder (read-only callers see "no script").
    assert core_paths.resolve_parsed_json() == Path("annotated_script.json")
    assert core_paths.resolve_parsed_json("ignored.json") == Path("annotated_script.json")


def test_resolve_parsed_json_named_file(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    assert core_paths.resolve_parsed_json("第一册.json") == ws / "03_parsed_json" / "第一册.json"


def test_resolve_parsed_json_most_recent_when_unnamed(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    older = d / "older.json"
    newer = d / "newer.json"
    older.write_text("[]", encoding="utf-8")
    newer.write_text("[]", encoding="utf-8")
    old_t = time.time() - 1000
    new_t = time.time()
    os.utime(older, (old_t, old_t))
    os.utime(newer, (new_t, new_t))
    # With no explicit name, the most recently written JSON wins.
    assert core_paths.resolve_parsed_json() == newer


def test_resolve_parsed_json_legacy_fallback_when_empty(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    # No JSON files yet (the dir may exist, empty) -> the legacy single-file name.
    assert core_paths.resolve_parsed_json() == ws / "03_parsed_json" / "annotated_script.json"


# -- resolve_parsed_json: orphan ``_checked.json`` files are inert ----------------
# (the retired check stages once wrote ``_checked`` copies; nothing reads them anymore)

def test_resolve_parsed_json_named_base_ignores_orphan_checked(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    (d / "第一册.json").write_text("[]", encoding="utf-8")
    (d / "第一册_checked.json").write_text("[]", encoding="utf-8")
    # An orphan _checked file never shadows the base: a named base file reads the base.
    assert core_paths.resolve_parsed_json("第一册.json") == d / "第一册.json"


def test_resolve_parsed_json_named_base_reads_base(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    (d / "第一册.json").write_text("[]", encoding="utf-8")
    assert core_paths.resolve_parsed_json("第一册.json") == d / "第一册.json"


def test_resolve_parsed_json_named_checked_not_double_suffixed(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    (d / "第一册_checked.json").write_text("[]", encoding="utf-8")
    # Asking for the _checked file directly returns it as-is (no ``_checked_checked``).
    assert core_paths.resolve_parsed_json("第一册_checked.json") == d / "第一册_checked.json"


def test_resolve_parsed_json_autopick_most_recent_base_without_checked(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    a = d / "a.json"; a.write_text("[]", encoding="utf-8")
    (d / "a_checked.json").write_text("[]", encoding="utf-8")
    b = d / "b.json"; b.write_text("[]", encoding="utf-8")
    # b is the most recent *base* file and has no checked copy -> resolves to b.
    os.utime(a, (0, 0))
    os.utime(b, (10, 10))
    assert core_paths.resolve_parsed_json() == b


def test_resolve_parsed_json_autopick_ignores_orphan_checked(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    a = d / "a.json"; a.write_text("[]", encoding="utf-8")
    ac = d / "a_checked.json"; ac.write_text("[]", encoding="utf-8")
    b = d / "b.json"; b.write_text("[]", encoding="utf-8")
    # ``a`` is the most recent base; its ``a_checked`` orphan (even though newer) is
    # inert and never shadows the base.
    os.utime(a, (10, 10))
    os.utime(ac, (20, 20))
    os.utime(b, (5, 5))
    assert core_paths.resolve_parsed_json() == a


def test_resolve_parsed_json_autopick_only_orphans_uses_legacy_fallback(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    c1 = d / "x_checked.json"; c1.write_text("[]", encoding="utf-8")
    c2 = d / "y_checked.json"; c2.write_text("[]", encoding="utf-8")
    os.utime(c1, (5, 5)); os.utime(c2, (10, 10))
    # Only orphan _checked files (no base): they are never read — the resolver falls
    # through to the legacy single-file name.
    assert core_paths.resolve_parsed_json() == d / "annotated_script.json"


# -- resolve_parsed_json_all (whole-book "all files" aggregate) -----------------

def test_resolve_parsed_json_all_unset_is_empty(sandbox, set_pointer):
    set_pointer("")
    assert core_paths.resolve_parsed_json_all() == []


def test_resolve_parsed_json_all_empty_dir_is_empty(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    assert core_paths.resolve_parsed_json_all() == []


def test_resolve_parsed_json_all_order_is_mtime_then_name(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    # Names sort a < b < c < d, but the mtimes are the reverse — so reading order
    # (mtime) yields d, c, b, a, proving mtime (not name) drives the order.
    a = d / "a.json"; a.write_text("[]", encoding="utf-8")
    b = d / "b.json"; b.write_text("[]", encoding="utf-8")
    c = d / "c.json"; c.write_text("[]", encoding="utf-8")
    (d / "c_checked.json").write_text("[]", encoding="utf-8")
    dd = d / "d.json"; dd.write_text("[]", encoding="utf-8")
    os.utime(a, (30, 30)); os.utime(b, (20, 20))
    os.utime(c, (10, 10)); os.utime(dd, (5, 5))
    # The orphan c_checked file is excluded; the base files come back in mtime order.
    assert core_paths.resolve_parsed_json_all() == [dd, c, b, a]


def test_resolve_parsed_json_all_only_orphans_is_empty(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    x = d / "x_checked.json"; x.write_text("[]", encoding="utf-8")
    y = d / "y_checked.json"; y.write_text("[]", encoding="utf-8")
    os.utime(x, (5, 5)); os.utime(y, (10, 10))
    # Orphan _checked files are never part of the aggregate.
    assert core_paths.resolve_parsed_json_all() == []


def test_resolve_parsed_json_all_ignores_non_json_and_dirs(sandbox, set_pointer):
    ws = sandbox / "ws"
    set_pointer(str(ws))
    d = ws / "03_parsed_json"
    d.mkdir(parents=True, exist_ok=True)
    good = d / "good.json"; good.write_text("[]", encoding="utf-8")
    (d / "notes.txt").write_text("not json", encoding="utf-8")
    (d / "weird.json").mkdir()  # a *directory* named *.json -> excluded by is_file()
    assert core_paths.resolve_parsed_json_all() == [good]
