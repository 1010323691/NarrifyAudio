from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend.platform.database import Base
from backend.platform.models import Project, Task, TaskEvent, User, utcnow
from backend.services.project_overview import overview_tasks


def test_overview_tasks_are_bounded_owner_scoped_and_do_not_expand_payloads():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([User(id='u', username='u', email='u@example.test', password_hash='x'), User(id='v', username='v', email='v@example.test', password_hash='x')])
        db.flush()
        db.add_all([Project(id='p', owner_id='u', name='book', directory_key='p'), Project(id='foreign', owner_id='v', name='foreign', directory_key='foreign'), Project(id='trash', owner_id='u', name='trash', directory_key='trash', deleted_at=utcnow())])
        db.flush()
        for i in range(301):
            db.add(Task(id=f't{i:03}', owner_id='u', project_id='p', task_type='script.parse', status='failed', created_at=utcnow()+timedelta(seconds=i), payload={'source_name': f'ch{i}.txt', 'config': 'x'*10000}, error_message='error'*500))
        db.add(Task(id='active', owner_id='u', project_id='p', task_type='script.parse', status='running', created_at=utcnow()+timedelta(seconds=400), payload={'source_name': 'ch300.txt'}))
        db.add(Task(id='other', owner_id='v', project_id='foreign', task_type='tts.batch', status='failed'))
        db.flush()
        db.add(TaskEvent(task_id='active', event_type='log', sequence=1, payload={'message': 'x'*10000}))
        db.commit()
        statements = []
        def capture(conn, cursor, statement, parameters, context, many): statements.append(statement.lower())
        event.listen(engine, 'before_cursor_execute', capture)
        try:
            result = overview_tasks(db, 'u', 'p')
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        assert len(statements) == 3
        assert result['failure_count'] == 300
        assert result['statuses'][0] == {'task_type': 'script.parse', 'status': 'running', 'count': 1}
        assert len(result['failures']) == 3
        assert all(len(row['error_message']) <= 500 for row in result['failures'])
        assert result['failures'][0]['id'] == 't299'
        assert all('task_events' not in sql and 'task_results' not in sql for sql in statements)
        assert all(', tasks.payload as' not in sql for sql in statements)
        for project in ('foreign', 'trash', 'missing'):
            with pytest.raises(ValueError): overview_tasks(db, 'u', project)
    engine.dispose()
