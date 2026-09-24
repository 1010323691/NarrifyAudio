from __future__ import annotations

from pathlib import Path

from backend.platform.artifact_publication import PublicationJournal, publication_transaction


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
