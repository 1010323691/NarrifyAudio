"""Contracts shared by legacy engines and the durable task adapter."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timezone
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.api.tasks import _durable_snapshot
from backend.core import config as core_config
from backend.platform import quota, task_worker
from backend.platform.database import Base
from backend.platform.models import TaskEvent


def test_task_config_snapshot_bypasses_live_admin_overlay(monkeypatch):
    snapshot = core_config.AppConfig()
    snapshot.llm.model_name = "submitted-model"
    monkeypatch.setattr(core_config, "_platform_config", lambda: {"llm": {"model_name": "new-admin-model"}})
    token = core_config.bind_task_config(snapshot)
    try:
        assert core_config.get_config().llm.model_name == "submitted-model"
    finally:
        core_config.reset_task_config(token)


def test_persistent_handle_distinguishes_fraction_and_percent(monkeypatch):
    observed = []
    monkeypatch.setattr(task_worker, "update_progress", lambda _claim, value, label: observed.append((value, label)))
    handle = task_worker.PersistentTaskHandle(object())
    handle.progress(0.25, "engine")
    handle.progress_percent(5, "adapter")
    assert observed == [(25, "engine"), (5, "adapter")]


def test_parallel_llm_operations_keep_context_and_distinct_charge_keys(monkeypatch):
    charged = []
    monkeypatch.setattr(quota, "require_quota", lambda *_args: None)

    def capture(_resource, _operation, _chars, *, idempotency_key):
        charged.append(idempotency_key)
        return True

    monkeypatch.setattr(quota, "consume_quota", capture)
    token = quota.set_quota_context("user-1", "task-1", "attempt-1")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(copy_context().run, quota.consume_llm_output, "accepted", "voices.foundation", operation_key=f"voices.foundation:{speaker}")
                for speaker in ("A", "B")
            ]
            assert all(future.result() for future in futures)
    finally:
        quota.reset_quota_context(token)
    assert set(charged) == {
        "task-1:llm:voices.foundation:A",
        "task-1:llm:voices.foundation:B",
    }


def test_reconnected_snapshot_uses_latest_events_and_preserves_phase():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    task_id = "snapshot-task"
    with Session(engine) as db:
        db.add_all([
            TaskEvent(task_id=task_id, sequence=1, event_type="phase", payload={"phase": "rendering"}),
            TaskEvent(task_id=task_id, sequence=2, event_type="progress", payload={"current": "part 1"}),
            TaskEvent(task_id=task_id, sequence=3, event_type="llm_chars", payload={"chars": 50, "secs": 2.5}),
            TaskEvent(task_id=task_id, sequence=4, event_type="segments", payload={"done": 2, "total": 4, "chars_done": 10, "chars_total": 20}),
            *(TaskEvent(task_id=task_id, sequence=i, event_type="log", payload={"msg": str(i)}) for i in range(5, 1005)),
        ])
        db.commit()
        task = SimpleNamespace(
            id=task_id, payload={}, task_type="tts.batch", status="running", progress=90,
            created_at=datetime.now(timezone.utc), started_at=None, finished_at=None,
            result=None, error_message="",
        )
        snapshot = _durable_snapshot(db, task)
    engine.dispose()
    assert snapshot["phase"] == "rendering"
    assert snapshot["current"] == "part 1"
    assert snapshot["logs"][-1]["msg"] == "1004"
    assert len(snapshot["logs"]) == 1000
    assert snapshot["llm_chars"] == 50
    assert snapshot["seg_done"] == 2
