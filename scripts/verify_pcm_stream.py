"""Isolated long-audio and RF64 measurement, using real streaming code."""
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
import psutil
from backend.core.pcm_stream import WaveInput, write_wave

mode = sys.argv[1]
process = psutil.Process()
baseline = process.memory_info().rss
peak = [baseline]
done = threading.Event()
def sample():
    while not done.wait(.01):
        peak[0] = max(peak[0], process.memory_info().rss)
thread = threading.Thread(target=sample)
thread.start()
try:
    with tempfile.TemporaryDirectory(prefix='narrify-pcm-probe-') as root:
        root = Path(root)
        start = time.monotonic()
        if mode == 'rf64':
            pcm = root / 'large.pcm'
            size = 4 * 1024 ** 3 + 1024
            with pcm.open('wb') as output:
                output.truncate(size)
            result = root / 'large.wav'
            duration = write_wave(pcm, result, (1, 24000, 2))
            source = WaveInput(result)
            assert source.size == size
            assert source.format == (1, 24000, 2)
            with result.open('rb') as reader:
                assert reader.read(4) == b'RF64'
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(result)]))
            assert int(probe['streams'][0]['duration_ts']) == size // 2
            print(json.dumps({'mode': mode, 'PCM_bytes': size, 'wave_bytes': result.stat().st_size,
                             'duration_ms': duration, 'ffprobe_frames': size // 2,
                             'extra_peak_RSS_bytes': peak[0] - baseline,
                             'execution_seconds': time.monotonic() - start}), flush=True)
        else:
            spec = importlib.util.spec_from_file_location('tw_probe', REPO_ROOT / 'tts-engine' / 'tts_worker.py')
            tw = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tw)
            tw.progress = lambda *_: None
            tw.log = lambda *_: None
            rows = []
            frames_per_segment = 24000 * 20
            for index in range(100):
                path = root / f'{index}.wav'
                # Each segment has a distinct signed PCM sample; gaps are zero.
                with wave.open(str(path), 'wb') as output:
                    output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                    for _ in range(20):
                        output.writeframesraw(struct.pack('<h', index + 1) * 24000)
                rows.append({'path': str(path), 'speaker': 'A' if index % 2 else 'B', 'pause_after': 123})
            parts = tw._merge_stage1(rows, str(root), 500, 250, 7, len(rows), str(root))
            result, duration = tw._merge_stage2(parts, str(root), 'book.mp3', 500, 250, len(parts))
            # Independently reproduce the legacy silence resampler's frame count:
            # 123 ms -> 1356 frames at 11025 -> 2949 frames at 24000.
            import audioop
            gap, _ = audioop.ratecv(b'\0\0' * int(11025 * .123), 2, 1, 11025, 24000, None)
            gap_frames = len(gap) // 2
            with wave.open(str(result), 'rb') as reader:
                assert reader.getnframes() == 100 * frames_per_segment + 99 * gap_frames
                for index in range(100):
                    for _ in range(20):
                        assert reader.readframes(24000) == struct.pack('<h', index + 1) * 24000
                    if index < 99:
                        assert reader.readframes(gap_frames) == gap
                assert not reader.readframes(1)
            assert duration >= 30 * 60
            print(json.dumps({'mode': mode, 'segments': len(rows), 'duration_seconds': duration,
                             'all_samples_and_boundaries_verified': True,
                             'extra_peak_RSS_bytes': peak[0] - baseline,
                             'execution_seconds': time.monotonic() - start}), flush=True)
        assert peak[0] - baseline <= 128 * 1024 ** 2
finally:
    done.set()
    thread.join()
