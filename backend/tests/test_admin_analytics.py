from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core import observability
from backend.main import app
from backend.platform import metrics_sampler
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import QuotaTransaction, SystemMetricSample, Task, User, utcnow
from backend.services.admin_analytics import metrics_history


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


def _register(client: TestClient, *, admin: bool = False) -> tuple[str, str, str]:
    username = f"user{uuid.uuid4().hex[:12]}"
    response = client.post("/api/auth/register", json={
        "email": f"{uuid.uuid4()}@example.test", "username": username, "password": "test-pass-1234"})
    assert response.status_code == 201, response.text
    body = response.json()
    if admin:
        with SessionLocal.begin() as db:
            db.get(User, body["user"]["id"]).role = "admin"
    return body["csrf_token"], body["user"]["id"], username


def _project_task(client: TestClient, csrf: str, status: str = "pending") -> str:
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": f"analytics-{uuid.uuid4().hex[:6]}"})
    assert project.status_code == 201, project.text
    response = client.post("/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json={
        "project_id": project.json()["id"], "task_type": "text.format", "payload": {}, "idempotency_key": str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    if status != "pending":
        with SessionLocal.begin() as db:
            db.get(Task, task_id).status = status
    return task_id


@pytest.fixture
def quiet_probes(monkeypatch):
    monkeypatch.setattr(metrics_sampler, "queue_status", lambda **_: {"available": True, "length": 3, "pending": 1})
    monkeypatch.setattr(metrics_sampler, "gpu_status", lambda: [])
    monkeypatch.setattr(metrics_sampler, "host_metrics", lambda *_, **__: {"cpu_percent": 12.5})


def test_sampler_writes_one_frame_per_bucket_and_prunes_old_frames(quiet_probes):
    initialize_schema()
    moment = utcnow() + timedelta(days=400)  # a bucket no other test touches
    assert metrics_sampler.sample_once(moment) is True
    assert metrics_sampler.sample_once(moment + timedelta(seconds=1)) is False
    with SessionLocal() as db:
        row = db.get(SystemMetricSample, metrics_sampler.sample_bucket(moment))
        assert row is not None
        assert set(row.data["tasks"]) >= {"llm", "tts", "audio", "system"}
        assert row.data["redis"]["length"] == 3
        assert row.data["host"]["cpu_percent"] == 12.5
    assert metrics_sampler.prune(moment + timedelta(days=8)) >= 1
    with SessionLocal() as db:
        assert db.get(SystemMetricSample, metrics_sampler.sample_bucket(moment)) is None


def test_history_converts_database_counters_into_rates_and_skips_resets():
    initialize_schema()
    base = utcnow() + timedelta(days=500)
    frames = [
        {"xact_commit": 100, "tup_inserted": 10, "tup_deleted": 0, "blks_hit": 90, "blks_read": 10},
        {"xact_commit": 160, "tup_inserted": 40, "tup_deleted": 6, "blks_hit": 180, "blks_read": 20},
        {"xact_commit": 5, "tup_inserted": 1, "tup_deleted": 0, "blks_hit": 1, "blks_read": 0},  # stats reset
    ]
    with SessionLocal.begin() as db:
        for index, counters in enumerate(frames):
            moment = base + timedelta(seconds=30 * index)
            db.add(SystemMetricSample(bucket=metrics_sampler.sample_bucket(moment), sampled_at=moment, data={
                "tasks": {"tts": {"running": index, "queued": 2}},
                "workers": {"active_slots": 1, "total_slots": 4, "online_workers": 2, **({"llm_gate_active": index} if index else {})},
                "db": {"counters": counters, "connections": {"active": 2, "idle": 3, "total": 5, "max": 100}},
            }))
    with SessionLocal() as db:
        history = metrics_history(db, "1h", now=base + timedelta(seconds=61))
    points = [point for point in history["points"] if "tts_running" in point]
    assert history["step_seconds"] == 30 and len(points) == 3
    assert "db_commit_ps" not in points[0]
    assert "llm_gate_active" not in points[0] and points[1]["llm_gate_active"] == 1  # old samples stay absent, never drawn as 0
    assert points[1]["db_commit_ps"] == 2.0 and points[1]["db_inserted_ps"] == 1.0 and points[1]["db_deleted_ps"] == 0.2
    assert points[1]["db_cache_hit_percent"] == 90.0
    assert "db_commit_ps" not in points[2]
    assert points[2]["db_conn_total"] == 5 and points[2]["slots_total"] == 4


def test_throughput_reports_task_buckets_and_model_characters(client: TestClient):
    csrf, user_id, _ = _register(client, admin=True)
    task_id = _project_task(client, csrf)
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        task.status = "succeeded"
        task.started_at = task.created_at + timedelta(seconds=2)
        task.finished_at = task.created_at + timedelta(seconds=12)
        for resource, chars in (("TTS", 1200), ("LLM", 300)):
            db.add(QuotaTransaction(user_id=user_id, task_id=task_id, amount=chars, kind="consume", resource_type=resource,
                                    char_count=chars, idempotency_key=f"analytics-{uuid.uuid4()}"))
    response = client.get("/api/v1/admin/analytics/throughput?range=24h&tz_offset_minutes=-480")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["points"]) in {24, 25}
    assert body["totals"]["tts_chars"] >= 1200 and body["totals"]["llm_chars"] >= 300
    assert sum(point["tts_chars"] for point in body["points"]) == body["totals"]["tts_chars"]
    text_format = next(row for row in body["by_type"] if row["task_type"] == "text.format")
    assert text_format["group"] == "system" and text_format["succeeded"] >= 1
    assert len(body["heatmap"]) == 7 and all(len(row) == 24 for row in body["heatmap"])

    detail = client.get(f"/api/v1/admin/users/{user_id}")
    assert detail.status_code == 200, detail.text
    assert sum(day["tts_chars"] for day in detail.json()["daily_usage"]) == 1200
    ledger = client.get(f"/api/v1/admin/users/{user_id}/quota-transactions?page=1&page_size=1")
    assert ledger.status_code == 200
    assert ledger.json()["pagination"]["total"] >= 2 and len(ledger.json()["items"]) == 1


def test_admin_provisions_users_and_rejects_duplicates(client: TestClient):
    csrf, _, existing = _register(client, admin=True)
    username = f"made{uuid.uuid4().hex[:10]}"
    payload = {"email": f"{username}@example.test", "username": username, "password": "secret-123", "role": "user"}
    assert client.post("/api/v1/admin/users", json=payload).status_code == 403  # CSRF required
    created = client.post("/api/v1/admin/users", headers={"X-CSRF-Token": csrf}, json=payload)
    assert created.status_code == 201, created.text
    assert created.json()["username"] == username
    duplicate = client.post("/api/v1/admin/users", headers={"X-CSRF-Token": csrf},
                            json={**payload, "email": f"other-{username}@example.test"})
    assert duplicate.status_code == 409
    invalid = client.post("/api/v1/admin/users", headers={"X-CSRF-Token": csrf},
                          json={**payload, "username": f"x{uuid.uuid4().hex[:8]}", "email": "bad"})
    assert invalid.status_code == 422
    with TestClient(app) as other:
        assert other.post("/api/auth/login", json={"identifier": username, "password": "secret-123"}).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(User).where(User.username == existing)) is not None


def test_regular_users_cannot_provision_or_revoke(client: TestClient):
    with TestClient(app) as other:
        csrf, user_id, _ = _register(other)
        assert other.post("/api/v1/admin/users", headers={"X-CSRF-Token": csrf}, json={}).status_code in {403, 422}
        assert other.post(f"/api/v1/admin/users/{user_id}/sessions/revoke", headers={"X-CSRF-Token": csrf}).status_code == 403


def test_force_sign_out_revokes_every_session_of_the_user(client: TestClient):
    csrf, _, _ = _register(client, admin=True)
    with TestClient(app) as other:
        _, user_id, _ = _register(other)
        assert other.get("/api/auth/me").status_code == 200
        response = client.post(f"/api/v1/admin/users/{user_id}/sessions/revoke", headers={"X-CSRF-Token": csrf})
        assert response.status_code == 200, response.text
        assert response.json()["revoked"] == 1
        assert other.get("/api/auth/me").status_code == 401
    assert client.get(f"/api/v1/admin/users/{user_id}").json()["sessions"]["active"] == 0


def test_task_detail_and_bulk_actions_report_each_outcome(client: TestClient):
    csrf, _, _ = _register(client, admin=True)
    failed = _project_task(client, csrf, "failed")
    running = _project_task(client, csrf)
    detail = client.get(f"/api/v1/admin/tasks/{failed}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["group"] == "system" and isinstance(detail.json()["events"], list)
    assert client.get(f"/api/v1/admin/tasks/{uuid.uuid4()}").status_code == 404

    retried = client.post("/api/v1/admin/tasks/bulk", headers={"X-CSRF-Token": csrf},
                          json={"action": "retry", "ids": [failed, running, "missing"]})
    assert retried.status_code == 200, retried.text
    outcomes = {row["id"]: row for row in retried.json()["results"]}
    assert outcomes[failed]["ok"] and outcomes[failed]["status"] == "pending"
    assert not outcomes[running]["ok"] and not outcomes["missing"]["ok"]

    cancelled = client.post("/api/v1/admin/tasks/bulk", headers={"X-CSRF-Token": csrf},
                            json={"action": "cancel", "ids": [running]})
    assert cancelled.json()["succeeded"] == 1
    filtered = client.get(f"/api/v1/admin/tasks?page=1&group=system&task_type=text.format&search={running}")
    assert [row["id"] for row in filtered.json()["items"]] == [running]
    assert client.get(f"/api/v1/admin/tasks?page=1&group=tts&search={running}").json()["items"] == []


def test_event_stats_and_csv_export_follow_the_same_filters(client: TestClient):
    csrf, _, _ = _register(client, admin=True)
    _project_task(client, csrf, "failed")
    stats = client.get("/api/v1/admin/events/stats?since_hours=24")
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body["total"] == sum(point["error"] + point["info"] for point in body["points"])
    assert any(row["module"] == "system" for row in body["modules"])
    export = client.get("/api/v1/admin/events/export?since_hours=24&level=error")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    lines = export.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("时间,级别") and all(",error," in line for line in lines[1:])
    audit_id = next(row["id"] for row in client.get("/api/v1/admin/events?page=1&level=info").json()["items"])
    assert client.get(f"/api/v1/admin/events/audit/{audit_id}").json()["action"]


def test_api_timeseries_buckets_latency_and_errors(monkeypatch):
    monkeypatch.setattr(observability, "_minute_stats", {})
    for duration in (4, 20, 30, 800):
        observability.record_api_request("/api/test/series", 200, float(duration), "POST")
    observability.record_api_request("/api/test/series", 503, 12.0, "POST")
    series = observability.api_timeseries(60)
    assert series["step_minutes"] == 1 and len(series["points"]) == 60
    last = series["points"][-1]
    assert last["requests"] == 5 and last["errors"] == 1
    assert last["average_ms"] == pytest.approx(173.2)
    assert last["p95_ms"] == 1000.0


def test_overview_and_performance_expose_real_counters(client: TestClient):
    _register(client, admin=True)
    overview = client.get("/api/v1/admin/overview").json()
    assert {"tts_chars", "llm_chars", "submitted"} <= set(overview["today"])
    performance = client.get("/api/v1/admin/performance").json()
    assert "unavailable_metrics" not in performance
    assert {"api_pool", "database", "llm_limit", "llm_task_limit"} <= set(performance)
    users = client.get("/api/v1/admin/users?page=1").json()
    assert {"new_7d", "active_15m", "admins", "enabled"} <= set(users["pagination"]["counts"])


def test_live_worker_pool_sums_llm_gate_permits_held_by_live_workers():
    from backend.platform.models import WorkerHeartbeat
    from backend.platform.worker_registry import live_worker_pool

    initialize_schema()
    now = utcnow()
    with SessionLocal.begin() as db:
        db.query(WorkerHeartbeat).delete()
        for index, held in enumerate((2, 3, 5)):
            db.add(WorkerHeartbeat(worker_id=f"gate-w{index}", status="idle", capabilities={"slots": 1, "llm_gate_active": held},
                                   started_at=now, last_seen_at=now - timedelta(days=1 if held == 5 else 0), updated_at=now))
    with SessionLocal() as db:
        pool, _ = live_worker_pool(db)
        assert pool["llm_gate_active"] == 5  # the stale worker's permits are not counted
        db.query(WorkerHeartbeat).delete()
        db.commit()


def test_tts_batch_activity_counts_members_not_pools(client, quiet_probes):
    from backend.platform.models import Project
    from backend.platform.tts_resource_budget import tts_batch_activity

    _, owner_id, _ = _register(client)
    with SessionLocal() as db:
        project_id = db.scalar(select(Project.id).where(Project.owner_id == owner_id))
        before = tts_batch_activity(db)

    def add(db, status, slot=None, parked=False):
        db.add(Task(id=str(uuid.uuid4()), owner_id=owner_id, project_id=project_id, task_type="tts.batch", status=status,
                    payload={}, ui_state={"tts_slot": slot, "tts_parked": parked} if slot else {}))
    with SessionLocal.begin() as db:
        for _ in range(5): add(db, "running", "pool-a")        # one executing pool of 5 chapters
        for _ in range(3): add(db, "running", "pool-b", True)  # a parked pool keeps leases, not the slot
        for _ in range(4): add(db, "pending")
    with SessionLocal() as db:
        after = tts_batch_activity(db)
        frame = metrics_sampler.collect_frame(db)
    delta = {key: after[key] - before[key] for key in after}
    assert delta == {"pools": 1, "active_members": 5, "parked_pools": 1, "parked_members": 3, "queued_members": 4}
    assert frame["tts_batch"] == after
    point = metrics_history.__globals__["_flatten_frame"](frame)
    assert point["tts_batch_active"] == after["active_members"] and point["tts_batch_queued"] == after["queued_members"]
    assert "tts_batch_pools" not in metrics_history.__globals__["_flatten_frame"]({"tasks": {}})  # old samples stay absent


def test_daily_labels_point_at_local_midnight_in_utc(client: TestClient):
    from backend.platform.models import QuotaTransaction
    from backend.services.admin_analytics import user_daily_usage

    _, user_id, _ = _register(client)
    now = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)  # 11:00 on 2026-10-09 at UTC+8
    with SessionLocal.begin() as db:
        db.add(QuotaTransaction(user_id=user_id, amount=7, kind="consume", resource_type="TTS", operation_type="tts.batch",
                                char_count=7, idempotency_key=str(uuid.uuid4()), note="label", created_at=now - timedelta(hours=1),
                                available_after=1))
    with SessionLocal() as db:
        rows = user_daily_usage(db, user_id, 3, -480, now=now)  # what the console sends at UTC+8
    today = next(row for row in rows if row["tts_chars"] == 7)
    # local midnight 2026-10-09 at UTC+8 is 2026-10-08T16:00Z
    assert today["time"] == "2026-10-08T16:00:00+00:00"


def test_csv_cells_neutralise_spreadsheet_formulas():
    from backend.api.admin import _csv_cell

    assert _csv_cell("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    assert _csv_cell("+1") == "'+1" and _csv_cell("@cmd") == "'@cmd" and _csv_cell("-2") == "'-2"
    assert _csv_cell("普通摘要") == "普通摘要" and _csv_cell(None) == ""


def test_long_range_history_keeps_exact_rates_from_strided_frames():
    initialize_schema()
    base = utcnow() + timedelta(days=501)
    with SessionLocal.begin() as db:
        for index in range(0, 2 * 3600 // 30 + 1):  # two hours of 30-second frames
            moment = base + timedelta(seconds=30 * index)
            db.add(SystemMetricSample(bucket=metrics_sampler.sample_bucket(moment), sampled_at=moment, data={
                "tasks": {"tts": {"running": index % 5, "queued": 0}},
                "workers": {"active_slots": 1, "total_slots": 4, "online_workers": 2},
                "db": {"counters": {"xact_commit": 1000 + 10 * 30 * index, "tup_inserted": 0, "tup_deleted": 0,
                                    "blks_hit": 0, "blks_read": 0}, "connections": {"total": 5}},
            }))
    with SessionLocal() as db:
        history = metrics_history(db, "7d", now=base + timedelta(hours=2))
    rated = [point for point in history["points"] if "db_commit_ps" in point]
    assert rated and history["step_seconds"] == 3600
    # each frame adds 300 commits over 30 seconds: 10 per second, however many frames are kept
    assert all(abs(point["db_commit_ps"] - 10.0) < 1e-6 for point in rated)


def test_heatmap_counts_local_hour_and_weekday_at_utc_plus_8(client: TestClient):
    from backend.services.admin_analytics import throughput

    csrf, _, _ = _register(client)
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    with SessionLocal() as db:
        before = throughput(db, "24h", -480, now=now)["heatmap"]
    task_id = _project_task(client, csrf)
    with SessionLocal.begin() as db:
        # 02:30 UTC on Thursday 2026-10-08 is 10:30 local at UTC+8
        db.get(Task, task_id).created_at = datetime(2026, 10, 8, 2, 30, tzinfo=timezone.utc)
    with SessionLocal() as db:
        after = throughput(db, "24h", -480, now=now)["heatmap"]
    assert after[3][10] == before[3][10] + 1  # Thursday (weekday 3), local hour 10
    assert after[3][2] == before[3][2]  # not the UTC hour
