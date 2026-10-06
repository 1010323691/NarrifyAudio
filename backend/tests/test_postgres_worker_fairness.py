"""PostgreSQL must schedule untouched accounts before already-served users."""
from datetime import timedelta
import os
import uuid

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.platform import task_worker
from backend.platform.database import Base
from backend.platform.models import Project, Task, TaskAttempt, User, UserQuotaAccount, utcnow


@pytest.mark.skipif(
    not os.environ.get("NARRIFY_TEST_POSTGRES_URL"),
    reason="set NARRIFY_TEST_POSTGRES_URL to run the PostgreSQL scheduling regression",
)
def test_fair_claim_serves_each_new_user_before_returning_to_first(monkeypatch):
    engine = create_engine(os.environ["NARRIFY_TEST_POSTGRES_URL"])
    if engine.dialect.name != "postgresql":
        engine.dispose()
        pytest.skip("NARRIFY_TEST_POSTGRES_URL must use PostgreSQL")
    schema = f"worker_fairness_{uuid.uuid4().hex}"
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        scoped_engine = engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(scoped_engine)
        sessions = sessionmaker(bind=scoped_engine, expire_on_commit=False)
        monkeypatch.setattr(task_worker, "SessionLocal", sessions)
        owners = [str(uuid.uuid4()) for _ in range(3)]
        created = utcnow() - timedelta(minutes=1)
        with sessions.begin() as db:
            for index, owner in enumerate(owners):
                db.add(User(id=owner, email=f"{owner}@example.invalid", username=f"u{index}", password_hash="test"))
                db.flush()
                project = Project(owner_id=owner, name=f"User {index}", directory_key=f"u{index}/project")
                db.add(project)
                db.add(UserQuotaAccount(user_id=owner))
                db.flush()
                for job in range(2):
                    db.add(Task(owner_id=owner, project_id=project.id, task_type="text.format",
                                created_at=created + timedelta(seconds=index * 2 + job)))
        claimed = []
        for index in range(6):
            claim = task_worker.claim_fair_task(f"fair-{index}", task_types=("text.format",))
            assert claim is not None
            claimed.append(claim)
        assert [claim.owner_id for claim in claimed] == owners * 2
        assert len({claim.task_id for claim in claimed}) == 6
        with sessions() as db:
            assert db.scalar(select(func.count()).select_from(TaskAttempt)) == 6
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()
