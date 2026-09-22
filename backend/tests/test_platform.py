from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from backend.main import app
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import OutboxEvent, Task, TaskAttempt, User, UserQuotaAccount, utcnow
from backend.platform.storage import configured_storage_root
from backend.platform.task_worker import claim_task, heartbeat_claim, process_task_message, recover_database_tasks


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


def _register(client: TestClient, email: str) -> dict:
    response = client.post("/api/auth/register", json={"email": email, "password": "a-strong-test-password", "display_name": "Test User"})
    assert response.status_code == 201, response.text
    return response.json()


def test_session_cookie_and_project_scope(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    response = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Private book"})
    assert response.status_code == 201, response.text
    project = response.json()
    assert client.get("/api/v1/projects").json()[0]["id"] == project["id"]
    assert client.post("/api/v1/projects", json={"name": "No CSRF"}).status_code == 403

    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert client.get("/api/v1/projects").status_code == 401


def test_idempotent_task_submission_and_quota_guard(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Queue book"}).json()
    response = client.post("/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "estimated_units": 1, "idempotency_key": "request-123456"})
    assert response.status_code == 409

    # A zero-cost task can be submitted and a duplicate request returns the
    # same persisted row rather than creating a second business effect.
    payload = {"project_id": project["id"], "task_type": "text.format", "payload": {"value": 1}, "estimated_units": 0, "idempotency_key": "request-123457"}
    created = client.post("/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json=payload)
    duplicate = client.post("/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json=payload)
    assert created.status_code == 201
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == created.json()["id"]


def test_admin_cannot_disable_last_admin(client: TestClient):
    # The test account is a regular user, so it cannot cross the admin path.
    first = _register(client, f"{uuid.uuid4()}@example.com")
    assert client.get("/api/v1/admin/users").status_code == 403


def test_workspace_directory_is_user_scoped_and_admin_root_is_persistent(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    created = client.post("/api/v1/workspaces", headers={"X-CSRF-Token": csrf}, json={"name": "My workspace"})
    assert created.status_code == 201, created.text
    workspace = created.json()
    assert workspace["directory_key"].startswith(f"{first['user']['username']}/")
    with SessionLocal() as db:
        storage_root = configured_storage_root(db)
    assert (storage_root / workspace["directory_key"]).is_dir()

    with SessionLocal.begin() as db:
        user = db.get(User, first["user"]["id"])
        assert user is not None
        user.role = "admin"

    root = (Path(".narrify") / "admin-selected-root").resolve()
    updated = client.patch("/api/v1/admin/settings/storage", headers={"X-CSRF-Token": csrf}, json={"root_path": str(root)})
    assert updated.status_code == 200, updated.text
    assert Path(updated.json()["root_path"]) == root
    assert client.get("/api/v1/admin/settings/storage").json()["source"] == "admin"
    assert client.get("/api/v1/admin/tasks").status_code == 200
    assert client.get("/api/v1/admin/workers").status_code == 200
    assert client.get("/api/v1/admin/queue").status_code == 200


def test_legacy_workspace_is_managed_and_requires_authenticated_csrf(client: TestClient):
    anonymous = TestClient(app)
    assert anonymous.get("/api/workspace").status_code == 401

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    current = client.get("/api/workspace")
    assert current.status_code == 200, current.text
    info = current.json()
    assert info["workspace_id"]
    assert info["path"].split("\\")[-2] == first["user"]["username"]

    rejected = client.put(
        "/api/workspace",
        headers={"X-CSRF-Token": csrf},
        json={"path": str(Path.cwd())},
    )
    assert rejected.status_code == 400, rejected.text
    uploaded = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("legacy.txt", b"legacy content", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text


def test_durable_worker_formats_uploaded_file_and_settles_quota(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects",
        headers={"X-CSRF-Token": csrf},
        json={"name": "Worker book"},
    ).json()
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/files",
        headers={"X-CSRF-Token": csrf},
        files={"upload": ("chapter.txt", "  第一章  \n你好...\n".encode("utf-8"), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    input_file = uploaded.json()

    with SessionLocal.begin() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        account.available_units = 2

    submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "text.format",
            "payload": {"input_file_id": input_file["id"]},
            "estimated_units": 2,
            "idempotency_key": "worker-format-123",
        },
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]

    assert process_task_message({"payload": {"task_id": task_id}}, worker_id="test-worker") == "succeeded"
    result = client.get(f"/api/v1/tasks/{task_id}")
    assert result.status_code == 200, result.text
    task = result.json()
    assert task["status"] == "succeeded"
    assert task["progress"] == 100
    assert task["result"]["file_id"]
    downloaded = client.get(f"/api/v1/projects/{project['id']}/files/{task['result']['file_id']}")
    assert downloaded.status_code == 200
    assert "第一章" in downloaded.text
    events = client.get(f"/api/v1/tasks/{task_id}/events")
    assert events.status_code == 200
    assert "succeeded" in events.text
    quota = client.get("/api/v1/quota")
    assert quota.json()["consumed_units"] == 2

    with SessionLocal() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        assert account.available_units == 0
        assert account.reserved_units == 0
        assert account.consumed_units == 2
        attempts = db.query(TaskAttempt).filter(TaskAttempt.task_id == task_id).all()
        assert len(attempts) == 1
        assert attempts[0].status == "succeeded"


def test_cancel_before_claim_releases_quota(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Cancel book"}).json()
    with SessionLocal.begin() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        account.available_units = 3
    submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "estimated_units": 3, "idempotency_key": "cancel-before-claim"},
    )
    task_id = submitted.json()["id"]
    cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel", headers={"X-CSRF-Token": csrf})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    with SessionLocal() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        assert account.available_units == 3
        assert account.reserved_units == 0


def test_expired_worker_lease_is_fenced_and_recovery_requeues(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Recovery book"}).json()
    submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "estimated_units": 0, "idempotency_key": "recovery-lease-123"},
    )
    task_id = submitted.json()["id"]
    with SessionLocal.begin() as db:
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == task_id))
        assert event is not None
        event.published_at = utcnow()

    claim_one = claim_task(task_id, "worker-one", lease_seconds=1)
    assert claim_one is not None
    with SessionLocal.begin() as db:
        attempt = db.get(TaskAttempt, claim_one.attempt_id)
        assert attempt is not None
        attempt.lease_expires_at = utcnow()
    claim_two = claim_task(task_id, "worker-two", lease_seconds=60)
    assert claim_two is not None
    assert claim_two.attempt_no == 2
    assert heartbeat_claim(claim_one) is False
    assert recover_database_tasks() == 0
