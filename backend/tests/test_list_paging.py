from datetime import timedelta
from types import SimpleNamespace
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from backend.platform.database import Base
from backend.platform.models import AuditLog, Project, Task, User, utcnow
from backend.core.paths import Layout
from backend.services.list_paging import project_page, page_text_state, entry_states, quota_page
from backend.services.admin_lists import event_page
from backend.services import script_parse_state
from backend.api import tts, bgm, music, admin, admin_resources


@pytest.fixture
def scope(tmp_path, monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(id='u', username='u', email='u@test.local', password_hash='unused')
        other = User(id='v', username='v', email='v@test.local', password_hash='unused')
        db.add_all([user, other]); db.flush()
        db.add_all([Project(id='book', owner_id='u', name='book', directory_key='book'), Project(id='foreign', owner_id='v', name='foreign', directory_key='foreign')]); db.commit()
        layout = Layout(tmp_path)
        for path in [layout.parsed_json, layout.split_text, layout.voice_profiles, layout.audio_chunk, layout.audio_merge, layout.bgm]: path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(tts, 'resolve_layout', lambda: layout)
        monkeypatch.setattr(bgm, 'resolve_layout', lambda: layout)
        ctx = SimpleNamespace(user=user, session=SimpleNamespace(active_project_id='book'))
        yield db, ctx, layout
    engine.dispose()


def test_project_pages_filter_before_limit_and_isolate_owners(scope):
    db, ctx, _ = scope
    db.add_all([Project(id=f'p{i:03}', owner_id='u', name=f'project-{i:03}', directory_key=f'p{i:03}') for i in range(31)])
    db.commit()
    first = project_page(db, ctx.user, 1, 10, 'project')
    second = project_page(db, ctx.user, 2, 10, 'project')
    assert first['pagination']['total'] == 31
    assert len(first['items']) == len(second['items']) == 10
    assert not ({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
    assert project_page(db, ctx.user, 1, 10, 'foreign')['items'] == []


def test_default_synthesis_page_only_checks_visible_files_and_bulk_keeps_all(scope, monkeypatch):
    db, ctx, layout = scope
    names = [f'第 {i:03} 章.json' for i in range(125)]
    monkeypatch.setattr(tts, 'resolve_parsed_json_all', lambda: [layout.parsed_json / n for n in names])
    monkeypatch.setattr(tts, '_read_voice_config', lambda _: {})
    checked = []
    def status(name, *_):
        checked.append(name)
        return {'name': name, 'is_script': True, 'complete': False, 'missing': []}
    monkeypatch.setattr(tts, '_cached_file_batch_status', status)
    page = tts.batch_list(page=2, page_size=10, db=db, ctx=ctx, selected=[names[0], 'removed.json'])
    assert checked == names[10:20]
    assert page['pagination']['total'] == 125
    assert page['missing_selected'] == ['removed.json']
    bulk = tts.batch_list(page=1, page_size=10, keys_only=True, db=db, ctx=ctx)
    assert [r['name'] for r in bulk['files']] == names
    # Listing/selection never creates or alters executable tasks.
    assert db.query(Task).count() == 0


def test_merge_page_enriches_only_page_and_full_selection_keeps_done_and_ready(scope, monkeypatch):
    db, ctx, layout = scope
    for i in range(61): (layout.audio_chunk / f'p{i:03}').mkdir()
    (layout.audio_merge / 'p000.mp3').touch()
    checked = []
    def status(name, *_):
        checked.append(name)
        return {'name': name, 'complete': True}
    monkeypatch.setattr(tts, '_package_merge_status', status)
    page = tts.merge_list(page=2, page_size=10, db=db, ctx=ctx)
    assert checked == [f'p{i:03}' for i in range(10, 20)]
    assert len(page['packages']) == 10
    full = tts.merge_list(keys_only=True, db=db, ctx=ctx)
    assert len(full['packages']) == 61
    assert full['packages'][0]['merged_filename'] == 'p000.mp3'


def test_voice_paging_enriches_only_current_roles_and_preserves_global_counts(scope, monkeypatch):
    _, _, layout = scope
    path = layout.parsed_json / 'book.json'
    path.write_text(json.dumps([{'speaker': f'role{i:03}', 'text': 'line'} for i in range(51)]))
    monkeypatch.setattr(tts, 'resolve_parsed_json_all', lambda: [path])
    checked = []
    monkeypatch.setattr(tts.V, 'effective_candidates', lambda entry: checked.append(entry) or [])
    result = tts.list_voices('__all__', page=2, page_size=10)
    assert len(checked) == len(result['speakers']) == 10
    assert result['speakers'][0]['name'] == 'role010'
    assert result['pagination']['counts']['all'] == 51
    checked.clear()
    assert len(tts.list_voices('__all__', summary_only=True)['speakers']) == 0
    assert checked == []


def test_bgm_default_page_does_not_load_other_chapter_timelines(scope, monkeypatch):
    db, ctx, layout = scope
    stems = [f'ch{i:03}' for i in range(87)]
    monkeypatch.setattr(bgm.Bgm, 'list_chapter_stems', lambda _: stems)
    checked = []
    monkeypatch.setattr(bgm, 'chapter_display_name', lambda stem, _: checked.append(stem) or stem)
    result = bgm.list_chapters(page=3, page_size=10, ctx=ctx, db=db)
    assert checked == stems[20:30]
    assert result['pagination']['total'] == 87
    full = bgm.list_chapters(page=1, page_size=10, keys_only=True, ctx=ctx, db=db)
    assert len(full['chapters']) == 87


def test_parse_page_checks_only_visible_chapters_and_selection_spans_all(scope, monkeypatch):
    db, ctx, _ = scope
    files = [{'name': f'ch{i:03}.txt', 'file_id': str(i)} for i in range(121)]
    chapters = [{'seq': i+1, 'key': str(i), 'title': f'chapter{i}', 'numStr': str(i)} for i in range(121)]
    monkeypatch.setattr(script_parse_state, '_source_section', lambda *_: {'mode': 'version', 'version': {'files': files, 'chapters': chapters}})
    monkeypatch.setattr(script_parse_state, '_text_format_busy', lambda *_: False)
    checked = []
    def build(_db, _user, _project, names):
        checked.extend(names)
        return [{'name': n, 'input': {'file_id': n, 'sha256': n}, 'latest_task': None, 'result_status': 'usable'} for n in names]
    monkeypatch.setattr(script_parse_state, '_build_file_states', build)
    result = script_parse_state.get_state(db, ctx.user, 'book', page=2, page_size=10)
    assert checked == [f'ch{i:03}.txt' for i in range(10,20)]
    assert len(result['source']['version']['files']) == len(result['chapter_refs']) == 10
    full = script_parse_state.get_state(db, ctx.user, 'book', page=1, page_size=10, filter='done', keys_only=True)
    assert len(full['items']) == 121
    assert all(r['result_status'] == 'usable' for r in full['items'])


def test_text_page_keeps_original_file_sequence_and_global_review_counts():
    chapters = [{'key': str(i), 'seq': i+1, 'title': f'ch{i}', 'numStr': str(i+1), 'orig_num': i//2,
                 'pending': True, 'adjusted': i % 2 == 0, 'reasons': ['duplicate'], 'matters': [str(i)]} for i in range(51)]
    state = {'version': {'chapters': chapters, 'files': [{'name': f'{i}.txt'} for i in range(51)], 'review_marks': ['0'], 'matters': [{'id': str(i)} for i in range(51)]}}
    result = page_text_state(state, 2, 10, filter='pending')
    assert result['version']['chapters'][0]['file']['name'] == '11.txt'
    assert len(result['version']['matters']) == 10
    assert result['pagination']['total'] == 50
    assert result['pagination']['counts']['all'] == 51
    assert len(state['version']['chapters']) == 51  # serialization cannot mutate worker state


def test_event_pages_do_not_truncate_old_matching_events(scope):
    db, _, _ = scope
    db.add_all([AuditLog(id=f'a{i:03}', actor_user_id='u', action='event', target_type='project', target_id='book', created_at=utcnow()-timedelta(seconds=i)) for i in range(131)])
    db.commit()
    result = event_page(db, 3, 50, 'info', 'system', 'event', 24, [])
    assert result['pagination']['total'] == 131
    assert len(result['items']) == 31
    assert result['items'][0]['id'] == 'a100'


def test_quota_page_does_not_scan_project_files(scope, monkeypatch):
    db, ctx, _ = scope
    result = quota_page(db, ctx.user, 1, 20, '', 'all')
    assert result['items'] == []
    assert result['daily'] == [0]*7
    assert result['registered_storage_bytes'] == 0


def test_paged_admin_users_never_walk_workspaces(scope, monkeypatch):
    db, ctx, _ = scope
    monkeypatch.setattr(admin, 'scan_project_directory', lambda *_: pytest.fail('paged users must not walk workspaces'))
    result = admin.list_users(db=db, page=1, page_size=1)
    assert len(result['items']) == 1
    assert result['pagination']['total'] == 2
    assert result['items'][0]['storage_bytes'] == 0


def test_enabled_music_is_filtered_before_paging(scope, monkeypatch):
    db, ctx, _ = scope
    monkeypatch.setattr(music.music_engine, 'load_index', lambda: {'tracks': {f't{i:03}.mp3': {'enabled': i >= 30} for i in range(55)}, 'tags': {}, 'folders': {}})
    monkeypatch.setattr(music.music_engine, 'load_suggestions', lambda: {'tracks': {}})
    monkeypatch.setattr(music, '_track_size', lambda _: 1)
    result = music.get_library(ctx=ctx, db=db, page=2, page_size=10, enabled_only=True)
    assert list(result['tracks']) == [f't{i:03}.mp3' for i in range(40,50)]
    assert result['pagination']['total'] == 25


def test_admin_task_page_reads_no_payload_or_logs(scope):
    db, ctx, _ = scope
    db.add_all([Task(id=f't{i:03}', owner_id=ctx.user.id, project_id='book', task_type='script.parse', status='succeeded', payload={'large': 'x'*10000}) for i in range(65)])
    db.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, many): statements.append(statement.lower())
    event.listen(db.bind, 'before_cursor_execute', capture)
    try:
        result = admin.list_all_tasks(db=db, page=2)
    finally:
        event.remove(db.bind, 'before_cursor_execute', capture)
    assert len(result['items']) == 15
    assert result['pagination']['total'] == 65
    reads = [s for s in statements if s.startswith('select') and 'limit' in s]
    assert reads and all('tasks.payload' not in s for s in reads)
    assert all('task_events' not in s and 'task_results' not in s for s in statements)
    assert len(statements) <= 4


def test_parse_summary_is_global_uses_two_status_axes_and_is_owner_scoped(scope, monkeypatch):
    db, ctx, _ = scope
    names = [f'c{i}.txt' for i in range(30)]
    monkeypatch.setattr(script_parse_state, '_source_section', lambda *_: {'mode': 'version', 'version': {'files': [{'name': n} for n in names]}})
    states = [{'name': n, 'result_status': 'usable', 'latest_task': None} for n in names]
    states[-1]['latest_task'] = {'id': 'active', 'status': 'running'}
    states[-2]['latest_task'] = {'id': 'failed', 'status': 'failed'}
    states[-3]['result_status'] = 'stale'
    checked = []
    monkeypatch.setattr(script_parse_state, '_build_file_states', lambda _db, _user, _project, requested: checked.extend(requested) or states)
    result = script_parse_state.get_state(db, ctx.user, 'book', page=2, filter='pending', summary_only=True)
    assert result == {'total': 30, 'done_count': 27, 'active_task_ids': ['active']}
    assert checked == names
    with pytest.raises(script_parse_state.ScriptParseError):
        script_parse_state.get_state(db, ctx.user, 'foreign', summary_only=True)


def test_parse_task_status_reads_scalars_without_full_payload_events_or_results(scope):
    db, ctx, _ = scope
    db.add(Task(id='parse', owner_id=ctx.user.id, project_id='book', task_type='script.parse', status='running', payload={'source_name': 'c.txt', 'large': 'x'*10000}))
    db.commit()
    owner_id = ctx.user.id
    statements = []
    def capture(conn, cursor, statement, parameters, context, many): statements.append(statement.lower())
    event.listen(db.bind, 'before_cursor_execute', capture)
    try:
        rows = script_parse_state._parse_tasks(db, owner_id, 'book', ['c.txt'])
    finally:
        event.remove(db.bind, 'before_cursor_execute', capture)
    assert len(rows) == 1
    assert rows[0].payload == {'source_name': 'c.txt', 'input_file_id': None}
    assert len(statements) == 1
    assert ', tasks.payload as' not in statements[0] and ', tasks.payload \n' not in statements[0]
    assert 'task_events' not in statements[0] and 'task_results' not in statements[0]
