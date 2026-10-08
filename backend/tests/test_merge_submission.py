"""Merge admission stays atomic, idempotent and independent of model quota."""
from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker

from backend.core.config import AppConfig
from backend.platform.database import Base
from backend.platform.models import User, Project, Task, TaskBatch, TaskEvent, OutboxEvent
from backend.platform import merge_submission as submission


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'merge.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    seed(factory)
    configure(monkeypatch, tmp_path)
    yield factory
    engine.dispose()


def seed(factory):
    with factory.begin() as db:
        db.add(User(id="owner", username="merger", email="merge@example.test", password_hash="unused"))
        db.flush()
        db.add(Project(id="project", owner_id="owner", name="Book", directory_key="merger/book"))


def configure(monkeypatch, tmp_path):
    monkeypatch.setattr(submission, "lock_storage_migration", lambda *a, **k: True)
    monkeypatch.setattr(submission, "storage_migration", lambda db: None)
    monkeypatch.setattr(submission, "project_workspace_path", lambda *a: tmp_path)
    monkeypatch.setattr(submission, "get_config", lambda: AppConfig())


def submit(factory, names, key="key", **kwargs):
    with factory() as db:
        ctx = SimpleNamespace(user=db.get(User, "owner"), session=SimpleNamespace(active_project_id="project"))
        return submission.submit_merge_tasks(packages=names, ctx=ctx, db=db, idempotency_key=key, **kwargs)


def counts(factory):
    with factory() as db:
        return [db.scalar(select(func.count()).select_from(model)) for model in (Task, TaskBatch, TaskEvent, OutboxEvent)]


def test_620_merges_share_snapshot_and_one_commit_without_quota(sessions, monkeypatch):
    config_calls, preflights, commits, sql = [], [], [], []
    monkeypatch.setattr(submission, "get_config", lambda: config_calls.append(True) or AppConfig())
    engine = sessions.kw['bind']
    event.listen(engine, "before_cursor_execute", lambda c, cur, statement, *a: sql.append(statement))
    event.listen(sessions.class_, "after_commit", lambda db: commits.append(True))
    names = [f"chapter-{i}" for i in range(620)]
    receipt = submit(sessions, names + [names[0]], preflight=lambda names: preflights.append(names))
    assert len(receipt['task_ids']) == 620
    assert [r['package'] for r in receipt['packages']] == names
    assert counts(sessions) == [620, 1, 620, 1]
    assert len(commits) == len(config_calls) == len(preflights) == 1
    assert not any('user_quota_accounts' in statement.lower() for statement in sql)
    assert sum(s.lower().startswith('insert into tasks ') for s in sql) == 7
    with sessions() as db:
        tasks = db.scalars(select(Task)).all()
        assert all('config' not in task.payload and task.payload['_batch_config_id'] == receipt['batch_id'] for task in tasks)
        assert len({task.payload['_audio_identity'] for task in tasks}) == 620
        assert all(task.event_sequence == 1 for task in tasks)
        wake = db.scalar(select(OutboxEvent))
        assert wake.payload['batch_id'] == receipt['batch_id']


def test_replay_ignores_current_config_and_active_tasks(sessions, monkeypatch):
    first = submit(sessions, ['one', 'two'])
    monkeypatch.setattr(submission, 'get_config', lambda: pytest.fail('replay must not read config'))
    assert submit(sessions, ['one', 'two'], preflight=lambda _: pytest.fail('replay must not probe locks')) == first
    assert counts(sessions) == [2, 1, 2, 1]
    with pytest.raises(HTTPException) as exc:
        submit(sessions, ['two', 'one'])
    assert exc.value.status_code == 409


def test_overlap_rejected_and_independent_package_allowed(sessions):
    submit(sessions, ['one'])
    with pytest.raises(HTTPException) as exc:
        submit(sessions, ['one', 'two'], key='other')
    assert exc.value.status_code == 409
    assert counts(sessions) == [1, 1, 1, 1]
    submit(sessions, ['two'], key='third')
    assert counts(sessions) == [2, 2, 2, 2]


def test_second_chunk_failure_rolls_back_every_record(sessions):
    inserts = []
    def fail(c, cur, statement, *args):
        if statement.lower().startswith('insert into tasks '):
            inserts.append(True)
            if len(inserts) == 2:
                raise RuntimeError('injected second chunk failure')
    engine = sessions.kw['bind']
    event.listen(engine, 'before_cursor_execute', fail)
    with pytest.raises(RuntimeError):
        submit(sessions, [f'chapter-{i}' for i in range(101)])
    event.remove(engine, 'before_cursor_execute', fail)
    assert counts(sessions) == [0, 0, 0, 0]
    assert len(submit(sessions, ['one'])['task_ids']) == 1


@pytest.mark.parametrize('names,status', [([], 400), (['..'], 400), ([''], 400),
    (['a/b'], 400), (['a\\b'], 400), ([f'c{i}' for i in range(1001)], 422)])
def test_invalid_inputs_create_nothing(sessions, names, status):
    with pytest.raises(HTTPException) as exc:
        submit(sessions, names)
    assert exc.value.status_code == status
    assert counts(sessions) == [0, 0, 0, 0]


def test_migration_and_preflight_failure_leave_no_partial_receipt(sessions, monkeypatch):
    monkeypatch.setattr(submission, 'storage_migration', lambda db: {'running': True})
    with pytest.raises(HTTPException) as exc:
        submit(sessions, ['one'])
    assert exc.value.status_code == 409
    monkeypatch.setattr(submission, 'storage_migration', lambda db: None)
    def reject(names):
        raise HTTPException(409, 'save lock held')
    with pytest.raises(HTTPException):
        submit(sessions, ['one'], preflight=reject)
    assert counts(sessions) == [0, 0, 0, 0]


def test_receipt_scoped_to_owned_project(sessions):
    submit(sessions, ['one'])
    with sessions.begin() as db:
        db.add(Project(id='second', owner_id='owner', name='Other', directory_key='merger/other'))
    with pytest.raises(HTTPException) as exc:
        submit(sessions, ['one'], project_id='second')
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException):
        submit(sessions, ['two'], key='new', project_id='unowned')
    assert counts(sessions) == [1, 1, 1, 1]


@pytest.fixture
def postgres_sessions(tmp_path, monkeypatch):
    url = os.environ.get('NARRIFY_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('isolated PostgreSQL required')
    engine = create_engine(url, pool_size=8)
    schema = 'merge_' + uuid.uuid4().hex
    with engine.begin() as c:
        c.execute(text(f'CREATE SCHEMA {schema}'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(scoped)
    factory = sessionmaker(bind=scoped, expire_on_commit=False, autoflush=False)
    seed(factory)
    configure(monkeypatch, tmp_path)
    yield factory
    with engine.begin() as c:
        c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
    engine.dispose()


@pytest.mark.parametrize('same_key', [False, True])
def test_postgres_concurrent_overlap_or_replay(postgres_sessions, same_key):
    barrier = Barrier(2)
    def worker(i):
        barrier.wait()
        try:
            return submit(postgres_sessions, ['one', 'two'], key='same' if same_key else f'key-{i}')
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert counts(postgres_sessions) == [2, 1, 2, 1]
    if same_key:
        assert results[0] == results[1]
    else:
        assert sum(isinstance(result, dict) for result in results) == 1
        assert 409 in results


@pytest.mark.parametrize('size', [620, 1000])
def test_postgres_bulk_admission_with_encoding_and_progress(postgres_sessions, tmp_path, size):
    """Controlled timing under real concurrent encoding and database updates."""
    import subprocess
    import threading
    import time
    import wave
    from sqlalchemy import update
    factory = postgres_sessions
    with factory.begin() as db:
        db.add(Task(id='working', owner_id='owner', project_id='project', task_type='tts.merge',
                    status='running', payload={'package': 'working', '_audio_identity': 'working'}))
    wav = tmp_path / 'segment.wav'
    with wave.open(str(wav), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(24000)
        out.writeframes(b'\0\0' * 24000)
    inputs = tmp_path / 'inputs.txt'
    inputs.write_text(''.join(f"file '{wav}'\n" for _ in range(80)))
    stop = threading.Event()
    updates, errors, encodes = [], [], []
    def background():
        try:
            while not stop.is_set():
                with factory.begin() as db:
                    db.execute(update(Task).where(Task.id == 'working').values(progress=len(updates) % 99))
                updates.append(True)
                stop.wait(.01)
        except BaseException as exc:
            errors.append(exc)
    def encode():
        try:
            while not stop.is_set():
                result = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0',
                    '-i', str(inputs), '-threads', '1', str(tmp_path / 'merged.mp3')], capture_output=True)
                if result.returncode:
                    raise RuntimeError(result.stderr.decode())
                encodes.append(True)
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=background), threading.Thread(target=encode)]
    for thread in threads:
        thread.start()
    durations = []
    try:
        for iteration in range(5):
            start = time.monotonic()
            from backend.api.tts import _merge_preflight
            receipt = submit(factory, [f'c{iteration}-{i}' for i in range(size)], key=f'b{iteration}', preflight=_merge_preflight)
            durations.append(time.monotonic() - start)
            assert len(receipt['task_ids']) == size
    finally:
        stop.set()
        for thread in threads:
            thread.join(30)
    assert not errors and updates and encodes
    assert all(not t.is_alive() for t in threads)
    print(f'bulk merge {size}: samples={durations}, max={max(durations):.3f}s, progress_updates={len(updates)}, encodes={len(encodes)}')
    assert max(durations) < 5


def test_merge_worker_loads_batch_snapshot_and_legacy_embedded_config(sessions, monkeypatch, tmp_path):
    from backend.core import config
    from backend.platform import task_engine_support as support
    from backend.platform.task_contracts import TaskClaim
    snapshot = AppConfig().model_copy(update={'tts': AppConfig().tts.model_copy(update={'pause_same_speaker_ms': 123})})
    monkeypatch.setattr(submission, 'get_config', lambda: snapshot)
    first = submit(sessions, ['one'])
    monkeypatch.setattr(support, 'SessionLocal', sessions)
    monkeypatch.setattr(support, 'project_workspace_path', lambda *args: tmp_path)
    with sessions() as db:
        task = db.get(Task, first['task_ids'][0]); payload = task.payload
    claim = TaskClaim(task_id=first['task_ids'][0], attempt_id='attempt', attempt_no=1,
                      lease_token='lease', worker_id='worker', owner_id='owner', project_id='project',
                      task_type='tts.merge', payload=payload)
    with support.engine_execution_context(claim):
        assert config.get_config().tts.pause_same_speaker_ms == 123
    from dataclasses import replace
    legacy = AppConfig().model_copy(update={'tts': AppConfig().tts.model_copy(update={'pause_same_speaker_ms': 456})})
    with support.engine_execution_context(replace(claim, payload={'config': legacy.model_dump(mode='json')})):
        assert config.get_config().tts.pause_same_speaker_ms == 456
