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
        assert "next_attempt_at" in {column["name"] for column in inspector.get_columns("tasks")}
        assert "uq_quota_hold_attempt_operation" in {index["name"] for index in inspector.get_indexes("quota_holds")}
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
        _run(env, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head")
        with engine.connect() as connection:
            project_ids = set(connection.scalars(sa.select(projects.c.id)))
            workspace_rows = {row.id: row for row in connection.execute(sa.select(workspaces))}
        assert project_ids == {"project-only", "workspace-only"}
        assert set(workspace_rows) == project_ids
        assert workspace_rows["project-only"].directory_key == "legacy/project-only"
    finally:
        engine.dispose()
