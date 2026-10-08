"""Complete inventory and resource operations through the durable Worker boundary."""
from __future__ import annotations

import io
import json
import os
import time
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import app
from backend.platform.database import SessionLocal
from backend.platform.models import ProjectFile, Task, TaskResult, utcnow
from backend.core.safe_filesystem import file_identity
from backend.platform.resource_inventory import internal_path
from backend.platform.storage import configured_storage_root
from backend.platform.task_worker import claim_task, complete_claim, execute_claim
from backend.platform.task_contracts import TaskExecutionError


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as value:
        yield value


@pytest.fixture
def workspace(client):
    suffix = uuid.uuid4().hex[:12]
    registered = client.post("/api/auth/register", json={
        "email": f"resource{suffix}@example.test", "username": f"resource{suffix}", "password": "test-pass-1234",
    })
    assert registered.status_code == 201, registered.text
    body = registered.json()
    project = client.get("/api/v1/projects").json()[0]
    with SessionLocal() as db:
        root = configured_storage_root(db) / project["directory_key"]
    return {"csrf": body["csrf_token"], "owner": body["user"]["id"], "project": project["id"], "root": root}


def _submit(client, workspace, task_type, payload):
    response = client.post("/api/v1/tasks", headers={"X-CSRF-Token": workspace["csrf"]}, json={
        "project_id": workspace["project"], "task_type": task_type, "payload": payload,
        "idempotency_key": uuid.uuid4().hex,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _run(task_id):
    claim = claim_task(task_id, "resource-test-worker", lease_seconds=600)
    assert claim is not None
    outcome = execute_claim(claim)
    assert complete_claim(claim, outcome)
    with SessionLocal() as db:
        return db.get(Task, task_id).result.result


def _scan(client, workspace):
    task_id = _submit(client, workspace, "resources.scan", {"project_ids": [workspace["project"]]})
    return _run(task_id)["snapshots"][0]


def _write(root: Path, relative: str, content: bytes):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _delivery(workspace, relative, task_type="audio.cut", status="succeeded"):
    path = workspace["root"] / relative
    with SessionLocal() as db:
        task = Task(owner_id=workspace["owner"], project_id=workspace["project"], task_type=task_type, status=status, finished_at=utcnow())
        db.add(task)
        db.flush()
        db.add(TaskResult(task_id=task.id, result={"deliveries": [{"relative_path": relative, "identity": list(file_identity(path.stat()))}]}))
        db.commit()
        return task.id


@pytest.mark.parametrize("relative", ["01_input/book.txt", "02_split_text/chapter.txt", "03_parsed_json/chapter.json", "04_voice_profiles/reference.wav", "05_audio_chunk/segment.wav", "08_bgm/timelines/chapter.json", "07_output/untracked.mp3", "config/setting.json"])
def test_production_resources_cannot_download_or_package_even_with_forged_scope(client, workspace, relative):
    _write(workspace["root"], relative, b"{}")
    _scan(client, workspace)
    files = client.get("/api/v1/resources/entries", params={"category": "system" if relative.startswith("config/") else "all"}).json()["items"]
    item = files[0]
    assert not item["can_download"] and not item["can_package"]
    assert item["resource_role"] == "production"
    assert client.get(f"/api/v1/resources/files/{item['id']}/download").status_code == 403
    for payload in ({"files": [{"resource_id": item["id"], "snapshot_id": item["snapshot_id"]}]}, {"scope": {"category": "system" if relative.startswith("config/") else "all", "snapshots": [{"project_id": workspace["project"], "snapshot_id": item["snapshot_id"]}]}}):
        response = client.post("/api/v1/tasks", headers={"X-CSRF-Token": workspace["csrf"]}, json={"project_id": workspace["project"], "task_type": "resources.package", "payload": payload, "idempotency_key": uuid.uuid4().hex})
        assert response.status_code == 403, response.text
    if not relative.startswith("config/"):
        module, name = relative.split("/", 1)
        assert client.get(f"/api/files/download/{module}/{name}").status_code == 403
        response = client.get(f"/api/files/preview/{module}/{name}")
        assert response.status_code == 200 and response.headers["content-disposition"].startswith("inline")


def test_delivery_filter_counts_only_verified_products_and_blocks_old_zips(client, workspace):
    for relative, kind in [("06_audio_merge/旁白.mp3", "tts.merge"), ("08_bgm/混音.mp3", "bgm.mix"), ("07_output/分集.wav", "audio.cut")]:
        _write(workspace["root"], relative, b"completed audio")
        _delivery(workspace, relative, kind)
    _write(workspace["root"], "05_audio_chunk/segment.wav", b"middle")
    _write(workspace["root"], "07_output/untracked.wav", b"unknown")
    _write(workspace["root"], "07_output/failed.wav", b"partial")
    _delivery(workspace, "07_output/failed.wav", status="failed")
    snapshot = _scan(client, workspace)
    products = client.get("/api/v1/resources/entries", params={"category": "deliverables"}).json()
    assert products["total"] == 3 and all(item["can_download"] for item in products["items"])
    assert client.get("/api/v1/resources/entries", params={"category": "all", "role": "production"}).json()["total"] == 3
    assert client.get("/api/v1/resources").json()["projects"][0]["delivery_count"] == 3
    for item in products["items"]:
        assert client.get(f"/api/v1/resources/files/{item['id']}/download").status_code == 200
        assert client.get(f"/api/files/download/{item['relative_path']}").status_code == 200
    task_id = _submit(client, workspace, "resources.package", {"scope": {"category": "deliverables", "snapshots": [{"project_id": workspace["project"], "snapshot_id": snapshot["snapshot_id"]}]}})
    assert _run(task_id)["file_count"] == 3
    with SessionLocal() as db:
        record = db.get(TaskResult, task_id)
        data = dict(record.result)
        data.pop("delivery_policy")
        record.result = data
        db.commit()
    assert client.get(f"/api/v1/resources/exports/{task_id}/download").status_code == 403
    exports = client.get("/api/v1/resources").json()["exports"]
    assert not next(item for item in exports if item["task_id"] == task_id)["available"]


def test_worker_completion_records_delivery_identity_and_replacement_revokes_it(client, workspace):
    from backend.platform.task_contracts import TaskOutcome
    from backend.platform.storage import sha256_file
    path = _write(workspace["root"], "06_audio_merge/book.mp3", b"completed audio")
    task_id = _submit(client, workspace, "tts.merge", {"package": "book"})
    claim = claim_task(task_id, "delivery-worker", lease_seconds=600)
    temp = _write(workspace["root"], ".tasks/result.json", b"{}")
    outcome = TaskOutcome(temp_path=temp, output_name="merge-result.json", content_type="application/json", size_bytes=2, sha256=sha256_file(temp), metadata={"path": str(path), "file": path.name, "size": path.stat().st_size})
    assert complete_claim(claim, outcome)
    _scan(client, workspace)
    item = client.get("/api/v1/resources/entries", params={"category": "deliverables"}).json()["items"][0]
    with SessionLocal() as db:
        assert db.get(TaskResult, task_id).result["deliveries"][0]["relative_path"] == "06_audio_merge/book.mp3"
    path.write_bytes(b"replaced with incomplete bytes")
    assert client.get(f"/api/v1/resources/files/{item['id']}/download").status_code == 403
    assert client.get("/api/v1/resources/entries", params={"category": "deliverables"}).json()["total"] == 0


def test_queued_query_export_cannot_silently_drop_a_changed_product(client, workspace):
    for name in ("a.mp3", "b.mp3"):
        _write(workspace["root"], f"07_output/{name}", b"ready")
        _delivery(workspace, f"07_output/{name}")
    snapshot = _scan(client, workspace)
    task_id = _submit(client, workspace, "resources.package", {"scope": {"category": "deliverables", "snapshots": [{"project_id": workspace["project"], "snapshot_id": snapshot["snapshot_id"]}]}})
    (workspace["root"] / "07_output/b.mp3").write_bytes(b"changed")
    claim = claim_task(task_id, "delivery-query-worker", lease_seconds=600)
    with pytest.raises(TaskExecutionError, match="成品范围已变化"):
        execute_claim(claim)


def test_old_successful_audio_results_are_recognized_but_old_archives_are_not(client, workspace):
    path = _write(workspace["root"], "06_audio_merge/old.mp3", b"published audio")
    with SessionLocal() as db:
        task = Task(owner_id=workspace["owner"], project_id=workspace["project"], task_type="tts.merge", status="succeeded", finished_at=utcnow())
        db.add(task); db.flush()
        db.add(TaskResult(task_id=task.id, result={"path": str(path), "file": path.name, "size": path.stat().st_size}))
        db.commit()
    _write(workspace["root"], "07_output/old.zip", b"not a verified product collection")
    _scan(client, workspace)
    items = client.get("/api/v1/resources/entries", params={"category": "deliverables"}).json()["items"]
    assert [item["name"] for item in items] == ["old.mp3"]
    assert client.get("/api/files/preview/07_output/old.zip").status_code == 403
    assert client.get("/api/files/download/07_output/old.zip").status_code == 403


def test_legacy_zip_rechecks_source_identity_during_archive_creation(client, workspace, monkeypatch):
    source = _write(workspace["root"], "07_output/ready.wav", b"ready audio")
    _delivery(workspace, "07_output/ready.wav")
    task_id = _submit(client, workspace, "audio.zip", {"base": "book", "files": [{"name": "ready.wav", "relative_path": "07_output/ready.wav"}]})
    claim = claim_task(task_id, "delivery-zip-worker", lease_seconds=600)
    original = zipfile._ZipWriteFile.write
    def replaced(writer, *args, **kwargs):
        result = original(writer, *args, **kwargs)
        source.write_bytes(b"replaced incomplete audio")
        return result
    monkeypatch.setattr(zipfile._ZipWriteFile, "write", replaced)
    with pytest.raises(TaskExecutionError, match="成品在导出期间发生变化"):
        execute_claim(claim)
    with SessionLocal() as db:
        assert db.get(TaskResult, task_id) is None


def test_successful_but_incomplete_production_output_is_not_deliverable(client, workspace):
    path = _write(workspace["root"], "06_audio_merge/partial.mp3", b"partial merged audio")
    task_id = _delivery(workspace, "06_audio_merge/partial.mp3", "tts.merge")
    with SessionLocal() as db:
        record = db.get(TaskResult, task_id)
        record.result = {**record.result, "complete": False, "path": str(path)}
        db.commit()
    _scan(client, workspace)
    assert client.get("/api/v1/resources/entries", params={"category": "deliverables"}).json()["total"] == 0
    assert client.get("/api/files/download/06_audio_merge/partial.mp3").status_code == 403


@pytest.mark.parametrize("source_complete", [True, False])
def test_mix_delivery_requires_a_complete_narration_source(client, workspace, monkeypatch, source_complete):
    from backend.engines import bgm
    source = _write(workspace["root"], "06_audio_merge/book.mp3", b"narration")
    source_task = _delivery(workspace, "06_audio_merge/book.mp3", "tts.merge")
    if not source_complete:
        with SessionLocal() as db:
            record = db.get(TaskResult, source_task)
            record.result = {**record.result, "complete": False, "path": str(source)}
            db.commit()
    output = _write(workspace["root"], "08_bgm/book.mp3", b"mixed audio")
    monkeypatch.setattr(bgm, "mix_chapter", lambda *args: {"path": str(output), "file": output.name})
    task_id = _submit(client, workspace, "bgm.mix", {"stem": "book"})
    result = _run(task_id)
    assert bool(result["deliveries"]) is source_complete
    _scan(client, workspace)
    assert client.get("/api/v1/resources/entries", params={"category": "deliverables"}).json()["total"] == (2 if source_complete else 0)


@pytest.mark.parametrize("module,producer,complete,changed", [
    ("06_audio_merge", "tts.merge", True, False),
    ("06_audio_merge", "tts.merge", False, False),
    ("08_bgm", "bgm.mix", True, False),
    ("08_bgm", "bgm.mix", False, False),
    ("05_audio_chunk", None, False, False),
    ("01_input", None, True, False),
    ("01_input", None, True, True),
])
def test_cut_inherits_delivery_eligibility_and_preserves_uploaded_audio(client, workspace, monkeypatch, module, producer, complete, changed):
    source = _write(workspace["root"], f"{module}/source.mp3", b"source audio")
    if producer:
        source_task = _delivery(workspace, f"{module}/source.mp3", producer)
        if not complete:
            with SessionLocal() as db:
                record = db.get(TaskResult, source_task)
                record.result = {**record.result, "complete": False, "path": str(source)}
                db.commit()
    monkeypatch.setattr("backend.platform.task_worker.audio_engine.probe_duration", lambda *args: (2.0, ""))

    def fake_cut(path, segments, out_dir, *args, **kwargs):
        output = _write(Path(out_dir), "episode.mp3", b"cut audio")
        if changed:
            source.write_bytes(b"replaced while cutting")
        return [{"name": output.name, "path": str(output), "size": output.stat().st_size, "duration": 2.0}]

    monkeypatch.setattr("backend.platform.task_worker.audio_engine.cut_segments", fake_cut)
    response = client.post("/api/audio/cut", headers={"X-CSRF-Token": workspace["csrf"]}, json={
        "path": str(source), "segments": [{"index": 0, "start": 0, "duration": 2}], "smart_align": False,
    })
    assert response.status_code == 200, response.text
    result = _run(response.json()["task_id"])
    eligible = complete and not changed
    assert result["complete"] is eligible
    assert bool(result["deliveries"]) is eligible
    _scan(client, workspace)
    assert client.get("/api/files/download/07_output/episode.mp3").status_code == (200 if eligible else 403)
    assert client.get("/api/files/preview/07_output/episode.mp3").status_code == 200
    for task_type in ("audio.zip", "audio.export"):
        packaged = client.post("/api/v1/tasks", headers={"X-CSRF-Token": workspace["csrf"]}, json={
            "project_id": workspace["project"], "task_type": task_type,
            "payload": {"base": "book", "source_relative": f"{module}/source.mp3",
                        "files": [{"name": "episode.mp3", "relative_path": "07_output/episode.mp3"}]},
            "idempotency_key": uuid.uuid4().hex,
        })
        assert packaged.status_code == (201 if eligible else 403), packaged.text


def test_inventory_searches_entire_workspace_with_natural_sort_and_no_fake_catalog(client, workspace):
    for index in range(1, 345):
        _write(workspace["root"], f"02_split_text/第{index}章.txt", f"第{index}章内容".encode())
    _write(workspace["root"], ".tasks/receipt.json", b"internal")
    _write(workspace["root"], "config/setting.json", b"{}")
    before = client.get("/api/v1/resources").json()
    assert before["projects"][0]["snapshot"] is None
    snapshot = _scan(client, workspace)
    assert snapshot["file_count"] == 345
    assert snapshot["complete"]
    page = client.get("/api/v1/resources/entries", params={"project_id": workspace["project"], "category": "02_split_text"}).json()
    assert page["total"] == 344
    assert len(page["items"]) == 50
    assert [item["name"] for item in page["items"][:3]] == ["第1章.txt", "第2章.txt", "第3章.txt"]
    search = client.get("/api/v1/resources/entries", params={"query": "第344章", "category": "all"}).json()
    assert search["total"] == 1
    assert search["items"][0]["name"] == "第344章.txt"
    with SessionLocal() as db:
        assert db.scalars(select(ProjectFile).where(ProjectFile.project_id == workspace["project"])).all() == []


def test_directory_browsing_same_names_literal_search_and_size_sort(client, workspace):
    _write(workspace["root"], "02_split_text/卷一/chapter.txt", b"a")
    _write(workspace["root"], "02_split_text/卷二/chapter.txt", b"b" * 2000)
    _write(workspace["root"], "02_split_text/100%_complete.txt", b"done")
    _scan(client, workspace)
    children = client.get("/api/v1/resources/entries", params={"project_id": workspace["project"], "path": "02_split_text", "directory_mode": True}).json()
    assert [item["kind"] for item in children["items"][:2]] == ["directory", "directory"]
    search = client.get("/api/v1/resources/entries", params={"query": "%_", "category": "all"}).json()
    assert search["total"] == 1
    sorted_items = client.get("/api/v1/resources/entries", params={"sort": "size"}).json()["items"]
    assert sorted_items[0]["relative_path"] == "02_split_text/卷二/chapter.txt"
    invalid = client.get("/api/v1/resources/entries", params={"project_id": workspace["project"], "path": "../other", "directory_mode": True})
    assert invalid.status_code == 422


def test_preview_redacts_config_and_supports_range_and_truncation(client, workspace):
    _write(workspace["root"], "config/setting.json", json.dumps({"llm": {"api_key": "super-secret"}, "speed": 1}).encode())
    _write(workspace["root"], "config/broken.json", b'{"api_key": "never-expose",')
    _write(workspace["root"], "07_output/audio.mp3", b"0123456789")
    _write(workspace["root"], "02_split_text/large.txt", b"a" * (1024 * 1024 + 20))
    _scan(client, workspace)
    files = client.get("/api/v1/resources/entries", params={"category": "system"}).json()["items"]
    config = next(item for item in files if item["name"] == "setting.json")
    response = client.get(f"/api/v1/resources/files/{config['id']}/preview")
    assert response.status_code == 200
    assert "super-secret" not in response.text
    assert response.json()["content"]
    broken = next(item for item in files if item["name"] == "broken.json")
    response = client.get(f"/api/v1/resources/files/{broken['id']}/preview")
    assert response.json()["content"] is None
    assert "never-expose" not in response.text
    files = client.get("/api/v1/resources/entries").json()["items"]
    audio = next(item for item in files if item["name"] == "audio.mp3")
    response = client.get(f"/api/v1/resources/files/{audio['id']}/preview", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"2345"
    large = next(item for item in files if item["name"] == "large.txt")
    response = client.get(f"/api/v1/resources/files/{large['id']}/preview").json()
    assert response["truncated"] and len(response["content"]) == 1024 * 1024


def test_export_preserves_directory_structure_and_has_separate_storage(client, workspace):
    _write(workspace["root"], "07_output/卷一/chapter.mp3", "第一卷".encode())
    _write(workspace["root"], "07_output/卷二/chapter.mp3", "第二卷".encode())
    _delivery(workspace, "07_output/卷一/chapter.mp3")
    _delivery(workspace, "07_output/卷二/chapter.mp3")
    _write(workspace["root"], "config/setting.json", b"secret")
    snapshot = _scan(client, workspace)
    task_id = _submit(client, workspace, "resources.package", {"scope": {"snapshots": [{"project_id": workspace["project"], "snapshot_id": snapshot["snapshot_id"]}], "category": "all"}, "name": "家庭资料"})
    result = _run(task_id)
    assert result["file_count"] == 2
    assert "file_id" not in result
    response = client.get(f"/api/v1/resources/exports/{task_id}/download")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert sorted(archive.namelist()) == ["07_output/卷一/chapter.mp3", "07_output/卷二/chapter.mp3"]
    resources = client.get("/api/v1/resources").json()
    assert resources["storage"]["export_bytes"] == len(response.content)
    assert resources["projects"][0]["snapshot"]["size_bytes"] == len("第一卷第二卷".encode()) + len(b"secret")


def test_changed_file_fails_package_without_publishing_partial_zip(client, workspace):
    path = _write(workspace["root"], "07_output/book.mp3", b"first")
    _delivery(workspace, "07_output/book.mp3")
    _scan(client, workspace)
    file = client.get("/api/v1/resources/entries").json()["items"][0]
    task_id = _submit(client, workspace, "resources.package", {"files": [{"resource_id": file["id"], "snapshot_id": file["snapshot_id"]}]})
    path.write_bytes(b"changed")
    claim = claim_task(task_id, "resource-test-worker", lease_seconds=600)
    with pytest.raises(TaskExecutionError, match="成品已变化"):
        execute_claim(claim)
    assert client.get(f"/api/v1/resources/exports/{task_id}/download").status_code == 404
    with SessionLocal() as db:
        assert not internal_path(db, workspace["owner"], "exports", task_id, "files.zip").exists()


def test_cleanup_rechecks_idle_identity_and_only_deletes_qualified_temp(client, workspace):
    old = _write(workspace["root"], "00_temp/old.tmp", b"delete")
    changed = _write(workspace["root"], ".cache/changed.tmp", b"old")
    fresh = _write(workspace["root"], "cache/fresh.tmp", b"keep")
    business = _write(workspace["root"], "05_audio_chunk/old.wav", b"keep")
    cutoff = time.time() - 8 * 24 * 3600
    for path in (old, changed, business):
        os.utime(path, (cutoff, cutoff))
    snapshot = _scan(client, workspace)
    preview = client.get("/api/v1/resources/cleanup-preview", params={"project_ids": workspace["project"]}).json()
    assert preview["total"] == 2
    task_id = _submit(client, workspace, "resources.cleanup", {"snapshots": [{"project_id": workspace["project"], "snapshot_id": snapshot["snapshot_id"]}]})
    changed.write_bytes(b"new data")
    result = _run(task_id)["projects"][0]
    assert result["deleted_count"] == 1
    assert result["skipped_count"] == 1
    assert not old.exists()
    assert fresh.exists() and changed.exists() and business.exists()


def test_cleanup_blocks_another_task_and_does_not_misreport_success(client, workspace):
    old = _write(workspace["root"], "00_temp/old.tmp", b"keep")
    value = time.time() - 8 * 24 * 3600
    os.utime(old, (value, value))
    snapshot = _scan(client, workspace)
    _submit(client, workspace, "text.format", {})
    task_id = _submit(client, workspace, "resources.cleanup", {"snapshots": [{"project_id": workspace["project"], "snapshot_id": snapshot["snapshot_id"]}]})
    result = _run(task_id)["projects"][0]
    assert result["blocked"] and result["deleted_count"] == 0
    assert old.exists()


def test_cross_account_cannot_read_file_export_or_submit_foreign_snapshot(client, workspace):
    _write(workspace["root"], "01_input/book.txt", b"private")
    _scan(client, workspace)
    file = client.get("/api/v1/resources/entries").json()["items"][0]
    suffix = uuid.uuid4().hex[:12]
    second = client.post("/api/auth/register", json={"email": f"other{suffix}@example.test", "username": f"other{suffix}", "password": "test-pass-1234"}).json()
    assert client.get(f"/api/v1/resources/files/{file['id']}/download").status_code == 404
    own_project = client.get("/api/v1/projects").json()[0]["id"]
    response = client.post("/api/v1/tasks", headers={"X-CSRF-Token": second["csrf_token"]}, json={"project_id": own_project, "task_type": "resources.package", "payload": {"files": [{"resource_id": file["id"], "snapshot_id": file["snapshot_id"]}]}, "idempotency_key": uuid.uuid4().hex})
    assert response.status_code == 404


def test_resource_scan_submission_coalesces_identical_active_scope(client, workspace):
    first = _submit(client, workspace, "resources.scan", {"project_ids": [workspace["project"]]})
    second = _submit(client, workspace, "resources.scan", {"project_ids": [workspace["project"]]})
    assert first == second


def test_resource_task_payload_rejects_forged_snapshots_and_paths(client, workspace):
    response = client.post("/api/v1/tasks", headers={"X-CSRF-Token": workspace["csrf"]}, json={"project_id": workspace["project"], "task_type": "resources.cleanup", "payload": {"snapshots": [{"project_id": workspace["project"], "snapshot_id": "../outside"}]}, "idempotency_key": uuid.uuid4().hex})
    assert response.status_code == 422


def test_reference_audio_is_not_counted_as_production_audio(client, workspace):
    _write(workspace["root"], "04_voice_profiles/reference.wav", b"reference")
    _write(workspace["root"], "01_input/input.mp3", b"source")
    _write(workspace["root"], "07_output/final.wav", b"output")
    snapshot = _scan(client, workspace)
    assert snapshot["audio_count"] == 1
    assert client.get("/api/v1/resources/entries", params={"category": "audio"}).json()["total"] == 1


def test_scan_cancellation_keeps_old_pointer_and_does_not_publish_partial_index(client, workspace, monkeypatch):
    from backend.core.task_control import TaskCancelled
    from backend.platform import resource_tasks
    _write(workspace["root"], "02_split_text/first.txt", b"first")
    snapshot = _scan(client, workspace)
    task_id = _submit(client, workspace, "resources.scan", {"project_ids": [workspace["project"]]})
    claim = claim_task(task_id, "resource-test-worker", lease_seconds=600)
    def cancelled(*args, **kwargs):
        raise TaskCancelled()
    monkeypatch.setattr(resource_tasks.EngineExecutionContext, "check", cancelled)
    with pytest.raises(TaskCancelled):
        execute_claim(claim)
    assert client.get("/api/v1/resources").json()["projects"][0]["snapshot"]["snapshot_id"] == snapshot["snapshot_id"]


def test_missing_workspace_reports_unknown_instead_of_zero(client, workspace):
    root = workspace["root"]
    moved = root.with_name(root.name + "-missing-qa")
    root.rename(moved)
    task_id = _submit(client, workspace, "resources.scan", {"project_ids": [workspace["project"]]})
    _run(task_id)
    overview = client.get("/api/v1/resources").json()
    project = overview["projects"][0]
    assert project["snapshot"] is None and project["scan_error"]
    assert not overview["storage"]["project_complete"]


def test_preview_truncation_does_not_split_utf8_characters(client, workspace):
    _write(workspace["root"], "02_split_text/large.txt", b"a" * (1024 * 1024 - 1) + "中尾".encode())
    _scan(client, workspace)
    file = client.get("/api/v1/resources/entries").json()["items"][0]
    preview = client.get(f"/api/v1/resources/files/{file['id']}/preview").json()
    assert preview["truncated"] and preview["encoding"] == "UTF-8"
    assert preview["content"].endswith("a")


def test_trashed_project_resource_id_cannot_be_read(client, workspace):
    _write(workspace["root"], "01_input/private.txt", b"private")
    snapshot = _scan(client, workspace)
    file = client.get("/api/v1/resources/entries").json()["items"][0]
    deleted = client.delete(f"/api/v1/projects/{workspace['project']}", headers={"X-CSRF-Token": workspace["csrf"]})
    assert deleted.status_code == 200
    assert client.get(f"/api/v1/resources/files/{file['id']}/download").status_code == 404
    overview = client.get("/api/v1/resources").json()
    assert overview["storage"]["trash_bytes"] == snapshot["size_bytes"]
    assert not any(item["project_id"] == workspace["project"] for item in overview["projects"])


def test_frozen_snapshots_in_active_export_survive_retention(client, workspace):
    from backend.platform.resource_retention import purge_resource_artifacts
    _write(workspace["root"], "07_output/book.mp3", b"book")
    _delivery(workspace, "07_output/book.mp3")
    snapshot = _scan(client, workspace)
    file = client.get("/api/v1/resources/entries").json()["items"][0]
    _submit(client, workspace, "resources.package", {"files": [{"resource_id": file["id"], "snapshot_id": file["snapshot_id"]}]})
    _scan(client, workspace)
    with SessionLocal() as db:
        index = internal_path(db, workspace["owner"], workspace["project"], f"{snapshot['snapshot_id']}.sqlite")
    expired = time.time() - 864000
    os.utime(index, (expired, expired))
    purge_resource_artifacts()
    assert index.exists()


def test_partial_rescan_preserves_previous_complete_snapshot(client, workspace, monkeypatch):
    from backend.platform import resource_inventory
    root = workspace["root"]
    _write(root, "02_split_text/first.txt", b"first")
    previous = _scan(client, workspace)
    _write(root, "03_parsed_json/new.json", b"{}")
    original = os.scandir
    def denied(path):
        if Path(path) == root / "03_parsed_json":
            raise PermissionError("qa permission boundary")
        return original(path)
    monkeypatch.setattr(resource_inventory.os, "scandir", denied)
    task_id = _submit(client, workspace, "resources.scan", {"project_ids": [workspace["project"]]})
    result = _run(task_id)
    assert not result["snapshots"][0]["complete"]
    current = client.get("/api/v1/resources").json()["projects"][0]
    assert current["scan_error"] and current["snapshot"]["snapshot_id"] == previous["snapshot_id"]


def test_cross_project_search_deep_pages_have_stable_natural_order(client, workspace):
    for n in range(1, 65):
        _write(workspace["root"], f"02_split_text/第{n}章.txt", b"same")
    created = client.post("/api/v1/projects", headers={"X-CSRF-Token": workspace["csrf"]}, json={"name": "第二本"}).json()
    second = created["id"]
    second_root = workspace["root"].parent / Path(created["directory_key"]).name
    for n in range(1, 65):
        _write(second_root, f"02_split_text/第{n}章.txt", b"same")
    task_id = _submit(client, workspace, "resources.scan", {"project_ids": [workspace["project"], second]})
    _run(task_id)
    items = []
    for page in (1, 2, 3):
        result = client.get("/api/v1/resources/entries", params={"page": page}).json()
        assert result["total"] == 128
        items.extend(result["items"])
    assert len({item["id"] for item in items}) == 128
    assert [item["name"] for item in items] == [f"第{n}章.txt" for n in range(1, 65) for _ in range(2)]


def test_exports_expire_and_are_inaccessible_after_project_trash(client, workspace):
    from datetime import datetime, timedelta, timezone
    from backend.platform.resource_retention import purge_resource_artifacts
    _write(workspace["root"], "07_output/book.mp3", b"book")
    _delivery(workspace, "07_output/book.mp3")
    _scan(client, workspace)
    file = client.get("/api/v1/resources/entries").json()["items"][0]
    payload = {"files": [{"resource_id": file["id"], "snapshot_id": file["snapshot_id"]}]}
    task_id = _submit(client, workspace, "resources.package", payload)
    _run(task_id)
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        task.result.result = {**task.result.result, "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        db.commit()
        path = internal_path(db, workspace["owner"], "exports", task_id, "files.zip")
    assert client.get(f"/api/v1/resources/exports/{task_id}/download").status_code == 410
    purge_resource_artifacts()
    assert not path.exists()
    second = _submit(client, workspace, "resources.package", payload)
    _run(second)
    client.delete(f"/api/v1/projects/{workspace['project']}", headers={"X-CSRF-Token": workspace["csrf"]})
    assert client.get(f"/api/v1/resources/exports/{second}/download").status_code == 404
    assert not next(item for item in client.get("/api/v1/resources").json()["exports"] if item["task_id"] == second)["available"]


def test_export_cancellation_and_disk_full_never_publish_partial_zip(client, workspace, monkeypatch):
    from backend.core.task_control import TaskCancelled
    from backend.platform import resource_tasks
    _write(workspace["root"], "07_output/large.wav", b"x" * (2 * 1024 * 1024))
    _delivery(workspace, "07_output/large.wav")
    _scan(client, workspace)
    file = client.get("/api/v1/resources/entries").json()["items"][0]
    payload = {"files": [{"resource_id": file["id"], "snapshot_id": file["snapshot_id"]}]}
    task_id = _submit(client, workspace, "resources.package", payload)
    claim = claim_task(task_id, "resource-test-worker", lease_seconds=600)
    count = 0
    def cancel(context):
        nonlocal count
        count += 1
        if count >= 4:
            raise TaskCancelled()
    with monkeypatch.context() as patch:
        patch.setattr(resource_tasks.EngineExecutionContext, "check", cancel)
        with pytest.raises(TaskCancelled):
            execute_claim(claim)
    with SessionLocal() as db:
        assert not internal_path(db, workspace["owner"], "attempts", claim.attempt_id, "files.zip").exists()
        assert not internal_path(db, workspace["owner"], "exports", task_id, "files.zip").exists()
    second = _submit(client, workspace, "resources.package", payload)
    second_claim = claim_task(second, "resource-test-worker", lease_seconds=600)
    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(resource_tasks.zipfile.ZipFile, "open", full)
    with pytest.raises(OSError, match="No space"):
        execute_claim(second_claim)
    with SessionLocal() as db:
        assert not internal_path(db, workspace["owner"], "exports", second, "files.zip").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_junction_substitution_is_blocked_at_read_and_cleanup(client, workspace, tmp_path):
    import subprocess
    old = _write(workspace["root"], "00_temp/old.tmp", b"private")
    timestamp = time.time() - 864000
    os.utime(old, (timestamp, timestamp))
    snapshot = _scan(client, workspace)
    with SessionLocal() as db:
        with __import__('backend.platform.resource_inventory', fromlist=['index_connection']).index_connection(internal_path(db, workspace["owner"], workspace["project"], f"{snapshot['snapshot_id']}.sqlite")) as connection:
            file_id = connection.execute("SELECT id FROM entries WHERE relative_path='00_temp/old.tmp'").fetchone()[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "old.tmp"
    target.write_bytes(b"must remain")
    cache = old.parent
    cache.rename(cache.with_name("former-cache"))
    linked = subprocess.run(["cmd", "/c", "mklink", "/J", str(cache), str(outside)], capture_output=True)
    assert linked.returncode == 0, linked.stderr
    try:
        assert client.get(f"/api/v1/resources/files/{file_id}/download").status_code == 404
        task_id = _submit(client, workspace, "resources.cleanup", {"snapshots": [{"project_id": workspace["project"], "snapshot_id": snapshot["snapshot_id"]}]})
        result = _run(task_id)["projects"][0]
        assert result["deleted_count"] == 0 and result["skipped_count"] == 1
        assert target.read_bytes() == b"must remain"
    finally:
        cache.rmdir()


@pytest.mark.parametrize("revoked_index", [0, 1])
def test_indexed_package_rechecks_revocation_between_files(client, workspace, monkeypatch, revoked_index):
    from backend.platform.delivery_index import backfill_project, write_authorities
    from backend.platform.models import User
    for name in ("a.mp3", "b.mp3"):
        path = _write(workspace["root"], f"07_output/{name}", b"ready audio")
        _delivery(workspace, f"07_output/{name}")
    with SessionLocal.begin() as db:
        assert backfill_project(db, db.get(User, workspace["owner"]), workspace["project"])
    _scan(client, workspace)
    items = client.get("/api/v1/resources/entries", params={"category": "deliverables"}).json()["items"]
    assert len(items) == 2
    task_id = _submit(client, workspace, "resources.package", {"files": [
        {"resource_id": item["id"], "snapshot_id": item["snapshot_id"]} for item in items]})
    original = zipfile._ZipWriteFile.write
    revoked = False
    def revoke_after_first_write(writer, data):
        nonlocal revoked
        value = original(writer, data)
        if not revoked:
            revoked = True
            relative = items[revoked_index]["relative_path"]
            with SessionLocal.begin() as db:
                task = Task(owner_id=workspace["owner"], project_id=workspace["project"], task_type="audio.cut",
                    status="succeeded", finished_at=utcnow())
                db.add(task); db.flush()
                result = {"complete": False, "path": str(workspace["root"] / relative)}
                db.add(TaskResult(task_id=task.id, result=result))
                write_authorities(db, db.get(User, workspace["owner"]), task, result)
        return value
    monkeypatch.setattr(zipfile._ZipWriteFile, "write", revoke_after_first_write)
    claim = claim_task(task_id, "indexed-revocation-package", lease_seconds=600)
    with pytest.raises(TaskExecutionError, match="成品"):
        execute_claim(claim)
    assert revoked
    with SessionLocal() as db:
        assert not internal_path(db, workspace["owner"], "exports", task_id, "files.zip").exists()


@pytest.mark.parametrize("task_type,relative,producer,payload", [
    ("audio.zip", "07_output/book.mp3", "audio.cut", {"base": "book", "files": [{"name": "book.mp3", "relative_path": "07_output/book.mp3"}]}),
    ("bgm.package", "08_bgm/book.mp3", "bgm.mix", {"base": "book", "chapters": ["book"]}),
])
def test_legacy_archive_rechecks_qualification_after_copy(client, workspace, monkeypatch, task_type, relative, producer, payload):
    from backend.platform.delivery_index import backfill_project, write_authorities
    from backend.platform.models import User
    path = _write(workspace["root"], relative, b"finished audio")
    _delivery(workspace, relative, producer)
    with SessionLocal.begin() as db:
        assert backfill_project(db, db.get(User, workspace["owner"]), workspace["project"])
    task_id = _submit(client, workspace, task_type, payload)
    original = zipfile._ZipWriteFile.write
    revoked = False
    def revoke(writer, data):
        nonlocal revoked
        count = original(writer, data)
        if not revoked:
            revoked = True
            with SessionLocal.begin() as db:
                task = Task(owner_id=workspace["owner"], project_id=workspace["project"], task_type=producer,
                    status="succeeded", finished_at=utcnow())
                db.add(task); db.flush()
                result = {"complete": False, "path": str(path)}
                db.add(TaskResult(task_id=task.id, result=result))
                write_authorities(db, db.get(User, workspace["owner"]), task, result)
        return count
    monkeypatch.setattr(zipfile._ZipWriteFile, "write", revoke)
    claim = claim_task(task_id, "archive-revocation", lease_seconds=600)
    with pytest.raises(TaskExecutionError, match="成品"):
        execute_claim(claim)
    assert revoked
    assert not (path.parent / "book.zip").exists()
