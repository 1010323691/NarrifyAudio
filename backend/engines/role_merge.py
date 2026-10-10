"""Atomic batch role merge / undo / veto over the parsed scripts and the role-link table.

Every mutation holds ``role_links.merge_lock_path`` (the foundation-publication lock) for the
whole read-modify-write, so it cannot interleave with voice publication or another merge.
Files are written together; if a write raises, the already-written ones are restored and the
link table is left untouched. That covers errors the process survives: a crash mid-write, or a
restore that itself fails, can leave files half-merged (the latter is logged and reported).
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..core import pathio
from ..core import role_links as RL
from ..core.bounded_json import read_json
from ..core.file_lock import exclusive_file_lock
from ..core.paths import ALL_PARSED_JSON, resolve_parsed_json, resolve_parsed_json_all
from ..core.role_hints import fold_script, suggest_role_links
from . import tts_batch as Batch

_SAMPLE_CHARS = 80
log = logging.getLogger(__name__)


class MergeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Scope:
    paths: list[Path]
    files: dict[Path, list]
    has_script: bool
    names: list[str]
    counts: dict[str, int]
    cooccur: dict
    samples: dict[str, str]
    voice_config: dict
    vc_path: Path
    vc_exists: bool
    roles: set[str] = field(default_factory=set)


def _line_hash(entry: dict) -> str:
    return hashlib.sha1(str(entry.get("text") or "").encode("utf-8")).hexdigest()[:16]


def _identity(entry: dict) -> tuple[str, str]:
    """(role, field) — ``speaker`` first, ``type`` only when ``speaker`` is empty."""
    speaker = (entry.get("speaker") or "").strip()
    if speaker:
        return speaker, "speaker"
    return (entry.get("type") or "").strip(), "type"


def preview_of(layout, ref: str) -> str:
    """A stored reference-audio path relative to 04_voice_profiles/ ('' when absent)."""
    if not ref:
        return ""
    try:
        path = pathio.resolve_path(ref, layout.workspace, strict=False)
    except pathio.PathOutsideWorkspace:
        path = None
    if path is None or not path.exists():
        return ""
    try:
        return str(path.relative_to(layout.voice_profiles)).replace(os.sep, "/")
    except ValueError:
        return str(path)


def _state_path(layout, script: str | None) -> Path:
    """State file of a scope; an omitted script means "the newest file", never the whole book."""
    return RL.state_path(layout, script or resolve_parsed_json(None).name)


def read_scope(layout, script: str | None, strict: bool = False) -> Scope:
    """Read the roles of a scope. ``strict`` turns corrupt inputs into 400s (write paths)."""
    if script == ALL_PARSED_JSON:
        paths = [p for p in resolve_parsed_json_all() if p.exists()]
    else:
        paths = [resolve_parsed_json(script)]
    files: dict[Path, list] = {}
    order: list[str] = []
    counts: dict[str, int] = {}
    cooccur: dict = {}
    samples: dict[str, str] = {}
    has_script = False
    for path in paths:
        if not path.exists():
            continue
        try:
            data = read_json(path)
            if not isinstance(data, list):
                raise ValueError
        except (OSError, ValueError, UnicodeError):
            if strict:
                raise MergeError(400, f"解析文件已损坏，无法合并：{path.name}")
            continue
        files[path] = data
        pairs: dict = {}
        has_script = fold_script(order, counts, [e for e in data if isinstance(e, dict)], pairs) or has_script
        for key, n in pairs.items():
            cooccur[key] = cooccur.get(key, 0) + n
        for entry in data:
            if isinstance(entry, dict):
                name, _ = _identity(entry)
                if name and name not in samples and isinstance(entry.get("text"), str) and entry["text"].strip():
                    samples[name] = entry["text"].strip()[:_SAMPLE_CHARS]

    vc_path = layout.voice_profiles / "voice_config.json"
    voice_config: dict = {}
    vc_exists = False
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config, vc_exists = loaded, True
        except Exception:  # noqa: BLE001
            if strict:
                raise MergeError(400, "声音配置已损坏，无法合并角色。")
        _n, migrated = pathio.migrate_entries_in(vc_path, layout.workspace, "dict", ("ref_audio",))
        if isinstance(migrated, dict):
            voice_config = migrated
    names = sorted(order if has_script else list(voice_config), key=lambda sp: -counts.get(sp, 0))
    return Scope(paths, files, has_script, names, counts, cooccur, samples, voice_config, vc_path, vc_exists,
                 set(counts) | set(voice_config))


def link_hints(layout, script: str | None, names, voice_config: dict, counts: dict, cooccur: dict) -> dict:
    """``{role: {target, basis}}`` for the list page. Read-only: a missing or stale table is
    answered by an in-memory match (what a rebuild would store), so listing never writes."""
    state = RL.load_state(_state_path(layout, script))
    if state is not None and state["fingerprint"] == RL.fingerprint(names, voice_config):
        return state["links"]
    vetoes = [(v["source"], v["target"]) for v in state["vetoes"]] if state else ()
    found = suggest_role_links(list(names), voice_config, counts, cooccur, vetoes)
    return {s: {"target": t, "basis": b} for s, (t, b) in found.items()}


def _ensure(layout, script, scope: Scope) -> dict:
    return RL.ensure_state(_state_path(layout, script), scope.names, scope.voice_config, scope.counts, scope.cooccur)


def _check_version(state: dict, version: int | None) -> None:
    if version is not None and version != state["version"]:
        raise MergeError(409, "角色数据已变化，已刷新，请重新确认")


def _locked(layout):
    return exclusive_file_lock(RL.merge_lock_path(layout), timeout=10.0)


def _check_busy(busy) -> None:
    """Re-check, now that the lock is held, that no voice task started since the request guard."""
    if busy is not None and busy():
        raise MergeError(409, "配音任务进行中，请待其结束后再修改角色。")


def _lock_busy() -> MergeError:
    return MergeError(409, "角色数据正被其他操作占用，请稍后重试。")


# ---------------------------------------------------------------- undo bookkeeping

def _record_status(layout, record: dict, names: set[str], cache: dict) -> tuple[bool, str]:
    if record["source"] in names:
        return False, "角色名已被占用，无法自动拆分"
    for filename, items in (record.get("payload") or {}).get("rewrites", {}).items():
        path = layout.parsed_json / filename
        if path not in cache:
            try:
                cache[path] = read_json(path)
            except (OSError, ValueError, UnicodeError):
                cache[path] = None
        data = cache[path]
        if not isinstance(data, list):
            return False, "台词已变化，无法自动拆分"
        for index, field_name, digest in items:
            entry = data[index] if index < len(data) else None
            if (not isinstance(entry, dict) or (entry.get(field_name) or "").strip() != record["target"]
                    or (field_name == "type" and (entry.get("speaker") or "").strip())
                    or _line_hash(entry) != digest):
                return False, "台词已变化，无法自动拆分"
    return True, ""


def _write_all(changed: dict[Path, list], originals: dict[Path, list], vc: tuple[Path, dict, dict] | None,
               after) -> None:
    """Write files then ``after()``; on any failure put every touched file back."""
    done: list[Path] = []
    vc_written = False
    try:
        for path, data in changed.items():
            pathio.rewrite_json_file(path, data)
            done.append(path)
        if vc is not None:
            pathio.rewrite_json_file(vc[0], vc[1])
            vc_written = True
        after()
    except Exception as error:
        unrestored: list[str] = []
        for path in reversed(done):
            try:
                pathio.rewrite_json_file(path, originals[path])
            except Exception:  # noqa: BLE001 - keep restoring the rest, then report
                log.exception("role merge rollback failed for %s", path)
                unrestored.append(path.name)
        if vc_written:
            try:
                pathio.rewrite_json_file(vc[0], vc[2])
            except Exception:  # noqa: BLE001
                log.exception("role merge rollback failed for %s", vc[0])
                unrestored.append(vc[0].name)
        if unrestored:
            raise MergeError(500, f"写入失败且以下文件未能还原，请检查后重试：{'、'.join(unrestored)}") from error
        raise


# ---------------------------------------------------------------- graph

def _graph(layout, script, scope: Scope, state: dict) -> dict:
    names = set(scope.names)
    cache: dict = {}
    records = []
    for record in state["records"]:
        ok, reason = _record_status(layout, record, names, cache)
        records.append({"id": record["id"], "source": record["source"], "target": record["target"],
                        "merged_at": record["merged_at"],
                        "line_count": (record.get("payload") or {}).get("line_count", 0),
                        "undoable": ok, "reason": reason,
                        "children": [c["source"] for c in record.get("children") or []]})
    involved = set(state["links"]) | {l["target"] for l in state["links"].values()} | {r["target"] for r in records}
    roles = []
    for name in scope.names:
        if name not in involved:
            continue
        entry = scope.voice_config.get(name) or {}
        roles.append({"name": name, "gender": entry.get("gender", ""), "line_count": scope.counts.get(name, 0),
                      "sample": scope.samples.get(name, ""), "preview": preview_of(layout, entry.get("ref_audio", "")),
                      "voice_config": bool(entry),
                      "cloned": bool(entry.get("ref_audio")) or entry.get("clone_status") == "done"})
    return {"version": state["version"], "script": script or "", "roles": roles, "links": state["links"],
            "records": records, "vetoes": state["vetoes"], "new_candidates": state["new_candidates"]}


def build_graph(layout, script: str | None) -> dict:
    """Read-only snapshot. A stale table is rebuilt in memory only (same result a write would
    persist, same version number), so polling never takes the lock or touches disk."""
    scope = read_scope(layout, script)
    state, _ = RL.current_state(_state_path(layout, script), scope.names, scope.voice_config, scope.counts, scope.cooccur)
    return _graph(layout, script, scope, state)


# ---------------------------------------------------------------- operations

def merge_batch(layout, script: str | None, target: str, sources: list[str], version: int | None = None,
                busy=None) -> dict:
    target = (target or "").strip()
    sources = list(dict.fromkeys((s or "").strip() for s in sources))
    if not target or not sources or "" in sources:
        raise MergeError(400, "角色名不能为空。")
    if target in sources:
        raise MergeError(400, "源角色与目标角色相同。")
    try:
        with _locked(layout):
            _check_busy(busy)
            scope = read_scope(layout, script, strict=True)
            for source in sources:
                if source not in scope.roles:
                    raise MergeError(404, f"未找到角色：{source}")
            if target not in scope.roles:
                raise MergeError(400, f"目标角色不在当前范围：{target}")
            state = _ensure(layout, script, scope)
            _check_version(state, version)

            merged = set(sources)
            payloads = {s: {"rewrites": {}, "line_count": 0, "voice_config": None} for s in sources}
            edits: dict[Path, list[tuple[int, str]]] = {}
            for path, data in scope.files.items():
                for index, entry in enumerate(data):
                    if not isinstance(entry, dict):
                        continue
                    name, field_name = _identity(entry)
                    if name in merged:
                        edits.setdefault(path, []).append((index, field_name))
                        payload = payloads[name]
                        payload["rewrites"].setdefault(path.name, []).append([index, field_name, _line_hash(entry)])
                        payload["line_count"] += 1
            changed = {}
            for path, items in edits.items():
                data = copy.deepcopy(scope.files[path])
                for index, field_name in items:
                    data[index][field_name] = target
                changed[path] = data

            original_vc = copy.deepcopy(scope.voice_config)
            new_vc = copy.deepcopy(scope.voice_config)
            vc_changed = False
            for source in sources:
                if source in new_vc:
                    payloads[source]["voice_config"] = new_vc.pop(source)
                    vc_changed = True
            post_names = [n for n in scope.names if n not in merged]
            if target not in post_names:
                post_names.append(target)
            outcome = RL.apply_merge(state, target, sources, payloads, post_names, new_vc, scope.cooccur)

            fresh: list[Scope] = []

            def commit():
                # Files are already rewritten: fingerprint what the reader will see from now on.
                fresh.append(read_scope(layout, script))
                state["fingerprint"] = RL.fingerprint(fresh[0].names, fresh[0].voice_config)
                RL.save_state(_state_path(layout, script), state)

            _write_all(changed, scope.files, (scope.vc_path, new_vc, original_vc) if vc_changed else None, commit)
            Batch.invalidate_speaker_outputs([*sources, target], layout)

            replaced = sum(p["line_count"] for p in payloads.values())
            after = fresh[0]
            return {"ok": True, "target": target, "sources": sources, "source": sources[0], "replaced": replaced,
                    "files": [p.name for p in changed], "orphans": outcome["orphans"],
                    "rematched": outcome["rematched"], "graph": _graph(layout, script, after, state)}
    except TimeoutError:
        raise _lock_busy()


def undo_merge(layout, script: str | None, record_id: str, version: int | None = None, busy=None) -> dict:
    try:
        with _locked(layout):
            _check_busy(busy)
            scope = read_scope(layout, script, strict=True)
            state = _ensure(layout, script, scope)
            _check_version(state, version)
            record = next((r for r in state["records"] if r["id"] == record_id), None)
            if record is None:
                raise MergeError(404, "未找到合并记录。")
            ok, reason = _record_status(layout, record, set(scope.names), {})
            if not ok:
                raise MergeError(409, reason)
            source = record["source"]
            payload = record.get("payload") or {}

            changed = {}
            originals = {}
            for filename, items in payload.get("rewrites", {}).items():
                path = layout.parsed_json / filename
                originals[path] = read_json(path)
                data = copy.deepcopy(originals[path])
                for index, field_name, _digest in items:
                    data[index][field_name] = source
                changed[path] = data

            original_vc = copy.deepcopy(scope.voice_config)
            new_vc = copy.deepcopy(scope.voice_config)
            vc_changed = bool(payload.get("voice_config")) and source not in new_vc
            if vc_changed:
                new_vc[source] = payload["voice_config"]
            post_names = [*scope.names, source]
            RL.undo_merge(state, record_id, post_names, new_vc)

            fresh: list[Scope] = []

            def commit():
                # Files are already rewritten: fingerprint what the reader will see from now on.
                fresh.append(read_scope(layout, script))
                state["fingerprint"] = RL.fingerprint(fresh[0].names, fresh[0].voice_config)
                RL.save_state(_state_path(layout, script), state)

            vc = (scope.vc_path, new_vc, original_vc) if vc_changed else None
            _write_all(changed, originals, vc, commit)
            Batch.invalidate_speaker_outputs([source, record["target"]], layout)
            after = fresh[0]
            return {"ok": True, "source": source, "target": record["target"], "graph": _graph(layout, script, after, state)}
    except TimeoutError:
        raise _lock_busy()


def _link_change(layout, script, version, operate, busy=None) -> dict:
    try:
        with _locked(layout):
            _check_busy(busy)
            scope = read_scope(layout, script)
            state = _ensure(layout, script, scope)
            _check_version(state, version)
            try:
                extra = operate(state, scope)
            except (ValueError, KeyError):
                raise MergeError(404, "指向关系已变化，请刷新后重试。")
            RL.save_state(_state_path(layout, script), state)
            return {"ok": True, **(extra or {}), "graph": _graph(layout, script, scope, state)}
    except TimeoutError:
        raise _lock_busy()


def add_veto(layout, script, source: str, target: str, version: int | None = None, busy=None) -> dict:
    return _link_change(layout, script, version, lambda state, scope: RL.add_veto(
        state, source, target, scope.names, scope.voice_config, scope.cooccur), busy)


def remove_veto(layout, script, source: str, target: str, version: int | None = None, busy=None) -> dict:
    return _link_change(layout, script, version, lambda state, scope: {"restored": RL.remove_veto(
        state, source, target, scope.names, scope.voice_config)}, busy)


def mark_reviewed(layout, script, target: str) -> dict:
    try:
        with _locked(layout):
            scope = read_scope(layout, script)
            state = _ensure(layout, script, scope)
            cleared = RL.mark_seen(state, target)
            if cleared:
                RL.save_state(_state_path(layout, script), state)
            return {"ok": True, "cleared": cleared, "version": state["version"]}
    except TimeoutError:
        raise _lock_busy()
