from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.main import app
from backend.platform import metrics_sampler, worker_registry
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import Task, TaskEvent, WorkerHeartbeat, utcnow


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


@pytest.fixture(autouse=True)
def _clean_registry():
    initialize_schema()
    worker_registry._last_beat.clear()
    with SessionLocal.begin() as db:
        db.query(WorkerHeartbeat).delete()
    yield
    worker_registry._last_beat.clear()
    with SessionLocal.begin() as db:
        db.query(WorkerHeartbeat).delete()


def _row(worker_id):
    with SessionLocal() as db:
        return db.get(WorkerHeartbeat, worker_id)


def test_unchanged_idle_heartbeat_is_written_at_most_once_per_interval(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(worker_registry.time, "monotonic", lambda: clock[0])
    worker_registry.heartbeat("hb-a", status="idle")
    first = _row("hb-a").updated_at
    clock[0] += 1.0
    worker_registry.heartbeat("hb-a", status="idle")
    assert _row("hb-a").updated_at == first  # throttled: no write
    clock[0] += worker_registry.HEARTBEAT_MIN_INTERVAL_SECONDS
    worker_registry.heartbeat("hb-a", status="idle")
    assert _row("hb-a").updated_at > first


def test_status_or_task_change_is_written_immediately_and_offline_resets(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(worker_registry.time, "monotonic", lambda: clock[0])
    worker_registry.heartbeat("hb-b", status="idle")
    worker_registry.heartbeat("hb-b", status="running", current_task_id="t1")
    row = _row("hb-b")
    assert (row.status, row.current_task_id) == ("running", "t1")
    worker_registry.mark_offline("hb-b")
    assert _row("hb-b").status == "offline"
    worker_registry.heartbeat("hb-b", status="running", current_task_id="t1")  # same as before offline
    assert _row("hb-b").status == "running"


def test_prune_stale_removes_only_rows_silent_for_a_day():
    now = utcnow()
    with SessionLocal.begin() as db:
        for wid, age in (("old", timedelta(days=2)), ("fresh", timedelta(minutes=1))):
            db.add(WorkerHeartbeat(worker_id=wid, status="idle", capabilities={}, started_at=now,
                                   last_seen_at=now - age, updated_at=now))
    assert worker_registry.prune_stale() == 1
    assert _row("old") is None and _row("fresh") is not None


def test_prune_task_events_keeps_recent_and_unfinished_tasks(client, monkeypatch):
    from backend.platform.models import Project
    from backend.tests.test_admin_analytics import _register

    monkeypatch.setattr(metrics_sampler, "_EVENT_PRUNE_BATCH", 2)  # force several batches
    now = utcnow()
    _, owner_id, _ = _register(client)
    with SessionLocal.begin() as db:
        project_id = db.scalar(select(Project.id).where(Project.owner_id == owner_id))
        ids = {}
        for name, status, age in (("old_done", "succeeded", 40), ("new_done", "succeeded", 1), ("old_running", "running", 40)):
            task = Task(id=str(uuid.uuid4()), owner_id=owner_id, project_id=project_id, task_type="tts.batch", status=status,
                        finished_at=None if status == "running" else now - timedelta(days=age), payload={})
            db.add(task)
            db.flush()
            for seq in range(5):
                db.add(TaskEvent(task_id=task.id, sequence=seq, event_type="progress", payload={}))
            ids[name] = task.id
    try:
        assert metrics_sampler.prune_task_events() == 5
        with SessionLocal() as db:
            count = lambda name: db.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.task_id == ids[name]))
            assert (count("old_done"), count("new_done"), count("old_running")) == (0, 5, 5)
    finally:
        with SessionLocal.begin() as db:
            db.query(Task).filter(Task.id.in_(ids.values())).delete(synchronize_session=False)
