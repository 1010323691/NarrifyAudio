"""Conservative script-check repairs and their unchanged LLM request budgets."""
from __future__ import annotations

import json
import random

import pytest

from backend.core.config import GenerationConfig, LLMConfig, PromptsConfig
from backend.engines import script
from backend.engines.text import is_chapter_title

pytestmark = pytest.mark.usefixtures("legacy_json_protocol")  # these tests script the JSON protocol


class Handle:
    cancelled = False
    def __init__(self):
        self.logs = []

    def check(self):
        pass

    def progress(self, *args):
        pass

    def phase(self, *args):
        pass

    def log(self, text, level=None):
        self.logs.append((level, text))


def entry(speaker, text, instruct=""):
    return {"speaker": speaker, "text": text, "instruct": instruct}


def forbid_llm(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("This mechanical repair must not make an LLM request")
    monkeypatch.setattr(script, "_llm_call", fail)


def test_ordinary_dao_words_neither_trigger_nor_allow_deletion(monkeypatch):
    for text in ["你知道：人生没有捷径。", "他不知道：门已经开了。", "难道：事情结束了？"]:
        with monkeypatch.context() as monkeypatch:
            forbid_llm(monkeypatch)
            wrapped = f"“{text}”"
            original = entry("林某", wrapped)
            assert not script.is_suspicious_entry_text(wrapped), (text,)
            assert script._strip_leading_saying_tag(wrapped, {"林某"}) == wrapped, (text,)
            dropped = text.split("：", 1)[1]
            assert script._reparse_vote([entry("林某", dropped)], original, {"林某"}) is None, (text,)
            rows = [original]
            out, checked, fixed = script.validate_sentence_splits(
                Handle(), LLMConfig(), GenerationConfig(), "sys", "{chunk}", rows,
            )
            assert out is rows and (checked, fixed) == (0, 0), (text,)


def test_reparse_gate_rejects_trimming_ordinary_sentence_tails():
    for text, dropped in [
        ("他走上街道。", "他走上街。"),
        ("他没有答。", "他没有。"),
        ("他转身喊道。", "他转身。"),
        ("她没有回答。", "她没有。"),
    ]:
        assert script._reparse_vote([entry("NARRATOR", dropped)],
                                   entry("NARRATOR", text), {"NARRATOR"}) is None, (text, dropped,)


def test_descriptive_action_cannot_be_dropped_as_a_leading_tag():
    original = entry("NARRATOR", "“林某转身喊道：“快走！””")
    assert script._strip_leading_saying_tag(original["text"], {"林某"}) == original["text"]
    assert script._reparse_vote([entry("林某", "快走！")], original,
                               {"林某", "NARRATOR"}) is None


def test_stricter_gate_does_not_spend_more_votes_than_old_early_stop(monkeypatch):
    for replies, expected_calls in [
        (["他走上街。", "他走上街。"], 2),
        (["他走上街。", "他走上街道。", "他走上街。"], 3),
    ]:
        with monkeypatch.context() as monkeypatch:
            calls = []

            def llm(*args, **kwargs):
                calls.append(1)
                assert len(calls) <= expected_calls, (replies, expected_calls,)
                return json.dumps([entry("NARRATOR", replies[len(calls) - 1])])

            monkeypatch.setattr(script, "_llm_call", llm)
            result = script.revalidate_entry(
                Handle(), LLMConfig(), GenerationConfig(), "sys", "{chunk}",
                entry("NARRATOR", "他走上街道。"), "", {"NARRATOR"},
            )
            assert result is None and len(calls) == expected_calls, (replies, expected_calls,)


def test_conservative_tag_cleanup_keeps_actions_negations_and_unknown_names(monkeypatch):
    for text in [
        "他走上街道。", "他指着山道。", "他没有答。", "他欲言又止，没说。",
        "他转身喊道。", "老道瞪眼怒道。", "杜尘暗喜，急道。", "她不知道。",
        "陌生人说道。",
    ]:
        with monkeypatch.context() as monkeypatch:
            forbid_llm(monkeypatch)
            rows = [entry("NARRATOR", text), entry("林某", "你走吧。")]
            audit = []
            out, deleted, texts = script.delete_pure_saying_tags(rows, is_chapter_title, audit=audit)
            assert out == rows and (deleted, texts, audit) == (0, [], []), (text,)


def test_known_pure_tags_deleted_with_audit_and_without_requests(monkeypatch):
    forbid_llm(monkeypatch)
    rows = [entry("NARRATOR", "林某低声说道。"), entry("林某", "走吧。")]
    audit = []
    out, deleted, texts = script.delete_pure_saying_tags(rows, is_chapter_title, audit=audit)
    assert out == rows[1:] and deleted == 1 and texts == [rows[0]["text"]]
    assert audit == [{"entry_index": 0, "text": rows[0]["text"],
                      "reason": "explicit_attribution_tag"}]
    assert rows[0]["text"] == "林某低声说道。"


def test_punctuation_runs_have_real_text_targets_and_preserve_exact_punctuation(monkeypatch):
    for rows, expected, absorbed, deleted in [
        ([entry("NARRATOR", "……"), entry("NARRATOR", "？"), entry("NARRATOR", "夜色。")],
         ["……？夜色。"], 2, 0),
        ([entry("NARRATOR", "夜色。"), entry("NARRATOR", "……"), entry("NARRATOR", "？")],
         ["夜色。……？"], 2, 0),
        ([entry("NARRATOR", "……"), entry("NARRATOR", "？"), entry("林某", "你好。")],
         ["你好。"], 0, 2),
        ([entry("NARRATOR", "第5章 风暴"), entry("NARRATOR", "……"), entry("NARRATOR", "？")],
         ["第5章 风暴"], 0, 2),
        ([entry("NARRATOR", "一天后。"), entry("NARRATOR", "……")],
         ["一天后。"], 0, 1),
    ]:
        with monkeypatch.context() as monkeypatch:
            forbid_llm(monkeypatch)
            original = [dict(row) for row in rows]
            out, a, d = script.absorb_punct_entries(rows, is_chapter_title)
            assert [row["text"] for row in out] == expected, (rows, expected, absorbed, deleted,)
            assert (a, d) == (absorbed, deleted), (rows, expected, absorbed, deleted,)
            assert rows == original, (rows, expected, absorbed, deleted,)
            assert all(script._skeleton(row["text"]) for row in out), (rows, expected, absorbed, deleted,)


def test_instruct_inheritance_stops_at_boundaries_without_new_requests(monkeypatch):
    for marker in ["第5章 风暴", "一天后。", "次日。", "与此同时。"]:
        with monkeypatch.context() as monkeypatch:
            forbid_llm(monkeypatch)
            rows = [entry("NARRATOR", "夜色。", "Warm narration."),
                    entry("NARRATOR", marker), entry("NARRATOR", "风声。")]
            handle = Handle()
            out, checked, fixed = script.validate_instructs(
                handle, LLMConfig(), GenerationConfig(), "sys", "unused", rows,
            )
            assert out == rows and (checked, fixed) == (2, 0), (marker,)
            assert any(level == "WARNING" and "待核对" in text for level, text in handle.logs), (marker,)


def test_incompatible_neighbor_instructs_remain_unresolved_without_requests(monkeypatch):
    forbid_llm(monkeypatch)
    rows = [entry("NARRATOR", "前文。", "Warm narration."),
            entry("NARRATOR", "中间。"), entry("NARRATOR", "后文。", "Urgent narration.")]
    out, checked, fixed = script.validate_instructs(
        Handle(), LLMConfig(), GenerationConfig(), "sys", "unused", rows,
    )
    assert out == rows and (checked, fixed) == (1, 0)


def test_budgeted_instruct_request_excludes_newly_blocked_inheritance(monkeypatch):
    rows = [entry("林某", "快走。"), entry("NARRATOR", "前文。", "Warm narration."),
            entry("NARRATOR", "第5章 风暴"), entry("NARRATOR", "后文。")]
    calls = []

    def llm(llm, generation, messages, handle):
        calls.append(messages)
        user = messages[1]["content"]
        targets = json.loads(user.split("【目标条目】（只有这些可以修改）：\n", 1)[1]
                             .split("\n\n【上下文】", 1)[0])
        assert [target["index"] for target in targets] == [0]
        return '[{"index": 0, "instruct": "Clear speech."}]'

    monkeypatch.setattr(script, "_llm_call", llm)
    out, checked, fixed = script.validate_instructs(
        Handle(), LLMConfig(), GenerationConfig(), "sys", "unused", rows,
    )
    assert len(calls) == 1 and (checked, fixed) == (3, 1)
    assert out[0]["instruct"] == "Clear speech."
    assert out[2:] == rows[2:]


def test_invalid_instruct_reply_is_not_applied_or_retried(monkeypatch):
    for reply in [
        [{"instruct": "Clear speech."}],
        [{"index": True, "instruct": "Clear speech."}],
        [{"index": 0.5, "instruct": "Clear speech."}],
        [{"index": 0, "instruct": 123}],
        [{"index": 0, "instruct": " ".join(["word"] * 36)}],
        [{"index": 0, "instruct": "Clear speech.", "speaker": "OTHER"}],
        [{"index": 0, "instruct": "Clear speech."}, {"index": 0, "instruct": "Shouting."}],
    ]:
        with monkeypatch.context() as monkeypatch:
            calls = []

            def llm(*args, **kwargs):
                calls.append(1)
                return json.dumps(reply)

            monkeypatch.setattr(script, "_llm_call", llm)
            rows = [entry("林某", "你好。")]
            handle = Handle()
            out, checked, fixed = script.validate_instructs(
                handle, LLMConfig(), GenerationConfig(), "sys", "unused", rows,
            )
            assert out == rows and (checked, fixed) == (1, 0) and len(calls) == 1, (reply,)
            assert any("待核对" in text for _, text in handle.logs), (reply,)


def test_35_word_instruct_is_valid_without_requests(monkeypatch):
    forbid_llm(monkeypatch)
    rows = [entry("林某", "你好。", " ".join(["word"] * 35))]
    out, checked, fixed = script.validate_instructs(
        Handle(), LLMConfig(), GenerationConfig(), "sys", "unused", rows,
    )
    assert out is rows and (checked, fixed) == (0, 0)


def test_invalid_existing_instruct_type_is_reported_without_new_request(monkeypatch):
    forbid_llm(monkeypatch)
    rows = [entry("林某", "你好。", 123)]
    out, checked, fixed = script.validate_instructs(
        Handle(), LLMConfig(), GenerationConfig(), "sys", "unused", rows,
    )
    assert out == [{**rows[0], "instruct": ""}] and (checked, fixed) == (1, 0)
    assert rows[0]["instruct"] == 123


def test_changed_speaker_guidance_only_emits_review_warning(monkeypatch):
    forbid_llm(monkeypatch)
    handle = Handle()
    before = [entry("NARRATOR", "快走！", "Neutral narration.")]
    after = [entry("林某", "快走！", "Neutral narration.")]
    script._log_changed_speaker_instructs(handle, before, after)
    assert after[0]["instruct"] == before[0]["instruct"]
    assert any("角色改判" in text for _, text in handle.logs)


def test_quote_safe_cut_never_moves_to_the_right_of_hard_limit(monkeypatch):
    forbid_llm(monkeypatch)
    text = "甲" * 195 + "“" + "乙" * 6 + "”" + "丙" * 150
    row = {**entry("林某", text, "Urgent whispered delivery."), "source_id": "original"}
    out, count = script.split_long_entries([row], 200, is_chapter_title)
    assert count == 1 and all(len(part["text"]) <= 200 for part in out)
    assert "".join(part["text"] for part in out) == text
    assert all(part["instruct"] == row["instruct"] and part["source_id"] == "original" for part in out)
    assert row["text"] == text


def test_mechanical_split_invariants_across_quotes_whitespace_and_small_limits(monkeypatch):
    forbid_llm(monkeypatch)
    rng = random.Random(42)
    for limit in (1, 5, 10, 35, 200):
        for _ in range(40):
            text = "".join(rng.choice("甲乙丙丁“”「」。，；！？ abc\n")
                           for _ in range(rng.randint(limit + 1, 700)))
            parts = script.split_long_text(text, limit)
            assert all(part and len(part) <= limit for part in parts)
            assert script._skeleton("".join(parts)) == script._skeleton(text)


def test_rejected_deletion_cannot_trigger_followup_model_repairs(monkeypatch, tmp_path):
    original = "“他走上街道：“" + "甲" * 193 + "””"
    assert len(original) > 200
    rows = [entry("NARRATOR", original), entry("林某", "走吧。", "Clear speech.")]
    calls = []

    def llm(*args, **kwargs):
        calls.append(1)
        assert len(calls) <= 2, "Rejected edits must not add length/instruct repair requests"
        return json.dumps([entry("林某", "甲" * 193, "Calm speech.")])

    monkeypatch.setattr(script, "_llm_call", llm)
    monkeypatch.setattr(script, "process_chunk", lambda *args, **kwargs: rows)
    src = tmp_path / "chapter.txt"
    src.write_text(original + "走吧。", encoding="utf-8")
    result = script.parse_script_file(
        Handle(), src, LLMConfig(model_name="mock"), PromptsConfig(),
        GenerationConfig(chunk_size=5000, spot_check_enabled=False),
        output_path=tmp_path / "result.json",
    )
    assert len(calls) == 2
    assert result["suspicious"] == 1 and result["suspicious_fixed"] == 0
    assert result["long_checked"] == 0 and result["long_split"] == 1
    assert result["instruct_checked"] == 1 and result["instruct_fixed"] == 0
    assert script._skeleton("".join(item["text"] for item in result["entries"])) == script._skeleton(original + "走吧。")


def test_punctuation_audit_records_exact_deleted_and_absorbed_text():
    audit = []
    rows = [entry("NARRATOR", "……"), entry("NARRATOR", "？"), entry("NARRATOR", "夜色。")]
    out, absorbed, deleted = script.absorb_punct_entries(rows, is_chapter_title, audit=audit)
    assert (absorbed, deleted) == (2, 0) and out[0]["text"] == "……？夜色。"
    assert audit == [
        {"entry_index": 0, "text": "……", "target_index": 2, "reason": "absorbed_into_narration"},
        {"entry_index": 1, "text": "？", "target_index": 2, "reason": "absorbed_into_narration"},
    ]
    audit = []
    script.absorb_punct_entries([entry("林某", "你好。"), entry("NARRATOR", "？")],
                               is_chapter_title, audit=audit)
    assert audit == [{"entry_index": 1, "text": "？", "reason": "no_narration_target"}]


def test_partial_guidance_reply_repairs_only_valid_targets_once(monkeypatch):
    calls = []

    def llm(*args, **kwargs):
        calls.append(1)
        return json.dumps([{"index": 0, "instruct": "Clear speech."},
                           {"index": 1, "instruct": ""},
                           {"index": 999, "instruct": "Unexpected."}])

    monkeypatch.setattr(script, "_llm_call", llm)
    rows = [entry("林某", "你好。"), entry("李四", "再见。")]
    out, checked, fixed = script.validate_instructs(
        Handle(), LLMConfig(), GenerationConfig(), "sys", "unused", rows,
    )
    assert len(calls) == 1 and (checked, fixed) == (2, 1)
    assert out[0]["instruct"] == "Clear speech." and out[1] == rows[1]


def test_boundary_quote_removal_keeps_review_marker_without_new_reparse_requests(
    monkeypatch, tmp_path,
):
    wrapped = "“林某道：“走吧。””"
    rows = [entry("林某", wrapped, "Calm speech."), entry("李四", "好。", "Clear speech.")]
    parsed = iter([[rows[0]], [rows[1]]])
    calls = []

    def llm(*args, **kwargs):
        calls.append(1)
        assert len(calls) == 1
        return json.dumps({"results": [
            {"index": 0, "speaker": "林某", "text": script.strip_outer_quotes(wrapped)},
            {"index": 1, "speaker": "李四"},
        ]})

    monkeypatch.setattr(script, "_llm_call", llm)
    monkeypatch.setattr(script, "split_into_chunks", lambda *args, **kwargs: ["first", "second"])
    monkeypatch.setattr(script, "process_chunk", lambda *args, **kwargs: next(parsed))
    src = tmp_path / "boundary.txt"
    src.write_text(wrapped + "好。", encoding="utf-8")
    handle = Handle()
    result = script.parse_script_file(
        handle, src, LLMConfig(model_name="mock"), PromptsConfig(),
        GenerationConfig(spot_check_enabled=False), output_path=tmp_path / "boundary.json",
    )
    assert len(calls) == 1 and result["suspicious"] == 0
    assert result["split_review_indices"] == [0]
    assert any("失去引号信号" in text for _, text in handle.logs)


def test_alignment_skeleton_equality_fast_path_keeps_decisions(monkeypatch):
    from backend.engines import script
    # Same skeleton, different surface text (quotation marks / punctuation):
    # the fast path returns the exact full-match verdict without difflib.
    source = "「走吧。」\n\n  走吧"
    entries = [{"speaker": "他", "text": "走吧。"}, {"speaker": "旁白", "text": "走吧"}]
    assert script._skeleton(source) == script._skeleton("".join(e["text"] for e in entries))
    def explode(*args, **kwargs):
        raise AssertionError("equal skeletons must not reach difflib")
    monkeypatch.setattr(script.difflib.SequenceMatcher, "__init__", explode)
    result = script.check_chunk_alignment(source, entries)
    assert result["ok"] is True
    assert result["coverage"] == 1.0
    assert result["missing"] == [] and result["extra"] == [] and result["suspicious"] == []
    assert result["source"] == script._skeleton(source)
    monkeypatch.undo()
    # Non-equal text still goes through the original difflib rules: a large
    # gap fails, a small one is tolerated.
    long_source = "用来确保大段缺失仍被完整性校验判定为异常而不是被快速路径放行的长文本。" * 4
    dropped = script.check_chunk_alignment(long_source, [])
    assert dropped["ok"] is False and dropped["missing"]
    unit = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥一二三四五六七八九十"
    small = script.check_chunk_alignment(unit * 3, [{"speaker": "A", "text": unit * 2 + "甲乙丙丁"}])
    assert small["ok"] is True and not small["missing"]
