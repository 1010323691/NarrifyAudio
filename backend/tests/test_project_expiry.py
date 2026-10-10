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
def _reset_retention(monkeypatch):
    monkeypatch.setattr("backend.services.project_retention.CANCEL_GRACE_SECONDS", 0.0)
    _clear_config()
    yield
    _clear_config()


def _clear_config():
    from backend.platform.system_config import RETENTION_STARTED_KEY

    with SessionLocal.begin() as db:
        for key in (PROJECT_RETENTION_KEY, RETENTION_STARTED_KEY, "retention.last_run"):
            row = db.get(SystemConfig, key)
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


def test_defaults_are_30_day_projects_7_day_trash_and_zero_disables_expiry(client):
    assert project_retention_defaults() == {"project_ttl_days": 30, "trash_days": 7}
    _, _, csrf = _account(client)
    project = _project(client, csrf)
    _set_retention(ttl=0)
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


def test_expiry_cancels_unfinished_tasks_and_deletes_in_the_same_pass_and_spares_the_default_workspace(client):
    user_id, username, csrf = _account(client)
    busy = _project(client, csrf)
    with SessionLocal.begin() as db:
        running_id = str(uuid.uuid4())
        db.add(Task(id=running_id, owner_id=user_id, project_id=busy["id"], task_type="tts.batch",
                    status="running", payload={}))
        default_id = db.scalar(select(Project.id).where(Project.owner_id == user_id, Project.name == "默认工作空间"))
    _set_retention(ttl=1)
    _age(busy["id"], created_days=10)
    if default_id:
        _age(default_id, created_days=10)
    purge_expired_projects()  # cancel is requested, then the project is deleted although no worker acknowledged it
    assert not _exists(busy["id"])
    with SessionLocal() as db:
        assert db.get(Task, running_id) is None
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
    assert client.get("/api/v1/admin/settings/retention").json() == {"project_ttl_days": 30, "trash_days": 7}
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


def _receipts(project_id):
    from backend.platform.models import ProjectPurgeRecord
    with SessionLocal() as db:
        return list(db.scalars(select(ProjectPurgeRecord).where(ProjectPurgeRecord.project_id == project_id)).all())


def _backdate_receipt(project_id, days=1):
    from backend.platform.models import ProjectPurgeRecord
    with SessionLocal.begin() as db:
        for record in db.scalars(select(ProjectPurgeRecord).where(ProjectPurgeRecord.project_id == project_id)):
            record.purged_at = utcnow() - timedelta(days=days)


def test_purge_leaves_a_receipt_and_the_next_day_check_marks_it_clean(client):
    from backend.services.project_purge_audit import verify_purged_projects

    user_id, username, csrf = _account(client)
    project = _project(client, csrf)
    _trash(client, csrf, project["id"])
    _age(project["id"], trashed_days=40)
    purge_expired_projects()
    (record,) = _receipts(project["id"])
    assert record.reason == "trash" and record.owner_id == user_id and record.verified_at is None

    verify_purged_projects()
    assert _receipts(project["id"])[0].verified_at is None  # same day: not due yet

    _backdate_receipt(project["id"])
    verify_purged_projects()
    record = _receipts(project["id"])[0]
    assert record.verified_at is not None and record.leftovers is None


def test_next_day_check_removes_files_and_rows_written_after_the_deletion(client):
    from backend.services.project_purge_audit import find_traces, verify_purged_projects

    user_id, username, csrf = _account(client)
    project = _project(client, csrf)
    root = _workspace(username, project["id"])
    resources = None
    with SessionLocal() as db:
        resources = internal_path(db, user_id, project["id"])
    _set_retention(ttl=1)
    _age(project["id"], created_days=5)
    purge_expired_projects()
    assert not _exists(project["id"]) and not root.exists()

    # a lingering worker recreates output after the deletion
    (root / "07_output").mkdir(parents=True)
    (root / "07_output" / "late.wav").write_bytes(b"x")
    resources.mkdir(parents=True)
    (resources / "current.json").write_text("{}")
    from backend.platform.models import TextFormatFlow
    with SessionLocal.begin() as db:  # a straggler row (sqlite test DB does not enforce the foreign key)
        db.add(TextFormatFlow(project_id=project["id"], owner_id=user_id, source_file_id=str(uuid.uuid4())))
    _backdate_receipt(project["id"])
    verify_purged_projects()

    record = _receipts(project["id"])[0]
    assert record.leftovers["rows"] == {"text_format_flows": 1}
    assert not root.exists() and not resources.exists()
    assert record.verified_at is not None
    assert record.leftovers and len(record.leftovers["paths"]) == 2
    with SessionLocal() as db:
        assert find_traces(db, record) == {"paths": [], "rows": {}}


def test_daily_retention_pass_runs_once_per_local_day(monkeypatch):
    from backend import worker

    calls = []
    monkeypatch.setattr(worker, "purge_expired_projects", lambda limit: calls.append("p") or 0)
    monkeypatch.setattr(worker, "purge_resource_artifacts", lambda limit: 0)
    monkeypatch.setattr("backend.services.project_purge_audit.verify_purged_projects", lambda limit: 0)
    with SessionLocal.begin() as db:
        row = db.get(SystemConfig, "retention.last_run")
        if row is not None:
            db.delete(row)
    worker._retention_state.update(done_on="", thread=None)

    def run_pass():
        result = worker._retention_pass()
        if worker._retention_state["thread"] is not None:
            worker._retention_state["thread"].join()
        return result

    assert run_pass() == (0, True) and calls == ["p"]
    assert run_pass() == (0, True) and calls == ["p"]                  # same day: cheap no-op
    worker._retention_state.update(done_on="", thread=None)            # restart the same day: DB marker blocks it
    run_pass()
    assert calls == ["p"]
    with SessionLocal.begin() as db:
        db.get(SystemConfig, "retention.last_run").value = {"date": "2000-01-01"}
    worker._retention_state.update(done_on="", thread=None)
    run_pass()                                                         # a later day: runs again
    assert calls == ["p", "p"]


def test_check_never_touches_a_live_project_that_reuses_the_deleted_directory_key(client):
    from backend.services.project_purge_audit import verify_purged_projects

    user_id, username, csrf = _account(client)
    old = _project(client, csrf, "同名书")
    _set_retention(ttl=1)
    _age(old["id"], created_days=5)
    purge_expired_projects()
    assert not _exists(old["id"])
    _set_retention(ttl=30)
    new = _project(client, csrf, "同名书")           # same name -> same name-based directory
    root = _workspace(username, new["id"])
    (root / "07_output").mkdir(parents=True, exist_ok=True)
    (root / "07_output" / "keep.wav").write_bytes(b"live")
    _backdate_receipt(old["id"])
    verify_purged_projects()
    assert (root / "07_output" / "keep.wav").read_bytes() == b"live"
    assert _receipts(old["id"])[0].verified_at is not None


def test_daily_pass_is_not_marked_done_while_storage_is_migrating(monkeypatch):
    from backend import worker

    calls = []
    monkeypatch.setattr(worker, "purge_expired_projects", lambda limit: calls.append("p") or 0)
    monkeypatch.setattr(worker, "purge_resource_artifacts", lambda limit: 0)
    monkeypatch.setattr("backend.platform.storage.storage_migration", lambda db: object())
    with SessionLocal.begin() as db:
        row = db.get(SystemConfig, "retention.last_run")
        if row is not None:
            db.delete(row)
    worker._retention_state.update(done_on="", thread=None)
    worker._retention_pass()
    worker._retention_state["thread"].join()
    assert calls == [] and worker._retention_state["done_on"] == ""


def test_cancel_grace_waits_for_cancelling_tasks_and_gives_up_after_the_deadline(client, monkeypatch):
    import threading
    from backend.services import project_retention as retention

    user_id, _username, csrf = _account(client)
    project = _project(client, csrf)
    with SessionLocal.begin() as db:
        task = Task(id=str(uuid.uuid4()), owner_id=user_id, project_id=project["id"], task_type="tts.batch",
                    status="running", payload={})
        db.add(task)
        task_id = task.id
    _set_retention(ttl=1)
    _age(project["id"], created_days=5)
    monkeypatch.setattr(retention, "CANCEL_GRACE_SECONDS", 5.0)
    monkeypatch.setattr(retention, "_CANCEL_POLL_SECONDS", 0.05)
    seen = {}

    def worker_acknowledges_later():
        import time
        for _ in range(100):                      # wait until the retention pass has requested the cancel
            with SessionLocal() as db:
                seen["status_before_ack"] = db.scalar(select(Task.status).where(Task.id == task_id))
            if seen["status_before_ack"] == "cancelling":
                break
            time.sleep(0.05)
        time.sleep(0.3)
        with SessionLocal.begin() as db:
            db.get(Task, task_id).status = "cancelled"
        seen["acked_at"] = time.monotonic()

    thread = threading.Thread(target=worker_acknowledges_later)
    thread.start()
    purge_expired_projects()
    deleted_at = __import__("time").monotonic()
    thread.join()
    assert seen["status_before_ack"] == "cancelling"
    assert deleted_at >= seen["acked_at"]           # deletion waited for the acknowledgement
    assert not _exists(project["id"])

    stuck = _project(client, csrf)
    with SessionLocal.begin() as db:
        db.add(Task(id=str(uuid.uuid4()), owner_id=user_id, project_id=stuck["id"], task_type="tts.batch",
                    status="running", payload={}))
    _age(stuck["id"], created_days=5)
    monkeypatch.setattr(retention, "CANCEL_GRACE_SECONDS", 0.2)
    purge_expired_projects()                          # nobody acknowledges: deleted after the deadline anyway
    assert not _exists(stuck["id"])


def test_projects_that_predate_the_retention_start_are_aged_from_it(client):
    from backend.platform.system_config import RETENTION_STARTED_KEY, ensure_retention_started

    _, _, csrf = _account(client)
    old = _project(client, csrf)
    trashed = _project(client, csrf)
    _trash(client, csrf, trashed["id"])
    _age(old["id"], created_days=400)
    _age(trashed["id"], created_days=400, trashed_days=100)
    _set_retention(ttl=30, trash=7)
    with SessionLocal.begin() as db:
        row = db.get(SystemConfig, RETENTION_STARTED_KEY)
        if row is not None:
            db.delete(row)
    with SessionLocal() as db:
        ensure_retention_started(db)               # first daily pass ever: the clock starts now
    purge_expired_projects()
    assert _exists(old["id"]) and _exists(trashed["id"])
    with SessionLocal.begin() as db:
        db.get(SystemConfig, RETENTION_STARTED_KEY).value = {"at": (utcnow() - timedelta(days=31)).isoformat()}
    purge_expired_projects()
    assert not _exists(old["id"]) and not _exists(trashed["id"])
    with SessionLocal.begin() as db:
        db.delete(db.get(SystemConfig, RETENTION_STARTED_KEY))


def test_default_workspace_name_is_reserved(client):
    _, _, csrf = _account(client)
    headers = {"X-CSRF-Token": csrf}
    assert client.post("/api/v1/projects", headers=headers, json={"name": "默认工作空间"}).status_code == 422
    normal = _project(client, csrf)
    assert client.patch(f"/api/v1/projects/{normal['id']}", headers=headers, json={"name": "默认工作空间"}).status_code == 422
    listed = client.get("/api/v1/projects?page=1&page_size=50").json()["items"]
    default = next(item for item in listed if item["name"] == "默认工作空间")
    assert client.patch(f"/api/v1/projects/{default['id']}", headers=headers, json={"name": "改名了"}).status_code == 422
