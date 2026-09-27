"""Contracts shared by the legacy engines and the durable task boundary."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timezone
from types import SimpleNamespace

import sqlalchemy as sa
import pytest
from sqlalchemy.orm import Session

from backend.services import task_views
from backend.core import config as core_config
from backend.platform import quota, task_context, task_worker
from backend.platform.engine_task_executor import execute_engine_task
from backend.platform.task_contracts import TaskClaim, TaskExecutionError
from sqlalchemy.dialects import postgresql
from backend.services import task_operations
from backend.services import task_operations as task_service
from backend.platform.database import Base
from backend.platform.models import TaskEvent, User
from backend.platform.security import create_session, revoke_session, session_is_valid_for_user


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
    monkeypatch.setattr(task_context, "update_progress", lambda _claim, value, label: observed.append((value, label)))
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
            TaskEvent(task_id=task_id, sequence=3, event_type="segments", payload={"done": 2, "total": 4, "chars_done": 10, "chars_total": 20}),
            *(TaskEvent(task_id=task_id, sequence=i, event_type="log", payload={"msg": str(i)}) for i in range(5, 1005)),
        ])
        db.commit()
        task = SimpleNamespace(
            id=task_id, payload={}, task_type="tts.batch", status="running", progress=90,
            created_at=datetime.now(timezone.utc), started_at=None, finished_at=None,
            result=None, error_message="",
        )
        snapshot = task_views.task_snapshot(db, task)
    engine.dispose()
    assert snapshot["phase"] == "rendering"
    assert snapshot["current"] == "part 1"
    assert snapshot["logs"][-1]["msg"] == "1004"
    assert len(snapshot["logs"]) == 1000
    assert snapshot["seg_done"] == 2


def test_cancel_transition_is_idempotent_and_finishes_unclaimed_task(monkeypatch):
    events = []
    monkeypatch.setattr(task_service, "append_task_event", lambda _db, _id, kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(task_service, "suppress_pending_dispatch", lambda *_args, **_kwargs: None)
    task = SimpleNamespace(
        id="task-1", status="pending", finished_at=None, updated_at=None,
    )

    assert task_service.cancel_task_record(object(), task) is True
    assert task.status == "cancelled"
    assert task.finished_at is not None
    assert events == [("cancel_requested", {"status": "cancelled"})]
    assert task_service.cancel_task_record(object(), task) is False
    assert len(events) == 1


def test_cancel_running_task_requests_cooperative_stop_once(monkeypatch):
    events = []
    monkeypatch.setattr(task_service, "append_task_event", lambda _db, _id, kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(task_service, "suppress_pending_dispatch", lambda *_args, **_kwargs: None)
    task = SimpleNamespace(
        id="task-running", status="running", finished_at=None, updated_at=None,
    )

    assert task_service.cancel_task_record(object(), task) is True
    assert task.status == "cancelling"
    assert task.finished_at is None
    assert task_service.cancel_task_record(object(), task) is False
    assert events == [("cancel_requested", {"status": "cancelling"})]


def test_cancel_queued_task_cancels_immediately_once(monkeypatch):
    events = []
    monkeypatch.setattr(task_service, "append_task_event", lambda _db, _id, kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(task_service, "suppress_pending_dispatch", lambda *_args, **_kwargs: None)
    task = SimpleNamespace(id="task-queued", status="queued", finished_at=None, updated_at=None)

    assert task_service.cancel_task_record(object(), task) is True
    assert task_service.cancel_task_record(object(), task) is False
    assert task.status == "cancelled"
    assert task.finished_at is not None
    assert events == [("cancel_requested", {"status": "cancelled"})]


def test_lifecycle_control_events_emit_authoritative_status_snapshot(monkeypatch):
    monkeypatch.setattr("backend.services.task_views.task_snapshot", lambda _db, task: {"status": task.status})
    for event_type, status in (
        ("retry_requested", "pending"),
        ("retry_scheduled", "pending"),
        ("attempt_expired", "pending"),
        ("dispatch_recovered", "pending"),
        ("attempt_started", "running"),
    ):
        task = SimpleNamespace(id="task", status=status)
        event = SimpleNamespace(event_type=event_type, payload={"status": status})
        mapped = task_views.event_frame(object(), task, event)
        assert mapped == {"type": "status", "status": status, "task_id": "task", "task": {"status": status}}


def test_worker_rejects_unsafe_bgm_paths_from_preexisting_tasks():
    for task_type, payload in (
        ("bgm.segment", {"stem": r"folder\outside"}),
        ("bgm.mix", {"stem": ".."}),
        ("bgm.match", {"chapters": ["safe", "../outside"]}),
        ("bgm.package", {"chapters": ["../outside"]}),
    ):
        claim = TaskClaim(
            task_id="task", attempt_id="attempt", attempt_no=1, lease_token="lease",
            worker_id="worker", owner_id="owner", project_id="project",
            task_type=task_type, payload=payload,
        )
        with pytest.raises(TaskExecutionError, match="BGM"):
            execute_engine_task(claim)


def test_bgm_match_executor_normalizes_legacy_llm_mode(monkeypatch):
    # 升级前提交、升级后执行的在途任务 payload 可能带退役的 "llm" mode：
    # executor 分支在执行前归一为 random（不进无标签评分分支、不回写退役值）。
    from backend.platform import engine_task_executor as ete

    seen = {}

    def fake_match_stems(layout, stems, mode, min_score, handle=None):
        seen["mode"] = mode
        return {"mode": mode, "matched": 0, "no_bgm": 0, "skipped_locked": 0}

    monkeypatch.setattr("backend.engines.bgm.match_stems", fake_match_stems)
    monkeypatch.setattr(ete, "get_or_prepare_layout", lambda: object())
    monkeypatch.setattr(
        ete.core_config, "get_config",
        lambda: SimpleNamespace(bgm=SimpleNamespace(min_match_score=1)),
    )
    ete._run_bgm_match(
        object(), None, {"chapters": ["ch1"], "mode": "llm"}, {}, {},
    )
    assert seen["mode"] == "random"


def test_task_read_queries_do_not_lock_but_controls_can(monkeypatch):
    task = SimpleNamespace(id="task")

    class CaptureSession:
        statement = None

        def scalar(self, statement):
            self.statement = statement
            return task

    read_session = CaptureSession()
    assert task_operations.owned_task(read_session, "owner", "task") is task
    assert "FOR UPDATE" not in str(read_session.statement.compile(dialect=postgresql.dialect()))

    write_session = CaptureSession()
    assert task_operations.owned_task(write_session, "owner", "task", lock=True) is task
    assert "FOR UPDATE" in str(write_session.statement.compile(dialect=postgresql.dialect()))


def test_open_stream_session_revalidation_observes_revocation():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            email="stream@example.com", username="stream-user", password_hash="test-hash",
        )
        db.add(user)
        db.flush()
        token, _csrf, session = create_session(db, user)
        db.flush()
        original_last_seen = session.last_seen_at
        assert session_is_valid_for_user(db, token, user.id) is True
        assert session.last_seen_at == original_last_seen
        revoke_session(session)
        db.flush()
        assert session_is_valid_for_user(db, token, user.id) is False
    engine.dispose()
