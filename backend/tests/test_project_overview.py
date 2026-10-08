from datetime import timedelta
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend.platform.database import Base
from backend.platform.models import Project, Task, TaskEvent, TaskResult, User, utcnow
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


def test_deleted_analysis_reports_use_durable_provenance_not_names(tmp_path):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    parsed = tmp_path / '03_parsed_json'
    parsed.mkdir()
    with Session(engine) as db:
        db.add_all([User(id=u, username=u, email=f'{u}@example.test', password_hash='x')
                    for u in ('u', 'v')])
        db.flush()
        db.add_all([Project(id=p, owner_id=u, name=p, directory_key=p)
                    for p, u in [('p', 'u'), ('other', 'u'), ('foreign', 'v')]])
        db.flush()
        reports = ['book_analysis.json'] + [f'book_analysis ({i}).json' for i in range(2, 5)]
        sources = [(name, 'u', 'p', 'book.analyze', 'succeeded', '03_parsed_json') for name in reports]
        sources += [
            ('other_analysis.json', 'u', 'other', 'book.analyze', 'succeeded', '03_parsed_json'),
            ('foreign_analysis.json', 'v', 'foreign', 'book.analyze', 'succeeded', '03_parsed_json'),
            ('script_analysis.json', 'u', 'p', 'script.parse', 'succeeded', '03_parsed_json'),
            ('failed_analysis.json', 'u', 'p', 'book.analyze', 'failed', '03_parsed_json'),
            ('temp_analysis.json', 'u', 'p', 'book.analyze', 'succeeded', '00_temp'),
        ]
        for i, (name, owner, project, kind, status, module) in enumerate(sources):
            db.add(Task(id=f'source{i}', owner_id=owner, project_id=project,
                        task_type=kind, status=status))
            db.flush()
            db.add(TaskResult(task_id=f'source{i}', result={
                'object_key': f'user/{project}/{module}/{name}', 'analysis': {'large': 'x'*10000},
            }))
            db.add(Task(id=f'failure{i}', owner_id='u', project_id='p', task_type='tts.batch',
                        status='failed', payload={'scripts': [name]}, error_message=name))
        db.add(Task(id='missing', owner_id='u', project_id='p', task_type='tts.batch',
                    status='failed', payload={'scripts': ['missing_analysis.json']}))
        db.add(Task(id='mixed', owner_id='u', project_id='p', task_type='tts.batch',
                    status='failed', payload={'scripts': [reports[0], 'chapter.json']}))
        db.commit()
        result = overview_tasks(db, 'u', 'p', root=tmp_path)
        assert result['failure_count'] == 7  # Five unproven reports, a missing chapter and a mixed batch.
        assert all(db.get(Task, f'failure{i}').status == 'failed' for i in range(4))
        # A real script now occupying an old report path must remain actionable.
        (parsed / reports[0]).write_text('[{"text":"chapter"}]')
        assert overview_tasks(db, 'u', 'p', root=tmp_path)['failure_count'] == 8
        (parsed / reports[0]).write_text('{}')
        assert overview_tasks(db, 'u', 'p', root=tmp_path)['failure_count'] == 8
        (parsed / reports[0]).write_text('corrupt')
        assert overview_tasks(db, 'u', 'p', root=tmp_path)['failure_count'] == 8
    engine.dispose()


def test_overview_excludes_legacy_report_failures_but_preserves_real_errors(tmp_path):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    parsed = tmp_path / '03_parsed_json'
    parsed.mkdir()
    (parsed / 'book_analysis.json').write_text(json.dumps({
        'source': 'book.txt', 'encoding': 'utf-8', 'base': 'book',
        'total_chars': 100, 'chapters': [], 'chapter_count': 0, 'filenames': [],
        'expected_format': '第 X 章', 'sequence': {},
    }))
    # Naming is not identity: a real script with an analysis suffix remains actionable.
    (parsed / 'chapter_analysis.json').write_text('[{"text":"hello"}]')
    (parsed / 'broken.json').write_text('broken')
    (parsed / 'object.json').write_text('{}')
    (parsed / 'partial_analysis.json').write_text('{"chapters": []}')
    with Session(engine) as db:
        db.add(User(id='u', username='u', email='u@example.test', password_hash='x'))
        db.flush()
        db.add(Project(id='p', owner_id='u', name='book', directory_key='p'))
        db.flush()
        entries = [
            ('report', 'failed', {'scripts': ['book_analysis.json']}),
            ('legacy', 'timeout', {'script': 'book_analysis.json'}),
            ('chapter', 'failed', {'scripts': ['chapter_analysis.json']}),
            ('broken', 'failed', {'scripts': ['broken.json']}),
            ('missing', 'failed', {'scripts': ['missing.json']}),
            ('unknown', 'failed', {}),
            ('object', 'failed', {'scripts': ['object.json']}),
            ('partial', 'failed', {'scripts': ['partial_analysis.json']}),
            ('mixed', 'failed', {'scripts': ['book_analysis.json', 'chapter_analysis.json']}),
        ]
        for id, status, payload in entries:
            db.add(Task(id=id, owner_id='u', project_id='p', task_type='tts.batch',
                        status=status, payload=payload, error_message=id))
        db.commit()
        result = overview_tasks(db, 'u', 'p', root=tmp_path)
        assert result['failure_count'] == 7
        assert result['statuses'] == [{'task_type': 'tts.batch', 'status': 'failed', 'count': 7}]
        assert all(row['id'] not in {'report', 'legacy'} for row in result['failures'])
        # History is preserved. With only the obsolete reports left, card health is clear.
        for id, _, _ in entries:
            if id not in {'report', 'legacy'}:
                db.get(Task, id).status = 'succeeded'
        db.commit()
        assert overview_tasks(db, 'u', 'p', root=tmp_path) == {
            'statuses': [], 'failures': [], 'failure_count': 0,
        }
        assert db.get(Task, 'report').status == 'failed'
        # Replacing a report with a real script restores its actionable failure.
        (parsed / 'book_analysis.json').write_text('[{"text":"restored"}]')
        assert overview_tasks(db, 'u', 'p', root=tmp_path)['failure_count'] == 2
    engine.dispose()
