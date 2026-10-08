"""One bounded Redis pipeline/DB transaction, partial failure and unknown replay."""
from collections import Counter
from datetime import timedelta
import json

import pytest
import redis
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from backend.platform.database import Base
from backend.platform.models import OutboxEvent, utcnow
from backend.platform import outbox


class FakeRedis:
    def __init__(self):
        self.commands, self.delivered = [], []
        self.responses = None
        self.unknown_prefix = None
        self.executions = 0
        self.closed = False
    def pipeline(self, transaction):
        assert transaction is False
        self.commands = []
        return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def xadd(self, stream, fields, **kwargs):
        assert stream == outbox.STREAM_NAME
        self.commands.append(json.loads(fields["event"]))
        return self
    def execute(self, raise_on_error):
        assert raise_on_error is False
        self.executions += 1
        if self.unknown_prefix is not None:
            self.delivered.extend(item["event_id"] for item in self.commands[:self.unknown_prefix])
            raise redis.ConnectionError("response lost")
        responses = self.responses if self.responses is not None else [f"stream-{i}" for i in range(len(self.commands))]
        self.delivered.extend(item["event_id"] for item, response in zip(self.commands, responses) if isinstance(response, str))
        return responses
    def close(self): self.closed = True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'outbox.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    with sessions.begin() as db:
        db.add_all([OutboxEvent(id=f"event-{i:04d}", aggregate_type="task", aggregate_id="task",
            event_type="task.submitted", payload={"task_id": "task"}) for i in range(1000)])
        db.add(OutboxEvent(id="future", aggregate_type="task", aggregate_id="task", event_type="task.submitted",
            available_at=utcnow() + timedelta(days=1)))
    client = FakeRedis()
    monkeypatch.setattr(outbox, "SessionLocal", sessions)
    monkeypatch.setattr(outbox.redis.Redis, "from_url", lambda *args, **kwargs: client)
    yield sessions, client
    engine.dispose()


def test_1000_events_use_ten_pipelines_and_ten_commits(setup):
    sessions, client = setup
    statements, commits = [], []
    listener = lambda c, cur, statement, *args: statements.append(statement)
    committed = lambda db: commits.append(True)
    event.listen(sessions.kw["bind"], "before_cursor_execute", listener)
    event.listen(sessions.class_, "after_commit", committed)
    try:
        for _ in range(10):
            assert outbox.publish_pending() == 100
            assert len(client.commands) == 100
        assert outbox.publish_pending() == 0
    finally:
        event.remove(sessions.kw["bind"], "before_cursor_execute", listener)
        event.remove(sessions.class_, "after_commit", committed)
    assert client.executions == len(commits) == 10
    assert len(statements) <= 25, len(statements)
    assert len(set(client.delivered)) == 1000 and client.closed
    with sessions() as db:
        assert db.get(OutboxEvent, "future").published_at is None


def test_partial_responses_mark_only_known_successes_once(setup):
    sessions, client = setup
    client.responses = ["first", redis.ResponseError("failed"), "third"]
    assert outbox.publish_pending(limit=3) == 2
    ids = [item["event_id"] for item in client.commands]
    with sessions() as db:
        assert [db.get(OutboxEvent, value).published_at is not None for value in ids] == [True, False, True]
        assert [db.get(OutboxEvent, value).attempts for value in ids] == [1, 1, 1]
    client.responses = None
    assert outbox.publish_pending(limit=1) == 1
    assert client.commands[0]["event_id"] == ids[1]
    with sessions() as db:
        assert db.get(OutboxEvent, ids[1]).attempts == 2


def test_connection_loss_keeps_unknown_rows_and_replays_stable_ids(setup):
    sessions, client = setup
    client.unknown_prefix = 2
    assert outbox.publish_pending(limit=5) == 0
    ids = [item["event_id"] for item in client.commands]
    with sessions() as db:
        assert all(db.get(OutboxEvent, value).published_at is None for value in ids)
    client.unknown_prefix = None
    assert outbox.publish_pending(limit=5) == 5
    assert [item["event_id"] for item in client.commands] == ids
    counts = Counter(client.delivered)
    assert [counts[value] for value in ids] == [2, 2, 1, 1, 1]


def test_database_commit_failure_after_delivery_replays_stable_ids(setup, monkeypatch):
    sessions, client = setup
    def failing_factory():
        db = sessions()
        db.commit = lambda: (_ for _ in ()).throw(RuntimeError("commit failed"))
        return db
    monkeypatch.setattr(outbox, "SessionLocal", failing_factory)
    with pytest.raises(RuntimeError, match="commit failed"):
        outbox.publish_pending(limit=2)
    ids = [item["event_id"] for item in client.commands]
    monkeypatch.setattr(outbox, "SessionLocal", sessions)
    assert outbox.publish_pending(limit=2) == 2
    assert [item["event_id"] for item in client.commands] == ids
    assert Counter(client.delivered) == Counter(dict.fromkeys(ids, 2))


def test_incomplete_response_list_does_not_ack_unknown_rows(setup):
    sessions, client = setup
    client.responses = ["only-one-response"]
    assert outbox.publish_pending(limit=3) == 0
    with sessions() as db:
        assert all(row.published_at is None for row in db.scalars(select(OutboxEvent)))
