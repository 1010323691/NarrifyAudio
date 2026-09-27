"""Book chunker core — ported (behavior-preserving) from BookChunker/chunker.js.

Pipeline: ``decode_buffer`` -> ``analyze_text`` (count + ``detect_chapters``) ->
``make_chapter_filenames`` / ``chapter_content`` (one file per chapter;
``make_whole_book_filename`` for the explicit whole-book fallback).

The JS core was written to be Python-compatible (see BookChunker/CLAUDE.md):
character count == Unicode code points excluding line breaks (matches Python
``len()`` on BMP text). Python strings are already code-point sequences (no
surrogate halves), so the counting is a direct, simpler port — a single sorted
list of line-break positions answered by binary search, exactly as the JS does.

Invariants: chapters tile the whole text; each chapter is written as exactly one
file (never split); chapters are never renumbered (the 分册NN index is a
positional sequence number, 第XXX章 carries the original number); no chapters ->
stop (only an explicit ``whole_book`` opt-in writes the single 全书 file);
concatenating all per-chapter files (or the single 全书 file) reproduces the
original exactly.
"""
from __future__ import annotations

import bisect
from collections import Counter
import hashlib
import math
import re
from pathlib import Path
from typing import Optional


# ============================ Encoding ============================

def _is_plausible_unit(cp: int) -> bool:
    """Is a code point 'plausible' as real (mostly CJK) text?

    Mirrors the JS ``isPlausibleUnit`` (which inspected UTF-16 units); in Python
    a non-BMP code point (>= 0x10000) is the equivalent of the surrogate check.
    """
    if 0x20 <= cp <= 0x7E:  # ASCII printable
        return True
    if cp in (0x09, 0x0A, 0x0D):  # tab / newlines
        return True
    if 0xA0 <= cp <= 0x24F:  # Latin-1 supplement
        return True
    if 0x3000 <= cp <= 0x303F:  # CJK punctuation
        return True
    if 0x3400 <= cp <= 0x4DBF:  # CJK Extension A
        return True
    if 0x4E00 <= cp <= 0x9FFF:  # CJK unified ideographs
        return True
    if 0xF900 <= cp <= 0xFAFF:  # CJK compatibility
        return True
    if 0xFF00 <= cp <= 0xFFEF:  # fullwidth forms
        return True
    if cp >= 0x10000:  # non-BMP
        return True
    return False


def plausible_ratio(text: str) -> float:
    n = min(len(text), 4000)
    if n == 0:
        return 0
    good = sum(1 for ch in text[:n] if _is_plausible_unit(ord(ch)))
    return good / n


def _try_decode(label: str, data: bytes) -> Optional[str]:
    try:
        return data.decode(label)
    except (UnicodeDecodeError, LookupError):
        return None


def decode_buffer(data: bytes) -> tuple[str, str]:
    """Detect the encoding of a raw buffer and decode it.

    Order: BOM -> strict UTF-8 (with a plausibility cross-check) -> GB18030 -> GBK.
    Returns ``(text, encoding_label)`` or raises ``ValueError``.
    """
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8"), "UTF-8（含 BOM）"
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le"), "UTF-16 LE（含 BOM）"
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be"), "UTF-16 BE（含 BOM）"

    # No BOM: try strict UTF-8 first.
    try:
        utf8 = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        utf8 = None

    if utf8 is not None:
        r = plausible_ratio(utf8)
        if r > 0.9:
            return utf8, "UTF-8"
        # Borderline: a GBK file can occasionally be valid UTF-8 — compare plausibility.
        gb = _try_decode("gb18030", data)
        if gb:
            r_gb = plausible_ratio(gb)
            if r_gb > r + 0.05:
                return gb, "GB18030 / GBK"
        return utf8, "UTF-8"

    # Not valid UTF-8: fall back to GB18030 (a superset of GBK).
    gb = _try_decode("gb18030", data)
    if gb and plausible_ratio(gb) > 0.5:
        return gb, "GB18030 / GBK"
    gbk = _try_decode("gbk", data)
    if gbk:
        return gbk, "GBK"

    raise ValueError("无法识别文件编码，请确认它是一份有效的文本（TXT）文件。")


# ======================= Character counting =======================

def build_newline_positions(text: str) -> list[int]:
    """Sorted positions of every line break (\\n or \\r) in the text."""
    return [i for i, ch in enumerate(text) if ch in "\r\n"]


def range_char_count(newline_positions: list[int], a: int, b: int) -> int:
    """Code points (minus line breaks) in the half-open range [a, b)."""
    if b <= a:
        return 0
    nl = newline_positions
    return (b - a) - (bisect.bisect_left(nl, b) - bisect.bisect_left(nl, a))


# ======================= Chapter detection =======================

# A chapter header line: 第<number>章 followed by a title (<= 50 chars), separated
# by space/full-width space/colon/... or attached directly. MULTILINE so ^ / $
# match at line boundaries, mirroring the JS /gm flag.
BOOK_CHAPTER_RE = re.compile(
    r"^[ \t　]*第([0-9]+|[0-9零〇一二三四五六七八九十百千两]+)章"
    r"[ \t　：:、·—\-–]*([^\r\n]{0,50})[ \t　\r]*$",
    re.MULTILINE,
)

# Some exports repeat the book/series name before each title, for example
# ``领地风云 第六十七章 魔法攻击``. Keep this as a separate pattern so the
# original strict shape remains easy to reason about.
BOOK_PREFIXED_CHAPTER_RE = re.compile(
    r"^[ \t\u3000]*(?P<prefix>[^\s\r\n][^\r\n]{0,39}?)[ \t\u3000]+"
    r"\u7b2c(?P<num>[0-9]+|[0-9\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e24]+)\u7ae0"
    r"[ \t\u3000\uff1a:\u3001\xb7\u2014\\-\u2013]*(?P<title>[^\r\n]{0,50})[ \t\u3000\r]*$",
    re.MULTILINE,
)

# A few exports concatenate the prefix/header to the previous paragraph
# without a line break. The repeated prefix is strong evidence even when the
# marker is embedded in that line, so keep the prefix as the split boundary.
BOOK_INLINE_PREFIXED_CHAPTER_RE = re.compile(
    r"(?<![\s])(?P<prefix>[^\s\r\n，。！？；：、‘’“”]{1,20})[ \t\u3000]+"
    r"\u7b2c(?P<num>[0-9]+|[0-9\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e24]+)\u7ae0"
    r"[ \t\u3000\uff1a:\u3001\xb7\u2014\\-\u2013]*(?P<title>[^\r\n]{0,50})",
    re.MULTILINE,
)

# Broader line-start forms used by Word/PDF/web exports: optional spaces,
# full-width digits, wrappers, and alternate chapter units. ``章节回集卷部篇幕场折``
# are accepted only as one dominant series (see _chapter_candidates).
BOOK_FLEX_CHAPTER_RE = re.compile(
    r"^[ \t\u3000]*(?:[☆★◆◇●○·•\-—_=~～]+[ \t\u3000]*)?"
    r"(?:[【〖〔（(「『《<\[［])?[ \t\u3000]*"
    r"\u7b2c[ \t\u3000]*(?P<num>[0-9０-９零〇○一二三四五六七八九十百千万亿两廿卅]+)"
    r"[ \t\u3000]*(?P<kind>[章节回集卷部篇幕场折])[ \t\u3000]*"
    r"(?:[】〗〕）)」』》>\]］])?[ \t\u3000：:、·\-—–]*"
    r"(?P<title>[^\r\n]{0,50})[ \t\u3000\r]*$",
    re.MULTILINE,
)

BOOK_FLEX_PREFIXED_CHAPTER_RE = re.compile(
    r"^[ \t\u3000]*(?:[☆★◆◇●○·•\-—_=~～]+[ \t\u3000]*)?"
    r"(?P<prefix>[^\s\r\n][^\r\n]{0,39}?)[ \t\u3000]+"
    r"(?:[【〖〔（(「『《<\[［])?[ \t\u3000]*\u7b2c[ \t\u3000]*"
    r"(?P<num>[0-9０-９零〇○一二三四五六七八九十百千万亿两廿卅]+)"
    r"[ \t\u3000]*(?P<kind>[章节回集卷部篇幕场折])[ \t\u3000]*"
    r"(?:[】〗〕）)」』》>\]］])?[ \t\u3000：:、·\-—–]*"
    r"(?P<title>[^\r\n]{0,50})[ \t\u3000\r]*$",
    re.MULTILINE,
)

BOOK_INLINE_FLEX_PREFIXED_CHAPTER_RE = re.compile(
    r"(?<![\s])(?P<prefix>[^\s\r\n，。！？；：、‘’“”]{1,20})"
    r"[ \t\u3000]+(?:[【〖〔（(「『《<\[［])?[ \t\u3000]*\u7b2c[ \t\u3000]*"
    r"(?P<num>[0-9０-９零〇○一二三四五六七八九十百千万亿两廿卅]+)"
    r"[ \t\u3000]*(?P<kind>[章节回集卷部篇幕场折])[ \t\u3000]*"
    r"(?:[】〗〕）)」』》>\]］])?[ \t\u3000：:、·\-—–]*(?P<title>[^\r\n]{0,50})",
    re.MULTILINE,
)

# The formatter removes internal whitespace by default. A source header such
# as ``领地风云 第一章 成人典礼`` therefore reaches the chunker as
# ``领地风云第一章成人典礼``. Keep a compact-prefix form so formatting does
# not erase the first chapter's boundary or title.
BOOK_COMPACT_PREFIXED_CHAPTER_RE = re.compile(
    r"^[ \t\u3000]*(?P<prefix>[^\s\r\n]{3,39}?)[ \t\u3000]*"
    r"\u7b2c[ \t\u3000]*(?P<num>[0-9０-９零〇○一二三四五六七八九十百千万亿两廿卅]+)"
    r"[ \t\u3000]*(?P<kind>[章节回集卷部篇幕场折])[ \t\u3000]*"
    r"(?:[】〗〕）)」』》>\]］])?[ \t\u3000：:、·.．\-—–]*"
    r"(?P<title>[^\r\n]{0,50})[ \t\u3000\r]*$",
    re.MULTILINE,
)

BOOK_INLINE_COMPACT_PREFIXED_CHAPTER_RE = re.compile(
    r"(?<![\s\r\n])"
    r"(?P<prefix>[^\s\r\n，。！？；：、‘’“”]{3,20})"
    r"\u7b2c[ \t\u3000]*(?P<num>[0-9０-９零〇○一二三四五六七八九十百千万亿两廿卅]+)"
    r"[ \t\u3000]*(?P<kind>[章节回集卷部篇幕场折])[ \t\u3000]*"
    r"(?:[】〗〕）)」』》>\]］])?[ \t\u3000：:、·.．\-—–]*"
    r"(?P<title>[^\r\n]{0,50})",
    re.MULTILINE,
)

BOOK_ENGLISH_CHAPTER_RE = re.compile(
    r"^[ \t\u3000]*(?:chapter|chap\.?|ch\.?)\s*[-_.#]?\s*"
    r"(?P<num>[0-9０-９]+)[ \t\u3000]*(?:[:：、.·\-—–]+[ \t\u3000]*|[ \t\u3000]+)"
    r"(?P<title>[^\r\n]{0,50})[ \t\u3000\r]*$",
    re.IGNORECASE | re.MULTILINE,
)

BOOK_PREFIXED_ENGLISH_CHAPTER_RE = re.compile(
    r"^[ \t\u3000]*(?P<prefix>[^\s\r\n][^\r\n]{0,39}?)[ \t\u3000]+"
    r"(?:chapter|chap\.?|ch\.?)\s*[-_.#]?\s*(?P<num>[0-9０-９]+)"
    r"[ \t\u3000]*(?:[:：、.·\-—–]+[ \t\u3000]*|[ \t\u3000]+)"
    r"(?P<title>[^\r\n]{0,50})[ \t\u3000\r]*$",
    re.IGNORECASE | re.MULTILINE,
)

BOOK_NUMBERED_ENGLISH_RE = re.compile(
    r"^[ \t\u3000]*(?:no\.?|number|#)\s*(?P<num>[0-9０-９]+)"
    r"[ \t\u3000]*(?:[:：、.·\-—–]+[ \t\u3000]*|[ \t\u3000]+)"
    r"(?P<title>[^\r\n]{1,50})[ \t\u3000\r]*$",
    re.IGNORECASE | re.MULTILINE,
)

BOOK_ENGLISH_UNIT_RE = re.compile(
    r"^[ \t\u3000]*(?:episode|ep\.?|part|section|volume|vol\.?)\s*[-_.#]?\s*"
    r"(?P<num>[0-9０-９]+)[ \t\u3000]*(?:[:：、.·\-—–]+[ \t\u3000]*|[ \t\u3000]+)"
    r"(?P<title>[^\r\n]{0,50})[ \t\u3000\r]*$",
    re.IGNORECASE | re.MULTILINE,
)

BOOK_ENGLISH_WORD_CHAPTER_RE = re.compile(
    r"^[ \t\u3000]*(?:chapter|chap\.?|ch\.?)\s+"
    r"(?P<num>zero|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
    r"eighteen|nineteen|twenty)[ \t\u3000]*(?:[:：、.·\-—–]+[ \t\u3000]*|[ \t\u3000]+)"
    r"(?P<title>[^\r\n]{0,50})[ \t\u3000\r]*$",
    re.IGNORECASE | re.MULTILINE,
)

BOOK_SUFFIX_UNIT_RE = re.compile(
    r"^[ \t\u3000]*(?:[☆★◆◇●○·•\-—_=~～]+[ \t\u3000]*)?"
    r"(?:[【〖〔（(「『《<\[［])?[ \t\u3000]*"
    r"(?P<num>[0-9０-９零〇○一二三四五六七八九十百千万亿两廿卅]+)"
    r"[ \t\u3000]*(?P<kind>[章节回集卷部篇幕场折])"
    r"(?:[】〗〕）)」』》>\]］])?[ \t\u3000：:、·.．,，\-—–_|｜/／]*"
    r"(?P<title>[^\r\n]{0,50})[ \t\u3000\r]*$",
    re.MULTILINE,
)

BOOK_BARE_NUMBER_SPACE_RE = re.compile(
    r"^[ \t\u3000]*(?:[☆★◆◇●○·•\-—_=~～]+[ \t\u3000]*)?"
    r"(?:[【〖〔（(「『《<\[［])?[ \t\u3000]*"
    r"(?P<num>[0-9０-９零〇○一二三四五六七八九十百千万亿两廿卅]+)"
    r"[ \t\u3000]{1,4}(?P<title>[^\r\n]{1,50})[ \t\u3000\r]*$",
    re.MULTILINE,
)

BOOK_VOLUME_ONLY_RE = re.compile(
    r"^[ \t\u3000]*(?:[☆★◆◇●○·•\-—_=~～]+[ \t\u3000]*)?"
    r"(?:[【〖〔（(「『《<])?[ \t\u3000]*"
    r"(?P<label>[卷部篇])[ \t\u3000]*(?P<num>[0-9０-９零〇○一二三四五六七八九十百千万亿两廿卅]+)"
    r"[ \t\u3000]*(?:[】〗〕）)」』》>\]］])?[ \t\u3000：:、·\-—–]+"
    r"(?P<title>[^\r\n]{0,50})[ \t\u3000\r]*$",
    re.MULTILINE,
)

# Weak but widespread plain-number exports. These are admitted only when the
# same separator produces at least three mostly increasing chapter numbers.
BOOK_BARE_NUMBER_CHAPTER_RE = re.compile(
    r"^[ \t\u3000]*(?:[☆★◆◇●○·•\-—_=~～]+[ \t\u3000]*)?"
    r"(?:[【〖〔（(「『《<\[［])?[ \t\u3000]*"
    r"(?P<num>[0-9０-９零〇○一二三四五六七八九十百千万亿两廿卅]+)"
    r"[ \t\u3000]*(?P<sep>[、,，.．。:：)）】〗〕」』》>\]］\-—–_|｜/／])"
    r"[ \t\u3000]*(?P<title>[^\r\n]{1,50})[ \t\u3000\r]*$",
    re.MULTILINE,
)

# Some ebook sources use a range marker such as ``第190章-第194章`` for a
# bundled block.  It is a structural range, not the title ``第194章``.  Keep
# the range as metadata so smart repair can fill the numbered chapters without
# leaking the second number into a filename.
BOOK_RANGE_HEADER_RE = re.compile(
    r"\u7b2c\s*(?P<start>[0-9\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u4ebf\u5eff\u5345\u5369]+)\s*\u7ae0"
    r"\s*[-~\uff5e\u2014\u2013\u81f3\u5230]+\s*"
    r"(?:\u7b2c\s*)?(?P<end>[0-9\u96f6\u3007\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u4ebf\u5eff\u5345\u5369]+)\s*\u7ae0"
)


def _chapter_candidates(text: str) -> list[dict]:
    """Normalize strict and repeated-prefix chapter headers.

    A prefix is accepted only when it occurs on at least two chapter-looking
    lines in the same text. This admits real exported headers while rejecting
    an isolated body sentence such as ``他说 第六章...``.
    """
    raw: list[dict] = []
    for m in BOOK_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group(1),
                "title": m.group(2) or "",
                "prefix": "",
                "kind": "章",
                "weak": False,
            }
        )
    for m in BOOK_PREFIXED_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": (m.group("prefix") or "").strip(),
                "kind": "章",
                "weak": False,
            }
        )
    for m in BOOK_INLINE_PREFIXED_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": (m.group("prefix") or "").strip(),
                "kind": "章",
                "weak": False,
            }
        )
    for m in BOOK_FLEX_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": "",
                "kind": m.group("kind"),
                "weak": False,
            }
        )
    for m in BOOK_FLEX_PREFIXED_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": (m.group("prefix") or "").strip(),
                "kind": m.group("kind"),
                "weak": False,
            }
        )
    for m in BOOK_INLINE_FLEX_PREFIXED_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": (m.group("prefix") or "").strip(),
                "kind": m.group("kind"),
                "weak": False,
            }
        )
    for m in BOOK_COMPACT_PREFIXED_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": (m.group("prefix") or "").strip(),
                "kind": m.group("kind"),
                "weak": False,
            }
        )
    for m in BOOK_INLINE_COMPACT_PREFIXED_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": (m.group("prefix") or "").strip(),
                "kind": m.group("kind"),
                "weak": False,
            }
        )
    for m in BOOK_ENGLISH_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": "",
                "kind": "章",
                "weak": False,
            }
        )
    for m in BOOK_PREFIXED_ENGLISH_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": (m.group("prefix") or "").strip(),
                "kind": "章",
                "weak": False,
            }
        )
    for m in BOOK_NUMBERED_ENGLISH_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": "",
                "kind": "章",
                "weak": False,
            }
        )
    for m in BOOK_ENGLISH_UNIT_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": "",
                "kind": "章",
                "weak": False,
            }
        )
    english_number_words = {
        "zero": 0,
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
    }
    for m in BOOK_ENGLISH_WORD_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": str(english_number_words[m.group("num").lower()]),
                "title": m.group("title") or "",
                "prefix": "",
                "kind": "章",
                "weak": False,
            }
        )
    for m in BOOK_SUFFIX_UNIT_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": "",
                "kind": m.group("kind"),
                "weak": True,
                "separator": m.group("kind"),
            }
        )
    for m in BOOK_VOLUME_ONLY_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": "",
                "kind": m.group("label"),
                "weak": False,
            }
        )
    for m in BOOK_BARE_NUMBER_CHAPTER_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": "",
                "kind": "章",
                "weak": True,
                "separator": m.group("sep"),
            }
        )
    for m in BOOK_BARE_NUMBER_SPACE_RE.finditer(text):
        raw.append(
            {
                "index": m.start(),
                "numStr": m.group("num"),
                "title": m.group("title") or "",
                "prefix": "",
                "kind": "章",
                "weak": True,
                "separator": "space",
            }
        )

    # Prefer explicit 章 markers when a file mixes chapter and sub-section
    # markers. If there is no 章 series, the most frequent alternate unit wins.
    explicit = [c for c in raw if not c["weak"]]
    kind_counts = Counter(c["kind"] for c in explicit)
    if "章" in kind_counts and kind_counts["章"] >= 2:
        raw = [c for c in raw if c["weak"] or c["kind"] == "章"]
    elif kind_counts:
        dominant = kind_counts.most_common(1)[0][0]
        raw = [c for c in raw if c["weak"] or c["kind"] == dominant]

    bare_groups: dict[str, list[dict]] = {"bare": []}
    for c in raw:
        if c["weak"]:
            bare_groups["bare"].append(c)
    valid_bare: set[int] = set()
    for group in bare_groups.values():
        numbers = [parse_chapter_number(c["numStr"]) for c in group]
        parsed = [n for n in numbers if n is not None]
        rises = sum(b > a for a, b in zip(parsed, parsed[1:]))
        if len(parsed) >= 3 and rises >= 2 and all(b >= a for a, b in zip(parsed, parsed[1:])):
            valid_bare.update(id(c) for c in group)
    raw = [c for c in raw if not c["weak"] or id(c) in valid_bare]
    # Count physical header lines, not regex matches. Several tolerant
    # patterns intentionally recognize the same line, and counting those
    # duplicate matches would incorrectly validate an isolated body prefix.
    prefix_positions: dict[str, set[int]] = {}
    for c in raw:
        if c["prefix"]:
            prefix_positions.setdefault(c["prefix"], set()).add(c["index"])
    candidates = [
        c
        for c in raw
        if not c["prefix"] or len(prefix_positions[c["prefix"]]) >= 2
    ]
    candidates.sort(key=lambda c: c["index"])

    # A common export writes both ``书名 第N章`` and a bare ``第N章`` on the
    # next whitespace-only line. They are one boundary, not two chapters.
    # Do this after prefix validation so a real second occurrence of the same
    # number, separated by body text, is still reported as a duplicate.
    deduped: list[dict] = []
    for candidate in candidates:
        if deduped:
            previous = deduped[-1]
            previous_line = text.rfind("\n", 0, previous["index"])
            candidate_line = text.rfind("\n", 0, candidate["index"])
            same_number = candidate["numStr"] == previous["numStr"]
            if not same_number:
                candidate_num = parse_chapter_number(candidate["numStr"])
                previous_num = parse_chapter_number(previous["numStr"])
                same_number = (
                    candidate_num is not None
                    and candidate_num == previous_num
                )
            if (
                same_number
                and candidate_line == previous_line
                and candidate["index"] - previous["index"] <= 50
            ):
                # The inline pattern can also match a suffix of a normal
                # prefixed header; one physical line must yield one boundary.
                continue
            if (
                not candidate["prefix"]
                and previous["prefix"]
                and same_number
            ):
                line_end = text.find("\n", previous["index"])
                if line_end >= 0 and text[line_end + 1 : candidate["index"]].strip() == "":
                    continue
        deduped.append(candidate)

    # Normalize range headers after all regex variants have been de-duplicated
    # so a line such as ``第190章-第194章`` becomes one boundary with an empty
    # title plus ``range_end=194``.
    for candidate in deduped:
        line_start = text.rfind("\n", 0, candidate["index"]) + 1
        line_end = text.find("\n", candidate["index"])
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        match = BOOK_RANGE_HEADER_RE.search(line)
        if not match:
            continue
        start = parse_chapter_number(match.group("start"))
        end = parse_chapter_number(match.group("end"))
        if start is not None and end is not None and start == parse_chapter_number(candidate["numStr"]) and end > start:
            candidate["range_end"] = end
            candidate["title"] = ""
    return deduped


# Main chapter-header shape plus tolerant export variants. Kept next to the
# detector so the user-facing analyze response describes the actual rule.
EXPECTED_CHAPTER_FORMAT = (
    "第N章（N 为阿拉伯数字或中文数字；兼容空格、全角数字、括号、书名/系列前缀、卷/回/节等单位、"
    "Chapter N/Part N，以及连续递增的 1、标题 格式）"
)


def detect_chapters(text: str) -> list[dict]:
    """Return contiguous chapter ranges. Chapters tile the whole text: chapter i
    spans ``[start_i, start_{i+1})``; the first starts at 0, the last runs to EOF."""
    found = []
    for m in _chapter_candidates(text):
        title = m["title"]
        title = re.sub(r"[ \t　]+$", "", title) if title else ""
        found.append(
            {
                "index": m["index"],
                "numStr": m["numStr"],
                "title": title,
                **({"range_end": m["range_end"]} if "range_end" in m else {}),
            }
        )

    kept = filter_spurious_chapters(found)

    chapters = []
    for i, k in enumerate(kept):
        start = 0 if i == 0 else k["index"]
        end = kept[i + 1]["index"] if i < len(kept) - 1 else len(text)
        chapters.append(
            {
                "seq": i + 1,
                "start": start,
                "end": end,
                "numStr": k["numStr"],
                "num": parse_chapter_number(k["numStr"]),
                "title": k["title"],
                **({"range_end": k["range_end"]} if "range_end" in k else {}),
                "chars": 0,
            }
        )
    return chapters


def filter_spurious_chapters(found: list[dict]) -> list[dict]:
    """Drop candidates that are isolated dips in the number sequence of their
    neighbours; keeps legitimate renumbering and boundary chapters intact."""
    n = len(found)
    if n < 3:
        return found
    keep = [True] * n
    for i in range(1, n - 1):
        prev = parse_chapter_number(found[i - 1]["numStr"])
        cur = parse_chapter_number(found[i]["numStr"])
        nxt = parse_chapter_number(found[i + 1]["numStr"])
        if prev is None or cur is None or nxt is None:
            continue
        if cur <= prev and nxt > cur and nxt >= prev:  # isolated dip
            keep[i] = False
    return [f for i, f in enumerate(found) if keep[i]]


# ======================= Chapter-number checks =======================

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def parse_cn_number(s: str) -> Optional[int]:
    """Chinese numeral -> int (一…九, 十/百/千, 零/〇, 两; e.g. 一千零一=1001)."""
    if not s:
        return None
    digits = dict(_CN_DIGITS)
    digits.update({"\u3007": 0, "\u25cb": 0})
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if s.isdigit() or all(ch in digits for ch in s):
        if s.isdigit():
            value = int(s)
        else:
            value = int("".join(str(digits[ch]) for ch in s))
        return value if value > 0 else None

    units = dict(_CN_UNITS)
    units.update({"\u4e07": 10_000, "\u4ebf": 100_000_000})
    special = {"\u5eff": 20, "\u5345": 30, "\u5369": 40}
    if s in special:
        return special[s]

    total = 0
    section = 0
    number = 0
    for ch in s:
        if ch in digits:
            number = digits[ch]
            continue
        if ch not in units:
            return None
        unit = units[ch]
        if unit < 10_000:
            if number == 0:
                if unit == 10 and total == 0 and section == 0:
                    number = 1
                else:
                    return None
            section += number * unit
            number = 0
        else:
            section += number
            total += section * unit
            section = 0
            number = 0
    value = total + section + number
    return value if value > 0 else None


def parse_chapter_number(num_str: Optional[str]) -> Optional[int]:
    if not num_str:
        return None
    if re.fullmatch(r"[0-9]+", num_str):
        return int(num_str)
    return parse_cn_number(num_str)


def check_chapter_sequence(chapters: list[dict]) -> dict:
    """Report gaps / duplicates / disorder in the detected numbers. Informational
    only — it never changes how the text is split."""
    report = {
        "count": len(chapters),
        "parseable": 0,
        "unparseable": 0,
        "first": None,
        "last": None,
        "gaps": [],
        "duplicates": [],
        "disorder": [],
        "hasIssues": False,
    }
    prev = None
    seen: set[int] = set()
    for c in chapters:
        num = parse_chapter_number(c["numStr"])
        if num is None:
            report["unparseable"] += 1
            continue
        report["parseable"] += 1
        if report["first"] is None:
            report["first"] = num
        report["last"] = num
        if num in seen:
            report["duplicates"].append({"seq": c["seq"], "num": num})
        else:
            seen.add(num)
        if prev is not None:
            if num < prev:
                report["disorder"].append({"seq": c["seq"], "num": num, "prevNum": prev})
            elif num > prev + 1:
                report["gaps"].append({"after": prev, "missing": list(range(prev + 1, num))})
        prev = num
    report["hasIssues"] = bool(report["gaps"] or report["duplicates"] or report["disorder"])
    return report


# ======================= Filenames =======================

def chapter_number(ch: dict):
    """Parsed int when available, else the raw number string (never renumber)."""
    return ch["num"] if ch["num"] is not None else ch["numStr"]


def pad_chapter_number(n, w: int) -> str:
    return str(n).zfill(w) if isinstance(n, int) else str(n)


def chapter_number_width(chapters: list[dict]) -> int:
    max_n = 0
    for c in chapters:
        n = c["num"]
        if n is not None and n > max_n:
            max_n = n
    return max(3, len(str(max_n)))


def base_name(file_name: str) -> str:
    b = file_name.replace("\\", "/").split("/")[-1]
    dot = b.rfind(".")
    if dot > 0:
        b = b[:dot]
    return b or file_name


def sanitize_file_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def make_chapter_filenames(base: str, chapters: list[dict]) -> list[str]:
    """``<base> 分册NN 第XXX章.txt`` — one file per chapter. ``NN`` is the 1-based
    sequential volume index (width max(2, digits of the chapter count); it also
    disambiguates duplicated chapter numbers); ``XXX`` is the chapter's ORIGINAL
    number (width max(3, digits of the largest number), never renumbered); an
    unparseable number keeps its raw ``numStr`` unpadded."""
    chap_width = chapter_number_width(chapters)
    nn_width = max(2, len(str(len(chapters))))
    out = []
    for i, ch in enumerate(chapters):
        name = (
            f"{base} 分册{str(i + 1).zfill(nn_width)} "
            f"第{pad_chapter_number(chapter_number(ch), chap_width)}章.txt"
        )
        out.append(sanitize_file_name(name))
    return out


def make_whole_book_filename(base: str) -> str:
    """Whole-book fallback name (zero chapters detected, user chose to continue):
    ``<base> 全书.txt`` (sanitized like chapter names)."""
    return sanitize_file_name(f"{base} 全书.txt")


# ======================= Analysis =======================

def analyze_text(text: str) -> dict:
    """Decoded text -> ``{text, newline_positions, chapters, totalChars}``."""
    newline_positions = build_newline_positions(text)

    def char_count(a: int, b: int) -> int:
        return range_char_count(newline_positions, a, b)

    chapters = detect_chapters(text)
    for c in chapters:
        c["chars"] = char_count(c["start"], c["end"])
    total_chars = char_count(0, len(text))
    return {
        "text": text,
        "newline_positions": newline_positions,
        "chapters": chapters,
        "totalChars": total_chars,
    }


def chapter_content(analysis: dict, chapter: dict) -> str:
    """One chapter's content = one slice of the original text. Concatenating the
    per-chapter slices in order reproduces the original exactly."""
    return analysis["text"][chapter["start"]: chapter["end"]]


# ======================= Smart repair (智能识别) =======================
#
# Mechanical chapter-structure repair for unproofed novels. Physical order is
# the ground truth; chapter numbers are data to be corrected. Abnormally long
# chapters (suspected of swallowing missing chapters) are split at inferred
# length positions snapped to paragraph boundaries — a gap-filling internal
# title line can never be found by rescan (the top-level spurious filter only
# drops isolated dips, which never fill a gap), so internal title-like lines
# are recorded as evidence only. Duplicate-number chapters with identical
# content are dropped (copy-paste error); all others are kept. The final
# structure is renumbered 1..N in physical order.
#
# This stage ALWAYS produces output (never blocks): every inferred action is
# reported with a confidence level for manual review. It never rewrites file
# content (title lines keep their original numbers) and never mutates inputs.

# chars >= median * LONG_CHAPTER_RATIO -> "abnormally long" (suspected merged
# chapters). A chapter that swallowed ONE missing chapter is ~2x the median,
# so the threshold is 2.0 to flag that primary case; a false positive costs at
# most one reported, low-confidence split when a gap exists (when no gap
# exists — consecutive/last chapter — the long chapter is simply kept
# SILENTLY: older novels legitimately have long chapters and a length flag
# alone is not an anomaly, 2026-09 user refinement) — the user requires
# always-split-and-report for missing numbers, never block.
LONG_CHAPTER_RATIO = 2.0
# Length-anomaly detection needs a stable baseline: fewer chapters than this,
# or a median below MIN_BASELINE_CHARS, disables it (short books / test books).
MIN_LENGTH_SAMPLES = 5
MIN_BASELINE_CHARS = 200
# Inferred split cut points search for the nearest paragraph boundary within
# +/- this fraction of the chapter's per-segment raw span.
INFER_SNAP_TOLERANCE = 0.5
# Smart filename number width: N <= 1000 -> 3 digits (001...), else 4 (0001...).
SMART_FILENAME_WIDTH_THRESHOLD = 1000
# Exact gap inference is only trustworthy for a small missing-number interval.
# Larger gaps use the paragraph-only mechanical fallback above, with an
# observed average target and an explicit low-confidence warning.
MAX_INFERRED_MISSING_CHAPTERS = 20
# If a book has no usable normal chapter average (for example, only one very
# long detected chapter), use this conservative target and still cut only at
# paragraph boundaries. The value is a fallback, never preferred over the
# observed average.
DEFAULT_MECHANICAL_CHAPTER_CHARS = 2_000


def _line_start_of(text: str, pos: int) -> int:
    """Index of the start of the line containing ``pos`` (skips back over the
    \\r\\n sequence preceding it)."""
    return max(text.rfind("\n", 0, pos), text.rfind("\r", 0, pos)) + 1


def _normalized_fingerprint(content: str) -> str:
    """sha256 of the content with ALL whitespace removed (Python ``\\s`` on str
    is Unicode-aware, so 　/\\r/\\n/\\t all count). Binary same/different test
    only — no similarity thresholds."""
    return hashlib.sha256(re.sub(r"\s+", "", content).encode("utf-8")).hexdigest()


def _format_number_range(numbers: list[int]) -> str:
    """Render sorted chapter numbers compactly for human-facing warnings."""
    if not numbers:
        return ""
    ranges: list[str] = []
    start = prev = numbers[0]
    for number in numbers[1:]:
        if number == prev + 1:
            prev = number
            continue
        ranges.append(str(start) if start == prev else f"{start}–{prev}")
        start = prev = number
    ranges.append(str(start) if start == prev else f"{start}–{prev}")
    return "、".join(ranges)


def _index_at_char_count(text: str, nl: list[int], start: int, n_chars: int) -> int:
    """Smallest raw index >= ``start`` whose range char count (line breaks
    excluded) reaches ``n_chars``."""
    if n_chars <= 0:
        return start
    lo, hi = start, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if range_char_count(nl, start, mid) >= n_chars:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _paragraph_boundaries(text: str, a: int, b: int) -> list[int]:
    """Cut points p in [a, b) where text[p] starts a paragraph gap (``\\n\\n`` or
    ``\\r\\n\\r\\n``). Cutting at p keeps every paragraph intact on one side."""
    return [p for p in range(a, b) if text.startswith("\n\n", p) or text.startswith("\r\n\r\n", p)]


def _internal_title_scan(text: str, ch: dict, own_header_line: int) -> list[dict]:
    """Rescan a chapter slice for chapter-title lines (same BOOK_CHAPTER_RE),
    skipping the chapter's own header line. Note the only lines that end up
    INSIDE a detected chapter's slice are the ones ``filter_spurious_chapters``
    dropped at the top level (isolated number dips: number <= previous
    candidate) — a line whose number fills the gap is never dropped, so an
    exact gap-filling title can never be found here; these candidates are
    evidence for the report (suspected copy-paste / renumbering residue), and
    the split itself is always inferred from length. No spurious-filter here —
    it is a whole-sequence heuristic that misfires on a single chapter slice.
    Returns candidates with absolute positions, sorted by position."""
    body = text[ch["start"]: ch["end"]]
    out = []
    for m in _chapter_candidates(body):
        abs_pos = ch["start"] + m["index"]
        if _line_start_of(text, abs_pos) == own_header_line:
            continue  # the chapter's own header line
        if parse_chapter_number(m["numStr"]) == ch.get("num"):
            own_line_end = text.find("\n", own_header_line)
            if own_line_end >= 0 and text[own_line_end + 1 : abs_pos].strip() == "":
                # Some exporters repeat a prefixed header and a bare header
                # on adjacent lines. The bare line is a mirror, not a second
                # copy of the chapter body.
                continue
        title = m["title"]
        title = re.sub(r"[ \t　]+$", "", title) if title else ""
        out.append(
            {
                "line_start": _line_start_of(text, abs_pos),
                "numStr": m["numStr"],
                "num": parse_chapter_number(m["numStr"]),
                "title": title,
            }
        )
    out.sort(key=lambda x: x["line_start"])
    return out


def _inferred_cut_points(
    text: str, nl: list[int], ch: dict, k: int, baseline: float
) -> tuple[list[int], bool]:
    """k inferred cut positions (raw indices) inside ``ch`` for splitting it
    into k+1 segments. The model: missing chapters' content was appended
    after the original chapter, so each cut sits ~``baseline`` chars before the
    next. Every cut is snapped to the nearest paragraph boundary (within
    +/- INFER_SNAP_TOLERANCE of the per-segment span, else the nearest one in
    the chapter, else the exact position) and kept strictly monotone. Returns
    (cuts, used_exact) — used_exact means at least one cut had no blank line
    to snap to (the cut is mid-paragraph, flagged by the caller)."""
    start, end = ch["start"], ch["end"]
    seg_span = (end - start) / (k + 1)
    # The trailing \n\n (separator before the next chapter header) is a
    # paragraph boundary at the very end of the slice — snapping a cut there
    # would leave a char-empty final segment, so it (and any boundary with no
    # non-newline content left of the chapter end) is excluded.
    boundaries = [
        b for b in _paragraph_boundaries(text, start, end)
        if range_char_count(nl, b, end) > 0
    ]
    cuts: list[int] = []
    used_exact = False
    prev = start
    for m in range(1, k + 1):
        t = _index_at_char_count(text, nl, start, int(baseline * m))
        t = max(t, prev + 1)
        lo = int(t - INFER_SNAP_TOLERANCE * seg_span)
        hi = int(t + INFER_SNAP_TOLERANCE * seg_span)
        window = [b for b in boundaries if lo <= b <= hi and prev < b < end]
        if window:
            pick = min(window, key=lambda b: (abs(b - t), b))
        else:
            allc = [b for b in boundaries if prev < b < end]
            if allc:
                pick = min(allc, key=lambda b: (abs(b - t), b))
            else:
                pick = min(t, end - 1)
                used_exact = True
        if pick <= prev:
            continue  # cannot keep monotone -> drop this cut (fewer segments)
        if (
            range_char_count(nl, prev, pick) <= 0
            or range_char_count(nl, pick, end) <= 0
        ):
            continue  # never create an empty tail segment near EOF
        cuts.append(pick)
        prev = pick
    return cuts, used_exact


def _mechanical_cut_points(
    text: str, nl: list[int], ch: dict, target_chars: float
) -> tuple[list[int], int]:
    """Split an anomalously long chapter near ``target_chars`` per segment.

    Unlike ``_inferred_cut_points`` this helper never falls back to an
    intra-paragraph index. If the text has fewer paragraph boundaries than the
    estimated segment count, it uses every available boundary and returns the
    smaller safe segment count.
    """
    if target_chars <= 0 or ch["chars"] <= target_chars:
        return [], 1
    boundaries = [
        b
        for b in _paragraph_boundaries(text, ch["start"], ch["end"])
        if range_char_count(nl, b, ch["end"]) > 0
    ]
    desired_segments = max(2, int(math.ceil(ch["chars"] / target_chars)))
    safe_segments = min(desired_segments, len(boundaries) + 1)
    if safe_segments <= 1:
        return [], 1
    cuts, _ = _inferred_cut_points(
        text,
        nl,
        ch,
        safe_segments - 1,
        target_chars,
    )
    return cuts, len(cuts) + 1


def smart_repair(text: str, chapters: list[dict]) -> dict:
    """Mechanically repair the chapter structure of ``chapters`` (as produced
    by ``analyze_text``) and renumber 1..N in physical order. Pure function:
    inputs are not mutated.

    Returns ``{status: "ok"|"clean", chapters (with start/end/final_num/
    repair), final_numbers, report {actions, warnings, removed},
    baseline_chars, original_count}``. A self-check (lossless concatenation,
    tiling, non-empty segments, unique 1..N numbering) runs last; on failure
    ``status="error"`` with a diagnostic ``error`` message and no output."""
    warnings: list[dict] = []
    removed: list[dict] = []
    removed_spans: list[tuple[int, int]] = []  # raw ranges deleted from the text
    if not chapters:
        return {
            "status": "ok",
            "chapters": [],
            "final_numbers": [],
            "report": {"actions": [], "warnings": [], "removed": []},
            "baseline_chars": None,
            "original_count": 0,
        }

    nl = build_newline_positions(text)

    def char_count(a: int, b: int) -> int:
        return range_char_count(nl, a, b)

    work = [dict(c) for c in chapters]
    for c in work:
        c["chars"] = char_count(c["start"], c["end"])

    # -- step 1: baseline (median) + abnormally-long flags -----------------
    baseline: Optional[float] = None
    if len(work) >= MIN_LENGTH_SAMPLES:
        s = sorted(c["chars"] for c in work)
        mid = len(s) // 2
        med = float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
        if med >= MIN_BASELINE_CHARS:
            baseline = med
    if baseline is not None:
        for c in work:
            c["_long"] = c["chars"] >= baseline * LONG_CHAPTER_RATIO
    else:
        for c in work:
            c["_long"] = False
        reason = (
            "章节数少于 5 章，" if len(work) < MIN_LENGTH_SAMPLES else "章节字数中位数过低（<200 字），"
        )
        warnings.append(
            {"type": "length_disabled", "detail": reason + "长度异常检测已禁用"}
        )

    # Prefer the observed average of normal chapters. When the median-based
    # detector is unavailable, use the shorter half so one merged long chapter
    # cannot distort the target; a genuinely single-chapter book gets the
    # explicit fallback target.
    if baseline is not None:
        reference_lengths = [c["chars"] for c in work if not c["_long"] and c["chars"] > 0]
    else:
        lengths = sorted(c["chars"] for c in work if c["chars"] > 0)
        reference_lengths = lengths[: max(1, (len(lengths) + 1) // 2)]
    chapter_average = (
        sum(reference_lengths) / len(reference_lengths)
        if reference_lengths
        else None
    )
    # A range header describes several bundled chapters.  Its block length
    # must be normalized by the number of named chapters before it is used as
    # a per-chapter target, otherwise the whole range looks like one chapter.
    range_reference_lengths = []
    range_reference_total = 0
    range_reference_count = 0
    for c in work:
        range_end = c.get("range_end")
        own = c.get("num")
        if range_end is not None and own is not None and range_end > own:
            count = range_end - own + 1
            range_reference_lengths.append(c["chars"] / count)
            range_reference_total += c["chars"]
            range_reference_count += count
    if range_reference_lengths:
        chapter_average = (
            sum(reference_lengths) + range_reference_total
        ) / (
            len(reference_lengths) + range_reference_count
        )
    if len(work) == 1 and work[0]["chars"] > DEFAULT_MECHANICAL_CHAPTER_CHARS:
        chapter_average = float(DEFAULT_MECHANICAL_CHAPTER_CHARS)
        work[0]["_long"] = True

    # Each chapter's own header line (chapter 1's slice starts at 0 and
    # contains the preamble, so its header is the first top-level match —
    # filter_spurious_chapters never removes the first candidate).
    first_candidate = next(iter(_chapter_candidates(text)), None)
    own_header_lines = []
    for i, c in enumerate(work):
        # Chapter 1's slice starts at 0 and contains the preamble, so its
        # header is the first top-level match (filter_spurious_chapters never
        # removes the first candidate). Every other chapter's slice begins at
        # its own header line.
        own_header_lines.append(
            _line_start_of(text, first_candidate["index"])
            if i == 0 and first_candidate
            else _line_start_of(text, c["start"])
        )

    # -- steps 2+3: handle absorbed duplicates + abnormally long chapters --
    # The rescan can only find the isolated dips filter_spurious_chapters
    # dropped (number <= previous candidate) — a gap-filling number is never
    # dropped, so an exact gap-filling title can never appear inside a slice.
    # The two forms of findable lines:
    #   * same number as its own chapter = absorbed duplicate (the spurious
    #     filter dropped the second copy, which this chapter swallowed):
    #     fingerprint head vs tail — truncate if identical, else keep both
    #     as a two-chapter split. Runs on EVERY parseable chapter, not only
    #     long ones: a chapter that swallowed one copy is ~2x the baseline,
    #     below the 3x "long" threshold;
    #   * any other number = evidence only; the gap split itself is always
    #     inferred from length (gated on _long).
    inferred_splits: dict[int, list[int]] = {}  # work index -> raw cut points
    split_nums: dict[int, list[tuple[str, Optional[int], str]]] = {}  # work index -> per-segment (numStr, num, title)
    for i, c in enumerate(work):
        own = c["num"]
        nxt = work[i + 1]["num"] if i + 1 < len(work) else None
        range_end = c.get("range_end")
        if own is not None and range_end is not None and range_end > own:
            # Range markers are authoritative for the numbers they name. A
            # real header inside the range remains a hard boundary; only the
            # missing numbers before that header are inferred here.
            target_end = range_end
            if nxt is not None and own < nxt <= range_end:
                target_end = nxt - 1
            target_count = target_end - own + 1
            if target_count > 1:
                cuts, used_exact = _inferred_cut_points(
                    text,
                    nl,
                    c,
                    target_count - 1,
                    c["chars"] / target_count,
                )
                if len(cuts) == target_count - 1:
                    inferred_splits[i] = cuts
                    split_nums[i] = [
                        (str(number), number, "")
                        for number in range(own, target_end + 1)
                    ]
                    c["_range_split"] = True
                    warnings.append(
                        {
                            "type": "range_header_split",
                            "detail": (
                                f"第{own}章至第{target_end}章的范围标题已按段落边界补齐，"
                                "保留范围内已识别的真实章节标题"
                            ),
                        }
                    )
                    if used_exact:
                        warnings.append(
                            {
                                "type": "range_header_split_mid_paragraph",
                                "detail": (
                                    f"第{own}章至第{target_end}章范围内没有足够段落边界，"
                                    "部分切点落在段落中间，请人工核对"
                                ),
                            }
                        )
                else:
                    warnings.append(
                        {
                            "type": "range_header_split_skipped",
                            "detail": (
                                f"第{own}章至第{target_end}章范围内段落边界不足，"
                                "未强行切断段落"
                            ),
                        }
                    )
            continue
        if own is None and c["_long"]:
            warnings.append(
                {
                    "type": "long_kept",
                    "detail": (
                        f"第{c['numStr']}章异常长（{c['chars']}字）且章号无法解析，"
                        "无法校验内部结构，原样保留"
                    ),
                }
            )
            continue
        if own is not None:
            cands = _internal_title_scan(text, c, own_header_lines[i])
            dup_cands = [x for x in cands if x["num"] == own]
            if dup_cands:
                # Absorbed duplicate: its own copy dropped by the spurious
                # filter and swallowed into this chapter (appended at the tail).
                last = dup_cands[-1]
                head_fp = _normalized_fingerprint(text[c["start"]: last["line_start"]])
                tail_fp = _normalized_fingerprint(text[last["line_start"]: c["end"]])
                if head_fp == tail_fp:
                    # Identical content twice -> drop the duplicate (keep the first).
                    old_end = c["end"]
                    c["end"] = last["line_start"]
                    removed_spans.append((last["line_start"], old_end))
                    c["chars"] = char_count(c["start"], c["end"])
                    c["_truncated"] = True
                    removed.append(
                        {
                            "seq": c["seq"],
                            "num": own,
                            "numStr": last["numStr"],
                            "title": last["title"],
                            "kind": "truncated",
                        }
                    )
                    warnings.append(
                        {
                            "type": "duplicate_truncated",
                            "detail": (
                                f"第{own}章出现两次且正文完全相同（疑似复制错误），已删除重复的一份"
                            ),
                        }
                    )
                else:
                    # Different content -> keep both: split at the duplicated line.
                    inferred_splits[i] = [last["line_start"]]
                    split_nums[i] = [
                        (c["numStr"], c["num"], c["title"]),
                        (last["numStr"], last["num"], last["title"]),
                    ]
                    c["_dup_split"] = True
                    warnings.append(
                        {
                            "type": "duplicate_split_kept",
                            "detail": (
                                f"第{own}章出现两次且正文不同，两份均保留并重新编号，请人工核对"
                            ),
                        }
                    )
                # A gap exists only when nxt > own + 1 (own < nxt also holds
                # for the consecutive case, which must NOT warn).
                if nxt is not None and nxt > own + 1:
                    gap_names = _format_number_range(list(range(own + 1, nxt)))
                    warnings.append(
                        {
                            "type": "gap_after_duplicate",
                            "detail": (
                                f"重复的第{own}章之后存在缺号区间（第{gap_names}章），"
                                "已由重编号吸收，请人工核对"
                            ),
                        }
                    )
                continue
            if not c["_long"]:
                continue
            gap = list(range(own + 1, nxt)) if nxt is not None and nxt > own + 1 else []
            needs_mechanical_split = (
                chapter_average is not None
                and (nxt is None or len(gap) > MAX_INFERRED_MISSING_CHAPTERS)
            )
            if needs_mechanical_split:
                cuts, segment_count = _mechanical_cut_points(
                    text, nl, c, chapter_average
                )
                if cuts:
                    inferred_splits[i] = cuts
                    split_nums[i] = [
                        (c["numStr"], c["num"], c["title"])
                    ] + [
                        (str(own + j), own + j, "")
                        for j in range(1, segment_count)
                    ]
                    c["_mechanical"] = True
                    suffix = (
                        f"与下一章之间缺少第{_format_number_range(gap)}章"
                        if gap
                        else "后续未检测到章节"
                    )
                    warnings.append(
                        {
                            "type": "mechanical_split",
                            "detail": (
                                f"第{c['num']}章（{c['chars']}字）{suffix}，"
                                f"按已识别正常章节平均 {round(chapter_average)} 字"
                                f"在段落边界机械拆分为 {segment_count} 段，"
                                "未切断任何段落，请人工核对"
                            ),
                        }
                    )
                    continue
                warnings.append(
                    {
                        "type": "mechanical_split_skipped",
                        "detail": (
                            f"第{c['num']}章过长但没有足够的段落边界，"
                            "无法在不切断段落的前提下机械拆分，已保留原章"
                        ),
                    }
                )
                if nxt is None or gap:
                    # Do not fall through to the old exact-length inference
                    # for a large gap: that path may cut inside a paragraph.
                    continue
            # No missing-number interval to fill (last chapter, or the next
            # number is consecutive / out-of-order / duplicate): there is no
            # evidence of a swallowed chapter — an abnormally long chapter is
            # just a long chapter (older novels legitimately have long
            # chapters). Kept as-is SILENTLY (no warning): the user requires
            # alerts only when a gap could not be resolved. (An unparseable
            # number still warns above, since that anomaly is invisible
            # elsewhere in the report.)
            if nxt is None or nxt <= own + 1:
                continue
            gap = list(range(own + 1, nxt))
            gap_names = _format_number_range(gap)
            if cands:
                warnings.append(
                    {
                        "type": "internal_title_evidence",
                        "detail": (
                            f"第{c['num']}章内部发现 {len(cands)} 行似章节标题行（"
                            + "、".join(f"第{x['numStr']}章" for x in cands)
                            + f"），与缺号（第{gap_names}章）不符，已记录供人工核对"
                        ),
                    }
                )
            if len(gap) > MAX_INFERRED_MISSING_CHAPTERS:
                warnings.append(
                    {
                        "type": "inferred_split_skipped",
                        "detail": (
                            f"第{c['num']}章（{c['chars']}字）与下一章之间缺少 {len(gap)} 章"
                            f"（第{gap_names}章），缺号范围超过自动推断上限"
                            f" {MAX_INFERRED_MISSING_CHAPTERS} 章，未按字数拆分，"
                            "已保留原章，请人工核对"
                        ),
                    }
                )
                continue
            cuts, used_exact = _inferred_cut_points(text, nl, c, len(gap), baseline)
            inferred_splits[i] = cuts
            split_nums[i] = (
                [(c["numStr"], c["num"], c["title"])]
                + [(str(g), g, "") for g in gap[: len(cuts)]]
            )
            c["_explained"] = True
            warnings.append(
                {
                    "type": "inferred_split",
                    "detail": (
                        f"第{c['num']}章（{c['chars']}字）疑似包含缺失的"
                        f"第{gap_names}章：已按推断长度拆分为 {len(cuts) + 1} 段，请人工核对"
                    ),
                }
            )
            if used_exact:
                warnings.append(
                    {
                        "type": "inferred_split_mid_paragraph",
                        "detail": (
                            f"第{c['num']}章内部没有空行可对齐，推断切点落在段落中间，"
                            "请务必人工核对"
                        ),
                    }
                )


    # -- step 4: fingerprints + duplicate removal --------------------------
    for c in work:
        c["fingerprint"] = _normalized_fingerprint(text[c["start"]: c["end"]])
    # duplicate groups (same parseable number), physical order
    groups: dict[int, list[dict]] = {}
    for c in work:
        if c["num"] is not None:
            groups.setdefault(c["num"], []).append(c)
    drop: set[int] = set()  # ids() of chapters to drop
    for num, members in groups.items():
        kept_fps: list[str] = [members[0]["fingerprint"]]  # first occurrence always kept
        for c in members[1:]:
            if c["fingerprint"] in kept_fps:
                drop.add(id(c))
                removed_spans.append((c["start"], c["end"]))
                removed.append(
                    {
                        "seq": c["seq"],
                        "num": num,
                        "numStr": c["numStr"],
                        "title": c["title"],
                        "kind": "dropped",
                    }
                )
            else:
                kept_fps.append(c["fingerprint"])
    # cross-number identical content -> warning only, never dropped
    seen_fp: dict[str, dict] = {}
    for c in work:
        fp = c["fingerprint"]
        if fp in seen_fp and id(c) not in drop:
            other = seen_fp[fp]
            if other["num"] != c["num"]:
                warnings.append(
                    {
                        "type": "content_collision",
                        "detail": (
                            f"第{other['numStr']}章与第{c['numStr']}章正文完全相同"
                            "（章号不同），未做处理，请留意"
                        ),
                    }
                )
        else:
            seen_fp[fp] = c

    # gap-before flag (same chain semantics as check_chapter_sequence: the
    # number jumped up from the last parseable predecessor) — report only.
    prev_num: Optional[int] = None
    for c in work:
        if c["num"] is None:
            continue
        if prev_num is not None and c["num"] > prev_num + 1:
            c["_gap_before"] = True
        prev_num = c["num"]

    # -- build the repaired structure (splits + drops) ---------------------
    new_work: list[dict] = []
    for i, c in enumerate(work):
        if id(c) in drop:
            continue
        cuts = inferred_splits.get(i)
        if not cuts:
            new_work.append(c)
            continue
        # Per-segment numbering was fixed up front: inferred splits fill the
        # gap numbers in order, a duplicate split keeps both copies' numbers.
        nums = split_nums[i]
        points = [c["start"]] + list(cuts) + [c["end"]]
        for j in range(len(points) - 1):
            s, e = points[j], points[j + 1]
            if e <= s or char_count(s, e) == 0:
                return {
                    "status": "error",
                    "error": f"拆分产生空段落（第{c['num']}章），边界异常，已放弃输出。",
                    "chapters": [],
                    "final_numbers": [],
                    "report": {"actions": [], "warnings": warnings, "removed": removed},
                    "baseline_chars": baseline,
                    "original_count": len(chapters),
                }
            seg = dict(c)
            seg["start"], seg["end"] = s, e
            seg["chars"] = char_count(s, e)
            seg["numStr"], seg["num"], seg["title"] = nums[j]
            seg["fingerprint"] = _normalized_fingerprint(text[s:e])  # per-segment
            seg["_parent_seq"] = c["seq"]
            if c.get("_dup_split"):
                seg["_split_kind"] = "duplicate_split"
            elif c.get("_range_split"):
                seg["_split_kind"] = "range_split"
            elif c.get("_mechanical"):
                seg["_split_kind"] = "mechanical_split"
            else:
                seg["_split_kind"] = "inferred_split"
            seg["_seg_index"] = j
            new_work.append(seg)

    # -- step 5: renumber 1..N in physical order + repair records ----------
    for pos, c in enumerate(new_work):
        c["seq"] = pos + 1
        c["final_num"] = pos + 1

    dup_nums = {n for n, ms in groups.items() if len(ms) >= 2}

    _conf_rank = {"high": 2, "medium": 1, "low": 0}  # high > medium > low
    actions: list[dict] = []
    for c in new_work:
        acts: list[str] = []
        levels: list[str] = ["high"]

        def add_action(kind: str, level: str = "high") -> None:
            if kind not in acts:
                acts.append(kind)
                levels.append(level)

        if c.get("_split_kind") == "range_split":
            add_action("range_split", "high")
        elif c.get("_split_kind") == "inferred_split":
            add_action("inferred_split", "low")
        elif c.get("_split_kind") == "mechanical_split":
            add_action("mechanical_split", "low")
        elif c.get("_split_kind") == "duplicate_split":
            add_action("duplicate_kept", "medium")
        if c.get("_truncated"):
            add_action("duplicate_truncated", "medium")
        if c["num"] in dup_nums:
            add_action("duplicate_kept", "medium")
        if c["num"] is not None and c["num"] != c["final_num"]:
            acts.append("renumbered")
        # inferred segments (j>0) exist to fill the missing numbers; unsplit
        # chapters absorb a gap when their number jumped up from the previous
        # parseable one. Duplicate-split segments fill no gap.
        is_inferred_seg = (
            c.get("_split_kind") in {"inferred_split", "mechanical_split"}
            and c.get("_seg_index", 0) > 0
        )
        if is_inferred_seg or c.get("_gap_before"):
            acts.append("gap_absorbed")
        if not acts:
            acts.append("kept")
        conf = min(levels, key=_conf_rank.get)  # lowest confidence level wins
        c["repair"] = {
            "orig_num": c["num"],
            "orig_numStr": c["numStr"],
            "final_num": c["final_num"],
            "actions": acts,
            "confidence": conf,
        }
        actions.append(
            {
                "seq": c["seq"],
                "orig_num": c["num"],
                "orig_numStr": c["numStr"],
                "orig_title": c["title"],
                "final_num": c["final_num"],
                "actions": acts,
                "confidence": conf,
            }
        )

    # -- step 6: safety self-check (never ship a broken structure) ---------
    # Dropped duplicate spans are excised, so the invariant is: the kept
    # chapters + the dropped spans tile [0, len(text)] exactly, and the kept
    # chapters concatenate to the original text with those spans removed
    # (no character lost, added, or reordered within the kept set).
    all_spans = sorted([(c["start"], c["end"]) for c in new_work] + removed_spans)
    tiling_ok = all(b[0] == a[1] for a, b in zip(all_spans, all_spans[1:]))
    tiling_ok = tiling_ok and bool(all_spans) and all_spans[0][0] == 0 and all_spans[-1][1] == len(text)
    nonempty_ok = all(c["end"] > c["start"] for c in new_work)
    excised = text
    for s, e in sorted(removed_spans, reverse=True):  # reverse keeps indices valid
        excised = excised[:s] + excised[e:]
    joined_ok = "".join(text[c["start"]: c["end"]] for c in new_work) == excised
    numbers = [c["final_num"] for c in new_work]
    numbers_ok = numbers == list(range(1, len(new_work) + 1))
    if not (joined_ok and tiling_ok and nonempty_ok and numbers_ok):
        return {
            "status": "error",
            "error": "结构自检未通过（拼接/边界/编号异常），已放弃输出，请检查原文。",
            "chapters": [],
            "final_numbers": [],
            "report": {"actions": [], "warnings": warnings, "removed": removed},
            "baseline_chars": baseline,
            "original_count": len(chapters),
        }

    # "clean" = nothing found at all (no actions, no warnings); any action or
    # warning makes it "ok" (the report carries the details).
    status = "clean" if all(a["actions"] == ["kept"] for a in actions) and not warnings else "ok"
    out_chapters = []
    for c in new_work:
        out = dict(c)
        for key in (
            "_long",
            "_explained",
            "_parent_seq",
            "_split_kind",
            "_mechanical",
            "_range_split",
            "_seg_index",
            "_gap_before",
            "_truncated",
            "_dup_split",
        ):
            out.pop(key, None)
        out_chapters.append(out)
    return {
        "status": status,
        "chapters": out_chapters,
        "final_numbers": numbers,
        "report": {"actions": actions, "warnings": warnings, "removed": removed},
        "baseline_chars": baseline,
        "original_count": len(chapters),
    }


def make_smart_filenames(chapters: list[dict]) -> list[str]:
    """``第 {NNN} 章 {title}.txt`` for smart-repair output. NNN width: N <=
    SMART_FILENAME_WIDTH_THRESHOLD -> 3 digits (001...), else 4 (0001...);
    an empty title yields ``第 {NNN} 章.txt``. Names are sanitized, then
    de-duplicated (a collision appends `` 2``, `` 3``... to the latest
    offender) so the list is always unique. Input chapters must carry
    ``final_num`` (1..N in physical order)."""
    n = len(chapters)
    w = 3 if n <= SMART_FILENAME_WIDTH_THRESHOLD else 4
    used: set[str] = set()
    out: list[str] = []
    for c in chapters:
        num = c.get("final_num", c.get("seq", 1))
        title = (c.get("title") or "").strip()
        base = f"第 {str(num).zfill(w)} 章 {title}.txt" if title else f"第 {str(num).zfill(w)} 章.txt"
        name = sanitize_file_name(base)
        k = 2
        while name in used:
            name = sanitize_file_name(f"{base} {k}")
            k += 1
        used.add(name)
        out.append(name)
    return out


# ======================= Length-based splitting (no-chapter fallback) ======
# When no legitimate chapter structure is detected, split the whole book into
# evenly sized segments near a target length: segment count n =
# round(total_chars / target), so every segment is exactly total_chars / n
# (6200 chars at target 3000 -> 3100 + 3100, never 3000 + 3000 + 200).
# Cut-point tiers, best first — a paragraph and a sentence are NEVER cut:
#   1. paragraph boundaries (``\\n\\n``);
#   2. sentence boundaries (right after 。！？…) — tried second within the
#      snap window, so a cut whose window holds no paragraph break (e.g. inside
#      one giant paragraph) still lands on a sentence end instead of degrading;
#   3. degradation: not enough boundaries of either kind -> fewer segments
#      (they grow, stay equal), down to a single whole-book segment.

DEFAULT_LENGTH_TARGET_CHARS = 3_000

# Characters that END a sentence: a cut right after one of these never splits
# a sentence. Closing quote/paren forms are deliberately NOT included —
# unbalanced quotes across a boundary are cosmetic; a cut after ” without
# sentence punctuation inside could split a sentence.
_SENTENCE_END_CHARS = "。！？…"


def _sentence_end_boundaries(text: str, nl: list[int], a: int, b: int) -> list[int]:
    """Cut points p in (a, b) right after a sentence-ending character (。！？…).
    The fallback tier for cuts whose snap window holds no paragraph break (a
    whole book with no blank lines, or one giant paragraph after a clustered
    short-paragraph prefix) — a paragraph is never cut mid-sentence."""
    out: list[int] = []
    # Line-scan: formatted text is one line per paragraph; only lines inside
    # [a, b) can host a cut point.
    line_start = a
    for line_end in nl:
        if line_end >= b:
            break
        line = text[line_start:line_end]
        for i, ch in enumerate(line):
            if ch in _SENTENCE_END_CHARS:
                p = line_start + i + 1
                if a < p < b and range_char_count(nl, a, p) > 0 and range_char_count(nl, p, b) > 0:
                    out.append(p)
        line_start = line_end + 1
    if line_start < b:
        line = text[line_start:b]
        for i, ch in enumerate(line):
            if ch in _SENTENCE_END_CHARS:
                p = line_start + i + 1
                if a < p < b and range_char_count(nl, a, p) > 0 and range_char_count(nl, p, b) > 0:
                    out.append(p)
    return out


def split_by_length(text: str, target_chars: Optional[int] = None) -> dict:
    """Split chapter-less text into near-equal segments of about ``target_chars``
    (default :data:`DEFAULT_LENGTH_TARGET_CHARS`), cut only at paragraph or
    sentence boundaries — paragraph breaks are preferred inside the snap
    window; where a window holds none (e.g. one giant paragraph) the cut
    falls back to sentence ends. A sentence is never cut.

    Returns ``{status, segments, target, segment_count, warnings, error}``.
    ``segments`` are ``{seq, start, end, chars}`` tiling ``[0, len(text))``
    exactly — concatenating ``text[s:e]`` over the segments reproduces the
    original exactly. ``status`` is ``"error"`` only when the input is empty
    (an empty book has nothing to write); every other shape degrades safely
    instead of cutting a paragraph or a sentence."""
    try:
        target = int(target_chars) if target_chars is not None else DEFAULT_LENGTH_TARGET_CHARS
    except (TypeError, ValueError):
        target = DEFAULT_LENGTH_TARGET_CHARS
    if target <= 0:
        target = DEFAULT_LENGTH_TARGET_CHARS
    nl = build_newline_positions(text)

    def char_count(a: int, b: int) -> int:
        return range_char_count(nl, a, b)

    total = char_count(0, len(text))
    if total <= 0:
        return {
            "status": "error",
            "error": "文本为空，无法按字数拆分",
            "segments": [],
            "target": target,
            "segment_count": 0,
            "warnings": [],
        }

    warnings: list[dict] = []
    # Even division: round(total / target) segments, each exactly total / n —
    # a floor division would leave a short tail segment (3000 + 200).
    wanted = max(1, int(total / target + 0.5))
    paragraph_bounds = [
        b for b in _paragraph_boundaries(text, 0, len(text)) if char_count(b, len(text)) > 0
    ]
    # The available pool is ALWAYS the union of both tiers: sentence ends
    # become relevant at any cut whose snap window holds no paragraph break —
    # e.g. a short-paragraph prefix followed by one giant paragraph, where
    # paragraph breaks are "enough" globally but clustered, so later cuts
    # would otherwise fall outside every window and degrade to whole-book
    # even though the giant paragraph is full of sentence ends.
    all_bounds = sorted(
        set(paragraph_bounds) | set(_sentence_end_boundaries(text, nl, 0, len(text)))
    )

    # The pick loop below consumes one boundary strictly to the right of the
    # previous cut per segment, so count-1 <= len(all_bounds) guarantees every
    # cut lands on a real boundary — a paragraph or a sentence is never cut.
    safe_count = min(wanted, len(all_bounds) + 1)
    if safe_count < wanted:
        if safe_count > 1:
            warnings.append(
                {
                    "type": "length_split_reduced",
                    "detail": (
                        f"段落与句子边界不足，已从 {wanted} 册降为 {safe_count} 册"
                        "（不切段落、不截断句子），每册字数相应变大"
                    ),
                }
            )
        else:
            warnings.append(
                {
                    "type": "length_split_degraded",
                    "detail": "段落与句子边界不足，已按整本单册输出（不切段落、不截断句子）",
                }
            )
    count = safe_count
    per = total / count

    if count == 1:
        cuts: list[int] = []
    else:
        span = len(text) / count
        cuts = []
        prev = 0
        for m in range(1, count):
            t = _index_at_char_count(text, nl, 0, int(round(per * m)))
            t = max(t, prev + 1)
            lo = int(t - INFER_SNAP_TOLERANCE * span)
            hi = int(t + INFER_SNAP_TOLERANCE * span)

            def pick_nearest(pool: list[int], center: int, in_window: bool) -> Optional[int]:
                win = [b for b in pool if prev < b < len(text) and (lo <= b <= hi if in_window else True)]
                if not win:
                    return None
                return min(win, key=lambda b: (abs(b - center), b))

            # Tier 1: paragraph breaks (only when the text actually has any —
            # a chapter-less book is often one paragraph, then every cut comes
            # from tier 2); tier 2: sentence ends. Within the tolerance window
            # each tier is tried first; outside it, paragraphs stay preferred.
            pick = pick_nearest(paragraph_bounds, t, True) if paragraph_bounds else None
            if pick is None:
                pick = pick_nearest(all_bounds, t, True)
            if pick is None:
                pick = pick_nearest(paragraph_bounds, t, False) if paragraph_bounds else None
            if pick is None:
                pick = pick_nearest(all_bounds, t, False)
            if pick is None:
                # Cannot happen when count <= len(all_bounds) + 1; guard so a
                # degenerate input degrades to whole-book instead of crashing.
                return {
                    "status": "ok",
                    "segments": [{"seq": 1, "start": 0, "end": len(text), "chars": total}],
                    "target": target,
                    "segment_count": 1,
                    "warnings": warnings
                    + [
                        {
                            "type": "length_split_degraded",
                            "detail": "段落与句子边界不足，已按整本单册输出（不切段落、不截断句子）",
                        }
                    ],
                    "error": None,
                }
            cuts.append(pick)
            prev = pick

    points = [0, *cuts, len(text)]
    segments = [
        {
            "seq": i + 1,
            "start": points[i],
            "end": points[i + 1],
            "chars": char_count(points[i], points[i + 1]),
        }
        for i in range(len(points) - 1)
    ]
    if not (
        points[0] == 0
        and points[-1] == len(text)
        and all(a < b for a, b in zip(points, points[1:]))
        and "".join(text[s:e] for s, e in zip(points, points[1:])) == text
    ):
        return {
            "status": "error",
            "error": "按字数拆分结构自检未通过（边界/拼接异常），已放弃输出。",
            "segments": [],
            "target": target,
            "segment_count": 0,
            "warnings": warnings,
        }
    return {
        "status": "ok",
        "segments": segments,
        "target": target,
        "segment_count": len(segments),
        "warnings": warnings,
        "error": None,
    }


def is_generated_split_output_name(name: str) -> bool:
    """Return whether a top-level split output name follows a generated convention."""
    return (
        (name.startswith("第") and name.endswith(".txt"))
        or bool(re.search(r" 分册\d+ 第\d+章\.txt$", name))
        or name.endswith(" 全书.txt")
    )
