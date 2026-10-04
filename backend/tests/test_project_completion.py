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
