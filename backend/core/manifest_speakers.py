"""Persistent speaker-to-manifest lookup with file-identity invalidation."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import os
import uuid

from .file_lock import exclusive_file_lock
from .safe_filesystem import file_identity


def _connect_index(index):
    schema = """
        CREATE TABLE IF NOT EXISTS manifests (path TEXT PRIMARY KEY, identity TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS speakers (speaker TEXT NOT NULL, path TEXT NOT NULL,
            PRIMARY KEY(speaker, path));
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


def candidate_manifests(root, names):
    root = Path(root)
    if not root.is_dir():
        return []
    index = root / ".speaker-index.sqlite"
    if index.is_symlink() or root.is_symlink():
        raise OSError("Manifest index must not follow symlinks")
    with exclusive_file_lock(root / ".speaker-index.lock"), closing(_connect_index(index)) as db:
        previous = dict(db.execute("SELECT path, identity FROM manifests"))
        found = set()
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
                    # An in-flight writer prevents a complete index. Fall back to
                    # the normal manifest checks, including unrelated candidates.
                    return list(root.glob("*/manifest.json"))
            except (OSError, ValueError):
                db.execute("DELETE FROM manifests WHERE path=?", (relative,))
                db.execute("DELETE FROM speakers WHERE path=?", (relative,))
                continue
            db.execute("DELETE FROM speakers WHERE path=?", (relative,))
            roles = {str(entry.get("speaker") or "").strip() for entry in data if isinstance(entry, dict)} if isinstance(data, list) else set()
            db.executemany("INSERT INTO speakers VALUES (?, ?)", [(name, relative) for name in roles if name])
            db.execute("INSERT OR REPLACE INTO manifests VALUES (?, ?)", (relative, identity))
        for missing in previous.keys() - found:
            db.execute("DELETE FROM manifests WHERE path=?", (missing,))
            db.execute("DELETE FROM speakers WHERE path=?", (missing,))
        db.commit()
        names = tuple(sorted(set(names)))
        if not names:
            return []
        placeholders = ",".join("?" for _ in names)
        return [root / row[0] for row in db.execute(f"SELECT DISTINCT path FROM speakers WHERE speaker IN ({placeholders}) ORDER BY path", names)
                if row[0] in found]
