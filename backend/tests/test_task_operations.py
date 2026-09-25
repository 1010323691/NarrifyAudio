"""Behavior snapshots for the converged task-operation rules (S2/Q6).

The retry gate, ownership lookups and module labels were copy-pasted into
route modules; they now live in ``services.task_operations``. These tests
pin the pre-convergence behavior — same input, same output — so later
refactors cannot silently change the rules.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select

from backend.platform.config import settings
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import (
    Project, QuotaTransaction, Task, TaskAttempt, User, utcnow,
)
from backend.services.task_operations import (
    RetryNotAllowedError,
    check_retry_eligible,
    owned_project,
    owned_task,
    task_module,
    task_worker_group,
)

initialize_schema()


@pytest.fixture(autouse=True)
def _remove_snapshot_rows():
    """Remove every task row this test created, so global admin listings in
    later test modules stay unpolluted (shared in-memory database)."""
    with SessionLocal() as db:
        before = set(db.scalars(select(Task.id)).all())
    yield
    with SessionLocal.begin() as db:
        for task_id in set(db.scalars(select(Task.id)).all()) - before:
            db.execute(delete(QuotaTransaction).where(QuotaTransaction.task_id == task_id))
            db.execute(delete(TaskAttempt).where(TaskAttempt.task_id == task_id))
            db.execute(delete(Task).where(Task.id == task_id))


def _fresh_owner(db):
    suffix = uuid.uuid4().hex[:12]
    user = User(
        email=f"snapshot-{suffix}@example.test",
        username=f"s{suffix}",
        password_hash="not-a-real-hash",
    )
    db.add(user)
    db.flush()
    project = Project(
        owner_id=user.id,
        name=f"snapshot-{suffix}",
        directory_key=f"s{suffix}/p-{suffix}",
    )
    db.add(project)
    db.flush()
    return user, project


def _failed_task(db, owner, project):
    task = Task(owner_id=owner.id, project_id=project.id, task_type="text.format", status="failed")
    db.add(task)
    db.flush()
    return task


# -- module labels ----------------------------------------------------------


def test_task_module_labels_snapshot():
    cases = {
        "voices.foundation": "voices-foundation",
        "voices.clone": "voices-clone",
        "tts.batch": "tts-batch",
        "tts.merge": "merge",
        "bgm.analysis": "bgm-analysis",
        "bgm.segment": "bgm-segment",
        "bgm.mix": "bgm-mix",
        "music.suggest_tags": "music-ai-tags",
        # fallback: task prefix
        "script.parse": "script",
        "audio.silences": "audio",
        "book.ocr": "book",
        "unknown.type": "unknown",
    }
    for task_type, expected in cases.items():
        assert task_module(task_type) == expected, task_type


def test_task_worker_groups_snapshot():
    cases = {
        "script.parse": "llm",
        "music.suggest_tags": "llm",
        "tts.batch": "tts",
        "voices.clone": "tts",
        "audio.silences": "audio",
        "bgm.mix": "audio",
        "book.ocr": "system",
        "text.format": "system",
        "unknown.type": "worker",
    }
    for task_type, expected in cases.items():
        assert task_worker_group(task_type) == expected, task_type


# -- retry gate ---------------------------------------------------------------


def test_retry_gate_allows_failed_zero_cost_task():
    with SessionLocal.begin() as db:
        owner, project = _fresh_owner(db)
        task = _failed_task(db, owner, project)
        assert check_retry_eligible(db, task) == 0


def test_retry_gate_rejects_non_terminal_status():
    with SessionLocal.begin() as db:
        owner, project = _fresh_owner(db)
        task = Task(owner_id=owner.id, project_id=project.id, task_type="text.format", status="running")
        db.add(task)
        db.flush()
        with pytest.raises(RetryNotAllowedError, match="任务当前不可重试"):
            check_retry_eligible(db, task)


def test_retry_gate_rejects_metered_consumption():
    with SessionLocal.begin() as db:
        owner, project = _fresh_owner(db)
        task = _failed_task(db, owner, project)
        db.add(QuotaTransaction(
            user_id=owner.id, task_id=task.id, amount=-1, kind="consume",
            resource_type="LLM", idempotency_key=uuid.uuid4().hex,
        ))
        db.flush()
        with pytest.raises(RetryNotAllowedError, match="已有模型消费"):
            check_retry_eligible(db, task)


def test_retry_gate_rejects_attempt_cap():
    with SessionLocal.begin() as db:
        owner, project = _fresh_owner(db)
        task = _failed_task(db, owner, project)
        for number in range(1, settings.task_max_attempts + 1):
            db.add(TaskAttempt(task_id=task.id, attempt_no=number))
        db.flush()
        with pytest.raises(RetryNotAllowedError, match="最大尝试次数"):
            check_retry_eligible(db, task)


# -- ownership lookups ----------------------------------------------------------


def test_owned_task_scopes_by_owner_and_project():
    with SessionLocal.begin() as db:
        owner, project = _fresh_owner(db)
        stranger, _ = _fresh_owner(db)
        task = Task(owner_id=owner.id, project_id=project.id, task_type="text.format", status="pending")
        db.add(task)
        db.flush()
        assert owned_task(db, owner.id, task.id) is not None
        assert owned_task(db, stranger.id, task.id) is None
        assert owned_task(db, owner.id, task.id, project_id=project.id) is not None
        other = Project(
            owner_id=owner.id,
            name=f"other-{uuid.uuid4().hex[:12]}",
            directory_key=f"o{uuid.uuid4().hex[:12]}".replace("-", ""),
        )
        db.add(other)
        db.flush()
        assert owned_task(db, owner.id, task.id, project_id=other.id) is None


def test_owned_project_excludes_foreign_and_soft_deleted():
    with SessionLocal.begin() as db:
        owner, project = _fresh_owner(db)
        stranger, _ = _fresh_owner(db)
        assert owned_project(db, owner.id, project.id) is not None
        assert owned_project(db, stranger.id, project.id) is None
        project.deleted_at = utcnow()
        db.flush()
        assert owned_project(db, owner.id, project.id) is None
