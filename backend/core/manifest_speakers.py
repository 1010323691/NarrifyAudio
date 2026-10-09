"""Persistent speaker-to-manifest lookup with epoch-gated refresh.

The index is rebuilt from a directory scan only when the managed-write epoch
of the audio package module changes (deterministic: every managed manifest
write bumps it) or when the bounded external-edit fallback expires. Ordinary
lookups between scans never touch the directory.
"""
from contextlib import closing
import json
import time
from pathlib import Path
import sqlite3
import os
import uuid

from .file_lock import exclusive_file_lock
from .safe_filesystem import file_identity
from .workspace_epochs import MODULES, versions, workspace_for

# External edits bypass managed epochs; this bounded fallback bounds how long
# the index may lag them. It complements (never replaces) the epoch trigger.
EXTERNAL_FALLBACK_SECONDS = 300


def _connect_index(index):
    schema = """
        CREATE TABLE IF NOT EXISTS manifests (path TEXT PRIMARY KEY, identity TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS speakers (speaker TEXT NOT NULL, path TEXT NOT NULL,
            PRIMARY KEY(speaker, path));
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """
    db = sqlite3.connect(index)
    try:
        db.executescript(schema)
    except sqlite3.DatabaseError as error:
        db.close()
        if getattr(error, "sqlite_errorcode", None) not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            raise
        # Preserve a damaged derived index for diagnosis; rebuild from manifests.
        os.replace(index, index.with_name(".speaker-index.corrupt-" + uuid.uuid4().hex + ".sqlite"))
        db = sqlite3.connect(index)
        db.executescript(schema)
    return db


def _module_epoch(root: Path, module: str) -> str | None:
    """The module's managed-write epoch, or None when it cannot be trusted."""
    workspace = workspace_for(root)
    if workspace is None or module not in MODULES:
        return None
    root_workspace, _ = workspace
    selected, _busy = versions(root_workspace)
    if not isinstance(selected, tuple) or len(selected) != len(MODULES):
        return None  # unreadable epoch record: force the strict scan
    return str(selected[MODULES.index(module)])


def _index_meta(db: sqlite3.Connection) -> tuple[str | None, float]:
    rows = dict(db.execute("SELECT key, value FROM meta"))
    try:
        checked_at = float(rows.get("checked_at") or 0)
    except ValueError:
        checked_at = 0.0
    return rows.get("epoch"), checked_at


def candidate_manifests(root, names):
    root = Path(root)
    if not root.is_dir():
        return []
    index = root / ".speaker-index.sqlite"
    if index.is_symlink() or root.is_symlink():
        raise OSError("Manifest index must not follow symlinks")
    with exclusive_file_lock(root / ".speaker-index.lock"), closing(_connect_index(index)) as db:
        names = tuple(sorted(set(str(name) for name in names if str(name).strip())))
        if not names:
            return []
        placeholders = ",".join("?" for _ in names)
        lookup = (
            f"SELECT DISTINCT path FROM speakers WHERE speaker IN ({placeholders}) ORDER BY path"
        )
        epoch = _module_epoch(root, root.name)
        checked_epoch, checked_at = _index_meta(db)
        fresh = (
            epoch is not None
            and checked_epoch == epoch
            and time.time() - checked_at < EXTERNAL_FALLBACK_SECONDS
        )
        if fresh:
            return [root / row[0] for row in db.execute(lookup, names)]
        previous = dict(db.execute("SELECT path, identity FROM manifests"))
        found = set()
        unreadable = []  # kept as candidates, never indexed: retried next call
        for path in root.glob("*/manifest.json"):
            if path.is_symlink() or path.parent.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            found.add(relative)
            try:
                identity = json.dumps(file_identity(path.stat()))
                if previous.get(relative) == identity:
                    continue
                data = json.loads(path.read_text("utf-8"))
                if json.dumps(file_identity(path.stat())) != identity:
                    # An in-flight writer: do not index a half-seen file.
                    unreadable.append(path)
                    continue
            except FileNotFoundError:
                # Removed between glob and read: the chapter is really gone.
                found.discard(relative)
                continue
            except (OSError, ValueError):
                # Unreadable now does not mean irrelevant: keep it as a
                # candidate, but one bad file must not block indexing the rest.
                unreadable.append(path)
                continue
            db.execute("DELETE FROM speakers WHERE path=?", (relative,))
            roles = {str(entry.get("speaker") or "").strip() for entry in data if isinstance(entry, dict)} if isinstance(data, list) else set()
            db.executemany("INSERT INTO speakers VALUES (?, ?)", [(name, relative) for name in roles if name])
            db.execute("INSERT OR REPLACE INTO manifests VALUES (?, ?)", (relative, identity))
        for missing in previous.keys() - found:
            db.execute("DELETE FROM manifests WHERE path=?", (missing,))
            db.execute("DELETE FROM speakers WHERE path=?", (missing,))
        if not unreadable:
            # Only a complete scan may arm the fresh fast path.
            db.execute("DELETE FROM meta")
            db.execute("INSERT INTO meta VALUES ('epoch', ?)", ("" if epoch is None else epoch,))
            db.execute("INSERT INTO meta VALUES ('checked_at', ?)", (str(time.time()),))
        db.commit()
        indexed = [root / row[0] for row in db.execute(lookup, names) if row[0] in found]
        return sorted({*indexed, *unreadable})
