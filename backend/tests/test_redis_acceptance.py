from __future__ import annotations

import json
import os
from urllib.parse import urlparse
import uuid

import pytest
import redis
from backend.platform.database import SessionLocal, initialize_schema
from backend.platform.models import Project, Task, User
from backend.platform.outbox import STREAM_NAME, publish_pending
from backend.platform.task_worker import (
    WORKER_GROUP,
    ensure_consumer_group,
    process_stream_entry,
    recover_database_tasks,
    _write_outcome,
)
from backend.platform.task_submission import submit_task_record


@pytest.fixture
def isolated_redis_url():
    value = os.getenv("NARRIFY_TEST_REDIS_URL")
    if not value:
        pytest.skip("set NARRIFY_TEST_REDIS_URL to an isolated loopback Redis instance")
    parsed = urlparse(value)
    assert parsed.hostname in {"127.0.0.1", "localhost", "::1"}, "Redis acceptance tests require loopback"
    assert parsed.port not in {None, 6379}, "Redis acceptance tests refuse the default application port"
    assert parsed.path == "/15", "Redis acceptance tests require dedicated database 15"
    return value


def _submit_echo_task() -> str:
    owner_id = uuid.uuid4().hex
    project_id = uuid.uuid4().hex
    with SessionLocal() as db:
        user = User(
            id=owner_id,
            email=f"{owner_id}@example.test",
            username=f"u-{owner_id[:20]}",
            password_hash="unused-test-hash",
        )
        db.add(user)
        db.flush()
        db.add(Project(id=project_id, owner_id=user.id, name=f"redis-{project_id[:12]}", directory_key=f"{user.username}/{project_id}"))
        db.commit()
        task = submit_task_record(
            db, user,
            project_id=project_id,
            task_type="text.format",
            payload={"value": "recovered"},
            idempotency_key=f"redis-acceptance-{uuid.uuid4().hex}",
        )
        return task.id


def _deliver_task(client: redis.Redis, task_id: str, worker_id: str) -> str:
    ensure_consumer_group(client)
    batches = client.xreadgroup(WORKER_GROUP, worker_id, {STREAM_NAME: ">"}, count=100, block=1000)
    assert batches, "expected a published task event in Redis Streams"
    for _stream, entries in batches:
        for entry_id, fields in entries:
            raw = fields.get("event") or fields.get(b"event")
            event = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            if event.get("payload", {}).get("task_id") == task_id:
                return process_stream_entry(client, entry_id, fields, worker_id=worker_id)
    pytest.fail(f"Redis Stream did not contain task {task_id}")


def test_redis_dispatch_and_database_recovery(isolated_redis_url, monkeypatch):
    initialize_schema()
    client = redis.Redis.from_url(isolated_redis_url, decode_responses=True)
    try:
        client.ping()
        client.flushdb()
        monkeypatch.setattr(
            "backend.platform.task_worker.execute_claim",
            lambda claim: _write_outcome(
                claim,
                "acceptance.json",
                "application/json",
                json.dumps({"ok": True, "task_id": claim.task_id}).encode(),
                {"ok": True, "task_id": claim.task_id},
            ),
        )
        first_id = _submit_echo_task()
        assert publish_pending(isolated_redis_url) >= 1
        result = _deliver_task(client, first_id, "acceptance-worker")
        assert result == "succeeded"
        with SessionLocal() as db:
            assert db.get(Task, first_id).status == "succeeded"

        recovered_id = _submit_echo_task()
        assert publish_pending(isolated_redis_url) >= 1
        client.flushdb()  # Simulate loss of Redis state while PostgreSQL retains the task.

        assert recover_database_tasks() >= 1
        assert publish_pending(isolated_redis_url) >= 1
        assert _deliver_task(client, recovered_id, "recovery-worker") == "succeeded"
        with SessionLocal() as db:
            recovered = db.get(Task, recovered_id)
            assert recovered.status == "succeeded"
            assert recovered.result.result["ok"] is True
            assert recovered.result.result["task_id"] == recovered_id
    finally:
        client.flushdb()
        client.close()
