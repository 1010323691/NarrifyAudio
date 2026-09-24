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
