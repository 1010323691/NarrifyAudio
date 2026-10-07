"""Task-center isolation, bounded reads and aggregate semantics."""
from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.main import app
from backend.platform.database import Base, get_db
from backend.platform.deps import require_authenticated_user, require_csrf
from backend.platform.models import Project, Task, TaskEvent, User, utcnow
from backend.services import task_center, task_views
from backend.api import platform_tasks


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([User(id=name, username=name, email=f"{name}@test.local", password_hash="unused") for name in ("u", "other")])
        session.flush()
        session.add_all([Project(id=name, owner_id=owner, name=name, directory_key=name, deleted_at=utcnow() if name == "trash" else None)
                         for name, owner in (("book", "u"), ("trash", "u"), ("foreign", "other"))])
        session.commit()
        yield session
    engine.dispose()


def add(db, id, *, project="book", owner="u", type="script.parse", status="succeeded", age=0, entry=None):
    value = Task(id=id, owner_id=owner, project_id=project, task_type=type, status=status,
                 created_at=utcnow() - timedelta(seconds=age), progress=40,
                 payload={"source_name": entry or id, "label": f"章节 {id}"})
    db.add(value)
    db.flush()
    return value


def test_visibility_counts_progress_and_filters(db):
    add(db, "old", age=10, entry="same")
    add(db, "new", entry="same", status="running")
    add(db, "failed", status="failed")
    add(db, "cancelled", status="cancelled")
    add(db, "timeout", status="timeout")
    add(db, "paused", status="paused")
    add(db, "queued", status="queued")
    add(db, "retrying", status="retrying")
    add(db, "cancelling", status="cancelling")
    add(db, "done")
    add(db, "deleted", project="trash")
    add(db, "foreign", project="foreign", owner="other")
    db.add_all([TaskEvent(task_id="new", sequence=sequence, event_type="progress", payload={"current": f"进度 {sequence}"}) for sequence in (1, 2)])
    db.commit()
    stage = task_center.summary(db, "u")["items"][0]
    assert stage == {"category": "script", "project_count": 1, "task_count": 7, "active_count": 5}
    group = task_center.groups(db, "u", "script")["items"][0]
    assert (group["succeeded_count"], group["pausable_count"], group["resumable_count"]) == (1, 3, 1)
    page = task_center.items(db, "u", "script", "book")
    assert len(page["items"]) == page["total"] == 7
    assert next(item for item in page["items"] if item["id"] == "new")["current"] == "进度 2"
    assert task_center.items(db, "u", "script", "book", "active")["total"] == 5
    assert task_center.items(db, "u", "script", "book", "completed")["total"] == 2
    assert task_center.items(db, "u", "script", "foreign")["total"] == 0
    assert task_center.items(db, "u", "script", "trash")["total"] == 0


def test_category_alignment_and_group_paging(db):
    add(db, "preview", type="tts.preview_render", status="paused")
    add(db, "analysis", type="bgm.analysis", status="running")
    for i in range(7):
        db.add(Project(id=f"p{i}", owner_id="u", name=f"p{i}", directory_key=f"p{i}"))
        db.flush()
        add(db, f"chapter{i}", project=f"p{i}", age=i, status="running" if i == 6 else "succeeded")
    db.commit()
    assert task_center.groups(db, "u", "preview")["items"][0]["resumable_count"] == 1
    assert task_center.groups(db, "u", "bgm")["items"][0]["active_count"] == 1
    first = task_center.groups(db, "u", "script")
    second = task_center.groups(db, "u", "script", 2)
    assert first["total"] == 7
    assert len(first["items"]) == 5 and len(second["items"]) == 2
    assert first["items"][0]["project_id"] == "p6"
    assert not ({item["project_id"] for item in first["items"]} & {item["project_id"] for item in second["items"]})


def test_rerun_semantics_cancelled_failed_and_missing_identity(db):
    add(db, "kept", entry="cancel", age=10)
    add(db, "cancel", entry="cancel", status="cancelled")
    add(db, "hidden", entry="fail", age=10)
    add(db, "fail", entry="fail", status="failed")
    add(db, "active-old", entry="running", status="running", age=10)
    add(db, "active-new", entry="running", status="queued")
    for name in ("no-identity1", "no-identity2"):
        add(db, name).payload = {}
    db.commit()
    ids = {item["id"] for item in task_center.items(db, "u", "script", "book")["items"]}
    assert ids == {"kept", "active-old", "active-new", "no-identity1", "no-identity2"}


def test_queries_are_bounded_and_first_level_never_reads_events_or_results(db):
    for i in range(120):
        add(db, f"t{i:03d}", status="queued")
    db.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        task_center.summary(db, "u")
        task_center.groups(db, "u", "script")
        assert len(statements) == 3
        assert not any("task_events" in sql or "task_results" in sql for sql in statements)
        statements.clear()
        first = task_center.items(db, "u", "script", "book")
        assert len(statements) == 3
        assert len(first["items"]) == 50 and first["total"] == 120
        assert first["counts"]["pausable_count"] == 120
        assert not any("task_results" in sql for sql in statements)
        assert all(not ({"logs", "result", "payload"} & item.keys()) for item in first["items"])
        statements.clear()
        last = task_center.items(db, "u", "script", "book", page=3)
        assert len(last["items"]) == 20 and len(statements) == 3
        assert not ({item["id"] for item in first["items"]} & {item["id"] for item in last["items"]})
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)


def test_routes_validation_and_compact_batch_compatibility(db):
    user = db.get(User, "u")
    overrides = {get_db: lambda: db, require_authenticated_user: lambda: user, require_csrf: lambda: user}
    previous = app.dependency_overrides.copy()
    app.dependency_overrides.update(overrides)
    try:
        client = TestClient(app)
        for path in ("summary", "groups?category=script", "items?category=script&project_id=book"):
            assert client.get(f"/api/v1/tasks/center/{path}").status_code == 200
        for query in ("category=invalid", "category=script&page=0"):
            assert client.get(f"/api/v1/tasks/center/groups?{query}").status_code == 422
        assert client.get("/api/v1/tasks/center/items?category=script&project_id=book&filter=unknown").status_code == 422
        add(db, "preview", type="tts.preview_render", status="queued")
        add(db, "bgm", type="bgm.analysis", status="queued")
        db.commit()
        response = client.post("/api/v1/tasks/batch-control?compact=true", json={"project_id": "book", "category": "preview", "action": "pause"})
        assert response.status_code == 200 and response.json() == {"changed": 1}
        response = client.post("/api/v1/tasks/batch-control", json={"project_id": "book", "category": "bgm", "action": "pause"})
        assert response.status_code == 200 and len(response.json()["tasks"]) == 1
        assert response.json()["tasks"][0]["status"] == "paused"
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_large_history_benchmark_and_index_plan(db, monkeypatch):
    # 1,000 distinct entries and 20,000 log events; never touch deployment data.
    now = utcnow()
    db.add_all([Task(id=f"bench{i:04d}", owner_id="u", project_id="book", task_type="script.parse",
                     status="succeeded", created_at=now + timedelta(seconds=i),
                     payload={"source_name": f"chapter{i}", "label": f"章节 {i}"}) for i in range(1000)])
    db.flush()
    db.execute(TaskEvent.__table__.insert(), [
        {"id": f"log{i}-{j}", "task_id": f"bench{i:04d}", "sequence": j + 1, "event_type": "log", "payload": {"msg": "x" * 120}}
        for i in range(1000) for j in range(20)
    ])
    db.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        statements.append((statement, parameters))
    event.listen(db.bind, "before_cursor_execute", capture)
    monkeypatch.setattr(task_views, "SessionLocal", lambda: Session(db.bind))
    try:
        start = time.perf_counter()
        old_page = platform_tasks.list_task_history(user=db.get(User, "u"), db=db)
        snapshots = task_views.snapshot_payload(lambda session: platform_tasks._user_tasks(session, "u"))[0]
        old_ms = (time.perf_counter() - start) * 1000
        old_queries = len(statements)
        old_bytes = len(json.dumps({"history": old_page, "snapshot": snapshots}).encode())
        statements.clear()
        start = time.perf_counter()
        overview = task_center.summary(db, "u")
        first_ms = (time.perf_counter() - start) * 1000
        project_page = task_center.groups(db, "u", "script")
        new_ms = (time.perf_counter() - start) * 1000
        assert len(statements) == 3
        new_bytes = len(json.dumps({"summary": overview, "groups": project_page}).encode())
        sql, parameters = statements[0]
        with db.bind.connect() as connection:
            plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters).all()
        assert any("ix_tasks_owner_created" in str(row) or "ix_tasks_entry_created" in str(row) for row in plan)
        print(f"\nTask-center benchmark SQLite: old ready={old_ms:.1f}ms/{old_queries}SQL/{old_bytes}B; "
              f"new summary={first_ms:.1f}ms, summary+groups={new_ms:.1f}ms/3SQL/{new_bytes}B")
        assert old_queries > 400 and new_bytes < old_bytes / 10
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)


def test_ranked_identity_matches_existing_predicate_for_all_registry_keys(db):
    from backend.platform.task_identity import current_entry_ids, one_row_per_entry
    from backend.platform.task_registry import TASK_TYPES

    statuses = ["succeeded", "timeout", "failed", "cancelled", "queued", "running", "paused", "retrying", "cancelling"]
    now = utcnow()
    names = [*TASK_TYPES, "unregistered.legacy"]
    for type_index, name in enumerate(names):
        keys = TASK_TYPES[name].entry_identity if name in TASK_TYPES else ("legacy",)
        for subject in range(4):
            for run in range(5):
                payload = {}
                for index, key in enumerate(keys):
                    if subject == 0:
                        continue
                    payload[key] = None if subject == 1 or (subject == 2 and index == 1) else [f"subject-{subject}", index]
                db.add(Task(id=f"identity-{type_index:02d}-{subject}-{run}", owner_id="u", project_id="book",
                            task_type=name, status=statuses[(subject * 5 + run) % len(statuses)],
                            created_at=now + timedelta(seconds=run // 2), payload=payload))
    db.commit()
    expected = set(db.scalars(select(Task.id).where(Task.owner_id == "u", Task.status != "cancelled", one_row_per_entry())).all())
    actual = set(db.scalars(current_entry_ids("u", names)).all())
    assert actual == expected
    # No registry identity must mean no deduplication, even if all payloads match.
    assert set(db.scalars(current_entry_ids("u", ["unregistered.legacy"])).all()) == {
        id for id in expected if id.startswith(f"identity-{len(names) - 1:02d}-")
    }
