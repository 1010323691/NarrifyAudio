"""Create 100 demo users and evenly submit 5,000 LLM + 5,000 TTS tasks."""
from __future__ import annotations

import concurrent.futures
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from sqlalchemy import insert, select

from backend.platform.database import SessionLocal
from backend.platform.models import Project, Task, TaskEvent, User, new_id


BASE_URL = os.getenv("LOAD_TEST_BASE_URL", "http://api:8642").rstrip("/")
USERS = int(os.getenv("LOAD_TEST_USERS", "100"))
TASKS_PER_TYPE = int(os.getenv("LOAD_TEST_TASKS_PER_TYPE", "5000"))
SUBMIT_SECONDS = float(os.getenv("LOAD_TEST_SUBMIT_SECONDS", "60"))
PASSWORD = os.getenv("LOAD_TEST_PASSWORD", "load-test-password-123")
REQUESTED_RUN_ID = os.getenv("LOAD_TEST_RUN_ID", "").strip()


class Client:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        request = urllib.request.Request(BASE_URL + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
                return response.status, json.loads(raw.decode()) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc


def register_user(index: int, run_id: str) -> dict:
    username = f"load-{run_id}-{index:03d}"
    client = Client()
    _, data = client.request("POST", "/api/auth/register", {
        "email": f"{username}@example.test",
        "username": username,
        "password": PASSWORD,
        "display_name": username,
    })
    return {"username": username, "user_id": data["user"]["id"]}


def resolve_run_id() -> str:
    if REQUESTED_RUN_ID:
        return REQUESTED_RUN_ID
    groups: dict[str, set[int]] = {}
    pattern = re.compile(r"^load-([0-9a-f]{16})-(\d{3})$")
    with SessionLocal() as db:
        names = db.scalars(select(User.username).where(User.username.like("load-%"))).all()
    for username in names:
        match = pattern.fullmatch(username)
        if match:
            groups.setdefault(match.group(1), set()).add(int(match.group(2)))
    partial = [run_id for run_id, indices in groups.items() if 0 < len(indices) < USERS]
    return max(partial) if partial else datetime.now(timezone.utc).strftime("%y%m%d%H%M%S") + uuid.uuid4().hex[:4]


def load_registered_users(run_id: str) -> tuple[list[dict], set[int]]:
    prefix = f"load-{run_id}-"
    with SessionLocal() as db:
        rows = db.execute(
            select(User.id, User.username, Project.id)
            .join(Project, Project.owner_id == User.id)
            .where(User.username.like(f"{prefix}%"), Project.deleted_at.is_(None))
        ).all()
    users = []
    indices = set()
    for user_id, username, project_id in rows:
        match = re.fullmatch(re.escape(prefix) + r"(\d{3})", username)
        if match:
            indices.add(int(match.group(1)))
            users.append({"user_id": user_id, "username": username, "project_id": project_id})
    return users, indices


def main() -> None:
    if USERS < 1 or TASKS_PER_TYPE < 1 or SUBMIT_SECONDS <= 0:
        raise SystemExit("users, task counts and submit duration must be positive")
    run_id = resolve_run_id()
    started = time.monotonic()
    users, existing_indices = load_registered_users(run_id)
    missing_indices = [index for index in range(USERS) if index not in existing_indices]
    if missing_indices:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(missing_indices))) as pool:
            list(pool.map(lambda index: register_user(index, run_id), missing_indices))
        users, existing_indices = load_registered_users(run_id)
    if len(existing_indices) != USERS:
        raise RuntimeError(f"expected {USERS} registered users, found {len(existing_indices)}")
    users.sort(key=lambda item: item["username"])
    print(json.dumps({"run_id": run_id, "registered_users": len(users), "reused_users": len(users) - len(missing_indices)}), flush=True)
    registration_seconds = time.monotonic() - started

    if TASKS_PER_TYPE % USERS:
        raise SystemExit("tasks per type must be divisible by user count for even distribution")
    per_user = TASKS_PER_TYPE // USERS
    rounds = per_user
    insertion_started = time.monotonic()
    inserted = 0
    batch_period = SUBMIT_SECONDS / rounds

    for round_index in range(rounds):
        target_time = insertion_started + round_index * batch_period
        delay = target_time - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        now = datetime.now(timezone.utc)
        task_rows: list[dict] = []
        event_rows: list[dict] = []
        for user in users:
            for task_type, kind, duration in (
                ("script.parse", "llm", 10.0),
                ("tts.batch", "tts", 50.0),
            ):
                task_id = new_id()
                created = now
                task_rows.append({
                    "id": task_id,
                    "owner_id": user["user_id"],
                    "project_id": user["project_id"],
                    "task_type": task_type,
                    "status": "queued",
                    "payload": {
                        "_load_simulation": True,
                        "_simulation_kind": kind,
                        "_simulation_seconds": duration,
                        "load_test_run": run_id,
                    },
                    "progress": 0,
                    "error_code": "",
                    "error_message": "",
                    "idempotency_key": f"load-{run_id}-{kind}-{user['username']}-{round_index}",
                    "created_at": created,
                    "updated_at": created,
                })
                event_rows.append({
                    "id": new_id(),
                    "task_id": task_id,
                    "sequence": 1,
                    "event_type": "queued",
                    "payload": {"status": "queued", "load_test": run_id},
                    "created_at": created,
                })
        with SessionLocal.begin() as db:
            db.execute(insert(Task), task_rows)
            db.execute(insert(TaskEvent), event_rows)
        inserted += len(task_rows)
        if inserted % 1000 == 0 or round_index == rounds - 1:
            print(json.dumps({
                "run_id": run_id,
                "users": USERS,
                "submitted": inserted,
                "target_total": TASKS_PER_TYPE * 2,
                "elapsed_seconds": round(time.monotonic() - insertion_started, 1),
            }), flush=True)

    print(json.dumps({
        "ok": True,
        "run_id": run_id,
        "user_count": len(users),
        "first_user": users[0]["username"],
        "last_user": users[-1]["username"],
        "tasks_per_type": TASKS_PER_TYPE,
        "task_types": {"script.parse": TASKS_PER_TYPE, "tts.batch": TASKS_PER_TYPE},
        "registration_seconds": round(registration_seconds, 1),
        "submission_seconds": round(time.monotonic() - insertion_started, 1),
        "simulated_limits": {"llm_slots": 4, "llm_seconds": 10, "tts_slots": 96, "tts_seconds": 50},
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
