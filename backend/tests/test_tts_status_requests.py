"""Large book status queries travel in JSON bodies with normal session/CSRF checks."""
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def status_client():
    with TestClient(app) as client:
        username = f"status{uuid.uuid4().hex[:12]}"
        response = client.post("/api/auth/register", json={
            "email": f"{username}@example.invalid", "username": username,
            "password": "test-pass-1234", "display_name": "Status queries",
        })
        assert response.status_code == 201, response.text
        headers = {"X-CSRF-Token": response.json()["csrf_token"]}
        response = client.post("/api/v1/projects", headers=headers, json={"name": "Status book"})
        assert response.status_code == 201, response.text
        yield client, headers


@pytest.mark.parametrize("endpoint,key,result_key,suffix", [
    ("batch-status", "scripts", "files", ".json"),
    ("merge-status", "packages", "packages", ""),
])
def test_large_status_lists_keep_order_and_get_compatibility(status_client, endpoint, key, result_key, suffix):
    client, headers = status_client
    names = [f"第{i:04d}章 中文标题 音频合成与合并测试{suffix}" for i in range(2000)]
    response = client.post(f"/api/tts/{endpoint}", headers=headers, json={key: names})
    assert response.status_code == 200, response.text
    rows = response.json()[result_key]
    assert [row["name"] for row in rows] == names
    assert all(row["total"] == row["completed"] == 0 for row in rows)
    assert client.post(f"/api/tts/{endpoint}", headers=headers, json={key: []}).json() == {result_key: []}
    legacy = client.get(f"/api/tts/{endpoint}", params=[(key, name) for name in names[:2]])
    assert legacy.status_code == 200
    assert legacy.json()[result_key] == rows[:2]
    assert client.post(f"/api/tts/{endpoint}", json={key: names[:1]}).status_code == 403
    assert client.post(f"/api/tts/{endpoint}", headers=headers, json={key: "bad"}).status_code == 422


@pytest.mark.parametrize("endpoint,key,names", [
    ("batch-status", "scripts", ["__all__"]),
    ("merge-status", "packages", ["../escape"]),
])
def test_status_posts_preserve_selection_validation(status_client, endpoint, key, names):
    client, headers = status_client
    assert client.post(f"/api/tts/{endpoint}", headers=headers, json={key: names}).status_code == 400
