"""OpenAI-compatible chat completion transport shared by business engines."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

HTTP_TIMEOUT = 300


class LLMHTTPError(RuntimeError):
    """Transport-level HTTP failure from :func:`request_chat_completion`.

    The message keeps the historical ``"LLM HTTP {code}: {detail}"`` wording;
    ``status`` carries the HTTP code so the caller can tell a strict gateway's
    400/422 (unknown-parameter rejection) from any other failure.
    """

    def __init__(self, status: int, detail: str):
        self.status = status
        super().__init__(f"LLM HTTP {status}: {detail}")


def request_chat_completion(base_url, api_key, model, messages,
                            temperature, top_p, presence_penalty, max_tokens,
                            top_k=0, min_p=0, banned_tokens=None,
                            extra_body: dict | None = None):
    """Issue an OpenAI-compatible ``chat/completions`` POST with stdlib ``urllib``.

    Sends the same body/headers the ``openai`` SDK would for
    ``client.chat.completions.create(...)``: ``Authorization: Bearer <api_key>``, a JSON
    body of ``model`` / ``messages`` / sampling params, plus any non-zero ``extra_body``
    keys (``top_k`` / ``min_p`` / ``banned_tokens``) merged at the top level. Returns
    ``(content, finish_reason, usage)`` where ``usage`` is a dict
    ``{"prompt_tokens", "completion_tokens"}`` (or ``None``). Raises on HTTP / network /
    parse failure — the caller's retry loop handles it.

    ``extra_body`` = additional top-level body keys (openai-SDK ``extra_body`` parity),
    e.g. ``{"enable_thinking": False}`` for thinking-model servers (LM Studio / vLLM /
    Ollama) so the model's reasoning trace does not consume the ``max_tokens`` budget
    that the actual answer needs. If the server rejects the request with HTTP 400/422
    while extra keys are in flight, the call is transparently re-issued ONCE without
    the extra keys — strict gateways (e.g. the real OpenAI API) reject unknown
    parameters, and the plain request still works there.
    """
    from ..platform.quota import require_quota
    require_quota("LLM", "llm.operation")
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "presence_penalty": presence_penalty,
        "max_tokens": max_tokens,
    }
    # openai ``extra_body`` keys go at the top level, only when set (non-zero).
    if top_k:
        body["top_k"] = top_k
    if min_p:
        body["min_p"] = min_p
    if banned_tokens:
        body["banned_tokens"] = banned_tokens
    if extra_body:
        for k, v in extra_body.items():
            body[k] = v

    def _post(b):
        data = json.dumps(b, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            # Surface the server's message (rate limit, auth, model-not-found, ...) in the log.
            detail = e.read().decode("utf-8", "replace")[:300]
            raise LLMHTTPError(e.code, detail) from e

    try:
        payload = _post(body)
    except LLMHTTPError as e:
        # Strict gateway rejected the extra keys → transparent one-shot retry
        # WITHOUT them. No extra keys → nothing to fall back to.
        if not extra_body or e.status not in (400, 422):
            raise
        payload = _post({k: v for k, v in body.items() if k not in extra_body})

    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("LLM 响应缺少 choices。")
    first = choices[0]
    message = first.get("message") or {}
    content = (message.get("content") or "").strip()
    finish_reason = first.get("finish_reason")
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = None
    return content, finish_reason, usage
