"""Multi-user, multi-task PostgreSQL/Redis durable-platform smoke test."""
from __future__ import annotations

import concurrent.futures
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime


BASE_URL = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:8642").rstrip("/")
LLM_BASE_URL = os.getenv("SMOKE_LLM_BASE_URL", "http://127.0.0.1:8090/v1")
ADMIN_EMAIL = os.getenv("SMOKE_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("SMOKE_ADMIN_PASSWORD", "smoke-admin-password-123")
TERMINAL = {"succeeded", "failed", "cancelled", "timeout"}


class Client:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def request(self, method: str, path: str, payload: dict | None = None, *, headers: dict | None = None, body: bytes | None = None, content_type: str | None = None):
        data = body
        request_headers = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if content_type:
            request_headers["Content-Type"] = content_type
        if self.csrf and method not in {"GET", "HEAD"}:
            request_headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(BASE_URL + path, data=data, method=method, headers=request_headers)
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read()
                return response.status, json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc

    def register(self, email: str) -> tuple[dict, str]:
        status, data = self.request("POST", "/api/auth/register", {
            "email": email,
            "username": email.split("@", 1)[0],
            "password": "smoke-user-password-123",
            "display_name": email.split("@", 1)[0],
        })
        assert status == 201
        self.csrf = data["csrf_token"]
        _, projects = self.request("GET", "/api/v1/projects")
        return data["user"], projects[0]["id"]

    def upload(self, project_id: str, name: str, content: str) -> str:
        boundary = "----NarrifySmoke" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="upload"; filename="{name}"\r\n'
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{content}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        _, data = self.request("POST", f"/api/v1/projects/{project_id}/files", body=body, content_type=f"multipart/form-data; boundary={boundary}")
        return data["id"]

    def submit(self, project_id: str, task_type: str, payload: dict, key: str, estimated_units: int = 0) -> dict:
        _, data = self.request("POST", "/api/v1/tasks", {
            "project_id": project_id,
            "task_type": task_type,
            "payload": payload,
            "estimated_units": estimated_units,
            "idempotency_key": key,
        })
        return data


def script_config() -> dict:
    return {
        "llm": {"base_url": LLM_BASE_URL, "api_key": "stub", "model_name": "narrify-smoke", "stream": False},
        "prompts": {},
        "generation": {
            "chunk_size": 3000,
            "max_tokens": 256,
            "max_concurrency": 1,
            "spot_check_rate": 0,
            "check_boundary_speakers": False,
            "revalidate_splits": False,
            "check_long_paragraphs": False,
            "absorb_punct_entries": False,
        },
    }


def poll(client: Client, task_ids: list[str], timeout: float = 90) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < deadline:
        pending = []
        for task_id in task_ids:
            _, task = client.request("GET", f"/api/v1/tasks/{task_id}")
            latest[task_id] = task
            if task["status"] not in TERMINAL:
                pending.append(task_id)
        if not pending:
            return latest
        time.sleep(0.5)
    raise RuntimeError(f"tasks did not finish: {[latest[item] for item in pending]}")


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def main() -> None:
    admin = Client()
    status, health = admin.request("GET", "/api/health")
    assert status == 200 and health["ok"] is True

    users: list[tuple[Client, dict, str, str]] = []
    for index in range(2):
        client = Client()
        user, project_id = client.register(f"smoke-user-{index}-{uuid.uuid4().hex[:8]}@example.com")
        users.append((client, user, project_id, user["username"]))

    def submit_for_user(item: tuple[Client, dict, str, str]) -> list[str]:
        client, _user, project_id, username = item
        source = f"这是 {username} 的 LLM stub 冒烟文本。"
        file_id = client.upload(project_id, f"{username}.txt", source)
        task_ids = []
        for index in range(3):
            task = client.submit(
                project_id,
                "script.parse",
                {"input_file_id": file_id, "source_name": f"{username}.txt", "config": script_config()},
                f"smoke-script-{username}-{index}-{uuid.uuid4().hex}",
            )
            task_ids.append(task["id"])
        # A billable reservation is deliberately cancelled before execution.
        quota_task = client.submit(
            project_id,
            "text.format",
            {"input_file_id": file_id, "output_name": f"{username}-cancelled.txt"},
            f"smoke-cancel-{username}-{uuid.uuid4().hex}",
            estimated_units=3,
        )
        cancelled = client.request("POST", f"/api/v1/tasks/{quota_task['id']}/cancel")[1]
        if cancelled["status"] not in {"cancelled", "cancelling"}:
            raise RuntimeError(f"cancel did not take effect: {cancelled}")
        task_ids.append(quota_task["id"])
        return task_ids

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        task_groups = list(pool.map(submit_for_user, users))

    results = []
    for (client, _user, _project, username), task_ids in zip(users, task_groups):
        tasks = poll(client, task_ids)
        script_tasks = [tasks[task_id] for task_id in task_ids[:3]]
        if any(task["status"] != "succeeded" for task in script_tasks):
            raise RuntimeError(f"LLM stub tasks failed for {username}: {script_tasks}")
        if not any(task.get("result", {}).get("engine") == "script.parse" for task in script_tasks):
            raise RuntimeError(f"LLM stub result missing for {username}: {script_tasks}")
        if tasks[task_ids[-1]]["status"] != "cancelled":
            raise RuntimeError(f"queued cancellation was not finalized: {tasks[task_ids[-1]]}")
        _, quota = client.request("GET", "/api/v1/quota")
        if quota["reserved_units"] != 0 or quota["available_units"] != 12:
            raise RuntimeError(f"quota did not release after cancellation for {username}: {quota}")
        results.extend(tasks.values())

    admin = Client()
    login = admin.request("POST", "/api/auth/login", {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})[1]
    admin.csrf = login["csrf_token"]
    _, metrics = admin.request("GET", "/api/v1/admin/task-metrics")
    _, all_tasks = admin.request("GET", "/api/v1/admin/tasks")
    by_id = {task["id"]: task for task in all_tasks}
    started = [by_id[task["id"]] for task in results if task["id"] in by_id and by_id[task["id"]].get("started_at")]
    started.sort(key=lambda task: parse_time(task["started_at"]))
    started_users = [task["owner_username"] for task in started]
    if len(set(started_users[:2])) < 2:
        raise RuntimeError(f"fair scheduling smoke check failed, first starts: {started_users[:6]}")
    if metrics["status_counts"].get("succeeded", 0) < 6 or metrics["status_counts"].get("cancelled", 0) < 2:
        raise RuntimeError(f"admin metrics incomplete: {metrics}")
    print(json.dumps({
        "ok": True,
        "users": [item[1]["username"] for item in users],
        "succeeded_llm_tasks": 6,
        "cancelled_tasks": 2,
        "fair_start_order": started_users[:6],
        "admin_status_counts": metrics["status_counts"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
