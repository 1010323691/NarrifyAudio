"""Script-generation engine — LLM → JSON (port of source ``app/generate_script.py``).

Migrates the source project's "call LLM → build prompt → generate JSON → parse/repair
JSON" pipeline into the app, so the "文本解析" page can turn novel text into the
``{speaker, text, instruct}`` entries that the (already-working) local TTS engine
consumes directly. The JSON clean / repair / salvage / chunk-splitting logic is a
faithful 1:1 port (sliced byte-for-byte from the source); the only adaptation is the
transport: the source called an OpenAI-compatible endpoint via the ``openai`` SDK,
which the backend does not need as a dependency, so the request is issued with
stdlib ``urllib`` in :mod:`backend.engines.llm_transport`.

``generate`` runs behind the durable engine context; it streams
per-chunk durable progress and log events and honours cooperative cancel between chunks.
"""
from __future__ import annotations

import bisect
import difflib
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.request

from pathlib import Path

from ..core.config import GenerationConfig, LLMConfig, PromptsConfig
from ..core.concurrency import gate
from ..core.paths import get_or_prepare_layout
from ..core.task_control import TaskCancelled
from .book import decode_buffer
from .llm_transport import HTTP_TIMEOUT, LLMHTTPError, request_chat_completion as _llm_chat_completion
from .script_prompts import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT
from .text import ends_sentence, is_chapter_title

log = logging.getLogger("audiobook.script")


# ---------------------------------------------------------------------------
# Pure JSON / text helpers (1:1 ports of the source; spliced in by the build step so
# their regex backslashes are never re-typed).
# ---------------------------------------------------------------------------
def clean_json_string(text):
    """Clean and extract valid JSON array from LLM response."""
    # Remove thinking tags (various formats used by different models)
    # GLM, DeepSeek, Qwen, etc. use different thinking tag formats
    text = re.sub(r'<think>[\s\S]*?</think>', '', text)
    text = re.sub(r'<thinking>[\s\S]*?</thinking>', '', text)
    text = re.sub(r'<reflection>[\s\S]*?</reflection>', '', text)
    text = re.sub(r'<reasoning>[\s\S]*?</reasoning>', '', text)
    # Handle unclosed thinking tags (model started thinking but didn't close)
    text = re.sub(r'<think>[\s\S]*$', '', text)
    text = re.sub(r'<thinking>[\s\S]*$', '', text)

    # Remove markdown code blocks
    if "```" in text:
        # Find content between ```json and ``` or just ``` and ```
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if match:
            text = match.group(1).strip()

    # Find the JSON array - match from first [ to its closing ]
    # Use a bracket counter to find the correct closing bracket
    start = text.find('[')
    if start == -1:
        return None

    bracket_count = 0
    end = -1
    in_string = False
    escape_next = False

    for i, char in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if char == '\\':
            escape_next = True
            continue
        if char == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == '[':
            bracket_count += 1
        elif char == ']':
            bracket_count -= 1
            if bracket_count == 0:
                end = i + 1
                break

    if end == -1:
        # No closing bracket found, try to salvage
        last_complete = text.rfind('},')
        if last_complete > start:
            return text[start:last_complete+1] + ']'
        return None

    json_text = text[start:end]

    # Clean control characters inside strings (common LLM issue)
    # Replace literal newlines/tabs inside JSON strings with escaped versions
    def fix_control_chars(match):
        s = match.group(0)
        # Replace unescaped control characters
        s = s.replace('\n', '\\n')
        s = s.replace('\r', '\\r')
        s = s.replace('\t', '\\t')
        return s

    # Fix control characters inside string values
    json_text = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', fix_control_chars, json_text)

    return json_text


def repair_json_array(json_text, log=None):
    """Attempt to repair common JSON array issues from LLM output."""
    if not json_text:
        return None

    def _filter_entries(lst):
        """Keep only dict entries; LLMs sometimes emit bare strings in the array."""
        filtered = [e for e in lst if isinstance(e, dict)]
        if len(filtered) < len(lst) and log:
            log(f"Dropped {len(lst) - len(filtered)} non-object entries from LLM JSON array")
        return filtered if filtered else None

    # Try parsing as-is first
    try:
        result = json.loads(json_text)
        if isinstance(result, list):
            return _filter_entries(result)
    except json.JSONDecodeError:
        pass

    # Fix 1: Add missing commas between objects (}\s*{" -> },\n{")
    fixed = re.sub(r'\}\s*\{', '},\n{', json_text)
    try:
        result = json.loads(fixed)
        if isinstance(result, list):
            return _filter_entries(result)
    except json.JSONDecodeError:
        pass

    # Fix 2: Remove trailing commas before ]
    fixed = re.sub(r',\s*\]', ']', fixed)
    try:
        result = json.loads(fixed)
        if isinstance(result, list):
            return _filter_entries(result)
    except json.JSONDecodeError:
        pass

    # Fix 3: Try to extract individual entries and rebuild
    entries = []
    # Match individual JSON objects
    pattern = r'\{\s*"speaker"\s*:\s*"[^"]*"\s*,\s*"text"\s*:\s*"(?:[^"\\]|\\.)*"\s*,\s*"instruct"\s*:\s*"(?:[^"\\]|\\.)*"\s*\}'
    matches = re.findall(pattern, json_text, re.DOTALL)

    for match in matches:
        try:
            entry = json.loads(match)
            entries.append(entry)
        except json.JSONDecodeError:
            continue

    if entries:
        return entries

    # Fix 4: Last resort - find last complete entry and truncate
    last_complete = json_text.rfind('},')
    if last_complete > 0:
        try:
            truncated = json_text[:last_complete+1] + ']'
            # Ensure it starts with [
            if not truncated.strip().startswith('['):
                truncated = '[' + truncated
            result = json.loads(truncated)
            if isinstance(result, list):
                return _filter_entries(result)
        except json.JSONDecodeError:
            pass

    return None

def salvage_json_entries(json_text):
    """Last resort: extract individual valid entries with regex."""
    entries = []
    # Match individual JSON objects with speaker, text, instruct fields
    pattern = r'\{\s*"speaker"\s*:\s*"([^"]*)"\s*,\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"instruct"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}'
    matches = re.finditer(pattern, json_text, re.DOTALL)

    for match in matches:
        try:
            entry = {
                "speaker": match.group(1),
                "text": match.group(2).replace('\\"', '"').replace('\\n', '\n'),
                "instruct": match.group(3).replace('\\"', '"').replace('\\n', '\n')
            }
            entries.append(entry)
        except Exception:
            continue

    return entries if entries else None


def fix_mojibake(text):
    """Fix common mojibake characters resulting from CP1252-as-UTF8."""
    replacements = {
        'â€™': ''',  # Right single quote
        'â€˜': ''',  # Left single quote
        'â€œ': '"',  # Left double quote
        'â€\x9d': '"', # Right double quote
        'â€?': '"', # Sometimes ? if undefined
        'â€"': '—',  # Em dash
        'â€"': '–',  # En dash
        'â€¦': '…',  # Ellipsis
    }

    for bad, good in replacements.items():
        text = text.replace(bad, good)

    return text

def _evict_trailing_title(chunk: str) -> tuple:
    """若章标题行落在 chunk 尾部 100 字内 → 切到标题行之前，标题移交下一 chunk 开头。

    标题留在 chunk 尾部有两害：本 chunk 的模型看到标题却看不到它统领的章节正文
    （判定漂移）；输出 JSON 被截断时，尾部条目恰是最先丢失的（标题随 chunk 一起消失）。
    移到下一 chunk 开头则与章节正文同段——上下文完整，也远离截断风险。

    返回 (去掉标题的 chunk, 标题行)；尾部无标题、或切掉标题会让 chunk 变空
    （chunk 本身就是一个标题）时返回 (chunk, "")。
    """
    if not chunk or "\n" not in chunk:
        return chunk, ""
    head, last_line = chunk.rsplit("\n", 1)
    if not is_chapter_title(last_line) or len(chunk) - len(last_line) > 100:
        return chunk, ""
    body = head.rstrip()
    if not body:
        return chunk, ""
    return body, last_line


def _run_start(S: str, q: int) -> int:
    """包含 q 的极大空白游程的起始偏移（q = 空白字符，或零宽句末位置）。

    句末候选落在标点之后：若后随空白则游程自标点后的第一个空白字符起；
    若标点直接衔接文字（中文常态）则游程长度为零、起点即标点后一位。
    """
    while q > 0 and S[q - 1].isspace():
        q -= 1
    return q


def _boundary_runs(S: str) -> list:
    """统一合法边界表（硬约束）：合法切点偏移的升序列表。

    一个极大空白游程 = 一个逻辑边界（同 gap 内多类候选合并为一个）；句末候选
    分两类——CJK 句末标点 。！？!?… 零宽（不要求后随空白——旧正则
    ``(?<=[.!?])\\s+`` 对中文从不触发，正是「超长中文段切不开」的根因）；
    ASCII .!? 须后随空白（保护 3.14 / e.g. 之类的词内点号）。
    切点只能落在这张表的偏移上；没有合法边界就不切。
    """
    starts = set()
    for m in re.finditer(r"\n\s*\n", S):
        starts.add(_run_start(S, m.start()))
    for m in re.finditer(r"\n", S):
        starts.add(_run_start(S, m.start()))
    for m in re.finditer(r"(?<=[。！？!?…])", S):
        p = m.end()
        if p < len(S):
            starts.add(_run_start(S, p))
    for m in re.finditer(r"(?<=[.!?])\s", S):
        starts.add(_run_start(S, m.end() - 1))
    starts.discard(0)
    return sorted(starts)


def _tail_is_title(block: str) -> bool:
    """块末行（strip 后）是否为章标题——悬题判定，严于兜底（无 100 字窗）。

    标题独占块不算悬题（那是预期的独立块形态，如书末标题 / 被移到下一块
    开头的标题），与 _evict_trailing_title 的「标题独占块不清空」一致。
    """
    b = block.strip()
    if not b or "\n" not in b:
        return False
    return is_chapter_title(b.rsplit("\n", 1)[1])


def _find_cut(runs: list, t: float, floor: int, S: str):
    """目标位 t 的切点：t 最近、且在上一切点（floor）右侧的合法边界。

    悬题改判：最近者悬题（切后前一块末行是章标题）而另一候选不悬题 →
    必选不悬题者（标题防丢是硬约束，均长只是审美）；两候选皆悬题 → 取最近
    （交兜底 _evict_trailing_title 前移）。平手取较小偏移（确定性）。
    无候选 / 切出的块为空 → None（余下并入前一块，绝不强切）。
    """
    i = bisect.bisect_left(runs, t)
    cands = []
    if i > 0 and runs[i - 1] > floor:
        cands.append(runs[i - 1])
    if i < len(runs) and runs[i] > floor:
        cands.append(runs[i])
    cands = [c for c in cands if S[floor:c].strip()]
    if not cands:
        return None
    safe = [c for c in cands if not _tail_is_title(S[floor:c])]
    pool = safe if safe else cands
    return min(pool, key=lambda c: (abs(t - c), c))


def _drop_short_chunks(S: str, cuts: list, size: int) -> list:
    """收尾调整：消除过短块（strip 后 < 目标长度一半）。

    合并 = 只删一个已有切点（不重切、不引入任何新切点——每轮严格收缩，
    必然终止、无震荡）；与较短邻块合并（均长最优）；合并不得跨越不可拆
    结构——合并块末行成为章标题（悬题，兜底会把它移进下一块）则该侧禁止；
    两侧都不可合并 → 保留该短块（允许，非异常）。
    """
    threshold = size / 2
    accepted = set()
    while True:
        bounds = [0] + list(cuts) + [len(S)]
        lens = [len(S[a:b].strip()) for a, b in zip(bounds, bounds[1:])]
        i = next(
            (k for k in range(len(lens))
             if lens[k] < threshold and (bounds[k], bounds[k + 1]) not in accepted),
            None,
        )
        if i is None:
            break
        a, b = bounds[i], bounds[i + 1]
        # 合并结果是否「末行成标题」按兜底自身口径（_evict，含 100 字窗）判定，
        # 与切片阶段的驱逐语义一致——绝不产出会在切片时被再驱逐的块。
        okL = i > 0 and _evict_trailing_title(S[bounds[i - 1]:b].strip())[1] == ""
        okR = i < len(lens) - 1 and _evict_trailing_title(S[a:bounds[i + 1]].strip())[1] == ""
        if not (okL or okR):
            accepted.add((a, b))
            continue
        if okL and (not okR or lens[i - 1] <= lens[i + 1]):
            cuts.remove(a)                # 与较短左邻合并（删本块起点切点）
        else:
            cuts.remove(b)                # 与较短右邻合并（删本块终点切点）
    return list(cuts)


def split_into_chunks(text, max_size=3000):
    """Split text into chunks: fix the count first, then snap each cut to the
    nearest legal structural boundary around the target average length.

    段数 = ceil(总长 / size)；固定切法下尾段 < size/2 时减一重新平均；再按合法
    边界数钳制（合法结构边界 = 最高约束，段数 / 均长只是软目标）。切点只能落在
    合法边界（段落 > 换行 > 句末；CJK 句末零宽、ASCII .!? 须后随空白）——绝不
    拦腰截断句子 / 对话；目标位附近无边界则吸附到最近合法边界（允许个别段超
    目标长）；完全没有合法边界则不切。
    章标题防丢：切后块末行是标题 → 切点改判移到标题行之前（搜索期悬题判定，
    严于兜底）；兜底 = _evict_trailing_title + carry 移交（保留旧版语义，覆盖
    「唯一候选即悬题」与「书末标题独立成块」）。
    过短块（不足目标半长）与较短邻块合并——只删切点、不跨章标题、无法合并则
    保留短块。无损口径 = 现有「去空白后拼接相等」。
    """
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    if not paragraphs:
        return []
    S = "\n\n".join(paragraphs)
    total = len(S)
    size = max(1, int(max_size))

    runs = _boundary_runs(S)

    # ① 段数（软目标，受硬约束钳制：内部合法边界数）
    count = (total + size - 1) // size
    if count > 1 and total - (count - 1) * size < size / 2:
        count -= 1
    count = min(count, len(runs) + 1)

    # ② 切点搜索：每个目标位的最近合法边界（严格单调，悬题改判）
    cuts = []
    for k in range(1, count):
        c = _find_cut(runs, total * k / count, cuts[-1] if cuts else 0, S)
        if c is None:
            break  # 无合法边界 → 余下并入前一块（不强切）
        cuts.append(c)

    # ③ 标题兜底：块末行是标题（100 字窗内）→ 切点前移到标题行起点
    for i in range(len(cuts)):
        prev = cuts[i - 1] if i else 0
        _, title = _evict_trailing_title(S[prev:cuts[i]].strip())
        if title:
            cuts[i] -= len(title)

    # ④ 收尾调整：消除过短块（只删切点）
    cuts = _drop_short_chunks(S, cuts, size)

    # ⑤ 切片 + carry（书末标题独立成块，绝不丢）
    bounds = [0] + cuts + [total]
    chunks = []
    carry = ""
    for a, b in zip(bounds, bounds[1:]):
        piece = S[a:b].strip()
        if carry:
            piece = f"{carry}\n\n{piece}" if piece else carry
            carry = ""
        body, title = _evict_trailing_title(piece)
        if body:
            chunks.append(body)
        carry = title
    if carry:
        chunks.append(carry)

    return chunks


# ---------------------------------------------------------------------------
# Structured-output retry: call the LLM, parse strictly, retry with feedback.
# Shared by every "one JSON object back" LLM surface (BGM chapter analysis,
# music-library AI tag suggestions) so malformed replies get an explicit,
# error-bearing retry instead of a blind re-roll.
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

    ``llm_call`` defaults to this module's :func:`_llm_chat_completion`; callers
    should pass their own imported reference (``llm_call=_llm_chat_completion``)
    so ``monkeypatch``-ing the caller module's attribute keeps working in
    tests. Returns ``(parsed, attempts_used)``. Mechanical work (sampling,
    registry writes, matching) must stay OUTSIDE this loop — it only re-runs
    the LLM call itself.

    ``extra_body`` is forwarded to every attempt (openai-SDK ``extra_body``
    parity) — e.g. ``{"enable_thinking": False}`` on small-JSON surfaces so a
    thinking model's reasoning trace cannot starve the answer budget.
    """
    call = llm_call or _llm_chat_completion
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


def _llm_chat_completion_stream(base_url, api_key, model, messages,
                                temperature, top_p, presence_penalty, max_tokens,
                                top_k=0, min_p=0, banned_tokens=None, handle=None):
    """Streaming twin of ``_llm_chat_completion``.

    Issues the same OpenAI-compatible ``chat/completions`` POST with ``stream: true``
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
    ``handle`` may be ``None`` (no UI forwarding / no mid-stream cancel) for tests.
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
        "stream": True,
        # Ask OpenAI-compatible servers to include usage in the final frame; harmless
        # for servers that ignore it (usage then stays None -> tokens logged as "?").
        "stream_options": {"include_usage": True},
    }
    # openai ``extra_body`` keys go at the top level, only when set (non-zero).
    if top_k:
        body["top_k"] = top_k
    if min_p:
        body["min_p"] = min_p
    if banned_tokens:
        body["banned_tokens"] = banned_tokens

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
        raise RuntimeError(f"LLM HTTP {e.code}: {detail}") from e

    flush(force=True)  # push any trailing buffer so the panel shows the full output
    if handle is not None and cps:
        handle.llm_rate(0, cps)  # final rate (resting value is already 0; skip a no-op)

    return "".join(emitted).strip(), finish_reason, usage


# ---------------------------------------------------------------------------
# 逐 chunk 忠实性校验 + 恢复（LLM 输出事后验证）
# ---------------------------------------------------------------------------

# 引语段抽取：六组常用引号对（弯双 / 弯单 / 六角 / 双六角 / 直双 / 直单）。
# 手写引号字符不可靠，一律 \uXXXX 转义（同忠实性校验的既有教训）。
_FIDELITY_QUOTE_PATTERNS = (
    re.compile("\\u201c([^\\u201c\\u201d]+)\\u201d"),   # “…” 弯双引号
    re.compile("\\u2018([^\\u2018\\u2019]+)\\u2019"),   # ‘…’ 弯单引号
    re.compile("\\u300c([^\\u300c\\u300d]+)\\u300d"),   # 「…」 六角引号
    re.compile("\\u300e([^\\u300e\\u300f]+)\\u300f"),   # 『…』 双六角引号
    re.compile('"([^"]+)"'),                             # "…" 直双引号
    re.compile("'([^']+)'"),                              # '…' 直单引号
)


def _skeleton(text: str) -> str:
    """词字符骨架：只留字母/数字/CJK（``str.isalnum`` 对汉字为真），去掉空白/标点/引号。

    忠实性校验比较的是骨架：对模型常见的引号增删、语气标签剥离、标点规范化
    天然免疫（不误报），而丢行、丢段、改写都会改变骨架（必被抓住）。
    """
    return "".join(ch for ch in text if ch.isalnum())


def check_chunk_fidelity(chunk: str, entries) -> list:
    """逐 chunk 忠实性校验：源 chunk 中所有 ≥4 个词字符的引语段，其骨架必须能在
    输出条目 text 的拼接骨架中找到。返回缺失的引语段骨架列表（空 = 通过）。

    用引语段而非全文字数作忠实性信号：引号/标签的机械增删不触发误报，而整块
    截断（JSON 尾部被截）与零星丢行都必然触发。
    """
    if not chunk:
        return []
    out = _skeleton("".join(
        e.get("text") for e in entries if isinstance(e.get("text"), str)
    ))
    missing = []
    seen = set()
    for pat in _FIDELITY_QUOTE_PATTERNS:
        for m in pat.finditer(chunk):
            sk = _skeleton(m.group(1))
            if len(sk) < 4 or sk in seen:
                continue
            seen.add(sk)
            if sk not in out:
                missing.append(sk)
    return missing


def _is_ignorable_alignment_gap(value: str) -> bool:
    """Ignore only short source-side attribution fragments.

    The parser is allowed to remove mechanical speech tags such as ``林某说道``
    while preserving the actual spoken/narrated text.  Everything else, including
    output-only text, remains an alignment error.
    """
    if not value or len(value) > 4:
        return False
    tag_trail = frozenset(globals().get("_SAY_VERBS", "说道问答喊"))
    return value[-1] in tag_trail and any(ch.isalnum() for ch in value[:-1])


def check_chunk_alignment(chunk: str, entries) -> dict:
    """Compare the complete local source chunk with parsed entry text.

    Unlike :func:`check_chunk_fidelity`, which only checks quoted passages, this
    check aligns the whole normalized source and output sequences.  Punctuation,
    whitespace and outer quotation marks are ignored; short attribution tags are
    the only permitted source-side omission.  The result is a diagnostic mapping
    so callers can decide whether to escalate without another LLM call.
    """
    source = _skeleton(chunk or "")
    source_positions = [i for i, ch in enumerate(chunk or "") if ch.isalnum()]
    output = _skeleton("".join(
        e.get("text") for e in entries
        if isinstance(e, dict) and isinstance(e.get("text"), str)
    ))
    speaker_skeletons = {
        _skeleton(e.get("speaker") or "")
        for e in entries
        if isinstance(e, dict) and e.get("speaker") and e.get("speaker") != "NARRATOR"
    }
    if not source:
        return {
            "ok": not output,
            "coverage": 1.0 if not output else 0.0,
            "missing": [],
            "extra": [output] if output else [],
            "source": source,
            "output": output,
        }

    matcher = difflib.SequenceMatcher(a=source, b=output, autojunk=False)
    missing: list[str] = []
    extra: list[str] = []
    matched = 0
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            matched += a1 - a0
            continue
        if tag in ("delete", "replace"):
            gap = source[a0:a1]
            quote_prefix = False
            if gap and a1 < len(source_positions) and a1 > 0:
                raw_lo = source_positions[a1 - 1] + 1
                raw_hi = source_positions[a1] + 1
                quote_prefix = any(ch in _QUOTE_CHARS for ch in (chunk or "")[raw_lo:raw_hi])
            if (len(gap) > 1 and gap not in speaker_skeletons
                    and not _is_ignorable_alignment_gap(gap)
                    and not (len(gap) <= 4 and quote_prefix)):
                missing.append(gap)
            else:
                matched += a1 - a0
        if tag in ("insert", "replace"):
            gap = output[b0:b1]
            if len(gap) > 1:
                extra.append(gap)

    missing = list(dict.fromkeys(missing))
    extra = list(dict.fromkeys(extra))
    return {
        "ok": not missing and not extra,
        "coverage": matched / len(source) if source else 1.0,
        "missing": missing,
        "extra": extra,
        "source": source,
        "output": output,
    }


def split_chunk_balanced(chunk: str) -> tuple:
    """把 chunk 在尽量靠近中点的安全边界（段落边界 > 换行 > 句末标点）切成两半。

    绝不拦腰切断文字；找不到任何边界时返回 ``(chunk, "")``（调用方保留整块）。
    """
    mid = len(chunk) // 2

    def cut(cands):
        # 优先中点±1/4 区间内的边界；没有则取全 chunk 范围内最靠近中点者。
        for lo, hi in ((len(chunk) // 4, (3 * len(chunk)) // 4), (1, len(chunk) - 1)):
            for c in sorted(cands, key=lambda c: abs(c - mid)):
                if not (lo <= c < hi):
                    continue
                left, right = chunk[:c].strip(), chunk[c:].strip()
                if left and right:
                    return left, right
        return None

    cands = {
        "para": [m.end() for m in re.finditer(r"\n\s*\n", chunk)],
        "nl": [m.end() for m in re.finditer(r"\n", chunk)],
        "sent": [m.end() for m in re.finditer(r"(?<=[。！？!?…])", chunk)],
    }
    for key in ("para", "nl", "sent"):
        result = cut([c for c in cands[key] if 0 < c < len(chunk)])
        if result:
            return result
    return chunk, ""


# ---------------------------------------------------------------------------
# Per-chunk orchestration + the Task worker.
# ---------------------------------------------------------------------------

def process_chunk(handle, llm, model_name, chunk, chunk_num, total_chunks,
                  previous_entries=None, max_retries=2,
                  system_prompt=None, user_prompt_template=None,
                  max_tokens=4096, temperature=0.6, top_p=0.8, top_k=0, min_p=0,
                  presence_penalty=0.0, banned_tokens=None, recover=True):
    """Process one text chunk via the LLM and return its JSON script entries.

    Faithful port of the source ``process_chunk``: build the cross-chunk context
    (Part N/M header + character roster + last-3 entries), fill the *user* template
    via ``.format(context=..., chunk=...)`` (the system prompt is used verbatim),
    then call the LLM up to ``max_retries + 1`` times, cleaning / repairing / salvaging
    the JSON each attempt. Progress is logged through the Task ``handle``.

    Fidelity recovery (appended to the port) is a deliberately short two-step ladder:
    a parseable reply is verified against the source — every quoted passage of the
    chunk (≥4 word chars, ``check_chunk_fidelity``) must be present in the output
    texts, so a truncated / drift-damaged reply that JSON repair would have silently
    accepted is caught. On failure: (1) ONE re-run with ``max_tokens`` temporarily
    doubled (truncation is the common root cause); (2) if still unfaithful, split the
    chunk in half at a safe boundary (``split_chunk_balanced``) and re-run both halves
    once (``recover=False`` — the halves get no further escalation, so the ladder
    can't run away). What can't be recovered is kept as-is (never silently dropped).
    """
    sys_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
    usr_template = user_prompt_template or DEFAULT_USER_PROMPT

    context_parts = []
    if chunk_num == 1:
        context_parts.append("(Beginning of text)")
    elif chunk_num == total_chunks:
        context_parts.append("(End of text)")
    else:
        context_parts.append(f"(Part {chunk_num} of {total_chunks})")

    if previous_entries and len(previous_entries) > 0:
        # Build a character roster for name consistency across chunks.
        characters_seen = sorted(set(
            entry.get("speaker", "") for entry in previous_entries
            if entry.get("speaker", "") and entry.get("speaker", "") != "NARRATOR"
        ))
        if characters_seen:
            context_parts.append(f"Characters in this book: {', '.join(characters_seen)}")
        # Include the last few entries so the model keeps style / tone continuity.
        tail = previous_entries[-3:]
        context_parts.append("\nPrevious section ended with:")
        for entry in tail:
            context_parts.append(json.dumps(entry, ensure_ascii=False))

    context = "\n".join(context_parts)
    user_prompt = usr_template.format(context=context, chunk=chunk)

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ]

    def _attempt(temp: float, mt=None):
        """一次 LLM 调用 + JSON 清理/修复/抢救 → (entries 或 None, 原始响应文本)。

        ``mt`` = 本次调用的 max_tokens 覆盖值（缺省用配置值）——恢复阶梯第 1 步
        （翻倍 max_tokens 重跑）用它。调用失败 / 响应不可解析 → ``(None, …)`` 交
        调用方重试；``TaskCancelled`` 恒上抛（取消不重试）。
        """
        mt = mt or max_tokens
        try:
            if llm.stream:
                # Stream the completion so the 「流式反馈」 panel shows the model's raw
                # output live; the accumulated content is identical to the non-streaming
                # response, so the JSON handling below is unchanged.
                text, finish_reason, usage = _llm_chat_completion_stream(
                    llm.base_url, llm.api_key, model_name, messages,
                    temperature=temp, top_p=top_p,
                    presence_penalty=presence_penalty, max_tokens=mt,
                    top_k=top_k, min_p=min_p, banned_tokens=banned_tokens,
                    handle=handle,
                )
            else:
                # Safety valve: some servers reject stream:true — fall back to the
                # non-streaming call (the stream panel then stays empty).
                text, finish_reason, usage = _llm_chat_completion(
                    llm.base_url, llm.api_key, model_name, messages,
                    temperature=temp, top_p=top_p,
                    presence_penalty=presence_penalty, max_tokens=mt,
                    top_k=top_k, min_p=min_p, banned_tokens=banned_tokens,
                )
            pt = usage.get("prompt_tokens", "?") if usage else "?"
            ct = usage.get("completion_tokens", "?") if usage else "?"
            handle.log(f"chunk {chunk_num}/{total_chunks}: finish_reason={finish_reason} | "
                       f"tokens prompt={pt} completion={ct}")
            if finish_reason == "length":
                handle.log(f"WARNING: 响应被截断（达到 max_tokens={max_tokens}），可增大 max_tokens。", "WARNING")
        except TaskCancelled:
            raise  # a cancel raised mid-stream must propagate, not be retried
        except Exception as e:  # noqa: BLE001 — a failed call retries, then gives up
            handle.log(f"调用 LLM API 出错：{e}", "ERROR")
            return None, ""

        # Clean and extract JSON from the response.
        json_text = clean_json_string(text)
        if not json_text:
            handle.log(f"chunk {chunk_num} 响应中未找到 JSON 数组", "WARNING")
            handle.log(f"Response preview: {text[:300]}...", "WARNING")
            return None, text

        # Try to parse, with repair attempts.
        entries = repair_json_array(json_text, log=lambda m: handle.log(m, "WARNING"))
        if entries:
            from ..platform.quota import consume_llm_output
            consume_llm_output(text, "script.parse")
            return entries, text

        handle.log(f"chunk {chunk_num} 响应无法解析为 JSON", "WARNING")
        handle.log(f"JSON preview: {json_text[:300]}...", "WARNING")

        # Last resort: extract individual valid entries with regex.
        salvaged = salvage_json_entries(json_text)
        if salvaged:
            from ..platform.quota import consume_llm_output
            consume_llm_output(text, "script.parse")
            handle.log(f"正则抢救出 {len(salvaged)} 条 entries（来自畸形响应）")
            return salvaged, text
        return None, text

    entries = None
    for attempt in range(max_retries + 1):
        handle.check()  # cooperative cancel / pause point between attempts
        entries, _raw = _attempt(temperature)
        if entries:
            if attempt > 0:
                handle.log(f"  Succeeded on retry {attempt + 1}")
            break
        if attempt < max_retries:
            handle.log("Retrying...")

    if not entries:
        return []

    # -- Fidelity check + recovery (two-step ladder) ------------------------------
    # A parseable reply is NOT automatically faithful: a truncated JSON (repair
    # "salvages" the head and silently drops the tail) or a drifting model (skips a
    # line) both parse cleanly. Verify the source's quoted passages against the
    # output, then escalate in exactly two steps: double max_tokens and re-run once;
    # if that is still unfaithful, split in half and re-run both halves once.
    if not recover:
        # 对半切出来的半段：只跑这一次，残余缺失记日志，不再升级（阶梯到头）。
        alignment = check_chunk_alignment(chunk, entries)
        missing = alignment["missing"] or alignment["extra"]
        if not alignment["ok"]:
            handle.log(f"chunk {chunk_num}（半段）仍缺失 {len(missing)} 处引语段，"
                       f"保留现有 {len(entries)} 条", "WARNING")
        return entries

    alignment = check_chunk_alignment(chunk, entries)
    missing = alignment["missing"] or alignment["extra"]
    if alignment["ok"]:
        return entries

    # Step 1: output was likely cut off at max_tokens — double the budget, run once.
    handle.log(
        f"chunk {chunk_num}/{total_chunks} 忠实性校验缺失 {len(missing)} 处引语段"
        f"（如 “{missing[0][:10]}…”） → max_tokens 临时翻倍"
        f"（{max_tokens} → {max_tokens * 2}）再跑一次",
        "WARNING",
    )
    handle.check()
    bigger, _raw = _attempt(temperature, max_tokens * 2)
    if bigger:
        alignment2 = check_chunk_alignment(chunk, bigger)
        if alignment2["ok"]:
            handle.log(f"chunk {chunk_num} 翻倍 max_tokens 后忠实性校验通过")
            return bigger
        if len(bigger) > len(entries):
            # 仍缺失，但内容更全 → 以翻倍结果为准（缺失清单同步更新）
            entries, alignment = bigger, alignment2
            missing = alignment["missing"] or alignment["extra"]

    # Step 2: still missing — split in half at a safe boundary, re-run both halves once.
    left, right = split_chunk_balanced(chunk)
    if left and right and len(left) < len(chunk) and len(right) < len(chunk):
        handle.log(
            f"chunk {chunk_num} 翻倍后仍缺失 {len(missing)} 处 → 对半切开"
            f"（{len(left)} + {len(right)} 字）各再跑一次",
            "WARNING",
        )
        left_entries = process_chunk(
            handle, llm, model_name, left, chunk_num, total_chunks,
            previous_entries=previous_entries,
            max_retries=max_retries,
            system_prompt=sys_prompt, user_prompt_template=usr_template,
            max_tokens=max_tokens, temperature=temperature, top_p=top_p,
            top_k=top_k, min_p=min_p, presence_penalty=presence_penalty,
            banned_tokens=banned_tokens, recover=False,
        )
        right_prev = (list(previous_entries) + left_entries) if previous_entries else left_entries
        right_entries = process_chunk(
            handle, llm, model_name, right, chunk_num, total_chunks,
            previous_entries=right_prev or None,
            max_retries=max_retries,
            system_prompt=sys_prompt, user_prompt_template=usr_template,
            max_tokens=max_tokens, temperature=temperature, top_p=top_p,
            top_k=top_k, min_p=min_p, presence_penalty=presence_penalty,
            banned_tokens=banned_tokens, recover=False,
        )
        return left_entries + right_entries

    handle.log(f"chunk {chunk_num} 缺失内容无法恢复，保留现有 {len(entries)} 条", "WARNING")
    return entries


def merge_adjacent_same_speaker(entries, title_test, max_chars=100, short_cap=10):
    """机械合并**连续同 speaker** 条目（解析后的确定性后处理，不经 LLM；NARRATOR 与角色同规则）。

    相邻两条同讲者条目会让 TTS 多插一次同人停顿（``pause_same_speaker_ms``）和一个段边界；
    合并成一条是内容保真的（除边界补「。」外不改任何字符）。自旧「仅 NARRATOR」版推广到
    **任意说话人**：角色连续短台词也合并（≤10 强制合并会吞掉对白刻意留白——韵律权衡，
    ``merge_same_speaker`` 开关可一键回退）。

    字数口径 = 词字符数（``_skeleton`` 骨架长度：只留字母/数字/CJK，排除标点/空白/引号）——
    与「不计标点、空格/换行」一致，**非原文长度**：
    - 相邻两段同人：合并后总字数 ≤ ``max_chars``（缺省 100）→ 合并；
    - **强制合并**：较短一方 ≤ ``short_cap``（缺省 10）→ 必合并（即使 > ``max_chars``）；
    - ≥3 段同人从左到右贪心：维护已合并块，后续段满足（块+该段 ≤ ``max_chars``）或
      （较短一方 ≤ ``short_cap``）则并入，否则封块、以该段开新块。

    合并产物：``speaker`` 不变；``text`` 首尾相接、**边界若前段尾（去尾空白）无收尾标点**
    （``ends_sentence``：。！？…及闭引号等）则补一个「。」（逐边界判定）；``instruct`` 取块内
    **词字符数最多**的成员（平手取最左）。

    章标题守卫：标题行**前后两侧**都不合并（章节边界处的停顿正是可听的章节分界）。**恒判、
    无豁免**——旧不变量「两个非标题拼接不构成 ≤40 字标题」为假（反例 ``"楔"``+``"子"`` 各自
    非标题、强制合并成 ``"楔子"`` = 章标题），故**每次吸收前**都对运行块文本跑 ``title_test``
    （``is_chapter_title`` >40 字短路，成本可忽略）：块一旦成为标题即封口、不再并入。

    字数上限用词字符、机械分段的 200 硬保证用原文长度（两档口径不同，勿「统一」）；**强制
    合并可造出 >200 字块**，200 硬保证由**本阶段之后**的机械分段（``split_long_entries``）切回
    ——本函数只保证内容保真 + 说话人归组，不负责 200 上界。

    返回 ``(新条目列表, 合并对数)``（合并对数 = 吸收步数，3 条并 1 条 = 2，同旧口径）；
    输入列表不被改动。
    """
    out = []
    merged = 0
    # 运行块 = [条目 dict(浅拷贝), 块词字符数, 最佳成员词字符数, 最佳 instruct 原值, 是否已合并]
    block = None

    def _wc(t):
        return len(_skeleton(t))

    def _settle(b):
        # 单条目块原样保留（不注入/不改 instruct）；合并块把 instruct 设为词字符最多
        # 成员的值——该成员无 instruct 键则删去本键（不凭空造值）。
        if not b[4]:
            return
        if b[3] is None:
            b[0].pop("instruct", None)
        else:
            b[0]["instruct"] = b[3]

    for e in entries:
        text = e.get("text") or ""
        nxt_wc = _wc(text)
        if block is None:
            # [条目 dict(浅拷贝), 块词字符数, 最佳成员词字符数, 最佳 instruct 原值, 是否已合并]
            block = [dict(e), nxt_wc, nxt_wc, e.get("instruct"), False]
            continue
        b_text = block[0].get("text") or ""
        b_wc = block[1]
        if (
            text
            and (e.get("speaker") or "") == (block[0].get("speaker") or "")
            and not title_test(b_text)
            and not title_test(text)
            and (b_wc + nxt_wc <= max_chars or min(b_wc, nxt_wc) <= short_cap)
        ):
            # 并入：块尾（去尾空白）无收尾标点则边界补「。」（逐边界判定）
            stripped_tail = b_text.rstrip()
            boundary = "。" if (stripped_tail and not ends_sentence(stripped_tail)) else ""
            block[0]["text"] = b_text + boundary + text
            block[1] = b_wc + nxt_wc  # 词字符可加（「。」是标点，不入骨架）
            if nxt_wc > block[2]:
                block[2] = nxt_wc
                block[3] = e.get("instruct")
            block[4] = True
            merged += 1
        else:
            _settle(block)
            out.append(block[0])
            block = [dict(e), nxt_wc, nxt_wc, e.get("instruct"), False]
    if block is not None:
        _settle(block)
        out.append(block[0])
    return out, merged


# ---------------------------------------------------------------------------
# 断句失败校验（解析后的条目级重判）
#
# 信号：条目 text 被外层双引号（弯 “ ” 或直 " "——任意形式）整体包裹，且引号跨度内
# 出现「…道」语气标签（说道 / 问道 / 答道 / 冷笑道 / xx道 等）后紧跟冒号——标签被包进
# 了台词里，说明断句没把内层台词拆出去。冒号是硬性条件：知道 / 难道 / 道理 / 道路
# 的「道」后无冒号，绝不触发。这种「标签在引号内」的形态对下游各阶段不可见
# （TTS 会照常规行把标签一起念出），故必须在解析阶段就地重跑校验。
# ---------------------------------------------------------------------------

_SAYING_TAG_RE = re.compile("(?:^|[\\u4e00-\\u9fffA-Za-z])道\\s*[:：]")
# 外层双引号对（弯双 + 直双——「任意形式」的引号对均被接受）。引号字符一律 \uXXXX
# 转义（同忠实性校验的既有教训：手写引号字符不可靠）；此处是普通字符串
# 比较（startswith / rfind）而非正则，故用单反斜杠让 Python 解码出真实引号字符。
_OUTER_DQUOTE_PAIRS = (("\u201c", "\u201d"), ("\u0022", "\u0022"))
# 引号内**开头**的「…道：」标签——重判提示词允许模型整段丢弃的纯语气标签（解析提示词
# 编辑 (e)）；只剥开头这一处：中段「…道：」可能是别的混合形态（本阶段不处理），
# 且按位置剥可避免把「知道：」这类真词误当标签剥掉。
_LEADING_SAYING_TAG_RE = re.compile("[\\u4e00-\\u9fffA-Za-z]+道\\s*[:：]")


def is_suspicious_entry_text(text: str) -> bool:
    """Whether an entry's text looks like a failed sentence split: the text is wrapped
    in an outer double-quote pair (curly or straight) and the quoted span holds a
    "…道：" speech tag (说道 / 问道 / xx道 …) — the tag was wrapped into the utterance
    instead of the inner line being split out into its own entry."""
    t = (text or "").strip()
    for open_q, close_q in _OUTER_DQUOTE_PAIRS:
        if t.startswith(open_q):
            # Widest span (the LAST matching closing quote): nested same-form quotes
            # ( “又道：“…”” ) close at the very end, so a mid-span tag is still seen.
            end = t.rfind(close_q)
            if end > len(open_q):
                return bool(_SAYING_TAG_RE.search(t[len(open_q):end]))
    return False


def suspicious_entry_indices(entries: list) -> list[int]:
    """Absolute indices of the entries whose text is a suspected sentence-split failure."""
    return [
        i for i, e in enumerate(entries)
        if isinstance(e, dict) and is_suspicious_entry_text(e.get("text"))
    ]


def _strip_leading_saying_tag(text: str) -> str:
    """The entry text with its leading "…道：" tag (inside the outer wrap) removed —
    the pure speech tag the parse prompt's edit (e) lets the model drop. Text without
    a leading tag is returned unchanged."""
    t = (text or "").strip()
    for open_q, _close in _OUTER_DQUOTE_PAIRS:
        if t.startswith(open_q):
            body = t[len(open_q):]
            m = _LEADING_SAYING_TAG_RE.match(body)
            if m:
                return t[: len(open_q)] + body[m.end():]
    return t


def _parse_entries_reply(text: str):
    """Parse a re-parse reply into entries — the same clean/repair/salvage ladder the
    chunk parse uses; ``None`` when nothing parseable survives."""
    if not text:
        return None
    json_text = clean_json_string(text)
    if not json_text:
        return None
    entries = repair_json_array(json_text)
    return entries if entries else salvage_json_entries(json_text)


def _reparse_vote(parts, entry: dict, roster: frozenset):
    """Gate one re-parse reply for a flagged entry and reduce it to its vote value.

    A reply votes only if it is well-formed and faithful to the original entry text:
    the parts' concatenated word-character skeleton (``_skeleton``) must equal the
    original's skeleton either WITH the leading "…道：" tag intact (tag kept, e.g. as a
    NARRATOR part) or WITH the tag dropped (the parse prompt's edit (e) — pure speech
    tags are omitted). The skeleton is immune to the prompt-permitted formatting edits
    (quote / colon / seam-punctuation changes) while any added, dropped, or reordered
    word character is caught. Part speakers must be ``NARRATOR`` or in the file-wide
    roster (no invented names); a multi-part answer needs ≥2 distinct speakers —
    same-character parts would be one entry per the parse prompt (a single-subject
    "split" is not a split). Returns the vote value (a hashable ``(speaker, skeleton)`` sequence) or
    ``None`` (the reply contributes no vote — the entry is never changed on a guess).
    """
    if not parts or not all(isinstance(p, dict) for p in parts):
        return None
    texts = []
    speakers = []
    for p in parts:
        sp = (p.get("speaker") or "").strip()
        tx = p.get("text")
        if not sp or sp not in roster:
            return None
        if not isinstance(tx, str) or not _skeleton(tx):
            return None
        texts.append(tx)
        speakers.append(sp)
    if len(parts) > 1 and len(set(speakers)) < 2:
        return None
    joined = _skeleton("".join(texts))
    orig = entry.get("text") or ""
    allowed = {_skeleton(orig), _skeleton(_strip_leading_saying_tag(orig))}
    if joined not in allowed:
        return None
    return tuple((p["speaker"].strip(), _skeleton(p["text"])) for p in parts)


# ---------------------------------------------------------------------------
# 重判批协议（断句失败校验 / 归属抽样共用）
#
# 批窗口构建、LLM 回复解析、多者胜投票、去外层引号、全书角色花名册、重试分组与
# LLM 调用封装。原属已退役的独立检查模块（speaker_check / mix_check），两检查链路
# 拆除后整体并入本模块——解析内两个重判阶段是它们如今唯一的调用方。
# ---------------------------------------------------------------------------

# Outer quotation-mark pairs the re-judgment may strip from a character entry's
# stored text. The prompt's optional ``text`` key = the stored text minus EXACTLY
# ONE such outer pair (inner quotes — a quoted term inside the utterance — are
# preserved).
_QUOTE_PAIRS = (
    ("\u201c", "\u201d"),  # curly double
    ("\u300c", "\u300d"),  # corner
    ("\u300e", "\u300f"),  # double corner
    ("\u2018", "\u2019"),  # curly single
    ("\u0022", "\u0022"),  # ASCII double
    ("\u0027", "\u0027"),  # ASCII single
)

# All quote characters (the full inventory, both halves of every pair above) — a
# text containing ANY of them is not "quote-free". Hand-written quote chars are
# unreliable: \uXXXX escapes only (the same lesson the fidelity quote patterns use).
_QUOTE_CHARS = frozenset(
    "\u0022\u0027\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f"  # the 10 quote chars: ascii + curly + corner (both halves of every pair)
)


def strip_outer_quotes(text: str) -> str | None:
    """The text with its ONE outermost pair of quotation marks removed, or ``None`` when
    the text is not wrapped in a matching outer pair (the caller then leaves it alone).

    Only the outermost pair is stripped; an empty interior (``“”``) also yields ``None``
    so a degenerate quote-only text is never reduced to an empty entry.
    """
    if not text or len(text) < 2:
        return None
    for open_q, close_q in _QUOTE_PAIRS:
        if text.startswith(open_q) and text.endswith(close_q):
            inner = text[len(open_q):-len(close_q)]
            return inner if inner else None
    return None


def build_roster(entries: list) -> list[str]:
    """The book-wide character roster: sorted, de-duplicated, non-empty, non-NARRATOR
    ``speaker`` values over the WHOLE input file.

    A re-judged speaker may name any roster character — not just those present in the
    local batch window (which would wrongly reject a character whose first appearance
    sits outside the window). Computed deterministically from the input, never from the
    LLM; mirrors the 解析 stage's roster injection.
    """
    roster = set()
    for e in entries:
        sp = (e.get("speaker") or "").strip()
        if sp and sp != "NARRATOR":
            roster.add(sp)
    return sorted(roster)


def build_batch_window(entries: list, start: int, size: int, n: int, skip=None) -> list[dict]:
    """The context window for a batch of ``size`` target entries starting at ``start``.

    The target range ``entries[start .. start+size)`` (clamped to the file) is flagged
    ``"target": true``; ``n`` entries before ``start`` and ``n`` after the block (clamped)
    are context only (no target flag). Each item carries its absolute ``index``, the
    ORIGINAL ``speaker``, and the ``text`` (``instruct`` is dropped to keep the window
    compact — speaker judgment does not need the voice direction). Yields the union, in
    index order, so the LLM sees the full scope of what it may reason from.

    ``skip`` (optional) is a set of absolute indices inside the target range that must
    NOT be flagged as targets (the 归属抽样 passes its in-span non-targets, which ride
    along unflagged like context); they still appear in the window unflagged. ``None``
    (the default) flags the whole range.
    """
    total = len(entries)
    t_lo = max(0, start)
    t_hi = min(total, start + size)
    lo = max(0, start - n)
    hi = min(total, start + size + n)
    skipped = frozenset(skip) if skip is not None else frozenset()
    out = []
    for j in range(lo, hi):
        item = {
            "index": j,
            "speaker": entries[j].get("speaker", ""),
            "text": entries[j].get("text", ""),
        }
        if t_lo <= j < t_hi and j not in skipped:
            item["target"] = True
        out.append(item)
    return out


def _strip_thinking(text: str) -> str:
    """Drop thinking/reasoning tags (a Qwen3 thinking model may leak them into content)."""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    text = re.sub(r"<think>[\s\S]*$", "", text)
    for tag in ("thinking", "reflection", "reasoning"):
        text = re.sub(rf"<{tag}>[\s\S]*?</{tag}>", "", text)
        text = re.sub(rf"<{tag}>[\s\S]*$", "", text)
    return text


def _extract_balanced(text: str, start: int, opener: str, closer: str):
    """Parse the JSON value that begins at ``text[start]`` (an ``opener``), string/escape
    aware, counting only ``opener``/``closer`` depth. Returns the parsed value or ``None``.
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:  # noqa: BLE001
                    return None
    return None


def _extract_json(text: str):
    """The first top-level JSON value (object or array) in ``text``, else ``None``.

    Whichever bracket opens first wins, so a bare ``[...]`` reply is not mistaken for the
    ``{...}`` object nested inside it.
    """
    o = text.find("{")
    a = text.find("[")
    if o == -1 and a == -1:
        return None
    if a != -1 and (o == -1 or a < o):
        return _extract_balanced(text, a, "[", "]")
    return _extract_balanced(text, o, "{", "}")


def _clean_reply(text: str) -> str:
    """Strip markdown code fences and thinking/reasoning tags from an LLM reply."""
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1).strip()
    return _strip_thinking(text).strip()


def parse_speaker(text: str | None) -> str | None:
    """Extract the re-judged speaker from a single-entry LLM response; ``None`` if unreadable.

    A ``None`` result means "keep the entry's original speaker" (the caller's safe
    default). Order: strip thinking tags / code fences → read a JSON object's ``speaker``
    (or a one-element string array); if no object, accept a single short bare token (a
    lone word with no spaces, not shaped like JSON) so a model that replies ``ELENA``
    instead of ``{"speaker": "ELENA"}`` still lands correctly. Anything else → ``None``.
    """
    if not text:
        return None
    text = _clean_reply(text.strip())

    value = _extract_json(text)
    if isinstance(value, dict):
        sp = value.get("speaker")
        if isinstance(sp, str) and sp.strip():
            return sp.strip()
        return None  # an object was present but had no usable speaker → don't guess
    if isinstance(value, list) and len(value) == 1:
        if isinstance(value[0], str) and value[0].strip():
            return value[0].strip()

    candidate = text.strip()
    if (
        candidate
        and " " not in candidate
        and len(candidate) <= 32
        and not candidate.startswith(("[", "{"))
    ):
        return candidate
    return None


def _as_int(v):
    """``int(v)`` when ``v`` is an int or an int-like string, else ``None``."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_speaker_map_full(text: str | None, target_indices: list) -> dict:
    """Parse a batch LLM reply into ``{absolute_index: (speaker, text)}``.

    Robust to thinking tags / code fences and to the reply being a wrapped
    ``{"results": [...]}`` object, a bare ``[...]`` array, or an index-keyed object.
    Returns ``{}`` for anything unreadable — the caller then keeps the original speaker
    for those entries (per-item isolation). ``text`` is the reply item's optional
    ``text`` key — the re-judgment prompt asks for the entry's stored text with its
    surrounding quotation marks removed (applied only after strict validation) —
    ``None`` when the item omits the key or it is blank. A single-target batch falls
    back to :func:`parse_speaker`.
    """
    targets = list(target_indices)
    if not targets or not text:
        return {}
    text = _clean_reply(text.strip())
    tset = set(targets)
    # Primary: the batch mapping ({"results": [...]}, a bare array, or an index-keyed object).
    value = _extract_json(text)
    if value is not None:
        m = _map_from_value(value, tset, targets)
        if m:
            return m
    # Single-target fallback: the scalar parser, so a reply shaped for one entry
    # ({"speaker": ...}, a bare token, or a one-element string array) still lands.
    if len(targets) == 1:
        sp = parse_speaker(text)
        if sp:
            return {targets[0]: (sp, None)}
    return {}


def _map_from_value(v, tset: set, targets: list) -> dict:
    """Extract ``{index: (speaker, text)}`` from a parsed JSON value (object or array);
    ``text`` is ``None`` whenever the reply item carries no usable ``text`` key."""
    if isinstance(v, dict):
        # {"results" / "speakers" / ... : [ ... ]}
        for key in ("results", "speakers", "entries", "items", "list"):
            inner = v.get(key)
            if isinstance(inner, list):
                m = _map_from_list(inner, tset, targets)
                if m:
                    return m
        # index-keyed object: {"0": "NARRATOR", "3": "BOB"} (speaker-only values)
        m = {}
        for k, sp in v.items():
            idx = _as_int(k)
            if isinstance(sp, str) and sp.strip() and idx is not None and idx in tset:
                m[idx] = (sp.strip(), None)
        return m
    if isinstance(v, list):
        return _map_from_list(v, tset, targets)
    return {}


def _map_from_list(lst: list, tset: set, targets: list) -> dict:
    if not lst:
        return {}
    # form A: [{"index": .., "speaker": .., "text": ..?}, ...]
    if all(isinstance(x, dict) for x in lst):
        m = {}
        for x in lst:
            idx = _as_int(x.get("index"))
            sp = x.get("speaker")
            if idx is not None and isinstance(sp, str) and sp.strip() and idx in tset:
                tx = x.get("text")
                m[idx] = (sp.strip(), tx.strip() if isinstance(tx, str) and tx.strip() else None)
        if m:
            return m
    # form B: ["SPEAKER", ...] in target order
    if all(isinstance(x, str) for x in lst):
        m = {}
        for i, sp in enumerate(lst):
            if i < len(targets) and sp.strip():
                m[targets[i]] = (sp.strip(), None)
        if m:
            return m
    return {}


def _pick_majority(candidates):
    """The unique speaker holding a strict majority (≥2 votes) of ``candidates``, else ``None``.

    ``None`` entries (a re-run that failed to return a speaker for an entry) are ignored;
    a tie at the top — or fewer than 2 votes for the leader — yields ``None`` so the caller
    keeps the original speaker rather than guessing.
    """
    counts: dict = {}
    for c in candidates:
        if c:
            counts[c] = counts.get(c, 0) + 1
    if not counts:
        return None
    top = max(counts.values())
    if top < 2:
        return None
    leaders = [sp for sp, c in counts.items() if c == top]
    return leaders[0] if len(leaders) == 1 else None


def group_retry_indices(failed: list[int], n: int, batch: int) -> list[list[int]]:
    """Group target indices that need one more LLM call per group (retry passes, and the
    归属抽样's initial grouping of close-together targets).

    Indices are walked in ascending order. A group keeps growing while the gap to the next
    index is ≤ ``n`` (the two entries' ±n context windows overlap, so one call covers
    both) and it holds at most ``batch`` targets — otherwise a new group starts, so one
    call never spans a huge run of irrelevant entries between two far-apart entries.
    """
    groups: list[list[int]] = []
    cur: list[int] = []
    for i in sorted(failed):
        if cur and (i - cur[-1] > max(n, 1) or len(cur) >= batch):
            groups.append(cur)
            cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    return groups


def _llm_call(llm: LLMConfig, generation: GenerationConfig, messages, handle=None) -> str:
    """One chat-completion against the configured LLM (streaming or not); returns the text.

    Shares the parse pipeline's transport (which already handles a Qwen3 thinking model's
    ``reasoning_content``) and its sampling params, so the in-parse re-judgment stages make
    the exact same kind of call the parse does — only the prompt differs.
    """
    if llm.stream:
        content, _fr, _usage = _llm_chat_completion_stream(
            llm.base_url, llm.api_key, llm.model_name, messages,
            temperature=generation.temperature, top_p=generation.top_p,
            presence_penalty=generation.presence_penalty,
            max_tokens=generation.max_tokens, top_k=generation.top_k,
            min_p=generation.min_p, banned_tokens=generation.banned_tokens,
            handle=handle,
        )
    else:
        content, _fr, _usage = _llm_chat_completion(
            llm.base_url, llm.api_key, llm.model_name, messages,
            temperature=generation.temperature, top_p=generation.top_p,
            presence_penalty=generation.presence_penalty,
            max_tokens=generation.max_tokens, top_k=generation.top_k,
            min_p=generation.min_p, banned_tokens=generation.banned_tokens,
        )
    return content


def _batch_user_prompt(template: str, context: str, size: int, n: int,
                       roster: list[str] | None = None) -> str:
    """Fill the user template's ``{context}`` and append the roster + context-scope notes.

    ``replace`` (not ``format``) keeps a user-edited template containing other braces from
    raising. The appended notes carry the book-wide character roster (a corrected speaker
    may name a character absent from the local window; omitted when the roster is empty)
    and the target count with the ``±n`` context scope, so the model knows exactly what it
    may copy from and reason over.
    """
    body = template.replace("{context}", context)
    extra = []
    if roster:
        extra.append("【本书角色】" + "、".join(roster))
    extra.append(
        f"【上下文范围】上方窗口含 {size} 个 target=true 的目标条目（请逐一给出 speaker），"
        f"目标块前后各有 {n} 条未标记 target 的上下文条目，仅供理解、不得改动。"
    )
    return body + "\n\n" + "\n".join(extra)


def revalidate_entry(handle, llm, generation, sys_prompt, usr_template, entry, context,
                     roster, stage: str = "断句校验") -> list | None:
    """Re-run the parse LLM on one flagged entry and resolve the re-derivation by
    majority vote — the 角色匹配检查 consensus rule: one first call plus two retries
    (three total), early-stopping the instant a strict majority is decided, and one
    further (4th) call only when the three still disagree (never guess).

    The entry text goes in the parse prompt's ``{chunk}`` slot and the pre-built
    context window in ``{context}``, so the model re-derives the entries exactly as the
    original parse did — same prompts, same transport (``_llm_call`` streams to the UI
    panel). Each reply is gated by :func:`_reparse_vote`; a failed call, an
    unparseable reply, or a gate failure contributes no vote. Returns the winning
    re-derived entries (the raw reply dicts), or ``None`` when no consensus forms —
    the caller then keeps the entry unchanged. ``stage`` = the log label（断句校验 /
    超长段落重切 等复用方传各自文案）.
    """
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user",
         "content": usr_template.format(context=context, chunk=entry.get("text") or "")},
    ]
    votes: list = []
    parts_by_sig: dict = {}

    def one_vote(attempt: int) -> None:
        handle.check()  # cooperative cancel / pause between validation calls
        handle.llm_rate(0, 0.0)  # reset the 吞吐 gauge for this call
        try:
            reply = _llm_call(llm, generation, messages, handle)
        except TaskCancelled:
            raise  # a cancel raised mid-stream must propagate, not be swallowed
        except Exception as e:  # noqa: BLE001 — a failed call contributes no vote
            handle.log(f"  {stage}第 {attempt} 次调用失败，本轮无票：{e}", "WARNING")
            return
        parts = _parse_entries_reply(reply)
        if not parts:
            handle.log(f"  {stage}第 {attempt} 次响应无法解析为条目数组，本轮无票", "WARNING")
            return
        sig = _reparse_vote(parts, entry, roster)
        if sig is None:
            handle.log(
                f"  {stage}第 {attempt} 次结果未通过忠实性校验"
                f"（文字无法拼回原文 / 角色不在花名册），本轮无票",
                "WARNING",
            )
            return
        from ..platform.quota import consume_llm_output
        consume_llm_output(reply, stage)
        votes.append(sig)
        parts_by_sig.setdefault(sig, parts)

    for attempt in range(1, 4):  # 基础一次 + 重试两次 = 合计 3 次
        one_vote(attempt)
        if _pick_majority(votes) is not None:
            break  # 多数已决（2:0 / 2:1）——剩余调用无法翻盘，省掉
    if _pick_majority(votes) is None:
        # 没决出来（1:1:1 或有效票不足）→ 再跑第四次
        handle.log(f"  {stage}3 次无共识 → 再跑第 4 次")
        handle.check()
        one_vote(4)
    winner = _pick_majority(votes)
    if winner is None:
        handle.log(f"  {stage}4 次仍无共识，条目保持原样", "WARNING")
        return None
    return parts_by_sig[winner]


def validate_sentence_splits(handle, llm, generation, sys_prompt, usr_template, entries,
                             context_window=4) -> tuple:
    """Post-parse sentence-split validation (runs in ``generate_file`` BEFORE the
    mechanical NARRATOR merge, so split-off narration parts merge with their
    neighbours as usual).

    Scans ``entries`` for wrapped utterances carrying a "…道：" tag inside
    (:func:`suspicious_entry_indices`); each is re-run through the parse LLM with a
    ±n context window (``build_batch_window``) and resolved by majority vote
    (:func:`revalidate_entry`). A win replaces the entry with its re-derived entries —
    a one-entry win is a clean rewrite (the wrap / tag stripped out), a multi-entry win
    a re-split. Every context window and the roster are built from the PRISTINE entry
    list up front, and wins are applied in DESCENDING index order, so one split never
    shifts an index a later window was built from. Cancel propagates; nothing is
    applied after a cancel (the file is written only after this step returns).

    Returns ``(entries, flagged, fixed)`` — the updated list (the original list object
    when nothing was applied), the count of flagged entries, the count rewritten.
    """
    flagged = suspicious_entry_indices(entries)
    if not flagged:
        # 零命中也留一行日志：静默退出会被用户误读成「这个阶段没跑/不在链路里」
        handle.log("断句校验：0 条疑似断句失败条目（无重判，零 LLM 调用）")
        return entries, 0, 0

    n = max(0, int(context_window))
    roster = frozenset({"NARRATOR", *build_roster(entries)})
    # All windows + context strings up front — from the pristine list.
    contexts = {}
    for i in flagged:
        window = build_batch_window(entries, i, 1, n)
        lines = [
            "(Re-check of one entry: the SOURCE TEXT below is a single entry's stored "
            "text that likely failed sentence splitting. Re-derive its entries exactly "
            "as if it were source text.)",
        ]
        if roster:
            lines.append("Characters in this book: " + ", ".join(sorted(roster - {"NARRATOR"})))
        before = [it for it in window if it["index"] < i]
        after = [it for it in window if it["index"] > i]
        if before:
            lines.append("Entries immediately before it (context only — never re-emit them):")
            lines.extend(json.dumps(it, ensure_ascii=False) for it in before)
        if after:
            lines.append("Entries immediately after it (context only — never re-emit them):")
            lines.extend(json.dumps(it, ensure_ascii=False) for it in after)
        contexts[i] = "\n".join(lines)

    handle.log(
        f"检测到 {len(flagged)} 条疑似断句失败条目（台词引号内含「…道：」标签），"
        f"逐条带上下文窗口（±{n} 条）重跑校验…"
    )
    # The mechanical check stages share the [0.9, 1.0) progress band (see
    # generate_file): re-validation owns [0.9, 0.98), the spot audit [0.98, 0.998).
    handle.progress(0.9, f"断句校验 {len(flagged)} 条")

    updated = None
    fixed = 0
    # Descending index order: applying a split never shifts the index an EARLIER
    # entry's (already built) window refers to.
    for seq, i in enumerate(sorted(flagged, reverse=True), 1):
        handle.check()
        handle.progress(0.9 + 0.08 * seq / len(flagged), f"断句校验 {seq}/{len(flagged)}")
        snippet = (entries[i].get("text") or "").replace("\n", " ")
        handle.log(f"条目 {i + 1}（疑似断句失败）：{snippet[:60]}{'…' if len(snippet) > 60 else ''}")
        parts = revalidate_entry(handle, llm, generation, sys_prompt, usr_template,
                                 entries[i], contexts[i], roster)
        if parts is None:
            continue  # 无共识 / 校验未过 → 条目保持原样
        if updated is None:
            updated = list(entries)  # the input list stays pristine until something applies
        updated[i:i + 1] = [
            {
                "speaker": (p.get("speaker") or "").strip(),
                "text": p.get("text") or "",
                "instruct": p.get("instruct") or "",
            }
            for p in parts
        ]
        fixed += 1
        if len(parts) > 1:
            speakers = "、".join(dict.fromkeys(
                (p.get("speaker") or "").strip() for p in parts))
            handle.log(f"条目 {i + 1} 校验通过：拆分为 {len(parts)} 条（{speakers}）")
        else:
            handle.log(f"条目 {i + 1} 校验通过：重写为单条（剥离包裹引号 / 语气标签）")
    return (updated if updated is not None else entries), len(flagged), fixed


INSTRUCT_MAX_WORDS = 35


def _parse_instruct_batch(reply: str, targets: list[int], max_words: int) -> dict[int, str]:
    """Parse a batch instruct response into validated ``index -> instruct`` values."""
    raw = clean_json_string(reply or "")
    if not raw:
        return {}
    rows = repair_json_array(raw)
    if not rows:
        try:
            value = json.loads(raw)
            rows = value if isinstance(value, list) else [value]
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    target_set = set(targets)
    result = {}
    for order, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        idx = row.get("index")
        try:
            idx = int(idx) if idx is not None else targets[order]
        except (TypeError, ValueError, IndexError):
            continue
        value = row.get("instruct")
        if idx not in target_set or not isinstance(value, str):
            continue
        value = value.strip()
        if value and instruct_word_count(value) < max_words:
            result[idx] = value
    return result


def _instruct_book_prompt(entries: list, targets: list[int], n: int, max_words: int) -> str:
    """Build one compact prompt for every flagged entry in the whole book."""
    target_set = set(targets)
    context_indices = set()
    for index in targets:
        start = max(0, index - n)
        end = min(len(entries), index + n + 1)
        context_indices.update(range(start, end))

    window = []
    for index in sorted(context_indices):
        entry = entries[index]
        row = {
            "index": index,
            "speaker": entry.get("speaker") or "",
            "text": entry.get("text") or "",
            "instruct": entry.get("instruct") or "",
        }
        if index in target_set:
            row["target"] = True
        window.append(row)
    target_rows = [item for item in window if item.get("target")]
    return (
        "You are repairing TTS voice directions for one complete book. Return exactly "
        "one JSON object per target: {\"index\": absolute_index, "
        "\"instruct\": concise_direction}. Keep every index, speaker, and text value "
        "unchanged; do not split, merge, delete, reorder, or repeat entries. Use "
        "nearby directions only for style continuity. "
        f"Each direction must be non-empty and shorter than {max_words} whitespace words.\n\n"
        "TARGETS (only these may be changed):\n"
        + json.dumps(target_rows, ensure_ascii=False, indent=2)
        + "\n\nCONTEXT (read-only):\n"
        + json.dumps(window, ensure_ascii=False, indent=2)
        + "\n\nSOURCE TEXT:\n"
        + "\n".join(item.get("text") or "" for item in target_rows)
    )


def _inherit_narrator_instructs(entries: list, flagged: list[int], max_words: int) -> tuple:
    """Repair flagged narrator entries from a nearby valid narrator direction.

    Only entries inside the same contiguous NARRATOR run are considered. This
    keeps narration style stable while avoiding an LLM request for mechanical
    omissions in a run that already has a usable direction.
    """
    updated = list(entries)
    flagged_set = set(flagged)
    inherited = {}
    for index in flagged:
        if str(entries[index].get("speaker") or "").strip().upper() != "NARRATOR":
            continue

        left = index
        while left > 0 and str(entries[left - 1].get("speaker") or "").strip().upper() == "NARRATOR":
            left -= 1
        right = index
        while right + 1 < len(entries) and str(entries[right + 1].get("speaker") or "").strip().upper() == "NARRATOR":
            right += 1

        candidates = []
        for candidate in range(left, right + 1):
            if candidate in flagged_set:
                continue
            value = str(entries[candidate].get("instruct") or "").strip()
            if value and instruct_word_count(value) < max_words:
                candidates.append((abs(candidate - index), candidate, value))
        if not candidates:
            continue
        _, _, value = min(candidates, key=lambda item: (item[0], item[1]))
        updated[index] = dict(updated[index])
        updated[index]["instruct"] = value
        inherited[index] = value
    return updated, inherited


def _validate_instructs_one_call(handle, llm, generation, sys_prompt, usr_template, entries,
                                 context_window=4, max_words=INSTRUCT_MAX_WORDS) -> tuple:
    """Mechanically inherit narration directions, then repair remaining targets once."""
    max_words = max(1, int(max_words))
    flagged = instruct_entry_indices(entries, max_words)
    if not flagged:
        return entries, 0, 0

    updated, inherited = _inherit_narrator_instructs(entries, flagged, max_words)
    targets = [index for index in flagged if index not in inherited]
    replacement = {}
    if targets:
        n = max(0, int(context_window))
        handle.log(f"instruct flags={len(flagged)}; narrator inherited={len(inherited)}; LLM targets={len(targets)}")
        handle.progress(0.94, f"instruct targets={len(targets)}")
        handle.check()
        messages = [
            {"role": "system", "content": (
                "Repair only the instruct field of target entries. Output JSON only. "
                "Return one object for every target index. Never alter index, speaker, or text."
            )},
            {"role": "user", "content": _instruct_book_prompt(updated, targets, n, max_words)},
        ]
        handle.llm_rate(0, 0.0)
        try:
            reply = _llm_call(llm, generation, messages, handle)
        except TaskCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            handle.log(f"instruct batch call failed: {e}", "WARNING")
            return updated, len(flagged), len(inherited)
        replacement = _parse_instruct_batch(reply, targets, max_words)
        for index, value in replacement.items():
            updated[index] = dict(updated[index])
            updated[index]["instruct"] = value

    fixed = len(inherited) + sum(
        1 for index, value in replacement.items()
        if value != (entries[index].get("instruct") or "")
    )
    handle.log(
        f"instruct repaired={fixed}; narrator inherited={len(inherited)}; "
        f"LLM requests={1 if targets else 0}"
    )
    return updated, len(flagged), fixed


def instruct_word_count(value) -> int:
    """Count whitespace-delimited words in an ``instruct`` value."""
    return len(str(value or "").strip().split())


def instruct_entry_indices(entries: list, max_words: int = INSTRUCT_MAX_WORDS) -> list[int]:
    """Return entries whose voice direction is missing or at least ``max_words`` long."""
    limit = max(1, int(max_words))
    return [
        i for i, e in enumerate(entries)
        if isinstance(e, dict)
        and (not str(e.get("instruct") or "").strip()
             or instruct_word_count(e.get("instruct")) >= limit)
    ]


# ---------------------------------------------------------------------------
# 超长段落检查（解析内阶段 A，两半：LLM 重切 + 机械分段兜底）
#
# 前半 ``long_paragraph_resplit``：超过 ``max_paragraph_chars`` 字的条目是「文本切割
# 失败」的嫌疑（旁白与台词合并成一条 / 多段叙述未拆开）→ 带上下文窗口重跑解析 LLM，
# 经共享重判批协议（:func:`revalidate_entry`）严格多数裁决——忠实性门要求多段需
# ≥2 个不同 speaker，故**同一说话人的长篇独白「拆分」无票**，超长会留到后半兜底
# （分工是特性：LLM 管语义边界，机械保证长度上界）。
# 后半 ``split_long_entries``：无论 LLM 改过与否，仍超长的条目确定性切分——
# 三级边界逐级放宽（句末 → 子句界 → 定宽硬切），切点要求引号安全（防悬空引号被
# TTS 念出），硬保证最终没有任何条目超过 ``max_paragraph_chars``
# （硬约束 > 「不拦腰截句」，硬切是最后一级）。
# ---------------------------------------------------------------------------


def long_entry_indices(entries: list, max_chars: int) -> list[int]:
    """整条 text（strip 后 Unicode 码点数）超过 ``max_chars`` 的条目下标。"""
    return [
        i for i, e in enumerate(entries)
        if isinstance(e, dict) and len((e.get("text") or "").strip()) > max_chars
    ]


validate_instructs = _validate_instructs_one_call


def long_paragraph_resplit(handle, llm, generation, sys_prompt, usr_template, entries,
                           max_chars, context_window=4) -> tuple:
    """超长条目的 LLM 重切（解析内，纯归属标签删除之后、归属抽样之前——重切可能
    产生新归属的台词条目，需要被抽样审计；受 ``generation.check_long_paragraphs``
    门控，由 ``generate_file`` 判断）。

    每条超过 ``max_chars`` 字的条目重跑解析 LLM（±n 上下文窗口，与断句失败校验
    同一形态），经 :func:`revalidate_entry`（stage = 超长段落重切）严格多数裁决：
    胜出整体替换条目——单条胜出 = 干净重写（条目可能仍超长，由机械分段兜底），
    多条胜出 = 语义重切；4 次无共识保留原条目（**从不猜**）。全部窗口 + 花名册
    按**原始**条目列表预建，胜出者按**降序下标**应用（重切 1→N 不移动更小下标
    条目的窗口）。取消立即上抛、不落盘任何文件（基文件在全部阶段返回后才写）。

    返回 ``(entries, checked, fixed)`` —— 更新后的列表（无修改时原列表对象）、
    送 LLM 的超长条目数、被替换的条目数。
    """
    max_chars = max(10, int(max_chars))
    flagged = long_entry_indices(entries, max_chars)
    if not flagged:
        # 零命中也留一行日志（与其余阶段同一纪律：静默退出会被误读成阶段缺失）
        handle.log(f"超长段落检查：0 条超过 {max_chars} 字条目（无 LLM 重切，零 LLM 调用）")
        return entries, 0, 0

    n = max(0, int(context_window))
    roster = frozenset({"NARRATOR", *build_roster(entries)})
    # All windows + context strings up front — from the pristine list.
    contexts = {}
    for i in flagged:
        window = build_batch_window(entries, i, 1, n)
        lines = [
            "(Re-check of one entry: the SOURCE TEXT below is a single entry's stored "
            f"text that is LONGER than {max_chars} characters — likely a failed split "
            "that left narration and dialogue (or several paragraphs) in one entry. "
            "Re-derive its entries exactly as if it were source text, splitting at "
            "every utterance / scene boundary.)",
        ]
        if roster:
            lines.append("Characters in this book: " + ", ".join(sorted(roster - {"NARRATOR"})))
        before = [it for it in window if it["index"] < i]
        after = [it for it in window if it["index"] > i]
        if before:
            lines.append("Entries immediately before it (context only — never re-emit them):")
            lines.extend(json.dumps(it, ensure_ascii=False) for it in before)
        if after:
            lines.append("Entries immediately after it (context only — never re-emit them):")
            lines.extend(json.dumps(it, ensure_ascii=False) for it in after)
        contexts[i] = "\n".join(lines)

    handle.log(
        f"超长段落检查：{len(flagged)} 条超过 {max_chars} 字条目，"
        f"逐条带上下文窗口（±{n} 条）重跑 LLM 重切…"
    )
    # The mechanical check stages share the [0.9, 1.0) progress band (see
    # generate_file): this stage shares the [0.96, 0.98) band with
    # sentence-split validation (runs after it in time, no overlap).
    updated = None
    fixed = 0
    # Descending index order: applying a re-split (1 → N entries) never shifts the
    # index a LATER (lower) entry's already-built window refers to.
    for seq, i in enumerate(sorted(flagged, reverse=True), 1):
        handle.check()
        handle.progress(0.96 + 0.02 * seq / len(flagged), f"超长段落重切 {seq}/{len(flagged)}")
        snippet = (entries[i].get("text") or "").replace("\n", " ")
        text_len = len((entries[i].get("text") or "").strip())
        handle.log(f"条目 {i + 1}（超长 {text_len} 字）：{snippet[:40]}{'…' if len(snippet) > 40 else ''}")
        parts = revalidate_entry(handle, llm, generation, sys_prompt, usr_template,
                                 entries[i], contexts[i], roster, stage="超长段落重切")
        if parts is None:
            continue  # 无共识 / 未过忠实性门 → 条目保持原样（机械分段兜底）
        if updated is None:
            updated = list(entries)
        updated[i:i + 1] = [
            {
                "speaker": (p.get("speaker") or "").strip(),
                "text": p.get("text") or "",
                "instruct": p.get("instruct") or "",
            }
            for p in parts
        ]
        fixed += 1
        if len(parts) > 1:
            speakers = "、".join(dict.fromkeys(
                (p.get("speaker") or "").strip() for p in parts))
            handle.log(f"条目 {i + 1} 重切通过：拆分为 {len(parts)} 条（{speakers}）")
        else:
            handle.log(f"条目 {i + 1} 重切通过：重写为单条（仍超长则由机械分段兜底）")
    return (updated if updated is not None else entries), len(flagged), fixed


# --- 机械分段兜底（确定性，零 LLM 成本） ---

_SENT_END_RE = re.compile(r"(?<=[。！？!?…])|(?<=[.!?])(?=\s)")
_CLAUSE_END_RE = re.compile(r"(?<=[，、；：,;:])")
_OPEN_QUOTE_CHARS = frozenset(op for op, _cl in _QUOTE_PAIRS)
_CLOSE_QUOTE_CHARS = frozenset(cl for _op, cl in _QUOTE_PAIRS)


def _quote_parity(text: str) -> list:
    """引号奇偶前缀：``par[i]`` = ``text[:i]`` 内开引号数 − 闭引号数。

    切点 ``i``（相对起点 ``start``）引号安全 = ``par[i] == par[start]``——切点前的
    段不以悬空开引号结尾（悬空开引号留在段尾会被 TTS 照念或读成转义）。
    """
    par = [0] * (len(text) + 1)
    for i, ch in enumerate(text):
        d = 0
        if ch in _OPEN_QUOTE_CHARS:
            d = 1
        elif ch in _CLOSE_QUOTE_CHARS:
            d = -1
        par[i + 1] = par[i] + d
    return par


def split_long_text(text: str, max_chars: int) -> list[str]:
    """把超长 ``text`` 机械切成若干段，每段 strip 后 ≤ ``max_chars``（Unicode 码点数）。

    三级边界逐级放宽（只在起点后 ``max_chars`` 字内找切点，取**最接近目标宽度**
    ``start + max_chars`` 的合法候选、平手取较小偏移——段尽量填满上限）：

    ① 句末（CJK ``[。！？!?…]`` 零宽 / ASCII ``[.!?]`` 须后随空白——与
       ``split_into_chunks`` 同一口径，保护 ``3.14`` / ``e.g.``）
    ② 子句界（``[，、；：,;:]``）
    ③ 定宽硬切（最后手段：「绝不出现超长条目」的硬保证高于「不拦腰截句」——
       切点先查 ±20 字窗内的引号安全位，找不到才裸定宽切）

    ①② 的切点要求**引号安全**（引号奇偶与起点一致，见 :func:`_quote_parity`），
    不安全则让位下一级。

    不变量：各段去空白拼接骨架 == 原文骨架（无损）；每段 ≤ ``max_chars``；
    切点严格前进、必然终止。短文本（≤ ``max_chars``）原样单段返回。
    ``max_chars ≤ 0`` 钳 10（退化值无意义）。
    """
    max_chars = max(10, int(max_chars))
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    par = _quote_parity(text)
    out: list[str] = []
    start = 0
    while True:
        rem = text[start:]
        if len(rem.strip()) <= max_chars:
            out.append(rem.strip())
            break
        # 前 max_chars 字内找**最接近目标宽度**（start + max_chars）的合法切点——
        # 段尽量填满上限（平手取较小偏移，与 split_into_chunks 同口径）；tier 内
        # 无任何候选才让位下一 tier。
        target = start + max_chars
        best = None
        for tier in (_SENT_END_RE, _CLAUSE_END_RE):
            cands = []
            for m in tier.finditer(text):
                b = m.end()
                if b <= start:
                    continue
                if b - start > max_chars:
                    break
                if par[b] != par[start]:
                    continue  # 引号不安全 → 跳过该候选
                if not text[start:b].strip() or not text[b:].strip():
                    continue
                cands.append(b)
            if cands:
                best = min(cands, key=lambda b: (abs(b - target), b))
                break
        if best is not None:
            cut = best
        else:
            # 定宽硬切：±20 字窗内找引号安全位（取离定宽位最近者），找不到裸切。
            cut = start + max_chars
            lo, hi = max(start + 1, cut - 20), min(len(text), cut + 20)
            safe = [p for p in range(lo, hi + 1) if par[p] == par[start]]
            if safe:
                cut = min(safe, key=lambda p: (abs(p - cut), p > cut))
        piece = text[start:cut].strip()
        if not piece:  # 退化：定宽窗内全空白（实际语料不会出现）→ 推到首字
            while cut < len(text) and not text[start:cut].strip():
                cut += 1
            piece = text[start:cut].strip()
        out.append(piece)
        start = cut
    return out


def split_long_entries(entries: list, max_chars: int, title_test) -> tuple:
    """超长条目的机械分段（确定性兜底，解析内阶段 A 的后半，归属抽样之后——
    speaker 已定稿，切段只继承父条目的 speaker / instruct；受
    ``generation.check_long_paragraphs`` 门控，由 ``generate_file`` 判断）。

    对每条仍超过 ``max_chars`` 的条目调 :func:`split_long_text`：speaker /
    instruct 继承（instruct 只给首段——声音指导随段重复会重复朗读），**非级联**
    单遍（切出的段恒 ≤ ``max_chars``，不会再触发）。

    与 ``merge_adjacent_same_speaker``（词字符 ≤100 / ≤10 强制合并）的交互：同人合并在
    本阶段**之前**运行，其 ≤10 强制合并可能造出超过 ``max_chars``（默认 200）的同人块
    ——本阶段是管线**末段**，负责把这类超限块切回 ≤ ``max_chars``。切出的段绝不会被
    回粘（本阶段是最后一步，其后无合并）。硬保证：返回后没有任何条目（strip 后）
    超过 ``max_chars``。

    返回 ``(entries, split_count)`` —— 更新后的列表（无修改时原列表对象）、
    被切分的条目数（切出的段总数 = 原条目数 + 各段增量，不在返回值里——日志带
    逐条明细）。
    """
    max_chars = max(10, int(max_chars))
    flagged = long_entry_indices(entries, max_chars)
    if not flagged:
        # 零命中也留一行日志（本半在 generate_file 的日志行之后，此处不重复）
        return entries, 0
    updated = list(entries)
    split_count = 0
    for i in sorted(flagged):
        e = entries[i]
        parts = split_long_text(e.get("text") or "", max_chars)
        if len(parts) < 2:
            continue
        speaker = (e.get("speaker") or "").strip()
        instruct = e.get("instruct") or ""
        updated[i:i + 1] = [
            {"speaker": speaker, "text": p, "instruct": instruct if k == 0 else ""}
            for k, p in enumerate(parts)
        ]
        split_count += 1
    return updated, split_count


def absorb_punct_entries(entries: list, title_test) -> tuple:
    """纯标点 / 无内容条目的吸收（解析内阶段 B，确定性零 LLM 成本；受
    ``generation.absorb_punct_entries`` 门控，由 ``generate_file`` 判断）。

    整条无任何词字符的条目（独立「……」/「？」/「…………」等——文本筛查报告里的
    32 段纯标点 NARRATOR）对 TTS 只是一次停顿 + 两次换人停顿。处置：并入相邻
    NARRATOR 条目（前邻优先，无前邻则后邻；**标题守卫**——``title_test`` 命中的
    邻接条目不吸收：标题两侧是章节分界停顿）；无 NARRATOR 邻接 → 删除。
    单遍非级联（邻接按删除前列表判定，与纯归属标签删除同一保守口径）；text
    直接拼接（并入前邻 = 追加到其尾，并入后邻 = 置于其开头）。相邻的纯标点
    条目链（punct, punct, NARRATOR）逐跳并入同一目标、内容不丢失。

    返回 ``(entries, absorbed, deleted)`` —— 更新后的列表（无修改时原列表对象）、
    被并入的条目数、被删除的条目数。
    """
    flagged = [
        i for i, e in enumerate(entries)
        if isinstance(e, dict) and not _skeleton((e.get("text") or "").strip())
    ]
    if not flagged:
        return entries, 0, 0
    texts = {i: (entries[i].get("text") or "").strip() for i in range(len(entries))}
    removed: set = set()
    consumed: set = set()  # 被吸收成目标的 flagged 条目（留盘、不再处理，防链式丢文本）
    changed: dict = {}  # target index → 合并后的 text（可被多次吸收累加）
    absorbed = deleted = 0
    for i in sorted(flagged):
        if i in removed or i in consumed:
            continue
        t = texts[i]
        target = -1
        for nb in (i - 1, i + 1):
            if nb < 0 or nb >= len(entries) or nb in removed:
                continue
            nb_e = entries[nb]
            if not isinstance(nb_e, dict):
                continue
            if (nb_e.get("speaker") or "").strip() != "NARRATOR":
                continue
            if title_test((nb_e.get("text") or "").strip()):
                continue  # 标题两侧不吸收
            target = nb
            break
        if target < 0:
            removed.add(i)
            deleted += 1
            continue
        cur = changed.get(target, texts[target])
        changed[target] = (cur + t) if target == i - 1 else (t + cur)
        if target in flagged:
            # 目标本身也是纯标点条（尚未处理）：标记为已消费——它留在输出里承载
            # 合并文本，跳过其自身的 flagged 处理（防链式吸收用旧文本覆盖丢内容）。
            consumed.add(target)
        removed.add(i)
        absorbed += 1
    if not removed:
        return entries, 0, 0
    out = []
    for i, e in enumerate(entries):
        if i in removed:
            continue
        if i in changed:
            e = {**e, "text": changed[i]}
        out.append(e)
    return out, absorbed, deleted


# ---------------------------------------------------------------------------
# 角色匹配检查（chunk 边界 speaker 重判；解析内四检查阶段之首）
#
# Chunk 切割切断了跨段上下文：解析边界条目的 LLM 看不到边界另一侧的对话 /
# 角色上下文，speaker 易误判（引语行紧邻切点、其归属角色只出现在另一侧时）。
# 本阶段用跨边界窗口 [前置上下文]+[检查目标]+[后置上下文] 重判每个**内部**
# chunk 边界两侧各 n 条（n = check_context_window，与其余重判阶段同一配置）；
# 上下文仅提供判断依据——**只有 target 条目可被修改**（硬不变式，见
# _run_rejudge_groups 的三层保证）。判定复用重判批协议（3 样本严格多数、
# 无共识继续取样本，从不猜）。首/末 chunk 边界不检查：首/末段解析时 LLM 明确
# 知道 Beginning / End of text，没有上下文被"切掉"。受
# ``generation.check_boundary_speakers`` 门控（默认开；关 = generate_file 跳过
# + 一行日志 + 结果字段 0）。本阶段居检查段最前：断句校验（拆条）与标签删除
# （删条）都会移动条目下标，本阶段的窗口必须取自 pristine 列表。
# 不建历史文件：本阶段无仪表语义（仪表读数只属归属抽样的纯随机桶）。
# ---------------------------------------------------------------------------

def select_boundary_targets(chunk_ends: list, n: int, total: int) -> list:
    """角色匹配检查目标：每个内部 chunk 边界两侧各 n 条（全局去重 → 升序）。

    ``chunk_ends`` = 每段结束时的累计条目数（第 k 段末 = ``chunk_ends[k-1]``）；
    边界 b = ``chunk_ends[k]``（后一段的首条目下标）→ 两侧各 n 条 =
    ``[b-n, b+n)``。文件头/尾边界不检查（首/末段解析无上下文被切掉——故障
    模式只存在于内部边界）。n=0 → 空（阶段零 LLM 调用）。
    """
    targets = set()
    for b in chunk_ends[:-1]:  # 内部边界（首/尾不查）
        for t in range(b - n, b + n):
            if 0 <= t < total:
                targets.add(t)
    return sorted(targets)


def select_boundary_risk_targets(entries: list, chunk_ends: list, n: int) -> list:
    """Return boundary windows only when local evidence makes them risky.

    Clean same-speaker boundaries with complete sentences do not spend an LLM
    call. Speaker turns, attribution tags, short responses, unfinished sentences,
    and quote-parity crossings activate the small window around a boundary.
    """
    if n <= 0 or not entries:
        return []
    total = len(entries)
    targets = set()
    for b in chunk_ends[:-1]:
        if not (0 < b < total):
            continue
        lo = max(0, b - n)
        hi = min(total, b + n)
        left = entries[b - 1]
        right = entries[b]
        risky = (left.get("speaker") or "") != (right.get("speaker") or "")
        local = entries[lo:hi]
        if any(_has_attribution_tag(
                e.get("text") or "",
                entries[j - 1].get("text")
                if j > 0 and entries[j - 1].get("speaker") == "NARRATOR" else None,
        ) for j, e in enumerate(local, lo)):
            risky = True
        if _quote_parity("".join((e.get("text") or "") for e in local))[-1] != 0:
            risky = True
        if risky:
            targets.update(range(lo, hi))
    return sorted(targets)


def boundary_check_speakers(handle, llm: LLMConfig, generation: GenerationConfig,
                            entries: list, chunk_ends: list) -> tuple:
    """角色匹配检查阶段（解析内四阶段之首；调用方负责开关门控）。

    用跨边界窗口重判每个内部 chunk 边界两侧各 n 条（n =
    ``check_context_window``）的 speaker——只改 target（context 条目逐字节
    不动），判定 = 共享重判批协议的 3 样本严格多数；高置信改判在内存中生效、
    随解析任务自己的基文件写出（基文件本就是解析任务的产物）。

    返回 ``(entries, {checked, fixed})``——未应用任何修正时返回**原始列表
    对象**（对象身份由测试钉死）；零目标 / 单段文件 → 零 LLM 调用 + 一行
    日志（防静默被误读为阶段缺失）。
    """
    from . import check_prompts  # bundled re-judgment prompt defaults

    stats = {"checked": 0, "fixed": 0}
    if not entries or len(chunk_ends) < 2:
        # 单段文件无内部边界 → 无事可做（静默即可：不是"阶段缺失"）。
        return entries, stats

    n = max(0, int(generation.check_context_window or 0))
    batch = max(1, int(generation.check_batch_size or 0))
    candidates = select_boundary_targets(chunk_ends, n, len(entries))
    targets = select_boundary_risk_targets(entries, chunk_ends, n)
    if not targets:
        # 零命中也留一行日志：与断句校验同一理由——静默退出会被误读成阶段缺失。
        handle.log("角色匹配检查：0 条边界目标（无重判，零 LLM 调用）")
        return entries, stats

    original = entries  # 窗口恒由 pristine 列表预建（硬不变式）
    roster = build_roster(original)
    sys_prompt = check_prompts.DEFAULT_CHECK_SYSTEM_PROMPT
    usr_template = check_prompts.DEFAULT_CHECK_USER_PROMPT
    groups = group_retry_indices(targets, n, batch)

    stats["checked"] = len(targets)
    handle.log(
        f"角色匹配检查：{len(targets)} 条边界条目（内部 chunk 边界两侧各 {n} 条，"
        f"共 {len(groups)} 组），复用重判批协议…"
    )

    result, rep = _run_rejudge_groups(
        handle, llm, generation, sys_prompt, usr_template,
        original, groups, n, roster,
        stage="角色匹配检查", progress_base=0.9, progress_span=0.06,
    )
    stats["fixed"] = rep["fixed"]
    if rep["fixed"] or rep["unwrapped"]:
        handle.log(
            f"角色匹配检查完成：重判 {stats['checked']} 条边界条目，更正 {rep['fixed']} 条 speaker"
            + (f"、{rep['unwrapped']} 条台词去引号" if rep["unwrapped"] else "")
        )
    return result, stats


# ---------------------------------------------------------------------------
# 归属抽样（解析后的条目级 speaker 抽查）
#
# 所有 chunk 解析完后，按 ``generation.spot_check_rate``（0 = 关闭）从**全部**条目
# 抽一小部分重判 speaker（NARRATOR 也在池内——「对白藏在旁白里」是真实错误方向，
# 检查提示词本就支持双向改判），高置信改判就地生效、随基文件写出。预算分两个
# **不相交**的桶：~1/3 纯随机（全条目均匀——整书错误率的无偏「仪表」，唯一可据以
# 判断「采样率能不能降」的读数）+ ~2/3 风险加权（按命中风险特征数 3→2→1→0 级联
# 抽取，tier 内均匀随机；tier 0 = 未抽中剩余，预算恒用满）。风险特征（代码可判）：
# ① 无显式归属标签——条目自身及紧邻前一条（仅当其为 NARRATOR）里没有
#    「2~3 字 CJK 人名 + 说/道/问/喊/答」或「他/她 + 五动词」；「道」前一字符为
#    知/难 时是 知道/难道 而非标签（形态守卫，不是词表黑名单）；
# ② 极短条目 len(text.strip()) ≤ 10（「嗯」/「好」式单行应答）；
# ③ 多角色场景——±10 条窗口内 ≥4 个不同非 NARRATOR 说话人。
# 重判走共享的重判批协议（首判 → 分歧条目动态多数投票：票 =
# [原值, 首判] + 至多 3 次同窗口重试，逐条一出严格多数即停，3 次后仍无共识 →
# 保留原值）；只允许 speaker 变化 + 台词确定性去外层引号。每本的纯随机桶读数
# 记入任务日志 + ``<workspace>/config/spot_check_history.json``——采样率永不自动
# 降，由用户在设置页按读数手动决定。
# ---------------------------------------------------------------------------

_SPOT_HISTORY_LOCK = threading.RLock()
SPOT_CHECK_HISTORY_NAME = "spot_check_history.json"
SPOT_CHECK_HISTORY_CAP = 50  # 历史保留最近 N 本（更早的丢弃）
SPOT_CHECK_SHORT_MAX = 10  # 特征②：极短台词（strip 后 ≤ 该字数）
SPOT_CHECK_MULTI_MIN = 4  # 特征③：窗口内不同非 NARRATOR 说话人 ≥ 该数
SPOT_CHECK_MULTI_WINDOW = 10  # 特征③：以条目为中心的 ±N 条窗口
_SAY_VERBS = "说道问答喊"  # 五归属动词（普通字符串，Python 直接解码）
_NONSPEAK_BEFORE_DAO = "知难"  # 「道」前一字符属此集 → 知道/难道，不是标签（形态守卫）
# 「2~3 字 CJK 人名 + 动词」归属标签（人名组贪婪，见 _tag_in 的 知/难 形态守卫）。
# 正则里的 \uXXXX 一律双反斜杠（既有约定）；五动词用已解码的普通字符串拼入。
_NAME_TAG_RE = re.compile("(?P<name>[\\u4e00-\\u9fff]{2,3})(?P<verb>[" + _SAY_VERBS + "])")
# 「他/她 + 动词」归属标签（他/她 是 CJK，按既有约定直接写）。
_PRONOUN_TAG_RE = re.compile("[他她][" + _SAY_VERBS + "]")


def _tag_in(text: str) -> bool:
    """Whether ``text`` carries an explicit attribution tag: a 2~3-char CJK name
    + 说/道/问/喊/答, or 他/她 + one of the five verbs.

    知道 / 难道 morphological guard (NOT a word-list blacklist): the name group is
    greedy, so in 林某知道 the regex swallows 知 into the name (name=林某知,
    verb=道) — the guard therefore inspects the char immediately BEFORE the 道,
    i.e. the name group's last char: in {知, 难} the hit is 知道/难道, skip it.
    """
    if not text:
        return False
    if _PRONOUN_TAG_RE.search(text):
        return True
    for m in _NAME_TAG_RE.finditer(text):
        name, verb = m.group("name"), m.group("verb")
        if verb == "道" and name[-1] in _NONSPEAK_BEFORE_DAO:
            continue  # 知道 / 难道 —— not a tag
        return True
    return False


def _has_attribution_tag(text: str, prev_text: str | None = None) -> bool:
    """Whether the entry text — or (a NARRATOR) preceding entry it follows — holds
    an explicit attribution tag (the tag may sit in the line before the dialogue)."""
    if _tag_in(text or ""):
        return True
    return bool(prev_text) and _tag_in(prev_text)


def _risk_tier(entry: dict, entries: list, i: int) -> int:
    """Count (0~3) of the risk features the ``i``-th entry of ``entries`` hits."""
    tier = 0
    text = entry.get("text") or ""
    # ① 无显式归属标签：标签可能落在紧邻的前一条旁白里（「林某说」在台词行之前）；
    #    前一条是角色台词时不算标签（那是台词本身）。
    prev_text = (
        entries[i - 1].get("text")
        if i > 0 and entries[i - 1].get("speaker") == "NARRATOR"
        else None
    )
    if not _has_attribution_tag(text, prev_text):
        tier += 1
    # ② 极短条目
    if len(text.strip()) <= SPOT_CHECK_SHORT_MAX:
        tier += 1
    # ③ 多角色场景：±N 条窗口（含自身、边界 clamp）内 ≥M 个不同非 NARRATOR 说话人
    lo = max(0, i - SPOT_CHECK_MULTI_WINDOW)
    hi = min(len(entries), i + SPOT_CHECK_MULTI_WINDOW + 1)
    speakers = {(entries[j].get("speaker") or "") for j in range(lo, hi)}
    if len(speakers - {"", "NARRATOR"}) >= SPOT_CHECK_MULTI_MIN:
        tier += 1
    return tier


def spot_budget(n_total: int, rate: float) -> tuple[int, int]:
    """The two disjoint sampling buckets ``(n_random, n_risk)``.

    rate ≤ 0 or no entries → (0, 0) (spotting off). Otherwise the total budget is
    ``max(1, round(rate·N))`` capped at N — a live rate always samples ≥1 entry so
    even a tiny book yields a gauge reading — of which 1/3 is the pure-random
    bucket (the unbiased error-rate gauge) and the rest the risk bucket.
    """
    if rate <= 0 or n_total <= 0:
        return 0, 0
    n_targets = min(n_total, max(1, round(rate * n_total)))
    n_random = round(n_targets / 3)
    return n_random, n_targets - n_random


def select_spot_targets(entries: list, n_random: int, n_risk: int,
                        rng: random.Random) -> tuple[list[int], list[int]]:
    """The two buckets' target indices (disjoint, each sorted ascending).

    The pure-random bucket is drawn FIRST with ``rng.sample`` uniformly over ALL
    entries (the gauge must stay uncontaminated by the risk weighting); the risk
    bucket then cascades over the REMAINDER — tiers 3→2→1→0 in descending order,
    ``rng.shuffle`` within a tier (uniform), and tier 0 (feature-free entries) tops
    the budget up so it is always fully used.
    """
    n = len(entries)
    n_random = min(n_random, n)
    random_targets = sorted(rng.sample(range(n), n_random)) if n_random else []
    taken = set(random_targets)
    tiers: dict[int, list[int]] = {3: [], 2: [], 1: [], 0: []}
    for i in range(n):
        if i in taken:
            continue
        tiers[_risk_tier(entries[i], entries, i)].append(i)
    risk: list[int] = []
    for tier in (3, 2, 1, 0):
        pool = tiers[tier]
        if pool:
            rng.shuffle(pool)
        while pool and len(risk) < n_risk:
            risk.append(pool.pop())
    return random_targets, sorted(risk)


def _spot_history_path(path_override: Path | None = None) -> Path | None:
    """``<workspace>/config/spot_check_history.json`` (None with no workspace).

    Deliberately in ``config/`` — never ``03_parsed_json/``: that directory is globbed
    wholesale as parsed scripts by ``resolve_parsed_json_all``, and a stray JSON there
    would corrupt the 全部文件 aggregate. ``config/`` is invisible to the file-list API.
    """
    if path_override is not None:
        return path_override
    config = get_or_prepare_layout().config
    if config is None:
        return None
    return config / SPOT_CHECK_HISTORY_NAME


def _load_spot_history(handle=None, path_override: Path | None = None) -> list:
    """The per-book readings (``[]`` when absent or corrupt — a stats read failure
    must never sink the parse task)."""
    path = _spot_history_path(path_override)
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except Exception:  # noqa: BLE001
        if handle is not None:
            handle.log("归属抽样历史文件损坏，按空历史处理", "WARNING")
        return []
    runs = data.get("runs") if isinstance(data, dict) else None
    return runs if isinstance(runs, list) else []


def adaptive_spot_rate(generation: GenerationConfig, history_path: Path | None = None) -> float:
    """Choose the next spot-check rate from the unbiased historical bucket.

    The configured rate is the cold-start rate.  After at least three recent runs
    and 50 pure-random observations, a very low error rate halves the next budget;
    a high error rate grows it by 1.5x.  Bounds make the feedback loop conservative
    and prevent a noisy small sample from turning sampling off or exploding costs.
    """
    configured = max(0.0, min(1.0, float(getattr(generation, "spot_check_rate", 0) or 0)))
    if configured <= 0 or not bool(getattr(generation, "spot_check_adaptive", True)):
        return configured
    minimum = max(0.0, min(1.0, float(getattr(generation, "spot_check_min_rate", 0.01) or 0)))
    maximum = max(minimum, min(1.0, float(getattr(generation, "spot_check_max_rate", 0.10) or 0)))
    current = min(configured, maximum)
    runs = []
    history = _load_spot_history() if history_path is None else _load_spot_history(path_override=history_path)
    for run in history:
        bucket = run.get("random") if isinstance(run, dict) else None
        if not isinstance(bucket, dict):
            continue
        try:
            n = int(bucket.get("n", 0) or 0)
            errors = int(bucket.get("errors", 0) or 0)
        except (TypeError, ValueError):
            continue
        if n > 0 and 0 <= errors <= n:
            runs.append((n, errors))
    recent = runs[-5:]
    samples = sum(n for n, _ in recent)
    if len(recent) < 3 or samples < 50:
        # Cold start keeps the explicitly configured rate, including deliberate
        # 100% audit runs used for diagnostics; bounds apply once feedback exists.
        return configured
    error_rate = sum(errors for _, errors in recent) / samples
    if error_rate <= 0.005:
        return max(minimum, current * 0.5)
    if error_rate >= 0.02:
        return min(maximum, current * 1.5)
    return current


def _append_spot_history(handle, file_stem: str, stats: dict, path_override: Path | None = None) -> None:
    """Append one book's pure-random-bucket reading: locked read-modify-write, keep
    the last ``SPOT_CHECK_HISTORY_CAP`` runs, ``write_bytes`` full rewrite.

    Any exception degrades to a WARNING — a stats-write failure must never turn the
    parse task into a failure (the base file is the primary artifact).
    """
    try:
        path = _spot_history_path(path_override)
        if path is None:
            return
        with _SPOT_HISTORY_LOCK:
            runs = _load_spot_history(path_override=path_override)
            runs.append({
                "file": file_stem,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "rate": stats["rate"],
                "random": {
                    "n": stats["random_n"],
                    "errors": stats["random_errors"],
                    "rate": stats["random_rate"],
                },
                "risk": {"n": stats["risk_n"], "errors": stats["risk_errors"]},
            })
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(json.dumps(
                {"runs": runs[-SPOT_CHECK_HISTORY_CAP:]},
                ensure_ascii=False, indent=2,
            ).encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        handle.log(f"归属抽样历史写入失败（不影响解析结果）：{e}", "WARNING")


def _run_rejudge_groups(handle, llm: LLMConfig, generation: GenerationConfig,
                        sys_prompt: str, usr_template: str,
                        entries: list, groups: list, n: int, roster, *,
                        stage: str, progress_base: float, progress_span: float,
                        on_first_map=None) -> tuple:
    """共享重判批协议执行器：逐组跑「首判 → 分歧条目动态多数投票 → 应用」。

    归属抽样与角色匹配检查（chunk 边界复查）共用此执行器——窗口构建 / 分组 /
    投票 / 提示词 / 配置几何全部同一套（各自只传目标组与参数）。

    **target / context 严格分离（硬不变式）**：context 条目（窗口内未标
    ``target`` 的条目）仅提供判断依据，**永不被修改**；只有 target 条目可被
    改写（``speaker`` + 台词「仅去外层引号」的机械值）——不允许因上下文中
    某条的 speaker 判断而改动 context 条目。三层保证：
    ① 捆绑提示词明令只判 target、上下文仅辅助、角色名逐字复制；
    ② 应用代码只写 target 下标（``result`` 是原始列表浅拷贝，投票采纳 /
    去引号均按 target 下标定位，非 target 条目逐字节不动）；
    ③ 窗口恒由**原始（pristine）列表**预建（首判 + 全部重试用同一窗口），
    任何已应用的修正不回流进后续窗口。

    协议：首判 → 分歧条目 ``votes=[原值, 首判]`` → 至多 3 次同窗口重试
    → ``_pick_majority`` 严格多数（≥2 票且唯一领先）一出即停；无共识 / 调用
    失败 → 保留原值（从不猜）。取消（``TaskCancelled``）上抛、零应用。
    ``on_first_map(first_map, grp)``（可选）在每组首判后回调（归属抽样的
    分桶仪表读数）；``stage`` 用于日志前缀，``progress_base/span`` 复现各
    阶段的进度带（``progress = base + span·seq/组数``）。

    返回 ``(列表, {checked, fixed, unwrapped})``——未应用任何修正时返回
    原始列表对象本身（调用方据此保持对象身份）。
    """
    original = entries  # 每个窗口（首判 + 全部重跑）都从 ORIGINAL 构建
    result = [dict(e) for e in original]
    fixed = 0
    unwrapped = 0
    proc_start = time.monotonic()
    window_chars = 0

    for seq, grp in enumerate(groups, 1):
        handle.check()  # cooperative cancel / pause before the group
        handle.progress(progress_base + progress_span * seq / len(groups),
                        f"{stage} {seq}/{len(groups)} 组")
        grp_set = set(grp)
        start, size = grp[0], grp[-1] - grp[0] + 1
        span = set(range(start, start + size))
        skip = span - grp_set  # in-span non-targets → context, never re-judged
        context = json.dumps(build_batch_window(original, start, size, n, skip=skip),
                             ensure_ascii=False, indent=2)
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": _batch_user_prompt(usr_template, context, len(grp), n, roster)},
        ]
        handle.llm_rate(0, 0.0)  # reset the 吞吐 gauge for this call
        handle.log(f"{stage}第 {seq}/{len(groups)} 组（{len(grp)} 条目标）…")

        # -- First pass: re-judge the whole group in one call -------------------
        try:
            full_map = parse_speaker_map_full(_llm_call(llm, generation, messages, handle), grp)
        except TaskCancelled:
            raise  # a cancel raised mid-stream must propagate, not be swallowed
        except Exception as e:  # noqa: BLE001 — unreadable first pass → keep originals
            handle.log(f"  首批解析失败，本组保留原 speaker：{e}", "WARNING")
            full_map = {}
        first_map = {i: sp for i, (sp, _tx) in full_map.items()}
        # The reply's optional "text" keys: applied only after strict validation below.
        text_sigs: dict = {i: tx for i, (_sp, tx) in full_map.items() if tx}
        window_chars += len(context)

        if on_first_map is not None:
            on_first_map(first_map, grp)

        discrepant = [
            t for t in grp
            if (sp := first_map.get(t)) is not None and sp != original[t].get("speaker")
        ]

        if discrepant:
            # -- Disagreement → dynamic majority voting (the shared protocol) --
            handle.log(f"  检测到 {len(discrepant)} 条分歧，进入动态投票…")
            # votes[t] = [原值, 首判, 重试1, (重试2), (重试3)]
            votes: dict = {t: [original[t].get("speaker"), first_map[t]] for t in discrepant}

            def retry_once(run: int, pending: list) -> list:
                """Re-send this group's window (retry #run) and fold the new judgments
                into the ``pending`` entries' votes; return the ones still without a
                strict majority. Optional ``text`` keys fold into ``text_sigs`` (first
                wins)."""
                handle.check()
                handle.llm_rate(0, 0.0)
                try:
                    full = parse_speaker_map_full(_llm_call(llm, generation, messages, handle), grp)
                except TaskCancelled:
                    raise
                except Exception as e:  # noqa: BLE001 — a failed retry adds no votes
                    handle.log(f"  重试第 {run} 次失败，无新票：{e}", "WARNING")
                    full = {}
                m = {i: sp for i, (sp, _tx) in full.items()}
                for i, tx in full.items():
                    if tx and i not in text_sigs:
                        text_sigs[i] = tx
                for t in pending:
                    votes[t].append(m.get(t))  # a missing/failed entry contributes no vote
                return [t for t in pending if _pick_majority(votes[t]) is None]

            handle.log(f"  重试第 1 次（3 样本：原值 + 首判 + 重试结果）…")
            still = retry_once(1, discrepant)
            window_chars += len(context)
            if still:
                handle.log(f"  {len(still)} 条 1:1 无共识 → 重试第 2 次…")
                still = retry_once(2, still)
                window_chars += len(context)
                if still:
                    handle.log(f"  {len(still)} 条仍无共识 → 重试第 3 次（末次）…")
                    still = retry_once(3, still)
                    window_chars += len(context)
                    if still:
                        handle.log(f"  {len(still)} 条重试 3 次仍无共识，保留原 speaker")

            # Apply: adopt the majority only if it beats the original; else keep original.
            # 只写 target 下标——result 是原始列表浅拷贝，非 target 条目逐字节不动。
            for t in discrepant:
                winner = _pick_majority(votes[t])
                orig_sp = original[t].get("speaker")
                if winner is not None and winner != orig_sp:
                    result[t]["speaker"] = winner  # only `speaker` changes
                    fixed += 1
                    handle.log(f"  条目 {t + 1}: {orig_sp} → {winner}")
                # winner None (no consensus) or == original → the original is kept.
        # (no disagreement → the group matches the originals; nothing to re-vote)

        # The re-judgment prompt's optional "text" keys: the stored text unwrapped of
        # its outer quotation marks. Applied only when the entry's FINAL speaker is a
        # character and the reply matches the mechanically computed strip (the value
        # written is the computed one — this stage can never rewrite text beyond the
        # quote removal; instruct and every context neighbour stay untouched).
        for t in grp:
            sig = text_sigs.get(t)
            if not sig or (result[t].get("speaker") or "") == "NARRATOR":
                continue
            expected = strip_outer_quotes(original[t].get("text") or "")
            if not expected or sig != expected:
                handle.log(
                    f"  {t + 1}: 模型返回的 text 与「仅去外层引号」不符，忽略（text 保持原样）",
                    "WARNING",
                )
                continue
            if result[t].get("text") != expected:
                result[t]["text"] = expected
                unwrapped += 1
                handle.log(f"  {t + 1}: 台词已去除外层引号")

        handle.llm_chars(window_chars, time.monotonic() - proc_start)

    checked = sum(len(g) for g in groups)
    return (result if (fixed or unwrapped) else entries), {
        "checked": checked, "fixed": fixed, "unwrapped": unwrapped,
    }


def spot_check_speakers(handle, llm: LLMConfig, generation: GenerationConfig,
                        entries: list, rate: float,
                        rng: "random.Random" | None = None) -> tuple:
    """Post-parse speaker spot audit (runs in ``generate_file`` after
    ``validate_sentence_splits`` and BEFORE the mechanical NARRATOR merge — a
    re-judgment may turn a NARRATOR entry into a character entry, and the merge must
    see the corrected speaker).

    Samples ``rate`` of ALL entries into two disjoint buckets (1/3 pure random = the
    unbiased error-rate gauge; 2/3 risk-weighted, feature-count cascade) and re-judges
    them through the bundled re-judgment prompts (``check_prompts`` — no longer
    user-configurable) and the shared batch protocol (first pass → dynamic majority
    vote per discrepant entry, ≤3 same-window retries, strict majority or keep
    original; only ``speaker`` may change, plus the deterministic outer-quote strip of
    a character line). Batch geometry comes from
    ``generation.check_batch_size`` / ``check_context_window``. Fixes are applied in
    memory on shallow copies — the parse task then bakes them into ITS OWN base file,
    which is the parse output (nothing else may rewrite it).

    rate ≤ 0 / empty list → the list unchanged with zeroed stats. ``rng`` (tests)
    injects a seeded ``random.Random``; ``None`` → a fresh one per run (no cross-book
    correlation). Cancel propagates and nothing is applied.

    Returns ``(entries, stats)`` — the updated list (the original list object when
    nothing was applied) and ``{checked, fixed, rate, random_n, random_errors,
    random_rate, risk_n, risk_errors}`` (risk_* feed the history file only).
    """
    stats = {
        "checked": 0, "fixed": 0, "rate": float(rate or 0),
        "random_n": 0, "random_errors": 0, "random_rate": None,
        "risk_n": 0, "risk_errors": 0,
    }
    if rate <= 0 or not entries:
        return entries, stats

    from . import check_prompts  # bundled re-judgment prompt defaults

    n_random, n_risk = spot_budget(len(entries), float(rate))
    if n_random + n_risk == 0:
        return entries, stats

    if rng is None:
        rng = random.Random()
    random_targets, risk_targets = select_spot_targets(entries, n_random, n_risk, rng)
    random_set = set(random_targets)
    risk_set = set(risk_targets)
    all_targets = sorted(random_targets + risk_targets)

    n = max(0, int(generation.check_context_window or 0))
    batch = max(1, int(generation.check_batch_size or 0))
    original = entries  # every window (first pass + all re-runs) is built from ORIGINAL
    roster = build_roster(original)
    # Bundled defaults — the re-judgment prompts are no longer user-configurable.
    sys_prompt = check_prompts.DEFAULT_CHECK_SYSTEM_PROMPT
    usr_template = check_prompts.DEFAULT_CHECK_USER_PROMPT

    # Groups pre-built ONCE outside the loop: each group of close-together targets is
    # one LLM call (the shared retry grouping rule), non-targets inside the span ride
    # along as unflagged context — wider coverage than the nominal rate, cheaper.
    groups = group_retry_indices(all_targets, n, batch)

    stats["checked"] = len(all_targets)
    stats["random_n"] = len(random_targets)
    stats["risk_n"] = len(risk_targets)

    handle.log(
        f"归属抽样：{len(all_targets)} 条（纯随机 {len(random_targets)} + 风险加权 "
        f"{len(risk_targets)}，共 {len(groups)} 组），复用重判批协议…"
    )

    # 分桶仪表读数（留在抽样侧）：首判改判 ≠ 原值 = 检出错误，按桶计数——
    # 桶的读数 = 该桶的解析错误率，与后续投票结局无关。
    bucket_errors = [0, 0]  # [纯随机桶, 风险桶]

    def _gauge(first_map: dict, grp: list) -> None:
        for t in grp:
            sp = first_map.get(t)
            if sp is None or sp == original[t].get("speaker"):
                continue
            if t in random_set:
                bucket_errors[0] += 1
            elif t in risk_set:
                bucket_errors[1] += 1

    result, rep = _run_rejudge_groups(
        handle, llm, generation, sys_prompt, usr_template,
        original, groups, n, roster,
        stage="归属抽样", progress_base=0.98, progress_span=0.018,
        on_first_map=_gauge,
    )
    fixed, unwrapped = rep["fixed"], rep["unwrapped"]
    random_errors, risk_errors = bucket_errors
    stats["fixed"] = fixed
    stats["random_errors"] = random_errors
    stats["risk_errors"] = risk_errors
    stats["random_rate"] = (random_errors / len(random_targets)) if random_targets else None
    if fixed or unwrapped:
        handle.log(
            f"归属抽样完成：抽查 {stats['checked']} 条，更正 {fixed} 条 speaker"
            + (f"、{unwrapped} 条台词去引号" if unwrapped else "")
        )
    return result, stats


# ---------------------------------------------------------------------------
# 纯归属标签条清理（确定性，零 LLM 成本）
#
# 信号：NARRATOR 条目整体就是一条纯归属标签（老道瞪眼怒道。/ 杜尘暗喜，急道。/
# 史蒂夫解释道。）——无引号、无冒号，断句校验看不见；留在结果里会成 TTS 的独立旁白行
# （多一次同人停顿 + 换人停顿），悬在它引入的台词之前/之后。删除是五条件合取：
# NARRATOR + 无引号 + ≤10 字 + 末尾（去末尾标点后）为五归属动词 + 非章标题 + 紧邻对白条。
# 知道/难道 的「道」与归属抽样同一形态守卫（道前一字符 ∈ 知难 → 不是标签）。
# 位置在归属抽样**之前**：抽样重判可能把标签条翻成角色 speaker，本规则便不再适用
# （且角色会用自己声音念第三人称标签）。
# ---------------------------------------------------------------------------

# 纯标签恒短；「叙述 + 标签」的混合条（…一字一顿地说道。）更长——阈值即两者的分界。
PURE_SAY_TAG_MAX_LEN = 10
# 标签动词之后的末尾标点（句末 / 冒号 / 省略 / 破折号）——全部剥掉后再看末字。
_SAY_TAG_TRAIL_PUNCT = "。！？…：；、，～—-!??:;,.~"


def _is_pure_saying_tag(entry, title_test) -> bool:
    """Whether an entry is a standalone pure-attribution-tag line: a NARRATOR entry of ≤
    PURE_SAY_TAG_MAX_LEN quote-free chars that, once trailing punctuation is stripped,
    ends in one of the five attribution verbs and is not a chapter title. The 知道/难道
    morphological guard applies to a final 道 (same guard as the spot-check tag
    detection); ANY quote character disqualifies — a wrapped utterance is content, not
    a tag, and dropping it would lose the line outright."""
    if not isinstance(entry, dict) or entry.get("speaker") != "NARRATOR":
        return False
    t = (entry.get("text") or "").strip()
    if not t or len(t) > PURE_SAY_TAG_MAX_LEN:
        return False
    if any(ch in _QUOTE_CHARS for ch in t):
        return False
    if title_test(t):
        return False
    core = t.rstrip(_SAY_TAG_TRAIL_PUNCT)
    if not core:
        return False
    last = core[-1]
    if last not in _SAY_VERBS:
        return False
    if last == "道" and len(core) >= 2 and core[-2] in _NONSPEAK_BEFORE_DAO:
        return False  # 知道 / 难道 — ordinary narrative words, never tags
    return True


def delete_pure_saying_tags(entries, title_test) -> tuple:
    """Deterministically drop standalone pure-attribution-tag entries (no LLM calls).

    Deletion requires ALL of: NARRATOR, quote-free text of ≤ PURE_SAY_TAG_MAX_LEN
    chars ending in 说/道/问/喊/答 after trailing punctuation is stripped (知道/难道
    guarded), not a chapter title, and immediately preceded or followed by a
    dialogue entry (non-empty speaker ≠ NARRATOR). Single pass, non-cascading —
    adjacency is judged against the pre-deletion list, so a tag two entries away
    from any line survives (deliberately conservative: code cannot tell a pure
    tag from a short narration sentence that merely ends in a verb). Deletion can
    never create a new NARRATOR/NARRATOR adjacency (one side is always a dialogue
    entry), so the downstream merge is unaffected. Returns
    ``(kept_entries, deleted_count, deleted_texts)``.
    """
    kept = []
    deleted_texts = []
    deleted = 0
    n = len(entries)
    for i, e in enumerate(entries):
        if _is_pure_saying_tag(e, title_test):
            prev_sp = entries[i - 1].get("speaker") if i > 0 else ""
            next_sp = entries[i + 1].get("speaker") if i + 1 < n else ""
            if (prev_sp and prev_sp != "NARRATOR") or (next_sp and next_sp != "NARRATOR"):
                deleted += 1
                deleted_texts.append((e.get("text") or "").strip())
                continue
        kept.append(e)
    return kept, deleted, deleted_texts


def parse_script_file(handle, path, llm: LLMConfig, prompts: PromptsConfig, generation: GenerationConfig,
                  rng: "random.Random" | None = None, *, output_path: Path | None = None,
                  spot_history_path: Path | None = None) -> dict:
    """Task worker: turn one ``02_split_text`` file into its ``{speaker, text, instruct}``
    JSON entries.

    Contract: first arg is the durable task context`; the second is the absolute path of
    the source ``.txt``. This is the per-file, concurrent form of the old text-paste
    worker: each file is its own independent task with its own LLM requests, status,
    and output. Concurrency is bounded by the shared gate (``core.concurrency``) —
    a task holds ONE slot for the **LLM chunk-parse stage only**: file read /
    decode / chunk-split happen before taking the slot (prep, so prefetched files
    from an ordered dispatch can be ready before their turn), and the slot is
    released the moment the chunk loop ends, so the mechanical check stages
    (whose re-judgment LLM calls are comparatively rare) run slot-free and a
    prefetched file can take the freed slot immediately. The result is written to
    ``03_parsed_json/<source-stem>.json`` (one file per source, no scratch dir).
    Progress is scaled so 100% means done: the parse stage owns [0, 0.9) and the
    check stages own [0.9, 1.0].
    ``llm`` / ``prompts`` / ``generation`` are the config section objects; empty
    ``prompts`` fall back to the bundled defaults.

    First in the check phase, a chunk-boundary re-check (``boundary_check_speakers``,
    ``generation.check_boundary_speakers`` — default on) re-judges the n entries
    flanking each INTERNAL chunk boundary (n = ``check_context_window``) through
    the same bundled re-judgment prompts and shared batch protocol: chunk cuts
    sever cross-chunk context, so boundary entries parsed without the other side's
    dialogue/character context are the classic speaker-misjudgment site. Context
    entries in the window are judgment aids ONLY — never modified; only target
    entries may change (and only ``speaker`` + the mechanical outer-quote strip).
    Windows are prebuilt from the PRISTINE entry list, which is why this stage runs
    BEFORE the split validator and the tag cleaner (both shift entry indices).
    Gated off → skipped with one log line and zeroed ``boundary_*`` result fields.
    No history file (no gauge semantics — the gauge belongs to the spot audit).

    After all chunks are parsed, entries whose stored text is wrapped in outer
    double quotes that still contain a ``…道：`` speech tag inside are treated as
    likely failed sentence splits and re-validated: each such entry is re-run
    through the *same parse prompt* with the entry's text as the chunk plus a
    ±``generation.check_context_window`` entry context window. The re-derivations
    vote (1 base call + up to 2 retries, majority wins; a 4th call only if the 3
    calls are still undecided) and a strict majority replaces the entry — a 1-part
    consensus is a clean rewrite (outer wrap / leading tag stripped), a
    multi-part one a split. No consensus keeps the entry unchanged (never guess).
    Gated by ``generation.revalidate_splits`` (default on): when off the stage is
    skipped with one log line and the ``suspicious`` / ``suspicious_fixed`` result
    fields stay zero.

    Before sentence-split validation, entries with an empty ``instruct`` or an
    ``instruct`` containing at least 35 whitespace-delimited words are mechanically
    checked. Contiguous NARRATOR targets inherit a nearby valid narrator direction;
    remaining targets are repaired in one index-keyed LLM request with a
    ±``generation.check_context_window`` local context. The ``instruct_checked`` /
    ``instruct_fixed`` result fields record the flagged and replaced counts.

    Before the spot audit, standalone pure-attribution-tag entries are deleted
    deterministically (no LLM calls): a NARRATOR entry of ≤10 chars, quote-free,
    ending (after trailing punctuation is stripped) in one of the five attribution
    verbs, not a chapter title, and immediately adjacent to a dialogue entry (e.g.
    老道瞪眼怒道。 — the unwrapped form the split validator cannot see) — kept,
    it would be read aloud as its own narration line with an extra pause.
    Deletion is non-cascading (adjacency is judged on the pre-deletion list) and
    can never create a new NARRATOR/NARRATOR adjacency, so the downstream merge
    is unaffected.

    After that, an attribution spot audit (``spot_check_speakers``,
    ``generation.spot_check_rate`` — 0 disables) re-judges a small sample of ALL
    entries (1/3 pure random = the unbiased whole-book error-rate gauge, 2/3
    risk-weighted by feature-count cascade) through the bundled re-judgment prompts
    (``check_prompts``) and the shared re-judgment batch protocol; high-confidence
    corrections are baked into the base file this task writes. Each book's pure-random-bucket reading is logged and
    appended to ``<workspace>/config/spot_check_history.json`` — the rate itself is
    NEVER auto-reduced (the user decides manually from the settings page).
    ``rng`` (tests) injects a seeded ``random.Random`` for deterministic sampling.
    """
    # Fail fast on a misconfigured model *before* doing any work.
    if not (llm.model_name or "").strip():
        raise RuntimeError("请先在「文本解析」页配置 LLM 模型名称（模型不能为空）。")

    src = Path(path)
    # -- 预备阶段（不占槽）：读文件 / 解码 / 分 chunk。廉价且零 LLM 调用，放在占槽
    # 之前，让「有序投放 + 预取」的批次里预取任务可以提前备好、排队等槽（见
    # api/script.py 的批次协调者）。
    if not src.is_file():
        raise RuntimeError(f"文件不存在：{src.name}")
    raw = src.read_bytes()
    if not raw:
        raise RuntimeError(f"{src.name} 内容为空。")
    try:
        text, _enc = decode_buffer(raw)
    except ValueError as e:
        raise RuntimeError(f"{src.name} 无法解码：{e}")
    body = (text or "").strip()
    if not body:
        raise RuntimeError(f"{src.name} 无有效文本。")

    body = fix_mojibake(body)
    handle.log(f"读入 {src.name}（{len(body)} 字）")

    chunks = split_into_chunks(body, max_size=generation.chunk_size)
    total = len(chunks)
    if total == 0:
        raise RuntimeError("未从文件切分出任何片段。")
    handle.log(
        f"切分为 {total} 段（目标均长约 {generation.chunk_size} 字，"
        f"切点吸附最近合法结构边界）"
    )
    handle.log(f"模型：{llm.model_name} · 端点：{llm.base_url}")
    handle.check()  # 排队前到达的取消/暂停就地生效

    # -- LLM 分段解析阶段（持槽）：槽位只覆盖这一阶段——chunk 循环一结束就 release，
    # 机械检查阶段（角色匹配检查 / 断句校验 / 标签删除 / 归属抽样）不占槽，
    # 让后续预取任务及时补位。
    handle.progress(0.0, "排队中（等待并发槽位）")
    if not gate().acquire(stop_check=lambda: handle.cancelled):
        raise TaskCancelled()  # 排队等待中被取消——未取槽，下面 finally 不得 release
    handle.phase("parse")
    slot_released = False
    try:
        sys_prompt = prompts.system_prompt or DEFAULT_SYSTEM_PROMPT
        usr_template = prompts.user_prompt or DEFAULT_USER_PROMPT

        all_entries = []
        chunk_ends = []  # 每段结束时的累计条目数——chunk 边界簿记（角色匹配检查用）
        processed_chars = 0  # 累计已处理原始字符数（处理速度 的分子），每完成一段累加
        proc_start = time.monotonic()  # 本文件开始逐段处理的时刻（处理速度分母冻结于每段完成）
        for i, chunk in enumerate(chunks, 1):
            handle.check()  # cooperative cancel / pause between chunks
            handle.log(f"处理第 {i}/{total} 段（{len(chunk)} 字）…")
            # The LLM parse stage owns [0, 0.9) of the progress bar — 0.9..1.0 is
            # reserved for the (slot-free) mechanical check stages, so a running
            # task never reads as 100% before it is actually done.
            handle.progress(0.9 * (i - 1) / total, f"处理第 {i}/{total} 段")
            handle.llm_rate(0, 0.0)  # reset the 字/s gauge per chunk (0 until the stream reports)
            previous = all_entries if all_entries else None
            entries = process_chunk(
                handle, llm, llm.model_name, chunk, i, total,
                previous_entries=previous,
                system_prompt=sys_prompt,
                user_prompt_template=usr_template,
                max_tokens=generation.max_tokens,
                temperature=generation.temperature,
                top_p=generation.top_p,
                top_k=generation.top_k,
                min_p=generation.min_p,
                presence_penalty=generation.presence_penalty,
                banned_tokens=generation.banned_tokens,
            )
            all_entries.extend(entries)
            chunk_ends.append(len(all_entries))
            handle.log(f"  得到 {len(entries)} 条")
            # 每段完成后上报"累计原始字符数 + 到本段为止的处理耗时"，让处理速度（Σ字÷Σ耗时）
            # 按段刷新、段间保持不变（耗时冻结于本段完成时刻，而非实时时钟，故不会持续衰减）。
            # len(chunk) 是该段的真实源文字符数（字符，而非 token）。
            processed_chars += len(chunk)
            handle.llm_chars(processed_chars, time.monotonic() - proc_start)

        if not all_entries:
            raise RuntimeError("未生成任何脚本条目。")

        # LLM 分段解析完成 → 释放槽位（预取补位点：后续排队任务立即拿到槽开始解析）。
        # 四机械检查阶段的 LLM 重判调用从此不再计入并发上限——有意语义：文件进入
        # 机械检查即让出解析槽，避免等待造成 LLM 资源空闲。
        gate().release()
        slot_released = True
        handle.phase("check")
        handle.progress(
            0.9,
            "机械检查（角色匹配检查 / 断句校验 / 标签删除 / 超长段落检查 / 归属抽样 / 纯标点吸收）",
        )

        # 角色匹配检查（检查段最前——断句校验拆条、标签删除删条都会移动下标，
        # 本阶段窗口必须取自 pristine 列表）：用跨边界窗口重判每个内部 chunk
        # 边界两侧各 n 条的 speaker（上下文仅辅助，只有 target 可被修改）。
        if generation.check_boundary_speakers:
            all_entries, boundary_stats = boundary_check_speakers(
                handle, llm, generation, all_entries, chunk_ends,
            )
        else:
            # 开关关闭：整体跳过并留一行日志（防静默被误读为阶段缺失），结果字段保持 0。
            handle.log("角色匹配检查已关闭（配置）——本任务跳过该阶段")
            boundary_stats = {"checked": 0, "fixed": 0}

        # instruct 检查（机械旁白合并之前）：空 instruct 或至少 35 words 的 instruct
        # 可能意味着声音指导生成失败/失控；带同一上下文窗口重跑解析 LLM，多数胜出
        # 后整体替换目标条目。此阶段不依赖后续机械合并，避免合并或硬切产生的空值被
        # 错误地再次送入 LLM。
        all_entries, instruct_checked, instruct_fixed = validate_instructs(
            handle, llm, generation, sys_prompt, usr_template, all_entries,
            context_window=int(generation.check_context_window or 0),
        )

        # 断句失败校验（机械旁白合并之前——拆出的旁白段随后照常合并）：外层双引号
        # 包裹、引号内含「…道：」标签的条目 = 疑似断句失败 → 带上下文窗口重跑解析
        # LLM，多者胜投票（1+2 次，无共识再第 4 次），胜出者整体替换该条目。
        if generation.revalidate_splits:
            all_entries, suspicious, suspicious_fixed = validate_sentence_splits(
                handle, llm, generation, sys_prompt, usr_template, all_entries,
                context_window=int(generation.check_context_window or 0),
            )
        else:
            # 开关关闭：整体跳过并留一行日志（防静默被误读为阶段缺失），结果字段保持 0。
            handle.log("断句失败校验已关闭（配置）——本任务跳过该阶段")
            suspicious, suspicious_fixed = 0, 0

        # 纯归属标签清理（零 LLM 成本；在归属抽样之前——抽样重判可能把标签条改成角色
        # speaker，使其逃过本规则，且角色会用本人声音念第三人称标签）：整条内容就是
        # 语气标签（老道瞪眼怒道。——无引号，断句校验不可见）的短 NARRATOR 条，紧邻
        # 台词条目时删除；标题守卫与机械合并同一 is_chapter_title；知道/难道 形态守卫。
        if generation.delete_saying_tags:
            all_entries, tags_deleted, deleted_tag_texts = delete_pure_saying_tags(
                all_entries, is_chapter_title,
            )
            if tags_deleted:
                handle.log(
                    f"纯归属标签清理：删除 {tags_deleted} 条独立短标签条（≤10 字 NARRATOR、"
                    f"末尾「说/道/问/喊/答」、邻接对白）"
                    + "、".join(f"「{t[:15]}」" for t in deleted_tag_texts[:3])
                )
            else:
                # 零命中也留一行日志：与断句校验同一理由——静默退出会被误读成阶段缺失。
                handle.log("纯归属标签清理：0 条独立短标签条（无删除，零 LLM 调用）")
        else:
            # 开关关闭：整体跳过并留一行日志，结果字段保持 0。
            handle.log("纯归属标签条删除已关闭（配置）——本任务跳过该阶段")
            tags_deleted, deleted_tag_texts = 0, []

        # 超长段落检查·LLM 重切（归属抽样之前——重切可能产生新归属的台词条目，
        # 需要被抽样审计；同一说话人的长篇独白「拆分」过不了重判忠实性门，
        # 超长会留到本阶段后半的机械分段兜底——分工是特性：LLM 管语义边界，
        # 机械保证长度硬上界）：超过 max_paragraph_chars 字的条目带上下文窗口
        # 重跑解析 LLM，严格多数胜出者整体替换条目（4 次无共识保留原样，从不猜）。
        max_para = int(generation.max_paragraph_chars or 200)
        if generation.check_long_paragraphs:
            all_entries, long_checked, long_fixed = long_paragraph_resplit(
                handle, llm, generation, sys_prompt, usr_template, all_entries,
                max_para, context_window=int(generation.check_context_window or 0),
            )
        else:
            # 开关关闭：整体跳过并留一行日志（含机械分段兜底），结果字段保持 0。
            handle.log("超长段落检查已关闭（配置）——本任务跳过该阶段（含机械分段兜底）")
            long_checked, long_fixed = 0, 0

        # 归属抽样（机械旁白合并之前——重判可把 NARRATOR 条改成角色条，合并必须看到
        # 改后 speaker）：按 spot_check_rate 从全部条目抽 1/3 纯随机（整书错误率仪表）
        # + 2/3 风险加权（特征数级联），用捆绑重判提示词 + 共享重判批协议重判，
        # 高置信改判在内存中生效、随本任务自己的基文件写出。
        configured_spot_rate = float(generation.spot_check_rate or 0)
        spot_rate = adaptive_spot_rate(generation, history_path=spot_history_path)
        if spot_rate != configured_spot_rate:
            handle.log(
                f"归属抽样：历史随机桶反馈将抽样率从 {configured_spot_rate:.1%} 调整为 {spot_rate:.1%}"
            )
        all_entries, spot_stats = spot_check_speakers(
            handle, llm, generation, all_entries, spot_rate, rng,
        )

        # 纯标点条目吸收（确定性零 LLM 成本；必须在超长机械分段**之前**——吸收把
        # 「……」拼进邻接 NARRATOR 可能把它推过上限，随后的机械分段兜底切回：
        # 顺序反了会产生吸收后的超长条目无人兜底）：整条无词字符的条目（独立
        # 「……」/「？」）并入相邻 NARRATOR（前邻优先，标题守卫），无邻接则删除。
        if generation.absorb_punct_entries:
            all_entries, punct_absorbed, punct_deleted = absorb_punct_entries(
                all_entries, is_chapter_title,
            )
            if punct_absorbed or punct_deleted:
                handle.log(
                    f"纯标点条目吸收：并入相邻 NARRATOR {punct_absorbed} 条、"
                    f"无 NARRATOR 邻接删除 {punct_deleted} 条（零 LLM 成本）"
                )
            else:
                # 零命中也留一行日志：与其余阶段同一纪律——静默退出会被误读成阶段缺失。
                handle.log("纯标点条目吸收：0 条无内容条目（无吸收 / 无删除，零 LLM 调用）")
        else:
            # 开关关闭：整体跳过并留一行日志，结果字段保持 0。
            handle.log("纯标点条目吸收已关闭（配置）——本任务跳过该阶段")
            punct_absorbed, punct_deleted = 0, 0

        # 同人段落合并（确定性零 LLM 成本；必须在超长机械分段**之前**——≤10 强制
        # 合并可造出 > max_paragraph_chars 的同人块，由随后的机械分段切回，保住
        # 200 字硬保证；顺序反了会回粘机械分段自己切出的同人短段）：连续同 speaker
        # 条目按词字符数合并（块+段 ≤100 或较短一方 ≤10 强制），边界无收尾标点补
        # 「。」，instruct 取词字符多者，章标题两侧不合并（恒判、无豁免）。
        if generation.merge_same_speaker:
            all_entries, merged_pairs = merge_adjacent_same_speaker(
                all_entries, is_chapter_title,
            )
            if merged_pairs:
                handle.log(
                    f"同人段落合并：合并 {merged_pairs} 对连续同 speaker 条目"
                    f"（章标题两侧不合并，零 LLM 成本）"
                )
            else:
                # 零命中也留一行日志：与其余阶段同一纪律——静默退出会被误读成阶段缺失。
                handle.log("同人段落合并：0 对连续同 speaker 条目（无合并，零 LLM 调用）")
        else:
            # 开关关闭：整体跳过并留一行日志，结果字段保持 0。
            handle.log("同人段落合并已关闭（配置）——本任务跳过该阶段")
            merged_pairs = 0

        # 超长段落检查·机械分段兜底（管线**末段**，同人合并之后——speaker 已定稿，
        # 切段只继承父条目 speaker / instruct；硬保证：最终没有任何条目超过
        # max_paragraph_chars。同人合并的 ≤10 强制合并可能造出超限块，由本阶段
        # 切回；切出的段绝不会被回粘（本阶段是最后一步，其后无合并）。
        if generation.check_long_paragraphs:
            all_entries, long_split = split_long_entries(
                all_entries, max_para, is_chapter_title,
            )
            if long_split:
                handle.log(
                    f"超长段落机械分段：{long_split} 条 LLM 重切后仍超 {max_para} 字的"
                    f"条目已按句界 / 子句界 / 定宽切开"
                    f"（硬保证：最终无 {max_para} 字以上条目）"
                )
            else:
                handle.log(
                    f"超长段落机械分段：0 条仍超 {max_para} 字（无分段；硬保证已满足）"
                )
        else:
            long_split = 0

        out_name = f"{src.stem}.json"
        out_path = output_path or ((get_or_prepare_layout().parsed_json / out_name) if get_or_prepare_layout().parsed_json is not None else None)
        if out_path is None:
            raise RuntimeError("未设置工作空间，无法写入解析结果")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(all_entries, indent=2, ensure_ascii=False), encoding="utf-8")

        # 仪表读数：基文件落盘成功后才追加历史（解析结果是主产物，统计写失败绝不影响它）。
        if spot_stats["random_n"] > 0:
            _append_spot_history(handle, src.stem, spot_stats, path_override=spot_history_path)
        if spot_stats["checked"]:
            rnd_rate = spot_stats["random_rate"]
            rate_txt = (
                f"{spot_stats['random_errors']}/{spot_stats['random_n']}"
                + (f"（{rnd_rate:.1%}）" if rnd_rate is not None else "")
            )
            handle.log(
                f"归属抽样读数：纯随机桶错误 {rate_txt}（无偏整书错误率估计；"
                f"风险桶 {spot_stats['risk_errors']}/{spot_stats['risk_n']} 偏高风险、"
                f"不用于判断能否降率）——连续几本 <1% 可考虑在设置页手动调低「归属抽样率」"
            )

        speakers = sorted({(e.get("speaker") or "UNKNOWN") for e in all_entries})
        handle.progress(1.0, "完成")
        handle.log(f"共生成 {len(all_entries)} 条；讲者：{', '.join(speakers)}")
        return {
            "entries": all_entries,
            "output_path": str(out_path),
            "output_name": out_name,
            "count": len(all_entries),
            # 同人段落合并：合并的连续同 speaker 条目对数（merge_same_speaker 关闭时为 0）
            "merged_same_speaker": merged_pairs,
            # 角色匹配检查（解析内 chunk 边界重判；开关关闭时为 0）
            "boundary_checked": boundary_stats["checked"],
            "boundary_fixed": boundary_stats["fixed"],
            "suspicious": suspicious,
            "suspicious_fixed": suspicious_fixed,
            "instruct_checked": instruct_checked,
            "instruct_fixed": instruct_fixed,
            # 纯归属标签清理：确定性删除的独立短标签条数（零 LLM 成本；开关关闭时为 0）
            "tags_deleted": tags_deleted,
            # 超长段落检查（LLM 重切 + 机械分段兜底；check_long_paragraphs 关闭时全 0）
            # —— long_checked = 送 LLM 的超长条目数、long_fixed = 经投票替换条数、
            # long_split = 机械切分的条目数（机械分段恒在 LLM 重切之后运行）
            "long_checked": long_checked,
            "long_fixed": long_fixed,
            "long_split": long_split,
            # 纯标点条目吸收（零 LLM 成本；absorb_punct_entries 关闭时为 0）
            "punct_absorbed": punct_absorbed,
            "punct_deleted": punct_deleted,
            "speakers": speakers,
            # 归属抽样（解析任务自有的基文件内修正，不违反「检查阶段不改写基文件」）
            "spot_checked": spot_stats["checked"],
            "spot_fixed": spot_stats["fixed"],
            "spot_rate": spot_stats["rate"],
            "spot_random_n": spot_stats["random_n"],
            "spot_random_errors": spot_stats["random_errors"],
            "spot_random_rate": spot_stats["random_rate"],
            # Original (decoded, stripped, mojibake-fixed) input length in chars — kept in
            # the result for reference. The live 处理速度 gauge uses the per-chunk llm_chars
            # instead (chars, never tokens).
            "input_chars": len(body),
        }
    finally:
        # Exactly one release per path: success path already released before the
        # check stages (slot_released=True); any abort DURING the parse stage
        # (cancel / error) releases here; the acquire-abort path (stop_check) and
        # the prep-stage failures never took a slot and must not release.
        if not slot_released:
            gate().release()


generate_file = parse_script_file  # compatibility for direct Python callers
