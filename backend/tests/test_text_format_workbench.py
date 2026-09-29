"""Text-format workbench: flow orchestration, versioned reads/exports, review marks.

Drives the real pipeline end-to-end: POST /flow submits stage tasks, and
``process_task_message`` executes them synchronously (same pattern as
test_platform.py) so the server-side continue/manifest/mark/preview logic is
exercised without a running worker.
"""
from __future__ import annotations

import hashlib
import io
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("sqlalchemy")

from backend.main import app
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import ChapterReviewMark, Task
from backend.platform.task_worker import process_task_message
from sqlalchemy import select


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


def _register(client: TestClient, email: str) -> dict:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234", "display_name": "Workbench User"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload(client: TestClient, csrf: str, name: str, body: bytes) -> dict:
    response = client.post(
        "/api/files/upload",
        headers={"X-CSRF-Token": csrf},
        files={"file": (name, body, "text/plain")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _post_flow(client: TestClient, csrf: str, project_id: str, body: dict) -> dict:
    return client.post(
        f"/api/v1/projects/{project_id}/text-format/flow",
        headers={"X-CSRF-Token": csrf},
        json=body,
    )


def _drive_to_ready(client: TestClient, csrf: str, project_id: str, body: dict) -> dict:
    """POST /flow, execute each submitted stage task, repeat until the flow
    is ready (or reports a failed stage)."""
    response = _post_flow(client, csrf, project_id, body)
    assert response.status_code == 200, response.text
    state = response.json()
    while state.get("next_task") and not state["next_task"].get("failed"):
        task = state["next_task"]
        result = process_task_message(
            {"payload": {"task_id": task["task_id"]}}, worker_id="test-workbench-worker"
        )
        assert result == "succeeded", f"stage {task.get('stage')} finished with {result}"
        response = _post_flow(client, csrf, project_id, body)
        assert response.status_code == 200, response.text
        state = response.json()
    assert state.get("flow", {}).get("status") == "ready", state
    assert state.get("version"), state
    return state


CHAPTERED_BODY = (
    "第 1 章 金陵秦尘\n\n清晨，窗外传来几声鸟鸣。秦尘推开房门，院中的石阶还留着昨夜的雨水。\n"
    "第 2 章 天上掉下来的仙女\n\n他将书卷放在桌上，重新整理好行囊，准备启程。\n"
).encode("utf-8")


def test_state_is_empty_for_a_fresh_project(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    project_id = client.get("/api/v1/projects/active").json()["project_id"]

    response = client.get(f"/api/v1/projects/{project_id}/text-format/state")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["flow"] is None
    assert body["version"] is None
    assert body["next_task"] is None
    assert body["active_tasks"] == []


def test_flow_runs_pipeline_and_recovers_without_resubmitting(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    csrf = first["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    uploaded = _upload(client, csrf, "workbench-novel.txt", CHAPTERED_BODY)
    body = {"source_file_id": uploaded["file_id"], "config": {}}

    started = _post_flow(client, csrf, project_id, body)
    assert started.status_code == 200, started.text
    started = started.json()
    assert started["flow"]["status"] == "running"
    assert started["next_task"]["stage"] == "format"
    format_task_id = started["next_task"]["task_id"]

    # Recovery semantics: re-POSTing the flow while a stage task is in flight
    # must NOT create a duplicate task — it re-evaluates and returns the
    # same next task (idempotency keys tflow:{id}:{stage}).
    repeated = _post_flow(client, csrf, project_id, body).json()
    assert repeated["next_task"]["task_id"] == format_task_id
    from_task_list = client.get("/api/v1/tasks", params={"project_id": project_id}).json()
    assert [t for t in from_task_list if t["task_type"] == "text.format"] and \
        all(t["id"] == format_task_id for t in from_task_list if t["task_type"] == "text.format")

    state = _drive_to_ready(client, csrf, project_id, body)
    flow = state["flow"]
    version = state["version"]
    assert flow["status"] == "ready"
    assert flow["split_mode"] == "smart"
    assert version["mode"] == "smart"
    assert version["version_status"] == "current"
    assert len(version["chapters"]) == 2
    assert [c["key"] for c in version["chapters"]] == ["c1", "c2"]
    assert [c["title"] for c in version["chapters"]] == ["金陵秦尘", "天上掉下来的仙女"]
    assert all(c["pending"] is False for c in version["chapters"])
    assert all(c["adjusted"] is False for c in version["chapters"])
    assert len(version["files"]) == 2
    assert version["review_marks"] == []
    # The final stage consumed the formatted output, and the flow recorded
    # the manifest for versioned reads.
    assert flow["manifest_count"] == 2

    # Continuing a ready flow is a no-op (no new tasks).
    again = _post_flow(client, csrf, project_id, body).json()
    assert again["flow"]["id"] == flow["id"]
    assert again["next_task"] is None


def test_zero_chapter_input_falls_back_to_by_length(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    csrf = first["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    # One giant paragraph: no chapter markers, no blank-line structure.
    body = _upload(client, csrf, "no-chapters.txt", (("甲" * 19 + "。") * 310).encode("utf-8"))
    state = _drive_to_ready(client, csrf, project_id, {"source_file_id": body["file_id"]})
    version = state["version"]
    assert version["mode"] == "by_length"
    assert len(version["chapters"]) >= 2
    assert all(c["reasons"] == ["length_split"] for c in version["chapters"])
    assert all(c["pending"] is False for c in version["chapters"])
    fallback = [m for m in version["matters"] if m["reason"] == "length_fallback"]
    assert fallback and fallback[0]["scope"] == "version" and fallback[0]["advisory"] is False


def test_whole_book_mode_normalizes_to_single_entry(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    csrf = first["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    uploaded = _upload(client, csrf, "whole-book.txt", CHAPTERED_BODY)
    state = _drive_to_ready(
        client, csrf, project_id,
        {"source_file_id": uploaded["file_id"], "whole_book": True},
    )
    version = state["version"]
    assert version["mode"] == "whole_book"
    assert len(version["chapters"]) == 1
    whole = version["chapters"][0]
    assert whole["key"] == "whole"
    assert whole["title"] == "整本"
    assert whole["chars"] > 0
    assert any(m["reason"] == "whole_book" for m in version["matters"])


def test_review_marks_are_version_scoped_and_idempotent(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    csrf = first["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    uploaded = _upload(client, csrf, "marks-novel.txt", CHAPTERED_BODY)
    state = _drive_to_ready(client, csrf, project_id, {"source_file_id": uploaded["file_id"]})
    task_id = state["version"]["task_id"]
    marks = f"/api/v1/projects/{project_id}/text-format/review-marks"

    marked = client.post(marks, headers={"X-CSRF-Token": csrf}, json={"task_id": task_id, "chapter_key": "c1"})
    assert marked.status_code == 200, marked.text
    replay = client.post(marks, headers={"X-CSRF-Token": csrf}, json={"task_id": task_id, "chapter_key": "c1"})
    assert replay.status_code == 200
    assert replay.json()["chapter_key"] == "c1"
    assert client.get(f"/api/v1/projects/{project_id}/text-format/state").json()["version"]["review_marks"] == ["c1"]

    # Unknown chapter / wrong task type are rejected, not created.
    bad_chapter = client.post(marks, headers={"X-CSRF-Token": csrf}, json={"task_id": task_id, "chapter_key": "c99"})
    assert bad_chapter.status_code == 404
    format_task = state["flow"]["format_task_id"]
    wrong_task = client.post(marks, headers={"X-CSRF-Token": csrf}, json={"task_id": format_task, "chapter_key": "c1"})
    assert wrong_task.status_code == 404

    removed = client.request(
        "DELETE", marks, params={"task_id": task_id, "chapter_key": "c1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert removed.status_code == 200, removed.text
    # Unmarking twice is idempotent.
    again = client.request("DELETE", marks, params={"task_id": task_id, "chapter_key": "c1"}, headers={"X-CSRF-Token": csrf})
    assert again.status_code == 200
    assert client.get(f"/api/v1/projects/{project_id}/text-format/state").json()["version"]["review_marks"] == []


def test_marks_never_carry_over_across_reprocess(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    csrf = first["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    novel_a = _upload(client, csrf, "reprocess-a.txt", CHAPTERED_BODY)
    state_a = _drive_to_ready(client, csrf, project_id, {"source_file_id": novel_a["file_id"]})
    task_a = state_a["version"]["task_id"]
    client.post(
        f"/api/v1/projects/{project_id}/text-format/review-marks",
        headers={"X-CSRF-Token": csrf}, json={"task_id": task_a, "chapter_key": "c1"},
    )
    assert client.get(f"/api/v1/projects/{project_id}/text-format/state").json()["version"]["review_marks"] == ["c1"]

    # Reprocess from a different source file: the old version stays behind
    # with its marks, the new version starts unmarked.
    novel_b = _upload(
        client, csrf, "reprocess-b.txt",
        "第 1 章 新的开篇\n\n这是一个完全不同内容的章节。\n".encode("utf-8"),
    )
    state_b = _drive_to_ready(client, csrf, project_id, {
        "source_file_id": novel_b["file_id"], "restart": True,
    })
    assert state_b["version"]["task_id"] != task_a
    assert state_b["version"]["review_marks"] == []

    with SessionLocal() as db:
        kept = db.scalars(select(ChapterReviewMark).where(ChapterReviewMark.task_id == task_a)).all()
        assert len(kept) == 1 and kept[0].chapter_key == "c1"

    # The superseded version's content is no longer live: versioned reads
    # of the old flow must fail loudly instead of serving mixed content.
    old_file_name = state_a["version"]["files"][0]["name"]
    denied = client.get(
        f"/api/v1/projects/{project_id}/text-format/file/{old_file_name}",
        params={"flow_id": state_a["flow"]["id"]},
    )
    assert denied.status_code == 409, denied.text


def test_preview_and_zip_are_bound_to_the_explicit_version(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    csrf = first["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    uploaded = _upload(client, csrf, "export-novel.txt", CHAPTERED_BODY)
    state = _drive_to_ready(client, csrf, project_id, {"source_file_id": uploaded["file_id"]})
    version = state["version"]
    flow_id = state["flow"]["id"]
    first_file = version["files"][0]

    # Inline preview returns the real chapter text.
    preview = client.get(
        f"/api/v1/projects/{project_id}/text-format/file/{first_file['name']}",
        params={"flow_id": flow_id},
    )
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"].startswith("text/plain")
    assert "金陵秦尘" in preview.text

    # Single-file download goes through the same versioned endpoint.
    download = client.get(
        f"/api/v1/projects/{project_id}/text-format/file/{first_file['name']}",
        params={"flow_id": flow_id, "download": "true"},
    )
    assert download.status_code == 200
    assert "attachment" in download.headers.get("content-disposition", "")

    # A file that does not belong to the flow is rejected.
    foreign = client.get(
        f"/api/v1/projects/{project_id}/text-format/file/不存在的章节.txt",
        params={"flow_id": flow_id},
    )
    assert foreign.status_code == 404

    # Whole-version export: complete zip whose content matches the manifest.
    zip_response = client.get(
        f"/api/v1/projects/{project_id}/text-format/zip", params={"flow_id": flow_id}
    )
    assert zip_response.status_code == 200, zip_response.text
    # The disposition header must stay latin-1 encodable (RFC 5987 filename*).
    disposition = zip_response.headers["content-disposition"]
    assert "attachment" in disposition and "filename*=utf-8''" in disposition
    disposition.encode("latin-1")
    data = zip_response.content
    assert zip_response.headers["x-zip-sha256"] == hashlib.sha256(data).hexdigest()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert names == {f["name"] for f in version["files"]}
        chapter_text = archive.read(version["files"][0]["name"]).decode("utf-8")
        assert "金陵秦尘" in chapter_text


def test_preview_size_limit_is_applied_before_reading(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    csrf = first["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    uploaded = _upload(client, csrf, "large-novel.txt", CHAPTERED_BODY)
    state = _drive_to_ready(client, csrf, project_id, {"source_file_id": uploaded["file_id"]})
    file_name = state["version"]["files"][0]["name"]

    import backend.services.text_format_workbench as workbench
    monkeypatch.setattr(workbench, "PREVIEW_MAX_BYTES", 1)
    too_large = client.get(
        f"/api/v1/projects/{project_id}/text-format/file/{file_name}",
        params={"flow_id": state["flow"]["id"]},
    )
    assert too_large.status_code == 413, too_large.text


def test_concurrent_flows_are_blocked_server_side(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    csrf = first["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    source_a = _upload(client, csrf, "concurrent-a.txt", CHAPTERED_BODY)
    source_b = _upload(client, csrf, "concurrent-b.txt", CHAPTERED_BODY)

    started = _post_flow(client, csrf, project_id, {"source_file_id": source_a["file_id"]})
    assert started.status_code == 200

    # A second flow (different file) while the first is in flight: 409.
    other = _post_flow(client, csrf, project_id, {"source_file_id": source_b["file_id"]})
    assert other.status_code == 409, other.text
    # Restarting while in flight is blocked too.
    restart = _post_flow(client, csrf, project_id, {"source_file_id": source_a["file_id"], "restart": True})
    assert restart.status_code == 409, restart.text

    # Resume of the SAME flow is still allowed (re-evaluation, no duplicate).
    same = _post_flow(client, csrf, project_id, {"source_file_id": source_a["file_id"]})
    assert same.status_code == 200, same.text
    _drive_to_ready(client, csrf, project_id, {"source_file_id": source_a["file_id"]})


def test_failed_stage_reports_and_retries_without_new_tasks(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    csrf = first["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    uploaded = _upload(client, csrf, "fail-novel.txt", CHAPTERED_BODY)

    started = _post_flow(client, csrf, project_id, {"source_file_id": uploaded["file_id"]})
    assert started.status_code == 200
    format_task_id = started.json()["next_task"]["task_id"]

    # Fail the format stage from the worker side, then continue: the flow
    # surfaces the failure and does not spawn replacement tasks on its own.
    with SessionLocal() as db:
        task = db.get(Task, format_task_id)
        task.status = "failed"
        task.error_code = "test_error"
        task.error_message = "排版失败（测试注入）"
        db.commit()
    continued = _post_flow(client, csrf, project_id, {"source_file_id": uploaded["file_id"]})
    assert continued.status_code == 200, continued.text
    continued = continued.json()
    assert continued["flow"]["status"] == "failed"
    assert "排版失败（测试注入）" in (continued["flow"]["error"] or "")
    assert continued["next_task"]["failed"] is True

    # Retry the same stage task (existing endpoint), then continue.
    retried = client.post(f"/api/v1/tasks/{format_task_id}/retry", headers={"X-CSRF-Token": csrf})
    assert retried.status_code == 200, retried.text
    result = process_task_message({"payload": {"task_id": format_task_id}}, worker_id="test-workbench-worker")
    assert result == "succeeded"
    state = _drive_to_ready(client, csrf, project_id, {"source_file_id": uploaded["file_id"]})
    # The same flow row was resumed — no replacement pipeline was created.
    assert state["flow"]["id"] == started.json()["flow"]["id"]
    assert state["flow"]["status"] == "ready"

    # No duplicate format tasks were created by the recovery path.
    rows = [t for t in client.get("/api/v1/tasks", params={"project_id": project_id}).json() if t["task_type"] == "text.format"]
    assert len(rows) == 1 and rows[0]["id"] == format_task_id


def test_flow_requires_csrf_and_valid_source(client: TestClient):
    first = _register(client, f"{uuid.uuid4()}@example.test")
    project_id = client.get("/api/v1/projects/active").json()["project_id"]

    no_csrf = client.post(f"/api/v1/projects/{project_id}/text-format/flow", json={"source_file_id": "x" * 36})
    assert no_csrf.status_code == 403

    csrf = first["csrf_token"]
    missing_file = _post_flow(client, csrf, project_id, {"source_file_id": None})
    assert missing_file.status_code == 422

    bogus_file = _post_flow(client, csrf, project_id, {"source_file_id": "0" * 36})
    assert bogus_file.status_code == 404

    state_forbidden = client.get(f"/api/v1/projects/{project_id}/text-format/state")
    assert state_forbidden.status_code == 200  # GET needs no CSRF token
