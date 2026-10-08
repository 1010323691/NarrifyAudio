"""Managed-write invalidation is bounded, cross-process and crash aware."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest
from backend.core.workspace_epochs import managed_mutation, versions
from backend.core.pathio import rewrite_json_file
from backend.platform.artifact_publication import PublicationJournal


def workspace(tmp_path):
    root = tmp_path / 'book'
    (root / '00_temp').mkdir(parents=True)
    (root / '03_parsed_json').mkdir()
    (root / '05_audio_chunk/chapter').mkdir(parents=True)
    return root


def test_managed_deep_write_invalidates_only_its_module_and_exposes_writer(tmp_path):
    root = workspace(tmp_path)
    before = versions(root)
    path = root / '05_audio_chunk/chapter/manifest.json'
    with managed_mutation(path):
        during = versions(root)
        assert during[1]
        assert during[0][4] != before[0][4]
        assert during[0][2] == before[0][2]
        path.write_text('[]')
    after = versions(root)
    assert not after[1] and after[0][4] != during[0][4]
    rewrite_json_file(root / '03_parsed_json/a.json', [])
    assert versions(root)[0][2] != after[0][2]


def test_parallel_mutations_preserve_both_module_versions_and_clear_writers(tmp_path):
    root = workspace(tmp_path)
    def change(index):
        module = '03_parsed_json' if index % 2 else '05_audio_chunk/chapter'
        with managed_mutation(root / module / f'{index}.json'):
            (root / module / f'{index}.json').write_text('[]')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(change, range(20)))
    data = json.loads((root / '00_temp/workspace-epochs.json').read_text())
    assert set(data['versions']) == {'03_parsed_json', '05_audio_chunk'}
    assert data['writers'] == {}
    assert (root / '00_temp/workspace-epochs.json').stat().st_size < 1000


def test_journal_publication_and_rollback_both_invalidate(tmp_path):
    root = workspace(tmp_path)
    target = root / '03_parsed_json/chapter.json'
    target.write_text('old')
    source = root / '00_temp/staged.json'
    source.write_text('new')
    journal = PublicationJournal(root, root / '00_temp/publication.json')
    journal.prepare()
    before = versions(root)[0]
    journal.publish(journal.add(target), source)
    published = versions(root)[0]
    assert published != before
    journal.rollback()
    assert target.read_text() == 'old'
    assert versions(root)[0] != published


def test_dead_writer_marker_never_reuses_the_pre_write_version(tmp_path):
    root = workspace(tmp_path)
    before = versions(root)
    script = '''
from pathlib import Path
import sys, time
from backend.core.workspace_epochs import managed_mutation
root = Path(sys.argv[1])
with managed_mutation(root / '03_parsed_json/a.json'):
    (root / '03_parsed_json/a.json').write_text('[]')
    print('written', flush=True)
    time.sleep(60)
'''
    child = subprocess.Popen([sys.executable, '-c', script, str(root)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'written'
        during = versions(root)
        assert during[1] and during[0] != before[0]
        child.terminate()
        child.wait(timeout=10)
        after = versions(root)
        assert not after[1] and after[0] != before[0]
    finally:
        if child.poll() is None: child.kill(); child.wait(timeout=10)
        child.stdout.close(); child.stderr.close()


def test_linked_epoch_file_is_rejected_without_touching_target(tmp_path):
    root = workspace(tmp_path)
    target = tmp_path / 'outside.json'
    target.write_text('untouched')
    try: (root / '00_temp/workspace-epochs.json').symlink_to(target)
    except OSError: pytest.skip('symlinks unavailable')
    with pytest.raises(ValueError):
        with managed_mutation(root / '03_parsed_json/a.json'): pass
    assert target.read_text() == 'untouched'
