"""Find the largest stable TTS batch near the best end-to-end throughput.

Start at 128 x 200 characters, move up/down in steps of 32, then refine the
pass/fail boundary to 8 rows. Confirm selected candidates with three fresh runs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from benchmark import recommend, trial


def next_size(runs, start=128, step=32, resolution=8, ceiling=512):
    if not runs:
        return start
    passed = [r['size'] for r in runs if r['status'] == 'ok']
    failed = [r['size'] for r in runs if r['status'] != 'ok']
    if not passed:
        lowest = min(failed)
        return max(resolution, lowest-step) if lowest > resolution else None
    low = max(passed)
    upper = [size for size in failed if size > low]
    if not upper:
        return min(ceiling, low+step) if low < ceiling else None
    high = min(upper)
    if high-low <= resolution:
        return None
    return max(low+resolution, ((low+high)//2//resolution)*resolution)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--initial-report', type=Path)
    parser.add_argument('--start', type=int, default=128)
    parser.add_argument('--chars', type=int, default=200)
    parser.add_argument('--step', type=int, default=32)
    parser.add_argument('--resolution', type=int, default=8)
    parser.add_argument('--ceiling', type=int, default=512)
    parser.add_argument('--vocoder-batch-size', type=int, default=8)
    parser.add_argument('--stall-seconds', type=float, default=240)
    parser.add_argument('--throughput-tolerance', type=float, default=0.05)
    args = parser.parse_args()
    if not (1 <= args.chars <= 200 and 1 <= args.start <= args.ceiling <= 512
            and 1 <= args.resolution <= args.step and 0 <= args.throughput_tolerance < 1):
        parser.error('Invalid search bounds')
    args.output.mkdir(parents=True, exist_ok=False)
    screened, validated = [], []
    if args.initial_report:
        initial = json.loads(args.initial_report.read_text('utf-8'))
        params = initial['parameters']
        if (params['lengths'] != str(args.chars) or params['vocoder_batch_size'] != args.vocoder_batch_size
                or not initial['runs'] or initial['runs'][0]['size'] != args.start
                or len({r['size'] for r in initial['runs']}) != len(initial['runs'])):
            parser.error('Initial report does not match this search')
        screened.extend(initial['runs'])
    def save():
        report = {'parameters': {k: str(v) if isinstance(v, Path) else v for k,v in vars(args).items()},
                  'screened': screened, 'validated': validated,
                  'recommended_size': recommend(validated, 3, args.throughput_tolerance),
                  'provisional_size': recommend(screened, 1, args.throughput_tolerance)}
        (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def probe(size, phase, repeat):
        folder = args.output/f'{phase}-{size}-{repeat}'
        folder.mkdir()
        options = SimpleNamespace(samples=size*(2 if phase == 'validate' else 1),
            lengths=str(args.chars), max_chars=size*args.chars, ratio=1.5,
            vocoder_batch_size=args.vocoder_batch_size, stall_seconds=args.stall_seconds,
            run_seconds=1800)
        result = trial(options, size, repeat, folder)
        print(json.dumps({'phase': phase, **{k:v for k,v in result.items()
                         if k not in ('batches','stages','gpu_samples','last_stage')}}), flush=True)
        return result
    save()
    while (size := next_size(screened, args.start, args.step, args.resolution, args.ceiling)) is not None:
        result = probe(size, 'screen', 0)
        screened.append(result)
        save()
        if result['status'] == 'error':
            return  # configuration/encoding errors are not a batch-size boundary
    candidates = list(screened)
    while (size := recommend(candidates, 1, args.throughput_tolerance)) is not None:
        successful = True
        for repeat in range(3):
            result = probe(size, 'validate', repeat)
            validated.append(result)
            save()
            if result['status'] != 'ok':
                successful = False
                break
        if successful:
            return
        # A failed confirmation disqualifies this gear and all larger gears.
        candidates = [r for r in candidates if r['size'] < size]


if __name__ == '__main__':
    main()
