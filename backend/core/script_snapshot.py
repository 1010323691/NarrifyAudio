"""Immutable, indexed script preparation shared by independent role tasks."""
from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .file_lock import exclusive_file_lock
from .safe_filesystem import file_identity
from .task_control import TaskCancelled
from .safe_filesystem import safe_regular_path

_reference = ContextVar("narrify_script_reference", default=None)

# Bounded retention for the immutable snapshot cache. The input-change check
# itself never trusts elapsed time: an external edit must invalidate the task
# immediately, so the version is re-derived from the source identities on
# every open (one stat per file, no content reads).
PRUNE_KEEP = 4
PRUNE_MAX_AGE_SECONDS = 7 * 86400
PRUNE_LIMIT = 10
PRUNE_INTERVAL_SECONDS = 3600
_SNAPSHOT_NAME_RE = re.compile(r"^[0-9a-f]{64}\.sqlite$")


def bind_reference(reference):
    return _reference.set(reference)


def reset_reference(token):
    _reference.reset(token)


def bound_reference():
    return _reference.get()


def capture_reference(paths, workspace, script):
    paths = tuple(paths)
    version, _sources = source_version(paths)
    return {"version": version, "paths": [path.resolve().relative_to(workspace.resolve()).as_posix() for path in paths], "script": script}


def reference_paths(reference, workspace):
    names = reference.get("paths")
    if not isinstance(names, list) or not names or any(
        not isinstance(name, str) or not name.startswith("03_parsed_json/")
        or len(name.split("/")) != 2 or not name.endswith(".json") for name in names
    ):
        raise ValueError("剧本快照引用无效")
    return [safe_regular_path(workspace, name) for name in names]


def source_version(paths):
    sources = []
    for path in paths:
        try:
            identity = list(file_identity(path.stat()))
        except FileNotFoundError:
            identity = None
        sources.append([str(path.resolve()), identity])
    digest = hashlib.sha256(json.dumps([1, sources], ensure_ascii=False).encode()).hexdigest()
    return digest, sources


def _connect(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def prune_snapshots(directory, *, keep=PRUNE_KEEP, max_age=PRUNE_MAX_AGE_SECONDS,
                    limit=PRUNE_LIMIT, interval=PRUNE_INTERVAL_SECONDS) -> int:
    """Bounded retention: at most once per interval, at most ``limit`` removals.

    Snapshots are immutable and rebuildable, so only stale versions beyond the
    recent window are removable; crash leftovers (staged builds) follow the
    same age gate. Failures never propagate to role preparation."""
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    now = time.time()
    stamp = directory / ".prune-at"
    try:
        if float(stamp.read_text()) > now - interval:
            return 0
    except (OSError, ValueError):
        pass
    deleted = 0
    try:
        cutoff = now - max_age
        rows = []
        for path in directory.glob("*.sqlite"):
            if not _SNAPSHOT_NAME_RE.match(path.name):
                continue
            try:
                rows.append((path, path.stat().st_mtime))
            except OSError:
                continue
        rows.sort(key=lambda item: item[1], reverse=True)
        for index, (path, mtime) in enumerate(rows):
            if deleted >= limit or mtime > cutoff or index < keep:
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError:
                continue
        for path in directory.glob(".*.sqlite"):
            if deleted >= limit:
                break
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    deleted += 1
            except OSError:
                continue
    finally:
        try:
            stamp.write_text(str(now))
        except OSError:
            pass
    return deleted


def open_snapshot(paths, directory, *, check=lambda: None, log=lambda *_: None, skip_invalid=False, speakers=None, cancelled=lambda: False, expected_version=None):
    check()  # never park a paused role while holding the shared build lock
    paths = tuple(Path(path) for path in paths)
    if not paths:
        raise RuntimeError("未找到脚本 JSON（03_parsed_json/）——请先在「文本解析」生成脚本。")
    directory = Path(directory)
    # The version must always be re-derived: a submitted task may only run
    # against the exact input it captured, and external edits carry no epoch
    # signal — only the source identities prove the input is unchanged.
    version, sources = source_version(paths)
    if expected_version is not None and version != expected_version:
        raise RuntimeError("剧本输入已变更，请刷新后重新提交任务。")
    try:
        prune_snapshots(directory)
    except Exception:
        pass  # retention must never break role preparation
    if directory.is_symlink():
        raise ValueError("Script snapshot cache must not follow directory symlinks")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (version + ".sqlite")
    with exclusive_file_lock(directory / (version + ".lock")):
        valid = False
        if path.is_file():
            try:
                with closing(_connect(path)) as db:
                    valid = db.execute("SELECT value FROM metadata WHERE key='version'").fetchone() == (version,)
            except sqlite3.DatabaseError:
                pass
        if not valid:
            pending = directory / ("." + version + "." + uuid.uuid4().hex + ".sqlite")
            try:
                with closing(sqlite3.connect(pending)) as db:
                    db.executescript("""
                        CREATE TABLE entries (position INTEGER PRIMARY KEY, chapter TEXT NOT NULL,
                            speaker TEXT NOT NULL, role_position INTEGER NOT NULL, text TEXT NOT NULL, data TEXT NOT NULL);
                        CREATE INDEX role_entries ON entries(speaker, role_position);
                        CREATE TABLE roles (name TEXT PRIMARY KEY, first_position INTEGER NOT NULL, count INTEGER NOT NULL);
                        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    """)
                    counts, first = {}, {}
                    total, loaded = 0, 0
                    for source in paths:
                        if cancelled():
                            raise TaskCancelled()
                        try:
                            data = json.loads(source.read_text("utf-8"))
                            if not isinstance(data, list) or not data:
                                raise ValueError("脚本为空")
                        except (OSError, ValueError) as error:
                            if not skip_invalid:
                                raise RuntimeError(f"{source.name} 无法解析：{error}") from error
                            log(f"{source.name} 无法解析，已跳过。", "WARNING")
                            continue
                        rows = []
                        for entry in data:
                            speaker = (entry.get("speaker") or entry.get("type") or "").strip()
                            text = (entry.get("text") or "").strip()
                            rank = counts.get(speaker, 0)
                            if speaker:
                                counts[speaker] = rank + 1
                                first.setdefault(speaker, total)
                            rows.append((total, source.name, speaker, rank, text, json.dumps(entry, ensure_ascii=False)))
                            total += 1
                            if len(rows) >= 100:
                                if cancelled():
                                    raise TaskCancelled()
                                db.executemany("INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?)", rows)
                                rows.clear()
                        db.executemany("INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?)", rows)
                        loaded += 1
                        del data, rows
                    if not total:
                        raise RuntimeError("所有脚本 JSON 均为空——请先生成脚本。")
                    if source_version(paths)[0] != version:
                        raise RuntimeError("脚本在准备期间发生变化，请重新提交任务。")
                    db.executemany("INSERT INTO roles VALUES (?, ?, ?)", [(name, first[name], count) for name, count in counts.items()])
                    db.executemany("INSERT INTO metadata VALUES (?, ?)", [("version", version), ("sources", json.dumps(sources)), ("total", str(total))])
                    db.commit()
                os.replace(pending, path)
                log(f"已建立共享剧本快照：{loaded} 章、{total} 条、{len(counts)} 个角色。")
            finally:
                pending.unlink(missing_ok=True)
    check()
    return ScriptSnapshot(path, speakers=speakers)


class ScriptSnapshot(Sequence):
    def __init__(self, path, *, speakers=None):
        self.path = Path(path)
        with closing(_connect(self.path)) as db:
            metadata = dict(db.execute("SELECT key, value FROM metadata WHERE key IN ('version', 'total')"))
            if speakers:
                names = tuple(dict.fromkeys(speakers))
                placeholders = ",".join("?" for _ in names)
                self.counts = dict(db.execute(f"SELECT name, count FROM roles WHERE name IN ({placeholders}) ORDER BY first_position", names))
            else:
                self.counts = dict(db.execute("SELECT name, count FROM roles ORDER BY first_position"))
        self.version = metadata["version"]
        self.total = int(metadata["total"])
        self.samples = RoleSamples(self)

    def __len__(self):
        return self.total

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self.total)
            if step != 1:
                return [self[position] for position in range(start, stop, step)]
            with closing(_connect(self.path)) as db:
                return [json.loads(row[0]) for row in db.execute("SELECT data FROM entries WHERE position>=? AND position<? ORDER BY position", (start, stop))]
        if index < 0:
            index += self.total
        if index < 0 or index >= self.total:
            raise IndexError(index)
        with closing(_connect(self.path)) as db:
            return json.loads(db.execute("SELECT data FROM entries WHERE position=?", (index,)).fetchone()[0])


class RoleSamples(Mapping):
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def __iter__(self):
        return iter(self.snapshot.counts)

    def __len__(self):
        return len(self.snapshot.counts)

    def __getitem__(self, name):
        if name not in self.snapshot.counts:
            raise KeyError(name)
        return RolePairs(self.snapshot, name)


class RolePairs(Sequence):
    def __init__(self, snapshot, name):
        self.snapshot, self.name = snapshot, name

    def __len__(self):
        return self.snapshot.counts[self.name]

    def __getitem__(self, rank):
        if isinstance(rank, slice):
            return [self[index] for index in range(*rank.indices(len(self)))]
        if rank < 0:
            rank += len(self)
        if rank < 0 or rank >= len(self):
            raise IndexError(rank)
        with closing(_connect(self.snapshot.path)) as db:
            return db.execute("SELECT position, text FROM entries WHERE speaker=? AND role_position=?", (self.name, rank)).fetchone()

    def __iter__(self):
        with closing(_connect(self.snapshot.path)) as db:
            yield from db.execute("SELECT position, text FROM entries WHERE speaker=? ORDER BY role_position", (self.name,))

    def bands(self, per):
        count = len(self)
        if count <= 3 * per:
            return [self[rank][0] for rank in range(count)], [], []
        region = count - 2 * per
        middle = list(range(per, count - per)) if region <= per else [per + round(index * (region - 1) / (per - 1)) for index in range(per)]
        return ([self[rank][0] for rank in range(per)], [self[rank][0] for rank in middle],
                [self[rank][0] for rank in range(count - per, count)])

    def texts(self):
        return RoleTexts(self)


class RoleTexts:
    def __init__(self, pairs):
        self.pairs = pairs

    def __iter__(self):
        for _, text in self.pairs:
            yield text
