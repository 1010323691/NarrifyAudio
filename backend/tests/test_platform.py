from __future__ import annotations

import uuid
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from backend.main import app
from backend.core.observability import record_api_request
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.artifact_publication import PublicationJournal
from backend.platform.models import OutboxEvent, SystemConfig, Task, TaskAttempt, User, UserQuotaAccount, utcnow
from backend.platform.storage import configured_storage_root, sha256_file, task_attempt_path, user_workspace_root
from backend.platform.task_worker import TaskOutcome, claim_fair_task, claim_task, complete_claim, heartbeat_claim, process_task_message, recover_database_tasks
from backend.platform.legacy_tasks import estimate_legacy_units


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


def _register(client: TestClient, email: str) -> dict:
    response = client.post("/api/auth/register", json={"email": email, "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234", "display_name": "Test User"})
    assert response.status_code == 201, response.text
    return response.json()


def test_session_cookie_and_project_scope(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    response = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Private book"})
    assert response.status_code == 201, response.text
    project = response.json()
    assert client.get("/api/v1/projects").json()[0]["id"] == project["id"]
    assert any(item["id"] == project["id"] for item in client.get("/api/v1/workspaces").json())
    assert client.post("/api/v1/projects", json={"name": "No CSRF"}).status_code == 403

    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert client.get("/api/v1/projects").status_code == 401


def test_project_routes_keep_managed_workspace_lifecycle_in_sync(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    created = client.post("/api/v1/workspaces", headers={"X-CSRF-Token": csrf}, json={"name": "Original"})
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    renamed = client.patch(
        f"/api/v1/projects/{workspace_id}", headers={"X-CSRF-Token": csrf}, json={"name": "Renamed"},
    )
    assert renamed.status_code == 200, renamed.text
    assert any(item["name"] == "Renamed" for item in client.get("/api/v1/workspaces").json())
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": workspace_id, "task_type": "text.format", "payload": {}, "estimated_units": 0, "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    assert client.delete(f"/api/v1/projects/{workspace_id}", headers={"X-CSRF-Token": csrf}).status_code == 409
    cancelled = client.post(f"/api/v1/tasks/{submitted.json()['id']}/cancel", headers={"X-CSRF-Token": csrf})
    assert cancelled.status_code == 200
    assert client.delete(f"/api/v1/projects/{workspace_id}", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert all(item["id"] != workspace_id for item in client.get("/api/v1/workspaces").json())


def test_script_batch_http_routes_use_persistent_tasks(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    workspace = Path(client.get("/api/workspace").json()["path"])
    source = workspace / "02_split_text" / "chapter.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("A short chapter.", encoding="utf-8")
    with SessionLocal.begin() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        account.available_units = 1000

    submitted = client.post(
        "/api/script/generate-files", headers={"X-CSRF-Token": csrf},
        json={"files": [source.name]},
    )
    assert submitted.status_code == 200, submitted.text
    task_id = submitted.json()["task_ids"][0]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task is not None and task.task_type == "script.parse"

    cancelled = client.post(
        "/api/script/cancel-batch", headers={"X-CSRF-Token": csrf},
        json={"task_ids": [task_id]},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["cancelled"][0]["id"] == task_id


def test_idempotent_task_submission_and_quota_guard(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Queue book"}).json()
    with SessionLocal.begin() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        account.available_units = 0
    response = client.post("/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json={"project_id": project["id"], "task_type": "script.parse", "payload": {}, "estimated_units": 1, "idempotency_key": "request-123456"})
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
    assert client.post("/api/music/tags", headers={"X-CSRF-Token": first["csrf_token"]},
                       json={"category": "scene", "name": "restricted"}).status_code == 403


def test_admin_can_cancel_persistent_task_and_release_reservation(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    with SessionLocal.begin() as db:
        user = db.get(User, first["user"]["id"])
        assert user is not None
        user.role = "admin"
        # The module-scoped database retains tasks submitted by earlier tests;
        # this case exercises an intentionally drained storage migration.
        for task in db.scalars(select(Task).where(Task.status.in_(("pending", "queued", "running", "paused", "cancelling", "retrying")))).all():
            task.status = "cancelled"
            task.finished_at = utcnow()
        account = db.get(UserQuotaAccount, user.id)
        assert account is not None
        account.available_units = 3
    quota_setting = client.get("/api/v1/admin/settings/quota")
    assert quota_setting.status_code == 200, quota_setting.text
    updated_setting = client.patch(
        "/api/v1/admin/settings/quota",
        headers={"X-CSRF-Token": csrf},
        json={"units": 5},
    )
    assert updated_setting.status_code == 200, updated_setting.text
    assert updated_setting.json()["initial_units"] == 5
    registration_setting = client.get("/api/v1/admin/settings/registration")
    assert registration_setting.status_code == 200, registration_setting.text
    disabled_registration = client.patch(
        "/api/v1/admin/settings/registration",
        headers={"X-CSRF-Token": csrf},
        json={"enabled": False},
    )
    assert disabled_registration.status_code == 200, disabled_registration.text
    assert client.post("/api/auth/register", json={"email": f"{uuid.uuid4()}@example.com", "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234"}).status_code == 403
    enabled_registration = client.patch(
        "/api/v1/admin/settings/registration",
        headers={"X-CSRF-Token": csrf},
        json={"enabled": True},
    )
    assert enabled_registration.status_code == 200, enabled_registration.text
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Admin task"}).json()
    submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "text.format",
            "payload": {},
            "estimated_units": 3,
            "idempotency_key": "admin-cancel-task-123",
        },
    )
    assert submitted.status_code == 201, submitted.text
    cancelled = client.post(f"/api/v1/admin/tasks/{submitted.json()['id']}/cancel", headers={"X-CSRF-Token": csrf})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    metrics = client.get("/api/v1/admin/task-metrics")
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["total"] >= 1
    assert any(item["task_type"] == "text.format" for item in metrics.json()["by_type"])
    assert metrics.json()["throughput_60s"]["window_seconds"] == 60
    assert {"online_workers", "total_slots", "active_slots", "idle_slots"} <= metrics.json()["worker_pool"].keys()
    activity = client.get("/api/v1/admin/task-activity")
    assert activity.status_code == 200, activity.text
    assert any(item["event_type"] == "admin_cancel_requested" for item in activity.json())
    overview = client.get("/api/v1/admin/overview")
    assert overview.status_code == 200, overview.text
    assert {item["key"] for item in overview.json()["services"]} >= {"api", "database", "queue", "llm", "tts", "audio", "gpu"}
    assert overview.json()["tasks"]["queued"] >= 0
    performance = client.get("/api/v1/admin/performance")
    assert performance.status_code == 200, performance.text
    assert performance.json()["system"]["disk_total_bytes"] > 0
    assert performance.json()["api"]["request_count"] >= 1
    assert performance.json()["api"]["p95_ms"] is not None
    record_api_request("/api/test/failure", 500, 3.0, "GET")
    api_errors = client.get("/api/v1/admin/events?module=api")
    assert api_errors.status_code == 200, api_errors.text
    assert any(row["type"] == "HTTP 500" and "/api/test/failure" in row["message"] for row in api_errors.json())
    overview_errors = client.get("/api/v1/admin/overview")
    assert any(row["module"] == "api" and row["type"] == "HTTP 500" for row in overview_errors.json()["recent_errors"])
    resources = client.get("/api/v1/admin/resources")
    assert resources.status_code == 200, resources.text
    assert resources.json()["projects"] >= 1
    assert client.get("/api/v1/admin/events?level=info&limit=2").status_code == 200
    task_rows = client.get("/api/v1/admin/tasks?status=cancelled&limit=1")
    assert task_rows.status_code == 200, task_rows.text
    assert len(task_rows.json()) <= 1
    assert all(row["status"] == "cancelled" for row in task_rows.json())
    quota = client.get("/api/v1/quota").json()
    assert quota["available_units"] == 3
    assert quota["reserved_units"] == 0
    reset_setting = client.patch(
        "/api/v1/admin/settings/quota",
        headers={"X-CSRF-Token": csrf},
        json={"units": 0},
    )
    assert reset_setting.status_code == 200, reset_setting.text


def test_workspace_directory_is_user_scoped_and_admin_root_is_persistent(client: TestClient, tmp_path):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    created = client.post("/api/v1/workspaces", headers={"X-CSRF-Token": csrf}, json={"name": "My workspace"})
    assert created.status_code == 201, created.text
    workspace = created.json()
    assert workspace["directory_key"].startswith(f"{first['user']['username']}/")
    with SessionLocal() as db:
        storage_root = configured_storage_root(db)
    old_workspace_path = storage_root / workspace["directory_key"]
    assert old_workspace_path.is_dir()

    with SessionLocal.begin() as db:
        user = db.get(User, first["user"]["id"])
        assert user is not None
        user.role = "admin"

    root = (tmp_path / "admin-selected-root").resolve()
    updated = client.patch("/api/v1/admin/settings/storage", headers={"X-CSRF-Token": csrf}, json={"root_path": str(root)})
    assert updated.status_code == 200, updated.text
    assert Path(updated.json()["root_path"]) == root
    assert (root / workspace["directory_key"]).is_dir()
    assert not old_workspace_path.exists()
    assert client.get("/api/v1/admin/settings/storage").json()["source"] == "admin"
    assert client.get("/api/v1/admin/tasks").status_code == 200
    assert client.get("/api/v1/admin/workers").status_code == 200
    assert client.get("/api/v1/admin/queue").status_code == 200


def test_workspaces_for_different_users_have_separate_username_roots(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    first_info = client.get("/api/workspace").json()
    second = _register(client, f"{uuid.uuid4()}@example.com")
    second_info = client.get("/api/workspace").json()

    assert first_info["path"] != second_info["path"]
    assert first_info["path"].replace("\\", "/").split("/")[-2] == first["user"]["username"]
    assert second_info["path"].replace("\\", "/").split("/")[-2] == second["user"]["username"]


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
    legacy_file = uploaded.json()
    legacy_split = client.post(
        "/api/book/split",
        headers={"X-CSRF-Token": csrf},
        json={"path": legacy_file["path"], "whole_book": True},
    )
    assert legacy_split.status_code == 200, legacy_split.text
    assert process_task_message(
        {"payload": {"task_id": legacy_split.json()["task_id"]}}, worker_id="test-book-worker"
    ) == "succeeded"
    project_files = client.get(f"/api/v1/projects/{legacy_file['project_id']}/files").json()
    assert any(item["module"] == "02_split_text" for item in project_files)

    formatted_upload = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("format.txt", b"text to format", "text/plain")},
    )
    assert formatted_upload.status_code == 200, formatted_upload.text
    formatted = client.post(
        "/api/text/format",
        headers={"X-CSRF-Token": csrf},
        json={"path": formatted_upload.json()["path"]},
    )
    assert formatted.status_code == 200, formatted.text
    assert formatted.json()["file_id"]
    assert process_task_message(
        {"payload": {"task_id": formatted.json()["task_id"]}}, worker_id="test-format-worker"
    ) == "succeeded"

    durable_upload = client.post(
        f"/api/v1/projects/{legacy_file['project_id']}/files",
        headers={"X-CSRF-Token": csrf},
        files={"upload": ("durable.txt", b"durable source", "text/plain")},
    )
    assert durable_upload.status_code == 201, durable_upload.text
    submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": legacy_file["project_id"],
            "task_type": "book.split",
            "payload": {"input_file_id": durable_upload.json()["id"], "whole_book": True},
            "estimated_units": 0,
            "idempotency_key": f"legacy-book-split-{uuid.uuid4()}",
        },
    )
    assert submitted.status_code == 201, submitted.text
    assert process_task_message({"payload": {"task_id": submitted.json()["id"]}}, worker_id="test-worker") == "succeeded"
    split_listing = client.get("/api/files/list/02_split_text?recursive=true")
    assert split_listing.status_code == 200
    assert split_listing.json()["items"]


def test_legacy_audio_packaging_route_uses_durable_worker(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    uploaded = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("cut.mp3", b"fake audio", "audio/mpeg")},
    )
    assert uploaded.status_code == 200, uploaded.text
    source = uploaded.json()
    submitted = client.post(
        "/api/audio/zip",
        headers={"X-CSRF-Token": csrf},
        json={
            "base": "book",
            "files": [{"name": "cut.mp3", "path": source["path"]}],
        },
    )
    assert submitted.status_code == 200, submitted.text
    task_id = submitted.json()["task_id"]
    assert process_task_message({"payload": {"task_id": task_id}}, worker_id="test-audio-package-worker") == "succeeded"
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert task["status"] == "succeeded"
    assert task["result"]["engine"] == "audio.zip"
    assert task["result"]["file_id"]
    assert "/07_output/" in task["result"]["path"].replace("\\", "/")


def test_durable_worker_formats_uploaded_file_without_quota_charge(client: TestClient):
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
            "payload": {"input_file_id": input_file["id"], "publish_module": "01_input"},
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
    assert "/01_input/" in task["result"]["path"].replace("\\", "/")
    analysis = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "book.analyze",
            "payload": {"input_file_id": task["result"]["file_id"]},
            "estimated_units": 0,
            "idempotency_key": "worker-book-analysis-123",
        },
    )
    assert analysis.status_code == 201, analysis.text
    assert process_task_message(
        {"payload": {"task_id": analysis.json()["id"]}}, worker_id="test-book-analysis-worker"
    ) == "succeeded"
    downloaded = client.get(f"/api/v1/projects/{project['id']}/files/{task['result']['file_id']}")
    assert downloaded.status_code == 200
    assert "第一章" in downloaded.text
    ranged = client.get(
        f"/api/v1/projects/{project['id']}/files/{task['result']['file_id']}",
        headers={"Range": "bytes=0-4"},
    )
    assert ranged.status_code == 206
    assert ranged.headers["content-range"].startswith("bytes 0-4/")
    assert len(ranged.content) == 5
    events = client.get(f"/api/v1/tasks/{task_id}/events")
    assert events.status_code == 200
    assert "succeeded" in events.text
    quota = client.get("/api/v1/quota")
    assert quota.json()["consumed_units"] == 0
    ledger = client.get("/api/v1/quota/transactions")
    assert ledger.json() == []

    analyzed = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "book.analyze",
            "payload": {"input_file_id": input_file["id"]},
            "estimated_units": 0,
            "idempotency_key": "worker-book-analyze-123",
        },
    )
    assert analyzed.status_code == 201, analyzed.text
    analyzed_id = analyzed.json()["id"]
    assert process_task_message({"payload": {"task_id": analyzed_id}}, worker_id="test-worker") == "succeeded"
    analyzed_task = client.get(f"/api/v1/tasks/{analyzed_id}").json()
    assert analyzed_task["status"] == "succeeded"
    assert analyzed_task["result"]["analysis"]["chapter_count"] == 1
    assert analyzed_task["result"]["analysis"]["chapters"][0]["title"] == ""

    split_input = client.post(
        f"/api/v1/projects/{project['id']}/files",
        headers={"X-CSRF-Token": csrf},
        files={"upload": ("split.txt", "第一章\n第一段\n第二章\n第二段\n".encode("utf-8"), "text/plain")},
    )
    assert split_input.status_code == 201, split_input.text
    split_submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "book.split",
            "payload": {"input_file_id": split_input.json()["id"], "smart": True},
            "estimated_units": 0,
            "idempotency_key": "worker-book-split-123",
        },
    )
    assert split_submitted.status_code == 201, split_submitted.text
    split_id = split_submitted.json()["id"]
    assert process_task_message({"payload": {"task_id": split_id}}, worker_id="test-worker") == "succeeded"
    split_task = client.get(f"/api/v1/tasks/{split_id}").json()
    assert split_task["status"] == "succeeded"
    assert split_task["result"]["file_count"] == 2
    assert len(split_task["result"]["files"]) == 2
    assert all("/02_split_text/" in item["path"].replace("\\", "/") for item in split_task["result"]["files"])

    with SessionLocal() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        assert account.available_units == 2
        assert account.reserved_units == 0
        assert account.consumed_units == 0
        attempts = db.query(TaskAttempt).filter(TaskAttempt.task_id == task_id).all()
        assert len(attempts) == 1
        assert attempts[0].status == "succeeded"


def test_durable_worker_parses_script_into_scoped_artifact(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects",
        headers={"X-CSRF-Token": csrf},
        json={"name": "Durable script"},
    ).json()
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/files",
        headers={"X-CSRF-Token": csrf},
        files={"upload": ("chapter.txt", b"narrator: hello", "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    input_file = uploaded.json()
    observed: dict[str, str] = {}
    with SessionLocal.begin() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        account.available_units = 3

    def fake_generate(handle, path, llm, prompts, generation, *, output_path, spot_history_path):
        observed["model"] = llm.model_name
        observed["workspace"] = str(output_path.parent)
        handle.log("durable script parsing started")
        handle.progress(0.5, "parsing")
        handle.check()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            '[{"speaker":"NARRATOR","text":"hello","instruct":""}]',
            encoding="utf-8",
        )
        return {
            "entries": [{"speaker": "NARRATOR", "text": "hello", "instruct": ""}],
            "count": 1,
            "output_path": str(output_path),
        }

    monkeypatch.setattr("backend.platform.task_worker.script_engine.parse_script_file", fake_generate)
    submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "script.parse",
            "payload": {
                "input_file_id": input_file["id"],
                "config": {
                    "llm": {"model_name": "durable-snapshot-model"},
                    "prompts": {},
                    "generation": {"spot_check_rate": 0},
                },
            },
            "estimated_units": 0,
            "idempotency_key": "durable-script-parse-123",
        },
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]
    assert process_task_message({"payload": {"task_id": task_id}}, worker_id="test-script-worker") == "succeeded"

    result = client.get(f"/api/v1/tasks/{task_id}").json()
    assert result["status"] == "succeeded"
    assert result["result"]["engine"] == "script.parse"
    assert "output_path" not in result["result"]
    assert observed["model"] == "durable-snapshot-model"
    files = client.get(f"/api/v1/projects/{project['id']}/files").json()
    artifacts = [item for item in files if item["module"] == "03_parsed_json"]
    assert len(artifacts) == 1
    assert artifacts[0]["name"] == "chapter.json"


def test_durable_worker_runs_audio_tasks_and_catalogs_outputs(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects",
        headers={"X-CSRF-Token": csrf},
        json={"name": "Durable audio"},
    ).json()
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/files",
        headers={"X-CSRF-Token": csrf},
        files={"upload": ("merged.mp3", b"audio", "audio/mpeg")},
    )
    assert uploaded.status_code == 201, uploaded.text
    input_file = uploaded.json()
    with SessionLocal.begin() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        account.available_units = 3

    monkeypatch.setattr("backend.platform.task_worker.audio_engine.probe_duration", lambda *args: (4.0, ""))
    monkeypatch.setattr(
        "backend.platform.task_worker.audio_engine.detect_silences",
        lambda *args, **kwargs: {"ok": True, "pauses": [{"start": 1.0, "end": 1.5}]},
    )
    planned = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "audio.silences",
            "payload": {"input_file_id": input_file["id"], "target": "2", "tolerance": 5},
            "estimated_units": 0,
            "idempotency_key": "durable-audio-plan-123",
        },
    )
    assert planned.status_code == 201, planned.text
    planned_id = planned.json()["id"]
    assert process_task_message({"payload": {"task_id": planned_id}}, worker_id="test-audio-worker") == "succeeded"
    planned_result = client.get(f"/api/v1/tasks/{planned_id}").json()
    assert planned_result["result"]["engine"] == "audio.silences"
    assert planned_result["result"]["count"] == 2

    def fake_cut(path, segments, out_dir, naming, start_number, *args, **kwargs):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for index, segment in enumerate(segments):
            output = out_dir / f"episode-{index + 1}.mp3"
            output.write_bytes(f"part-{index}".encode())
            result.append({"name": output.name, "path": str(output), "size": output.stat().st_size, "duration": segment["duration"]})
        return result

    monkeypatch.setattr("backend.platform.task_worker.audio_engine.cut_segments", fake_cut)
    cut = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "audio.cut",
            "payload": {
                "input_file_id": input_file["id"],
                "smart_align": False,
                "segments": [{"index": 0, "start": 0, "duration": 2}, {"index": 1, "start": 2, "duration": 2}],
                "naming": "第 {} 集",
                "start_number": "1",
            },
            "estimated_units": 0,
            "idempotency_key": "durable-audio-cut-123",
        },
    )
    assert cut.status_code == 201, cut.text
    cut_id = cut.json()["id"]
    assert process_task_message({"payload": {"task_id": cut_id}}, worker_id="test-audio-worker") == "succeeded"
    cut_result = client.get(f"/api/v1/tasks/{cut_id}").json()
    assert cut_result["result"]["engine"] == "audio.cut"
    assert cut_result["result"]["file_count"] == 2
    assert all("/07_output/" in item["path"].replace("\\", "/") for item in cut_result["result"]["files"])


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
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == task_id))
        assert event is not None
        assert event.published_at is not None
    assert process_task_message({"payload": {"task_id": task_id}}, worker_id="cancelled-task-worker") == "skipped"
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "cancelled"


def test_fair_scheduler_rotates_between_users(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    first_csrf = first["csrf_token"]
    first_project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": first_csrf}, json={"name": "Fair first"}
    ).json()
    with TestClient(app) as second_client:
        second = _register(second_client, f"{uuid.uuid4()}@example.com")
        second_csrf = second["csrf_token"]
        second_project = second_client.post(
            "/api/v1/projects", headers={"X-CSRF-Token": second_csrf}, json={"name": "Fair second"}
        ).json()

        def submit(test_client: TestClient, csrf: str, project_id: str, key: str) -> str:
            response = test_client.post(
                "/api/v1/tasks",
                headers={"X-CSRF-Token": csrf},
                json={"project_id": project_id, "task_type": "scheduler.test", "payload": {}, "estimated_units": 0, "idempotency_key": key},
            )
            assert response.status_code == 201, response.text
            return response.json()["id"]

        first_task = submit(client, first_csrf, first_project["id"], "fair-first-1")
        submit(client, first_csrf, first_project["id"], "fair-first-2")
        second_task = submit(second_client, second_csrf, second_project["id"], "fair-second-1")

    claim_one = claim_fair_task("fair-worker-1", task_types=("scheduler.test",))
    assert claim_one is not None
    claim_two = claim_fair_task("fair-worker-2", task_types=("scheduler.test",))
    assert claim_two is not None
    assert claim_one.task_id == first_task
    assert claim_two.task_id == second_task


def test_legacy_task_estimates_are_non_billable():
    assert estimate_legacy_units("tts.batch", {"scripts": ["a.json", "b.json"]}) == 0
    assert estimate_legacy_units("bgm.match", {"chapters": ["a", "b", "c"]}) == 0
    assert estimate_legacy_units("audio.zip", {"files": [{"name": "a.mp3"}]}) == 0


def test_public_submission_cannot_underestimate_known_task(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Quota floor"}).json()
    response = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "tts.batch",
            "payload": {"scripts": ["one.json", "two.json"]},
            "estimated_units": 0,
            "idempotency_key": "quota-floor-known-task",
        },
    )
    assert response.status_code == 409


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
    recover_database_tasks()
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        attempt = db.get(TaskAttempt, claim_two.attempt_id)
        assert task is not None and task.status == "running"
        assert attempt is not None and attempt.status == "running"


def test_retry_deadline_applies_to_direct_and_fair_claims(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Backoff book"}).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "backoff.test", "payload": {}, "estimated_units": 0, "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        task.status = "retrying"
        task.next_attempt_at = utcnow() + timedelta(minutes=1)
    assert claim_task(task_id, "direct-worker") is None
    assert claim_fair_task("fair-worker", task_types=("backoff.test",)) is None
    with SessionLocal.begin() as db:
        db.get(Task, task_id).next_attempt_at = utcnow() - timedelta(seconds=1)
    claim = claim_fair_task("fair-worker", task_types=("backoff.test",))
    assert claim is not None and claim.task_id == task_id


def test_expired_cancelling_attempt_releases_tts_hold(client: TestClient):
    from backend.platform.quota import reserve_tts_quota, reset_quota_context, set_quota_context

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Cancel recovery"}).json()
    with SessionLocal.begin() as db:
        db.get(UserQuotaAccount, first["user"]["id"]).available_units = 10
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "estimated_units": 0, "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]
    claim = claim_task(task_id, "cancel-recovery-worker", lease_seconds=60)
    assert claim is not None
    token = set_quota_context(first["user"]["id"], task_id, claim.attempt_id)
    try:
        assert reserve_tts_quota(5, "test.hold")
    finally:
        reset_quota_context(token)
    with SessionLocal.begin() as db:
        db.get(Task, task_id).status = "cancelling"
        db.get(TaskAttempt, claim.attempt_id).lease_expires_at = utcnow() - timedelta(seconds=1)
    recover_database_tasks()
    with SessionLocal() as db:
        assert db.get(Task, task_id).status == "cancelled"
        assert db.get(TaskAttempt, claim.attempt_id).status == "expired"
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account.available_units == 10
        assert account.reserved_units == 0


def test_failed_result_commit_restores_previous_workspace_file(client: TestClient, monkeypatch):
    from backend.platform import task_worker

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Publish rollback"}).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "estimated_units": 0, "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    claim = claim_task(submitted.json()["id"], "publish-rollback-worker")
    assert claim is not None
    with SessionLocal() as db:
        workspace = user_workspace_root(db, first["user"]["username"], project["id"])
        staged = task_attempt_path(db, first["user"]["username"], project["id"], claim.task_id, claim.attempt_id, "staged.txt")
    final = workspace / "01_input" / "chapter.txt"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_text("original", encoding="utf-8")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("replacement", encoding="utf-8")
    outcome = TaskOutcome(
        temp_path=staged, output_name="chapter.txt", content_type="text/plain",
        size_bytes=staged.stat().st_size, sha256=sha256_file(staged), metadata={},
        publish_module="01_input",
    )
    original_factory = task_worker.SessionLocal

    def failing_session():
        session = original_factory()
        session.commit = lambda: (_ for _ in ()).throw(RuntimeError("commit failed"))
        return session

    monkeypatch.setattr(task_worker, "SessionLocal", failing_session)
    with pytest.raises(RuntimeError, match="commit failed"):
        complete_claim(claim, outcome)
    assert final.read_text("utf-8") == "original"
    assert not (staged.parent / "publication.json").exists()
    with original_factory() as db:
        assert db.get(Task, claim.task_id).status == "running"


def test_expired_attempt_restores_interrupted_publication(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Interrupted publish"}).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "estimated_units": 0, "idempotency_key": uuid.uuid4().hex},
    )
    claim = claim_task(submitted.json()["id"], "interrupted-publish-worker")
    assert claim is not None
    with SessionLocal() as db:
        root = configured_storage_root(db)
        workspace = user_workspace_root(db, first["user"]["username"], project["id"])
        journal_path = task_attempt_path(db, first["user"]["username"], project["id"], claim.task_id, claim.attempt_id, "publication.json")
    final = workspace / "01_input" / "chapter.txt"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_text("old", encoding="utf-8")
    staged = journal_path.parent / "staged.txt"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("new", encoding="utf-8")
    journal = PublicationJournal(root, journal_path)
    journal.prepare()
    journal.publish(journal.add(final), staged)
    with SessionLocal.begin() as db:
        db.get(TaskAttempt, claim.attempt_id).lease_expires_at = utcnow() - timedelta(seconds=1)
    recover_database_tasks()
    assert final.read_text("utf-8") == "old"
    assert not journal_path.exists()


def test_interrupted_storage_migration_blocks_writes_and_resumes(client: TestClient, tmp_path):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    workspace = client.post("/api/v1/workspaces", headers={"X-CSRF-Token": csrf}, json={"name": "Storage resume"}).json()
    with SessionLocal.begin() as db:
        db.get(User, first["user"]["id"]).role = "admin"
        for task in db.scalars(select(Task).where(Task.status.in_(("pending", "queued", "running", "paused", "cancelling", "retrying")))).all():
            task.status = "cancelled"
            task.finished_at = utcnow()
        source_root = configured_storage_root(db)
        target_root = (tmp_path / "resumed-root").resolve()
        db.add(SystemConfig(key="storage.migration", value={"source": str(source_root), "target": str(target_root)}))
    source = source_root / workspace["directory_key"]
    target = target_root / workspace["directory_key"]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    assert client.get("/api/workspace").status_code == 409
    assert not source.exists()
    blocked = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": workspace["id"], "task_type": "text.format", "payload": {}, "estimated_units": 0, "idempotency_key": uuid.uuid4().hex},
    )
    assert blocked.status_code == 409
    resumed = client.patch("/api/v1/admin/settings/storage", headers={"X-CSRF-Token": csrf}, json={"root_path": str(target_root)})
    assert resumed.status_code == 200, resumed.text
    assert target.is_dir()
    with SessionLocal() as db:
        assert configured_storage_root(db) == target_root
        assert db.get(SystemConfig, "storage.migration") is None


def test_api_snapshot_p95_uses_request_durations():
    from backend.core import observability

    with observability._lock:
        observability._samples.clear()
    for duration in range(1, 101):
        observability.record_api_request("/api/test/latency", 200, float(duration))
    snapshot = observability.api_snapshot()
    assert snapshot["p95_ms"] == 95
    assert snapshot["endpoints"][0]["p95_ms"] == 95


def test_admin_memory_metrics_use_container_cgroup_limit(tmp_path):
    from backend.api.admin import _memory_usage

    (tmp_path / "memory.current").write_text("1500000")
    (tmp_path / "memory.max").write_text("2000000")
    assert _memory_usage(8_000_000, tmp_path) == (1_500_000, 2_000_000)
