"""Tests for persistent single-track music recommendation task submission."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api import music as api_music
from backend.core import config as core_config
from backend.core import paths as core_paths


@pytest.fixture
def library(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    core_paths.MUSIC_LIBRARY_DIR.mkdir()
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text('{"paths":{"working_dir":""}}', encoding="utf-8")
    core_config.reset_config_cache()
    workspace = tmp_path / "Book"
    workspace.mkdir()
    core_config.set_workspace_pointer(str(workspace))
    core_config.update_config({"llm": {"model_name": "test-model"}})
    for name in ("one.mp3", "two.mp3"):
        (core_paths.MUSIC_LIBRARY_DIR / name).write_bytes(b"audio")
    yield core_paths.MUSIC_LIBRARY_DIR
    core_config.reset_config_cache()


def _ctx():
    return SimpleNamespace(user=SimpleNamespace(id="user-1"), session=object())


def test_batch_submits_one_durable_task_per_unique_track(library, monkeypatch):
    submitted = []
    monkeypatch.setattr(api_music, "active_durable_targets", lambda **_kwargs: set())

    def submit(**kwargs):
        submitted.append(kwargs)
        return {"id": f"task-{len(submitted)}"}

    monkeypatch.setattr(api_music, "submit_legacy_engine_task", submit)
    result = api_music.suggest_tags_batch(
        api_music.SuggestBatchReq(names=["one.mp3", "two.mp3", "one.mp3"]),
        ctx=_ctx(), db=object(),
    )

    assert result == {
        "task_ids": ["task-1", "task-2"],
        "tracks": [
            {"name": "one.mp3", "task_id": "task-1"},
            {"name": "two.mp3", "task_id": "task-2"},
        ],
    }
    assert [item["task_type"] for item in submitted] == ["music.suggest_tags"] * 2
    assert [item["payload"]["name"] for item in submitted] == ["one.mp3", "two.mp3"]
    assert all(item["payload"]["config"]["llm"]["model_name"] == "test-model" for item in submitted)


def test_batch_rejects_track_with_active_durable_task(library, monkeypatch):
    monkeypatch.setattr(api_music, "active_durable_targets", lambda **_kwargs: {"one.mp3"})
    monkeypatch.setattr(api_music, "submit_legacy_engine_task", lambda **_kwargs: pytest.fail("must not submit"))

    with pytest.raises(HTTPException) as exc:
        api_music.suggest_tags_batch(
            api_music.SuggestBatchReq(names=["one.mp3"]), ctx=_ctx(), db=object(),
        )
    assert exc.value.status_code == 409


@pytest.mark.parametrize("names,status", [([], 400), (["../one.mp3"], 400), (["missing.mp3"], 404)])
def test_batch_rejects_invalid_track_selections(library, monkeypatch, names, status):
    monkeypatch.setattr(api_music, "active_durable_targets", lambda **_kwargs: set())
    with pytest.raises(HTTPException) as exc:
        api_music.suggest_tags_batch(
            api_music.SuggestBatchReq(names=names), ctx=_ctx(), db=object(),
        )
    assert exc.value.status_code == status
