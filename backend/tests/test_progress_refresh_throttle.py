"""Actual fenced progress tasks under controlled time, including trailing work."""
from datetime import timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.core.pathio import rewrite_json_file
from backend.platform.database import SessionLocal
from backend.platform.models import Project, ProjectProgress, ProjectProgressRefresh, Task, User, utcnow
from backend.platform.storage import project_workspace_path
from backend.platform import task_worker, progress_refresh
from backend.services import project_progress
from backend.engines import project_completion as calculator


@pytest.fixture
def book(monkeypatch):
    from backend.platform.models import User
    clock = [utcnow()]
    for module in (task_worker, progress_refresh, project_progress):
        monkeypatch.setattr(module, 'utcnow', lambda: clock[0])
    monkeypatch.setattr(progress_refresh.time, 'time', lambda: clock[0].timestamp())
    with SessionLocal.begin() as db:
        suffix = uuid4().hex
        user = User(username=f'throttle-{suffix}', email=f'{suffix}@test.local', password_hash='test')
        db.add(user); db.flush()
        project = Project(owner_id=user.id, name='Throttle', directory_key=f'{user.username}/book')
        db.add(project); db.flush()
        owner, project_id = user.id, project.id
        root = project_workspace_path(db, user.username, project_id)
        (root / '00_temp').mkdir(parents=True)
        (root / '02_split_text').mkdir()
        (root / '03_parsed_json').mkdir()
        (root / '02_split_text/a.txt').write_text('chapter')
        (root / '03_parsed_json/a.json').write_text('[{"speaker":"A","text":"hello"}]')
    return clock, owner, project_id, root


def request(book):
    _clock, owner, project_id, root = book
    with SessionLocal() as db:
        project_progress.progress_summary(db, db.get(User, owner),
                                          project_id, root, None)
    with SessionLocal() as db:
        return db.scalar(select(Task).where(Task.project_id == project_id, Task.task_type == 'project.progress',
                                          Task.status.in_(['pending', 'queued', 'retrying', 'running'])))


def run(task):
    claim = task_worker.claim_task(task.id, 'progress-throttle-test', lease_seconds=600)
    if claim is None: return None
    outcome = task_worker.execute_claim(claim)
    assert task_worker.complete_claim(claim, outcome)
    return claim


def test_pure_task_progress_and_heartbeat_do_not_change_artifact_signature(book):
    clock, owner, project_id, root = book
    from backend.platform.models import User
    with SessionLocal.begin() as db:
        task = Task(owner_id=owner, project_id=project_id, task_type='text.format', status='running', payload={})
        db.add(task); db.flush()
        user = db.get(User, owner)
        before = project_progress.progress_signature(db, user, project_id, root)
        db.add(ProjectProgress(project_id=project_id, signature=before, stages={}))
        task.progress = 99
        task.updated_at = clock[0] + timedelta(seconds=1)
    with SessionLocal() as db:
        assert project_progress.progress_signature(db, db.get(User, owner), project_id, root) == before
    assert request(book) is None


def test_running_dirty_refresh_trails_without_any_further_http_request(book, monkeypatch):
    clock, owner, project_id, root = book
    original = calculator.project_completion
    changed = []
    def scan(path):
        result = original(path)
        if not changed:
            rewrite_json_file(root / '03_parsed_json/a.json', [])
            changed.append(True)
        return result
    monkeypatch.setattr(calculator, 'project_completion', scan)
    first = request(book)
    claim = task_worker.claim_task(first.id, 'trailing-test', lease_seconds=600)
    outcome = task_worker.execute_claim(claim)
    assert task_worker.claim_task(first.id, 'duplicate-event') is None
    with SessionLocal() as db: assert db.get(Task, first.id).status == 'running'
    assert task_worker.complete_claim(claim, outcome)
    with SessionLocal() as db:
        state = db.get(ProjectProgressRefresh, project_id)
        assert state.dirty and state.task_id is None
    clock[0] += timedelta(seconds=29)
    project_progress.refresh_due_progress()
    with SessionLocal() as db:
        assert db.get(ProjectProgressRefresh, project_id).task_id is None
    clock[0] += timedelta(seconds=1)
    project_progress.refresh_due_progress()
    with SessionLocal() as db:
        state = db.get(ProjectProgressRefresh, project_id)
        assert state.task_id and state.task_id != first.id
        second = db.get(Task, state.task_id)
    assert run(second)
    with SessionLocal() as db:
        assert not db.get(ProjectProgressRefresh, project_id).dirty
        assert db.get(ProjectProgress, project_id).stages['03_parsed_json']['percent'] == 0


def test_retry_cannot_start_a_second_scan_before_thirty_seconds(book):
    from backend.platform.task_contracts import TaskExecutionError
    clock, owner, project_id, root = book
    task = request(book)
    claim = task_worker.claim_task(task.id, 'failed-scan', lease_seconds=600)
    task_worker.execute_claim(claim)
    task_worker.fail_claim(claim, TaskExecutionError('scan_interrupted', 'interrupted', retryable=True))
    clock[0] += timedelta(seconds=3)
    assert task_worker.claim_task(task.id, 'early-retry', lease_seconds=600) is None
    with SessionLocal() as db:
        assert progress_refresh.as_utc(db.get(Task, task.id).next_attempt_at) == clock[0] + timedelta(seconds=27)
    clock[0] += timedelta(seconds=27)
    assert run(task)


def test_external_in_place_edit_has_five_minute_fallback(book):
    from backend.platform.models import User
    clock, owner, project_id, root = book
    with SessionLocal() as db:
        user = db.get(User, owner)
        before = project_progress.progress_signature(db, user, project_id, root)
        (root / '03_parsed_json/a.json').write_text('[]')  # external, no managed epoch
        assert project_progress.progress_signature(db, user, project_id, root) == before
        clock[0] += timedelta(seconds=300)
        assert project_progress.progress_signature(db, user, project_id, root) != before


def test_thirty_minute_polling_coalesces_and_never_starts_under_thirty_seconds(book, monkeypatch):
    clock, owner, project_id, root = book
    baseline = clock[0]
    starts = []
    original = calculator.project_completion
    def scan(path):
        starts.append(clock[0])
        return original(path)
    monkeypatch.setattr(calculator, 'project_completion', scan)
    # 每个 30s 窗口边界附近（28~31s）逐秒走，保住“29s 不得起扫”的精度；其余用 5s 步长省时
    for second in sorted(set(range(0, 1801, 5)) | {s for s in range(1801) if s % 30 in (0, 1, 28, 29)}):
        clock[0] = baseline + timedelta(seconds=second)
        rewrite_json_file(root / '03_parsed_json/a.json', [{'speaker': 'A', 'text': str(second)}])
        task = request(book)
        if task is not None: run(task)
    assert len(starts) == 61
    assert all((right - left).total_seconds() >= 30 for left, right in zip(starts, starts[1:]))
    with SessionLocal() as db:
        state = db.get(ProjectProgressRefresh, project_id)
        assert not state.dirty and state.task_id is None
        assert db.get(ProjectProgress, project_id).signature == state.requested_signature


def test_active_managed_writer_keeps_snapshot_dirty_until_trailing_refresh(book):
    from backend.core.workspace_epochs import managed_mutation
    clock, owner, project_id, root = book
    with managed_mutation(root / '03_parsed_json/a.json'):
        assert run(request(book))
        with SessionLocal() as db:
            assert db.get(ProjectProgressRefresh, project_id).dirty
        # Even an equal signature with an active writer cannot mark the state clean.
        project_progress.request_progress_refresh(owner, project_id)
        with SessionLocal() as db:
            assert db.get(ProjectProgressRefresh, project_id).dirty
    clock[0] += timedelta(seconds=30)
    project_progress.refresh_due_progress()
    with SessionLocal() as db:
        task = db.get(Task, db.get(ProjectProgressRefresh, project_id).task_id)
    assert run(task)
    with SessionLocal() as db:
        assert not db.get(ProjectProgressRefresh, project_id).dirty


def test_concurrent_refresh_requests_coalesce_to_one_durable_task(book):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import func
    _clock, owner, project_id, _root = book
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: project_progress.request_progress_refresh(owner, project_id), range(8)))
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Task).where(
            Task.project_id == project_id, Task.task_type == 'project.progress')) == 1
        assert db.get(ProjectProgressRefresh, project_id).task_id is not None


def test_hot_signature_never_queries_task_history_or_enumerates_directories(book, monkeypatch):
    from pathlib import Path
    from sqlalchemy import event
    _clock, owner, project_id, root = book
    statements = []
    def forbidden(*args, **kwargs): raise AssertionError('hot progress signature must not scan directories')
    monkeypatch.setattr(Path, 'iterdir', forbidden)
    monkeypatch.setattr(Path, 'glob', forbidden)
    with SessionLocal() as db:
        user = db.get(User, owner)
        engine = db.get_bind()
        def recorded(conn, cursor, statement, parameters, context, many): statements.append(statement)
        event.listen(engine, 'before_cursor_execute', recorded)
        try:
            assert len(project_progress.progress_signature(db, user, project_id, root)) == 64
        finally:
            event.remove(engine, 'before_cursor_execute', recorded)
    assert statements == []
