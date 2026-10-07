"""Whole-task admission stays bounded across types, processes and resumes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.platform import task_context, task_worker, system_config
from backend.platform.database import Base
from backend.platform.gpu_scheduler import store
from backend.platform.models import Project, SystemConfig, Task, TaskAttempt, User, UserQuotaAccount, utcnow
from backend.platform.task_admission import LLM_TASK_TYPES
from backend.services.task_operations import control_task_category


@pytest.fixture
def admission_db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'admission.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    for module in (task_worker, task_context, system_config, store):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setattr(store, "PROJECT_ROOT", tmp_path)
    with sessions.begin() as db:
        for owner in ("owner-a", "owner-b"):
            db.add(User(id=owner, username=owner, email=f"{owner}@example.invalid", password_hash="test"))
            db.flush()
            db.add(UserQuotaAccount(user_id=owner))
            db.add(Project(id=f"project-{owner}", owner_id=owner, name=owner, directory_key=f"{owner}/{owner}"))
        db.add(SystemConfig(key="application.features", value={"generation": {"parse_worker_concurrency": 8}}))
    yield sessions, tmp_path
    engine.dispose()


def add_tasks(sessions, count, types=LLM_TASK_TYPES):
    with sessions.begin() as db:
        tasks = [Task(owner_id=f"owner-{'a' if i % 2 == 0 else 'b'}",
                      project_id=f"project-owner-{'a' if i % 2 == 0 else 'b'}",
                      task_type=types[i % len(types)], payload={"stem": f"chapter-{i}"})
                 for i in range(count)]
        db.add_all(tasks)
        db.flush()
        return [task.id for task in tasks]


def claim(task_id, worker="test-worker"):
    return task_worker.claim_task(task_id, worker, defer_workspace_conflicts=False)


def finish(sessions, task_id):
    with sessions.begin() as db:
        db.get(Task, task_id).status = "succeeded"
        db.scalar(select(TaskAttempt).where(TaskAttempt.task_id == task_id)).status = "succeeded"


def test_mixed_llm_tasks_share_sixteen_positions_across_workers(admission_db):
    sessions, _ = admission_db
    ids = add_tasks(sessions, 24)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda pair: claim(pair[1], f"worker-{pair[0] % 4}"), enumerate(ids)))
    assert sum(c is not None for c in claims) == 16
    blocked = [task_id for task_id, c in zip(ids, claims) if c is None]
    with sessions() as db:
        assert {db.get(Task, task_id).status for task_id in blocked} == {"pending"}
        assert db.scalar(select(func.count()).select_from(TaskAttempt)) == 16
    # Mechanical tasks do not consume the LLM task budget, and a released task
    # position is usable by a different LLM feature without waiting for a batch.
    mechanical = add_tasks(sessions, 1, ("text.format",))[0]
    assert claim(mechanical) is not None
    finish(sessions, next(task_id for task_id, c in zip(ids, claims) if c is not None))
    assert claim(blocked[0]) is not None
    assert claim(blocked[1]) is None


def test_fair_selection_does_not_block_mechanical_tasks_at_llm_capacity(admission_db):
    sessions, _ = admission_db
    ids = add_tasks(sessions, 17, ("script.parse",))
    for task_id in ids[:16]:
        assert claim(task_id) is not None
    mechanical = add_tasks(sessions, 1, ("text.format",))[0]
    selected = task_worker.claim_fair_task("mixed-worker", task_types=("script.parse", "text.format"))
    assert selected.task_id == mechanical
    assert task_worker.claim_fair_task("llm-worker", task_types=LLM_TASK_TYPES) is None


def test_paused_live_attempt_reacquires_capacity_before_resuming(admission_db):
    sessions, _ = admission_db
    ids = add_tasks(sessions, 16, ("script.parse",))
    claims = [claim(task_id) for task_id in ids]
    with sessions.begin() as db:
        control_task_category(db, "owner-a", "project-owner-a", "script", "pause")
    # Eight paused tasks release their positions; replacements may start.
    extra = add_tasks(sessions, 8, ("script.parse",))
    for task_id in extra:
        assert claim(task_id) is not None
    with sessions.begin() as db:
        control_task_category(db, "owner-a", "project-owner-a", "script", "resume")
    context = task_context.EngineExecutionContext(claims[0])
    assert context._paused()  # full: same attempt remains parked and displayed as queued
    with sessions() as db:
        assert db.get(Task, ids[0]).status == "queued"
        assert db.get(TaskAttempt, claims[0].attempt_id).status == "running"
    # Periodic dead-worker recovery must preserve queued, still-leased attempts.
    task_worker.recover_database_tasks()
    with sessions() as db:
        assert db.get(TaskAttempt, claims[0].attempt_id).status == "running"
    finish(sessions, ids[1])
    with ThreadPoolExecutor(max_workers=8) as pool:
        waiting = list(pool.map(lambda c: task_context.EngineExecutionContext(c)._paused(), claims[::2]))
    assert waiting.count(False) == 1
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Task).where(Task.status == "running")) == 16
        assert db.scalar(select(func.count()).select_from(TaskAttempt)) == 24


def test_reduction_cancellation_and_expired_attempts_respect_capacity(admission_db):
    sessions, _ = admission_db
    ids = add_tasks(sessions, 18, ("script.parse",))
    for task_id in ids[:16]:
        assert claim(task_id) is not None
    with sessions.begin() as db:
        db.get(SystemConfig, "application.features").value = {"generation": {"parse_worker_concurrency": 4}}
        for task_id in ids[:8]:
            db.get(Task, task_id).status = "cancelling"
    assert claim(ids[16]) is None  # cancellation still holds capacity until fenced completion
    for task_id in ids[:8]:
        finish(sessions, task_id)
    assert claim(ids[16]) is None  # lower cap applies immediately to subsequent admissions
    with sessions.begin() as db:
        attempt = db.scalar(select(TaskAttempt).where(TaskAttempt.task_id == ids[8]))
        attempt.lease_expires_at = utcnow() - timedelta(seconds=1)
    assert claim(ids[16]) is None  # visible running task keeps its place until recovery
    # The same task can reclaim its position without adding a seventeenth task.
    assert claim(ids[8]) is not None
    finish(sessions, ids[9])
    assert claim(ids[16]) is not None
    assert claim(ids[17]) is None


def test_four_processes_cannot_multiply_the_task_limit(admission_db):
    sessions, tmp_path = admission_db
    ids = add_tasks(sessions, 40, ("script.parse",))
    script = """
import sys
from pathlib import Path
from backend.platform import task_worker
from backend.platform.gpu_scheduler import store
store.PROJECT_ROOT = Path(sys.argv[1])
for task_id in sys.argv[2:]:
    task_worker.claim_task(task_id, 'process-' + str(__import__('os').getpid()))
"""
    env = {**os.environ, "NARRIFY_DATABASE_URL": f"sqlite:///{tmp_path / 'admission.db'}"}
    children = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), *ids[i::4]],
                                 cwd=Path(__file__).resolve().parents[2], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for i in range(4)]
    try:
        for child in children:
            output, error = child.communicate(timeout=30)
            assert child.returncode == 0, output + error
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate()
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(TaskAttempt)) == 16
        assert db.scalar(select(func.count()).select_from(Task).where(Task.status == "pending")) == 24
