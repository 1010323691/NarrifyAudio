"""OpenAI-compatible chat transport shared by business engines.

Single transport layer (plan Q15/S5 convergence): one business-body builder
(:func:`build_chat_body`), two verbs — non-streaming
(:func:`request_chat_completion`) and streaming
(:func:`request_chat_completion_stream`) — plus the shared "one JSON object
back" retry kit (:func:`llm_json_with_retry` and friends). The verbs' bodies
differ ONLY by the legitimate ``stream``/``stream_options`` pair; the common
business parameters (model, messages, the six sampling params, the optional
top_k/min_p/banned_tokens, extra_body) are built exactly once, here, so the
two paths cannot drift — tests/test_llm_transport.py diff-covers that.
"""
from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.request

from ..core.task_control import TaskCancelled

HTTP_TIMEOUT = 300

log = logging.getLogger("audiobook.llm")


class LLMHTTPError(RuntimeError):
    """Transport-level HTTP failure from :func:`request_chat_completion`.

    The message keeps the historical ``"LLM HTTP {code}: {detail}"`` wording;
    ``status`` carries the HTTP code so the caller can tell a strict gateway's
    400/422 (unknown-parameter rejection) from any other failure.
    """

    def __init__(self, status: int, detail: str):
        self.status = status
        super().__init__(f"LLM HTTP {status}: {detail}")


class LLMUnavailableError(RuntimeError):
    """The configured LLM endpoint cannot currently accept requests."""


def llm_server_is_alive(
    base_url: str, api_key: str = "", *, timeout: float = 5.0, model_name: str | None = None,
) -> bool:
    """Probe an OpenAI-compatible endpoint; any non-5xx HTTP response proves liveness.

    With ``model_name`` set the server must additionally list that model in its
    ``/models`` payload: a freshly restarted server that has not loaded the
    configured model yet answers ``/models`` fine, but redispatching against it
    would only re-fail the attempt with ``model_not_found``.
    """
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            if not model_name:
                return True
            try:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            except json.JSONDecodeError:
                return False
            models = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                return False
            return any(item == model_name or (isinstance(item, dict) and item.get("id") == model_name)
                       for item in models)
    except urllib.error.HTTPError as exc:
        # With a required model we cannot verify it on the error path — treat
        # any HTTP error (4xx included) as "not recovered".
        if model_name:
            return False
        return exc.code < 500
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError):
        return False


class LLMModelsFetchError(RuntimeError):
    """The configured endpoint could not be listed (admin 「拉取模型」surface)."""


def list_llm_models(base_url: str, api_key: str = "", *, timeout: float = 10.0) -> list[str]:
    """Return the model names an OpenAI-compatible endpoint lists at ``/models``.

    Deduplicated, server order preserved. Entries may be bare strings or
    ``{"id": ...}`` objects (same payload shapes ``llm_server_is_alive``
    accepts). Raises :class:`LLMModelsFetchError` with a user-facing Chinese
    message on HTTP / network failure or when no parseable ``data`` list
    comes back.
    """
    url = base_url.rstrip("/") + "/models"
    try:
        # Request construction can reject malformed URLs (bad port, spaces,
        # schemeless hosts) before any socket is touched — keep that inside the
        # error contract too, so callers always see LLMModelsFetchError.
        request = urllib.request.Request(
            url, method="GET",
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise LLMModelsFetchError(f"LLM 服务返回 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise LLMModelsFetchError(f"LLM 服务连接失败：{exc}") from exc
    except (http.client.InvalidURL, ValueError) as exc:
        raise LLMModelsFetchError(f"LLM 服务地址无效：{exc}") from exc
    except http.client.HTTPException as exc:
        # 其他协议级异常（如非 HTTP TCP 服务的 BadStatusLine）。InvalidURL 是
        # HTTPException 子类，须由上面的子句先捕获。
        raise LLMModelsFetchError(f"LLM 服务响应协议异常：{exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMModelsFetchError("LLM 服务响应不是有效 JSON") from exc
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise LLMModelsFetchError("LLM 服务响应中没有模型列表（data）")
    names: list[str] = []
    for item in models:
        name = item if isinstance(item, str) else (item.get("id") if isinstance(item, dict) else None)
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def build_chat_body(model, messages, temperature, top_p, presence_penalty,
                    max_tokens, top_k=0, min_p=0, banned_tokens=None,
                    *, stream: bool = False, extra_body: dict | None = None) -> dict:
    """The single source of the ``chat/completions`` business body (Q15).

    Non-streaming and streaming requests must differ ONLY by the
    ``stream``/``stream_options`` pair: every other key (model, messages, the
    six sampling params, the optional ``top_k``/``min_p``/``banned_tokens``,
    and any non-empty ``extra_body`` top-level keys) is built here, once, so
    the two verbs cannot drift.
    """
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "presence_penalty": presence_penalty,
        "max_tokens": max_tokens,
    }
    if stream:
        body["stream"] = True
        # Ask OpenAI-compatible servers to include usage in the final frame; harmless
        # for servers that ignore it (usage then stays None -> tokens logged as "?").
        body["stream_options"] = {"include_usage": True}
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
    return body


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

    ``extra_body`` = additional top-level body keys (openai-SDK ``extra_body``
    parity), e.g. ``{"enable_thinking": False}`` for thinking-model servers (LM Studio / vLLM /
    Ollama) so the model's reasoning trace does not consume the ``max_tokens`` budget
    that the actual answer needs. If the server rejects the request with HTTP 400/422
    while extra keys are in flight, the call is transparently re-issued ONCE without
    the extra keys — strict gateways (e.g. the real OpenAI API) reject unknown
    parameters, and the plain request still works there.
    """
    from ..platform.quota import require_quota
    require_quota("LLM", "llm.operation")
    url = base_url.rstrip("/") + "/chat/completions"
    body = build_chat_body(
        model, messages, temperature, top_p, presence_penalty, max_tokens,
        top_k=top_k, min_p=min_p, banned_tokens=banned_tokens, extra_body=extra_body)

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
            if 500 <= e.code < 600:
                raise LLMUnavailableError(f"LLM 服务暂不可用（HTTP {e.code}）：{detail}") from e
            raise LLMHTTPError(e.code, detail) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise LLMUnavailableError(f"LLM 服务连接失败：{e}") from e

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


def request_chat_completion_stream(base_url, api_key, model, messages,
                                   temperature, top_p, presence_penalty, max_tokens,
                                   top_k=0, min_p=0, banned_tokens=None, handle=None):
    """Streaming twin of :func:`request_chat_completion`; the body comes from the
    same :func:`build_chat_body` call (``stream=True`` adds only the legitimate
    ``stream``/``stream_options`` pair).

    Issues the OpenAI-compatible ``chat/completions`` POST with ``stream: true``
    and reads the SSE delta frames as the model generates. Each coalesced slice of the
    raw stream is forwarded to the UI via ``handle.llm_chunk(...)`` so the 文本解析
    「流式反馈」 panel fills in real time — display only, never read back by the parse
    pipeline. For reasoning models the stream carries ``delta.reasoning_content``
    (the model's live "thinking") before any ``delta.content``; the panel shows both,
    but the returned content is the ``delta.content`` answer only — byte-identical to
    the non-streaming response (same body / sampling params; deltas concatenated and
    stripped) — so the caller's downstream JSON clean / repair / salvage is unaffected.

    Returns the same ``(content, finish_reason, usage)`` triple as the non-streaming
    call. Raises on HTTP / network / parse failure — the caller's retry loop handles it.
    5xx and connection failures raise ``LLMUnavailableError``; other HTTP errors raise
    ``LLMHTTPError`` (same contract as the non-streaming verb) so callers can fast-fail
    a configuration problem (e.g. ``model_not_found``) instead of retrying it as a
    transient chunk failure.
    ``handle`` may be ``None`` (no UI forwarding / no mid-stream cancel) for tests.

    No ``extra_body`` pass-through, deliberately (Q15 arbitration): the non-streaming
    verb forwards ``extra_body`` (e.g. ``{"enable_thinking": False}`` on the small-JSON
    BGM/music surfaces) with a one-shot 400/422 fallback; adding the same pass-through
    here would change the main parse's request and was not approved for this batch.
    """
    from ..platform.quota import require_quota
    require_quota("LLM", "llm.operation")
    url = base_url.rstrip("/") + "/chat/completions"
    body = build_chat_body(
        model, messages, temperature, top_p, presence_penalty, max_tokens,
        top_k=top_k, min_p=min_p, banned_tokens=banned_tokens, stream=True)

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    # Coalescing: buffer raw deltas and flush on a throttle so the per-task SSE queue
    # (queue.Queue(maxsize=500), which silently drops when full) never chokes on
    # token-rate events. A ~120 ms / ~256-char cadence reads as live to the panel.
    #
    # Two buffers, one cadence — a reasoning model (e.g. Qwen3 "thinking") streams its
    # working in ``delta.reasoning_content`` long before any ``delta.content``:
    #   * ``pending``     = the raw display stream (reasoning + content, in arrival
    #                       order) -> the llm_chunk panel shows the model "thinking".
    #   * ``content_buf`` = the answer only (delta.content) -> returned to the caller so
    #                       the downstream JSON parser sees exactly the non-streaming text.
    FLUSH_INTERVAL = 0.12  # seconds between UI flushes
    FLUSH_SIZE = 256  # chars — flush early if a burst arrives
    pending: list[str] = []
    content_buf: list[str] = []
    emitted: list[str] = []  # accumulated answer (content-only) = the return value
    finish_reason = None
    usage = None
    last_flush = time.monotonic()
    # Real-time generation rate (chars/s) for the 文本解析 吞吐量 / per-window gauge —
    # measured from the actual streamed text (content + reasoning) as it arrives, never
    # estimated. ``usage`` is still read per frame only for the per-chunk token log line
    # and the return value; the rate itself uses the real chars, so it stays live on any
    # endpoint (token counts are only reported by some servers, usually just in the final
    # frame — and not at all by others).
    cps = 0.0

    def flush(force: bool = False) -> None:
        nonlocal last_flush, cps
        if not pending:
            return
        chars = sum(len(p) for p in pending)
        if not (force
                or time.monotonic() - last_flush >= FLUSH_INTERVAL
                or chars >= FLUSH_SIZE):
            return
        now = time.monotonic()
        dt = now - last_flush
        if dt > 1e-3:  # chars/s over the window since the previous flush (skip ~0 window)
            cps = chars / dt
        last_flush = now
        content_slice = "".join(content_buf)
        if content_slice:
            emitted.append(content_slice)
        if handle is not None:
            handle.llm_chunk("".join(pending))
            handle.llm_rate(chars, cps)
        content_buf.clear()
        pending.clear()

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload_text = line[5:].strip()
                if payload_text == "[DONE]":
                    break
                try:
                    payload = json.loads(payload_text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                frame_usage = payload.get("usage")
                if isinstance(frame_usage, dict):
                    usage = frame_usage
                choices = payload.get("choices") or []
                if not choices:
                    continue  # e.g. the trailing usage-only frame
                first = choices[0]
                if not isinstance(first, dict):
                    continue
                delta = first.get("delta") or {}
                # Reasoning models emit their thinking in ``reasoning_content`` before the
                # answer in ``content``: show both live, but return only ``content`` so the
                # parse pipeline stays byte-identical to the non-streaming path.
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    pending.append(reasoning)
                piece = delta.get("content")
                if piece:
                    pending.append(piece)
                    content_buf.append(piece)
                fr = first.get("finish_reason")
                if fr:
                    finish_reason = fr
                flush()
                # Honour cancel within one frame of a request. Pause stays at the
                # between-chunk handle.check() in the caller (can't pause a live HTTP
                # read meaningfully).
                if handle is not None and handle.cancelled:
                    raise TaskCancelled()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if 500 <= e.code < 600:
            raise LLMUnavailableError(f"LLM 服务暂不可用（HTTP {e.code}）：{detail}") from e
        raise LLMHTTPError(e.code, detail) from e
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise LLMUnavailableError(f"LLM 服务连接失败：{e}") from e

    flush(force=True)  # push any trailing buffer so the panel shows the full output
    if handle is not None and cps:
        handle.llm_rate(0, cps)  # final rate (resting value is already 0; skip a no-op)

    return "".join(emitted).strip(), finish_reason, usage


# ---------------------------------------------------------------------------
# Structured-output retry: call the LLM, parse strictly, retry with feedback.
# Shared by every "one JSON object back" LLM surface (script parsing, BGM
# chapter analysis, music-library AI tag suggestions) so malformed replies get
# an explicit, error-bearing retry instead of a blind re-roll.
# ---------------------------------------------------------------------------
class LLMJSONRetryExhausted(RuntimeError):
    """All LLM attempts failed to yield a parseable reply.

    ``last_err`` is ``"回复不可解析"`` when the final attempt's reply did not
    parse (or ``"回复不可解析（{具体原因}）"`` when the parser returned a
    :class:`ParseRejected` with a specific violation — the reason is the
    model's actionable self-correction hint); ``last_raw`` is the final raw
    reply (``""`` when the final failure was a call error).
    """

    def __init__(self, attempts: int, last_err: str, last_raw: str = ""):
        self.attempts = attempts
        self.last_err = last_err
        self.last_raw = last_raw
        super().__init__(
            f"{attempts} 次 LLM 调用均未返回可解析的 JSON（最后错误：{last_err}）"
        )


class ParseRejected:
    """A parse that failed with a KNOWN, model-actionable reason.

    ``parse → None`` says "not parseable" (blind re-roll); returning a
    ``ParseRejected(reason)`` says "parseable, but this specific field
    violates the contract" (e.g. ``end_segment`` past the batch bound).
    :func:`llm_json_with_retry` threads ``reason`` into ``last_err`` and the
    【重试】 feedback block — a deterministic model then receives the exact
    violation description on every retry instead of re-rolling an identical
    reply (2026-09 incident: chapter-final batches, stable 3× identical
    rejection → stable batch failure).
    """

    def __init__(self, reason: str):
        self.reason = reason


def _retry_feedback(user: str, last_err: str, last_raw: str, format_hint: str) -> str:
    """Append the 【重试】 block to the original user message (the system prompt
    stays byte-identical across attempts)."""
    excerpt = last_raw[:500] or "（调用失败，无回复）"
    return (
        user
        + "\n\n【重试】你上一次的回复不合格，请重新输出。"
        + f"\n问题：{last_err}"
        + f"\n上次回复节选（可能不完整）：\n{excerpt}"
        + "\n要求：只输出 JSON（不要解释、不要前后缀、不要代码围栏）。格式：" + format_hint
    )


def _reply_excerpt(content: str, full_limit: int = 600, head: int = 200,
                   tail: int = 400) -> str:
    """One-line excerpt of a failed LLM reply for the terminal log.

    A max_tokens-truncated reply ends mid-string/mid-array; a format
    violation is a COMPLETE reply. For short replies (≤ ``full_limit``) the
    WHOLE reply is shown — the old tail-only view was blind to the head,
    where a violation's offending fields (e.g. ``start_segment`` /
    ``end_segment`` indices) actually sit. Longer replies show head + tail."""
    flat = content.replace("\n", " ").replace("\r", " ")
    if not flat:
        return "（空回复）"
    if len(flat) <= full_limit:
        return flat
    return flat[:head] + " …… " + flat[-tail:]


def llm_json_with_retry(llm_cfg, system: str, user: str, parse, *,
                        handle=None, llm_call=None, max_attempts: int = 3,
                        max_tokens: int = 512, temperature: float = 0.2,
                        top_p: float = 0.9, presence_penalty: float = 0.0,
                        format_hint: str = "",
                        extra_body: dict | None = None,
                        operation_type: str = "llm.operation") -> tuple[object, int]:
    """LLM call + strict parse with error-feedback retries.

    Each attempt: ``handle.check()`` (``TaskCancelled`` propagates and is never
    retried) → ``llm_call(...)`` → ``parse(content)``. A call exception or a
    ``parse → None`` feeds the NEXT attempt's user message with a 【重试】 block
    carrying the parse error plus an excerpt of the previous reply; the system
    prompt is never modified. A ``parse → ParseRejected(reason)`` (a known,
    model-actionable violation) additionally threads the SPECIFIC reason into
    ``last_err`` / the error text / the 【重试】 block (``回复不可解析（{reason}）``)
    so a deterministic model can self-correct instead of re-rolling an
    identical reply. Exhaustion raises :class:`LLMJSONRetryExhausted` —
    callers map it to their own user-facing error text.

    ``llm_call`` defaults to :func:`request_chat_completion`; callers should
    pass their own imported reference (``llm_call=_llm_chat_completion``)
    so ``monkeypatch``-ing the caller module's attribute keeps working in
    tests. Returns ``(parsed, attempts_used)``. Mechanical work (sampling,
    registry writes, matching) must stay OUTSIDE this loop — it only re-runs
    the LLM call itself.

    ``extra_body`` is forwarded to every attempt (openai-SDK ``extra_body``
    parity) — e.g. ``{"enable_thinking": False}`` on small-JSON surfaces so a
    thinking model's reasoning trace cannot starve the answer budget.
    """
    call = llm_call or request_chat_completion
    last_err = "未知错误"
    last_raw = ""
    for attempt in range(1, max_attempts + 1):
        if handle is not None:
            handle.check()
            handle.log(f"LLM（第 {attempt}/{max_attempts} 次）…")
        try:
            content, finish, usage = call(
                llm_cfg.base_url, llm_cfg.api_key, llm_cfg.model_name,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                temperature=temperature, top_p=top_p,
                presence_penalty=presence_penalty, max_tokens=max_tokens,
                extra_body=extra_body,
            )
        except TaskCancelled:
            raise
        except Exception as e:  # noqa: BLE001 — record, retry with feedback
            if isinstance(e, LLMUnavailableError):
                raise
            last_err = str(e)
            last_raw = ""
            log.warning("LLM JSON 调用失败（第 %d/%d 次）：%s",
                        attempt, max_attempts, last_err)
            if attempt < max_attempts and handle is not None:
                handle.log(f"第 {attempt} 次调用失败（{last_err}），携带错误反馈重试…",
                           "WARNING")
            continue
        parsed = parse(content)
        rejected = parsed if isinstance(parsed, ParseRejected) else None
        if rejected is None and parsed is not None:
            from ..platform.quota import consume_llm_output
            consume_llm_output(content, operation_type)
            return parsed, attempt
        last_raw = content
        # 诊断失败根因并打进终端日志：截断（服务端明确报 finish_reason=length）
        # 与格式违规表面都是「回复不可解析」，此前两者完全无法区分。
        if finish == "length":
            detail = "finish_reason=length"
            if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
                detail += f", completion_tokens={usage['completion_tokens']}"
            detail += f" / max_tokens={max_tokens}"
            last_err = f"回复不可解析（被 max_tokens 截断：{detail}）"
            log.warning(
                "LLM JSON 回复被 max_tokens 截断（第 %d/%d 次，%s）——思考 token 与正文"
                "共用预算；回复：%s",
                attempt, max_attempts, detail, _reply_excerpt(content))
        else:
            # ParseRejected 携带具体违规原因 → 并入 last_err 与【重试】反馈：
            # 确定性模型能按原因自我纠正（同形回复重复投喂同一具体原因）。
            last_err = (f"回复不可解析（{rejected.reason}）"
                        if rejected else "回复不可解析")
            log.warning(
                "LLM JSON 回复不可解析（第 %d/%d 次，finish_reason=%s，回复长度=%d）"
                "——%s；回复：%s",
                attempt, max_attempts, finish, len(content), last_err,
                _reply_excerpt(content))
        if attempt < max_attempts and handle is not None:
            handle.log("回复不可解析，携带错误反馈重试…", "WARNING")
        user = _retry_feedback(user, last_err, last_raw, format_hint)
    raise LLMJSONRetryExhausted(max_attempts, last_err, last_raw)
