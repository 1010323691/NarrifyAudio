"""Recoverable replacement of attempt outputs published into the workspace."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from contextlib import contextmanager, nullcontext
from pathlib import Path

log = logging.getLogger("audiobook.platform.artifact_publication")


class PublicationJournal:
    def __init__(self, root: Path, path: Path):
        self.root = root.resolve()
        self.path = path.resolve()
        if not self.path.is_relative_to(self.root):
            raise ValueError("Publication journal is outside the storage root")
        self.entries: list[tuple[Path, Path, bool, bool]] = []
        self._published_hashes: dict[int, str] = {}
        self._checkpoints: set[int] = set()
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

    def add(self, final: Path, *, guard: bool = False, checkpoint: bool = False) -> int:
        resolved_final = final.resolve()
        if not resolved_final.is_relative_to(self.root):
            raise ValueError("Publication target is outside the storage root")
        index = len(self.entries)
        backup = self.path.parent / f"publication-backup-{index}.bin"
        if backup.exists():
            raise RuntimeError(f"Publication backup already exists: {backup}")
        entries = [*self.entries, (resolved_final, backup, resolved_final.exists(), guard)]
        if checkpoint:
            self._checkpoints.add(index)
        self._write(entries)
        self.entries = entries
        return index

    def _write(self, entries: list[tuple[Path, Path, bool, bool]]) -> None:
        files = []
        for index, (final, backup, had_original, guard) in enumerate(entries):
            item = {
                "final": str(final.relative_to(self.root)),
                "backup": str(backup.relative_to(self.root)),
                "had_original": had_original,
            }
            if guard:
                item["guard"] = True
                item["published_sha256"] = self._published_hashes.get(index, "")
            if index in self._checkpoints:
                item["checkpoint"] = True
            files.append(item)
        data = {"version": 1, "files": files}
        pending = self.path.with_suffix(".tmp")
        with pending.open("w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, self.path)

    def add_many(self, finals: list[Path]) -> list[int]:
        """Durably register a batch before moving any files (one fsync).

        Unstarted entries are safe to reconcile: existing originals have no
        backup yet, and new targets do not exist yet. Guarded/checkpoint writes
        continue to use add(), since their metadata changes after publication.
        """
        entries = list(self.entries)
        indices = []
        for final in finals:
            resolved = final.resolve()
            if not resolved.is_relative_to(self.root):
                raise ValueError("Publication target is outside the storage root")
            index = len(entries)
            backup = self.path.parent / f"publication-backup-{index}.bin"
            if backup.exists():
                raise RuntimeError(f"Publication backup already exists: {backup}")
            entries.append((resolved, backup, resolved.exists(), False))
            indices.append(index)
        if indices:
            self._write(entries)
            self.entries = entries
        return indices

    def remove_many(self, finals: list[Path]) -> None:
        for index in self.add_many(finals):
            final, backup, had_original, _guard = self.entries[index]
            if had_original:
                os.replace(final, backup)

    def publish(self, index: int, source: Path) -> None:
        final, backup, had_original, guard = self.entries[index]
        final.parent.mkdir(parents=True, exist_ok=True)
        if had_original:
            os.replace(final, backup)
        os.replace(source, final)
        if guard:
            # Fingerprint the PUBLISHED bytes now: at rollback time this is
            # the only copy left, and the compare decides whether a
            # concurrent writer touched the file after this task.
            self._published_hashes[index] = hashlib.sha256(final.read_bytes()).hexdigest()
            self._write(self.entries)

    def remove(self, final: Path) -> int:
        """Move an existing file or directory aside until the DB commit is durable."""
        index = self.add(final)
        resolved_final, backup, had_original, _guard = self.entries[index]
        if had_original:
            os.replace(resolved_final, backup)
        return index

    @staticmethod
    def _remove_backup(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def rollback(self, guarded_lock=None) -> None:
        """Undo transactional publications, retaining incremental checkpoints.

        UNGUARDED entries keep the old behaviour
        (unconditional restore). GUARDED entries are restored ONLY while the
        file still holds exactly what this task published — ``guarded_lock``
        (the writer domain's cross-process lock, e.g. the 08_bgm storage
        lock) is held across that compare-and-restore so a concurrent writer
        cannot slip in between. If the lock is busy (TimeoutError) the whole
        guarded phase is skipped: the concurrent writer wins, the backups are
        dropped so a later reconcile cannot resurrect the restore, and the
        journal is still cleaned up."""
        if self._closed:
            return
        for index in reversed(range(len(self.entries))):
            final, backup, had_original, guard = self.entries[index]
            if index in self._checkpoints:
                # A completed incremental output survives task failure/restart.
                # If killed between moving the original aside and publishing,
                # restore that original rather than losing the last checkpoint.
                if not final.exists() and backup.exists():
                    os.replace(backup, final)
                else:
                    self._remove_backup(backup)
                continue
            if guard:
                continue
            if backup.exists():
                os.replace(backup, final)
            elif not had_original:
                final.unlink(missing_ok=True)
        guarded = [(index, self.entries[index]) for index in reversed(range(len(self.entries)))
                   if self.entries[index][3] and index not in self._checkpoints]
        if guarded:
            try:
                with (guarded_lock if guarded_lock is not None else nullcontext()):
                    for index, (final, backup, had_original, _guard) in guarded:
                        self._restore_guarded(index, final, backup, had_original)
            except TimeoutError:
                log.warning(
                    "Rollback skipped guarded publications (storage lock busy) — "
                    "keeping the concurrent writer's version: %s", self.path,
                )
                for _index, (_final, backup, _had, _guard) in guarded:
                    self._remove_backup(backup)
        self.path.unlink(missing_ok=True)
        self._closed = True

    def _restore_guarded(self, index: int, final: Path, backup: Path, had_original: bool) -> None:
        """Restore one guarded entry ONLY if the file still holds exactly what
        this task published; a concurrent writer (API tag propagation, manual
        edit, another worker) means the compare misses and their version is
        kept (warning logged)."""
        recorded = self._published_hashes.get(index, "")
        if not recorded:
            # Guarded entry without a fingerprint (crash between add and
            # publish): cannot verify — keeping the on-disk version is the
            # safe side.
            log.warning("Guarded publication has no recorded fingerprint — keeping on-disk version: %s", final)
            self._remove_backup(backup)
            return
        matches = final.exists() and hashlib.sha256(final.read_bytes()).hexdigest() == recorded
        if matches:
            if backup.exists():
                os.replace(backup, final)
            elif not had_original:
                final.unlink(missing_ok=True)
            else:
                log.warning("Guarded backup missing at rollback — keeping published version: %s", final)
            return
        log.warning(
            "Concurrent writer modified %s after publication — keeping their version, skipping restore",
            final,
        )
        self._remove_backup(backup)

    def finish(self) -> None:
        if self._closed:
            return
        for _final, backup, _had, _guard in self.entries:
            self._remove_backup(backup)
        self.path.unlink(missing_ok=True)
        self._closed = True

    @classmethod
    def reconcile(cls, root: Path, path: Path, *, committed: bool,
                  checkpoint_directory: Path | None = None) -> bool:
        if not path.exists():
            return False
        journal = cls(root, path)
        checkpoint_root = checkpoint_directory.resolve() if checkpoint_directory is not None else None
        if checkpoint_root is not None and not checkpoint_root.is_relative_to(journal.root):
            raise ValueError("Checkpoint directory is outside the storage root")
        data = json.loads(path.read_text("utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("files"), list):
            raise ValueError("Invalid publication journal")
        for index, item in enumerate(data["files"]):
            final = journal._under_root(str(item["final"]))
            backup = journal._under_root(str(item["backup"]))
            if not backup.is_relative_to(journal.path.parent):
                raise ValueError("Publication backup is outside the attempt directory")
            guard = item.get("guard") is True
            journal.entries.append((final, backup, item["had_original"] is True, guard))
            # Older TTS attempts did not mark their incremental outputs. The
            # worker supplies their managed audio directory during recovery.
            if item.get("checkpoint") is True or (
                checkpoint_root is not None and final.is_relative_to(checkpoint_root)
            ):
                journal._checkpoints.add(index)
            if guard:
                journal._published_hashes[index] = str(item.get("published_sha256", ""))
        if committed:
            journal.finish()
        else:
            # No lock is available on the recovery path: the guarded
            # compare-and-restore still applies (a live concurrent writer
            # wins), the missing lock only leaves a small compare→restore
            # window — acceptable here because the worker that owns the
            # journal is already dead.
            journal.rollback()
        return True


class PublicationJournalBundle:
    """One task attempt publishing files under several independently rooted stores."""

    def __init__(self, journals: list[PublicationJournal]):
        if not journals:
            raise ValueError("A publication bundle needs at least one journal")
        self.journals = list(journals)

    def prepare(self) -> None:
        prepared: list[PublicationJournal] = []
        try:
            for journal in self.journals:
                journal.prepare()
                prepared.append(journal)
        except BaseException:
            for journal in reversed(prepared):
                journal.rollback()
            raise

    def rollback(self) -> None:
        for journal in reversed(self.journals):
            journal.rollback()

    def finish(self) -> None:
        for journal in self.journals:
            journal.finish()


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
