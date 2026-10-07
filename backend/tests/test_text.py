"""Invariant tests for the text-formatting engine (``backend/engines/text.py``).

A faithful port of ``TextFormatter/engine.test.js``: the same inputs, the same
expected outputs, and — most importantly — the two invariants that guard the
engine: **content preservation** (punctuation-normalized content is never lost or
reordered) and **idempotency** (formatting twice == formatting once).

``TextConfig()``'s defaults are identical to the JS test's ``CFG``.
"""
from __future__ import annotations

import re

from backend.core.config import TextConfig
from backend.engines.text import apply_punct, format_text, is_chapter_title, normalize_line

CFG = TextConfig()
CFG_NOSB = CFG.model_copy(update={"sentence_break": False})


def test_format_progress_reports_real_work_without_changing_output():
    text = "第1章 开篇\n" + "这是正文。\n" * 100
    values = []
    result = format_text(text, CFG, on_progress=values.append)
    assert result == format_text(text, CFG)
    assert values == sorted(values)
    assert values[-1] == 1
    assert len({v for v in values if 0 < v < 1}) > 10

# The sample exactly as authored in TextFormatter/index.html (tabs preserved).
SAMPLE = (
    "   第一章   雪   夜\n"
    "\n"
    "\n"
    "\t北风  呼啸，  夜色  沉沉...\n"
    "\n"
    "   林 风 裹紧  大衣，  快步 穿过 积雪 的小巷。  他  今天 心情 很 糟，，  方案 又 被 打回 了。\n"
    "\n"
    "\"你 怎么 才 来 ？？\"  身后 传来 一个 清冷 的 声音。\n"
    "\n"
    "林 风 停下 脚步，  缓缓 转身。\n"
    "\n"
    "\"苏 婉。\"  他 轻声 说。\n"
    "\n"
    "\"嗯，，  我 等 了 你 一 整 天。\"  苏 婉 抱着 手臂，  呼出 的 白气 很快 消散 在 冷风 里。\n"
    "\n"
    "\n"
    "她 走到 他 身边，  两人 沉默 着 并肩 向前 走 去。\n"
    "\n"
    "\t...\n"
    "\n"
    "\"下 周 三，，  公司 会 重新 评审。\"  她 忽然 开口，\"我 听 说 新 总监 很 看重 你 的 项目。!!\"\n"
    "\n"
    "林 风 微微 一 怔。\n"
    "\n"
    "\"真的 ？？\"\n"
    "\n"
    "\"我 不 骗 你。\"\n"
    "\n"
    "\n"
    "   第二 章   重  审\n"
    "\n"
    "会议室 里 很 安静，  长桌 尽头 坐 着 那位 新 总监。"
)

# Inputs used by both the preservation and idempotency invariants.
PRESERVE_INPUTS = [
    "你好，世界！\n\n这是第二行。",
    "他...说，,好的??",
    "Chapter 1: The Beginning\n\nIt was a cold day.",
    '"你好"\n"再见"\n走了',
    "第一场雪 很大\n\n我们 去 玩",
    "   \t 前后空白  ",
    "。。。,,,!!",
    "数字 123 和 3.14 与 URL http://a.com?x=1&y=2",
    '他说："你好。"\n\n\n\n然后离开了。',
]


def content_of(inp: str, cfg) -> str:
    """Punctuation-normalized, whitespace-free content of the input — the
    reference the engine's output must match (mirrors the JS ``contentOf``)."""
    text = re.sub(r"\r\n?", "\n", inp)
    text = re.sub(r"\r", "\n", text)
    out = ""
    for raw in text.split("\n"):
        out += re.sub(r"\s", "", apply_punct(normalize_line(raw, cfg), cfg))
    return out


# --------------------------------------------------------------------------- #
# Whitespace / punctuation rules
# --------------------------------------------------------------------------- #

def test_whitespace_and_punctuation():
    # whitespace
    assert format_text("  你好\t\n\n\n\n  世界  \n", CFG)["text"] == "你好\n\n世界"

    # ellipsis
    assert format_text("他想...然后走开", CFG)["text"] == "他想……然后走开"
    assert format_text("他说了。。。", CFG)["text"] == "他说了……"
    assert format_text("四个点....", CFG)["text"] == "四个点……"

    # repeated punct
    assert format_text("好,,真的!!这样??", CFG)["text"] == "好，真的！这样？"

    # single ascii punct preserved
    assert format_text("a.b, c! d?", CFG)["text"] == "a.b,c!d?"


# --------------------------------------------------------------------------- #
# Dialogue / narration / sentence-boundary
# --------------------------------------------------------------------------- #

def test_dialogue_and_sentence_boundaries():
    # dialogue
    assert (format_text('"你来了。"\n"嗯。"\n"最近怎么样？"\n', CFG)["text"]
            == '"你来了。"\n\n"嗯。"\n\n"最近怎么样？"')
    assert (format_text('"你来了。"李明说道。\n\n"嗯。"\n\n他转身走了出去。\n', CFG)["text"]
            == '"你来了。"李明说道。\n\n"嗯。"\n\n他转身走了出去。')

    # narration merge
    assert format_text("他走了一段路，\n感觉非常疲惫。\n", CFG)["text"] == "他走了一段路，感觉非常疲惫。"

    # sentence boundary breaking
    assert (format_text("他走了。\n她哭了。\n天黑了。", CFG)["text"]
            == "他走了。\n\n她哭了。\n\n天黑了。")
    assert (format_text("他抬起头，\n看向远方。\n她低着头。", CFG)["text"]
            == "他抬起头，看向远方。\n\n她低着头。")
    assert (format_text("快看！\n好可怕？\n这……", CFG)["text"]
            == "快看！\n\n好可怕？\n\n这……")

    # sentence boundary off merges
    assert format_text("他走了。\n她哭了。\n天黑了。", CFG_NOSB)["text"] == "他走了。她哭了。天黑了。"

    # blank separated lines not affected by sentence break
    assert format_text("他说。\n\n她走了。", CFG)["text"] == "他说。\n\n她走了。"


# --------------------------------------------------------------------------- #
# Chapter detection
# --------------------------------------------------------------------------- #

def test_chapter_formatting():
    # chapter isolation
    assert (format_text("夜色沉沉。\n第一章 雪夜\n他推开了门。\n", CFG)["text"]
            == "夜色沉沉。\n\n第一章 雪夜\n\n他推开了门。")

    # chapter header spacing survives when detection disabled
    cfg = CFG.model_copy(update={"detect_chapters": False})
    text = "\u9886\u5730\u98ce\u4e91 \u7b2c\u4e00\u7ae0 \u6210\u4eba\u5178\u793c\uff08\u4e0a\uff09"
    assert format_text(text, cfg)["text"] == text

    # chapter number title space
    # 章节号与章节名紧贴 → 中间补一个空格
    assert format_text("第十九章神秘分阁主", CFG)["text"] == "第十九章 神秘分阁主"
    assert format_text("第19节 开场", CFG)["text"] == "第19节 开场"
    assert format_text("【第68章】标题", CFG)["text"] == "【第68章】 标题"
    # 已有空格/分隔符 → 保持原样，不叠加
    assert format_text("第十九章 神秘分阁主", CFG)["text"] == "第十九章 神秘分阁主"
    assert format_text("第十九章：神秘分阁主", CFG)["text"] == "第十九章：神秘分阁主"
    # 无章节名 → 不产生尾随空格
    assert format_text("第十九章", CFG)["text"] == "第十九章"
    # 幂等：再过一遍不变
    once = format_text("第十九章神秘分阁主", CFG)["text"]
    assert format_text(once, CFG)["text"] == once


def test_compound_chapter_titles_preserve_pacing_spaces():
    expected = "第一卷 异界的兽医 第三章 给巨龙放血"
    for source in [
        "第一卷 异界的兽医第三章给巨龙放血",
        "第一卷 异界的兽医 第三章 给巨龙放血",
        "第一卷　异界的兽医　第三章　给巨龙放血",
        "第 一 卷 异界的兽医 第 三 章 给巨龙放血",
    ]:
        result = format_text(source, CFG)["text"]
        assert result == expected
        assert format_text(result, CFG)["text"] == result
    assert format_text("第十九章 神秘 分阁主", CFG)["text"] == "第十九章 神秘 分阁主"
    assert format_text("书名 第三章 给巨龙放血", CFG)["text"] == "书名 第三章 给巨龙放血"
    assert format_text("第一章 第二章之后的故事", CFG)["text"] == "第一章 第二章之后的故事"
    duplicate = "第一卷 异界的兽医第三章给巨龙放血\n\n" + expected + "\n正文。"
    result = format_text(duplicate, CFG)
    assert result["text"] == expected + "\n\n正文。"
    assert result["stats"]["chapters"] == 1


def test_episode_quoted_title_formatting():
    for header in ['第四话「师傅」', '第４话『师傅』', '第四話「師傅」']:
        assert is_chapter_title(header)
        source = f"上一段尚未结束\n{header}\n他推开了门。"
        expected_header = header.replace('话', '话 ').replace('話', '話 ')
        once = format_text(source, CFG)["text"]
        assert once == f"上一段尚未结束\n\n{expected_header}\n\n他推开了门。"
        assert format_text(once, CFG)["text"] == once
    assert is_chapter_title('书名 第四话「师傅」')
    assert is_chapter_title('第四话')
    assert not is_chapter_title('第四话还没看完')
    assert not is_chapter_title('他说第四话「师傅」很好看')


def test_special_quoted_chapter_headers_are_isolated():
    for header in ['序章', '外传「格雷拉特家的母亲」', '外傳『母親』', '番外「朋友」']:
        assert is_chapter_title(header)
        result = format_text(f"前文未完\n{header}\n后文开始。", CFG)
        assert result["text"] == f"前文未完\n\n{header}\n\n后文开始。"
        assert result["stats"]["chapters"] == 1
    assert not is_chapter_title('外传说的是母亲的故事。')


def test_adjacent_duplicate_chapter_titles_are_removed_during_formatting():
    headers = [
        "第一卷 异界的兽医第一章穿越了", "第一章 雪夜",
        '第四话「师傅」', '外传「格雷拉特家的母亲」', "序章",
        "书名 第一章 雪夜", "Chapter 1: First",
    ]
    for header in headers:
        for separator in ["\n", "\n\n", "\r\n\r\n"]:
            single = f"{header}\n正文不删除。"
            source = separator.join([header, f"　{header}　", header, "正文不删除。"])
            result = format_text(source, CFG)
            assert result["text"] == format_text(single, CFG)["text"]
            assert result["stats"]["chapters"] == 1
            assert format_text(result["text"], CFG) == result


def test_duplicate_title_removal_preserves_body_and_distinct_headers():
    for source in [
        "第一章 雪夜\n正文。\n第一章 雪夜\n更多正文。",
        "第一章 雪夜\n第一章 另一标题\n正文。",
        "第一章 雪夜\n第二章 雪夜\n正文。",
        "重复正文\n重复正文",
    ]:
        assert re.sub(r"\s", "", format_text(source, CFG)["text"]) == content_of(source, CFG)
    source = "第一章 雪夜\n\n第一章 雪夜\n正文。"
    disabled = CFG.model_copy(update={"detect_chapters": False})
    assert re.sub(r"\s", "", format_text(source, disabled)["text"]) == content_of(source, disabled)


def test_chapter_title_recognition():
    # chapter title positive
    for s in ["第一章", "第1章", "第一章 xxx", "Chapter 1", "Chapter 1: Sub",
              "卷一", "楔子", "第1回", "尾声", "第 ６７ 章 标题",
              "【第68章】标题", "第一卷", "Book 第69章 标题", "Ch. 70 Final",
              "Chapter One: First", "Part 71: Next", "No. 72 Closing"]:
        assert is_chapter_title(s), s

    # chapter title negative
    for s in ["第一场雪", "第一天", "他走了",
              "第一节课开始了，同学们都安静下来，等待老师走进教室。"]:
        assert not is_chapter_title(s), s


# --------------------------------------------------------------------------- #
# Config variants
# --------------------------------------------------------------------------- #

def test_config_variants():
    c = CFG.model_copy(update={"keep_single_space": True})
    assert format_text("hello   world\n\na\t\tb", c)["text"] == "hello world\n\na b"

    c = CFG.model_copy(update={"dialogue_separate": False})
    assert format_text('"你来了。"\n"嗯。"', c)["text"] == '"你来了。""嗯。"'

    c = CFG.model_copy(update={"detect_chapters": False})
    assert format_text("第一章 雪夜\n他推门。", c)["text"] == "第一章 雪夜他推门。"

    c = CFG.model_copy(update={"punct_lone_ascii": True})
    assert format_text("你好,世界!对吧?", c)["text"] == "你好，世界！对吧？"
    assert format_text("看 http://a.com?x=1", c)["text"] == "看http://a.com?x=1"

    c = CFG.model_copy(update={"punct_quotes": True})
    assert format_text('他说"你好"然后"再见"', c)["text"] == "他说“你好”然后“再见”"

    c = CFG.model_copy(update={"punct_dash": True})
    assert format_text("他走了--没有回头", c)["text"] == "他走了——没有回头"


# --------------------------------------------------------------------------- #
# The two safety-net invariants
# --------------------------------------------------------------------------- #

def test_format_preserves_content_and_is_idempotent():
    # content preservation
    for inp in PRESERVE_INPUTS:
        out = format_text(inp, CFG)["text"]
        assert content_of(inp, CFG) == re.sub(r"\s", "", out), repr(inp)

    # idempotency
    for inp in PRESERVE_INPUTS:
        once = format_text(inp, CFG)["text"]
        twice = format_text(once, CFG)["text"]
        assert once == twice, (repr(once), repr(twice))


# --------------------------------------------------------------------------- #
# End-to-end sample
# --------------------------------------------------------------------------- #

def test_sample_end_to_end():
    r = format_text(SAMPLE, CFG)["text"]
    assert r.startswith("第一章 雪 夜\n\n")
    for bad in ("...", "。。", "，，", "??", "!!", "  ", "\t"):
        assert bad not in r, (bad, r)
    assert not re.search(r"^( )", r, re.MULTILINE)
    assert "第二章 重 审" in r and "第一章 雪 夜" in r
    assert content_of(SAMPLE, CFG) == re.sub(r"\s", "", r)
