from __future__ import annotations

import json
import os
from urllib.parse import urlparse
import uuid

import pytest
import redis
from sqlalchemy import func, select
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
        client.flushdb()  # Lose the recovery stream too, before any claim.
        from datetime import timedelta
        from backend.platform.models import OutboxEvent, utcnow
        with SessionLocal.begin() as db:
            recovery = db.scalar(select(OutboxEvent).where(
                OutboxEvent.aggregate_id == recovered_id, OutboxEvent.event_type == 'task.recovered'))
            original_event_id = recovery.id
            recovery.published_at = utcnow() - timedelta(seconds=11)
        assert recover_database_tasks() >= 1
        assert publish_pending(isolated_redis_url) >= 1
        with SessionLocal() as db:
            assert db.get(OutboxEvent, original_event_id).published_at is not None
            assert db.scalar(select(func.count()).select_from(OutboxEvent).where(
                OutboxEvent.aggregate_id == recovered_id, OutboxEvent.event_type == 'task.recovered')) == 1
        assert _deliver_task(client, recovered_id, "recovery-worker") == "succeeded"
        with SessionLocal() as db:
            recovered = db.get(Task, recovered_id)
            assert recovered.status == "succeeded"
            assert recovered.result.result["ok"] is True
            assert recovered.result.result["task_id"] == recovered_id
    finally:
        client.flushdb()
        client.close()


def test_pipeline_response_loss_replays_event_without_repeating_business_execution(isolated_redis_url, monkeypatch):
    from sqlalchemy import func, select
    from backend.platform.models import TaskAttempt, TaskResult
    from backend.platform import outbox
    client = redis.Redis.from_url(isolated_redis_url, decode_responses=True)
    executions = []
    class Pipeline:
        def __init__(self, inner, lose): self.inner, self.lose = inner, lose
        def __enter__(self): self.inner.__enter__(); return self
        def __exit__(self, *args): return self.inner.__exit__(*args)
        def xadd(self, *args, **kwargs): self.inner.xadd(*args, **kwargs); return self
        def execute(self, **kwargs):
            responses = self.inner.execute(**kwargs)
            if self.lose: raise redis.ConnectionError("accepted XADDs but response lost")
            return responses
    class Connection:
        lose = True
        def pipeline(self, transaction): return Pipeline(client.pipeline(transaction=transaction), self.lose)
        def close(self): pass
    connection = Connection()
    try:
        client.ping(); client.flushdb()
        monkeypatch.setattr(outbox.redis.Redis, "from_url", lambda *a, **k: connection)
        def execute(claim):
            executions.append(claim.task_id)
            return _write_outcome(claim, "replay.json", "application/json", b"{}", {"ok": True})
        monkeypatch.setattr("backend.platform.task_worker.execute_claim", execute)
        task_id = _submit_echo_task()
        assert publish_pending(isolated_redis_url) == 0
        connection.lose = False
        assert publish_pending(isolated_redis_url) >= 1
        assert _deliver_task(client, task_id, "lost-response-worker") == "succeeded"
        copies = []
        for entry_id, fields in client.xrange(STREAM_NAME):
            event = json.loads(fields["event"])
            if event.get("event_type") == "task.submitted" and event.get("payload", {}).get("task_id") == task_id:
                copies.append((entry_id, fields, event["event_id"]))
        assert len(copies) == 2 and copies[0][2] == copies[1][2]
        process_stream_entry(client, copies[1][0], copies[1][1], worker_id="replay-worker")
        assert executions == [task_id]
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(TaskAttempt).where(TaskAttempt.task_id == task_id)) == 1
            assert db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == task_id)) == 1
    finally:
        client.flushdb(); client.close()
