"""Startup-load regressions: real durable records, no model dependency."""
from __future__ import annotations
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import event, select, update
from backend.platform.database import SessionLocal, engine
from backend.platform.models import User, Project, UserQuotaAccount, Task, TaskAttempt, TaskBatch, TaskEvent, OutboxEvent
from backend.platform.tts_submission import submit_tts_tasks


@pytest.fixture
def owner():
    name = uuid.uuid4().hex
    with SessionLocal() as db:
        user = User(username=name, email=f'{name}@example.com', password_hash='unused')
        db.add(user); db.flush()
        project = Project(owner_id=user.id, name=name, directory_key=f"{name}/{name}")
        db.add(project); db.flush()
        db.add(UserQuotaAccount(user_id=user.id, available_units=10000000)); db.commit()
        ids = user.id, project.id
    yield ids
    with SessionLocal.begin() as db:
        tasks = list(db.scalars(select(Task.id).where(Task.owner_id == ids[0])))
        db.execute(update(Task).where(Task.id.in_(tasks)).values(status='cancelled'))
        db.execute(update(TaskAttempt).where(TaskAttempt.task_id.in_(tasks)).values(status='cancelled'))


def submit(owner, names, key='key', task_type='tts.batch'):
    with SessionLocal() as db:
        user = db.get(User, owner[0])
        ctx = SimpleNamespace(user=user, session=SimpleNamespace(active_project_id=owner[1]))
        entries = [{'label': f'音频合成 · {name}', 'payload': {'script': name, 'scripts': [name], 'indices': None,
                    'config': {'tts': {'batch_concurrency': 4}}}} for name in names]
        return submit_tts_tasks(task_type=task_type, entries=entries, ctx=ctx, db=db, idempotency_key=key)


def test_500_chapter_submission_has_bounded_sql_and_one_config(owner):
    queries = []
    def count(*args): queries.append(1)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        result = submit(owner, [f'{i}.json' for i in range(500)])
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    assert len(queries) <= 100
    assert len(result['task_ids']) == 500
    with SessionLocal() as db:
        tasks = db.scalars(select(Task).where(Task.batch_id == result['batch_id'])).all()
        assert all('config' not in task.payload and task.event_sequence == 1 for task in tasks)
        assert db.get(TaskBatch, result['batch_id']).config['tts']['batch_concurrency'] == 4
        assert len(db.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id.in_(result['task_ids']))).all()) == 1


def test_same_key_concurrent_replay_creates_one_batch(owner):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(owner, ['s.json']), range(2)))
    assert results[0] == results[1]


def test_alias_and_different_key_conflict_is_atomic(owner):
    first = submit(owner, ['s.json'])
    with pytest.raises(HTTPException) as conflict:
        submit(owner, ['s_checked.json', 'other.json'], key='other')
    assert conflict.value.status_code == 409
    assert conflict.value.detail['task_ids'] == first['task_ids']
    with SessionLocal() as db:
        assert len(db.scalars(select(Task).where(Task.owner_id == owner[0])).all()) == 1


def test_replay_after_completion_and_changed_configuration_still_returns_receipt(owner):
    result = submit(owner, ['s.json'])
    with SessionLocal.begin() as db:
        db.execute(update(Task).where(Task.id.in_(result['task_ids'])).values(status='succeeded'))
        db.execute(update(UserQuotaAccount).where(UserQuotaAccount.user_id == owner[0]).values(available_units=0))
    assert submit(owner, ['s.json']) == result
    with pytest.raises(HTTPException) as conflict:
        submit(owner, ['other.json'])
    assert conflict.value.status_code == 409


def test_prepare_budget_rejects_oversize_before_read(tmp_path, monkeypatch):
    from backend.platform import tts_resource_budget as budgets
    from backend.platform.task_contracts import TaskExecutionError
    monkeypatch.setattr(budgets, 'memory_snapshot', lambda: (8 * 2**30, 100 * 2**20))
    path = tmp_path / 'huge.json'
    with path.open('wb') as stream: stream.truncate(budgets.MAX_FILE_BYTES + 1)
    with pytest.raises(TaskExecutionError, match='预算'):
        budgets.PreparationBudget().file(path)
    budget = budgets.PreparationBudget()
    with pytest.raises(TaskExecutionError, match='50000'):
        budget.add_segments(50001)


def test_member_claim_and_baseline_use_collection_transactions(owner):
    from backend.platform.task_worker import claim_task
    from backend.platform.tts_batch_claims import claim_tts_members, heartbeat_members
    from backend.platform.task_context import write_claim_events
    result = submit(owner, [f'{i}.json' for i in range(500)])
    # Other modules can leave artificial live attempts; this test exercises members only.
    primary = claim_task(result['task_ids'][0], 'overload-test', defer_workspace_conflicts=False)
    assert primary
    statements = []
    def count(*args): statements.append(1)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        members = claim_tts_members(primary, result['task_ids'][1:])
        assert len(members) == 499
        assert len(statements) <= 100
        statements.clear()
        write_claim_events([(claim, [('segments', {'done': 0, 'total': 100}), ('progress', {'progress': 0, 'current': '准备章节'})]) for claim in [primary, *members]])
        assert len(statements) <= 100
        statements.clear()
        heartbeat_members([primary, *members])
        assert len(statements) <= 100
    finally:
        event.remove(engine, 'before_cursor_execute', count)


def test_preparation_controls_and_stage_roots_do_not_query_per_chapter(owner):
    from backend.platform.task_worker import claim_task
    from backend.platform.tts_batch_claims import claim_tts_members
    from backend.platform.tts_batch_execution import TTSBatchContext
    result = submit(owner, [f'{i}.json' for i in range(500)])
    primary = claim_task(result['task_ids'][0], 'control-budget', defer_workspace_conflicts=False)
    claims = [primary, *claim_tts_members(primary, result['task_ids'][1:])]
    context = TTSBatchContext(claims, lambda *_: True, lambda *_: True)
    statements = []
    def count(*args): statements.append(1)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        for name in context.contexts:
            context.check()
            assert not context.chapter_cancelled(name)
        assert len(statements) <= 10
    finally:
        event.remove(engine, 'before_cursor_execute', count)


def test_compact_stream_reads_idle_and_changed_collections_with_bounded_sql(owner):
    from backend.api.platform_tasks import _user_tasks
    from backend.services.task_views import compact_snapshot_payload, compact_new_frames
    from backend.platform.task_worker import claim_task
    from backend.platform.task_context import write_claim_events
    from backend.platform.tts_batch_claims import claim_tts_members
    result = submit(owner, [f'{i}.json' for i in range(500)])
    rows = lambda db: _user_tasks(db, owner[0])
    statements = []
    def count(*args): statements.append(1)
    event.listen(engine, 'before_cursor_execute', count)
    try:
        snapshots, seen, _, delivered = compact_snapshot_payload(rows)
        assert len(snapshots) == 500 and all(not s['logs'] for s in snapshots)
        assert len(statements) <= 20
        statements.clear()
        assert compact_new_frames(rows, seen, delivered) == []
        assert len(statements) <= 10
    finally:
        event.remove(engine, 'before_cursor_execute', count)
    primary = claim_task(result['task_ids'][0], 'stream-budget', defer_workspace_conflicts=False)
    claims = [primary, *claim_tts_members(primary, result['task_ids'][1:])]
    write_claim_events([(claim, [('phase', {'phase': '准备章节', 'current': '准备章节'})]) for claim in claims])
    event.listen(engine, 'before_cursor_execute', count)
    try:
        statements.clear()
        frames = compact_new_frames(rows, seen, delivered)
        assert len(statements) <= 20
        assert len([frame for frame in frames if frame['type'] == 'phase']) <= 500
        assert all(frame['current'] == '准备章节' for frame in frames if frame['type'] == 'phase')
        assert all(frame['task']['current'] == '准备章节' for frame in frames if frame['type'] == 'status')
        assert len([frame for frame in frames if frame['type'] == 'status']) == 500
    finally:
        event.remove(engine, 'before_cursor_execute', count)


def test_preparation_hysteresis_and_segment_limit(monkeypatch):
    from backend.platform import tts_resource_budget as budgets
    from backend.engines.tts_batch import build_segments
    from backend.platform.task_contracts import TaskExecutionError
    available = [2**30]
    monkeypatch.setattr(budgets, 'memory_snapshot', lambda: (available[0], 0))
    monkeypatch.setattr(budgets, '_memory_paused', False)
    assert not budgets.preparation_memory_available()
    available[0] = int(2.5 * 2**30)
    assert not budgets.preparation_memory_available()
    available[0] = 3 * 2**30
    assert budgets.preparation_memory_available()
    with pytest.raises(TaskExecutionError, match='50000'):
        build_segments([{'text': 'x'}, {'text': 'y'}], maximum=1)


def test_capacity_rejection_and_replay_precedes_capacity_check(owner, monkeypatch):
    import backend.platform.tts_submission as submissions
    first = submit(owner, ['one.json', 'two.json'])
    monkeypatch.setattr(submissions, 'MAX_USER_PENDING', 2)
    assert submit(owner, ['one.json', 'two.json']) == first
    with pytest.raises(HTTPException) as error:
        submit(owner, ['three.json'], key='user-full')
    assert error.value.status_code == 429 and error.value.headers['Retry-After'] == '10'
    monkeypatch.setattr(submissions, 'MAX_USER_PENDING', 1000)
    monkeypatch.setattr(submissions, 'MAX_GLOBAL_PENDING', 2)
    with pytest.raises(HTTPException) as error:
        submit(owner, ['three.json'], key='global-full')
    assert error.value.status_code == 429
    with SessionLocal() as db:
        assert len(db.scalars(select(TaskBatch).where(TaskBatch.owner_id == owner[0])).all()) == 1


def test_stream_limit_closes_auth_session_before_yield(owner, monkeypatch):
    from backend.platform import deps
    from backend.platform.security import create_session
    from backend.platform.platform_settings import settings
    with SessionLocal.begin() as db:
        token, _, _ = create_session(db, db.get(User, owner[0]))
    request = SimpleNamespace(cookies={settings.session_cookie: token})
    streams = [deps.require_stream_user(request) for _ in range(3)]
    for stream in streams: assert next(stream).id == owner[0]
    fourth = deps.require_stream_user(request)
    try:
        with pytest.raises(HTTPException) as error: next(fourth)
        assert error.value.status_code == 429
        assert deps._streams[owner[0]] == 3
    finally:
        for stream in streams: stream.close()
    assert owner[0] not in deps._streams


def test_http_admission_bounds_body_and_releases_slots_on_failure():
    import asyncio
    from backend.platform.tts_admission import TTSAdmissionMiddleware
    async def exercise():
        called = []
        async def downstream(scope, receive, send):
            called.append(await receive())
            raise RuntimeError('endpoint failed')
        middleware = TTSAdmissionMiddleware(downstream)
        scope = {'type': 'http', 'method': 'POST', 'path': '/api/tts/batch'}
        sent = []
        async def send(message): sent.append(message)
        async def oversized(): return {'type': 'http.request', 'body': b'x' * (1024 * 1024 + 1), 'more_body': False}
        await middleware(scope, oversized, send)
        assert sent[0]['status'] == 413 and not called
        assert middleware.active == middleware.waiting == 0
        chunks = iter([{'type': 'http.request', 'body': b'a', 'more_body': True},
                       {'type': 'http.request', 'body': b'b', 'more_body': False}])
        async def receive(): return next(chunks)
        with pytest.raises(RuntimeError, match='endpoint failed'): await middleware(scope, receive, send)
        assert called == [{'type': 'http.request', 'body': b'ab', 'more_body': False}]
        assert middleware.active == middleware.waiting == 0
        middleware.waiting = 32; sent.clear()
        await middleware(scope, receive, send)
        assert sent[0]['status'] == 429
    asyncio.run(exercise())


def test_generic_multi_chapter_rows_count_chapters_in_pending_budget(owner, monkeypatch):
    from backend.api.task_operations import submit_task, TaskSubmit
    import backend.platform.tts_submission as submissions
    with SessionLocal() as db:
        user = db.get(User, owner[0])
        first = submit_task(TaskSubmit(project_id=owner[1], task_type='tts.batch', payload={'scripts': ['one.json', 'two.json']},
                                       idempotency_key='generic-multi'), user=user, db=db)
        assert db.get(Task, first['id']).admission_units == 2
    monkeypatch.setattr(submissions, 'MAX_USER_PENDING', 2)
    with pytest.raises(HTTPException) as error: submit(owner, ['three.json'], key='generic-cap')
    assert error.value.status_code == 429


def test_bounded_summary_cache_does_not_retain_large_rows():
    from backend.core.bounded_cache import BoundedCache
    cache = BoundedCache(1024, 5)
    for i in range(20): cache[str(i)] = {'missing': ['x' * 100]}
    assert len(cache) < 5 and cache.retained_bytes <= 1024
    cache['oversized'] = 'x' * 2000
    assert 'oversized' not in cache
    cache.clear()
    assert cache.retained_bytes == 0 and not cache


def test_compact_result_keeps_counts_and_retry_removes_old_summary():
    from backend.platform.task_lifecycle import compact_result, _update_ui_state
    state = {'result': compact_result({'total': 100, 'completed': 98, 'failed': [{'index': 1}, {'index': 2}], 'files': ['large'], 'oversized_text': 'x' * 2000})}
    assert state['result'] == {'total': 100, 'completed': 98, 'failed_count': 2}
    _update_ui_state(state, 'retry_requested', {})
    assert 'result' not in state and state['current'] == '等待启动'


def test_compact_stream_does_not_advance_past_the_task_snapshot(owner):
    from backend.api.platform_tasks import _user_tasks
    from backend.services.task_views import compact_snapshot_payload, compact_new_frames
    from backend.platform.task_lifecycle import append_task_event
    result = submit(owner, ['one.json'])
    task_id = result['task_id']
    normal = lambda db: _user_tasks(db, owner[0], compact=True)
    _, seen, _, delivered = compact_snapshot_payload(normal)
    def raced(db):
        rows = normal(db)
        with SessionLocal.begin() as writer:
            task = writer.get(Task, task_id)
            task.progress = 50
            append_task_event(writer, task_id, 'progress', {'progress': 50, 'current': 'new progress'})
        return rows
    assert compact_new_frames(raced, seen, delivered) == []
    assert seen[task_id] == 1
    frames = compact_new_frames(normal, seen, delivered)
    assert next(frame for frame in frames if frame['type'] == 'progress')['progress'] == .5
    assert seen[task_id] == 2


def test_single_chapter_tts_startup_is_a_phase_not_audio_progress():
    from types import SimpleNamespace
    from backend.platform.task_context import EngineExecutionContext
    context = EngineExecutionContext.__new__(EngineExecutionContext)
    context.claim = SimpleNamespace(task_type='tts.batch')
    phases, progress = [], []
    context.phase = phases.append
    context.progress_percent = lambda percent, current: progress.append((percent, current))
    context.progress(.02, '启动引擎')
    context.progress(.05, '加载模型')
    context.progress(.5, '合成台词')
    assert phases == ['启动引擎', '加载模型']
    assert progress == [(50, '合成台词')]
