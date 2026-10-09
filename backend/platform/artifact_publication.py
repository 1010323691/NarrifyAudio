"""Recoverable replacement of attempt outputs published into the workspace."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from ..core.workspace_epochs import managed_mutation

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
        self._append_failed = False
        self.metrics = {"journal_seconds": 0.0, "journal_bytes": 0,
                        "journal_flushes": 0, "move_seconds": 0.0, "published_audio": 0}

    def _under_root(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("Publication path is outside the storage root")
        return candidate

    def prepare(self) -> None:
        if self.path.exists():
            raise RuntimeError(f"Publication journal already exists: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("x", encoding="utf-8") as stream:
            stream.write('{"version":2}\n')
            stream.flush()
            os.fsync(stream.fileno())

    def _append(self, record: dict) -> None:
        # A complete newline-terminated record is the recovery boundary. Never
        # move outputs until this append has been flushed durably.
        if self._closed or self._append_failed:
            raise RuntimeError("Publication journal is closed or has a failed append")
        started = time.monotonic()
        try:
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self.metrics["journal_bytes"] += len(encoded.encode("utf-8"))
            self.metrics["journal_flushes"] += 1
        except BaseException:
            self._append_failed = True
            raise
        finally:
            self.metrics["journal_seconds"] += time.monotonic() - started

    def add(self, final: Path, *, guard: bool = False, checkpoint: bool = False) -> int:
        return self.add_many([final], guards=[guard], checkpoints=[checkpoint])[0]

    def _write(self, entries: list[tuple[Path, Path, bool, bool]]) -> None:
        """Append only newly registered files; history is never serialized again."""
        start = len(self.entries)
        files = []
        for index, (final, backup, had_original, guard) in enumerate(entries, start):
            item = {"final": str(final.relative_to(self.root)),
                    "backup": backup.name, "had_original": had_original}
            if guard:
                item["guard"] = True
            if index in self._checkpoints:
                item["checkpoint"] = True
            files.append(item)
        self._append({"files": files})

    def add_many(self, finals: list[Path], *, guards: list[bool] | None = None,
                 checkpoints: list[bool] | None = None) -> list[int]:
        """Register a batch, including checkpoint outputs, with one fsync."""
        guards = [False] * len(finals) if guards is None else guards
        checkpoints = [False] * len(finals) if checkpoints is None else checkpoints
        if len(guards) != len(finals) or len(checkpoints) != len(finals):
            raise ValueError("Publication flags must match the file count")
        entries = []
        for offset, final in enumerate(finals):
            resolved = final.resolve()
            if not resolved.is_relative_to(self.root):
                raise ValueError("Publication target is outside the storage root")
            index = len(self.entries) + offset
            backup = self.path.parent / f"publication-backup-{index}.bin"
            if backup.exists():
                raise RuntimeError(f"Publication backup already exists: {backup}")
            entries.append((resolved, backup, resolved.exists(), guards[offset]))
        indices = list(range(len(self.entries), len(self.entries) + len(entries)))
        marked = {index for index, checkpoint in zip(indices, checkpoints) if checkpoint}
        self._checkpoints.update(marked)
        try:
            if entries:
                self._write(entries)
        except BaseException:
            self._checkpoints.difference_update(marked)
            raise
        self.entries.extend(entries)
        return indices

    def remove_many(self, finals: list[Path]) -> None:
        for index in self.add_many(finals):
            final, backup, had_original, _guard = self.entries[index]
            if had_original:
                with managed_mutation(final):
                    os.replace(final, backup)

    def publish(self, index: int, source: Path) -> None:
        started = time.monotonic()
        final, backup, had_original, guard = self.entries[index]
        final.parent.mkdir(parents=True, exist_ok=True)
        with managed_mutation(final):
            if had_original:
                os.replace(final, backup)
            os.replace(source, final)
        self.metrics["move_seconds"] += time.monotonic() - started
        if index in self._checkpoints and final.suffix.lower() == ".mp3":
            self.metrics["published_audio"] += 1
        if guard:
            # Fingerprint the PUBLISHED bytes now: at rollback time this is
            # the only copy left, and the compare decides whether a
            # concurrent writer touched the file after this task.
            self._published_hashes[index] = hashlib.sha256(final.read_bytes()).hexdigest()
            self._append({"index": index, "sha256": self._published_hashes[index]})

    def remove(self, final: Path) -> int:
        """Move an existing file or directory aside until the DB commit is durable."""
        index = self.add(final)
        resolved_final, backup, had_original, _guard = self.entries[index]
        if had_original:
            with managed_mutation(resolved_final):
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
                    with managed_mutation(final):
                        os.replace(backup, final)
                else:
                    self._remove_backup(backup)
                continue
            if guard:
                continue
            if backup.exists():
                with managed_mutation(final):
                    os.replace(backup, final)
            elif not had_original:
                with managed_mutation(final):
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
                with managed_mutation(final):
                    os.replace(backup, final)
            elif not had_original:
                with managed_mutation(final):
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
        with path.open("rb") as stream:
            first = stream.readline()
            try:
                header = json.loads(first)
            except ValueError:
                stream.seek(0)
                header = json.load(stream)
            if not isinstance(header, dict):
                raise ValueError("Invalid publication journal")
            if header.get("version") == 1:
                # Existing attempts retain their original snapshot format.
                stream.seek(0)
                data = json.load(stream)
                if not isinstance(data.get("files"), list):
                    raise ValueError("Invalid publication journal")
                records = [{"files": data["files"]}]
                legacy = True
            elif header == {"version": 2} and first.endswith(b"\n"):
                records = []
                legacy = False
                for line in stream:
                    if not line.endswith(b"\n"):
                        break  # crash while appending the final record
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("Invalid publication journal record")
                    records.append(record)
            else:
                raise ValueError("Invalid publication journal")
        for record in records:
            if "files" not in record:
                index = record.get("index")
                digest = record.get("sha256")
                if (type(index) is not int or not 0 <= index < len(journal.entries)
                        or not journal.entries[index][3] or not isinstance(digest, str)
                        or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
                    raise ValueError("Invalid publication fingerprint record")
                journal._published_hashes[index] = digest
                continue
            if not isinstance(record["files"], list):
                raise ValueError("Invalid publication journal files")
            for item in record["files"]:
                index = len(journal.entries)
                final = journal._under_root(str(item["final"]))
                backup = (journal._under_root(str(item["backup"])) if legacy else
                          (journal.path.parent / str(item["backup"])).resolve())
                if not backup.is_relative_to(journal.path.parent) or backup == journal.path:
                    raise ValueError("Publication backup is outside the attempt directory")
                if not legacy and backup != journal.path.parent / f"publication-backup-{index}.bin":
                    raise ValueError("Invalid publication backup identity")
                guard = item.get("guard") is True
                journal.entries.append((final, backup, item["had_original"] is True, guard))
                if item.get("checkpoint") is True or (
                    checkpoint_root is not None and final.is_relative_to(checkpoint_root)
                ):
                    journal._checkpoints.add(index)
                if guard and legacy:
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
