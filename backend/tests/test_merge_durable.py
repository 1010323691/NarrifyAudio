"""Tests for durable TTS merge submission and read-only package status."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api import tts as api_tts
from backend.core import config as core_config
from backend.core import paths as core_paths


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    ws.mkdir()
    core_config.set_workspace_pointer(str(ws))
    yield ws
    core_config.reset_config_cache()


def _ctx():
    return SimpleNamespace(user=SimpleNamespace(id="user-1"), session=object())


def test_merge_submits_one_durable_task_per_unique_package(workspace, monkeypatch):
    submitted = []
    def submit(**kwargs):
        submitted.append(kwargs)
        return {"batch_id": "batch", "task_ids": ["task-1", "task-2"],
                "packages": [{"package": "pkg1", "task_id": "task-1"},
                             {"package": "pkg2", "task_id": "task-2"}]}
    monkeypatch.setattr(api_tts, "submit_merge_tasks", submit)
    result = api_tts.run_merge(api_tts.MergeRequest(packages=["pkg1", "pkg2", "pkg1"]),
                               ctx=_ctx(), db=object(), idempotency_key="key")
    assert result["task_ids"] == ["task-1", "task-2"]
    assert len(submitted) == 1
    assert submitted[0]["packages"] == ["pkg1", "pkg2"]
    assert submitted[0]["idempotency_key"] == "key"


def test_merge_rejects_package_with_active_durable_task(workspace, monkeypatch):
    def reject(**kwargs):
        raise HTTPException(409, "已有合并任务在途")
    monkeypatch.setattr(api_tts, "submit_merge_tasks", reject)
    with pytest.raises(HTTPException) as exc:
        api_tts.run_merge(api_tts.MergeRequest(packages=["pkg1"]), ctx=_ctx(), db=object())
    assert exc.value.status_code == 409


@pytest.mark.parametrize("merge_request,status", [
    (api_tts.MergeRequest(), 400),
    (api_tts.MergeRequest(packages=["../escape"]), 400),
])
def test_merge_rejects_empty_or_unsafe_selections(workspace, merge_request, status):
    with pytest.raises(HTTPException) as exc:
        api_tts.run_merge(merge_request, ctx=_ctx(), db=object())
    assert exc.value.status_code == status


def _seed_package(ws, pkg: str, n_segments: int, n_ok_manifest: int, missing=()) -> None:
    """A source parsed JSON of ``n_segments`` lines + a manifest of ``n_ok_manifest``
    ok entries (files for non-missing indices created on disk)."""
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    data = [{"speaker": "NARRATOR", "text": f"台词 {i}。", "instruct": ""}
            for i in range(n_segments)]
    (ws / "03_parsed_json" / f"{pkg}.json").write_bytes(
        json.dumps(data, ensure_ascii=False).encode("utf-8"))
    pkg_dir = ws / "05_audio_chunk" / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for i in range(n_ok_manifest):
        f = pkg_dir / f"{i:04d}.mp3"
        if i not in missing:
            f.write_bytes(b"0" * 32)
        entries.append({"index": i, "speaker": "N", "text": f"t{i}",
                        "path": str(f), "ok": True, "reason": ""})
    if entries:
        (pkg_dir / "manifest.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def test_merge_status_counts_json_total(workspace):
    # total = the source parsed JSON's synthesizable segment count (the /batch-status
    # rule), not the manifest length.
    _seed_package(workspace, "p1", n_segments=10, n_ok_manifest=10)
    row = api_tts.merge_status(packages=["p1"])["packages"][0]
    assert row == {"name": "p1", "display_name": "p1", "total": 10, "completed": 10,
                   "remaining": 0, "complete": True}


def test_merge_status_partial_manifest_not_ready(workspace):
    # The key fix: a synthesis cancelled mid-way leaves a manifest shorter than the
    # source JSON — total must come from the JSON, so the package is NOT 已就绪 and a
    # half book can never be merged silently.
    _seed_package(workspace, "p1", n_segments=100, n_ok_manifest=60)
    row = api_tts.merge_status(packages=["p1"])["packages"][0]
    assert row["total"] == 100
    assert row["completed"] == 60
    assert row["remaining"] == 40
    assert row["complete"] is False


def test_merge_status_missing_source_falls_back(workspace):
    # No source JSON -> degrade to the manifest length (older projects / manual setups).
    _seed_package(workspace, "p1", n_segments=0, n_ok_manifest=3)
    row = api_tts.merge_status(packages=["p1"])["packages"][0]
    assert row["total"] == 3
    assert row["completed"] == 3
    assert row["complete"] is True


def test_merge_status_missing_segment_file_not_done(workspace):
    # completed = ok AND file still on disk (Batch.is_done): a vanished file counts as
    # remaining, so the row reports 已合成 2/3 instead of 已就绪.
    _seed_package(workspace, "p1", n_segments=3, n_ok_manifest=3, missing={2})
    row = api_tts.merge_status(packages=["p1"])["packages"][0]
    assert row["total"] == 3
    assert row["completed"] == 2
    assert row["remaining"] == 1
    assert row["complete"] is False


def test_merge_status_zero_row(workspace):
    row = api_tts.merge_status(packages=["ghost"])["packages"][0]
    assert row == {"name": "ghost", "display_name": "ghost", "total": 0, "completed": 0,
                   "remaining": 0, "complete": False}


def test_merge_status_traversal_400(workspace, monkeypatch):
    with pytest.raises(HTTPException) as e:
        api_tts.merge_status(packages=["../evil"])
    assert e.value.status_code == 400


def test_merge_status_no_workspace_zero_rows(monkeypatch, tmp_path):
    # Read-only endpoint: no workspace -> all-zero rows (degrade, never 409).
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        res = api_tts.merge_status(packages=["p1", "p2"])
        assert res == {"packages": [
            {"name": "p1", "display_name": "p1", "total": 0, "completed": 0, "remaining": 0, "complete": False},
            {"name": "p2", "display_name": "p2", "total": 0, "completed": 0, "remaining": 0, "complete": False},
        ]}
    finally:
        core_config.reset_config_cache()


def test_merge_rows_display_source_titles_without_changing_package_identity(workspace):
    stem = "第 001 章 标题_章节内标签_"
    layout = core_paths.get_or_prepare_layout()
    (layout.split_text / f"{stem}.txt").write_text(
        "书籍简介\n第1章 标题【章节内标签】！\n正文", encoding="utf-8",
    )
    _seed_package(workspace, stem, n_segments=1, n_ok_manifest=1)
    row = api_tts.merge_status(packages=[stem])["packages"][0]
    assert row["name"] == stem
    assert row["display_name"] == "第 001 章 标题【章节内标签】！"
    assert row["complete"] is True


def test_merge_cache_singleflight_and_copy(workspace, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import time
    api_tts.reset_batch_status_cache()
    layout = core_paths.resolve_layout()
    calls = []
    def compute(name, layout, voice_config, **kwargs):
        calls.append(name)
        time.sleep(.03)
        return {'name': name, 'complete': True}
    monkeypatch.setattr(api_tts, '_package_merge_status', compute)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: api_tts._cached_package_merge_status('one', layout, {}), range(8)))
    assert calls == ['one']
    rows[0]['complete'] = False
    assert api_tts._cached_package_merge_status('one', layout, {})['complete'] is True


def test_merge_cache_invalidates_inputs_and_expires(workspace, monkeypatch):
    api_tts.reset_batch_status_cache()
    clock = [0.0]
    monkeypatch.setattr(api_tts, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    _seed_package(workspace, 'p1', 2, 2)
    pkg = workspace / '05_audio_chunk' / 'p1'
    assert api_tts.merge_status(packages=['p1'])['packages'][0]['complete']
    # Directory fingerprint detects a deleted segment immediately.
    (pkg / '0001.mp3').unlink()
    assert api_tts.merge_status(packages=['p1'])['packages'][0]['completed'] == 1
    (pkg / '0001.mp3').write_bytes(b'audio')
    assert api_tts.merge_status(packages=['p1'])['packages'][0]['completed'] == 2
    source = workspace / '03_parsed_json' / 'p1.json'
    data = json.loads(source.read_text()); data.append({'speaker': 'NARRATOR', 'text': 'new'})
    source.write_text(json.dumps(data))
    assert api_tts.merge_status(packages=['p1'])['packages'][0]['total'] == 3
    # Even when a caller restores the directory fingerprint, expiry rechecks existence.
    before = api_tts._stat_key(pkg)
    stat_key = api_tts._stat_key
    monkeypatch.setattr(api_tts, "_stat_key", lambda path: before if path == pkg else stat_key(path))
    (pkg / '0001.mp3').unlink()
    assert api_tts.merge_status(packages=['p1'])['packages'][0]['completed'] == 2
    clock[0] = 2.01
    assert api_tts.merge_status(packages=['p1'])['packages'][0]['completed'] == 1
    (pkg / 'manifest.json').write_text('broken')
    assert api_tts.merge_status(packages=['p1'])['packages'][0]['completed'] == 0


def test_merge_cache_voice_title_and_workspace_fences(workspace, monkeypatch):
    from backend.core.request_context import bind_workspace, reset_workspace
    api_tts.reset_batch_status_cache()
    _seed_package(workspace, 'p1', 1, 1)
    voice = workspace / '04_voice_profiles' / 'voice_config.json'
    voice.parent.mkdir(exist_ok=True)
    assert api_tts.merge_status(packages=['p1'])['packages'][0]['complete']
    voice.write_text(json.dumps({'NARRATOR': {'voice': 'new'}}))
    # Changed voice params invalidate previously completed legacy segments.
    assert not api_tts.merge_status(packages=['p1'])['packages'][0]['complete']
    other = workspace.parent / 'Other'; other.mkdir()
    token = bind_workspace(other)
    try:
        assert api_tts.merge_status(packages=['p1'])['packages'][0]['total'] == 0
    finally:
        reset_workspace(token)


def test_merge_cache_reads_voice_config_once_per_request(workspace, monkeypatch):
    api_tts.reset_batch_status_cache()
    for name in ['one', 'two']:
        _seed_package(workspace, name, 1, 1)
    calls = []
    original = api_tts._read_voice_config
    monkeypatch.setattr(api_tts, '_read_voice_config', lambda layout: calls.append(True) or original(layout))
    api_tts.merge_status(packages=['one', 'two'])
    assert len(calls) == 1


def test_merge_cache_title_fingerprint_and_bounded_entries(workspace, monkeypatch):
    api_tts.reset_batch_status_cache()
    name = '第 001 章 标题'
    _seed_package(workspace, name, 1, 1)
    source = workspace / '02_split_text' / f'{name}.txt'
    source.parent.mkdir(exist_ok=True)
    source.write_text('第1章 原标题\n正文', encoding='utf-8')
    first = api_tts.merge_status(packages=[name])['packages'][0]
    source.write_text('第1章 新标题更长\n正文', encoding='utf-8')
    second = api_tts.merge_status(packages=[name])['packages'][0]
    assert first['display_name'] != second['display_name']
    def compute(name, layout, voice_config, **kwargs):
        return {'name': name, 'total': 0, 'completed': 0, 'remaining': 0, 'complete': False}
    monkeypatch.setattr(api_tts, '_package_merge_status', compute)
    layout = core_paths.resolve_layout()
    for i in range(2050):
        api_tts._cached_package_merge_status(f'new-{i}', layout, {})
    assert len(api_tts._MERGE_STATUS_CACHE) <= 2048
    assert len(api_tts._MERGE_INPUT_CACHE) <= 2048
    assert api_tts._MERGE_INPUT_CACHE.retained_bytes <= api_tts._MERGE_INPUT_CACHE.max_bytes
