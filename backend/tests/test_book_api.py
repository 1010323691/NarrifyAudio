"""Regression tests for generated book output naming."""
from __future__ import annotations

from backend.engines.book import is_generated_split_output_name


def test_generated_split_output_names_match_only_known_patterns():
    generated = [
        "\u7b2c 001 \u7ae0.txt",
        "\u7b2c 001 \u7ae0 \u732b\u5496.txt",
        "\u5c0f\u8bf4 \u5206\u518c01 \u7b2c001\u7ae0.txt",
        "\u5c0f\u8bf4 \u5168\u4e66.txt",
    ]
    preserved = ["manual.txt", "\u81ea\u5b9a\u4e49 \u7b2c001\u7ae0.txt"]

    assert all(is_generated_split_output_name(name) for name in generated)
    assert not any(is_generated_split_output_name(name) for name in preserved)
