from __future__ import annotations

import shutil
import socket
import subprocess
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from backend.api import admin
from backend.platform.database import Base
from backend.platform.models import SystemConfig, User, Workspace
from backend.platform.storage import lock_storage_migration


@pytest.fixture
def storage_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'storage.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(admin, "SessionLocal", factory)
    root = tmp_path / "source"
    with factory() as db:
        actor = User(id="admin", username="admin", email="admin@example.com", password_hash="unused", role="admin")
        db.add(actor)
        db.add(SystemConfig(key="storage.root", value={"path": str(root)}))
        for name in ("one", "two"):
            db.add(Workspace(id=name, owner_id=actor.id, name=name, directory_key=f"admin/{name}"))
            folder = root / "admin" / name
            folder.mkdir(parents=True)
            (folder / "book.txt").write_text(name, encoding="utf-8")
        db.commit()
        yield db, actor, root
    engine.dispose()


def test_conflicting_target_does_not_disable_writes(storage_db, tmp_path):
    db, actor, root = storage_db
    target = tmp_path / "target"
    (target / "admin" / "one").mkdir(parents=True)
    with pytest.raises(HTTPException) as error:
        admin.update_storage_settings(admin.StorageRootUpdate(root_path=str(target)), actor, db)
    assert error.value.status_code == 409
    assert db.get(SystemConfig, "storage.migration") is None
    assert (root / "admin" / "one" / "book.txt").read_text("utf-8") == "one"


def test_failed_move_clears_marker_after_successful_compensation(storage_db, tmp_path, monkeypatch):
    db, actor, root = storage_db
    target = tmp_path / "target"
    real_move = shutil.move
    calls = 0

    def fail_second_move(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected move failure")
        return real_move(source, destination)

    monkeypatch.setattr(admin.shutil, "move", fail_second_move)
    with pytest.raises(HTTPException):
        admin.update_storage_settings(admin.StorageRootUpdate(root_path=str(target)), actor, db)
    assert db.get(SystemConfig, "storage.migration") is None
    assert db.get(SystemConfig, "storage.root").value["path"] == str(root)
    for name in ("one", "two"):
        assert (root / "admin" / name / "book.txt").read_text("utf-8") == name


def test_partial_cross_volume_copy_retains_recovery_marker(storage_db, tmp_path, monkeypatch):
    db, actor, _root = storage_db
    target = tmp_path / "target"

    def fail_during_copy(source, destination):
        Path(destination).mkdir(parents=True)
        raise OSError("copy interrupted")

    monkeypatch.setattr(admin.shutil, "move", fail_during_copy)
    with pytest.raises(HTTPException):
        admin.update_storage_settings(admin.StorageRootUpdate(root_path=str(target)), actor, db)
    assert db.get(SystemConfig, "storage.migration") is not None


@pytest.fixture
def isolated_postgres(tmp_path):
    # Never accept a deployment URL: create a disposable cluster on loopback.
    executable = shutil.which("initdb")
    if executable is None:
        candidates = sorted(Path("C:/Program Files/PostgreSQL").glob("*/bin/initdb.exe"))
        executable = str(candidates[-1]) if candidates else None
    if executable is None:
        pytest.skip("PostgreSQL binaries are not installed")
    binary = Path(executable).parent
    data = tmp_path / "postgres"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    def run(name, *args):
        # PostgreSQL's Windows launcher may pass pipe handles to its daemon.
        # Use a file so subprocess communication cannot wait on those handles.
        output = tmp_path / f"{name}.log"
        with output.open("wb") as stream:
            result = subprocess.run(
                [str(binary / name), *map(str, args)], stdout=stream, stderr=stream,
                stdin=subprocess.DEVNULL, timeout=45,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        assert result.returncode == 0, output.read_text("utf-8", errors="replace")

    run("initdb", "-D", data, "-A", "trust", "-U", "postgres", "--encoding=UTF8", "--no-locale")
    run("pg_ctl", "-D", data, "-l", tmp_path / "postgres.log", "-o", f"-h 127.0.0.1 -p {port}", "-w", "start")
    engine = create_engine(f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres")
    try:
        yield engine
    finally:
        engine.dispose()
        run("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")


def test_postgres_storage_lock_rejects_contention_without_blocking(isolated_postgres):
    factory = sessionmaker(isolated_postgres)
    with factory() as request, factory() as migration, factory() as submission:
        assert lock_storage_migration(request, shared=True)
        assert not lock_storage_migration(migration)
        # A task route may enter under the middleware's shared lock.
        assert lock_storage_migration(submission, shared=True)
        submission.rollback()
        request.rollback()
        assert lock_storage_migration(migration)
        assert not lock_storage_migration(request, shared=True)
        migration.rollback()
        assert lock_storage_migration(request, shared=True)


def test_postgres_migration_keeps_lock_across_marker_commit(isolated_postgres, tmp_path, monkeypatch):
    Base.metadata.create_all(isolated_postgres)
    factory = sessionmaker(isolated_postgres, expire_on_commit=False)
    monkeypatch.setattr(admin, "SessionLocal", factory)
    root, target = tmp_path / "source", tmp_path / "target"
    folder = root / "admin" / "one"
    folder.mkdir(parents=True)
    (folder / "book.txt").write_text("book", encoding="utf-8")
    real_move = shutil.move

    def inspect_during_move(source, destination):
        with factory() as competing:
            assert competing.get(SystemConfig, "storage.migration") is not None
            assert not lock_storage_migration(competing)
            assert not lock_storage_migration(competing, shared=True)
        return real_move(source, destination)

    monkeypatch.setattr(admin.shutil, "move", inspect_during_move)
    with factory() as db:
        actor = User(id="admin", username="admin", email="admin@example.com", password_hash="unused", role="admin")
        db.add(actor)
        db.flush()
        db.add(Workspace(id="one", owner_id=actor.id, name="one", directory_key="admin/one"))
        db.add(SystemConfig(key="storage.root", value={"path": str(root)}))
        db.commit()
        result = admin.update_storage_settings(admin.StorageRootUpdate(root_path=str(target)), actor, db)
        assert result["root_path"] == str(target)
        assert db.get(SystemConfig, "storage.migration") is None


@pytest.mark.parametrize("legacy_hold_table", [False, True])
def test_postgres_schema_upgrade(isolated_postgres, tmp_path, legacy_hold_table):
    env = {
        **os.environ,
        "NARRIFY_DATABASE_URL": isolated_postgres.url.render_as_string(hide_password=False),
        "NARRIFY_AUTO_CREATE_SCHEMA": "false",
        "NARRIFY_STORAGE_ROOT": str(tmp_path / "artifacts"),
    }

    def upgrade(revision):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", revision],
            cwd=Path(__file__).resolve().parents[2], env=env,
            capture_output=True, text=True, timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    if legacy_hold_table:
        upgrade("0010_character_quota")
        from backend.platform.models import QuotaHold
        QuotaHold.__table__.create(isolated_postgres)
    upgrade("head")
    schema = inspect(isolated_postgres)
    assert "quota_holds" in schema.get_table_names()
    assert "next_attempt_at" in {column["name"] for column in schema.get_columns("tasks")}
