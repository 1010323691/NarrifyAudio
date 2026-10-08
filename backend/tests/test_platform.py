from __future__ import annotations

import hashlib
import json
import io
import uuid
import shutil
import threading
import time
import zipfile
from types import SimpleNamespace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

from backend.main import app
from backend.core.observability import record_api_request
from backend.core import paths as core_paths
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.artifact_publication import PublicationJournal
from backend.platform.models import OutboxEvent, ProjectFile, SystemConfig, Task, TaskAttempt, TaskEvent, User, UserQuotaAccount, utcnow
from backend.platform.storage import configured_storage_root, object_path, sha256_file, task_attempt_path, project_workspace_path
from backend.platform.task_worker import PersistentTaskHandle, TaskOutcome, _workspace_engine_lock, cancellation_requested, claim_fair_task, claim_task, complete_claim, execute_claim, fail_claim, heartbeat_claim, process_task_message, recover_database_tasks, resume_llm_unavailable_tasks
from backend.platform.task_contracts import TaskExecutionError
from backend.platform.task_lifecycle import ACTIVE_TASK_STATUSES
from backend.platform.system_config import update_feature_defaults_cache
from backend.tests.resource_delivery_helpers import record_delivery


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


def _register(client: TestClient, email: str) -> dict:
    response = client.post("/api/auth/register", json={"email": email, "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234", "display_name": "Test User"})
    assert response.status_code == 201, response.text
    return response.json()


def test_persistent_handle_journals_shared_music_file(monkeypatch, tmp_path):
    library = tmp_path / "music_library"
    library.mkdir()
    index_path = library / "music_index.json"
    index_path.write_text("old", encoding="utf-8")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", library)
    monkeypatch.setattr("backend.platform.task_context.cancellation_requested", lambda _claim: False)
    handle = PersistentTaskHandle(SimpleNamespace(task_id="task", attempt_id="attempt"))

    handle.stage_shared_file(index_path, b"new")

    assert index_path.read_bytes() == b"new"
    assert isinstance(handle.publication_journal, PublicationJournal)
    handle.rollback_publications()
    assert index_path.read_text("utf-8") == "old"


def test_session_cookie_and_project_scope(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    response = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Private book"})
    assert response.status_code == 201, response.text
    project = response.json()
    assert client.get("/api/v1/projects").json()[0]["id"] == project["id"]
    assert any(item["id"] == project["id"] for item in client.get("/api/v1/projects").json())
    assert client.post("/api/v1/projects", json={"name": "No CSRF"}).status_code == 403

    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert client.get("/api/v1/projects").status_code == 401


def test_retired_filesystem_and_workspace_routes_are_not_registered(client: TestClient):
    assert client.get("/api/filesystem/drives").status_code == 404
    assert client.get("/api/v1/workspaces").status_code == 404
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert not any(path.startswith("/api/filesystem") for path in paths)
    assert not any(path.startswith("/api/v1/workspaces") for path in paths)


def test_project_routes_keep_managed_workspace_lifecycle_in_sync(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    created = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Original"})
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    renamed = client.patch(
        f"/api/v1/projects/{workspace_id}", headers={"X-CSRF-Token": csrf}, json={"name": "Renamed"},
    )
    assert renamed.status_code == 200, renamed.text
    assert any(item["name"] == "Renamed" for item in client.get("/api/v1/projects").json())
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": workspace_id, "task_type": "text.format", "payload": {}, "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    assert client.delete(f"/api/v1/projects/{workspace_id}", headers={"X-CSRF-Token": csrf}).status_code == 409
    cancelled = client.post(f"/api/v1/tasks/{submitted.json()['id']}/cancel", headers={"X-CSRF-Token": csrf})
    assert cancelled.status_code == 200
    assert client.delete(f"/api/v1/projects/{workspace_id}", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert all(item["id"] != workspace_id for item in client.get("/api/v1/projects").json())


def test_task_surface_lists_and_controls_durable_tasks(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    workspace = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Task UI adapter"},
    )
    assert workspace.status_code == 201, workspace.text
    project_id = workspace.json()["id"]
    selected = client.put(
        "/api/v1/projects/active", headers={"X-CSRF-Token": csrf}, json={"project_id": project_id},
    )
    assert selected.status_code == 200, selected.text
    unsupported = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json={
            "project_id": project_id, "task_type": "unknown.executor", "payload": {},
            "idempotency_key": f"unsupported-{uuid.uuid4().hex}",
        },
    )
    assert unsupported.status_code == 422, unsupported.text
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json={
            "project_id": project_id,
            "task_type": "text.format",
            "payload": {},
            "idempotency_key": f"adapter-{uuid.uuid4().hex}",
        },
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]

    listed = client.get("/api/v1/tasks")
    assert listed.status_code == 200, listed.text
    assert any(row["id"] == task_id and row["task_type"] == "text.format" for row in listed.json())
    fetched = client.get(f"/api/v1/tasks/{task_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == task_id
    cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel", headers={"X-CSRF-Token": csrf})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


def test_task_center_batch_pause_and_resume_is_category_scoped(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Batch controls"},
    ).json()
    other_project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Other book"},
    ).json()
    with SessionLocal.begin() as db:
        script_task = Task(
            owner_id=first["user"]["id"], project_id=project["id"],
            task_type="script.parse", status="pending",
        )
        tts_task = Task(
            owner_id=first["user"]["id"], project_id=project["id"],
            task_type="tts.batch", status="pending",
        )
        other_book_script_task = Task(
            owner_id=first["user"]["id"], project_id=other_project["id"],
            task_type="script.parse", status="pending",
        )
        db.add_all([script_task, tts_task, other_book_script_task])
        db.flush()
        script_id, tts_id, other_book_script_id = script_task.id, tts_task.id, other_book_script_task.id

    paused = client.post(
        "/api/v1/tasks/batch-control", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "category": "script", "action": "pause"},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["changed"] == 1
    assert paused.json()["tasks"][0]["id"] == script_id
    assert paused.json()["tasks"][0]["status"] == "paused"
    assert client.get(f"/api/v1/tasks/{tts_id}").json()["status"] == "pending"
    assert client.get(f"/api/v1/tasks/{other_book_script_id}").json()["status"] == "pending"

    resumed = client.post(
        "/api/v1/tasks/batch-control", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "category": "script", "action": "resume"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["tasks"][0]["id"] == script_id
    assert resumed.json()["tasks"][0]["status"] == "pending"

    cancelled = client.post(
        "/api/v1/tasks/batch-control", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "category": "script", "action": "cancel"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["changed"] == 1
    assert cancelled.json()["tasks"][0]["id"] == script_id
    assert cancelled.json()["tasks"][0]["status"] == "cancelled"
    assert client.get(f"/api/v1/tasks/{tts_id}").json()["status"] == "pending"
    assert client.get(f"/api/v1/tasks/{other_book_script_id}").json()["status"] == "pending"


def test_durable_bgm_packaging_publishes_downloadable_archive(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "BGM package"},
    ).json()
    with SessionLocal() as db:
        workspace = project_workspace_path(db, first["user"]["username"], project["id"])
    bgm_dir = workspace / "08_bgm"
    bgm_dir.mkdir(parents=True, exist_ok=True)
    (bgm_dir / "chapter-1.mp3").write_bytes(b"audio-one")
    (bgm_dir / "chapter-2.mp3").write_bytes(b"audio-two")
    for name in ("chapter-1.mp3", "chapter-2.mp3"):
        record_delivery(first["user"]["id"], project["id"], f"08_bgm/{name}", "bgm.mix")
    submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "bgm.package",
            "payload": {"chapters": ["chapter-1", "chapter-2"], "base": "Book"},
            "idempotency_key": f"bgm-package-{uuid.uuid4().hex}",
        },
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]
    assert process_task_message({"payload": {"task_id": task_id}}, worker_id="bgm-package-worker") == "succeeded"
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert task["status"] == "succeeded"
    assert task["result"]["file_count"] == 2
    activated = client.put(
        "/api/v1/projects/active",
        headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"]},
    )
    assert activated.status_code == 200, activated.text
    download = client.get("/api/files/download/08_bgm/Book.zip")
    assert download.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert archive.namelist() == ["Book/chapter-1.mp3", "Book/chapter-2.mp3"]
        assert archive.read("Book/chapter-1.mp3") == b"audio-one"
        assert archive.read("Book/chapter-2.mp3") == b"audio-two"


def test_generic_task_submission_rejects_unsafe_legacy_task_paths(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Unsafe package"},
    ).json()
    invalid_payloads = [
        ("bgm.segment", {"stem": r"folder\outside"}),
        ("bgm.mix", {"stem": ".."}),
        ("bgm.match", {"chapters": ["safe", "../outside"]}),
        ("bgm.package", {"chapters": [""]}),
        ("tts.merge", {"package": "../outside"}),
        ("tts.batch", {"scripts": [r"folder\outside.json"]}),
        ("voices.clone", {"script": "../outside.json"}),
    ]
    for index, (task_type, payload) in enumerate(invalid_payloads):
        response = client.post(
            "/api/v1/tasks",
            headers={"X-CSRF-Token": csrf},
            json={
                "project_id": project["id"], "task_type": task_type,
                "payload": payload,
                "idempotency_key": f"unsafe-bgm-{index}-{uuid.uuid4().hex}",
            },
        )
        assert response.status_code == 422, response.text


def test_generic_music_tag_task_requires_admin(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Restricted tags"},
    ).json()
    response = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"], "task_type": "music.suggest_tags",
            "payload": {"name": "track.mp3"},
            "idempotency_key": f"music-tags-user-{uuid.uuid4().hex}",
        },
    )
    assert response.status_code == 403


def test_music_tag_task_fails_if_owner_is_demoted_before_execution(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Demoted admin task"},
    ).json()
    with SessionLocal.begin() as db:
        user = db.get(User, first["user"]["id"])
        user.role = "admin"
        account = db.get(UserQuotaAccount, user.id)
        if account is None:
            account = UserQuotaAccount(user_id=user.id, available_units=1)
            db.add(account)
        else:
            account.available_units = 1
    submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"], "task_type": "music.suggest_tags",
            "payload": {"name": "track.mp3"},
            "idempotency_key": f"demoted-music-task-{uuid.uuid4().hex}",
        },
    )
    assert submitted.status_code == 201, submitted.text
    with SessionLocal.begin() as db:
        user = db.get(User, first["user"]["id"])
        user.role = "user"

    assert process_task_message(
        {"payload": {"task_id": submitted.json()["id"]}}, worker_id="demoted-music-worker",
    ) == "permission_revoked"
    task = client.get(f"/api/v1/tasks/{submitted.json()['id']}").json()
    assert task["status"] == "failed"
    assert task["error_code"] == "permission_revoked"


def test_tts_status_reports_worker_file_readiness_without_loading_models(client: TestClient, monkeypatch):
    from backend.api import tts as tts_api

    monkeypatch.setattr(tts_api.T, "resolve_engine", lambda: (Path("python"), Path("worker.py")))
    ready = client.get("/api/tts/status").json()
    assert ready["implemented"] is True
    assert ready["ready"] is True
    assert "任务启动时检查" in ready["message"]

    def missing_engine():
        raise RuntimeError("engine not installed")

    monkeypatch.setattr(tts_api.T, "resolve_engine", missing_engine)
    unavailable = client.get("/api/tts/status").json()
    assert unavailable["implemented"] is True
    assert unavailable["ready"] is False


def test_workspace_engine_lock_serializes_project_writers(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Serialized writers"},
    ).json()
    claims = []
    for suffix in ("one", "two"):
        task = client.post(
            "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
            json={
                "project_id": project["id"], "task_type": "tts.reset", "payload": {"scripts": []},
                "idempotency_key": f"writer-lock-{suffix}-{uuid.uuid4().hex}",
            },
        ).json()
        claim = claim_task(task["id"], f"lock-test-{suffix}", defer_workspace_conflicts=False)
        assert claim is not None
        claims.append(claim)

    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    failures: list[BaseException] = []

    def hold_first():
        try:
            with _workspace_engine_lock(claims[0]) as active:
                assert active
                first_entered.set()
                assert release_first.wait(3)
        except BaseException as exc:
            failures.append(exc)

    def hold_second():
        try:
            with _workspace_engine_lock(claims[1]) as active:
                assert active
                second_entered.set()
        except BaseException as exc:
            failures.append(exc)

    first_thread = threading.Thread(target=hold_first)
    second_thread = threading.Thread(target=hold_second)
    first_thread.start()
    try:
        assert first_entered.wait(3)
        second_thread.start()
        assert not second_entered.wait(0.15)
    finally:
        release_first.set()
    first_thread.join(3)
    second_thread.join(3)
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert not failures
    assert second_entered.is_set()
    with SessionLocal.begin() as db:
        for claim in claims:
            task = db.get(Task, claim.task_id)
            attempt = db.get(TaskAttempt, claim.attempt_id)
            assert task is not None and attempt is not None
            task.status = "cancelled"
            attempt.status = "cancelled"
            task.finished_at = attempt.finished_at = utcnow()


def test_script_batch_http_routes_use_persistent_tasks(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    workspace = Path(client.get("/api/v1/projects/active").json()["path"])
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


def test_script_generate_files_checks_merge_into_task_payload(client: TestClient):
    # 解析检查开关（用户解析页 6 项）随任务提交：提交值合并进该 Task 的
    # config 快照（每任务权威值，压过工作区配置与平台默认）；未提交键保留生效配置值。
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    workspace = Path(client.get("/api/v1/projects/active").json()["path"])
    source = workspace / "02_split_text" / "chapter.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("A short chapter.", encoding="utf-8")
    with SessionLocal.begin() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        account.available_units = 1000

    submitted = client.post(
        "/api/script/generate-files",
        headers={"X-CSRF-Token": csrf},
        json={
            "files": [source.name],
            "checks": {
                "check_chunk_alignment": False,
                "check_boundary_speakers": False,
                "validate_instructs": False,
                "revalidate_splits": True,
                "check_long_paragraphs": False,
                "spot_check_enabled": False,
            },
        },
    )
    assert submitted.status_code == 200, submitted.text
    task_id = submitted.json()["task_ids"][0]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task is not None
        gen = task.payload["config"]["generation"]
    assert gen["check_chunk_alignment"] is False
    assert gen["check_boundary_speakers"] is False
    assert gen["validate_instructs"] is False
    assert gen["revalidate_splits"] is True  # 显式提交 True 也原样固化
    assert gen["check_long_paragraphs"] is False
    assert gen["spot_check_enabled"] is False
    # checks 之外的键保留生效配置值（合并只覆盖 6 键，不清空快照）
    assert gen["chunk_size"] == 3000
    assert gen["spot_check_rate"] == 0.05

    # 不带 checks（旧客户端兼容）：6 键取当前生效配置值（默认全 True）
    plain = client.post(
        "/api/script/generate-files",
        headers={"X-CSRF-Token": csrf},
        json={"files": [source.name]},
    )
    assert plain.status_code == 200, plain.text
    plain_id = plain.json()["task_ids"][0]
    with SessionLocal() as db:
        plain_task = db.get(Task, plain_id)
        assert plain_task is not None
        plain_gen = plain_task.payload["config"]["generation"]
    for key in (
        "check_chunk_alignment", "check_boundary_speakers", "validate_instructs",
        "revalidate_splits", "check_long_paragraphs", "spot_check_enabled",
    ):
        assert plain_gen[key] is True


def test_idempotent_task_submission_and_quota_guard(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Queue book"}).json()
    with SessionLocal.begin() as db:
        account = db.get(UserQuotaAccount, first["user"]["id"])
        assert account is not None
        account.available_units = 0
    response = client.post("/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json={"project_id": project["id"], "task_type": "script.parse", "payload": {}, "idempotency_key": "request-123456"})
    assert response.status_code == 409

    # A zero-cost task can be submitted and a duplicate request returns the
    # same persisted row rather than creating a second business effect.
    payload = {"project_id": project["id"], "task_type": "text.format", "payload": {"value": 1}, "idempotency_key": "request-123457"}
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


def test_admin_can_cancel_persistent_task_and_keep_quota_unchanged(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    with SessionLocal.begin() as db:
        user = db.get(User, first["user"]["id"])
        assert user is not None
        user.role = "admin"
        # The module-scoped database retains tasks submitted by earlier tests;
        # this case exercises an intentionally drained storage migration.
        for task in db.scalars(select(Task).where(Task.status.in_(ACTIVE_TASK_STATUSES))).all():
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
    created = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "My workspace"})
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


def test_workspaces_for_different_users_have_separate_username_roots(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    first_info = client.get("/api/v1/projects/active").json()
    second = _register(client, f"{uuid.uuid4()}@example.com")
    second_info = client.get("/api/v1/projects/active").json()

    assert first_info["path"] != second_info["path"]
    assert first_info["path"].replace("\\", "/").split("/")[-2] == first["user"]["username"]
    assert second_info["path"].replace("\\", "/").split("/")[-2] == second["user"]["username"]


def test_legacy_workspace_is_managed_and_requires_authenticated_csrf(client: TestClient):
    anonymous = TestClient(app)
    assert anonymous.get("/api/v1/projects/active").status_code == 401

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    current = client.get("/api/v1/projects/active")
    assert current.status_code == 200, current.text
    info = current.json()
    assert info["project_id"]
    assert info["path"].replace("\\", "/").split("/")[-2] == first["user"]["username"]

    rejected = client.put(
        "/api/v1/projects/active",
        headers={"X-CSRF-Token": csrf},
        json={"path": str(Path.cwd())},
    )
    assert rejected.status_code == 422, rejected.text
    uploaded = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("legacy.txt", b"legacy content", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    legacy_file = uploaded.json()
    legacy_split = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": legacy_file["project_id"],
            "task_type": "book.split",
            "payload": {"input_file_id": legacy_file["file_id"], "whole_book": True},
            "idempotency_key": f"legacy-book-split-{uuid.uuid4()}",
        },
    )
    assert legacy_split.status_code == 201, legacy_split.text
    assert process_task_message(
        {"payload": {"task_id": legacy_split.json()["id"]}}, worker_id="test-book-worker"
    ) == "succeeded"
    project_files = client.get(f"/api/v1/projects/{legacy_file['project_id']}/files").json()
    assert any(item["module"] == "02_split_text" for item in project_files)

    formatted_upload = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("format.txt", b"text to format", "text/plain")},
    )
    assert formatted_upload.status_code == 200, formatted_upload.text
    formatted_upload = formatted_upload.json()
    formatted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": formatted_upload["project_id"],
            "task_type": "text.format",
            "payload": {"input_file_id": formatted_upload["file_id"], "publish_module": "01_input"},
            "idempotency_key": f"legacy-text-format-{uuid.uuid4()}",
        },
    )
    assert formatted.status_code == 201, formatted.text
    assert formatted.json()["id"]
    assert process_task_message(
        {"payload": {"task_id": formatted.json()["id"]}}, worker_id="test-format-worker"
    ) == "succeeded"

    durable_upload = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("durable.txt", b"durable source", "text/plain")},
    )
    assert durable_upload.status_code == 200, durable_upload.text
    submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": legacy_file["project_id"],
            "task_type": "book.split",
            "payload": {"input_file_id": durable_upload.json()["id"], "whole_book": True},
            "idempotency_key": f"legacy-book-split-{uuid.uuid4()}",
        },
    )
    assert submitted.status_code == 201, submitted.text
    assert process_task_message({"payload": {"task_id": submitted.json()["id"]}}, worker_id="test-worker") == "succeeded"
    split_listing = client.get("/api/files/list/02_split_text?recursive=true")
    assert split_listing.status_code == 200
    assert split_listing.json()["items"]


def test_durable_worker_splits_chapterless_text_by_length(client: TestClient):
    # 零章节兜底：未识别出章节结构的文本用 by_length 分册（约 3000 字/册、
    # 字数平均、只切段落/句子边界）。
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    # Chapter-less text: one giant paragraph of 310 sentences x 20 chars
    # (6200 chars total) — no chapter markers, no blank-line gaps.
    body = (("甲" * 19 + "。") * 310).encode("utf-8")
    uploaded = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("novel.txt", body, "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    source = uploaded.json()
    submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": source["project_id"],
            "task_type": "book.split",
            "payload": {"input_file_id": source["file_id"], "by_length": True},
            "idempotency_key": f"book-split-length-{uuid.uuid4()}",
        },
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]
    assert process_task_message({"payload": {"task_id": task_id}}, worker_id="test-length-split-worker") == "succeeded"
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    assert task["status"] == "succeeded"
    result = task["result"]
    assert result["engine"] == "book.split"
    assert result["length_target"] == 3000  # default target
    assert result["file_count"] == 2
    assert [c["actions"] for c in result["chapters"]] == [["length_split"], ["length_split"]]
    chars = [c["chars"] for c in result["chapters"]]
    assert sum(chars) == 6200
    assert all(c >= 2800 for c in chars)  # even division, no short tail
    project_files = client.get(f"/api/v1/projects/{source['project_id']}/files").json()
    split_files = [item for item in project_files if item["module"] == "02_split_text"]
    assert len(split_files) == 2


def test_smart_split_admin_policy_snapshot_idempotency_retry_and_legacy(client: TestClient):
    account = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = {"X-CSRF-Token": account["csrf_token"]}
    settings_url = "/api/v1/admin/settings/application"
    assert client.patch(settings_url, headers=csrf, json={"split": {"smart_split_long_chapters": False}}).status_code == 403
    with SessionLocal.begin() as db:
        db.get(User, account["user"]["id"]).role = "admin"
        previous = db.get(SystemConfig, "application.features")
        previous_value = dict(previous.value) if previous else None
    try:
        response = client.patch(settings_url, headers=csrf, json={
            "split": {"length_target": 2000, "smart_split_long_chapters": True},
            "text": {"split_long_continuous_chapters": True},
        })
        assert response.status_code == 200, response.text
        assert response.json()["config"]["split"]["smart_split_long_chapters"] is True
        saved = client.put("/api/config", headers=csrf, json={"split": {"smart_split_long_chapters": False},
                                                          "text": {"split_long_continuous_chapters": False}})
        assert saved.status_code == 200, saved.text
        assert saved.json()["split"]["smart_split_long_chapters"] is True
        assert saved.json()["text"]["split_long_continuous_chapters"] is False
        with SessionLocal() as db:
            assert "split_long_continuous_chapters" not in db.get(SystemConfig, "application.features").value["text"]

        text = "\n\n".join(
            f"第{i}章 标题{i}\n\n" + (chr(ord("甲") + i) * 99 + "。") * (150 if i == 2 else 30)
            for i in range(1, 6)
        )
        upload = client.post("/api/files/upload", headers=csrf, files={"file": ("long-novel.txt", text.encode(), "text/plain")})
        assert upload.status_code == 200, upload.text
        source = upload.json()
        request = {
            "project_id": source["project_id"], "task_type": "book.split",
            "payload": {"input_file_id": source["file_id"], "smart": True,
                        "split_policy": {"smart_split_long_chapters": False, "length_target": 100}},
            "idempotency_key": f"smart-policy-{uuid.uuid4()}",
        }
        # Direct submissions use the project preference, ignoring forged policy.
        request["payload"]["split_policy"]["split_long_continuous_chapters"] = True
        protected = client.post("/api/v1/tasks", headers=csrf, json=request)
        assert protected.status_code == 201, protected.text
        protected_id = protected.json()["id"]
        with SessionLocal() as db:
            assert db.get(Task, protected_id).payload["split_policy"]["split_long_continuous_chapters"] is False
        assert process_task_message({"payload": {"task_id": protected_id}}, worker_id="test-protected-policy") == "succeeded"
        assert client.get(f"/api/v1/tasks/{protected_id}").json()["result"]["file_count"] == 5
        saved = client.put("/api/config", headers=csrf, json={"text": {"split_long_continuous_chapters": True}})
        assert saved.status_code == 200 and saved.json()["text"]["split_long_continuous_chapters"] is True
        request["idempotency_key"] = f"smart-policy-{uuid.uuid4()}"
        submitted = client.post("/api/v1/tasks", headers=csrf, json=request)
        assert submitted.status_code == 201, submitted.text
        task_id = submitted.json()["id"]
        with SessionLocal() as db:
            snapshot = db.get(Task, task_id).payload["split_policy"]
            assert snapshot == {"smart_split_long_chapters": True, "length_target": 2000,
                                "split_long_continuous_chapters": True}
        response = client.patch(settings_url, headers=csrf, json={"split": {"length_target": 5000, "smart_split_long_chapters": False}})
        assert response.status_code == 200, response.text
        replay = client.post("/api/v1/tasks", headers=csrf, json=request)
        assert replay.status_code == 201 and replay.json()["id"] == task_id
        # Retry retains the original server snapshot even after defaults change.
        with SessionLocal.begin() as db:
            db.get(Task, task_id).status = "failed"
        retried = client.post(f"/api/v1/tasks/{task_id}/retry", headers=csrf)
        assert retried.status_code == 200, retried.text
        assert process_task_message({"payload": {"task_id": task_id}}, worker_id="test-smart-policy") == "succeeded"
        result = client.get(f"/api/v1/tasks/{task_id}").json()["result"]
        assert result["split_policy"]["enabled"] is True
        assert result["split_policy"]["length_target"] == 2000
        parts = [c for c in result["chapters"] if "long_chapter_split" in c["actions"]]
        assert len(parts) == 5
        assert all(c["orig_num"] == 2 and c["long_split"]["segment_count"] == 5 for c in parts)

        # New disabled tasks and pre-upgrade tasks both preserve the middle chapter.
        for legacy in (False, True):
            request["idempotency_key"] = f"smart-policy-{uuid.uuid4()}"
            submitted = client.post("/api/v1/tasks", headers=csrf, json=request)
            assert submitted.status_code == 201, submitted.text
            new_id = submitted.json()["id"]
            if legacy:
                with SessionLocal.begin() as db:
                    task = db.get(Task, new_id)
                    task.payload = {k: v for k, v in task.payload.items() if k != "split_policy"}
            assert process_task_message({"payload": {"task_id": new_id}}, worker_id="test-smart-policy-old") == "succeeded"
            old_result = client.get(f"/api/v1/tasks/{new_id}").json()["result"]
            assert old_result["file_count"] == 5
            assert old_result["split_policy"]["enabled"] is False
    finally:
        with SessionLocal.begin() as db:
            row = db.get(SystemConfig, "application.features")
            if previous_value is None:
                if row:
                    db.delete(row)
            elif row:
                row.value = previous_value
        update_feature_defaults_cache(previous_value or {})


def test_durable_worker_by_length_uses_admin_configured_split_target(client: TestClient):
    # 分册目标字数在管理员后台配置（application.features 的 split 段，100~200000）：
    # by_length 任务未显式带 length_target 时用配置值，payload 仍可逐任务覆盖。
    admin = _register(client, f"{uuid.uuid4()}@example.com")
    admin_csrf = admin["csrf_token"]
    with SessionLocal.begin() as db:
        db.get(User, admin["user"]["id"]).role = "admin"

    # 越界值被拒绝（下限 100 / 上限 200000）。
    for bad in (50, 200_001):
        rejected = client.patch(
            "/api/v1/admin/settings/application",
            headers={"X-CSRF-Token": admin_csrf},
            json={"split": {"length_target": bad}},
        )
        assert rejected.status_code == 422, rejected.text

    patched = client.patch(
        "/api/v1/admin/settings/application",
        headers={"X-CSRF-Token": admin_csrf},
        json={"split": {"length_target": 2000}, "llm": {"api_key": "e2e-shared-llm-key"}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["config"]["split"]["length_target"] == 2000
    assert patched.json()["config"]["llm"]["api_key"] == "e2e-shared-llm-key"
    try:
        first = _register(client, f"{uuid.uuid4()}@example.com")
        csrf = first["csrf_token"]
        # 用户侧 PUT 响应是有效合并配置：split 段回管理员配置值（而非工作区强制
        # 默认），否则前端整店替换（切主题等）会把工作台展示的目标字数刷回 3000。
        saved = client.put(
            "/api/config",
            headers={"X-CSRF-Token": csrf},
            json={"log": {"level": "DEBUG"}},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["log"]["level"] == "DEBUG"
        assert saved.json()["split"]["length_target"] == 2000
        # 合并视图携带的管理员共享凭据不得经用户侧响应泄漏（与 GET 掩码一致）。
        assert saved.json()["llm"]["api_key"] == ""
        # 6200 chars：单个长段落，310 个 20 字段落句（无章节标记、无空行）。
        body = (("甲" * 19 + "。") * 310).encode("utf-8")
        uploaded = client.post(
            "/api/files/upload",
            headers={"X-CSRF-Token": csrf},
            files={"file": ("novel.txt", body, "text/plain")},
        )
        assert uploaded.status_code == 200, uploaded.text
        source = uploaded.json()

        # 未显式带 length_target → 用管理员配置的 2000（6200 → 3 册、册均约 2067）。
        submitted = client.post(
            "/api/v1/tasks",
            headers={"X-CSRF-Token": csrf},
            json={
                "project_id": source["project_id"],
                "task_type": "book.split",
                "payload": {"input_file_id": source["file_id"], "by_length": True},
                "idempotency_key": f"book-split-length-cfg-{uuid.uuid4()}",
            },
        )
        assert submitted.status_code == 201, submitted.text
        task_id = submitted.json()["id"]
        assert process_task_message({"payload": {"task_id": task_id}}, worker_id="test-length-cfg-worker") == "succeeded"
        result = client.get(f"/api/v1/tasks/{task_id}").json()["result"]
        assert result["length_target"] == 2000
        assert result["file_count"] == 3
        chars = [c["chars"] for c in result["chapters"]]
        assert sum(chars) == 6200
        assert all(c >= 1800 for c in chars)  # 册均接近目标，无短尾章

        # payload 的 length_target 仍可覆盖配置值（3000 → 2 册）。
        submitted2 = client.post(
            "/api/v1/tasks",
            headers={"X-CSRF-Token": csrf},
            json={
                "project_id": source["project_id"],
                "task_type": "book.split",
                "payload": {"input_file_id": source["file_id"], "by_length": True, "length_target": 3000},
                "idempotency_key": f"book-split-length-override-{uuid.uuid4()}",
            },
        )
        assert submitted2.status_code == 201, submitted2.text
        task_id2 = submitted2.json()["id"]
        assert process_task_message({"payload": {"task_id": task_id2}}, worker_id="test-length-override-worker") == "succeeded"
        result2 = client.get(f"/api/v1/tasks/{task_id2}").json()["result"]
        assert result2["length_target"] == 3000
        assert result2["file_count"] == 2

        # 零章节 book.analyze 的提示文案与 by_length 分支同一解析来源（配置值 2000）。
        analyzed_upload = client.post(
            "/api/files/upload",
            headers={"X-CSRF-Token": csrf},
            files={"file": ("analyze-nochap.txt", (("甲" * 19 + "。") * 100).encode("utf-8"), "text/plain")},
        )
        assert analyzed_upload.status_code == 200, analyzed_upload.text
        analyzed = client.post(
            "/api/v1/tasks",
            headers={"X-CSRF-Token": csrf},
            json={
                "project_id": source["project_id"],
                "task_type": "book.analyze",
                "payload": {"input_file_id": analyzed_upload.json()["file_id"]},
                "idempotency_key": f"book-analyze-nochap-{uuid.uuid4()}",
            },
        )
        assert analyzed.status_code == 201, analyzed.text
        analyzed_id = analyzed.json()["id"]
        assert process_task_message({"payload": {"task_id": analyzed_id}}, worker_id="test-analyze-nochap-worker") == "succeeded"
        analyze_result = client.get(f"/api/v1/tasks/{analyzed_id}").json()["result"]
        assert analyze_result["analysis"]["chapter_count"] == 0
        assert "约 2000 字/册" in analyze_result["analysis"]["error"]
    finally:
        # 清理功能默认行与进程内缓存（模块级共享 DB，避免影响后续测试）。
        with SessionLocal.begin() as db:
            row = db.get(SystemConfig, "application.features")
            if row is not None:
                db.delete(row)
        update_feature_defaults_cache({})


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
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    with SessionLocal() as db:
        cut = project_workspace_path(db, first["user"]["username"], project_id) / "07_output" / "cut.mp3"
    cut.parent.mkdir(parents=True, exist_ok=True)
    cut.write_bytes(b"completed cut audio")
    record_delivery(first["user"]["id"], project_id, "07_output/cut.mp3")
    source["path"] = str(cut)
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
    activated = client.put(
        "/api/v1/projects/active",
        headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"]},
    )
    assert activated.status_code == 200, activated.text
    uploaded = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("chapter.txt", "  第一章  \n你好...\n".encode("utf-8"), "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
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
            "idempotency_key": "worker-book-analysis-123",
        },
    )
    assert analysis.status_code == 201, analysis.text
    assert process_task_message(
        {"payload": {"task_id": analysis.json()["id"]}}, worker_id="test-book-analysis-worker"
    ) == "succeeded"
    formatted_name = Path(task["result"]["path"]).name
    assert client.get(f"/api/files/download/01_input/{formatted_name}").status_code == 403
    downloaded = client.get(f"/api/files/preview/01_input/{formatted_name}")
    assert downloaded.status_code == 200
    assert "第一章" in downloaded.text
    ranged = client.get(
        f"/api/files/preview/01_input/{formatted_name}",
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

    # The original upload row is soft-deleted when the format result publishes
    # into 01_input (module replacement), so the raw-text analysis input must be
    # a fresh upload that outlives the earlier publish.
    analyzed_upload = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("analyze.txt", "  第一章  \n你好...\n".encode("utf-8"), "text/plain")},
    )
    assert analyzed_upload.status_code == 200, analyzed_upload.text
    analyzed = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "book.analyze",
            "payload": {"input_file_id": analyzed_upload.json()["id"]},
            "idempotency_key": "worker-book-analyze-123",
        },
    )
    assert analyzed.status_code == 201, analyzed.text
    analyzed_id = analyzed.json()["id"]
    assert process_task_message({"payload": {"task_id": analyzed_id}}, worker_id="test-worker") == "succeeded"
    analyzed_task = client.get(f"/api/v1/tasks/{analyzed_id}").json()
    assert analyzed_task["status"] == "succeeded"
    assert "/00_temp/" in analyzed_task["result"]["object_key"]
    assert analyzed_task["result"]["analysis"]["chapter_count"] == 1
    assert analyzed_task["result"]["analysis"]["chapters"][0]["title"] == ""

    split_input = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("split.txt", "第一章\n第一段\n第二章\n第二段\n".encode("utf-8"), "text/plain")},
    )
    assert split_input.status_code == 200, split_input.text
    split_submitted = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"],
            "task_type": "book.split",
            "payload": {"input_file_id": split_input.json()["id"], "smart": True},
            "idempotency_key": "worker-book-split-123",
        },
    )
    assert split_submitted.status_code == 201, split_submitted.text
    split_id = split_submitted.json()["id"]
    stale_key = f"{project['directory_key']}/02_split_text/\u65e7\u4e66 \u5168\u4e66.txt"
    manual_key = f"{project['directory_key']}/02_split_text/manual.txt"
    with SessionLocal() as db:
        for name, key in (("\u65e7\u4e66 \u5168\u4e66.txt", stale_key), ("manual.txt", manual_key)):
            stale_path = object_path(key, configured_storage_root(db))
            stale_path.parent.mkdir(parents=True, exist_ok=True)
            stale_path.write_text("old", encoding="utf-8")
            db.add(ProjectFile(
                project_id=project["id"],
                owner_id=first["user"]["id"],
                original_name=name,
                object_key=key,
                content_type="text/plain; charset=utf-8",
                size_bytes=3,
                sha256=sha256_file(stale_path),
                kind="artifact",
            ))
        db.commit()
    assert process_task_message({"payload": {"task_id": split_id}}, worker_id="test-worker") == "succeeded"
    split_task = client.get(f"/api/v1/tasks/{split_id}").json()
    assert split_task["status"] == "succeeded"
    assert split_task["result"]["file_count"] == 2
    assert len(split_task["result"]["files"]) == 2
    assert all("/02_split_text/" in item["path"].replace("\\", "/") for item in split_task["result"]["files"])
    with SessionLocal() as db:
        stale = db.scalar(select(ProjectFile).where(ProjectFile.object_key == stale_key))
        manual = db.scalar(select(ProjectFile).where(ProjectFile.object_key == manual_key))
        assert stale is not None and stale.deleted_at is not None
        assert manual is not None and manual.deleted_at is None
        assert not object_path(stale_key, configured_storage_root(db)).exists()
        assert object_path(manual_key, configured_storage_root(db)).is_file()

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
    activated = client.put(
        "/api/v1/projects/active",
        headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"]},
    )
    assert activated.status_code == 200, activated.text
    uploaded = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("chapter.txt", b"narrator: hello", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
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
    # 输入指纹随结果落盘：解析页用它判断同名文件被覆盖后旧结果是否过期。
    assert result["result"]["source_sha256"] == hashlib.sha256(b"narrator: hello").hexdigest()
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
    activated = client.put(
        "/api/v1/projects/active",
        headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"]},
    )
    assert activated.status_code == 200, activated.text
    uploaded = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("merged.mp3", b"audio", "audio/mpeg")},
    )
    assert uploaded.status_code == 200, uploaded.text
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
        json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "idempotency_key": "cancel-before-claim"},
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
                json={"project_id": project_id, "task_type": "text.format", "payload": {}, "idempotency_key": key},
            )
            assert response.status_code == 201, response.text
            return response.json()["id"]

        first_task = submit(client, first_csrf, first_project["id"], "fair-first-1")
        submit(client, first_csrf, first_project["id"], "fair-first-2")
        second_task = submit(second_client, second_csrf, second_project["id"], "fair-second-1")

    claim_one = claim_fair_task("fair-worker-1", task_types=("text.format",))
    assert claim_one is not None
    claim_two = claim_fair_task("fair-worker-2", task_types=("text.format",))
    assert claim_two is not None
    assert claim_one.task_id == first_task
    assert claim_two.task_id == second_task


def test_idempotent_replay_survives_estimated_units_removal(client: TestClient):
    """M5: ``estimated_units`` is gone from the submission surface. Historical
    requests (which all sent 0) keep the exact idempotency hash — the key is
    pinned to a constant — so a replay of an estimated_units=0-era request
    still matches, even when the old client keeps sending the (now ignored)
    field and the new one omits it: same key, same task, no 409."""
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Replay book"}).json()
    base = {"project_id": project["id"], "task_type": "text.format", "payload": {"value": 1}, "idempotency_key": "replay-unit-1"}
    legacy = client.post("/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json={**base, "estimated_units": 0})
    assert legacy.status_code == 201, legacy.text
    modern = client.post("/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json=base)
    assert modern.status_code == 201, modern.text
    assert modern.json()["id"] == legacy.json()["id"]


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
        json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "idempotency_key": "recovery-lease-123"},
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
    assert cancellation_requested(claim_one) is True
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
        json={"project_id": project["id"], "task_type": "book.analyze", "payload": {}, "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        task.status = "retrying"
        task.next_attempt_at = utcnow() + timedelta(minutes=1)
    assert claim_task(task_id, "direct-worker") is None
    assert claim_fair_task("fair-worker", task_types=("book.analyze",)) is None
    with SessionLocal.begin() as db:
        db.get(Task, task_id).next_attempt_at = utcnow() - timedelta(seconds=1)
    claim = claim_fair_task("fair-worker", task_types=("book.analyze",))
    assert claim is not None and claim.task_id == task_id


def test_llm_unavailable_task_pauses_and_resumes_even_after_attempt_limit(client: TestClient, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from backend.platform import task_worker
    from backend.platform.platform_settings import settings

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "LLM recovery"},
    ).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {},
              "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]

    for index in range(settings.task_max_attempts + 1):
        claim = claim_task(task_id, "llm-recovery-worker")
        assert claim is not None
        assert fail_claim(claim, TaskExecutionError("llm_unavailable", "endpoint offline")) == "paused"
        if index < settings.task_max_attempts:
            with SessionLocal.begin() as db:
                task = db.get(Task, task_id)
                task.status = "retrying"
                task.error_code = "llm_unavailable"

    monkeypatch.setattr(task_worker, "project_workspace_path", lambda *_args: tmp_path)
    probe_calls: list[dict] = []
    monkeypatch.setattr(
        task_worker.core_config,
        "get_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(model_dump=lambda **_kwargs: {
            "base_url": "http://llm.test/v1", "api_key": "secret",
            "model_name": "qwen3-27b",
        })),
    )
    monkeypatch.setattr(
        "backend.engines.llm_transport.llm_server_is_alive",
        lambda *args, **kwargs: probe_calls.append(kwargs) or True,
    )

    assert resume_llm_unavailable_tasks() == 1
    # The probe must be told WHICH model must be loaded — "server answers
    # /models" alone is exactly the false recovery that redispatched the
    # incident tasks into model_not_found again.
    assert probe_calls == [{"model_name": "qwen3-27b"}]
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task is not None and task.status == "retrying"
        assert task.error_code == "llm_unavailable"

    resumed_claim = claim_task(task_id, "llm-recovery-worker")
    assert resumed_claim is not None
    assert resumed_claim.attempt_no == settings.task_max_attempts + 2


def test_llm_http_error_status_mapping(client: TestClient, monkeypatch):
    """LLMHTTPError 按状态码分流：404（model_not_found，通常是服务重启后模型
    尚未加载）→ 暂停等恢复探针；400/401/403/422（配置错误）→ 终态快速失败，
    不再走可重试的 worker_error 循环。"""
    from dataclasses import replace
    from backend.engines.llm_transport import LLMHTTPError
    from backend.platform import task_worker

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "LLM HTTP mapping"},
    ).json()

    def submit() -> str:
        out = client.post(
            "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
            json={"project_id": project["id"], "task_type": "text.format", "payload": {},
                  "idempotency_key": uuid.uuid4().hex},
        )
        assert out.status_code == 201, out.text
        return out.json()["id"]

    raised: dict = {}

    def fake_execute(_claim):
        raise raised["exc"]

    monkeypatch.setattr(task_worker, "execute_claim", fake_execute)

    # 404 → paused / llm_unavailable（恢复探针每分钟检查模型是否已加载）
    raised["exc"] = LLMHTTPError(404, "model_not_found")
    task_404 = submit()
    assert process_task_message(
        {"payload": {"task_id": task_404}}, worker_id="http-map-worker") == "paused"
    with SessionLocal() as db:
        task = db.get(Task, task_404)
        assert task.status == "paused"
        assert task.error_code == "llm_unavailable"
        assert "model_not_found" in (task.error_message or "")

    # 401 → 终态 llm_configuration_error（密钥错误重试一万次也一样）
    raised["exc"] = LLMHTTPError(401, "invalid api key")
    task_401 = submit()
    assert process_task_message(
        {"payload": {"task_id": task_401}}, worker_id="http-map-worker") == "llm_configuration_error"
    with SessionLocal() as db:
        task = db.get(Task, task_401)
        assert task.status == "failed"
        assert task.error_code == "llm_configuration_error"


def test_lease_renewal_survives_transient_db_error(client: TestClient, monkeypatch):
    """租约续期线程的一次性 DB 抖动不能杀死心跳：心跳线程死了，活任务就会丢租约
    被别的 worker 抢走（进度归零重跑）——这正是本次事故的放大器。"""
    from dataclasses import replace
    from backend.engines.llm_transport import LLMUnavailableError
    from backend.platform import task_worker
    from backend.platform.platform_settings import settings
    from backend.platform.task_worker import _run_claim_fenced

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Lease renewal"},
    ).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {},
              "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text

    claim = claim_task(submitted.json()["id"], "lease-renewal-worker")
    assert claim is not None

    # 1s 心跳间隔（默认 120s 租约 → 10s 间隔，测试里等不起）
    monkeypatch.setattr(task_worker, "settings", replace(settings, task_lease_seconds=3))

    beats: list = []

    def flaky_heartbeat(clm):
        beats.append(True)
        if len(beats) == 1:
            raise RuntimeError("transient db blip")
        return True

    monkeypatch.setattr(task_worker, "heartbeat_claim", flaky_heartbeat)

    def slow_engine(_clm):
        time.sleep(2.6)
        raise LLMUnavailableError("connection refused")

    monkeypatch.setattr(task_worker, "execute_claim", slow_engine)

    assert _run_claim_fenced(claim) == "paused"
    # 第一次心跳抛异常被吞掉后，线程必须在 ~2s 处再次 tick（主线程睡了 2.6s）。
    assert len(beats) >= 2


def test_resume_probe_uses_payload_snapshot_config(client: TestClient, monkeypatch, tmp_path):
    """恢复探针必须按任务 payload 的配置快照探活（任务重派后仍按快照执行）：
    暂停期间用户改了工作区 model_name，按活动配置探活会通过、把任务重派回旧
    模型 → 再次 404 → 无限循环。"""
    from types import SimpleNamespace
    from backend.platform import task_worker

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Probe snapshot"},
    ).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {},
              "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]

    claim = claim_task(task_id, "probe-snapshot-worker")
    assert claim is not None
    assert fail_claim(claim, TaskExecutionError("llm_unavailable", "endpoint offline")) == "paused"

    # 提交时快照（任务实际会按它执行）与活动工作区配置指向不同的模型
    with SessionLocal.begin() as db:
        db.get(Task, task_id).payload = {
            "config": {"llm": {
                "base_url": "http://snap.test/v1", "api_key": "snap-key",
                "model_name": "snapshot-model",
            }},
        }

    # 清掉共享库里其他测试留下的 paused llm_unavailable 任务（恢复扫描是全库的）
    with SessionLocal.begin() as db:
        for row in db.execute(
            select(Task)
            .where(Task.status == "paused", Task.error_code == "llm_unavailable")
        ).scalars():
            if row.id != task_id:
                row.status = "cancelled"
                row.finished_at = utcnow()
                row.updated_at = utcnow()

    monkeypatch.setattr(task_worker, "project_workspace_path", lambda *_args: tmp_path)
    monkeypatch.setattr(
        task_worker.core_config,
        "get_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(model_dump=lambda **_kwargs: {
            "base_url": "http://live.test/v1", "api_key": "live-key",
            "model_name": "live-model",
        })),
    )
    probe_calls: list[tuple] = []
    monkeypatch.setattr(
        "backend.engines.llm_transport.llm_server_is_alive",
        lambda *args, **kwargs: probe_calls.append((args, kwargs)) or True,
    )

    assert resume_llm_unavailable_tasks() == 1
    assert probe_calls == [
        (("http://snap.test/v1", "snap-key"), {"model_name": "snapshot-model"}),
    ]
    with SessionLocal() as db:
        assert db.get(Task, task_id).status == "retrying"


def test_resume_stops_and_fails_task_after_max_cycles(client: TestClient, monkeypatch, tmp_path):
    """端点自报健康但重派后仍持续失败（/models 与 chat 矛盾的病态状态）：
    连续失败到阈值后任务终态化，不再无限 pause/resume。"""
    from backend.platform import task_worker
    from backend.platform.task_lifecycle import append_task_event

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Recovery cap"},
    ).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {},
              "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]

    claim = claim_task(task_id, "recovery-cap-worker")
    assert claim is not None
    assert fail_claim(claim, TaskExecutionError("llm_unavailable", "endpoint offline")) == "paused"

    # fail_claim 已记 1 次；补齐到阈值
    with SessionLocal.begin() as db:
        for index in range(task_worker.LLM_RECOVERY_MAX_CYCLES - 1):
            append_task_event(db, task_id, "llm_unavailable", {"attempt_id": f"cycle-{index}"})

    # 清掉共享库里其他测试留下的 paused llm_unavailable 任务（恢复扫描是全库的）
    with SessionLocal.begin() as db:
        for row in db.execute(
            select(Task)
            .where(Task.status == "paused", Task.error_code == "llm_unavailable")
        ).scalars():
            if row.id != task_id:
                row.status = "cancelled"
                row.finished_at = utcnow()
                row.updated_at = utcnow()

    monkeypatch.setattr(task_worker, "project_workspace_path", lambda *_args: tmp_path)
    monkeypatch.setattr(
        task_worker.core_config,
        "get_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(model_dump=lambda **_kwargs: {
            "base_url": "http://llm.test/v1", "api_key": "secret",
        })),
    )
    monkeypatch.setattr("backend.engines.llm_transport.llm_server_is_alive", lambda *_args, **_kwargs: True)

    assert resume_llm_unavailable_tasks() == 0
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        assert task.status == "failed"
        assert task.error_code == "llm_unavailable"
        events = {row.event_type for row in db.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id)).scalars()}
        assert "llm_recovery_exhausted" in events


def test_resume_config_helper_prefers_snapshot(monkeypatch):
    """_resume_llm_config：快照可用则用快照；快照缺失/无 base_url 回退工作区配置。"""
    from types import SimpleNamespace
    from backend.platform import task_worker

    workspace = {"base_url": "http://ws.test/v1", "api_key": "ws", "model_name": "ws-model"}

    def row(payload):
        return SimpleNamespace(payload=payload)

    # 完整快照 → 用快照
    assert task_worker._resume_llm_config(
        row({"config": {"llm": {"base_url": "http://snap.test/v1", "model_name": "snap-model"}}}),
        workspace) == {"base_url": "http://snap.test/v1", "model_name": "snap-model"}
    # 无快照 / 空 payload / 快照无 base_url → 回退工作区配置
    assert task_worker._resume_llm_config(row({}), workspace) == workspace
    assert task_worker._resume_llm_config(row(None), workspace) == workspace
    assert task_worker._resume_llm_config(
        row({"config": {"llm": {"base_url": ""}}}), workspace) == workspace
    # 两侧都没有 → None
    assert task_worker._resume_llm_config(row({}), None) is None


def test_expired_cancelling_attempt_releases_tts_hold(client: TestClient):
    from backend.platform.quota import reserve_tts_quota, reset_quota_context, set_quota_context

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Cancel recovery"}).json()
    with SessionLocal.begin() as db:
        db.get(UserQuotaAccount, first["user"]["id"]).available_units = 10
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "idempotency_key": uuid.uuid4().hex},
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
        json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    claim = claim_task(submitted.json()["id"], "publish-rollback-worker")
    assert claim is not None
    with SessionLocal() as db:
        workspace = project_workspace_path(db, first["user"]["username"], project["id"])
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


def test_invalid_publish_module_fails_task_with_actionable_code(client: TestClient, monkeypatch, tmp_path):
    from backend.platform import task_worker

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Invalid publish module"},
    ).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"], "task_type": "text.format", "payload": {},
            "idempotency_key": uuid.uuid4().hex,
        },
    )
    assert submitted.status_code == 201, submitted.text
    staged = tmp_path / "bad-module.txt"
    staged.write_text("result", encoding="utf-8")
    monkeypatch.setattr(task_worker, "execute_claim", lambda _claim: TaskOutcome(
        temp_path=staged, output_name="bad-module.txt", content_type="text/plain",
        size_bytes=staged.stat().st_size, sha256=sha256_file(staged), metadata={},
        publish_module="outside",
    ))

    result = process_task_message(
        {"payload": {"task_id": submitted.json()["id"]}}, worker_id="bad-publish-module-worker",
    )

    assert result == "invalid_publish_module"
    with SessionLocal() as db:
        task = db.get(Task, submitted.json()["id"])
        assert task.status == "failed"
        assert task.error_code == "invalid_publish_module"
    assert not staged.exists()


def test_incremental_legacy_workspace_write_rolls_back_with_task_commit(client: TestClient, monkeypatch):
    from backend.platform import task_worker

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Incremental journal"}).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "tts.reset", "payload": {"scripts": []},
        "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    claim = claim_task(submitted.json()["id"], "incremental-journal-worker")
    assert claim is not None
    with SessionLocal() as db:
        workspace = project_workspace_path(db, first["user"]["username"], project["id"])
        staged = task_attempt_path(db, first["user"]["username"], project["id"], claim.task_id, claim.attempt_id, "result.json")
    final = workspace / "04_voice_profiles" / "voice_config.json"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"old config")
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"task result")

    handle = PersistentTaskHandle(claim)
    handle.stage_workspace_file(final, b"new config")
    assert final.read_bytes() == b"new config"
    outcome = TaskOutcome(
        temp_path=staged, output_name="result.json", content_type="application/json",
        size_bytes=staged.stat().st_size, sha256=sha256_file(staged), metadata={},
        publication_journal=handle.publication_journal,
    )

    original_factory = task_worker.SessionLocal

    def failing_session():
        session = original_factory()
        session.commit = lambda: (_ for _ in ()).throw(RuntimeError("commit failed"))
        return session

    monkeypatch.setattr(task_worker, "SessionLocal", failing_session)
    with pytest.raises(RuntimeError, match="commit failed"):
        complete_claim(claim, outcome)
    assert final.read_bytes() == b"old config"
    assert not (staged.parent / "publication.json").exists()


def test_expired_attempt_restores_interrupted_publication(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Interrupted publish"}).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": project["id"], "task_type": "text.format", "payload": {}, "idempotency_key": uuid.uuid4().hex},
    )
    claim = claim_task(submitted.json()["id"], "interrupted-publish-worker")
    assert claim is not None
    with SessionLocal() as db:
        root = configured_storage_root(db)
        workspace = project_workspace_path(db, first["user"]["username"], project["id"])
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


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("recover", [False, True, "legacy"])
def test_tts_partial_progress_survives_failure_and_worker_recovery(client, monkeypatch, multi, recover):
    import json
    from backend.engines import tts_batch
    from backend.platform import engine_task_executor
    from backend.platform.task_engine_support import engine_execution_context

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf},
                          json={"name": "TTS checkpoints"}).json()
    scripts = ["one.json", "two.json"] if multi else ["one.json"]
    # Set up the attempt without requiring real TTS quota/model dependencies.
    task_id = str(uuid.uuid4())
    with SessionLocal.begin() as db:
        db.add(Task(id=task_id, owner_id=first["user"]["id"], project_id=project["id"],
                    task_type="tts.batch", status="pending", payload={"scripts": scripts}))
    claim = claim_task(task_id, "tts-checkpoint-worker")
    assert claim is not None

    def interrupted_synthesis(handle, *_args):
        layout = core_paths.get_or_prepare_layout()
        for name in scripts:
            source = [{"speaker": "A", "text": "完成"}, {"speaker": "A", "text": "待合成"}]
            (layout.parsed_json / name).write_text(json.dumps(source), encoding="utf-8")
            segments = tts_batch.build_segments(source)
            out = layout.audio_chunk / tts_batch.package_for(Path(name))
            stage = handle.allocate_workspace_directory(out)
            audio = stage / "0001.mp3"
            audio.write_bytes(b"completed audio")
            results = {}
            tts_batch._handle_segment(f"[segment] 0 ok {audio}", {0: segments[0]}, 2,
                                     results, handle, stage, out)
            for _ in range(2):
                # Exercise repeated manifest replacement in the same attempt.
                tts_batch.write_manifest_file(out / "manifest.json", tts_batch.build_manifest(
                    segments, {}, results, root=layout.workspace), handle)
        handle.publish_workspace_bytes(layout.voice_profiles / "temporary.json", b"temporary")
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(tts_batch, "synthesize_multi" if multi else "synthesize", interrupted_synthesis)
    if recover:
        handle = PersistentTaskHandle(claim)
        with engine_execution_context(claim), pytest.raises(RuntimeError, match="simulated interruption"):
            engine_task_executor._run_tts_batch(handle, claim, claim.payload, [], [])
        if recover == "legacy":
            journal = handle.publication_journal
            # Construct a pre-checkpoint v1 snapshot from the registered files;
            # new attempts use the append-only v2 format.
            data = {"version": 1, "files": [
                {"final": str(final.relative_to(journal.root)),
                 "backup": str(backup.relative_to(journal.root)),
                 "had_original": had_original}
                for final, backup, had_original, _guard in journal.entries
            ]}
            journal.path.write_text(json.dumps(data), encoding="utf-8")
        with SessionLocal.begin() as db:
            db.get(TaskAttempt, claim.attempt_id).lease_expires_at = utcnow() - timedelta(seconds=1)
        recover_database_tasks()
    else:
        with pytest.raises(RuntimeError, match="simulated interruption"):
            engine_task_executor.execute_engine_task(claim)

    with engine_execution_context(claim):
        layout = core_paths.get_or_prepare_layout()
        assert not (layout.voice_profiles / "temporary.json").exists()
        for name in scripts:
            out = layout.audio_chunk / tts_batch.package_for(Path(name))
            entries = tts_batch.read_manifest(out)
            done = tts_batch.done_indices(entries, out, layout.workspace)
            assert done == {0}
            assert tts_batch.plan_to_synthesize([0, 1], done) == {1}
            assert (out / "0001.mp3").read_bytes() == b"completed audio"
        # The same read path used by the UI must expose retained completion.
        from backend.api.tts import batch_status
        status = batch_status(script=None, scripts=scripts)
        assert all(row["completed"] == 1 and row["remaining"] == 1 for row in status["files"])


@pytest.mark.parametrize(("committed", "should_exist"), [(False, True), (True, False)])
def test_publication_reconciles_directory_removal(tmp_path, committed, should_exist):
    root = tmp_path / "storage"
    target = root / "user" / "project" / "05_audio_chunk" / "chapter"
    target.mkdir(parents=True)
    (target / "keep.wav").write_bytes(b"audio")
    journal_path = root / "user" / "project" / ".tasks" / "task" / "attempt" / "publication.json"
    journal = PublicationJournal(root, journal_path)
    journal.prepare()
    journal.remove(target)

    assert not target.exists()
    assert PublicationJournal.reconcile(root, journal_path, committed=committed)
    assert target.exists() is should_exist
    if should_exist:
        assert (target / "keep.wav").read_bytes() == b"audio"
    assert not journal_path.exists()


def test_audio_export_stages_replacement_until_commit(client: TestClient, monkeypatch):
    from backend.platform import task_worker

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Export journal"}).json()
    with SessionLocal() as db:
        workspace = project_workspace_path(db, first["user"]["username"], project["id"])
    source = workspace / "07_output" / "source.wav"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source audio")
    record_delivery(first["user"]["id"], project["id"], "07_output/source.wav")
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"], "task_type": "audio.export",
            "payload": {"source_relative": "07_output/source.wav", "files": [
                {"relative_path": "07_output/source.wav", "name": "take.wav"},
            ]},
            "idempotency_key": uuid.uuid4().hex,
        },
    )
    assert submitted.status_code == 201, submitted.text
    claim = claim_task(submitted.json()["id"], "export-journal-worker")
    assert claim is not None
    with SessionLocal() as db:
        workspace = project_workspace_path(db, first["user"]["username"], project["id"])
    source = workspace / "07_output" / "source.wav"
    final = workspace / "07_output" / "分集" / "take.wav"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"original export")

    outcome = execute_claim(claim)
    assert final.read_bytes() == b"original export"
    assert len(outcome.side_effect_outputs) == 1
    assert outcome.side_effect_outputs[0].temp_path.read_bytes() == b"source audio"

    original_factory = task_worker.SessionLocal

    def failing_session():
        session = original_factory()
        session.commit = lambda: (_ for _ in ()).throw(RuntimeError("commit failed"))
        return session

    monkeypatch.setattr(task_worker, "SessionLocal", failing_session)
    with pytest.raises(RuntimeError, match="commit failed"):
        complete_claim(claim, outcome)
    assert final.read_bytes() == b"original export"


def test_tts_reset_restores_deleted_package_when_commit_fails(client: TestClient, monkeypatch):
    from backend.platform import task_worker

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Reset journal"}).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project["id"], "task_type": "tts.reset", "payload": {"scripts": ["chapter.json"]},
            "idempotency_key": uuid.uuid4().hex,
        },
    )
    assert submitted.status_code == 201, submitted.text
    claim = claim_task(submitted.json()["id"], "reset-journal-worker")
    assert claim is not None
    with SessionLocal() as db:
        workspace = project_workspace_path(db, first["user"]["username"], project["id"])
    package = workspace / "05_audio_chunk" / "chapter"
    package.mkdir(parents=True, exist_ok=True)
    (package / "keep.wav").write_bytes(b"existing audio")

    outcome = execute_claim(claim)
    assert package.is_dir()
    assert outcome.side_effect_deletes == (package,)

    original_factory = task_worker.SessionLocal

    def failing_session():
        session = original_factory()
        session.commit = lambda: (_ for _ in ()).throw(RuntimeError("commit failed"))
        return session

    monkeypatch.setattr(task_worker, "SessionLocal", failing_session)
    with pytest.raises(RuntimeError, match="commit failed"):
        complete_claim(claim, outcome)
    assert (package / "keep.wav").read_bytes() == b"existing audio"


def test_interrupted_storage_migration_blocks_writes_and_resumes(client: TestClient, tmp_path):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    workspace = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Storage resume"}).json()
    with SessionLocal.begin() as db:
        db.get(User, first["user"]["id"]).role = "admin"
        for task in db.scalars(select(Task).where(Task.status.in_(ACTIVE_TASK_STATUSES))).all():
            task.status = "cancelled"
            task.finished_at = utcnow()
        source_root = configured_storage_root(db)
        target_root = (tmp_path / "resumed-root").resolve()
        db.add(SystemConfig(key="storage.migration", value={"source": str(source_root), "target": str(target_root)}))
    source = source_root / workspace["directory_key"]
    target = target_root / workspace["directory_key"]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    assert client.get("/api/v1/projects/active").status_code == 409
    assert not source.exists()
    blocked = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
        json={"project_id": workspace["id"], "task_type": "text.format", "payload": {}, "idempotency_key": uuid.uuid4().hex},
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


def test_admin_gpu_sample_is_ttl_cached(monkeypatch):
    """Q20: the Admin page refreshes every ~15 s; each poll reads /overview
    and /performance, both on the same TTL-cached nvidia-smi sample — the
    spawn runs under the lock, so a stale window costs one spawn even for
    racing callers (and idle polls inside the TTL window cost none)."""
    from backend.api import admin

    samples: list[list[dict]] = []

    def fake_sample() -> list[dict]:
        rows = [{"index": 0, "name": "Test GPU"}]
        samples.append(rows)
        return rows

    monkeypatch.setattr(admin, "_sample_gpus", fake_sample)
    monkeypatch.setattr(admin, "_gpu_sample", None)

    try:
        # Three refresh cycles — one spawn per TTL window, repeated reads
        # inside a window share the sample (no double spawn on racing
        # callers either: the spawn runs under the lock).
        for expected in (1, 2, 3):
            if admin._gpu_sample is not None:
                admin._gpu_sample = (
                    admin._gpu_sample[0] - admin._GPU_SAMPLE_TTL_SECONDS - 1, admin._gpu_sample[1],
                )
            admin._gpu_status()
            assert len(samples) == expected, f"cycle {expected} cost {len(samples)} spawns"
            assert admin._gpu_status() == admin._gpu_sample[1]
            assert len(samples) == expected
    finally:
        admin._gpu_sample = None


def test_task_stream_polls_on_event_loop_not_threadpool(client: TestClient):
    """Q4: the SSE body must be an *async* generator. Starlette bridges sync
    generators through ``iterate_in_threadpool`` — one thread-pool worker per
    0.5 s poll — so idle connections would occupy workers linearly. An async
    generator drives on the event loop and merely awaits while waiting."""
    import asyncio
    import inspect

    from starlette.concurrency import iterate_in_threadpool
    from starlette.requests import Request

    from backend.api import platform_tasks as api_platform_tasks
    from backend.platform.platform_settings import settings
    from backend.platform.security import load_session

    _register(client, f"{uuid.uuid4()}@example.com")
    token = client.cookies.get(settings.session_cookie)
    assert token
    with SessionLocal() as db:
        session = load_session(db, token, touch=False)
        assert session is not None
        user = session.user
    request = Request({
        "type": "http", "method": "GET", "path": "/api/v1/tasks/stream",
        "headers": [(b"cookie", f"{settings.session_cookie}={token}".encode("ascii"))],
        "query_string": b"",
    })

    # Structural: starlette only thread-pool-bridges NON-async iterables, so
    # an unbridged async body is precisely what keeps idle connections off
    # the worker pool.
    body = api_platform_tasks.stream_user_tasks(request, user).body_iterator
    assert inspect.isasyncgen(body)
    body_code = getattr(body, "ag_code", None) or getattr(body, "gi_code", None)
    assert body_code is not iterate_in_threadpool.__code__, \
        "SSE body is thread-pool bridged — idle connections would occupy workers"

    # Functional: the generator runs to its first yield (driven on a bare
    # event loop — a blocking time.sleep would freeze the loop here).
    async def first_chunk():
        gen = api_platform_tasks.stream_user_tasks(request, user).body_iterator
        try:
            return await gen.__anext__()
        finally:
            await gen.aclose()

    assert "snapshot_all" in asyncio.run(first_chunk())


def test_task_stream_serves_concurrent_readers(client: TestClient):
    """Several concurrent SSE connections each get their snapshot and their
    poll tick. Driven straight against the endpoint's StreamingResponse ASGI
    callable on one event loop (the test client's httpx transport buffers
    whole responses and cannot follow an infinite stream) — so every
    generator iteration also runs on the loop itself: exactly the
    no-thread-worker shape the production server gets from the async body."""
    import asyncio
    import time

    from backend.api import platform_tasks as api_platform_tasks
    from backend.platform.platform_settings import settings
    from backend.platform.security import load_session
    from starlette.requests import Request

    _register(client, f"{uuid.uuid4()}@example.com")
    token = client.cookies.get(settings.session_cookie)
    assert token
    with SessionLocal() as db:
        session = load_session(db, token, touch=False)
        assert session is not None
        user = session.user
    # The body re-checks ``request.is_disconnected`` every poll, so the fake
    # request needs a receive channel. One shared wire: it stays silent until
    # BOTH readers below have seen their chunks, then reports the disconnect —
    # each stream (the generator's non-blocking peek, and starlette's
    # disconnect listener) reads it independently.
    disconnected = asyncio.Event()

    async def wire_receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http", "method": "GET", "path": "/api/v1/tasks/stream",
            "headers": [(b"cookie", f"{settings.session_cookie}={token}".encode("ascii"))],
            "query_string": b"",
        },
        receive=wire_receive,
    )

    done = 0

    async def drive(num_chunks: int) -> tuple:
        nonlocal done
        chunks: list = []
        start: dict = {}

        async def send(message: dict) -> None:
            nonlocal done
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"])
                if len(chunks) == num_chunks:
                    done += 1
                    if done >= 2:
                        disconnected.set()

        started = time.monotonic()
        # The deadline bounds the SCHEDULING of a stream that must end the
        # moment the shared disconnect fires — not a latency SLO. 15 s
        # flipped to a spurious TimeoutError on a loaded machine (both
        # readers are driven on one loop; under CPU contention the poll
        # ticks and the disconnect peek simply land late). 120 s keeps the
        # hang-detection property while tolerating any realistic load.
        await asyncio.wait_for(
            api_platform_tasks.stream_user_tasks(request, user)(
                {"type": "http", "headers": []}, wire_receive, send,
            ),
            timeout=120,
        )
        return start, chunks, time.monotonic() - started

    async def probe():
        return await asyncio.gather(drive(2), drive(2))

    (start_a, chunks_a, _), (start_b, chunks_b, _) = asyncio.run(probe())
    for start, chunks in ((start_a, chunks_a), (start_b, chunks_b)):
        assert start["status"] == 200
        assert b"text/event-stream" in dict(start["headers"]).get(b"content-type", b"")
        assert b"snapshot_all" in chunks[0]
        assert b"ping" in chunks[1]  # the 0.5 s poll ticked while the reader was idle


def test_task_stream_poll_units_run_in_worker_threads(client: TestClient, monkeypatch):
    """M3: aggregate_stream's synchronous DB units run in pool threads, not on
    the event loop — a slow poll cannot freeze every other connection — and
    closing the stream leaves every submitted unit completed (no abandoned
    pool work queued behind a dead reader)."""
    import asyncio
    import threading
    import time

    from backend.services import task_views

    state = {"submitted": 0, "completed": 0}
    heartbeats = 0
    windows: list[tuple[int, int, int]] = []
    frames: list[str] = []

    def slow_frames(rows_fn, seen, delivered):
        state["submitted"] += 1
        before = heartbeats
        ident = threading.get_ident()
        time.sleep(0.25)  # stands in for a slow query
        state["completed"] += 1
        windows.append((ident, before, heartbeats))
        return []

    def ok_auth(auth_token, user_id):
        state["submitted"] += 1
        state["completed"] += 1
        return True

    monkeypatch.setattr(task_views, "_new_frames", slow_frames)
    monkeypatch.setattr(task_views, "session_still_valid", ok_auth)

    async def drive():
        nonlocal heartbeats
        loop_ident = threading.get_ident()
        stop = asyncio.Event()
        disconnected = asyncio.Event()

        async def heartbeat():
            nonlocal heartbeats
            while not stop.is_set():
                heartbeats += 1
                await asyncio.sleep(0.02)

        async def is_disconnected():
            return disconnected.is_set()

        hb = asyncio.create_task(heartbeat())

        async def consume():
            gen = task_views.aggregate_stream(lambda db: [], "tok", "user-1", is_disconnected)
            try:
                while True:
                    frames.append(await asyncio.wait_for(gen.__anext__(), timeout=10))
            except StopAsyncIteration:
                pass

        cons = asyncio.create_task(consume())
        await asyncio.sleep(1.6)  # ~two 0.5 s ticks, each with the 0.25 s unit
        disconnected.set()
        await cons
        stop.set()
        await hb
        return loop_ident, heartbeats

    loop_ident, total_heartbeats = asyncio.run(drive())

    assert len(windows) >= 2, f"expected several poll ticks, saw {len(windows)}"
    # Every unit ran off the event-loop thread.
    assert all(ident != loop_ident for ident, _b, _a in windows)
    # The loop stayed responsive DURING each unit's sleep: a frozen loop
    # would show 0 heartbeats inside the window (the floor accounts for
    # Windows' ~15 ms event-loop timer granularity on the 0.02 s heartbeat).
    assert all(after - before >= 3 for _ident, before, after in windows), \
        f"event loop starved while a poll unit ran: {windows}"
    assert total_heartbeats >= 20, f"heartbeat itself starved: {total_heartbeats}"
    # The stream actually ticked.
    assert "snapshot_all" in frames[0] and any("ping" in f for f in frames[1:])
    # No queue leak: everything submitted before the close has completed.
    deadline = time.monotonic() + 5
    while state["completed"] < state["submitted"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state["completed"] == state["submitted"]


def test_task_event_stream_poll_runs_in_worker_threads(client: TestClient, monkeypatch):
    """M3 (second surface): the per-task ``/{task_id}/events`` loop hands its
    auth + poll units to pool threads the same way the aggregate stream does."""
    import asyncio
    import threading
    import time

    from starlette.requests import Request

    from backend.api import platform_tasks as api_platform_tasks
    from backend.platform.platform_settings import settings
    from backend.platform.security import load_session

    first = _register(client, f"{uuid.uuid4()}@example.com")
    token = client.cookies.get(settings.session_cookie)
    assert token
    with SessionLocal() as db:
        session = load_session(db, token, touch=False)
        user = session.user
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": first["csrf_token"]}, json={"name": "evt"},
    ).json()
    submitted = client.post(
        "/api/v1/tasks", headers={"X-CSRF-Token": first["csrf_token"]},
        json={"project_id": project["id"], "task_type": "tts.reset", "payload": {"scripts": []},
        "idempotency_key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 201, submitted.text
    task_id = submitted.json()["id"]

    state = {"submitted": 0, "completed": 0}
    idents: list[int] = []

    def fake_auth(cookie, user_id):
        state["submitted"] += 1
        idents.append(threading.get_ident())
        time.sleep(0.1)
        state["completed"] += 1
        return True

    class _Event:
        sequence = 1
        event_type = "progress"
        payload = {"progress": 50, "current": "x"}

    def fake_poll(tid, uid, after):
        # One event on the first poll, then silence — keeps the generator
        # alive (non-terminal, no yield is fine) without ending it.
        state["submitted"] += 1
        idents.append(threading.get_ident())
        time.sleep(0.1)
        state["completed"] += 1
        return SimpleNamespace(status="running"), ([_Event()] if after == 0 else []), False

    monkeypatch.setattr(api_platform_tasks, "_stream_session_valid", fake_auth)
    monkeypatch.setattr(api_platform_tasks, "_stream_poll", fake_poll)

    # Starlette's is_disconnected peeks the receive channel non-blocking: the
    # wire stays silent until the test reports the disconnect.
    disconnected = asyncio.Event()

    async def wire_receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http", "method": "GET", "path": f"/api/v1/tasks/{task_id}/events",
            "headers": [(b"cookie", f"{settings.session_cookie}={token}".encode("ascii"))],
            "query_string": b"",
        },
        receive=wire_receive,
    )

    body = api_platform_tasks.stream_task_events(task_id, request, user, last_event_id=None).body_iterator

    async def drive():
        loop_ident = threading.get_ident()
        chunks: list[str] = []

        async def consume():
            try:
                while True:
                    chunks.append(await asyncio.wait_for(body.__anext__(), timeout=10))
            except StopAsyncIteration:
                pass

        cons = asyncio.create_task(consume())
        await asyncio.sleep(1.8)
        disconnected.set()
        await cons
        return loop_ident, chunks

    loop_ident, chunks = asyncio.run(drive())

    assert len(idents) >= 4, f"expected auth + several poll ticks, saw {len(idents)}"
    assert all(ident != loop_ident for ident in idents)
    assert any("progress" in c for c in chunks)
    deadline = time.monotonic() + 5
    while state["completed"] < state["submitted"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state["completed"] == state["submitted"]


def test_workspace_rollback_guard_uses_task_lock_and_fingerprint(client: TestClient):
    """M1 wiring on the real handle: the engine registers mark_workspace_guarded
    + set_rollback_lock, and rollback restores the guarded shared-cache entry
    only while its bytes still match the task's publication — a concurrent
    writer's version survives, and the same entry is restored when untouched."""
    from backend.engines import bgm_storage

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Guard wiring"}).json()
    username = first["user"]["username"]

    def _submit() -> dict:
        response = client.post(
            "/api/v1/tasks", headers={"X-CSRF-Token": csrf},
            json={"project_id": project["id"], "task_type": "tts.reset", "payload": {"scripts": []},
            "idempotency_key": uuid.uuid4().hex},
        )
        assert response.status_code == 201, response.text
        return response.json()

    with SessionLocal() as db:
        root = configured_storage_root(db)
        bgm_dir = project_workspace_path(db, username, project["id"]) / "08_bgm"
    bgm_dir.mkdir(parents=True, exist_ok=True)
    layout = SimpleNamespace(bgm=bgm_dir)
    final = bgm_dir / "chapter_music_analysis.json"
    final.write_text('{"version": 1, "model": "", "chapters": {}}', encoding="utf-8")

    # 1) concurrent writer wins: the API process rewrites under the same lock.
    submitted = _submit()
    claim = claim_task(submitted["id"], "guard-wiring-worker")
    assert claim is not None
    handle = PersistentTaskHandle(claim)
    handle.mark_workspace_guarded(final)
    handle.set_rollback_lock(bgm_storage.storage_lock, layout)
    handle.publish_workspace_bytes(final, b'{"published": true}')
    assert final.read_text("utf-8") == '{"published": true}'
    with bgm_storage.storage_lock(layout):
        final.write_text('{"concurrent": true}', encoding="utf-8")
    handle.rollback_publications()
    assert final.read_text("utf-8") == '{"concurrent": true}'

    # 2) untouched entry is restored to its pre-publication bytes.
    final.write_text('{"fresh": true}', encoding="utf-8")
    first_claim = claim
    claim = claim_task(_submit()["id"], "guard-wiring-worker-2", defer_workspace_conflicts=False)
    assert claim is not None
    handle = PersistentTaskHandle(claim)
    handle.mark_workspace_guarded(final)
    handle.set_rollback_lock(bgm_storage.storage_lock, layout)
    handle.publish_workspace_bytes(final, b'{"published": true}')
    handle.rollback_publications()
    assert final.read_text("utf-8") == '{"fresh": true}'

    # Both journals are cleaned up (no file, no orphaned backups).
    with SessionLocal() as db:
        for task_claim in (first_claim, claim):
            journal = task_attempt_path(
                db, username, project["id"], task_claim.task_id, task_claim.attempt_id, "publication.json",
            )
            assert not journal.exists()
            assert not list(journal.parent.glob("publication-backup-*"))


@pytest.mark.parametrize("workspace_change", [False, True])
@pytest.mark.parametrize("fail_commit", [False, True])
def test_shared_music_and_workspace_result_publish_atomically(client, monkeypatch, tmp_path, workspace_change, fail_commit):
    from backend.platform import task_worker
    from backend.engines import music

    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project = client.post("/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Shared publication"}).json()
    with SessionLocal.begin() as db:
        db.get(User, first["user"]["id"]).role = "admin"
        db.merge(UserQuotaAccount(user_id=first["user"]["id"], available_units=10))
        workspace = project_workspace_path(db, first["user"]["username"], project["id"])
    library = tmp_path / "music"
    library.mkdir()
    shared = library / "music_index.json"
    shared.write_bytes(b"original")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", library)
    workspace_file = workspace / "04_voice_profiles" / "fixture.json"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_bytes(b"old workspace")

    def suggest(handle, *args, **kwargs):
        handle.stage_shared_file(shared, b"new music")
        if workspace_change:
            handle.stage_workspace_file(workspace_file, b"new workspace")
        return {"scene": ["test"], "mood": [], "emotion": []}

    monkeypatch.setattr(music, "suggest_track_tags", suggest)
    submitted = client.post("/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json={
        "project_id": project["id"], "task_type": "music.suggest_tags", "payload": {"name": "track.mp3"},
        "idempotency_key": uuid.uuid4().hex,
    })
    assert submitted.status_code == 201, submitted.text
    claim = claim_task(submitted.json()["id"], "shared-publication-worker")
    outcome = execute_claim(claim)
    factory = task_worker.SessionLocal
    if fail_commit:
        def failing_session():
            session = factory()
            session.commit = lambda: (_ for _ in ()).throw(RuntimeError("commit failed"))
            return session
        monkeypatch.setattr(task_worker, "SessionLocal", failing_session)
        with pytest.raises(RuntimeError, match="commit failed"):
            complete_claim(claim, outcome)
        assert shared.read_bytes() == b"original"
        assert workspace_file.read_bytes() == b"old workspace"
        with factory() as db:
            assert db.get(Task, claim.task_id).status == "running"
            assert not db.scalars(select(ProjectFile).where(ProjectFile.project_id == project["id"], ProjectFile.kind == "artifact")).all()
    else:
        assert complete_claim(claim, outcome)
        assert shared.read_bytes() == b"new music"
        assert workspace_file.read_bytes() == (b"new workspace" if workspace_change else b"old workspace")
        with factory() as db:
            assert db.get(Task, claim.task_id).status == "succeeded"
            output = db.scalar(select(ProjectFile).where(ProjectFile.project_id == project["id"], ProjectFile.kind == "artifact"))
            assert json.loads(object_path(output.object_key, configured_storage_root(db)).read_text())["scene"] == ["test"]
    assert not list(library.rglob("publication.json"))
    assert not list((workspace / "00_temp" / "tasks").rglob("publication.json"))


def test_workspace_busy_tasks_stay_queued_while_other_work_is_claimed(client, monkeypatch):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first['csrf_token']
    from backend.platform import task_worker
    original_eligibility=task_worker._workspace_claim_eligibility
    monkeypatch.setattr(task_worker,'_workspace_claim_eligibility',lambda now: original_eligibility(now) & (Task.owner_id==first['user']['id']))
    with SessionLocal.begin() as db:
        db.merge(UserQuotaAccount(user_id=first['user']['id'], available_units=100))
    project = client.post('/api/v1/projects', headers={'X-CSRF-Token':csrf}, json={'name':'Long TTS'}).json()
    other = client.post('/api/v1/projects', headers={'X-CSRF-Token':csrf}, json={'name':'Independent'}).json()
    def submit(kind, payload, target=project):
        response=client.post('/api/v1/tasks',headers={'X-CSRF-Token':csrf},json={
            'project_id':target['id'],'task_type':kind,'payload':payload,'idempotency_key':uuid.uuid4().hex})
        assert response.status_code==201,response.text
        return response.json()['id']
    running=submit('tts.batch',{'scripts':['chapter.json']})
    assert claim_task(running,'model-worker')
    merge=submit('tts.merge',{'package':'chapter'})
    mutation=submit('tts.reset',{'scripts':['chapter.json']})
    independent=submit('tts.reset',{'scripts':[]},other)
    assert claim_task(merge,'redis-delivery-worker') is None
    claim=claim_fair_task('mechanical-worker',task_types=('tts.merge','tts.reset'))
    assert claim and claim.task_id==independent
    with SessionLocal() as db:
        assert db.get(Task,merge).status=='pending'
        assert db.get(Task,mutation).status=='pending'
        assert not db.scalars(select(TaskAttempt).where(TaskAttempt.task_id.in_((merge,mutation)))).all()
    with SessionLocal.begin() as db:
        db.get(Task,running).status='succeeded'
        for attempt in db.scalars(select(TaskAttempt).where(TaskAttempt.task_id==running)):
            attempt.status='succeeded'
    claim=claim_fair_task('mechanical-worker',task_types=('tts.merge',))
    assert claim and claim.task_id==merge


def test_workspace_admission_preserves_parallel_chapters_and_defers_same_target(client, monkeypatch):
    first=_register(client,f'{uuid.uuid4()}@example.com')
    csrf=first['csrf_token']
    project=client.post('/api/v1/projects',headers={'X-CSRF-Token':csrf},json={'name':'Parallel audio'}).json()
    from backend.platform import task_worker
    original_eligibility=task_worker._workspace_claim_eligibility
    monkeypatch.setattr(task_worker,'_workspace_claim_eligibility',lambda now: original_eligibility(now) & (Task.owner_id==first['user']['id']))
    def submit(kind,payload):
        response=client.post('/api/v1/tasks',headers={'X-CSRF-Token':csrf},json={
            'project_id':project['id'],'task_type':kind,'payload':payload,'idempotency_key':uuid.uuid4().hex})
        assert response.status_code==201,response.text
        return response.json()['id']
    first_merge=submit('tts.merge',{'package':'chapter-one'})
    assert claim_task(first_merge,'merge-one')
    same=submit('bgm.mix',{'stem':'chapter-one'})
    different=submit('tts.merge',{'package':'chapter-two'})
    writer=submit('tts.reset',{'scripts':[]})
    assert claim_task(same,'same-target') is None
    assert claim_task(writer,'exclusive-writer') is None
    claim=claim_fair_task('parallel-merge',task_types=('tts.merge','bgm.mix','tts.reset'))
    assert claim and claim.task_id==different


def test_concurrent_same_named_uploads_use_flat_readable_names_without_overwriting(client: TestClient):
    from concurrent.futures import ThreadPoolExecutor
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    def upload(content):
        return client.post(
            "/api/files/upload", headers={"X-CSRF-Token": csrf},
            files={"file": ("same.txt", content, "text/plain")},
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(upload, [b"first version", b"second version"]))
    assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
    paths = [Path(response.json()["path"]) for response in responses]
    assert {path.name for path in paths} == {"same.txt", "same (2).txt"}
    assert all(path.parent.name == "01_input" for path in paths)
    assert {path.read_bytes() for path in paths} == {b"first version", b"second version"}
    assert len({response.json()["file_id"] for response in responses}) == 2


@pytest.mark.parametrize("name", ["a" * 176 + ".txt", "a" * 180])
def test_long_same_named_uploads_and_worker_outputs_keep_file_indexes(client: TestClient, name):
    first = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = first["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    uploads = []
    for content in (b"first", b"second"):
        response = client.post("/api/files/upload", headers={"X-CSRF-Token": csrf},
                               files={"file": (name, content, "text/plain")})
        assert response.status_code == 200, response.text
        uploads.append(response.json())
    paths = [Path(item["path"]) for item in uploads]
    assert paths[0] != paths[1]
    assert {path.read_bytes() for path in paths} == {b"first", b"second"}
    with SessionLocal() as db:
        for item in uploads:
            record = db.get(ProjectFile, item["file_id"])
            assert object_path(record.object_key, configured_storage_root(db)) == Path(item["path"])
    output_ids = []
    for item in uploads:
        submitted = client.post("/api/v1/tasks", headers={"X-CSRF-Token": csrf}, json={
            "project_id": project_id, "task_type": "text.format",
            "payload": {"input_file_id": item["file_id"], "output_name": name},
            "idempotency_key": uuid.uuid4().hex,
        })
        assert submitted.status_code == 201, submitted.text
        task_id = submitted.json()["id"]
        assert process_task_message({"payload": {"task_id": task_id}}, worker_id="long-name-worker") == "succeeded"
        output_ids.append(client.get(f"/api/v1/tasks/{task_id}").json()["result"]["file_id"])
    with SessionLocal() as db:
        outputs = [db.get(ProjectFile, id) for id in output_ids]
        assert outputs[0].object_key != outputs[1].object_key
        assert {object_path(row.object_key, configured_storage_root(db)).read_text().strip() for row in outputs} == {
            "first", "second",
        }


@pytest.mark.parametrize("target_enabled", [False, True])
def test_smart_split_snapshot_uses_target_project_not_active_project(client: TestClient, target_enabled: bool):
    account = _register(client, f"{uuid.uuid4()}@example.com")
    csrf = {"X-CSRF-Token": account["csrf_token"]}
    saved = client.put("/api/config", headers=csrf,
                       json={"text": {"split_long_continuous_chapters": target_enabled}})
    assert saved.status_code == 200, saved.text
    uploaded = client.post("/api/files/upload", headers=csrf,
                           files={"file": ("target.txt", "第1章 正文。".encode(), "text/plain")})
    assert uploaded.status_code == 200, uploaded.text
    source = uploaded.json()
    other = client.post("/api/v1/projects", headers=csrf, json={"name": "Other active book"})
    assert other.status_code == 201, other.text
    activated = client.put("/api/v1/projects/active", headers=csrf, json={"project_id": other.json()["id"]})
    assert activated.status_code == 200, activated.text
    saved = client.put("/api/config", headers=csrf,
                       json={"text": {"split_long_continuous_chapters": not target_enabled}})
    assert saved.status_code == 200, saved.text
    request = {"project_id": source["project_id"], "task_type": "book.split",
               "payload": {"input_file_id": source["file_id"], "smart": True},
               "idempotency_key": f"target-policy-{uuid.uuid4()}"}
    submitted = client.post("/api/v1/tasks", headers=csrf, json=request)
    assert submitted.status_code == 201, submitted.text
    with SessionLocal() as db:
        assert db.get(Task, submitted.json()["id"]).payload["split_policy"]["split_long_continuous_chapters"] is target_enabled
    # Temporary target binding must not leak back into the active project.
    assert client.get("/api/config").json()["text"]["split_long_continuous_chapters"] is not target_enabled
