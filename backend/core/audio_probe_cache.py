"""Bounded persistent ffprobe results shared by API and Worker processes."""
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from .file_lock import exclusive_file_lock
from .paths import PROJECT_ROOT
from .safe_filesystem import file_identity, is_link_or_junction

PARAMETERS = ("-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1")
MAX_ENTRIES = 100_000
MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def cache_root():
    configured = os.getenv("NARRIFY_PROBE_CACHE_DIR", "").strip()
    return (Path(configured).expanduser() if configured else PROJECT_ROOT / ".narrify" / "audio-probes").absolute()


def tool_identity(command):
    executable = shutil.which(command or "ffprobe")
    if not executable:
        return None
    try:
        path = Path(executable).resolve(strict=True)
        return (str(path), file_identity(path.stat())) if path.is_file() else None
    except (OSError, ValueError, RuntimeError):
        return None


def input_identity(path):
    try:
        path = Path(path).resolve(strict=True)
        return (str(path), file_identity(path.stat())) if path.is_file() else None
    except (OSError, ValueError, RuntimeError):
        return None


def _key(source, tool, parameters):
    return hashlib.sha256(json.dumps([1, source, tool, parameters], ensure_ascii=False).encode()).hexdigest()


def _safe_root():
    root = cache_root()
    for parent in [root, *root.parents]:
        if is_link_or_junction(parent):
            raise ValueError("探测缓存目录不可使用链接")
    root.mkdir(parents=True, exist_ok=True)
    database = root / "duration.sqlite"
    if is_link_or_junction(database):
        raise ValueError("探测缓存文件不可使用链接")
    return root


def _connect(root):
    db = sqlite3.connect(root / "duration.sqlite", timeout=0.25)
    try:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        db.execute(f"PRAGMA max_page_count={32 * 1024 * 1024 // page_size}")
        db.execute("CREATE TABLE IF NOT EXISTS probes (key TEXT PRIMARY KEY, duration REAL NOT NULL, created_at REAL NOT NULL)")
        return db
    except BaseException:
        db.close()
        raise


def _corrupt(error):
    return getattr(error, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)


def _quarantine(root):
    with exclusive_file_lock(root / "database.lock", timeout=1):
        try:
            with closing(_connect(root)) as db:
                db.execute("SELECT key FROM probes LIMIT 1").fetchone()
        except sqlite3.DatabaseError as error:
            if not _corrupt(error):
                return
            (root / "duration.sqlite").replace(root / f"duration.corrupt-{uuid.uuid4().hex}.sqlite")
            diagnostics = []
            for path in root.glob("duration.corrupt-*.sqlite"):
                if is_link_or_junction(path):
                    continue
                try:
                    uuid.UUID(path.name.removeprefix("duration.corrupt-").removesuffix(".sqlite"))
                    diagnostics.append((path.stat().st_mtime_ns, path))
                except (OSError, ValueError):
                    continue
            for _stamp, path in sorted(diagnostics, reverse=True)[2:]:
                path.unlink(missing_ok=True)


def _read(root, key):
    try:
        with closing(_connect(root)) as db:
            row = db.execute("SELECT duration FROM probes WHERE key=? AND created_at>?", (key, time.time() - MAX_AGE_SECONDS)).fetchone()
        return row[0] if row and math.isfinite(row[0]) and row[0] > 0 else None
    except sqlite3.DatabaseError as error:
        if _corrupt(error):
            _quarantine(root)
        return None


def _write(root, key, duration):
    with closing(_connect(root)) as db:
        with db:
            db.execute("INSERT OR REPLACE INTO probes VALUES (?,?,?)", (key, duration, time.time()))


def cached_duration(path, command, measure, *, timeout=120.0, parameters=None):
    """No DB connection during probe/lock wait; only stable successful measurements persist."""
    parameters = PARAMETERS if parameters is None else tuple(parameters)
    source, tool = input_identity(path), tool_identity(command)
    if source is None or tool is None:
        return measure()
    key = _key(source, tool, parameters)
    try:
        root = _safe_root()
        value = _read(root, key)
        if value is not None:
            if source == input_identity(path) and tool == tool_identity(command):
                return value, ""
            return float("nan"), "读取缓存期间音频或探测工具已变化，请重试。"
        # Fixed stripes bound lock-file count while merging identical probes
        # across processes. The database connection is closed before waiting.
        lock = exclusive_file_lock(root / f"probe-{int(key[:4], 16) % 64:02d}.lock", timeout=max(0.1, timeout))
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError, TimeoutError):
        return _measure_stable(path, command, measure, source, tool)
    measurement_started = False
    try:
        with lock:
            # Do not use an accepted old key after waiting for another process.
            if source != input_identity(path) or tool != tool_identity(command):
                return float("nan"), "等待探测期间音频或工具已变化，请重试。"
            value = _read(root, key)
            if value is not None:
                if source != input_identity(path) or tool != tool_identity(command):
                    return float("nan"), "读取缓存期间音频或探测工具已变化，请重试。"
                return value, ""
            measurement_started = True
            result = _measure_stable(path, command, measure, source, tool)
            if not result[1] and math.isfinite(result[0]) and result[0] > 0:
                try:
                    _write(root, key, result[0])
                except (OSError, ValueError, sqlite3.DatabaseError):
                    pass  # cache is derived; persistence failure cannot fail a valid probe
            return result
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError, TimeoutError):
        if measurement_started:
            raise
        return _measure_stable(path, command, measure, source, tool)


def _measure_stable(path, command, measure, source, tool):
    result = measure()
    if source != input_identity(path) or tool != tool_identity(command):
        return float("nan"), "探测期间音频或工具已变化，请重试。"
    return result


def prune_cache():
    """Bound maintenance work; invoked by the shared maintenance coordinator."""
    try:
        root = _safe_root()
        with exclusive_file_lock(root / "database.lock", timeout=0.25):
            with closing(_connect(root)) as db:
                with db:
                    db.execute("DELETE FROM probes WHERE key IN (SELECT key FROM probes WHERE created_at<=? ORDER BY created_at LIMIT 1000)",
                               (time.time() - MAX_AGE_SECONDS,))
                    excess = max(0, db.execute("SELECT count(*) FROM probes").fetchone()[0] - MAX_ENTRIES)
                    if excess:
                        db.execute("DELETE FROM probes WHERE key IN (SELECT key FROM probes ORDER BY created_at LIMIT ?)", (min(1000, excess),))
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError, TimeoutError):
        pass
