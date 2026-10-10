"""Persistent role-link table: who each role is suspected to be, merge records and user vetoes.

One JSON state file per scope (``__all__`` or a single parsed script) under
``04_voice_profiles/role_links/``. The list page and the batch-merge dialog read the same
table, so a merge only moves the links that the merge itself invalidates ("orphans") instead
of recomputing every hint from scratch.

State shape::

    {"version": int, "fingerprint": str,
     "links": {role: {"target": str, "basis": str}},
     "vetoes": [{"source": str, "target": str, "basis": str}],
     "new_candidates": {target: [role, ...]},      # unseen additions to a target's chain
     "records": [{"id", "source", "target", "merged_at", "prior_link", "payload", "children"}]}

Functions here are pure state transitions plus load/save. Callers pass the roles and voice
config **as they are after the operation** (merged roles gone, restored roles back) and must
hold ``merge_lock_path(layout)`` around the whole read-modify-write, file rewrites included.
Nothing here reads parsed scripts; ``payload`` is opaque undo data owned by the caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .role_hints import _pair_key, rematch_one, suggest_role_links

ALL_SCOPE = "__all__"


def scope_key(script: str | None) -> str:
    if not script or script == ALL_SCOPE:
        return "all"
    return "file-" + hashlib.sha256(script.encode()).hexdigest()[:16]


def state_path(layout, script: str | None) -> Path:
    return Path(layout.voice_profiles) / "role_links" / (scope_key(script) + ".json")


def merge_lock_path(layout) -> Path:
    """Same lock the foundation publisher uses, so merges never interleave with voice writes."""
    return Path(layout.temp) / "tasks" / "foundation-publication.lock"


def new_state() -> dict:
    return {"version": 0, "fingerprint": "", "links": {}, "vetoes": [], "new_candidates": {}, "records": []}


def fingerprint(names, config: dict) -> str:
    rows = sorted([n, (config.get(n) or {}).get("gender") or "", (config.get(n) or {}).get("description") or ""]
                  for n in set(names))
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def load_state(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    state = new_state()
    for key in state:
        if key in data and isinstance(data[key], type(state[key])):
            state[key] = data[key]
    return state


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        pending.write_bytes(json.dumps(state, ensure_ascii=False, indent=2).encode())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


# ---------------------------------------------------------------- graph helpers

def _reverse(links: dict) -> dict[str, list[str]]:
    reverse: dict[str, list[str]] = {}
    for source, link in links.items():
        reverse.setdefault(link["target"], []).append(source)
    return reverse


def upstream(links: dict, node: str) -> set[str]:
    """Every role that points at ``node`` directly or through intermediaries."""
    reverse = _reverse(links)
    seen: set[str] = set()
    stack = list(reverse.get(node, ()))
    while stack:
        current = stack.pop()
        if current in seen or current == node:
            continue
        seen.add(current)
        stack.extend(reverse.get(current, ()))
    return seen


def downstream(links: dict, node: str) -> list[str]:
    """The roles ``node`` points at, nearest first (cycle-safe)."""
    chain: list[str] = []
    seen = {node}
    current = links.get(node)
    while current and current["target"] not in seen:
        chain.append(current["target"])
        seen.add(current["target"])
        current = links.get(current["target"])
    return chain


def _vetoed_pairs(state: dict) -> set[tuple[str, str]]:
    return {_pair_key(v["source"], v["target"]) for v in state["vetoes"]}


def _bump(state: dict, names, config: dict) -> None:
    state["version"] += 1
    state["fingerprint"] = fingerprint(names, config)


def _prune_candidates(state: dict, names) -> None:
    """Drop unseen-candidate entries that no longer describe a real chain."""
    independent = set(names)
    links = state["links"]
    for target in list(state["new_candidates"]):
        if target not in independent:
            del state["new_candidates"][target]
            continue
        chain = upstream(links, target)
        kept = [c for c in state["new_candidates"][target] if c in chain]
        if kept:
            state["new_candidates"][target] = kept
        else:
            del state["new_candidates"][target]


def _rematch(state: dict, orphans, names, config: dict, cooccur: dict | None) -> dict[str, str | None]:
    """Give each orphan a new target (never itself, its upstream, or a vetoed role)."""
    links = state["links"]
    independent = [n for n in names]
    vetoes = _vetoed_pairs(state)
    for orphan in orphans:
        links.pop(orphan, None)
    result: dict[str, str | None] = {}
    for orphan in sorted(orphans):
        blocked = upstream(links, orphan) | {orphan}
        found = rematch_one(orphan, [n for n in independent if n not in blocked], config, cooccur, vetoes)
        if found is None:
            result[orphan] = None
            continue
        target, basis = found
        links[orphan] = {"target": target, "basis": basis}
        result[orphan] = target
        fresh = {orphan} | upstream(links, orphan)
        for affected in [target, *downstream(links, target)]:
            known = state["new_candidates"].setdefault(affected, [])
            known.extend(sorted(fresh - set(known)))
    return result


# ---------------------------------------------------------------- lifecycle

def rebuild_links(state: dict, names, config: dict, counts: dict | None, cooccur: dict | None) -> None:
    """Recompute every link from scratch (first read, or the roles changed outside our merges)."""
    vetoes = [(v["source"], v["target"]) for v in state["vetoes"]]
    found = suggest_role_links(list(names), config, counts, cooccur, vetoes)
    state["links"] = {s: {"target": t, "basis": b} for s, (t, b) in found.items()}
    state["new_candidates"] = {}
    state["fingerprint"] = fingerprint(names, config)


def current_state(path: Path, names, config: dict, counts: dict | None, cooccur: dict | None) -> tuple[dict, bool]:
    """The table as it should be now, plus whether that differs from disk (rebuilt after external changes)."""
    state = load_state(path)
    if state is not None and state["fingerprint"] == fingerprint(names, config):
        return state, False
    state = state or new_state()
    rebuild_links(state, names, config, counts, cooccur)
    state["version"] += 1
    return state, True


def ensure_state(path: Path, names, config: dict, counts: dict | None, cooccur: dict | None) -> dict:
    """Like ``current_state`` but persists a rebuild. Only call while holding the merge lock."""
    state, rebuilt = current_state(path, names, config, counts, cooccur)
    if rebuilt:
        save_state(path, state)
    return state


# ---------------------------------------------------------------- operations

def apply_merge(state: dict, target: str, sources: list[str], payloads: dict[str, dict] | None,
                names, config: dict, cooccur: dict | None = None, now: str | None = None) -> dict:
    """Merge ``sources`` into ``target``; returns ``{orphans, rematched, records}``.

    ``names`` / ``config`` are the independent roles after the merge (sources already gone).
    """
    sources = list(dict.fromkeys(sources))
    merged = set(sources)
    independent = set(names)
    if not sources or target in merged or target not in independent or merged & independent:
        raise ValueError("invalid merge request")
    links = state["links"]
    now = now or datetime.now(timezone.utc).isoformat()
    orphans = sorted(r for r, link in links.items() if link["target"] in merged and r not in merged and r in independent)

    new_ids = []
    for source in sources:
        children = [rec for rec in state["records"] if rec["target"] == source]
        state["records"] = [rec for rec in state["records"] if rec["target"] != source]
        record = {"id": uuid.uuid4().hex, "source": source, "target": target, "merged_at": now,
                  "prior_link": links.get(source), "payload": (payloads or {}).get(source, {}),
                  "children": children}
        state["records"].append(record)
        new_ids.append(record["id"])
    for source in sources:
        links.pop(source, None)

    rematched = _rematch(state, orphans, names, config, cooccur)
    _prune_candidates(state, names)
    _bump(state, names, config)
    return {"orphans": orphans, "rematched": rematched, "records": new_ids}


def undo_merge(state: dict, record_id: str, names, config: dict) -> dict:
    """Restore one top-level merge record. ``names`` / ``config`` already include the restored role."""
    record = next((r for r in state["records"] if r["id"] == record_id), None)
    if record is None:
        raise KeyError(record_id)
    source = record["source"]
    independent = set(names)
    if source not in independent:
        raise ValueError("restored role missing from names")
    state["records"] = [r for r in state["records"] if r["id"] != record_id] + list(record.get("children") or [])
    prior = record.get("prior_link")
    links = state["links"]
    if (prior and prior["target"] in independent and prior["target"] != source
            and _pair_key(source, prior["target"]) not in _vetoed_pairs(state)
            and prior["target"] not in upstream(links, source)):
        links[source] = dict(prior)
    _prune_candidates(state, names)
    _bump(state, names, config)
    return {"source": source, "children": [c["source"] for c in record.get("children") or []]}


def add_veto(state: dict, source: str, target: str, names, config: dict, cooccur: dict | None = None) -> dict:
    """Rule out ``source`` -> ``target``; the role is then rematched like an orphan."""
    link = state["links"].get(source)
    if not link or link["target"] != target:
        raise ValueError("no such link")
    state["vetoes"].append({"source": source, "target": target, "basis": link["basis"]})
    rematched = _rematch(state, [source], names, config, cooccur)
    _prune_candidates(state, names)
    _bump(state, names, config)
    return {"rematched": rematched}


def remove_veto(state: dict, source: str, target: str, names, config: dict) -> bool:
    """Lift a veto; re-adopts the original link only if the role has none and it is still safe."""
    pair = _pair_key(source, target)
    entry = next((v for v in state["vetoes"] if _pair_key(v["source"], v["target"]) == pair), None)
    if entry is None:
        raise KeyError(pair)
    state["vetoes"].remove(entry)
    links = state["links"]
    independent = set(names)
    restored = (entry["source"] not in links and entry["source"] in independent and entry["target"] in independent
                and entry["target"] not in upstream(links, entry["source"]))
    if restored:
        links[entry["source"]] = {"target": entry["target"], "basis": entry["basis"]}
    _bump(state, names, config)
    return restored


def mark_seen(state: dict, target: str) -> bool:
    """Clear a target's unseen candidates. Not a data change, so ``version`` stays put."""
    return state["new_candidates"].pop(target, None) is not None
