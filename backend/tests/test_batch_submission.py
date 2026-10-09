"""Atomic non-TTS batch admission, real statement/transaction budgets and replays."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from backend.platform.database import Base
from backend.platform.models import OutboxEvent, Project, ProjectFile, Task, TaskBatch, TaskEvent, User, UserQuotaAccount
from backend.platform import batch_submission as submission
from backend.platform.task_submission import TaskSubmissionError


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'batch.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        db.add(User(id="owner", username="batch", email="batch@example.test", password_hash="unused"))
        db.flush()
        db.add(Project(id="project", owner_id="owner", name="Book", directory_key="batch/book"))
        db.add(UserQuotaAccount(user_id="owner", available_units=10000))
        db.flush()
        db.add_all([ProjectFile(id=f"file-{i}", owner_id="owner", project_id="project", kind="legacy",
            object_key=f"batch/book/02_split_text/chapter-{i}.txt", original_name=f"chapter-{i}.txt",
            content_type="text/plain", size_bytes=1, sha256="a" * 64) for i in range(1000)])
    monkeypatch.setattr(submission, "lock_storage_migration", lambda *a, **k: True)
    monkeypatch.setattr(submission, "storage_migration", lambda db: None)
    yield factory
    engine.dispose()


def entries(kind, count=2):
    result = []
    for i in range(count):
        payload = ({"input_file_id": f"file-{i}", "source_name": f"chapter-{i}.txt", "input_sha256": "a" * 64}
                   if kind == "script.parse" else
                   {"speakers": [f"role-{i}"], "script": "book.json"} if kind.startswith("voices.") else
                   {"chapters": [f"chapter-{i}"], "mode": "random"} if kind == "bgm.match" else {"stem": f"chapter-{i}"})
        result.append({"label": str(i), "payload": payload, "receipt": {"name": str(i)}})
    return result


def submit(factory, kind="voices.foundation", count=2, key="key", prepare=None, request=None):
    with factory() as db:
        return submission.submit_task_batch(db=db, user=db.get(User, "owner"), project_id="project", task_type=kind,
            request=request or {"count": count}, prepare=prepare or (lambda: (entries(kind, count), {"generation": {"max_concurrency": 2}})),
            idempotency_key=key, receipt_field="files")


def counts(factory):
    with factory() as db:
        return [db.scalar(select(func.count()).select_from(model)) for model in (Task, TaskBatch, TaskEvent, OutboxEvent)]


@pytest.mark.parametrize("kind", sorted(submission.BATCH_TYPES))
def test_1000_tasks_use_at_most_80_statements_one_commit_and_one_wake(sessions, kind):
    statements, commits, preparations = [], [], []
    engine = sessions.kw["bind"]
    listen = lambda c, cur, statement, *a: statements.append(statement)
    committed = lambda db: commits.append(True)
    event.listen(engine, "before_cursor_execute", listen)
    event.listen(sessions.class_, "after_commit", committed)
    try:
        receipt = submit(sessions, kind, 1000, prepare=lambda: preparations.append(True) or (entries(kind, 1000), {"llm": {"model_name": "submitted"}}))
    finally:
        event.remove(engine, "before_cursor_execute", listen)
        event.remove(sessions.class_, "after_commit", committed)
    assert len(statements) <= 80, len(statements)
    print(f"{kind}: tasks=1000, statements={len(statements)}, commits={len(commits)}, preparations={len(preparations)}")
    assert len(commits) == len(preparations) == 1
    assert counts(sessions) == [1000, 1, 1000, 1]
    assert len(receipt["task_ids"]) == len(receipt["files"]) == 1000
    with sessions() as db:
        tasks = db.scalars(select(Task)).all()
        assert all("config" not in task.payload and task.payload["_batch_config_id"] == receipt["batch_id"] for task in tasks)
        assert all(task.event_sequence == 1 for task in tasks)
        if kind == "voices.clone":
            assert {task.payload["execution_batch"] for task in tasks} == {receipt["batch_id"]}


def test_replay_returns_original_receipt_before_mutable_preflight_or_quota(sessions):
    first = submit(sessions)
    with sessions.begin() as db:
        db.get(UserQuotaAccount, "owner").available_units = 0
    assert submit(sessions, prepare=lambda: pytest.fail("replay must not repeat mutable preflight")) == first
    assert counts(sessions) == [2, 1, 2, 1]
    with pytest.raises(TaskSubmissionError) as error:
        submit(sessions, request={"count": 3})
    assert error.value.status_code == 409


def test_late_insert_failure_rolls_back_receipt_tasks_events_and_catalog(sessions):
    calls = []
    def fail(c, cur, statement, *args):
        if statement.lower().startswith("insert into tasks "):
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("injected second chunk failure")
    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with sessions() as db:
            def prepare():
                db.add(ProjectFile(id="new-catalog", owner_id="owner", project_id="project", kind="legacy",
                    object_key="batch/book/02_split_text/new.txt", original_name="new.txt",
                    content_type="text/plain", size_bytes=1, sha256="a" * 64))
                return entries("voices.foundation", 101), {}
            with pytest.raises(RuntimeError):
                submission.submit_task_batch(db=db, user=db.get(User, "owner"), project_id="project",
                    task_type="voices.foundation", request={"count": 101}, prepare=prepare, idempotency_key="key")
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert counts(sessions) == [0, 0, 0, 0]
    with sessions() as db:
        assert db.get(ProjectFile, "new-catalog") is None
    assert len(submit(sessions)["task_ids"]) == 2


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_replays_or_overlap_create_one_atomic_batch(sessions, same_key):
    barrier = Barrier(2)
    def worker(index):
        barrier.wait()
        try:
            return submit(sessions, key="same" if same_key else f"key-{index}")
        except TaskSubmissionError as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert counts(sessions) == [2, 1, 2, 1]
    if same_key:
        assert results[0] == results[1]
    else:
        assert sum(isinstance(result, dict) for result in results) == 1
        assert 409 in results


def test_changed_parse_input_duplicate_target_and_oversize_leave_no_partial_batch(sessions):
    for prepared, status in [(entries("script.parse") + [entries("script.parse")[0]], 422),
                             (entries("voices.foundation", 1001), 422)]:
        with pytest.raises(TaskSubmissionError) as error:
            submit(sessions, kind="script.parse" if status == 422 and len(prepared) == 3 else "voices.foundation",
                prepare=lambda: (prepared, {}))
        assert error.value.status_code == status
    prepared = entries("script.parse")
    prepared[-1]["payload"]["input_sha256"] = "b" * 64
    with pytest.raises(TaskSubmissionError) as error:
        submit(sessions, kind="script.parse", prepare=lambda: (prepared, {}))
    assert error.value.status_code == 409
    assert counts(sessions) == [0, 0, 0, 0]


def test_bgm_cross_type_overlap_is_atomic(sessions):
    submit(sessions, "bgm.match")
    with pytest.raises(TaskSubmissionError) as error:
        submit(sessions, "bgm.mix", key="mix")
    assert error.value.status_code == 409
    assert counts(sessions) == [2, 1, 2, 1]


def test_worker_uses_batch_configuration_and_rejects_changed_parse_input(sessions, tmp_path, monkeypatch):
    from backend.platform import task_engine_support as support, task_worker as worker
    from backend.platform.task_contracts import TaskClaim, TaskExecutionError
    from backend.platform.storage import sha256_file
    from backend.core import config
    first = submit(sessions, kind="script.parse", count=1,
        prepare=lambda: (entries("script.parse", 1), {"llm": {"model_name": "submitted-model"}}))
    monkeypatch.setattr(support, "SessionLocal", sessions)
    monkeypatch.setattr(support, "project_workspace_path", lambda *args: tmp_path)
    with sessions() as db:
        payload = db.get(Task, first["task_id"]).payload
    claim = TaskClaim(first["task_id"], "attempt", 1, "lease", "worker", "owner", "project", "script.parse", payload)
    with support.engine_execution_context(claim):
        assert config.get_config().llm.model_name == "submitted-model"
    path = tmp_path / "input.txt"
    path.write_text("accepted", encoding="utf-8")
    expected = sha256_file(path)
    with sessions.begin() as db:
        db.get(ProjectFile, "file-0").sha256 = expected
    payload = {**payload, "input_sha256": expected}
    from dataclasses import replace
    claim = replace(claim, payload=payload)
    monkeypatch.setattr(worker, "configured_storage_root", lambda db: tmp_path)
    monkeypatch.setattr(worker, "object_path", lambda *args: path)
    with sessions() as db:
        assert worker._input_file(db, claim)[3] == path
        path.write_text("modified externally", encoding="utf-8")
        with pytest.raises(TaskExecutionError) as error:
            worker._input_file(db, claim)
        assert error.value.code == "input_changed"


def test_resume_probe_uses_submitted_batch_model_not_current_workspace(sessions):
    from types import SimpleNamespace
    from backend.platform.task_worker import _resume_llm_config
    first = submit(sessions, prepare=lambda: (entries("voices.foundation"), {"llm": {"base_url": "http://submitted", "model_name": "old-model"}}))
    with sessions() as db:
        task = db.get(Task, first["task_ids"][0])
        batch = db.get(TaskBatch, first["batch_id"])
        row = SimpleNamespace(payload=task.payload, owner_id=task.owner_id, project_id=task.project_id)
        assert _resume_llm_config(row, {"base_url": "http://current"}, {batch.id: batch})["model_name"] == "old-model"
        assert _resume_llm_config(row, {"base_url": "http://current"}, {}) is None


def test_parse_rejects_input_replaced_during_model_work(sessions, tmp_path, monkeypatch):
    from backend.platform import task_worker as worker
    from backend.platform.task_contracts import TaskClaim, TaskExecutionError
    from backend.platform.storage import sha256_file
    path = tmp_path / "input.txt"
    path.write_text("accepted", encoding="utf-8")
    expected = sha256_file(path)
    output = tmp_path / "attempt" / "result.json"
    output.parent.mkdir()
    with sessions.begin() as db:
        db.get(ProjectFile, "file-0").sha256 = expected
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "configured_storage_root", lambda db: tmp_path)
    monkeypatch.setattr(worker, "object_path", lambda *args: path)
    monkeypatch.setattr(worker, "project_workspace_path", lambda *args: tmp_path)
    monkeypatch.setattr(worker, "task_attempt_path", lambda *args: output)
    def parse(*args, **kwargs):
        output.write_text('[{"text":"old"}]', encoding="utf-8")
        path.write_text("replaced during parse", encoding="utf-8")
        return {"count": 1}
    monkeypatch.setattr(worker.script_engine, "parse_script_file", parse)
    claim = TaskClaim("task", "attempt", 1, "lease", "worker", "owner", "project", "script.parse",
        {"input_file_id": "file-0", "input_sha256": expected, "config": {}})
    with pytest.raises(TaskExecutionError) as error:
        worker._execute_script_parse(claim)
    assert error.value.code == "input_changed"
    assert not output.exists()


def test_explicit_project_pins_preparation_and_conflict_helpers_without_changing_ui(sessions, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from backend.platform.engine_task_submission import submit_engine_batch
    from backend.platform.project_context import active_project
    from backend.platform import storage
    from backend.core.request_context import bound_workspace
    with sessions.begin() as db:
        db.add(Project(id="other", owner_id="owner", name="Other", directory_key="batch/other"))
    monkeypatch.setattr(storage, "project_workspace_path", lambda db, username, project_id: tmp_path / project_id)
    with sessions() as db:
        ctx = SimpleNamespace(user=db.get(User, "owner"), session=SimpleNamespace(active_project_id="project"))
        def prepare():
            assert active_project(db, ctx.user, ctx.session).id == "other"
            assert bound_workspace() == tmp_path / "other"
            return entries("bgm.match"), {}
        receipt = submit_engine_batch(task_type="bgm.match", request={"chapters": ["one", "two"]},
            prepare=prepare, ctx=ctx, db=db, project_id="other", idempotency_key="other-project")
        assert ctx.session.active_project_id == "project"
        assert active_project(db, ctx.user, ctx.session).id == "project"
        assert all(task.project_id == "other" for task in db.scalars(select(Task).where(Task.id.in_(receipt["task_ids"]))))


def test_receipt_lookup_preserves_mapping_and_owner_isolation(sessions):
    from fastapi import HTTPException
    from backend.api.platform_tasks import submission_receipt
    first = submit(sessions, kind="script.parse", key="receipt-lookup")
    with sessions() as db:
        restored = submission_receipt("receipt-lookup", db.get(User, "owner"), db)
        assert restored["files"] == first["files"]
        assert restored["task_ids"] == first["task_ids"]
        assert restored["batch_id"] == first["batch_id"]
        assert restored["project_id"] == "project"
        assert restored["statuses"] == dict.fromkeys(first["task_ids"], "pending")
        with pytest.raises(HTTPException) as error:
            submission_receipt("receipt-lookup", User(id="another"), db)
        assert error.value.status_code == 404


def test_batch_rows_sort_by_created_at_in_submission_order(sessions):
    # Claims and task lists order by (created_at, id); ids are random UUIDs.
    # Rows of one batch must therefore sort exactly as they were submitted.
    receipt = submit(sessions, kind="script.parse", count=50, key="ordering")
    with sessions() as db:
        ordered = db.scalars(select(Task.id).where(Task.id.in_(receipt["task_ids"]))
                             .order_by(Task.created_at.asc(), Task.id.asc())).all()
    assert list(ordered) == receipt["task_ids"]


def test_consecutive_batches_at_same_instant_do_not_interleave(sessions, monkeypatch):
    frozen = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(submission, "utcnow", lambda: frozen)
    first = submit(sessions, kind="script.parse", count=5, key="first", request={"batch": 1})
    second = submit(sessions, kind="script.parse", count=5, key="second", request={"batch": 2},
                    prepare=lambda: (entries("script.parse", 10)[5:], {"generation": {"max_concurrency": 2}}))
    with sessions() as db:
        ordered = db.scalars(select(Task.id).where(Task.id.in_(first["task_ids"] + second["task_ids"]))
                             .order_by(Task.created_at.asc(), Task.id.asc())).all()
    assert list(ordered) == first["task_ids"] + second["task_ids"]


def test_fair_claim_takes_batch_rows_in_submission_order(sessions, monkeypatch):
    from backend.platform import task_worker
    monkeypatch.setattr(task_worker, "SessionLocal", sessions)
    claimed = []

    def fake_claim(task_id, worker, **kwargs):
        claimed.append(task_id)
        with sessions.begin() as db:
            db.get(Task, task_id).status = "running"
        return None

    monkeypatch.setattr(task_worker, "claim_task", fake_claim)
    receipt = submit(sessions, kind="script.parse", count=5, key="claim")
    for _ in receipt["task_ids"]:
        task_worker.claim_fair_task("order-worker", task_types=("script.parse",))
    assert claimed == receipt["task_ids"]


def test_batch_creates_missing_quota_account_under_lock(sessions):
    # Without an account row there is no lock to serialize on; admission must
    # create it (as task_submission does) so every batch of the owner takes it.
    with sessions.begin() as db:
        db.query(UserQuotaAccount).filter(UserQuotaAccount.user_id == "owner").delete()
    receipt = submit(sessions, kind="bgm.match", count=2, key="no-account")
    assert len(receipt["task_ids"]) == 2
    with sessions() as db:
        assert db.get(UserQuotaAccount, "owner") is not None



def test_lock_owner_account_creates_once_and_returns_existing(sessions):
    # The helper is the batch path's only account lock; a second call must lock
    # the row the first call created rather than insert a duplicate.
    with sessions.begin() as db:
        db.query(UserQuotaAccount).filter(UserQuotaAccount.user_id == "owner").delete()
    with sessions() as db:
        created = submission._lock_owner_account(db, "owner")
        assert created.available_units == 0
        db.commit()
    with sessions() as db:
        existing = submission._lock_owner_account(db, "owner")
        assert existing.user_id == "owner"
        assert db.scalar(select(func.count()).select_from(UserQuotaAccount).where(UserQuotaAccount.user_id == "owner")) == 1


def test_lock_owner_account_conflict_branch_returns_winner_row(sessions, monkeypatch):
    # Force the race: the first lookup misses, the insert hits the committed row
    # (IntegrityError), and the re-select returns the winner. The sentinel proves
    # the except branch produced the result. A real transaction is opened first
    # so the savepoint nests inside it as on PostgreSQL.
    winner = object()
    with sessions() as db:
        db.add(User(id="txn-opener", username="opener", email="opener@example.test", password_hash="unused"))
        db.flush()
        lookups = [None, winner]
        monkeypatch.setattr(db, "scalar", lambda *a, **k: lookups.pop(0))
        assert submission._lock_owner_account(db, "owner", use_savepoint=True) is winner
        assert lookups == []
        db.rollback()


def test_lock_owner_account_raises_when_row_stays_invisible(sessions, monkeypatch):
    with sessions() as db:
        db.add(User(id="txn-opener", username="opener", email="opener@example.test", password_hash="unused"))
        db.flush()
        monkeypatch.setattr(db, "scalar", lambda *a, **k: None)
        with pytest.raises(RuntimeError):
            submission._lock_owner_account(db, "owner", use_savepoint=True)
        db.rollback()


def test_rejected_billable_batch_leaves_no_orphan_account(sessions):
    # Zero units rejects a billable batch; the account created for it must roll
    # back with the transaction rather than persist as a committed side effect.
    with sessions.begin() as db:
        db.query(UserQuotaAccount).filter(UserQuotaAccount.user_id == "owner").delete()
    with pytest.raises(TaskSubmissionError) as rejected:
        submit(sessions, kind="voices.foundation", count=2, key="orphan")
    assert rejected.value.status_code == 409
    with sessions() as db:
        assert db.get(UserQuotaAccount, "owner") is None
