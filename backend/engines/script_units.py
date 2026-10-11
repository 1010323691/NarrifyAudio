"""Numbered-unit parse protocol — pure helpers (no LLM, no I/O).

The legacy parse protocol asks the model to re-type the whole chunk as JSON (about 1.7
output tokens per source character). This protocol inverts the work: code cuts the chunk
into short numbered *units* (:func:`segment_chunk`), the model answers only with labels
for the units that are not plain narration (:func:`parse_unit_reply`), and code stitches
the result back into the ``{speaker, text, instruct}`` entries every downstream stage
already consumes (:func:`assemble`). Faithfulness to the source text is then structural:
the only text the model can influence is an ``E`` edit, which :func:`validate_edit`
restricts to deleting one contiguous speech-tag span.

Label grammar (one line each, ``|`` separated, free text always last)::

    12 姜维 | 紧张的低声耳语          dialogue (a range ``12-14`` shares speaker/instruct)
    13 X                              delete (pure speech tag / watermark / bare URL)
    15 E 史蒂夫 | - | 新文本           edit: speaker | instruct ("-" = none) | text with one span deleted
    20 B                              start a new narration entry here
    END                               mandatory terminator (a missing END = truncated reply)

Units the reply does not list are narration (``NARRATOR`` with the fixed narrator instruct).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .script import (
    _clean_reply,
    _skeleton,
    _strip_thinking,
    is_chapter_title,
)

NARRATOR = "NARRATOR"

# Top-level quote openers → matching closers. The ASCII double quote opens and closes with
# the same character. ASCII single quotes are apostrophes in mixed text and never open a
# span. Built with chr(): hand-typed quote glyphs are unreliable in source.
_QUOTE_OPEN_TO_CLOSE = {
    chr(0x201C): chr(0x201D),
    chr(0x300C): chr(0x300D),
    chr(0x300E): chr(0x300F),
    chr(0x2018): chr(0x2019),
    chr(0x0022): chr(0x0022),
}
# Openers whose missing closer means "the quote continues into the next paragraph" (a
# multi-paragraph quotation); the single curly quote is too often an apostrophe for that.
_RUNON_OPENERS = frozenset(chr(c) for c in (0x201C, 0x300C, 0x300E, 0x0022))

_SPEAKER_STRIP = "[]<>" + "".join(chr(c) for c in (0x22, 0x27, 0x201C, 0x201D, 0x300C, 0x300D, 0x300E, 0x300F, 0x3010, 0x3011, 0x300A, 0x300B))
_SENT_TAIL_RE = re.compile(r"[^。！？!?…]*[。！？!?…]+|[^。！？!?…]+$")
_CLAUSE_TAIL_RE = re.compile(r"[^，、；：,;:]*[，、；：,;:]+|[^，、；：,;:]+$")

# Characters allowed in an edit's replacement besides those already in the unit.
_EDIT_PUNCT = "。，、！？…；：——"
# A deleted span must contain a speech verb: the edit exists only to drop speech tags.
_EDIT_VERB_CHARS = "说道问答喊叫吼喝笑哭叹嚷骂呼"
_EDIT_VERB_PHRASES = ("表示", "回应", "开口", "出声", "嘟囔", "喃喃", "低语", "咕哝", "嘀咕", "念叨")


@dataclass
class Unit:
    pos: int            # position in the full unit list (hidden units included)
    n: int              # 1-based number shown to the model; 0 = hidden (see segment_chunk)
    text: str
    kind: str           # "title" | "quote" | "plain"
    lead: str = ""      # opening quote char carried by this unit ("" when none)
    trail: str = ""     # closing quote char carried by this unit ("" when none)
    para_start: bool = False
    whole_line_quote: bool = False
    embedded: bool = False   # short quote in the middle of a sentence (scare quote / mention)


# Speech-verb tail of a speech tag ("杜尘笑道", "他压低声音说", "低声问了句"). Only a clause that
# ENDS in a speech verb counts; "叹了口气" / "摇了摇头" / "抿了抿嘴" are actions, not tags.
_SPEECH_TAIL_RE = re.compile(
    r"(?:(?<![知难味街])道|说|问|答|喊|叫|吼|嚷|骂|斥|表示|回应|回答|解释|补充|提醒|追问|反问|低语"
    r"|嘟囔|喃喃|咕哝|嘀咕|念叨|插嘴|开口|出声)"
    r"(?:了|着|过)?(?:(?:一|两|几)?句话?|一声|几句话?|一遍)?(?:道)?$"
)
# A quote introduced like this is a thought / written text, deliberately left as narration.
_THOUGHT_INTRO_RE = re.compile(
    r"(?:心想|心道|暗想|暗道|心说|想道|想着|写着|刻着|显示着|写道|念道|读道|写下)[：:，,]?\s*$")
_TAG_PRONOUNS = ("他们", "她们", "它们", "我们", "你们", "他", "她", "它", "我", "你")
_CLAUSE_SEP_RE = re.compile(r"[，、；：,;:]")
_TAIL_PUNCT = "。，、；：！？…,.;:!? \t\u3000"


def _is_embedded_quote(before: str, quote: str, after: str) -> bool:
    """A short quote glued into the middle of a sentence ("一种“他不属于你”的感觉"): the text
    before it is mid-clause (no sentence / clause end, no speech verb that introduces it)
    and the sentence goes on after it. Such a quote is a mention or scare quote, not speech."""
    if len(_skeleton(quote)) > 12:
        return False
    head = before.rstrip()
    if not head or head[-1] in _TAIL_PUNCT or head[-1] in "\"" + "".join(_QUOTE_OPEN_TO_CLOSE) + "".join(_QUOTE_OPEN_TO_CLOSE.values()):
        return False
    if _SPEECH_TAIL_RE.search(head):
        return False
    return bool(after.strip()) and any(ch.isalnum() for ch in after)


def split_clauses(text: str) -> list[str]:
    """Clauses of ``text`` (each keeps its trailing separator), trailing punctuation included."""
    return [c for c in re.findall(r"[^，、；：,;:]*[，、；：,;:]+|[^，、；：,;:]+$", text) if c.strip()]


def is_pure_speech_tag(text: str) -> bool:
    """True iff ``text`` is ONE clause that ends in a speech verb: "杜尘笑道：", "他压低声音说，".
    Anything with a second clause ("杜尘冷冷地看了他一眼，说道：") or without a speech verb
    ("顾秋情叹了口气，") carries narration of its own and must not be deleted."""
    body = text.strip().rstrip(_TAIL_PUNCT)
    if not body or _CLAUSE_SEP_RE.search(body):
        return False
    return bool(_SPEECH_TAIL_RE.search(body))


def salvage_tag_edit(text: str) -> str | None:
    """For a unit the model asked to delete although it is "action + speech tag": the unit
    minus its trailing speech clause ("安知鱼拉了拉她的手，略微强势地说道。" → "安知鱼拉了拉她的手。").
    ``None`` when there is no such split (no trailing speech clause or nothing would be left)."""
    clauses = split_clauses(text.strip())
    if len(clauses) < 2:
        return None
    last = clauses[-1].rstrip(_TAIL_PUNCT)
    if not last or not _SPEECH_TAIL_RE.search(last):
        return None
    kept = "".join(clauses[:-1]).rstrip(_TAIL_PUNCT)
    if not _skeleton(kept):
        return None
    return kept + "。"


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def _split_greedy(text: str, max_chars: int) -> list[str]:
    """Cut ``text`` into pieces of at most ``max_chars`` at sentence ends (clause ends for
    a single over-long sentence). A piece with no boundary at all stays whole."""
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    cur = ""
    for sent in _SENT_TAIL_RE.findall(text):
        parts = [sent]
        if len(sent) > max_chars:
            parts = _CLAUSE_TAIL_RE.findall(sent) or [sent]
        for part in parts:
            if cur and len(cur) + len(part) > max_chars:
                pieces.append(cur)
                cur = ""
            cur += part
    if cur:
        pieces.append(cur)
    return pieces or [text]


def _scan_line(line: str) -> list[tuple]:
    """Split one line into ``(kind, text, closed)`` segments: quote spans and the plain
    text between them. ``closed`` is True when the span ends with its closing quote."""
    out: list[tuple] = []
    plain_start = 0
    i = 0
    n = len(line)
    while i < n:
        opener = line[i]
        closer = _QUOTE_OPEN_TO_CLOSE.get(opener)
        if closer is None:
            i += 1
            continue
        depth = 1
        j = i + 1
        end = -1
        while j < n:
            ch = line[j]
            if closer != opener and ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        terminated = end >= 0
        if not terminated:
            if opener not in _RUNON_OPENERS:
                i += 1
                continue
            end = n - 1  # unterminated: the quote runs to the end of the line
        if plain_start < i:
            out.append(("plain", line[plain_start:i], False))
        out.append(("quote", line[i:end + 1], terminated))
        i = end + 1
        plain_start = i
    if plain_start < n:
        out.append(("plain", line[plain_start:], False))
    return out


def segment_chunk(chunk: str, unit_max_chars: int = 80) -> list[Unit]:
    """Deterministically cut ``chunk`` into numbered units. Lossless: concatenating the
    unit texts reproduces the chunk with only line breaks and edge whitespace removed.

    Cut points: line breaks, quote-span boundaries, sentence ends in plain text, and
    (for a span longer than ``unit_max_chars``) sentence / clause ends inside it. A chapter
    title line is its own ``title`` unit. Punctuation-only plain fragments are *hidden*
    (``n == 0``): the model never sees them and :func:`assemble` glues them onto the
    preceding unit.
    """
    max_chars = max(10, int(unit_max_chars))
    units: list[Unit] = []

    def add(text: str, kind: str, lead: str = "", trail: str = "", *, para_start=False,
            whole=False, embedded=False) -> None:
        units.append(Unit(pos=len(units), n=0, text=text, kind=kind, lead=lead, trail=trail,
                          para_start=para_start, whole_line_quote=whole, embedded=embedded))

    pending: tuple | None = None   # (closer, lines_left): a quote opened on an earlier line
    for raw in chunk.splitlines():
        line = raw.strip()
        if not line:
            continue
        first = True
        if is_chapter_title(line):
            add(line, "title", para_start=True)
            pending = None
            continue
        if pending is not None and line[0] in _QUOTE_OPEN_TO_CLOSE:
            pending = None  # a fresh paragraph-opening quote ends the unclosed one
        if pending is not None:
            closer, left = pending
            idx = line.find(closer)
            if idx >= 0:
                segments = [("cont", line[:idx + 1], True)] + _scan_line(line[idx + 1:])
                pending = None
            else:
                segments = [("cont", line, False)]
                pending = (closer, left - 1) if left > 1 else None
        else:
            segments = _scan_line(line)
        if pending is None and segments:
            kind, text, closed = segments[-1]
            if kind == "quote" and not closed and text[0] in _RUNON_OPENERS:
                pending = (_QUOTE_OPEN_TO_CLOSE[text[0]], 3)
        whole_line = (
            len(segments) == 1 and segments[0][0] == "quote"
            or len(segments) == 2 and segments[0][0] == "quote" and segments[1][0] == "plain"
            and not any(ch.isalnum() for ch in segments[1][1])
        )
        for seg_i, (kind, text, closed) in enumerate(segments):
            if kind == "plain":
                for sent in _SENT_TAIL_RE.findall(text):
                    if not sent.strip():
                        continue
                    add(sent, "plain", para_start=first)
                    first = False
                continue
            pieces = _split_greedy(text, max_chars)
            embedded = (
                kind == "quote" and len(pieces) == 1 and closed
                and 0 < seg_i < len(segments) - 1
                and segments[seg_i - 1][0] == "plain" and segments[seg_i + 1][0] == "plain"
                and _is_embedded_quote(segments[seg_i - 1][1], text, segments[seg_i + 1][1])
            )
            for k, piece in enumerate(pieces):
                lead = piece[0] if k == 0 and kind == "quote" and text[0] in _QUOTE_OPEN_TO_CLOSE else ""
                trail = piece[-1] if k == len(pieces) - 1 and closed else ""
                add(piece, "quote", lead, trail, para_start=first,
                    whole=whole_line and len(pieces) == 1, embedded=embedded)
                first = False

    number = 0
    for unit in units:
        hidden = (
            unit.kind == "plain" and unit.pos > 0
            and not any(ch.isalnum() for ch in unit.text)
        )
        if not hidden:
            number += 1
            unit.n = number
    return units


def render_units(units: list[Unit]) -> str:
    """The numbered list shown to the model: one ``[n] text`` line per visible unit, with
    a blank line wherever a new source paragraph starts."""
    lines: list[str] = []
    for unit in units:
        if not unit.n:
            continue
        if unit.para_start and lines:
            lines.append("")
        lines.append(f"[{unit.n}] {unit.text}")
    return "\n".join(lines)


def likely_dialogue_numbers(units: list[Unit]) -> list[int]:
    """Visible units that are almost certainly spoken: a quote span that fills its line.
    Used only as a sanity signal against replies that silently label nothing."""
    return [u.n for u in units if u.n and u.whole_line_quote]


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------

@dataclass
class UnitLabel:
    kind: str                # "S" speaker | "X" delete | "E" edit | "B" break
    speaker: str = ""
    instruct: str = ""
    text: str = ""


@dataclass
class UnitPlan:
    labels: dict = field(default_factory=dict)   # unit number -> UnitLabel
    ended: bool = False                           # saw the END terminator
    bad_lines: int = 0
    total_lines: int = 0


_LINE_RE = re.compile(r"^\[?(\d+)\]?(?:\s*[-~–—]\s*\[?(\d+)\]?)?\s+(.+)$")


def _norm_speaker(value: str) -> str:
    value = value.strip().strip(_SPEAKER_STRIP).strip()
    if value.upper() in ("N", "NARRATOR") or value == "旁白":
        return NARRATOR
    return value


def _norm_instruct(value: str) -> str:
    value = value.strip()
    return "" if value in ("", "-", "—", "无") else value


def parse_unit_reply(reply: str | None, n_units: int) -> UnitPlan:
    """Parse the model's label lines. Robust to thinking tags and code fences; anything
    unreadable counts in ``bad_lines`` and is otherwise ignored (the unit stays narration)."""
    plan = UnitPlan()
    if not reply:
        return plan
    text = _clean_reply(_strip_thinking(reply)).strip()
    for raw in text.splitlines():
        line = raw.strip().strip("`").strip()
        if not line:
            continue
        if line.upper() == "END":
            plan.ended = True
            break
        plan.total_lines += 1
        m = _LINE_RE.match(line)
        if not m:
            plan.bad_lines += 1
            continue
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        rest = m.group(3).strip()
        if lo < 1 or hi < lo or hi > n_units or hi - lo > 200:
            plan.bad_lines += 1
            continue
        label = _parse_label(rest)
        if label is None or (label.kind in ("E", "B") and hi != lo):
            plan.bad_lines += 1
            continue
        fresh = [k for k in range(lo, hi + 1) if k not in plan.labels]
        if not fresh:
            plan.bad_lines += 1  # duplicate line: the first one wins
            continue
        for k in fresh:
            plan.labels[k] = label
    return plan


def _parse_label(rest: str) -> UnitLabel | None:
    head = rest.split(None, 1)[0] if rest else ""
    if rest.upper() == "X":
        return UnitLabel("X")
    if rest.upper() == "B":
        return UnitLabel("B")
    if head.upper() == "E" and len(rest) > 1 and rest[1] in " \t|":
        fields = [f.strip() for f in rest[1:].split("|", 2)]
        if len(fields) < 3:
            return None
        speaker = _norm_speaker(fields[0])
        if not speaker or not fields[2]:
            return None
        return UnitLabel("E", speaker=speaker, instruct=_norm_instruct(fields[1]), text=fields[2])
    fields = [f.strip() for f in rest.split("|", 1)]
    speaker = _norm_speaker(fields[0])
    if not speaker or len(speaker) > 40:
        return None
    return UnitLabel("S", speaker=speaker,
                     instruct=_norm_instruct(fields[1]) if len(fields) > 1 else "")


# ---------------------------------------------------------------------------
# Edit validation
# ---------------------------------------------------------------------------

def validate_edit(orig: str, new: str, max_delete: int = 24) -> bool:
    """True iff ``new`` is ``orig`` with exactly one contiguous span of word characters
    removed (punctuation is free), the span is at most ``max_delete`` word characters and
    contains a speech verb, and nothing but ordinary punctuation was added."""
    sk_orig, sk_new = _skeleton(orig), _skeleton(new)
    deleted = len(sk_orig) - len(sk_new)
    if not sk_new or deleted <= 0 or deleted > max(1, int(max_delete)):
        return False
    prefix = 0
    limit = len(sk_new)
    while prefix < limit and sk_orig[prefix] == sk_new[prefix]:
        prefix += 1
    if sk_orig[prefix + deleted:] != sk_new[prefix:]:
        return False
    gone = sk_orig[prefix:prefix + deleted]
    if not (any(ch in _EDIT_VERB_CHARS for ch in gone) or any(w in gone for w in _EDIT_VERB_PHRASES)):
        return False
    allowed = set(orig) | set(_EDIT_PUNCT)
    return all(ch.isalnum() or ch.isspace() or ch in allowed for ch in new)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Balanced grouping of mechanically merged pieces
# ---------------------------------------------------------------------------

_BALANCE_MAX_PIECES = 400


def balanced_groups(sizes: list[int], cap: int, joins: list[int] | None = None,
                    cut_cost: list[float] | None = None) -> list[tuple[int, int]]:
    """Partition consecutive pieces (``sizes[i]`` code points each) into the FEWEST contiguous
    groups of at most ``cap`` code points, as EVEN as possible. A run of 150 with cap 100 comes
    out 75 + 75, not 100 + 50: the cut falls on a piece boundary (a sentence / paragraph end the
    caller guarantees), never inside a piece. ``joins[i]`` = characters inserted between piece
    ``i`` and ``i+1`` when they share a group; ``cut_cost[j]`` = penalty for cutting before piece
    ``j`` (paragraph starts are cheap, mid-sentence-looking cuts expensive). A single piece
    larger than ``cap`` is its own group. Ties prefer the larger earlier group. Returns
    half-open ``(start, end)`` index pairs.
    """
    n = len(sizes)
    if n == 0:
        return []
    if n > _BALANCE_MAX_PIECES:   # pathological run: the O(k·n²) DP is not worth it, fill greedily
        groups_g, start = [], 0
        for j in range(1, n + 1):
            if j == n or sum(sizes[start:j + 1]) > max(1, int(cap)):
                groups_g.append((start, j))
                start = j
        return groups_g
    joins = joins or [0] * max(0, n - 1)
    cut_cost = cut_cost or [0.0] * n
    cap = max(1, int(cap))
    prefix = [0]
    for i, size in enumerate(sizes):
        prefix.append(prefix[-1] + size + (joins[i] if i < n - 1 else 0))

    def span(i: int, j: int) -> int:          # size of pieces i..j-1 as one group
        return prefix[j] - prefix[i] - (joins[j - 1] if j - 1 < n - 1 else 0)

    # Fewest groups: greedy fill.
    groups = 1
    start = 0
    for j in range(1, n):
        if span(start, j + 1) > cap and j - start >= 1:
            groups += 1
            start = j
    inf = float("inf")
    dp = [[inf] * (n + 1) for _ in range(groups + 1)]
    back = [[0] * (n + 1) for _ in range(groups + 1)]
    dp[0][0] = 0.0
    for g in range(1, groups + 1):
        for j in range(g, n + 1):
            best, best_i = inf, -1
            for i in range(g - 1, j):
                if dp[g - 1][i] == inf:
                    continue
                size = span(i, j)
                if size > cap and j - i > 1:
                    continue
                cost = dp[g - 1][i] + size * size + (cut_cost[i] if i > 0 else 0.0)
                if cost <= best:
                    best, best_i = cost, i
            dp[g][j], back[g][j] = best, best_i
    if dp[groups][n] == inf:                  # cannot happen (greedy proves feasibility); stay safe
        return [(i, i + 1) for i in range(n)]
    out: list = []
    j = n
    for g in range(groups, 0, -1):
        i = back[g][j]
        out.append((i, j))
        j = i
    return out[::-1]


def _piece_cut_costs(pieces: list, cap: int) -> list[float]:
    """Cut penalties for :func:`balanced_groups`: free before a paragraph start, small after a
    sentence end, larger after a clause-level piece."""
    base = 0.02 * cap * cap
    costs = [0.0]
    for k in range(1, len(pieces)):
        prev_text, _ins, _para = pieces[k - 1]
        nxt_para = pieces[k][2]
        tail = prev_text.rstrip()[-1:] if prev_text.strip() else ""
        sentence_end = tail in "。！？!?…”」』\"" or tail == "’"
        costs.append(0.0 if nxt_para else (base if sentence_end else 3 * base))
    return costs


_WATERMARK_RE = re.compile(r"https?://|www\.|\.(?:com|net|org|cn|cc|me|top|info)\b", re.IGNORECASE)


def delete_allowed(unit_text: str, max_chars: int) -> bool:
    """Whether an ``X`` label may delete this unit: a URL / site-watermark line of any
    length, or a short *pure speech tag* (one clause ending in a speech verb — see
    :func:`is_pure_speech_tag`). Narration of any kind is kept even if the model asked to
    drop it: "顾秋情叹了口气，" and "安知鱼摇了摇头，说道：" are story, not tags."""
    if _WATERMARK_RE.search(unit_text):
        return True
    return len(_skeleton(unit_text)) <= max(1, int(max_chars)) and is_pure_speech_tag(unit_text)


@dataclass
class AssembleResult:
    entries: list
    edit_applied: int = 0
    edit_rejected: int = 0
    delete_rejected: int = 0
    delete_salvaged: int = 0     # X on "action + speech tag": only the speech clause dropped
    demoted: int = 0             # dialogue labels downgraded to narration (embedded / unquoted)
    narrated_quotes: list = field(default_factory=list)   # quote units that ended up as narration
    edit_rejections: list = field(default_factory=list)   # (unit text, proposed text)
    deleted: int = 0
    ok: bool = True      # skeleton self-check: no text lost or invented by the stitching


def _strip_quote_chars(text: str, lead: str, trail: str) -> str:
    if lead and text.startswith(lead):
        text = text[len(lead):]
    if trail and text.endswith(trail) and text:
        text = text[:-len(trail)]
    return text


def _instruct_weight(value: str) -> int:
    return sum(1 for ch in value if ch.isalnum())


def assemble(units: list[Unit], plan: UnitPlan, narrator_instruct: str, soft_max: int = 150,
             *, edit_enabled: bool = True, edit_max_delete: int = 24,
             delete_max_chars: int = 30, unquoted_guard: bool = True) -> AssembleResult:
    """Stitch labelled units back into ``{speaker, text, instruct}`` entries.

    * Unlisted units are narration; ``X`` units vanish (only short tag-like units or
      URL / watermark lines — see :func:`delete_allowed`; a longer ``X`` is ignored); a quote-wrapped unit labelled as a
      character loses its outer quote characters.
    * ``X`` only deletes a pure speech tag (one clause ending in a speech verb) or a
      watermark. An ``X`` on "action + speech tag" keeps the action and drops only the
      speech clause (``delete_salvaged``); an ``X`` on any other narration is ignored.
    * Adjacent same-speaker units of one paragraph merge into one entry (a pure speech tag
      deleted between them is transparent); dialogue never merges across a paragraph break.
    * A dialogue label is downgraded to narration (``demoted``) on a short quote embedded
      mid-sentence, and — in a quote-style chunk — on an unquoted unit that does not follow
      a colon (an inner thought mislabelled as speech).
      Narration merges across lines but never across a title, dialogue or ``B``. Neither
      kind of entry grows past ``soft_max`` (callers pass half of the administrator's hard
      cap: the longer one TTS entry runs, the faster the voice drifts) — a unit that would
      cross it starts a new entry.
    * A narration entry that ends in ``，：、`` right before another speaker gets ``。``.
    """
    result = AssembleResult(entries=[])
    entries: list = []
    cur: dict | None = None        # {"speaker", "pieces": [[text, instruct, para_start]], "instruct"}
    forced_break = False
    quote_style = unquoted_guard and sum(1 for u in units if u.kind == "quote") >= 3
    prev_text = ""                 # raw text of the previous visible unit
    visible = [u for u in units if u.n]
    vis_at = {u.pos: k for k, u in enumerate(visible)}

    names = {lab.speaker for lab in plan.labels.values()
             if lab.kind in ("S", "E") and lab.speaker and lab.speaker != NARRATOR}

    def has_subject(text: str) -> bool:
        """A tag the model left alone is only dropped when it plainly names who speaks (a known
        speaker or a pronoun): "那声惨叫" / "他读着小说" end like a speech verb but are not tags."""
        head = text.strip()
        return head.startswith(_TAG_PRONOUNS) or any(head.startswith(n) for n in names)

    def speaks(u: Unit) -> bool:
        lab = plan.labels.get(u.n)
        return (u.kind == "quote" and lab is not None and lab.kind in ("S", "E")
                and lab.speaker != NARRATOR)

    def beside_speech(u: Unit) -> bool:
        """``u`` sits in the same paragraph right next to a quote the model gave to a character."""
        k = vis_at.get(u.pos, -1)
        nxt = visible[k + 1] if 0 <= k + 1 < len(visible) else None
        prv = visible[k - 1] if k > 0 else None
        return bool((nxt is not None and not nxt.para_start and speaks(nxt))
                    or (prv is not None and not u.para_start and speaks(prv)))

    source_skeleton = sum(len(_skeleton(u.text)) for u in units)
    removed_skeleton = 0   # word characters legitimately dropped by X units and E edits

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            pieces = cur["pieces"]
            # Mechanical merging never builds an entry past ``soft_max``; a run that would is cut
            # at piece (sentence / paragraph) boundaries into the fewest, EVENLY sized entries.
            groups = balanced_groups([len(p[0]) for p in pieces], soft_max,
                                     cut_cost=_piece_cut_costs(pieces, soft_max))
            for lo, hi in groups:
                chunk = pieces[lo:hi]
                if cur["speaker"] == NARRATOR:
                    instruct = cur["instruct"]
                else:
                    instruct = chunk[0][1]
                    for _text, ins, _para in chunk[1:]:
                        if _instruct_weight(ins) > _instruct_weight(instruct):
                            instruct = ins
                entries.append({
                    "speaker": cur["speaker"],
                    "text": "".join(p[0] for p in chunk),
                    "instruct": instruct,
                })
            cur = None

    def start(speaker: str, instruct: str) -> dict:
        nonlocal cur
        flush()
        cur = {"speaker": speaker, "pieces": [], "instruct": instruct}
        return cur

    for unit in units:
        if unit.n == 0:  # hidden punctuation glue: rides on the piece before it
            if cur is None:
                start(NARRATOR, narrator_instruct)
            if cur["pieces"]:
                cur["pieces"][-1][0] += unit.text
            else:
                cur["pieces"].append([unit.text, cur["instruct"], False])
            continue

        label = plan.labels.get(unit.n)
        if unit.kind == "title":
            start(NARRATOR, narrator_instruct)["pieces"].append([unit.text, narrator_instruct, True])
            flush()
            forced_break = False
            prev_text = unit.text
            continue
        salvaged = None
        if label is not None and label.kind == "X" and not delete_allowed(unit.text, delete_max_chars):
            result.delete_rejected += 1
            salvaged = salvage_tag_edit(unit.text)
            if salvaged is not None and edit_enabled and validate_edit(unit.text, salvaged, edit_max_delete):
                result.delete_salvaged += 1
            else:
                salvaged = None
            label = None
        if label is not None and label.kind == "X":
            result.deleted += 1
            removed_skeleton += len(_skeleton(unit.text))
            prev_text = unit.text
            continue
        if label is not None and label.kind == "B":
            forced_break = True
            label = None
        prior_text = prev_text
        body = unit.text.strip().rstrip(_TAIL_PUNCT)
        if (label is not None and label.kind in ("S", "E") and label.speaker != NARRATOR
                and ((unit.embedded and unquoted_guard)
                     or (quote_style and unit.kind == "plain"
                         and not any(ch in _QUOTE_OPEN_TO_CLOSE or ch in "：:" for ch in body)
                         and not prior_text.rstrip().endswith(("：", ":"))))):
            # An unquoted unit is narration even if the model gave it a speaker; an edit on
            # it (dropping the speech tag) is still honoured, as a narration edit.
            result.demoted += 1
            label = UnitLabel("E", speaker=NARRATOR, text=label.text) if label.kind == "E" else None
        prev_text = unit.text

        if label is None and salvaged is None and unit.kind == "plain" and beside_speech(unit):
            # A speech tag the model left in the narration next to its dialogue: drop a pure
            # tag, or just the trailing speech clause of "action + tag" (same rule as X).
            if (is_pure_speech_tag(unit.text) and has_subject(unit.text)
                    and len(_skeleton(unit.text)) <= max(1, int(delete_max_chars))):
                result.deleted += 1
                removed_skeleton += len(_skeleton(unit.text))
                continue
            auto = salvage_tag_edit(unit.text)
            if auto is not None and edit_enabled and validate_edit(unit.text, auto, edit_max_delete):
                salvaged = auto
                result.delete_salvaged += 1

        speaker, instruct, text = NARRATOR, narrator_instruct, salvaged if salvaged is not None else unit.text
        if label is not None and label.kind in ("S", "E") and label.speaker != NARRATOR:
            speaker = label.speaker
            instruct = label.instruct
            text = unit.text
            if label.kind == "E" and _skeleton(label.text) == _skeleton(unit.text):
                pass  # nothing was deleted: not an edit, keep the unit as is
            elif label.kind == "E":
                if edit_enabled and validate_edit(unit.text, label.text, edit_max_delete):
                    text = label.text
                    result.edit_applied += 1
                else:
                    result.edit_rejected += 1
                    result.edit_rejections.append((unit.text, label.text))
            text = _strip_quote_chars(text, unit.lead, unit.trail)
        elif label is not None and label.kind == "E" and _skeleton(label.text) == _skeleton(unit.text):
            pass  # no-op edit: keep the unit as is
        elif label is not None and label.kind == "E":
            # Narration edit (typically "action, then a speech tag" → keep the action).
            if edit_enabled and validate_edit(unit.text, label.text, edit_max_delete):
                text = label.text
                result.edit_applied += 1
            else:
                result.edit_rejected += 1
                result.edit_rejections.append((unit.text, label.text))

        removed_skeleton += len(_skeleton(unit.text)) - len(_skeleton(text))
        if (speaker == NARRATOR and unit.kind == "quote" and not unit.embedded
                and len(_skeleton(unit.text)) >= 1 and not _THOUGHT_INTRO_RE.search(prior_text)):
            result.narrated_quotes.append(unit.n)
        joinable = (
            cur is not None and not forced_break and cur["speaker"] == speaker
            and (speaker == NARRATOR or not unit.para_start)
        )
        if not joinable:
            start(speaker, instruct)
        cur["pieces"].append([text, instruct, unit.para_start])
        forced_break = False
    flush()

    for i, entry in enumerate(entries[:-1]):
        nxt = entries[i + 1]
        if (entry["speaker"] == NARRATOR and nxt["speaker"] != NARRATOR
                and entry["text"] and entry["text"][-1] in "，：、"):
            entry["text"] = entry["text"][:-1] + "。"

    result.entries = [e for e in entries if e["text"].strip()]
    # Self-check: stitching may delete only what X/E removed, and nothing else.
    kept = sum(len(_skeleton(e["text"])) for e in result.entries)
    result.ok = kept + removed_skeleton == source_skeleton
    return result


def entry_has_unclaimed_quote(text: str, unit_max_chars: int = 80) -> bool:
    """True iff ``text`` (a stored narration entry) holds a quote span that is neither a
    mid-sentence mention nor introduced as a thought / written text: dialogue the narrator
    would read with its quote marks."""
    units = [u for u in segment_chunk(text, unit_max_chars) if u.n]
    for k, unit in enumerate(units):
        if unit.kind != "quote" or unit.embedded or not _skeleton(unit.text):
            continue
        if k and _THOUGHT_INTRO_RE.search(units[k - 1].text):
            continue
        return True
    return False
