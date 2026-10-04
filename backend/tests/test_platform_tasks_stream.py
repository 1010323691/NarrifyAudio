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
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

pytest.importorskip("sqlalchemy")

from backend.main import app
from backend.platform.platform_settings import settings
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import Project, Task, TaskEvent, TaskResult, UserSession, utcnow
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


def _commit_parse_completion(task_id: str) -> None:
    """Commit the terminal row, result and event together, like the worker."""
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        task.status = "succeeded"
        task.progress = 100
        task.finished_at = utcnow()
        db.add(TaskResult(task_id=task_id, result={"name": "parsed.json"}))
        db.add(TaskEvent(task_id=task_id, sequence=2, event_type="succeeded", payload={}))


@pytest.mark.parametrize("initial_replay", [False, True])
def test_completion_between_task_and_event_reads_uses_fresh_snapshot(initial_replay):
    task_id = _create_task(str(uuid.uuid4()), task_type="script.parse", progress=100)
    _add_event(task_id, 1, "progress", {"current": "完成"})

    def racing_rows(db):
        rows = db.scalars(select(Task).where(Task.id == task_id)).all()
        assert rows[0].status == "running"
        # Also cache the absent result: the completion snapshot must refresh it.
        assert rows[0].result is None
        _commit_parse_completion(task_id)
        return rows

    def rows_fn(db):
        return db.scalars(select(Task).where(Task.id == task_id)).all()

    if initial_replay:
        snapshots, seen, _, delivered = task_views.snapshot_payload(racing_rows)
        snapshot = snapshots[0]
    else:
        seen, delivered = {task_id: 1}, {task_id: "running"}
        frames = task_views._new_frames(racing_rows, seen, delivered)
        frame = next(frame for frame in frames if frame["type"] == "status")
        assert frame["status"] == "succeeded"
        snapshot = frame["task"]
    assert snapshot["status"] == "succeeded"
    assert snapshot["progress"] == 1.0
    assert snapshot["current"] == "完成"
    assert snapshot["result"] == {"name": "parsed.json"}
    assert snapshot["finished"] > 0
    assert seen[task_id] == 2
    assert delivered[task_id] == "succeeded"
    assert task_views._new_frames(rows_fn, seen, delivered) == []


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "paused", "retrying"])
def test_poll_reconciles_status_even_when_transition_event_was_consumed(status):
    task_id = _create_task(str(uuid.uuid4()), status=status)
    _add_event(task_id, 1, "log", {"msg": "already consumed"})
    seen, delivered = {task_id: 1}, {task_id: "running"}

    def rows_fn(db):
        return db.scalars(select(Task).where(Task.id == task_id)).all()

    frames = task_views._new_frames(rows_fn, seen, delivered)
    assert len(frames) == 1
    assert frames[0]["type"] == "status"
    assert frames[0]["status"] == task_views.legacy_status(status)
    assert frames[0]["task"]["status"] == frames[0]["status"]
    assert task_views._new_frames(rows_fn, seen, delivered) == []


def test_initial_snapshot_completion_after_cursor_read_is_replayed(monkeypatch):
    task_id = _create_task(str(uuid.uuid4()), task_type="script.parse", progress=100)
    _add_event(task_id, 1, "progress", {"current": "完成"})
    original_events = task_views.task_events
    committed = False

    def racing_events(db, current_id):
        nonlocal committed
        events = original_events(db, current_id)
        if not committed:
            committed = True
            _commit_parse_completion(task_id)
        return events

    def rows_fn(db):
        return db.scalars(select(Task).where(Task.id == task_id)).all()

    monkeypatch.setattr(task_views, "task_events", racing_events)
    snapshots, seen, _, delivered = task_views.snapshot_payload(rows_fn)
    assert snapshots[0]["status"] == "succeeded"
    assert seen[task_id] == 1
    frames = task_views._new_frames(rows_fn, seen, delivered)
    assert len(frames) == 1
    assert frames[0]["task"]["status"] == "succeeded"
    assert seen[task_id] == 2
    assert task_views._new_frames(rows_fn, seen, delivered) == []


def test_old_terminal_event_replay_uses_current_retry_status():
    task_id = _create_task(str(uuid.uuid4()), status="running")
    _add_event(task_id, 1, "succeeded", {})
    seen, delivered = {}, {}

    def rows_fn(db):
        return db.scalars(select(Task).where(Task.id == task_id)).all()

    frames = task_views._new_frames(rows_fn, seen, delivered)
    assert len(frames) == 1
    assert frames[0]["status"] == frames[0]["task"]["status"] == "running"
    assert delivered[task_id] == "running"


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


def _parse_entry_task(user_id: str, project_id: str, source: str, status: str, created_at: datetime) -> str:
    task_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(Task(
            id=task_id, owner_id=user_id, project_id=project_id,
            task_type="script.parse", status=status, progress=100 if status == "succeeded" else 0,
            payload={"source_name": source, "label": f"文本解析（{source}）"},
            created_at=created_at, updated_at=created_at,
        ))
        db.commit()
    return task_id


def test_superseded_frame_emitted_when_rerun_replaces_old_row():
    """A re-run of the same entry hides the old terminal row in the view; the
    long-lived stream must tell the client (a ``superseded`` frame) so the
    完成/失败 row is replaced by the new 进行中 row instead of sitting next to it."""
    a = _register(f"v1superseded-{uuid.uuid4().hex[:10]}@example.com")
    uid = a["json"]["user"]["id"]
    token = a["cookie"]
    project_id = str(uuid.uuid4())
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_id = _parse_entry_task(uid, project_id, "第018章 庆典.txt", "succeeded", base)

    state = {"rows": [old_id], "disconnected": False}

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

        snap = await take()
        assert snap["type"] == "snapshot_all"
        assert {t["id"] for t in snap["tasks"]} == {old_id}
        # The user re-runs the entry: a new queued run appears and the old
        # completed row drops out of the (entry-deduplicated) view.
        new_id = _parse_entry_task(uid, project_id, "第018章 庆典.txt", "queued", base + timedelta(hours=1))
        _add_event(new_id, 1, "submitted", {})
        state["rows"] = [new_id]
        got: dict[str, dict] = {}
        for _ in range(30):
            frame = await take()
            if frame.get("type") == "superseded" and frame.get("task_id") == old_id:
                got["superseded"] = frame
            if frame.get("type") == "snapshot" and frame.get("task_id") == new_id:
                got["snapshot"] = frame
            if len(got) == 2:
                break
        assert "superseded" in got, f"the old row was never told to be dropped: {frames}"
        assert "snapshot" in got, f"the new run was never delivered: {frames}"
        assert got["snapshot"]["task"]["status"] == "pending"
        state["disconnected"] = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), 3.0)

    asyncio.run(drive())


def test_window_exit_without_rerun_emits_no_superseded():
    """A terminal row that simply ages out of the live window (no re-run of the
    entry) is pruned silently — it is still part of the /history record, so a
    ``superseded`` frame for it would make it vanish from the centre."""
    a = _register(f"v1prune-{uuid.uuid4().hex[:10]}@example.com")
    uid = a["json"]["user"]["id"]
    token = a["cookie"]
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_id = _parse_entry_task(uid, str(uuid.uuid4()), "第018章 庆典.txt", "succeeded", base)

    state = {"rows": [old_id], "disconnected": False}

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

        snap = await take()
        assert snap["type"] == "snapshot_all"
        assert {t["id"] for t in snap["tasks"]} == {old_id}
        state["rows"] = []  # the row ages out of the live window, nothing re-runs
        for _ in range(8):
            await take()
        state["disconnected"] = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), 3.0)
        assert not any(f["type"] == "superseded" for f in frames), \
            f"an aged-out row must not be reported as superseded: {[f for f in frames if f['type'] != 'ping']}"

    asyncio.run(drive())


def _mark_succeeded(task_id: str) -> None:
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        task.status = "succeeded"
        task.progress = 100
        task.finished_at = datetime.now(timezone.utc)


def test_window_exit_delivers_unseen_terminal_transition_once():
    """A batch can finish past the newest-200 live window: the terminal
    transition can land in the same poll tick the row leaves the window, so
    no in-row ``status`` frame can carry it — the prune must deliver ONE
    catch-up ``status`` frame instead (otherwise the client keeps the row
    进行中 until a full reload), and must NOT repeat the terminal status for a
    row the client already saw complete inside the window."""
    a = _register(f"v1catchup-{uuid.uuid4().hex[:10]}@example.com")
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

        snap = await take()
        assert snap["type"] == "snapshot_all"
        assert {t["id"] for t in snap["tasks"]} == {ta, tb}
        # ta completes while still in the window: its status frame delivers in row
        _mark_succeeded(ta)
        _add_event(ta, 2, "succeeded", {})
        ta_frame = None
        for _ in range(30):
            frame = await take()
            if frame.get("type") == "status" and frame.get("task_id") == ta:
                ta_frame = frame
                break
        assert ta_frame is not None, f"ta's in-window status frame never arrived: {frames}"
        assert ta_frame["status"] == "succeeded"
        # Both rows leave the window; tb's terminal transition lands with its
        # exit (no in-row frame possible), ta's status is already known to the
        # client and must not be repeated.
        state["rows"] = []
        _mark_succeeded(tb)
        _add_event(tb, 2, "succeeded", {})
        status_counts = {ta: 1, tb: 0}
        tb_frame = None
        for _ in range(30):
            frame = await take()
            if frame.get("type") == "status" and frame.get("task_id") in (ta, tb):
                status_counts[frame["task_id"]] += 1
                if frame["task_id"] == tb:
                    tb_frame = frame
        assert status_counts[ta] == 1, \
            f"ta's already-delivered terminal status was repeated at prune: {frames}"
        assert status_counts[tb] == 1, \
            f"tb's unseen terminal transition was not delivered exactly once: {frames}"
        assert tb_frame is not None and tb_frame["status"] == "succeeded"
        assert tb_frame["task"]["status"] == "succeeded"
        assert tb_frame["task"]["progress"] == 1.0
        state["disconnected"] = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), 3.0)

    asyncio.run(drive())


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


def test_user_task_views_hide_trashed_project_rows_but_keep_orphan_rows(client):
    account = _register(f"trash-task-view-{uuid.uuid4().hex[:10]}@example.com")
    user_id = account["json"]["user"]["id"]
    trashed_project_id = str(uuid.uuid4())
    orphan_project_id = str(uuid.uuid4())
    with SessionLocal.begin() as db:
        db.add(Project(
            id=trashed_project_id,
            owner_id=user_id,
            name="待清理项目",
            directory_key=f"trash-task-view/{trashed_project_id}",
            deleted_at=utcnow(),
        ))
        trashed_task = Task(
            owner_id=user_id,
            project_id=trashed_project_id,
            task_type="text.format",
            status="succeeded",
            progress=100,
            payload={},
        )
        orphan_task = Task(
            owner_id=user_id,
            project_id=orphan_project_id,
            task_type="text.format",
            status="succeeded",
            progress=100,
            payload={},
        )
        db.add_all([trashed_task, orphan_task])

    from backend.api.platform_tasks import _user_tasks

    with SessionLocal() as db:
        stream_rows = {task.id for task in _user_tasks(db, user_id)}
    listed_rows = {task["id"] for task in account["client"].get("/api/v1/tasks").json()}
    assert trashed_task.id not in stream_rows | listed_rows
    assert orphan_task.id in stream_rows & listed_rows


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


def test_task_history_hides_cancelled_by_default(client):
    # Bulk cancels leave hundreds of identical terminal rows; the newest-first
    # history must not let them push completed work out of the visible pages.
    first = _register(f"hist-cancel-{uuid.uuid4().hex[:10]}@example.com")
    user_id = first["json"]["user"]["id"]
    with SessionLocal() as db:
        db.add_all([
            Task(
                id=str(uuid.uuid4()),
                owner_id=user_id,
                project_id=str(uuid.uuid4()),
                task_type="script.parse",
                status="cancelled",
                progress=100,
                payload={},
            )
            for _ in range(60)
        ])
        succeeded = Task(
            id=str(uuid.uuid4()),
            owner_id=user_id,
            project_id=str(uuid.uuid4()),
            task_type="script.parse",
            status="succeeded",
            progress=100,
            payload={"label": "已完成的任务"},
        )
        db.add(succeeded)
        db.commit()

    page = first["client"].get("/api/v1/tasks/history").json()
    assert len(page["items"]) == 1
    assert page["items"][0]["id"] == succeeded.id
    assert page["items"][0]["status"] == "succeeded"

    full = first["client"].get("/api/v1/tasks/history", params={"include_cancelled": "true"}).json()
    assert len(full["items"]) == 50
    assert full["next_cursor"]  # 61 rows total, so the full record still paginates


def _user_task_rows(user_id: str) -> list[dict]:
    with SessionLocal() as db:
        return [
            {"id": task.id}
            for task in db.scalars(select(Task).where(Task.owner_id == user_id)).all()
        ]


def test_terminal_row_superseded_by_newer_run_is_hidden(client):
    # A rerun of the same entry (same project + type + subject) supersedes the
    # older terminal row: the centre shows one row per entry, not 失败 next to
    # its 进行中/已完成 replacement.
    first = _register(f"supersede-{uuid.uuid4().hex[:10]}@example.com")
    user_id = first["json"]["user"]["id"]
    project_id = str(uuid.uuid4())
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with SessionLocal() as db:
        failed_old = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="failed", progress=0,
            payload={"source_name": "第018章 庆典.txt"},
            created_at=base, updated_at=base,
        )
        running_new = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="running", progress=40,
            payload={"source_name": "第018章 庆典.txt"},
            created_at=base + timedelta(hours=1),
            updated_at=base + timedelta(hours=1),
        )
        other = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="succeeded", progress=100,
            payload={"source_name": "第030章 骑兵_一_.txt"},
            created_at=base + timedelta(hours=2),
            updated_at=base + timedelta(hours=2),
        )
        db.add_all([failed_old, running_new, other])
        db.commit()

    page = first["client"].get("/api/v1/tasks/history").json()
    ids = {item["id"] for item in page["items"]}
    assert running_new.id in ids, "the newer run of the entry must be visible"
    assert other.id in ids, "an entry without a newer run must stay"
    assert failed_old.id not in ids, "the failed row must be superseded by its rerun"

    # The SSE live window applies the same rule.
    from backend.api.platform_tasks import _user_tasks

    with SessionLocal() as db:
        window = {task.id for task in _user_tasks(db, user_id)}
    assert failed_old.id not in window
    assert running_new.id in window and other.id in window

    # GET /api/v1/tasks (durable list surface) matches too.
    listed = first["client"].get("/api/v1/tasks").json()
    listed_ids = {task["id"] for task in listed}
    assert failed_old.id not in listed_ids and running_new.id in listed_ids


def test_active_rows_are_never_superseded_and_cross_page_hide_applies(client):
    first = _register(f"supersede2-{uuid.uuid4().hex[:10]}@example.com")
    user_id = first["json"]["user"]["id"]
    project_id = str(uuid.uuid4())
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with SessionLocal() as db:
        # Two ACTIVE rows for the same entry both stay visible: real work is
        # still in flight, so neither supersedes the other.
        active_a = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="running", progress=10,
            payload={"source_name": "甲.txt"}, created_at=base, updated_at=base,
        )
        active_b = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="pending", progress=0,
            payload={"source_name": "甲.txt"}, created_at=base + timedelta(hours=1),
            updated_at=base + timedelta(hours=1),
        )
        # A failed row whose newer sibling (same entry) is on page 1: the older
        # row sits on page 2 and must still be hidden there (cross-page).
        failed_old = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="failed", progress=0,
            payload={"source_name": "乙.txt"}, created_at=base, updated_at=base,
        )
        succeeded_new = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="succeeded", progress=100,
            payload={"source_name": "乙.txt"},
            created_at=base + timedelta(hours=2), updated_at=base + timedelta(hours=2),
        )
        db.add_all([failed_old, succeeded_new, active_a, active_b])
        db.add_all([
            # Distinct entries (their own source_name each): whole-book types
            # like text.format would collapse to a single row, so the fillers
            # must be per-entry rows to keep history paginating.
            Task(
                owner_id=user_id, project_id=project_id, task_type="script.parse",
                status="succeeded", progress=100,
                payload={"source_name": f"filler-{index:02d}.txt"},
                # Older than every subject row above: the special rows all land
                # on page 1, the rest of the fillers on page 2.
                created_at=base - timedelta(minutes=30 * index),
                updated_at=base - timedelta(minutes=30 * index),
            )
            for index in range(1, 61)  # filler rows making history paginate
        ])
        db.commit()

    page_one = first["client"].get("/api/v1/tasks/history").json()
    assert active_a.id in {item["id"] for item in page_one["items"]}
    assert active_b.id in {item["id"] for item in page_one["items"]}
    assert failed_old.id not in {item["id"] for item in page_one["items"]}
    cursor = page_one["next_cursor"]
    assert cursor, "enough rows must exist to need page 2"
    page_two = first["client"].get("/api/v1/tasks/history", params={"cursor": cursor}).json()
    assert failed_old.id not in {item["id"] for item in page_two["items"]}, (
        "the superseded row must stay hidden even when its newer sibling is on an earlier page"
    )


def test_display_label_collision_does_not_supersede(client):
    # Entry identity comes from stable payload keys, never from the display
    # label: two tts.reset runs whose files DIFFER but whose (legacy-style)
    # labels would be identical (same count) are different entries. A re-run
    # of the same source_name still supersedes.
    first = _register(f"supersede3-{uuid.uuid4().hex[:10]}@example.com")
    user_id = first["json"]["user"]["id"]
    project_id = str(uuid.uuid4())
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with SessionLocal() as db:
        reset_old = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="tts.reset", status="succeeded", progress=100,
            payload={"label": "重新合成：2 个文件", "scripts": ["a.json", "b.json"]},
            created_at=base, updated_at=base,
        )
        reset_new = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="tts.reset", status="succeeded", progress=100,
            payload={"label": "重新合成：2 个文件", "scripts": ["c.json", "d.json"]},
            created_at=base + timedelta(hours=1), updated_at=base + timedelta(hours=1),
        )
        parse_old = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="failed", progress=0,
            payload={"source_name": "第018章 庆典.txt"},
            created_at=base, updated_at=base,
        )
        parse_new = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="running", progress=40,
            payload={"source_name": "第018章 庆典.txt"},
            created_at=base + timedelta(hours=2), updated_at=base + timedelta(hours=2),
        )
        db.add_all([reset_old, reset_new, parse_old, parse_new])
        db.commit()

    page = first["client"].get("/api/v1/tasks/history").json()
    ids = {item["id"] for item in page["items"]}
    assert reset_old.id in ids, "a label collision must not hide the older, different entry"
    assert reset_new.id in ids
    assert parse_new.id in ids
    assert parse_old.id not in ids, "a re-run of the same source_name still supersedes"


def test_rows_without_identity_values_are_never_superseded(client):
    # Legacy rows carry no identity payload (no source_name): they have no
    # subject, so nothing may hide them — not a newer row that DOES name its
    # entry, not an identically unsubjected newer row.
    first = _register(f"supersede4-{uuid.uuid4().hex[:10]}@example.com")
    user_id = first["json"]["user"]["id"]
    project_id = str(uuid.uuid4())
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with SessionLocal() as db:
        legacy_old = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="succeeded", progress=100,
            payload={}, created_at=base, updated_at=base,
        )
        legacy_new = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="succeeded", progress=100,
            payload={}, created_at=base + timedelta(hours=1),
            updated_at=base + timedelta(hours=1),
        )
        named_new = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="succeeded", progress=100,
            payload={"source_name": "第001章 序幕.txt"},
            created_at=base + timedelta(hours=2), updated_at=base + timedelta(hours=2),
        )
        db.add_all([legacy_old, legacy_new, named_new])
        db.commit()

    page = first["client"].get("/api/v1/tasks/history").json()
    ids = {item["id"] for item in page["items"]}
    assert {legacy_old.id, legacy_new.id, named_new.id} <= ids, (
        "rows whose identity keys are all absent must never be superseded"
    )


def test_cancelled_rerun_does_not_supersede_old_terminal_row(client):
    # A cancelled re-run is not a completed attempt: it must not hide the
    # last real record. Otherwise (with /history excluding cancelled rows)
    # the entry would vanish from the default history entirely.
    first = _register(f"supersede5-{uuid.uuid4().hex[:10]}@example.com")
    user_id = first["json"]["user"]["id"]
    project_id = str(uuid.uuid4())
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with SessionLocal() as db:
        succeeded_old = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="succeeded", progress=100,
            payload={"source_name": "第018章 庆典.txt"},
            created_at=base, updated_at=base,
        )
        cancelled_new = Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=project_id,
            task_type="script.parse", status="cancelled", progress=0,
            payload={"source_name": "第018章 庆典.txt"},
            created_at=base + timedelta(hours=1), updated_at=base + timedelta(hours=1),
        )
        db.add_all([succeeded_old, cancelled_new])
        db.commit()

    page = first["client"].get("/api/v1/tasks/history").json()
    ids = {item["id"] for item in page["items"]}
    assert succeeded_old.id in ids, "the entry must not vanish behind its cancelled re-run"
    assert cancelled_new.id not in ids  # the default cancelled filter still applies

    full = first["client"].get("/api/v1/tasks/history", params={"include_cancelled": "true"}).json()
    full_ids = {item["id"] for item in full["items"]}
    assert succeeded_old.id in full_ids and cancelled_new.id in full_ids, (
        "the full record keeps both rows of the entry"
    )


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


def test_superseded_frame_reaches_rows_beyond_the_live_window():
    """The re-run's predecessor can sit beyond the live window (active + newest
    200): the 任务中心 preloads up to 500 /history rows, so the old terminal
    row is a client-side record no stream ever tracked. It must still be named
    with a ``superseded`` frame — both when the re-run lands on a connected
    stream and when it is already part of the connect-time replay — or
    allTasks keeps the old 完成/失败 row next to the new run."""
    from backend.api.platform_tasks import _user_tasks

    a = _register(f"v1hist-{uuid.uuid4().hex[:10]}@example.com")
    uid = a["json"]["user"]["id"]
    token = a["cookie"]
    project_id = str(uuid.uuid4())
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_id = _parse_entry_task(uid, project_id, "第018章 庆典.txt", "succeeded", base)
    # 205 distinct-entry fillers push the old row out of the newest-200 window
    # (the /history endpoint still serves it — that is the point).
    with SessionLocal() as db:
        db.add_all([
            Task(
                id=str(uuid.uuid4()), owner_id=uid, project_id=project_id,
                task_type="script.parse", status="succeeded", progress=100,
                payload={"source_name": f"填充{index:03d}.txt",
                         "label": f"文本解析（填充{index:03d}.txt）"},
                created_at=base + timedelta(minutes=index),
                updated_at=base + timedelta(minutes=index),
            )
            for index in range(1, 206)
        ])
        db.commit()

    with SessionLocal() as db:
        assert old_id not in {task.id for task in _user_tasks(db, uid)}, \
            "the old row must be outside the live window for this test"
    ids: dict[str, str] = {}

    async def drive_live():
        # The re-run lands while connected: the fresh row's snapshot plus a
        # superseded frame for the untracked old row.
        state = {"disconnected": False}

        async def disconnected():
            return state["disconnected"]

        frames: list[dict] = []
        gen = task_views.aggregate_stream(lambda db: _user_tasks(db, uid), token, uid, disconnected)

        async def take():
            raw = await asyncio.wait_for(gen.__anext__(), 5.0)
            frames.append(json.loads(raw[len("data: "):]))
            return frames[-1]

        snap = await take()
        assert snap["type"] == "snapshot_all"
        assert old_id not in {t["id"] for t in snap["tasks"]}
        new_id = _parse_entry_task(uid, project_id, "第018章 庆典.txt", "queued", base + timedelta(days=1))
        ids["new"] = new_id
        _add_event(new_id, 1, "submitted", {})
        got: dict[str, dict] = {}
        for _ in range(40):
            frame = await take()
            if frame.get("type") == "superseded" and frame.get("task_id") == old_id:
                got["superseded"] = frame
            if frame.get("type") == "snapshot" and frame.get("task_id") == new_id:
                got["snapshot"] = frame
            if len(got) == 2:
                break
        assert "superseded" in got, \
            f"the untracked old row was never told to be dropped: {frames}"
        assert "snapshot" in got, f"the new run was never delivered: {frames}"
        state["disconnected"] = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), 5.0)

    asyncio.run(drive_live())

    # Reconnect with the re-run already present: the replay itself must name
    # the old row right after snapshot_all.
    async def drive_replay():
        state = {"disconnected": False}

        async def disconnected():
            return state["disconnected"]

        frames: list[dict] = []
        gen = task_views.aggregate_stream(lambda db: _user_tasks(db, uid), token, uid, disconnected)

        async def take():
            raw = await asyncio.wait_for(gen.__anext__(), 5.0)
            frames.append(json.loads(raw[len("data: "):]))
            return frames[-1]

        snap = await take()
        assert snap["type"] == "snapshot_all"
        snap_ids = {t["id"] for t in snap["tasks"]}
        assert ids["new"] in snap_ids and old_id not in snap_ids
        named = False
        for _ in range(10):
            frame = await take()
            if frame.get("type") == "superseded" and frame.get("task_id") == old_id:
                named = True
                break
        assert named, f"the replay did not name the untracked old row: {frames}"
        state["disconnected"] = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), 5.0)

    asyncio.run(drive_replay())
