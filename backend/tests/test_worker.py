import threading
from queue import Empty, Queue

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.platform.database import Base
from backend.platform.models import Task, TaskAttempt
from backend import worker
from backend.core.concurrency import ConcurrencyGate
from backend.worker import PARSE_WORKER_MAX, parse_worker_slot_count


def test_merge_workers_execute_multiple_jobs_concurrently(monkeypatch):
    gate = ConcurrencyGate()
    gate.set_limit(4)
    monkeypatch.setattr(worker, "merge_gate", lambda: gate)
    pending = Queue()
    for task in range(8):
        pending.put(task)
    stop = threading.Event()
    overlap = threading.Barrier(4)
    executed = []
    errors = []
    offline = []

    def claim(slot_id, *, task_types):
        assert task_types == ("tts.merge", "bgm.mix")
        try:
            return pending.get_nowait()
        except Empty:
            return None

    def execute(task):
        try:
            overlap.wait(timeout=5)
            executed.append(task)
            if len(executed) == 8:
                stop.set()
        except Exception as exc:
            errors.append(exc)
            stop.set()

    monkeypatch.setattr(worker, "claim_fair_task", claim)
    monkeypatch.setattr(worker, "_run_claim_fenced", execute)
    monkeypatch.setattr(worker, "heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "mark_offline", offline.append)
    threads = worker._start_merge_workers("worker-test", stop)
    try:
        for thread in threads:
            thread.join(timeout=10)
        assert not errors
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(executed) == list(range(8))
        assert sorted(offline) == [f"worker-test-merge-{slot:02d}" for slot in range(1, 5)]
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)


def test_parse_worker_slots_double_llm_concurrency():
    assert parse_worker_slot_count(1) == 2
    assert parse_worker_slot_count(4) == 8
    assert parse_worker_slot_count(16) == 32


def test_parse_worker_slots_respect_hard_maximum():
    assert parse_worker_slot_count(32) == PARSE_WORKER_MAX == 64


def test_paused_parse_attempts_get_replacement_worker_slots():
    assert parse_worker_slot_count(4, parked_worker_count=8) == 16
    assert parse_worker_slot_count(4, parked_worker_count=0) == 8
    assert parse_worker_slot_count(32, parked_worker_count=8) == PARSE_WORKER_MAX


def test_paused_parse_worker_count_is_scoped_to_this_process(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as db:
        for task_id, owner_worker, status, error_code in (
            ("paused-local", "worker-a-parse-01", "paused", "manual_pause"),
            ("paused-other", "worker-b-parse-01", "paused", "manual_pause"),
            ("paused-system", "worker-a-parse-02", "paused", "llm_unavailable"),
        ):
            db.add(Task(
                id=task_id, owner_id="owner", project_id="project", task_type="script.parse",
                status=status, error_code=error_code,
            ))
            db.add(TaskAttempt(
                id=f"attempt-{task_id}", task_id=task_id, attempt_no=1,
                worker_id=owner_worker, lease_token="lease", status="running",
            ))
        db.commit()
    monkeypatch.setattr(worker, "SessionLocal", factory)
    assert worker._paused_parse_worker_count("worker-a") == 1
