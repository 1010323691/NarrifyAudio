"""Real row-lock regression for concurrent cancellation (opt-in PostgreSQL test)."""
from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from backend.platform.database import Base
from backend.platform.models import (
    Project,
    QuotaReservation,
    QuotaTransaction,
    Task,
    TaskEvent,
    User,
    UserQuotaAccount,
)
from backend.services.tasks import cancel_task_record


@pytest.mark.skipif(
    not os.environ.get("NARRIFY_TEST_POSTGRES_URL"),
    reason="set NARRIFY_TEST_POSTGRES_URL to run the PostgreSQL row-lock regression",
)
def test_concurrent_cancel_releases_a_reservation_once():
    from sqlalchemy import create_engine

    url = os.environ["NARRIFY_TEST_POSTGRES_URL"]
    engine = create_engine(url, pool_size=5, max_overflow=0)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("NARRIFY_TEST_POSTGRES_URL must use PostgreSQL")

    schema = f"narrify_cancel_{uuid.uuid4().hex}"
    tenant_engine = None
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        tenant_engine = engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(tenant_engine)
        sessions = sessionmaker(bind=tenant_engine, class_=Session, expire_on_commit=False)

        owner_id = str(uuid.uuid4())
        project_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        reservation_id = str(uuid.uuid4())
        with sessions.begin() as db:
            db.add(User(
                id=owner_id,
                email=f"{owner_id}@example.test",
                username=f"u{owner_id.replace('-', '')[:20]}",
                password_hash="test-hash",
            ))
            db.flush()
            db.add(Project(
                id=project_id,
                owner_id=owner_id,
                name="Concurrent cancellation",
                directory_key=f"{owner_id}/{project_id}",
            ))
            db.add(UserQuotaAccount(
                user_id=owner_id, available_units=0, reserved_units=2,
                frozen_units=0, consumed_units=0,
            ))
            db.flush()
            db.add(Task(
                id=task_id,
                owner_id=owner_id,
                project_id=project_id,
                task_type="script.parse",
                status="queued",
                payload={},
                idempotency_key=f"cancel-{task_id}",
            ))
            db.flush()
            db.add(QuotaReservation(
                id=reservation_id,
                user_id=owner_id,
                task_id=task_id,
                units=2,
                status="reserved",
            ))

        first_locked = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_pid: list[int] = []
        outcomes: dict[str, bool] = {}
        errors: list[BaseException] = []

        def cancel(name: str) -> None:
            try:
                with sessions() as db:
                    if name == "first":
                        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
                        first_locked.set()
                        if not release_first.wait(10):
                            raise TimeoutError("test did not release the first transaction")
                    else:
                        second_pid.append(int(db.scalar(text("SELECT pg_backend_pid()"))))
                        second_started.set()
                        task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
                    outcomes[name] = cancel_task_record(db, task)
                    db.commit()
            except BaseException as exc:  # forward worker-thread failures to pytest
                errors.append(exc)

        first = threading.Thread(target=cancel, args=("first",), daemon=True)
        second = threading.Thread(target=cancel, args=("second",), daemon=True)
        first.start()
        assert first_locked.wait(5), "first session did not acquire the task row lock"
        second.start()
        assert second_started.wait(5), "second session did not start its locking query"

        blocked = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not blocked:
            with engine.connect() as connection:
                blockers = connection.scalar(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": second_pid[0]},
                )
            blocked = bool(blockers)
            if not blocked:
                time.sleep(0.02)
        try:
            assert blocked, "second cancellation was not observed waiting on the row lock"
        finally:
            release_first.set()
            first.join(10)
            second.join(10)
        assert not first.is_alive() and not second.is_alive(), "cancellation threads did not finish"
        assert not errors, f"concurrent cancellation raised: {errors!r}"
        assert outcomes == {"first": True, "second": False}

        with sessions() as db:
            task = db.get(Task, task_id)
            reservation = db.get(QuotaReservation, reservation_id)
            account = db.get(UserQuotaAccount, owner_id)
            releases = db.scalar(select(func.count()).select_from(QuotaTransaction).where(
                QuotaTransaction.task_id == task_id,
                QuotaTransaction.kind == "release",
            ))
            cancel_events = db.scalar(select(func.count()).select_from(TaskEvent).where(
                TaskEvent.task_id == task_id,
                TaskEvent.event_type == "cancel_requested",
            ))
            assert task.status == "cancelled"
            assert reservation.status == "released"
            assert (account.available_units, account.reserved_units) == (2, 0)
            assert releases == 1
            assert cancel_events == 1
    finally:
        if tenant_engine is not None:
            Base.metadata.drop_all(tenant_engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()
