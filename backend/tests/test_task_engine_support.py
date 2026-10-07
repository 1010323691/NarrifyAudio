from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from backend.platform import task_engine_support
from backend.platform.task_contracts import TaskCancelledError, TaskClaim


@pytest.mark.parametrize('cancel_after', [1, 2])
def test_cancelled_progress_callback_removes_all_attempt_outputs(tmp_path, monkeypatch, cancel_after):
    attempt_dir = tmp_path / 'user' / 'project' / '00_temp' / 'tasks' / 'task' / 'attempt'
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


@pytest.mark.parametrize("kind", ["foundation", "clone"])
def test_single_role_results_only_repeat_the_selected_character(monkeypatch, kind):
    from backend.platform import engine_task_executor as executor
    from backend.engines import voices
    result = {"count": 1, "speakers": [f"role-{i}" for i in range(1000)], "results": [{"speaker": "role-0", "ok": True}]}
    monkeypatch.setattr(voices, "prepare_foundations" if kind == "foundation" else "generate_voice_candidates", lambda *args: result)
    runner = executor._run_voices_foundation if kind == "foundation" else executor._run_voices_clone
    assert runner(None, None, {"speakers": ["role-0"]}, [], [])["speakers"] == ["role-0"]
    assert len(result["speakers"]) == 1000  # Legacy multi-role calls keep their old shape.
    assert len(runner(None, None, {"speakers": None}, [], [])["speakers"]) == 1000


def test_single_chapter_matching_result_does_not_repeat_the_whole_book(monkeypatch):
    from backend.platform import engine_task_executor as executor
    from backend.engines import bgm
    assignments = {"chapters": {f"chapter-{i}": {"music": None} for i in range(1000)}}
    monkeypatch.setattr(executor, "get_or_prepare_layout", lambda: None)
    monkeypatch.setattr(bgm, "match_stems", lambda *args, **kwargs: {"mode": "random", "matched": 0, "no_bgm": 1, "assignments": assignments})
    result = executor._run_bgm_match(None, None, {"chapters": ["chapter-0"]}, [], [])
    assert set(result["assignments"]["chapters"]) == {"chapter-0"}
    assert len(assignments["chapters"]) == 1000
