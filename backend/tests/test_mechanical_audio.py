"""Host-wide admission and actual owned-child lifecycle, with isolated storage."""
from datetime import timedelta
import multiprocessing
import os
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.platform.database import Base
from backend.platform import mechanical_audio as audio
from backend.platform.gpu_scheduler import store
from backend.platform.models import MechanicalAudioPermit, MechanicalAudioState, Task, TaskAttempt, utcnow
from backend.platform.task_contracts import TaskClaim
from backend.core.managed_process import process_identity, terminate_owned


def claim(index):
    return TaskClaim(str(index), f"attempt-{index}", 1, f"token-{index}", "worker", "owner", "project", "tts.merge", {})


def seed(factory, count=8):
    with factory.begin() as db:
        for index in range(count):
            item = claim(index)
            db.add(Task(id=item.task_id, owner_id="owner", project_id="project", task_type="tts.merge",
                        status="running", payload={}, idempotency_key=f"key-{index}"))
            db.add(TaskAttempt(id=item.attempt_id, task_id=item.task_id, attempt_no=1, worker_id="worker",
                               lease_token=item.lease_token, status="running", lease_expires_at=utcnow() + timedelta(seconds=60)))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'audio.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    seed(factory)
    monkeypatch.setattr(audio, "SessionLocal", factory)
    monkeypatch.setattr(audio, "memory_snapshot", lambda: (4 * audio.GIB, 0))
    monkeypatch.setattr(audio, "merge_concurrency_limit", lambda: 2)
    monkeypatch.setattr(store, "PROJECT_ROOT", tmp_path)
    yield factory, tmp_path
    engine.dispose()


def _competing_process(url, root, index, barrier, results, done):
    engine = create_engine(url)
    audio.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    audio.memory_snapshot = lambda: (4 * audio.GIB, 0)
    audio.merge_concurrency_limit = lambda: 2
    store.PROJECT_ROOT = Path(root)
    barrier.wait(timeout=30)
    with store.host_lock(), audio.SessionLocal.begin() as db:
        admitted = audio.reserve(db, claim(index))
    results.put(admitted)
    done.wait(timeout=30)
    if admitted:
        audio.release(claim(index))
    engine.dispose()


def test_multiple_worker_processes_share_one_capacity(isolated):
    factory, root = isolated
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(8)
    results, done = context.Queue(), context.Event()
    workers = [context.Process(target=_competing_process,
               args=(str(factory.kw['bind'].url), str(root), i, barrier, results, done)) for i in range(8)]
    try:
        for worker in workers:
            worker.start()
        assert sum(results.get(timeout=45) for _ in workers) == 2
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(MechanicalAudioPermit)) == 2
        assert not audio.capacity_available()
    finally:
        done.set()
        for worker in workers:
            worker.join(timeout=10)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
    assert all(worker.exitcode == 0 for worker in workers)
    assert audio.capacity_available()


def test_memory_hysteresis_is_persisted_between_checks(isolated, monkeypatch):
    factory, _ = isolated
    for available, expected in [(audio.GIB, False), (int(2.5 * audio.GIB), False), (3 * audio.GIB, True)]:
        monkeypatch.setattr(audio, "memory_snapshot", lambda: (available, 0))
        assert audio.capacity_available() is expected
        with factory() as db:
            assert db.get(MechanicalAudioState, "local").memory_paused is (not expected)


def test_unknown_memory_allows_only_one_job(isolated, monkeypatch):
    factory, _ = isolated
    monkeypatch.setattr(audio, "memory_snapshot", lambda: (None, None))
    with store.host_lock(), factory.begin() as db:
        assert audio.reserve(db, claim(0))
        assert not audio.reserve(db, claim(1))


def test_child_remains_fenced_after_task_lease_expiry(isolated):
    factory, _ = isolated
    current = claim(0)
    with store.host_lock(), factory.begin() as db:
        assert audio.reserve(db, current)
    token = audio.bind_claim(current)
    proc = None
    try:
        proc = audio.spawn_registered([sys.executable, "-c", "import time; time.sleep(60)"])
        with factory.begin() as db:
            db.get(TaskAttempt, current.attempt_id).lease_expires_at = utcnow() - timedelta(seconds=1)
        assert not audio.release(current)
        assert audio.capacity_available()
        with factory() as db:
            assert db.get(MechanicalAudioPermit, current.attempt_id) is not None
    finally:
        if proc is not None:
            terminate_owned(proc)
            proc.wait(timeout=10)
            audio.finish_registered(proc)
        assert audio.release(current)
        audio.reset_claim(token)


def test_dead_owner_with_live_child_blocks_new_admissions(isolated):
    factory, _ = isolated
    with factory.begin() as db:
        db.add(MechanicalAudioPermit(attempt_id=claim(0).attempt_id, task_id=claim(0).task_id,
                                     process={"owner": {"pid": os.getpid(), "created": -1},
                                              "children": [process_identity(os.getpid())], "spawning": False}))
    assert not audio.capacity_available()
    with factory() as db:
        assert db.get(MechanicalAudioState, "local").error


def test_reused_pid_without_child_does_not_keep_a_slot(isolated):
    factory, _ = isolated
    with factory.begin() as db:
        db.add(MechanicalAudioPermit(attempt_id=claim(0).attempt_id, task_id=claim(0).task_id,
                                     process={"owner": {"pid": os.getpid(), "created": -1},
                                              "children": [], "spawning": False}))
    assert audio.capacity_available()
    with factory() as db:
        assert db.get(MechanicalAudioPermit, claim(0).attempt_id) is None


def test_unregistered_launch_window_is_not_reclaimed(isolated):
    factory, _ = isolated
    with factory.begin() as db:
        db.add(MechanicalAudioPermit(attempt_id=claim(0).attempt_id, task_id=claim(0).task_id,
                                     process={"owner": {}, "children": [], "spawning": True}))
    assert not audio.capacity_available()


def test_expired_attempt_cannot_launch_a_new_child(isolated):
    factory, _ = isolated
    current = claim(0)
    with store.host_lock(), factory.begin() as db:
        assert audio.reserve(db, current)
        db.get(TaskAttempt, current.attempt_id).lease_expires_at = utcnow() - timedelta(seconds=1)
    token = audio.bind_claim(current)
    try:
        with pytest.raises(Exception, match="音频任务执行许可已失效"):
            audio.spawn_registered([sys.executable, "-c", "raise SystemExit(0)"])
    finally:
        assert audio.release(current)
        audio.reset_claim(token)


def test_nested_host_lock_is_reentrant_and_released_on_exception(isolated):
    with pytest.raises(ValueError):
        with store.host_lock(), store.host_lock():
            raise ValueError("test")
    with store.host_lock():
        assert audio.capacity_available()


@pytest.mark.parametrize('failure', ['timeout', 'cancel', 'database'])
def test_registered_probe_stops_and_finishes_child_on_failure(isolated, monkeypatch, failure):
    import subprocess
    from backend.platform import task_context
    from backend.core.task_control import TaskCancelled
    factory, _ = isolated
    current = claim(0)
    with store.host_lock(), factory.begin() as db:
        assert audio.reserve(db, current)
    children = []
    spawn = audio.spawn_registered
    def record(*args, **kwargs):
        child = spawn(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(audio, 'spawn_registered', record)
    monkeypatch.setattr(task_context.EngineExecutionContext, 'check', lambda self: None)
    def check(self, on_pause):
        if failure == 'cancel':
            raise TaskCancelled()
        if failure == 'database':
            raise RuntimeError('database unavailable')
    monkeypatch.setattr(task_context.EngineExecutionContext, 'check_interruptible', check)
    error = {'timeout': subprocess.TimeoutExpired, 'cancel': TaskCancelled, 'database': RuntimeError}[failure]
    token = audio.bind_claim(current)
    try:
        with pytest.raises(error):
            audio.run_registered([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 timeout=.1, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert len(children) == 1 and children[0].poll() is not None
        with factory() as db:
            assert all(child['finished'] for child in db.get(MechanicalAudioPermit, current.attempt_id).process['children'])
        assert audio.release(current)
    finally:
        audio.reset_claim(token)


def test_registered_probe_pause_stops_before_release_and_restarts(isolated, monkeypatch):
    import subprocess
    from backend.platform import task_context
    factory, _ = isolated
    current = claim(0)
    with store.host_lock(), factory.begin() as db:
        assert audio.reserve(db, current)
    children = []
    spawn = audio.spawn_registered
    def record(*args, **kwargs):
        child = spawn(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(audio, 'spawn_registered', record)
    monkeypatch.setattr(task_context.EngineExecutionContext, 'check', lambda self: None)
    def pause_once(self, on_pause):
        if len(children) == 1:
            on_pause()
            assert children[0].poll() is not None
            assert audio.release(current)
            with store.host_lock(), factory.begin() as db:
                assert audio.reserve(db, current)
    monkeypatch.setattr(task_context.EngineExecutionContext, 'check_interruptible', pause_once)
    token = audio.bind_claim(current)
    try:
        result = audio.run_registered([sys.executable, '-c', 'import time; time.sleep(.1); print(2.5)'],
                                      timeout=2, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert result.returncode == 0 and result.stdout.strip() == b'2.5'
        assert len(children) == 2 and all(child.poll() is not None for child in children)
        assert audio.release(current)
    finally:
        audio.reset_claim(token)


def test_orphaned_spawning_permit_is_reclaimed_after_the_window(isolated, monkeypatch):
    factory, _ = isolated
    dead = {"pid": 2 ** 22 + 1, "create_time": 1.0}
    with factory.begin() as db:
        db.add(MechanicalAudioPermit(attempt_id="attempt-0", task_id="0", process={
            "owner": dead, "children": [], "spawning": True, "spawning_at": 0}))
    monkeypatch.setattr(audio, "identity_alive", lambda identity: False)
    # Inside the window the launch is unresolved and blocks admission.
    with factory.begin() as db:
        db.get(MechanicalAudioPermit, "attempt-0").process = {
            "owner": dead, "children": [], "spawning": True, "spawning_at": audio.time.time()}
    assert audio.capacity_available() is False
    with factory.begin() as db:
        assert db.get(MechanicalAudioState, "local").error
        assert db.scalar(select(func.count()).select_from(MechanicalAudioPermit)) == 1
    # Past the window it is reaped instead of blocking forever.
    with factory.begin() as db:
        db.get(MechanicalAudioPermit, "attempt-0").process = {
            "owner": dead, "children": [], "spawning": True,
            "spawning_at": audio.time.time() - audio.SPAWN_ORPHAN_SECONDS - 1}
    assert audio.capacity_available() is True
    with factory.begin() as db:
        assert db.scalar(select(func.count()).select_from(MechanicalAudioPermit)) == 0
        assert not db.get(MechanicalAudioState, "local").error


def test_claim_capacity_check_is_skipped_when_no_audio_task_is_claimable(monkeypatch):
    from backend.platform import task_worker
    calls = []
    monkeypatch.setattr(audio, "capacity_available", lambda: calls.append(1) or True)
    assert task_worker._audio_capacity_for(("script.parse",), ()) is True
    assert task_worker._audio_capacity_for(None, audio.AUDIO_TASK_TYPES) is True
    assert calls == []
    assert task_worker._audio_capacity_for(None, ()) is True
    assert task_worker._audio_capacity_for(("tts.merge",), ()) is True
    assert calls == [1, 1]
