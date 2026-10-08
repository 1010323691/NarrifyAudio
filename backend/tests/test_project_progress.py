"""HTTP reads never scan; one fenced Worker publishes a reusable DB snapshot."""
import json
import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.platform.database import SessionLocal
from backend.platform.models import Project, ProjectProgress, Task, User
from backend.platform.storage import project_workspace_path
from backend.platform.task_worker import claim_task, complete_claim, execute_claim
from backend.services.project_progress import progress_summary
from backend.services.project_overview import overview_tasks


@pytest.mark.parametrize('linked_part', ['directory', 'lock', 'dangling_lock', 'junction'])
@pytest.mark.parametrize('cached', [False, True])
def test_progress_reads_reject_linked_lock_paths(tmp_path, linked_part, cached):
    from backend.platform.storage import safe_project_workspace_path
    if linked_part == 'junction' and os.name != 'nt':
        pytest.skip('Windows junction regression')
    suffix = uuid4().hex
    with SessionLocal() as db:
        user = User(username=f'lock-{suffix}', email=f'{suffix}@example.test', password_hash='test')
        db.add(user)
        db.flush()
        project = Project(owner_id=user.id, name='Linked lock', directory_key=f'{user.username}/book')
        db.add(project)
        db.commit()
        root = project_workspace_path(db, user.username, project.id)
        root.mkdir(parents=True)
        outside = tmp_path / 'outside'
        outside.mkdir()
        target = outside / 'progress-request.lock'
        if linked_part == 'junction':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(root / '00_temp'), str(outside)], check=True, capture_output=True)
        else:
            try:
                if linked_part == 'directory':
                    (root / '00_temp').symlink_to(outside, target_is_directory=True)
                else:
                    (root / '00_temp').mkdir()
                    if linked_part == 'lock':
                        target.write_bytes(b'untouched')
                    (root / '00_temp/progress-request.lock').symlink_to(target)
            except OSError:
                pytest.skip('Directory/file symlinks unavailable')
        assert safe_project_workspace_path(db, user.username, project.id) == root
        stages = {'02_split_text': {'completed': 1, 'total': 2, 'unit': '章节', 'percent': 50}}
        if cached:
            db.add(ProjectProgress(project_id=project.id, signature='0' * 64, stages=stages))
            db.commit()
        try:
            assert progress_summary(db, user, project.id, root, 'text') == (stages if cached else {})
            assert db.scalar(select(Task.id).where(Task.project_id == project.id)) is None
            if linked_part == 'lock':
                assert target.read_bytes() == b'untouched'
            else:
                assert not target.exists()
        finally:
            # Junction removal must remove the link, never traverse its external target.
            if linked_part == 'junction':
                os.rmdir(root / '00_temp')


def test_progress_lock_prepares_a_new_workspace(tmp_path):
    from backend.services.project_progress import _progress_request_lock
    root = tmp_path / 'new-project'
    assert _progress_request_lock(root) == root / '00_temp/progress-request.lock'
    assert (root / '00_temp').is_dir()


def test_cold_reads_coalesce_worker_persists_and_warm_reads_do_not_scan(monkeypatch):
    from backend.engines import project_completion as calculator
    suffix = uuid4().hex
    with SessionLocal() as db:
        user = User(username=f'progress-{suffix}', email=f'{suffix}@example.test', password_hash='test')
        db.add(user)
        db.flush()
        project = Project(owner_id=user.id, name='Progress', directory_key=f'{user.username}/book')
        db.add(project)
        db.commit()
        user_id, project_id = user.id, project.id
        root = project_workspace_path(db, user.username, project.id)
        (root / '02_split_text').mkdir(parents=True)
        (root / '03_parsed_json').mkdir()
        (root / '02_split_text/a.txt').write_text('chapter')
        (root / '03_parsed_json/a.json').write_text(json.dumps([{'speaker': 'A', 'text': 'hello'}]))
        calculate = calculator.project_completion
        calls = []
        def scan(*args, **kwargs):
            calls.append(args)
            return calculate(*args, **kwargs)
        monkeypatch.setattr(calculator, 'project_completion', scan)
        for section in ('text', 'catalog', 'production'):
            assert progress_summary(db, user, project_id, root, section) == {}
        assert calls == []
        tasks = db.scalars(select(Task).where(Task.project_id == project_id)).all()
        assert len(tasks) == 1
        task_id = tasks[0].id
        # Internal reconciliation must not turn a finished book into "processing".
        assert overview_tasks(db, user_id, project_id)['statuses'] == []
    claim = claim_task(task_id, 'progress-test-worker', lease_seconds=600)
    assert claim is not None
    assert complete_claim(claim, execute_claim(claim))
    assert len(calls) == 1
    with SessionLocal() as db:
        user = db.get(User, user_id)
        assert db.get(ProjectProgress, project_id).stages['03_parsed_json']['percent'] == 100
        for _ in range(3):
            assert progress_summary(db, user, project_id, root, 'catalog')['03_parsed_json']['percent'] == 100
        assert len(calls) == 1
        assert len(db.scalars(select(Task).where(Task.project_id == project_id)).all()) == 1
        # Delete an output: immediately return the old snapshot while one rebuild queues.
        (root / '03_parsed_json/a.json').unlink()
        assert progress_summary(db, user, project_id, root, 'catalog')['03_parsed_json']['percent'] == 100
        jobs = db.scalars(select(Task).where(Task.project_id == project_id, Task.status == 'pending')).all()
        assert len(jobs) == 1
        next_id = jobs[0].id
    next_claim = claim_task(next_id, 'progress-test-worker', lease_seconds=600)
    assert complete_claim(next_claim, execute_claim(next_claim))
    with SessionLocal() as db:
        assert progress_summary(db, db.get(User, user_id), project_id, root, 'catalog')['03_parsed_json']['percent'] == 0
        assert len(calls) == 2


def test_rejected_attempt_cannot_replace_the_progress_snapshot(monkeypatch):
    # The standard complete_claim fencing is also the snapshot publication boundary.
    from backend.platform import task_worker
    from backend.platform.task_contracts import TaskClaim, TaskOutcome
    monkeypatch.setattr(task_worker, '_attempt_is_current', lambda *_: (None, None))
    monkeypatch.setattr(task_worker, '_cleanup_outcome', lambda *_: None)
    claim = TaskClaim('missing', 'attempt', 1, 'token', 'worker', 'owner', 'project', 'project.progress', {})
    from pathlib import Path
    outcome = TaskOutcome(Path('/unused'), 'progress.json', 'application/json', 0, '',
                          {'signature': '0' * 64, 'stages': {}}, result_only=True)
    assert complete_claim(claim, outcome) is False


def test_project_cards_and_full_overview_share_the_same_persisted_snapshot(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.engines import project_completion as calculator
    from backend.services.project_progress import progress_signature
    suffix = uuid4().hex[:12]
    with TestClient(app) as client:
        registered = client.post('/api/auth/register', json={
            'email': f'{suffix}@example.test', 'username': f'progress{suffix}', 'password': 'test-pass-1234',
        })
        assert registered.status_code == 201
        owner = registered.json()['user']['id']
        project_id = client.get('/api/v1/projects').json()[0]['id']
        with SessionLocal() as db:
            user = db.get(User, owner)
            root = project_workspace_path(db, user.username, project_id)
            stages = calculator.project_completion(root)
            db.add(ProjectProgress(project_id=project_id,
                                   signature=progress_signature(db, user, project_id, root), stages=stages))
            db.commit()
        def forbidden(*_args, **_kwargs):
            raise AssertionError('HTTP must not calculate project completion')
        monkeypatch.setattr(calculator, 'project_completion', forbidden)
        full = client.get(f'/api/v1/projects/{project_id}/summary?progress=true')
        assert full.status_code == 200
        assert full.json()['stage_completion'] == stages
        combined = {}
        for section in ('text', 'catalog', 'production'):
            response = client.get(f'/api/v1/projects/{project_id}/summary?progress=true&section={section}')
            assert response.status_code == 200
            combined.update(response.json()['stage_completion'])
        assert combined == stages


def test_concurrent_cold_sections_create_one_rebuild():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    suffix = uuid4().hex
    with SessionLocal() as db:
        user = User(username=f'concurrent-{suffix}', email=f'{suffix}@example.test', password_hash='test')
        db.add(user)
        db.flush()
        project = Project(owner_id=user.id, name='Concurrent', directory_key=f'{user.username}/book')
        db.add(project)
        db.commit()
        user_id, project_id = user.id, project.id
        root = project_workspace_path(db, user.username, project_id)
        root.mkdir(parents=True)
    barrier = Barrier(3)
    def read(section):
        with SessionLocal() as db:
            user = db.get(User, user_id)
            barrier.wait()
            return progress_summary(db, user, project_id, root, section)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(read, ('text', 'catalog', 'production')))
    assert results == [{}, {}, {}]
    with SessionLocal() as db:
        assert len(db.scalars(select(Task).where(Task.project_id == project_id)).all()) == 1
