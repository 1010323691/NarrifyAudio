"""Normalize extracted chapter names, without altering source text or slices."""
from __future__ import annotations

import re

# These delimiters have no title content at the chapter-number boundary.
# Quotes/brackets, question/exclamation marks and ellipses can open a real
# title, so never include them in an indiscriminate punctuation strip.
CHAPTER_TITLE_DELIMITERS = ':：;；﹔,，﹐、·•・‧|｜/／_＿—–-'
_EDGE_WHITESPACE = '\u200b\ufeff\u2060'
_TRAILING_WHITESPACE = re.compile(rf'[\s{_EDGE_WHITESPACE}]+$')
_LEADING_DELIMITERS = re.compile(rf'^[\s{_EDGE_WHITESPACE}{re.escape(CHAPTER_TITLE_DELIMITERS)}]+')


def clean_chapter_title(title: str) -> str:
    """Remove only leading number/title delimiters and surrounding whitespace.

    Interior punctuation and meaningful title openers are left intact. The
    rule is shared by detection and old workbench-result display.
    """
    return _TRAILING_WHITESPACE.sub('', _LEADING_DELIMITERS.sub('', title))
