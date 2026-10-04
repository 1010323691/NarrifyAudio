from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from backend.platform import task_engine_support
from backend.platform.task_contracts import TaskCancelledError, TaskClaim


@pytest.mark.parametrize('cancel_after', [1, 2])
def test_cancelled_progress_callback_removes_all_attempt_outputs(tmp_path, monkeypatch, cancel_after):
    attempt_dir = tmp_path / 'user' / 'project' / '.tasks' / 'task' / 'attempt'
    db = SimpleNamespace(get=lambda _model, _owner_id: SimpleNamespace(username='user'))
    claim = TaskClaim(
        task_id='task', attempt_id='attempt', attempt_no=1, lease_token='lease',
        worker_id='worker', owner_id='owner', project_id='project',
        task_type='text.format', payload={},
    )
    monkeypatch.setattr(task_engine_support, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(
        task_engine_support,
        'task_attempt_path',
        lambda *_args: attempt_dir / _args[-1],
    )
    progress_calls = 0

    def report_progress(_value):
        nonlocal progress_calls
        progress_calls += 1
        if progress_calls == cancel_after:
            raise TaskCancelledError()

    with pytest.raises(TaskCancelledError):
        task_engine_support.write_task_outcome(
            claim, 'main.txt', 'text/plain', b'main', {},
            additional_outputs=[('part-a.txt', 'text/plain', b'a'), ('part-b.txt', 'text/plain', b'b')],
            on_progress=report_progress,
        )

    assert not attempt_dir.exists()
