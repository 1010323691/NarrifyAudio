"""Migration paths must work independently of the live ORM's create_all mode."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa



ROOT = Path(__file__).resolve().parents[2]


def _run(env: dict[str, str], *args: str) -> None:
    result = subprocess.run(
        [sys.executable, *args], cwd=ROOT, env=env, text=True, capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("preexisting_hold_table", [False, True])
def test_upgrade_from_empty_database_to_head(tmp_path, preexisting_hold_table):
    database = tmp_path / "migration.db"
    env = {**os.environ, "NARRIFY_DATABASE_URL": f"sqlite:///{database.as_posix()}", "NARRIFY_AUTO_CREATE_SCHEMA": "false"}
    if preexisting_hold_table:
        _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "0010_character_quota")
        # Simulate databases where the old 0001 used the newer live ORM and
        # therefore created quota_holds before revision 0011.
        _run(env, "-c", "from backend.platform.database import engine; from backend.platform.models import QuotaHold; QuotaHold.__table__.create(engine)")
    _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
    engine = sa.create_engine(env["NARRIFY_DATABASE_URL"])
    try:
        inspector = sa.inspect(engine)
        assert "quota_holds" in inspector.get_table_names()
        assert "workspaces" not in inspector.get_table_names()
        assert "next_attempt_at" in {column["name"] for column in inspector.get_columns("tasks")}
        assert "ix_tasks_recovery_page" in {index["name"] for index in inspector.get_indexes("tasks")}
        assert "ix_task_attempts_live_lease" in {index["name"] for index in inspector.get_indexes("task_attempts")}
        assert {"directory_key", "last_selected_at"} <= {column["name"] for column in inspector.get_columns("projects")}
        assert "uq_quota_hold_attempt_operation" in {index["name"] for index in inspector.get_indexes("quota_holds")}
        assert {"text_format_flows", "chapter_review_marks"} <= set(inspector.get_table_names())
        assert "manifest" in {column["name"] for column in inspector.get_columns("text_format_flows")}
        assert {"force_by_length", "source_file_ids"} <= {column["name"] for column in inspector.get_columns("text_format_flows")}
        assert {"gpu_scheduler_state", "gpu_requests"} <= set(inspector.get_table_names())
        assert "uq_review_marks_task_chapter" in {index["name"] for index in inspector.get_indexes("chapter_review_marks")}
    finally:
        engine.dispose()


def test_upgrade_downgrade_upgrade_chapter_review_marks(tmp_path):
    """0017 must roundtrip: downgrade removes both workbench tables, upgrade restores them."""
    database = tmp_path / "workbench-roundtrip.db"
    env = {**os.environ, "NARRIFY_DATABASE_URL": f"sqlite:///{database.as_posix()}", "NARRIFY_AUTO_CREATE_SCHEMA": "false"}
    _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
    _run(env, "-m", "alembic", "-c", "alembic.ini", "downgrade", "0016_unique_active_project_names")
    engine = sa.create_engine(env["NARRIFY_DATABASE_URL"])
    try:
        inspector = sa.inspect(engine)
        assert "text_format_flows" not in inspector.get_table_names()
        assert "chapter_review_marks" not in inspector.get_table_names()
    finally:
        engine.dispose()
    _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
    engine = sa.create_engine(env["NARRIFY_DATABASE_URL"])
    try:
        inspector = sa.inspect(engine)
        assert {"text_format_flows", "chapter_review_marks"} <= set(inspector.get_table_names())
    finally:
        engine.dispose()


def test_upgrade_repairs_legacy_project_workspace_orphans(tmp_path):
    database = tmp_path / "legacy-pairs.db"
    env = {**os.environ, "NARRIFY_DATABASE_URL": f"sqlite:///{database.as_posix()}", "NARRIFY_AUTO_CREATE_SCHEMA": "false"}
    _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "0012_task_retry_eligibility")
    engine = sa.create_engine(env["NARRIFY_DATABASE_URL"])
    try:
        metadata = sa.MetaData()
        users = sa.Table("users", metadata, autoload_with=engine)
        projects = sa.Table("projects", metadata, autoload_with=engine)
        workspaces = sa.Table("workspaces", metadata, autoload_with=engine)
        sessions = sa.Table("user_sessions", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        with engine.begin() as connection:
            connection.execute(users.insert().values(
                id="user-1", email="legacy@example.com", username="legacy", display_name="Legacy",
                password_hash="unused", role="user", is_active=True, created_at=now, updated_at=now,
            ))
            connection.execute(projects.insert().values(
                id="project-only", owner_id="user-1", name="Old project", description="existing",
                created_at=now, updated_at=now,
            ))
            connection.execute(workspaces.insert().values(
                id="workspace-only", owner_id="user-1", name="Old workspace",
                directory_key="legacy/workspace-only", created_at=now, updated_at=now,
            ))
            connection.execute(sessions.insert().values(
                id="session-1", user_id="user-1", token_hash="a" * 64, csrf_hash="b" * 64,
                expires_at=now, last_seen_at=now, active_workspace_id="workspace-only",
            ))
        _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            assert "workspaces" not in inspector.get_table_names()
            migrated_projects = sa.Table("projects", sa.MetaData(), autoload_with=connection)
            migrated_sessions = sa.Table("user_sessions", sa.MetaData(), autoload_with=connection)
            project_rows = {row.id: row for row in connection.execute(sa.select(migrated_projects))}
            active_project_id = connection.execute(sa.select(migrated_sessions.c.active_project_id)).scalar_one()
        assert set(project_rows) == {"project-only", "workspace-only"}
        assert project_rows["project-only"].directory_key == "legacy/project-only"
        assert project_rows["workspace-only"].directory_key == "legacy/workspace-only"
        assert project_rows["project-only"].last_selected_at.replace(tzinfo=timezone.utc) == now
        assert active_project_id == "workspace-only"
        _run(env, "-m", "alembic", "-c", "alembic.ini", "downgrade", "0013_project_workspace_pairs")
        with engine.connect() as connection:
            restored_workspaces = sa.Table("workspaces", sa.MetaData(), autoload_with=connection)
            session_table = sa.Table("user_sessions", sa.MetaData(), autoload_with=connection)
            restored = {row.id: row for row in connection.execute(sa.select(restored_workspaces))}
            active_workspace_id = connection.execute(sa.select(session_table.c.active_workspace_id)).scalar_one()
        assert restored["workspace-only"].directory_key == "legacy/workspace-only"
        assert active_workspace_id == "workspace-only"
    finally:
        engine.dispose()


def _reset_public_schema(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")


@pytest.mark.skipif(
    not os.environ.get("NARRIFY_TEST_POSTGRES_URL"),
    reason="set NARRIFY_TEST_POSTGRES_URL to run the 0015 downgrade roundtrip on real PostgreSQL",
)
def test_drop_quota_reservations_downgrade_roundtrip_on_postgres():
    """0015's upgrade drops the retired reservation machinery and its downgrade
    rebuilds it (structural rollback only — plan A1), the PostgreSQL branch
    restoring the FK upgrade removed. Opt-in: needs a DISPOSEABLE scratch
    database — its public schema is reset at start and end."""
    url = os.environ["NARRIFY_TEST_POSTGRES_URL"]
    engine = sa.create_engine(url)
    try:
        if engine.dialect.name != "postgresql":
            pytest.skip("NARRIFY_TEST_POSTGRES_URL must use PostgreSQL")
        _reset_public_schema(engine)
        env = {
            **os.environ,
            "NARRIFY_DATABASE_URL": url,
            "NARRIFY_AUTO_CREATE_SCHEMA": "false",
        }
        # 0014 -> 0015 (head): the reservation machinery disappears...
        _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "0014_project_storage_identity")
        _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            assert "quota_reservations" not in inspector.get_table_names()
            assert "reservation_id" not in {column["name"] for column in inspector.get_columns("quota_transactions")}
            assert "ix_quota_transactions_reservation_id" not in {index["name"] for index in inspector.get_indexes("quota_transactions")}
        # ...downgrade 0015 rebuilds it, including the PG-only FK.
        _run(env, "-m", "alembic", "-c", "alembic.ini", "downgrade", "0014_project_storage_identity")
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            assert "quota_reservations" in inspector.get_table_names()
            assert {"id", "user_id", "task_id", "units", "status", "created_at", "settled_at"} <= {
                column["name"] for column in inspector.get_columns("quota_reservations")
            }
            assert "ix_quota_reservations_user_id" in {index["name"] for index in inspector.get_indexes("quota_reservations")}
            assert "reservation_id" in {column["name"] for column in inspector.get_columns("quota_transactions")}
            assert "ix_quota_transactions_reservation_id" in {index["name"] for index in inspector.get_indexes("quota_transactions")}
            fks = {fk["name"] for fk in inspector.get_foreign_keys("quota_transactions")}
            assert "fk_quota_transactions_reservation_id" in fks, "PG FK branch did not restore the constraint"
        # ...and upgrading back to head drops it again.
        _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            assert "quota_reservations" not in inspector.get_table_names()
            assert "reservation_id" not in {column["name"] for column in inspector.get_columns("quota_transactions")}
    finally:
        try:
            _reset_public_schema(engine)
        finally:
            engine.dispose()


def test_batch_submission_migration_backfills_and_preserves_config_on_downgrade(tmp_path):
    database = tmp_path / 'batch-roundtrip.db'
    env = {**os.environ, 'NARRIFY_DATABASE_URL': f'sqlite:///{database.as_posix()}', 'NARRIFY_AUTO_CREATE_SCHEMA': 'false'}
    _run(env, '-m', 'alembic', '-c', 'alembic.ini', 'upgrade', '0022_task_center_indexes')
    engine = sa.create_engine(env['NARRIFY_DATABASE_URL'])
    now = datetime.now(timezone.utc)
    metadata = sa.MetaData()
    users = sa.Table('users', metadata, autoload_with=engine)
    projects = sa.Table('projects', metadata, autoload_with=engine)
    tasks = sa.Table('tasks', metadata, autoload_with=engine)
    events = sa.Table('task_events', metadata, autoload_with=engine)
    with engine.begin() as db:
        db.execute(users.insert().values(id='batch-user', username='batchuser', email='batch@example.test', display_name='Batch',
                                        password_hash='unused', role='user', is_active=True, created_at=now, updated_at=now))
        db.execute(projects.insert().values(id='batch-project', owner_id='batch-user', name='Batch', description='', directory_key='batchuser/Batch', created_at=now, updated_at=now))
        db.execute(tasks.insert().values(id='batch-task', owner_id='batch-user', project_id='batch-project', task_type='tts.batch',
                                        status='succeeded', payload={'scripts': ['one.json', 'two.json']}, progress=100, error_code='', error_message='', created_at=now, updated_at=now))
        db.execute(events.insert().values(id='batch-event', task_id='batch-task', sequence=7, event_type='succeeded', payload={}, created_at=now))
    engine.dispose()
    _run(env, '-m', 'alembic', '-c', 'alembic.ini', 'upgrade', 'head')
    metadata = sa.MetaData()
    tasks = sa.Table('tasks', metadata, autoload_with=engine)
    batches = sa.Table('task_batches', metadata, autoload_with=engine)
    with engine.begin() as db:
        row = db.execute(sa.select(tasks).where(tasks.c.id == 'batch-task')).one()
        assert row.event_sequence == 7 and row.admission_units == 2 and row.ui_state is None
        db.execute(batches.insert().values(id='batch-receipt', owner_id='batch-user', project_id='batch-project', task_type='tts.batch',
                                          idempotency_key='batch-key', request_hash='a' * 64, config={'tts': {'batch_concurrency': 4}}, task_ids=['batch-task'], created_at=now))
        db.execute(tasks.update().where(tasks.c.id == 'batch-task').values(batch_id='batch-receipt', payload={'scripts': ['one.json'], '_batch_config_id': 'batch-receipt'}))
    engine.dispose()
    _run(env, '-m', 'alembic', '-c', 'alembic.ini', 'downgrade', '0022_task_center_indexes')
    tasks = sa.Table('tasks', sa.MetaData(), autoload_with=engine)
    with engine.connect() as db:
        payload = db.scalar(sa.select(tasks.c.payload).where(tasks.c.id == 'batch-task'))
        assert payload['config']['tts']['batch_concurrency'] == 4 and '_batch_config_id' not in payload
    assert 'task_batches' not in sa.inspect(engine).get_table_names()
    engine.dispose()
    _run(env, '-m', 'alembic', '-c', 'alembic.ini', 'upgrade', 'head')


def test_portable_keys_backfill_preserves_collisions_and_roundtrips(tmp_path):
    database = tmp_path / "portable-keys.db"
    env = {**os.environ, "NARRIFY_DATABASE_URL": f"sqlite:///{database.as_posix()}", "NARRIFY_AUTO_CREATE_SCHEMA": "false"}
    _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "0025_mechanical_audio_permits")
    engine = sa.create_engine(env["NARRIFY_DATABASE_URL"])
    now = datetime.now(timezone.utc)
    metadata = sa.MetaData()
    users, projects, files = [sa.Table(name, metadata, autoload_with=engine) for name in ("users", "projects", "project_files")]
    keys = [f"owner/project/03_parsed_json/chapter-{i}.json" for i in range(1000)]
    keys += ["owner/project/03_parsed_json/Straße.json", "owner/project/03_parsed_json/STRASSE.json",
             "owner/project/03_parsed_json/é.json", "owner/project/03_parsed_json/e\u0301.json"]
    keys.append("owner/project/03_parsed_json/" + "ΐ" * 600 + ".json")
    with engine.begin() as connection:
        connection.execute(users.insert().values(id="owner", username="owner", email="portable@example.test",
            display_name="Owner", password_hash="unused", role="user", is_active=True, created_at=now, updated_at=now))
        connection.execute(projects.insert().values(id="project", owner_id="owner", name="Book", description="",
            directory_key="owner/project", last_selected_at=now, created_at=now, updated_at=now))
        connection.execute(files.insert(), [dict(id=f"file-{i:04d}", owner_id="owner", project_id="project",
            original_name=key.split("/")[-1], object_key=key, content_type="application/json", size_bytes=1,
            sha256="a" * 64, kind="artifact", created_at=now) for i, key in enumerate(keys)])
    _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
    with engine.connect() as connection:
        table = sa.Table("project_files", sa.MetaData(), autoload_with=connection)
        rows = connection.execute(sa.select(table.c.object_key, table.c.object_key_normalized)).all()
        assert len(rows) == len(keys)
        from backend.core.object_keys import object_key_lookup_key
        assert all(row.object_key_normalized == object_key_lookup_key(row.object_key) for row in rows)
        index = next(index for index in sa.inspect(connection).get_indexes("project_files") if index["name"] == "ix_project_files_normalized")
        assert not index["unique"]
        assert {"current_deliveries", "delivery_index_state"} <= set(sa.inspect(connection).get_table_names())
    _run(env, "-m", "alembic", "-c", "alembic.ini", "downgrade", "0025_mechanical_audio_permits")
    _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM project_files")) == len(keys)
    engine.dispose()
