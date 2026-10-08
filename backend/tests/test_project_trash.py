from __future__ import annotations

import shutil
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.main import app
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import (
    ChapterReviewMark,
    OutboxEvent,
    Project,
    ProjectFile,
    QuotaHold,
    QuotaTransaction,
    Task,
    TaskAttempt,
    TaskEvent,
    TaskResult,
    TextFormatFlow,
    WorkerHeartbeat,
    utcnow,
)
from backend.platform.storage import configured_storage_root
from backend.services.project_retention import purge_expired_projects
from backend.worker import _project_retention_loop


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


def _account(client: TestClient) -> tuple[str, str, str]:
    response = client.post(
        "/api/auth/register",
        json={
            "email": f"{uuid.uuid4()}@example.test",
            "username": f"trash{uuid.uuid4().hex[:12]}",
            "password": "test-pass-1234",
            "display_name": "Trash Test",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["user"]["id"], body["user"]["username"], body["csrf_token"]


def _project(client: TestClient, csrf: str, name: str | None = None) -> dict:
    response = client.post(
        "/api/v1/projects",
        json={"name": name or f"项目-{uuid.uuid4().hex[:8]}"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _workspace(username: str, project_id: str) -> Path:
    with SessionLocal() as db:
        return configured_storage_root(db) / db.get(Project, project_id).directory_key


def _trash(client: TestClient, csrf: str, project_id: str) -> None:
    response = client.delete(
        f"/api/v1/projects/{project_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text


def _expire(project_id: str) -> None:
    with SessionLocal.begin() as db:
        project = db.get(Project, project_id)
        assert project is not None
        project.deleted_at = utcnow() - timedelta(days=40)


def test_project_trash_retains_files_hides_tasks_and_restores_them(client: TestClient):
    user_id, username, csrf = _account(client)
    project = _project(client, csrf)
    root = _workspace(username, project["id"])
    retained_file = root / "07_output" / "keep.wav"
    retained_file.parent.mkdir(parents=True, exist_ok=True)
    retained_file.write_bytes(b"audio stays while trashed")
    task = Task(
        owner_id=user_id,
        project_id=project["id"],
        task_type="text.format",
        status="succeeded",
        progress=100,
        payload={"label": "trash visibility"},
    )
    with SessionLocal.begin() as db:
        db.add(task)

    assert task.id in {row["id"] for row in client.get("/api/v1/tasks").json()}
    _trash(client, csrf, project["id"])

    listed = client.get("/api/v1/projects/trash")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [project["id"]]
    assert listed.json()[0]["expires_at"]
    assert (_workspace(username, project["id"]) / "07_output" / "keep.wav").read_bytes() == b"audio stays while trashed"
    assert task.id not in {row["id"] for row in client.get("/api/v1/tasks").json()}

    restored = client.post(
        f"/api/v1/projects/{project['id']}/restore",
        headers={"X-CSRF-Token": csrf},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == project["id"]
    assert retained_file.read_bytes() == b"audio stays while trashed"
    assert task.id in {row["id"] for row in client.get("/api/v1/tasks").json()}
    assert client.get("/api/v1/projects/trash").json() == []


def test_project_restore_renames_name_collisions_and_rejects_expired_projects(client: TestClient):
    _user_id, _username, csrf = _account(client)
    original = _project(client, csrf, "重名项目")
    _trash(client, csrf, original["id"])
    _project(client, csrf, "重名项目")

    restored = client.post(
        f"/api/v1/projects/{original['id']}/restore",
        headers={"X-CSRF-Token": csrf},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["name"] == "重名项目（恢复）"

    expired = _project(client, csrf, "过期项目")
    _trash(client, csrf, expired["id"])
    _expire(expired["id"])
    response = client.post(
        f"/api/v1/projects/{expired['id']}/restore",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 410, response.text
    assert purge_expired_projects() == 1


def test_restore_concurrent_name_conflict_returns_conflict_status(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    _user_id, _username, csrf = _account(client)
    project = _project(client, csrf, "并发恢复项目")
    _trash(client, csrf, project["id"])

    def raise_name_conflict(_db, _project):
        raise IntegrityError("restore", {}, RuntimeError("unique constraint conflict"))

    monkeypatch.setattr("backend.api.projects.restore_project", raise_name_conflict)
    response = client.post(
        f"/api/v1/projects/{project['id']}/restore",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 409, response.text


def test_database_enforces_unique_names_for_active_projects(client: TestClient):
    user_id, _username, csrf = _account(client)
    project = _project(client, csrf, "唯一活动名称")
    duplicate = Project(
        owner_id=user_id,
        name=project["name"],
        directory_key=f"duplicate/{uuid.uuid4()}",
    )
    with pytest.raises(IntegrityError):
        with SessionLocal.begin() as db:
            db.add(duplicate)
            db.flush()


def test_expired_project_purge_removes_database_rows_and_resumes_staged_cleanup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
):
    user_id, username, csrf = _account(client)
    project = _project(client, csrf)
    _trash(client, csrf, project["id"])
    _expire(project["id"])
    root = _workspace(username, project["id"])
    first_file = root / "07_output" / "first.wav"
    second_file = root / "07_output" / "second.wav"
    first_file.parent.mkdir(parents=True, exist_ok=True)
    first_file.write_bytes(b"first")
    second_file.write_bytes(b"second")

    task = Task(
        owner_id=user_id,
        project_id=project["id"],
        task_type="text.format",
        status="succeeded",
        progress=100,
        payload={},
    )
    with SessionLocal.begin() as db:
        db.add(task)
    with SessionLocal.begin() as db:
        db.add_all([
            TaskAttempt(task_id=task.id, attempt_no=1, status="succeeded"),
            TaskEvent(task_id=task.id, sequence=1, event_type="completed", payload={}),
            TaskResult(task_id=task.id, result={"ok": True}),
            ProjectFile(
                project_id=project["id"], owner_id=user_id,
                original_name="first.wav", object_key=f"{db.get(Project, project['id']).directory_key}/07_output/first.wav",
                content_type="audio/wav", size_bytes=5, sha256="a" * 64, kind="artifact",
            ),
            QuotaHold(
                user_id=user_id, task_id=task.id, attempt_id=str(uuid.uuid4()),
                operation_type="tts.generate", units=1,
            ),
            QuotaTransaction(
                user_id=user_id, task_id=task.id, amount=-1, kind="consume",
                idempotency_key=f"trash-purge-{uuid.uuid4()}", note="test",
            ),
            OutboxEvent(
                aggregate_type="task", aggregate_id=task.id,
                event_type="task.completed", payload={},
            ),
            WorkerHeartbeat(
                worker_id=f"trash-test-{uuid.uuid4()}", current_task_id=task.id,
                status="idle", capabilities={},
            ),
            TextFormatFlow(
                project_id=project["id"], owner_id=user_id,
                source_file_id=str(uuid.uuid4()), config_snapshot={},
            ),
            ChapterReviewMark(
                project_id=project["id"], owner_id=user_id,
                task_id=task.id, chapter_key="chapter-1",
            ),
        ])

    real_rmtree = shutil.rmtree

    def fail_after_partial_removal(path, *, onerror=None):
        staged = Path(path)
        next((item for item in staged.rglob("*") if item.is_file()), None).unlink()
        raise OSError("simulated interrupted directory cleanup")

    monkeypatch.setattr("backend.services.projects.shutil.rmtree", fail_after_partial_removal)
    assert purge_expired_projects() == 0
    assert root.exists() is False
    staged_paths = list(root.parent.glob(f".{project['id']}.deleting-*"))
    assert len(staged_paths) == 1
    with SessionLocal() as db:
        assert db.get(Project, project["id"]) is not None

    monkeypatch.setattr("backend.services.projects.shutil.rmtree", real_rmtree)
    assert purge_expired_projects() == 1
    assert not staged_paths[0].exists()
    with SessionLocal() as db:
        assert db.get(Project, project["id"]) is None
        assert db.get(Task, task.id) is None
        assert db.scalar(select(func.count()).select_from(ProjectFile).where(ProjectFile.project_id == project["id"])) == 0
        assert db.scalar(select(func.count()).select_from(QuotaHold).where(QuotaHold.task_id == task.id)) == 0
        assert db.scalar(select(func.count()).select_from(QuotaTransaction).where(QuotaTransaction.task_id == task.id)) == 0
        assert db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.aggregate_id == task.id)) == 0
        assert db.scalar(select(func.count()).select_from(WorkerHeartbeat).where(WorkerHeartbeat.current_task_id == task.id)) == 0
        assert db.scalar(select(func.count()).select_from(TaskAttempt).where(TaskAttempt.task_id == task.id)) == 0
        assert db.scalar(select(func.count()).select_from(TaskEvent).where(TaskEvent.task_id == task.id)) == 0
        assert db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == task.id)) == 0
        assert db.scalar(select(func.count()).select_from(TextFormatFlow).where(TextFormatFlow.project_id == project["id"])) == 0
        assert db.scalar(select(func.count()).select_from(ChapterReviewMark).where(ChapterReviewMark.project_id == project["id"])) == 0


def test_project_retention_check_runs_from_worker_loop(monkeypatch: pytest.MonkeyPatch):
    import threading

    stop = threading.Event()
    calls = []

    def purge_once():
        calls.append(True)
        stop.set()

    monkeypatch.setattr("backend.worker.purge_expired_projects", purge_once)
    _project_retention_loop(stop)
    assert calls == [True]


def test_project_retention_retries_once_after_startup_skip(monkeypatch: pytest.MonkeyPatch):
    class ImmediateStop:
        waits: list[float] = []
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, timeout):
            self.waits.append(timeout)
            return self.stopped

    stop = ImmediateStop()
    calls = []

    def skip_then_complete():
        calls.append(True)
        if len(calls) == 2:
            stop.stopped = True
        return 0

    monkeypatch.setattr("backend.worker.purge_expired_projects", skip_then_complete)
    _project_retention_loop(stop)
    assert len(calls) == 2
    assert stop.waits == [60, 24 * 60 * 60]


def test_restore_casefold_conflict_returns_success_instead_of_expired_status(client: TestClient):
    _user_id, _username, csrf = _account(client)
    original = _project(client, csrf, "Book")
    _trash(client, csrf, original["id"])
    _project(client, csrf, "book")
    _project(client, csrf, "book（恢复）")
    restored = client.post(f"/api/v1/projects/{original['id']}/restore", headers={"X-CSRF-Token": csrf})
    assert restored.status_code == 200, restored.text
    assert restored.json()["name"] == "Book（恢复 2）"
    assert restored.json()["directory_key"].endswith("/Book（恢复 2）")


def test_purge_budget_bounds_each_round_and_continues(client: TestClient):
    _user_id, _username, csrf = _account(client)
    first = _project(client, csrf, "预算第一轮")
    second = _project(client, csrf, "预算第二轮")
    for project_id in (first["id"], second["id"]):
        _trash(client, csrf, project_id)
        _expire(project_id)
    assert purge_expired_projects(limit=1) == 1
    assert purge_expired_projects(limit=1) == 1
    assert purge_expired_projects(limit=1) == 0
    with SessionLocal() as db:
        assert db.get(Project, first["id"]) is None
        assert db.get(Project, second["id"]) is None


def test_purge_budget_rejects_out_of_range(client: TestClient):
    with pytest.raises(ValueError):
        purge_expired_projects(limit=0)
    with pytest.raises(ValueError):
        purge_expired_projects(limit=101)
