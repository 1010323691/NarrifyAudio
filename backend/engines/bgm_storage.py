"""Atomic persistence for chapter-level BGM analysis and assignment caches."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import logging

from backend.core.file_lock import exclusive_file_lock

log = logging.getLogger("audiobook.bgm")
_BGMS_LOCK = threading.RLock()

ANALYSIS_NAME = "chapter_music_analysis.json"
ASSIGNMENTS_NAME = "bgm_assignments.json"
SEGMENT_ANALYSIS_NAME = "segment_music_analysis.json"
TIMELINE_DIR = "timelines"
LOCK_NAME = ".bgm_storage.lock"


@contextmanager
def storage_lock(layout):
    """Cross-process advisory lock around the ``08_bgm`` JSON caches.

    The in-process ``_BGMS_LOCK`` cannot coordinate the independent API and
    Worker processes, so every read → modify → publish cycle additionally
    holds this lock (same pattern as ``engines.music`` index updates). The
    lock file lives in the workspace's ``08_bgm`` directory so every
    process locks the same path; a stale process cannot hold it forever
    (the lock dies with the process, and acquisition times out rather
    than blocking forever).
    """
    with exclusive_file_lock(_bgm_dir(layout) / LOCK_NAME):
        yield

def _bgm_dir(layout) -> Path:
    return layout.bgm


def _default_analysis() -> dict:
    return {"version": 1, "model": "", "chapters": {}}


def _default_assignments() -> dict:
    return {"version": 1, "mode": "llm", "updated_at": "", "chapters": {}}


def _default_segment_analysis() -> dict:
    return {"version": 1, "model": "", "chapters": {}}


def _atomic_write_json(p: Path, data: dict, handle=None) -> None:
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
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_cached(layout, name: str, default_factory) -> dict:
    p = _bgm_dir(layout) / name
    if not p.exists():
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


def load_analysis(layout) -> dict:
    return _load_cached(layout, ANALYSIS_NAME, _default_analysis)


def load_assignments(layout) -> dict:
    return _load_cached(layout, ASSIGNMENTS_NAME, _default_assignments)


def load_segment_analysis(layout) -> dict:
    return _load_cached(layout, SEGMENT_ANALYSIS_NAME, _default_segment_analysis)




def save_analysis(layout, data: dict, handle=None) -> None:
    with _BGMS_LOCK:
        _atomic_write_json(_bgm_dir(layout) / ANALYSIS_NAME, data, handle)


def save_assignments(layout, data: dict, handle=None) -> None:
    with _BGMS_LOCK:
        _atomic_write_json(_bgm_dir(layout) / ASSIGNMENTS_NAME, data, handle)


def save_segment_analysis(layout, data: dict, handle=None) -> None:
    with _BGMS_LOCK:
        _atomic_write_json(_bgm_dir(layout) / SEGMENT_ANALYSIS_NAME, data, handle)


def update_analysis(layout, mutator, handle=None) -> dict:
    """Atomic read → mutate → write on the analysis cache.

    Mirrors :func:`backend.engines.music.update_index`: the module lock is
    held across load → mutator → save, so parallel analyze tasks (each
    rewriting the whole file) cannot lose each other's entries — and the
    cross-process :func:`storage_lock` extends the same guarantee to the
    API process's tag propagation. ``mutator`` may raise to abort — nothing
    is written. Returns the saved data.
    """
    with _BGMS_LOCK:
        with storage_lock(layout):
            data = load_analysis(layout)
            mutator(data)
            save_analysis(layout, data, handle)
            return data


def update_assignments(layout, mutator, handle=None) -> dict:
    """Atomic read → mutate → write on the assignments file (same pattern as
    :func:`update_analysis` — concurrent matches on disjoint stems can't
    lose each other's entries)."""
    with _BGMS_LOCK:
        with storage_lock(layout):
            data = load_assignments(layout)
            mutator(data)
            save_assignments(layout, data, handle)
            return data


def update_segment_analysis(layout, mutator, handle=None) -> dict:
    """Atomic read → mutate → write on the segment-analysis cache (same pattern
    as :func:`update_analysis` — parallel paragraph-analysis tasks on disjoint
    chapters can't lose each other's entries)."""
    with _BGMS_LOCK:
        with storage_lock(layout):
            data = load_segment_analysis(layout)
            mutator(data)
            save_segment_analysis(layout, data, handle)
            return data

