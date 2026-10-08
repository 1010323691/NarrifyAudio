"""Atomic persistence for chapter-level BGM analysis and assignment caches."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import logging
import hashlib
import uuid
from contextlib import ExitStack

from backend.core.file_lock import exclusive_file_lock, shared_file_lock

log = logging.getLogger("audiobook.bgm")

ANALYSIS_NAME = "chapter_music_analysis.json"
ASSIGNMENTS_NAME = "bgm_assignments.json"
SEGMENT_ANALYSIS_NAME = "segment_music_analysis.json"
TIMELINE_DIR = "timelines"
LOCK_NAME = ".bgm_storage.lock"
STATE_DIR = ".state-v2"
_held = threading.local()
_KINDS = {ANALYSIS_NAME: "analysis", ASSIGNMENTS_NAME: "assignments", SEGMENT_ANALYSIS_NAME: "segments"}


@contextmanager
def storage_lock(layout):
    """Exclusive maintenance/rollback access, reentrant within the owning thread.

    Normal chapter writers share this lock and take their own chapter lock;
    migration, project-wide edits and guarded rollback exclude all of them.
    """
    path = _bgm_dir(layout) / LOCK_NAME
    held = getattr(_held, "paths", set())
    if path in held:
        yield
        return
    with exclusive_file_lock(path):
        _held.paths = held | {path}
        try:
            yield
        finally:
            _held.paths = held


@contextmanager
def _chapter_locks(layout, stems):
    """Readers/writers share maintenance access; only overlapping chapters wait."""
    maintenance = _bgm_dir(layout) / LOCK_NAME
    with ExitStack() as stack:
        if maintenance not in getattr(_held, "paths", set()):
            stack.enter_context(shared_file_lock(maintenance))
        for stem in sorted(set(stems)):
            stack.enter_context(exclusive_file_lock(_root(layout) / "locks" / (_stem_key(stem) + ".lock")))
        yield


def _root(layout):
    root = _bgm_dir(layout) / STATE_DIR
    if root.is_symlink() or _bgm_dir(layout).is_symlink():
        raise ValueError("BGM state directory must not be a symlink")
    return root


def _stem_key(stem):
    return hashlib.sha256(str(stem).encode("utf-8")).hexdigest()


def chapter_path(layout, name, stem, *, summary=False):
    kind = _KINDS[name] + ("-summaries" if summary else "")
    return _root(layout) / kind / (_stem_key(stem) + ".json")


def _complete(layout):
    marker = _root(layout) / "migration-complete.json"
    return not marker.is_symlink() and marker.is_file()

def _bgm_dir(layout) -> Path:
    return layout.bgm


def _default_analysis() -> dict:
    return {"version": 1, "model": "", "chapters": {}}


def _default_assignments() -> dict:
    return {"version": 1, "mode": "random", "updated_at": "", "chapters": {}}


def _default_segment_analysis() -> dict:
    return {"version": 1, "model": "", "chapters": {}}


def _atomic_write_json(p: Path, data: dict, handle=None) -> None:
    if p.parent.is_symlink():
        raise ValueError("BGM state parent must not be a symlink")
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    stage_file = getattr(handle, "publish_workspace_bytes", None) or getattr(handle, "stage_workspace_file", None)
    if callable(stage_file):
        stage_file(p, payload)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".bgm_", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        from ..core.workspace_epochs import managed_mutation
        with managed_mutation(p):
            os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_cached(layout, name: str, default_factory) -> dict:
    p = _bgm_dir(layout) / name
    if p.is_symlink() or not p.exists():
        return default_factory()
    try:
        data = json.loads(p.read_bytes().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        chapters = data.get("chapters")
        if not isinstance(chapters, dict):
            data["chapters"] = {}
        return data
    except Exception as e:
        log.warning("08_bgm 缓存 %s 损坏，降级为空：%s", name, e)
        return default_factory()


def _default(name):
    return _default_assignments() if name == ASSIGNMENTS_NAME else _default_analysis()


def _read_json(path, default, max_bytes=None):
    try:
        if path.is_symlink() or path.parent.is_symlink() or (max_bytes is not None and path.stat().st_size > max_bytes):
            return default
        data = json.loads(path.read_bytes())
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        log.warning("BGM 状态损坏，忽略 %s", path)
        return default


def _summary(name, entry):
    if not isinstance(entry, dict):
        return entry
    if name != SEGMENT_ANALYSIS_NAME:
        return dict(entry)
    tags = {}
    blocks = entry.get("blocks") or entry.get("entries") or []
    for block in blocks:
        if not isinstance(block, dict) or block.get("extend"):
            continue
        block_tags = block.get("music_tags") or block.get("tags") or {}
        if not isinstance(block_tags, dict):
            continue
        for category, values in block_tags.items():
            union = tags.setdefault(category, [])
            for value in values or []:
                if isinstance(value, str) and value not in union:
                    union.append(value)
    return {"fingerprint": entry.get("fingerprint"), "entry_count": entry.get("entry_count", 0),
            "analyzed_at": entry.get("analyzed_at", ""), "has_analysis": bool(blocks), "tags": tags}


def _envelope(stem, entry, *, source_version=None, revision=None):
    encoded = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"version": 2, "stem": stem, "source_version": source_version or hashlib.sha256(encoded).hexdigest(),
            "revision": revision, "entry": entry}


def ensure_migrated(layout):
    """One recoverable migration, serialized against all v2 writers/rollback.

    Legacy snapshots are immutable. Completion is the final write; an interrupted
    migration resumes from those snapshots before any v2 writer can publish.
    """
    if _complete(layout):
        return
    with storage_lock(layout):
        if _complete(layout):
            return
        snapshot = _root(layout) / "legacy-snapshot"
        for name in _KINDS:
            frozen = snapshot / name
            if not frozen.exists():
                _atomic_write_json(frozen, _load_cached(layout, name, lambda: _default(name)))
        _atomic_write_json(snapshot / "snapshot-complete.json", {"version": 2})
        counts = {}
        for name, kind in _KINDS.items():
            data = _read_json(snapshot / name, _default(name))
            chapters = data.get("chapters") or {}
            for stem, entry in chapters.items():
                envelope = _envelope(stem, entry)
                _atomic_write_json(chapter_path(layout, name, stem), envelope)
                _atomic_write_json(chapter_path(layout, name, stem, summary=True), _envelope(stem, _summary(name, entry), source_version=envelope["source_version"]))
            _atomic_write_json(_root(layout) / kind / "metadata.json", {key: value for key, value in data.items() if key != "chapters"})
            counts[kind] = len(chapters)
        _atomic_write_json(_root(layout) / "migration-complete.json", {"version": 2, "chapters": counts})


def _load(layout, name, stems=None, *, summary=False, max_bytes=None):
    if not _complete(layout):
        # Read-only access never migrates a workspace in an HTTP request.
        legacy = _bgm_dir(layout) / name
        if max_bytes is not None and legacy.is_file() and (legacy.is_symlink() or legacy.stat().st_size > max_bytes):
            return _default(name)
        data = _load_cached(layout, name, lambda: _default(name))
        chapters = data.get("chapters") or {}
        if stems is not None:
            chapters = {stem: chapters[stem] for stem in stems if stem in chapters}
        data["chapters"] = {stem: _summary(name, entry) for stem, entry in chapters.items()} if summary else chapters
        return data
    kind = _KINDS[name]
    data = {**_default(name), **_read_json(_root(layout) / kind / "metadata.json", {}, max_bytes)}
    chapters = {}
    if stems is None:
        folder = _root(layout) / (kind + ("-summaries" if summary else ""))
        paths = [] if folder.is_symlink() else ((None, path) for path in sorted(folder.glob("*.json")) if path.name != "metadata.json")
    else:
        paths = ((stem, chapter_path(layout, name, stem, summary=summary)) for stem in stems)
    for expected_stem, path in paths:
        envelope = _read_json(path, {}, max_bytes)
        stem = envelope.get("stem")
        if envelope.get("version") != 2 or not isinstance(stem, str) or (expected_stem is not None and expected_stem != stem):
            continue
        if path.name != _stem_key(stem) + ".json":
            continue
        if not envelope.get("deleted"):
            chapters[stem] = envelope.get("entry")
    data["chapters"] = chapters
    return data


def load_analysis(layout, stems=None):
    return _load(layout, ANALYSIS_NAME, stems)


def load_assignments(layout, stems=None, *, max_bytes=None):
    return _load(layout, ASSIGNMENTS_NAME, stems, max_bytes=max_bytes)


def load_segment_analysis(layout, stems=None):
    return _load(layout, SEGMENT_ANALYSIS_NAME, stems)


def load_segment_summaries(layout, stems):
    return _load(layout, SEGMENT_ANALYSIS_NAME, stems, summary=True)


def _guard(layout, path, handle):
    if handle is None:
        return
    mark = getattr(handle, "mark_workspace_guarded", None)
    if callable(mark):
        mark(path)
    set_lock = getattr(handle, "set_rollback_lock", None)
    if callable(set_lock):
        # Exclusive maintenance access excludes every per-chapter writer during
        # compare-and-restore, including API edits after task publication.
        set_lock(storage_lock, layout)


def _publish(layout, name, before, after, handle):
    revision = uuid.uuid4().hex
    modified = False
    previous = before.get("chapters") or {}
    chapters = after.get("chapters") or {}
    for stem, entry in chapters.items():
        if stem in previous and entry == previous[stem]:
            continue
        modified = True
        envelope = _envelope(stem, entry, revision=revision)
        for summary in (False, True):
            path = chapter_path(layout, name, stem, summary=summary)
            _guard(layout, path, handle)
            payload = _envelope(stem, _summary(name, entry), source_version=envelope["source_version"], revision=revision) if summary else envelope
            _atomic_write_json(path, payload, handle)
    # Explicit removals publish tombstones so no old snapshot can reappear.
    for stem in previous.keys() - chapters.keys():
        modified = True
        for summary in (False, True):
            path = chapter_path(layout, name, stem, summary=summary)
            _guard(layout, path, handle)
            _atomic_write_json(path, {"version": 2, "stem": stem, "deleted": True, "revision": revision}, handle)
    metadata = {key: value for key, value in after.items() if key != "chapters"}
    old_metadata = {key: value for key, value in before.items() if key != "chapters"}
    changed = {key: value for key, value in metadata.items() if key not in old_metadata or old_metadata[key] != value}
    removed = old_metadata.keys() - metadata.keys()
    if not modified and not changed and not removed:
        return
    path = _root(layout) / _KINDS[name] / "metadata.json"
    with exclusive_file_lock(_root(layout) / "locks" / (_KINDS[name] + "-metadata.lock")):
        current = _read_json(path, {})
        merged = {key: value for key, value in current.items() if key not in removed}
        merged.update(changed)
        merged["_publication_version"] = revision
        if current != merged:
            _guard(layout, path, handle)
            _atomic_write_json(path, merged, handle)


def _update(layout, name, mutator, handle=None, stems=None):
    import copy
    stems = tuple(sorted(set(stems))) if stems is not None else None
    ensure_migrated(layout)
    # A full-project maintenance edit is uncommon (e.g. a tag rename). Hot
    # chapter tasks provide their exact read/write scope, including neighbours.
    lock = storage_lock(layout) if stems is None else _chapter_locks(layout, stems)
    with lock:
        before = _load(layout, name, stems)
        data = copy.deepcopy(before)
        mutator(data)
        if stems is not None and not set(data.get("chapters", {})).issubset(set(stems)):
            raise ValueError("BGM mutation exceeded its chapter scope")
        _publish(layout, name, before, data, handle)
        return data


def update_analysis(layout, mutator, handle=None, *, stems=None):
    return _update(layout, ANALYSIS_NAME, mutator, handle, stems)


def update_assignments(layout, mutator, handle=None, *, stems=None):
    return _update(layout, ASSIGNMENTS_NAME, mutator, handle, stems)


def update_segment_analysis(layout, mutator, handle=None, *, stems=None):
    return _update(layout, SEGMENT_ANALYSIS_NAME, mutator, handle, stems)


def _save(layout, name, data, handle=None):
    return _update(layout, name, lambda current: (current.clear(), current.update(data)), handle)


def save_analysis(layout, data, handle=None):
    _save(layout, ANALYSIS_NAME, data, handle)


def save_assignments(layout, data, handle=None):
    _save(layout, ASSIGNMENTS_NAME, data, handle)


def save_segment_analysis(layout, data, handle=None):
    _save(layout, SEGMENT_ANALYSIS_NAME, data, handle)


def export_legacy(layout, destination: Path):
    """Write a downgrade export, preserving both live v2 and original legacy files."""
    destination = Path(destination)
    if destination.resolve() == _bgm_dir(layout).resolve():
        raise ValueError("Export to a separate directory before downgrading")
    with storage_lock(layout):
        if not _complete(layout):
            raise ValueError("BGM migration has not completed")
        for name in _KINDS:
            data = _load(layout, name)
            data.pop("_publication_version", None)
            _atomic_write_json(destination / name, data)
