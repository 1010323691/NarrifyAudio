"""Offline tests for the character voice-prep engine's pure logic
(``backend/engines/voices.py``).

These pin the pure helpers of the voice-prep engine — JSON extraction from LLM
output, name normalisation, token-Jaccard, canonical-name resolution (the basis of
alias folding), target-line sampling and per-line context windows (the persona
evidence basis), ref-text selection, and the filename sanitizer — against known
inputs, with no network or engine access. Mirrors the style and focus of
``test_script.py``.

The lower half of this module also exercises the Phase-2 clone pipeline end to end —
``make_clones``'s per-character candidate rendering, the candidate/selection
bookkeeping behind ``list_voices`` and the select endpoint — in a throwaway workspace,
with the TTS engine stubbed by a fake design worker (a real subprocess, no torch).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.tts import (
    MergeSpeakersRequest,
    SelectVoiceRequest,
    _clone_status,
    _foundation_status,
    list_voices,
    merge_speakers,
    select_voice,
)
import backend.api.tts as tts_api
from backend.platform.deps import AuthContext
from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core.task_control import TaskCancelled
from backend.engines import voices as V
from backend.engines.voices import (
    _clone_done,
    _clone_have,
    _effective_line_counts,
    _fallback_persona,
    _has_foundation,
    _resolve_to_canonical,
    _sanitize,
    _select_target_bands,
    _token_jaccard,
    _window_block,
    auto_candidate_count,
    effective_candidates,
    extract_json_object,
    normalize_speaker_name,
    pick_ref_text,
)
from fastapi import HTTPException

BS = chr(92)  # backslash — built via chr() so no literal backslashes live in this file


# --------------------------------------------------------------------------- #
# extract_json_object
# --------------------------------------------------------------------------- #

def test_extract_json_object_contract():
    # extract simple object
    assert extract_json_object('{"description":"d","ref_text":"r"}') == {
        "description": "d", "ref_text": "r",
    }

    # extract embedded in prose
    got = extract_json_object('Here you go: {"description":"x"} and more')
    assert got == {"description": "x"}

    # extract nested object
    assert extract_json_object('{"a": {"b": 1}}') == {"a": {"b": 1}}

    # extract ignores brace in string
    assert extract_json_object('{"s":"a}b"}') == {"s": "a}b"}

    # extract ignores escaped quote
    # raw JSON text: {"a":"a\"b"}  — the \" must not close the string
    raw = '{"a":"a' + BS + '"b"}'
    assert extract_json_object(raw) == {"a": 'a"b'}

    # extract none without braces
    assert extract_json_object("no braces here") is None

    # extract none when unbalanced
    assert extract_json_object('{"a":1') is None


# --------------------------------------------------------------------------- #
# normalize_speaker_name
# --------------------------------------------------------------------------- #

def test_normalize_speaker_name_contract():
    for args, expected in [
        (('Dr. Smith',), 'smith'),
        (('  Mr. Jones  ',), 'jones'),
        (('Prof. Ada',), 'ada'),
        (('Alice',), 'alice'),
        (("O'Brien",), 'obrien'),
        (('John   Smith',), 'john smith'),
        ((123,), ''),
        ((None,), ''),
        (('张三',), '张三'),
        (('熊猫A',), '熊猫a'),
        (('aＫ',), 'ak'),
    ]:
        assert normalize_speaker_name(*args) == expected, args


# --------------------------------------------------------------------------- #
# _token_jaccard
# --------------------------------------------------------------------------- #

def test_token_jaccard_contract():
    for args, expected in [
        (('John Smith', 'john smith',), 1.0),
        (('John', 'Smith',), 0.0),
        (('John Smith', 'John Doe',), 1 / 3),
        (('', 'John',), 0.0),
    ]:
        assert _token_jaccard(*args) == expected, args


# --------------------------------------------------------------------------- #
# _resolve_to_canonical
# --------------------------------------------------------------------------- #

def test_canonical_name_resolution():
    # canonical exact match
    # "Smith" -> "smith" matches "Dr. Smith" -> "smith" after normalization.
    assert _resolve_to_canonical("Smith", ["John", "Dr. Smith"]) == "Dr. Smith"

    # canonical exact after normalize
    assert _resolve_to_canonical("Dr. Smith", ["John", "Smith"]) == "Smith"

    # canonical substring
    assert _resolve_to_canonical("Smithson", ["Smith"]) == "Smith"

    # canonical jaccard pass
    # Neither exact nor substring; the token-Jaccard (1/3) clears the low threshold.
    assert _resolve_to_canonical("John Peter", ["John James"], threshold=0.2) == "John James"

    # canonical jaccard below threshold
    assert _resolve_to_canonical("John Peter", ["John James"], threshold=0.5) is None

    # canonical empty raw
    assert _resolve_to_canonical("", ["John"]) is None

    # Different CJK names remain distinct.
    assert _resolve_to_canonical("张三", ["李四"]) is None


def test_auto_aliases_preserve_distinct_chinese_roles():
    config = {"NARRATOR": {"type": "foundation"}, "熊猫A": {"type": "foundation"}}
    unique, aliases = V._fold_aliases(_Handle(), ["NARRATOR", "熊猫A", "aＫ", "小熊猫"], config)
    assert unique == ["NARRATOR", "熊猫A", "aＫ", "小熊猫"]
    assert "NARRATOR" not in aliases and "aＫ" not in aliases
    assert all("alias_of" not in entry for entry in config.values())


def test_similar_names_never_create_aliases():
    config = {"Alice": {"type": "clone"}, "Bob": {"alias_of": "Alice"}}
    before = json.dumps(config, sort_keys=True)
    unique, aliases = V._fold_aliases(_Handle(), ["Ａｌｉｃｅ", "alice", "Bob", "A"], config)
    assert unique == ["Ａｌｉｃｅ", "alice", "Bob", "A"]
    assert aliases == {"alice": "Alice", "Ａｌｉｃｅ": "Alice"}
    assert json.dumps(config, sort_keys=True) == before
    ambiguous = {"Alice": {"type": "foundation"}, "ALICE": {"type": "foundation"}}
    assert V._fold_aliases(_Handle(), ["alice"], ambiguous)[0] == ["alice"]


def test_voice_readiness_uses_own_voice_despite_hint():
    from backend.api.tts import _voice_ready

    config = {"A": {"alias_of": "B", "type": "foundation"},
              "B": {"type": "clone", "ref_audio": "other.wav"}}
    assert not _voice_ready("A", config)
    config["A"].update(type="clone", ref_audio="own.wav")
    assert _voice_ready("A", config)
    config["B"]["alias_of"] = "A"
    assert _voice_ready("A", config)
    config["A"]["alias_of"] = "Missing"
    assert _voice_ready("A", config)


def test_hint_cycle_does_not_block_voice_candidates(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"NARRATOR": 3, "熊猫A": 1})
    _seed_foundations(clone_ws, ["NARRATOR"], extra={
        "熊猫A": {"alias_of": "NARRATOR"},
    })
    config = _load_vc(clone_ws)
    config["NARRATOR"]["alias_of"] = "熊猫A"
    path = clone_ws / "04_voice_profiles" / "voice_config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    _stub_design_engine(monkeypatch, clone_ws)

    result = V.generate_voice_candidates(_Handle(), speakers=["NARRATOR"], candidate_count=1)

    assert result["ok"] == 1
    repaired = _load_vc(clone_ws)
    assert repaired["NARRATOR"]["type"] == "clone"
    assert repaired["NARRATOR"]["alias_of"] == "熊猫A"
    assert repaired["熊猫A"]["alias_of"] == "NARRATOR"
    assert (clone_ws / repaired["NARRATOR"]["ref_audio"]).is_file()


def test_similar_names_get_independent_foundations(clone_ws):
    names = ["Alice", "Ａｌｉｃｅ", "alice", "熊猫A", "aＫ"]
    _seed_script(clone_ws, dict.fromkeys(names, 1))
    _seed_foundations(clone_ws, ["Alice"])

    result = V.prepare_foundations(_Handle(), overrides=dict.fromkeys(names, "独立角色声音描述"))

    assert result["count"] == len(names)
    assert result["aliases"] > 0
    config = _load_vc(clone_ws)
    assert set(config) == set(names)
    assert all(entry["foundation_status"] == "done" and not entry.get("alias_of")
               for entry in config.values())


def test_name_and_profile_hint_is_visible_without_blocking_clone_submission(clone_ws):
    _seed_script(clone_ws, {"贝尔蒙德": 20, "小贝贝": 2})
    _seed_foundations(clone_ws, ["贝尔蒙德", "小贝贝"], extra={
        name: {"type": "foundation", "description": "幼女，女声", "foundation_status": "done"}
        for name in ["贝尔蒙德", "小贝贝"]
    })
    path = clone_ws / "04_voice_profiles" / "voice_config.json"
    original = path.read_bytes()
    rows = {row["name"]: row for row in list_voices()["speakers"]}
    assert rows["小贝贝"]["alias_of"] == "贝尔蒙德"
    assert rows["小贝贝"]["status"] == "pending"
    assert tts_api._voice_task_speakers(None, None, False, clone=True) == ["贝尔蒙德", "小贝贝"]
    assert path.read_bytes() == original


def test_foundation_checkpoint_does_not_restore_removed_alias(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"Alice": 1, "Bob": 1})
    _seed_foundations(clone_ws, ["Alice"], extra={"Bob": {"alias_of": "Alice"}})
    original_fold = V._fold_aliases

    def remove_link_after_snapshot(*args):
        result = original_fold(*args)
        config = _load_vc(clone_ws)
        config["Bob"].pop("alias_of")
        (clone_ws / "04_voice_profiles" / "voice_config.json").write_text(
            json.dumps(config), encoding="utf-8")
        return result

    monkeypatch.setattr(V, "_fold_aliases", remove_link_after_snapshot)
    V.prepare_foundations(_Handle(), overrides={"Alice": "独立角色声音描述"})
    assert "alias_of" not in _load_vc(clone_ws)["Bob"]


# --------------------------------------------------------------------------- #
# _select_target_bands  (optimization A: front / middle / back sampling)
# --------------------------------------------------------------------------- #

def _pairs(n, start=0):
    return [(start + k, f"line {k}") for k in range(n)]


def test_target_sampling_bands():
    # bands empty
    assert _select_target_bands([]) == ([], [], [])

    # bands few lines all to front
    # Fewer than 3 * per lines: everything folds into 'front' (none dropped).
    assert _select_target_bands(_pairs(5)) == ([0, 1, 2, 3, 4], [], [])

    # bands front back middle spread
    front, middle, back = _select_target_bands(_pairs(40))
    assert front == list(range(0, 8))
    assert back == list(range(32, 40))
    # 8 lines spread evenly through the middle region (indices 8..31).
    assert middle == [8, 11, 15, 18, 21, 24, 28, 31]
    assert all(8 <= x <= 31 for x in middle)

    # bands are disjoint
    front, middle, back = _select_target_bands(_pairs(60))
    seen = set(front) | set(middle) | set(back)
    assert len(seen) == len(front) + len(middle) + len(back)  # no overlap


# --------------------------------------------------------------------------- #
# _window_block  (optimization A: per-line ±context, any speaker)
# --------------------------------------------------------------------------- #

def test_context_window_contract():
    # window block marks target and keeps order
    script = [
        {"speaker": "NARRATOR", "text": "c0"},
        {"speaker": "Bob", "text": "c1"},
        {"speaker": "Alice", "text": "target"},
        {"speaker": "NARRATOR", "text": "c2"},
    ]
    assert _window_block(script, 2, window=4) == (
        "   NARRATOR: c0\n   Bob: c1\n★ Alice: target\n   NARRATOR: c2"
    )

    # window block clamps at start
    script = [
        {"speaker": "Alice", "text": "t0"},
        {"speaker": "NARRATOR", "text": "c1"},
        {"speaker": "Bob", "text": "c2"},
    ]
    assert _window_block(script, 0, window=4) == (
        "★ Alice: t0\n   NARRATOR: c1\n   Bob: c2"
    )

    # window block respects window
    script = [
        {"speaker": "NARRATOR", "text": "c0"},
        {"speaker": "NARRATOR", "text": "c1"},
        {"speaker": "Alice", "text": "target"},
        {"speaker": "NARRATOR", "text": "c2"},
        {"speaker": "NARRATOR", "text": "c3"},
    ]
    # window=1 keeps only the immediate neighbours of the target.
    assert _window_block(script, 2, window=1) == (
        "   NARRATOR: c1\n★ Alice: target\n   NARRATOR: c2"
    )

    # window block skips empty text
    script = [
        {"speaker": "NARRATOR", "text": "   "},  # whitespace -> empty -> dropped
        {"speaker": "Alice", "text": "hi"},
    ]
    assert _window_block(script, 1, window=4) == "★ Alice: hi"


# --------------------------------------------------------------------------- #
# pick_ref_text
# --------------------------------------------------------------------------- #

def test_reference_text_selection():
    for args, expected in [
        ((['short', 'this is a long enough line'],), 'this is a long enough line'),
        ((['a', 'b c'],), 'a'),
        ((['', '  ', 'hello there friend'],), 'hello there friend'),
        (([],), ''),
    ]:
        assert pick_ref_text(*args) == expected, args


# --------------------------------------------------------------------------- #
# _fallback_persona
# --------------------------------------------------------------------------- #

def test_fallback_persona_shape():
    desc, ref, gender = _fallback_persona("Bob", ["a long enough line here"])
    assert desc == "Bob has a clear, natural audiobook voice."
    assert ref == "a long enough line here"
    assert gender == ""  # the fallback carries no gender (the badge stays 未定)


# --------------------------------------------------------------------------- #
# _sanitize (filename-safe)
# --------------------------------------------------------------------------- #

def test_filename_sanitization():
    for args, expected in [
        (('John Smith',), 'john_smith'),
        (('A.B/C',), 'a_b_c'),
        (('张三',), '张三'),
        (('',), 'unknown'),
        ((None,), 'unknown'),
    ]:
        assert _sanitize(*args) == expected, args


# --------------------------------------------------------------------------- #
# Phase-selection predicates  (_has_foundation / _clone_done)
# --------------------------------------------------------------------------- #

def test_has_foundation_requires_description():
    for args, expected in [
        (({'type': 'foundation', 'description': 'a voice'},), True),
        (({'type': 'foundation', 'description': '   '},), False),
        (({'type': 'clone', 'description': 'a voice', 'ref_audio': '/x.wav'},), True),
        (({'type': 'foundation'},), False),
        (({},), False),
        ((None,), False),
    ]:
        assert _has_foundation(*args) is expected, args


def test_clone_done_requires_clone_with_ref_audio():
    for args, expected in [
        (({'type': 'clone', 'ref_audio': '/x.wav'},), True),
        (({'type': 'clone'},), False),
        (({'type': 'clone', 'ref_audio': ''},), False),
        (({'type': 'design', 'description': 'x'},), False),
        (({'type': 'foundation', 'description': 'x'},), False),
        (({},), False),
        ((None,), False),
    ]:
        assert _clone_done(*args) is expected, args


# --------------------------------------------------------------------------- #
# Phase-status inference  (_foundation_status / _clone_status)
# --------------------------------------------------------------------------- #

def test_foundation_status_contract():
    # foundation status explicit field wins
    # An explicit Phase-1 field is authoritative, even over a stored description.
    assert _foundation_status({"foundation_status": "failed", "description": "x"}) == "failed"
    assert _foundation_status({"foundation_status": "done", "description": ""}) == "done"
    # Unknown values fall through to inference (empty description -> none).
    assert _foundation_status({"foundation_status": "weird", "description": "x"}) == "done"
    assert _foundation_status({"foundation_status": "weird"}) == "none"

    # foundation status inferred from description
    assert _foundation_status({"description": "a voice"}) == "done"
    assert _foundation_status({"type": "clone", "description": "a voice", "ref_audio": "/x"}) == "done"
    assert _foundation_status({"type": "foundation", "description": "   "}) == "none"
    assert _foundation_status({}) == "none"
    assert _foundation_status(None) is not None  # does not raise; empty dict is the caller's concern


def test_clone_status_contract():
    for args, expected in [
        (({'clone_status': 'failed', 'type': 'clone', 'ref_audio': '/x'},), 'failed'),
        (({'clone_status': 'done'},), 'done'),
        (({'type': 'clone', 'ref_audio': '/x.wav'},), 'done'),
        (({'type': 'clone'},), 'none'),
        (({'type': 'design', 'description': 'x'},), 'none'),
        (({},), 'none'),
    ]:
        assert _clone_status(*args) == expected, args


# --------------------------------------------------------------------------- #
# auto_candidate_count  (AUTO-mode budget: absolute log-scale bands)
# --------------------------------------------------------------------------- #

def test_auto_candidate_count_contract():
    # auto count cameo below 20 is one
    # Under 20 lines is a 龙套 (cameo): exactly one candidate, auto-used (no manual pick).
    assert auto_candidate_count(19) == 1
    assert auto_candidate_count(5) == 1
    assert auto_candidate_count(0) == 1

    # auto count log ladder anchors
    # Absolute log-scale bands (no project-relative ratio): 100–200 lines earn 3–4,
    # the 570 → 5700 decade spans 6–8, and a 20k-line 旁白 saturates at 8 without
    # demoting any lead.
    assert auto_candidate_count(20) == 2
    assert auto_candidate_count(100) == 3
    assert auto_candidate_count(200) == 4
    assert auto_candidate_count(500) == 5
    assert auto_candidate_count(570) == 6
    assert auto_candidate_count(1700) == 7
    assert auto_candidate_count(5700) == 8
    assert auto_candidate_count(20000) == 8

    # auto count floor two from 20 lines
    # 20+ lines always earns at least two candidates; the log steps land where the
    # ladder says (50 = last two, 51 the first three, 99 the last three).
    assert auto_candidate_count(20) == 2
    assert auto_candidate_count(50) == 2
    assert auto_candidate_count(51) == 3
    assert auto_candidate_count(99) == 3

    # auto count monotone and bounded
    prev = 0
    for lines in range(0, 20001):
        c = auto_candidate_count(lines)
        assert 1 <= c <= 8
        assert c >= prev  # monotone non-decreasing in lines
        prev = c


# --------------------------------------------------------------------------- #
# effective_candidates / _clone_have  (candidate bookkeeping shared by UI + select)
# --------------------------------------------------------------------------- #

def test_effective_candidates_contract():
    # effective candidates new format cleaned
    entry = {
        "type": "clone",
        "ref_audio": "04_voice_profiles/designed_voices/a_c1.wav",
        "candidates": [
            {"id": "1", "ref_audio": "04_voice_profiles/designed_voices/a_c1.wav", "seed": 11},
            {"id": " 2 ", "ref_audio": " 04_voice_profiles/designed_voices/a_c2.wav ", "seed": "12"},
            {"ref_audio": "no-id.wav"},      # missing id -> dropped
            {"id": "4"},                     # missing ref_audio -> dropped
            "junk",                          # non-dict -> dropped
            {"id": "5", "ref_audio": "   "},  # blank ref_audio -> dropped
        ],
    }
    out = effective_candidates(entry)
    assert [c["id"] for c in out] == ["1", "2"]
    assert out[1]["ref_audio"] == "04_voice_profiles/designed_voices/a_c2.wav"  # stripped
    assert [c["seed"] for c in out] == [11, 12]  # the string "12" is coerced to int

    # effective candidates legacy clone synthesised
    # A pre-candidates entry (no candidates key) holding a usable clone appears as a
    # single candidate, so the UI stays coherent (the pick button stays disabled).
    entry = {"type": "clone", "ref_audio": "04_voice_profiles/designed_voices/old.wav", "seed": 7}
    assert effective_candidates(entry) == [
        {"id": "1", "ref_audio": "04_voice_profiles/designed_voices/old.wav", "seed": 7},
    ]

    # effective candidates empty when no clone
    assert effective_candidates({"type": "foundation", "description": "x"}) == []
    assert effective_candidates({"type": "clone"}) == []  # a clone with no ref_audio
    assert effective_candidates({"type": "design", "candidates": [], "description": "x"}) == []
    assert effective_candidates(None) == []
    assert effective_candidates("junk") == []


def test_clone_have_counts():
    for args, expected in [
        (({'type': 'clone', 'ref_audio': '/x'},), 1),
        (({'type': 'clone', 'ref_audio': '/x', 'candidates': [{'id': '1', 'ref_audio': '/a'}, {'id': '2', 'ref_audio': '/b'}]},), 2),
        (({},), 0),
    ]:
        assert _clone_have(*args) == expected, args


# --------------------------------------------------------------------------- #
# _effective_line_counts  (display hints never combine role budgets)
# --------------------------------------------------------------------------- #

def test_effective_line_counts_contract():
    # Hints A -> B -> C must not change any role's own candidate budget.
    order = ["A", "B", "C"]
    samples = {"A": list(range(10)), "B": list(range(5)), "C": list(range(3))}
    vc = {"A": {"alias_of": "B"}, "B": {"alias_of": "C"}, "C": {}}
    assert _effective_line_counts(order, samples, vc) == {"A": 10, "B": 5, "C": 3}

    # effective line counts no aliases unchanged
    order = ["A", "B"]
    samples = {"A": list(range(7)), "B": list(range(3))}
    assert _effective_line_counts(order, samples, {"A": {}, "B": {}}) == {"A": 7, "B": 3}

    # effective line counts alias to out of scope contributes nothing
    # An alias whose canonical is not in the loaded scope must not inflate any budget.
    order = ["A"]
    samples = {"A": list(range(4))}
    out = _effective_line_counts(order, samples, {"A": {"alias_of": "Ghost"}})
    assert out == {"A": 4}
    assert max(out.values()) == 4  # no phantom canonical line count

    # effective line counts alias cycle safe
    # A -> B -> A: the hop guard terminates and nothing is folded (no inflation).
    order = ["A", "B"]
    samples = {"A": list(range(2)), "B": list(range(3))}
    vc = {"A": {"alias_of": "B"}, "B": {"alias_of": "A"}}
    assert _effective_line_counts(order, samples, vc) == {"A": 2, "B": 3}


# --------------------------------------------------------------------------- #
# make_clones end to end — the real engine path with a fake design worker
# --------------------------------------------------------------------------- #

FAKE_DESIGN_WORKER = '''
"""Design-batch stand-in for tts_worker.py (stdlib only, no torch).

Simulates the worker's design-batch mode: reads the jobs file (--segments-file), renders
each row's placeholder WAV (>= 1024 bytes, per-row content so files stay distinct), and
emits the stdout protocol lines the backend parses:

    [progress] <frac> <label>
    [design] <index> ok <seed> <path>
    [design] <index> error <reason>
    [watchdog] timeout batch=design#<n> indices=[...] elapsed=...s

Rows are grouped into sub-batches of FAKE_DESIGN_ROWS_PER_BATCH (default 1); the rows of
one sub-batch share the seed ``--seed + sub-batch#`` — exactly the real worker's
per-sub-batch seeding. FAKE_DESIGN_FAIL=all fails every row; =odd fails exactly the rows
whose seed is odd (a partial-failure injector — the process still exits 0, like a real
run with per-row failures). FAKE_DESIGN_WATCHDOG=once makes the worker render the first
sub-batch, then "hang" on the next one: it emits the [watchdog] line (naming that
sub-batch's indices) and exits 124 — only while --done-offset is 0 (the first attempt),
so a restart recovers. =always hangs on the first sub-batch before it renders, on every
attempt (a permanently poisoned run). FAKE_DESIGN_POISON=<sp>:<k> hangs on the poisoned
row before it renders — a deterministic single poison-row for the strike tests.
Each rendered row appends a capture record (jsonl) to the file named by
FAKE_DESIGN_CAPTURE.
"""
import json
import os
import sys


def main():
    args = sys.argv[1:]
    opts = {}
    i = 0
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            opts[args[i]] = args[i + 1]
            i += 2
        else:
            i += 1
    fail = os.environ.get("FAKE_DESIGN_FAIL", "")
    watchdog = os.environ.get("FAKE_DESIGN_WATCHDOG", "")
    poison = os.environ.get("FAKE_DESIGN_POISON", "")
    rows_per_batch = max(1, int(os.environ.get("FAKE_DESIGN_ROWS_PER_BATCH", "1")))
    seed_base = int(opts.get("--seed", "-1"))
    concurrency = int(opts.get("--concurrency", "1"))
    done_offset = int(opts.get("--done-offset", "0"))
    total = int(opts.get("--total", "0"))

    with open(opts["--segments-file"], encoding="utf-8") as f:
        jobs = json.load(f)
    if not total:
        total = len(jobs)
    base = done_offset / total if total else 0.0

    def progress(frac, label):
        print(f"[progress] {frac:.6f} {label}", flush=True)

    capture = os.environ.get("FAKE_DESIGN_CAPTURE", "")

    progress(base, "解析输入")
    progress(base, "加载 VoiceDesign 模型")

    done = 0
    for batch_start in range(0, len(jobs), rows_per_batch):
        chunk = jobs[batch_start:batch_start + rows_per_batch]
        sub_no = batch_start // rows_per_batch
        # The sub-batch sequence continues across restarts (starts at --done-offset), so
        # a re-rendered row never lands on a seed an already-settled row holds.
        row_seed = seed_base + done_offset + sub_no

        def _hang(indices):
            print(f"[watchdog] timeout batch=design#{sub_no} "
                  f"indices=[{', '.join(str(i) for i in indices)}] elapsed=999s", flush=True)
            return 124

        # Global watchdog: "always" hangs on the FIRST sub-batch before it renders
        # (every attempt dies the same way — a permanently poisoned run).
        if watchdog == "always" and batch_start == 0:
            return _hang([j["index"] for j in chunk])
        # Poison row in this sub-batch: the "hang" happens on it, before it renders.
        if poison:
            p_sp, _, p_k = poison.partition(":")
            if any(j.get("sp") == p_sp and str(j.get("k")) == p_k for j in chunk):
                return _hang([j["index"] for j in chunk])
        # Render the sub-batch (one shared seed for all its rows).
        for j in chunk:
            out = j["out"]
            ok = not (fail == "all" or (fail == "odd" and row_seed % 2 == 1))
            if ok:
                parent = os.path.dirname(out)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(out, "wb") as f:
                    f.write(b"RIFF" + str(j["index"]).encode() + b"\\x00" * 1024)
            if ok:
                print(f"[design] {j['index']} ok {row_seed} {out}", flush=True)
            else:
                print(f"[design] {j['index']} error 强制失败（fake worker）", flush=True)
            done += 1
            if capture:
                with open(capture, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"mode": "design-batch", "index": j["index"], "pid": os.getpid(),
                                        "out": out, "seed": row_seed,
                                        "seed_arg": seed_base, "concurrency": concurrency,
                                        "rows_per_batch": rows_per_batch},
                                       ensure_ascii=False) + chr(10))
            progress(base + done / len(jobs) * (1.0 - base), f"完成 {done_offset + done}/{total} 候选")
        # "once": after the first sub-batch, "hang" on the next one (if any) — only on the
        # first attempt (while --done-offset is 0), so a restart recovers.
        if watchdog == "once" and done_offset == 0 \\
                and batch_start + rows_per_batch < len(jobs):
            return _hang([j["index"] for j in jobs[batch_start + rows_per_batch:
                                                  batch_start + 2 * rows_per_batch]])

    progress(1.0, f"完成（共 {total} 候选）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


@pytest.fixture(autouse=True)
def _quiet_durable_task_guard(monkeypatch):
    monkeypatch.setattr(tts_api, "has_active_durable_tasks", lambda **kwargs: False)


@pytest.fixture
def clone_ws(monkeypatch, tmp_path):
    """A throwaway project root + workspace wired into core.config/paths so
    ``get_or_prepare_layout()`` / ``get_config()`` / script resolution all resolve — the e2e tests
    run the real make_clones / select / list_voices code against it (engine stubbed)."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    core_config.set_workspace_pointer(str(ws))
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    (ws / "04_voice_profiles").mkdir(parents=True, exist_ok=True)
    yield ws
    core_config.reset_config_cache()


def _seed_script(ws, counts):
    """One parsed script with the given per-character line counts (name -> lines)."""
    entries = []
    for sp, n in counts.items():
        for i in range(n):
            entries.append({"speaker": sp, "text": f"{sp} speaks line {i}."})
    (ws / "03_parsed_json" / "s.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _seed_foundations(ws, names, extra=None):
    """A voice_config.json where every named character holds a done foundation entry."""
    cfg = {
        sp: {"type": "foundation",
             "description": f"{sp} has a clear, natural voice.",
             "ref_text": f"{sp} speaks in a clear, natural voice.",
             "foundation_status": "done"}
        for sp in names
    }
    if extra:
        cfg.update(extra)
    (ws / "04_voice_profiles" / "voice_config.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _stub_design_engine(monkeypatch, ws, fail="", rows_per_batch=0, watchdog="", poison=""):
    """Point the engine at the fake design-batch worker; returns (worker, capture) paths.

    Knobs (env, consumed by the fake worker): ``fail`` = all|odd (per-row failure
    injector), ``rows_per_batch`` = sub-batch size (0 = default 1 — candidates share a
    seed per sub-batch), ``watchdog`` = once|always (exit-124 restart injector),
    ``poison`` = <sp>:<k> (the row that always "hangs" the worker).
    """
    worker = ws / "fake_design_worker.py"
    worker.write_text(FAKE_DESIGN_WORKER, encoding="utf-8")
    capture = ws / "design_calls.jsonl"
    monkeypatch.setattr(V, "resolve_engine", lambda: (sys.executable, str(worker)))
    monkeypatch.setenv("FAKE_DESIGN_CAPTURE", str(capture))
    if fail:
        monkeypatch.setenv("FAKE_DESIGN_FAIL", fail)
    else:
        monkeypatch.delenv("FAKE_DESIGN_FAIL", raising=False)
    if rows_per_batch:
        monkeypatch.setenv("FAKE_DESIGN_ROWS_PER_BATCH", str(rows_per_batch))
    else:
        monkeypatch.delenv("FAKE_DESIGN_ROWS_PER_BATCH", raising=False)
    if watchdog:
        monkeypatch.setenv("FAKE_DESIGN_WATCHDOG", watchdog)
    else:
        monkeypatch.delenv("FAKE_DESIGN_WATCHDOG", raising=False)
    if poison:
        monkeypatch.setenv("FAKE_DESIGN_POISON", poison)
    else:
        monkeypatch.delenv("FAKE_DESIGN_POISON", raising=False)
    return worker, capture


def _load_vc(ws):
    return json.loads((ws / "04_voice_profiles" / "voice_config.json").read_text("utf-8"))


def test_foundation_pause_does_not_hold_sibling_publication_lock(clone_ws, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    _seed_script(clone_ws, {"A": 3, "B": 3})
    _seed_foundations(clone_ws, ["A", "B"])
    parked, resume = Event(), Event()
    real_lock = V.exclusive_file_lock
    monkeypatch.setattr(V, "exclusive_file_lock", lambda path: real_lock(path, timeout=0.5))

    class PausedHandle(_Handle):
        def check(self):
            parked.set()
            assert resume.wait(5), "test did not resume the paused character"

    with ThreadPoolExecutor(max_workers=2) as pool:
        paused = pool.submit(V.prepare_foundations, PausedHandle(), speakers=["A"],
                             overrides={"A": "A refreshed voice"})
        try:
            assert parked.wait(3)
            sibling = pool.submit(V.prepare_foundations, _Handle(), speakers=["B"],
                                  overrides={"B": "B refreshed voice"})
            assert sibling.result(timeout=3)["results"][0]["ok"]
            assert _load_vc(clone_ws)["B"]["description"] == "B refreshed voice"
        finally:
            resume.set()
        assert paused.result(timeout=3)["results"][0]["ok"]
    assert _load_vc(clone_ws)["A"]["description"] == "A refreshed voice"
    assert _load_vc(clone_ws)["B"]["description"] == "B refreshed voice"
    assert not (clone_ws / ".tasks").exists()
    assert (clone_ws / "00_temp" / "tasks").is_dir()


def test_foundation_cancelled_before_publication_preserves_config(clone_ws):
    _seed_script(clone_ws, {"A": 3})
    _seed_foundations(clone_ws, ["A"])
    before = _load_vc(clone_ws)

    class CancelledHandle(_Handle):
        cancelled = True

    with pytest.raises(TaskCancelled):
        V.prepare_foundations(CancelledHandle(), speakers=["A"],
                             overrides={"A": "Replacement voice"})
    assert _load_vc(clone_ws) == before


def test_clone_eight_candidate_batch_uses_configured_ceiling(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"A": 3})
    _seed_foundations(clone_ws, ["A"])
    _, capture = _stub_design_engine(monkeypatch, clone_ws, rows_per_batch=8)
    result = V.generate_voice_candidates(_Handle(), speakers=["A"],
                                         concurrency=8, candidate_count=8)
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert result["ok"] == 1
    assert len(calls) == 8
    assert all(row["concurrency"] == row["rows_per_batch"] == 8 for row in calls)
    assert len(_load_vc(clone_ws)["A"]["candidates"]) == 8


class _Handle:
    """A minimal TaskHandle stand-in: records log/progress, never cancels or pauses."""

    def __init__(self):
        self.logs = []
        self.progresses = []

    def log(self, msg, level="INFO"):
        self.logs.append((level, msg))

    def progress(self, frac, current=""):
        self.progresses.append((frac, current))

    def check(self):
        pass


class _CancelHandle:
    """A TaskHandle stand-in that cancels at the first check() — mid-run teardown."""

    def __init__(self):
        self.logs = []
        self.progresses = []

    def log(self, msg, level="INFO"):
        self.logs.append((level, msg))

    def progress(self, frac, current=""):
        self.progresses.append((frac, current))

    def check(self):
        raise TaskCancelled("cancelled")


class _StagingHandle(_Handle):
    def __init__(self, workspace):
        super().__init__()
        self.stage_root = workspace / ".tasks" / "test-attempt"
        self.published = []

    def stage_workspace_file(self, final_path, data):
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(data)

    def allocate_workspace_stage(self, final_path):
        self.stage_root.mkdir(parents=True, exist_ok=True)
        return self.stage_root / final_path.name

    def publish_workspace_stage(self, final_path, staged_path):
        final_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.replace(final_path)
        self.published.append(final_path)

    def discard_workspace_stage(self, staged_path):
        staged_path.unlink(missing_ok=True)


def test_make_clones_fixed_two_candidates(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"A": 3, "B": 2})
    _seed_foundations(clone_ws, ["A", "B"])
    _stub_design_engine(monkeypatch, clone_ws)
    h = _Handle()
    res = V.generate_voice_candidates(h, concurrency=2, candidate_count=2)

    assert res["count"] == 2 and res["ok"] == 2 and res["failed"] == 0
    assert {r["speaker"]: r["candidates"] for r in res["results"]} == {"A": 2, "B": 2}
    vc = _load_vc(clone_ws)
    for sp in ("A", "B"):
        e = vc[sp]
        assert e["type"] == "clone"
        assert e["clone_status"] == "done"
        assert e["selected_audio_id"] is None  # a re-render voids any earlier pick
        assert [c["id"] for c in e["candidates"]] == ["1", "2"]
        # The top-level ref_audio mirrors the (first) active candidate, relatively.
        assert e["ref_audio"] == e["candidates"][0]["ref_audio"]
        assert e["ref_audio"].startswith("04_voice_profiles/designed_voices/")
        assert e["candidates"][0]["ref_audio"].endswith("_c1.wav")
        assert e["candidates"][1]["ref_audio"].endswith("_c2.wav")
        # The files really exist on disk, at the stored relative path.
        assert (clone_ws / e["candidates"][1]["ref_audio"]).exists()
        # Candidate k is seeded base+k: adjacent candidates differ by exactly one.
        assert e["candidates"][1]["seed"] - e["candidates"][0]["seed"] == 1


def test_make_clones_publishes_staged_candidates_to_final_paths(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"A": 3})
    _seed_foundations(clone_ws, ["A"])
    _stub_design_engine(monkeypatch, clone_ws)
    handle = _StagingHandle(clone_ws)

    result = V.generate_voice_candidates(handle, concurrency=2, candidate_count=2)

    voice = _load_vc(clone_ws)["A"]
    assert result["ok"] == 1
    assert len(handle.published) == 2
    assert all(path.is_file() for path in handle.published)
    assert all(".tasks" not in candidate["ref_audio"] for candidate in voice["candidates"])
    assert not list(handle.stage_root.glob("*.wav"))


def test_make_clones_auto_counts_follow_ladder(clone_ws, monkeypatch):
    # Auto mode (candidate_count=None) at real book scale: the 20k-line 旁白 (N) must
    # NOT demote the leads — the ladder is absolute, so every character budgets off its
    # OWN lines: 20000/5700 → 8, 1700 → 7, 500 → 5, 90 → 3, 5 → 1 (龙套).
    _seed_script(clone_ws, {"N": 20000, "A": 5700, "B": 1700, "C": 500, "D": 90, "E": 5})
    _seed_foundations(clone_ws, ["N", "A", "B", "C", "D", "E"])
    _stub_design_engine(monkeypatch, clone_ws)
    h = _Handle()
    res = V.generate_voice_candidates(h, concurrency=4)
    vc = _load_vc(clone_ws)
    assert {sp: len(vc[sp]["candidates"]) for sp in "NABCDE"} == {
        "N": 8, "A": 8, "B": 7, "C": 5, "D": 3, "E": 1,
    }
    assert res["ok"] == 6 and res["failed"] == 0
    assert any("备选计划" in msg for _lvl, msg in h.logs)  # the plan is visible in the log


def test_make_clones_partial_failure_keeps_character_usable(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"A": 5})
    _seed_foundations(clone_ws, ["A"])
    _stub_design_engine(monkeypatch, clone_ws, fail="odd")  # odd-seeded candidates fail
    h = _Handle()
    res = V.generate_voice_candidates(h, concurrency=2, candidate_count=4)
    assert res["ok"] == 1 and res["failed"] == 0  # the character still succeeds
    e = _load_vc(clone_ws)["A"]
    assert e["type"] == "clone" and e["clone_status"] == "done"
    # Exactly two of four survive (independent of the random seed base), re-numbered
    # 1..2 in k order; the active reference is the first survivor.
    assert [c["id"] for c in e["candidates"]] == ["1", "2"]
    assert {c["seed"] % 2 for c in e["candidates"]} == {0}
    assert e["ref_audio"] == e["candidates"][0]["ref_audio"]
    assert e["selected_audio_id"] is None


def test_make_clones_total_failure_falls_back_and_keeps_ref_audio(clone_ws, monkeypatch):
    (clone_ws / "04_voice_profiles" / "designed_voices").mkdir(parents=True, exist_ok=True)
    old = "04_voice_profiles/designed_voices/old_a.wav"
    (clone_ws / "04_voice_profiles" / "designed_voices" / "old_a.wav").write_bytes(b"0")
    _seed_script(clone_ws, {"A": 5})
    # A pre-existing (legacy) clone: the run re-renders it, and a total failure must
    # fall back to design WITHOUT destroying the last-known reference audio.
    _seed_foundations(clone_ws, ["A"], extra={
        "A": {"type": "clone", "description": "A has a clear, natural voice.",
              "ref_text": "A speaks in a clear, natural voice.",
              "ref_audio": old, "foundation_status": "done", "clone_status": "done"},
    })
    _stub_design_engine(monkeypatch, clone_ws, fail="all")
    h = _Handle()
    res = V.generate_voice_candidates(h, concurrency=1, candidate_count=2)
    assert res["ok"] == 0 and res["failed"] == 1
    e = _load_vc(clone_ws)["A"]
    assert e["type"] == "design"
    assert e["clone_status"] == "failed"
    assert e["candidates"] == []
    assert e["selected_audio_id"] is None
    assert e["ref_audio"] == old  # the old take stays audible in the preview column


def test_make_clones_cancel_leaves_untouched_entries(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"A": 3, "B": 2})
    _seed_foundations(clone_ws, ["A", "B"])
    before = _load_vc(clone_ws)
    _stub_design_engine(monkeypatch, clone_ws)
    with pytest.raises(TaskCancelled):
        V.generate_voice_candidates(_CancelHandle(), concurrency=2, candidate_count=2)
    # With two candidates per character, no character's full set can have settled by the
    # time the first check() fires — so the file is exactly as it was before the run.
    assert _load_vc(clone_ws) == before


def test_make_clones_new_only_skips_satisfied(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"A": 40, "B": 30})
    a_cands = [
        {"id": str(i), "ref_audio": f"04_voice_profiles/designed_voices/a_old_c{i}.wav",
         "seed": 10 + i}
        for i in (1, 2, 3, 4)
    ]
    b_cands = [
        {"id": str(i), "ref_audio": f"04_voice_profiles/designed_voices/b_old_c{i}.wav",
         "seed": 20 + i}
        for i in (1, 2)
    ]
    _seed_foundations(clone_ws, ["A", "B"], extra={
        # A already holds the full target (4) — and a user pick — so it is skipped.
        "A": {"type": "clone", "description": "A voice.", "ref_text": "A line.",
              "ref_audio": "04_voice_profiles/designed_voices/a_old_c2.wav",
              "candidates": a_cands, "selected_audio_id": "2",
              "foundation_status": "done", "clone_status": "done"},
        # B holds 2 < target 4 -> re-rendered.
        "B": {"type": "clone", "description": "B voice.", "ref_text": "B line.",
              "ref_audio": "04_voice_profiles/designed_voices/b_old_c1.wav",
              "candidates": b_cands, "selected_audio_id": None,
              "foundation_status": "done", "clone_status": "done"},
    })
    _worker, capture = _stub_design_engine(monkeypatch, clone_ws)
    h = _Handle()
    res = V.generate_voice_candidates(h, new_only=True, concurrency=2, candidate_count=4)

    assert res["count"] == 1 and res["ok"] == 1  # only B is re-rendered
    vc = _load_vc(clone_ws)
    # A is untouched — entry, candidate list and the user's pick all intact.
    assert vc["A"]["candidates"] == a_cands
    assert vc["A"]["selected_audio_id"] == "2"
    # B's set is replaced wholesale; its (absent) pick is voided to the default.
    assert [c["id"] for c in vc["B"]["candidates"]] == ["1", "2", "3", "4"]
    assert vc["B"]["selected_audio_id"] is None
    # Exactly B's four renders ran, and nothing else.
    calls = [json.loads(line) for line in capture.read_text("utf-8").splitlines() if line.strip()]
    assert len(calls) == 4
    assert all(Path(c["out"]).name.startswith("b_") for c in calls)


def test_make_clones_new_only_upgrades_legacy_single_clone(clone_ws, monkeypatch):
    # A pre-candidates character holds one (legacy) clone: have=1 < target -> re-rendered.
    _seed_script(clone_ws, {"A": 5})
    _seed_foundations(clone_ws, ["A"], extra={
        "A": {"type": "clone", "description": "A has a clear, natural voice.",
              "ref_text": "A speaks in a clear, natural voice.",
              "ref_audio": "04_voice_profiles/designed_voices/a_legacy.wav",
              "foundation_status": "done", "clone_status": "done"},
    })
    _stub_design_engine(monkeypatch, clone_ws)
    h = _Handle()
    res = V.generate_voice_candidates(h, new_only=True, concurrency=1, candidate_count=2)
    assert res["count"] == 1 and res["ok"] == 1
    e = _load_vc(clone_ws)["A"]
    assert [c["id"] for c in e["candidates"]] == ["1", "2"]
    assert e["ref_audio"] == e["candidates"][0]["ref_audio"]
    assert e["selected_audio_id"] is None


def test_make_clones_progress_worker_driven_monotone(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"A": 2, "B": 1})
    _seed_foundations(clone_ws, ["A", "B"])
    _stub_design_engine(monkeypatch, clone_ws)
    h = _Handle()
    V.generate_voice_candidates(h, concurrency=1, candidate_count=2)  # single attempt: deterministic
    fracs = [f for f, _l in h.progresses]
    labels = [l for _f, l in h.progresses]
    # The worker drives the progress bar: its [progress] lines are forwarded verbatim
    # (there is no separate coordinator fraction and no child suppression to check).
    assert fracs == sorted(fracs)  # monotonically non-decreasing
    assert fracs[0] == 0.0
    assert fracs[-1] == 1.0
    assert labels[0] == "解析输入"
    assert "加载 VoiceDesign 模型" in labels
    assert "完成 1/4 候选" in labels
    assert "完成 4/4 候选" in labels
    assert labels[-1] == "完成"


# --------------------------------------------------------------------------- #
# make_clones  (watchdog restart loop, isolation, breakpoint adoption, seed layout)
# --------------------------------------------------------------------------- #

def test_make_clones_watchdog_shrink_restart(clone_ws, monkeypatch):
    # A hung child (exit 124) shrinks the per-batch cap, restarts a fresh subprocess, and
    # re-runs only the unsettled rows — progress never dips back across the restart.
    _seed_script(clone_ws, {"A": 3, "B": 2})
    _seed_foundations(clone_ws, ["A", "B"])
    _stub_design_engine(monkeypatch, clone_ws, watchdog="once")  # first attempt dies 124
    h = _Handle()
    res = V.generate_voice_candidates(h, concurrency=2, candidate_count=2)
    assert res["ok"] == 2 and res["failed"] == 0
    fracs = [f for f, _l in h.progresses]
    assert fracs == sorted(fracs)  # no backward jump across the restart
    assert fracs[-1] == 1.0
    assert any("看门狗触发" in m and "批内上限缩到 1 行" in m for _l, m in h.logs)
    vc = _load_vc(clone_ws)
    # The sub-batch seed sequence continues across the restart (offset = settled rows),
    # so no candidate lands on a seed an already-settled candidate holds.
    assert vc["A"]["candidates"][1]["seed"] - vc["A"]["candidates"][0]["seed"] == 1
    assert vc["B"]["candidates"][1]["seed"] - vc["B"]["candidates"][0]["seed"] == 1
    assert vc["A"]["candidates"][0]["seed"] < vc["B"]["candidates"][0]["seed"]
    calls = [json.loads(l) for l in
             (clone_ws / "design_calls.jsonl").read_text("utf-8").splitlines() if l.strip()]
    # A's first candidate rendered in attempt 1 (cap 2); the rest in attempt 2 (cap 1).
    a1 = [c for c in calls if Path(c["out"]).name.startswith("a_") and c["out"].endswith("_c1.wav")]
    assert len(a1) == 1 and a1[0]["concurrency"] == 2  # not re-rendered on the restart
    assert all(c["concurrency"] == 1 for c in calls if c is not a1[0])


def test_make_clones_poison_isolated_at_cap_one(clone_ws, monkeypatch):
    # At cap 1 a poison candidate (two consecutive timeouts) is isolated as a recorded
    # failure; its character settles with the surviving candidates and the rest completes.
    _seed_script(clone_ws, {"A": 5, "B": 5})
    _seed_foundations(clone_ws, ["A", "B"])
    _stub_design_engine(monkeypatch, clone_ws, poison="A:1")
    h = _Handle()
    res = V.generate_voice_candidates(h, concurrency=1, candidate_count=2)
    assert res["ok"] == 2 and res["failed"] == 0  # isolation is not a task failure
    vc = _load_vc(clone_ws)
    a = vc["A"]
    assert a["type"] == "clone" and a["clone_status"] == "done"
    # A's surviving candidate (c2) re-numbered to id 1; the poison c1 was never rendered.
    assert [c["id"] for c in a["candidates"]] == ["1"]
    assert a["ref_audio"] == a["candidates"][0]["ref_audio"]
    assert a["ref_audio"].endswith("_c2.wav")
    assert a["selected_audio_id"] is None
    # The poison candidate was never rendered (the "hang" killed the worker first).
    assert list((clone_ws / "04_voice_profiles" / "designed_voices").glob("a_*_c1.wav")) == []
    assert len(vc["B"]["candidates"]) == 2
    assert any("首次记罚" in m for _l, m in h.logs)
    assert any("连续两次超时" in m and "隔离为失败" in m for _l, m in h.logs)


def test_make_clones_watchdog_attempt_cap_raises(clone_ws, monkeypatch):
    # A child that hangs on every attempt: strikes isolate candidates two-attempts apart,
    # but 8 restarts run out first — the run fails loud, with settled progress preserved.
    names = ["A", "B", "C", "D", "E"]
    _seed_script(clone_ws, {s: 5 for s in names})
    _seed_foundations(clone_ws, names)
    _stub_design_engine(monkeypatch, clone_ws, watchdog="always")
    h = _Handle()
    with pytest.raises(RuntimeError, match="反复超时"):
        V.generate_voice_candidates(h, concurrency=1, candidate_count=2)
    vc = _load_vc(clone_ws)
    # The first two characters' candidates were all isolated -> settled as design fallback.
    assert vc["A"]["type"] == "design" and vc["A"]["clone_status"] == "failed"
    assert vc["B"]["type"] == "design" and vc["B"]["clone_status"] == "failed"
    # Characters never reached keep their pre-run (foundation) entries, untouched.
    assert vc["C"]["type"] == "foundation"


def test_make_clones_resume_adoption_zero_jobs(clone_ws, monkeypatch):
    # Every candidate already on disk (a complete prior attempt) -> all adopted with
    # seed -1, settled, and the engine is never spawned.
    _seed_script(clone_ws, {"A": 5})
    _seed_foundations(clone_ws, ["A"])
    # Pin the run namespace / seed base so the candidate file names are known up front.
    monkeypatch.setattr(V.time, "time_ns", lambda: 111)
    monkeypatch.setattr(V.secrets, "randbelow", lambda n: 7)
    dv = clone_ws / "04_voice_profiles" / "designed_voices"
    dv.mkdir(parents=True, exist_ok=True)
    for k in (1, 2):
        (dv / f"a_111_c{k}.wav").write_bytes(b"0" * 2048)
    _worker, capture = _stub_design_engine(monkeypatch, clone_ws)
    h = _Handle()
    res = V.generate_voice_candidates(h, concurrency=2, candidate_count=2)
    assert res["ok"] == 1 and res["failed"] == 0
    assert not capture.exists()  # the engine never ran (zero-jobs short-circuit)
    e = _load_vc(clone_ws)["A"]
    assert [c["seed"] for c in e["candidates"]] == [-1, -1]  # adopted: seed unrecoverable
    assert e["ref_audio"] == e["candidates"][0]["ref_audio"]
    assert any("断点采纳" in m for _l, m in h.logs)
    assert h.progresses and h.progresses[-1][0] == 1.0


def test_make_clones_batched_layout_shares_seed(clone_ws, monkeypatch):
    # With several candidates per sub-batch the rows share the sub-batch's seed (the
    # accepted Level-2 semantics) — yet each candidate renders its own distinct file.
    _seed_script(clone_ws, {"A": 5})
    _seed_foundations(clone_ws, ["A"])
    _stub_design_engine(monkeypatch, clone_ws, rows_per_batch=2)
    h = _Handle()
    res = V.generate_voice_candidates(h, concurrency=4, candidate_count=2)
    assert res["ok"] == 1 and res["failed"] == 0
    e = _load_vc(clone_ws)["A"]
    assert e["candidates"][0]["seed"] == e["candidates"][1]["seed"] >= 0
    f1 = (clone_ws / e["candidates"][0]["ref_audio"]).read_bytes()
    f2 = (clone_ws / e["candidates"][1]["ref_audio"]).read_bytes()
    assert f1 != f2  # same seed, still distinct renders (per-row sampling)


def test_make_clones_disabled_checks_in_cmd(clone_ws, monkeypatch):
    # The design-batch cmd carries --disabled-checks only when a planner check is closed
    # in the config; the value lists the closed check(s) in canonical order. run_worker is
    # stubbed to record the cmd and fail every row, so the run still settles.
    _seed_script(clone_ws, {"A": 3})
    _seed_foundations(clone_ws, ["A"])
    _stub_design_engine(monkeypatch, clone_ws)
    cmds = []

    def _record(cmd, handle, on_line, **_kw):
        cmds.append(list(cmd))
        rows = json.loads(Path(cmd[cmd.index("--segments-file") + 1]).read_text("utf-8"))
        for i in range(len(rows)):
            on_line(f"[design] {i} error 测试桩（未渲染）")

    monkeypatch.setattr(V, "run_tts_subprocess", _record)
    h = _Handle()
    res = V.generate_voice_candidates(h, concurrency=1, candidate_count=2)
    assert res["ok"] == 0 and res["failed"] == 1  # the stubbed run settles as failed
    assert "--disabled-checks" not in cmds[0]  # all checks on (default) -> flag omitted

    core_config.update_config({"tts": {"planner_vram": False}})
    res = V.generate_voice_candidates(h, concurrency=1, candidate_count=2)
    assert res["ok"] == 0 and res["failed"] == 1
    assert "--disabled-checks" not in cmds[1]


def test_make_clones_disabled_checks_survive_fake_worker(clone_ws, monkeypatch):
    # One check closed in the config: the REAL run_worker spawns the fake design worker
    # with paired flags — inspect the spawn arguments directly because logs
    # intentionally omit full command lines. (Worker cmds must stay
    # strictly `--flag value` pairs: the fake worker's generic pair parser would swallow
    # the next token if a flag ever arrived value-less.)
    _seed_script(clone_ws, {"A": 3, "B": 2})
    _seed_foundations(clone_ws, ["A", "B"])
    core_config.update_config({"tts": {"planner_vram": False}})
    _stub_design_engine(monkeypatch, clone_ws)
    from backend.engines import tts
    commands = []
    spawn = tts.GPUServiceManager.spawn_tts
    def capture(cmd, *args, **kwargs):
        commands.append(cmd)
        return spawn(cmd, *args, **kwargs)
    monkeypatch.setattr(tts.GPUServiceManager, "spawn_tts", capture)
    h = _Handle()
    res = V.generate_voice_candidates(h, concurrency=2, candidate_count=2)
    assert res["ok"] == 2 and res["failed"] == 0
    logs = sorted((clone_ws / "logs").glob("tts_clone_*.log"))
    assert logs  # the run mirrored its transcript to disk
    transcript = "\n".join(p.read_text("utf-8") for p in logs)
    assert "--disabled-checks" not in transcript
    assert commands
    assert all(cmd[cmd.index("--vocoder-batch-size") + 1] == "8" for cmd in commands)
    assert "cmd:" not in transcript


# --------------------------------------------------------------------------- #
# the select endpoint  (PUT /api/tts/voices/select, called in-process)
# --------------------------------------------------------------------------- #

def _seed_select_ws(ws):
    """A workspace whose character A holds two clone candidates (files on disk)."""
    dv = ws / "04_voice_profiles" / "designed_voices"
    dv.mkdir(parents=True, exist_ok=True)
    for name in ("a_c1.wav", "a_c2.wav"):
        (dv / name).write_bytes(b"0")
    _seed_script(ws, {"A": 2})
    _seed_foundations(ws, ["A"], extra={
        "A": {"type": "clone", "description": "A has a clear, natural voice.",
              "ref_text": "A speaks in a clear, natural voice.",
              "ref_audio": "04_voice_profiles/designed_voices/a_c1.wav",
              "candidates": [
                  {"id": "1", "ref_audio": "04_voice_profiles/designed_voices/a_c1.wav", "seed": 1},
                  {"id": "2", "ref_audio": "04_voice_profiles/designed_voices/a_c2.wav", "seed": 2},
              ],
              "selected_audio_id": None, "foundation_status": "done", "clone_status": "done"},
    })


def test_select_voice_updates_pick_and_active_ref(clone_ws):
    _seed_select_ws(clone_ws)
    out = select_voice(SelectVoiceRequest(speaker="A", audio_id="2"))
    assert out["ok"] is True
    assert out["selected_audio_id"] == "2"
    assert out["ref_audio"] == "04_voice_profiles/designed_voices/a_c2.wav"
    e = _load_vc(clone_ws)["A"]
    assert e["selected_audio_id"] == "2"
    # The top-level ref_audio follows the pick, so downstream synthesis uses take #2.
    assert e["ref_audio"] == "04_voice_profiles/designed_voices/a_c2.wav"


def test_select_voice_none_resets_to_first(clone_ws):
    _seed_select_ws(clone_ws)
    out = select_voice(SelectVoiceRequest(speaker="A", audio_id=None))
    assert out["selected_audio_id"] is None
    assert out["ref_audio"] == "04_voice_profiles/designed_voices/a_c1.wav"
    e = _load_vc(clone_ws)["A"]
    assert e["selected_audio_id"] is None
    assert e["ref_audio"] == "04_voice_profiles/designed_voices/a_c1.wav"


def test_select_voice_rejects_bad_id(clone_ws):
    _seed_select_ws(clone_ws)
    with pytest.raises(HTTPException) as ex:
        select_voice(SelectVoiceRequest(speaker="A", audio_id="9"))
    assert ex.value.status_code == 400


def test_select_voice_unknown_speaker(clone_ws):
    _seed_select_ws(clone_ws)
    with pytest.raises(HTTPException) as ex:
        select_voice(SelectVoiceRequest(speaker="Z"))
    assert ex.value.status_code == 404


def test_select_voice_no_candidates(clone_ws):
    # A plain foundation entry (no clone) has nothing to pick from.
    _seed_script(clone_ws, {"A": 2})
    _seed_foundations(clone_ws, ["A"])
    with pytest.raises(HTTPException) as ex:
        select_voice(SelectVoiceRequest(speaker="A", audio_id="1"))
    assert ex.value.status_code == 400


def test_select_voice_requires_workspace(tmp_path, monkeypatch):
    # No workspace pointer: the guard fires before anything is read or written.
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    try:
        with pytest.raises(HTTPException) as ex:
            select_voice(SelectVoiceRequest(speaker="A"))
        assert ex.value.status_code == 409
    finally:
        core_config.reset_config_cache()


def test_phase_task_guard_reads_durable_voice_tasks(monkeypatch):
    from sqlalchemy.orm import Session

    queried = []
    monkeypatch.setattr(
        tts_api, "has_active_durable_tasks",
        lambda **kwargs: queried.append(kwargs["task_type"]) or kwargs["task_type"] == "voices.clone",
    )
    ctx = AuthContext(user=SimpleNamespace(id="u"), session=SimpleNamespace(active_project_id="w"))
    assert tts_api._phase_task_active(ctx, Session()) is True
    assert queried == ["voices.foundation", "voices.clone"]


def test_select_voice_refused_while_phase_task_active(clone_ws, monkeypatch):
    _seed_select_ws(clone_ws)

    monkeypatch.setattr(tts_api, "_phase_task_active", lambda *args: True)
    with pytest.raises(HTTPException) as ex:
        select_voice(SelectVoiceRequest(speaker="A", audio_id="2"))
    assert ex.value.status_code == 409
    # The refused write left the file untouched.
    assert _load_vc(clone_ws)["A"]["selected_audio_id"] is None

    # A finished voices task (or an unrelated running one) does not block the pick.
    monkeypatch.setattr(tts_api, "_phase_task_active", lambda *args: False)
    out = select_voice(SelectVoiceRequest(speaker="A", audio_id="2"))
    assert out["ok"] is True and out["selected_audio_id"] == "2"


# --------------------------------------------------------------------------- #
# the merge-speakers endpoint  (POST /api/tts/voices/merge-speakers, called in-process)
# --------------------------------------------------------------------------- #

def _read_script(ws, name="s.json"):
    return json.loads((ws / "03_parsed_json" / name).read_text("utf-8"))


def test_merge_speakers_single_file(clone_ws):
    _seed_script(clone_ws, {"A": 3, "B": 2})
    _seed_foundations(clone_ws, ["A", "B", "C"])
    out = merge_speakers(MergeSpeakersRequest(source="A", target="B", script="s.json"))
    assert out == {"ok": True, "source": "A", "target": "B", "replaced": 3, "files": ["s.json"]}
    data = _read_script(clone_ws)
    assert {e["speaker"] for e in data} == {"B"} and len(data) == 5
    vc = _load_vc(clone_ws)
    assert "A" not in vc
    # The untouched characters keep their entries byte-for-byte.
    assert vc["B"]["foundation_status"] == "done"
    assert vc["C"]["description"].startswith("C ")


def test_merge_speakers_all_scope(clone_ws):
    _seed_script(clone_ws, {"A": 2, "B": 1})
    (clone_ws / "03_parsed_json" / "s2.json").write_text(
        json.dumps([{"speaker": "A", "text": "x"}, {"speaker": "C", "text": "y"}],
                   ensure_ascii=False), encoding="utf-8")
    _seed_foundations(clone_ws, ["A", "B", "C"])
    out = merge_speakers(MergeSpeakersRequest(source="A", target="B",
                                              script=core_paths.ALL_PARSED_JSON))
    assert out["replaced"] == 3 and out["files"] == ["s.json", "s2.json"]
    assert {e["speaker"] for e in _read_script(clone_ws)} == {"B"}
    s2 = _read_script(clone_ws, "s2.json")
    assert [e["speaker"] for e in s2] == ["B", "C"]  # C's line is untouched


def test_merge_speakers_partial_files(clone_ws):
    # A file without the source is NOT rewritten (a rewrite would refresh its mtime and
    # perturb the most-recent-file / __all__ ordering).
    _seed_script(clone_ws, {"A": 1, "B": 1})
    s3 = clone_ws / "03_parsed_json" / "s3.json"
    s3.write_text(json.dumps([{"speaker": "C", "text": "x"}], ensure_ascii=False),
                  encoding="utf-8")
    before = s3.read_bytes()
    out = merge_speakers(MergeSpeakersRequest(source="A", target="B",
                                              script=core_paths.ALL_PARSED_JSON))
    assert out["files"] == ["s.json"]
    assert s3.read_bytes() == before


def test_merge_speakers_zero_lines_removes_vc_entry(clone_ws):
    # The source has no lines in scope but a voice-config entry: the merge still cleans it.
    _seed_script(clone_ws, {"B": 2})
    _seed_foundations(clone_ws, ["A", "B"])
    out = merge_speakers(MergeSpeakersRequest(source="A", target="B", script="s.json"))
    assert out == {"ok": True, "source": "A", "target": "B", "replaced": 0, "files": []}
    assert "A" not in _load_vc(clone_ws)


def test_merge_speakers_redirects_aliases(clone_ws):
    _seed_script(clone_ws, {"A": 1, "B": 1})
    _seed_foundations(clone_ws, ["A", "B", "C", "D"], extra={
        "C": {"alias_of": "A"},
        "D": {"alias": "A"},  # legacy field (still recognised downstream)
    })
    out = merge_speakers(MergeSpeakersRequest(source="A", target="B", script="s.json"))
    assert out["replaced"] == 1
    vc = _load_vc(clone_ws)
    assert "A" not in vc
    assert vc["C"]["alias_of"] == "B"
    assert vc["D"]["alias"] == "B"


def test_merge_speakers_type_fallback_only(clone_ws):
    # ``type`` stands in for the identity ONLY when ``speaker`` is absent — a line whose
    # speaker is someone else must not have its ``type`` field clobbered.
    (clone_ws / "03_parsed_json" / "s.json").write_text(
        json.dumps([
            {"type": "A", "text": "narration"},                 # type-only identity -> merged
            {"speaker": "B", "type": "A", "text": "dialogue"},  # identity is B -> untouched
        ], ensure_ascii=False), encoding="utf-8")
    _seed_foundations(clone_ws, ["A", "B"])
    out = merge_speakers(MergeSpeakersRequest(source="A", target="B", script="s.json"))
    assert out["replaced"] == 1
    data = _read_script(clone_ws)
    assert data[0]["type"] == "B" and "speaker" not in data[0]
    assert data[1]["speaker"] == "B" and data[1]["type"] == "A"


def test_merge_speakers_no_voice_config(clone_ws):
    _seed_script(clone_ws, {"A": 1, "B": 1})
    out = merge_speakers(MergeSpeakersRequest(source="A", target="B", script="s.json"))
    assert out["replaced"] == 1
    # Absent voice_config is legal: the merge must not create one.
    assert not (clone_ws / "04_voice_profiles" / "voice_config.json").exists()


def test_merge_speakers_wav_kept_on_disk(clone_ws):
    dv = clone_ws / "04_voice_profiles" / "designed_voices"
    dv.mkdir(parents=True, exist_ok=True)
    wav = dv / "a_c1.wav"
    wav.write_bytes(b"RIFF-fake")
    _seed_script(clone_ws, {"A": 1, "B": 1})
    _seed_foundations(clone_ws, ["A", "B"], extra={
        "A": {"ref_audio": "04_voice_profiles/designed_voices/a_c1.wav"},
    })
    out = merge_speakers(MergeSpeakersRequest(source="A", target="B", script="s.json"))
    assert out["replaced"] == 1
    assert "A" not in _load_vc(clone_ws)
    assert wav.exists() and wav.read_bytes() == b"RIFF-fake"  # user data is never deleted


def test_merge_speakers_source_equals_target(clone_ws):
    _seed_script(clone_ws, {"A": 1, "B": 1})
    with pytest.raises(HTTPException) as ex:
        merge_speakers(MergeSpeakersRequest(source="A", target="A", script="s.json"))
    assert ex.value.status_code == 400


def test_merge_speakers_empty_names(clone_ws):
    _seed_script(clone_ws, {"A": 1})
    with pytest.raises(HTTPException) as ex:
        merge_speakers(MergeSpeakersRequest(source="  ", target="B", script="s.json"))
    assert ex.value.status_code == 400


def test_merge_speakers_unknown_source(clone_ws):
    _seed_script(clone_ws, {"A": 1, "B": 1})
    _seed_foundations(clone_ws, ["A", "B"])
    before = (clone_ws / "03_parsed_json" / "s.json").read_bytes()
    with pytest.raises(HTTPException) as ex:
        merge_speakers(MergeSpeakersRequest(source="Z", target="B", script="s.json"))
    assert ex.value.status_code == 404
    assert (clone_ws / "03_parsed_json" / "s.json").read_bytes() == before
    assert set(_load_vc(clone_ws)) == {"A", "B"}  # nothing was written


def test_merge_speakers_unknown_target(clone_ws):
    _seed_script(clone_ws, {"A": 1, "B": 1})
    before = (clone_ws / "03_parsed_json" / "s.json").read_bytes()
    with pytest.raises(HTTPException) as ex:
        merge_speakers(MergeSpeakersRequest(source="A", target="Z", script="s.json"))
    assert ex.value.status_code == 400
    assert (clone_ws / "03_parsed_json" / "s.json").read_bytes() == before


def test_merge_speakers_requires_workspace(tmp_path, monkeypatch):
    # No workspace pointer: the guard fires before anything is read or written.
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    try:
        with pytest.raises(HTTPException) as ex:
            merge_speakers(MergeSpeakersRequest(source="A", target="B"))
        assert ex.value.status_code == 409
    finally:
        core_config.reset_config_cache()


def test_merge_speakers_refused_while_phase_task_active(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"A": 1, "B": 1})
    _seed_foundations(clone_ws, ["A", "B"])

    monkeypatch.setattr(tts_api, "_phase_task_active", lambda *args: True)
    with pytest.raises(HTTPException) as ex:
        merge_speakers(MergeSpeakersRequest(source="A", target="B", script="s.json"))
    assert ex.value.status_code == 409
    assert set(_load_vc(clone_ws)) == {"A", "B"}
    assert {e["speaker"] for e in _read_script(clone_ws)} == {"A", "B"}

    # A finished voices task (or an unrelated running one) does not block the merge.
    monkeypatch.setattr(tts_api, "_phase_task_active", lambda *args: False)
    out = merge_speakers(MergeSpeakersRequest(source="A", target="B", script="s.json"))
    assert out["ok"] is True and out["replaced"] == 1


def test_merge_speakers_corrupt_script(clone_ws):
    (clone_ws / "03_parsed_json" / "s.json").write_text("not json at all", encoding="utf-8")
    with pytest.raises(HTTPException) as ex:
        merge_speakers(MergeSpeakersRequest(source="A", target="B", script="s.json"))
    assert ex.value.status_code == 400


def test_merge_speakers_corrupt_voice_config(clone_ws):
    _seed_script(clone_ws, {"A": 1, "B": 1})
    (clone_ws / "04_voice_profiles" / "voice_config.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(HTTPException) as ex:
        merge_speakers(MergeSpeakersRequest(source="A", target="B", script="s.json"))
    assert ex.value.status_code == 400


# --------------------------------------------------------------------------- #
# gender badge  (Phase-1 pre-fill + user pick; POST /api/tts/voices/gender)
# --------------------------------------------------------------------------- #

def test_set_gender_creates_updates_clears(clone_ws):
    _seed_script(clone_ws, {"A": 1, "B": 1})
    # A script-only character (no voice_config entry yet) gets a minimal one.
    out = tts_api.set_gender(tts_api.SetGenderRequest(speaker="A", gender="male"))
    assert out == {"ok": True, "speaker": "A", "gender": "male"}
    assert _load_vc(clone_ws)["A"]["gender"] == "male"
    # Update, then clear back to unknown.
    tts_api.set_gender(tts_api.SetGenderRequest(speaker="A", gender="female"))
    assert _load_vc(clone_ws)["A"]["gender"] == "female"
    out = tts_api.set_gender(tts_api.SetGenderRequest(speaker="A", gender=""))
    assert out["gender"] == ""
    assert "gender" not in _load_vc(clone_ws)["A"]
    # Other characters are untouched.
    assert "A" == next(iter(_load_vc(clone_ws))) and "B" not in _load_vc(clone_ws)


def test_set_gender_preserves_other_entry_fields(clone_ws):
    _seed_script(clone_ws, {"A": 1})
    _seed_foundations(clone_ws, ["A"])
    tts_api.set_gender(tts_api.SetGenderRequest(speaker="A", gender="male"))
    e = _load_vc(clone_ws)["A"]
    assert e["gender"] == "male" and e["foundation_status"] == "done"
    assert e["description"].startswith("A ")


def test_set_gender_rejects_bad_values(clone_ws):
    _seed_script(clone_ws, {"A": 1})
    _seed_foundations(clone_ws, ["A"])
    before = (clone_ws / "04_voice_profiles" / "voice_config.json").read_bytes()
    for req in (tts_api.SetGenderRequest(speaker="  ", gender="male"),
                tts_api.SetGenderRequest(speaker="A", gender="unknown"),
                tts_api.SetGenderRequest(speaker="A", gender="andro")):
        with pytest.raises(HTTPException) as ex:
            tts_api.set_gender(req)
        assert ex.value.status_code == 400
    assert (clone_ws / "04_voice_profiles" / "voice_config.json").read_bytes() == before


def test_set_gender_requires_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    try:
        with pytest.raises(HTTPException) as ex:
            tts_api.set_gender(tts_api.SetGenderRequest(speaker="A", gender="male"))
        assert ex.value.status_code == 409
    finally:
        core_config.reset_config_cache()


def test_set_gender_refused_while_phase_task_active(clone_ws, monkeypatch):
    _seed_script(clone_ws, {"A": 1})
    _seed_foundations(clone_ws, ["A"])

    monkeypatch.setattr(tts_api, "_phase_task_active", lambda *args: True)
    with pytest.raises(HTTPException) as ex:
        tts_api.set_gender(tts_api.SetGenderRequest(speaker="A", gender="male"))
    assert ex.value.status_code == 409
    assert "gender" not in _load_vc(clone_ws)["A"]


def test_list_voices_includes_gender(clone_ws):
    _seed_script(clone_ws, {"A": 2, "B": 1})
    _seed_foundations(clone_ws, ["A", "B"], extra={"A": {"gender": "female"}})
    out = list_voices("s.json")
    by_name = {s["name"]: s for s in out["speakers"]}
    assert by_name["A"]["gender"] == "female"
    assert by_name["B"]["gender"] == ""


def _stub_persona_llm(monkeypatch, reply):
    """Point the persona LLM channel at a canned reply (``_llm_persona`` imports the
    function at call time, so patching the module attribute is enough)."""
    from backend.engines import llm_transport

    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        return reply, "stop", {}

    monkeypatch.setattr(llm_transport, "request_chat_completion", fake)
    return calls


def test_llm_persona_gender_explicit_key_wins(clone_ws, monkeypatch):
    reply = json.dumps({"description": "青年男性，音色清亮。", "ref_text": "你好，我是A。",
                        "gender": "female"}, ensure_ascii=False)
    _stub_persona_llm(monkeypatch, reply)
    llm = SimpleNamespace(model_name="m", base_url="b", api_key="k")
    script = [{"speaker": "A", "text": "line"} for _ in range(3)]
    desc, ref, gender = V._llm_persona(_Handle(), llm, "sys", "user {speaker}", "A", script,
                                       ([0], [1], [2]))
    # The explicit gender key beats the description's own words (here on purpose).
    assert (desc, ref, gender) == ("青年男性，音色清亮。", "你好，我是A。", "female")


def test_llm_persona_gender_from_description(clone_ws, monkeypatch):
    # Prompts without the gender key still state it in the description ("少女女声").
    reply = json.dumps({"description": "少女女声，音色软糯。", "ref_text": "你好，我是B。"},
                       ensure_ascii=False)
    _stub_persona_llm(monkeypatch, reply)
    llm = SimpleNamespace(model_name="m", base_url="b", api_key="k")
    script = [{"speaker": "B", "text": "line"} for _ in range(3)]
    _desc, _ref, gender = V._llm_persona(_Handle(), llm, "sys", "user {speaker}", "B", script,
                                         ([0], [1], [2]))
    assert gender == "female"


def test_llm_persona_gender_unknown(clone_ws, monkeypatch):
    reply = json.dumps({"description": "A mature, clear voice.", "ref_text": "Hello."},
                       ensure_ascii=False)
    _stub_persona_llm(monkeypatch, reply)
    llm = SimpleNamespace(model_name="m", base_url="b", api_key="k")
    script = [{"speaker": "C", "text": "line"} for _ in range(3)]
    _desc, _ref, gender = V._llm_persona(_Handle(), llm, "sys", "user {speaker}", "C", script,
                                         ([0], [1], [2]))
    assert gender == ""


def test_prepare_foundations_gender_prefill_respects_existing(clone_ws):
    # A's stored pick (badge) survives a foundation (re)generation even when the new
    # description says the opposite; B (no gender yet) gets the description-derived pre-fill.
    _seed_script(clone_ws, {"A": 3, "B": 3})
    _seed_foundations(clone_ws, ["A", "B"], extra={"A": {"gender": "female"}})
    h = _Handle()
    V.prepare_foundations(h, overrides={
        "A": "青年男性，音色清亮，语速偏快。",
        "B": "少女女声，音色软糯，语速偏慢。",
    })
    vc = _load_vc(clone_ws)
    assert vc["A"]["gender"] == "female"  # never clobbered
    assert vc["B"]["gender"] == "female"  # pre-filled from the description text


# --------------------------------------------------------------------------- #
# list_voices  (the GET /api/tts/voices view: candidates + selection per character)
# --------------------------------------------------------------------------- #

def test_list_voices_returns_candidates_and_selection(clone_ws):
    dv = clone_ws / "04_voice_profiles" / "designed_voices"
    dv.mkdir(parents=True, exist_ok=True)
    for name in ("a_c1.wav", "a_c2.wav", "a_c3.wav", "b_c1.wav", "b_c2.wav", "c_legacy.wav"):
        (dv / name).write_bytes(b"0")
    # First-appearance order is C, A, B — deliberately NOT count-descending, so the
    # assertion below proves the list is re-sorted by line count (3/2/1).
    _seed_script(clone_ws, {"C": 1, "A": 3, "B": 2})
    _seed_foundations(clone_ws, ["A", "B", "C"], extra={
        "A": {"type": "clone", "description": "d", "ref_text": "r",
              "ref_audio": "04_voice_profiles/designed_voices/a_c1.wav",
              "candidates": [
                  {"id": "1", "ref_audio": "04_voice_profiles/designed_voices/a_c1.wav", "seed": 1},
                  {"id": "2", "ref_audio": "04_voice_profiles/designed_voices/a_c2.wav", "seed": 2},
                  {"id": "3", "ref_audio": "04_voice_profiles/designed_voices/a_c3.wav", "seed": 3}],
              "selected_audio_id": "2", "foundation_status": "done", "clone_status": "done"},
        "B": {"type": "clone", "description": "d", "ref_text": "r",
              "ref_audio": "04_voice_profiles/designed_voices/b_c1.wav",
              "candidates": [
                  {"id": "1", "ref_audio": "04_voice_profiles/designed_voices/b_c1.wav", "seed": 1},
                  {"id": "2", "ref_audio": "04_voice_profiles/designed_voices/b_c2.wav", "seed": 2}],
              "selected_audio_id": None, "foundation_status": "done", "clone_status": "done"},
        # C is a pre-candidates (legacy) clone: the view synthesises one candidate.
        "C": {"type": "clone", "description": "d", "ref_text": "r",
              "ref_audio": "04_voice_profiles/designed_voices/c_legacy.wav",
              "foundation_status": "done", "clone_status": "done"},
    })
    out = list_voices()
    # The list is sorted by line_count descending; first-appearance order must not leak.
    assert [s["name"] for s in out["speakers"]] == ["A", "B", "C"]
    by_name = {s["name"]: s for s in out["speakers"]}
    a = by_name["A"]
    assert [c["id"] for c in a["candidates"]] == ["1", "2", "3"]
    assert [c["preview"] for c in a["candidates"]] == [
        "designed_voices/a_c1.wav", "designed_voices/a_c2.wav", "designed_voices/a_c3.wav"]
    assert [c["seed"] for c in a["candidates"]] == [1, 2, 3]
    assert a["selected_audio_id"] == "2"
    assert a["preview"] == "designed_voices/a_c1.wav"  # top level mirrors the stored ref
    b = by_name["B"]
    assert [c["id"] for c in b["candidates"]] == ["1", "2"]
    assert b["selected_audio_id"] is None  # the default first candidate is active
    c = by_name["C"]
    assert [x["id"] for x in c["candidates"]] == ["1"]
    assert c["candidates"][0]["preview"] == "designed_voices/c_legacy.wav"
    assert c["selected_audio_id"] is None
