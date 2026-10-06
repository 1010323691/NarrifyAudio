"""Real subprocess protocol, valid audio files, without loading a TTS model."""
import argparse
import json
import hashlib
import re
import uuid
import os
import shutil
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--mode', required=True)
parser.add_argument('--segments-file', type=Path, required=True)
parser.add_argument('--out-dir', type=Path)
parser.add_argument('--out', type=Path)
args, _ = parser.parse_known_args()
rows = json.loads(args.segments_file.read_text())
location=str(args.out_dir or args.out)
match=re.search(r'mix(\d+)_(\d+)_',location)
stage, user = (int(match[1]), int(match[2])) if match else (-1, -1)
variant='preview' if 'chapter_preview' in location else 'batch'
case=f'{user}:{args.mode}:{variant}'
minimum=float(os.environ.get('NARRIFY_TEST_TTS_MIN_DELAY','.2'))
maximum=float(os.environ.get('NARRIFY_TEST_TTS_MAX_DELAY',str(minimum)))
# Deterministic spread, including both endpoints of the requested range.
fraction=(int(hashlib.sha256(case.encode()).hexdigest(),16)%11)/10
planned=float(os.environ.get('NARRIFY_TEST_MERGE_DELAY','.2')) if args.mode=='merge' else minimum+(maximum-minimum)*fraction
started=time.monotonic()
time.sleep(planned)
report_dir=Path(os.environ['NARRIFY_TEST_TTS_REPORT_DIR'])
report_dir.mkdir(parents=True,exist_ok=True)
(report_dir/(uuid.uuid4().hex+'.json')).write_text(json.dumps({
    'stage':stage,'user':user,'mode':args.mode,'variant':variant,
    'planned_seconds':planned,'actual_wait_seconds':time.monotonic()-started,
}))
if args.mode == 'merge':
    args.out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(os.environ['NARRIFY_TEST_MP3'], args.out)
    print('[result] '+str(args.out), flush=True)
else:
    for row in rows:
        index = row['index']
        if args.mode == 'design-batch':
            target = Path(row['out'])
            template = os.environ['NARRIFY_TEST_WAV']
            line = f'[design] {index} ok 123 {target}'
        else:
            target = args.out_dir / f'{index+1:04d}.mp3'
            template = os.environ['NARRIFY_TEST_MP3']
            line = f'[segment] {index} ok {target}'
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, target)
        print(line, flush=True)
    print('[done]', flush=True)
