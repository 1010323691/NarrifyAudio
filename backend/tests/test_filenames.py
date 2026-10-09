from __future__ import annotations

import io
import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.filenames import (
    safe_filename, legacy_storage_name, package_stem,
    MAX_NAME_BYTES, workspace_audio_identity,
)
from backend.platform.storage import available_file_name, safe_display_name, storage_username


@pytest.fixture(scope="module")
def filename_client():
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.platform.database import initialize_schema
    initialize_schema()
    with TestClient(app) as client:
        yield client


def _register(filename_client):
    from backend.platform.database import SessionLocal
    from backend.platform.models import UserQuotaAccount
    response = filename_client.post("/api/auth/register", json={
        "email": f"{uuid.uuid4()}@example.test", "username": "file" + uuid.uuid4().hex[:12],
        "password": "test-pass-1234", "display_name": "Filename test",
    })
    assert response.status_code == 201, response.text
    account = response.json()
    project = filename_client.get("/api/v1/projects/active").json()["project_id"]
    with SessionLocal.begin() as db:
        db.get(UserQuotaAccount, account["user"]["id"]).available_units = 100
    return account, project


@pytest.mark.parametrize("name", ["你好，世界—再见！.txt", "【序章】（第一幕）.json", "café 🎧.mp3"])
def test_storage_preserves_valid_punctuation(name):
    assert safe_display_name(name) == name
    assert safe_display_name(name) != legacy_storage_name(name)


@pytest.mark.parametrize("name", ["CON.txt", "nul.mp3", "COM¹.wav", "LPT9.json"])
def test_device_names_are_escaped_with_extensions(name):
    assert safe_filename(name) == "_" + name
    assert safe_filename(safe_filename(name)) == "_" + name


def test_forbidden_characters_are_replaced_individually():
    assert safe_filename('a??b\x00\t.txt') == "a__b__.txt"
    assert safe_filename('  ...  ') == "upload.bin"
    assert safe_filename('file.txt. ') == 'file.txt'


@pytest.mark.parametrize("stem", ["a" * 180, "章" * 180, "🎧" * 180])
def test_long_names_keep_extension_and_remain_unique_and_idempotent(tmp_path, stem):
    first = safe_filename(stem + "甲.txt")
    second = safe_filename(stem + "乙.txt")
    assert first != second
    assert len(first) <= 180 and len(first.encode("utf-8")) <= MAX_NAME_BYTES
    assert first.endswith(".txt") and safe_filename(first) == first
    (tmp_path / first).write_text("first")
    collision = available_file_name(tmp_path, stem + "甲.txt")
    assert collision != first and collision.endswith(" (2).txt")
    assert len(collision.encode("utf-8")) <= MAX_NAME_BYTES
    assert safe_filename(collision) == collision
    (tmp_path / collision).write_text("second")


def test_package_naming_and_lock_identity_share_canonical_target():
    from backend.core.paths import merged_audio_filename
    from backend.engines.tts_manifest import _safe_package_name, merged_output_paths
    package = "第" * 180 + "??"
    name = merged_audio_filename(package)
    assert name == _safe_package_name(package) + ".mp3"
    assert "\x00" not in name and len(name.encode()) <= MAX_NAME_BYTES
    assert workspace_audio_identity("tts.merge", {"package": package}) == name.casefold()
    assert "\x00" not in merged_audio_filename("a\x00b")
    assert merged_audio_filename("...") == "audiobook.mp3"
    paths = merged_output_paths(SimpleNamespace(audio_merge=Path("/ws")), package)
    assert paths[0].name == name
    assert any(p.name == package.replace("?", "_") + ".mp3" for p in paths)


@pytest.mark.parametrize("name", ["reader.", "reader..", "con.txt", "lpt1.foo"])
def test_new_usernames_cannot_alias_storage_roots(name):
    from backend.services.user_provisioning import ProvisioningError, normalize_username
    with pytest.raises(ProvisioningError) as error:
        normalize_username(name, "valid@example.test")
    assert error.value.status_code == 422
    # Existing accounts remain at their existing roots.
    assert storage_username("reader.") == "reader"


def test_legacy_alias_resolution_refuses_ambiguous_records():
    from backend.services.script_parse_state import _resolve_split_name
    rows = {
        "甲_乙.txt": SimpleNamespace(id="legacy", object_key="02_split_text/甲_乙.txt"),
        "甲:乙.txt": SimpleNamespace(id="other", object_key="02_split_text/甲:乙.txt"),
    }
    assert _resolve_split_name(rows, "甲?乙.txt") is rows["甲_乙.txt"]
    rows["甲:乙.txt"].object_key = "02_split_text/甲_乙.txt"
    assert _resolve_split_name(rows, "甲?乙.txt") is None
    assert _resolve_split_name(rows, "甲:乙.txt") is rows["甲:乙.txt"]


def test_new_punctuation_names_do_not_alias_each_other():
    from backend.services.script_parse_state import _resolve_split_name
    rows = {"甲，乙.txt": SimpleNamespace(id="comma", object_key="02_split_text/甲，乙.txt")}
    assert _resolve_split_name(rows, "甲—乙.txt") is None


@pytest.mark.parametrize("previous", [False, True])
def test_publication_rejects_collisions_before_writing(filename_client, previous):
    from backend.platform.database import SessionLocal
    from backend.platform.models import Task, ProjectFile
    from backend.platform.task_worker import claim_task, complete_claim
    from backend.platform.task_engine_support import write_task_outcome
    from backend.platform.task_contracts import TaskExecutionError
    account, project = _register(filename_client)
    existing_path = None
    if previous:
        from backend.platform.storage import project_workspace_path
        from backend.platform.models import Project
        with SessionLocal.begin() as db:
            workspace = project_workspace_path(db, account["user"]["username"], project)
            existing_path = workspace / "02_split_text" / "a_.txt"
            existing_path.parent.mkdir(parents=True, exist_ok=True)
            existing_path.write_bytes(b"original")
            db.add(ProjectFile(owner_id=account["user"]["id"], project_id=project,
                               original_name="a*.txt", object_key=db.get(Project, project).directory_key + "/02_split_text/a_.txt",
                               content_type="text/plain", size_bytes=8, sha256=hashlib.sha256(b"original").hexdigest(), kind="artifact"))
    response = filename_client.post("/api/v1/tasks", headers={"X-CSRF-Token": account["csrf_token"]}, json={
        "project_id": project, "task_type": "tts.merge", "payload": {"package": "chapter"},
        "idempotency_key": uuid.uuid4().hex,
    })
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    claim = claim_task(task_id, "filename-test")
    assert claim is not None
    name = "a?.txt" if previous else "A?.txt"
    outcome = write_task_outcome(claim, name, "text/plain", b"first", {},
                                 publish_module="02_split_text", additional_outputs=[] if previous else [("a*.txt", "text/plain", b"second")])
    assert outcome.output_name == name
    with pytest.raises(TaskExecutionError) as error:
        complete_claim(claim, outcome)
    assert error.value.code == "output_name_collision"
    from sqlalchemy import select
    with SessionLocal.begin() as db:
        assert len(db.scalars(select(ProjectFile).where(ProjectFile.project_id == project)).all()) == int(previous)
        db.get(Task, task_id).status = "cancelled"
    assert not outcome.temp_path.exists()
    if existing_path is not None:
        assert existing_path.read_bytes() == b"original"


def test_submission_persists_server_computed_unicode_target(filename_client):
    from backend.platform.database import SessionLocal
    from backend.platform.models import Task
    account, project = _register(filename_client)
    response = filename_client.post("/api/v1/tasks", headers={"X-CSRF-Token": account["csrf_token"]}, json={
        "project_id": project, "task_type": "tts.merge", "payload": {
            "package": "Straße??", "_audio_identity": "forged.mp3",
        }, "idempotency_key": uuid.uuid4().hex,
    })
    assert response.status_code == 201, response.text
    with SessionLocal.begin() as db:
        task = db.get(Task, response.json()["id"])
        assert task.payload["_audio_identity"] == "strasse__.mp3"
        task.status = "cancelled"


def test_registration_rejects_collision_with_legacy_username(filename_client):
    from backend.platform.database import SessionLocal
    from backend.platform.models import User
    username = "legacy" + uuid.uuid4().hex[:8]
    with SessionLocal.begin() as db:
        db.add(User(email=f"{uuid.uuid4()}@example.test", username=username + ".",
                    display_name="Legacy", password_hash="unused", role="user"))
    response = filename_client.post("/api/auth/register", json={
        "email": f"{uuid.uuid4()}@example.test", "username": username,
        "password": "test-pass-1234", "display_name": "New",
    })
    assert response.status_code == 409


@pytest.mark.parametrize("same_source", [True, False])
def test_legacy_republication_requires_matching_source(filename_client, same_source):
    from backend.platform.database import SessionLocal
    from backend.platform.models import Task, TaskResult, ProjectFile, Project
    from backend.platform.storage import object_path, configured_storage_root
    from backend.platform.task_worker import claim_task, complete_claim
    from backend.platform.task_engine_support import write_task_outcome
    from backend.platform.task_contracts import TaskExecutionError
    account, project = _register(filename_client)
    response = filename_client.post("/api/v1/tasks", headers={"X-CSRF-Token": account["csrf_token"]}, json={
        "project_id": project, "task_type": "tts.merge", "payload": {"package": "chapter"},
        "idempotency_key": uuid.uuid4().hex,
    })
    assert response.status_code == 201
    task_id = response.json()["id"]
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        task.task_type = "text.format"
        task.payload = {"input_file_id": "current-source"}
        key = db.get(Project, project).directory_key + "/02_split_text/a_.txt"
        file = ProjectFile(owner_id=task.owner_id, project_id=project, original_name="a_.txt",
                           object_key=key, content_type="text/plain", size_bytes=8,
                           sha256=hashlib.sha256(b"original").hexdigest(), kind="artifact")
        db.add(file)
        old = Task(owner_id=task.owner_id, project_id=project, task_type="text.format", status="succeeded",
                   payload={"input_file_id": "current-source" if same_source else "other-source"},
                   idempotency_key=uuid.uuid4().hex)
        db.add(old)
        db.flush()
        db.add(TaskResult(task_id=old.id, result={"file_id": file.id, "object_key": key}))
        file_id = file.id
        path = object_path(key, configured_storage_root(db))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
    claim = claim_task(task_id, "filename-test")
    assert claim is not None
    outcome = write_task_outcome(claim, "a?.txt", "text/plain", b"new", {}, publish_module="02_split_text")
    if same_source:
        assert complete_claim(claim, outcome) is True
        assert path.read_bytes() == b"new"
        with SessionLocal() as db:
            assert db.get(ProjectFile, file_id).original_name == "a?.txt"
    else:
        with pytest.raises(TaskExecutionError, match="名称冲突"):
            complete_claim(claim, outcome)
        assert path.read_bytes() == b"original"
        with SessionLocal.begin() as db:
            db.get(Task, task_id).status = "cancelled"


@pytest.mark.parametrize("file_exists", [False, True])
def test_deleted_target_case_change_reuses_row_only_without_disk_conflict(filename_client, file_exists):
    from backend.platform.database import SessionLocal
    from backend.platform.models import Task, ProjectFile, Project, utcnow
    from backend.platform.storage import object_path, configured_storage_root
    from backend.platform.task_worker import claim_task, complete_claim
    from backend.platform.task_engine_support import write_task_outcome
    from backend.platform.task_contracts import TaskExecutionError
    from sqlalchemy import select
    account, project = _register(filename_client)
    response = filename_client.post("/api/v1/tasks", headers={"X-CSRF-Token": account["csrf_token"]}, json={
        "project_id": project, "task_type": "tts.merge", "payload": {"package": "chapter"},
        "idempotency_key": uuid.uuid4().hex,
    })
    assert response.status_code == 201
    task_id = response.json()["id"]
    with SessionLocal.begin() as db:
        task = db.get(Task, task_id)
        task.task_type = "book.split"
        key = db.get(Project, project).directory_key + "/02_split_text/第 1 章 title.txt"
        file = ProjectFile(owner_id=task.owner_id, project_id=project, original_name="第 1 章 title.txt",
                           object_key=key, content_type="text/plain", size_bytes=8,
                           sha256=hashlib.sha256(b"original").hexdigest(), kind="artifact", deleted_at=utcnow())
        db.add(file)
        db.flush()
        file_id = file.id
        path = object_path(key, configured_storage_root(db))
        if file_exists:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"original")
    claim = claim_task(task_id, "filename-test")
    assert claim is not None
    outcome = write_task_outcome(claim, "第 1 章 Title.txt", "text/plain", b"new", {}, publish_module="02_split_text")
    if file_exists:
        with pytest.raises(TaskExecutionError, match="名称冲突"):
            complete_claim(claim, outcome)
        assert path.read_bytes() == b"original"
        with SessionLocal.begin() as db:
            db.get(Task, task_id).status = "cancelled"
    else:
        assert complete_claim(claim, outcome) is True
        assert path.read_bytes() == b"new"
        with SessionLocal() as db:
            files = list(db.scalars(select(ProjectFile).where(ProjectFile.project_id == project)))
            assert len(files) == 1 and files[0].id == file_id
            assert files[0].original_name == "第 1 章 Title.txt"
            assert files[0].deleted_at is None
        # Subsequent splitting must retain this actual disk casing instead of
        # treating the revived output as a stale chapter and removing it.
        with SessionLocal.begin() as db:
            repeat = Task(owner_id=account["user"]["id"], project_id=project,
                          task_type="book.split", payload={}, idempotency_key=uuid.uuid4().hex)
            db.add(repeat)
            db.flush()
            repeat_id = repeat.id
        repeat_claim = claim_task(repeat_id, "filename-test")
        assert repeat_claim is not None
        repeat_outcome = write_task_outcome(repeat_claim, "第 1 章 Title.txt", "text/plain", b"again", {},
                                           publish_module="02_split_text")
        assert complete_claim(repeat_claim, repeat_outcome) is True
        assert path.read_bytes() == b"again"
        with SessionLocal() as db:
            assert db.get(ProjectFile, file_id).deleted_at is None


def test_completion_queries_only_targets_and_never_scans_unique_artifact_directory(filename_client, monkeypatch):
    from sqlalchemy import event, insert
    from backend.platform.database import SessionLocal
    from backend.platform.models import Project, ProjectFile
    from backend.platform.task_worker import claim_task, complete_claim
    from backend.platform.task_engine_support import write_task_outcome
    from backend.platform import task_worker
    account, project = _register(filename_client)
    with SessionLocal.begin() as db:
        prefix = db.get(Project, project).directory_key
        db.execute(insert(ProjectFile), [dict(owner_id=account["user"]["id"], project_id=project,
            original_name=f"old-{i}.json", object_key=f"{prefix}/03_parsed_json/old-{i}.json",
            size_bytes=1, sha256="a" * 64, kind="artifact") for i in range(5000)])
    statements = []
    engine = SessionLocal.kw["bind"]
    capture = lambda c, cur, statement, *args: statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        for module in ("03_parsed_json", None):
            response = filename_client.post("/api/v1/tasks", headers={"X-CSRF-Token": account["csrf_token"]}, json={
                "project_id": project, "task_type": "tts.merge", "payload": {"package": "chapter"},
                "idempotency_key": uuid.uuid4().hex})
            assert response.status_code == 201
            claim = claim_task(response.json()["id"], "indexed-publication")
            outcome = write_task_outcome(claim, "new.json", "application/json", b"{}", {}, publish_module=module)
            monkeypatch.setattr(Path, "iterdir", lambda *_: pytest.fail("completion must not enumerate output directories"))
            assert complete_claim(claim, outcome)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    catalog_selects = [statement for statement in statements if statement.lstrip().startswith("SELECT") and "FROM project_files" in statement]
    assert catalog_selects
    assert all("object_key_normalized" in statement.split("WHERE", 1)[-1] for statement in catalog_selects)
