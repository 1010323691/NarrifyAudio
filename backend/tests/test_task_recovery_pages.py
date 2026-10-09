"""Recovery must select expired work in SQL and advance past pending dispatches."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from backend.platform.database import Base
from backend.platform import task_worker
from backend.platform.models import Task, TaskAttempt, OutboxEvent, utcnow


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "recovery.db"}')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(task_worker, 'SessionLocal', factory)
    monkeypatch.setattr(task_worker, '_reconcile_attempt_publication', lambda *args: None)
    yield factory, engine
    engine.dispose()


def add(db, name, *, live=False, expired=False, pending=False, future=False):
    now = utcnow()
    db.add(Task(id=name, owner_id='owner', project_id='project', task_type='text.format',
                status='running' if live or expired else 'pending', payload={},
                idempotency_key=name, next_attempt_at=now + timedelta(hours=1) if future else None))
    if live or expired:
        db.add(TaskAttempt(id=f'attempt-{name}', task_id=name, attempt_no=1, status='running',
                           lease_token='token', lease_expires_at=now + timedelta(hours=1) if live else now - timedelta(seconds=1)))
    if pending:
        db.add(OutboxEvent(id=f'event-{name}', aggregate_type='task', aggregate_id=name,
                           event_type='task.submitted', payload={}, available_at=now))


def test_live_prefix_never_materializes_or_blocks_expired_tail(isolated):
    factory, engine = isolated
    with factory.begin() as db:
        for index in range(1000):
            add(db, f'a-{index:04}', live=True)
        for index in range(205):
            add(db, f'z-{index:04}', expired=True)
        add(db, 'future', future=True)
    loaded = []
    def record(session, obj):
        if isinstance(obj, Task): loaded.append(obj.id)
    event.listen(factory, 'loaded_as_persistent', record)
    try:
        count, cursor = task_worker.recover_database_task_page()
        assert count == 100 and cursor == 'z-0099'
        assert loaded == [f'z-{index:04}' for index in range(100)]
        count, cursor = task_worker.recover_database_task_page(after_id=cursor)
        assert count == 100 and cursor == 'z-0199'
        count, cursor = task_worker.recover_database_task_page(after_id=cursor)
        assert count == 5 and cursor == ''
    finally:
        event.remove(factory, 'loaded_as_persistent', record)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(TaskAttempt).where(TaskAttempt.status == 'running')) == 1000
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 205
        assert db.get(Task, 'future').status == 'pending'


def test_cursor_skips_pending_outbox_prefix_and_wraps(isolated):
    factory, _ = isolated
    with factory.begin() as db:
        for index in range(105): add(db, f'a-{index:04}', pending=True)
        add(db, 'z-expired', expired=True)
    count, cursor = task_worker.recover_database_task_page()
    assert count == 0 and cursor == 'a-0099'
    count, cursor = task_worker.recover_database_task_page(after_id=cursor)
    assert count == 1 and cursor == ''
    assert task_worker.recover_database_task_page(after_id='zz-deleted-cursor') == (0, '')
    assert task_worker.recover_database_task_page(after_id='a-0099') == (0, '')


def test_commit_failure_does_not_return_cursor_or_expire_attempt(isolated, monkeypatch):
    factory, _ = isolated
    with factory.begin() as db: add(db, 'expired', expired=True)
    with monkeypatch.context() as patch:
        def fail(session): raise RuntimeError('commit interrupted')
        patch.setattr(factory.class_, 'commit', fail)
        with pytest.raises(RuntimeError, match='commit interrupted'):
            task_worker.recover_database_task_page(limit=1)
    with factory() as db:
        assert db.get(TaskAttempt, 'attempt-expired').status == 'running'
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
    assert task_worker.recover_database_task_page(limit=1) == (1, 'expired')


def test_unsafe_reconciliation_keeps_attempt_fenced_without_blocking_later_rows(isolated, monkeypatch):
    factory, _ = isolated
    with factory.begin() as db:
        add(db, 'a-unsafe', expired=True)
        add(db, 'b-valid', expired=True)
    def reconcile(db, task, attempt):
        if task.id == 'a-unsafe': raise ValueError('unsafe workspace')
    monkeypatch.setattr(task_worker, '_reconcile_attempt_publication', reconcile)
    assert task_worker.recover_database_task_page(limit=1) == (0, 'a-unsafe')
    assert task_worker.recover_database_task_page(limit=1, after_id='a-unsafe') == (1, 'b-valid')
    with factory() as db:
        assert db.get(TaskAttempt, 'attempt-a-unsafe').status == 'running'
        assert db.get(TaskAttempt, 'attempt-b-valid').status == 'expired'


def test_repeated_stream_loss_replays_one_stable_recovery_event(isolated):
    factory, _ = isolated
    with factory.begin() as db: add(db, 'pending')
    assert task_worker.recover_database_tasks() == 1
    with factory.begin() as db:
        item = db.scalar(select(OutboxEvent))
        original_id = item.id
        item.published_at = utcnow()
    assert task_worker.recover_database_tasks() == 0
    with factory.begin() as db:
        db.get(OutboxEvent, original_id).published_at = utcnow() - timedelta(seconds=11)
    assert task_worker.recover_database_tasks() == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert db.get(OutboxEvent, original_id).published_at is None


def test_replay_backoff_doubles_per_replay_and_caps(isolated):
    factory, _ = isolated
    with factory.begin() as db: add(db, 'pending')
    assert task_worker.recover_database_tasks() == 1
    with factory.begin() as db:
        item = db.scalar(select(OutboxEvent))
        original_id, item.published_at = item.id, utcnow()
    for replays, wait in ((0, 11), (1, 21), (2, 41)):
        # Younger than the doubled interval: not replayed yet.
        with factory.begin() as db:
            db.get(OutboxEvent, original_id).published_at = utcnow() - timedelta(seconds=wait // 2 + 1)
        assert task_worker.recover_database_tasks() == (1 if replays == 0 and wait // 2 + 1 > 10 else 0)
        with factory.begin() as db:
            event = db.get(OutboxEvent, original_id)
            event.published_at = utcnow() - timedelta(seconds=wait)
            event.payload = {**event.payload, "replays": replays}
        assert task_worker.recover_database_tasks() == 1
        with factory.begin() as db:
            event = db.get(OutboxEvent, original_id)
            assert event.payload["replays"] == replays + 1
            event.published_at = utcnow()
    with factory.begin() as db:
        event = db.get(OutboxEvent, original_id)
        event.payload = {**event.payload, "replays": 30}
        event.published_at = utcnow() - timedelta(seconds=599)
    assert task_worker.recover_database_tasks() == 0
    with factory.begin() as db:
        db.get(OutboxEvent, original_id).published_at = utcnow() - timedelta(seconds=601)
    assert task_worker.recover_database_tasks() == 1


def test_claim_task_skips_the_audio_permit_check_for_other_task_types(isolated, monkeypatch):
    from backend.platform import mechanical_audio
    factory, _ = isolated
    with factory.begin() as db: add(db, 'pending')
    calls = []
    monkeypatch.setattr(mechanical_audio, 'capacity_available', lambda: calls.append(1) or True)
    try:
        task_worker.claim_task('pending', 'worker')
    except Exception:
        pass  # only the admission pre-check is under test
    assert calls == []
