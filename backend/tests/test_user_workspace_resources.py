from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import Project, Task
from backend.platform.storage import configured_storage_root


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


def _create_workspace(client: TestClient) -> tuple[str, str, Path]:
    email = f"{uuid.uuid4()}@example.test"
    response = client.post(
        "/api/auth/register",
        json={"email": email, "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234"},
    )
    assert response.status_code == 201, response.text
    user = response.json()["user"]
    workspaces = client.get("/api/v1/projects")
    assert workspaces.status_code == 200, workspaces.text
    workspace_id = workspaces.json()[0]["id"]
    with SessionLocal() as db:
        path = configured_storage_root(db) / user["username"] / workspace_id
    return response.json()["csrf_token"], workspace_id, path


def test_user_workspace_summary_only_returns_owned_project_storage(client: TestClient):
    csrf, workspace_id, root = _create_workspace(client)
    output = root / "07_output" / "book_01.mp3"
    temp = root / "00_temp" / "old.tmp"
    output.parent.mkdir(parents=True, exist_ok=True)
    temp.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"audio")
    temp.write_bytes(b"cache")
    old_time = time.time() - 8 * 24 * 60 * 60
    os.utime(temp, (old_time, old_time))

    response = client.get(f"/api/v1/projects/{workspace_id}/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file_count"] == 2
    assert body["size_bytes"] == len(b"audiocache")
    assert body["cleanup_candidates"]["count"] == 1
    assert body["recent_outputs"][0]["name"] == "book_01.mp3"

    other = client.post(
        "/api/auth/register",
        json={"email": f"{uuid.uuid4()}@example.test", "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234"},
    )
    assert other.status_code == 201, other.text
    denied = client.get(f"/api/v1/projects/{workspace_id}/summary")
    assert denied.status_code == 404
    shutil.rmtree(root, ignore_errors=True)


def test_user_workspace_cleanup_is_limited_to_old_cache_and_blocks_active_task(client: TestClient):
    csrf, workspace_id, root = _create_workspace(client)
    temp = root / "00_temp" / "old.tmp"
    input_file = root / "01_input" / "keep.txt"
    temp.parent.mkdir(parents=True, exist_ok=True)
    input_file.parent.mkdir(parents=True, exist_ok=True)
    temp.write_bytes(b"cache")
    input_file.write_bytes(b"input")
    old_time = time.time() - 8 * 24 * 60 * 60
    os.utime(temp, (old_time, old_time))

    with SessionLocal.begin() as db:
        workspace = db.get(Project, workspace_id)
        assert workspace is not None
        db.add(Task(
            id=str(uuid.uuid4()), owner_id=workspace.owner_id, project_id=workspace_id,
            task_type="text.format", status="running", payload={}, progress=1,
        ))

    blocked = client.post(
        f"/api/v1/projects/{workspace_id}/cleanup-temp",
        headers={"X-CSRF-Token": csrf},
    )
    assert blocked.status_code == 409, blocked.text
    assert temp.exists()
    assert input_file.exists()

    with SessionLocal.begin() as db:
        task = db.query(Task).filter(Task.project_id == workspace_id).first()
        assert task is not None
        task.status = "cancelled"
    cleaned = client.post(
        f"/api/v1/projects/{workspace_id}/cleanup-temp",
        headers={"X-CSRF-Token": csrf},
    )
    assert cleaned.status_code == 200, cleaned.text
    assert cleaned.json()["deleted_count"] == 1
    assert not temp.exists()
    assert input_file.exists()
    shutil.rmtree(root, ignore_errors=True)
