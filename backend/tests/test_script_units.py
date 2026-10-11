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
    delete_allowed,
    is_pure_speech_tag,
    salvage_tag_edit,
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


def test_validate_edit_accepts_non_char_speech_phrases():
    assert validate_edit("艾基尔点了点头表示", "艾基尔点了点头。")
    assert validate_edit("贝尔库利短短叹了口气，低声说了句", "贝尔库利短短叹了口气。")


def test_assemble_noop_edit_is_neither_applied_nor_rejected():
    units = segment_chunk("贝尔库利在参杂叹息声当中回答了一句")
    res = assemble(units, parse_unit_reply("1 E N | - | 贝尔库利在参杂叹息声当中回答了一句\nEND", 1), NARR)
    assert res.edit_applied == 0 and res.edit_rejected == 0
    assert res.entries[0]["text"] == "贝尔库利在参杂叹息声当中回答了一句"


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


def test_assemble_delete_guard_keeps_long_units_unless_watermark():
    story = "这是一段并不短的正文叙述，不应该因为模型手滑标了删除就从脚本里消失掉。"
    url = "本书来自 www.example.com 欢迎访问获取更多更新内容和精彩资源下载地址"
    text = f"{story}\n\n姜维说，\n\n{url}"
    units = segment_chunk(text)
    res = assemble(units, parse_unit_reply("1 X\n2 X\n3 X\nEND", len(units)), NARR, delete_max_chars=30)
    assert res.ok and res.deleted == 2 and res.delete_rejected == 1
    assert [e["text"] for e in res.entries] == [story]


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
        # Text can only vanish through deletions the guard allows (pure speech tags, watermarks,
        # trailing speech clauses); assemble's own self-check (res.ok) proves the accounting.
        assert len(kept) <= len(_skeleton(SAMPLE))


# ---------------------------------------------------------------------------
# End-to-end through generate_file (fake HTTP): units protocol, retry, fallback
# ---------------------------------------------------------------------------
import json
import urllib.request

from backend.core.config import GenerationConfig, PromptsConfig
from backend.engines.script import generate_file
from backend.tests.test_script import _LLM, _BodyResp, _chat_payload, _LogHandle, workspace  # noqa: F401

GOOD_REPLY = (
    "4 姜维 | 随意放松的聊天语气\n5 任昊 | 懒洋洋地拖长语调\n8 董雪 | 压抑的低声呻吟\n"
    "9 姜维 | 紧张的低声耳语\n10 X\n11 姜维 | 紧张的低声耳语\nEND"
)
# Every mechanical check stage off: only the parse stage talks to the (fake) model.
_QUIET = dict(
    spot_check_enabled=False, check_boundary_speakers=False, revalidate_splits=False,
    validate_instructs=False, check_long_paragraphs=False, parse_protocol="units",
)


def _run(workspace, monkeypatch, replies, **gen):
    seen = []

    def urlopen(req, *a, **k):
        seen.append(json.loads(req.data.decode("utf-8")))
        reply, finish = replies[min(len(seen), len(replies)) - 1]
        return _BodyResp(_chat_payload(reply, finish))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "ch.txt"
    src.write_bytes(SAMPLE.encode("utf-8"))
    handle = _LogHandle()
    result = generate_file(handle, str(src), _LLM, PromptsConfig(),
                           GenerationConfig(**{**_QUIET, **gen}))
    out = json.loads((workspace / "03_parsed_json" / "ch.json").read_text("utf-8"))
    return result, out, seen, handle


def test_generate_file_units_protocol_end_to_end(workspace, monkeypatch):
    result, out, seen, handle = _run(workspace, monkeypatch, [(GOOD_REPLY, "stop")],
                                     narrator_instruct="管理员固定旁白语气。",
                                     merge_same_speaker=False)
    assert len(seen) == 1
    prompt = seen[0]["messages"][1]["content"]
    assert "[4] " + LQ + "耗子" in prompt and "【原文单元】" in prompt
    assert [(e["speaker"], e["text"]) for e in out] == [
        (NARRATOR, "第十二章 夜雨"),
        (NARRATOR, "雨下了一整夜。姜维把车停在巷口，回头看了一眼。"),
        ("姜维", "耗子，我跟小雪去买点东西，你跟车里等会儿吧。"),
        ("任昊", "行，快去快回。"),
        (NARRATOR, "任昊打了个哈欠。董雪捂着手臂，疼得直吸气。"),
        ("董雪", "嗯……疼……"),
        ("姜维", "别出声，有人。"),
        (NARRATOR, f"他心想：{LQ}这下麻烦了。{RQ}"),
    ]
    assert all(e["instruct"] == "管理员固定旁白语气。" for e in out if e["speaker"] == NARRATOR)
    assert result["parse_protocol"] == "units" and result["unit_fallback_chunks"] == 0
    assert result["unit_labels"] == 6
    assert any("解析协议：编号单元" in msg for _lv, msg in handle.logs)


def test_generate_file_units_retries_reply_without_end(workspace, monkeypatch):
    truncated = GOOD_REPLY.rsplit("\nEND", 1)[0]
    result, out, seen, _ = _run(workspace, monkeypatch,
                                [(truncated, "length"), (GOOD_REPLY, "stop")])
    assert len(seen) == 2 and result["unit_fallback_chunks"] == 0
    assert seen[0]["max_tokens"] == 4096 and seen[1]["max_tokens"] == 8192  # budget doubled
    assert {e["speaker"] for e in out} >= {"姜维", "任昊", "董雪"}


def test_generate_file_units_falls_back_to_json_protocol(workspace, monkeypatch):
    legacy = json.dumps([{"speaker": NARRATOR, "text": SAMPLE.replace("\n\n", ""),
                          "instruct": "旧协议"}], ensure_ascii=False)
    replies = [("没有标签，只有闲聊", "stop")] * 3 + [(legacy, "stop")]
    result, out, seen, handle = _run(workspace, monkeypatch, replies)
    assert len(seen) == 4 and result["unit_fallback_chunks"] == 1
    assert any("回退旧 JSON 协议" in msg for _lv, msg in handle.logs)
    assert out and all(e["instruct"] for e in out)


def test_generate_file_units_rejects_bad_edit_and_keeps_text(workspace, monkeypatch):
    reply = "4 姜维\n12 E N | - | 他胡乱改写了这句话。\nEND"
    result, out, seen, handle = _run(workspace, monkeypatch, [(reply, "stop")])
    assert result["unit_edit_rejected"] == 1 and result["unit_edit_applied"] == 0
    assert any("edit 不合规" in msg for _lv, msg in handle.logs)
    assert "".join(e["text"] for e in out).count("他心想") == 1


def test_generate_file_units_accepts_natural_stop_without_end(workspace, monkeypatch):
    no_end = GOOD_REPLY.rsplit("\nEND", 1)[0]
    result, out, seen, _ = _run(workspace, monkeypatch, [(no_end, "stop")])
    assert len(seen) == 1 and result["unit_fallback_chunks"] == 0
    assert {e["speaker"] for e in out} >= {"姜维", "任昊", "董雪"}


def test_generate_file_units_rejects_early_stop_without_end(workspace, monkeypatch):
    # Labels stop at unit 3 although four whole-line quotes follow → treated as an early
    # stop: retried, and the retry with the complete labels is used.
    source = "\n\n".join(["旁白一。", "旁白二。", "旁白三。"] + [f"{LQ}台词{i}。{RQ}" for i in range(4)])
    early = "3 甲 | x"
    full = "4-7 甲 | x\nEND"
    replies = [(early, "stop"), (full, "stop")]
    seen = []

    def urlopen(req, *a, **k):
        seen.append(1)
        reply, finish = replies[min(len(seen), len(replies)) - 1]
        return _BodyResp(_chat_payload(reply, finish))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "ch.txt"
    src.write_bytes(source.encode("utf-8"))
    handle = _LogHandle()
    generate_file(handle, str(src), _LLM, PromptsConfig(), GenerationConfig(**_QUIET))
    assert len(seen) == 2
    assert any("疑似提前停笔" in msg for _lv, msg in handle.logs)


def test_shipped_default_is_units_protocol_and_admin_can_switch_back():
    # No fixture pins the protocol here: this is the real default every deployment gets.
    assert GenerationConfig().parse_protocol == "units"
    assert GenerationConfig().chunk_size == 3000
    assert GenerationConfig(parse_protocol="json").parse_protocol == "json"


def test_long_resplit_threshold_is_independent_of_mechanical_cap(workspace, monkeypatch):
    from backend.engines import script as script_engine

    seen = []

    def fake_resplit(handle, llm, generation, sys_prompt, usr_template, entries, max_chars, **kw):
        seen.append(max_chars)
        return entries, 0, 0

    monkeypatch.setattr(script_engine, "long_paragraph_resplit", fake_resplit)
    quiet = {**_QUIET, "check_long_paragraphs": True}
    for gen, expected in (({}, 130), ({"long_resplit_chars": 100}, 100),
                          ({"max_paragraph_chars": 90}, 90)):  # never above the hard cap
        seen.clear()
        _run_with(workspace, monkeypatch, quiet, gen)
        assert seen == [expected], (gen, seen)
    assert GenerationConfig().long_resplit_chars == 130 and GenerationConfig().max_paragraph_chars == 200


def _run_with(workspace, monkeypatch, base, extra):
    import shutil

    shutil.rmtree(workspace, ignore_errors=True)
    (workspace / "02_split_text").mkdir(parents=True)
    (workspace / "02_split_text" / "ch.txt").write_bytes(SAMPLE.encode("utf-8"))
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, *a, **k: _BodyResp(_chat_payload(GOOD_REPLY, "stop")))
    return generate_file(_LogHandle(), str(workspace / "02_split_text" / "ch.txt"), _LLM,
                         PromptsConfig(), GenerationConfig(**{**base, **extra}))


def test_units_protocol_bills_the_assembled_script_not_the_label_reply(workspace, monkeypatch):
    import backend.platform.quota as quota

    charged = []
    monkeypatch.setattr(quota, "consume_llm_output", lambda output, op="x", **k: charged.append(output))
    result, out, _seen, _h = _run(workspace, monkeypatch, [(GOOD_REPLY, "stop")])
    assert len(charged) == 1 and len(charged[0]) > 10 * len(GOOD_REPLY) / 10
    assert json.loads(charged[0]) == result["entries"]  # billed on the real output, not "4 姜维 | …"


def test_segment_nested_unterminated_quote_has_no_trail():
    units = segment_chunk(f"{LQ}他说{CL}不行，我才不信{RQ}")  # inner 「 never closed; outer closed by ”
    assert all(u.trail != CR for u in units)
    open_nested = segment_chunk(f"{CL}外层{CL}内层没有闭合{CR}")
    assert open_nested[0].trail == ""  # depth never returned to 0 → not a closed span


def test_unit_plan_accepts_unreported_finish_reason_without_end():
    from backend.engines.script import _unit_plan_problem

    plan = parse_unit_reply("4 甲\n5 乙", 13)
    assert _unit_plan_problem(plan, [4, 5], None) is None       # provider reports no reason
    assert _unit_plan_problem(plan, [4, 5], "stop") is None
    assert "并非正常停笔" in _unit_plan_problem(plan, [4, 5], "length")
    assert "疑似提前停笔" in _unit_plan_problem(parse_unit_reply("1 甲", 13), [3, 4, 5], None)


# ---------------------------------------------------------------------------
# X guard: only pure speech tags / watermarks vanish; action narration is kept
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["杜尘笑道：", "他压低声音说，", "老道继续笑道。", "安知鱼问道", "姜维说，"])
def test_pure_speech_tags_are_deletable(text):
    assert is_pure_speech_tag(text) and delete_allowed(text, 30)


@pytest.mark.parametrize("text", [
    "顾秋情叹了口气，", "安知鱼摇了摇头，", "顾秋情抿了抿嘴，", "顾秋情呵呵一笑，", "顾秋情咯咯一笑，",
    "安知鱼闻言有些好笑地说道。",  # one clause but carries narration: still a tag by shape (kept below)
])
def test_action_units_without_speech_verb_are_not_tags(text):
    if text.endswith("说道。"):
        assert is_pure_speech_tag(text)   # a lone "…地说道" is a tag; tone goes to instruct
    else:
        assert not is_pure_speech_tag(text) and not delete_allowed(text, 30)


@pytest.mark.parametrize("text,expected", [
    ("安知鱼思考了一会儿，接着问道：", "安知鱼思考了一会儿。"),
    ("顾秋情摇了摇头，把这些事情放在一旁，说道。", "顾秋情摇了摇头，把这些事情放在一旁。"),
    ("安知鱼拉了拉顾秋情的手，略微有些强势地说道。", "安知鱼拉了拉顾秋情的手。"),
    ("安知鱼听到这话，稍微有些心酸，他沉默了一会儿，才说道：", "安知鱼听到这话，稍微有些心酸，他沉默了一会儿。"),
])
def test_salvage_keeps_action_drops_speech_clause(text, expected):
    assert not delete_allowed(text, 30)
    assert salvage_tag_edit(text) == expected
    assert validate_edit(text, expected)


def test_salvage_none_for_pure_action_or_tag():
    assert salvage_tag_edit("顾秋情叹了口气，") is None
    assert salvage_tag_edit("杜尘笑道：") is None


def test_assemble_x_on_action_keeps_narration_and_splits_dialogue():
    text = f"{LQ}你一个人在外面闯荡。{RQ}顾秋情叹了口气，{LQ}不觉得孤单吗？{RQ}"
    units = segment_chunk(text)
    res = assemble(units, parse_unit_reply("1 顾秋情\n2 X\n3 顾秋情\nEND", len(units)), NARR)
    assert res.ok and res.deleted == 0 and res.delete_rejected == 1
    assert [(e["speaker"], e["text"]) for e in res.entries] == [
        ("顾秋情", "你一个人在外面闯荡。"), (NARRATOR, "顾秋情叹了口气。"), ("顾秋情", "不觉得孤单吗？")]


def test_assemble_x_on_action_plus_speech_tag_is_salvaged():
    text = f"安知鱼思考了一会儿，接着问道：{LQ}你确定吗？{RQ}"
    units = segment_chunk(text)
    res = assemble(units, parse_unit_reply("1 X\n2 安知鱼\nEND", len(units)), NARR)
    assert res.ok and res.delete_salvaged == 1 and res.deleted == 0
    assert [(e["speaker"], e["text"]) for e in res.entries] == [
        (NARRATOR, "安知鱼思考了一会儿。"), ("安知鱼", "你确定吗？")]


def test_assemble_pure_tag_x_still_merges_same_paragraph_quotes():
    text = f"{LQ}别出声，{RQ}姜维压低声音说，{LQ}有人。{RQ}"
    units = segment_chunk(text)
    res = assemble(units, parse_unit_reply("1 姜维\n2 X\n3 姜维\nEND", len(units)), NARR)
    assert res.ok and [e["text"] for e in res.entries] == ["别出声，有人。"]


def test_assemble_x_never_merges_dialogue_across_paragraphs():
    text = f"{LQ}第一句。{RQ}\n\n姜维说道：\n\n{LQ}第二句。{RQ}"
    units = segment_chunk(text)
    res = assemble(units, parse_unit_reply("1 姜维\n2 X\n3 姜维\nEND", len(units)), NARR)
    assert res.ok and [e["text"] for e in res.entries] == ["第一句。", "第二句。"]


# ---------------------------------------------------------------------------
# Embedded scare quotes and unquoted "dialogue"
# ---------------------------------------------------------------------------

def test_embedded_short_quote_stays_in_narration():
    text = f"对方表现出来的感觉，就好像是隐隐透露着一种{LQ}他不属于你{RQ}的感觉。"
    units = segment_chunk(text)
    quote = [u for u in units if u.kind == "quote"][0]
    assert quote.embedded
    res = assemble(units, parse_unit_reply(f"{quote.n} 顾秋情 | 内心独白\nEND", len(units)), NARR)
    assert res.ok and res.demoted == 1
    assert len(res.entries) == 1 and res.entries[0]["speaker"] == NARRATOR
    assert "他不属于你" in res.entries[0]["text"]


def test_quote_after_speech_intro_is_not_embedded():
    for text in (f"他喊{LQ}救命{RQ}，然后跑了。", f"他说：{LQ}好的{RQ}然后走了。", f"{LQ}好的。{RQ}他说。"):
        assert not any(u.embedded for u in segment_chunk(text))


def test_unquoted_inner_thought_is_not_dialogue_in_quote_style_chunk():
    text = "\n\n".join([
        f"{LQ}甲。{RQ}", f"{LQ}乙。{RQ}", f"{LQ}丙。{RQ}",
        "她想让两人和解，可不是想给自己找一个情敌……头疼啊……",
        f"{LQ}咱们回家吧。{RQ}顾秋情说道。",
    ])
    units = segment_chunk(text)
    thought = [u for u in units if u.text.startswith("头疼啊")] or [u for u in units if "情敌" in u.text]
    target = thought[-1]
    reply = f"{target.n} 顾秋情\n" + "\n".join(f"{u.n} 顾秋情" for u in units if u.text.startswith(LQ + "咱们"))
    res = assemble(units, parse_unit_reply(reply + "\nEND", len(units)), NARR)
    assert res.ok and res.demoted == 1
    assert any(e["speaker"] == "顾秋情" and e["text"] == "咱们回家吧。" for e in res.entries)
    assert not any(e["speaker"] == "顾秋情" and "头疼" in e["text"] for e in res.entries)


def test_unquoted_dialogue_after_colon_is_kept():
    text = "\n\n".join([f"{LQ}甲。{RQ}", f"{LQ}乙。{RQ}", f"{LQ}丙。{RQ}", "他说：你好呀"])
    units = segment_chunk(text)
    last = units[-1]
    res = assemble(units, parse_unit_reply(f"{last.n} 他\nEND", len(units)), NARR)
    assert res.demoted == 0


def test_speaker_on_unquoted_tag_unit_becomes_narration_edit():
    text = "\n\n".join([f"{LQ}甲。{RQ}", f"{LQ}乙。{RQ}", f"{LQ}丙。{RQ}",
                        f"顾秋情看向了安知鱼，微笑着问道：{LQ}觉得她是个怎样的人？{RQ}"])
    units = segment_chunk(text)
    tag = [u for u in units if u.text.startswith("顾秋情看向")][0]
    q = units[-1]
    reply = f"1-3 甲\n{tag.n} E 安知鱼 | - | 顾秋情看向了安知鱼，微笑着问。\n{q.n} 安知鱼 | 温和\nEND"
    res = assemble(units, parse_unit_reply(reply, len(units)), NARR)
    assert res.ok and res.demoted == 1 and res.edit_applied == 1
    assert [(e["speaker"], e["text"]) for e in res.entries][-2:] == [
        (NARRATOR, "顾秋情看向了安知鱼，微笑着问。"), ("安知鱼", "觉得她是个怎样的人？")]


def test_unlabelled_quotes_are_reported_but_thoughts_are_exempt():
    text = "\n\n".join([f"{LQ}第一句话。{RQ}", f"{LQ}第二句话。{RQ}", "他心想：", f"{LQ}这下麻烦了。{RQ}"])
    units = segment_chunk(text)
    res = assemble(units, parse_unit_reply("1 甲\nEND", len(units)), NARR)
    quote_nums = [u.n for u in units if u.kind == "quote"]
    assert res.narrated_quotes == [quote_nums[1]]   # 乙 was skipped; the thought quote is exempt


def test_entry_has_unclaimed_quote_flags_dialogue_but_not_thoughts_or_mentions():
    from backend.engines.script_units import entry_has_unclaimed_quote
    assert entry_has_unclaimed_quote(f"安知鱼走进小区。{LQ}可卿来了啊……{RQ}顾秋情抿了抿嘴。")
    assert not entry_has_unclaimed_quote(f"他心想：{LQ}这下麻烦了。{RQ}")
    assert not entry_has_unclaimed_quote(f"隐隐透露着一种{LQ}他不属于你{RQ}的感觉。")
    assert not entry_has_unclaimed_quote("没有任何引号的普通旁白。")


def test_narrated_quote_indices_only_narrator_entries():
    from backend.engines.script import narrated_quote_indices
    entries = [
        {"speaker": "NARRATOR", "text": f"走进小区。{LQ}可卿来了啊……{RQ}顾秋情抿了抿嘴。", "instruct": ""},
        {"speaker": "顾秋情", "text": "可卿来了啊……", "instruct": ""},
        {"speaker": "NARRATOR", "text": "平常的旁白。", "instruct": ""},
    ]
    assert narrated_quote_indices(entries) == [0]


def test_a_single_unclaimed_quote_pair_is_enough_in_any_bracket_style():
    from backend.engines.script_units import entry_has_unclaimed_quote
    assert entry_has_unclaimed_quote(f"他回头。{LQ}哦{RQ}")                       # one-character exclamation
    assert entry_has_unclaimed_quote(f"他回头。{CL}等一下。{CR}她笑了。")          # Japanese corner brackets
    assert entry_has_unclaimed_quote("他回头。『等一下。』她笑了。")
    assert entry_has_unclaimed_quote('他回头。"等一下。"她笑了。')


def test_unlabelled_speech_tags_beside_dialogue_are_stripped():
    text = "\n\n".join([
        f"{LQ}咱们回家吧。{RQ}顾秋情摇了摇头，把这些事情放在一旁，说道。",
        f"安知鱼听到这话，稍微有些心酸，他沉默了一会儿，才说道：{LQ}如果你想的话。{RQ}",
        f"{LQ}好。{RQ}他问道。",
        "旁白里没有对话的一句，说道。",
    ])
    units = segment_chunk(text)
    quotes = [u.n for u in units if u.kind == "quote"]
    reply = "\n".join(f"{n} 甲" for n in quotes) + "\nEND"
    res = assemble(units, parse_unit_reply(reply, len(units)), NARR)
    got = [e["text"] for e in res.entries]
    assert res.ok
    joined = "".join(got)
    assert "顾秋情摇了摇头，把这些事情放在一旁。" in joined
    assert "安知鱼听到这话，稍微有些心酸，他沉默了一会儿。" in joined
    assert "说道" not in joined.replace("旁白里没有对话的一句，说道", "")
    assert not any("他问道" in t for t in got)              # pure tag with a subject: removed
    assert any("没有对话的一句，说道" in t for t in got)    # not beside dialogue: untouched


def test_rederive_keeps_unlabelled_text_of_a_character_entry_with_that_character(monkeypatch):
    from backend.engines import script as script_mod
    from backend.core.config import GenerationConfig

    long_speech = ("性格稍稍有些强势，和我姐姐不太一样，不过性子同样有些急，这一点倒是和我姐姐一样。"
                   "她好像更注重于强调‘安知鱼才是我们相认的关键’的感觉。顾秋情叹了口气。这弄得我很尴尬啊。")
    entry = {"speaker": "顾秋情", "text": long_speech, "instruct": "分析性的语气"}
    seen = {}

    def fake_call(llm, generation, messages, handle):
        seen["prompt"] = messages[-1]["content"]
        units = segment_chunk(long_speech, GenerationConfig().unit_max_chars)
        narr = [u.n for u in units if u.n and u.text.startswith("顾秋情叹了口气")]
        return "".join(f"{n} N\n" for n in narr) + "END"

    monkeypatch.setattr(script_mod, "_llm_call", fake_call)

    class H:
        def check(self): pass
        def log(self, *a, **k): pass

    monkeypatch.setattr("backend.platform.quota.consume_llm_output", lambda *a, **k: None)
    parts = script_mod.rederive_entries_units(
        H(), None, GenerationConfig(), ("sys", "{context}\n{units}"), [(entry, "")],
        frozenset({"NARRATOR", "顾秋情"}), stage="测试", head="")
    assert parts and parts[0]
    assert "是角色「顾秋情」说的话" in seen["prompt"]
    got = [(p["speaker"], p["text"]) for p in parts[0]]
    assert ("NARRATOR", "顾秋情叹了口气。") in got
    assert "".join(t for sp, t in got if sp == "顾秋情").startswith("性格稍稍有些强势")
    assert all(sp in ("顾秋情", "NARRATOR") for sp, _ in got)
    assert sum(1 for sp, _ in got if sp == "NARRATOR") == 1   # nothing else turned into narration


def test_long_admin_narrator_instruct_is_not_flagged_or_rewritten():
    from backend.engines.script import validate_instructs
    from backend.core.config import GenerationConfig

    fixed = ("中年男性旁白，声音沉稳成熟、自然有磁性。普通话标准，吐字清晰，语速中等偏慢，节奏平稳。"
             "语气克制、有叙事感，情绪自然含蓄，不夸张。")
    entries = [
        {"speaker": "NARRATOR", "text": "旁白一。", "instruct": fixed},
        {"speaker": "甲", "text": "台词。", "instruct": "自然的对话语气。"},
        {"speaker": "NARRATOR", "text": "旁白二。", "instruct": fixed},
    ]

    class H:
        def check(self): pass
        def log(self, *a, **k): pass
        def progress(self, *a, **k): pass

    got, checked, fixed_n = validate_instructs(
        H(), None, GenerationConfig(), "", "", entries, max_chars=60, fixed_narrator_instruct=fixed)
    assert (checked, fixed_n) == (0, 0) and got == entries      # no flag, no LLM call (llm=None would raise)


def test_long_resplit_logs_normal_unchanged_reply_as_success(monkeypatch):
    from backend.engines import script as script_mod
    from backend.core.config import GenerationConfig

    entry = {"speaker": "NARRATOR", "text": "这是一段没有任何引号的很长的旁白，" * 12, "instruct": "平稳"}
    monkeypatch.setattr(script_mod, "_llm_call", lambda *a, **k: "END")
    monkeypatch.setattr("backend.platform.quota.consume_llm_output", lambda *a, **k: None)
    logs = []

    class H:
        def check(self): pass
        def progress(self, *a, **k): pass
        def log(self, msg, level="INFO"): logs.append((level, msg))

    out, checked, fixed = script_mod.long_paragraph_resplit(
        H(), None, GenerationConfig(), "s", "u", [entry], 100, context_window=0,
        unit_prompts=("sys", "{context}\n{units}"))
    assert (checked, fixed) == (1, 0) and out == [entry]
    assert any("确认无需重切" in m for lv, m in logs if lv == "INFO")
    assert not any(lv == "WARNING" for lv, _ in logs)
    assert any("1 条确认无需改动" in m for _lv, m in logs)


# ---------------------------------------------------------------------------
# Mechanical merging is capped (half the hard cap) and splits EVENLY at sentence ends
# ---------------------------------------------------------------------------

def test_balanced_groups_splits_150_as_75_75_not_100_50():
    from backend.engines.script_units import balanced_groups
    assert balanced_groups([25] * 6, 100) == [(0, 3), (3, 6)]          # 75 + 75
    assert balanced_groups([50, 50, 50], 100) == [(0, 2), (2, 3)] or balanced_groups([50, 50, 50], 100) == [(0, 1), (1, 3)]
    assert balanced_groups([30, 30, 30], 100) == [(0, 3)]               # fits: one group
    assert balanced_groups([120, 10], 100) == [(0, 1), (1, 2)]          # an oversize piece stays whole
    assert balanced_groups([], 100) == []


def test_balanced_groups_prefers_paragraph_cuts_when_equally_even():
    from backend.engines.script_units import balanced_groups
    # 4 pieces of 40: cut after piece 2 (80|80) beats 120... both even; the paragraph start wins
    cost = [0.0, 100.0, 0.0, 100.0]
    assert balanced_groups([40, 40, 40, 40], 100, cut_cost=cost) == [(0, 2), (2, 4)]


def test_assemble_splits_a_long_narration_run_evenly_at_sentence_ends():
    sentence = "这是一句长度刚好二十五个字的旁白句子内容整整齐齐。"          # 25 chars incl. 。
    assert len(sentence) == 25
    text = "\n\n".join([sentence * 3, sentence * 3])                          # two paragraphs, 150 total
    units = segment_chunk(text)
    res = assemble(units, parse_unit_reply("END", len(units)), NARR, soft_max=100)
    assert res.ok
    assert [len(e["text"]) for e in res.entries] == [75, 75]
    assert all(e["text"].endswith("。") for e in res.entries)


def test_assemble_never_merges_past_the_cap_for_dialogue_either():
    line = "这是一句台词的内容刚好是二十五个字呢。"
    quotes = "".join(f"{LQ}{line}{RQ}" for _ in range(8))
    units = segment_chunk(quotes)
    reply = "\n".join(f"{u.n} 甲 | 语气" for u in units if u.n) + "\nEND"
    res = assemble(units, parse_unit_reply(reply, len(units)), NARR, soft_max=60)
    assert res.ok and all(len(e["text"]) <= 60 for e in res.entries)
    assert len(res.entries) >= 3


def test_merge_adjacent_balances_a_run_instead_of_greedy_fill():
    from backend.engines.script import merge_adjacent_same_speaker, is_chapter_title
    entries = [{"speaker": "NARRATOR", "text": chr(0x7532 + i) * 24 + "。", "instruct": "x"} for i in range(6)]
    out, n = merge_adjacent_same_speaker(entries, is_chapter_title, max_chars=100)
    assert [len(e["text"]) for e in out] == [75, 75] and n == 4


def test_unlabelled_noun_ending_like_a_speech_verb_is_not_stripped():
    text = f"{LQ}快走。{RQ}那声惨叫。\n\n{LQ}别怕。{RQ}"
    units = segment_chunk(text)
    reply = "\n".join(f"{u.n} 甲" for u in units if u.kind == "quote") + "\nEND"
    res = assemble(units, parse_unit_reply(reply, len(units)), NARR)
    assert any("那声惨叫" in e["text"] for e in res.entries)


def test_balanced_groups_pathological_run_falls_back_to_greedy():
    from backend.engines.script_units import balanced_groups, _BALANCE_MAX_PIECES
    groups = balanced_groups([10] * (_BALANCE_MAX_PIECES + 50), 100)
    assert groups[0] == (0, 10) and groups[-1][1] == _BALANCE_MAX_PIECES + 50
