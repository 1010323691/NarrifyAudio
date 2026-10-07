"""Batch requests create independent durable rows in one transaction."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from backend.platform import engine_task_submission as submission
from backend.platform.database import SessionLocal
from backend.platform.models import OutboxEvent, Project, Task, TaskEvent, User, UserQuotaAccount


@pytest.fixture
def owner():
    suffix = uuid4().hex
    with SessionLocal() as db:
        user = User(username=f"batch-{suffix}", email=f"{suffix}@example.test", password_hash="test")
        db.add(user)
        db.flush()
        project = Project(owner_id=user.id, name="Batch", directory_key=f"{user.username}/book")
        db.add(project)
        db.flush()
        db.add(UserQuotaAccount(user_id=user.id, available_units=10000))
        db.commit()
        yield user.id, project.id
    with SessionLocal.begin() as db:
        ids = select(Task.id).where(Task.owner_id == user.id)
        db.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_id.in_(ids)))
        db.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(ids)))
        db.execute(delete(Task).where(Task.owner_id == user.id))
        db.execute(delete(UserQuotaAccount).where(UserQuotaAccount.user_id == user.id))
        db.execute(delete(Project).where(Project.owner_id == user.id))
        db.execute(delete(User).where(User.id == user.id))


def submit(db, owner, entries):
    user_id, project_id = owner
    ctx = SimpleNamespace(user=db.get(User, user_id), session=SimpleNamespace(active_project_id=project_id))
    return submission.submit_legacy_engine_tasks(
        task_type="tts.batch", entries=entries, ctx=ctx, db=db, idempotency_prefix="test-batch",
    )


def entry(script):
    return {"label": f"音频合成 · {script}", "payload": {"scripts": [script], "script": script}}


def test_batch_creates_independent_rows_and_outbox_events(owner):
    with SessionLocal() as db:
        result = submit(db, owner, [entry("one.json"), entry("two.json")])
        assert len(set(result["task_ids"])) == 2
        assert "task_id" not in result
        rows = db.scalars(select(Task).where(Task.id.in_(result["task_ids"]))).all()
        assert {tuple(row.payload["scripts"]) for row in rows} == {("one.json",), ("two.json",)}
        assert all(row.status == "pending" for row in rows)
        assert len(db.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id.in_(result["task_ids"]))).all()) == 2


def test_invalid_later_entry_rolls_back_the_whole_batch(owner):
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            submit(db, owner, [entry("one.json"), entry("../escape.json")])
        assert exc.value.status_code == 422
        assert not db.scalars(select(Task).where(Task.owner_id == owner[0])).all()
        assert not db.scalars(select(OutboxEvent).join(Task, OutboxEvent.aggregate_id == Task.id).where(Task.owner_id == owner[0])).all()


def test_single_entry_retains_compatible_id_and_empty_batch_creates_nothing(owner):
    with SessionLocal() as db:
        result = submit(db, owner, [entry("one.json")])
        assert result["task_ids"] == [result["task_id"]]
        assert submit(db, owner, []) == {"task_ids": []}


def test_clone_entries_keep_independent_records_and_share_only_their_execution_batch(owner):
    with SessionLocal() as db:
        user_id, project_id = owner
        ctx = SimpleNamespace(user=db.get(User, user_id), session=SimpleNamespace(active_project_id=project_id))
        batches = []
        for names in (["A", "B"], ["C"]):
            result = submission.submit_legacy_engine_tasks(
                task_type="voices.clone", ctx=ctx, db=db, idempotency_prefix="voices-clone",
                entries=[{"label": f"克隆音频 · {name}", "payload": {"speakers": [name], "script": "book.json"}}
                         for name in names],
            )
            rows = db.scalars(select(Task).where(Task.id.in_(result["task_ids"]))).all()
            assert len(rows) == len(names)
            assert {row.payload["speakers"][0] for row in rows} == set(names)
            assert all(row.status == "pending" for row in rows)
            batch_ids = {row.payload["execution_batch"] for row in rows}
            assert len(batch_ids) == 1
            batches.append(batch_ids.pop())
        assert batches[0] != batches[1]
