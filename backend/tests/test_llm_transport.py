"""Q15/S5: the two LLM transport verbs must share ONE business body.

Plan acceptance: 「LLM 两路径共同业务参数 diff 为空」 — ``stream`` /
``stream_options`` are exempt (the legitimate per-verb difference); the
``extra_body`` asymmetry is retained as-is (non-streaming only — see the
arbitration note in ``request_chat_completion_stream``).
"""
from __future__ import annotations

import inspect
import json
import urllib.request

from backend.engines.llm_transport import (
    build_chat_body,
    request_chat_completion,
    request_chat_completion_stream,
)

COMMON = dict(
    model="m",
    messages=[{"role": "user", "content": "hi"}],
    temperature=0.6,
    top_p=0.8,
    presence_penalty=0.2,
    max_tokens=128,
    top_k=4,
    min_p=0.1,
    banned_tokens=[7, 9],
)

EXEMPT = {"stream", "stream_options"}


def test_build_chat_body_stream_adds_only_stream_pair():
    plain = build_chat_body(**COMMON)
    streamed = build_chat_body(stream=True, **COMMON)
    assert streamed == {**plain, "stream": True, "stream_options": {"include_usage": True}}


def test_build_chat_body_omits_zero_optionals():
    body = build_chat_body(
        model="m", messages=[], temperature=0.0, top_p=1.0,
        presence_penalty=0.0, max_tokens=10,
        top_k=0, min_p=0, banned_tokens=None)
    assert set(body) == {
        "model", "messages", "temperature", "top_p", "presence_penalty", "max_tokens"}


def test_build_chat_body_merges_extra_body_top_level():
    body = build_chat_body(
        model="m", messages=[], temperature=0.0, top_p=1.0,
        presence_penalty=0.0, max_tokens=10,
        extra_body={"enable_thinking": False})
    assert body["enable_thinking"] is False


class _NonStreamResp:
    def read(self):
        return json.dumps(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        ).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StreamResp:
    def __init__(self):
        self._lines = iter([
            b'data: {"choices": [{"delta": {"content": "ok"}}]}\n',
            b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n',
            b"data: [DONE]\n",
        ])

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_two_paths_share_business_body(monkeypatch):
    """Both verbs, identical inputs → bodies identical except the stream pair."""
    captured: dict = {}

    def fake_nonstream(req, timeout=None):
        captured.setdefault("non", []).append(json.loads(req.data))
        return _NonStreamResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_nonstream)
    request_chat_completion("http://x/v1", "key", **COMMON)

    def fake_stream(req, timeout=None):
        captured["stream"] = json.loads(req.data)
        return _StreamResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_stream)
    request_chat_completion_stream("http://x/v1", "key", handle=None, **COMMON)

    non = captured["non"][0]
    stream = captured["stream"]
    # 共同业务参数 diff 为空（stream / stream_options 豁免）。
    assert {k: v for k, v in non.items() if k not in EXEMPT} == \
           {k: v for k, v in stream.items() if k not in EXEMPT}
    assert stream["stream"] is True
    assert stream["stream_options"] == {"include_usage": True}
    assert "stream" not in non and "stream_options" not in non


def test_extra_body_asymmetry_is_documented_and_stable():
    """Q15 裁决：extra_body 透传保留在非流式一侧（含 400/422 单次回退）；
    流式路径不新增透传（属行为变化，本轮不做）。"""
    assert "extra_body" in inspect.signature(request_chat_completion).parameters
    assert "extra_body" not in inspect.signature(request_chat_completion_stream).parameters
