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
        if end < 0:
            if opener not in _RUNON_OPENERS:
                i += 1
                continue
            end = n - 1  # unterminated: the quote runs to the end of the line
        if plain_start < i:
            out.append(("plain", line[plain_start:i], False))
        out.append(("quote", line[i:end + 1], line[end] == closer and end > i))
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
            whole=False) -> None:
        units.append(Unit(pos=len(units), n=0, text=text, kind=kind, lead=lead, trail=trail,
                          para_start=para_start, whole_line_quote=whole))

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
        for kind, text, closed in segments:
            if kind == "plain":
                for sent in _SENT_TAIL_RE.findall(text):
                    if not sent.strip():
                        continue
                    add(sent, "plain", para_start=first)
                    first = False
                continue
            pieces = _split_greedy(text, max_chars)
            for k, piece in enumerate(pieces):
                lead = piece[0] if k == 0 and kind == "quote" and text[0] in _QUOTE_OPEN_TO_CLOSE else ""
                trail = piece[-1] if k == len(pieces) - 1 and closed else ""
                add(piece, "quote", lead, trail, para_start=first,
                    whole=whole_line and len(pieces) == 1)
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

_WATERMARK_RE = re.compile(r"https?://|www\.|\.(?:com|net|org|cn|cc|me|top|info)\b", re.IGNORECASE)


def delete_allowed(unit_text: str, max_chars: int) -> bool:
    """Whether an ``X`` label may delete this unit: a short tag-like unit, or a URL /
    site-watermark line of any length. A long unit without a URL is story (or a
    translator's note) and is kept even if the model asked to drop it."""
    return len(_skeleton(unit_text)) <= max(1, int(max_chars)) or bool(_WATERMARK_RE.search(unit_text))


@dataclass
class AssembleResult:
    entries: list
    edit_applied: int = 0
    edit_rejected: int = 0
    delete_rejected: int = 0
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
             delete_max_chars: int = 30) -> AssembleResult:
    """Stitch labelled units back into ``{speaker, text, instruct}`` entries.

    * Unlisted units are narration; ``X`` units vanish (only short tag-like units or
      URL / watermark lines — see :func:`delete_allowed`; a longer ``X`` is ignored); a quote-wrapped unit labelled as a
      character loses its outer quote characters.
    * Adjacent same-speaker units merge into one entry — an ``X`` between them is
      transparent (a character's words split only by a pure speech tag are one entry),
      and dialogue merges across a paragraph break only through such an ``X``.
      Narration merges across lines but never across a title, dialogue or ``B`` and is
      cut at a unit boundary once it would exceed ``soft_max``.
    * A narration entry that ends in ``，：、`` right before another speaker gets ``。``.
    """
    result = AssembleResult(entries=[])
    entries: list = []
    cur: dict | None = None        # {"speaker", "parts", "instruct", "len"}
    pending_x = False              # an X unit was seen since the last kept unit
    forced_break = False
    source_skeleton = sum(len(_skeleton(u.text)) for u in units)
    removed_skeleton = 0   # word characters legitimately dropped by X units and E edits

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            entries.append({
                "speaker": cur["speaker"],
                "text": "".join(cur["parts"]),
                "instruct": cur["instruct"],
            })
            cur = None

    def start(speaker: str, instruct: str) -> dict:
        nonlocal cur
        flush()
        cur = {"speaker": speaker, "parts": [], "instruct": instruct, "len": 0}
        return cur

    for unit in units:
        if unit.n == 0:  # hidden punctuation glue
            if cur is None:
                start(NARRATOR, narrator_instruct)
            cur["parts"].append(unit.text)
            cur["len"] += len(unit.text)
            continue

        label = plan.labels.get(unit.n)
        if unit.kind == "title":
            start(NARRATOR, narrator_instruct)["parts"].append(unit.text)
            flush()
            pending_x, forced_break = False, False
            continue
        if label is not None and label.kind == "X" and not delete_allowed(unit.text, delete_max_chars):
            result.delete_rejected += 1
            label = None
        if label is not None and label.kind == "X":
            result.deleted += 1
            removed_skeleton += len(_skeleton(unit.text))
            pending_x = True
            continue
        if label is not None and label.kind == "B":
            forced_break = True
            label = None

        speaker, instruct, text = NARRATOR, narrator_instruct, unit.text
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
        joinable = (
            cur is not None and not forced_break and cur["speaker"] == speaker
            and (speaker == NARRATOR and cur["len"] + len(text) <= soft_max
                 or speaker != NARRATOR and (pending_x or not unit.para_start))
        )
        if not joinable:
            start(speaker, instruct)
        cur["parts"].append(text)
        cur["len"] += len(text)
        if speaker != NARRATOR and _instruct_weight(instruct) > _instruct_weight(cur["instruct"]):
            cur["instruct"] = instruct
        pending_x, forced_break = False, False
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
