"""Q15/S5: the two LLM transport verbs must share ONE business body.

Plan acceptance: 「LLM 两路径共同业务参数 diff 为空」 — ``stream`` /
``stream_options`` are exempt (the legitimate per-verb difference); the
``extra_body`` asymmetry is retained as-is (non-streaming only — see the
arbitration note in ``request_chat_completion_stream``).
"""
from __future__ import annotations

import inspect
import io
import json
import urllib.error
import urllib.request

import pytest

from backend.engines.llm_transport import (
    LLMHTTPError,
    build_chat_body,
    llm_server_is_alive,
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


# --------------------------------------------------------------------------- #
# 流式动词的 HTTP 错误契约：非 5xx 必须抛 LLMHTTPError（与非流式一致），
# 否则 404 model_not_found 会被上层当成"瞬时 chunk 失败"吞掉重试。
# --------------------------------------------------------------------------- #
def test_stream_verb_http_404_raises_llmhttperror(monkeypatch):
    def urlopen(req, *a, **k):
        raise urllib.error.HTTPError(
            req.full_url, 404, "model_not_found", {}, io.BytesIO(b"model_not_found"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(LLMHTTPError) as ei:
        request_chat_completion_stream(
            "http://x/v1", "key", "model",
            [{"role": "user", "content": "hi"}],
            temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
            handle=None,
        )
    assert ei.value.status == 404
    assert "model_not_found" in str(ei.value)


def test_stream_verb_http_500_stays_unavailable(monkeypatch):
    def urlopen(req, *a, **k):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b"busy"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    from backend.engines.llm_transport import LLMUnavailableError
    with pytest.raises(LLMUnavailableError):
        request_chat_completion_stream(
            "http://x/v1", "key", "model",
            [{"role": "user", "content": "hi"}],
            temperature=0.6, top_p=0.8, presence_penalty=0.0, max_tokens=100,
            handle=None,
        )


# --------------------------------------------------------------------------- #
# 恢复探针：带 model_name 时，服务活着还不够——/models 列表里必须真有该模型，
# 否则"重启后模型没加载完"的服务器会被误判为已恢复（事故根因之一）。
# --------------------------------------------------------------------------- #
class _ModelsResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_is_alive_model_name_requires_model_in_listing(monkeypatch):
    body = json.dumps({"data": [{"id": "other-model"}, "qwen3-27b"]}).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _ModelsResp(body))
    assert llm_server_is_alive("http://x/v1", "key", model_name="qwen3-27b") is True
    assert llm_server_is_alive("http://x/v1", "key", model_name="not-loaded-yet") is False


def test_is_alive_model_name_strict_on_bad_or_erroring_responses(monkeypatch):
    # 解析不出 /models 列表 = 无法证明模型已加载。
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _ModelsResp(b"not json"))
    assert llm_server_is_alive("http://x/v1", model_name="m") is False

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _ModelsResp(b'{"no_data": []}'))
    assert llm_server_is_alive("http://x/v1", model_name="m") is False

    # 任何 HTTP 错误（含 4xx）都不能算恢复；连接失败同样。
    def http_401(req, *a, **k):
        raise urllib.error.HTTPError("http://x/v1/models", 401, "auth", {}, io.BytesIO(b""))

    def conn_refused(req, *a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", http_401)
    assert llm_server_is_alive("http://x/v1", model_name="m") is False
    monkeypatch.setattr(urllib.request, "urlopen", conn_refused)
    assert llm_server_is_alive("http://x/v1", model_name="m") is False


def test_is_alive_without_model_name_keeps_legacy_semantics(monkeypatch):
    # 不带 model_name：任何非 5xx 响应都算活着（401 也算——语义未变，
    # 恢复探针之外的调用方不受影响）。
    def http_401(req, *a, **k):
        raise urllib.error.HTTPError("http://x/v1/models", 401, "auth", {}, io.BytesIO(b""))

    def http_500(req, *a, **k):
        raise urllib.error.HTTPError("http://x/v1/models", 500, "boom", {}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", http_401)
    assert llm_server_is_alive("http://x/v1") is True
    monkeypatch.setattr(urllib.request, "urlopen", http_500)
    assert llm_server_is_alive("http://x/v1") is False
