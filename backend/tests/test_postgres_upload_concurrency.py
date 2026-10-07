"""Opt-in real row-lock upload regression, on one ASGI event loop."""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import uuid

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from backend.api import files
from backend.platform.database import Base, get_db
from backend.platform.deps import AuthContext, get_auth_context
from backend.platform.models import Project, ProjectFile, User
from backend.platform import storage


@pytest.mark.skipif(not os.environ.get("NARRIFY_TEST_POSTGRES_URL"),
                    reason="set NARRIFY_TEST_POSTGRES_URL for real upload row locks")
def test_large_concurrent_uploads_do_not_block_one_event_loop(tmp_path, monkeypatch):
    engine = create_engine(os.environ["NARRIFY_TEST_POSTGRES_URL"],
                           connect_args={"options": "-c statement_timeout=3000"})
    schema = f"narrify_upload_{uuid.uuid4().hex}"
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("PostgreSQL is required")
    release_first = threading.Event()
    first_read = threading.Event()
    second_lock = threading.Event()
    guard = threading.Lock()
    lock_count = 0
    read_count = 0
    timer = threading.Timer(8, release_first.set)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        scoped = engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(scoped)
        sessions = sessionmaker(scoped, expire_on_commit=False)
        with sessions.begin() as db:
            user = User(username="upload-reader", email="upload@example.test", password_hash="unused")
            db.add(user)
            db.flush()
            project = Project(owner_id=user.id, name="Book", directory_key="upload-reader/Book")
            db.add(project)
            db.flush()
            owner_id, project_id = user.id, project.id
        monkeypatch.setattr(storage, "configured_storage_root", lambda _db=None: tmp_path)
        monkeypatch.setattr(files._common, "require_workspace", lambda: None)

        def database():
            with sessions() as db:
                yield db

        def context(db: Session = Depends(get_db)):
            from types import SimpleNamespace
            return AuthContext(db.get(User, owner_id), SimpleNamespace(active_project_id=project_id))

        app = FastAPI()
        app.include_router(files.router)
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_auth_context] = context

        def before_execute(conn, cursor, statement, params, context, executemany):
            nonlocal lock_count
            if "FOR UPDATE" in statement and "projects" in statement:
                with guard:
                    lock_count += 1
                    if lock_count == 2:
                        second_lock.set()

        event.listen(engine, "before_cursor_execute", before_execute)
        original_read = tempfile.SpooledTemporaryFile.read

        def controlled_read(handle, *args, **kwargs):
            nonlocal read_count
            if handle._rolled:
                with guard:
                    read_count += 1
                    first = read_count == 1
                if first:
                    first_read.set()
                    if not release_first.wait(8):
                        raise TimeoutError("first upload was not released")
            return original_read(handle, *args, **kwargs)

        monkeypatch.setattr(tempfile.SpooledTemporaryFile, "read", controlled_read)
        timer.start()

        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                async def upload(byte):
                    return await client.post("/api/files/upload", files={"file": ("book.txt", byte * (2 * 1024 * 1024), "text/plain")})
                first = asyncio.create_task(upload(b"a"))
                assert await asyncio.to_thread(first_read.wait, 4)
                second = asyncio.create_task(upload(b"b"))
                assert await asyncio.to_thread(second_lock.wait, 4)
                # This event-loop turn must run while one request owns the row
                # and the other waits on it; only then release the first read.
                await asyncio.sleep(0.05)
                release_first.set()
                responses = await asyncio.wait_for(asyncio.gather(first, second), 6)
                assert [r.status_code for r in responses] == [200, 200]
                return [r.json() for r in responses]

        responses = asyncio.run(exercise())
        assert responses[0]["path"] != responses[1]["path"]
        with sessions() as db:
            rows = list(db.scalars(select(ProjectFile)))
            assert len(rows) == 2
            for row in rows:
                path = tmp_path / row.object_key
                assert row.size_bytes == path.stat().st_size == 2 * 1024 * 1024
                assert row.sha256 == storage.sha256_file(path)
    finally:
        release_first.set()
        timer.cancel()
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()
