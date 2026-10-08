from __future__ import annotations

import json
from pathlib import Path

from backend.services.project_completion import project_completion
from backend.core.request_context import bind_workspace, bound_workspace, reset_workspace


def write(root: Path, name: str, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(json.dumps(value), encoding='utf-8')


def chapter(root: Path, name: str, done: int, total: int = 3):
    write(root, f'02_split_text/{name}.txt', 'chapter')
    entries = [{'speaker': 'A', 'text': f'line {i}'} for i in range(total)]
    write(root, f'03_parsed_json/{name}.json', entries)
    manifest = []
    for i in range(done):
        audio = f'05_audio_chunk/{name}/{i}.mp3'
        write(root, audio, b'audio')
        manifest.append({'index': i, 'ok': True, 'path': audio})
    write(root, f'05_audio_chunk/{name}/manifest.json', manifest)


def test_voice_hint_does_not_count_as_completed_voice(tmp_path):
    chapter(tmp_path, 'a', 0)
    write(tmp_path, '04_voice_profiles/voice_config.json', {
        'A': {'alias_of': 'B', 'type': 'foundation'},
        'B': {'type': 'clone', 'ref_audio': 'other.wav'},
    })
    assert project_completion(tmp_path)['04_voice_profiles']['completed'] == 0


def test_partial_book_is_not_complete_even_when_every_submitted_run_succeeded(tmp_path):
    for i in range(25):
        chapter(tmp_path, f'ch{i}', 3 if i == 0 else 1)
    write(tmp_path, '06_audio_merge/ch0.mp3', b'merged')
    write(tmp_path, '08_bgm/ch0.mp3', b'mixed')
    # Stale output must not count when its chapter's synthesis is incomplete.
    write(tmp_path, '06_audio_merge/ch1.mp3', b'old merged')
    write(tmp_path, '08_bgm/ch1.mp3', b'old mixed')
    result = project_completion(tmp_path)
    assert result['06_audio_merge'] == {'completed': 1, 'total': 25, 'unit': '章节', 'percent': 4}
    assert result['08_bgm']['percent'] == 4
    assert result['05_audio_chunk']['completed'] == 27
    assert result['05_audio_chunk']['total'] == 75
    assert result['05_audio_chunk']['percent'] == 36


def test_unparsed_chapters_are_included_in_denominator(tmp_path):
    chapter(tmp_path, 'a', 3)
    write(tmp_path, '02_split_text/b.txt', 'unparsed')
    result = project_completion(tmp_path)
    assert result['03_parsed_json']['percent'] == 50
    assert result['06_audio_merge']['total'] == 2
    assert result['05_audio_chunk']['percent'] is None
    assert result['04_voice_profiles']['percent'] is None


def test_non_utf8_original_uses_the_supported_text_decoder_for_split_progress(tmp_path):
    write(tmp_path, '01_input/book.txt', '第一章 标题\n内容\n第二章 标题\n内容'.encode('gb18030'))
    write(tmp_path, '02_split_text/第1章.txt', '内容')

    result = project_completion(tmp_path)

    assert result['02_split_text']['completed'] == 1
    assert result['02_split_text']['total'] == 2
    assert result['02_split_text']['percent'] == 50


def test_undecodable_original_does_not_claim_an_unknown_split_percentage(tmp_path):
    write(tmp_path, '01_input/book.txt', b'\xff\x00\xfe')

    result = project_completion(tmp_path)

    assert result['02_split_text']['percent'] is None


def test_missing_audio_reduces_completion_and_does_not_change_bound_project(tmp_path):
    chapter(tmp_path, 'a', 3)
    write(tmp_path, '06_audio_merge/a.mp3', b'merged')
    (tmp_path / '05_audio_chunk/a/1.mp3').unlink()
    token = bind_workspace(tmp_path / 'other-project')
    try:
        result = project_completion(tmp_path)
        assert result['05_audio_chunk']['completed'] == 2
        assert result['06_audio_merge']['completed'] == 0
        assert bound_workspace() == (tmp_path / 'other-project').resolve()
    finally:
        reset_workspace(token)


def test_voice_readiness_and_changed_voice_invalidate_old_audio(tmp_path):
    chapter(tmp_path, 'a', 3)
    write(tmp_path, '04_voice_profiles/voice_config.json', {'A': {'type': 'custom'}})
    result = project_completion(tmp_path)
    assert result['04_voice_profiles']['percent'] == 100
    assert result['05_audio_chunk']['completed'] == 0


def test_all_chapters_must_have_current_outputs_to_reach_100(tmp_path):
    chapter(tmp_path, 'a', 3)
    chapter(tmp_path, 'b', 3)
    for name in ('a', 'b'):
        write(tmp_path, f'06_audio_merge/{name}.mp3', b'merged')
        write(tmp_path, f'08_bgm/{name}.mp3', b'mixed')
    result = project_completion(tmp_path)
    assert result['05_audio_chunk']['percent'] == 100
    assert result['06_audio_merge']['percent'] == 100
    assert result['08_bgm']['percent'] == 100


def test_corrupt_script_and_checked_duplicate_do_not_inflate_coverage(tmp_path):
    chapter(tmp_path, 'a', 3)
    write(tmp_path, '03_parsed_json/a_checked.json', [{'text': 'duplicate'}])
    write(tmp_path, '02_split_text/b.txt', 'chapter')
    write(tmp_path, '03_parsed_json/b.json', [42])
    result = project_completion(tmp_path)
    assert result['03_parsed_json']['completed'] == 1
    assert result['03_parsed_json']['percent'] == 50


def test_overview_sections_match_full_completion_without_audio_tree_walk(tmp_path, monkeypatch):
    import backend.engines.project_completion as service
    chapter(tmp_path, 'a', 3)
    chapter(tmp_path, 'b', 1)
    write(tmp_path, '06_audio_merge/a.mp3', b'merged')
    expected = project_completion(tmp_path)
    original = service.iter_regular_project_files
    walked = []
    def scan(root):
        walked.append(root.name)
        assert root.name not in {'05_audio_chunk', '00_temp', 'logs', '07_output'}
        yield from original(root)
    monkeypatch.setattr(service, 'iter_regular_project_files', scan)
    text = project_completion(tmp_path, section='text')
    assert walked == ['01_input', '02_split_text']
    assert text == {'02_split_text': expected['02_split_text']}
    walked.clear()
    catalog = project_completion(tmp_path, section='catalog')
    assert walked == ['01_input', '02_split_text', '03_parsed_json']
    assert {**text, **catalog, **project_completion(tmp_path, section='production')} == expected


def test_symlink_manifest_and_audio_are_not_counted(tmp_path):
    chapter(tmp_path, 'a', 1)
    external = tmp_path.parent / 'outside-overview.mp3'
    external.write_bytes(b'external')
    audio = tmp_path / '05_audio_chunk/a/0.mp3'
    audio.unlink()
    audio.symlink_to(external)
    assert project_completion(tmp_path)['05_audio_chunk']['completed'] == 0
