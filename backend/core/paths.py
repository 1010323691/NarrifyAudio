"""Unified directory layout for the AudiobookStudio backend.

One root — the user's chosen workspace (``paths.working_dir`` in the root
``app.json``) holds everything for the project::

    00_temp/           # scratch files for engine subprocesses
    01_input/          # raw uploads + formatted text
    02_split_text/     # per-chapter split files (one per chapter; or a single 全书 file)
    03_parsed_json/    # LLM-parsed script JSON
    04_voice_profiles/ # character voice configs / preview WAVs
    05_audio_chunk/    # per-segment batch audio (+ manifest)
    06_audio_merge/    # merged audiobook file
    07_output/         # final episode files
    08_bgm/            # background-music analysis cache + assignments + final mixes
    logs/              # the project's app.log
    config/            # the project's config/app.json

The project root itself holds only ``app.json`` — the generic default config
template plus the bootstrap pointer to the active workspace (see
``core/config.py``). Until a workspace is set there is *no* project location at
all: the pipeline is locked, the config falls back to the (read-only) root
template, and logging is console-only. Nothing here ever deletes anything.
"""
from __future__ import annotations

import threading
from pathlib import Path

# backend/core/paths.py  ->  parents[0]=core  parents[1]=backend  parents[2]=<project>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The nine directories (eight artifacts + scratch) that follow the user's
# workspace: (Layout attribute, on-disk directory name), in pipeline order.
WORKSPACE_DIRS = (
    ("temp", "00_temp"),
    ("input", "01_input"),
    ("split_text", "02_split_text"),
    ("parsed_json", "03_parsed_json"),
    ("voice_profiles", "04_voice_profiles"),
    ("audio_chunk", "05_audio_chunk"),
    ("audio_merge", "06_audio_merge"),
    ("output", "07_output"),
    ("bgm", "08_bgm"),
)

# The global music library (背景音乐系统): a top-level project-root directory that
# lives OUTSIDE any workspace — music files and their shared tag registry are
# project-level resources shared by every workspace. Deliberately NOT a
# workspace dir and NOT configurable: config is per-workspace, while the
# library is global (a per-project setting that pins a global path is
# self-contradictory). Fixed constant, code-managed (like ``TEMPLATE_FILE``).
# Engines/tests access it via module attribute at call time
# (``core_paths.MUSIC_LIBRARY_DIR``) so monkeypatching stays effective.
MUSIC_LIBRARY_DIR = PROJECT_ROOT / "music_library"

# On-disk directory names only (for creation / validation).
WORKSPACE_DIR_NAMES = tuple(name for _, name in WORKSPACE_DIRS)


class Layout:
    """Resolved, on-disk directory tree: a single workspace root holding everything.

    ``workspace is None`` (no workspace set) -> every path property is ``None``
    and :meth:`ensure` / :meth:`dirs` are inert, so nothing can be planted in the
    project directory before a workspace exists.
    """

    def __init__(self, workspace: Path | None):
        self.workspace = workspace
        if workspace is None:
            # Unset: no artifact / log / config locations exist.
            self.temp = None
            self.input = None
            self.split_text = None
            self.parsed_json = None
            self.voice_profiles = None
            self.audio_chunk = None
            self.audio_merge = None
            self.output = None
            self.bgm = None
            self.logs = None
            self.config = None
            return
        # Everything lives under the workspace:
        self.temp = workspace / "00_temp"
        self.input = workspace / "01_input"
        self.split_text = workspace / "02_split_text"
        self.parsed_json = workspace / "03_parsed_json"
        self.voice_profiles = workspace / "04_voice_profiles"
        self.audio_chunk = workspace / "05_audio_chunk"
        self.audio_merge = workspace / "06_audio_merge"
        self.output = workspace / "07_output"
        self.bgm = workspace / "08_bgm"
        self.logs = workspace / "logs"
        self.config = workspace / "config"

    # -- directory creation (mkdir only — nothing is ever deleted) ------------
    def ensure(self) -> "Layout":
        """Create the nine artifact dirs + logs + config under the workspace."""
        if self.workspace is None:
            return self
        for name in (*WORKSPACE_DIR_NAMES, "logs", "config"):
            (self.workspace / name).mkdir(parents=True, exist_ok=True)
        return self

    def dirs(self) -> dict[str, str]:
        """``{on-disk dir name: absolute path}`` for the artifact dirs (``{}`` when unset)."""
        if self.workspace is None:
            return {}
        return {name: str(getattr(self, attr)) for attr, name in WORKSPACE_DIRS}


def is_workspace_set() -> bool:
    """True once the user has picked a workspace folder (root ``app.json`` pointer)."""
    from .config import _workspace_path  # local import to avoid a cycle

    return _workspace_path() is not None


# -- get_layout memoization ------------------------------------------------------
# ``get_layout()`` sits on every hot path (the 待合成 poll resolves one layout per package
# AND per completed segment), and each uncached call re-reads the root ``app.json`` pointer
# plus re-runs the 11 mkdir probes. The pointer only changes when the root file itself is
# rewritten (``set_workspace_pointer`` / ``clear_workspace`` both rewrite it), so the file's
# ``(mtime_ns, size)`` is a faithful invalidation key: steady state costs ONE stat per call.
# The ``ensured`` flag remembers whether the cached layout's skeleton was planted — a stale-
# pointer entry (the folder was gone when cached) plants it exactly once if the folder
# comes back, instead of re-running the probes on every hit.
_MISSING_KEY = object()
_layout_lock = threading.Lock()
_layout_cache: Layout | None = None
_layout_cache_key = _MISSING_KEY  # a real key is (st_mtime_ns, st_size)
_layout_ensured = False


def _root_pointer_key():
    """The cache key: the root ``app.json``'s ``(mtime_ns, size)``; a sentinel while the
    file does not exist yet (a fresh sandbox before the first pointer write)."""
    from .config import TEMPLATE_FILE  # local import: config imports paths at top level

    try:
        st = TEMPLATE_FILE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return _MISSING_KEY


def get_layout() -> Layout:
    """Return the Layout for the configured workspace (memoized on the root file).

    The workspace root (artifact dirs + logs + config) is created idempotently only
    when a working directory is actually set AND the folder still exists — an unset
    workspace yields an inert ``Layout(None)`` and must not plant any folders in the
    project directory. A pointer to a folder that no longer exists (the workspace was
    moved / deleted) must NOT resurrect an empty skeleton at the old location either:
    the dashboard then reports the missing folder (``exists: false``) and the write
    endpoints refuse with a clear 409 until the user re-selects the (moved) folder.
    Relative ``working_dir`` values resolve against ``PROJECT_ROOT``.

    Memoization: the pointer is re-read only when the root file's ``mtime_ns``/``size``
    change (every real pointer set/clear rewrites it); otherwise the cached ``Layout``
    is returned as-is — it is never mutated after creation. A cached stale-pointer entry
    whose folder reappears runs ``ensure()`` exactly once (same skeleton-planting as an
    uncached call), after which the mkdir probes stop.
    """
    global _layout_cache, _layout_cache_key, _layout_ensured
    key = _root_pointer_key()
    with _layout_lock:
        layout = _layout_cache
        if layout is not None and _layout_cache_key == key:
            if (layout.workspace is not None and not _layout_ensured
                    and layout.workspace.exists()):
                # Stale-pointer entry whose folder came back: plant the skeleton once.
                layout.ensure()
                _layout_ensured = True
            return layout
        from .config import _workspace_path  # local import to avoid a cycle

        workspace = _workspace_path()
        if workspace is None:
            layout, ensured = Layout(None), True
        else:
            layout = Layout(workspace)
            if workspace.exists():
                # Live folder (newly created by the workspace endpoint, or an existing
                # project) -> create any missing subdirs.
                layout.ensure()
                ensured = True
            else:
                # A missing folder is the stale-pointer case: stay inert so nothing is
                # planted at the old location.
                ensured = False
        _layout_cache, _layout_cache_key, _layout_ensured = layout, key, ensured
    return layout


def reset_layout_cache() -> None:
    """Drop the memoized layout; the next ``get_layout()`` re-reads the pointer.

    A test / debug seam — a real pointer change already invalidates via the root file's
    ``mtime_ns``/``size``. Mirrors ``core.config.reset_config_cache``.
    """
    global _layout_cache, _layout_cache_key, _layout_ensured
    with _layout_lock:
        _layout_cache, _layout_cache_key, _layout_ensured = None, _MISSING_KEY, False


# Sentinel: a 角色配音 request to operate over *every* parsed JSON at once
# (aggregate the whole book's characters). Distinct from any real file name
# (which ends in ``.json``); the frontend uses the same literal.
ALL_PARSED_JSON = "__all__"


def resolve_parsed_json(script: str | None = None) -> Path:
    """The parsed script JSON a downstream stage (角色配音 / 音频合成) should read.

    ``script`` names a file inside ``03_parsed_json/`` and it is read as-is: both a base
    ``<stem>.json`` and an explicit ``<stem>_checked.json`` name resolve directly (an
    already-suffixed name is never re-suffixed). When omitted, fall back to the most
    recently modified *base* ``*.json`` — orphan ``_checked`` files left over from the
    retired check stages are never read (inert leftovers: visible and downloadable, but
    nothing in the pipeline consumes them) — and finally to the legacy single-file name
    ``annotated_script.json`` (so an older project that still has it keeps working). With
    no workspace set, returns a non-existent relative placeholder so read-only callers
    degrade cleanly. Callers validate existence and raise a clear error when nothing
    resolves.
    """
    layout = get_layout()
    d = layout.parsed_json
    if d is None:  # no workspace: inert (read-only callers see "no script")
        return Path("annotated_script.json")
    if script:
        return d / script
    if d.exists():
        # The most recent base file; _checked orphans are excluded (see docstring).
        base = [p for p in d.glob("*.json")
                if p.is_file() and not p.name.endswith("_checked.json")]
        if base:
            return max(base, key=lambda p: p.stat().st_mtime)
    return d / "annotated_script.json"


def resolve_parsed_json_all() -> list[Path]:
    """Every parsed script a 角色配音 "all files" request should read (whole-book aggregate).

    Returns the base ``*.json`` files in ``03_parsed_json/`` (orphan ``_checked`` files are
    excluded — inert leftovers, never read) in reading order. Order is by ``(mtime, name)``:
    mtime ≈ the order the volumes were generated (≈ reading order), which is robust to
    Chinese-numeral stems that ``name`` alone would scramble, with name as a deterministic
    tiebreaker — the same mtime semantic the single-file "most recent" fallback uses. With
    no workspace set or an empty directory it returns ``[]`` so callers degrade cleanly.
    """
    layout = get_layout()
    d = layout.parsed_json
    if d is None or not d.exists():
        return []
    base = [p for p in d.glob("*.json") if p.is_file() and not p.name.endswith("_checked.json")]
    base.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return base
