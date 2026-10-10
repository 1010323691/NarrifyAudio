from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import app
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import Project, SystemConfig, Task, TaskEvent, User, utcnow
from backend.platform.resource_inventory import internal_path
from backend.platform.system_config import PROJECT_RETENTION_KEY, project_retention
from backend.services.project_retention import purge_expired_projects
from backend.tests.test_project_trash import _account, _project, _trash, _workspace


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


@pytest.fixture(autouse=True)
def _reset_retention():
    yield
    with SessionLocal.begin() as db:
        row = db.get(SystemConfig, PROJECT_RETENTION_KEY)
        if row is not None:
            db.delete(row)


def _set_retention(ttl: int = 0, trash: int = 30) -> None:
    with SessionLocal.begin() as db:
        value = {"project_ttl_days": ttl, "trash_days": trash}
        row = db.get(SystemConfig, PROJECT_RETENTION_KEY)
        if row is None:
            db.add(SystemConfig(key=PROJECT_RETENTION_KEY, value=value))
        else:
            row.value = value


def _age(project_id: str, *, created_days: int = 0, trashed_days: int | None = None) -> None:
    with SessionLocal.begin() as db:
        project = db.get(Project, project_id)
        project.created_at = utcnow() - timedelta(days=created_days)
        if trashed_days is not None:
            project.deleted_at = utcnow() - timedelta(days=trashed_days)


def _exists(project_id: str) -> bool:
    with SessionLocal() as db:
        return db.get(Project, project_id) is not None


def test_defaults_never_expire_live_projects(client):
    assert project_retention_defaults() == {"project_ttl_days": 0, "trash_days": 30}
    _, _, csrf = _account(client)
    project = _project(client, csrf)
    _age(project["id"], created_days=3000)
    purge_expired_projects()
    assert _exists(project["id"])


def project_retention_defaults():
    with SessionLocal() as db:
        return project_retention(db)


def test_live_project_past_its_lifetime_is_removed_with_files_logs_rows_and_exports(client):
    user_id, username, csrf = _account(client)
    project = _project(client, csrf)
    keep = _project(client, csrf)
    root = _workspace(username, project["id"])
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "tts_batch_1.log").write_text("log")
    (root / "07_output").mkdir(parents=True, exist_ok=True)
    (root / "07_output" / "a.wav").write_bytes(b"audio")
    with SessionLocal.begin() as db:
        task = Task(id=str(uuid.uuid4()), owner_id=user_id, project_id=project["id"], task_type="resources.package",
                    status="succeeded", payload={})
        db.add(task)
        db.flush()
        db.add(TaskEvent(task_id=task.id, sequence=1, event_type="progress", payload={}))
        export = internal_path(db, user_id, "exports", task.id)
        snapshots = internal_path(db, user_id, project["id"])
        task_id = task.id
    export.mkdir(parents=True, exist_ok=True)
    (export / "files.zip").write_bytes(b"zip")
    snapshots.mkdir(parents=True, exist_ok=True)
    (snapshots / "current.json").write_text("{}")

    _set_retention(ttl=30)
    _age(project["id"], created_days=31)
    _age(keep["id"], created_days=5)
    assert purge_expired_projects() >= 1

    assert not _exists(project["id"]) and _exists(keep["id"])
    assert not root.exists() and not export.exists() and not snapshots.exists()
    with SessionLocal() as db:
        assert db.get(Task, task_id) is None
        assert db.scalar(select(TaskEvent.id).where(TaskEvent.task_id == task_id)) is None


def test_expiry_skips_busy_projects_and_the_default_workspace(client):
    user_id, username, csrf = _account(client)
    busy = _project(client, csrf)
    with SessionLocal.begin() as db:
        db.add(Task(id=str(uuid.uuid4()), owner_id=user_id, project_id=busy["id"], task_type="tts.batch",
                    status="running", payload={}))
        default_id = db.scalar(select(Project.id).where(Project.owner_id == user_id, Project.name == "默认工作空间"))
    _set_retention(ttl=1)
    _age(busy["id"], created_days=10)
    if default_id:
        _age(default_id, created_days=10)
    purge_expired_projects()
    assert _exists(busy["id"])
    if default_id:
        assert _exists(default_id)


def test_trash_deadline_follows_the_configured_days_and_restore_respects_both_deadlines(client):
    _, _, csrf = _account(client)
    project = _project(client, csrf)
    _trash(client, csrf, project["id"])
    _age(project["id"], trashed_days=10)

    _set_retention(trash=30)
    purge_expired_projects()
    assert _exists(project["id"])  # 10 days into a 30-day trash

    _set_retention(trash=7)
    response = client.post(f"/api/v1/projects/{project['id']}/restore", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 410, response.text
    purge_expired_projects()
    assert not _exists(project["id"])

    late = _project(client, csrf)
    _trash(client, csrf, late["id"])
    _set_retention(ttl=30, trash=30)
    _age(late["id"], created_days=60, trashed_days=1)
    response = client.post(f"/api/v1/projects/{late['id']}/restore", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 410 and "保质期" in response.text


def test_admin_can_read_and_change_retention_and_listings_follow(client):
    username = f"adm{uuid.uuid4().hex[:10]}"
    reg = client.post("/api/auth/register", json={"email": f"{uuid.uuid4()}@example.test", "username": username,
                                                  "password": "test-pass-1234"}).json()
    with SessionLocal.begin() as db:
        db.get(User, reg["user"]["id"]).role = "admin"
    headers = {"X-CSRF-Token": reg["csrf_token"]}
    assert client.get("/api/v1/admin/settings/retention").json() == {"project_ttl_days": 0, "trash_days": 30}
    bad = client.patch("/api/v1/admin/settings/retention", headers=headers, json={"project_ttl_days": -1, "trash_days": 30})
    assert bad.status_code == 422
    assert client.patch("/api/v1/admin/settings/retention", headers=headers,
                        json={"project_ttl_days": 30, "trash_days": 0}).status_code == 422
    ok = client.patch("/api/v1/admin/settings/retention", headers=headers, json={"project_ttl_days": 45, "trash_days": 14})
    assert ok.json() == {"project_ttl_days": 45, "trash_days": 14}

    project = client.post("/api/v1/projects", headers=headers, json={"name": f"p{uuid.uuid4().hex[:6]}"}).json()
    listed = client.get("/api/v1/projects?page=1&page_size=50").json()["items"]
    mine = next(item for item in listed if item["id"] == project["id"])
    assert mine["expires_at"]
    assert client.delete(f"/api/v1/projects/{project['id']}", headers=headers).status_code == 200
    trashed = client.get("/api/v1/projects/trash").json()[0]
    assert trashed["expires_at"][:10] == (utcnow() + timedelta(days=14)).date().isoformat()
