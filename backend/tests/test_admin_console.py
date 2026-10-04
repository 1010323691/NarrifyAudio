from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.services.task_operations import task_worker_group
from backend.services.admin_storage import scan_project_directory
from backend.core import observability
from backend.main import app
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import QuotaTransaction, Task, User
from backend.platform.storage import configured_storage_root


@pytest.fixture(scope="module")
def client():
    initialize_schema()
    with TestClient(app) as value:
        yield value


def _create_admin(client: TestClient) -> tuple[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": f"{uuid.uuid4()}@example.test", "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    with SessionLocal.begin() as db:
        user = db.get(User, body["user"]["id"])
        assert user is not None
        user.role = "admin"
    return body["csrf_token"], body["user"]["id"]


def test_regular_user_cannot_access_admin_api(client: TestClient):
    registered = client.post(
        "/api/auth/register",
        json={"email": f"{uuid.uuid4()}@example.test", "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234"},
    )
    assert registered.status_code == 201, registered.text

    overview = client.get("/api/v1/admin/overview")
    users = client.get("/api/v1/admin/users")
    assert overview.status_code == 403, overview.text
    assert users.status_code == 403, users.text


def test_regular_user_cannot_call_admin_write_endpoints(client: TestClient):
    # Every admin write endpoint must reject a regular user at the role gate
    # (403「需要管理员权限」) once CSRF is satisfied, and reject a missing CSRF
    # token earlier (403「CSRF 校验失败」) — the require_admin_csrf order.
    registered = client.post(
        "/api/auth/register",
        json={"email": f"{uuid.uuid4()}@example.test", "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234"},
    )
    assert registered.status_code == 201, registered.text
    body = registered.json()
    csrf = body["csrf_token"]
    user_id = body["user"]["id"]

    def call(method: str, url: str, payload: dict | None = None) -> None:
        response = client.request(method, url, json=payload, headers={"X-CSRF-Token": csrf})
        assert response.status_code == 403, (url, response.status_code, response.text)
        assert "需要管理员权限" in response.text, (url, response.text)

    call("patch", "/api/v1/admin/settings/application", {})
    call("patch", "/api/v1/admin/settings/storage", {"root_path": "C:/narrify-storage"})
    call("patch", "/api/v1/admin/settings/quota", {"units": 1000})
    call("patch", "/api/v1/admin/settings/registration", {"enabled": True})
    call("patch", f"/api/v1/admin/users/{user_id}", {"is_active": True})
    call("post", f"/api/v1/admin/users/{user_id}/quota/adjust",
         {"amount": 1, "idempotency_key": uuid.uuid4().hex})
    call("post", "/api/v1/admin/tasks/00000000-0000-0000-0000-000000000000/cancel")
    call("post", "/api/v1/admin/tasks/00000000-0000-0000-0000-000000000000/retry")
    call("post", "/api/v1/admin/resources/cleanup-temp")

    # CSRF is checked before the role gate: without the token the failure is
    # the CSRF 403, not the role 403.
    response = client.post("/api/v1/admin/resources/cleanup-temp")
    assert response.status_code == 403, response.text
    assert "CSRF 校验失败" in response.text, response.text


def _create_task(client: TestClient, csrf: str, project_id: str, key: str) -> str:
    response = client.post(
        "/api/v1/tasks",
        headers={"X-CSRF-Token": csrf},
        json={
            "project_id": project_id,
            "task_type": "text.format",
            "payload": {},
            "idempotency_key": key,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_admin_task_filters_group_failed_and_completed_states(client: TestClient):
    csrf, _ = _create_admin(client)
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Admin filter test"}
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    failed_id = _create_task(client, csrf, project_id, f"failed-{uuid.uuid4()}")
    timeout_id = _create_task(client, csrf, project_id, f"timeout-{uuid.uuid4()}")
    completed_id = _create_task(client, csrf, project_id, f"done-{uuid.uuid4()}")
    with SessionLocal.begin() as db:
        db.get(Task, failed_id).status = "failed"
        db.get(Task, timeout_id).status = "timeout"
        db.get(Task, completed_id).status = "succeeded"

    # 套件里 xdist worker 共享一个 sqlite DB（conftest 进程级 URL），其他模块
    # 会留下自己的 failed/succeeded 任务——按项目收敛（search 服务端过滤
    # project_id），断言本项目的分组口径，而不是全局精确集。
    failed = client.get(f"/api/v1/admin/tasks?status=failed&search={project_id}")
    completed = client.get(f"/api/v1/admin/tasks?status=completed&search={project_id}")
    system_events = client.get("/api/v1/admin/events?module=system")
    assert failed.status_code == 200, failed.text
    assert completed.status_code == 200, completed.text
    assert system_events.status_code == 200, system_events.text
    assert {row["id"] for row in failed.json()} == {failed_id, timeout_id}
    assert {row["id"] for row in completed.json()} == {completed_id}
    assert failed_id in {row["id"] for row in system_events.json()}


def test_admin_can_retry_failed_zero_cost_task(client: TestClient):
    csrf, _ = _create_admin(client)
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Admin retry test"}
    )
    assert project.status_code == 201, project.text
    task_id = _create_task(client, csrf, project.json()["id"], f"retry-{uuid.uuid4()}")
    with SessionLocal.begin() as db:
        db.get(Task, task_id).status = "failed"

    response = client.post(f"/api/v1/admin/tasks/{task_id}/retry", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    queued = client.get("/api/v1/admin/tasks?status=queued")
    assert task_id in {row["id"] for row in queued.json()}


def test_admin_cannot_retry_a_task_after_metered_model_consumption(client: TestClient):
    csrf, _ = _create_admin(client)
    project = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": "Metered retry test"}
    )
    assert project.status_code == 201, project.text
    task_id = _create_task(client, csrf, project.json()["id"], f"metered-retry-{uuid.uuid4()}")
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        task.status = "failed"
        db.add(QuotaTransaction(
            user_id=task.owner_id,
            task_id=task.id,
            amount=-1,
            kind="consume",
            resource_type="LLM",
            idempotency_key=f"metered-retry-{uuid.uuid4()}",
        ))

    response = client.post(f"/api/v1/admin/tasks/{task_id}/retry", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 409
    assert "已有模型消费" in response.json()["detail"]


def test_temp_cleanup_deletes_only_old_files_under_inactive_temp_directory(client: TestClient):
    csrf, _ = _create_admin(client)
    workspace = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": f"Cleanup {uuid.uuid4()}"}
    )
    assert workspace.status_code == 201, workspace.text
    with SessionLocal() as db:
        storage_root = configured_storage_root(db)
    workspace_path = storage_root / workspace.json()["directory_key"]
    temp_dir = workspace_path / "00_temp"
    other_dir = workspace_path / "05_audio_chunk"
    temp_dir.mkdir(parents=True, exist_ok=True)
    other_dir.mkdir(parents=True, exist_ok=True)
    old_file = temp_dir / "old.tmp"
    recent_file = temp_dir / "recent.tmp"
    audio_file = other_dir / "keep.wav"
    old_file.write_bytes(b"old")
    recent_file.write_bytes(b"recent")
    audio_file.write_bytes(b"keep")
    old_time = time.time() - 8 * 24 * 60 * 60
    os.utime(old_file, (old_time, old_time))

    response = client.post("/api/v1/admin/resources/cleanup-temp", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_count"] == 1
    assert response.json()["deleted_bytes"] == len(b"old")
    assert not old_file.exists()
    assert recent_file.exists()
    assert audio_file.exists()


def test_temp_cleanup_skips_workspaces_with_active_tasks(client: TestClient):
    csrf, user_id = _create_admin(client)
    workspace = client.post(
        "/api/v1/projects", headers={"X-CSRF-Token": csrf}, json={"name": f"Active {uuid.uuid4()}"}
    )
    assert workspace.status_code == 201, workspace.text
    workspace_data = workspace.json()
    with SessionLocal() as db:
        storage_root = configured_storage_root(db)
    workspace_path = storage_root / workspace_data["directory_key"]
    temp_dir = workspace_path / "00_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    old_file = temp_dir / "in-use.tmp"
    old_file.write_bytes(b"still in use")
    old_time = time.time() - 8 * 24 * 60 * 60
    os.utime(old_file, (old_time, old_time))
    with SessionLocal.begin() as db:
        db.add(Task(
            id=str(uuid.uuid4()), owner_id=user_id, project_id=workspace_data["id"],
            task_type="text.format", status="running", payload={}, progress=0,
        ))

    response = client.post("/api/v1/admin/resources/cleanup-temp", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    assert response.json()["deleted_count"] == 0
    assert old_file.exists()


def test_workspace_scan_counts_files_and_only_marks_old_temp_files(tmp_path: Path):
    workspace = tmp_path / "user" / "workspace"
    temp = workspace / "00_temp"
    audio = workspace / "05_audio_chunk"
    temp.mkdir(parents=True)
    audio.mkdir()
    old_file = temp / "old.tmp"
    recent_file = temp / "recent.tmp"
    audio_file = audio / "chunk.wav"
    old_file.write_bytes(b"old")
    recent_file.write_bytes(b"recent")
    audio_file.write_bytes(b"audio")
    old_time = time.time() - 8 * 24 * 60 * 60
    os.utime(old_file, (old_time, old_time))

    result = scan_project_directory(workspace)
    assert result["file_count"] == 3
    assert result["size_bytes"] == len(b"oldrecentaudio")
    assert result["cleanup_count"] == 1
    assert result["cleanup_bytes"] == len(b"old")
    assert result["categories"]["05_audio_chunk"]["count"] == 1
    assert scan_project_directory(workspace, active=True)["cleanup_count"] == 0


def test_workspace_scan_skips_symlinked_directories(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.dat").write_bytes(b"not part of workspace")
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable in this environment")

    result = scan_project_directory(workspace)
    assert result["file_count"] == 0
    assert result["size_bytes"] == 0


@pytest.mark.parametrize(
    ("task_type", "expected"),
    [("script.parse", "llm"), ("tts.batch", "tts"), ("bgm.mix", "audio"), ("text.format", "system")],
)
def test_admin_event_module_mapping(task_type: str, expected: str):
    assert task_worker_group(task_type) == expected


def test_api_requests_today_uses_a_separate_daily_aggregate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(observability, "_daily_counts", {})
    for _ in range(8):
        observability.record_api_request("/api/health", 200, 1.0)
    assert observability.api_requests_today() == 8


# --------------------------------------------------------------------------- #
# LLM 模型列表拉取（GET /api/v1/admin/llm/models）：只读探测端点，
# 管理员可用表单草稿值探测未保存的配置；上游失败转 502 + 中文 detail。
# --------------------------------------------------------------------------- #
class _FakeModelsResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_llm_models_listing_requires_admin(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    registered = client.post(
        "/api/auth/register",
        json={"email": f"{uuid.uuid4()}@example.test", "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234"},
    )
    assert registered.status_code == 201, registered.text

    import io
    import urllib.error
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeModelsResp(b'{"data": []}'))

    forbidden = client.get("/api/v1/admin/llm/models?base_url=http://x/v1")
    assert forbidden.status_code == 403, forbidden.text

    csrf, _ = _create_admin(client)
    # 读端点不需要 CSRF 头；带参数探测表单草稿值。
    ok = client.get("/api/v1/admin/llm/models?base_url=http://x/v1&api_key=k")
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"models": []}


def test_llm_models_listing_maps_upstream_errors_to_502(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    csrf, _ = _create_admin(client)

    import io
    import urllib.error
    import urllib.request

    body = b'{"data": [{"id": "qwen3-14b"}, "llama3:8b"]}'
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeModelsResp(body))
    ok = client.get("/api/v1/admin/llm/models?base_url=http://x/v1&api_key=k")
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"models": ["qwen3-14b", "llama3:8b"]}

    def http_500(req, *a, **k):
        raise urllib.error.HTTPError("http://x/v1/models", 500, "boom", {}, io.BytesIO(b"boom"))

    monkeypatch.setattr(urllib.request, "urlopen", http_500)
    bad = client.get("/api/v1/admin/llm/models?base_url=http://x/v1&api_key=k")
    assert bad.status_code == 502, bad.text
    assert "500" in bad.json()["detail"]

    def conn_refused(req, *a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", conn_refused)
    refused = client.get("/api/v1/admin/llm/models?base_url=http://x/v1&api_key=k")
    assert refused.status_code == 502, refused.text
    assert "连接失败" in refused.json()["detail"]


def test_llm_models_listing_falls_back_to_saved_config(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    # 省略 base_url 参数时用已保存平台配置探测（get_config 回退分支）。
    import urllib.request
    from types import SimpleNamespace

    import backend.core.config as core_config_mod

    _create_admin(client)
    captured: list = []

    def fake_urlopen(request, *a, **k):
        captured.append(request)
        return _FakeModelsResp(b'{"data": ["saved-model"]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        core_config_mod, "get_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(base_url="http://saved-host:9999/v1", api_key="saved-key")),
    )
    ok = client.get("/api/v1/admin/llm/models")
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"models": ["saved-model"]}
    assert captured[0].full_url == "http://saved-host:9999/v1/models"
    assert captured[0].get_header("Authorization") == "Bearer saved-key"


def test_llm_models_listing_empty_saved_url_returns_422(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    # 已保存配置无地址且未传参数 → 422，不发上游请求。
    import urllib.request
    from types import SimpleNamespace

    import backend.core.config as core_config_mod

    _create_admin(client)
    called: list = []

    def fail_if_called(request, *a, **k):
        called.append(1)
        return _FakeModelsResp(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fail_if_called)
    monkeypatch.setattr(
        core_config_mod, "get_config",
        lambda: SimpleNamespace(llm=SimpleNamespace(base_url="", api_key="")),
    )
    resp = client.get("/api/v1/admin/llm/models")
    assert resp.status_code == 422, resp.text
    assert "地址" in resp.json()["detail"]
    assert not called


def test_admin_application_settings_never_persist_user_owned_parse_checks(client: TestClient):
    # 用户「文本解析」页专属的 6 个解析检查开关不落平台默认：PATCH application
    # settings 携带它们也被剔除（顺带清掉历史残留值），返回与后续读取均为代码默认；
    # 管理台管的 spot_check_rate 照常持久化；用户侧 GET /api/config 的 6 项不受
    # 平台侧 False 影响（防覆盖漏洞的另一半——页面初值 / 任务快照基线不被压）。
    csrf, _ = _create_admin(client)

    check_keys = (
        "check_chunk_alignment", "check_boundary_speakers", "validate_instructs",
        "revalidate_splits", "check_long_paragraphs", "spot_check_enabled",
    )
    patched = client.patch(
        "/api/v1/admin/settings/application",
        headers={"X-CSRF-Token": csrf},
        json={"generation": {
            "spot_check_rate": 0.07,
            **{key: False for key in check_keys},
        }},
    )
    assert patched.status_code == 200, patched.text
    gen = patched.json()["config"]["generation"]
    for key in check_keys:
        assert gen[key] is True  # 未落平台默认 → 回代码默认（全 True），False 未生效
    assert gen["spot_check_rate"] == 0.07  # 管理台管的比例照常持久化

    # 用户侧合并视图：注册会切换 TestClient 会话（须在管理台请求之后），6 项仍为
    # 工作区 / 代码默认（平台侧的 False 不压工作区值）
    user = client.post(
        "/api/auth/register",
        json={"email": f"{uuid.uuid4()}@example.test", "username": f"user{uuid.uuid4().hex[:12]}", "password": "test-pass-1234"},
    )
    assert user.status_code == 201, user.text
    config = client.get("/api/config", headers={"X-CSRF-Token": user.json()["csrf_token"]})
    assert config.status_code == 200, config.text
    ws_gen = config.json()["generation"]
    for key in check_keys:
        assert ws_gen[key] is True
    assert ws_gen["spot_check_rate"] == 0.07
