"""Real bounded backfill and path-local reads under 20 rounds of history."""
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, event, insert
from sqlalchemy.orm import sessionmaker
from backend.core.safe_filesystem import file_identity
from backend.platform.database import Base
from backend.platform.models import Project, Task, TaskResult, User, utcnow
from backend.platform.delivery_index import backfill_project
from backend.platform.resource_delivery import require_delivery


def measure(count, rounds=20):
    with tempfile.TemporaryDirectory(prefix="narrify-delivery-index-") as temporary:
        root = Path(temporary) / "storage"
        workspace = root / "owner" / "project"
        workspace.mkdir(parents=True)
        engine = create_engine(f"sqlite:///{temporary}/delivery.db")
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False, autoflush=False)
        with sessions.begin() as db:
            db.add(User(id="owner", username="owner", email="pressure@example.test", password_hash="unused"))
            db.flush()
            db.add(Project(id="project", owner_id="owner", name="Book", directory_key="owner/project"))
        tasks, results = [], []
        for i in range(count):
            relative = f"06_audio_merge/chapter-{i}.mp3"
            path = workspace / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"audio")
            identity = list(file_identity(path.stat()))
            for history in range(rounds):
                task_id = f"task-{i:04d}-{history:03d}"
                tasks.append(dict(id=task_id, owner_id="owner", project_id="project", task_type="tts.merge",
                    status="succeeded", finished_at=utcnow()))
                results.append(dict(task_id=task_id, result={"deliveries": [{"relative_path": relative, "identity": identity}]}))
        with sessions.begin() as db:
            for offset in range(0, len(tasks), 100):
                db.execute(insert(Task), tasks[offset:offset + 100])
                db.execute(insert(TaskResult), results[offset:offset + 100])
        with patch("backend.platform.storage.configured_storage_root", return_value=root):
            start = time.perf_counter()
            chunks = 0
            while True:
                with sessions.begin() as db:
                    done = backfill_project(db, db.get(User, "owner"), "project")
                chunks += 1
                if done:
                    break
            backfill_seconds = time.perf_counter() - start
            queries = []
            listener = lambda c, cur, statement, *args: queries.append(statement)
            event.listen(engine, "before_cursor_execute", listener)
            start = time.perf_counter()
            with sessions() as db:
                user = db.get(User, "owner")
                for i in range(count):
                    record = require_delivery(db, user, "project", f"06_audio_merge/chapter-{i}.mp3")
                    assert record["task_id"] == f"task-{i:04d}-{rounds - 1:03d}"
            seconds = time.perf_counter() - start
            event.remove(engine, "before_cursor_execute", listener)
        assert not any("task_results" in query for query in queries)
        assert len(queries) <= count * 6 + 2, len(queries)
        print(json.dumps(dict(chapters=count, history_rounds=rounds, history_tasks=len(tasks),
            backfill_chunks=chunks, backfill_seconds=round(backfill_seconds, 3),
            path_reads=count, path_read_sql=len(queries), history_result_queries=0,
            path_read_seconds=round(seconds, 3))), flush=True)
        engine.dispose()


if __name__ == "__main__":
    for count in (100, 500, 1000):
        measure(count)
