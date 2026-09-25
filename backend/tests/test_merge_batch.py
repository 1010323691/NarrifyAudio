"""Tests for durable TTS merge submission and read-only package status."""
from __future__ import annotations

import json
from pathlib import Path
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
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
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
    monkeypatch.setattr(api_tts, "active_durable_targets", lambda **_kwargs: set())

    def submit(**kwargs):
        submitted.append(kwargs)
        return {"id": f"task-{len(submitted)}"}

    monkeypatch.setattr(api_tts, "submit_legacy_engine_task", submit)
    result = api_tts.run_merge(
        api_tts.MergeRequest(packages=["pkg1", "pkg2", "pkg1"]),
        ctx=_ctx(), db=object(),
    )

    assert result == {
        "task_ids": ["task-1", "task-2"],
        "packages": [
            {"package": "pkg1", "task_id": "task-1"},
            {"package": "pkg2", "task_id": "task-2"},
        ],
    }
    assert [item["task_type"] for item in submitted] == ["tts.merge", "tts.merge"]
    assert [item["payload"]["package"] for item in submitted] == ["pkg1", "pkg2"]


def test_merge_rejects_package_with_active_durable_task(workspace, monkeypatch):
    monkeypatch.setattr(api_tts, "active_durable_targets", lambda **_kwargs: {"pkg1"})
    monkeypatch.setattr(api_tts, "submit_legacy_engine_task", lambda **_kwargs: pytest.fail("must not submit"))

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
    assert row == {"name": "p1", "total": 10, "completed": 10,
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
    assert row == {"name": "ghost", "total": 0, "completed": 0,
                   "remaining": 0, "complete": False}


def test_merge_status_traversal_400(workspace, monkeypatch):
    with pytest.raises(HTTPException) as e:
        api_tts.merge_status(packages=["../evil"])
    assert e.value.status_code == 400


def test_merge_status_no_workspace_zero_rows(monkeypatch, tmp_path):
    # Read-only endpoint: no workspace -> all-zero rows (degrade, never 409).
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        res = api_tts.merge_status(packages=["p1", "p2"])
        assert res == {"packages": [
            {"name": "p1", "total": 0, "completed": 0, "remaining": 0, "complete": False},
            {"name": "p2", "total": 0, "completed": 0, "remaining": 0, "complete": False},
        ]}
    finally:
        core_config.reset_config_cache()
