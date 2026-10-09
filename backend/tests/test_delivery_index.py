"""Delivery authority, resumable backfill and indexed hot-path budgets."""
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from backend.platform.database import Base
from backend.platform.models import CurrentDelivery, DeliveryIndexState, Project, ProjectFile, Task, TaskResult, User, utcnow
from backend.platform.delivery_index import backfill_project, write_authorities
from backend.platform.resource_delivery import delivery_records, require_delivery, DeliveryDenied
from backend.core.safe_filesystem import file_identity
from backend.core.object_keys import object_key_lookup_key
from backend.platform import storage


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'deliveries.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    root = tmp_path / "storage"
    workspace = root / "owner" / "project"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(storage, "configured_storage_root", lambda db=None: root)
    with factory.begin() as db:
        db.add(User(id="owner", username="owner", email="deliveries@example.test", password_hash="unused"))
        db.flush()
        db.add(Project(id="project", owner_id="owner", name="Book", directory_key="owner/project"))
    yield factory, workspace
    engine.dispose()


def create_file(workspace, relative="06_audio_merge/chapter.mp3", content=b"audio"):
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def add_history(db, path, result=None, *, number=0, task_id=None):
    task = Task(id=task_id or str(uuid.uuid4()), owner_id="owner", project_id="project", task_type="tts.merge",
                status="succeeded", finished_at=utcnow() + timedelta(seconds=number))
    db.add(task)
    db.flush()
    result = result if result is not None else {"deliveries": [{"relative_path": "06_audio_merge/chapter.mp3",
                                                              "identity": list(file_identity(path.stat()))}]}
    db.add(TaskResult(task_id=task.id, result=result))
    return task, result


def finish_backfill(factory):
    for _ in range(100):
        with factory.begin() as db:
            complete = backfill_project(db, db.get(User, "owner"), "project", limit=3)
        if complete:
            return
    pytest.fail("backfill did not converge")


def test_backfill_resumes_and_new_publication_wins_over_old_cursor_chunks(setup):
    factory, workspace = setup
    path = create_file(workspace)
    with factory.begin() as db:
        for i in range(12):
            add_history(db, path, number=i, task_id=f"old-{i:03d}")
    with factory.begin() as db:
        assert not backfill_project(db, db.get(User, "owner"), "project", limit=3)
        state = db.get(DeliveryIndexState, "project")
        assert state.cursor == "old-002" and not state.complete
    with factory.begin() as db:
        task, result = add_history(db, path, number=100, task_id="new-before-cursor")
        write_authorities(db, db.get(User, "owner"), task, result)
    finish_backfill(factory)
    with factory() as db:
        record = require_delivery(db, db.get(User, "owner"), "project", "06_audio_merge/chapter.mp3")
        assert record["task_id"] == "new-before-cursor"
        assert db.get(DeliveryIndexState, "project").complete
        assert len(db.scalars(select(CurrentDelivery)).all()) == 1


def test_new_incomplete_result_revokes_even_when_file_is_missing_and_reappears(setup):
    factory, workspace = setup
    path = create_file(workspace)
    with factory.begin() as db:
        add_history(db, path, number=0)
        add_history(db, path, {"complete": False, "path": str(path)}, number=20)
    path.unlink()
    finish_backfill(factory)
    path.write_bytes(b"audio")
    with factory() as db:
        row = db.get(CurrentDelivery, ("project", object_key_lookup_key("06_audio_merge/chapter.mp3")))
        assert row is not None and not row.valid
        with pytest.raises(DeliveryDenied):
            require_delivery(db, db.get(User, "owner"), "project", "06_audio_merge/chapter.mp3")


def test_index_writes_and_cursor_roll_back_with_transaction(setup):
    factory, workspace = setup
    path = create_file(workspace)
    with factory.begin() as db:
        add_history(db, path)
    with factory() as db:
        backfill_project(db, db.get(User, "owner"), "project")
        db.rollback()
    with factory() as db:
        assert db.get(DeliveryIndexState, "project") is None
        assert db.scalars(select(CurrentDelivery)).all() == []
    finish_backfill(factory)
    with factory() as db:
        old = db.get(CurrentDelivery, ("project", object_key_lookup_key("06_audio_merge/chapter.mp3"))).task_id
        task, result = add_history(db, path, {"complete": False, "path": str(path)}, number=30)
        write_authorities(db, db.get(User, "owner"), task, result)
        db.rollback()
    with factory() as db:
        assert require_delivery(db, db.get(User, "owner"), "project", "06_audio_merge/chapter.mp3")["task_id"] == old


def test_single_path_reads_no_result_history_and_revalidates_identity(setup):
    factory, workspace = setup
    path = create_file(workspace)
    with factory.begin() as db:
        task, result = add_history(db, path)
        write_authorities(db, db.get(User, "owner"), task, result)
        db.add(DeliveryIndexState(project_id="project", complete=True))
        for i in range(1000):
            db.add(Task(owner_id="owner", project_id="project", task_type="tts.merge", status="succeeded"))
    queries = []
    listener = lambda c, cur, statement, *args: queries.append(statement)
    event.listen(factory.kw["bind"], "before_cursor_execute", listener)
    try:
        with factory() as db:
            user = db.get(User, "owner")
            assert require_delivery(db, user, "project", "06_audio_merge/chapter.mp3")
            assert not delivery_records(db, User(id="other", username="owner"), "project", ["06_audio_merge/chapter.mp3"])
    finally:
        event.remove(factory.kw["bind"], "before_cursor_execute", listener)
    assert len(queries) <= 12
    assert not any("task_results" in query for query in queries)
    identity = path.stat()
    path.write_bytes(b"replaced")
    with factory() as db:
        with pytest.raises(DeliveryDenied):
            require_delivery(db, db.get(User, "owner"), "project", "06_audio_merge/chapter.mp3")


def test_historical_catalog_alias_is_backfilled_without_changing_file_identity(setup):
    factory, workspace = setup
    path = create_file(workspace)
    with factory.begin() as db:
        item = ProjectFile(id="legacy-file", owner_id="owner", project_id="project", original_name="old title.mp3",
            object_key="owner/project/06_audio_merge/chapter.mp3", size_bytes=path.stat().st_size, sha256="a" * 64)
        db.add(item)
        add_history(db, path, {"file_id": item.id}, number=3)
    finish_backfill(factory)
    with factory() as db:
        assert require_delivery(db, db.get(User, "owner"), "project", "06_audio_merge/chapter.mp3")


def test_legacy_cleanup_retains_old_only_files_and_checks_identity_after_commit(setup):
    import hashlib
    from backend.platform.artifact_maintenance import retire_legacy_artifacts, remove_retired_files
    factory, workspace = setup
    old_key = f"03_parsed_json/{uuid.uuid4()}/chapter.json"
    old = create_file(workspace, old_key)
    flat = create_file(workspace, "03_parsed_json/chapter.json")
    old_only_key = f"03_parsed_json/{uuid.uuid4()}/old-only.json"
    old_only = create_file(workspace, old_only_key)
    with factory.begin() as db:
        for key in (old_key, "03_parsed_json/chapter.json", old_only_key):
            db.add(ProjectFile(owner_id="owner", project_id="project", original_name=key.split("/")[-1],
                object_key="owner/project/" + key, size_bytes=5, sha256=hashlib.sha256(b"audio").hexdigest(), kind="artifact"))
    with factory.begin() as db:
        _, retired = retire_legacy_artifacts(db)
        assert [path for path, _ in retired] == [old]
    old.write_bytes(b"replaced during commit window")
    remove_retired_files(retired)
    assert old.exists() and old_only.exists() and flat.exists()
    with factory.begin() as db:
        _, retired = retire_legacy_artifacts(db)
    remove_retired_files(retired)
    assert old.exists()  # unrecorded user edits must not be retired on retry
    old.write_bytes(b"audio")
    with factory.begin() as db:
        _, retired = retire_legacy_artifacts(db)
    remove_retired_files(retired)
    assert not old.exists()
    assert old_only.exists() and flat.exists()


def test_maintenance_releases_session_before_migration_backoff(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from backend.platform import delivery_maintenance as maintenance
    active = []
    waits = []
    class Database:
        def __enter__(self):
            active.append(True)
            return self
        def __exit__(self, *args):
            active.clear()
    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            assert not active
            waits.append(seconds)
            self.stopped = True
    monkeypatch.setattr(maintenance, "SessionLocal", Database)
    monkeypatch.setattr(maintenance, "exclusive_file_lock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(maintenance, "lock_storage_migration", lambda *a, **k: False)
    monkeypatch.setattr(maintenance, "backfill_pending_project", lambda *_: pytest.fail("blocked migration must not backfill"))
    maintenance.run(Stop())
    assert waits == [10]


def test_maintenance_keeps_scan_cursor_when_commit_fails(monkeypatch):
    from contextlib import nullcontext
    from backend.platform import delivery_maintenance as maintenance
    cursors, commits = [], []
    class Database:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def commit(self):
            commits.append(True)
            if len(commits) == 1:
                raise RuntimeError("commit failed")
    class Stop:
        waits = 0
        def is_set(self): return self.waits >= 2
        def wait(self, seconds): self.waits += 1
    def retire(db, cursor):
        cursors.append(cursor)
        return ("after-first-chunk", []) if len(cursors) == 1 else ("", [])
    monkeypatch.setattr(maintenance, "SessionLocal", Database)
    monkeypatch.setattr(maintenance, "exclusive_file_lock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(maintenance, "lock_storage_migration", lambda *a, **k: True)
    monkeypatch.setattr(maintenance, "storage_migration", lambda *a: None)
    monkeypatch.setattr(maintenance, "backfill_pending_project", lambda *_: False)
    monkeypatch.setattr(maintenance, "retire_legacy_artifacts", retire)
    maintenance.run(Stop())
    assert cursors == ["", ""]


def test_unresolvable_workspace_never_reports_pending_work(setup, tmp_path):
    from backend.platform import delivery_index
    from backend.platform.delivery_index import backfill_pending_project
    delivery_index._unresolvable_until.clear()
    factory, workspace = setup
    target = tmp_path / "elsewhere"
    workspace.rmdir()
    workspace.symlink_to(target, target_is_directory=True)
    target.mkdir()
    with factory.begin() as db:
        assert backfill_pending_project(db) is False
    with factory.begin() as db:
        # Remembered in-process: the database is not written on its account.
        assert db.get(DeliveryIndexState, "project") is None
    assert "project" in delivery_index._unresolvable_until
    with factory.begin() as db:
        assert backfill_pending_project(db) is False
    # A resolvable project queued behind it is still served and reported.
    with factory.begin() as db:
        db.add(Project(id="good", owner_id="owner", name="Good", directory_key="owner/good"))
    (workspace.parent / "good").mkdir()
    with factory.begin() as db:
        assert backfill_pending_project(db) is True
    delivery_index._unresolvable_until.clear()
