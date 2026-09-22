"""Tests for packaging completed BGM mixes into a browser-downloadable ZIP."""
from __future__ import annotations

import json
import zipfile

import pytest
from fastapi import HTTPException

from backend.api import bgm as api_bgm
from backend.core import config as core_config
from backend.core import paths as core_paths


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(
        json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8"
    )
    core_config.reset_config_cache()
    core_paths.reset_layout_cache()
    ws = tmp_path / "Book"
    ws.mkdir()
    for dirname in ("01_input", "02_split_text", "08_bgm"):
        (ws / dirname).mkdir()
    core_config.set_workspace_pointer(str(ws))
    core_paths.reset_layout_cache()
    yield ws
    core_config.reset_config_cache()
    core_paths.reset_layout_cache()


def test_package_mixed_audio_uses_source_txt_folder(workspace):
    source = workspace / "01_input" / "My Book.txt"
    formatted = workspace / "01_input" / "My Book_排版.txt"
    source.write_text("原文", encoding="utf-8")
    formatted.write_text("排版后", encoding="utf-8")
    stems = ["第 001 章", "第 002 章"]
    for stem in stems:
        (workspace / "02_split_text" / f"{stem}.txt").write_text(
            "章节", encoding="utf-8"
        )
        (workspace / "08_bgm" / f"{stem}.mp3").write_bytes(stem.encode())

    result = api_bgm.package_mixed_audio()

    assert result["base"] == "My Book"
    zip_path = workspace / "08_bgm" / "My Book" / "My Book.zip"
    assert result["zip_path"] == str(zip_path)
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == [
            "My Book/第 001 章.mp3",
            "My Book/第 002 章.mp3",
        ]
        assert archive.read("My Book/第 002 章.mp3") == "第 002 章".encode()


def test_package_mixed_audio_rejects_incomplete_chapters(workspace):
    (workspace / "01_input" / "Book.txt").write_text("原文", encoding="utf-8")
    for stem in ("第 001 章", "第 002 章"):
        (workspace / "02_split_text" / f"{stem}.txt").write_text(
            "章节", encoding="utf-8"
        )
    (workspace / "08_bgm" / "第 001 章.mp3").write_bytes(b"done")

    with pytest.raises(HTTPException, match="未完成混音"):
        api_bgm.package_mixed_audio()
