"""Offline tests for the numbered-unit parse protocol (``backend/engines/script_units.py``).

Pure functions only — no LLM. The sample chunk is the worked example from the bundled
parse prompt, so the expected entries below are what the legacy JSON protocol produces.
"""
from __future__ import annotations

import pytest

from backend.engines.script import _skeleton
from backend.engines.script_units import (
    NARRATOR,
    assemble,
    likely_dialogue_numbers,
    parse_unit_reply,
    render_units,
    segment_chunk,
    validate_edit,
)

LQ, RQ = "“", "”"
CL, CR = "「", "」"
NARR = "平稳中性的叙述语气。"

SAMPLE = "\n\n".join([
    "第十二章 夜雨",
    "雨下了一整夜。",
    "姜维把车停在巷口，回头看了一眼。",
    f"{LQ}耗子，我跟小雪去买点东西，你跟车里等会儿吧。{RQ}",
    f"{LQ}行，快去快回。{RQ}任昊打了个哈欠。",
    f"董雪捂着手臂，疼得直吸气：{LQ}嗯……疼……{RQ}",
    f"{LQ}别出声，{RQ}姜维压低声音说，{LQ}有人。{RQ}",
    f"他心想：{LQ}这下麻烦了。{RQ}",
])


def _texts(units):
    return [u.text for u in units]


def test_segment_is_lossless_and_numbered():
    units = segment_chunk(SAMPLE)
    assert "".join(_texts(units)) == SAMPLE.replace("\n\n", "")
    assert _texts(units) == [
        "第十二章 夜雨",
        "雨下了一整夜。",
        "姜维把车停在巷口，回头看了一眼。",
        f"{LQ}耗子，我跟小雪去买点东西，你跟车里等会儿吧。{RQ}",
        f"{LQ}行，快去快回。{RQ}",
        "任昊打了个哈欠。",
        "董雪捂着手臂，疼得直吸气：",
        f"{LQ}嗯……疼……{RQ}",
        f"{LQ}别出声，{RQ}",
        "姜维压低声音说，",
        f"{LQ}有人。{RQ}",
        "他心想：",
        f"{LQ}这下麻烦了。{RQ}",
    ]
    assert [u.n for u in units] == list(range(1, 14))
    assert units[0].kind == "title"
    assert all(u.para_start for u in (units[0], units[1], units[3], units[4]))
    assert not units[5].para_start
    assert likely_dialogue_numbers(units) == [4]  # only the line that is one whole quote


def test_render_marks_paragraphs():
    out = render_units(segment_chunk(SAMPLE)).splitlines()
    assert out[0] == "[1] 第十二章 夜雨"
    assert out[1] == "" and out[2] == "[2] 雨下了一整夜。"


@pytest.mark.parametrize("pair", [
    (LQ, RQ), (CL, CR), ("『", "』"), ("‘", "’"), ('"', '"'),
])
def test_segment_each_quote_pair(pair):
    o, c = pair
    text = f"他说：{o}走吧。{c}然后离开了。"
    units = segment_chunk(text)
    assert "".join(_texts(units)) == text
    quote = [u for u in units if u.kind == "quote"]
    assert len(quote) == 1 and quote[0].lead == o and quote[0].trail == c


def test_segment_nested_quote_stays_in_one_span():
    text = f"{LQ}他说{CL}不行{CR}，我才不信。{RQ}"
    units = segment_chunk(text)
    assert len(units) == 1 and units[0].kind == "quote"
    assert units[0].lead == LQ and units[0].trail == RQ


def test_segment_unterminated_quote_carries_to_closing_line():
    units = segment_chunk(f"{LQ}第一段还没说完，\n\n后面接着说完了。{RQ}\n\n他点了点头。")
    assert [(u.lead, u.trail) for u in units] == [(LQ, ""), ("", RQ), ("", "")]
    assert units[1].kind == "quote" and units[2].kind == "plain"


def test_segment_new_opening_quote_ends_unclosed_one():
    units = segment_chunk(f"{LQ}第一段没有闭合，\n\n{LQ}第二段说完了。{RQ}")
    assert [(u.lead, u.trail) for u in units] == [(LQ, ""), (LQ, RQ)]


def test_segment_carry_gives_up_after_three_lines():
    text = f"{LQ}开头没闭合\n\n" + "\n\n".join(f"旁白第{i}行。" for i in range(1, 6))
    units = segment_chunk(text)
    assert [u.kind for u in units] == ["quote", "quote", "quote", "quote", "plain", "plain"]


def test_segment_splits_long_quote_and_keeps_edges():
    sentence = "这是一句相当长的话，需要被拆开。"
    text = LQ + sentence * 8 + RQ
    units = segment_chunk(text, unit_max_chars=40)
    assert "".join(_texts(units)) == text
    assert len(units) > 1
    assert units[0].lead == LQ and units[0].trail == ""
    assert units[-1].trail == RQ and units[-1].lead == ""
    assert all(len(u.text) <= 40 for u in units)


def test_segment_hides_punctuation_only_fragment():
    units = segment_chunk(f"{LQ}好{RQ}。他走了。")
    assert _texts(units) == [f"{LQ}好{RQ}", "。", "他走了。"]
    assert [u.n for u in units] == [1, 0, 2]


def test_parse_reply_basic_and_range_and_noise():
    reply = (
        "```\n4 姜维 | 随意放松的聊天语气\n[5] 任昊 | 懒洋洋地拖长语调\n"
        "8-9 董雪\n10 X\n12 E 史蒂夫 | - | 他走了。\n13 B\nEND\n```\n99 不会读到"
    )
    plan = parse_unit_reply(reply, 13)
    assert plan.ended and plan.bad_lines == 0
    assert plan.labels[4].speaker == "姜维" and plan.labels[4].instruct == "随意放松的聊天语气"
    assert plan.labels[5].speaker == "任昊"
    assert plan.labels[8] is plan.labels[9] and plan.labels[8].speaker == "董雪"
    assert plan.labels[10].kind == "X"
    assert plan.labels[12].kind == "E" and plan.labels[12].text == "他走了。"
    assert plan.labels[13].kind == "B"


def test_parse_reply_flags_bad_lines_and_missing_end():
    plan = parse_unit_reply("hello\n3 姜维\n40 任昊\n3 董雪\n4-2 甲\n", 13)
    assert not plan.ended
    assert plan.bad_lines == 4  # prose, out of range, duplicate, reversed range
    assert plan.labels[3].speaker == "姜维"


def test_parse_reply_narrator_aliases_and_thinking():
    plan = parse_unit_reply("<think>先想想</think>\n2 N\n3 旁白\nEND", 5)
    assert plan.ended
    assert plan.labels[2].speaker == NARRATOR and plan.labels[3].speaker == NARRATOR


def test_validate_edit_accepts_only_tag_deletion():
    orig = "史蒂夫猛地抓住杜尘的衣领，吼道："
    assert validate_edit(orig, "史蒂夫猛地抓住杜尘的衣领。")
    assert validate_edit("吼道：快走！", "快走！")           # leading tag
    assert not validate_edit(orig, orig)                     # nothing deleted
    assert not validate_edit(orig, "史蒂夫抓住杜尘的衣领。")   # deletes 猛地, no speech verb
    assert not validate_edit(orig, "史蒂夫猛地抓住杜尘的衣领，怒吼。")  # rewrites
    assert not validate_edit(orig, "史蒂夫猛地抓住杜尘的衣领[gasps]")  # invented symbols
    assert not validate_edit(orig, "")                       # empties the unit
    assert not validate_edit("他说道" * 20, "他说道", max_delete=24)  # span too long
    assert not validate_edit("甲说乙戊道丙", "甲乙丙")            # two separate spans


def _labels(**by_n):
    reply = "\n".join(f"{n} {v}" for n, v in by_n.items()) + "\nEND"
    return reply


def test_assemble_matches_legacy_example():
    units = segment_chunk(SAMPLE)
    plan = parse_unit_reply(
        "4 姜维 | 随意放松的聊天语气，语速平稳不急。\n"
        "5 任昊 | 懒洋洋地拖长语调，句尾带出哈欠，咬字略含糊。\n"
        "8 董雪 | 压抑的低声呻吟中挤出微弱的字，声音之间急促吸气，尾音渐弱。\n"
        "9 姜维 | 紧张的低声耳语。\n10 X\n11 姜维\nEND",
        len(units),
    )
    res = assemble(units, plan, NARR)
    assert res.ok and res.deleted == 1
    got = [(e["speaker"], e["text"]) for e in res.entries]
    assert got == [
        (NARRATOR, "第十二章 夜雨"),
        (NARRATOR, "雨下了一整夜。姜维把车停在巷口，回头看了一眼。"),
        ("姜维", "耗子，我跟小雪去买点东西，你跟车里等会儿吧。"),
        ("任昊", "行，快去快回。"),
        (NARRATOR, "任昊打了个哈欠。董雪捂着手臂，疼得直吸气。"),
        ("董雪", "嗯……疼……"),
        ("姜维", "别出声，有人。"),
        (NARRATOR, f"他心想：{LQ}这下麻烦了。{RQ}"),
    ]
    assert [e["instruct"] for e in res.entries if e["speaker"] == NARRATOR] == [NARR] * 4
    assert res.entries[6]["instruct"] == "紧张的低声耳语。"  # the non-empty one survives the merge


def test_assemble_edit_applied_and_rejected():
    text = f"史蒂夫猛地抓住杜尘的衣领，吼道：{LQ}放手！{RQ}"
    units = segment_chunk(text)
    good = assemble(units, parse_unit_reply(
        "1 E N | - | 史蒂夫猛地抓住杜尘的衣领。\n2 史蒂夫 | 厉声怒吼\nEND", 2), NARR)
    assert good.ok and good.edit_applied == 1 and good.edit_rejected == 0
    assert [(e["speaker"], e["text"]) for e in good.entries] == [
        (NARRATOR, "史蒂夫猛地抓住杜尘的衣领。"), ("史蒂夫", "放手！")]

    bad = assemble(units, parse_unit_reply(
        "1 E N | - | 史蒂夫一把揪住杜尘。\n2 史蒂夫\nEND", 2), NARR)
    assert bad.edit_applied == 0 and bad.edit_rejected == 1
    assert bad.entries[0]["text"] == "史蒂夫猛地抓住杜尘的衣领，吼道：" or \
        bad.entries[0]["text"] == "史蒂夫猛地抓住杜尘的衣领，吼道。"

    off = assemble(units, parse_unit_reply(
        "1 E N | - | 史蒂夫猛地抓住杜尘的衣领。\nEND", 2), NARR, edit_enabled=False)
    assert off.edit_applied == 0 and off.edit_rejected == 1


def test_assemble_dialogue_not_merged_across_paragraph_without_tag():
    text = f"{LQ}第一句。{RQ}\n\n{LQ}第二句。{RQ}"
    units = segment_chunk(text)
    res = assemble(units, parse_unit_reply("1-2 甲\nEND", 2), NARR)
    assert [e["text"] for e in res.entries] == ["第一句。", "第二句。"]


def test_assemble_break_and_soft_max_and_title_isolation():
    body = "".join(f"第{i}句旁白文字写得稍微长一点。" for i in range(1, 7))
    units = segment_chunk("第一章 开始\n" + body)
    res = assemble(units, parse_unit_reply("END", len(units)), NARR, soft_max=40)
    assert res.ok
    assert res.entries[0]["text"] == "第一章 开始"
    assert len(res.entries) > 2 and all(len(e["text"]) <= 40 for e in res.entries[1:])
    assert "".join(e["text"] for e in res.entries) == "第一章 开始" + body

    two = segment_chunk("甲说话了。\n\n时间到了第二天。")
    merged = assemble(two, parse_unit_reply("END", 2), NARR)
    split = assemble(two, parse_unit_reply("2 B\nEND", 2), NARR)
    assert len(merged.entries) == 1 and len(split.entries) == 2


def test_assemble_long_quote_pieces_rejoin_and_strip_quotes():
    sentence = "这是一句相当长的话，需要被拆开。"
    text = LQ + sentence * 8 + RQ
    units = segment_chunk(text, unit_max_chars=40)
    plan = parse_unit_reply(f"1-{len(units)} 甲 | 平静地讲述\nEND", len(units))
    res = assemble(units, plan, NARR)
    assert res.ok and len(res.entries) == 1
    assert res.entries[0]["text"] == sentence * 8
    assert res.entries[0]["instruct"] == "平静地讲述"


def test_assemble_narrator_label_ignores_instruct_and_hidden_glue():
    units = segment_chunk(f"{LQ}好{RQ}。他走了。")
    res = assemble(units, parse_unit_reply("1 甲 | 干脆\n2 N | 乱写的旁白语气\nEND", 2), NARR)
    assert res.ok
    assert [(e["speaker"], e["text"], e["instruct"]) for e in res.entries] == [
        ("甲", "好。", "干脆"), (NARRATOR, "他走了。", NARR)]


def test_assemble_punctuation_fix_before_other_speaker():
    units = segment_chunk(f"史蒂夫抓住衣领，\n{LQ}放手！{RQ}")
    res = assemble(units, parse_unit_reply("2 史蒂夫\nEND", 2), NARR)
    assert res.entries[0]["text"] == "史蒂夫抓住衣领。"


def test_assemble_property_skeleton_preserved_for_any_labelling():
    units = segment_chunk(SAMPLE)
    n = len(units)
    for seed in range(40):
        lines = []
        for k in range(2, n + 1):  # unit 1 is the chapter title: always kept as narration
            pick = (seed * 7 + k * 3) % 5
            if pick == 0:
                lines.append(f"{k} 甲 | 语气")
            elif pick == 1:
                lines.append(f"{k} X")
        res = assemble(units, parse_unit_reply("\n".join(lines) + "\nEND", n), NARR)
        assert res.ok
        kept = _skeleton("".join(e["text"] for e in res.entries))
        dropped = sum(len(_skeleton(units[int(l.split()[0]) - 1].text))
                      for l in lines if l.endswith(" X"))
        assert len(kept) + dropped == len(_skeleton(SAMPLE))
