"""Isolated TTS throughput sweep. Run with the project's .venv Python.

Each candidate uses the same corpus and a fresh CUDA process. Outputs never touch
book audio. JSON reports include raw batches, stage events and device samples.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SIZES = [32, 36, 40, 44, 48, 52, 56, 60, 64]


def recommend(runs, repeats, tolerance=0.05):
    """All repetitions must succeed; prefer more rows within 5% of best throughput."""
    groups = {}
    for run in runs:
        groups.setdefault(run['size'], []).append(run)
    stable = {size: statistics.median(r['chars_per_second'] for r in rows)
              for size, rows in groups.items()
              if len(rows) == repeats and all(r['status'] == 'ok' for r in rows)}
    if not stable:
        return None
    best = max(stable.values())
    eligible = [size for size, rate in stable.items() if rate >= best * (1 - tolerance)]
    # A larger configured ceiling is useful only if the constraints actually admit more rows.
    return max(eligible, key=lambda size: (
        max((b['rows'] for r in groups[size] for b in r.get('batches', [])), default=size), -size))


def memory_pressure_stalled(pressure_since, last_progress, now):
    """Reject sustained near-full VRAM with no completed stage, before paging drags on."""
    return (pressure_since is not None and now-pressure_since >= 10
            and now-last_progress >= 20)


def memory_pressure_full(total_mib, used_mib, floor_mib=1024):
    """A full-GPU sample is an immediate failed probe; do not wait for paging."""
    return total_mib is not None and used_mib is not None and total_mib - used_mib < floor_mib


_PROBE_SENTENCES = (
    "山下的雾气还没有散，村口的老槐树上落满了灰喜鹊。",
    "他把茶碗放下，目光越过院墙望向北面的山头。",
    "风从街口穿过来，带着一股淡淡的柴火气味。",
    "老人咳嗽了两声，慢慢把门帘掀开了一道缝。",
    "集市上的叫卖声一阵高过一阵，人群挤得水泄不通。",
    "她低声说，别出声，先听他们说完再说。",
    "雨点打在瓦片上，噼里啪啦地响个不停。",
    "马在槽边低头吃着草，尾巴不耐烦地甩来甩去。",
    "他把信读了一遍又一遍，手指微微有些发抖。",
    "远处的钟声响了七下，天色已经暗了下来。",
    "伙计端上一碗热汤，热气在冷风里散得很快。",
    "他抬起头，看见门口站着个陌生的年轻人。",
    "那条巷子又窄又长，石板路被脚步磨得发亮。",
    "她说这话的时候，手一直攥着袖口的流苏。",
    "炉火映着四壁，影子随着火苗轻轻摇晃。",
    "掌柜的拨着算盘，头也不抬地报了个数。",
    "夜色像水一样漫上来了，星星一颗一颗地亮。",
    "他深吸一口气，把要说的话又咽了回去。",
)
_PROBE_PARAGRAPH = "".join(_PROBE_SENTENCES) * 20


def _probe_text(length, offset):
    length = max(0, int(length))
    if length <= 0:
        return ""
    paragraph = _PROBE_PARAGRAPH * 2
    start = int(offset) % len(_PROBE_PARAGRAPH)
    return paragraph[start:start + length]


def _pick_clone_speaker(voice_config):
    for name, entry in voice_config.items():
        if (isinstance(entry, dict) and entry.get("type") == "clone"
                and entry.get("ref_audio") and (entry.get("ref_text") or "").strip()):
            return name
    raise RuntimeError("No usable cloned voice is configured for this benchmark.")


def child(args):
    from backend.core.config import get_config
    from backend.core.paths import get_layout
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('worker', ROOT / 'tts-engine/tts_worker.py')
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    cfg, layout = get_config(), get_layout()
    voices = json.loads((layout.voice_profiles / 'voice_config.json').read_text('utf-8'))
    speaker = _pick_clone_speaker(voices)
    worker._add_ffmpeg_to_path(cfg.ffmpeg.ffmpeg_path)
    model = worker.load_model(cfg.tts.base_model, 'cuda')
    start = time.perf_counter()
    prompt = worker._build_clone_prompt(model, voices[speaker], str(layout.workspace), speaker)
    print('[perf] ' + json.dumps({'stage': 'reference_read_encode', 'event': 'end',
                                 'seconds': time.perf_counter() - start}), flush=True)
    options = SimpleNamespace(language='chinese', speaker='', profile_stages=True, benchmark_strict=True, vocoder_batch_size=args.vocoder_batch_size)
    preparation_started = time.perf_counter()
    lengths = [int(x) for x in args.lengths.split(',')]
    rows = [{'index': i, 'speaker': speaker, 'vd': voices[speaker], 'instruct': '',
             'text': _probe_text(lengths[i % len(lengths)], i * 17),
             'chars': lengths[i % len(lengths)]} for i in range(args.samples)]
    rows.sort(key=lambda row: row['chars'])
    print('[perf] ' + json.dumps({'stage': 'text_preparation', 'event': 'end',
                                 'seconds': time.perf_counter()-preparation_started}), flush=True)
    # Warm up on one full candidate batch; excluded from measured throughput.
    warm_positions = worker.plan_sub_batches([r['chars'] for r in rows],
        max_batch=args.size, max_batch_chars=args.max_chars, length_ratio=args.ratio, first_only=True)[0]
    warm = [rows[i] for i in warm_positions]
    print('[perf] ' + json.dumps({'stage': 'warmup', 'event': 'start', 'rows': len(warm)}), flush=True)
    with worker.bounded_vocoder(model, args.vocoder_batch_size), worker.profile_stages(model, True):
        worker.run_with_watchdog(lambda: worker._generate_rows(model, 'clone', warm, options,
                                 {speaker: prompt}, 42), args.stall_seconds, 'warmup', [])
    print('[perf] ' + json.dumps({'stage': 'warmup', 'event': 'end'}), flush=True)
    print('[benchmark_ready]', flush=True)
    counter = [0]
    completed = set()
    def report(index, ok, detail):
        if not ok:
            raise RuntimeError(f'Output {index} failed: {detail}')
        completed.add(index)
    output_context = (contextlib.nullcontext(args.audio_dir) if args.audio_dir else
                      tempfile.TemporaryDirectory(prefix='tts-bench-audio-'))
    with output_context as out:
        for positions in worker.plan_sub_batches([r['chars'] for r in rows],
                max_batch=args.size, max_batch_chars=args.max_chars, length_ratio=args.ratio):
            batch = [rows[i] for i in positions]
            worker._synth_sub_batch(model, 'clone', batch, args=options,
                clone_prompts={speaker: prompt}, device='cuda', seed=42,
                sub_counter=counter, out_dir=out, width=4, report=report)
            worker._clear_gpu_cache('cuda')
    if completed != {row['index'] for row in rows}:
        raise RuntimeError('Benchmark output count does not match the input corpus')


def kill_tree(proc):
    if proc.poll() is None:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True)
        else:
            proc.kill()
        proc.wait(timeout=30)


def trial(args, size, repeat, folder):
    audio_dir = tempfile.TemporaryDirectory(prefix='tts-bench-audio-')
    # Parent owns this unique temp directory so hard worker termination cannot leak audio.
    assert Path(audio_dir.name).resolve().parent == Path(tempfile.gettempdir()).resolve()
    cmd = [sys.executable, str(Path(__file__).resolve()), '--child', '--size', str(size),
           '--samples', str(args.samples), '--lengths', args.lengths,
           '--max-chars', str(args.max_chars), '--ratio', str(args.ratio),
           '--stall-seconds', str(args.stall_seconds), '--vocoder-batch-size', str(args.vocoder_batch_size), '--audio-dir', audio_dir.name]
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1', HF_HUB_OFFLINE='1')
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace', env=env, cwd=ROOT)
    except BaseException:
        audio_dir.cleanup()
        raise
    lines = queue.Queue()
    def read():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    started = last_progress = time.monotonic()
    ready = None
    status = 'ok'
    events, gpu = [], []
    last_poll = 0
    pressure_since = None
    oom = False
    ram_peak = None
    try:
        import psutil
        monitored = psutil.Process(proc.pid)
    except ImportError:
        monitored = None
    try:
        with (folder / f'{size}-{repeat}.log').open('w', encoding='utf-8') as log:
            while True:
                now = time.monotonic()
                if now - started > args.run_seconds or now - last_progress > args.stall_seconds:
                    status = 'timeout' if ready is None else 'no_progress'
                    break
                if now - last_poll >= 1:
                    last_poll = now
                    if monitored is not None:
                        try:
                            processes = [monitored] + monitored.children(recursive=True)
                            ram_peak = max(ram_peak or 0, sum(p.memory_info().rss for p in processes))
                        except psutil.Error:
                            pass
                    try:
                        sample = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total',
                            '--format=csv,noheader,nounits', '--id=0'], capture_output=True, text=True, timeout=3)
                        values = [float(v.strip()) for v in sample.stdout.strip().split(',')]
                        if len(values) == 3:
                            gpu.append({'seconds': now - started, 'util': values[0], 'used_mib': values[1],
                                        'total_mib': values[2], 'measured': ready is not None})
                            if memory_pressure_full(values[2], values[1]):
                                status = 'memory_pressure'
                                break  # full VRAM is already a failed candidate
                            pressure_since = None
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        pass
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                log.write(line)
                log.flush()
                oom |= 'out of memory' in line.lower()
                if line.startswith('[benchmark_ready]'):
                    ready = last_progress = time.monotonic()
                if line.startswith('[perf] '):
                    event = json.loads(line[7:])
                    event['seconds_since_start'] = time.monotonic() - started
                    events.append(event)
                    # Heartbeats are NOT progress. Only completed stages reset the clock.
                    if event.get('event') == 'end':
                        last_progress = time.monotonic()
    finally:
        kill_tree(proc)
        reader.join(timeout=5)
        proc.stdout.close()
        audio_dir.cleanup()
    elapsed_synthesis = time.monotonic() - ready if ready is not None else 0
    batches = [e for e in events if e.get('stage') == 'batch' and e.get('event') == 'end']
    batch_seconds = sum(b['seconds'] for b in batches)
    seconds = elapsed_synthesis
    count = sum(b['rows'] for b in batches)
    if proc.returncode == 124 and status == 'ok':
        status = 'watchdog_timeout'
    if status == 'ok' and (proc.returncode != 0 or count != args.samples or oom):
        status = 'oom' if oom else 'error'
    measured = [g for g in gpu if g['measured']]
    memory_pressure = any(g['total_mib'] - g['used_mib'] < 1024 for g in gpu)
    if status == 'ok' and memory_pressure:
        status = 'memory_pressure'
    return dict(size=size, repeat=repeat, status=status, oom=oom, exit_code=proc.returncode,
                memory_pressure=memory_pressure, total_seconds=time.monotonic()-started, synthesis_seconds=seconds, batch_seconds=batch_seconds,
                non_batch_seconds=max(0, seconds-batch_seconds),
                samples_per_second=count/seconds if seconds else 0,
                chars_per_second=sum(b['chars'] for b in batches)/seconds if seconds else 0,
                audio_seconds_per_second=sum(b['audio_seconds'] for b in batches)/seconds if seconds else 0,
                gpu_util_mean=statistics.mean(g['util'] for g in measured) if measured else None,
                vram_peak_mib=max((g['used_mib'] for g in gpu), default=None),
                ram_peak_bytes=ram_peak, last_stage=events[-1] if events else None,
                batches=batches, stages=events, gpu_samples=gpu)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--sizes', default=','.join(map(str, SIZES)))
    parser.add_argument('--samples', type=int, default=256)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--full-sweep', action='store_true', help='continue through performance plateaus; still stop on instability')
    parser.add_argument('--throughput-tolerance', type=float, default=0.05)
    parser.add_argument('--lengths', default='25,50,75,100,125,150,175,200')
    parser.add_argument('--max-chars', type=int, default=6400)
    parser.add_argument('--vocoder-batch-size', type=int, default=0)
    parser.add_argument('--ratio', type=float, default=1.5)
    parser.add_argument('--stall-seconds', type=float, default=240)
    parser.add_argument('--run-seconds', type=float, default=3600)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--audio-dir', default='')
    parser.add_argument('--size', type=int, default=32)
    args = parser.parse_args()
    sizes = [int(s) for s in args.sizes.split(',')]
    if (min(sizes) < 1 or max(sizes) > 512 or args.samples < (args.size if args.child else max(sizes))
            or args.repeats < 1 or not 0 <= args.throughput_tolerance < 1 or args.ratio < 1 or args.max_chars < 200
            or any(not 1 <= int(n) <= 200 for n in args.lengths.split(','))):
        parser.error('Use benchmark sizes 1..512, samples >= max size, repeats >= 1, ratio >= 1, lengths 1..200')
    if args.child:
        child(args)
        return
    if args.output is None:
        parser.error('--output is required (new diagnostic directory)')
    args.output.mkdir(parents=True, exist_ok=False)
    versions = {}
    for package in ('torch', 'transformers', 'qwen-tts'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    try:
        gpu_identity = subprocess.run(['nvidia-smi', '--query-gpu=name,driver_version',
            '--format=csv,noheader', '--id=0'], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        gpu_identity = None
    runs = []
    for size in sizes:
        for repeat in range(args.repeats):
            result = trial(args, size, repeat, args.output)
            runs.append(result)
            report = {'versions': versions, 'gpu': gpu_identity, 'parameters': {k: str(v) if isinstance(v, Path) else v for k,v in vars(args).items()},
                      'runs': runs, 'recommended_size': recommend(runs, args.repeats, args.throughput_tolerance) if args.repeats >= 3 else None,
                      'provisional_size': recommend(runs, args.repeats, args.throughput_tolerance)}
            (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k:v for k,v in result.items() if k not in ('batches','stages','gpu_samples','last_stage')}), flush=True)
            if result['status'] != 'ok':
                return  # stop escalating on first instability
        # Flat throughput is acceptable: continue looking for a larger stable batch.
        # Stop only after two materially slower gears, or immediately on instability.
        if not args.full_sweep and len(runs) >= 3*args.repeats:
            rates = [statistics.median(r['chars_per_second'] for r in runs if r['size']==s)
                     for s in sizes[:sizes.index(size)+1]]
            floor = max(rates) * (1 - args.throughput_tolerance)
            if rates[-1] < floor and rates[-2] < floor:
                break


if __name__ == '__main__':
    main()
