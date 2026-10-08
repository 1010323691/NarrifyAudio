"""Exercise Worker publication callbacks with a real, tightly bounded QueuePool."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from backend.platform.database import Base
from backend.platform.models import User
from backend.platform import task_engine_support as support
from backend.platform.task_contracts import TaskClaim


@pytest.fixture
def pool(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path}/pool.db', poolclass=QueuePool,
                           pool_size=3, max_overflow=0, pool_timeout=0.2,
                           connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as db:
        db.add(User(id='u', username='u', email='u@test.local', password_hash='unused'))
    monkeypatch.setattr(support, 'SessionLocal', sessions)
    monkeypatch.setattr(support, 'task_attempt_path',
                        lambda db, username, project, task, attempt, name: tmp_path / task / name)
    yield engine, sessions, tmp_path
    engine.dispose()


def claim(index):
    return TaskClaim(f'task-{index}', 'attempt', 1, 'token', 'worker', 'u', 'p', 'book.split', {})


def test_concurrent_publishers_release_pool_slots_before_progress_callbacks(pool):
    engine, sessions, _ = pool
    barrier = Barrier(3)
    def publish(index):
        def progress(value):
            barrier.wait(timeout=5)
            # The old writer kept all three connections while these callbacks
            # tried to open three more, deterministically timing out here.
            with sessions() as db:
                assert db.scalar(select(User.id)) == 'u'
        return support.write_task_outcome(claim(index), 'first.txt', 'text/plain', b'first', {},
                                          additional_outputs=[('second.txt', 'text/plain', b'second')],
                                          on_progress=progress)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(publish, range(3)))
    assert all(result.temp_path.read_bytes() == b'first' for result in results)
    assert all(result.additional_outputs[0].temp_path.read_bytes() == b'second' for result in results)
    assert engine.pool.checkedout() == 0


def test_callback_failure_removes_every_staged_output_and_returns_connections(pool):
    engine, sessions, root = pool
    def progress(value):
        with sessions() as db:
            assert db.scalar(select(User.id)) == 'u'
        if value == 1:
            raise RuntimeError('publication cancelled')
    with pytest.raises(RuntimeError, match='publication cancelled'):
        support.write_task_outcome(claim('failed'), 'first.txt', 'text/plain', b'first', {},
                                   additional_outputs=[('second.txt', 'text/plain', b'second')],
                                   on_progress=progress)
    assert not (root / 'task-failed').exists()
    assert engine.pool.checkedout() == 0
