"""Regression tests for the book-splitting API's output-directory policy."""
from __future__ import annotations

from backend.api.book import _clear_generated_split_files


def test_clear_generated_split_files_removes_only_splitter_outputs(tmp_path):
    output = tmp_path / "02_split_text"
    output.mkdir()
    generated = [
        "第 001 章.txt",
        "第 001 章 成人典礼.txt",
        "小说 分册01 第001章.txt",
        "小说 全书.txt",
    ]
    preserved = [
        "manual.txt",
        "自定义 第001章.txt",
    ]
    for name in generated + preserved:
        (output / name).write_text("x", encoding="utf-8")
    (output / "archive").mkdir()
    (output / "archive" / "第 001 章.txt").write_text("x", encoding="utf-8")

    _clear_generated_split_files(output)

    assert {p.name for p in output.iterdir() if p.is_file()} == set(preserved)
    assert (output / "archive" / "第 001 章.txt").exists()
