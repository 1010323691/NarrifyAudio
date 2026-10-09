"""Stage 8 history-window verification: 1000 chapters x 20 parse rounds.

Seeds a full task history in a private SQLite database and verifies the
parse-state read materializes only the nearest tasks per chapter (SQL window
aggregation) instead of the whole history. The last round is always failed so
every chapter contributes both rows — the latest task (failed) and the latest
succeeded task — and the expected materialization is exactly 2 per chapter.
No production service, Redis, user workspace or model calls are used.
"""
import argparse
import json
import os
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(chapters=1000, rounds=20):
    with tempfile.TemporaryDirectory(prefix="narrify-history-window-") as temporary:
        isolated = Path(temporary)
        os.environ.update(NARRIFY_DATABASE_URL=f"sqlite:///{isolated / 'history.sqlite'}",
                          NARRIFY_STORAGE_ROOT=str(isolated / "storage"), NARRIFY_AUTO_CREATE_SCHEMA="true",
                          NARRIFY_BOOTSTRAP_ADMIN_EMAIL="", NARRIFY_BOOTSTRAP_ADMIN_PASSWORD="",
                          NARRIFY_REGISTRATION_ENABLED="true", NARRIFY_COOKIE_SECURE="false")
        from fastapi.testclient import TestClient
        from sqlalchemy import select
        from backend.core import paths
        paths.PROJECT_ROOT = isolated
        paths.MUSIC_LIBRARY_DIR = isolated / "music_library"
        from backend.main import app
        from backend.platform.database import SessionLocal, engine, lock_engine
        from backend.platform.models import Task, TaskResult, User, utcnow
        from backend.services import script_parse_state as state

        started = time.monotonic()
        with TestClient(app) as client:
            suffix = uuid4().hex[:12]
            registration = client.post("/api/auth/register", json={
                "username": f"history{suffix}", "email": f"{suffix}@example.test",
                "password": "isolated-test-123"})
            assert registration.status_code == 201, registration.status_code
            username = registration.json()["user"]["username"]
            project_id = client.get("/api/v1/projects").json()[0]["id"]
            seed_started = time.monotonic()
            with SessionLocal.begin() as db:
                user = db.scalar(select(User).where(User.username == username))
                for chapter in range(chapters):
                    name = f"{chapter:04d}_章.txt"
                    base = utcnow()
                    for round_index in range(rounds):
                        # The newest round always failed, so the latest-task
                        # and latest-succeeded windows return different rows.
                        succeeded = round_index != rounds - 1
                        created = base + timedelta(seconds=round_index)
                        task = Task(
                            owner_id=user.id, project_id=project_id, task_type="script.parse",
                            status="succeeded" if succeeded else "failed", progress=100,
                            payload={"source_name": name, "input_file_id": f"seed-{name}", "config": {}},
                            created_at=created, updated_at=created)
                        db.add(task)
                        db.flush()
                        if succeeded:
                            db.add(TaskResult(task_id=task.id, result={"name": name, "file_id": None}))
            seed_seconds = time.monotonic() - seed_started
            with SessionLocal() as db:
                names = [f"{chapter:04d}_章.txt" for chapter in range(chapters)]
                read_started = time.monotonic()
                tasks = state._parse_tasks(db, user.id, project_id, names, [])
                read_seconds = time.monotonic() - read_started
        result = dict(
            chapters=chapters, rounds=rounds,
            total_task_rows=chapters * rounds,
            materialized=len(tasks),
            expected_materialized=2 * chapters,
            seed_seconds=round(seed_seconds, 3),
            state_read_seconds=round(read_seconds, 3),
            total_seconds=round(time.monotonic() - started, 3),
            environment="isolated SQLite, seeded script.parse history")
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        assert len(tasks) == 2 * chapters, (
            "history window must materialize exactly latest + latest-succeeded per chapter")
        engine.dispose()
        lock_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chapters", type=int, default=1000)
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()
    if not 10 <= args.chapters <= 10000 or not 2 <= args.rounds <= 100:
        parser.error("chapters must be 10..10000 and rounds 2..100")
    main(args.chapters, args.rounds)
