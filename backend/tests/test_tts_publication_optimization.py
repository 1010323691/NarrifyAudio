"""Publication batching must preserve charging, progress fencing and retry semantics."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from threading import Barrier
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from backend.platform import quota, task_context
from backend.platform.database import Base
from backend.platform.models import (
    Project, QuotaHold, QuotaTransaction, Task, TaskAttempt, TaskEvent, User,
    UserQuotaAccount, utcnow,
)
from backend.engines.tts_batch import _BatchRunLogHandle, _PooledFile, _update_pool_result


def seed(sessions):
    with sessions() as db:
        db.add(User(id="user", email="batch@example.com", username="batch", password_hash="unused"))
        db.flush()
        db.add(Project(id="project", owner_id="user", name="batch", directory_key="batch"))
        db.flush()
        db.add(Task(id="task", owner_id="user", project_id="project", task_type="tts.batch", status="running"))
        db.flush()
        db.add_all([
            UserQuotaAccount(user_id="user", available_units=100, reserved_units=20, consumed_units=0),
            QuotaHold(task_id="task", user_id="user", attempt_id="attempt", operation_type="a", units=12),
            QuotaHold(task_id="task", user_id="user", attempt_id="attempt", operation_type="b", units=8),
            TaskAttempt(id="attempt", task_id="task", attempt_no=1, lease_token="lease",
                        lease_expires_at=utcnow() + timedelta(hours=1)),
        ])
        db.commit()


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'batch.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    seed(sessions)
    monkeypatch.setattr(quota, "SessionLocal", sessions)
    monkeypatch.setattr(task_context, "SessionLocal", sessions)
    token = quota.set_quota_context("user", "task", "attempt")
    yield sessions
    quota.reset_quota_context(token)
    engine.dispose()


def balances(sessions):
    with sessions() as db:
        account = db.get(UserQuotaAccount, "user")
        return (account.available_units, account.reserved_units, account.consumed_units,
                [(h.operation_type, h.units, h.status) for h in db.scalars(select(QuotaHold).order_by(QuotaHold.operation_type))],
                list(db.scalars(select(QuotaTransaction).order_by(QuotaTransaction.consumed_before))))


def test_batch_quota_one_commit_preserves_each_segment_ledger(sessions):
    commits = []
    event.listen(sessions.class_, "after_commit", lambda _db: commits.append(True))
    assert quota.consume_tts_inputs([(3, "a", "a:0"), (4, "a", "a:1"), (5, "b", "b:0"), (3, "a", "a:0")])
    assert len(commits) == 1
    available, reserved, consumed, holds, ledger = balances(sessions)
    assert (available, reserved, consumed) == (100, 8, 12)
    assert holds == [("a", 5, "held"), ("b", 3, "held")]
    assert [t.char_count for t in ledger] == [3, 4, 5]
    assert [(t.reserved_before, t.reserved_after, t.consumed_before, t.consumed_after) for t in ledger] == [
        (20, 17, 0, 3), (17, 13, 3, 7), (13, 8, 7, 12),
    ]


def test_quota_mixed_retry_and_consumed_hold_do_not_double_charge(sessions):
    assert quota.consume_tts_input(12, "a", "a:0")
    assert quota.consume_tts_inputs([(12, "a", "a:0"), (8, "b", "b:0")])
    assert quota.consume_tts_inputs([(12, "a", "a:0"), (8, "b", "b:0")])
    _, reserved, consumed, holds, ledger = balances(sessions)
    assert (reserved, consumed, len(ledger)) == (0, 20, 2)
    assert holds == [("a", 0, "consumed"), ("b", 0, "consumed")]


@pytest.mark.parametrize("inputs", [[(3, "a", "0"), (9, "b", "1")], [(21, "a", "0")], [(1, "unknown", "0")]])
def test_quota_batch_validation_rolls_back_every_segment(sessions, inputs):
    with pytest.raises(RuntimeError, match="预留不足"):
        quota.consume_tts_inputs(inputs)
    available, reserved, consumed, holds, ledger = balances(sessions)
    assert (available, reserved, consumed, len(ledger)) == (100, 20, 0, 0)
    assert holds == [("a", 12, "held"), ("b", 8, "held")]


def test_released_quota_and_conflicting_duplicate_cannot_charge(sessions):
    with pytest.raises(ValueError, match="幂等键"):
        quota.consume_tts_inputs([(2, "a", "same"), (3, "a", "same")])
    with sessions() as db:
        quota.release_attempt_holds("task", "attempt", db=db)
        db.commit()
    with pytest.raises(RuntimeError, match="预留不足"):
        quota.consume_tts_inputs([(1, "a", "0")])
    available, reserved, consumed, _, ledger = balances(sessions)
    assert (available, reserved, consumed) == (120, 0, 0)
    assert all(t.kind == "release" for t in ledger)


def claim():
    return SimpleNamespace(task_id="task", attempt_id="attempt", lease_token="lease")


def test_combined_chapter_snapshot_commits_once_and_keeps_order(sessions):
    commits = []
    event.listen(sessions.class_, "after_commit", lambda _db: commits.append(True))
    assert task_context.update_chapter_snapshot(claim(), 3, 10, 12, 50, "保存中")
    assert len(commits) == 1
    with sessions() as db:
        task = db.get(Task, "task")
        events = list(db.scalars(select(TaskEvent).order_by(TaskEvent.sequence)))
        assert task.progress == 30
        assert [(e.sequence, e.event_type) for e in events] == [(1, "segments"), (2, "progress")]
        assert events[0].payload == {"done": 3, "total": 10, "chars_done": 12, "chars_total": 50}


def test_combined_snapshot_failure_is_atomic_and_stale_lease_is_fenced(sessions, monkeypatch):
    append = task_context.append_task_event
    def fail_progress(db, task_id, kind, payload):
        if kind == "progress":
            raise OSError("event write failed")
        return append(db, task_id, kind, payload)
    monkeypatch.setattr(task_context, "append_task_event", fail_progress)
    with pytest.raises(OSError):
        task_context.update_chapter_snapshot(claim(), 3, 10, 12, 50, "保存中")
    with sessions() as db:
        assert db.get(Task, "task").progress == 0
        assert not list(db.scalars(select(TaskEvent)))
        attempt = db.get(TaskAttempt, "attempt")
        attempt.lease_token = "new-owner"
        db.commit()
    assert not task_context.update_chapter_snapshot(claim(), 3, 10, 12, 50, "保存中")


def test_incremental_counts_handle_retry_error_and_resume_baseline():
    f = _PooledFile(name="chapter", by_index={0: {"text": "old"}, 1: {"text": "new text"}},
                    done_set={0}, done_count=1, done_chars=3)
    _update_pool_result(f, 1, {"ok": True})
    _update_pool_result(f, 1, {"ok": True})
    assert (f.done_count, f.done_chars) == (2, 11)
    _update_pool_result(f, 1, {"ok": False})
    assert (f.done_count, f.done_chars) == (1, 3)
    _update_pool_result(f, 1, {"ok": True})
    assert (f.done_count, f.done_chars) == (2, 11)


def test_quota_buffer_flushes_audio_before_charge_and_retries_failed_batch(monkeypatch, tmp_path):
    order = []
    handle = SimpleNamespace(flush_workspace_stages=lambda **kw: order.append("audio"))
    wrapped = _BatchRunLogHandle(handle, tmp_path / "run.log")
    def charge(inputs):
        order.append("quota")
        if order.count("quota") == 1:
            raise RuntimeError("temporary quota failure")
        assert inputs == [(3, "a", "0")]
    monkeypatch.setattr(quota, "consume_tts_inputs", charge)
    wrapped.queue_tts_input(3, "a", "0")
    with pytest.raises(RuntimeError):
        wrapped.flush_workspace_stages(force=True)
    assert wrapped.quota_inputs == [(3, "a", "0")]
    wrapped.flush_workspace_stages(force=True)
    assert not wrapped.quota_inputs
    assert all(order[i - 1] == "audio" for i, value in enumerate(order) if value == "quota")


def test_quota_buffer_has_bounded_batch_size(monkeypatch, tmp_path):
    batches = []
    monkeypatch.setattr(quota, "consume_tts_inputs", lambda rows: batches.append(list(rows)))
    wrapped = _BatchRunLogHandle(SimpleNamespace(), tmp_path / "run.log")
    monkeypatch.setattr("backend.engines.tts_batch.time.monotonic", lambda: 1.0)
    for index in range(257):
        wrapped.queue_tts_input(1, "a", str(index))
    assert [len(batch) for batch in batches] == [128, 128]
    wrapped.flush_workspace_stages(force=True)
    assert [len(batch) for batch in batches] == [128, 128, 1]


def test_publication_metrics_are_throttled_and_show_staged_backlog(tmp_path, monkeypatch):
    messages = []
    metrics = {"published_audio": 2, "journal_bytes": 100, "journal_seconds": 0.1, "move_seconds": 0.01}
    handle = SimpleNamespace(log=lambda msg, *args: messages.append(msg),
                             _publication_journal=SimpleNamespace(metrics=metrics))
    wrapped = _BatchRunLogHandle(handle, tmp_path / "run.log")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "0003.mp3").write_bytes(b"audio")
    wrapped.stage_dirs = [stage]
    wrapped.expected_audio = 3
    wrapped.last_metrics = 0
    monkeypatch.setattr("backend.engines.tts_batch.time.monotonic", lambda: 31)
    wrapped.report_publication()
    wrapped.report_publication()
    assert len(messages) == 1
    assert "已生成文件 3/3" in messages[0] and "暂存待发布 1" in messages[0]
    wrapped.report_publication(force=True)
    assert len(messages) == 2


def test_chapter_snapshot_retries_rejected_write_instead_of_caching(monkeypatch):
    from backend.platform.tts_batch_execution import TTSBatchContext
    attempts = []
    def update(*args):
        attempts.append(args)
        return len(attempts) > 1
    monkeypatch.setattr(task_context, "update_chapter_snapshot", update)
    c = SimpleNamespace(task_id="task", payload={"scripts": ["chapter"]})
    handle = TTSBatchContext([c], lambda *args: True, lambda *args: True)
    for _ in range(3):
        handle.chapter_progress("chapter", 1, 2, 3, 6)
    assert len(attempts) == 2


@pytest.mark.skipif(not os.environ.get("NARRIFY_TEST_POSTGRES_URL"), reason="real PostgreSQL required for concurrent quota retries")
@pytest.mark.parametrize("with_release", [False, True])
def test_postgres_concurrent_batch_retries_charge_each_key_once(monkeypatch, with_release):
    engine = create_engine(os.environ["NARRIFY_TEST_POSTGRES_URL"],
                           connect_args={"options": "-c lock_timeout=5000 -c statement_timeout=10000"})
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("PostgreSQL required")
    schema = "tts_batch_" + uuid.uuid4().hex
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        scoped = engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(scoped)
        sessions = sessionmaker(bind=scoped, expire_on_commit=False, autoflush=False)
        seed(sessions)
        monkeypatch.setattr(quota, "SessionLocal", sessions)
        barrier = Barrier(8)
        def consume(index):
            token = quota.set_quota_context("user", "task", "attempt")
            try:
                barrier.wait(timeout=10)
                if with_release and index == 0:
                    with sessions() as db:
                        quota.release_attempt_holds("task", "attempt", db=db)
                        db.commit()
                    return True
                try:
                    return quota.consume_tts_inputs([(12, "a", "a:0"), (8, "b", "b:0")])
                except RuntimeError:
                    if not with_release:
                        raise
                    return True
            finally:
                quota.reset_quota_context(token)
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert all(pool.map(consume, range(8)))
        available, reserved, consumed, _, ledger = balances(sessions)
        assert reserved == 0 and len(ledger) == 2
        assert (available, consumed) in ([(100, 20), (120, 0)] if with_release else [(100, 20)])
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()
