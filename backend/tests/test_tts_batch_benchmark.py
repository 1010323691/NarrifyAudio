"""Offline regression for real-GPU diagnostic safety and capacity accounting."""
import json
from types import SimpleNamespace

import pytest

from backend.core.tts_batch_limits import AUTO_BATCH_POINTS, AUTO_BATCH_MAX
from backend.engines.tts_batch import AUTO_MAX_CONCURRENCY
from scripts.testing import tts_batch_benchmark as bench
from scripts.testing.tts_batch_probe import load_worker


def test_production_limits_share_headroom_curve():
    raw = [296, 242, 192, 128, 90, 80]
    assert [bench.safe_even_cap(n) for n in raw] == [266, 216, 172, 114, 80, 72]
    assert tuple(cap for _, cap in AUTO_BATCH_POINTS) == tuple(bench.safe_even_cap(n) for n in raw)
    worker = load_worker()
    assert worker.AUTO_BATCH_POINTS == AUTO_BATCH_POINTS
    assert worker.AUTO_BATCH_MAX == AUTO_MAX_CONCURRENCY == AUTO_BATCH_MAX == 266
    assert bench.safe_even_cap(1) == 0  # No positive even cap fits below this measured limit.


def test_search_rechecks_seed_dependent_failure():
    calls = []
    def trial(chars, cap, seed):
        calls.append((chars, cap, seed))
        return cap <= (7 if seed == 43 else 8)
    assert bench.find_limit(5, 10, trial, [41, 42, 43]) == (7, 8)
    assert all((5, 7, seed) in calls for seed in [41, 42, 43])
    assert (5, 8, 43) in calls
    assert max(cap for _, cap, _ in calls) <= 10


def test_search_distinguishes_ceiling_from_failure():
    assert bench.find_limit(5, 2, lambda *a: True, [41, 42, 43]) == (2, None)
    assert bench.find_limit(5, 2, lambda *a: False, [41, 42, 43]) == (0, 1)


@pytest.mark.parametrize("content,returncode", [
    ('[perf] {"stage":"batch","event":"start","rows":2}\n[segment] 0 ok x\n[segment] 0 ok y\n', 0),
    ('[perf] {"stage":"batch","event":"start","rows":1}\n[segment] 0 ok x\n[segment] 1 ok y\n', 0),
    ('[perf] {"stage":"batch","event":"start","rows":2}\n[segment] 0 ok x\n[segment] 1 ok y\n[watchdog] timeout\n', 0),
    ('CUDA out of memory', 1),
])
def test_protocol_does_not_accept_split_recovery_or_duplicates(content, returncode):
    assert bench.inspect_trial(content, returncode, 2)[0] is False


def test_protocol_accepts_one_full_batch():
    content = '[perf] {"stage":"batch","event":"start","rows":2}\n[segment] 0 ok x\n[segment] 1 ok y\n'
    assert bench.inspect_trial(content, 0, 2)[0] is True


def test_inputs_read_only_exact_endpoint_and_alias_guard(tmp_path):
    profiles = tmp_path / '04_voice_profiles'
    scripts = tmp_path / '03_parsed_json'
    profiles.mkdir(); scripts.mkdir()
    reference = profiles / 'ref.wav'
    reference.write_bytes(b'test')
    config = profiles / 'voice_config.json'
    config.write_text(json.dumps({'A': {'alias_of': 'B'}, 'B': {'type': 'clone', 'ref_audio': '04_voice_profiles/ref.wav'}}))
    (scripts / 'chapter.json').write_text(json.dumps([{'text': 'hello'}]))
    original = config.read_bytes()
    voices, texts = bench.load_inputs(tmp_path, 'A', [(5, 2)])
    assert voices['A']['ref_audio'] == str(reference)
    assert texts[5] == ['hello']
    assert config.read_bytes() == original
    with pytest.raises(ValueError, match='exactly 20'):
        bench.load_inputs(tmp_path, 'A', [(20, 2)])
    config.write_text(json.dumps({'A': {'alias_of': 'B'}, 'B': {'alias_of': 'A'}}))
    with pytest.raises(ValueError, match='cycle'):
        bench.load_inputs(tmp_path, 'A', [(5, 2)])


def test_production_request_cancels_only_probe_and_preserves_source(tmp_path, monkeypatch):
    monkeypatch.setattr(bench.importlib.metadata, 'version', lambda name: 'test')
    args = SimpleNamespace(output=tmp_path / 'output', speaker='A', resident_types='custom,clone',
                           seeds=[41, 42, 43], project=tmp_path / 'project', timeout=10, keep_audio=False)
    source = tmp_path / 'project' / '05_audio_chunk'
    source.mkdir(parents=True)
    sentinel = source / 'keep.mp3'; sentinel.write_bytes(b'production')
    benchmark = bench.Benchmark(args, [(5, 2)], {'A': {}}, {5: ['hello']})
    monkeypatch.setattr(benchmark, 'gpu_pids', lambda: set())
    checks = iter([False, True])
    def pending():
        try: return next(checks)
        except StopIteration: raise KeyboardInterrupt
    monkeypatch.setattr(bench, 'production_pending', pending)
    process = SimpleNamespace(pid=987654, returncode=None, poll=lambda: None)
    monkeypatch.setenv('PYTHONUTF8', '0')
    monkeypatch.setenv('PYTHONIOENCODING', 'ascii')
    def spawn(*a, **kw):
        assert kw['env']['PYTHONUTF8'] == '1'
        assert kw['env']['PYTHONIOENCODING'] == 'utf-8'
        assert kw['stdout'].encoding == 'utf-8'
        return process
    monkeypatch.setattr(bench.subprocess, 'Popen', spawn)
    stopped = []
    def stop(child):
        stopped.append(child.pid); child.returncode = -15
    monkeypatch.setattr(bench, 'stop_child', stop)
    with pytest.raises(KeyboardInterrupt):
        benchmark.trial(5, 2, 41)
    assert stopped == [987654]
    assert benchmark.report['trials'][0]['status'] == 'interrupted'
    assert sentinel.read_bytes() == b'production'
