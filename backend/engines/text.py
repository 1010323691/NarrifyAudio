"""Text formatting engine — ported (behavior-preserving) from TextFormatter.

Source: ``TextFormatter/index.html``, the block between the ``__ENGINE_BEGIN__``
/ ``__ENGINE_END__`` markers. The deterministic rules (whitespace / paragraph /
punctuation / chapter) are reproduced 1:1. The invariant test suite (content
preservation + idempotency) is ported to ``tests/test_text.py`` as the safety net.

``cfg`` is duck-typed: any object exposing the toggles (see
``core.config.TextConfig``) — ``keep_single_space``, ``sentence_break``,
``dialogue_separate``, ``detect_chapters``, ``punct_*``.
"""
from __future__ import annotations

import re
from typing import Any


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
    r"[节回卷集部篇幕场折](?:[】〗〕）)」』》>\]］])?(?:[:：、·\-—\s].*|)"
    r"|[^\s\r\n][^\r\n]{0,39}?\s+(?:[【〖〔（(「『《<\[［])?\s*第\s*[0-9０-９〇零一二三四五六七八九十百千万亿两廿卅]+\s*"
    r"[章节回卷集部篇幕场折](?:[】〗〕）)」』》>\]］])?(?:[:：、·\-—\s].*|)"
    r"|(?:卷|部|篇)\s*[0-9０-９〇零一二三四五六七八九十百千万亿两廿卅]+(?:[:：、·\-—\s].*|)"
    r"|(?:Chapter|Chap\.?|Ch\.?)\s*(?:[0-9０-９]+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)(?:[\s:：、·.\-—].*|)"
    r"|(?:Episode|Ep\.?|Part|Section|Volume|Vol\.?)\s*[0-9０-９]+(?:[\s:：、·.\-—].*|)"
    r"|(?:No\.?|Number|#)\s*[0-9０-９]+(?:[\s:：、·.\-—].*|)"
    r"|(?:楔子|序章|引子|序言|前言|尾声|后记|番外|附录|正文|终章)(?:[:：·\-\s].*|)"
    r")$",
    re.IGNORECASE,
)


def is_chapter_title(line: str) -> bool:
    if not line or len(line) > 40:
        return False
    return bool(CHAPTER_RE.match(line))


_SENT_END = re.compile(r"[。！？…“”」』）]")


def ends_sentence(s: str) -> bool:
    """A trailing sentence-punct makes this line's newline a real paragraph break."""
    return bool(s) and bool(_SENT_END.match(s[-1]))


def starts_quote(s: str) -> bool:
    return bool(s) and s[0] in '"“'


def count_chars(s: str) -> int:
    """Character count = length with all whitespace removed."""
    return len(re.sub(r"\s", "", s))


def format_text(text: str, cfg: Any) -> dict:
    """The main formatting pipeline. Returns ``{text, stats}``."""
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\r", "\n", text)
    raw_lines = text.split("\n")

    lines = []
    for raw in raw_lines:
        trimmed = trim_edges(raw)  # for title detection (keeps inner spaces)
        chapter_like = is_chapter_title(trimmed)
        is_chapter = cfg.detect_chapters and chapter_like
        # Keep separators on chapter lines: they distinguish a repeated book
        # name from ``第N章`` and the number marker from its title.
        starts_with_marker = bool(
            re.match(
                r"^(?:[【〖〔（(「『《<\[［])?\s*(?:第|卷|部|篇|"
                r"Chapter|Chap\.?|Ch\.?|Episode|Ep\.?|Part|Section|"
                r"Volume|Vol\.?|No\.?|Number|#|楔子|序章|引子|序言|前言|"
                r"尾声|后记|番外|附录|正文|终章)",
                trimmed,
                re.IGNORECASE,
            )
        )
        norm = apply_punct(
            normalize_line(
                raw,
                cfg,
                preserve_internal_spaces=chapter_like and not starts_with_marker,
            ),
            cfg,
        )
        lines.append((norm, is_chapter))

    paragraphs: list[str] = []
    current = ""
    had_blank = False  # a blank line appeared after the last content line
    prev_ended = False  # last content line ended with sentence punctuation
    force_break = False  # force a new paragraph after a chapter title

    for norm, is_chapter in lines:
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
    return {
        "text": output,
        "stats": {
            "chars": count_chars(output),
            "paras": len(paragraphs),
            "chapters": sum(1 for _, c in lines if c),
        },
    }
