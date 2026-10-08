"""Isolated real-HTTP TTS admission/preparation benchmark; never loads a model.

Default: disposable SQLite. Set NARRIFY_LOADTEST_POSTGRES_SERVER to a PostgreSQL
administrative URL to create/drop a uniquely named disposable database. This
variable is deliberately separate from the application's deployment URL.
Example: .venv/bin/python scripts/loadtest-tts-startup.py --duration-seconds 1800
"""
from __future__ import annotations
import argparse
import contextvars
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def percentile(values, fraction=.95):
    values = sorted(values)
    return round(values[max(0, __import__('math').ceil(len(values) * fraction) - 1)], 2) if values else None


def orchestrate(args):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    server = os.environ.get('NARRIFY_LOADTEST_POSTGRES_SERVER')
    name, admin = 'narrify_loadtest_' + uuid.uuid4().hex, None
    with tempfile.TemporaryDirectory(prefix='narrify-loadtest-') as directory:
        try:
            if server:
                url = make_url(server)
                if url.get_backend_name() != 'postgresql':
                    raise SystemExit('Load-test server must be PostgreSQL')
                admin = create_engine(url, isolation_level='AUTOCOMMIT')
                with admin.connect() as connection:
                    connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
                database = url.set(database=name).render_as_string(hide_password=False)
            else:
                database = f'sqlite:///{directory}/benchmark.db'
            env = {**os.environ, 'NARRIFY_DATABASE_URL': database, 'NARRIFY_STORAGE_ROOT': directory,
                   'NARRIFY_AUTO_CREATE_SCHEMA': 'false', 'NARRIFY_INITIAL_QUOTA_UNITS': '100000000',
                   'NARRIFY_BOOTSTRAP_ADMIN_EMAIL': '', 'NARRIFY_BOOTSTRAP_ADMIN_PASSWORD': ''}
            subprocess.run([sys.executable, '-m', 'alembic', '-c', 'alembic.ini', 'upgrade', 'head'],
                           cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)
            command = [sys.executable, str(Path(__file__).resolve()), '--child', '--duration-seconds', str(args.duration_seconds)]
            return subprocess.call(command, cwd=ROOT, env=env)
        finally:
            if admin:
                with admin.connect() as connection:
                    connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                admin.dispose()


def benchmark(args):
    import httpx
    import uvicorn
    from sqlalchemy import event, select, update
    from backend.main import app
    from backend.platform.database import SessionLocal, engine, lock_engine
    from backend.platform.models import Task, TaskAttempt, User
    from backend.platform.platform_settings import settings
    from backend.platform.storage import project_workspace_path
    from backend.platform.tts_resource_budget import memory_snapshot

    request_queries = contextvars.ContextVar('loadtest_queries', default=None)
    traces, trace_lock = [], threading.Lock()
    peak_connections = {'business': 0, 'locks': 0}
    class TraceMiddleware:
        def __init__(self, app): self.app = app
        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http': return await self.app(scope, receive, send)
            counts = [0]; token = request_queries.set(counts)
            try: await self.app(scope, receive, send)
            finally:
                request_queries.reset(token)
                with trace_lock: traces.append((scope['path'], counts[0]))
    app.add_middleware(TraceMiddleware)
    def count_sql(*_):
        counts = request_queries.get()
        if counts is not None: counts[0] += 1
        with trace_lock:
            for name, pool in [('business', engine.pool), ('locks', lock_engine.pool)]:
                if hasattr(pool, 'checkedout'):
                    peak_connections[name] = max(peak_connections[name], pool.checkedout())
    event.listen(engine, 'before_cursor_execute', count_sql)
    event.listen(lock_engine, 'before_cursor_execute', count_sql)
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen(128)
    base = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    start = time.monotonic()
    while not server.started:
        if time.monotonic() - start > 10: raise RuntimeError('isolated HTTP server failed to start')
        time.sleep(.02)
    clients, owners = [], []
    stop = threading.Event(); readers = []; health_times, control_times, rss = [], [], []
    monitor_errors = []; receipts = []; stream_frames = []
    try:
        chapter_names = [f'chapter-{i:04}.json' for i in range(500)]
        chapter = json.dumps([{'speaker': 'A', 'text': f'第{i}条合成内容，用于启动压力测试。'} for i in range(100)], ensure_ascii=False)
        for i in range(10):
            client = httpx.Client(base_url=base, timeout=30); clients.append(client)
            response = client.post('/api/auth/register', json={'username': f'loadtest{i}', 'email': f'loadtest{i}@example.test', 'password': 'loadtest-pass'})
            response.raise_for_status(); auth = response.json()
            client.headers['X-CSRF-Token'] = auth['csrf_token']
            response = client.post('/api/v1/projects', json={'name': 'Load test'}); response.raise_for_status()
            project_id = response.json()['id']; owners.append((auth['user']['id'], project_id))
            with SessionLocal() as db:
                user = db.get(User, auth['user']['id'])
                workspace = project_workspace_path(db, user.username, project_id)
            parsed = workspace / '03_parsed_json'; parsed.mkdir(parents=True, exist_ok=True)
            for name in chapter_names: (parsed / name).write_text(chapter, encoding='utf-8')
            voices = workspace / '04_voice_profiles'; voices.mkdir(parents=True, exist_ok=True)
            (voices / 'voice_config.json').write_text('{"A":{"type":"custom"}}', encoding='utf-8')

        def observe(index):
            try:
                with httpx.Client(base_url=base, cookies=clients[index].cookies, timeout=15) as reader:
                    with reader.stream('GET', '/api/v1/tasks/stream?compact=true') as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if line.startswith('data:'): stream_frames.append(len(line))
                            if stop.is_set(): break
            except Exception as error:
                if not stop.is_set(): monitor_errors.append(type(error).__name__)
        for index in range(10):
            reader = threading.Thread(target=observe, args=(index,), daemon=True); reader.start(); readers.append(reader)

        def monitor():
            cancelled = False
            with httpx.Client(base_url=base, cookies=clients[0].cookies, timeout=10) as probe:
                while not stop.is_set():
                    try:
                        begun = time.monotonic(); probe.get('/api/health').raise_for_status(); health_times.append((time.monotonic() - begun) * 1000)
                        begun = time.monotonic(); probe.get('/api/v1/tasks/center/summary').raise_for_status(); control_times.append((time.monotonic() - begun) * 1000)
                        if receipts and not cancelled:
                            begun = time.monotonic()
                            clients[9].post('/api/v1/tasks/' + receipts[9]['task_ids'][-1] + '/cancel').raise_for_status()
                            control_times.append((time.monotonic() - begun) * 1000)
                            cancelled = True
                        rss.append(memory_snapshot()[1] or 0)
                    except Exception as error: monitor_errors.append(type(error).__name__)
                    stop.wait(.2)
        monitor_thread = threading.Thread(target=monitor, daemon=True); monitor_thread.start()
        barrier = threading.Barrier(10)
        def submit(index):
            barrier.wait(); begun = time.monotonic()
            response = clients[index].post('/api/tts/batch', headers={'Idempotency-Key': f'burst-{index}'},
                                          json={'scripts': chapter_names, 'project_id': owners[index][1]})
            response.raise_for_status()
            return (time.monotonic() - begun) * 1000, response.json()
        with ThreadPoolExecutor(max_workers=10) as executor: submitted = list(executor.map(submit, range(10)))
        receipts = [receipt for _, receipt in submitted]
        assert all(len(receipt['task_ids']) == 500 for receipt in receipts)
        replay = clients[0].post('/api/tts/batch', headers={'Idempotency-Key': 'burst-0'}, json={'scripts': chapter_names, 'project_id': owners[0][1]})
        assert replay.json() == receipts[0], replay.text
        conflict = clients[0].post('/api/tts/batch', headers={'Idempotency-Key': 'duplicate-key'}, json={'scripts': chapter_names, 'project_id': owners[0][1]})
        assert conflict.status_code == 409

        from backend.platform.task_worker import claim_task
        from backend.platform.tts_batch_claims import claim_tts_members
        from backend.platform.tts_batch_execution import TTSBatchContext
        from backend.platform.task_engine_support import engine_execution_context
        from backend.platform.quota import set_quota_context, reset_quota_context
        from backend.engines import tts_batch
        primary = claim_task(receipts[0]['task_ids'][0], 'loadtest-worker')
        assert primary
        claims = [primary, *claim_tts_members(primary, receipts[0]['task_ids'][1:])]
        assert len(claims) == 500
        assert claim_task(receipts[1]['task_ids'][0], 'second-loadtest-worker') is None
        handle = TTSBatchContext(claims, lambda *_: True, lambda *_: True)
        prep = {}; baseline_rss = memory_snapshot()[1] or 0
        class Prepared(BaseException): pass
        def stub(cmd, *_args, **_kwargs):
            with Path(cmd[cmd.index('--segments-file') + 1]).open() as file: rows = json.load(file)
            assert len(rows) == 50000
            prep.update(milliseconds=round((time.monotonic() - prep_start) * 1000, 2), segments=len(rows),
                        rss_growth_mib=round(((memory_snapshot()[1] or 0) - baseline_rss) / 2**20, 2), sql=prep_counts[0])
            raise Prepared()
        original_engine, original_run = tts_batch.resolve_engine, tts_batch.run_tts_subprocess
        tts_batch.resolve_engine = lambda: (Path('/fake/python'), Path('/fake/worker'))
        tts_batch.run_tts_subprocess = stub
        prep_counts = [0]; trace_token = request_queries.set(prep_counts)
        quota_token = set_quota_context(primary.owner_id, primary.task_id, primary.attempt_id)
        prep_start = time.monotonic()
        try:
            with engine_execution_context(primary):
                try: tts_batch.synthesize_multi(handle, chapter_names)
                except Prepared: pass
        finally:
            request_queries.reset(trace_token); reset_quota_context(quota_token)
            tts_batch.resolve_engine, tts_batch.run_tts_subprocess = original_engine, original_run
        assert prep, 'preparation did not reach the child boundary'
        with SessionLocal.begin() as db:
            db.execute(update(TaskAttempt).where(TaskAttempt.id.in_([claim.attempt_id for claim in claims])).values(status='cancelled'))
            db.execute(update(Task).where(Task.id.in_(receipts[0]['task_ids'])).values(status='cancelled'))
        until = time.monotonic() + args.duration_seconds
        while time.monotonic() < until: stop.wait(min(.5, until - time.monotonic()))
        stop.set(); monitor_thread.join(12)
        for reader in readers: reader.join(5)
        with SessionLocal() as db:
            assert len(db.scalars(select(Task.id)).all()) == 5000
            version = db.connection().exec_driver_sql('SELECT version()').scalar() if engine.dialect.name == 'postgresql' else 'SQLite (development only)'
        submit_sql = [count for path, count in traces if path == '/api/tts/batch']
        result = {'database': version, 'users': 10, 'chapters': 5000, 'segments_queued': 500000,
                  'submit_p95_ms': percentile([duration for duration, _ in submitted]), 'submit_sql_max': max(submit_sql),
                  'health_p95_ms': percentile(health_times), 'control_p95_ms': percentile(control_times),
                  'api_rss_max_mib': round(max(rss or [baseline_rss]) / 2**20, 2), 'preparation': prep,
                  'peak_connections': peak_connections,
                  'sse_frames': len(stream_frames), 'largest_sse_frame_bytes': max(stream_frames or [0]),
                  'monitor_errors': monitor_errors, 'observation_seconds': args.duration_seconds}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        passed = (not monitor_errors and result['submit_p95_ms'] <= 3000 and result['health_p95_ms'] <= 300
                  and result['control_p95_ms'] <= 1000 and result['api_rss_max_mib'] <= 1024
                  and result['submit_sql_max'] <= 100 and prep['sql'] <= 100 and prep['rss_growth_mib'] <= 1024)
        return 0 if passed else 1
    finally:
        stop.set()
        for client in clients: client.close()
        server.should_exit = True; thread.join(15); sock.close()
        engine.dispose(); lock_engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration-seconds', type=int, default=30)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.duration_seconds < 1: parser.error('duration must be positive')
    raise SystemExit(benchmark(options) if options.child else orchestrate(options))
