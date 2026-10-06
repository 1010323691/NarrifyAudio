"""Invariant tests for the book-chunking engine (``backend/engines/book.py``).

These are the Python port of BookChunker's behavioural contract (its CLAUDE.md
invariants): chapters tile the whole text; each chapter is written as exactly one
file (never split); chapters are never renumbered; no chapters -> stop (no forced
split); and concatenating all per-chapter files reproduces the original exactly.
Plus unit checks for encoding detection, character counting, Chinese numerals,
the chapter-sequence report and the exact output-file naming.
"""
from __future__ import annotations

from backend.engines import book as B


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_novel(num_chapters: int = 20, body_repeats: int = 40) -> str:
    """Build a synthetic novel: a preamble, then ``num_chapters`` numbered
    chapters with fixed-length bodies (each "段。" line ends in a newline, so the
    newline-exclusion counting path is exercised)."""
    lines = ["这是一部用于测试的分册小说。", "前言内容，若干行。", ""]
    for i in range(1, num_chapters + 1):
        lines.append(f"第{i}章 标题{i}")
        for _ in range(body_repeats):
            lines.append(f"这是第{i}章的一段正文内容。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Character counting
# --------------------------------------------------------------------------- #

def test_range_char_count_contract():
    # range char count excludes newlines
    text = "abc\ndef\n"  # positions: a0 b1 c2 \n3 d4 e5 f6 \n7
    nl = B.build_newline_positions(text)
    assert nl == [3, 7]
    assert B.range_char_count(nl, 0, 8) == 6  # a b c d e f
    assert B.range_char_count(nl, 0, 3) == 3  # a b c
    assert B.range_char_count(nl, 4, 7) == 3  # d e f
    assert B.range_char_count(nl, 3, 3) == 0  # empty range
    # The whole-text count equals non-newline code points.
    assert sum(1 for c in text if c not in "\r\n") == B.range_char_count(nl, 0, len(text))

    # range char count multibyte code points
    # Emoji (non-BMP) count as one code point each, matching Python len().
    text = "你好🌍🔥"  # 6 code points, 2 of them non-BMP
    nl = B.build_newline_positions(text)
    assert B.range_char_count(nl, 0, len(text)) == len(text)


# --------------------------------------------------------------------------- #
# Chapter detection + tiling
# --------------------------------------------------------------------------- #

def test_chapter_detection_tiling_and_round_trip():
    # chapters tile the whole text
    text = make_novel(20)
    analysis = B.analyze_text(text)
    chs = analysis["chapters"]
    assert len(chs) == 20

    # Preamble is absorbed into the first chapter: it starts at 0.
    assert chs[0]["start"] == 0
    # The last chapter runs to the end of the file.
    assert chs[-1]["end"] == len(text)
    # Contiguous tiling: chapter i ends where chapter i+1 begins.
    for i in range(len(chs) - 1):
        assert chs[i]["end"] == chs[i + 1]["start"]
    # Each chapter has a real (non-negative) length.
    for c in chs:
        assert c["end"] > c["start"]
        assert c["chars"] >= 0
    # Chapter numbers parsed as 1..20.
    assert [c["num"] for c in chs] == list(range(1, 21))

    # chapter header positions are real headers
    text = make_novel(10)
    analysis = B.analyze_text(text)
    chs = analysis["chapters"]
    # From chapter 2 onward, the slice begins at its "第N章" header.
    for i in range(1, len(chs)):
        assert text[chs[i]["start"]].startswith("第")

    # round trip concatenation equals original
    text = make_novel(40)
    analysis = B.analyze_text(text)
    # One file per chapter: concatenating the per-chapter slices reproduces the
    # original exactly (no character lost, added, or reordered).
    joined = "".join(B.chapter_content(analysis, ch) for ch in analysis["chapters"])
    assert joined == text


def test_no_chapters_yields_no_files():
    text = "这是一段没有任何章节标记的普通文本。\n它只是正文，没有第几章。"
    analysis = B.analyze_text(text)
    assert analysis["chapters"] == []
    assert B.make_chapter_filenames("书", analysis["chapters"]) == []  # stop, never force-split


# --------------------------------------------------------------------------- #
# The core invariant: round-trip
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Output-file naming (exact contract)
# --------------------------------------------------------------------------- #

def test_chapter_filename_contract():
    # chapter filenames format and padding
    # Chapters numbered 1..5 -> NN width max(2, 1) = 2; chapter width max(3, 1) = 3.
    chapters = [
        {"num": 1, "numStr": "1"},
        {"num": 2, "numStr": "2"},
        {"num": 3, "numStr": "3"},
        {"num": 4, "numStr": "4"},
        {"num": 5, "numStr": "5"},
    ]
    names = B.make_chapter_filenames("测试小说", chapters)
    assert names == [
        "测试小说 分册01 第001章.txt",
        "测试小说 分册02 第002章.txt",
        "测试小说 分册03 第003章.txt",
        "测试小说 分册04 第004章.txt",
        "测试小说 分册05 第005章.txt",
    ]

    # chapter filenames widen with largest number
    # Largest chapter number is 1234 -> width 4 (>= 3).
    chapters = [{"num": 1, "numStr": "1"}, {"num": 1234, "numStr": "1234"}]
    names = B.make_chapter_filenames("书", chapters)
    assert names == ["书 分册01 第0001章.txt", "书 分册02 第1234章.txt"]

    # chapter filenames keep original non numeric labels
    # A chapter whose number can't be parsed keeps its raw label, unpadded.
    chapters = [
        {"num": None, "numStr": "楔子"},
        {"num": 1, "numStr": "1"},
    ]
    names = B.make_chapter_filenames("书", chapters)
    # 楔子 is unpadded; chapter 1 padded to width 3.
    assert names == ["书 分册01 第楔子章.txt", "书 分册02 第001章.txt"]

    # chapter filenames never renumber
    # A gap (1, 2, 5) must be preserved, not renumbered to 1,2,3.
    chapters = [
        {"num": 1, "numStr": "1"},
        {"num": 2, "numStr": "2"},
        {"num": 5, "numStr": "5"},
    ]
    names = B.make_chapter_filenames("书", chapters)
    assert names == [
        "书 分册01 第001章.txt",
        "书 分册02 第002章.txt",
        "书 分册03 第005章.txt",
    ]

    # chapter filenames widen with count
    # 120 chapters -> NN width max(2, digits of 120) = 3; chapter-number width
    # is driven by the largest NUMBER (120 -> 3), not by the count.
    chapters = [{"num": i, "numStr": str(i)} for i in range(1, 121)]
    names = B.make_chapter_filenames("书", chapters)
    assert names[0] == "书 分册001 第001章.txt"
    assert names[-1] == "书 分册120 第120章.txt"

    # chapter filenames unique for duplicate numbers
    # Duplicated chapter numbers must not collide: the positional 分册NN disambiguates.
    chapters = [{"num": 5, "numStr": "5"}, {"num": 5, "numStr": "5"}]
    names = B.make_chapter_filenames("书", chapters)
    assert names == ["书 分册01 第005章.txt", "书 分册02 第005章.txt"]


def test_whole_book_filename():
    assert B.make_whole_book_filename("书") == "书 全书.txt"
    # Same sanitizing rules as chapter names.
    assert B.make_whole_book_filename("a/b*c") == "a_b_c 全书.txt"


def test_expected_format_string():
    # The user-visible recognition rule, shipped with the analyze response.
    fmt = B.EXPECTED_CHAPTER_FORMAT
    assert "第N章" in fmt
    assert "阿拉伯数字" in fmt
    assert "中文数字" in fmt


def test_base_name_and_sanitizing():
    assert B.base_name("novel.txt") == "novel"
    assert B.base_name(r"C:\books\novel.txt") == "novel"
    assert B.base_name("noext") == "noext"
    assert B.sanitize_file_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


# --------------------------------------------------------------------------- #
# Encoding detection
# --------------------------------------------------------------------------- #

def test_decode_buffer_encodings():
    # decode utf8 bom
    data = b"\xef\xbb\xbf" + "你好".encode("utf-8")
    text, enc = B.decode_buffer(data)
    assert text == "你好"
    assert enc == "UTF-8（含 BOM）"

    # decode utf16 le bom
    data = b"\xff\xfe" + "你好".encode("utf-16-le")
    text, enc = B.decode_buffer(data)
    assert text == "你好"
    assert enc == "UTF-16 LE（含 BOM）"

    # decode plain utf8 cjk
    text = "你好世界，这是一段中文文本。" * 20
    text, enc = B.decode_buffer(text.encode("utf-8"))
    assert enc == "UTF-8"

    # decode gbk fallback
    original = "你好世界，测试文本，中文内容。" * 50
    data = original.encode("gbk")  # not valid UTF-8 -> falls back to GB18030
    text, enc = B.decode_buffer(data)
    assert enc == "GB18030 / GBK"
    assert text == original


# --------------------------------------------------------------------------- #
# Chinese-numeral parsing
# --------------------------------------------------------------------------- #

def test_chinese_number_parsing():
    # parse cn number
    cases = {
        "一": 1, "二": 2, "两": 2, "九": 9,
        "十": 10, "十一": 11, "二十": 20, "九十九": 99,
        "一百": 100, "一千": 1000, "一千零一": 1001,
    }
    for s, expected in cases.items():
        assert B.parse_cn_number(s) == expected, s

    # parse cn number invalid
    assert B.parse_cn_number("") is None
    assert B.parse_cn_number("〇") is None  # total 0 -> None
    assert B.parse_cn_number("abc") is None
    assert B.parse_cn_number("千") is None  # leading 千 with no preceding digit


def test_parse_chapter_number():
    assert B.parse_chapter_number("12") == 12
    assert B.parse_chapter_number("二十一") == 21
    assert B.parse_chapter_number("") is None
    assert B.parse_chapter_number(None) is None


# --------------------------------------------------------------------------- #
# Chapter-sequence checks (informational)
# --------------------------------------------------------------------------- #

def _chs(*numstrs):
    return [{"seq": i + 1, "numStr": s} for i, s in enumerate(numstrs)]


def test_chapter_sequence_report():
    # sequence gap
    rep = B.check_chapter_sequence(_chs("1", "2", "3", "5"))
    assert rep["hasIssues"]
    assert rep["gaps"] == [{"after": 3, "missing": [4]}]

    # sequence duplicate
    rep = B.check_chapter_sequence(_chs("1", "2", "2", "3"))
    assert rep["hasIssues"]
    assert rep["duplicates"] == [{"seq": 3, "num": 2}]

    # sequence disorder
    rep = B.check_chapter_sequence(_chs("3", "1", "2"))
    assert rep["hasIssues"]
    assert any(d["num"] == 1 for d in rep["disorder"])

    # sequence clean
    rep = B.check_chapter_sequence(_chs("1", "2", "3", "4"))
    assert not rep["hasIssues"]
    assert rep["first"] == 1 and rep["last"] == 4


# --------------------------------------------------------------------------- #
# Smart recognition (smart_repair — mechanical chapter-structure repair)
# --------------------------------------------------------------------------- #

def smart_body(n, seed):
    return [f"这是{seed}的第{j}段正文内容，字数足够长一些。" for j in range(n)]


def smart_novel(blocks):
    """blocks: (num, title, body_lines). Paragraphs joined by \\n\\n, mimicking
    formatted (排版) output."""
    paras = ["这是一部用于测试的智能识别小说。", "前言内容，若干行。"]
    for num, title, bl in blocks:
        paras.append(f"第{num}章 {title}")
        paras.extend(bl)
    return "\n\n".join(paras)


def smart_run(text, chapters=None):
    if chapters is None:
        chapters = B.analyze_text(text)["chapters"]
    return B.smart_repair(text, chapters)


def balanced_fixture(lengths, *, separator="\n\n", numbers=None, sentence_chars=100):
    """Exact character counts, independent of title detection and formatting."""
    blocks, chapters, offset = [], [], 0
    for i, length in enumerate(lengths):
        char = chr(ord("甲") + i)
        sentences = [char * (min(sentence_chars, length - j) - 1) + "。"
                     for j in range(0, length, sentence_chars)]
        block = separator.join(sentences) + separator
        num = numbers[i] if numbers else i + 1
        chapters.append({"seq": i + 1, "start": offset, "end": offset + len(block),
                         "num": num, "numStr": str(num) if num is not None else "楔子", "title": f"标题{i}"})
        blocks.append(block)
        offset += len(block)
    return "".join(blocks), chapters


def test_long_chapter_balance_average_lossless_and_no_short_tail():
    for separator in ["\n\n", "\r\n\r\n", ""]:
        text, chapters = balanced_fixture([6000, 26000, 6000, 6000, 6000], separator=separator)
        result = B.smart_repair(text, chapters, split_long_chapters=True)
        parts = [c for c in result["chapters"] if "long_split" in c]
        assert result["split_policy"]["target_chars"] == 6000, (separator,)
        assert result["split_policy"]["normal_sample_count"] == 4, (separator,)
        assert [p["chars"] for p in parts] == [6500] * 4, (separator,)
        assert [p["long_split"]["segment_index"] for p in parts] == [1, 2, 3, 4], (separator,)
        assert all(p["num"] == 2 and p["title"] == "标题1" for p in parts), (separator,)
        assert all("duplicate_kept" not in p["repair"]["actions"] for p in parts), (separator,)
        assert "".join(text[c["start"]:c["end"]] for c in result["chapters"]) == text, (separator,)
        assert len(set(B.make_smart_filenames(result["chapters"]))) == len(result["chapters"]), (separator,)
        # Sentence fallback also cuts on full sentence ends.
        if not separator:
            assert all(text[p["end"] - 1] == "。" for p in parts), (separator,)


def test_long_chapter_balance_two_times_threshold():
    for length, expected in [(11999, 0), (12000, 2)]:
        text, chapters = balanced_fixture([6000, length, 6000, 6000, 6000])
        result = B.smart_repair(text, chapters, split_long_chapters=True)
        assert sum("long_split" in c for c in result["chapters"]) == expected, (length, expected,)


def test_long_chapter_balance_multiple_and_unparseable_numbers():
    text, chapters = balanced_fixture([3000, 15000, 3000, 9000, 3000, 3000], numbers=[1, None, 3, 4, 5, 6])
    result = B.smart_repair(text, chapters, split_long_chapters=True)
    parts = [c for c in result["chapters"] if "long_split" in c]
    assert len(parts) == 8
    assert {c["source_chapter_id"] for c in parts} == {"2", "4"}
    assert result["split_policy"]["target_chars"] == 3000
    assert "".join(text[c["start"]:c["end"]] for c in result["chapters"]) == text


def test_long_chapter_balance_uses_fallback_when_baseline_unreliable():
    for lengths in [[500, 5000], [100, 5000, 100, 100, 100]]:
        text, chapters = balanced_fixture(lengths)
        result = B.smart_repair(text, chapters, split_long_chapters=True, length_target=2000)
        assert result["split_policy"]["target_source"] == "length_target", (lengths,)
        assert result["split_policy"]["target_chars"] == 2000, (lengths,)
        assert any("long_chapter_split" in c["repair"]["actions"] for c in result["chapters"]), (lengths,)


def test_long_chapter_balance_boundary_guardrails():
    # long chapter balance no safe boundaries is reported on chapter
    text, chapters = balanced_fixture([3000, 15000, 3000, 3000, 3000], separator="")
    text = text[:chapters[1]["start"]] + text[chapters[1]["start"]:chapters[1]["end"]].replace("。", "乙") + text[chapters[1]["end"]:]
    result = B.smart_repair(text, chapters, split_long_chapters=True)
    kept = result["chapters"][1]
    assert kept["chars"] == 15000
    assert "long_chapter_split_skipped" in kept["repair"]["reasons"]
    assert any(w["type"] == "long_chapter_split_skipped" for w in result["report"]["warnings"])

    # long chapter balance reduces count at safe boundaries
    text, chapters = balanced_fixture([3000, 15000, 3000, 3000, 3000], separator="", sentence_chars=7500)
    result = B.smart_repair(text, chapters, split_long_chapters=True)
    parts = [c for c in result["chapters"] if "long_split" in c]
    assert [p["chars"] for p in parts] == [7500, 7500]
    assert all("long_chapter_split_reduced" in p["repair"]["reasons"] for p in parts)


def test_long_chapter_balance_composes_with_structural_repair():
    for kind in ["gap", "range", "duplicate", "last"]:
        numbers = [1, 2, 4, 5, 6, 7] if kind == "gap" else [1, 2, 2, 3, 4, 5] if kind == "duplicate" else None
        lengths = [3000, 30000, 3000, 3000, 3000, 30000 if kind == "last" else 3000]
        text, chapters = balanced_fixture(lengths, numbers=numbers)
        if kind == "range":
            chapters[1]["range_end"] = 3
        old = B.smart_repair(text, chapters)
        result = B.smart_repair(text, chapters, split_long_chapters=True)
        assert result["status"] == "ok", (kind,)
        assert "".join(text[c["start"]:c["end"]] for c in result["chapters"]) == text, (kind,)
        assert result["final_numbers"] == list(range(1, len(result["chapters"]) + 1)), (kind,)
        old_actions = {a for c in old["chapters"] for a in c["repair"]["actions"] if a != "kept"}
        new_actions = {a for c in result["chapters"] for a in c["repair"]["actions"]}
        assert old_actions <= new_actions, (kind,)


def test_smart_clean_novel():
    text = smart_novel([(i, f"标题{i}", smart_body(30, i)) for i in range(1, 11)])
    res = smart_run(text)
    assert res["status"] == "clean"
    assert res["report"]["warnings"] == []
    assert res["report"]["removed"] == []
    assert [c["final_num"] for c in res["chapters"]] == list(range(1, 11))
    assert all(c["repair"]["actions"] == ["kept"] for c in res["chapters"])
    # lossless round-trip
    assert "".join(B.chapter_content({"text": text}, c) for c in res["chapters"]) == text
    # inputs are not mutated
    again = B.analyze_text(text)["chapters"]
    assert res["original_count"] == len(again)


def test_chapter_header_prefix_and_spacing():
    # detect prefixed and inline chapter headers
    prefix = "\u9886\u5730\u98ce\u4e91"
    text = "\n\n".join(
        [
            f"{prefix} \u7b2c\u4e00\u7ae0 \u5f00\u7bc7",
            "\u7b2c\u4e00\u7ae0\u6b63\u6587",
            f"{prefix} \u7b2c\u4e8c\u7ae0 \u8f6c\u6298",
            "\u7b2c\u4e8c\u7ae0\u6b63\u6587\uff0c"
            f"{prefix} \u7b2c\u4e09\u7ae0 \u7ed3\u5c40",
            "\u7b2c\u4e09\u7ae0\u6b63\u6587",
        ]
    )
    chapters = B.analyze_text(text)["chapters"]
    assert [c["num"] for c in chapters] == [1, 2, 3]
    assert [c["title"] for c in chapters] == [
        "\u5f00\u7bc7",
        "\u8f6c\u6298",
        "\u7ed3\u5c40",
    ]

    # detect compact prefixed headers after formatting
    text = "\n\n".join(
        [
            "\u9886\u5730\u98ce\u4e91\u7b2c\u4e00\u7ae0\u6210\u4eba\u5178\u793c",
            "\u9886\u5730\u98ce\u4e91\u7b2c\u4e8c\u7ae0\u9b54\u6cd5\u6bd4\u62fc",
            "\u6b63\u6587\u91cc\u5d4c\u5165\uff0c\u9886\u5730\u98ce\u4e91\u7b2c\u4e09\u7ae0\u51b3\u6218",
        ]
    )
    chapters = B.analyze_text(text)["chapters"]
    assert [c["num"] for c in chapters] == [1, 2, 3]
    assert [c["title"] for c in chapters] == [
        "\u6210\u4eba\u5178\u793c",
        "\u9b54\u6cd5\u6bd4\u62fc",
        "\u51b3\u6218",
    ]

    # detect attached chinese title without spacing
    text = "\n\n".join(
        [
            "\u9886\u5730\u98ce\u4e91\u7b2c\u4e94\u5341\u4e5d\u7ae0\u51fb\u6e83\u5de6\u7ffc",
            "\u9886\u5730\u98ce\u4e91\u7b2c\u516d\u5341\u7ae0\u519b\u4e2d\u6625\u8272",
        ]
    )
    chapters = B.analyze_text(text)["chapters"]
    assert [c["num"] for c in chapters] == [59, 60]
    assert [c["title"] for c in chapters] == [
        "\u51fb\u6e83\u5de6\u7ffc",
        "\u519b\u4e2d\u6625\u8272",
    ]


def test_detect_odd_chapter_header_formats():
    cases = [
        (
            [
                "\u7b2c \uff16\uff17 \u7ae0 \u5168\u5e45\u7a7a\u683c",
                "\u7b2c\uff10\uff16\uff18\u7ae0\u5168\u89d2\u6570\u5b57",
            ],
            [67, 68],
        ),
        (
            [
                "\u3010\u7b2c1\u7ae0\u3011\u65b0\u624b\u6751",
                "\uff3b\u7b2c2\u7ae0\uff3d\u5f00\u59cb\u5192\u9669",
            ],
            [1, 2],
        ),
        (
            ["\u7b2c1\u56de\u7532", "\u7b2c2\u56de\u4e59", "\u7b2c3\u56de\u4e19"],
            [1, 2, 3],
        ),
        (
            [
                "Chapter 1: One",
                "Chapter 2 - Two",
                "Ch. 3 Three",
            ],
            [1, 2, 3],
        ),
        (
            ["Chapter One: First", "Chapter Two: Second"],
            [1, 2],
        ),
        (
            ["Part 1: First", "Episode 2 - Second", "Vol. 3 Third"],
            [1, 2, 3],
        ),
        (
            [
                "Book Chapter 1: One",
                "Book Chapter 2: Two",
            ],
            [1, 2],
        ),
        (
            ["No. 1 First", "Number 2 Second", "#3 Third"],
            [1, 2, 3],
        ),
        (
            ["\u5377\u4e00\uff1a\u5f00\u7bc7", "\u5377\u4e8c - \u8f6c\u6298"],
            [1, 2],
        ),
        (
            ["[1] \u7b2c\u4e00", "[2] \u7b2c\u4e8c", "[3] \u7b2c\u4e09"],
            [1, 2, 3],
        ),
        (
            ["1\u7ae0 \u7b2c\u4e00", "2\u7ae0 \u7b2c\u4e8c", "3\u7ae0 \u7b2c\u4e09"],
            [1, 2, 3],
        ),
        (
            ["1\u3001\u7b2c\u4e00", "2. \u7b2c\u4e8c", "3 - \u7b2c\u4e09"],
            [1, 2, 3],
        ),
        (
            ["1 \u7b2c\u4e00", "2 \u7b2c\u4e8c", "3 \u7b2c\u4e09"],
            [1, 2, 3],
        ),
        (
            ["\u7b2c\u4e8c\u3007\u4e8c\u56db\u7ae0", "\u7b2c\u4e00\u4e07\u4e8c\u5343\u7ae0"],
            [2024, 12000],
        ),
    ]
    for lines, expected in cases:
        text = "\n\n".join(lines)
        assert [c["num"] for c in B.analyze_text(text)["chapters"]] == expected


def test_bare_number_headers_need_a_monotonic_sequence():
    one_header = "1\u3001\u8fd9\u662f\u6b63\u6587\n\n\u8fd9\u4e0d\u662f\u7ae0\u8282\u6807\u9898"
    assert B.analyze_text(one_header)["chapters"] == []

    two_headers = "\n\n".join(["1. \u7532", "2. \u4e59", "\u666e\u901a\u6bb5\u843d"])
    assert B.analyze_text(two_headers)["chapters"] == []


def test_smart_large_missing_range_mechanically_splits_at_paragraphs():
    text = smart_novel(
        [(i, f"\u6807\u9898{i}", smart_body(20, i)) for i in range(1, 6)]
        + [(6, "\u8d85\u957f", smart_body(300, 6))]
        + [(30, "\u540e\u7eed", smart_body(20, 30))]
    )
    res = smart_run(text)
    assert res["status"] == "ok"
    assert len(res["chapters"]) > 7
    mechanical = [
        c for c in res["chapters"] if "mechanical_split" in c["repair"]["actions"]
    ]
    assert len(mechanical) > 1
    assert any(w["type"] == "mechanical_split" for w in res["report"]["warnings"])
    assert not any(w["type"] == "inferred_split_skipped" for w in res["report"]["warnings"])
    # Every generated boundary is a paragraph gap, never the middle of a line.
    for c in mechanical[1:]:
        assert text[c["start"] : c["start"] + 2] == "\n\n"
    assert "".join(text[c["start"] : c["end"]] for c in res["chapters"]) == text


def test_smart_single_long_chapter_uses_default_paragraph_target():
    text = smart_novel([(1, "\u957f\u7bc7", smart_body(180, 1))])
    res = smart_run(text)
    assert res["status"] == "ok"
    assert len(res["chapters"]) > 1
    assert any(w["type"] == "mechanical_split" for w in res["report"]["warnings"])
    generated = [
        c for c in res["chapters"] if "mechanical_split" in c["repair"]["actions"]
    ]
    for c in generated[1:]:
        assert text[c["start"] : c["start"] + 2] == "\n\n"


def test_smart_gap_renumbers_without_splitting():
    text = smart_novel(
        [(1, "标题一", smart_body(30, 1)), (2, "标题二", smart_body(30, 2))]
        + [(i, f"标题{i}", smart_body(30, i)) for i in range(4, 11)]
    )
    res = smart_run(text)
    assert res["status"] == "ok"
    assert len(res["chapters"]) == 9  # no splitting
    assert [c["final_num"] for c in res["chapters"]] == list(range(1, 10))
    acts = {c["final_num"]: c["repair"]["actions"] for c in res["chapters"]}
    assert "gap_absorbed" in acts[3] and "renumbered" in acts[3]
    assert not any("inferred_split" in a for a in acts.values())
    assert "".join(text[c["start"]: c["end"]] for c in res["chapters"]) == text


def test_smart_duplicate_content_policy():
    # smart absorbed duplicate same content truncated
    # 1..10 with ch5 = header+body twice (the second 第5章 line is dropped by
    # the spurious filter, so the copy is absorbed inside top-level ch5).
    text = smart_novel(
        [(i, f"标题{i}", smart_body(30, i)) for i in range(1, 5)]
        + [("5", "标题5", smart_body(30, 5)), ("5", "标题5", smart_body(30, 5))]
        + [(i, f"标题{i}", smart_body(30, i)) for i in range(6, 11)]
    )
    orig = B.analyze_text(text)["chapters"]
    assert len(orig) == 10  # one 第5章 dropped by the filter -> absorbed
    res = smart_run(text)
    assert res["status"] == "ok"
    assert len(res["chapters"]) == 10
    assert [c["final_num"] for c in res["chapters"]] == list(range(1, 11))
    # the duplicate copy is removed (truncated), reported with kind=truncated
    assert len(res["report"]["removed"]) == 1
    rm = res["report"]["removed"][0]
    assert rm["kind"] == "truncated" and rm["num"] == 5
    assert any(w["type"] == "duplicate_truncated" for w in res["report"]["warnings"])
    truncated = [c for c in res["chapters"] if "duplicate_truncated" in c["repair"]["actions"]]
    assert len(truncated) == 1 and truncated[0]["final_num"] == 5
    # round-trip over the KEPT chapters: original minus the dropped tail
    ch6_start = orig[5]["start"]
    kept = "".join(text[c["start"]: c["end"]] for c in res["chapters"])
    assert kept == text[: res["chapters"][4]["end"]] + text[ch6_start:]

    # smart absorbed duplicate diff content kept
    # Same shape, but the absorbed copy has different content -> both kept,
    # split at the duplicated line, renumbered.
    text = smart_novel(
        [(i, f"标题{i}", smart_body(30, i)) for i in range(1, 5)]
        + [("5", "标题5", smart_body(30, 5)), ("5", "标题五乙", smart_body(30, "5b"))]
        + [(i, f"标题{i}", smart_body(30, i)) for i in range(6, 11)]
    )
    res = smart_run(text)
    assert res["status"] == "ok"
    assert len(res["chapters"]) == 11
    assert [c["final_num"] for c in res["chapters"]] == list(range(1, 12))
    fives = [c for c in res["chapters"] if c["repair"]["orig_num"] == 5]
    assert len(fives) == 2
    assert all("duplicate_kept" in c["repair"]["actions"] for c in fives)
    assert fives[0]["final_num"] == 5 and fives[1]["final_num"] == 6
    assert fives[1]["title"] == "标题五乙"
    assert all(c["repair"]["confidence"] == "medium" for c in fives)
    assert any(w["type"] == "duplicate_split_kept" for w in res["report"]["warnings"])
    # nothing dropped -> full round-trip
    assert "".join(text[c["start"]: c["end"]] for c in res["chapters"]) == text

    # smart toplevel duplicate dropped
    # A duplicate at the very end survives the spurious filter (the last
    # candidate is never dropped) -> handled by the top-level fingerprint
    # groups and dropped.
    text = smart_novel(
        [(i, f"标题{i}", smart_body(30, i)) for i in range(1, 4)] + [(3, "标题3", smart_body(30, 3))]
    )
    orig = B.analyze_text(text)["chapters"]
    assert len(orig) == 4  # both 第3章 present at the top level
    res = smart_run(text)
    assert res["status"] == "ok"
    assert len(res["chapters"]) == 3
    assert [c["final_num"] for c in res["chapters"]] == [1, 2, 3]
    assert len(res["report"]["removed"]) == 1
    rm = res["report"]["removed"][0]
    assert rm["kind"] == "dropped" and rm["num"] == 3
    kept = "".join(text[c["start"]: c["end"]] for c in res["chapters"])
    assert kept == text[: orig[3]["start"]]  # dropped trailing chapter excised
    assert "duplicate_kept" in res["chapters"][2]["repair"]["actions"]


def test_smart_gap_after_duplicate_only_when_real_gap():
    # [1..4, 5, 5, 6..10]: the absorbed duplicate is followed by the
    # CONSECUTIVE 6 -> no gap -> no gap_after_duplicate warning.
    text = smart_novel(
        [(i, f"标题{i}", smart_body(30, i)) for i in range(1, 5)]
        + [("5", "标题5", smart_body(30, 5)), ("5", "标题5", smart_body(30, 5))]
        + [(i, f"标题{i}", smart_body(30, i)) for i in range(6, 11)]
    )
    res = smart_run(text)
    assert not any(
        w["type"] == "gap_after_duplicate" for w in res["report"]["warnings"]
    )
    # [1..4, 5, 5, 7..10]: a real gap after the duplicate -> warning.
    text = smart_novel(
        [(i, f"标题{i}", smart_body(30, i)) for i in range(1, 5)]
        + [("5", "标题5", smart_body(30, 5)), ("5", "标题5", smart_body(30, 5))]
        + [(i, f"标题{i}", smart_body(30, i)) for i in range(7, 11)]
    )
    res = smart_run(text)
    assert any(w["type"] == "gap_after_duplicate" for w in res["report"]["warnings"])


def test_smart_long_inferred_split():
    # 1,2,4..10 with ch2 ~2.25x the others and no internal title lines:
    # inferred 1-cut split filling the missing 第3章.
    text = smart_novel(
        [(1, "标题一", smart_body(20, 1)), (2, "标题二", smart_body(45, 2))]
        + [(i, f"标题{i}", smart_body(20, i)) for i in range(4, 11)]
    )
    res = smart_run(text)
    assert res["status"] == "ok"
    assert len(res["chapters"]) == 10
    assert [c["final_num"] for c in res["chapters"]] == list(range(1, 11))
    segs = [c for c in res["chapters"] if "inferred_split" in c["repair"]["actions"]]
    assert len(segs) == 2
    assert segs[0]["repair"]["orig_num"] == 2 and segs[1]["repair"]["orig_num"] == 3
    assert segs[1]["repair"]["orig_numStr"] == "3"  # inferred number, Arabic
    assert all(s["repair"]["confidence"] == "low" for s in segs)
    # the cut sits on a paragraph boundary
    assert text[segs[1]["start"]: segs[1]["start"] + 2] == "\n\n"
    # full round-trip (nothing dropped)
    assert "".join(text[c["start"]: c["end"]] for c in res["chapters"]) == text
    # the next chapter absorbs the gap in the report
    assert "gap_absorbed" in res["chapters"][3]["repair"]["actions"]


def test_smart_idempotent_on_repaired_structure():
    text = smart_novel(
        [(1, "标题一", smart_body(20, 1)), (2, "标题二", smart_body(45, 2))]
        + [(i, f"标题{i}", smart_body(20, i)) for i in range(4, 11)]
    )
    first = smart_run(text)
    # Re-running on the repaired structure (same text, repaired chapters)
    # must find nothing to do: all kept, no warnings.
    second = B.smart_repair(text, first["chapters"])
    assert second["status"] == "clean"
    assert second["report"]["warnings"] == []
    assert all(c["repair"]["actions"] == ["kept"] for c in second["chapters"])
    assert [c["final_num"] for c in second["chapters"]] == [c["final_num"] for c in first["chapters"]]


def test_smart_long_split_fallbacks():
    # smart long no blank lines exact cut
    # A long chapter with no blank lines inside: the cut cannot snap to a
    # paragraph boundary -> exact position + mid-paragraph warning.
    paras = ["这是一部用于测试的智能识别小说。", "前言内容，若干行。", "第1章 标题1"]
    paras.extend(smart_body(20, 1))
    paras.append("第2章 标题2\n" + "\n".join(smart_body(100, 2)))  # one line-separated block
    for i in range(4, 11):
        paras.append(f"第{i}章 标题{i}")
        paras.extend(smart_body(20, i))
    text = "\n\n".join(paras)
    res = smart_run(text)
    assert res["status"] == "ok"
    assert any(w["type"] == "inferred_split_mid_paragraph" for w in res["report"]["warnings"])
    assert len(res["chapters"]) == 10
    segs = [c for c in res["chapters"] if "inferred_split" in c["repair"]["actions"]]
    assert len(segs) == 2
    assert "".join(text[c["start"]: c["end"]] for c in res["chapters"]) == text

    # smart last chapter long mechanically splits
    # A long LAST chapter has no next number to compare, so use the observed
    # average and paragraph boundaries as the mechanical fallback.
    text = smart_novel(
        [(i, f"标题{i}", smart_body(20, i)) for i in range(1, 10)]
        + [(10, "标题10", smart_body(120, 10))]
    )
    res = smart_run(text)
    assert res["status"] == "ok"
    assert any(w["type"] == "mechanical_split" for w in res["report"]["warnings"])
    assert len(res["chapters"]) > 10
    assert "".join(text[c["start"]: c["end"]] for c in res["chapters"]) == text


def test_smart_long_consecutive_numbers_silent():
    # Long chapter but the following number is consecutive (no missing
    # number): older novels simply have long chapters -> kept as-is
    # SILENTLY (no warning, status clean), never split. User refinement
    # 2026-09: a length flag without a gap is not an anomaly worth alerting.
    text = smart_novel(
        [(1, "标题一", smart_body(20, 1)), (2, "标题二", smart_body(100, 2)),
         (3, "标题三", smart_body(20, 3)), (4, "标题四", smart_body(20, 4)),
         (5, "标题五", smart_body(20, 5)), (6, "标题六", smart_body(20, 6))]
    )
    res = smart_run(text)
    assert res["status"] == "clean"
    assert res["report"]["warnings"] == []
    assert len(res["chapters"]) == 6
    assert all(c["repair"]["actions"] == ["kept"] for c in res["chapters"])
    assert "".join(text[c["start"]: c["end"]] for c in res["chapters"]) == text


def test_smart_unparseable_number_long_kept():
    # 第〇章 parses to no number: a long such chapter cannot be validated.
    paras = ["前言内容。"]
    paras.append("第1章 标题1")
    paras.extend(smart_body(20, 1))
    paras.append("第〇章 标题〇")
    paras.extend(smart_body(120, "〇"))
    for i in range(2, 11):
        paras.append(f"第{i}章 标题{i}")
        paras.extend(smart_body(20, i))
    text = "\n\n".join(paras)
    res = smart_run(text)
    assert res["status"] in ("ok", "clean")
    assert len(res["chapters"]) >= 10
    unparseable = [c for c in res["chapters"] if c["repair"]["orig_num"] is None]
    assert len(unparseable) == 1
    # it still gets a position in the 1..N renumbering
    assert [c["final_num"] for c in res["chapters"]] == list(range(1, len(res["chapters"]) + 1))


def test_smart_length_guardrails():
    # smart small book disables length
    text = smart_novel([(i, f"标题{i}", smart_body(500, i)) for i in range(1, 4)])
    res = smart_run(text)
    assert res["status"] == "ok"  # length_disabled warning -> not "clean"
    assert res["baseline_chars"] is None
    assert any(w["type"] == "length_disabled" for w in res["report"]["warnings"])
    assert "章节数少于 5 章" in next(w["detail"] for w in res["report"]["warnings"] if w["type"] == "length_disabled")

    # smart low median disables length
    # >= 5 chapters but the median is below 200 chars -> length detection off.
    text = smart_novel([(i, f"标题{i}", smart_body(5, i)) for i in range(1, 8)])
    res = smart_run(text)
    assert res["baseline_chars"] is None
    assert any(w["type"] == "length_disabled" for w in res["report"]["warnings"])
    assert "中位数过低" in next(w["detail"] for w in res["report"]["warnings"] if w["type"] == "length_disabled")


def test_smart_chinese_numerals():
    # 一,二,五,六..十 with 二 abnormally long -> inferred segments 3,4 fill the
    # gap; Chinese numbers parse via parse_chapter_number.
    paras = ["前言一。", "前言二。"]
    paras.append("第一章 标题甲")
    paras.extend(smart_body(20, "甲"))
    paras.append("第二章 标题乙")
    paras.extend(smart_body(45, "乙"))
    for cn in ["五", "六", "七", "八", "九", "十"]:
        paras.append(f"第{cn}章 标题{cn}")
        paras.extend(smart_body(20, cn))
    text = "\n\n".join(paras)
    res = smart_run(text)
    assert res["status"] == "ok"
    assert len(res["chapters"]) == 10
    assert [c["final_num"] for c in res["chapters"]] == list(range(1, 11))
    segs = [c for c in res["chapters"] if "inferred_split" in c["repair"]["actions"]]
    assert [s["repair"]["orig_num"] for s in segs] == [2, 3, 4]
    assert [s["repair"]["orig_numStr"] for s in segs] == ["二", "3", "4"]


def test_smart_filename_contract():
    # smart filename widths
    # N <= 1000 -> 3-digit width (001); N > 1000 -> 4-digit (0001).
    names = B.make_smart_filenames([{"final_num": i, "title": f"标题{i}"} for i in range(1, 1001)])
    assert names[0] == "第 001 章 标题1.txt"
    assert names[-1] == "第 1000 章 标题1000.txt"
    names = B.make_smart_filenames([{"final_num": i, "title": f"标题{i}"} for i in range(1, 1002)])
    assert names[0] == "第 0001 章 标题1.txt"
    assert names[-1] == "第 1001 章 标题1001.txt"
    # empty title -> no title part
    assert B.make_smart_filenames([{"final_num": 1, "title": ""}]) == ["第 001 章.txt"]
    assert B.make_smart_filenames([{"final_num": 7, "title": "  " }]) == ["第 007 章.txt"]

    # smart filenames unique and sanitized
    # final_num is unique by construction, but the de-dup pass must keep the
    # list unique even when sanitizing collapses names.
    chs = [
        {"final_num": 1, "title": "甲/b"},   # -> 甲_b
        {"final_num": 2, "title": "甲_b"},   # sanitizes to the same title,
        # but a different number prefix keeps the full names unique
    ]
    names = B.make_smart_filenames(chs)
    assert names == ["第 001 章 甲_b.txt", "第 002 章 甲_b.txt"]
    assert len(set(names)) == len(names)
    # illegal characters are replaced
    names = B.make_smart_filenames([{"final_num": 1, "title": 'a/b\\c:d*e'}])
    assert all(ch not in names[0] for ch in '\\/:*?')
    assert names[0].endswith(".txt")


def test_smart_range_title_policy():
    # smart range headers fill numbers without fake titles
    def block(start: int, end: int) -> str:
        header = f"\u7b2c{start}\u7ae0-\u7b2c{end}\u7ae0"
        body = "\n\n".join(f"\u8fd9\u662f\u8303\u56f4\u6bb5\u843d{i}" + "\u7532" * 180 for i in range(12))
        return header + "\n\n" + body

    # The second block intentionally omits the second 第 marker: both forms
    # occur in downloaded ebook sources.
    text = block(1, 3) + "\n\n" + block(4, 6).replace("\u7b2c6\u7ae0", "6\u7ae0")
    chapters = B.analyze_text(text)["chapters"]
    result = B.smart_repair(text, chapters)

    assert result["status"] == "ok"
    assert [c["final_num"] for c in result["chapters"]] == list(range(1, 7))
    assert all(c["title"] == "" for c in result["chapters"])
    assert all("range_split" in c["repair"]["actions"] for c in result["chapters"])

    # smart range keeps real title inside range
    text = (
        "\u7b2c1\u7ae0-\u7b2c3\u7ae0\n\n"
        + "\n\n".join("\u7532" * 180 for _ in range(12))
        + "\n\n\u7b2c3\u7ae0\u771f\u5b9e\u6807\u9898\n\n"
        + "\n\n".join("\u4e59" * 180 for _ in range(12))
        + "\n\n\u7b2c4\u7ae0\u4e0b\u4e00\u7ae0\n\n"
        + "\n\n".join("\u4e19" * 180 for _ in range(12))
    )
    result = B.smart_repair(text, B.analyze_text(text)["chapters"])

    assert result["status"] == "ok"
    assert [c["final_num"] for c in result["chapters"]] == [1, 2, 3, 4]
    assert result["chapters"][2]["title"] == "\u771f\u5b9e\u6807\u9898"


def test_smart_fingerprint_ignores_whitespace():
    assert B._normalized_fingerprint("甲 乙\n丙") == B._normalized_fingerprint("甲乙　丙\r\n")
    assert B._normalized_fingerprint("甲乙") != B._normalized_fingerprint("甲乙丙")


def test_smart_cross_number_collision_warns_only():
    # Two chapters with identical content but different numbers: the header
    # embeds the number, so this is only reachable when a chapter's content
    # matches another's exactly (constructed here via an explicit chapters
    # list). Warn, never drop.
    half = "段落内容甲。\n段落内容乙。\n" * 3
    text = half + half
    res = B.smart_repair(
        text,
        [
            {"seq": 1, "start": 0, "end": len(half), "numStr": "1", "num": 1, "title": "一", "chars": 0},
            {"seq": 2, "start": len(half), "end": len(text), "numStr": "2", "num": 2, "title": "二", "chars": 0},
        ],
    )
    assert any(w["type"] == "content_collision" for w in res["report"]["warnings"])
    assert len(res["chapters"]) == 2  # both kept
    assert res["report"]["removed"] == []


def test_smart_round_trip_and_tiling_invariants():
    # Every non-error result must tile [0, len] with kept chapters + removed
    # spans and renumber 1..N — checked across all the fixture shapes above.
    cases = [
        smart_novel([(i, f"标题{i}", smart_body(30, i)) for i in range(1, 11)]),
        smart_novel(
            [(1, "标题一", smart_body(30, 1)), (2, "标题二", smart_body(30, 2))]
            + [(i, f"标题{i}", smart_body(30, i)) for i in range(4, 11)]
        ),
        smart_novel(
            [(i, f"标题{i}", smart_body(30, i)) for i in range(1, 5)]
            + [("5", "标题5", smart_body(30, 5)), ("5", "标题5", smart_body(30, 5))]
            + [(i, f"标题{i}", smart_body(30, i)) for i in range(6, 11)]
        ),
        smart_novel(
            [(1, "标题一", smart_body(20, 1)), (2, "标题二", smart_body(45, 2))]
            + [(i, f"标题{i}", smart_body(20, i)) for i in range(4, 11)]
        ),
    ]
    for text in cases:
        orig = B.analyze_text(text)["chapters"]
        res = smart_run(text, orig)
        assert res["status"] in ("ok", "clean")
        n = len(res["chapters"])
        assert [c["final_num"] for c in res["chapters"]] == list(range(1, n + 1))
        for c in res["chapters"]:
            assert c["end"] > c["start"]
        # kept chapters + removed spans must tile [0, len(text)] exactly.
        # (Kept chapters alone are NOT contiguous after a truncation: the
        # removed tail sits between the truncated chapter and the next one.)
        # removed entries keep the ORIGINAL seq (recorded before step-5
        # renumbers seq 1..N), so the original chapters list resolves spans.
        orig_by_seq = {c["seq"]: c for c in orig}
        spans = sorted((c["start"], c["end"]) for c in res["chapters"])
        for r in res["report"]["removed"]:
            oc = orig_by_seq[r["seq"]]
            if r["kind"] == "dropped":
                spans.append((oc["start"], oc["end"]))
            else:  # truncated: removed span = kept chapter's excised tail
                kept_end = next(
                    c["end"] for c in res["chapters"]
                    if "duplicate_truncated" in c["repair"]["actions"]
                )
                spans.append((kept_end, oc["end"]))
        spans.sort()
        assert spans[0][0] == 0
        assert spans[-1][1] == len(text)
        assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
        # excision round-trip: dropping the removed spans from the text must
        # leave exactly the kept chapters, concatenated.
        excised = text
        for s, e in sorted(spans, reverse=True):
            if not any((s, e) == (c["start"], c["end"]) for c in res["chapters"]):
                excised = excised[:s] + excised[e:]
        assert excised == "".join(text[c["start"]: c["end"]] for c in res["chapters"])


# --------------------------------------------------------------------------- #
# Length-based splitting (chapter-less fallback: split_by_length)
# --------------------------------------------------------------------------- #

def test_length_split_paragraph_balancing():
    # length split even division uses paragraph bounds
    # 31 paragraphs x 200 chars = 6200 chars. Target 3000 -> wanted =
    # round(6200/3000) = 2 segments near 3100 each — never a short 200-char tail.
    text = "\n\n".join("甲" * 200 for _ in range(31))
    res = B.split_by_length(text, target_chars=3000)
    assert res["status"] == "ok"
    assert res["target"] == 3000
    assert res["segment_count"] == 2
    segs = res["segments"]
    assert segs[0]["start"] == 0
    assert segs[-1]["end"] == len(text)
    assert all(a["end"] == b["start"] for a, b in zip(segs, segs[1:]))
    assert sum(s["chars"] for s in segs) == 6200
    # no short tail: both segments stay far above half the target
    assert all(s["chars"] >= 2800 for s in segs)
    # cuts land on paragraph gaps (this text has plenty of them)
    for s in segs[1:]:
        assert text[s["start"]: s["start"] + 2] == "\n\n"
    # lossless tiling
    assert "".join(text[s["start"]: s["end"]] for s in segs) == text
    assert res["warnings"] == []

    # length split even division 5800
    # 29 paragraphs x 200 = 5800 -> wanted = round(5800/3000) = 2 (~2900 each).
    text = "\n\n".join("甲" * 200 for _ in range(29))
    res = B.split_by_length(text, target_chars=3000)
    assert res["status"] == "ok"
    assert res["segment_count"] == 2
    chars = [s["chars"] for s in res["segments"]]
    assert sum(chars) == 5800
    assert all(c >= 2600 for c in chars)


def test_length_split_sentence_fallback():
    # length split single paragraph splits at sentence ends
    # One giant paragraph, no blank lines: 310 sentences of 20 chars = 6200.
    # The paragraph tier is empty, so cuts must fall on sentence ends — and
    # never mid-sentence: each cut is right after 。.
    text = ("甲" * 19 + "。") * 310
    res = B.split_by_length(text, target_chars=3000)
    assert res["status"] == "ok"
    assert res["segment_count"] == 2
    assert [s["chars"] for s in res["segments"]] == [3100, 3100]
    for s in res["segments"]:
        if s["end"] < len(text):
            assert text[s["end"] - 1] in "。！？…"
        if s["start"] > 0:
            assert text[s["start"] - 1] in "。！？…"
    assert "".join(text[s["start"]: s["end"]] for s in res["segments"]) == text

    # length split falls back to sentences only where paragraphs lack
    # Two paragraphs (1 internal gap); wanted = round(5000/1000) = 5 needs 4
    # cuts, so 3 come from sentence ends inside the long paragraphs.
    p1 = ("甲" * 99 + "。") * 20  # 2000 chars, single line
    p2 = ("乙" * 99 + "。") * 30  # 3000 chars, single line
    text = p1 + "\n\n" + p2
    res = B.split_by_length(text, target_chars=1000)
    assert res["status"] == "ok"
    assert res["segment_count"] == 5
    segs = res["segments"]
    assert sum(s["chars"] for s in segs) == 5000
    # every cut is on a legal boundary: a paragraph gap or right after a
    # sentence-ending character; no segment is cut mid-sentence.
    for s in segs[1:]:
        at_gap = text[s["start"]: s["start"] + 2] == "\n\n"
        after_sentence = text[s["start"] - 1] in "。！？…"
        assert at_gap or after_sentence, f"cut at {s['start']} is not on a boundary"
    assert "".join(text[s["start"]: s["end"]] for s in segs) == text
    # near-even: no segment strays far from 5000/5
    assert all(abs(s["chars"] - 1000) <= 200 for s in segs)


def test_length_split_boundary_shortage():
    # length split reduces count when boundaries run short
    # 4 paragraphs x 1200 filler chars (no punctuation anywhere): only 3 legal
    # cut points exist, so wanted = round(4800/1000) = 5 must degrade to 4.
    text = "\n\n".join("甲" * 1200 for _ in range(4))
    res = B.split_by_length(text, target_chars=1000)
    assert res["status"] == "ok"
    assert res["segment_count"] == 4
    assert all(s["chars"] == 1200 for s in res["segments"])
    assert [w["type"] for w in res["warnings"]] == ["length_split_reduced"]
    assert "5 册降为 4 册" in res["warnings"][0]["detail"]

    # length split without any boundary degrades to whole book
    text = "甲" * 6200  # one paragraph, no sentence punctuation at all
    res = B.split_by_length(text, target_chars=3000)
    assert res["status"] == "ok"
    assert res["segment_count"] == 1
    assert res["segments"] == [{"seq": 1, "start": 0, "end": len(text), "chars": 6200}]
    assert [w["type"] for w in res["warnings"]] == ["length_split_degraded"]

    # length split small text is single segment
    text = "这是一本很短的小说，只有几百字而已。"
    res = B.split_by_length(text)
    assert res["status"] == "ok"
    assert res["target"] == 3000  # default target
    assert res["segment_count"] == 1
    assert res["warnings"] == []
    assert res["segments"][0]["end"] == len(text)


def test_length_split_invalid_inputs():
    # length split invalid target falls back to default
    text = "甲" * 300
    for bad in (None, 0, -5, "abc"):
        res = B.split_by_length(text, bad)
        assert res["target"] == B.DEFAULT_LENGTH_TARGET_CHARS == 3000
        assert res["status"] == "ok"

    # length split empty text errors
    for text in ("", "\n\n", "\r\n\r\n"):
        res = B.split_by_length(text)
        assert res["status"] == "error"
        assert res["error"]
        assert res["segments"] == []
        assert res["segment_count"] == 0


def test_long_chapter_balance_reserves_later_boundaries():
    for separator in ["\n\n", "\r\n\r\n", "\n\n\n\n"]:
        normal = ("甲" * 99 + "。") * 60
        long_body = separator.join(char * length for char, length in zip("乙丙丁戊", [1000, 1000, 23000, 1000]))
        text = separator.join(f"第{i}章 标题{i}{separator}{long_body if i == 2 else normal}{separator}" for i in range(1, 6))
        raw = B.analyze_text(text)
        assert len(raw["chapters"]) == 5, (separator,)
        result = B.smart_repair(text, raw["chapters"], split_long_chapters=True)
        parts = [c for c in result["chapters"] if "long_split" in c]
        assert len(parts) == 4, (separator,)
        assert all(c["chars"] > 0 and c["long_split"]["segment_count"] == 4 for c in parts), (separator,)
        assert all("long_chapter_split_skipped" not in c["repair"]["actions"] for c in parts), (separator,)
        assert "".join(text[c["start"]:c["end"]] for c in result["chapters"]) == text, (separator,)


def test_length_split_reserves_boundaries_for_shared_by_length_mode():
    for separator in ["\n\n", "。", "\n\n\n\n"]:
        text = separator.join(char * length for char, length in zip("甲乙丙丁戊", [1000, 1000, 1000, 22000, 1000]))
        result = B.split_by_length(text, 6000)
        assert result["segment_count"] == 4, (separator,)
        assert all(c["chars"] > 0 for c in result["segments"]), (separator,)
        assert "".join(text[c["start"]:c["end"]] for c in result["segments"]) == text, (separator,)


def test_length_split_crlf_gap_and_lone_carriage_return():
    # CRLF paragraph gap: recognized as a paragraph boundary; cuts land
    # inside the gap; tiling stays lossless.
    crlf_text = "\r\n\r\n".join("甲" * 200 for _ in range(31))
    res = B.split_by_length(crlf_text, target_chars=3000)
    assert res["status"] == "ok"
    assert res["segment_count"] == 2
    for s in res["segments"][1:]:
        assert crlf_text[s["start"]: s["start"] + 4] == "\r\n\r\n"
    assert "".join(crlf_text[s["start"]: s["end"]] for s in res["segments"]) == crlf_text
    # A lone \r also counts as a line break: 310 \r-joined sentences of 20
    # chars (6200 chars, no blank lines) still split evenly at sentence ends.
    cr_text = "\r".join(("乙" * 19 + "。") for _ in range(310))
    res_cr = B.split_by_length(cr_text, target_chars=3000)
    assert res_cr["status"] == "ok"
    assert res_cr["segment_count"] == 2
    for s in res_cr["segments"]:
        if s["end"] < len(cr_text):
            assert cr_text[s["end"] - 1] in "。！？…"
    assert "".join(cr_text[s["start"]: s["end"]] for s in res_cr["segments"]) == cr_text


def test_length_split_segment_naming_uses_smart_convention():
    # The worker names by-length segments through make_smart_filenames with
    # empty titles: 第 001 章.txt ... — and the names must pass the generated
    # split-output check so downstream stages accept them.
    res = B.split_by_length("\n\n".join("丁" * 99 + "。" for _ in range(100)), target_chars=500)
    assert res["status"] == "ok"
    names = B.make_smart_filenames([{"final_num": s["seq"], "title": ""} for s in res["segments"]])
    assert names[0] == "第 001 章.txt"
    assert names[1] == "第 002 章.txt"
    assert all(B.is_generated_split_output_name(n) for n in names)
    assert len(names) == res["segment_count"]
