"""Recoverable replacement of attempt outputs published into the workspace."""
from __future__ import annotations

import json
import logging
import os
import shutil
from contextlib import contextmanager
from pathlib import Path


class PublicationJournal:
    def __init__(self, root: Path, path: Path):
        self.root = root.resolve()
        self.path = path.resolve()
        if not self.path.is_relative_to(self.root):
            raise ValueError("Publication journal is outside the storage root")
        self.entries: list[tuple[Path, Path, bool]] = []
        self._closed = False

    def _under_root(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Publication path is outside the storage root")
        return candidate

    def prepare(self) -> None:
        if self.path.exists():
            raise RuntimeError(f"Publication journal already exists: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write([])

    def add(self, final: Path) -> int:
        resolved_final = final.resolve()
        if not resolved_final.is_relative_to(self.root):
            raise ValueError("Publication target is outside the storage root")
        index = len(self.entries)
        backup = self.path.parent / f"publication-backup-{index}.bin"
        if backup.exists():
            raise RuntimeError(f"Publication backup already exists: {backup}")
        entries = [*self.entries, (resolved_final, backup, resolved_final.exists())]
        self._write(entries)
        self.entries = entries
        return index

    def _write(self, entries: list[tuple[Path, Path, bool]]) -> None:
        data = {
            "version": 1,
            "files": [
                {"final": str(final.relative_to(self.root)), "backup": str(backup.relative_to(self.root)), "had_original": had_original}
                for final, backup, had_original in entries
            ],
        }
        pending = self.path.with_suffix(".tmp")
        with pending.open("w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, self.path)

    def publish(self, index: int, source: Path) -> None:
        final, backup, had_original = self.entries[index]
        final.parent.mkdir(parents=True, exist_ok=True)
        if had_original:
            os.replace(final, backup)
        os.replace(source, final)

    def remove(self, final: Path) -> int:
        """Move an existing file or directory aside until the DB commit is durable."""
        index = self.add(final)
        resolved_final, backup, had_original = self.entries[index]
        if had_original:
            os.replace(resolved_final, backup)
        return index

    @staticmethod
    def _remove_backup(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def rollback(self) -> None:
        if self._closed:
            return
        for final, backup, had_original in reversed(self.entries):
            if backup.exists():
                os.replace(backup, final)
            elif not had_original:
                final.unlink(missing_ok=True)
        self.path.unlink(missing_ok=True)
        self._closed = True

    def finish(self) -> None:
        if self._closed:
            return
        for _, backup, _ in self.entries:
            self._remove_backup(backup)
        self.path.unlink(missing_ok=True)
        self._closed = True

    @classmethod
    def reconcile(cls, root: Path, path: Path, *, committed: bool) -> bool:
        if not path.exists():
            return False
        journal = cls(root, path)
        data = json.loads(path.read_text("utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("files"), list):
            raise ValueError("Invalid publication journal")
        for item in data["files"]:
            final = journal._under_root(str(item["final"]))
            backup = journal._under_root(str(item["backup"]))
            if not backup.is_relative_to(journal.path.parent):
                raise ValueError("Publication backup is outside the attempt directory")
            journal.entries.append((final, backup, item["had_original"] is True))
        if committed:
            journal.finish()
        else:
            journal.rollback()
        return True


@contextmanager
def publication_transaction(journal: PublicationJournal, *, prepared: bool = False):
    if not prepared:
        journal.prepare()
    try:
        yield journal
    except BaseException:
        try:
            journal.rollback()
        except Exception:
            logging.getLogger(__name__).exception("Could not roll back artifact publication")
        raise
    else:
        try:
            journal.finish()
        except OSError:
            # The task is committed; leave the journal for a later cleanup.
            logging.getLogger(__name__).exception("Could not clean up artifact publication")
