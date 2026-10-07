"""Text formatting engine — ported (behavior-preserving) from TextFormatter.

Source: ``TextFormatter/index.html``, the block between the ``__ENGINE_BEGIN__``
/ ``__ENGINE_END__`` markers. The deterministic rules (whitespace / paragraph /
punctuation / chapter) are reproduced 1:1. One rule added after the port:
``ensure_title_space`` puts a single space between a chapter number and its
title when they are attached (第十九章神秘分阁主 → 第十九章 神秘分阁主).
Adjacent identical chapter headers (ignoring whitespace) are reduced to one
when chapter detection is enabled. Body content is preserved; the invariant
test suite also checks idempotency in ``tests/test_text.py``.

``cfg`` is duck-typed: any object exposing the toggles (see
``core.config.TextConfig``) — ``keep_single_space``, ``sentence_break``,
``dialogue_separate``, ``detect_chapters``, ``punct_*``.
"""
from __future__ import annotations

import re
from typing import Any, Callable


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        (0x2E80 <= cp <= 0x9FFF)  # CJK radicals + unified ideographs
        or (0x3000 <= cp <= 0x303F)  # CJK punctuation
        or (0xFE00 <= cp <= 0xFE6F)  # CJK compatibility forms
        or (0xFF00 <= cp <= 0xFFEF)  # fullwidth forms
    )


def trim_edges(s: str) -> str:
    return re.sub(r"^\s+", "", re.sub(r"\s+$", "", s))


def normalize_line(line: str, cfg: Any, preserve_internal_spaces: bool = False) -> str:
    """Trim the line and unify internal whitespace per the keepSingleSpace toggle."""
    s = trim_edges(line)
    if preserve_internal_spaces or cfg.keep_single_space:
        s = re.sub(r"\s+", " ", s)
    else:
        s = re.sub(r"\s+", "", s)
    return s


def convert_lone_ascii(s: str) -> str:
    """Convert a lone half-width , ! ? to its CJK form, but only when adjacent to
    CJK — this protects English and URLs."""
    out = []
    for i, c in enumerate(s):
        if c in ",!?":
            prev = s[i - 1] if i > 0 else ""
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if _is_cjk(prev) or _is_cjk(nxt):
                out.append("，" if c == "," else "！" if c == "!" else "？")
                continue
        out.append(c)
    return "".join(out)


def apply_punct(s: str, cfg: Any) -> str:
    """Punctuation standardization (quotes are handled per-paragraph, not here)."""
    if cfg.punct_ellipsis:
        s = re.sub(r"\.{3,}", "……", s)  # ... / ....  ->  ……
        s = re.sub(r"。{2,}", "……", s)  # 。。。      ->  ……
    if cfg.punct_repeated:
        s = re.sub(r"(,|，){2,}", "，", s)  # ,, / ，， -> ，
        s = re.sub(r"(!|！){2,}", "！", s)  # !! / ！！ -> ！
        s = re.sub(r"(\?|？){2,}", "？", s)  # ?? / ？？ -> ？
    if cfg.punct_dash:
        s = re.sub(r"-{2,}", "——", s)  # --  ->  ——
    if cfg.punct_lone_ascii:
        s = convert_lone_ascii(s)
    return s


def convert_quotes(s: str) -> str:
    """Alternate straight double-quotes to “ ” within a paragraph (heuristic)."""
    out = []
    opening = True
    for c in s:
        if c == '"':
            out.append("“" if opening else "”")
            opening = not opening
        else:
            out.append(c)
    return "".join(out)


# Chapter-title detection: tolerate export-specific spaces, full-width digits,
# wrappers, repeated book-name prefixes, alternate units, and common English
# forms. Ambiguous bare-number lines are deliberately excluded here because a
# single line cannot establish that they are chapters; the book chunker checks
# those only as a monotonic sequence.
CHAPTER_RE = re.compile(
    r"^(?:"
    r"(?:[【〖〔（(「『《<\[［])?\s*第\s*[0-9０-９〇零一二三四五六七八九十百千万亿两廿卅]+\s*章"
    r"(?:[】〗〕）)」』》>\]］])?.*"
    r"|(?:[【〖〔（(「『《<\[［])?\s*第\s*[0-9０-９〇零一二三四五六七八九十百千万亿两廿卅]+\s*"
    r'[节回话話卷集部篇幕场折](?:[】〗〕）)」』》>\]］])?(?:[:：;；,，、·|｜/／_\-—\s「『“"].*|)'
    r"|[^\s\r\n][^\r\n]{0,39}?\s+(?:[【〖〔（(「『《<\[［])?\s*第\s*[0-9０-９〇零一二三四五六七八九十百千万亿两廿卅]+\s*"
    r'[章节回话話卷集部篇幕场折](?:[】〗〕）)」』》>\]］])?(?:[:：;；,，、·|｜/／_\-—\s「『“"].*|)'
    r"|(?:卷|部|篇)\s*[0-9０-９〇零一二三四五六七八九十百千万亿两廿卅]+(?:[:：;；,，、·|｜/／_\-—\s].*|)"
    r"|(?:Chapter|Chap\.?|Ch\.?)\s*(?:[0-9０-９]+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)(?:[\s:：;；,，、·|｜/／_.\-—].*|)"
    r"|(?:Episode|Ep\.?|Part|Section|Volume|Vol\.?)\s*[0-9０-９]+(?:[\s:：;；,，、·|｜/／_.\-—].*|)"
    r"|(?:No\.?|Number|#)\s*[0-9０-９]+(?:[\s:：;；,，、·|｜/／_.\-—].*|)"
    r"|(?:楔子|序章|引子|序言|前言|尾声|后记|番外|外传|外傳|附录|正文|终章)(?:[:：·\-\s「『“\"].*|)"
    r")$",
    re.IGNORECASE,
)


def is_chapter_title(line: str) -> bool:
    if not line or len(line) > 40:
        return False
    return bool(CHAPTER_RE.match(line))


# Number markers in a heading: retain title spacing, while normalizing spaces
# inside the number itself and separating volume/book/chapter/title boundaries.
_NUM_TITLE_RE = re.compile(
    r"第[ \t　]*(?P<num>[0-9０-９〇零一二三四五六七八九十百千万亿两廿卅]+"
    r"(?:[ \t　]+[0-9０-９〇零一二三四五六七八九十百千万亿两廿卅]+)*)"
    r"[ \t　]*(?P<kind>[章节回话話卷集部篇幕场折])(?P<close>[】〗〕）)」』》>\]］]?)"
)


def ensure_title_space(line: str) -> str:
    """Separate heading fields without deleting spaces inside their names."""
    matches = list(_NUM_TITLE_RE.finditer(line))
    if not matches:
        return line
    # A volume/arc header can precede the actual chapter marker. Once the
    # leaf marker is reached, later numbered references belong to its title.
    selected = [matches[0]]
    if matches[0].group("kind") in "卷部篇" and len(matches) > 1:
        selected.append(matches[1])
    delimiters = "：:;；﹔,，﹐、·•・‧|｜/／_＿—–-"
    for match in reversed(selected):
        marker = "第" + re.sub(r"\s+", "", match.group("num")) + match.group("kind") + match.group("close")
        start, end = match.span()
        before = line[:start]
        after = line[end:]
        if before and not before[-1].isspace() and before[-1] not in delimiters + "【〖〔（(「『《<[［":
            before += " "
        if after and not after[0].isspace() and after[0] not in delimiters:
            marker += " "
        line = before + marker + after
    return line


_SENT_END = re.compile(r"[。！？…“”」』）]")


def ends_sentence(s: str) -> bool:
    """A trailing sentence-punct makes this line's newline a real paragraph break."""
    return bool(s) and bool(_SENT_END.match(s[-1]))


def starts_quote(s: str) -> bool:
    return bool(s) and s[0] in '"“'


def count_chars(s: str) -> int:
    """Character count = length with all whitespace removed."""
    return len(re.sub(r"\s", "", s))


def format_text(text: str, cfg: Any, *, on_progress: Callable[[float], None] | None = None) -> dict:
    """The main formatting pipeline. Returns ``{text, stats}``."""
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\r", "\n", text)
    raw_lines = text.split("\n")

    lines = []
    previous_chapter_key: str | None = None
    for line_index, raw in enumerate(raw_lines, 1):
        trimmed = trim_edges(raw)  # for title detection (keeps inner spaces)
        chapter_like = is_chapter_title(trimmed)
        is_chapter = cfg.detect_chapters and chapter_like
        # Keep separators on chapter lines: they distinguish a repeated book
        # name from ``第N章`` and the number marker from its title.
        norm = apply_punct(
            normalize_line(
                raw,
                cfg,
                preserve_internal_spaces=chapter_like,
            ),
            cfg,
        )
        if chapter_like:
            # 章节号与章节名的分界补一个空格（第十九章神秘分阁主 → 第十九章 神秘分阁主）
            norm = ensure_title_space(norm)
        if norm:
            chapter_key = re.sub(r"\s+", "", norm) if is_chapter else None
            duplicate_title = is_chapter and chapter_key == previous_chapter_key
            previous_chapter_key = chapter_key
        else:
            duplicate_title = False  # Blank lines do not separate mirrored headers.
        if not duplicate_title:
            lines.append((norm, is_chapter))
        if on_progress:
            on_progress(0.5 * line_index / max(1, len(raw_lines)))

    paragraphs: list[str] = []
    current = ""
    had_blank = False  # a blank line appeared after the last content line
    prev_ended = False  # last content line ended with sentence punctuation
    force_break = False  # force a new paragraph after a chapter title

    for line_index, (norm, is_chapter) in enumerate(lines, 1):
        if on_progress:
            on_progress(0.5 + 0.4 * line_index / max(1, len(lines)))
        if norm == "":  # blank line -> hard boundary
            if current:
                paragraphs.append(current)
                current = ""
            had_blank = True
            prev_ended = False
            force_break = False
            continue

        is_first = len(paragraphs) == 0 and current == ""
        starts_q = cfg.dialogue_separate and starts_quote(norm)
        new_para = (
            is_first or had_blank or is_chapter or starts_q
            or force_break or (cfg.sentence_break and prev_ended)
        )
        if new_para:
            if current:
                paragraphs.append(current)
                current = ""
            current = norm
        else:
            current = (current + norm) if current else norm  # merge intra-paragraph newlines
        prev_ended = ends_sentence(norm)
        force_break = is_chapter
        had_blank = False

    if current:
        paragraphs.append(current)

    if cfg.punct_quotes:
        paragraphs = [convert_quotes(p) for p in paragraphs]

    output = "\n\n".join(paragraphs)
    if on_progress:
        on_progress(1.0)
    return {
        "text": output,
        "stats": {
            "chars": count_chars(output),
            "paras": len(paragraphs),
            "chapters": sum(1 for _, c in lines if c),
        },
    }
