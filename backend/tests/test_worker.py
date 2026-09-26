from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.platform.database import Base
from backend.platform.models import Task, TaskAttempt
from backend import worker
from backend.worker import PARSE_WORKER_MAX, parse_worker_slot_count


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
