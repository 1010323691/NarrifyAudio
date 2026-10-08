"""Isolated real-wall-clock HTTP polling; default run lasts thirty minutes.

Uses actual ASGI routes and fenced progress tasks with private SQLite/storage.
No production service, Redis, user workspace or model calls are used.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def measure(seconds, report):
    with tempfile.TemporaryDirectory(prefix='narrify-progress-pressure-') as temporary:
        isolated = Path(temporary)
        os.environ.update(NARRIFY_DATABASE_URL=f'sqlite:///{isolated / "pressure.sqlite"}',
                          NARRIFY_STORAGE_ROOT=str(isolated / 'storage'), NARRIFY_AUTO_CREATE_SCHEMA='true',
                          NARRIFY_BOOTSTRAP_ADMIN_EMAIL='', NARRIFY_BOOTSTRAP_ADMIN_PASSWORD='',
                          NARRIFY_REGISTRATION_ENABLED='true', NARRIFY_COOKIE_SECURE='false',
                          NARRIFY_PROBE_CACHE_DIR=str(isolated / 'probes'))
        from backend.core import paths
        paths.PROJECT_ROOT = isolated
        paths.MUSIC_LIBRARY_DIR = isolated / 'music_library'
        from fastapi.testclient import TestClient
        from sqlalchemy import select
        from backend.main import app
        from backend.core.pathio import rewrite_json_file
        from backend.engines import project_completion as calculator
        from backend.platform.database import SessionLocal, engine, lock_engine
        from backend.platform.models import ProjectProgress, ProjectProgressRefresh, Task, TaskAttempt
        from backend.platform.progress_refresh import as_utc
        from backend.platform.storage import project_workspace_path
        from backend.platform import task_worker
        from backend.services.project_progress import refresh_due_progress
        starts, attempt_starts, polls, maximum_active = [], [], 0, 0
        calculate = calculator.project_completion
        def scan(*args, **kwargs):
            starts.append(time.monotonic())
            return calculate(*args, **kwargs)
        calculator.project_completion = scan
        def drain(project_id):
            nonlocal maximum_active
            refresh_due_progress()
            with SessionLocal() as db:
                tasks = db.scalars(select(Task).where(Task.project_id == project_id,
                    Task.task_type == 'project.progress', Task.status.in_(['pending', 'queued', 'retrying', 'running']))).all()
                maximum_active = max(maximum_active, len(tasks))
                assert len(tasks) <= 1
            for task in tasks:
                claim = task_worker.claim_task(task.id, 'isolated-progress-pressure', lease_seconds=120)
                if claim is not None:
                    with SessionLocal() as db:
                        attempt_starts.append(as_utc(db.get(TaskAttempt, claim.attempt_id).started_at))
                    assert task_worker.complete_claim(claim, task_worker.execute_claim(claim))
        started = time.monotonic()
        next_report = started + 60
        print(json.dumps(dict(run_pid=os.getpid(), requested_seconds=seconds, phase="starting")), flush=True)
        try:
            with TestClient(app) as client:
                suffix = uuid4().hex[:12]
                registration = client.post('/api/auth/register', json={
                    'username': f'pressure{suffix}', 'email': f'{suffix}@example.test', 'password': 'isolated-test-123'})
                assert registration.status_code == 201, registration.status_code
                project_id = client.get('/api/v1/projects').json()[0]['id']
                username = registration.json()['user']['username']
                with SessionLocal() as db:
                    root = project_workspace_path(db, username, project_id)
                (root / '02_split_text').mkdir(parents=True, exist_ok=True)
                (root / '03_parsed_json').mkdir(exist_ok=True)
                (root / '02_split_text/a.txt').write_text('chapter')
                deadline = time.monotonic() + seconds
                while True:
                    rewrite_json_file(root / '03_parsed_json/a.json', [{'speaker': 'A', 'text': str(polls)}])
                    response = client.get(f'/api/v1/projects/{project_id}/summary?progress=true')
                    assert response.status_code == 200, response.status_code
                    polls += 1
                    drain(project_id)
                    if time.monotonic() >= next_report:
                        print(json.dumps(dict(phase="polling", elapsed_seconds=round(time.monotonic() - started, 1), http_polls=polls, actual_scans=len(starts))), flush=True)
                        next_report = time.monotonic() + 60
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(min(1, deadline - time.monotonic()))
                finish_deadline = time.monotonic() + 45
                while True:
                    drain(project_id)
                    with SessionLocal() as db:
                        state = db.get(ProjectProgressRefresh, project_id)
                        snapshot = db.get(ProjectProgress, project_id)
                        if state is not None and not state.dirty and state.task_id is None:
                            assert snapshot.signature == state.requested_signature
                            break
                    assert time.monotonic() < finish_deadline, 'trailing refresh did not finish'
                    time.sleep(.25)
            intervals = [right - left for left, right in zip(starts, starts[1:])]
            task_intervals = [(right - left).total_seconds() for left, right in zip(attempt_starts, attempt_starts[1:])]
            assert not task_intervals or min(task_intervals) >= 30, task_intervals
            result = dict(requested_seconds=seconds, elapsed_seconds=round(time.monotonic() - started, 3),
                          http_polls=polls, actual_scans=len(starts), maximum_active=maximum_active,
                          minimum_start_interval=round(min(task_intervals), 3) if task_intervals else None,
                          minimum_scan_interval=round(min(intervals), 3) if intervals else None,
                          final_dirty=False, final_snapshot_current=True,
                          environment='isolated ASGI HTTP + SQLite + fenced Worker execution, real wall clock')
            if report: Path(report).write_text(json.dumps(result, indent=2))
            print(json.dumps(result), flush=True)
        finally:
            calculator.project_completion = calculate
            engine.dispose(); lock_engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=int, default=1800)
    parser.add_argument('--report')
    arguments = parser.parse_args()
    if not 0 <= arguments.seconds <= 3600: parser.error('seconds must be between 0 and 3600')
    measure(arguments.seconds, arguments.report)
