import threading
from queue import Empty, Queue
from types import SimpleNamespace

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError, ProgrammingError, TimeoutError as PoolTimeoutError

from backend.platform.database import Base
from backend.platform.models import Task, TaskAttempt
from backend import worker
from backend.core.concurrency import ConcurrencyGate
from backend.worker import PARSE_WORKER_MAX, parse_worker_slot_count


class RecordingStop:
    def __init__(self):
        self.delays = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, delay):
        self.delays.append(delay)
        return self.stopped


def test_dispatch_recovers_from_heartbeat_and_claim_connection_errors(monkeypatch):
    stop = RecordingStop()
    counts = {"heartbeat": 0, "claim": 0}

    def heartbeat(*args, **kwargs):
        counts["heartbeat"] += 1
        if counts["heartbeat"] == 1:
            raise OperationalError("heartbeat", {}, SimpleNamespace(sqlstate="53300"))

    def run_once(*args, **kwargs):
        counts["claim"] += 1
        if counts["claim"] == 1:
            raise PoolTimeoutError("connection pool exhausted")
        stop.stopped = True
        return "succeeded"

    monkeypatch.setattr(worker, "heartbeat", heartbeat)
    monkeypatch.setattr(worker, "recover_database_tasks", lambda: None)
    monkeypatch.setattr(worker, "publish_pending", lambda: None)
    monkeypatch.setattr(worker, "run_once", run_once)
    monkeypatch.setattr(worker, "load_gpu_config", lambda: SimpleNamespace(enabled=False))
    monkeypatch.setattr(worker, "read_gpu_state", lambda: {"managed": False})
    worker._dispatch_loop(None, "recover-worker", {}, stop, interval=.01, once=False)
    assert counts == {"heartbeat": 4, "claim": 2}
    assert stop.delays[:2] == [1, 2]


def test_database_retry_backoff_is_bounded_and_cancellable():
    stop = RecordingStop()

    def disconnected():
        if len(stop.delays) == 7:
            stop.stopped = True
        raise OperationalError("connect", {}, ConnectionError("disconnected"))

    assert not worker._retry_database_operation(disconnected, stop)
    assert stop.delays == [1, 2, 4, 8, 16, 30, 30, 30]


@pytest.mark.parametrize("once,error", [
    (True, OperationalError("connect", {}, ConnectionError("disconnected"))),
    (False, OperationalError("connect", {}, SimpleNamespace(sqlstate="28P01"))),
    (False, ProgrammingError("query", {}, ValueError("invalid SQL"))),
])
def test_once_and_non_transient_failures_are_not_retried(once, error):
    stop = RecordingStop()

    def fail():
        raise error

    with pytest.raises(type(error)) as caught:
        worker._retry_database_operation(fail, stop, once=once)
    assert caught.value is error
    assert not stop.delays


def test_failed_error_and_offline_heartbeats_do_not_mask_original_exception(monkeypatch):
    error = RuntimeError("original dispatch failure")

    def registry_failure(*args, **kwargs):
        raise OperationalError("registry", {}, ConnectionError("disconnected"))

    def dispatch_failure(*args, **kwargs):
        raise error

    monkeypatch.setattr(worker, "_retry_database_operation", lambda *args, **kwargs: True)
    monkeypatch.setattr(worker, "heartbeat", registry_failure)
    monkeypatch.setattr(worker, "mark_offline", registry_failure)
    monkeypatch.setattr(worker, "_dispatch_loop", dispatch_failure)
    monkeypatch.setattr("sys.argv", ["worker", "--once"])
    with pytest.raises(RuntimeError) as caught:
        worker.main()
    assert caught.value is error


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
