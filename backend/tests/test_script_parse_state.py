"""Script-parse workbench contract: project-scoped chapter state,
version-bound submission, project-bound result reads.

The heavy scenarios (window independence, parallel task/result state) are
exercised by seeding the task table directly and calling the service — the
point of this suite is that the STATE endpoint answers from the full task
history, not from the "active + recent 200" task-list window.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace

pytest.importorskip("sqlalchemy")

from backend.main import app
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.deps import AuthContext
from backend.platform.file_catalog import catalog_managed_file
from backend.platform.models import Project, ProjectFile, Task, TaskResult, User, UserQuotaAccount, utcnow
from backend.platform.storage import configured_storage_root, object_path
from backend.platform.task_submission import task_dict
from backend.services.script_parse_state import _build_file_states, get_state
from sqlalchemy import select


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


def _register(client: TestClient) -> tuple[str, str, str, str]:
    """Register a fresh user; return (email, csrf, user_id, project_id)."""
    email = f"{uuid.uuid4()}@example.test"
    response = client.post(
        "/api/auth/register",
        json={"email": email, "username": f"u{uuid.uuid4().hex[:12]}", "password": "test-pass-1234", "display_name": "Parse"},
    )
    assert response.status_code == 201, response.text
    csrf = response.json()["csrf_token"]
    project_id = client.get("/api/v1/projects/active").json()["project_id"]
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
    return email, csrf, user.id, project_id


def _sha(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _add_split_file(db, user_id: str, project_id: str, username: str, name: str, sha: str | None) -> ProjectFile:
    item = ProjectFile(
        project_id=project_id,
        owner_id=user_id,
        original_name=name,
        object_key=f"{username}/{project_id}/02_split_text/{name}",
        content_type="text/plain",
        kind="output",
        size_bytes=len(name),
        sha256=sha or _sha(name),
    )
    db.add(item)
    db.flush()
    return item


def _add_artifact(db, user_id: str, project_id: str, username: str, name: str, sha: str) -> ProjectFile:
    """A parse result lives in 03_parsed_json — keeping it out of the split
    directory so the state's chapter listing stays clean."""
    item = ProjectFile(
        project_id=project_id,
        owner_id=user_id,
        original_name=name,
        object_key=f"{username}/{project_id}/03_parsed_json/{name}",
        content_type="application/json",
        kind="output",
        size_bytes=len(name),
        sha256=sha or _sha(name),
    )
    db.add(item)
    db.flush()
    return item


def _add_parse_task(
    db, user_id: str, project_id: str, name: str, *, status: str,
    source_sha: str | None = None, result_name: str | None = None,
    result_file_id: str | None = None, source_file_id: str | None = None,
    input_chars: int | None = None,
    error: str = "", offset_seconds: int = 0,
) -> Task:
    created = utcnow() + timedelta(seconds=offset_seconds)
    task = Task(
        owner_id=user_id,
        project_id=project_id,
        task_type="script.parse",
        status=status,
        progress=100 if status in {"succeeded", "failed", "timeout"} else 0,
        error_message=error,
        payload={"source_name": name, "input_file_id": f"seed-{name}", "config": {}},
        created_at=created,
        updated_at=created,
    )
    if status in {"succeeded", "failed", "timeout", "cancelled"}:
        task.finished_at = created
    db.add(task)
    db.flush()
    if status == "succeeded":
        envelope = {
            "file_id": result_file_id,
            "object_key": f"x/{project_id}/03_parsed_json/{result_name}",
            "name": result_name,
            "source_sha256": source_sha,
        }
        # 旧版信封没有 source_sha256，但有 source_file_id（入表文件行 ID）与
        # input_chars（引擎口径的输入文本长度）。
        if source_file_id is not None:
            envelope["source_file_id"] = source_file_id
        if input_chars is not None:
            envelope["input_chars"] = input_chars
        db.add(TaskResult(task_id=task.id, result=envelope))
    return task


def _grant_quota(user_id: str, units: int = 10000) -> None:
    """Billable script.parse submissions need balance; the test deployment
    starts users at zero units (NARRIFY_INITIAL_QUOTA_UNITS unset)."""
    with SessionLocal() as db:
        account = db.scalar(select(UserQuotaAccount).where(UserQuotaAccount.user_id == user_id))
        if account is None:
            db.add(UserQuotaAccount(user_id=user_id, available_units=units))
        else:
            account.available_units = units
        db.commit()


def _state(client: TestClient, email: str, csrf: str, project_id: str) -> dict:
    response = client.get(f"/api/v1/projects/{project_id}/script-parse/state")
    assert response.status_code == 200, response.text
    return response.json()


def test_task_dict_reports_input_identity_from_payload():
    """Failed/cancelled tasks have no result — the input identity must come
    from the payload, so the page can still attach the task to its chapter."""
    task = Task(
        id="t-failed", project_id="p1", task_type="script.parse", status="failed",
        payload={"input_file_id": "f-1", "source_name": "0001_章节.txt", "config": {}},
        created_at=utcnow(), updated_at=utcnow(),
    )
    body = task_dict(task)
    assert body["result"] is None
    assert body["source_name"] == "0001_章节.txt"
    assert body["source_file_id"] == "f-1"
    # Non-parse tasks carry neither identifier.
    other = Task(
        id="t-other", project_id="p1", task_type="tts.merge", status="failed",
        payload={"chunks": []}, created_at=utcnow(), updated_at=utcnow(),
    )
    other_body = task_dict(other)
    assert other_body["source_name"] is None
    assert other_body["source_file_id"] is None


def test_state_is_empty_for_a_fresh_project(client: TestClient):
    email, csrf, _user_id, project_id = _register(client)
    body = _state(client, email, csrf, project_id)
    assert body["source"]["mode"] == "empty"
    assert body["files"] == []
    assert body["text_format_busy"] is False


def test_state_aggregates_beyond_the_recent_task_window(client: TestClient):
    """300+ chapters: the oldest successful parses fall OUT of the
    /api/v1/tasks window (active + recent 200), but the state endpoint must
    still report them 已完成 — the window is not a chapter-state source."""
    email, csrf, user_id, project_id = _register(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        count = 250
        for i in range(count):
            name = f"{i:04d}_章节.txt"
            _add_split_file(db, user_id, project_id, user.username, name, _sha(name))
            # 解析产物在 03_parsed_json（不能落在分册目录，否则混进章节清单）。
            artifact = ProjectFile(
                project_id=project_id,
                owner_id=user_id,
                original_name=f"{i:04d}.json",
                object_key=f"{user.username}/{project_id}/03_parsed_json/{i:04d}.json",
                content_type="application/json",
                kind="output",
                size_bytes=1,
                sha256=_sha(f"art-{i}"),
            )
            db.add(artifact)
            db.flush()
            _add_parse_task(
                db, user_id, project_id, name, status="succeeded",
                source_sha=_sha(name), result_name=f"{i:04d}.json",
                result_file_id=artifact.id, offset_seconds=i,
            )
        db.commit()

    # The task-list window only ever shows the recent 200: the oldest success
    # is not there…
    task_list = client.get("/api/v1/tasks", params={"project_id": project_id}).json()
    with SessionLocal() as db:
        oldest = db.scalar(
            select(Task).where(
                Task.project_id == project_id,
                Task.payload["source_name"].as_string() == "0000_章节.txt",
            )
        )
        oldest_id = oldest.id
    assert oldest_id not in [t["id"] for t in task_list]

    # …but the state endpoint answers from the full history.
    body = _state(client, email, csrf, project_id)
    assert body["source"]["mode"] == "legacy"
    assert len(body["files"]) == count
    first = next(f for f in body["files"] if f["name"] == "0000_章节.txt")
    assert first["latest_task"]["status"] == "succeeded"
    assert first["result_status"] == "usable"
    assert first["result"]["verified"] is True
    assert first["result"]["source_sha256"] == _sha("0000_章节.txt")


def test_failed_latest_task_keeps_older_result_available(client: TestClient):
    """「重新解析失败但旧结果仍有效」= two parallel axes: latest_task failed
    AND result usable. A timeout is reported as its own status (前端：超时失败)."""
    email, csrf, user_id, project_id = _register(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        name = "0001_章节.txt"
        _add_split_file(db, user_id, project_id, user.username, name, _sha(name))
        artifact = _add_artifact(db, user_id, project_id, user.username, "0001.json", _sha("art-1"))
        _add_parse_task(db, user_id, project_id, name, status="succeeded",
                        source_sha=_sha(name), result_name="0001.json", result_file_id=artifact.id, offset_seconds=0)
        _add_parse_task(db, user_id, project_id, name, status="failed", error="LLM 调用失败", offset_seconds=10)
        timeout_name = "0002_章节.txt"
        _add_split_file(db, user_id, project_id, user.username, timeout_name, _sha(timeout_name))
        _add_parse_task(db, user_id, project_id, timeout_name, status="timeout", error="", offset_seconds=5)
        db.commit()

    body = _state(client, email, csrf, project_id)
    by_name = {f["name"]: f for f in body["files"]}
    failed_row = by_name[name]
    assert failed_row["latest_task"]["status"] == "failed"
    assert failed_row["latest_task"]["error"] == "LLM 调用失败"
    assert failed_row["result_status"] == "usable"  # 旧结果仍在，两个轴并存
    assert by_name[timeout_name]["latest_task"]["status"] == "timeout"


def test_result_is_stale_when_input_digest_changed(client: TestClient):
    email, csrf, user_id, project_id = _register(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        name = "0001_章节.txt"
        # 重新分册后文件表 sha 已更新（upsert 覆盖），旧结果的 source_sha 不再匹配。
        _add_split_file(db, user_id, project_id, user.username, name, _sha("content-v2"))
        artifact = _add_artifact(db, user_id, project_id, user.username, "0001.json", _sha("art-1"))
        _add_parse_task(db, user_id, project_id, name, status="succeeded",
                       source_sha=_sha("content-v1"), result_name="0001.json", result_file_id=artifact.id)
        db.commit()

    body = _state(client, email, csrf, project_id)
    row = next(f for f in body["files"] if f["name"] == name)
    assert row["result_status"] == "stale"
    assert row["result"]["verified"] is True


def test_unverified_when_artifact_or_digest_missing(client: TestClient):
    email, csrf, user_id, project_id = _register(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        # 产物行不存在（被删/未入表）→ 待确认，不当完成
        _add_split_file(db, user_id, project_id, user.username, "a.txt", _sha("a"))
        _add_parse_task(db, user_id, project_id, "a.txt", status="succeeded",
                       source_sha=_sha("a"), result_name="a.json", result_file_id="ghost-id")
        # 成功信封缺 source_sha（早期任务无此元数据）→ 不可判
        _add_split_file(db, user_id, project_id, user.username, "b.txt", _sha("b"))
        artifact_b = _add_artifact(db, user_id, project_id, user.username, "b.json", _sha("art-b"))
        _add_parse_task(db, user_id, project_id, "b.txt", status="succeeded",
                        source_sha=None, result_name="b.json", result_file_id=artifact_b.id)
        db.commit()

    body = _state(client, email, csrf, project_id)
    by_name = {f["name"]: f for f in body["files"]}
    assert by_name["a.txt"]["result_status"] == "unverified"
    assert by_name["a.txt"]["result"]["verified"] is False
    assert by_name["b.txt"]["result_status"] == "unverified"


def test_legacy_success_without_digest_judged_by_input_content(client: TestClient):
    """Pre-fingerprint results (envelope 无 source_sha256，有 source_file_id +
    input_chars)：重新分册是同一文件行的原地覆盖（清 deleted_at、更新 sha，
    行仍存活），所以判定必须按引擎口径复算磁盘输入文本长度，与信封的
    input_chars 比对——一致 → 已完成；不一致（内容已被重新分册）→ 输入已
    变更；文件丢失 → 待确认。"""
    email, csrf, user_id, project_id = _register(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        root = configured_storage_root(db)

        def _write(name: str, content: str) -> ProjectFile:
            item = _add_split_file(db, user_id, project_id, user.username, name, _sha(name + content))
            path = object_path(item.object_key, root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return item

        # 0001: 磁盘内容与解析时的长度一致 → usable
        item1 = _write("0001_章节.txt", "甲内容一")
        art1 = _add_artifact(db, user_id, project_id, user.username, "0001.json", _sha("art-1"))
        _add_parse_task(db, user_id, project_id, "0001_章节.txt", status="succeeded",
                       source_sha=None, result_name="0001.json", result_file_id=art1.id,
                       source_file_id=item1.id, input_chars=len("甲内容一"), offset_seconds=10)
        # 0002: 分册已被重新分册（磁盘内容变更、长度不同）→ stale
        item2 = _write("0002_章节.txt", "乙重新分册后的新内容")
        art2 = _add_artifact(db, user_id, project_id, user.username, "0002.json", _sha("art-2"))
        _add_parse_task(db, user_id, project_id, "0002_章节.txt", status="succeeded",
                       source_sha=None, result_name="0002.json", result_file_id=art2.id,
                       source_file_id=item2.id, input_chars=5, offset_seconds=10)
        # 0003: 分册磁盘内容丢失 → unverified
        item3 = _add_split_file(db, user_id, project_id, user.username, "0003_章节.txt", _sha("0003"))
        art3 = _add_artifact(db, user_id, project_id, user.username, "0003.json", _sha("art-3"))
        _add_parse_task(db, user_id, project_id, "0003_章节.txt", status="succeeded",
                       source_sha=None, result_name="0003.json", result_file_id=art3.id,
                       source_file_id=item3.id, input_chars=5, offset_seconds=10)
        db.commit()

    body = _state(client, email, csrf, project_id)
    by_name = {f["name"]: f for f in body["files"]}
    assert by_name["0001_章节.txt"]["result_status"] == "usable"
    assert by_name["0001_章节.txt"]["result"]["verified"] is True
    assert by_name["0002_章节.txt"]["result_status"] == "stale"
    assert by_name["0003_章节.txt"]["result_status"] == "unverified"


def test_run_rejects_changed_digest(client: TestClient):
    email, csrf, user_id, project_id = _register(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        _add_split_file(db, user_id, project_id, user.username, "0001_章节.txt", _sha("current"))
        db.commit()
    response = client.post(
        f"/api/v1/projects/{project_id}/script-parse/run",
        headers={"X-CSRF-Token": csrf},
        json={"files": [{"name": "0001_章节.txt", "sha256": _sha("stale-page-view")}]},
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "已变更" in detail["message"]
    assert detail["changed"] == ["0001_章节.txt"]


def test_run_rejects_missing_file(client: TestClient):
    email = f"{uuid.uuid4()}@example.test"
    email, csrf, _user_id, project_id = _register(client)
    response = client.post(
        f"/api/v1/projects/{project_id}/script-parse/run",
        headers={"X-CSRF-Token": csrf},
        json={"files": [{"name": "不存在.txt"}]},
    )
    assert response.status_code == 409, response.text
    assert "不存在" in response.json()["detail"]["message"]


def test_run_rejects_while_text_format_busy(client: TestClient):
    email, csrf, user_id, project_id = _register(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        _add_split_file(db, user_id, project_id, user.username, "0001_章节.txt", _sha("s"))
        db.add(Task(
            owner_id=user_id, project_id=project_id, task_type="book.split",
            status="running", payload={"input_file_id": "x"},
        ))
        db.commit()
    response = client.post(
        f"/api/v1/projects/{project_id}/script-parse/run",
        headers={"X-CSRF-Token": csrf},
        json={"files": [{"name": "0001_章节.txt", "sha256": _sha("s")}]},
    )
    assert response.status_code == 409, response.text
    assert "排版与分册" in response.json()["detail"]["message"]


def test_run_rejects_in_flight_parse_of_same_file(client: TestClient):
    email, csrf, user_id, project_id = _register(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        _add_split_file(db, user_id, project_id, user.username, "0001_章节.txt", _sha("s"))
        db.add(Task(
            owner_id=user_id, project_id=project_id, task_type="script.parse",
            status="running", payload={"source_name": "0001_章节.txt", "input_file_id": "x"},
        ))
        db.commit()
    response = client.post(
        f"/api/v1/projects/{project_id}/script-parse/run",
        headers={"X-CSRF-Token": csrf},
        json={"files": [{"name": "0001_章节.txt", "sha256": _sha("s")}]},
    )
    assert response.status_code == 409, response.text
    assert "0001_章节.txt" in response.json()["detail"]["message"]


def test_run_submits_version_bound_tasks_with_checks(client: TestClient):
    email, csrf, user_id, project_id = _register(client)
    _grant_quota(user_id)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        item = _add_split_file(db, user_id, project_id, user.username, "0001_章节.txt", _sha("s"))
        db.commit()
        item_id = item.id
    response = client.post(
        f"/api/v1/projects/{project_id}/script-parse/run",
        headers={"X-CSRF-Token": csrf},
        json={
            "files": [{"name": "0001_章节.txt", "sha256": _sha("s")}],
            "checks": {"check_chunk_alignment": True},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"][0]["name"] == "0001_章节.txt"
    assert body["files"][0]["input_sha256"] == _sha("s")
    with SessionLocal() as db:
        task = db.get(Task, body["files"][0]["task_id"])
        assert task.task_type == "script.parse"
        assert task.status == "pending"
        assert task.payload["input_file_id"] == item_id
        assert task.payload["source_name"] == "0001_章节.txt"
        # 检查开关随快照固化（与 legacy 端点同语义）。
        assert task.payload["config"]["generation"]["check_chunk_alignment"] is True


def test_run_catalogs_legacy_disk_file_without_table_row(client: TestClient):
    """历史文件兼容入口：磁盘有分册文本、文件表无记录（无摘要）——提交时登记
    并计算摘要，而不是拒之门外。"""
    email, csrf, user_id, project_id = _register(client)
    _grant_quota(user_id)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        split_dir = configured_storage_root(db) / user.username / project_id / "02_split_text"
        split_dir.mkdir(parents=True, exist_ok=True)
        path = split_dir / "legacy-chapter.txt"
        path.write_text("第一章内容", encoding="utf-8")
        disk_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    response = client.post(
        f"/api/v1/projects/{project_id}/script-parse/run",
        headers={"X-CSRF-Token": csrf},
        json={"files": [{"name": "legacy-chapter.txt"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["files"][0]["input_sha256"] == disk_sha
    with SessionLocal() as db:
        row = db.scalar(select(ProjectFile).where(ProjectFile.original_name == "legacy-chapter.txt"))
        assert row is not None
        assert row.sha256 == disk_sha
    # 登记后 state 从 legacy 清单读到该文件（带摘要）。
    body = _state(client, email, csrf, project_id)
    listed = {f["name"] for f in body["files"]}
    assert "legacy-chapter.txt" in listed


def test_run_catalog_preserves_published_original_name(client: TestClient):
    """补登撞到发布行（object_key 撞车）：只更新摘要，不得把 original_name
    改写成磁盘名。发布行留引擎原始名（带全角标点），磁盘名经
    safe_display_name 已把「，」换成「_」。"""
    email, csrf, user_id, project_id = _register(client)
    _grant_quota(user_id)
    raw_name = "第 005 章 九州，欢族.txt"
    disk_name = "第 005 章 九州_欢族.txt"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        split_dir = configured_storage_root(db) / user.username / project_id / "02_split_text"
        split_dir.mkdir(parents=True, exist_ok=True)
        path = split_dir / disk_name
        path.write_text("章节内容", encoding="utf-8")
        disk_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        item = ProjectFile(
            project_id=project_id,
            owner_id=user_id,
            original_name=raw_name,
            object_key=f"{user.username}/{project_id}/02_split_text/{disk_name}",
            content_type="text/plain",
            kind="artifact",
            size_bytes=4,
            sha256=_sha("旧摘要"),
        )
        db.add(item)
        db.commit()
        item_id = item.id
    response = client.post(
        f"/api/v1/projects/{project_id}/script-parse/run",
        headers={"X-CSRF-Token": csrf},
        json={"files": [{"name": disk_name}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"][0]["input_sha256"] == disk_sha
    with SessionLocal() as db:
        row = db.get(ProjectFile, item_id)
        assert row.original_name == raw_name
        assert row.sha256 == disk_sha
        task = db.get(Task, body["files"][0]["task_id"])
        assert task.payload["input_file_id"] == item_id


def test_run_accepts_name_differing_only_by_storage_sanitization(client: TestClient):
    """行的 original_name 已是磁盘名（历史补登改写过），页面仍按引擎原始名
    引用：名字只差一层 safe_display_name 即同一文件，不得判「分册文本不存在」。"""
    email, csrf, user_id, project_id = _register(client)
    _grant_quota(user_id)
    raw_name = "第 010 章 夜阑，柔情.txt"
    disk_name = "第 010 章 夜阑_柔情.txt"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        item = _add_split_file(db, user_id, project_id, user.username, disk_name, _sha("s"))
        db.commit()
        item_id = item.id
    response = client.post(
        f"/api/v1/projects/{project_id}/script-parse/run",
        headers={"X-CSRF-Token": csrf},
        json={"files": [{"name": raw_name, "sha256": _sha("s")}]},
    )
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        task = db.get(Task, response.json()["files"][0]["task_id"])
        assert task.payload["input_file_id"] == item_id


def test_state_legacy_disk_only_aligns_with_storage_sanitization(client: TestClient):
    """legacy 模式下，表名与磁盘名只差 safe_display_name 的章节只按表名出现
    一次，不再以磁盘名双列。"""
    email, csrf, user_id, project_id = _register(client)
    raw_name = "第 016 章 都城—燕云.txt"
    disk_name = "第 016 章 都城_燕云.txt"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        _add_split_file(db, user_id, project_id, user.username, raw_name, _sha("s"))
        split_dir = configured_storage_root(db) / user.username / project_id / "02_split_text"
        split_dir.mkdir(parents=True, exist_ok=True)
        (split_dir / disk_name).write_text("章节内容", encoding="utf-8")
        db.commit()
    state = _state(client, email, csrf, project_id)
    assert state["source"]["mode"] == "legacy"
    names = [f["name"] for f in state["files"]]
    assert raw_name in names
    assert disk_name not in names
    assert disk_name not in state["source"]["disk_only"]


def test_download_falls_back_to_storage_sanitized_name(client: TestClient):
    """legacy 预览/下载：按表名（引擎原始名，含，）请求，磁盘名经清洗（→_）——
    精确名未命中时按清洗名回退解析，而不是 400 非法路径。"""
    email, csrf, user_id, project_id = _register(client)
    raw_name = "第 005 章 九州，欢族.txt"
    disk_name = "第 005 章 九州_欢族.txt"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        split_dir = configured_storage_root(db) / user.username / project_id / "02_split_text"
        split_dir.mkdir(parents=True, exist_ok=True)
        (split_dir / disk_name).write_text("章节内容", encoding="utf-8")
        db.commit()
    response = client.get(f"/api/files/download/02_split_text/{raw_name}")
    assert response.status_code == 200, response.text
    assert response.content == "章节内容".encode("utf-8")
    # 真实存在的磁盘名直取不受影响。
    assert client.get(f"/api/files/download/02_split_text/{disk_name}").status_code == 200
    # 两者都不存在仍是 400。
    assert client.get("/api/files/download/02_split_text/不存在_章节.txt").status_code == 400


def test_catalog_managed_file_collision_keeps_original_name(client: TestClient):
    """catalog_managed_file 撞已有行（与解析补登同款根因）：只更新摘要，
    不把 original_name 改写成磁盘名。"""
    email, csrf, user_id, project_id = _register(client)
    raw_name = "第 007 章 清明，故人.txt"
    disk_name = "第 007 章 清明_故人.txt"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        split_dir = configured_storage_root(db) / user.username / project_id / "02_split_text"
        split_dir.mkdir(parents=True, exist_ok=True)
        path = split_dir / disk_name
        path.write_text("章节内容", encoding="utf-8")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        item = ProjectFile(
            project_id=project_id,
            owner_id=user_id,
            original_name=raw_name,
            object_key=f"{user.username}/{project_id}/02_split_text/{disk_name}",
            content_type="text/plain",
            kind="artifact",
            size_bytes=4,
            sha256=_sha("旧摘要"),
        )
        db.add(item)
        db.commit()
        item_id = item.id
        ctx = AuthContext(user=user, session=SimpleNamespace(active_project_id=project_id))
        row = catalog_managed_file(path, ctx, db)
        db.commit()
    assert row.id == item_id
    assert row.original_name == raw_name
    assert row.sha256 == sha


def test_build_file_states_resolves_storage_sanitized_alias(client: TestClient):
    """version 列表按引擎原始名（含，）引用、行 original_name 已是磁盘名
    （历史改写过）：input 经别名索引解析到该行，而不是显示成无输入。"""
    email, csrf, user_id, project_id = _register(client)
    raw_name = "第 011 章 破晓，秋水.txt"
    disk_name = "第 011 章 破晓_秋水.txt"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        _add_split_file(db, user_id, project_id, user.username, disk_name, _sha("s"))
        db.commit()
        project = db.get(Project, project_id)
        states = _build_file_states(db, user, project, [raw_name])
    assert len(states) == 1
    assert states[0]["input"] is not None
    assert states[0]["input"]["name"] == disk_name
    assert states[0]["input"]["sha256"] == _sha("s")


def test_run_rejects_in_flight_parse_renamed_by_alias(client: TestClient):
    """在途章节按别名（原始名 vs 磁盘名）重提：在途判定按清洗名对齐，
    不绕过「已有解析任务在进行」409。"""
    email, csrf, user_id, project_id = _register(client)
    raw_name = "第 012 章 长夜，灯尽.txt"
    disk_name = "第 012 章 长夜_灯尽.txt"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        _add_split_file(db, user_id, project_id, user.username, disk_name, _sha("s"))
        db.add(Task(
            owner_id=user_id, project_id=project_id, task_type="script.parse",
            status="running", payload={"source_name": disk_name, "input_file_id": "x"},
        ))
        db.commit()
    response = client.post(
        f"/api/v1/projects/{project_id}/script-parse/run",
        headers={"X-CSRF-Token": csrf},
        json={"files": [{"name": raw_name, "sha256": _sha("s")}]},
    )
    assert response.status_code == 409, response.text
    assert "在进行" in response.json()["detail"]["message"]


def test_results_endpoint_serves_project_bound_artifact(client: TestClient):
    email, csrf, user_id, project_id = _register(client)
    payload = {"chapters": [{"speaker": "旁白", "text": "你好"}]}
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        artifact = _add_split_file(db, user_id, project_id, user.username, "0001.json", _sha("result-body"))
        db.flush()
        # _add_split_file 用 02_split_text 建行的习惯只用于造数据；这里把行纠正到
        # 03_parsed_json（result_file 按 module 校验）。
        artifact.object_key = f"{user.username}/{project_id}/03_parsed_json/0001.json"
        artifact.original_name = "0001.json"
        artifact.size_bytes = len(json.dumps(payload, ensure_ascii=False).encode())
        artifact.sha256 = _sha("result-body")
        artifact.kind = "output"
        db.flush()
        path = object_path(artifact.object_key, configured_storage_root(db))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        artifact_id = artifact.id
        db.commit()
    response = client.get(f"/api/v1/projects/{project_id}/script-parse/results/{artifact_id}")
    assert response.status_code == 200, response.text
    assert response.json() == payload
    assert response.headers["etag"] == f'"{_sha("result-body")}"'
    # 条件请求命中 ETag → 304。
    again = client.get(
        f"/api/v1/projects/{project_id}/script-parse/results/{artifact_id}",
        headers={"If-None-Match": f'"{_sha("result-body")}"'},
    )
    assert again.status_code == 304


def test_results_endpoint_rejects_foreign_or_wrong_module(client: TestClient):
    email, csrf, user_id, project_id = _register(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        split = _add_split_file(db, user_id, project_id, user.username, "0001_章节.txt", _sha("s"))
        db.commit()
        split_id = split.id
    # 项目内文件但 module 不是 03_parsed_json → 404（不是解析结果）。
    response = client.get(f"/api/v1/projects/{project_id}/script-parse/results/{split_id}")
    assert response.status_code == 404, response.text
    # 不存在的 file_id → 404。
    response = client.get(f"/api/v1/projects/{project_id}/script-parse/results/no-such-id")
    assert response.status_code == 404
    # 其他项目 → 404。
    _other_email, _other_csrf, _other_user, other_project = _register(client)
    response = client.get(f"/api/v1/projects/{other_project}/script-parse/results/{split_id}")
    assert response.status_code == 404


def test_results_endpoint_reports_missing_artifact_content(client: TestClient):
    email, csrf, user_id, project_id = _register(client)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        artifact = _add_split_file(db, user_id, project_id, user.username, "0001.json", _sha("r"))
        artifact.object_key = f"{user.username}/{project_id}/03_parsed_json/0001.json"
        artifact.original_name = "0001.json"
        db.flush()
        artifact_id = artifact.id
        db.commit()
        # 磁盘上没有内容（行还在）→ 409，前端提示刷新。
    response = client.get(f"/api/v1/projects/{project_id}/script-parse/results/{artifact_id}")
    assert response.status_code == 409, response.text
    assert "已丢失" in response.json()["detail"]["message"]
