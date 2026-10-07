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
            # Exercise the filesystem fence with intentionally conflicting manual claims.
            claim = claim_task(response.json()["id"], f"audio-test-{len(claims)}", defer_workspace_conflicts=False)
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
    (("bgm.segment", {"stem": "one"}), ("bgm.segment", {"stem": "two"}), True),
    (("bgm.segment", {"stem": "one"}), ("bgm.segment", {"stem": "one"}), False),
    (("bgm.segment", {"stem": "one"}), ("bgm.mix", {"stem": "one"}), False),
    (("bgm.segment", {"stem": "one"}), ("tts.merge", {"package": "two"}), True),
    (("bgm.segment", {"stem": "one"}), ("tts.reset", {"scripts": []}), False),
    (("tts.merge", {"package": "one"}), ("bgm.mix", {"stem": "one"}), False),
    (("tts.merge", {"package": "one"}), ("tts.merge", {"package": "one"}), False),
    (("tts.merge", {"package": "one"}), ("tts.merge", {"package": "one "}), False),
    (("tts.merge", {"package": "chapter??"}), ("tts.merge", {"package": "chapter__"}), False),
    (("tts.merge", {"package": "chapter??"}), ("bgm.mix", {"stem": "chapter__"}), False),
    (("tts.merge", {"package": "one"}), ("tts.reset", {"scripts": []}), False),
    (("tts.batch", {"scripts": []}), ("tts.merge", {"package": "one"}), False),
    (("bgm.mix", {"stem": "one"}), ("bgm.match", {"chapters": []}), False),
    (("tts.merge", {}), ("tts.merge", {"package": "one"}), False),
    (("bgm.match", {"chapters": ["one"]}), ("bgm.match", {"chapters": ["two"]}), True),
    (("bgm.match", {"chapters": ["one"]}), ("bgm.match", {"chapters": ["one"]}), False),
    (("bgm.match", {"chapters": ["one"]}), ("bgm.mix", {"stem": "one"}), False),
    (("bgm.match", {"chapters": ["one"]}), ("bgm.segment", {"stem": "two"}), True),
    (("bgm.match", {"chapters": ["one", "two"]}), ("bgm.mix", {"stem": "three"}), False),
    (("voices.foundation", {"speakers": ["A"]}), ("voices.foundation", {"speakers": ["B"]}), True),
    (("voices.foundation", {"speakers": ["A"]}), ("voices.foundation", {"speakers": ["A"]}), False),
    (("voices.foundation", {"speakers": ["A"]}), ("voices.clone", {"speakers": ["B"]}), False),
    (("voices.foundation", {"speakers": ["A"]}), ("tts.reset", {"scripts": []}), False),
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


@pytest.mark.parametrize("interruption", [None, "cancel_role", "pause_role", "crash"])
@pytest.mark.parametrize("legacy_queue", [False, True])
def test_clone_roles_share_one_model_run_and_settle_independently(audio_project, monkeypatch, interruption, legacy_queue):
    from backend.tests.test_voices import _seed_script, _seed_foundations, _stub_design_engine
    from backend.platform.models import TaskResult

    submit, workspace = audio_project
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "03_parsed_json").mkdir(exist_ok=True)
    (workspace / "04_voice_profiles").mkdir(exist_ok=True)
    names = [f"Role-{index}" for index in range(100)]
    _seed_script(workspace, {name: 1 for name in names})
    _seed_foundations(workspace, names)
    _, capture = _stub_design_engine(monkeypatch, workspace, rows_per_batch=64)
    payload = {"script": "s.json", "concurrency": 128, "execution_batch": uuid.uuid4().hex}
    if legacy_queue:
        payload.pop("execution_batch")
    leader = submit("voices.clone", {**payload, "speakers": [names[0]], "label": "克隆音频 · Role-0"})
    with SessionLocal.begin() as db:
        db.get(UserQuotaAccount, leader.owner_id).available_units = 100000
        members = [Task(owner_id=leader.owner_id, project_id=leader.project_id,
                        task_type="voices.clone", status="pending",
                        payload={**payload, "speakers": [name], "label": f"克隆音频 · {name}"})
                   for name in names[1:]]
        db.add_all(members)
        db.flush()
        ids = [leader.task_id, *(member.id for member in members)]
        duplicate = Task(owner_id=leader.owner_id, project_id=leader.project_id,
                         task_type="voices.clone", status="pending",
                         payload={**payload, "speakers": [names[0]]})
        db.add(duplicate)
        db.flush()
        duplicate_id = duplicate.id
    assert claim_task(ids[1], "another-worker") is None
    real_generate = __import__("backend.engines.voices", fromlist=["generate_voice_candidates"]).generate_voice_candidates
    settled_speakers = []
    paused = threading.Event()
    resumed = threading.Event()

    def generate(handle, *args):
        if interruption == "cancel_role":
            with SessionLocal.begin() as db:
                db.get(Task, handle.contexts[names[-1]].claim.task_id).status = "cancelling"
        if interruption == "pause_role":
            ctx = handle.contexts[names[-1]]
            real_pause = ctx._pause
            def pause(on_pause=None):
                paused.set()
                return real_pause(on_pause)
            ctx._pause = pause
            with SessionLocal.begin() as db:
                db.get(Task, ctx.claim.task_id).status = "paused"
            def resume():
                if paused.wait(3):
                    with SessionLocal.begin() as db:
                        db.get(Task, ctx.claim.task_id).status = "running"
                    resumed.set()
            threading.Thread(target=resume, daemon=True).start()
        real_settle = handle.speaker_settled
        def settle(result):
            real_settle(result)
            settled_speakers.append(result["speaker"])
            if result["speaker"] != names[0]:
                with SessionLocal() as db:
                    task = db.scalar(select(Task).where(Task.id.in_(ids),
                                         Task.payload["speakers"][0].as_string() == result["speaker"]))
                    assert task.status == "succeeded"
                    assert db.get(Task, leader.task_id).status == "running"
                if interruption == "crash":
                    raise RuntimeError("shared child interrupted after a checkpoint")
        handle.speaker_settled = settle
        return real_generate(handle, *args)
    monkeypatch.setattr("backend.engines.voices.generate_voice_candidates", generate)
    assert _run_claim_fenced(leader) == ("worker_error" if interruption == "crash" else "succeeded")
    if interruption == "pause_role":
        assert paused.is_set() and resumed.is_set()
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    if interruption == "crash":
        config = json.loads((workspace / "04_voice_profiles" / "voice_config.json").read_text())
        with SessionLocal() as db:
            completed = [db.get(Task, task_id) for task_id in ids if db.get(Task, task_id).status == "succeeded"]
            assert len(completed) == 1
            name = completed[0].payload["speakers"][0]
            assert config[name]["clone_status"] == "done"
            assert (workspace / config[name]["ref_audio"]).exists()
            assert all(db.get(Task, task_id).status in {"succeeded", "retrying"} for task_id in ids)
        return
    assert len(calls) == 100
    assert all(row["concurrency"] == 64 for row in calls)
    assert len({row["seed_arg"] for row in calls}) == 1  # one process / model load
    assert len({row["pid"] for row in calls}) == 1
    assert len({row["seed"] for row in calls}) == 2  # 64 + 36 cross-role tensor batches
    with SessionLocal() as db:
        assert db.get(Task, duplicate_id).status == "pending"
        assert all(db.get(Task, task_id).status ==
                   ("cancelled" if interruption == "cancel_role" and db.get(Task, task_id).payload["speakers"] == [names[-1]] else "succeeded")
                   for task_id in ids)
        for task_id in ids:
            if db.get(Task, task_id).status == "cancelled":
                continue
            result = db.scalar(select(TaskResult).where(TaskResult.task_id == task_id)).result
            assert result["count"] == result["ok"] == 1
            assert len(result["speakers"]) == len(result["results"]) == 1
            assert result["voice_config_path"] and result["output_dir"]
    config = json.loads((workspace / "04_voice_profiles" / "voice_config.json").read_text())
    assert all(config[name]["clone_status"] == "done" for name in names if interruption != "cancel_role" or name != names[-1])
    if interruption == "cancel_role":
        assert config[names[-1]]["type"] == "foundation"


@pytest.mark.parametrize("wait_stage", ["check", "interruptible", "completion"])
def test_cancel_clone_primary_interrupts_a_paused_member(audio_project, monkeypatch, wait_stage):
    submit, _ = audio_project
    payload = {"execution_batch": uuid.uuid4().hex, "script": "s.json"}
    primary = submit("voices.clone", {**payload, "speakers": ["A"]})
    with SessionLocal.begin() as db:
        member = Task(owner_id=primary.owner_id, project_id=primary.project_id,
                      task_type="voices.clone", payload={**payload, "speakers": ["B"]})
        db.add(member)
        db.flush()
        member_id = member.id
    parked, child_stopped = threading.Event(), threading.Event()

    def generate(handle, *args):
        ctx = handle.contexts["B"]
        original_pause = ctx._pause
        def pause(on_pause=None):
            parked.set()
            return original_pause(on_pause)
        ctx._pause = pause
        with SessionLocal.begin() as db:
            db.get(Task, member_id).status = "paused"
        if wait_stage == "check":
            handle.check()
        elif wait_stage == "interruptible":
            handle.check_interruptible(child_stopped.set)
        # Completion also waits through the member context after the child exits.
        return {}

    monkeypatch.setattr("backend.engines.voices.generate_voice_candidates", generate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(_run_claim_fenced, primary)
        try:
            assert parked.wait(3)
            if wait_stage == "interruptible":
                assert child_stopped.wait(1)
            with SessionLocal.begin() as db:
                db.get(Task, primary.task_id).status = "cancelling"
            assert running.result(timeout=3) == "cancelled"
            with SessionLocal() as db:
                assert db.get(Task, primary.task_id).status == "cancelled"
                assert db.get(Task, member_id).status == "retrying"
                assert not db.scalars(select(TaskAttempt).where(
                    TaskAttempt.task_id.in_([primary.task_id, member_id]), TaskAttempt.status == "running",
                )).all()
        finally:
            # Keep a broken implementation from leaving a test worker parked.
            with SessionLocal.begin() as db:
                row = db.get(Task, member_id)
                if row.status == "paused":
                    row.status = "running"


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


def test_bgm_analysis_claims_disjoint_chapters_but_defers_same_chapter(audio_project, monkeypatch):
    from backend.platform.task_worker import claim_fair_task, _workspace_claim_available
    from backend.platform.models import utcnow
    submit, _ = audio_project
    first = submit("bgm.segment", {"stem": "one"})
    same = submit("bgm.segment", {"stem": "one"})
    different = submit("bgm.segment", {"stem": "two"})
    with SessionLocal.begin() as db:
        for claim in (same, different):
            db.get(Task, claim.task_id).status = "pending"
            db.get(TaskAttempt, claim.attempt_id).status = "cancelled"
    assert claim_task(same.task_id, "same-bgm") is None
    with SessionLocal() as db:
        assert not _workspace_claim_available(db, db.get(Task, same.task_id), utcnow())
        assert _workspace_claim_available(db, db.get(Task, different.task_id), utcnow())
    original = task_worker._workspace_claim_eligibility
    monkeypatch.setattr(task_worker, "_workspace_claim_eligibility",
                        lambda now: original(now) & (Task.owner_id == first.owner_id))
    claimed = claim_fair_task("parallel-bgm", task_types=("bgm.segment",))
    assert claimed and claimed.task_id == different.task_id


def test_foundation_claims_allow_distinct_characters(audio_project, monkeypatch):
    from backend.platform.models import utcnow
    submit, _ = audio_project
    first = submit("voices.foundation", {"speakers": ["A"]})
    same = submit("voices.foundation", {"speakers": ["A"]})
    different = submit("voices.foundation", {"speakers": ["B"]})
    with SessionLocal.begin() as db:
        for claim in (same, different):
            db.get(Task, claim.task_id).status = "pending"
            db.get(TaskAttempt, claim.attempt_id).status = "cancelled"
    assert claim_task(same.task_id, "same-foundation") is None
    with SessionLocal() as db:
        assert not task_worker._workspace_claim_available(db, db.get(Task, same.task_id), utcnow())
        assert task_worker._workspace_claim_available(db, db.get(Task, different.task_id), utcnow())
    original = task_worker._workspace_claim_eligibility
    monkeypatch.setattr(task_worker, "_workspace_claim_eligibility",
                        lambda now: original(now) & (Task.owner_id == first.owner_id))
    claimed = task_worker.claim_fair_task("parallel-foundation", task_types=("voices.foundation",))
    assert claimed and claimed.task_id == different.task_id


def test_eight_foundations_overlap_and_preserve_every_checkpoint(audio_project, monkeypatch):
    from backend.engines import voices
    submit, workspace = audio_project
    names = [f"role-{index}" for index in range(8)]
    script = workspace / "03_parsed_json" / "roles.json"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(json.dumps([{"speaker": name, "text": "hello"} for name in names]))
    claims = [submit("voices.foundation", {"speakers": [name], "script": script.name,
                                          "config": {"generation": {"max_concurrency": 8}}}) for name in names]
    overlap = threading.Barrier(8)

    def persona(handle, llm, system, user, speaker, script, bands):
        overlap.wait(timeout=10)
        return f"voice for {speaker}", "hello", "male"

    monkeypatch.setattr(voices, "_llm_persona", persona)
    # A metadata failure after publication must not revert a successful sibling.
    complete = task_worker.complete_claim

    def fail_one(claim, outcome):
        if claim.task_id == claims[0].task_id:
            raise RuntimeError("metadata failure")
        return complete(claim, outcome)

    monkeypatch.setattr(task_worker, "complete_claim", fail_one)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_run_claim_fenced, claims))
    assert results == ["worker_error", *(["succeeded"] * 7)]
    profiles = json.loads((workspace / "04_voice_profiles" / "voice_config.json").read_text())
    assert set(profiles) == set(names)
    assert all(profiles[name]["description"] == f"voice for {name}" for name in names)


def test_foundation_row_dispatch_respects_configured_role_limit(audio_project, monkeypatch):
    submit, _ = audio_project
    payload = {"config": {"generation": {"max_concurrency": 2}}}
    first = submit("voices.foundation", {**payload, "speakers": ["A"]})
    second = submit("voices.foundation", {**payload, "speakers": ["B"]})
    # Submit the third pending row through the API without manually claiming it.
    with SessionLocal.begin() as db:
        third = Task(owner_id=first.owner_id, project_id=first.project_id,
                     task_type="voices.foundation", status="pending",
                     payload={**payload, "speakers": ["C"]})
        db.add(third)
        db.flush()
        third_id = third.id
    assert claim_task(third_id, "over-role-limit") is None
    original = task_worker._workspace_claim_eligibility
    monkeypatch.setattr(task_worker, "_workspace_claim_eligibility",
                        lambda now: original(now) & (Task.owner_id == first.owner_id))
    assert task_worker.claim_fair_task("over-role-limit", task_types=("voices.foundation",)) is None
    with SessionLocal.begin() as db:
        db.get(Task, second.task_id).status = "succeeded"
        db.get(TaskAttempt, second.attempt_id).status = "succeeded"
    claimed = claim_task(third_id, "free-role-slot")
    assert claimed is not None
    with SessionLocal.begin() as db:
        db.get(Task, third_id).status = "cancelled"
        db.get(TaskAttempt, claimed.attempt_id).status = "cancelled"


def test_match_claims_allow_disjoint_chapters_and_defer_overlapping_legacy_batches(audio_project, monkeypatch):
    from backend.platform.models import utcnow
    submit, _ = audio_project
    first = submit("bgm.match", {"chapters": ["one"]})
    same = submit("bgm.match", {"chapters": ["one"]})
    different = submit("bgm.match", {"chapters": ["two"]})
    legacy = submit("bgm.match", {"chapters": ["two", "three"]})
    with SessionLocal.begin() as db:
        for claim in (same, different, legacy):
            db.get(Task, claim.task_id).status = "pending"
            db.get(TaskAttempt, claim.attempt_id).status = "cancelled"
    assert claim_task(same.task_id, "same-match") is None
    assert claim_task(legacy.task_id, "legacy-match") is None
    with SessionLocal() as db:
        assert not task_worker._workspace_claim_available(db, db.get(Task, same.task_id), utcnow())
        assert task_worker._workspace_claim_available(db, db.get(Task, different.task_id), utcnow())
    original = task_worker._workspace_claim_eligibility
    monkeypatch.setattr(task_worker, "_workspace_claim_eligibility",
                        lambda now: original(now) & (Task.owner_id == first.owner_id))
    claim = task_worker.claim_fair_task("parallel-match", task_types=("bgm.match",))
    assert claim and claim.task_id == different.task_id


def test_disjoint_matching_overlaps_without_losing_shared_assignments(audio_project, monkeypatch):
    from backend.engines import bgm
    submit, workspace = audio_project
    (workspace / "08_bgm").mkdir(parents=True, exist_ok=True)
    claims = [submit("bgm.match", {"chapters": [stem], "mode": "random"}) for stem in ("one", "two")]
    monkeypatch.setattr(bgm.music_engine, "load_index", lambda: {"tracks": {}})
    monkeypatch.setattr(bgm, "list_chapter_stems", lambda layout: ["one", "two"])
    overlap = threading.Barrier(2)
    original = bgm.match_stems
    def matching(*args, **kwargs):
        overlap.wait(timeout=5)
        return original(*args, **kwargs)
    monkeypatch.setattr(bgm, "match_stems", matching)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run_claim_fenced, claim) for claim in claims]
        assert [future.result(timeout=10) for future in futures] == ["succeeded", "succeeded"]
    data = json.loads((workspace / "08_bgm" / "bgm_assignments.json").read_text("utf-8"))
    assert set(data["chapters"]) == {"one", "two"}
