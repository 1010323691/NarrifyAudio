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


@pytest.fixture
def postgres_workspace_sessions(monkeypatch):
    url=os.environ.get('NARRIFY_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('set NARRIFY_TEST_POSTGRES_URL for concurrent workspace admission')
    engine=create_engine(url)
    if engine.dialect.name!='postgresql':
        engine.dispose()
        pytest.skip('PostgreSQL required')
    schema='workspace_admission_'+uuid.uuid4().hex
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        scoped=engine.execution_options(schema_translate_map={None:schema})
        Base.metadata.create_all(scoped)
        sessions=sessionmaker(bind=scoped,expire_on_commit=False)
        monkeypatch.setattr(task_worker,'SessionLocal',sessions)
        yield sessions
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()


@pytest.mark.parametrize('parallel', [False,True])
def test_postgres_concurrent_admission_defers_conflicts_without_attempts(postgres_workspace_sessions,parallel):
    from concurrent.futures import ThreadPoolExecutor
    sessions=postgres_workspace_sessions
    owner=str(uuid.uuid4())
    with sessions.begin() as db:
        db.add(User(id=owner,email=owner+'@example.invalid',username='u'+owner,password_hash='test'))
        db.flush()
        project=Project(owner_id=owner,name='Long model task',directory_key=owner+'/project')
        db.add(project);db.add(UserQuotaAccount(user_id=owner));db.flush()
        tasks=[Task(owner_id=owner,project_id=project.id,task_type='tts.merge' if parallel else 'tts.reset',
                    payload={'package':f'chapter-{i}'} if parallel else {'scripts':[]}) for i in range(2)]
        db.add_all(tasks);db.flush();ids=[t.id for t in tasks]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(lambda tid:task_worker.claim_task(tid,'pg-'+tid),ids))
    assert sum(c is not None for c in claims)==(2 if parallel else 1)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(TaskAttempt))==(2 if parallel else 1)
        if not parallel:
            blocked=next(tid for tid,c in zip(ids,claims) if c is None)
            assert db.get(Task,blocked).status=='pending'
            assert task_worker.claim_fair_task('fair-blocked',task_types=('tts.reset',)) is None
