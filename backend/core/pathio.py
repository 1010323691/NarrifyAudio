"""Unified path serialization / resolution for the workspace-persisted JSONs.

The JSON files the pipeline persists *inside the workspace* (the package
``manifest.json``, ``voice_config.json``, …) store file paths as
**workspace-root-relative** values (forward-slash form, e.g.
``05_audio_chunk/s/0001.mp3``). At runtime every such value is resolved
against the *current* workspace root, so a whole project can be moved to any
other location on disk and still open: nothing here assumes the workspace
lives at a fixed place.

Global / external resources are deliberately never converted: user-selected
tool binaries (``ffmpeg_path`` / ``ffprobe_path``) and the workspace pointer
itself stay absolute — only paths that point *inside the workspace* become
relative.

Legacy values (absolute paths recorded before this design, pointing into the
workspace's *old* location) are handled transparently:

* on read they are resolved against the current root;
* on the next save of their file (and, for manifests / voice configs, already
  at load time) they are rewritten in relative form — an idempotent migration;
* if the workspace was moved so the old absolute path is gone, the file is
  recovered from its original directory structure (the tail after the first
  known workspace directory) or, failing that, by a unique file name inside
  the workspace. Only when recovery is impossible is a *clear* error raised —
  never a silent miss.

All helpers are pure string manipulation plus existence probes; the only
filesystem access is ``exists`` / name search (no reads, no writes except the
explicit :func:`rewrite_json_file` rewrite performed by :func:`migrate_entries_in`).
"""
from __future__ import annotations

import json
import logging
import ntpath
import os
import posixpath
from pathlib import Path, PurePosixPath, PureWindowsPath

from .paths import WORKSPACE_DIR_NAMES

log = logging.getLogger("audiobook.pathio")

# The workspace's own top-level directory names — the anchors used to re-root a
# stale absolute path after a move (the tail after the first of these that
# appears in the old path is its workspace-relative position).
WORKSPACE_MARKERS: tuple[str, ...] = (*WORKSPACE_DIR_NAMES, "logs", "config")


class PathOutsideWorkspace(ValueError):
    """A relative path value escapes the workspace root (``..`` traversal).

    Such a value must never be persisted, and a caller resolving one gets a
    loud error instead of a path that points outside the project.
    """


class PathNotFoundError(RuntimeError):
    """A stored absolute path is gone and could not be recovered.

    The message is user-facing: it names the value, the current workspace, and
    the likely cause (the workspace was moved / deleted) plus the remedy.
    """


# -- normalization (pure string work, host-independent) ---------------------- #

def _is_abs(value: str) -> bool:
    """True for absolute paths in *any* style (native, Windows, or POSIX).

    A JSON value was written on some machine; checking all flavours keeps
    resolution correct when a project crosses hosts.
    """
    v = str(value).strip()
    if not v:
        return False
    return (
        Path(v).is_absolute()
        or PureWindowsPath(v).is_absolute()
        or PurePosixPath(v).is_absolute()
    )


def _norm(value: str) -> str:
    """Canonical form: slash-separated, ``.``/``..`` collapsed, case-folded on
    Windows. Pure string manipulation — never touches the filesystem.

    Windows drive / UNC paths normalize with NT semantics (and are folded to
    lower case, the filesystem being case-insensitive); everything else with
    POSIX semantics.
    """
    v = str(value or "").strip().replace("\\", "/")
    if not v:
        return ""
    if ntpath.isabs(v) or v.startswith("//"):
        v = ntpath.normpath(v).replace("\\", "/")
        return v.lower()
    return posixpath.normpath(v)


def _within(vn: str, rn: str) -> bool:
    """Whether normalized ``vn`` is ``rn`` itself or inside it (segment-wise)."""
    return vn == rn or vn.startswith(rn + "/")


def _escapes(vn: str) -> bool:
    return vn == ".." or vn.startswith("../")


# -- write side: serialization ------------------------------------------------ #

def to_workspace_relative(value, root) -> str | None:
    """Serialize a path for persistence in a workspace JSON file.

    * value inside the workspace  → the workspace-relative (slash) string;
    * value equal to the root      → ``""``;
    * value outside the workspace  → ``None`` (a global / external resource —
      the caller keeps it absolute);
    * empty / ``None`` value       → ``None``.

    Raises :class:`PathOutsideWorkspace` for a *relative* value that escapes
    the root via ``..`` — such a value must never be persisted.
    """
    if root is None or value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    rn = _norm(str(root))
    if not _is_abs(v):
        vn = _norm(v)
        if _escapes(vn):
            raise PathOutsideWorkspace(f"相对路径越出工作目录：{value}")
        return vn
    vn = _norm(v)
    if _within(vn, rn):
        return "" if vn == rn else vn[len(rn) + 1:]
    return None


# -- recovery of stale absolute paths (after a workspace move) ---------------- #

def _find_by_name(root: Path, name: str) -> list[Path]:
    """Every file named ``name`` under ``root`` (case-insensitive on Windows)."""
    if not name:
        return []
    target = name.lower() if os.name == "nt" else name

    def match(n: str) -> bool:
        return n.lower() == target if os.name == "nt" else n == target

    hits = []
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for n in filenames:
            if match(n):
                hits.append(Path(dirpath) / n)
    return hits


def _recover(vn: str, rn: str, original: str) -> str | None:
    """Best-effort re-location of a stale absolute path (the workspace moved).

    1) Original structure: the tail of the old path starting at the first known
       workspace directory (``01_input/`` … ``07_output/``, ``logs/``, ``config/``)
       is re-anchored under the current root.
    2) File name: a *unique* same-named file anywhere in the workspace.

    Returns the recovered absolute path (string) or ``None``.
    """
    parts = [p for p in vn.split("/") if p not in ("", ".")]
    for i, part in enumerate(parts):
        if part in WORKSPACE_MARKERS:  # both sides already normalized (case-folded on win)
            cand = posixpath.join(rn, *parts[i:])
            if os.path.exists(cand):
                return cand
    name = parts[-1] if parts else ""
    if name and rn:
        try:
            hits = _find_by_name(Path(rn), name)
        except OSError:
            hits = []
        if len(hits) == 1:
            return str(hits[0])
    return None


# -- read side: resolution ---------------------------------------------------- #

def resolve_path(value, root, *, strict: bool = True, label: str = "") -> Path | None:
    """Resolve a persisted path value to a real, absolute :class:`Path`.

    * relative value              → ``root`` + value (a ``..`` escape raises
      :class:`PathOutsideWorkspace`);
    * absolute, inside root       → as-is (a legacy value; callers migrate);
    * absolute, outside, exists   → as-is (a global / external resource);
    * absolute, outside, gone     → recovered (structure, then file name); when
      unrecoverable: raise :class:`PathNotFoundError` if ``strict`` (a clear,
      actionable message), else return ``None``.

    Empty values resolve to ``None``. Existence is *not* checked for values
    inside the root — callers decide how a missing file degrades.
    """
    if root is None or value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if not _is_abs(v):
        vn = _norm(v)
        if _escapes(vn):
            raise PathOutsideWorkspace(f"{label}路径越出工作目录：{value}")
        return Path(str(root)) / vn
    vn, rn = _norm(v), _norm(str(root))
    if _within(vn, rn):
        return Path(v)  # legacy absolute inside the workspace: valid as-is
    if os.path.exists(v):
        return Path(v)  # external resource (a user-selected tool / model dir, …)
    cand = _recover(vn, rn, v)
    if cand is not None:
        log.warning("路径已失效，已按原目录结构/文件名恢复：%s → %s", v, cand)
        return Path(cand)
    if strict:
        raise PathNotFoundError(
            f"{label}找不到：{v}。它不在当前工作目录（{root}）内，且无法按原目录结构或文件名恢复"
            "——工作目录可能已被移动或删除，请重新选择工作目录。"
        )
    return None


# -- legacy migration (idempotent) -------------------------------------------- #

def migrate_entries(entries, root, fields: tuple[str, ...] = ("path",)) -> int:
    """Convert legacy absolute values to the workspace-relative form, in place.

    * values inside the workspace  → the relative form;
    * stale values (absolute, pointing at the workspace's *old* location after a
      move) → recovered (original directory structure, then a unique file name)
      and stored relatively, so a moved project's JSONs heal on load / save;
    * external values (a tool binary, a model dir — found outside or not found
      inside the workspace) are left untouched (they stay absolute).

    Returns the number of converted values — 0 means the data is already in
    the new form (the conversion is idempotent).
    """
    if root is None:
        return 0
    n = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        for f in fields:
            v = e.get(f)
            if not isinstance(v, str) or not v.strip():
                continue
            try:
                rel = to_workspace_relative(v, root)
            except PathOutsideWorkspace:
                continue  # a relative value with ``..``: leave it; resolve() reports
            if rel is None and _is_abs(v):
                # Absolute and outside the current workspace: either a stale
                # in-project value (the workspace moved) or a genuine external
                # resource. Recover it — a file found inside the workspace is
                # re-serialized relatively; one that is not found stays absolute.
                try:
                    cand = resolve_path(v, root, strict=False)
                except (PathOutsideWorkspace, PathNotFoundError, OSError, TypeError):
                    cand = None
                if cand is not None:
                    try:
                        rel = to_workspace_relative(cand, root)
                    except PathOutsideWorkspace:
                        rel = None
            if rel is not None and rel != v:
                e[f] = rel
                n += 1
    return n


def rewrite_json_file(file, data) -> None:
    """Rewrite a JSON file with the (migrated) data — UTF-8 bytes, pretty-printed
    (the project's no-CRLF convention)."""
    Path(file).write_bytes(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def migrate_entries_in(file, root, kind: str, fields: tuple[str, ...] = ("path",)) -> tuple[int, object]:
    """Load ``file``, migrate its path fields in place, and rewrite it if anything
    changed. Returns ``(converted_count, parsed_data)`` — the parsed data is
    already in the migrated form, so callers keep using it directly.

    ``kind``: ``"list"`` (a list of entry dicts, e.g. ``manifest.json``) or
    ``"dict"`` (a name → entry-dict mapping, e.g. ``voice_config.json``). A
    missing or unparseable file yields ``(0, None)`` (callers keep their own
    degradation path).
    """
    f = Path(file)
    if root is None or not f.exists():
        return 0, None
    try:
        data = json.loads(f.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — a corrupt file is the caller's problem
        return 0, None
    if kind == "list":
        if not isinstance(data, list):
            return 0, data
        n = migrate_entries(data, root, fields)
    elif kind == "dict":
        if not isinstance(data, dict):
            return 0, data
        n = migrate_entries(data.values(), root, fields)
    else:
        raise ValueError(f"unknown kind: {kind}")
    if n:
        rewrite_json_file(f, data)
    return n, data
