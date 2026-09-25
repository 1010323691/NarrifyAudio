from __future__ import annotations

from pathlib import Path

from backend.platform.artifact_publication import (
    PublicationJournal,
    PublicationJournalBundle,
    publication_transaction,
)


def _journal(root: Path) -> PublicationJournal:
    return PublicationJournal(root, root / ".tasks" / "task" / "attempt" / "publication.json")


def test_failed_database_step_restores_replaced_artifact(tmp_path):
    final = tmp_path / "chapter.json"
    final.write_text("old", encoding="utf-8")
    staged = tmp_path / "attempt.json"
    staged.write_text("new", encoding="utf-8")
    journal = _journal(tmp_path)
    try:
        with publication_transaction(journal):
            journal.publish(journal.add(final), staged)
            assert final.read_text("utf-8") == "new"
            raise RuntimeError("database commit failed")
    except RuntimeError:
        pass
    assert final.read_text("utf-8") == "old"
    assert not journal.path.exists()


def test_expired_attempt_removes_uncommitted_new_artifact(tmp_path):
    final = tmp_path / "new.json"
    staged = tmp_path / "attempt.json"
    staged.write_text("uncommitted", encoding="utf-8")
    journal = _journal(tmp_path)
    journal.prepare()
    journal.publish(journal.add(final), staged)
    assert PublicationJournal.reconcile(tmp_path, journal.path, committed=False)
    assert not final.exists()


def test_committed_attempt_keeps_new_artifact_and_removes_backup(tmp_path):
    final = tmp_path / "chapter.json"
    final.write_text("old", encoding="utf-8")
    staged = tmp_path / "attempt.json"
    staged.write_text("new", encoding="utf-8")
    journal = _journal(tmp_path)
    journal.prepare()
    journal.publish(journal.add(final), staged)
    assert PublicationJournal.reconcile(tmp_path, journal.path, committed=True)
    assert final.read_text("utf-8") == "new"
    assert not journal.path.exists()
    assert not list(journal.path.parent.glob("publication-backup-*"))


def test_rollback_is_idempotent_after_later_writer_creates_target(tmp_path):
    final = tmp_path / "new.json"
    staged = tmp_path / "attempt.json"
    staged.write_text("attempt output", encoding="utf-8")
    journal = _journal(tmp_path)
    journal.prepare()
    journal.publish(journal.add(final), staged)
    journal.rollback()
    final.write_text("later writer", encoding="utf-8")

    journal.rollback()

    assert final.read_text("utf-8") == "later writer"


def test_bundle_rollback_restores_files_across_independent_roots(tmp_path):
    workspace = tmp_path / "workspace"
    shared = tmp_path / "music-library"
    workspace.mkdir()
    shared.mkdir()
    workspace_file = workspace / "result.json"
    shared_file = shared / "music_index.json"
    workspace_file.write_text("old workspace", encoding="utf-8")
    shared_file.write_text("old shared", encoding="utf-8")
    workspace_staged = tmp_path / "workspace-staged.json"
    shared_staged = tmp_path / "shared-staged.json"
    workspace_staged.write_text("new workspace", encoding="utf-8")
    shared_staged.write_text("new shared", encoding="utf-8")
    workspace_journal = PublicationJournal(
        workspace, workspace / ".tasks" / "task" / "attempt" / "publication.json",
    )
    shared_journal = PublicationJournal(
        shared, shared / ".tasks" / "task" / "attempt" / "publication.json",
    )
    bundle = PublicationJournalBundle([workspace_journal, shared_journal])

    try:
        with publication_transaction(bundle):
            workspace_journal.publish(workspace_journal.add(workspace_file), workspace_staged)
            shared_journal.publish(shared_journal.add(shared_file), shared_staged)
            raise RuntimeError("database commit failed")
    except RuntimeError:
        pass

    assert workspace_file.read_text("utf-8") == "old workspace"
    assert shared_file.read_text("utf-8") == "old shared"
    assert not workspace_journal.path.exists()
    assert not shared_journal.path.exists()


def test_bundle_prepare_rolls_back_first_root_if_second_root_is_unavailable(tmp_path):
    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    workspace_journal = PublicationJournal(
        workspace, workspace / ".tasks" / "task" / "attempt" / "publication.json",
    )
    shared_journal = PublicationJournal(
        shared, shared / ".tasks" / "task" / "attempt" / "publication.json",
    )
    shared_journal.path.parent.mkdir(parents=True)
    shared_journal.path.write_text("conflict", encoding="utf-8")
    bundle = PublicationJournalBundle([workspace_journal, shared_journal])

    try:
        bundle.prepare()
    except RuntimeError:
        pass
    else:
        raise AssertionError("pre-existing second journal must reject bundle preparation")

    assert not workspace_journal.path.exists()
    assert shared_journal.path.read_text("utf-8") == "conflict"


# --------------------------------------------------------------------------- #
# Guarded entries: rollback may restore a shared-cache file ONLY while its
# bytes still match what the task published (M1 — a concurrent writer, e.g.
# the API process's tag propagation, must win over a late failure rollback).
# --------------------------------------------------------------------------- #

def _locked(raise_timeout: bool = False):
    from contextlib import contextmanager

    @contextmanager
    def lock():
        if raise_timeout:
            raise TimeoutError("storage lock busy")
        yield

    return lock()


def _publish_guarded(root: Path, name: str, content: str) -> tuple[PublicationJournal, Path, Path]:
    final = root / name
    final.write_text("original", encoding="utf-8")
    staged = root / "staged.json"
    staged.write_text(content, encoding="utf-8")
    journal = _journal(root)
    journal.prepare()
    journal.publish(journal.add(final, guard=True), staged)
    return journal, final, staged


def test_guarded_rollback_restores_untouched_publication(tmp_path):
    journal, final, _ = _publish_guarded(tmp_path, "cache.json", "published")
    assert final.read_text("utf-8") == "published"
    journal.rollback(guarded_lock=_locked())
    assert final.read_text("utf-8") == "original"
    assert not journal.path.exists()
    assert not list(journal.path.parent.glob("publication-backup-*"))


def test_guarded_rollback_keeps_concurrent_writer_version(tmp_path):
    journal, final, _ = _publish_guarded(tmp_path, "cache.json", "published")
    # A concurrent writer (API process) rewrites the shared cache after the
    # publication — the rollback must NOT restore over their edit.
    final.write_text("concurrent edit", encoding="utf-8")
    journal.rollback(guarded_lock=_locked())
    assert final.read_text("utf-8") == "concurrent edit"
    assert not journal.path.exists()
    assert not list(journal.path.parent.glob("publication-backup-*"))


def test_guarded_rollback_new_file_removed_when_untouched(tmp_path):
    final = tmp_path / "new.json"
    staged = tmp_path / "staged.json"
    staged.write_text("published new", encoding="utf-8")
    journal = _journal(tmp_path)
    journal.prepare()
    journal.publish(journal.add(final, guard=True), staged)
    assert final.read_text("utf-8") == "published new"
    journal.rollback(guarded_lock=_locked())
    assert not final.exists()
    assert not journal.path.exists()


def test_guarded_rollback_new_file_kept_when_concurrently_created(tmp_path):
    final = tmp_path / "new.json"
    staged = tmp_path / "staged.json"
    staged.write_text("published new", encoding="utf-8")
    journal = _journal(tmp_path)
    journal.prepare()
    journal.publish(journal.add(final, guard=True), staged)
    # A concurrent writer touched the path after publication — their file wins.
    final.write_text("concurrent new", encoding="utf-8")
    journal.rollback(guarded_lock=_locked())
    assert final.read_text("utf-8") == "concurrent new"
    assert not journal.path.exists()


def test_guarded_rollback_lock_timeout_skips_and_cleans(tmp_path):
    """The writer-domain lock is still busy at rollback time: the guarded
    phase is skipped entirely (published version stays on disk — the
    concurrent writer may be mid-write), backups are dropped so a later
    reconcile cannot resurrect the restore, and the journal is cleaned up.
    No exception escapes rollback."""
    journal, final, _ = _publish_guarded(tmp_path, "cache.json", "published")
    final.write_text("concurrent edit", encoding="utf-8")
    journal.rollback(guarded_lock=_locked(raise_timeout=True))
    assert final.read_text("utf-8") == "concurrent edit"
    assert not journal.path.exists()
    assert not list(journal.path.parent.glob("publication-backup-*"))


def test_unguarded_entries_unaffected_by_guarded_phase(tmp_path):
    """Phase split must not change the unguarded behaviour: the task's own
    artifacts restore unconditionally while a modified guarded entry is kept."""
    journal, guarded, _ = _publish_guarded(tmp_path, "shared.json", "published")
    artifact = tmp_path / "artifact.json"
    artifact.write_text("old artifact", encoding="utf-8")
    staged_artifact = tmp_path / "staged-artifact.json"
    staged_artifact.write_text("published artifact", encoding="utf-8")
    journal.publish(journal.add(artifact), staged_artifact)
    assert artifact.read_text("utf-8") == "published artifact"
    guarded.write_text("concurrent edit", encoding="utf-8")
    journal.rollback(guarded_lock=_locked())
    assert artifact.read_text("utf-8") == "old artifact"
    assert guarded.read_text("utf-8") == "concurrent edit"
    assert not journal.path.exists()


def test_reconcile_rebuilds_legacy_journal_without_guard_field(tmp_path):
    """Journals written before the guard field exists keep rolling back
    exactly as before (unguarded entries)."""
    final = tmp_path / "c.json"
    final.write_text("old", encoding="utf-8")
    staged = tmp_path / "s.json"
    staged.write_text("new", encoding="utf-8")
    journal = _journal(tmp_path)
    journal.prepare()
    journal.publish(journal.add(final), staged)
    import json

    legacy = {
        "version": 1,
        "files": [{
            "final": "c.json",
            "backup": str((journal.path.parent / "publication-backup-0.bin").relative_to(tmp_path)),
            "had_original": True,
        }],
    }
    journal.path.write_text(json.dumps(legacy), encoding="utf-8")
    assert PublicationJournal.reconcile(tmp_path, journal.path, committed=False)
    assert final.read_text("utf-8") == "old"


def test_reconcile_applies_guarded_compare_without_lock(tmp_path):
    """Crash recovery has no lock available: the fingerprint compare still
    protects the concurrent writer's version, and a fingerprint is only
    trusted when the journal carried one."""
    import json

    final = tmp_path / "c.json"
    final.write_text("old", encoding="utf-8")
    staged = tmp_path / "s.json"
    staged.write_text("new", encoding="utf-8")
    journal = _journal(tmp_path)
    journal.prepare()
    journal.publish(journal.add(final, guard=True), staged)
    final.write_text("concurrent edit", encoding="utf-8")
    assert PublicationJournal.reconcile(tmp_path, journal.path, committed=False)
    assert final.read_text("utf-8") == "concurrent edit"
    assert not journal.path.exists()
    assert not list(journal.path.parent.glob("publication-backup-*"))
