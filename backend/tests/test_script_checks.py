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


# ---------------------------------------------------------------------------
# Re-judgment batch packing (several windows in one LLM call) and boundary narrowing
# ---------------------------------------------------------------------------

def test_plan_rejudge_packs_budgets_overlap_and_disabled():
    plan = script.plan_rejudge_packs
    spans = [(3, 3), (20, 20), (40, 41), (60, 60)]
    assert plan([1, 1, 2, 1], [100] * 4, max_targets=0, max_chars=9999, max_windows=9) == [[0], [1], [2], [3]]
    assert plan([1, 1, 2, 1], [100] * 4, max_targets=30, max_chars=9999, max_windows=9) == [[0, 1, 2, 3]]
    assert plan([1, 1, 2, 1], [100] * 4, max_targets=3, max_chars=9999, max_windows=9) == [[0, 1], [2, 3]]
    assert plan([1, 1, 2, 1], [400] * 4, max_targets=30, max_chars=900, max_windows=9) == [[0, 1], [2, 3]]
    assert plan([1, 1, 2, 1], [100] * 4, max_targets=30, max_chars=9999, max_windows=2) == [[0, 1], [2, 3]]
    # Neighbouring windows that would overlap (gap <= 2n) are never put in one prompt.
    assert plan([1, 1, 1], [100] * 3, max_targets=30, max_chars=9999, max_windows=9,
                group_spans=[(10, 10), (13, 13), (30, 30)], min_gap=4) == [[0], [1, 2]]
    assert plan([1], [10 ** 6], max_targets=30, max_chars=100, max_windows=9) == [[0]]  # never empty


def test_merge_overlapping_groups_respects_batch_cap():
    merge = script.merge_overlapping_groups
    assert merge([[3], [6], [30]], n=2, batch=20) == [[3, 6], [30]]      # gap 3 <= 2n
    assert merge([[3], [8]], n=2, batch=20) == [[3], [8]]                  # gap 5 > 2n
    assert merge([[3], [6]], n=2, batch=1) == [[3], [6]]                   # cap holds


def test_batch_user_prompt_describes_multiple_windows():
    single = script._batch_user_prompt("{context}", "W", 2, 4)
    multi = script._batch_user_prompt("{context}", "W", 5, 4, windows=3)
    assert "上方窗口含 2 个" in single and "互不连续" not in single
    assert "共 3 个互不连续的窗口" in multi and "合计含 5 个" in multi


def _rejudge_entries(count=40):
    return [entry("NARRATOR" if i % 2 else "甲", f"第{i}条文字。") for i in range(count)]


def _run_rejudge(monkeypatch, entries, groups, replies, **gen):
    seen = []

    def fake_call(llm, generation, messages, handle=None):
        seen.append(messages[1]["content"])
        return replies(len(seen), messages[1]["content"])

    monkeypatch.setattr(script, "_llm_call", fake_call)
    result, stats = script._run_rejudge_groups(
        Handle(), LLMConfig(), GenerationConfig(**gen), "sys", "{context}", entries, groups, 2,
        script.build_roster(entries), stage="测试", progress_base=0.0, progress_span=0.1,
    )
    return result, stats, seen


def _targets_in(prompt):
    """Indices flagged ``"target": true`` anywhere in a (possibly multi-window) prompt."""
    return [it["index"] for part in prompt.split("【窗口")[1:] or [prompt]
            for it in json.loads(part[part.index("["):part.rindex("]") + 1]) if it.get("target")]


def test_run_rejudge_packs_sparse_windows_into_one_call_and_retries_only_undecided(monkeypatch):
    entries = _rejudge_entries()
    groups = [[3], [15], [27]]

    def replies(call_no, prompt):
        # 15 settles on 乙 at the first retry; 27 keeps changing its mind (no majority).
        flip = {1: {15: "乙", 27: "丙"}, 2: {15: "乙", 27: "丁"}}.get(call_no, {27: f"新{call_no}"})
        return json.dumps({"results": [
            {"index": t, "speaker": flip.get(t, entries[t]["speaker"])} for t in _targets_in(prompt)]},
            ensure_ascii=False)

    result, stats, seen = _run_rejudge(monkeypatch, entries, groups, replies)
    assert len(seen) == 4  # 1 packed first pass + 3 retry rounds (the last two only for target 27)
    assert "【窗口 1/3】" in seen[0] and "【窗口 3/3】" in seen[0]
    assert sorted(_targets_in(seen[0])) == [3, 15, 27]
    # Retry 1 re-sends only the windows holding a target without a majority yet.
    assert sorted(_targets_in(seen[1])) == [15, 27]
    assert _targets_in(seen[2]) == [27] and _targets_in(seen[3]) == [27]  # decided windows drop out
    assert "【窗口" not in seen[2]  # a single window is sent without the multi-window header
    # 15 reached 乙:乙 + original → adopted; 27 never repeated → original kept.
    assert result[15]["speaker"] == "乙" and result[27]["speaker"] == entries[27]["speaker"]
    assert stats == {"checked": 3, "fixed": 1, "unwrapped": 0}
    # Only targets may change: every other entry is byte-identical to the input.
    assert [e for i, e in enumerate(result) if i != 15] == [e for i, e in enumerate(entries) if i != 15]


def test_run_rejudge_without_packing_is_one_call_per_group(monkeypatch):
    entries = _rejudge_entries()

    def replies(call_no, prompt):
        return json.dumps({"results": [
            {"index": t, "speaker": entries[t]["speaker"]} for t in _targets_in(prompt)]})

    _result, stats, seen = _run_rejudge(monkeypatch, entries, [[3], [15], [27]], replies,
                                        check_pack_targets=0)
    assert len(seen) == 3 and all("【窗口" not in p for p in seen) and stats["checked"] == 3


def test_run_rejudge_cancel_propagates_from_packed_call(monkeypatch):
    from backend.core.task_control import TaskCancelled

    def replies(call_no, prompt):
        raise TaskCancelled("cancel")

    with pytest.raises(TaskCancelled):
        _run_rejudge(monkeypatch, _rejudge_entries(), [[3], [15]], replies)


def test_boundary_risk_targets_narrow_to_dialogue_near_boundary():
    # Boundary at 6 (chunk_ends 6 | 12); the two sides alternate NARRATOR / character and
    # carry a speaker turn, so the boundary is risky.
    rows = [entry("NARRATOR", "他走进房间。") for _ in range(12)]
    for i in (2, 4, 5, 7):
        rows[i] = entry("甲", "好的，我知道了。")
    wide = script.select_boundary_risk_targets(rows, [6, 12], 4)
    narrow = script.select_boundary_risk_targets(rows, [6, 12], 4, target_window=2)
    assert wide == list(range(2, 10))
    # ±2 around the boundary = {4,5,6,7}; the narrator at 6 survives only because it touches
    # the boundary, 4/5/7 survive because they are dialogue.
    assert narrow == [4, 5, 6, 7]
    assert script.select_boundary_risk_targets(rows, [6, 12], 4, target_window=0) == wide
    assert script.select_boundary_risk_targets(rows, [6, 12], 4, target_window=4) == wide


def _packed_reply(parts_per_entry):
    return json.dumps([p for parts in parts_per_entry for p in parts], ensure_ascii=False)


def _long_rows():
    a = entry("NARRATOR", "他走进房间，" * 12 + "“你好。”" + "她笑了笑，" * 12)   # > 130, hides a quote
    b = entry("NARRATOR", "夜色渐深，" * 30)                                    # > 130, plain narration
    c = entry("NARRATOR", "窗外下着雨，" * 26)
    return [entry("甲", "短句。"), a, entry("乙", "短句。"), b, entry("甲", "短句。"), c]


def test_long_resplit_packs_entries_and_partitions_the_reply(monkeypatch):
    rows = _long_rows()
    a, b, c = rows[1], rows[3], rows[5]
    a1, a2, a3 = a["text"][:72], "“你好。”", a["text"][72 + 4:]
    calls = []

    def fake_call(llm, generation, messages, handle=None):
        calls.append(messages[1]["content"])
        return _packed_reply([
            [{"speaker": "NARRATOR", "text": a1}, {"speaker": "甲", "text": "你好。"},
             {"speaker": "NARRATOR", "text": a3}],
            [{"speaker": "NARRATOR", "text": b["text"]}],
            [{"speaker": "NARRATOR", "text": c["text"]}],
        ])

    monkeypatch.setattr(script, "_llm_call", fake_call)
    gen = GenerationConfig(long_resplit_pack=4)
    out, checked, fixed = script.long_paragraph_resplit(Handle(), LLMConfig(), gen, "sys",
                                                          "{context}\n{chunk}", rows, 130)
    assert len(calls) == 1 and checked == 3  # three long entries, ONE call
    assert "【条目 1/3】" in calls[0] and "【条目 3/3】" in calls[0]
    assert calls[0].count("Entries immediately before it") >= 1
    assert fixed == 3 and [e["speaker"] for e in out].count("甲") == 3  # 2 original + the hidden line split out
    assert script._skeleton("".join(e["text"] for e in out)) == script._skeleton(
        "".join(e["text"] for e in rows))  # nothing lost; only the quote marks were stripped


def test_long_resplit_pack_gates_each_entry_and_falls_back_on_bad_boundaries(monkeypatch):
    rows = _long_rows()
    a, b, c = rows[1], rows[3], rows[5]
    replies = []

    def fake_call(llm, generation, messages, handle=None):
        return replies.pop(0)

    monkeypatch.setattr(script, "_llm_call", fake_call)
    gen = GenerationConfig(long_resplit_pack=4)
    # one entry rewritten (text changed → fails its own gate), the others faithful
    replies.append(_packed_reply([
        [{"speaker": "NARRATOR", "text": "完全改写了。" * 3}],
        [{"speaker": "NARRATOR", "text": b["text"]}],
        [{"speaker": "NARRATOR", "text": c["text"]}],
    ]))
    out, checked, fixed = script.long_paragraph_resplit(Handle(), LLMConfig(), gen, "sys",
                                                          "{context}\n{chunk}", rows, 130)
    assert checked == 3 and out[1] == a  # the bad entry kept unchanged; no mechanical split here
    # A reply whose output boundary straddles two source entries cannot be partitioned: the
    # pack is re-asked entry by entry (3 more calls, each a single-entry answer).
    straddle = [{"speaker": "NARRATOR", "text": a["text"] + b["text"][:10]},
                {"speaker": "NARRATOR", "text": b["text"][10:] + c["text"]}]
    replies[:] = [json.dumps(straddle, ensure_ascii=False)] + [
        json.dumps([{"speaker": "NARRATOR", "text": r["text"]}], ensure_ascii=False)
        for r in (a, b, c)]  # single-entry fallback asks in reading order
    out, checked, fixed = script.long_paragraph_resplit(Handle(), LLMConfig(), gen, "sys",
                                                          "{context}\n{chunk}", rows, 130)
    assert replies == [] and checked == 3  # 1 packed call + 3 single calls, all consumed


def test_long_resplit_pack_one_keeps_one_call_per_entry(monkeypatch):
    rows = _long_rows()
    asked = []

    def fake_call(llm, generation, messages, handle=None):
        asked.append(messages[1]["content"])
        return json.dumps([{"speaker": "NARRATOR", "text": "x"}])

    monkeypatch.setattr(script, "_llm_call", fake_call)
    script.long_paragraph_resplit(Handle(), LLMConfig(), GenerationConfig(long_resplit_pack=1),
                                  "sys", "{context}\n{chunk}", rows, 130)
    assert len(asked) == 3 and all("【条目" not in p for p in asked)


def test_long_resplit_uses_fixed_narrator_instruct_under_units_protocol(monkeypatch):
    text = "她望向窗外，" * 30
    quoted = entry("NARRATOR", text, "原来的旁白语气")
    rows = [quoted]
    reply = json.dumps([{"speaker": "NARRATOR", "text": text, "instruct": "模型自己写的旁白语气"}],
                       ensure_ascii=False)
    monkeypatch.setattr(script, "_llm_call", lambda *a, **k: reply)
    out, _c, fixed = script.long_paragraph_resplit(
        Handle(), LLMConfig(), GenerationConfig(), "sys", "{context}\n{chunk}", rows, 130,
        narrator_instruct="平稳中性的叙述语气。")
    assert fixed == 1 and [e["instruct"] for e in out] == ["平稳中性的叙述语气。"]
    out2, _c, _f = script.long_paragraph_resplit(
        Handle(), LLMConfig(), GenerationConfig(), "sys", "{context}\n{chunk}", rows, 130)
    assert [e["instruct"] for e in out2] == ["模型自己写的旁白语气"]  # JSON protocol keeps the model's


def test_long_resplit_pack_zero_is_one_call_per_chapter_until_the_output_cap(monkeypatch):
    rows = [entry("NARRATOR", f"第{k}段长旁白，" * 24) for k in range(6)]
    calls = []

    def fake_call(llm, generation, messages, handle=None):
        calls.append(messages[1]["content"])
        return json.dumps([{"speaker": "NARRATOR", "text": "x"}])  # unpartitionable → singles follow

    monkeypatch.setattr(script, "_llm_call", fake_call)
    handle = Handle()
    script.long_paragraph_resplit(handle, LLMConfig(), GenerationConfig(long_resplit_pack=0,
                                  max_tokens=100000, chunk_size=20000), "sys", "{context}\n{chunk}", rows, 130)
    assert calls[0].count("【条目 ") == 6 and "6/6" in calls[0]   # 6 entries, ONE packed call
    assert any("共 1 次调用" in m for _l, m in handle.logs)
    calls.clear()
    handle = Handle()
    script.long_paragraph_resplit(handle, LLMConfig(), GenerationConfig(long_resplit_pack=0,
                                  max_tokens=1000, chunk_size=20000), "sys", "{context}\n{chunk}", rows, 130)
    # max_tokens no longer limits a pack: only chunk_size does (20000 → still one pack)
    assert any("共 1 次调用" in m for _l, m in handle.logs)


def test_long_resplit_pack_never_exceeds_the_chunk_budget(monkeypatch):
    rows = [entry("NARRATOR", f"第{k}段长旁白，" * 24) for k in range(6)]   # 168 chars each
    monkeypatch.setattr(script, "_llm_call", lambda *a, **k: json.dumps([{"speaker": "NARRATOR", "text": "x"}]))
    handle = Handle()
    script.long_paragraph_resplit(handle, LLMConfig(), GenerationConfig(long_resplit_pack=0, max_tokens=100000,
                                  chunk_size=1000), "sys", "{context}\n{chunk}", rows, 130)
    # 1000 chars of text per pack (2000 with context): only part of the chapter fits the one allowed pack
    assert any("装不进一个 chunk" in m for _l, m in handle.logs)


def test_long_resplit_pack_raises_max_tokens_for_the_pack_reply(monkeypatch):
    rows = [entry("NARRATOR", f"第{k}段长旁白，" * 24) for k in range(6)]   # ~1000 chars
    seen = []

    def fake_call(llm, generation, messages, handle=None):
        seen.append(generation.max_tokens)
        return json.dumps([{"speaker": "NARRATOR", "text": "x"}])

    monkeypatch.setattr(script, "_llm_call", fake_call)
    script.long_paragraph_resplit(Handle(), LLMConfig(), GenerationConfig(long_resplit_pack=0, max_tokens=1000, chunk_size=20000),
                                  "sys", "{context}\n{chunk}", rows, 130)
    assert seen[0] >= 2 * 1000   # the packed call; later singles use the configured value


# --- 单元协议下的断句校验 / 超长重切（rederive_entries_units） ---

_LQ, _RQ = chr(0x201C), chr(0x201D)


def _label_quotes_fake(speaker, calls, drop_prefix=None):
    """Fake LLM for the unit protocol: label every whole-quote unit as ``speaker`` (and
    optionally delete the plain unit starting with ``drop_prefix``) by reading the numbers
    out of the prompt."""
    import re

    def fake_call(llm, generation, messages, handle=None):
        prompt = messages[1]["content"]
        calls.append(prompt)
        lines = []
        for n, text in re.findall(r"^\[(\d+)\] (.*)$", prompt, re.M):
            if text.startswith(_LQ):
                lines.append(f"{n} {speaker} | 自然的对话语气。")
            elif drop_prefix and text.startswith(drop_prefix):
                lines.append(f"{n} X")
        return "\n".join(lines + ["END"])

    return fake_call


def _long_narration(tag, quoted):
    return ("夜色笼罩着整座城市，" * 8) + f"{_LQ}{quoted}{_RQ}" + ("他转身离开了巷口，" * 6) + tag


def test_units_long_resplit_labels_dialogue_out_of_narration(monkeypatch):
    rows = [entry("姜维", "走吧。"), entry("NARRATOR", _long_narration("。", "我先走了。"))]
    calls = []
    monkeypatch.setattr(script, "_llm_call", _label_quotes_fake("姜维", calls))
    out, checked, fixed = script.long_paragraph_resplit(
        Handle(), LLMConfig(), GenerationConfig(long_resplit_pack=0), "sys", "{context}\n{chunk}", rows, 130,
        narrator_instruct="平稳中性的叙述语气。", unit_prompts=("usys", "{context}\n{units}"))
    assert (checked, fixed) == (1, 1) and len(calls) == 1
    assert "[1] " in calls[0] and "【条目" not in calls[0]       # numbered units, not a JSON rewrite
    assert [e["speaker"] for e in out] == ["姜维", "NARRATOR", "姜维", "NARRATOR"]
    assert out[2]["text"] == "我先走了。"                      # outer quotes stripped mechanically
    assert out[1]["instruct"] == out[3]["instruct"] == "平稳中性的叙述语气。"
    assert script._skeleton("".join(e["text"] for e in out[1:])) == \
        script._skeleton(_long_narration("。", "我先走了。"))   # no word character lost or invented


def test_units_long_resplit_packs_a_chapter_into_one_call(monkeypatch):
    rows = [entry("姜维", "走吧。"), entry("NARRATOR", _long_narration("。", "第一句。")),
            entry("NARRATOR", "短旁白。"), entry("NARRATOR", _long_narration("。", "第二句。"))]
    calls = []
    monkeypatch.setattr(script, "_llm_call", _label_quotes_fake("姜维", calls))
    out, checked, fixed = script.long_paragraph_resplit(
        Handle(), LLMConfig(), GenerationConfig(long_resplit_pack=0), "sys", "{context}\n{chunk}", rows, 130,
        narrator_instruct="平稳中性的叙述语气。", unit_prompts=("usys", "{context}\n{units}"))
    assert (checked, fixed, len(calls)) == (2, 2, 1)            # both flagged entries, ONE call
    assert "【条目 1/2】" in calls[0] and "【条目 2/2】" in calls[0]
    assert [e["text"] for e in out if e["speaker"] == "姜维"] == ["走吧。", "第一句。", "第二句。"]


def test_units_long_resplit_without_labels_changes_nothing(monkeypatch):
    rows = [entry("姜维", "走吧。"), entry("NARRATOR", "没有台词的长旁白，" * 20)]
    monkeypatch.setattr(script, "_llm_call", lambda *a, **k: "END")
    out, checked, fixed = script.long_paragraph_resplit(
        Handle(), LLMConfig(), GenerationConfig(long_resplit_pack=0), "sys", "{context}\n{chunk}", rows, 130,
        narrator_instruct="平稳中性的叙述语气。", unit_prompts=("usys", "{context}\n{units}"))
    assert (checked, fixed) == (1, 0) and out is rows           # a cut-at-soft-max narration is no split


def test_units_rederive_strips_a_tag_between_two_quotes_into_one_entry(monkeypatch):
    item = entry("姜维", f"{_LQ}走吧，{_RQ}姜维说道，{_LQ}快点。{_RQ}")
    calls = []
    monkeypatch.setattr(script, "_llm_call", _label_quotes_fake("姜维", calls, drop_prefix="姜维说道"))
    got = script.rederive_entries_units(
        Handle(), LLMConfig(), GenerationConfig(), ("usys", "{context}\n{units}"), [(item, "")],
        frozenset({"NARRATOR", "姜维"}), stage="断句校验", head="")
    assert got is not None and len(got) == 1
    assert [(p["speaker"], p["text"]) for p in got[0]] == [("姜维", "走吧，快点。")]


def test_units_rederive_rejects_names_outside_the_roster(monkeypatch):
    item = entry("NARRATOR", _long_narration("。", "我先走了。"))
    monkeypatch.setattr(script, "_llm_call", _label_quotes_fake("路人乙", []))
    got = script.rederive_entries_units(
        Handle(), LLMConfig(), GenerationConfig(), ("usys", "{context}\n{units}"), [(item, "")],
        frozenset({"NARRATOR", "姜维"}), stage="超长段落重切", head="")
    assert got == [None]                                        # invented speaker → entry untouched


def test_long_resplit_sends_at_most_one_chunk_per_chapter_longest_first(monkeypatch):
    rows = []
    for k in range(12):   # 12 long entries; lengths grow with k, so the longest are the last ones
        rows.append(entry("NARRATOR", f"第{k}段长旁白，" * (24 + 4 * k)))
    calls = []

    def fake_call(llm, generation, messages, handle=None):
        calls.append(messages[1]["content"])
        return json.dumps([{"speaker": "NARRATOR", "text": "x"}])

    monkeypatch.setattr(script, "_llm_call", fake_call)
    handle = Handle()
    out, checked, fixed = script.long_paragraph_resplit(
        handle, LLMConfig(), GenerationConfig(long_resplit_pack=0, chunk_size=3000, max_tokens=100000),
        "sys", "{context}\n{chunk}", rows, 130)
    packed_first = calls[0]
    assert checked < 12 and packed_first.count("【条目 ") == checked   # one pack, a subset of the chapter
    assert any("装不进一个 chunk" in m for _l, m in handle.logs)
    assert "第11段长旁白" in packed_first and "第0段长旁白" not in packed_first   # longest kept, shortest dropped
    first_pack_calls = [c for c in calls if "【条目 " in c]
    assert len(first_pack_calls) == 1                        # never a second packed call


def test_long_resplit_whole_chapter_that_fits_one_chunk_is_unchanged(monkeypatch):
    rows = [entry("NARRATOR", f"第{k}段长旁白，" * 24) for k in range(3)]
    calls = []
    monkeypatch.setattr(script, "_llm_call",
                        lambda l, g, m, handle=None: calls.append(m[1]["content"]) or json.dumps(
                            [{"speaker": "NARRATOR", "text": "x"}]))
    handle = Handle()
    _o, checked, _f = script.long_paragraph_resplit(
        handle, LLMConfig(), GenerationConfig(long_resplit_pack=0, chunk_size=20000, max_tokens=100000),
        "sys", "{context}\n{chunk}", rows, 130)
    assert checked == 3 and not any("装不进一个 chunk" in m for _l, m in handle.logs)


def test_instruct_targets_are_split_into_bounded_batches():
    rows = [entry("甲", "一句话。" * 20) for _ in range(40)]
    one = script._instruct_target_batches(rows, [5], 4, 6000)
    assert one == [[5]]
    many = script._instruct_target_batches(rows, list(range(0, 40, 4)), 4, 3000)
    assert len(many) > 1 and sorted(i for b in many for i in b) == list(range(0, 40, 4))
    assert script._instruct_target_batches(rows, [3, 20], 4, 10) == [[3], [20]]   # each target alone fits no cap


def test_rejudge_window_over_the_chunk_budget_is_halved(monkeypatch):
    rows = [entry("甲" if k % 2 else "乙", "很长的一句话，" * 25) for k in range(30)]   # 175 chars each
    asked = []

    def fake_call(llm, generation, messages, handle=None):
        asked.append(messages[1]["content"])
        return json.dumps([])

    monkeypatch.setattr(script, "_llm_call", fake_call)
    gen = GenerationConfig(check_pack_targets=0, chunk_size=1000)   # 2000-char window cap
    script._run_rejudge_groups(Handle(), LLMConfig(), gen, "sys", "{context}", rows,
                               [list(range(10, 20))], 1, frozenset({"甲", "乙"}),
                               stage="归属抽样", progress_base=0.9, progress_span=0.01)
    assert len(asked) > 1   # one 10-target group would not fit 2000 chars of window text
    assert all(len(p) < 4000 for p in asked)


def test_units_rederive_unlabeled_reply_never_demotes_a_character_entry(monkeypatch):
    item = entry("姜维", f"{_LQ}我先走了，{_RQ}他说。" * 3)
    monkeypatch.setattr(script, "_llm_call", lambda *a, **k: "END")   # the model labels nothing
    got = script.rederive_entries_units(
        Handle(), LLMConfig(), GenerationConfig(), ("usys", "{context}\n{units}"), [(item, "")],
        frozenset({"NARRATOR", "姜维"}), stage="断句校验", head="")
    assert got == [None]


def test_long_resplit_longest_entry_always_gets_the_pack_even_over_the_budget(monkeypatch):
    huge = entry("NARRATOR", "超长的一整段旁白，" * 400)                  # 3600 chars: alone over a 3000 budget
    rows = [huge] + [entry("NARRATOR", f"第{k}段长旁白，" * 24) for k in range(3)]
    calls = []
    monkeypatch.setattr(script, "_llm_call", lambda l, g, m, handle=None: calls.append(m[1]["content"]) or "[]")
    _o, checked, _f = script.long_paragraph_resplit(
        Handle(), LLMConfig(), GenerationConfig(long_resplit_pack=0, chunk_size=3000, max_tokens=100000),
        "sys", "{context}\n{chunk}", rows, 130)
    assert checked == 1 and "超长的一整段旁白" in calls[0]        # the longest, not a shorter neighbour


def test_units_long_resplit_single_entry_pack_is_not_asked_twice(monkeypatch):
    rows = [entry("姜维", "走吧。"), entry("NARRATOR", _long_narration("。", "我先走了。"))]
    calls = []

    def boom(llm, generation, messages, handle=None):
        calls.append(1)
        raise RuntimeError("HTTP 400 context_length_exceeded")

    monkeypatch.setattr(script, "_llm_call", boom)
    _o, checked, fixed = script.long_paragraph_resplit(
        Handle(), LLMConfig(), GenerationConfig(long_resplit_pack=0), "sys", "{context}\n{chunk}", rows, 130,
        narrator_instruct="平稳中性的叙述语气。", unit_prompts=("usys", "{context}\n{units}"))
    assert (checked, fixed, len(calls)) == (1, 0, 1)
