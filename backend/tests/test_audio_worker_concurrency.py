"""Exercise project locks and the real fenced merge/publication execution path."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import app
from backend.core import concurrency
from backend.engines import merge
from backend.platform import task_worker
from backend.platform.database import SessionLocal
from backend.platform.models import Task, TaskAttempt, User, UserQuotaAccount
from backend.platform.storage import project_workspace_path
from backend.platform.task_worker import _run_claim_fenced, _workspace_engine_lock, claim_task


@pytest.fixture
def audio_project():
    with TestClient(app) as client:
        username = f"audio{uuid.uuid4().hex[:12]}"
        response = client.post("/api/auth/register", json={
            "email": f"{uuid.uuid4()}@example.com", "username": username,
            "password": "test-pass-1234", "display_name": "Audio concurrency",
        })
        assert response.status_code == 201, response.text
        headers = {"X-CSRF-Token": response.json()["csrf_token"]}
        response = client.post("/api/v1/projects", headers=headers, json={"name": "Audio locks"})
        assert response.status_code == 201, response.text
        project_id = response.json()["id"]
        with SessionLocal.begin() as db:
            workspace = project_workspace_path(db, username, project_id)
            user = db.scalar(select(User).where(User.username == username))
            db.get(UserQuotaAccount, user.id).available_units = 100
        claims = []

        def submit(task_type, payload):
            response = client.post("/api/v1/tasks", headers=headers, json={
                "project_id": project_id, "task_type": task_type, "payload": payload,
                "idempotency_key": uuid.uuid4().hex,
            })
            assert response.status_code == 201, response.text
            claim = claim_task(response.json()["id"], f"audio-test-{len(claims)}")
            assert claim is not None
            claims.append(claim)
            return claim

        yield submit, workspace
        with SessionLocal.begin() as db:
            for claim in claims:
                task = db.get(Task, claim.task_id)
                attempt = db.get(TaskAttempt, claim.attempt_id)
                if task.status == "running":
                    task.status = "cancelled"
                    attempt.status = "cancelled"


@pytest.mark.parametrize("first,second,parallel", [
    (("tts.merge", {"package": "one"}), ("tts.merge", {"package": "two"}), True),
    (("bgm.mix", {"stem": "one"}), ("bgm.mix", {"stem": "two"}), True),
    (("tts.merge", {"package": "one"}), ("bgm.mix", {"stem": "one"}), False),
    (("tts.merge", {"package": "one"}), ("tts.merge", {"package": "one"}), False),
    (("tts.merge", {"package": "one"}), ("tts.merge", {"package": "one "}), False),
    (("tts.merge", {"package": "chapter??"}), ("tts.merge", {"package": "chapter__"}), False),
    (("tts.merge", {"package": "chapter??"}), ("bgm.mix", {"stem": "chapter__"}), False),
    (("tts.merge", {"package": "one"}), ("tts.reset", {"scripts": []}), False),
    (("tts.batch", {"scripts": []}), ("tts.merge", {"package": "one"}), False),
    (("bgm.mix", {"stem": "one"}), ("bgm.match", {"chapters": []}), False),
    (("tts.merge", {}), ("tts.merge", {"package": "one"}), False),
])
def test_audio_project_lock_preserves_conflicting_writers(audio_project, first, second, parallel):
    submit, _ = audio_project
    claims = [submit(*first), submit(*second)]
    first_entered, second_entered, release = threading.Event(), threading.Event(), threading.Event()

    def hold(claim, entered):
        with _workspace_engine_lock(claim) as active:
            assert active
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(hold, claims[0], first_entered)
        try:
            assert first_entered.wait(3)
            second_future = pool.submit(hold, claims[1], second_entered)
            assert second_entered.wait(1 if parallel else 0.2) is parallel
        finally:
            release.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)
    assert second_entered.is_set()


def test_same_project_merges_overlap_through_fenced_execution_and_publication(audio_project, monkeypatch):
    submit, workspace = audio_project
    claims = []
    for package in ("one", "two"):
        directory = workspace / "05_audio_chunk" / package
        directory.mkdir(parents=True)
        segment = directory / "0000.mp3"
        segment.write_bytes(b"segment")
        (directory / "manifest.json").write_text(json.dumps([
            {"index": 0, "path": str(segment), "ok": True, "speaker": "A", "text": "test"},
        ]), encoding="utf-8")
        claims.append(submit("tts.merge", {"package": package}))
    writer = submit("tts.reset", {"scripts": []})
    gate = concurrency.ConcurrencyGate()
    gate.set_limit(2)
    monkeypatch.setattr(concurrency, "_merge_gate", gate)
    overlap = threading.Barrier(2)
    monkeypatch.setattr(merge, "resolve_engine", lambda: (Path("fake-python"), Path("fake-worker")))

    def run_subprocess(cmd, handle, on_line, *, temp_files=(), **kwargs):
        overlap.wait(timeout=5)
        out = Path(cmd[cmd.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"audio" * 1024)
        on_line(f"[result] {out}")
        for temp in temp_files:
            temp.unlink(missing_ok=True)

    monkeypatch.setattr(merge, "run_tts_subprocess", run_subprocess)
    publication_ready, release_publication, writer_entered = (
        threading.Event(), threading.Event(), threading.Event()
    )
    publication_overlap = threading.Barrier(2)
    original_complete = task_worker.complete_claim

    def complete(claim, outcome):
        publication_overlap.wait(timeout=5)
        publication_ready.set()
        assert release_publication.wait(5)
        return original_complete(claim, outcome)

    def enter_writer():
        with _workspace_engine_lock(writer) as active:
            assert active
            writer_entered.set()

    monkeypatch.setattr(task_worker, "complete_claim", complete)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_run_claim_fenced, claim) for claim in claims]
        try:
            assert publication_ready.wait(5)
            writer_future = pool.submit(enter_writer)
            assert not writer_entered.wait(0.2)
        finally:
            release_publication.set()
        assert [future.result(timeout=15) for future in futures] == ["succeeded", "succeeded"]
        writer_future.result(timeout=5)
    assert writer_entered.is_set()
    assert gate.active == 0
    assert all((workspace / "06_audio_merge" / f"{package}.mp3").is_file()
               for package in ("one", "two"))
    with SessionLocal() as db:
        assert all(db.get(Task, claim.task_id).status == "succeeded" for claim in claims)
