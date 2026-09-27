"""Offline tests for the script-generation engine (``backend/engines/script.py``).

These pin the faithful port of the source ``generate_script.py`` JSON pipeline —
chunking, response cleaning, array repair, and regex salvage — against known inputs,
with no network access. The LLM transport (streaming + non-streaming) is unit-tested
here against a mocked ``urllib.request.urlopen`` (no real HTTP). Mirrors the style and
focus of ``test_text.py``.
"""
from __future__ import annotations

import io
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request

import pytest

from backend.core import config as core_config
from backend.core import concurrency
from backend.core import paths as core_paths
from backend.core.config import GenerationConfig, LLMConfig, PromptsConfig
from backend.tests.task_support import TERMINAL, TaskCancelled, TaskManager, TaskStatus
from backend.engines.llm_transport import (
    LLMHTTPError,
    LLMJSONRetryExhausted,
    ParseRejected,
    llm_json_with_retry,
    request_chat_completion_stream,
)
from backend.engines.script import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_PROMPT,
    INSTRUCT_MAX_WORDS,
    adaptive_spot_rate,
    SPOT_CHECK_HISTORY_CAP,
    _ALIGN_FAIL_MIN,
    _ALIGN_SUSPICIOUS_MIN,
    _append_spot_history,
    _has_attribution_tag,
    _is_pure_saying_tag,
    _llm_chat_completion,
    _load_spot_history,
    _parse_entries_reply,
    _pick_majority,
    _quote_parity,
    _reparse_vote,
    _risk_tier,
    _strip_leading_saying_tag,
    _tag_in,
    absorb_punct_entries,
    boundary_check_speakers,
    build_batch_window,
    check_chunk_alignment,
    check_chunk_fidelity,
    clean_json_string,
    delete_pure_saying_tags,
    fix_mojibake,
    generate_file,
    group_retry_indices,
    is_suspicious_entry_text,
    instruct_entry_indices,
    instruct_word_count,
    long_entry_indices,
    long_paragraph_resplit,
    merge_adjacent_same_speaker,
    parse_speaker,
    parse_speaker_map_full,
    process_chunk,
    revalidate_entry,
    repair_json_array,
    salvage_json_entries,
    select_boundary_targets,
    select_boundary_risk_targets,
    select_spot_targets,
    spot_budget,
    spot_check_speakers,
    split_chunk_balanced,
    split_into_chunks,
    split_long_entries,
    split_long_text,
    strip_outer_quotes,
    suspicious_entry_indices,
    validate_sentence_splits,
    validate_instructs,
)
from backend.engines.text import is_chapter_title

BS = chr(92)  # backslash — built via chr() so no literal backslashes live in this file
LQ, RQ = chr(0x201C), chr(0x201D)  # curly double quotes — via chr() (hand-typed quotes are unreliable)
SQ = chr(0x0022)  # straight double quote — via chr()


def _entry(speaker: str, text: str, instruct: str = "tone") -> dict:
    return {"speaker": speaker, "text": text, "instruct": instruct}


# --------------------------------------------------------------------------- #
# split_into_chunks
# --------------------------------------------------------------------------- #

def test_split_by_paragraphs():
    assert split_into_chunks("para one\n\npara two\n\npara three", max_size=10) == [
        "para one", "para two", "para three",
    ]


def test_split_packs_small_paragraphs():
    assert split_into_chunks("aa\n\nbb\n\ncc", max_size=100) == ["aa\n\nbb\n\ncc"]


def test_split_long_paragraph_by_sentences():
    # 17 字 @8：段数 ceil(17/8)=3 → 固定切法尾段 1 字 < 4（半长）→ 减一为 2 段，
    # 目标均长 8.5，最近合法边界（句末+空白）= 11 → 两段尽量平均。
    assert split_into_chunks("aaaa. bbbb. cccc.", max_size=8) == ["aaaa. bbbb.", "cccc."]


def test_split_empty():
    assert split_into_chunks("") == []
    assert split_into_chunks("   \n\n  ", max_size=10) == []


def test_split_preserves_content():
    samples = [
        "para one\n\npara two\n\npara three",
        "aaaa. bbbb. cccc.",
        "第一段。\n\n第二段。句子一。句子二。\n\n第三段。",
    ]
    for src in samples:
        chunks = split_into_chunks(src, max_size=20)
        assert re.sub(r"\s", "", src) == re.sub(r"\s", "", "\n\n".join(chunks)), repr(src)


def _paras(n, ch, width=100):
    """n 个等宽段落（每段 width 个相同字符）拼成的正文（段落间空行）。"""
    return "\n\n".join(ch * width for _ in range(n))


def test_split_count_rule_evening_out_short_tail():
    # 段数公式四例（每段 100 字、段间 2 字空行；长度 = strip 后）：
    # ① 31 段 = 3160 字 @1500：ceil → 3，固定切法尾段 160 < 750 → 2 段均分
    # ② 38 段 = 3874 字 @1500：ceil → 3，尾段 874 ≥ 750 → 保持 3 段
    # ③ 45 段 = 4588 字 @1500：ceil → 4，尾段 88 < 750 → 3 段均分
    # ④ 46 段 = 4690 字 @1500：ceil → 4，尾段 190 < 750 → 3 段均分（而非 1500/1500/1600）
    assert [len(c) for c in split_into_chunks(_paras(31, "甲"), max_size=1500)] == [1630, 1528]
    assert [len(c) for c in split_into_chunks(_paras(38, "乙"), max_size=1500)] == [1324, 1222, 1324]
    assert [len(c) for c in split_into_chunks(_paras(45, "丙"), max_size=1500)] == [1528, 1528, 1528]
    assert [len(c) for c in split_into_chunks(_paras(46, "丁"), max_size=1500)] == [1528, 1630, 1528]


def test_split_clamps_count_to_boundary_count():
    # 段数公式给 4（ceil(30/8)），但内部合法边界仅 1 个（段间空行）→ 钳回 2 段：
    # 目标均长只是软目标，合法结构边界是最高约束（无边界处绝不强切）。
    src = "夜色像潮水一样漫进街巷，行人渐稀。\n\n巷口的灯一盏盏亮起来。"
    assert len(split_into_chunks(src, max_size=8)) == 2


def test_split_cjk_sentence_boundaries():
    # 无空格中文句：CJK 句末标点 。！？!?… 是零宽合法边界（不要求后随空白）——
    # 旧正则 (?<=[.!?])\s+ 对中文从不触发，超长中文段从此可切。
    src = "句子一。句子二。句子三。句子四。"
    chunks = split_into_chunks(src, max_size=8)
    assert chunks == ["句子一。句子二。", "句子三。句子四。"]
    assert all(c.endswith("。") for c in chunks)


def test_split_ascii_mid_token_dot_protected():
    # ASCII .!? 须后随空白才是句末边界——3.14 的词内点号绝不可成为切点。
    # 4 字尾段「dddd」< 半长 5 → 收尾 pass 并入左邻（只删切点）；
    # 「3.14」始终完整地位于同一块内。
    src = "aaaa. bbbb 3.14 cccc. dddd"
    chunks = split_into_chunks(src, max_size=10)
    assert chunks == ["aaaa.", "bbbb 3.14 cccc. dddd"]
    assert "3.14" in chunks[1]


def test_split_short_tail_merged():
    # 固定切法会留下 10 字尾段（< 半长 100）→ 收尾 pass 删掉相邻切点、
    # 与较短邻块合并（只删切点，不引入新切点）→ 2 段。
    src = "甲" * 300 + "\n\n" + "乙" * 300 + "\n\n" + "丙" * 10
    chunks = split_into_chunks(src, max_size=200)
    assert [len(c) for c in chunks] == [300, 312]
    assert chunks[1] == "乙" * 300 + "\n\n" + "丙" * 10


def test_split_keeps_lone_tail_title_block():
    # 书末短标题块（6 字 < 半长 10）无法与左邻合并（合并块末行成标题 = 悬题）
    # → 保留短块（允许，非异常）——标题独立成块，绝不丢失。
    title = "第十章 舞会"
    src = "甲" * 30 + "\n\n" + title
    chunks = split_into_chunks(src, max_size=20)
    assert chunks == ["甲" * 30, title]


# --------------------------------------------------------------------------- #
# clean_json_string
# --------------------------------------------------------------------------- #

def test_clean_strips_code_fence():
    assert clean_json_string('```json\n[{"a":1}]\n```') == '[{"a":1}]'


def test_clean_strips_thinking_tags():
    assert clean_json_string('<thinking>blah</thinking>[{"a":1}] tail') == '[{"a":1}]'


def test_clean_bracket_counter_with_noise():
    assert clean_json_string('x [{"a":1},{"b":2}] y') == '[{"a":1},{"b":2}]'


def test_clean_salvages_unclosed_array():
    assert clean_json_string('noise [{"a":1},{"b":2}') == '[{"a":1}]'


def test_clean_returns_none_without_array():
    assert clean_json_string("no brackets here") is None


def test_clean_escapes_control_chars_in_strings():
    raw = '[{"speaker":"N","text":"a\nb","instruct":"i"}]'  # \n is a real newline
    cleaned = clean_json_string(raw)
    assert json.loads(cleaned)[0]["text"] == "a\nb"


# --------------------------------------------------------------------------- #
# repair_json_array
# --------------------------------------------------------------------------- #

def test_repair_valid_array():
    assert repair_json_array('[{"speaker":"N","text":"a","instruct":"b"}]') == [_entry("N", "a", "b")]


def test_repair_missing_comma():
    got = repair_json_array(
        '[{"speaker":"A","text":"1","instruct":"i"}'
        '{"speaker":"B","text":"2","instruct":"j"}]'
    )
    assert [e["speaker"] for e in got] == ["A", "B"]


def test_repair_trailing_comma():
    assert repair_json_array('[{"speaker":"A","text":"1","instruct":"i"},]') == [_entry("A", "1", "i")]


def test_repair_drops_non_dict_and_logs():
    logs = []
    got = repair_json_array('[{"speaker":"A","text":"1","instruct":"i"}, "stray"]', log=logs.append)
    assert got == [_entry("A", "1", "i")]
    assert logs == ["Dropped 1 non-object entries from LLM JSON array"]


def test_repair_drops_non_dict_without_log():
    # No ``log`` callback: the drop path must not crash (the ``and log`` guard).
    assert repair_json_array('[{"speaker":"A","text":"1","instruct":"i"}, "stray"]') == [_entry("A", "1", "i")]


def test_repair_unparseable():
    assert repair_json_array("not json at all") is None
    assert repair_json_array("") is None


# --------------------------------------------------------------------------- #
# salvage_json_entries
# --------------------------------------------------------------------------- #

def test_salvage_single_entry():
    got = salvage_json_entries('noise [{"speaker":"NARRATOR","text":"他说","instruct":"calm"}, garbage')
    assert got == [_entry("NARRATOR", "他说", "calm")]


def test_salvage_unescapes_text():
    raw = '{"speaker":"N","text":"a' + BS + 'nb","instruct":"i"}'  # text value is a\nb (escaped)
    got = salvage_json_entries(raw)
    assert got[0]["text"] == "a\nb"


def test_salvage_none_when_no_match():
    assert salvage_json_entries("no objects here") is None


# --------------------------------------------------------------------------- #
# fix_mojibake
# --------------------------------------------------------------------------- #

def test_fix_mojibake_noop_on_clean_text():
    assert fix_mojibake("plain text 123 中文") == "plain text 123 中文"


def test_fix_mojibake_replaces_ellipsis():
    # The CP1252-as-UTF8 mojibake of "…" (E2 80 A6) is mapped back to the real ellipsis.
    assert fix_mojibake("aâ€¦b") == "a…b"


# --------------------------------------------------------------------------- #
# Bundled default prompts
# --------------------------------------------------------------------------- #

def test_default_prompts_loaded():
    assert DEFAULT_SYSTEM_PROMPT.strip()
    assert "{context}" in DEFAULT_USER_PROMPT
    assert "{chunk}" in DEFAULT_USER_PROMPT


# --------------------------------------------------------------------------- #
# LLM transport — mocked urllib.request.urlopen (no real HTTP)
# --------------------------------------------------------------------------- #

class _StreamResp:
    """Iterable stand-in for a streaming ``chat/completions`` response (SSE lines)."""

    def __init__(self, lines: list):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _BodyResp:
    """Stand-in for a non-streaming response: one ``read()`` of the full JSON body."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sse_frame(obj) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


class _Handle:
    """Minimal TaskHandle: records llm_chunk calls; ``cancelled`` is fixed per test."""

    def __init__(self, cancelled: bool = False):
        self.cancelled = cancelled
        self.chunks: list = []

    def llm_chunk(self, text: str) -> None:
        self.chunks.append(text)

    def log(self, *a, **k):
        pass

    def check(self):
        if self.cancelled:
            raise TaskCancelled("test handle cancelled")

    def progress(self, *a, **k):
        pass

    def phase(self, *a):
        pass  # display-neutral stage marker (generate_file's slot-scope handoff)


class _LogHandle(_Handle):
    """A ``_Handle`` that also records ``log()`` calls as ``(level, msg)`` pairs."""

    def __init__(self):
        super().__init__()
        self.logs: list = []

    def log(self, msg, level=None):
        self.logs.append((level or "INFO", str(msg)))


def test_stream_llm_accumulates_and_forwards(monkeypatch):
    deltas = ["[",
              '{"speaker":"N","text":"他说","instruct":"calm"},',
              '{"speaker":"B","text":"好","instruct":"loud"}',
              "]"]
    full = "".join(deltas)
    lines = [_sse_frame({"choices": [{"delta": {"role": "assistant", "content": ""}}]})]
    lines += [_sse_frame({"choices": [{"delta": {"content": d}}]}) for d in deltas]
    lines.append(_sse_frame({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    lines.append(_sse_frame({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}))
    lines.append(b"data: [DONE]\n")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _StreamResp(lines))
    handle = _Handle()

    content, finish_reason, usage = request_chat_completion_stream(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
        handle=handle,
    )

    # Accumulated deltas, stripped — identical to what the non-streaming call returns.
    assert content == full
    assert finish_reason == "stop"
    assert usage == {"prompt_tokens": 5, "completion_tokens": 7}
    # Coalesced forwards lose nothing (the 「流式反馈」 panel sees the whole stream).
    assert "".join(handle.chunks) == full
    assert handle.chunks  # at least one flush reached the UI
    # The accumulated content still flows through the unchanged JSON pipeline.
    assert clean_json_string(content) == full
    assert repair_json_array(full) == [
        {"speaker": "N", "text": "他说", "instruct": "calm"},
        {"speaker": "B", "text": "好", "instruct": "loud"},
    ]


def test_stream_llm_none_handle_still_works(monkeypatch):
    # ``handle=None`` (tests / no UI) must still accumulate and return the content.
    lines = [
        _sse_frame({"choices": [{"delta": {"content": "hello "}}]}),
        _sse_frame({"choices": [{"delta": {"content": "world"}}]}),
        _sse_frame({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _StreamResp(lines))

    content, finish_reason, usage = request_chat_completion_stream(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
        handle=None,
    )
    assert content == "hello world"
    assert finish_reason == "stop"
    assert usage is None  # no usage frame sent


def test_stream_llm_reasoning_shown_but_not_returned(monkeypatch):
    # A reasoning model (e.g. Qwen3 "thinking") streams its working in
    # ``delta.reasoning_content`` before the answer in ``delta.content``: the panel
    # (llm_chunk) must show both, but the returned content is the answer only, so the
    # JSON pipeline never sees the thinking text.
    lines = [
        _sse_frame({"choices": [{"delta": {"reasoning_content": "Thinking hard "}}]}),
        _sse_frame({"choices": [{"delta": {"reasoning_content": "about the answer"}}]}),
        _sse_frame({"choices": [{"delta": {"content": '[{"a":1}]'}}]}),
        _sse_frame({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _StreamResp(lines))
    handle = _Handle()

    content, finish_reason, _usage = request_chat_completion_stream(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
        handle=handle,
    )

    # Returned content = the answer only (no thinking) — JSON pipeline unaffected.
    assert content == '[{"a":1}]'
    assert finish_reason == "stop"
    # The panel saw reasoning + answer in arrival order (the live "working" state).
    shown = "".join(handle.chunks)
    assert shown == 'Thinking hard about the answer[{"a":1}]'
    assert "Thinking hard " in shown
    assert "Thinking hard " not in content


def test_nonstream_llm_strips_content(monkeypatch):
    full = '[{"speaker":"N","text":"a","instruct":"b"}]'
    payload = json.dumps({
        "choices": [{"message": {"content": "  " + full + "  "}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _BodyResp(payload))

    content, finish_reason, usage = _llm_chat_completion(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
    )
    # The non-streaming path strips the content; the streaming path matches it.
    assert content == full
    assert finish_reason == "stop"
    assert usage == {"prompt_tokens": 3, "completion_tokens": 4}


def _ok_payload(content: str = '{"scene": []}') -> bytes:
    return json.dumps({
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }, ensure_ascii=False).encode("utf-8")


def _http_error(code: int, detail: str = "bad request") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x/v1/chat/completions", code, "error", {},
        io.BytesIO(detail.encode("utf-8")),
    )


def test_nonstream_llm_extra_body_merged_top_level(monkeypatch):
    # openai-SDK ``extra_body`` parity: keys are merged at the TOP level of the
    # request body; omitting ``extra_body`` leaves the default command shape
    # byte-identical (the existing cmd-shape pins rely on that).
    seen = []

    def urlopen(req, *a, **k):
        seen.append(json.loads(req.data.decode("utf-8")))
        return _BodyResp(_ok_payload())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    _llm_chat_completion(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
        extra_body={"enable_thinking": False},
    )
    _llm_chat_completion(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
    )
    assert seen[0]["enable_thinking"] is False  # top level, never nested
    assert "enable_thinking" not in seen[1]  # default shape unchanged


def test_nonstream_llm_http400_falls_back_without_extra_body(monkeypatch):
    # A strict gateway rejects the extra key (HTTP 400) → the call is
    # transparently re-issued ONCE without it; the plain request's result wins.
    seen = []

    def urlopen(req, *a, **k):
        body = json.loads(req.data.decode("utf-8"))
        seen.append(body)
        if "enable_thinking" in body:
            raise _http_error(400, "unknown parameter: enable_thinking")
        return _BodyResp(_ok_payload())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    content, finish_reason, _usage = _llm_chat_completion(
        "http://x/v1", "key", "model",
        [{"role": "user", "content": "hi"}],
        temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
        extra_body={"enable_thinking": False},
    )
    assert content == '{"scene": []}' and finish_reason == "stop"
    assert len(seen) == 2
    assert seen[0]["enable_thinking"] is False
    assert "enable_thinking" not in seen[1]  # the retry drops the extra key


def test_nonstream_llm_http400_without_extra_body_propagates(monkeypatch):
    # No extra_body → nothing to fall back to; the error surfaces with the
    # historical "LLM HTTP {code}: {detail}" wording (now as LLMHTTPError).
    calls = []

    def urlopen(req, *a, **k):
        calls.append(1)
        raise _http_error(400, "model not found")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(LLMHTTPError) as ei:
        _llm_chat_completion(
            "http://x/v1", "key", "model",
            [{"role": "user", "content": "hi"}],
            temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
        )
    assert ei.value.status == 400
    assert str(ei.value) == "LLM HTTP 400: model not found"
    assert len(calls) == 1  # no redundant retry


def test_nonstream_llm_http429_with_extra_body_not_fallback(monkeypatch):
    # Only 400/422 (unknown-parameter rejection) trigger the fallback — a 429
    # rate limit surfaces immediately with exactly one request.
    calls = []

    def urlopen(req, *a, **k):
        calls.append(1)
        raise _http_error(429, "rate limited")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(LLMHTTPError) as ei:
        _llm_chat_completion(
            "http://x/v1", "key", "model",
            [{"role": "user", "content": "hi"}],
            temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
            extra_body={"enable_thinking": False},
        )
    assert ei.value.status == 429
    assert len(calls) == 1


def test_llm_json_with_retry_forwards_extra_body_and_budget():
    # The structured-output retry surface forwards ``extra_body`` (thinking off)
    # and the caller's ``max_tokens`` to EVERY attempt — a thinking model must
    # not be able to starve the answer budget on the small-JSON surfaces.
    seen = []

    def fake_llm(*args, **kw):
        seen.append(kw)
        return ('{"scene": []}', "stop", None)

    parsed, attempts = llm_json_with_retry(
        LLMConfig(model_name="model"), "sys", "user",
        lambda c: json.loads(c) if c else None,
        llm_call=fake_llm, max_attempts=2, max_tokens=2048,
        extra_body={"enable_thinking": False},
    )
    assert parsed == {"scene": []} and attempts == 1
    assert seen[0]["max_tokens"] == 2048
    assert seen[0]["extra_body"] == {"enable_thinking": False}

    # Default (no extra_body) → None is forwarded; the transport then adds
    # nothing to the body (default command shape unchanged).
    seen.clear()
    llm_json_with_retry(
        LLMConfig(model_name="model"), "sys", "user",
        lambda c: json.loads(c) if c else None,
        llm_call=fake_llm,
    )
    assert seen[0]["extra_body"] is None
    assert seen[0]["max_tokens"] == 512  # the surface's default budget


def test_llm_json_with_retry_threads_parse_rejected_reason():
    # ``parse → ParseRejected(reason)`` = 已知、模型可执行的违规 → 具体原因透传进
    # last_err +【重试】反馈 + 耗尽异常（确定性模型能按原因自我纠正，而非裸
    # ``parse → None`` 的同形重掷）。2026-09 事故：章末批 end_segment 越上界
    # → 3 次同形拒收 → 稳定失败。
    users: list[str] = []

    def fake_llm(base_url, api_key, model, messages, **kw):
        users.append(messages[1]["content"])
        return ('[{"start_segment": 0, "end_segment": 99}]', "stop", None)

    reason = "第 1 个块 end_segment=99 超出本批上界 9，已钳制到 9"

    def parse(content):
        return ParseRejected(reason)

    with pytest.raises(LLMJSONRetryExhausted) as ei:
        llm_json_with_retry(
            LLMConfig(model_name="model"), "sys", "user", parse,
            llm_call=fake_llm, max_attempts=3, max_tokens=2048,
        )
    assert len(users) == 3  # 三轮全拒
    assert users[0] == "user"  # 首轮不带反馈
    assert "【重试】" in users[1]
    assert f"问题：回复不可解析（{reason}）" in users[1]
    # 耗尽异常文案同样携带具体原因（任务 error 字段可见）
    assert f"回复不可解析（{reason}）" in str(ei.value)

    # 裸 ``parse → None`` 保持旧文案（不带括号）——既有子串断言零改动
    users.clear()

    def parse_none(content):
        return None

    with pytest.raises(LLMJSONRetryExhausted) as ei2:
        llm_json_with_retry(
            LLMConfig(model_name="model"), "sys", "user", parse_none,
            llm_call=fake_llm, max_attempts=2, max_tokens=2048,
        )
    assert "问题：回复不可解析" in users[1]
    assert "问题：回复不可解析（" not in users[1]
    assert "回复不可解析（" not in str(ei2.value)

    # 首轮 ParseRejected、次轮通过 → 采纳次轮结果（a 与 None 语义区分的关键）
    users.clear()
    calls: list[int] = []

    def parse_flip(content):
        calls.append(1)
        return ParseRejected("违规X") if len(calls) == 1 else {"ok": True}

    parsed, attempts = llm_json_with_retry(
        LLMConfig(model_name="model"), "sys", "user", parse_flip,
        llm_call=fake_llm, max_attempts=2, max_tokens=2048,
    )
    assert parsed == {"ok": True} and attempts == 2
    assert "问题：回复不可解析（违规X）" in users[1]


def test_stream_cancel_propagates_through_process_chunk(monkeypatch):
    # A cancel raised mid-stream must escape process_chunk (not be swallowed/retried
    # by the generic ``except Exception``).
    lines = [_sse_frame({"choices": [{"delta": {"content": "x"}}]})]
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _StreamResp(lines))
    handle = _Handle(cancelled=True)  # already cancelled -> raise on the first frame

    with pytest.raises(TaskCancelled):
        process_chunk(handle, LLMConfig(stream=True), "model", "chunk text", 1, 1)


def test_stream_http404_propagates_through_process_chunk(monkeypatch):
    # 4xx (model_not_found / bad key) must escape process_chunk as LLMHTTPError:
    # the old ``except Exception`` swallowed it into an empty "successful" chunk,
    # which is what produced the fake progress + whole-file retry loop.
    def urlopen(req, *a, **k):
        raise _http_error(404, "model_not_found")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    handle = _LogHandle()

    with pytest.raises(LLMHTTPError) as ei:
        process_chunk(handle, LLMConfig(stream=True), "model", "chunk text", 1, 1)
    assert ei.value.status == 404
    assert "model_not_found" in str(ei.value)
    # No per-chunk "LLM API error" noise was logged (the error is not a chunk retry).
    assert not any("调用 LLM API 出错" in msg for _level, msg in handle.logs)


# --------------------------------------------------------------------------- #
# 章标题防丢（split_into_chunks 尾部标题守卫）
# --------------------------------------------------------------------------- #

def test_split_evicts_trailing_title_to_next_chunk():
    p1 = "旁" * 80
    title = "第十章 舞会"
    p2 = "景" * 80
    src = p1 + "\n\n" + title + "\n\n" + p2
    chunks = split_into_chunks(src, max_size=100)
    # p1 (80) + title (6) fits one chunk (88) but p2 (80) doesn't -> the chunk
    # closes with the title at its tail -> the title moves to the NEXT chunk's head
    # (stays with the chapter body it heads, far from truncation risk).
    assert chunks == [p1, title + "\n\n" + p2]


def test_split_keeps_lone_title_chunk():
    p1 = "旁" * 100
    title = "第十一章 雨"
    p2 = "景" * 100
    src = p1 + "\n\n" + title + "\n\n" + p2
    chunks = split_into_chunks(src, max_size=100)
    # 210 字 @100：段数 3 → 固定切法尾段 10 < 50 → 减一为 2 段，目标均长 105。
    # 最近边界（108，标题之后）悬题（切后块末行是标题）→ 改判取次近边界 100
    # （标题之前）——标题恒随其统领正文同段，不再独占一块。
    assert chunks == [p1, title + "\n\n" + p2]


# --------------------------------------------------------------------------- #
# 忠实性校验（check_chunk_fidelity）
# --------------------------------------------------------------------------- #

def test_fidelity_passes_on_faithful_output():
    chunk = f"他道：{LQ}这条路没有尽头。{RQ}\n\n她答：{LQ}那就一直走。{RQ}"
    entries = [
        {"speaker": "NARRATOR", "text": "他道"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}"},
        {"speaker": "NARRATOR", "text": "她答"},
        {"speaker": "B", "text": f"{LQ}那就一直走。{RQ}"},
    ]
    assert check_chunk_fidelity(chunk, entries) == []


def test_fidelity_catches_dropped_line():
    chunk = f"他道：{LQ}这条路没有尽头。{RQ}\n\n她答：{LQ}那就一直走。{RQ}"
    entries = [
        {"speaker": "NARRATOR", "text": "他道"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}"},
    ]
    # the second quoted passage is absent from every output text -> reported by skeleton
    assert check_chunk_fidelity(chunk, entries) == ["那就一直走"]


def test_fidelity_immune_to_quote_and_tag_removal():
    # 引号被剥离、语气标签并入旁白 —— 骨架不变 -> 不误报
    chunk = f"他道：{LQ}这条路没有尽头。{RQ}\n\n她答：{LQ}那就一直走。{RQ}"
    entries = [
        {"speaker": "NARRATOR", "text": "他道，她答"},
        {"speaker": "A", "text": "这条路没有尽头"},
        {"speaker": "B", "text": "那就一直走。"},
    ]
    assert check_chunk_fidelity(chunk, entries) == []


def test_fidelity_ignores_short_and_empty_quotes():
    # <4 词字符的引语不作保真信号（单字感叹不值得触发重发）
    chunk = f"他喊：{LQ}走。{RQ}"
    assert check_chunk_fidelity(chunk, [{"speaker": "A", "text": "随便什么内容。"}]) == []
    assert check_chunk_fidelity("", [{"speaker": "A", "text": "x"}]) == []


# --------------------------------------------------------------------------- #
# 对半切开（split_chunk_balanced）
# --------------------------------------------------------------------------- #

def test_check_chunk_alignment_large_block_loss_fails():
    # 规则 4/5：连续未匹配 > 100 字（骨架）→ 完整性异常 → ok False。源侧大段缺失
    # 进 missing、输出侧大段新增进 extra，各侧独立判。
    source = "开篇" + "甲" * 100 + "收束" + "乙" * 125
    missing = check_chunk_alignment(source, [
        {"speaker": "NARRATOR", "text": "开篇" + "甲" * 100 + "收束" + "乙" * 20},
    ])
    assert missing["ok"] is False
    assert any(len(g) > _ALIGN_FAIL_MIN for g in missing["missing"])

    extra = check_chunk_alignment("开篇" + "甲" * 100, [
        {"speaker": "NARRATOR", "text": "开篇" + "甲" * 100 + "乙" * 125},
    ])
    assert extra["ok"] is False
    assert any(len(g) > _ALIGN_FAIL_MIN for g in extra["extra"])
    assert not extra["missing"]


def test_check_chunk_alignment_suspicious_band_passes():
    # 规则 4：50 < 连续未匹配 ≤ 100 字 → 标记可疑：仍 ok True（不阻塞、不触发
    # 恢复阶梯），区段暴露在 suspicious 供日志，不进 missing/extra。
    res = check_chunk_alignment("甲" * 100 + "乙" * 60, [
        {"speaker": "NARRATOR", "text": "甲" * 100},
    ])
    assert res["ok"] is True
    assert not res["missing"] and not res["extra"]
    assert any(_ALIGN_SUSPICIOUS_MIN < len(g) <= _ALIGN_FAIL_MIN
               for g in res["suspicious"])


def test_check_chunk_alignment_small_gaps_ignored():
    # 规则 1/3：≤ 50 字连续未匹配（短标签删除 / 代词替换 / 轻微整理）→ 忽略，
    # 不累计、不报。
    res = check_chunk_alignment("甲" * 100 + "乙" * 30 + "丙" * 30, [
        {"speaker": "NARRATOR", "text": "甲" * 100 + "丙" * 30},
    ])
    assert res["ok"] is True
    assert not res["missing"] and not res["extra"] and not res["suspicious"]


def test_check_chunk_alignment_reordered_blocks_fail_both_sides():
    # 规则 5：顺序错乱 → difflib 非交叉匹配在两侧各留大段连续未匹配
    # （missing 与 extra 同时出现）→ ok False，无需独立检测器。
    a, b = "甲" * 120, "乙" * 120
    res = check_chunk_alignment(a + b, [
        {"speaker": "NARRATOR", "text": b + a},
    ])
    assert res["ok"] is False
    assert any(len(g) > _ALIGN_FAIL_MIN for g in res["missing"])
    assert any(len(g) > _ALIGN_FAIL_MIN for g in res["extra"])


def test_check_chunk_alignment_empty_source_uses_same_bands():
    # 空 source 早退分支与主路径同一判档：≤50 字输出忽略、50–100 字标记可疑
    # （不阻塞）、>100 字判大段新增（extra）→ ok False。
    small = check_chunk_alignment("", [{"speaker": "NARRATOR", "text": "甲" * 30}])
    assert small["ok"] is True and not small["extra"] and not small["suspicious"]
    mid = check_chunk_alignment("", [{"speaker": "NARRATOR", "text": "甲" * 60}])
    assert mid["ok"] is True and not mid["extra"]
    assert any(_ALIGN_SUSPICIOUS_MIN < len(g) <= _ALIGN_FAIL_MIN
               for g in mid["suspicious"])
    large = check_chunk_alignment("", [{"speaker": "NARRATOR", "text": "甲" * 120}])
    assert large["ok"] is False
    assert any(len(g) > _ALIGN_FAIL_MIN for g in large["extra"])


def test_split_balanced_prefers_paragraph_boundary():
    chunk = "甲" * 50 + "\n\n" + "乙" * 50
    assert split_chunk_balanced(chunk) == ("甲" * 50, "乙" * 50)


def test_split_balanced_falls_back_to_newline():
    chunk = "甲" * 50 + "\n" + "乙" * 50
    assert split_chunk_balanced(chunk) == ("甲" * 50, "乙" * 50)


def test_split_balanced_falls_back_to_sentence_end():
    chunk = "甲" * 40 + "。" + "乙" * 50 + "。"
    assert split_chunk_balanced(chunk) == ("甲" * 40 + "。", "乙" * 50 + "。")


def test_split_balanced_no_boundary_returns_whole():
    assert split_chunk_balanced("甲" * 50) == ("甲" * 50, "")


# --------------------------------------------------------------------------- #
# process_chunk 忠实性恢复（mocked urlopen）
# --------------------------------------------------------------------------- #

def _chat_payload(content: str, finish_reason: str = "stop") -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
        ensure_ascii=False,
    ).encode("utf-8")


def test_process_chunk_fidelity_double_tokens_recovers_missing_line(monkeypatch):
    # 首轮回复被 token 预算切断（finish_reason=length，静默丢了 >100 字的一段内容）：
    # 忠实性校验捕获，分诊判「预算截断」-> max_tokens 临时翻倍再跑一次，其完整回复
    # 被采纳（共 2 次调用）。
    missing_line = f"{LQ}那就一直走，直到看见尽头{'乙' * 110}。{RQ}她答。"
    partial = json.dumps([
        {"speaker": "NARRATOR", "text": "他道"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}"},
    ], ensure_ascii=False)
    full = json.dumps([
        {"speaker": "NARRATOR", "text": "他道"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}"},
        {"speaker": "B", "text": missing_line},
    ], ensure_ascii=False)
    calls = {"n": 0, "temps": [], "max_tokens": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        body = json.loads(req.data.decode("utf-8"))
        calls["temps"].append(body["temperature"])
        calls["max_tokens"].append(body["max_tokens"])
        return _BodyResp(_chat_payload(partial, "length") if calls["n"] == 1
                         else _chat_payload(full))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    chunk = f"他道：{LQ}这条路没有尽头。{RQ}\n\n{missing_line}"
    result = process_chunk(
        _Handle(),
        LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False),
        "m", chunk, 1, 1, temperature=0.6, max_tokens=100,
    )

    assert [e["text"] for e in result] == [
        "他道", f"{LQ}这条路没有尽头。{RQ}", missing_line,
    ]
    assert calls["n"] == 2
    assert calls["temps"] == [0.6, 0.6]
    assert calls["max_tokens"] == [100, 200]


def test_process_chunk_fidelity_split_in_half(monkeypatch):
    # 原发 finish_reason=stop 却静默丢了 >100 字的后半引语（模型自己停笔）：分诊判
    # 「质量不达标」，翻倍无意义，直接对半切开：chunk 在段落边界切开，两半各再跑一次
    # （1 + 1 + 1 = 3 次调用，阶梯到头不递归），内容全部找回，且全程不翻倍预算。
    second = "第二句话内容" + "乙" * 110
    chunk = f"A说：{LQ}第一句话内容。{RQ}\n\nB说：{LQ}{second}。{RQ}"
    left, _right = split_chunk_balanced(chunk)  # the paragraph boundary, mid-chunk
    left_json = json.dumps([
        {"speaker": "NARRATOR", "text": "A说"},
        {"speaker": "A", "text": f"{LQ}第一句话内容。{RQ}"},
    ], ensure_ascii=False)
    right_json = json.dumps([
        {"speaker": "NARRATOR", "text": "B说"},
        {"speaker": "B", "text": f"{LQ}{second}。{RQ}"},
    ], ensure_ascii=False)
    full_user = DEFAULT_USER_PROMPT.format(context="(Beginning of text)", chunk=chunk)
    left_user = DEFAULT_USER_PROMPT.format(context="(Beginning of text)", chunk=left)
    calls = {"n": 0, "max_tokens": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        body = json.loads(req.data.decode("utf-8"))
        calls["max_tokens"].append(body["max_tokens"])
        user = body["messages"][1]["content"]
        content = left_json if user in (full_user, left_user) else right_json
        return _BodyResp(_chat_payload(content))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    result = process_chunk(
        _Handle(),
        LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False),
        "m", chunk, 1, 1, temperature=0.6, max_tokens=100,
    )

    assert [e["text"] for e in result] == [
        "A说", f"{LQ}第一句话内容。{RQ}", "B说", f"{LQ}{second}。{RQ}",
    ]
    assert calls["n"] == 3
    assert calls["max_tokens"] == [100, 100, 100]  # stop 直达切分，无一次翻倍


def test_process_chunk_fidelity_unrecoverable_keeps_best(monkeypatch):
    # 原发 stop 且静默丢 >100 字内容，chunk 又无任何安全切分边界（阶梯到头）：分诊判
    # 「质量不达标」直奔切分、切分失败 -> 优雅保留最好结果（共 1 次调用，不翻倍不循环），不丢弃。
    partial = json.dumps([
        {"speaker": "NARRATOR", "text": "他道"},
    ], ensure_ascii=False)
    calls = {"n": 0, "temps": [], "max_tokens": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        body = json.loads(req.data.decode("utf-8"))
        calls["temps"].append(body["temperature"])
        calls["max_tokens"].append(body["max_tokens"])
        return _BodyResp(_chat_payload(partial))  # 每次都丢行

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    # 无换行、无句末标点 → split_chunk_balanced 找不到任何安全边界，不可再切
    chunk = f"他道：{LQ}这条路没有尽头{'乙' * 110}{RQ}她答"
    result = process_chunk(
        _Handle(),
        LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False),
        "m", chunk, 1, 1, temperature=0.6, max_tokens=100,
    )

    assert [e["text"] for e in result] == ["他道"]  # 保留最好结果，不丢弃
    assert calls["n"] == 1
    assert calls["temps"] == [0.6]
    assert calls["max_tokens"] == [100]


def test_process_chunk_truncated_json_doubles_budget_on_retry(monkeypatch):
    # 首轮 JSON 被整体切断（无法解析，finish_reason=length）：重试循环为下一次尝试
    # 翻倍预算；第二次返回合法 JSON（stop）-> 采纳（共 2 次调用）。
    truncated = '[{"speaker": "NARRATOR", "text": "他'
    full = json.dumps([
        {"speaker": "NARRATOR", "text": "他道"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}"},
    ], ensure_ascii=False)
    calls = {"n": 0, "max_tokens": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        calls["max_tokens"].append(json.loads(req.data.decode("utf-8"))["max_tokens"])
        return _BodyResp(_chat_payload(truncated, "length") if calls["n"] == 1
                         else _chat_payload(full))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    chunk = f"他道：{LQ}这条路没有尽头。{RQ}"
    result = process_chunk(
        _Handle(),
        LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False),
        "m", chunk, 1, 1, temperature=0.6, max_tokens=100,
    )

    assert [e["text"] for e in result] == ["他道", f"{LQ}这条路没有尽头。{RQ}"]
    assert calls["n"] == 2
    assert calls["max_tokens"] == [100, 200]


def test_process_chunk_alignment_off_skips_recovery(monkeypatch):
    # check_alignment=False（用户解析页「chunk 忠实性校验」开关关）：JSON 可解析即原样
    # 返回——大段缺失也不触发翻倍预算 / 对半切开（对照 :890 开关开时的 2 次恢复调用）。
    missing_line = f"{LQ}那就一直走，直到看见尽头{'乙' * 110}。{RQ}她答。"
    partial = json.dumps([
        {"speaker": "NARRATOR", "text": "他道"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}"},
    ], ensure_ascii=False)
    calls = {"n": 0, "max_tokens": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        calls["max_tokens"].append(json.loads(req.data.decode("utf-8"))["max_tokens"])
        return _BodyResp(_chat_payload(partial, "length"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    chunk = f"他道：{LQ}这条路没有尽头。{RQ}\n\n{missing_line}"
    result = process_chunk(
        _Handle(),
        LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False),
        "m", chunk, 1, 1, temperature=0.6, max_tokens=100,
        check_alignment=False,
    )

    assert [e["text"] for e in result] == ["他道", f"{LQ}这条路没有尽头。{RQ}"]
    assert calls["n"] == 1
    assert calls["max_tokens"] == [100]  # 不翻倍、不切开


def test_process_chunk_alignment_off_still_retries_unparseable_json(monkeypatch):
    # 恒执行项回归：JSON 可解析性的 max_retries 重试不受本开关影响——check_alignment=
    # False 时首轮 JSON 被切断（不可解析）仍翻倍预算重试，与开关开时行为一致（:1004）。
    truncated = '[{"speaker": "NARRATOR", "text": "他'
    full = json.dumps([
        {"speaker": "NARRATOR", "text": "他道"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}"},
    ], ensure_ascii=False)
    calls = {"n": 0, "max_tokens": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        calls["max_tokens"].append(json.loads(req.data.decode("utf-8"))["max_tokens"])
        return _BodyResp(_chat_payload(truncated, "length") if calls["n"] == 1
                         else _chat_payload(full))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    chunk = f"他道：{LQ}这条路没有尽头。{RQ}"
    result = process_chunk(
        _Handle(),
        LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False),
        "m", chunk, 1, 1, temperature=0.6, max_tokens=100,
        check_alignment=False,
    )

    assert [e["text"] for e in result] == ["他道", f"{LQ}这条路没有尽头。{RQ}"]
    assert calls["n"] == 2
    assert calls["max_tokens"] == [100, 200]


def test_check_chunk_alignment_speaker_name_not_flagged_missing():
    # 纯标签删除（提示词编辑 (e)）：名字只留在 speaker 字段，「名字+说话动词」的
    # 标签缺口（6 字骨架）落在 ≤50 字忽略档，不误判为「缺失」。
    res = check_chunk_alignment(f"东方明风笑道：{LQ}走吧。{RQ}", [
        {"speaker": "东方明风", "text": f"{LQ}走吧。{RQ}"},
    ])
    assert res["ok"] is True and not res["missing"] and not res["extra"]


def test_check_chunk_alignment_tag_gap_ignored_long_loss_flagged():
    # 短标签/短叙述行删除（≤50 字）→ 忽略；> 100 字的叙述段缺失 → 完整性异常
    # （旧「标签文法豁免」路径已并入长度判档，不再特判）。
    source = f"他继续笑道：{LQ}此事不可说。{RQ}\n\n她转身离去。"
    kept = check_chunk_alignment(source, [
        {"speaker": "NARRATOR", "text": f"{LQ}此事不可说。{RQ}她转身离去。"},
    ])
    assert kept["ok"] is True and not kept["missing"]
    # 短标签删除（5 字缺口）现在同样被忽略——规则只看「明显大段」
    short_drop = check_chunk_alignment(source, [
        {"speaker": "NARRATOR", "text": f"{LQ}此事不可说。{RQ}"},
    ])
    assert short_drop["ok"] is True and not short_drop["missing"]
    long_loss = f"他继续笑道：{LQ}此事不可说。{RQ}\n\n" + \
        f"她转身离去，再也没有回来。{'甲' * 100}"
    res = check_chunk_alignment(long_loss, [
        {"speaker": "NARRATOR", "text": f"{LQ}此事不可说。{RQ}"},
    ])
    assert res["ok"] is False
    assert any(len(g) > _ALIGN_FAIL_MIN for g in res["missing"])


def test_reparse_vote_accepts_speech_verb_removed_variant():
    # 提示词编辑 (e) 的第三处变体：标签含描述性动作时保留动作、只删说话动词
    # （…衣领，吼道 → …衣领。）——骨架去尾动词簇的变体通过投票门；
    # 多动一个词（真丢内容）仍被拒。
    entry = {"speaker": "NARRATOR", "text": "史蒂夫猛地抓住杜尘的衣领，吼道"}
    roster = frozenset({"NARRATOR"})
    kept_action = _reparse_vote(
        [{"speaker": "NARRATOR", "text": "史蒂夫猛地抓住杜尘的衣领。"}], entry, roster)
    assert kept_action == (("NARRATOR", "史蒂夫猛地抓住杜尘的衣领"),)
    tag_kept = _reparse_vote(
        [{"speaker": "NARRATOR", "text": "史蒂夫猛地抓住杜尘的衣领，吼道"}], entry, roster)
    assert tag_kept == (("NARRATOR", "史蒂夫猛地抓住杜尘的衣领吼道"),)
    real_drop = _reparse_vote(
        [{"speaker": "NARRATOR", "text": "史蒂夫猛地抓住杜尘。"}], entry, roster)
    assert real_drop is None


def test_process_chunk_fidelity_unknown_finish_reason_doubles_first(monkeypatch):
    # finish_reason 未报告（None，部分 provider 不返回该字段）：保留旧版「先翻倍」
    # 兜底顺序（只有 stop 才直达切半），翻倍后的完整回复被采纳（共 2 次调用）。
    missing_line = f"{LQ}那就一直走，直到看见尽头{'乙' * 110}。{RQ}她答。"
    partial = json.dumps([
        {"speaker": "NARRATOR", "text": "他道"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}"},
    ], ensure_ascii=False)
    full = json.dumps([
        {"speaker": "NARRATOR", "text": "他道"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}"},
        {"speaker": "B", "text": missing_line},
    ], ensure_ascii=False)
    calls = {"n": 0, "max_tokens": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        calls["max_tokens"].append(json.loads(req.data.decode("utf-8"))["max_tokens"])
        return _BodyResp(_chat_payload(partial, None) if calls["n"] == 1
                         else _chat_payload(full))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    chunk = f"他道：{LQ}这条路没有尽头。{RQ}\n\n{missing_line}"
    result = process_chunk(
        _Handle(),
        LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False),
        "m", chunk, 1, 1, temperature=0.6, max_tokens=100,
    )

    assert [e["text"] for e in result] == [
        "他道", f"{LQ}这条路没有尽头。{RQ}", missing_line,
    ]
    assert calls["n"] == 2
    assert calls["max_tokens"] == [100, 200]


def test_check_chunk_alignment_zhidao_line_judged_by_length():
    # 旧「道」尾豁免已从对齐校验移除：以「…才知道」结尾的缺失不再特判，只按
    # 连续未匹配长度判档——短行（≤50 字）忽略，> 100 字照报为缺失
    # （知/难 形态守卫如今只作用于 _reparse_vote 投票门）。
    short = check_chunk_alignment(f"林某才知道。{LQ}走吧。{RQ}", [
        {"speaker": "林某", "text": f"{LQ}走吧。{RQ}"},
    ])
    assert short["ok"] is True and not short["missing"]
    long_line = (
        "林某才终于明白这条路其实从来没有尽头，"
        "当年师傅说的那番话他一个字也没有听进去，"
        "此刻回想起来才觉得自己实在是荒唐至极。"
        "窗外夜色深沉，烛火摇曳，把他的影子拉得很长，"
        "他在这条路上走了整整十年从未有过一丝动摇，"
        "直到此刻才恍然大悟"
    )
    assert len("".join(c for c in long_line if c.isalnum())) > _ALIGN_FAIL_MIN
    res = check_chunk_alignment(f"{long_line}。{LQ}走吧。{RQ}", [
        {"speaker": "林某", "text": f"{LQ}走吧。{RQ}"},
    ])
    assert res["ok"] is False
    assert any(len(g) > _ALIGN_FAIL_MIN for g in res["missing"])


def test_reparse_vote_rejects_zhidao_tail_variant():
    # 同一守卫作用于 _reparse_vote：以「…才知道」结尾的条目，「去掉尾道」变体
    # 不在合法集合内，重判投票拒绝（对照：「…吼道」的动词变体仍放行）。
    entry = {"speaker": "NARRATOR", "text": "林某才知道"}
    roster = frozenset({"NARRATOR"})
    assert _reparse_vote(
        [{"speaker": "NARRATOR", "text": "林某才知。"}], entry, roster) is None
    entry2 = {"speaker": "NARRATOR", "text": "杜尘吼道"}
    assert _reparse_vote(
        [{"speaker": "NARRATOR", "text": "杜尘吼道。"}], entry2, roster) == (
        ("NARRATOR", "杜尘吼道"),)


def test_process_chunk_doubled_budget_rejected_falls_back_to_split(monkeypatch):
    # 严格网关拒绝翻倍预算（LLMHTTPError/4xx）：分诊阶段放弃翻倍、回落到切半
    # 兜底，不把「可恢复的预算截断」升级为整任务 fast-fail。
    second = "第二句话内容" + "乙" * 110
    chunk = f"A说：{LQ}第一句话内容。{RQ}\n\nB说：{LQ}{second}。{RQ}"
    left, _right = split_chunk_balanced(chunk)
    left_json = json.dumps([
        {"speaker": "NARRATOR", "text": "A说"},
        {"speaker": "A", "text": f"{LQ}第一句话内容。{RQ}"},
    ], ensure_ascii=False)
    right_json = json.dumps([
        {"speaker": "NARRATOR", "text": "B说"},
        {"speaker": "B", "text": f"{LQ}{second}。{RQ}"},
    ], ensure_ascii=False)
    full_user = DEFAULT_USER_PROMPT.format(context="(Beginning of text)", chunk=chunk)
    left_user = DEFAULT_USER_PROMPT.format(context="(Beginning of text)", chunk=left)
    calls = {"n": 0, "max_tokens": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        body = json.loads(req.data.decode("utf-8"))
        calls["max_tokens"].append(body["max_tokens"])
        if calls["n"] == 2:
            # 翻倍预算的调用被严格网关拒绝
            raise LLMHTTPError(400, "max_tokens exceeds model limit")
        user = body["messages"][1]["content"]
        content = left_json if user in (full_user, left_user) else right_json
        return _BodyResp(_chat_payload(content, "length" if calls["n"] == 1 else "stop"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    result = process_chunk(
        _Handle(),
        LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False),
        "m", chunk, 1, 1, temperature=0.6, max_tokens=100,
    )

    assert [e["text"] for e in result] == [
        "A说", f"{LQ}第一句话内容。{RQ}", "B说", f"{LQ}{second}。{RQ}",
    ]
    assert calls["n"] == 4
    assert calls["max_tokens"] == [100, 200, 100, 100]


def test_process_chunk_retry_doubled_budget_rejected_falls_back(monkeypatch):
    # 重试循环的翻倍调用被拒绝（4xx）：回退原预算继续重试，不 fast-fail；
    # 第三次以原预算返回合法完整 JSON 被采纳。
    truncated = '[{"speaker": "NARRATOR", "text": "他'
    full = json.dumps([
        {"speaker": "NARRATOR", "text": "他道"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}"},
    ], ensure_ascii=False)
    calls = {"n": 0, "max_tokens": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        calls["max_tokens"].append(json.loads(req.data.decode("utf-8"))["max_tokens"])
        if calls["n"] == 2:
            raise LLMHTTPError(400, "max_tokens exceeds model limit")
        return _BodyResp(_chat_payload(truncated, "length") if calls["n"] == 1
                         else _chat_payload(full))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    chunk = f"他道：{LQ}这条路没有尽头。{RQ}"
    result = process_chunk(
        _Handle(),
        LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False),
        "m", chunk, 1, 1, temperature=0.6, max_tokens=100,
    )

    assert [e["text"] for e in result] == ["他道", f"{LQ}这条路没有尽头。{RQ}"]
    assert calls["n"] == 3
    assert calls["max_tokens"] == [100, 200, 100]


# --------------------------------------------------------------------------- #
# 同人段落机械合并（merge_adjacent_same_speaker：连续同 speaker，词字符口径）
# --------------------------------------------------------------------------- #

def test_merge_same_speaker_merges_adjacent_run():
    entries = [
        {"speaker": "NARRATOR", "text": "夜色沉了下来", "instruct": "a"},
        {"speaker": "NARRATOR", "text": "风穿过巷子", "instruct": "b"},
        {"speaker": "BOB", "text": f"{LQ}回来。{RQ}", "instruct": "c"},
        {"speaker": "NARRATOR", "text": "灯亮了", "instruct": "d"},
    ]
    original = [dict(e) for e in entries]
    out, n = merge_adjacent_same_speaker(entries, is_chapter_title)
    # 边界无收尾标点补「。」；instruct 取词字符多者（6>5 → a）；BOB 打断连续段
    assert n == 1
    assert out == [
        {"speaker": "NARRATOR", "text": "夜色沉了下来。风穿过巷子", "instruct": "a"},
        {"speaker": "BOB", "text": f"{LQ}回来。{RQ}", "instruct": "c"},
        {"speaker": "NARRATOR", "text": "灯亮了", "instruct": "d"},
    ]
    assert entries == original  # 输入列表不被改动


def test_merge_same_speaker_chains_and_keeps_first_instruct():
    entries = [
        {"speaker": "NARRATOR", "text": "一", "instruct": "slow"},
        {"speaker": "NARRATOR", "text": "二", "instruct": "fast"},
        {"speaker": "NARRATOR", "text": "三", "instruct": "loud"},
    ]
    out, n = merge_adjacent_same_speaker(entries, is_chapter_title)
    # 三个 1 字段平手 → instruct 取最左（slow）；每个内部边界各补一个「。」
    assert n == 2
    assert out == [{"speaker": "NARRATOR", "text": "一。二。三", "instruct": "slow"}]


def test_merge_same_speaker_keeps_titles_standalone():
    entries = [
        {"speaker": "NARRATOR", "text": "前章结尾"},
        {"speaker": "NARRATOR", "text": "第十章 舞会"},
        {"speaker": "NARRATOR", "text": "舞会开始了"},
    ]
    out, n = merge_adjacent_same_speaker(entries, is_chapter_title)
    assert n == 0  # 标题行的前后两侧一律不合并
    assert [e["text"] for e in out] == ["前章结尾", "第十章 舞会", "舞会开始了"]


def test_merge_same_speaker_caps_merged_word_chars():
    # 词字符口径：甲×3900 + 乙×200 = 4100 > 4000 且较短方 200 > 10 → 不合并
    a = {"speaker": "NARRATOR", "text": "甲" * 3900}
    b = {"speaker": "NARRATOR", "text": "乙" * 200}
    out, n = merge_adjacent_same_speaker([a, b], is_chapter_title, max_chars=4000)
    assert n == 0
    assert out == [a, b]


def test_merge_same_speaker_default_cap_blocks_oversize():
    # 缺省上限 100 词字符：60 + 60 = 120 > 100 且较短方 60 > 10 → 不合并
    a = {"speaker": "NARRATOR", "text": "甲" * 60}
    b = {"speaker": "NARRATOR", "text": "乙" * 60}
    out, n = merge_adjacent_same_speaker([a, b], is_chapter_title)
    assert n == 0
    assert out == [a, b]


def test_merge_same_speaker_merges_short_run_by_default():
    # 缺省上限 100 词字符：40 + 40 = 80 ≤ 100 → 合并（边界补「。」）
    a = {"speaker": "NARRATOR", "text": "甲" * 40}
    b = {"speaker": "NARRATOR", "text": "乙" * 40}
    out, n = merge_adjacent_same_speaker([a, b], is_chapter_title)
    assert n == 1
    assert out == [{"speaker": "NARRATOR", "text": "甲" * 40 + "。" + "乙" * 40}]


def test_merge_same_speaker_forced_merge_short_member():
    # ≤10 强制合并：较短一方 5 字 → 即使合并后 > 100 也必合并
    a = {"speaker": "NARRATOR", "text": "甲" * 100}
    b = {"speaker": "NARRATOR", "text": "乙" * 5}
    out, n = merge_adjacent_same_speaker([a, b], is_chapter_title)
    assert n == 1
    assert out == [{"speaker": "NARRATOR", "text": "甲" * 100 + "。" + "乙" * 5}]


def test_merge_same_speaker_forced_merge_grows_long_block():
    # 贪心：块已 100 字，后续 5 字段较短方 ≤10 → 强制并入（块 → 105）；
    # 再后 50 字段：块 105+50=155>100 且较短方 50>10 → 封块、开新块
    entries = [
        {"speaker": "BOB", "text": "丙" * 100, "instruct": "x"},
        {"speaker": "BOB", "text": "丁" * 5, "instruct": "y"},
        {"speaker": "BOB", "text": "戊" * 50, "instruct": "z"},
    ]
    out, n = merge_adjacent_same_speaker(entries, is_chapter_title)
    assert n == 1
    assert len(out) == 2
    assert out[0]["text"] == "丙" * 100 + "。" + "丁" * 5
    assert out[1]["text"] == "戊" * 50


def test_merge_same_speaker_greedy_absorbs_while_within_cap():
    # 贪心：块+段 ≤100 持续并入；超过即封块
    entries = [
        {"speaker": "BOB", "text": "甲" * 40, "instruct": "a"},
        {"speaker": "BOB", "text": "乙" * 40, "instruct": "b"},
        {"speaker": "BOB", "text": "丙" * 40, "instruct": "c"},
    ]
    out, n = merge_adjacent_same_speaker(entries, is_chapter_title)
    # 甲(40)+乙(40)=80≤100 并入 → 块 80；丙(40)：80+40=120>100 且较短方 40>10 → 封块
    assert n == 1
    assert len(out) == 2
    assert out[0]["text"] == "甲" * 40 + "。" + "乙" * 40
    assert out[1]["text"] == "丙" * 40


def test_merge_same_speaker_block_becomes_title_blocks_further_merge():
    # 章标题恒判、无豁免：运行块一旦成为标题即封口（自定义 title_test 命中合并后形态
    # "楔。子"——楔+子 边界补「。」后的实际形态）
    def t_test(t):
        return t == "楔。子"
    entries = [
        {"speaker": "NARRATOR", "text": "楔", "instruct": "a"},
        {"speaker": "NARRATOR", "text": "子", "instruct": "b"},
        {"speaker": "NARRATOR", "text": "很长的一段文字内容在这里面", "instruct": "c"},
    ]
    out, n = merge_adjacent_same_speaker(entries, t_test)
    assert n == 1
    assert len(out) == 2
    assert out[0]["text"] == "楔。子"
    assert out[1]["text"] == "很长的一段文字内容在这里面"


def test_merge_same_speaker_instruct_takes_most_word_chars():
    # instruct 取词字符数最多的成员（非首条）
    entries = [
        {"speaker": "BOB", "text": "短", "instruct": "a"},
        {"speaker": "BOB", "text": "这段文字要长得多一些才行", "instruct": "b"},
        {"speaker": "BOB", "text": "中", "instruct": "c"},
    ]
    out, n = merge_adjacent_same_speaker(entries, is_chapter_title)
    # 短(1)+长(12)=13≤100 并入；+中(1)：13+1=14≤100 并入 → 一条
    # 词字符最多 = 长(12) → instruct b
    assert n == 2
    assert len(out) == 1
    assert out[0]["instruct"] == "b"


# --------------------------------------------------------------------------- #
# 断句失败校验（解析后条目级重判：外层引号包裹 + 引号内「…道：」标签 → 带窗口重跑）
# --------------------------------------------------------------------------- #

# 被校验条目的固定样例：外层弯引号包裹，引号内开头是纯语气标签（可整段丢弃）
SUSP_ENTRY = {
    "speaker": "NARRATOR",
    "text": f"{LQ}林某冷笑道：{LQ}二哥还没出来吗？{RQ}{RQ}",
    "instruct": "",
}
ROSTER = frozenset({"NARRATOR", "林某"})
_LLM = LLMConfig(base_url="http://x/v1", api_key="k", model_name="m", stream=False)

# 四种互不相同的合法重判（骨架都能拼回原文，角色都在花名册内）
W_KEEP = json.dumps([  # 标签保留为旁白段 + 台词段（2 段，2 主体）
    {"speaker": "NARRATOR", "text": "林某冷笑道。"},
    {"speaker": "林某", "text": "二哥还没出来吗？"},
], ensure_ascii=False)
W_DROP = json.dumps([  # 纯标签整段丢弃（提示词编辑 (e)）→ 单段
    {"speaker": "林某", "text": "二哥还没出来吗？"},
], ensure_ascii=False)
W_ALL_NARR = json.dumps([{"speaker": "NARRATOR", "text": "林某冷笑道二哥还没出来吗？"}],
                        ensure_ascii=False)
W_ALL_LIN = json.dumps([{"speaker": "林某", "text": "林某冷笑道二哥还没出来吗？"}],
                       ensure_ascii=False)


def test_suspicious_detector_pins():
    # 触发 = 外层双引号包裹（弯/直任意形式）且引号跨度内有「…道 + 冒号」；
    # 冒号是硬条件（知道/难道/道理 的「道」后无冒号 → 不触发）。
    suspicious = [
        f"{LQ}又道：{LQ}二哥还没出来吗？{RQ}{RQ}",  # 嵌套同款引号（rfind 取最宽跨度）
        f"{LQ}杜尘笑道：{LQ}快走！{RQ}{RQ}",
        f"{SQ}林某道：{SQ}知道了{SQ}{SQ}",          # 直引号包裹 + 直引号内层
        f"{LQ}道：{LQ}快走！{RQ}{RQ}",              # 标签在跨度开头（^ 分支）
        f"{SQ}沉声道：{SQ}嗯{SQ}{SQ}",
        f"{LQ}冷笑道 ：{LQ}走{RQ}{RQ}",             # 道 与冒号之间允许空白
        f"{LQ}又道：{SQ}嗯{SQ}{RQ}",                # 弯包裹 + 直内层（混合形式）
        f"{LQ}他低声道：{LQ}快走。{RQ}{RQ}",
    ]
    clean = [
        f"说道：二哥还没出来吗？",                  # 有标签但无包裹
        f"{LQ}二哥还没出来吗？{RQ}",                # 有包裹但无标签
        f"他低声道：{LQ}快走。{RQ}",                # 标签在包裹之外
        f"{LQ}难道是这样。{RQ}",                    # 「道」后无冒号
        f"{LQ}道理很简单。{RQ}",
        f"{LQ}他知道了。{RQ}",                      # 知道（无冒号）
        f"{LQ}嗯。{RQ}",
        f"{LQ}{RQ}",                                # 空包裹
        "",
    ]
    for t in suspicious:
        assert is_suspicious_entry_text(t) is True, repr(t)
    for t in clean:
        assert is_suspicious_entry_text(t) is False, repr(t)


def test_suspicious_indices_skips_non_dicts():
    entries = [
        {"speaker": "NARRATOR", "text": f"{LQ}又道：{LQ}嗯？{RQ}{RQ}", "instruct": ""},
        {"speaker": "林某", "text": f"{LQ}走。{RQ}", "instruct": ""},
        {"speaker": "NARRATOR", "text": f"{LQ}他知道了。{RQ}", "instruct": ""},
        {"speaker": "NARRATOR", "text": f'{SQ}说道：{SQ}好{SQ}{SQ}', "instruct": ""},
        "not-a-dict",
    ]
    assert suspicious_entry_indices(entries) == [0, 3]


def test_strip_leading_saying_tag():
    wrapped = f"{LQ}林某冷笑道：{LQ}二哥还没出来吗？{RQ}{RQ}"
    # 内层开引号保留（剥掉的只有标签本身）——该形态只用于忠实性门的骨架，骨架忽略引号
    assert _strip_leading_saying_tag(wrapped) == f"{LQ}{LQ}二哥还没出来吗？{RQ}{RQ}"
    # 开头的「知道：」同样可剥（两种骨架都过门，由重判多票裁决）
    assert _strip_leading_saying_tag(f"{LQ}他知道：这件事。{RQ}") == f"{LQ}这件事。{RQ}"
    # 无开头标签 / 未包裹 → 原样返回（中段标签不剥——交给段落混合检查）
    assert _strip_leading_saying_tag(f"{LQ}二哥。{RQ}") == f"{LQ}二哥。{RQ}"
    assert _strip_leading_saying_tag("林某冷笑道：走") == "林某冷笑道：走"


def test_reparse_vote_accepts_faithful_rederivations():
    # 单段：纯标签整段丢弃（编辑 (e)）→ 骨架 = 剥标签后的原文
    assert _reparse_vote([{"speaker": "林某", "text": "二哥还没出来吗？"}],
                         SUSP_ENTRY, ROSTER) == (("林某", "二哥还没出来吗"),)
    # 两段：标签保留为旁白段 → 骨架 = 原文（含标签）
    assert _reparse_vote([
        {"speaker": "NARRATOR", "text": "林某冷笑道。"},
        {"speaker": "林某", "text": "二哥还没出来吗？"},
    ], SUSP_ENTRY, ROSTER) == (("NARRATOR", "林某冷笑道"), ("林某", "二哥还没出来吗"))
    # 骨架对引号/冒号编辑免疫：原样保留包裹也通过
    assert _reparse_vote(
        [{"speaker": "NARRATOR", "text": SUSP_ENTRY["text"]}], SUSP_ENTRY, ROSTER) is not None


def test_reparse_vote_rejects_unfaithful_or_unknown():
    bad = [
        [{"speaker": "林某", "text": "二哥出来了吗？"}],        # 丢词（还没）
        [{"speaker": "林某", "text": f"{LQ}二哥还没出来吗？{RQ}罢了"}],  # 增词
        [{"speaker": "赵四", "text": "二哥还没出来吗？"}],       # 角色不在花名册
        [{"speaker": "林某", "text": "二哥"},
         {"speaker": "林某", "text": "还没出来吗？"}],          # 多段同主体
        [{"speaker": "林某", "text": "二哥还没出来吗？"},
         {"speaker": "NARRATOR", "text": "林某冷笑道。"}],      # 段序颠倒
        [{"speaker": "NARRATOR", "text": ""}],                  # 空段文字
        [{"speaker": "NARRATOR"}],                              # 缺 text
        [],                                                     # 无段
        ["not-a-dict"],                                         # 非 dict 段
    ]
    for parts in bad:
        assert _reparse_vote(parts, SUSP_ENTRY, ROSTER) is None, parts


def _run_revalidate(monkeypatch, payloads, handle=None):
    """Serve ``payloads`` in order (one non-stream body per call); over-call = error."""
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        if calls["n"] > len(payloads):
            raise AssertionError(f"LLM called {calls['n']} times, expected {len(payloads)}")
        return _BodyResp(_chat_payload(payloads[calls["n"] - 1]))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    parts = revalidate_entry(
        handle or _Handle(), _LLM, GenerationConfig(),
        "sys", "CTX={context}\nSOURCE TEXT:\n{chunk}", SUSP_ENTRY, "ctx", ROSTER,
    )
    return parts, calls


def test_revalidate_identical_replies_stop_after_two_calls(monkeypatch):
    # 2:0 即出多数 → 第 3 次调用省掉（与角色匹配检查同一「首次 2:1 即止」规则）
    parts, calls = _run_revalidate(monkeypatch, [W_KEEP, W_KEEP, W_KEEP])
    assert calls["n"] == 2
    assert [(p["speaker"], p["text"]) for p in parts] == [
        ("NARRATOR", "林某冷笑道。"), ("林某", "二哥还没出来吗？"),
    ]


def test_revalidate_21_majority_settles_on_third_call(monkeypatch):
    parts, calls = _run_revalidate(monkeypatch, [W_KEEP, W_DROP, W_KEEP])
    assert calls["n"] == 3  # 两次 1:1 → 第 3 次打破平局
    assert parts[0]["speaker"] == "NARRATOR"  # 2 段（保留标签）的回复胜出


def test_revalidate_tie_escalates_to_fourth_call(monkeypatch):
    parts, calls = _run_revalidate(monkeypatch, [W_KEEP, W_DROP, W_ALL_NARR, W_KEEP])
    assert calls["n"] == 4  # 3 次 1:1:1 无共识 → 按规则再跑第 4 次
    assert parts[0]["speaker"] == "NARRATOR"


def test_revalidate_four_distinct_keeps_entry(monkeypatch):
    parts, calls = _run_revalidate(monkeypatch, [W_KEEP, W_DROP, W_ALL_NARR, W_ALL_LIN])
    assert calls["n"] == 4
    assert parts is None  # 无共识 → 从不猜，条目保持原样


def test_revalidate_bad_replies_contribute_no_votes(monkeypatch):
    # 不可解析 + 未过门的回复不投票；两次无票耗尽前 2 次尝试后，
    # 两次相同的好回复仍能在第 4 次调用后定出多数
    bad_gate = json.dumps([{"speaker": "赵四", "text": "二哥还没出来吗？"}],
                          ensure_ascii=False)
    parts, calls = _run_revalidate(
        monkeypatch, ["这不是JSON", bad_gate, W_DROP, W_DROP])
    assert calls["n"] == 4
    assert [(p["speaker"], p["text"]) for p in parts] == [("林某", "二哥还没出来吗？")]


def test_revalidate_cancel_propagates(monkeypatch):
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        return _BodyResp(_chat_payload(W_KEEP))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(TaskCancelled):
        revalidate_entry(_Handle(cancelled=True), _LLM, GenerationConfig(),
                         "sys", "CTX={context}\nSOURCE TEXT:\n{chunk}",
                         SUSP_ENTRY, "ctx", ROSTER)
    assert calls["n"] == 0  # 取消在任何调用之前上抛


def test_revalidate_single_call_mode_exact_one_call(monkeypatch):
    # single_call=True（超长段落重切用）：恰好 1 次 LLM 调用——过门直接采纳、
    # 未过门返回 None（交由机械分段兜底）；两种结局都不再发起第二次调用。
    good = {"n": 0}

    def urlopen_good(req, *a, **k):
        good["n"] += 1
        if good["n"] > 1:
            raise AssertionError("single_call 必须恰好一次调用")
        return _BodyResp(_chat_payload(W_KEEP))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen_good)
    parts = revalidate_entry(
        _Handle(), _LLM, GenerationConfig(),
        "sys", "CTX={context}\nSOURCE TEXT:\n{chunk}", SUSP_ENTRY, "ctx", ROSTER,
        single_call=True)
    assert good["n"] == 1
    assert [(p["speaker"], p["text"]) for p in parts] == [
        ("NARRATOR", "林某冷笑道。"), ("林某", "二哥还没出来吗？")]

    bad_reply = json.dumps(
        [{"speaker": "陌生人", "text": SUSP_ENTRY["text"]}], ensure_ascii=False)
    bad = {"n": 0}

    def urlopen_bad(req, *a, **k):
        bad["n"] += 1
        if bad["n"] > 1:
            raise AssertionError("single_call 必须恰好一次调用")
        return _BodyResp(_chat_payload(bad_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen_bad)
    assert revalidate_entry(
        _Handle(), _LLM, GenerationConfig(),
        "sys", "CTX={context}\nSOURCE TEXT:\n{chunk}", SUSP_ENTRY, "ctx", ROSTER,
        single_call=True) is None
    assert bad["n"] == 1


def test_validate_no_flagged_entries_skips_llm(monkeypatch):
    entries = [
        {"speaker": "NARRATOR", "text": "夜色。", "instruct": ""},
        {"speaker": "林某", "text": f"{LQ}走。{RQ}", "instruct": ""},
    ]

    def urlopen(req, *a, **k):
        raise AssertionError("no LLM call expected")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    out, flagged, fixed = validate_sentence_splits(
        _Handle(), _LLM, GenerationConfig(), "sys", "T", entries)
    assert (out, flagged, fixed) == (entries, 0, 0)  # 同一个 list 对象原样返回


def test_validate_applies_wins_in_descending_order(monkeypatch):
    # 下标 1 的 1→2 拆分必须在下标 3 的重写之后应用——窗口全部按原始列表预建，
    # 升序应用会让拆分移动尚未处理条目的下标，窗口指错条目。
    t1 = f"{LQ}林某冷笑道：{LQ}二哥还没出来吗？{RQ}{RQ}"
    t2 = f'{SQ}说道：{SQ}知道了。{SQ}{SQ}'
    entries = [
        {"speaker": "NARRATOR", "text": "夜色。", "instruct": ""},
        {"speaker": "NARRATOR", "text": t1, "instruct": ""},
        {"speaker": "NARRATOR", "text": "风起了。", "instruct": ""},
        {"speaker": "NARRATOR", "text": t2, "instruct": ""},
        {"speaker": "林某", "text": "嗯。", "instruct": ""},
        {"speaker": "李四", "text": "哦。", "instruct": ""},
    ]
    pristine = [dict(e) for e in entries]
    w1 = json.dumps([
        {"speaker": "NARRATOR", "text": "林某冷笑道。"},
        {"speaker": "林某", "text": "二哥还没出来吗？"},
    ], ensure_ascii=False)
    w2 = json.dumps([{"speaker": "李四", "text": "知道了。"}], ensure_ascii=False)

    def urlopen(req, *a, **k):
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        chunk = user.rsplit("SOURCE TEXT:", 1)[1].strip()
        if chunk == t1:
            return _BodyResp(_chat_payload(w1))
        if chunk == t2:
            return _BodyResp(_chat_payload(w2))
        raise AssertionError(f"unexpected re-parse chunk: {chunk!r}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    out, flagged, fixed = validate_sentence_splits(
        _Handle(), _LLM, GenerationConfig(), "sys",
        "CTX={context}\nSOURCE TEXT:\n{chunk}", entries, context_window=1)

    assert (flagged, fixed) == (2, 2)
    assert entries == pristine  # 输入列表保持原样
    # 下标 1 拆成 2 条（列表 +1 项），原条目顺序其余不动 → 7 条
    assert [(e["speaker"], e["text"]) for e in out] == [
        ("NARRATOR", "夜色。"),
        ("NARRATOR", "林某冷笑道。"),
        ("林某", "二哥还没出来吗？"),
        ("NARRATOR", "风起了。"),
        ("李四", "知道了。"),
        ("林某", "嗯。"),
        ("李四", "哦。"),
    ]


def test_validate_no_consensus_keeps_entries_and_list(monkeypatch):
    t1 = f"{LQ}林某冷笑道：{LQ}二哥还没出来吗？{RQ}{RQ}"
    entries = [
        {"speaker": "NARRATOR", "text": "夜色。", "instruct": ""},
        {"speaker": "NARRATOR", "text": t1, "instruct": ""},
        {"speaker": "林某", "text": "嗯。", "instruct": ""},
    ]
    bad = json.dumps([{"speaker": "赵四", "text": "二哥还没出来吗？"}], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        return _BodyResp(_chat_payload(bad))  # 每次都未过花名册门 → 无票

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    out, flagged, fixed = validate_sentence_splits(
        _Handle(), _LLM, GenerationConfig(), "sys", "T", entries)
    assert (out, flagged, fixed) == (entries, 1, 0)  # 同一对象，零改写
    assert calls["n"] == 4  # 基础 1 + 重试 2 无共识 → 再跑第 4 次


def test_instruct_check_flags_empty_and_35_words_only():
    exactly_34 = " ".join(f"word{i}" for i in range(34))
    exactly_35 = " ".join(f"word{i}" for i in range(35))
    entries = [
        {"speaker": "NARRATOR", "text": "a", "instruct": ""},
        {"speaker": "NARRATOR", "text": "b", "instruct": exactly_34},
        {"speaker": "林某", "text": "c", "instruct": exactly_35},
        {"speaker": "林某", "text": "d"},
    ]

    assert instruct_word_count(exactly_35) == 35
    assert instruct_entry_indices(entries) == [0, 2, 3]
    assert INSTRUCT_MAX_WORDS == 35


def test_validate_instructs_reparses_with_context_and_preserves_clean_entries(monkeypatch):
    entries = [
        {"speaker": "NARRATOR", "text": "夜色渐深。", "instruct": ""},
        {"speaker": "林某", "text": "你来了。", "instruct": "Steady and clear."},
        {"speaker": "林某", "text": "我们走。", "instruct": " ".join(f"word{i}" for i in range(35))},
    ]
    replies = {
        "夜色渐深。": json.dumps([{
            "speaker": "NARRATOR", "text": "夜色渐深。", "instruct": "Neutral, even narration.",
        }], ensure_ascii=False),
        "我们走。": json.dumps([{
            "speaker": "林某", "text": "我们走。", "instruct": "Firm, deliberate delivery.",
        }], ensure_ascii=False),
    }
    calls = []

    def urlopen(req, *a, **k):
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        calls.append(user)
        return _BodyResp(_chat_payload(json.dumps([
            {"index": 0, "instruct": "Neutral, even narration."},
            {"index": 2, "instruct": "Firm, deliberate delivery."},
        ])))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    out, checked, fixed = validate_instructs(
        _Handle(), _LLM, GenerationConfig(), "sys",
        "CTX={context}\nSOURCE TEXT:\n{chunk}", entries, context_window=1,
    )

    assert (checked, fixed) == (2, 2)
    assert entries[0]["instruct"] == ""  # pristine input is never mutated
    assert [e["instruct"] for e in out] == [
        "Neutral, even narration.", "Steady and clear.", "Firm, deliberate delivery.",
    ]
    assert len(calls) == 1  # all targets are repaired in one request
    assert '"index": 0' in calls[0]
    assert '"index": 2' in calls[0]


# --------------------------------------------------------------------------- #
# 重判批协议（断句失败校验 / 归属抽样共用）——自已退役的独立检查模块整体搬入
# --------------------------------------------------------------------------- #

def test_validate_instructs_batches_adjacent_targets(monkeypatch):
    entries = [
        {"speaker": "NARRATOR", "text": "clean", "instruct": "steady"},
        {"speaker": "A", "text": "one", "instruct": ""},
        {"speaker": "B", "text": "two", "instruct": ""},
    ]
    calls = []

    def urlopen(req, *a, **k):
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        calls.append(user)
        return _BodyResp(_chat_payload(json.dumps([
            {"index": 1, "instruct": "calm"},
            {"index": 2, "instruct": "firm"},
        ])))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    out, checked, fixed = validate_instructs(
        _Handle(), _LLM, GenerationConfig(check_batch_size=20), "sys", "unused",
        entries, context_window=1,
    )
    assert (checked, fixed, len(calls)) == (2, 2, 1)
    assert [e["instruct"] for e in out] == ["steady", "calm", "firm"]
    assert entries[1]["instruct"] == ""


def test_validate_instructs_inherits_contiguous_narrator_directions_without_llm(monkeypatch):
    entries = [
        {"speaker": "NARRATOR", "text": "before", "instruct": "warm and steady"},
        {"speaker": "NARRATOR", "text": "middle", "instruct": ""},
        {"speaker": "NARRATOR", "text": "after", "instruct": " ".join(f"word{i}" for i in range(35))},
        {"speaker": "A", "text": "dialogue", "instruct": "clear"},
    ]

    def fail_if_called(*args, **kwargs):
        raise AssertionError("narrator inheritance should not call the LLM")

    monkeypatch.setattr(urllib.request, "urlopen", fail_if_called)
    out, checked, fixed = validate_instructs(
        _Handle(), _LLM, GenerationConfig(), "sys", "unused", entries, context_window=1,
    )

    assert (checked, fixed) == (2, 2)
    assert [entry["instruct"] for entry in out] == [
        "warm and steady", "warm and steady", "warm and steady", "clear",
    ]
    assert entries[1]["instruct"] == ""


def test_batch_window_flags_the_targets():
    entries = [_entry("N", f"t{i}") for i in range(30)]
    # A middle batch of 3 targets starting at index 10, with ±1 context.
    w = build_batch_window(entries, start=10, size=3, n=1)
    assert [e["index"] for e in w] == [9, 10, 11, 12, 13]
    assert [e.get("target") for e in w] == [None, True, True, True, None]
    assert all("instruct" not in e for e in w)  # instruct is dropped to save tokens


def test_batch_window_clamps_at_start():
    entries = [_entry("N", f"t{i}") for i in range(5)]
    w = build_batch_window(entries, start=0, size=3, n=2)  # no context before index 0
    assert [e["index"] for e in w] == [0, 1, 2, 3, 4]
    assert [e.get("target") for e in w] == [True, True, True, None, None]


def test_batch_window_clamps_at_end():
    entries = [_entry("N", f"t{i}") for i in range(5)]
    w = build_batch_window(entries, start=3, size=5, n=2)  # the block runs past the end
    assert [e["index"] for e in w] == [1, 2, 3, 4]
    assert [e.get("target") for e in w] == [None, None, True, True]


def test_batch_window_zero_n_only_targets():
    entries = [_entry("N", f"t{i}") for i in range(6)]
    w = build_batch_window(entries, start=2, size=3, n=0)
    assert [e["index"] for e in w] == [2, 3, 4]
    assert all(e.get("target") is True for e in w)


def test_batch_window_carries_original_speaker_and_text():
    entries = [_entry("ALICE", "你好"), _entry("NARRATOR", "他走了"), _entry("BOB", "再见")]
    w = build_batch_window(entries, start=0, size=2, n=1)
    assert w[0] == {"index": 0, "speaker": "ALICE", "text": "你好", "target": True}
    assert w[1] == {"index": 1, "speaker": "NARRATOR", "text": "他走了", "target": True}
    assert w[2] == {"index": 2, "speaker": "BOB", "text": "再见"}  # context: no target key


def test_batch_window_skip_excludes_skipped_targets():
    entries = [_entry("N", f"t{i}") for i in range(5)]
    # Index 2 sits inside the target range but is skipped (the spot audit's in-span
    # non-targets ride along unflagged, like context). (The block starts at 0, so the
    # window has no leading context and one trailing entry, index 3.)
    w = build_batch_window(entries, start=0, size=3, n=1, skip={2})
    assert [e["index"] for e in w] == [0, 1, 2, 3]
    assert [e.get("target") for e in w] == [True, True, None, None]
    # The default (skip=None) flags the whole range.
    w0 = build_batch_window(entries, start=0, size=3, n=1)
    assert [e["index"] for e in w0] == [0, 1, 2, 3]
    assert [e.get("target") for e in w0] == [True, True, True, None]


def test_parse_speaker_clean_object():
    assert parse_speaker('{"speaker": "ELENA"}') == "ELENA"


def test_parse_speaker_object_with_extra_keys():
    assert parse_speaker('{"speaker": "NARRATOR", "reason": "it is narration"}') == "NARRATOR"


def test_parse_speaker_strips_closed_thinking_tags():
    lt, gt = chr(60), chr(62)  # build the tags so no literal <> / newline lives in the file
    raw = lt + "think" + gt + "hmm, it is dialogue" + lt + "/think" + gt + ' {"speaker": "BOB"}'
    assert parse_speaker(raw) == "BOB"


def test_parse_speaker_markdown_fence():
    raw = "```json\n" + '{"speaker": "CARL"}' + "\n```"
    assert parse_speaker(raw) == "CARL"


def test_parse_speaker_single_element_array():
    assert parse_speaker('["ELENA"]') == "ELENA"


def test_parse_speaker_bare_token():
    assert parse_speaker("ELENA") == "ELENA"


def test_parse_speaker_sentence_is_none():
    assert parse_speaker("I think the speaker is probably ELENA because...") is None


def test_parse_speaker_empty_is_none():
    assert parse_speaker("") is None
    assert parse_speaker(None) is None


def test_parse_speaker_object_without_speaker_is_none():
    assert parse_speaker('{"foo": "bar"}') is None


def test_parse_speaker_map_full_captures_text():
    text = ('{"results": [{"index": 0, "speaker": "A", "text": "t0"},'
            ' {"index": 1, "speaker": "B"}]}')
    assert parse_speaker_map_full(text, [0, 1]) == {0: ("A", "t0"), 1: ("B", None)}


def test_parse_speaker_map_full_ignores_non_targets_and_blank_text():
    # A text for a non-target index is dropped; a blank text key is treated as absent.
    text = ('{"results": [{"index": 0, "speaker": "A", "text": "  "},'
            ' {"index": 9, "speaker": "X", "text": "z"}]}')
    assert parse_speaker_map_full(text, [0, 1]) == {0: ("A", None)}


def test_parse_speaker_map_full_speaker_projection():
    # The speaker projection of the full parser lands exactly on the target indices,
    # one speaker each (the legacy speaker-only contract).
    text = '{"results": [{"index": 3, "speaker": "A", "text": "t"}, {"index": 7, "speaker": "B"}]}'
    full = parse_speaker_map_full(text, [3, 7])
    assert {i: sp for i, (sp, _tx) in full.items()} == {3: "A", 7: "B"}


def test_strip_outer_quotes_all_supported_pairs():
    # Corner / double-corner / curly-single pairs via chr() — hand-typed quotes are unreliable.
    CB, CC = chr(0x300C), chr(0x300D)  # corner
    DB, DC = chr(0x300E), chr(0x300F)  # double corner
    LS, RS = chr(0x2018), chr(0x2019)  # curly single
    assert strip_outer_quotes(LQ + "你终于来了。" + RQ) == "你终于来了。"
    assert strip_outer_quotes(CB + "快跑！" + CC) == "快跑！"
    assert strip_outer_quotes(DB + "小心！" + DC) == "小心！"
    assert strip_outer_quotes(LS + "小声点。" + RS) == "小声点。"
    assert strip_outer_quotes(SQ + "hello" + SQ) == "hello"
    assert strip_outer_quotes(chr(39) + "hi" + chr(39)) == "hi"


def test_strip_outer_quotes_keeps_inner_quotes():
    # Only the outermost pair is stripped — an inner quoted term is preserved.
    CB, CC = chr(0x300C), chr(0x300D)
    inner = "他说" + CB + "快跑" + CC + "。"
    assert strip_outer_quotes(LQ + inner + RQ) == inner


def test_strip_outer_quotes_not_wrapped_is_none():
    assert strip_outer_quotes("他走进了房间") is None
    # A leading CLOSER (the pair is reversed) is not a wrap.
    assert strip_outer_quotes(RQ + "你来了。" + LQ) is None


def test_strip_outer_quotes_empty_interior_is_none():
    CB, CC = chr(0x300C), chr(0x300D)
    assert strip_outer_quotes(LQ + RQ) is None
    assert strip_outer_quotes(CB + CC) is None
    assert strip_outer_quotes(LQ) is None
    assert strip_outer_quotes("") is None
    assert strip_outer_quotes(None) is None


def test_majority_two_of_three():
    assert _pick_majority(["A", "A", "B"]) == "A"


def test_majority_all_three():
    assert _pick_majority(["A", "A", "A"]) == "A"


def test_majority_all_distinct_is_none():
    assert _pick_majority(["A", "B", "C"]) is None


def test_majority_tie_is_none():
    assert _pick_majority(["A", "A", "B", "B"]) is None


def test_majority_ignores_none_and_rejects_lone_vote():
    assert _pick_majority(["A"]) is None              # a lone vote is not a majority
    assert _pick_majority([None, "A", "A"]) == "A"    # None votes are ignored
    assert _pick_majority([None, None]) is None


def test_majority_four_with_and_without_majority():
    assert _pick_majority(["A", "B", "A", "C"]) == "A"
    assert _pick_majority(["A", "B", "C", "D"]) is None


def test_retry_grouping_gap_and_batch_cap():
    # Consecutive failures share a call while their ±n context windows overlap; a
    # larger gap or the batch cap starts a new group (one LLM call per group).
    assert group_retry_indices([], 4, 20) == []
    assert group_retry_indices([5], 4, 20) == [[5]]
    # Gaps ≤ n (4) stay together: 6-5=1, 10-6=4.
    assert group_retry_indices([5, 6, 10], 4, 20) == [[5, 6, 10]]
    # Gap 6 > 4 → a new group (one call must not span the gap).
    assert group_retry_indices([5, 11], 4, 20) == [[5], [11]]
    # The batch cap splits a long run into groups of ≤ batch targets.
    assert group_retry_indices(list(range(25)), 4, 10) == [
        list(range(0, 10)), list(range(10, 20)), list(range(20, 25))]
    # n=0: only truly adjacent (gap ≤ 1) indices share a call.
    assert group_retry_indices([5, 6, 8], 0, 20) == [[5, 6], [8]]


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """A throwaway project root + workspace (mirrors ``test_merge.py``) —
    ``generate_file`` writes its output through ``get_or_prepare_layout().parsed_json``."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    core_config.set_workspace_pointer(str(ws))
    yield ws
    core_config.reset_config_cache()


def test_generate_file_e2e_revalidates_suspicious_entries(tmp_path, monkeypatch, workspace):
    # 1 次解析 + 2 条疑似断句失败 × 2 次校验调用（2:0 即止）= 共 5 次 LLM 调用。
    # 第 2 条（直引号包裹 + 说道：）重写为单条；第 1 条（弯引号 + 冷笑道：）拆为两段，
    # 拆出的旁白段恰是独立纯归属标签（林某冷笑道。——无引号）→ 被确定性标签清理删除
    # （紧邻台词条目），不再进入机械合并；负例（知道，无冒号）保持原样。
    # 同人合并（全说话人）：标签删除后「二哥还没出来吗？」与「知道了。」同属林某 → 并 1 对；
    # 「嗯，去吧。」与「嗯。」同属李四 → 并 1 对；边界已有收尾标点（？/。）故不补「。」。
    source = (
        "夜色像潮水一样漫进街巷。\n"
        f"林某冷笑道：{LQ}二哥还没出来吗？{RQ}\n"
        f"林某：{LQ}知道了。{RQ}\n"
        f"说道：{SQ}嗯，去吧。{SQ}\n"
        f"李四：{SQ}嗯。{SQ}\n"
        "他知道了。\n"
    )
    t1 = f"{LQ}林某冷笑道：{LQ}二哥还没出来吗？{RQ}{RQ}"
    t2 = f"{SQ}说道：{SQ}嗯，去吧。{SQ}{SQ}"
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。", "instruct": "a"},
        {"speaker": "NARRATOR", "text": t1, "instruct": "b"},
        {"speaker": "林某", "text": f"{LQ}知道了。{RQ}", "instruct": "c"},
        {"speaker": "NARRATOR", "text": t2, "instruct": "d"},
        {"speaker": "李四", "text": f"{SQ}嗯。{SQ}", "instruct": "e"},
        {"speaker": "NARRATOR", "text": f"{LQ}他知道了。{RQ}", "instruct": "f"},
    ], ensure_ascii=False)
    w1 = json.dumps([
        {"speaker": "NARRATOR", "text": "林某冷笑道。", "instruct": "g"},
        {"speaker": "林某", "text": "二哥还没出来吗？", "instruct": "h"},
    ], ensure_ascii=False)
    w2 = json.dumps([{"speaker": "李四", "text": "嗯，去吧。", "instruct": "i"}],
                    ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        chunk = user.rsplit("SOURCE TEXT:", 1)[1].strip()
        if chunk == t1:
            return _BodyResp(_chat_payload(w1))
        if chunk == t2:
            return _BodyResp(_chat_payload(w2))
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "chapter.txt"
    src.write_bytes(source.encode("utf-8"))  # write_text 在 Windows 会翻译 \n → \r\n
    result = generate_file(
        _Handle(), str(src), _LLM, PromptsConfig(), GenerationConfig(spot_check_rate=0.0),
    )  # spot_check_rate=0：默认 0.05 会在此跑归属抽样，打破下面的调用数断言

    assert calls["n"] == 5  # 1 解析 + 2×2 校验
    assert result["count"] == 4  # 6 → 标签删除 1（林某冷笑道。）→ 同人合并 2 对（林某 / 李四各并 1）
    assert result["suspicious"] == 2
    assert result["suspicious_fixed"] == 2
    assert result["tags_deleted"] == 1  # 拆出的旁白段 = 独立纯标签（无引号）→ 删除
    assert result["merged_same_speaker"] == 2  # 林某对 / 李四对连续同 speaker → 各并 1 对
    assert result["speakers"] == ["NARRATOR", "李四", "林某"]
    assert result["output_name"] == "chapter.json"
    assert result["input_chars"] == len(source.strip())

    out = json.loads((workspace / "03_parsed_json" / "chapter.json").read_text("utf-8"))
    assert [(e["speaker"], e["text"]) for e in out] == [
        ("NARRATOR", "夜色像潮水一样漫进街巷。"),  # 纯标签段已删除，开头旁白保持原样
        ("林某", f"二哥还没出来吗？{LQ}知道了。{RQ}"),  # 连续同 speaker → 合并（边界已有 ？无补「。」）
        ("李四", f"嗯，去吧。{SQ}嗯。{SQ}"),            # 直引号重写 + 原「嗯。」同人合并（边界已有 。无补）
        ("NARRATOR", f"{LQ}他知道了。{RQ}"),           # 负例（知道，无冒号）原样
    ]
    assert out[0]["instruct"] == "a"
    assert result["entries"] == out


def test_generate_file_revalidate_off_skips_stage(tmp_path, monkeypatch, workspace):
    # revalidate_splits=False：断句失败校验整体跳过——零校验调用、suspicious /
    # suspicious_fixed = 0、日志留一行「已关闭（配置）」；疑似条目原样留在结果里，
    # 后续阶段（标签清理 / 抽样 / 合并）不受影响。
    source = (
        f"说道：{SQ}嗯，好。{SQ}\n"
        f"林某：{SQ}嗯，去吧。{SQ}\n"
    )
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": f"{SQ}说道：{SQ}嗯，好。{SQ}{SQ}", "instruct": "a"},
        {"speaker": "林某", "text": f"{SQ}嗯，去吧。{SQ}", "instruct": "b"},
    ], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "noval.txt"
    src.write_bytes(source.encode("utf-8"))
    handle = _LogHandle()
    result = generate_file(
        handle, str(src), _LLM, PromptsConfig(),
        GenerationConfig(revalidate_splits=False, spot_check_rate=0.0),
    )

    assert calls["n"] == 1  # 仅解析——校验阶段零 LLM 调用
    assert result["suspicious"] == 0
    assert result["suspicious_fixed"] == 0
    assert any("已关闭（配置）" in msg for _lv, msg in handle.logs)
    out = json.loads((workspace / "03_parsed_json" / "noval.json").read_text("utf-8"))
    # 疑似条目保持原样（未重推）
    assert [(e["speaker"], e["text"]) for e in out] == [
        ("NARRATOR", f"{SQ}说道：{SQ}嗯，好。{SQ}{SQ}"),
        ("林某", f"{SQ}嗯，去吧。{SQ}"),
    ]


def test_generate_file_instruct_off_skips_stage(tmp_path, monkeypatch, workspace):
    # validate_instructs=False（用户解析页「instruct 检查」开关关）：阶段整体跳过——
    # 空 instruct 条目不触发修复 LLM 调用（开关开时会有一批一次的重判）、
    # instruct_checked / instruct_fixed = 0、日志留一行「已关闭（配置）」。
    source = (
        "夜色渐深。\n"
        f"林某说：{LQ}你去哪了？{RQ}\n"
    )
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": "夜色渐深。", "instruct": ""},
        {"speaker": "林某", "text": f"{LQ}你去哪了？{RQ}", "instruct": ""},
    ], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "noval.txt"
    src.write_bytes(source.encode("utf-8"))
    handle = _LogHandle()
    result = generate_file(
        handle, str(src), _LLM, PromptsConfig(),
        GenerationConfig(validate_instructs=False, spot_check_rate=0.0),
    )

    assert calls["n"] == 1  # 仅解析——instruct 阶段零 LLM 调用
    assert result["instruct_checked"] == 0
    assert result["instruct_fixed"] == 0
    assert any("instruct 检查已关闭（配置）" in msg for _lv, msg in handle.logs)
    out = json.loads((workspace / "03_parsed_json" / "noval.json").read_text("utf-8"))
    assert [(e["speaker"], e["text"]) for e in out] == [
        ("NARRATOR", "夜色渐深。"),
        ("林某", f"{LQ}你去哪了？{RQ}"),
    ]


def test_generate_file_spot_off_skips_stage(tmp_path, monkeypatch, workspace):
    # spot_check_enabled=False（总开关，独立于 spot_check_rate）：抽样阶段整体跳过——
    # 即使 spot_check_rate=1.0 也零 LLM 调用、spot_* 结果字段全 0、不写
    # spot_check_history.json、日志留一行「已关闭（配置）」。
    source = (
        "夜色渐深。\n"
        f"林某说：{LQ}你终于来了。{RQ}\n"
    )
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": "夜色渐深。", "instruct": "a"},
        {"speaker": "林某", "text": f"{LQ}你终于来了。{RQ}", "instruct": "b"},
    ], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "noval.txt"
    src.write_bytes(source.encode("utf-8"))
    handle = _LogHandle()
    result = generate_file(
        handle, str(src), _LLM, PromptsConfig(),
        GenerationConfig(spot_check_enabled=False, spot_check_rate=1.0),
    )

    assert calls["n"] == 1  # 仅解析——rate 1.0 下抽样仍零 LLM 调用
    assert result["spot_checked"] == 0
    assert result["spot_fixed"] == 0
    assert result["spot_rate"] == 0.0
    assert result["spot_random_n"] == 0
    assert result["spot_random_errors"] == 0
    assert result["spot_random_rate"] is None
    assert any("归属抽样已关闭（配置）" in msg for _lv, msg in handle.logs)
    assert not (workspace / "config" / "spot_check_history.json").exists()


def test_generate_file_chunk_alignment_off_keeps_partial(tmp_path, monkeypatch, workspace):
    # check_chunk_alignment=False（用户解析页「chunk 忠实性校验」开关关）：解析阶段
    # 的忠实性校验 + 恢复重跑跳过——大段缺失也不触发恢复调用（开关开时会翻倍预算
    # 重跑，见 :890 系列），缺失部分原样保留并记入结果；解析阶段留一次性日志
    # 「chunk 忠实性校验已关闭（配置）」。
    missing = f"{LQ}那就一直走，直到看见尽头{'乙' * 110}。{RQ}她答。"
    partial = json.dumps([
        {"speaker": "NARRATOR", "text": "夜色渐深。", "instruct": "a"},
        {"speaker": "A", "text": f"{LQ}这条路没有尽头。{RQ}", "instruct": "b"},
    ], ensure_ascii=False)
    calls = {"n": 0, "max_tokens": []}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        calls["max_tokens"].append(json.loads(req.data.decode("utf-8"))["max_tokens"])
        return _BodyResp(_chat_payload(partial, "length"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    source = f"夜色渐深。\n\n{LQ}这条路没有尽头。{RQ}\n\n{missing}"
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "noval.txt"
    src.write_bytes(source.encode("utf-8"))
    handle = _LogHandle()
    result = generate_file(
        handle, str(src), _LLM, PromptsConfig(),
        GenerationConfig(check_chunk_alignment=False, spot_check_rate=0.0),
    )

    assert calls["n"] == 1  # 大段缺失也不恢复（开关开时同场景会重跑一次）
    assert calls["max_tokens"] == [4096]  # 默认预算，不翻倍
    assert any("chunk 忠实性校验已关闭（配置）" in msg for _lv, msg in handle.logs)
    assert result["count"] == 2  # 部分结果原样保留
    out = json.loads((workspace / "03_parsed_json" / "noval.json").read_text("utf-8"))
    assert [(e["speaker"], e["text"]) for e in out] == [
        ("NARRATOR", "夜色渐深。"),
        ("A", f"{LQ}这条路没有尽头。{RQ}"),
    ]


def test_generate_file_delete_tags_off_keeps_tags(tmp_path, monkeypatch, workspace):
    # delete_saying_tags=False：纯归属标签条清理整体跳过——标签条保留在结果里
    # （随后与紧邻旁白机械合并，成为被念出来的旁白行），tags_deleted=0、
    # 日志留一行「已关闭（配置）」。
    source = (
        "老道士坐在堂中，闭目养神。\n"
        "老道士瞪眼怒道。\n"
        f"老道士：{LQ}你敢动我的弟子？{RQ}\n"
    )
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": "老道士坐在堂中，闭目养神。", "instruct": "a"},
        {"speaker": "NARRATOR", "text": "老道士瞪眼怒道。", "instruct": "b"},
        {"speaker": "老道士", "text": f"{LQ}你敢动我的弟子？{RQ}", "instruct": "c"},
    ], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "tag3.txt"
    src.write_bytes(source.encode("utf-8"))
    handle = _LogHandle()
    result = generate_file(
        handle, str(src), _LLM, PromptsConfig(),
        GenerationConfig(delete_saying_tags=False, spot_check_rate=0.0),
    )

    assert calls["n"] == 1
    assert result["tags_deleted"] == 0
    assert result["count"] == 2  # 保留的标签条与紧邻旁白合并（同人合并照常运行）
    assert result["merged_same_speaker"] == 1
    assert any("已关闭（配置）" in msg for _lv, msg in handle.logs)
    out = json.loads((workspace / "03_parsed_json" / "tag3.json").read_text("utf-8"))
    assert [(e["speaker"], e["text"]) for e in out] == [
        ("NARRATOR", "老道士坐在堂中，闭目养神。老道士瞪眼怒道。"),
        ("老道士", f"{LQ}你敢动我的弟子？{RQ}"),
    ]


# --------------------------------------------------------------------------- #
# 纯归属标签条清理（确定性删除，零 LLM 成本）
# --------------------------------------------------------------------------- #


def test_pure_tag_predicate_positives():
    # 五归属动词收尾（含末尾标点形态与无标点形态；「，」/「：」/「！」/「……」均剥掉后再判）
    for t in (
        "老道瞪眼怒道。", "杜尘暗喜，急道。", "史蒂夫解释道。",
        "他沉声道：", "胖女人惊呼道！", "她颤声道……",
        "他低声说。", "他追问。", "他放声喊。", "他答道。",
        "他冷笑道",  # 无末尾标点同样命中
    ):
        assert _is_pure_saying_tag(_entry("NARRATOR", t, ""), is_chapter_title), t


def test_pure_tag_predicate_negatives():
    # 知道/难道 的「道」= 形态守卫（与归属抽样同一守卫），不是标签
    for t in ("他不知道。", "她不知道。", "他难道。"):
        assert not _is_pure_saying_tag(_entry("NARRATOR", t, ""), is_chapter_title), t
    # 动词不在句末 → 普通叙述
    for t in ("这很有道理。", "他知道答案。"):
        assert not _is_pure_saying_tag(_entry("NARRATOR", t, ""), is_chapter_title), t
    # 「叙述 + 标签」混合条（>10 字）→ 保留（阈值即两者分界）
    assert not _is_pure_saying_tag(
        _entry("NARRATOR", "老道被这一记马屁拍得舒舒服服，点头道。", ""), is_chapter_title)
    # 任何引号 → 台词内容而非标签（删了会整句丢失台词）
    assert not _is_pure_saying_tag(_entry("NARRATOR", f"{LQ}快说。{RQ}", ""), is_chapter_title)
    # 章标题（注意 CHAPTER_RE 要求「第N回」与标题之间有分隔符，空格形式才会命中）
    assert not _is_pure_saying_tag(_entry("NARRATOR", "第5回 问道", ""), is_chapter_title)
    # 角色条 → 不动
    assert not _is_pure_saying_tag(_entry("老道", "打老道。", ""), is_chapter_title)
    # 空 / 纯标点（剥尽后无核）/ 非 dict
    assert not _is_pure_saying_tag(_entry("NARRATOR", "", ""), is_chapter_title)
    assert not _is_pure_saying_tag(_entry("NARRATOR", "。。。", ""), is_chapter_title)
    assert not _is_pure_saying_tag("NARRATOR: 他说道。", is_chapter_title)


def test_pure_tag_delete_adjacency():
    line = _entry("林某", f"{LQ}知道了。{RQ}", "")
    narr = _entry("NARRATOR", "夜色像潮水一样漫进街巷。", "")
    tag = _entry("NARRATOR", "林某冷笑道。", "")
    # 台词在前 → 删
    kept, n, texts = delete_pure_saying_tags([line, tag], is_chapter_title)
    assert n == 1 and len(kept) == 1 and kept[0] is line
    assert texts == ["林某冷笑道。"]
    # 台词在后 → 删
    kept, n, _ = delete_pure_saying_tags([tag, line], is_chapter_title)
    assert n == 1 and len(kept) == 1 and kept[0] is line
    # 孤立（无邻接）→ 留
    assert delete_pure_saying_tags([tag], is_chapter_title)[1] == 0
    # 两侧皆旁白 → 留
    kept, n, _ = delete_pure_saying_tags(
        [narr, tag, _entry("NARRATOR", "风声很紧。", "")], is_chapter_title)
    assert n == 0 and len(kept) == 3
    # 隔一条旁白（距离 2）→ 留
    kept, n, _ = delete_pure_saying_tags([line, narr, tag], is_chapter_title)
    assert n == 0 and len(kept) == 3
    # 非级联：两个相邻标签，只删与台词紧邻的那一个（邻接按删除前列表判定）
    kept, n, texts = delete_pure_saying_tags(
        [_entry("NARRATOR", "风声很紧。", ""), tag, tag, line], is_chapter_title)
    assert n == 1 and len(kept) == 3 and texts == ["林某冷笑道。"]
    # 空 speaker 条目 ≠ 对白 → 不满足邻接
    kept, n, _ = delete_pure_saying_tags([_entry("", "嗯。", ""), tag], is_chapter_title)
    assert n == 0 and len(kept) == 2


def test_generate_file_e2e_pure_tag_delete(tmp_path, monkeypatch, workspace):
    # 解析产出 [旁白, 纯标签, 台词] → 标签条确定性删除（零额外 LLM 调用），
    # 结果 2 条、无合并（删除不会制造新的旁白相邻对）。
    source = (
        "老道士坐在堂中，闭目养神。\n"
        "老道士瞪眼怒道。\n"
        f"老道士：{LQ}你敢动我的弟子？{RQ}\n"
    )
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": "老道士坐在堂中，闭目养神。", "instruct": "a"},
        {"speaker": "NARRATOR", "text": "老道士瞪眼怒道。", "instruct": "b"},
        {"speaker": "老道士", "text": f"{LQ}你敢动我的弟子？{RQ}", "instruct": "c"},
    ], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "tag.txt"
    src.write_bytes(source.encode("utf-8"))
    result = generate_file(
        _Handle(), str(src), _LLM, PromptsConfig(), GenerationConfig(spot_check_rate=0.0),
    )
    assert calls["n"] == 1  # 除解析外零 LLM 调用（断句校验零命中、标签清理纯机械）
    assert result["count"] == 2
    assert result["tags_deleted"] == 1
    assert result["merged_same_speaker"] == 0
    assert result["suspicious"] == 0

    out = json.loads((workspace / "03_parsed_json" / "tag.json").read_text("utf-8"))
    assert [(e["speaker"], e["text"]) for e in out] == [
        ("NARRATOR", "老道士坐在堂中，闭目养神。"),
        ("老道士", f"{LQ}你敢动我的弟子？{RQ}"),
    ]


def test_generate_file_e2e_pure_tag_gone_before_spot(tmp_path, monkeypatch, workspace):
    # 标签条必须在归属抽样之前被删除：抽样若先跑，可能把标签条重判成角色 speaker，
    # 使其逃过纯 NARRATOR 规则。rate=1.0 全量抽样、零分歧 → 每批恰 1 次调用。
    source = (
        "老道士坐在堂中，闭目养神。\n"
        "老道士瞪眼怒道。\n"
        f"老道士：{LQ}你敢动我的弟子？{RQ}\n"
        "堂外风声鹤唳。\n"
    )
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": "老道士坐在堂中，闭目养神。", "instruct": "a"},
        {"speaker": "NARRATOR", "text": "老道士瞪眼怒道。", "instruct": "b"},
        {"speaker": "老道士", "text": f"{LQ}你敢动我的弟子？{RQ}", "instruct": "c"},
        {"speaker": "NARRATOR", "text": "堂外风声鹤唳。", "instruct": "d"},
    ], ensure_ascii=False)
    spot_payloads = []

    def urlopen(req, *a, **k):
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        if "SOURCE TEXT:" not in user:
            spot_payloads.append(user)
            return _BodyResp(_chat_payload({
                "results": [
                    {"index": 0, "speaker": "NARRATOR"},
                    {"index": 1, "speaker": "老道士"},
                    {"index": 2, "speaker": "NARRATOR"},
                ]
            }))
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "tag2.txt"
    src.write_bytes(source.encode("utf-8"))
    result = generate_file(
        _Handle(), str(src), _LLM, PromptsConfig(),
        GenerationConfig(spot_check_rate=1.0, check_context_window=10),
        rng=random.Random(7),
    )
    # 标签条在抽样前已删除：抽样窗口里根本没有它
    assert result["tags_deleted"] == 1
    assert result["count"] == 3
    assert len(spot_payloads) == 1
    assert "瞪眼怒道" not in spot_payloads[0]
    assert result["spot_checked"] == 3
    assert result["spot_fixed"] == 0  # 零分歧 → 无改判
    assert result["merged_same_speaker"] == 0

    out = json.loads((workspace / "03_parsed_json" / "tag2.json").read_text("utf-8"))
    assert [(e["speaker"], e["text"]) for e in out] == [
        ("NARRATOR", "老道士坐在堂中，闭目养神。"),
        ("老道士", f"{LQ}你敢动我的弟子？{RQ}"),
        ("NARRATOR", "堂外风声鹤唳。"),
    ]


# --------------------------------------------------------------------------- #
# 归属抽样（spot check）
# --------------------------------------------------------------------------- #

def test_adaptive_spot_rate_uses_history_without_touching_cold_start(monkeypatch):
    gen = GenerationConfig(spot_check_rate=0.05)
    monkeypatch.setattr("backend.engines.script._load_spot_history", lambda: [])
    assert adaptive_spot_rate(gen) == 0.05

    stable = [{"random": {"n": 20, "errors": 0}} for _ in range(3)]
    monkeypatch.setattr("backend.engines.script._load_spot_history", lambda: stable)
    assert adaptive_spot_rate(gen) == 0.025

    noisy = [{"random": {"n": 20, "errors": 2}} for _ in range(3)]
    monkeypatch.setattr("backend.engines.script._load_spot_history", lambda: noisy)
    assert adaptive_spot_rate(gen) == pytest.approx(0.075)


def test_spot_budget_table():
    # @0.05：预算 = max(1, round(rate·N)) 封顶 N；1/3 → 纯随机，其余 → 风险
    assert spot_budget(1, 0.05) == (0, 1)
    assert spot_budget(2, 0.05) == (0, 1)
    assert spot_budget(10, 0.05) == (0, 1)
    assert spot_budget(100, 0.05) == (2, 3)
    assert spot_budget(101, 0.05) == (2, 3)
    # rate = 0 / 负 → 关闭；无条目 → 关闭
    assert spot_budget(100, 0.0) == (0, 0)
    assert spot_budget(100, -0.1) == (0, 0)
    assert spot_budget(0, 0.05) == (0, 0)
    # rate ≥ 1 → 全量
    assert spot_budget(5, 1.0) == (2, 3)


def test_tag_in_positives():
    # 人名（2~3 字）× 五动词
    for name in ("林某", "王小明", "任昊"):
        for verb in "说道问答喊":
            assert _tag_in(f"{name}{verb}"), (name, verb)
    # 他/她 × 五动词
    for pronoun in "他她":
        for verb in "说道问答喊":
            assert _tag_in(f"{pronoun}{verb}"), (pronoun, verb)
    # 标签在句首之外的位置同样命中
    assert _tag_in("林某说他知道这件事")


def test_tag_in_negatives():
    # 知道/难道 是形态而非标签（「道」前一字符 = 知/难 的形态守卫，不是词表黑名单）
    for t in ("他知道", "她知道", "不知道", "谁知道", "他难道", "这道理", "林某知道"):
        assert not _tag_in(t), t
    # 纯旁白行（无五动词）
    assert not _tag_in("风平浪静")
    assert not _tag_in("")


def test_has_attribution_tag_prev_rule():
    # 标签可落在前一条（「林某说」在台词行之前）
    assert _has_attribution_tag("", "林某说")
    assert _has_attribution_tag("他走了", "王小明答")
    assert not _has_attribution_tag("他走了", None)
    assert not _has_attribution_tag("", "他走了")  # 前一条也无标签


def test_risk_tier_combos():
    long_tx = "这件事必须从长计议，绝对不可轻举妄动，否则后果不堪设想。"  # 29 字
    # tier 0：有标签 + 长 + 少说话人
    e0 = [_entry("林某", "林某说" + long_tx, "")]
    assert _risk_tier(e0[0], e0, 0) == 0
    # tier 1：无标签 + 长 + 少说话人
    e1 = [_entry("林某", long_tx, "")]
    assert _risk_tier(e1[0], e1, 0) == 1
    # tier 2：无标签 + 短
    e2 = [_entry("林某", "嗯。", "")]
    assert _risk_tier(e2[0], e2, 0) == 2
    # tier 3：无标签 + 短 + 窗口内 ≥4 个不同非 NARRATOR 说话人
    e3 = [
        _entry("王某", "王某沉默着，没有开口。", ""),
        _entry("赵某", "赵某只是站在原地不动。", ""),
        _entry("孙某", "孙某把门轻轻关上了。", ""),
        _entry("周某", "周某回头看了一眼。", ""),
        _entry("林某", "嗯。", ""),
    ]
    assert _risk_tier(e3[4], e3, 4) == 3


def test_risk_tier_short_boundary():
    # ≤10 字 → 短（10 字命中，11 字不命中）
    e10 = [_entry("林某", "1234567890", "")]
    e11 = [_entry("林某", "12345678901", "")]
    assert _risk_tier(e10[0], e10, 0) == 2  # 无标签 + 短
    assert _risk_tier(e11[0], e11, 0) == 1  # 仅无标签


def test_risk_tier_multi_speaker_boundary():
    long_tx = "这是一段足够长的旁白文字，用来撑满字数。"

    def mk(neighbors):
        es = [_entry(s, long_tx, "") for s in neighbors]
        es.append(_entry("林某", "林某说这是一段足够长的旁白文字。", ""))  # 有标签 + 长
        return es

    # 4 个不同非 NARRATOR 说话人（含自身）→ 命中
    e4 = mk(["王某", "赵某", "孙某", "周某"])
    assert _risk_tier(e4[-1], e4, 4) == 1
    # 3 个（2 邻居 + 自身）→ 不命中
    e3 = mk(["王某", "赵某"])
    assert _risk_tier(e3[-1], e3, len(e3) - 1) == 0


def test_risk_tier_window_clamps_at_file_start():
    # i=0：窗口 clamp 到 [0, N)——不越界，计数照常
    es = [_entry("林某", "林某说这是一段足够长的旁白文字。", "")] + [
        _entry(s, "这是一段足够长的旁白文字，用来撑满字数。", "")
        for s in ("王某", "赵某", "孙某", "周某")
    ]
    assert _risk_tier(es[0], es, 0) == 1  # 窗口 [0,5) 内 5 个不同说话人 → 命中


def _big_entries(n):
    """n 条确定性混层条目（仅两个说话人 → 无人命中多角色特征；tier = 标签 + 短）。"""
    long_tx = "这是一段足够长的旁白文字，用来撑满字数，供抽样使用。"  # 26 字，无五动词
    out = []
    for i in range(n):
        sp = "林某" if i % 2 == 0 else "李四"
        if i % 4 == 0:
            out.append(_entry(sp, "嗯。", ""))                       # 无标签 + 短 → tier 2
        elif i % 4 == 1:
            out.append(_entry(sp, f"{sp}说{long_tx}", ""))           # 标签 + 长 → tier 0
        elif i % 4 == 2:
            out.append(_entry(sp, long_tx, ""))                      # 无标签 + 长 → tier 1
        else:
            out.append(_entry(sp, f"{sp}说嗯。", ""))                # 标签 + 短 → tier 1
    return out


def test_select_buckets_disjoint_and_deterministic():
    entries = _big_entries(200)
    n_random, n_risk = spot_budget(200, 0.05)
    assert (n_random, n_risk) == (3, 7)
    rt, rk = select_spot_targets(entries, n_random, n_risk, random.Random(42))
    assert len(rt) == n_random and len(rk) == n_risk
    assert set(rt).isdisjoint(rk)  # 两桶不相交
    assert rt == sorted(rt) and rk == sorted(rk)
    assert all(0 <= i < 200 for i in rt + rk)
    # 同 seed → 同选择；换 seed → 不同选择
    assert select_spot_targets(entries, n_random, n_risk, random.Random(42)) == (rt, rk)
    assert select_spot_targets(entries, n_random, n_risk, random.Random(43)) != (rt, rk)


def test_select_cascade_fills_high_tiers_first():
    long_tx = "这是一段足够长的旁白文字，用来撑满字数。"
    E = lambda sp, tx: {"speaker": sp, "text": tx, "instruct": ""}
    # 仅两个说话人 → 无多角色特征；tier = 无标签 + 短
    entries = (
        [E("林某", "林某说" + long_tx) for _ in range(5)]   # tier 0 ×5
        + [E("李四", long_tx) for _ in range(4)]            # tier 1 ×4
        + [E("林某", "林某说嗯。") for _ in range(4)]        # tier 1 ×4
        + [E("李四", "嗯。") for _ in range(3)]             # tier 2 ×3
    )
    tier_of = [_risk_tier(entries[i], entries, i) for i in range(len(entries))]
    assert tier_of == [0] * 5 + [1] * 8 + [2] * 3  # 层构成钉死
    # 预算 12 > tier1+2 可用 11 → 必然动用 tier 0 补足（预算恒用满）
    n_random, n_risk = 1, 12
    rt, rk = select_spot_targets(entries, n_random, n_risk, random.Random(7))
    assert len(rt) == 1 and len(rk) == n_risk
    assert set(rt).isdisjoint(rk)
    # 级联不变量：抽中了 tier k，则所有更高 tier 的（非随机桶）条目都已抽中
    in_rk = set(rk)
    for k in range(3):
        if any(tier_of[i] == k for i in in_rk):
            for higher in range(k + 1, 4):
                for i in range(len(entries)):
                    if tier_of[i] == higher and i not in rt:
                        assert i in in_rk, (k, higher, i)
    # tier 0 补足必然发生
    assert any(tier_of[i] == 0 for i in in_rk)
    # 同 seed → 同选择
    assert select_spot_targets(entries, 1, 12, random.Random(7)) == (rt, rk)


def test_spot_history_append_keeps_last_cap(tmp_path, monkeypatch, workspace):
    handle = _Handle()
    for i in range(55):
        _append_spot_history(handle, f"book{i:02d}", {
            "rate": 0.05,
            "random_n": 10, "random_errors": i % 3, "random_rate": (i % 3) / 10,
            "risk_n": 20, "risk_errors": i % 2,
        })
    data = json.loads(
        (workspace / "config" / "spot_check_history.json").read_bytes().decode("utf-8"))
    runs = data["runs"]
    assert len(runs) == SPOT_CHECK_HISTORY_CAP  # 55 条 → 丢最旧的 5 条
    assert [r["file"] for r in runs] == [f"book{i:02d}" for i in range(5, 55)]
    first = runs[0]
    assert first["file"] == "book05"
    assert first["rate"] == 0.05
    assert first["random"] == {"n": 10, "errors": 2, "rate": 0.2}
    assert first["risk"] == {"n": 20, "errors": 1}
    assert first["ts"]


def test_spot_history_corrupt_or_missing(tmp_path, monkeypatch, workspace):
    (workspace / "config").mkdir(parents=True, exist_ok=True)
    hist = workspace / "config" / "spot_check_history.json"
    hist.write_bytes(b"{corrupt")
    assert _load_spot_history() == []  # 损坏 → 空历史（不抛）
    hist.write_bytes(b'{"runs": "not-a-list"}')
    assert _load_spot_history() == []
    hist.unlink()
    assert _load_spot_history() == []  # 缺失 → 空历史


def test_spot_off_makes_zero_llm_calls(monkeypatch):
    def urlopen(req, *a, **k):
        raise AssertionError("no LLM call when spot is off")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    entries = [dict(e) for e in _SPOT_FIXTURE]
    res, st = spot_check_speakers(_Handle(), _LLM, _SPOT_GEN, entries, 0.0)
    assert res is entries
    assert st == {
        "checked": 0, "fixed": 0, "rate": 0.0,
        "random_n": 0, "random_errors": 0, "random_rate": None,
        "risk_n": 0, "risk_errors": 0,
    }
    # rate ≤ 0 → 关闭；空条目 → 关闭（零统计、零调用）
    res, st = spot_check_speakers(_Handle(), _LLM, _SPOT_GEN, entries, -0.5)
    assert res is entries and st["checked"] == 0
    res, st = spot_check_speakers(_Handle(), _LLM, _SPOT_GEN, [], 0.05)
    assert res == [] and st["checked"] == 0


# 6 条小文件：check_context_window=10 → 任何目标组合都只有一组（一次调用）
_SPOT_FIXTURE = [
    {"speaker": "NARRATOR", "text": "夜色渐浓，街上的行人稀落下来。", "instruct": "a"},
    {"speaker": "林某", "text": f"{LQ}你终于来了。{RQ}", "instruct": "b"},
    {"speaker": "林某", "text": "他站在门口，半天没有出声。", "instruct": "c"},
    {"speaker": "NARRATOR", "text": f"{LQ}嗯，路上堵。{RQ}", "instruct": "d"},
    {"speaker": "李四", "text": f"{SQ}知道了。{SQ}", "instruct": "e"},
    {"speaker": "NARRATOR", "text": "两人转身，走进了巷子深处。", "instruct": "f"},
]
_SPOT_GEN = GenerationConfig(check_batch_size=20, check_context_window=10)


def test_spot_zero_disagreement_one_call_no_change(monkeypatch):
    # 首判与原判全同 → 每组恰 1 次调用、零改动、原列表对象原样返回
    entries = [dict(e) for e in _SPOT_FIXTURE]
    n_random, n_risk = spot_budget(len(entries), 0.5)
    rt, rk = select_spot_targets(entries, n_random, n_risk, random.Random(1))
    targets = sorted(set(rt) | set(rk))
    assert len(targets) == n_random + n_risk
    payload = json.dumps({"results": [
        {"index": t, "speaker": entries[t]["speaker"]} for t in targets
    ]}, ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        return _BodyResp(_chat_payload(payload))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    result, stats = spot_check_speakers(
        _Handle(), _LLM, _SPOT_GEN, entries, 0.5, random.Random(1))
    assert calls["n"] == 1  # 无分歧 → 无重试
    assert result is entries  # 零修正 → 原列表对象
    assert stats["checked"] == len(targets)
    assert stats["fixed"] == 0
    assert stats["random_errors"] == 0 and stats["risk_errors"] == 0


def test_spot_21_majority_applies_after_two_calls(monkeypatch):
    # 模型恒判「林某」：非林某目标 → [原值, 林某, 林某] = 2:1 → 首判 + 1 重试即停
    entries = [dict(e) for e in _SPOT_FIXTURE]
    n_random, n_risk = spot_budget(len(entries), 0.5)
    rt, rk = select_spot_targets(entries, n_random, n_risk, random.Random(1))
    targets = sorted(set(rt) | set(rk))
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        payload = json.dumps({"results": [
            {"index": t, "speaker": "林某"} for t in targets
        ]}, ensure_ascii=False)
        return _BodyResp(_chat_payload(payload))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    result, stats = spot_check_speakers(
        _Handle(), _LLM, _SPOT_GEN, entries, 0.5, random.Random(1))

    flipped = [t for t in targets if entries[t]["speaker"] != "林某"]
    assert flipped  # 6 条中至多 2 条是林某 → 必有可翻转目标
    assert calls["n"] == 2  # 首判 + 1 次重试（2:1 早停，无第 2/3 次）
    assert stats["fixed"] == len(flipped)
    assert [e["speaker"] for e in result] == [
        "林某" if t in flipped else e["speaker"] for t, e in enumerate(entries)
    ]
    # 只改 speaker：text / instruct 一律不动
    for t in flipped:
        assert result[t]["text"] == entries[t]["text"]
        assert result[t]["instruct"] == entries[t]["instruct"]
    for t, e in enumerate(result):
        if t not in flipped:
            assert e == entries[t]


def test_spot_no_consensus_four_calls_keeps_original(monkeypatch):
    # 每次调用都换一个全新说话人 → 5 票 5 样 → 3 次重试跑满 → 保留原值
    entries = [dict(e) for e in _SPOT_FIXTURE]
    n_random, n_risk = spot_budget(len(entries), 0.5)
    rt, rk = select_spot_targets(entries, n_random, n_risk, random.Random(1))
    targets = sorted(set(rt) | set(rk))
    seq = ["王某", "赵某", "孙某", "周某"]  # 均不在夹具说话人集合里
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        sp = seq[min(calls["n"] - 1, len(seq) - 1)]
        payload = json.dumps({"results": [
            {"index": t, "speaker": sp} for t in targets
        ]}, ensure_ascii=False)
        return _BodyResp(_chat_payload(payload))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    result, stats = spot_check_speakers(
        _Handle(), _LLM, _SPOT_GEN, entries, 0.5, random.Random(1))

    assert calls["n"] == 4  # 首判 + 3 次重试（始终无严格多数）
    assert stats["fixed"] == 0
    assert result is entries  # 全部保留 → 原列表对象


def test_spot_quote_strip_adoption_rules(monkeypatch):
    # 零分歧（重申原 speaker）+ 可选 text 键的三条采纳规则（rate=1.0 → 全量 6 条）：
    #  - 与「仅去外层引号」机械值严格一致 + 角色 → 采纳（写入计算值）
    #  - 不符 → WARNING + 忽略
    #  - 终值 NARRATOR → 绝不剥
    entries = [dict(e) for e in _SPOT_FIXTURE]
    n_random, n_risk = spot_budget(len(entries), 1.0)
    rt, rk = select_spot_targets(entries, n_random, n_risk, random.Random(1))
    targets = sorted(set(rt) | set(rk))
    assert targets == list(range(6))

    handle = _LogHandle()
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        items = []
        for t in targets:
            item = {"index": t, "speaker": entries[t]["speaker"]}
            stripped = strip_outer_quotes(entries[t]["text"] or "")
            if stripped:
                if entries[t]["speaker"] == "NARRATOR":
                    item["text"] = stripped           # 终值 NARRATOR → 必须忽略
                elif t == 1:
                    item["text"] = stripped + "改"    # 不符 → WARNING + 忽略
                else:
                    item["text"] = stripped           # 严格一致 → 采纳
            items.append(item)
        return _BodyResp(_chat_payload(json.dumps({"results": items}, ensure_ascii=False)))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    result, stats = spot_check_speakers(
        handle, _LLM, _SPOT_GEN, entries, 1.0, random.Random(1))

    assert calls["n"] == 1  # 零分歧 → 无重试
    assert stats["fixed"] == 0
    # 条目 4（李四，直引号包裹）→ 采纳（写入机械计算值）
    assert result[4]["text"] == strip_outer_quotes(entries[4]["text"])
    assert result[4]["text"] != entries[4]["text"]
    # 条目 1（林某包裹，回值不符）→ 保持原样
    assert result[1]["text"] == entries[1]["text"]
    # 条目 3（NARRATOR 包裹）→ 不剥
    assert result[3]["text"] == entries[3]["text"]
    # 其余条目整体不动；instruct 全程不动
    for t in (0, 2, 5):
        assert result[t] == entries[t]
    assert all(r["instruct"] == e["instruct"] for r, e in zip(result, entries))
    # 不符回值产生了 WARNING
    assert any(level == "WARNING" for level, _ in handle.logs)


def test_spot_cancel_propagates_and_changes_nothing(monkeypatch):
    entries = [dict(e) for e in _SPOT_FIXTURE]

    def urlopen(req, *a, **k):
        raise AssertionError("no LLM call may happen once cancelled")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(TaskCancelled):
        spot_check_speakers(
            _Handle(cancelled=True), _LLM, _SPOT_GEN, entries, 0.5, random.Random(1))
    # 输入列表原样（浅拷贝在取消前不落地）
    assert entries == [dict(e) for e in _SPOT_FIXTURE]


def test_generate_file_e2e_spot_check(tmp_path, monkeypatch, workspace):
    # 1 解析 + spot 首判 + 1 次投票重试 = 3 次调用。纯随机桶目标被（每次回复都）改判为
    # 花名册内另一角色 → 2:1 → 应用并随基文件写出；读数进 result 与历史文件。
    source = (
        "夜色像潮水一样漫进街巷。\n"
        f"林某说：{LQ}这件事要慎重。{RQ}\n"
        f"李四：{SQ}嗯，好。{SQ}\n"
        "他点了点头，没有再说什么。\n"
    )
    parse_entries = [
        {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。", "instruct": "a"},
        {"speaker": "林某", "text": f"{LQ}这件事要慎重。{RQ}", "instruct": "b"},
        {"speaker": "NARRATOR", "text": f"{SQ}嗯，好。{SQ}", "instruct": "c"},  # 对白藏在旁白
        {"speaker": "NARRATOR", "text": "他点了点头，没有再说什么。", "instruct": "d"},
    ]
    parse_reply = json.dumps(parse_entries, ensure_ascii=False)
    rate = 1.0
    n_random, n_risk = spot_budget(len(parse_entries), rate)
    assert (n_random, n_risk) == (1, 3)
    rng_seed = 42
    rt, rk = select_spot_targets(parse_entries, n_random, n_risk,
                                 random.Random(rng_seed))
    assert rt  # 纯随机仪表桶非空
    flip = rt[0]
    wrong = "李四" if parse_entries[flip]["speaker"] != "李四" else "林某"
    targets = sorted(set(rt) | set(rk))

    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        if "SOURCE TEXT:" in user:
            return _BodyResp(_chat_payload(parse_reply))
        payload = json.dumps({"results": [
            {"index": t, "speaker": wrong if t == flip else parse_entries[t]["speaker"]}
            for t in targets
        ]}, ensure_ascii=False)
        return _BodyResp(_chat_payload(payload))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "chapter.txt"
    src.write_bytes(source.encode("utf-8"))
    result = generate_file(
        _Handle(), str(src), _LLM, PromptsConfig(),
        GenerationConfig(spot_check_rate=rate, check_context_window=10),
        rng=random.Random(rng_seed),
    )

    assert calls["n"] == 3  # 1 解析 + spot 首判 + 1 次投票重试
    # 修正已随基文件写出——翻牌条目本身是角色，永不被合并，下标不受影响
    out = json.loads((workspace / "03_parsed_json" / "chapter.json").read_text("utf-8"))
    assert out[flip]["speaker"] == wrong
    # 其余条目 = 翻牌后的列表经同人段落合并（spot 阶段在合并之前——与引擎同一确定性链）
    post = [dict(e) for e in parse_entries]
    post[flip]["speaker"] = wrong
    expected, _merged = merge_adjacent_same_speaker(post, is_chapter_title)
    assert [(e["speaker"], e["text"]) for e in out] == \
        [(e["speaker"], e["text"]) for e in expected]
    assert result["count"] == len(expected)
    assert result["merged_same_speaker"] == _merged
    # 六个 result 字段
    assert result["spot_checked"] == len(targets)
    assert result["spot_fixed"] == 1
    assert result["spot_rate"] == rate
    assert result["spot_random_n"] == n_random
    assert result["spot_random_errors"] == 1  # 被改判的条目属纯随机桶
    assert result["spot_random_rate"] == 1.0 / n_random
    # 历史文件含本书读数（config/ 下，绝不进 03_parsed_json/）
    hist = json.loads(
        (workspace / "config" / "spot_check_history.json").read_bytes().decode("utf-8"))
    assert len(hist["runs"]) == 1
    run = hist["runs"][0]
    assert run["file"] == "chapter"
    assert run["rate"] == rate
    assert run["random"] == {"n": n_random, "errors": 1, "rate": 1.0 / n_random}
    assert run["risk"]["n"] == n_risk
    assert run["risk"]["errors"] == 0
    assert run["ts"]
    # 解析产物目录里只有基文件（历史文件不得污染「全部文件」聚合）
    assert [p.name for p in (workspace / "03_parsed_json").iterdir()] == ["chapter.json"]


def test_generate_file_cancel_mid_spot_writes_nothing(tmp_path, monkeypatch, workspace):
    # 取消落在 spot 阶段的 LLM 调用上 → TaskCancelled 上抛：基文件与历史文件都不落盘
    # （两者都只在阶段整体返回之后才写）。
    source = (
        "夜色像潮水一样漫进街巷。\n"
        f"林某说：{LQ}这件事要慎重。{RQ}\n"
        "他走了。\n"
    )
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。", "instruct": "a"},
        {"speaker": "林某", "text": f"{LQ}这件事要慎重。{RQ}", "instruct": "b"},
        {"speaker": "NARRATOR", "text": "他走了。", "instruct": "c"},
    ], ensure_ascii=False)

    def urlopen(req, *a, **k):
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        if "SOURCE TEXT:" in user:
            return _BodyResp(_chat_payload(parse_reply))
        raise TaskCancelled("cancel landed in the spot stage")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "chapter.txt"
    src.write_bytes(source.encode("utf-8"))
    with pytest.raises(TaskCancelled):
        generate_file(
            _Handle(), str(src), _LLM, PromptsConfig(),
            GenerationConfig(spot_check_rate=1.0, check_context_window=10),
        )
    assert not (workspace / "03_parsed_json" / "chapter.json").exists()
    assert not (workspace / "config" / "spot_check_history.json").exists()


# --------------------------------------------------------------------------- #
# 角色匹配检查（chunk 边界重判——解析内第四检查阶段）
# --------------------------------------------------------------------------- #

def _window_from(user: str, end_marker: str) -> list:
    """Extract the context-window JSON array at the head of a re-judgment / re-parse
    user prompt (the window precedes the trailing instruction block)."""
    return json.loads(user[:user.index(end_marker)])


def _two_para_source() -> str:
    """Two 3-line paragraphs (dialogue / narration / dialogue) — with chunk_size=50
    this splits into exactly 2 chunks at the paragraph boundary."""
    p1 = (
        f"林某说：{LQ}这件事要慎重。{RQ}\n"
        "夜色像潮水一样漫进街巷。\n"
        f"林某说：{LQ}我们明天再谈。{RQ}"
    )
    p2 = (
        f"李四说：{LQ}这件事我不同意。{RQ}\n"
        "他点了点头。\n"
        f"杜尘说：{LQ}那就先这样。{RQ}"
    )
    return p1 + "\n\n" + p2


# 每段 3 条解析条目（对白 / 旁白 / 对白）
PARSE_REPLY_P1 = json.dumps([
    {"speaker": "林某", "text": f"{LQ}这件事要慎重。{RQ}", "instruct": "a"},
    {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。", "instruct": "b"},
    {"speaker": "林某", "text": f"{LQ}我们明天再谈。{RQ}", "instruct": "c"},
], ensure_ascii=False)
PARSE_REPLY_P2 = json.dumps([
    {"speaker": "李四", "text": f"{LQ}这件事我不同意。{RQ}", "instruct": "d"},
    {"speaker": "NARRATOR", "text": "他点了点头。", "instruct": "e"},
    {"speaker": "杜尘", "text": f"{LQ}那就先这样。{RQ}", "instruct": "f"},
], ensure_ascii=False)


def _expected_entries() -> list:
    return [
        {"speaker": "林某", "text": f"{LQ}这件事要慎重。{RQ}", "instruct": "a"},
        {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。", "instruct": "b"},
        {"speaker": "林某", "text": f"{LQ}我们明天再谈。{RQ}", "instruct": "c"},
        {"speaker": "李四", "text": f"{LQ}这件事我不同意。{RQ}", "instruct": "d"},
        {"speaker": "NARRATOR", "text": "他点了点头。", "instruct": "e"},
        {"speaker": "杜尘", "text": f"{LQ}那就先这样。{RQ}", "instruct": "f"},
    ]


# 被测阶段之外的检查阶段全部关闭（调用数才能钉死）；同人段落合并同样关闭——
# 边界重判可改出连续同 speaker 对，合并在其**后**运行会坍缩基文件条目数、干扰
# 「out == 原始 6 条目」的断言（合并本身有专测，不在此处掺入）。
_STAGES_OFF = dict(
    revalidate_splits=False, delete_saying_tags=False, spot_check_rate=0.0,
    check_long_paragraphs=False, absorb_punct_entries=False, merge_same_speaker=False,
)


def _boundary_e2e(tmp_path, monkeypatch, workspace, rejudge_reply,
                  parse_replies=(PARSE_REPLY_P1, PARSE_REPLY_P2), **gen_kwargs):
    """双段源 e2e：解析回复固定，重判回复由测试经 ``rejudge_reply(user, state)``
    供给（state = 共享调用计数）。返回 ``(handle, result, state)``。"""
    gen = GenerationConfig(chunk_size=50, **gen_kwargs)
    state = {"n": 0}

    def urlopen(req, *a, **k):
        state["n"] += 1
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        if "SOURCE TEXT:" in user:
            if state["n"] <= len(parse_replies):
                # 解析调用（前 N 次 = 按 chunk 序解析）
                return _BodyResp(_chat_payload(parse_replies[state["n"] - 1]))
            # 断句重推（同形 "SOURCE TEXT:" 用户提示）——同样交给测试
            return _BodyResp(_chat_payload(rejudge_reply(user, state)))
        # 边界重判（捆绑重判提示词，无 SOURCE TEXT 标记）
        return _BodyResp(_chat_payload(rejudge_reply(user, state)))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "boundary.txt"
    src.write_bytes(_two_para_source().encode("utf-8"))
    handle = _LogHandle()
    result = generate_file(handle, str(src), _LLM, PromptsConfig(), gen)
    return handle, result, state


def test_select_boundary_targets_unit():
    # 单 chunk → 无内部边界 → 零目标
    assert select_boundary_targets([6], 4, 6) == []
    # n=0 → 零目标（窗宽 0 = 阶段退化为零 LLM 调用的空操作）
    assert select_boundary_targets([3, 6], 0, 6) == []
    # 单个内部边界 b=3、两侧各 n 条 → [3-1, 3+1)
    assert select_boundary_targets([3, 6], 1, 6) == [2, 3]
    # 相邻两边界窗口重叠 → 全局去重
    assert select_boundary_targets([3, 5, 6], 2, 6) == [1, 2, 3, 4, 5]
    # 边界贴近文件头/尾 → 钳入 [0, total)
    assert select_boundary_targets([1, 6], 3, 6) == [0, 1, 2, 3]
    assert select_boundary_targets([5, 6], 3, 6) == [2, 3, 4, 5]


def test_boundary_risk_filter_skips_clean_and_keeps_speaker_turn():
    clean = [
        {"speaker": "A", "text": "first."},
        {"speaker": "A", "text": "second."},
        {"speaker": "A", "text": "third."},
    ]
    assert select_boundary_risk_targets(clean, [2, 3], 1) == []
    risky = [
        {"speaker": "A", "text": "first."},
        {"speaker": "B", "text": "second."},
        {"speaker": "B", "text": "third."},
    ]
    assert select_boundary_risk_targets(risky, [1, 3], 1) == [0, 1]


def test_boundary_single_chunk_silent(monkeypatch):
    # 单 chunk 文件（无内部边界）→ 静默返回：零 LLM 调用、零日志行
    # （与「零目标」留痕日志相区分——单段文件不是"阶段缺失"）
    def boom(*a, **k):
        raise AssertionError("no LLM call may happen")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    entries = [_entry("NARRATOR", "夜色像潮水一样漫进街巷。")]
    handle = _LogHandle()
    out, stats = boundary_check_speakers(handle, _LLM,
                                         GenerationConfig(check_context_window=4),
                                         entries, [1])
    assert out is entries  # 原始列表对象身份（零修正）
    assert stats == {"checked": 0, "fixed": 0}
    assert handle.logs == []


def test_boundary_zero_targets_leaves_log(monkeypatch):
    # n=0 → 零边界目标：仍留一行日志（与断句校验同一理由——防静默退出被误读为
    # 阶段缺失），零 LLM 调用
    def boom(*a, **k):
        raise AssertionError("no LLM call may happen")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    entries = [_entry("NARRATOR", "夜色。"), _entry("林某", "好。"),
               _entry("NARRATOR", "他走了。"), _entry("李四", "嗯。")]
    handle = _LogHandle()
    out, stats = boundary_check_speakers(handle, _LLM,
                                         GenerationConfig(check_context_window=0),
                                         entries, [2, 4])
    assert out is entries
    assert stats == {"checked": 0, "fixed": 0}
    assert handle.logs == [("INFO", "角色匹配检查：0 条边界目标（无重判，零 LLM 调用）")]


def test_boundary_stage_switch_off(tmp_path, monkeypatch, workspace):
    # check_boundary_speakers=False → 整体跳过：零重判 LLM 调用、一行「已关闭（配置）」
    # 日志、结果字段保持 0；基文件照常落盘
    def rejudge(user, state):
        raise AssertionError("the boundary stage must be skipped entirely")

    handle, result, calls = _boundary_e2e(
        tmp_path, monkeypatch, workspace, rejudge, **_STAGES_OFF,
        check_context_window=1, check_boundary_speakers=False)
    assert calls["n"] == 2  # 只有两次解析调用
    assert result["boundary_checked"] == 0 and result["boundary_fixed"] == 0
    assert any("角色匹配检查已关闭（配置）" in m for _l, m in handle.logs)
    out = json.loads((workspace / "03_parsed_json" / "boundary.json").read_text("utf-8"))
    assert out == _expected_entries()


def test_boundary_window_crosses_chunks_context_untouched(tmp_path, monkeypatch, workspace):
    # 跨 chunk 窗口：窗口 = 目标块 ± n，首尾条目来自边界两侧的不同 chunk（正是解析时
    # 被切断的上下文）；context 条目永不被改写——零修正时基文件 = 原始 6 条目
    users = []

    def rejudge(user, state):
        users.append(user)
        win = _window_from(user, "\n\nRe-judge")
        return json.dumps({"results": [
            {"index": it["index"], "speaker": it["speaker"]}
            for it in win if it.get("target")
        ]}, ensure_ascii=False)

    handle, result, calls = _boundary_e2e(
        tmp_path, monkeypatch, workspace, rejudge, **_STAGES_OFF, check_context_window=1)
    assert calls["n"] == 3  # 2 解析 + 1 重判（无分歧 → 无重试）
    assert result["boundary_checked"] == 2 and result["boundary_fixed"] == 0

    win = _window_from(users[0], "\n\nRe-judge")
    assert [it["index"] for it in win] == [1, 2, 3, 4]
    assert [it["index"] for it in win if it.get("target")] == [2, 3]
    # 窗口首 = chunk 1 的边界侧条目、尾 = chunk 2 的边界侧条目（均未标 target 的上下文）
    assert win[0] == {"index": 1, "speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。"}
    assert win[3] == {"index": 4, "speaker": "NARRATOR", "text": "他点了点头。"}

    out = json.loads((workspace / "03_parsed_json" / "boundary.json").read_text("utf-8"))
    assert out == _expected_entries()


def test_boundary_majority_adoption(tmp_path, monkeypatch, workspace):
    # 2:1 严格多数 → 修正生效：目标条目 speaker 被改写并随基文件落盘
    # （context 条目保持原样）
    def rejudge(user, state):
        win = _window_from(user, "\n\nRe-judge")
        return json.dumps({"results": [
            {"index": it["index"],
             "speaker": "李四" if it["index"] == 2 else it["speaker"]}
            for it in win if it.get("target")
        ]}, ensure_ascii=False)

    handle, result, calls = _boundary_e2e(
        tmp_path, monkeypatch, workspace, rejudge, **_STAGES_OFF, check_context_window=1)
    assert calls["n"] == 4  # 2 解析 + 首判 + 1 次投票重试（2:1 即决）
    assert result["boundary_checked"] == 2 and result["boundary_fixed"] == 1

    out = json.loads((workspace / "03_parsed_json" / "boundary.json").read_text("utf-8"))
    expected = _expected_entries()
    expected[2]["speaker"] = "李四"
    assert out == expected


def test_boundary_no_consensus_keeps_original(tmp_path, monkeypatch, workspace):
    # 5 个互异投票（原值 + 首判 + 3 次重试）→ 无严格多数 → 保留原值（从不猜），
    # 条目原样落盘
    flips = {3: "李四", 4: "NARRATOR", 5: "杜尘", 6: "王五"}

    def rejudge(user, state):
        win = _window_from(user, "\n\nRe-judge")
        return json.dumps({"results": [
            {"index": it["index"],
             "speaker": flips[state["n"]] if it["index"] == 2 else it["speaker"]}
            for it in win if it.get("target")
        ]}, ensure_ascii=False)

    handle, result, calls = _boundary_e2e(
        tmp_path, monkeypatch, workspace, rejudge, **_STAGES_OFF, check_context_window=1)
    assert calls["n"] == 6  # 2 解析 + 首判 + 3 次重试（始终无共识）
    assert result["boundary_fixed"] == 0
    assert any("重试 3 次仍无共识" in m for _l, m in handle.logs)
    out = json.loads((workspace / "03_parsed_json" / "boundary.json").read_text("utf-8"))
    assert out == _expected_entries()


def test_boundary_cancel_mid_stage_writes_nothing(tmp_path, monkeypatch, workspace):
    # 取消落在边界阶段的 LLM 调用上 → TaskCancelled 上抛：基文件不落盘
    # （基文件在所有检查阶段返回后才写）
    def rejudge(user, state):
        raise TaskCancelled("cancel landed in the boundary stage")

    with pytest.raises(TaskCancelled):
        _boundary_e2e(tmp_path, monkeypatch, workspace, rejudge,
                      **_STAGES_OFF, check_context_window=1)
    assert not (workspace / "03_parsed_json" / "boundary.json").exists()


def test_boundary_groups_use_pristine_windows(tmp_path, monkeypatch, workspace):
    # check_batch_size=1 → 2 个单目标组（n=1 时边界两侧共 2 条目标）：第 1 组把 E2
    # 改为 李四；第 2 组的窗口里 E2 作为未标记 context 条目出现，仍须显示原始 E2——
    # 窗口恒由 pristine 列表预建，已应用的修正不回流
    windows = []

    def rejudge(user, state):
        win = _window_from(user, "\n\nRe-judge")
        windows.append(win)
        return json.dumps({"results": [
            {"index": it["index"],
             "speaker": "李四" if (state["n"] in (3, 4) and it["index"] == 2) else it["speaker"]}
            for it in win if it.get("target")
        ]}, ensure_ascii=False)

    handle, result, calls = _boundary_e2e(
        tmp_path, monkeypatch, workspace, rejudge, **_STAGES_OFF,
        check_context_window=1, check_batch_size=1)
    assert calls["n"] == 5  # 2 解析 + (首判 + 重试) + 首判
    assert result["boundary_fixed"] == 1
    # 第 1 组的重试逐字节复用首判窗口（同一预建窗口），其中 E2 仍是原值
    assert windows[0] == windows[1]
    assert all(it["index"] != 2 or it["speaker"] == "林某"
               for win in windows[:2] for it in win)
    # 第 2 组窗口 = E2, E3, E4：E2（context）仍是原始 林某，不是改后的 李四
    # （context 条目不带 target 键——build_batch_window 只给 target 打标记）
    g2 = {it["index"]: it for it in windows[2]}
    assert "target" not in g2[2] and g2[2]["speaker"] == "林某"
    assert g2[3].get("target") is True and g2[3]["speaker"] == "李四"
    assert "target" not in g2[4] and g2[4]["speaker"] == "NARRATOR"
    # 修正只落在 target 条目：E2 = 李四 进基文件，其余不动
    out = json.loads((workspace / "03_parsed_json" / "boundary.json").read_text("utf-8"))
    expected = _expected_entries()
    expected[2]["speaker"] = "李四"
    assert out == expected


def test_boundary_stage_ordering_before_revalidate(tmp_path, monkeypatch, workspace):
    # 阶段顺序钉死：边界窗口取自 pristine 列表（E1 仍是 NARRATOR）；断句校验窗口
    # 取自边界修正后的列表（E1 已是 林某）——边界阶段必须居检查段最前（断句拆条 /
    # 标签删条都会移动下标，后两者的窗口必须看到改后列表）
    e2_susp = f"{LQ}林某道：{LQ}我们明天再谈。{RQ}{RQ}"  # 解析回复的 E2 = 疑似断句失败形态
    p1_susp = json.dumps([
        {"speaker": "林某", "text": f"{LQ}这件事要慎重。{RQ}", "instruct": "a"},
        {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。", "instruct": "b"},
        {"speaker": "林某", "text": e2_susp, "instruct": "c"},
    ], ensure_ascii=False)
    rederive = json.dumps([{"speaker": "林某", "text": "我们明天再谈。"}], ensure_ascii=False)
    boundary_users, revalidate_users = [], []

    def rejudge(user, state):
        if "SOURCE TEXT:" in user:
            # 断句重推（两次相同回复 → 2:0）
            revalidate_users.append(user)
            return rederive
        boundary_users.append(user)
        win = _window_from(user, "\n\nRe-judge")
        return json.dumps({"results": [
            {"index": it["index"],
             "speaker": "林某" if it["index"] == 1 else it["speaker"]}
            for it in win if it.get("target")
        ]}, ensure_ascii=False)

    handle, result, calls = _boundary_e2e(
        tmp_path, monkeypatch, workspace, rejudge,
        parse_replies=(p1_susp, PARSE_REPLY_P2),
        delete_saying_tags=False, spot_check_rate=0.0, check_context_window=2,
        merge_same_speaker=False)  # 本测锁定边界/断句阶段顺序；合并在其后运行会坍缩
    # 基文件条目数（E0+E1+E2 同为 林某），干扰对 E1/E2 逐条的断言——合并本身有专测
    # 断句失败校验保持开启（默认）；2 解析 + 边界(首判 + 重试) + 断句(2 次同票)
    assert calls["n"] == 6
    assert result["boundary_checked"] == 4 and result["boundary_fixed"] == 1
    assert result["suspicious"] == 1 and result["suspicious_fixed"] == 1
    assert result["count"] == 6 and result["merged_same_speaker"] == 0

    # 边界窗口（pristine）：E1 仍是原始 NARRATOR
    b_items = {it["index"]: it for it in _window_from(boundary_users[0], "\n\nRe-judge")}
    assert set(b_items) == {0, 1, 2, 3, 4, 5}
    assert b_items[1]["speaker"] == "NARRATOR"
    assert {i for i, it in b_items.items() if it.get("target")} == {1, 2, 3, 4}
    # 断句窗口（改后）：E1 已是边界阶段修正的 林某
    line = next(l for l in revalidate_users[0].splitlines()
                if l.startswith('{"index": 1'))
    assert json.loads(line)["speaker"] == "林某"
    # 断句输入 = E2 的原文（边界阶段只改 speaker、不改 text）
    i = revalidate_users[0].index("SOURCE TEXT:\n")
    assert revalidate_users[0][i + len("SOURCE TEXT:\n"):] == e2_susp
    # 基文件：E1 = 林某（边界修正，instruct 不动）；E2 = 断句重推（instruct 归空）
    out = json.loads((workspace / "03_parsed_json" / "boundary.json").read_text("utf-8"))
    assert out[1] == {"speaker": "林某", "text": "夜色像潮水一样漫进街巷。", "instruct": "b"}
    assert out[2] == {"speaker": "林某", "text": "我们明天再谈。", "instruct": ""}


# --------------------------------------------------------------------------- #
# 超长段落检查（阶段 A：LLM 重切 + 机械分段兜底）与纯标点条目吸收（阶段 B）
# --------------------------------------------------------------------------- #

# 固定超长样例（212 字 > 200）：旁白长独白 + 内嵌一句台词 + 收尾旁白
LONG_HEAD = ("夜色像潮水一样漫进街巷。" + "风从窗缝里挤进来，吹得烛火摇个不停。" * 10
             + "林某说：")
LONG_SRC = LONG_HEAD + " " + LQ + "我们明天再谈。" + RQ + "他点了点头。"
assert len(LONG_SRC) == 212 and len(LONG_HEAD) == 196

_SKEL = lambda t: "".join(c for c in t if c.isalnum())


def test_split_long_text_unit():
    # 短文本（≤ 上限）原样单段
    assert split_long_text("短文本。", 200) == ["短文本。"]
    assert split_long_text("", 200) == []
    # ① 句末 tier：取最接近目标宽度的边界（300 字 @ 200 → [200, 100]，无损、各段 ≤ 200）
    t = "一。" * 150
    parts = split_long_text(t, 200)
    assert [len(p) for p in parts] == [200, 100]
    assert _SKEL("".join(parts)) == _SKEL(t)
    assert all(len(p) <= 200 for p in parts)
    # ② 子句 tier（无句末标点、全逗号）
    t2 = "甲，" * 150
    parts2 = split_long_text(t2, 200)
    assert [len(p) for p in parts2] == [200, 100]
    assert _SKEL("".join(parts2)) == _SKEL(t2)
    # ③ 定宽硬切（无任何边界字符）
    t3 = "甲" * 300
    parts3 = split_long_text(t3, 200)
    assert [len(p) for p in parts3] == [200, 100]
    # 唯一边界远离目标宽度 → 仍取该边界（段 190 字 + 剩余再硬切）
    t4 = "一" * 189 + "。" + "乙" * 210
    parts4 = split_long_text(t4, 200)
    assert [len(p) for p in parts4] == [190, 200, 10]
    assert _SKEL("".join(parts4)) == _SKEL(t4)
    # ASCII 词内点号不切（3.14 无边界）；定宽 200 处恰在其后
    t5 = "甲" * 100 + " 3.14 " + "乙" * 150
    parts5 = split_long_text(t5, 200)
    assert _SKEL("".join(parts5)) == _SKEL(t5)
    assert all(len(p) <= 200 for p in parts5)
    # 引号 parity：句末切点在引号内不安全 → 让位定宽；定宽 ±20 窗内找引号安全位
    t6 = "甲" * 195 + LQ + "乙。" * 5 + RQ + "丙" * 150  # LQ@195, RQ@206
    parts6 = split_long_text(t6, 200)
    assert parts6[0] == "甲" * 195  # 离定宽位最近的安全位 = 195（开引号之前）
    assert parts6[1].startswith(LQ)
    assert _SKEL("".join(parts6)) == _SKEL(t6)
    # 整条包裹在引号内（内部句末全不安全）→ 裸定宽切，骨架仍无损
    t7 = LQ + "一。" * 150 + RQ
    parts7 = split_long_text(t7, 200)
    assert _SKEL("".join(parts7)) == _SKEL(t7)
    assert all(len(p) <= 200 for p in parts7)
    # max_chars ≤ 0 钳 10（退化值无意义）
    parts8 = split_long_text("甲" * 250, 0)
    assert all(len(p) == 10 for p in parts8[:24]) and _SKEL("".join(parts8)) == _SKEL("甲" * 250)


def test_quote_parity_unit():
    par = _quote_parity(LQ + "甲" + RQ)
    assert par == [0, 1, 1, 0]
    # 开闭各 5 种引号（ASCII 引号开 == 闭，计为开——与既有协议同一保守口径）
    par2 = _quote_parity("".join(op for op, _c in [
        (chr(0x201C), 0), (chr(0x300C), 0), (chr(0x300E), 0),
        (chr(0x2018), 0), (chr(0x0022), 0)]))
    assert par2[-1] == 5
    # 闭合 → 奇偶回零
    par3 = _quote_parity(LQ + "甲" + RQ + "乙")
    assert par3[-1] == 0


def test_long_entry_indices_unit():
    entries = [
        {"speaker": "NARRATOR", "text": "短。"},
        {"speaker": "NARRATOR", "text": "甲" * 201},
        {"speaker": "林某", "text": " " * 5 + "乙" * 199 + " " * 5},  # strip 后 199 ≤ 200
        {"speaker": "NARRATOR", "text": "丙" * 200},  # 恰好 200 = 不超长
    ]
    assert long_entry_indices(entries, 200) == [1]


def test_split_long_entries_unit():
    # speaker / instruct 继承（instruct 只给首段）；输入列表不动；硬保证 ≤ 上限
    e = [
        {"speaker": "NARRATOR", "text": LONG_SRC, "instruct": "calm"},
        {"speaker": "林某", "text": "短。", "instruct": "x"},
    ]
    out, n = split_long_entries(e, 200, is_chapter_title)
    assert n == 1
    assert e[0]["text"] == LONG_SRC  # 输入列表保持原样
    assert len(out) == 3
    assert all(x["speaker"] == "NARRATOR" for x in out[:2])
    assert out[0]["instruct"] == "calm" and out[1]["instruct"] == ""
    assert all(len(x["text"].strip()) <= 200 for x in out)
    assert _SKEL(out[0]["text"] + out[1]["text"]) == _SKEL(LONG_SRC)
    assert out[2] == {"speaker": "林某", "text": "短。", "instruct": "x"}
    # 零命中 → 原列表对象 + 0
    short = [{"speaker": "NARRATOR", "text": "短。", "instruct": ""}]
    out2, n2 = split_long_entries(short, 200, is_chapter_title)
    assert out2 is short and n2 == 0


def test_split_long_entries_replaces_multiple_long_rows_without_shifting():
    first = "甲" * 450
    middle = {"speaker": "林某", "text": "中间短句。", "instruct": "middle"}
    second = "乙" * 450
    last = {"speaker": "NARRATOR", "text": "结尾短句。", "instruct": "last"}
    entries = [
        {"speaker": "NARRATOR", "text": first, "instruct": "first"},
        middle,
        {"speaker": "林某", "text": second, "instruct": "second"},
        last,
    ]

    out, split_count = split_long_entries(entries, 200, is_chapter_title)

    assert split_count == 2
    assert [entry["text"] for entry in out] == [
        "甲" * 200, "甲" * 200, "甲" * 50,
        "中间短句。",
        "乙" * 200, "乙" * 200, "乙" * 50,
        "结尾短句。",
    ]
    assert [entry["speaker"] for entry in out] == [
        "NARRATOR", "NARRATOR", "NARRATOR", "林某",
        "林某", "林某", "林某", "NARRATOR",
    ]
    assert [entry["instruct"] for entry in out] == [
        "first", "", "", "middle", "second", "", "", "last",
    ]
    first_parts = [entry["text"] for entry in out if entry["text"].startswith("甲")]
    second_parts = [entry["text"] for entry in out if entry["text"].startswith("乙")]
    assert "".join(first_parts) == first
    assert "".join(second_parts) == second
    assert entries[0]["text"] == first and entries[2]["text"] == second


def test_absorb_punct_entries_unit():
    N = lambda t: {"speaker": "NARRATOR", "text": t, "instruct": ""}
    # 并入前邻（追加到其尾）
    out, a, d = absorb_punct_entries(
        [N("夜色。"), N("……"), {"speaker": "林某", "text": "你好。", "instruct": ""}],
        is_chapter_title)
    assert (a, d) == (1, 0)
    assert [(x["speaker"], x["text"]) for x in out] == [
        ("NARRATOR", "夜色。……"), ("林某", "你好。")]
    # 无前邻 NARRATOR → 并入后邻（置于其开头）
    out, a, d = absorb_punct_entries(
        [{"speaker": "林某", "text": "你好。", "instruct": ""}, N("……"), N("夜色。")],
        is_chapter_title)
    assert (a, d) == (1, 0)
    assert out[1]["text"] == "……夜色。"
    # 无 NARRATOR 邻接 → 删除
    out, a, d = absorb_punct_entries(
        [{"speaker": "林某", "text": "你好。", "instruct": ""}, N("？"),
         {"speaker": "李四", "text": "嗯。", "instruct": ""}],
        is_chapter_title)
    assert (a, d) == (0, 1)
    assert len(out) == 2
    # 标题守卫：邻接条目是章标题 → 不吸收（此处无其他邻接 → 删除）
    out, a, d = absorb_punct_entries([N("第 5 章 风暴"), N("……")], is_chapter_title)
    assert (a, d) == (0, 1) and out[0]["text"] == "第 5 章 风暴"
    # 前邻普通 NARRATOR、后邻标题 → 吸收前邻（前邻优先）
    out, a, d = absorb_punct_entries(
        [N("夜色。"), N("……"), N("第 5 章 风暴")], is_chapter_title)
    assert (a, d) == (1, 0) and out[0]["text"] == "夜色。……"
    # 纯标点链（punct, punct, NARR）：逐跳并入、内容不丢失
    out, a, d = absorb_punct_entries([N("……"), N("？"), N("夜色。")], is_chapter_title)
    assert (a, d) == (1, 0)
    assert _SKEL("".join(x["text"] for x in out)) == _SKEL("……？夜色。")
    # 同一目标两侧吸收（punct, NARR, punct）→ 累加
    out, a, d = absorb_punct_entries([N("……"), N("夜色。"), N("？")], is_chapter_title)
    assert (a, d) == (2, 0)
    assert out == [N("……夜色。？")]
    # 零命中 → 原列表对象
    plain = [{"speaker": "林某", "text": "你好。", "instruct": ""}]
    out, a, d = absorb_punct_entries(plain, is_chapter_title)
    assert out is plain and (a, d) == (0, 0)
    # 输入列表不动
    src = [N("夜色。"), N("……")]
    absorb_punct_entries(src, is_chapter_title)
    assert src[0]["text"] == "夜色。" and src[1]["text"] == "……"


def test_long_resplit_zero_hit_leaves_log(monkeypatch):
    # 零命中：一行「0 条…（无 LLM 重切，零 LLM 调用）」日志、零调用、原列表对象
    def boom(*a, **k):
        raise AssertionError("no LLM call may happen")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    entries = [{"speaker": "NARRATOR", "text": "短文本。", "instruct": ""}]
    handle = _LogHandle()
    out, checked, fixed = long_paragraph_resplit(
        handle, _LLM, GenerationConfig(), "sys", "CTX={context}\nSOURCE TEXT:\n{chunk}",
        entries, 200)
    assert out is entries and (checked, fixed) == (0, 0)
    assert handle.logs == [("INFO", "超长段落检查：0 条超过 200 字条目（无 LLM 重切，零 LLM 调用）")]


def test_long_resplit_single_call_replaces_entry(monkeypatch):
    # 一条超长条目（213 字）→ 单次重切回复过忠实性门（恰好 1 次调用，不再投票）
    # → 整体替换为 3 条；上下文窗口带「LONGER than 200 characters」注记 + 花名册。
    entries = [
        {"speaker": "NARRATOR", "text": LONG_SRC, "instruct": "a"},
        {"speaker": "林某", "text": "我先走了。", "instruct": "c"},
    ]
    resplit = json.dumps([
        {"speaker": "NARRATOR", "text": LONG_HEAD, "instruct": "g"},
        {"speaker": "林某", "text": "我们明天再谈。", "instruct": "h"},
        {"speaker": "NARRATOR", "text": "他点了点头。", "instruct": "i"},
    ], ensure_ascii=False)
    calls = {"n": 0}
    seen_users = []

    def urlopen(req, *a, **k):
        calls["n"] += 1
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        seen_users.append(user)
        return _BodyResp(_chat_payload(resplit))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    handle = _LogHandle()
    out, checked, fixed = long_paragraph_resplit(
        handle, _LLM, GenerationConfig(check_context_window=1),
        "sys", "CTX={context}\nSOURCE TEXT:\n{chunk}", entries, 200)
    assert calls["n"] == 1  # 每条只重跑一次 LLM（single_call）
    assert (checked, fixed) == (1, 1)
    assert [(x["speaker"], x["text"]) for x in out] == [
        ("NARRATOR", LONG_HEAD), ("林某", "我们明天再谈。"),
        ("NARRATOR", "他点了点头。"), ("林某", "我先走了。")]
    assert entries[0]["text"] == LONG_SRC  # 原列表对象保持原样
    # 窗口：超长注记 + 花名册 + 后邻上下文条
    user = seen_users[0]
    assert "LONGER than 200 characters" in user
    assert "Characters in this book: 林某" in user
    assert "我先走了" in user
    assert any("条目 1（超长 212 字）" in m for _l, m in handle.logs)


def test_long_resplit_gate_fail_keeps_entry(monkeypatch):
    # 单次重切回复未过忠实性门（角色不在花名册）→ 恰好 1 次调用、保留原条目
    # （从不猜；机械分段兜底）——不再像旧协议那样跑满 4 次投票。
    entries = [
        {"speaker": "NARRATOR", "text": LONG_SRC, "instruct": "a"},
        {"speaker": "林某", "text": "我先走了。", "instruct": "c"},
    ]
    bad = json.dumps([{"speaker": "陌生人", "text": LONG_SRC}], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        if calls["n"] > 1:
            raise AssertionError(f"LLM called {calls['n']} times, expected 1")
        return _BodyResp(_chat_payload(bad))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    handle = _LogHandle()
    out, checked, fixed = long_paragraph_resplit(
        handle, _LLM, GenerationConfig(), "sys", "CTX={context}\nSOURCE TEXT:\n{chunk}",
        entries, 200)
    assert calls["n"] == 1
    assert out is entries and (checked, fixed) == (1, 0)
    assert any("单次重切未通过" in m for _l, m in handle.logs)


def test_long_resplit_cancel_propagates(monkeypatch):
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        return _BodyResp(_chat_payload("[]"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    entries = [{"speaker": "NARRATOR", "text": LONG_SRC, "instruct": "a"}]
    with pytest.raises(TaskCancelled):
        long_paragraph_resplit(
            _Handle(cancelled=True), _LLM, GenerationConfig(),
            "sys", "CTX={context}\nSOURCE TEXT:\n{chunk}", entries, 200)
    assert calls["n"] == 0  # 取消在任何调用之前上抛


def test_generate_file_long_paragraph_llm_resplit(tmp_path, monkeypatch, workspace):
    # A 段 LLM 重切路径 e2e：1 解析 + 1 单次重切（过门采纳）= 2 次调用；重切后
    # 全部 ≤ 200 字 → 机械分段 0 条（long_split=0）。
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": LONG_SRC, "instruct": "a"},
        {"speaker": "林某", "text": "我先走了。", "instruct": "c"},
    ], ensure_ascii=False)
    resplit_reply = json.dumps([
        {"speaker": "NARRATOR", "text": LONG_HEAD, "instruct": "g"},
        {"speaker": "林某", "text": "我们明天再谈。", "instruct": "h"},
        {"speaker": "NARRATOR", "text": "他点了点头。", "instruct": "i"},
    ], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        chunk = user.rsplit("SOURCE TEXT:", 1)[1].strip()
        if chunk == LONG_SRC:
            return _BodyResp(_chat_payload(resplit_reply))
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "longchap.txt"
    src.write_bytes((LONG_SRC + "\n我先走了。\n").encode("utf-8"))
    result = generate_file(
        _Handle(), str(src), _LLM, PromptsConfig(),
        GenerationConfig(chunk_size=5000, revalidate_splits=False,
                         delete_saying_tags=False, spot_check_rate=0.0,
                         check_boundary_speakers=False),
    )
    assert calls["n"] == 2  # 1 解析 + 1 单次重切
    assert result["long_checked"] == 1 and result["long_fixed"] == 1
    assert result["long_split"] == 0  # LLM 重切后已在上限内
    assert all(len(e["text"].strip()) <= 200 for e in result["entries"])
    out = json.loads((workspace / "03_parsed_json" / "longchap.json").read_text("utf-8"))
    assert [(e["speaker"], e["text"]) for e in out[:3]] == [
        ("NARRATOR", LONG_HEAD), ("林某", "我们明天再谈。"), ("NARRATOR", "他点了点头。"),
    ]
    assert out[0]["instruct"] == "g"


def test_generate_file_long_paragraph_mech_fallback(tmp_path, monkeypatch, workspace):
    # A 段机械兜底路径 e2e：单次重切回复 = 原样单条（同人独白过不了多主体门）→
    # 过门采纳（long_fixed=1）后仍 212 字 → 机械分段切开（long_split=1）；硬保证成立。
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": LONG_SRC, "instruct": "a"},
        {"speaker": "林某", "text": "我先走了。", "instruct": "c"},
    ], ensure_ascii=False)
    resplit_reply = json.dumps([
        {"speaker": "NARRATOR", "text": LONG_SRC, "instruct": "a"},
    ], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        chunk = user.rsplit("SOURCE TEXT:", 1)[1].strip()
        if chunk == LONG_SRC:
            return _BodyResp(_chat_payload(resplit_reply))
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "longchap.txt"
    src.write_bytes((LONG_SRC + "\n我先走了。\n").encode("utf-8"))
    result = generate_file(
        _Handle(), str(src), _LLM, PromptsConfig(),
        GenerationConfig(chunk_size=5000, revalidate_splits=False,
                         delete_saying_tags=False, spot_check_rate=0.0,
                         check_boundary_speakers=False),
    )
    assert calls["n"] == 2  # 1 解析 + 1 单次重切
    assert result["long_checked"] == 1 and result["long_fixed"] == 1
    assert result["long_split"] == 1
    assert result["count"] == 3  # 2 段切分 + 1 台词条
    out = json.loads((workspace / "03_parsed_json" / "longchap.json").read_text("utf-8"))
    assert all(len(e["text"].strip()) <= 200 for e in out)  # 硬保证
    assert _SKEL(out[0]["text"] + out[1]["text"]) == _SKEL(LONG_SRC)
    assert out[0]["speaker"] == "NARRATOR" and out[1]["speaker"] == "NARRATOR"
    assert out[0]["instruct"] == "a" and out[1]["instruct"] == ""
    assert out[2] == {"speaker": "林某", "text": "我先走了。", "instruct": "c"}
    # 同人合并在机械分段**之前**运行：合并时看到的是切分前的 [NARRATOR 长条, 林某 台词]
    # （无连续同 speaker 对），切段是末段产物、合并看不到 → 不会回粘（硬保证由末段守住）
    assert result["merged_same_speaker"] == 0


def test_generate_file_merge_before_split_forced_over200(tmp_path, monkeypatch, workspace):
    # 顺序钉死「合并在切段**前**」：≤10 强制合并（5 ≤ 10；总字数 215 > 100，≤100
    # 条件不满足、只有强制路径能并）把 210 字段并成 216 字同人块（>200），末段机械
    # 分段切回 ≤200——若合并在切段之后，会把切段粘回 216 直接破硬保证。
    long_a = "甲" * 210   # 210 词字符（>200）
    short_b = "乙" * 5     # 5 词字符（≤10 → 触发强制合并）
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": long_a, "instruct": "a"},
        {"speaker": "NARRATOR", "text": short_b, "instruct": "b"},
    ], ensure_ascii=False)
    # 重切回复 = 原样单条（同人独白过不了多主体门）→ 单次重切采纳后仍超长 → 交给机械分段
    resplit_reply = json.dumps([
        {"speaker": "NARRATOR", "text": long_a, "instruct": "a"},
    ], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        user = json.loads(req.data.decode("utf-8"))["messages"][1]["content"]
        chunk = user.rsplit("SOURCE TEXT:", 1)[1].strip()
        if chunk == long_a:
            return _BodyResp(_chat_payload(resplit_reply))
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "forced.txt"
    src.write_bytes((long_a + "\n" + short_b + "\n").encode("utf-8"))
    result = generate_file(
        _Handle(), str(src), _LLM, PromptsConfig(),
        GenerationConfig(chunk_size=5000, revalidate_splits=False,
                         delete_saying_tags=False, spot_check_rate=0.0,
                         check_boundary_speakers=False),
    )
    assert calls["n"] == 2  # 1 解析 + 1 单次重切（采纳原样）
    assert result["long_checked"] == 1 and result["long_fixed"] == 1
    assert result["merged_same_speaker"] == 1  # 仅 ≤10 强制路径（215 > 100）
    assert result["long_split"] == 1  # 合并造出的 >200 块由末段切回
    assert result["count"] == 2  # 216 字 → 定宽切 [200, 16]
    out = json.loads((workspace / "03_parsed_json" / "forced.json").read_text("utf-8"))
    assert all(len(e["text"].strip()) <= 200 for e in out)  # 硬保证
    assert _SKEL("".join(e["text"] for e in out)) == _SKEL(long_a + short_b)  # 骨架无损
    assert all(e["speaker"] == "NARRATOR" for e in out)
    assert out[0]["instruct"] == "a" and out[1]["instruct"] == ""


def test_generate_file_long_paragraph_llm_off_still_mechanically_splits(tmp_path, monkeypatch, workspace):
    # check_long_paragraphs=False → 跳过 LLM 语义重切（零重切调用、long_checked/fixed
    # 为 0），但机械分段兜底**恒执行**：超长条目仍被切开，最终无超过硬上界的条目。
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": LONG_SRC, "instruct": "a"},
    ], ensure_ascii=False)
    calls = {"n": 0}

    def urlopen(req, *a, **k):
        calls["n"] += 1
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "longchap.txt"
    src.write_bytes(LONG_SRC.encode("utf-8"))
    handle = _LogHandle()
    result = generate_file(
        handle, str(src), _LLM, PromptsConfig(),
        GenerationConfig(chunk_size=5000, revalidate_splits=False,
                         delete_saying_tags=False, spot_check_rate=0.0,
                         check_boundary_speakers=False, check_long_paragraphs=False),
    )
    assert calls["n"] == 1  # 只有解析（LLM 重切零调用）
    assert result["long_checked"] == 0 and result["long_fixed"] == 0
    assert result["long_split"] == 1  # 机械分段兜底恒执行：开关关也切回
    assert result["count"] >= 2  # 212 字 → 至少切 2 段
    out = json.loads((workspace / "03_parsed_json" / "longchap.json").read_text("utf-8"))
    assert all(len(e["text"].strip()) <= 200 for e in out)  # 硬保证
    assert _SKEL("".join(e["text"] for e in out)) == _SKEL(LONG_SRC)  # 骨架无损
    assert all(e["speaker"] == "NARRATOR" for e in out)
    assert any("超长段落检查已关闭（配置）" in m for _l, m in handle.logs)
    assert any("超长段落机械分段" in m for _l, m in handle.logs)


def test_generate_file_absorb_punct_e2e(tmp_path, monkeypatch, workspace):
    # B 段 e2e：解析产物含独立「……」NARRATOR 条 → 并入前邻（punct_absorbed=1）。
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷。", "instruct": "a"},
        {"speaker": "NARRATOR", "text": "……", "instruct": "b"},
        {"speaker": "林某", "text": "你好。", "instruct": "c"},
    ], ensure_ascii=False)

    def urlopen(req, *a, **k):
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "punct.txt"
    src.write_bytes("夜色像潮水一样漫进街巷。\n……\n林某：你好。\n".encode("utf-8"))
    result = generate_file(
        _Handle(), str(src), _LLM, PromptsConfig(),
        GenerationConfig(revalidate_splits=False, delete_saying_tags=False,
                         spot_check_rate=0.0, check_boundary_speakers=False,
                         check_long_paragraphs=False),
    )
    assert result["punct_absorbed"] == 1 and result["punct_deleted"] == 0
    out = json.loads((workspace / "03_parsed_json" / "punct.json").read_text("utf-8"))
    assert out[0]["text"] == "夜色像潮水一样漫进街巷。……"
    assert result["count"] == 2


def test_generate_file_absorb_punct_off_keeps_entry(tmp_path, monkeypatch, workspace):
    # absorb_punct_entries=False → 纯标点条保留（此处无 NARRATOR 邻接可合并）；
    # 对照：开关开时 = 删除（punct_deleted=1）。
    parse_reply = json.dumps([
        {"speaker": "林某", "text": "你好。", "instruct": "a"},
        {"speaker": "NARRATOR", "text": "……", "instruct": "b"},
        {"speaker": "李四", "text": "嗯。", "instruct": "c"},
    ], ensure_ascii=False)

    def urlopen(req, *a, **k):
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "punctoff.txt"
    src.write_bytes("林某：你好。\n……\n李四：嗯。\n".encode("utf-8"))
    result = generate_file(
        _Handle(), str(src), _LLM, PromptsConfig(),
        GenerationConfig(revalidate_splits=False, delete_saying_tags=False,
                         spot_check_rate=0.0, check_boundary_speakers=False,
                         check_long_paragraphs=False, absorb_punct_entries=False),
    )
    assert result["punct_absorbed"] == 0 and result["punct_deleted"] == 0
    out = json.loads((workspace / "03_parsed_json" / "punctoff.json").read_text("utf-8"))
    assert [e["text"] for e in out] == ["你好。", "……", "嗯。"]

    # 对照（开关开）：无 NARRATOR 邻接 → 删除
    result2 = generate_file(
        _Handle(), str(src), _LLM, PromptsConfig(),
        GenerationConfig(revalidate_splits=False, delete_saying_tags=False,
                         spot_check_rate=0.0, check_boundary_speakers=False,
                         check_long_paragraphs=False),
    )
    assert result2["punct_deleted"] == 1
    out2 = json.loads((workspace / "03_parsed_json" / "punctoff.json").read_text("utf-8"))
    assert [e["text"] for e in out2] == ["你好。", "嗯。"]


# --------------------------------------------------------------------------- #
# generate_file 槽位作用域（预备阶段不占槽；chunk 循环一结束即 release；排队可即时取消）
# --------------------------------------------------------------------------- #

class _GateSpy:
    """Wraps the real shared gate and records acquire / release in event order."""

    def __init__(self, real, events):
        self._real = real
        self._events = events
        self.acquires = 0
        self.releases = 0

    def acquire(self, stop_check=None):
        self.acquires += 1
        self._events.append(("acquire", None))
        return self._real.acquire(stop_check=stop_check)

    def release(self):
        self.releases += 1
        self._events.append(("release", None))
        self._real.release()

    @property
    def active(self):
        return self._real.active


class _TraceHandle(_LogHandle):
    """A ``_LogHandle`` that also records progress / phase into a shared event list."""

    def __init__(self, events):
        super().__init__()
        self._events = events

    def progress(self, frac, current=""):
        self._events.append(("progress", current))

    def phase(self, name):
        self._events.append(("phase", name))


def test_generate_file_slot_scope(tmp_path, monkeypatch, workspace):
    # 槽位只覆盖 LLM 分段解析阶段：预备（读文件 / 分 chunk）在 acquire 之前；release 恰好
    # 发生在检查阶段入口日志之前；acquire / release 各恰一次且配平。
    events: list = []
    real_gate = concurrency.gate()
    spy = _GateSpy(real_gate, events)
    monkeypatch.setattr("backend.engines.script.gate", lambda: spy)

    source = "夜色像潮水一样漫进街巷。"
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": source, "instruct": "a"},
    ], ensure_ascii=False)

    def urlopen(req, *a, **k):
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "chapter.txt"
    src.write_bytes(source.encode("utf-8"))

    result = generate_file(
        _TraceHandle(events), str(src), _LLM, PromptsConfig(),
        GenerationConfig(revalidate_splits=False, delete_saying_tags=False,
                         spot_check_rate=0.0),
    )
    assert result["count"] == 1

    def pos(kind, value):
        return events.index((kind, value))

    # 各恰一次：acquire 一次（返回 True 才进 try）、release 一次（成功路径在检查前释放）。
    assert spy.acquires == 1 and spy.releases == 1
    assert events.count(("acquire", None)) == 1 and events.count(("release", None)) == 1
    # 「排队中」文案在取槽前；parse 阶段标记在取槽后。
    assert pos("progress", "排队中（等待并发槽位）") < pos("acquire", None)
    assert pos("phase", "parse") > pos("acquire", None)
    # release 先于检查阶段入口（phase("check") 标记 + progress 0.9 文案）——预取补位点
    # （引擎先发 phase 后发 progress，两者都在 release 之后；相对次序无功能意义）。
    # 源为单段 → 角色匹配检查无内部边界、静默零调用，不产生额外事件。
    check_mark = pos(
        "progress",
        "机械检查（角色匹配检查 / 断句校验 / 标签删除 / 超长段落检查 / 归属抽样 / 纯标点吸收）",
    )
    assert pos("release", None) < pos("phase", "check")
    assert pos("release", None) < check_mark
    # 100% 只在真正完成时出现，且是最后一条事件。
    assert pos("progress", "完成") == len(events) - 1
    assert real_gate.active == 0  # 槽位配平


def test_generate_file_cancel_while_queued(tmp_path, monkeypatch, workspace):
    # C=1 且主线程持有唯一槽 → 任务停在 gate().acquire(stop_check=…) 排队；取消 → 一个
    # 0.2s stop_check 轮询内弃位退出：CANCELLED、从未取槽、无产出（取消 ≠ 失败）。
    source = "夜色像潮水一样漫进街巷。"
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": source, "instruct": "a"},
    ], ensure_ascii=False)

    def urlopen(req, *a, **k):
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "chapter.txt"
    src.write_bytes(source.encode("utf-8"))

    g = concurrency.gate()
    try:
        g.acquire()  # 主线程持有唯一槽位
        mgr = TaskManager()
        task = mgr.create(
            "script", "文本解析（chapter.txt）",
            generate_file, str(src), _LLM, PromptsConfig(),
            GenerationConfig(revalidate_splits=False, delete_saying_tags=False,
                             spot_check_rate=0.0),
        )
        # 等任务真正进入等槽队列（「排队中」进度文案是队列标记，勿改引擎文案）。
        deadline = time.time() + 5
        while time.time() < deadline and "排队" not in (task.current or ""):
            time.sleep(0.02)
        assert "排队" in (task.current or ""), "task never reached the slot queue"
        assert g.active == 1  # 只有主线程持槽；任务仍在排队

        t0 = time.time()
        mgr.control(task.id, "cancel")
        while task.status not in TERMINAL and time.time() - t0 < 2.0:
            time.sleep(0.02)
        assert task.status is TaskStatus.CANCELLED
        assert time.time() - t0 < 1.0  # stop_check 0.2s 轮询内弃位，不是等满超时
        assert task.error == ""  # 取消不是失败
        assert g.active == 1  # 排队中的任务从未取走槽位
    finally:
        g.release()
    assert not (workspace / "03_parsed_json" / "chapter.json").exists()


def test_generate_file_cancel_mid_chunk_releases_once(tmp_path, monkeypatch, workspace):
    # 取消落在第 1 段的 LLM 调用进行期间 → 调用返回后在下一段循环头的 check() 上抛
    # TaskCancelled：acquire 恰 1 次 / release 恰 1 次（finally 配平路径，非成功路径的
    # 提前 release），CANCELLED、槽位归还、无产出。
    events: list = []
    real_gate = concurrency.gate()
    spy = _GateSpy(real_gate, events)
    monkeypatch.setattr("backend.engines.script.gate", lambda: spy)

    # 两个短段 + 小 chunk_size → 恰 2 段（段数公式给 4，被合法边界数钳回 2；
    # 第 2 段的循环头 check() 承接取消）。
    source = "夜色像潮水一样漫进街巷，行人渐稀。\n\n巷口的灯一盏盏亮起来。"
    parse_reply = json.dumps([
        {"speaker": "NARRATOR", "text": "夜色像潮水一样漫进街巷，行人渐稀。", "instruct": "a"},
    ], ensure_ascii=False)
    calls = {"n": 0}
    hold = threading.Event()

    def urlopen(req, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            hold.wait(20)  # 停在第 1 段的 LLM 调用内——取消在此刻落下
        return _BodyResp(_chat_payload(parse_reply))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    (workspace / "02_split_text").mkdir(parents=True)
    src = workspace / "02_split_text" / "chapter.txt"
    src.write_bytes(source.encode("utf-8"))

    try:
        mgr = TaskManager()
        task = mgr.create(
            "script", "文本解析（chapter.txt）",
            generate_file, str(src), _LLM, PromptsConfig(),
            GenerationConfig(chunk_size=8, revalidate_splits=False,
                             delete_saying_tags=False, spot_check_rate=0.0),
        )
        deadline = time.time() + 5
        while time.time() < deadline and calls["n"] < 1:
            time.sleep(0.02)
        assert calls["n"] >= 1  # 任务已取槽并发出第 1 段解析
        assert real_gate.active == 1

        mgr.control(task.id, "cancel")
        hold.set()  # 放行在途调用；下一段循环头的 check() 上抛 TaskCancelled

        deadline = time.time() + 5
        while time.time() < deadline and task.status not in TERMINAL:
            time.sleep(0.02)
        assert task.status is TaskStatus.CANCELLED
        assert task.error == ""
        assert spy.acquires == 1 and spy.releases == 1  # finally 配平：恰一次 release
        assert real_gate.active == 0
    finally:
        hold.set()
    assert not (workspace / "03_parsed_json" / "chapter.json").exists()
