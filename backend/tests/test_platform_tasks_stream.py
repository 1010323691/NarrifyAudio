"""5a: the v1 aggregate task stream (user-scoped, one connection, many tasks)
+ the v1 retry route. The legacy ``/api/tasks`` surface is retired (5a-3).

Test strategy: this starlette's TestClient request path blocks until the entire
response completes, so the never-ending aggregate stream is exercised two ways —
(1) through the real route, terminated by revoking the session mid-stream (the
generator's 5 s auth re-check ends the body), and (2) by driving
``task_views.aggregate_stream`` directly in ``asyncio`` for the live-frame /
ping / pruning / disconnect semantics.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

pytest.importorskip("sqlalchemy")

from backend.main import app
from backend.platform.platform_settings import settings
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import Task, TaskEvent, UserSession
from backend.services import task_views
from backend.services.task_operations import task_module


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


def _register(email: str) -> dict:
    # Bare TestClient (no ``with``) — no second app lifespan / worker.
    c = TestClient(app)
    response = c.post(
        "/api/auth/register",
        json={"email": email, "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234", "display_name": "Test User"},
    )
    assert response.status_code == 201, response.text
    return {"json": response.json(), "cookie": c.cookies.get(settings.session_cookie), "client": c}


def _create_task(user_id: str, task_type: str = "text.format", *, status: str = "running",
                 progress: int = 40, label: str = "", project_id: str | None = None) -> str:
    with SessionLocal() as db:
        task = Task(
            owner_id=user_id, project_id=project_id or str(uuid.uuid4()), task_type=task_type,
            status=status, progress=progress, payload={"label": label} if label else {},
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return task.id


def _add_event(task_id: str, sequence: int, event_type: str, payload: dict) -> None:
    with SessionLocal() as db:
        db.add(TaskEvent(task_id=task_id, sequence=sequence, event_type=event_type, payload=payload))
        db.commit()


def _revoke_sessions(user_id: str) -> None:
    with SessionLocal() as db:
        for session in db.scalars(select(UserSession).where(UserSession.user_id == user_id)).all():
            session.revoked_at = datetime.now(timezone.utc)
        db.commit()


def _frames(text: str) -> list[dict]:
    return [json.loads(line[len("data: "):])
            for line in text.splitlines() if line.startswith("data: ")]


def test_v1_stream_route_is_user_scoped_and_terminates_on_session_revocation(client):
    a = _register(f"v1stream-a-{uuid.uuid4().hex[:10]}@example.com")
    a_id = a["json"]["user"]["id"]
    ta = _create_task(a_id, label="任务 A")
    tb = _create_task(a_id, task_type="audio.zip", status="queued", progress=0)
    _add_event(ta, 1, "submitted", {})
    _add_event(ta, 2, "progress", {"current": "第一步", "fraction": 0.4})
    _add_event(ta, 3, "log", {"level": "INFO", "msg": "开始"})
    # A second user's task must never appear in A's stream.
    b = _register(f"v1stream-b-{uuid.uuid4().hex[:10]}@example.com")
    tc = _create_task(b["json"]["user"]["id"], status="running")

    result: dict = {}
    # The registering client owns A's session cookie; it is used only by the
    # reader thread below (TestClient is not thread-safe, so no sharing).
    reader = a["client"]

    def read_stream():
        # Blocks until the stream ends — which it does once the session below is revoked.
        response = reader.get("/api/v1/tasks/stream")
        result["status"] = response.status_code
        result["body"] = response.text

    thread = threading.Thread(target=read_stream, daemon=True)
    thread.start()
    time.sleep(2.0)  # stream is open: snapshot_all + pings are on the wire
    _revoke_sessions(a_id)  # the generator's 5 s auth re-check then ends the stream
    thread.join(timeout=15)

    assert not thread.is_alive(), "stream never terminated after session revocation"
    assert result["status"] == 200
    frames = _frames(result["body"])
    snap = next(f for f in frames if f["type"] == "snapshot_all")
    ids = {t["id"] for t in snap["tasks"]}
    assert {ta, tb} <= ids
    assert tc not in ids, "another user's task leaked into the stream"
    snap_a = next(t for t in snap["tasks"] if t["id"] == ta)
    assert snap_a["module"] == task_module("text.format")
    assert snap_a["label"] == "任务 A"
    assert snap_a["status"] == "running"
    assert snap_a["progress"] == 0.4
    assert snap_a["current"] == "第一步"
    assert [log["msg"] for log in snap_a["logs"]] == ["开始"]
    assert any(f["type"] == "ping" for f in frames)


def test_aggregate_stream_live_frames_pruning_and_disconnect():
    """Drive the shared generator directly: initial snapshot, live event frames,
    idle pings, row pruning, and clean termination on disconnect."""
    a = _register(f"v1live-{uuid.uuid4().hex[:10]}@example.com")
    uid = a["json"]["user"]["id"]
    token = a["cookie"]
    project_id = str(uuid.uuid4())
    ta = _create_task(uid, project_id=project_id)
    tb = _create_task(uid, project_id=project_id)
    _add_event(ta, 1, "submitted", {})
    _add_event(tb, 1, "submitted", {})

    state = {"rows": [ta, tb], "disconnected": False}

    def rows_fn(db):
        return db.scalars(select(Task).where(Task.id.in_(state["rows"]))).all()

    async def disconnected():
        return state["disconnected"]

    async def drive():
        frames: list[dict] = []
        gen = task_views.aggregate_stream(rows_fn, token, uid, disconnected)

        async def take():
            raw = await asyncio.wait_for(gen.__anext__(), 3.0)
            frames.append(json.loads(raw[len("data: "):]))
            return frames[-1]

        # 1) initial snapshot replays both tasks
        snap = await take()
        assert snap["type"] == "snapshot_all"
        assert {t["id"] for t in snap["tasks"]} == {ta, tb}
        # 2) an event appended after connect arrives as a live frame
        _add_event(ta, 2, "progress", {"current": "第二步", "fraction": 0.6})
        live = None
        for _ in range(30):
            frame = await take()
            if frame.get("type") == "progress" and frame.get("task_id") == ta:
                live = frame
                break
        assert live is not None, f"no live progress frame: {frames}"
        assert live["current"] == "第二步"
        # 3) a row that leaves the view and comes back is replayed from scratch:
        #    prune tb (one poll must see it absent), append a tb event meanwhile,
        #    then restore the row — both of tb's events arrive again.
        state["rows"] = [ta]
        await take()  # the poll that prunes tb from `seen`
        _add_event(tb, 2, "progress", {"current": "重新出现", "fraction": 0.2})
        state["rows"] = [ta, tb]
        tb_frames = []
        for _ in range(30):
            frame = await take()
            tb_frames.extend(f for f in [frame] if f.get("task_id") == tb)
            if any(f["type"] == "progress" for f in tb_frames):
                break
        types = [f["type"] for f in tb_frames]
        assert "snapshot" in types, f"tb was not re-replayed from the top: {tb_frames}"
        assert "progress" in types and any(
            f.get("current") == "重新出现" for f in tb_frames
        ), f"tb's events did not replay: {tb_frames}"
        # 4) disconnect ends the stream cleanly
        state["disconnected"] = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), 3.0)
        return frames

    frames = asyncio.run(drive())
    assert any(f["type"] == "ping" for f in frames)


def test_user_tasks_project_filter():
    a = _register(f"v1proj-{uuid.uuid4().hex[:10]}@example.com")
    uid = a["json"]["user"]["id"]
    project_a = str(uuid.uuid4())
    in_project = _create_task(uid, project_id=project_a)
    out_project = _create_task(uid)  # a different random project

    from backend.api.platform_tasks import _user_tasks

    with SessionLocal() as db:
        all_rows = {t.id for t in _user_tasks(db, uid)}
        filtered = {t.id for t in _user_tasks(db, uid, project_a)}
    assert in_project in all_rows and out_project in all_rows
    assert in_project in filtered and out_project not in filtered


def test_task_history_cursor_pages_are_stable_and_user_scoped(client):
    first = _register(f"history-{uuid.uuid4().hex[:10]}@example.com")
    user_id = first["json"]["user"]["id"]
    shared_time = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add_all([
            Task(
                id=str(uuid.uuid4()),
                owner_id=user_id,
                project_id=str(uuid.uuid4()),
                task_type="script.parse",
                status="succeeded",
                progress=100,
                payload={"label": f"历史任务 {index}"},
                created_at=shared_time,
                updated_at=shared_time,
            )
            for index in range(53)
        ])
        db.commit()

    other = _register(f"history-other-{uuid.uuid4().hex[:10]}@example.com")
    _create_task(other["json"]["user"]["id"], task_type="script.parse", status="succeeded")

    response = first["client"].get("/api/v1/tasks/history")
    assert response.status_code == 200, response.text
    page_one = response.json()
    assert len(page_one["items"]) == 50
    assert page_one["next_cursor"]
    assert all(item["project_name"] == "已删除项目" for item in page_one["items"])

    response = first["client"].get("/api/v1/tasks/history", params={"cursor": page_one["next_cursor"]})
    assert response.status_code == 200, response.text
    page_two = response.json()
    assert len(page_two["items"]) == 3
    assert page_two["next_cursor"] is None

    first_ids = [item["id"] for item in page_one["items"]]
    second_ids = [item["id"] for item in page_two["items"]]
    assert len(set(first_ids + second_ids)) == 53
    assert set(first_ids + second_ids).isdisjoint(
        {task["id"] for task in _user_task_rows(other["json"]["user"]["id"])}
    )

    invalid = first["client"].get("/api/v1/tasks/history", params={"cursor": "bad-cursor"})
    assert invalid.status_code == 422


def _user_task_rows(user_id: str) -> list[dict]:
    with SessionLocal() as db:
        return [
            {"id": task.id}
            for task in db.scalars(select(Task).where(Task.owner_id == user_id)).all()
        ]


def test_user_task_stream_keeps_old_active_tasks_in_live_window():
    account = _register(f"oldactive-{uuid.uuid4().hex[:10]}@example.com")
    user_id = account["json"]["user"]["id"]
    project_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    old_active_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(Task(
            id=old_active_id,
            owner_id=user_id,
            project_id=project_id,
            task_type="script.parse",
            status="running",
            progress=12,
            payload={},
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            updated_at=now,
        ))
        db.add_all([
            Task(
                owner_id=user_id,
                project_id=project_id,
                task_type="text.format",
                status="succeeded",
                progress=100,
                payload={},
                created_at=now,
                updated_at=now,
            )
            for _ in range(205)
        ])
        db.commit()

    from backend.api.platform_tasks import _user_tasks

    with SessionLocal() as db:
        rows = _user_tasks(db, user_id)
    assert old_active_id in {task.id for task in rows}


def test_v1_retry_route():
    a = _register(f"v1retry-{uuid.uuid4().hex[:10]}@example.com")
    a_id = a["json"]["user"]["id"]
    csrf = a["json"]["csrf_token"]
    ac = a["client"]
    failed = _create_task(a_id, status="failed", progress=0)

    ok = ac.post(f"/api/v1/tasks/{failed}/retry", headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "pending"

    # Someone else's task is a 404 from this user's seat.
    b = _register(f"v1retry-b-{uuid.uuid4().hex[:10]}@example.com")
    foreign = _create_task(b["json"]["user"]["id"], status="failed")
    denied = ac.post(f"/api/v1/tasks/{foreign}/retry", headers={"X-CSRF-Token": csrf})
    assert denied.status_code == 404

    # A non-terminal task cannot be retried.
    active = _create_task(a_id, status="running")
    refused = ac.post(f"/api/v1/tasks/{active}/retry", headers={"X-CSRF-Token": csrf})
    assert refused.status_code == 409
