"""Real GPU probes using production inference, isolated output and read-only inputs."""
from __future__ import annotations

import argparse
import collections
from datetime import datetime
import json
import importlib.metadata
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CEILINGS = "5:340,20:272,50:224,100:128,150:96,200:80"


def safe_even_cap(cap):
    """Keep at least 10% headroom, then round down to an even batch size."""
    return ((int(cap) * 9 // 10) // 2) * 2


def parse_ceilings(raw):
    values = [tuple(map(int, pair.split(":"))) for pair in raw.split(",")]
    if not values or any(len(pair) != 2 or min(pair) < 1 for pair in values):
        raise ValueError("ceilings must be positive chars:rows pairs")
    if any(a[0] >= b[0] for a, b in zip(values, values[1:])):
        raise ValueError("character ceilings must be strictly increasing")
    return values


def find_limit(chars, ceiling, trial, seeds):
    """Binary search inside a declared ceiling, then confirm every seed.

    This is an observed boundary under the sampled inputs, not a proof of a
    monotonic capacity curve: sampling/voice/audio length can alter VRAM usage.
    """
    low, high = 0, ceiling + 1
    if trial(chars, ceiling, seeds[0]):
        low = ceiling
    else:
        high = ceiling
    while True:
        while high - low > 1:
            candidate = (low + high) // 2
            if trial(chars, candidate, seeds[0]):
                low = candidate
            else:
                high = candidate
        if low == 0 or all(trial(chars, low, seed) for seed in seeds[1:]):
            return low, high if high <= ceiling else None
        high, low = low, 0


def inspect_trial(content, returncode, cap):
    perf = []
    for line in content.splitlines():
        if line.startswith("[perf] "):
            perf.append(json.loads(line[7:]))
    starts = [p for p in perf if p.get("stage") == "batch" and p.get("event") == "start"]
    ids = re.findall(r"^\[segment\] (\d+) ok ", content, re.M)
    success = (returncode == 0 and len(ids) == cap and set(map(int, ids)) == set(range(cap))
               and len(starts) == 1 and starts[0].get("rows") == cap
               and "[watchdog]" not in content)
    return success, {"ok_segments": len(ids), "perf": perf,
                     "oom": "out of memory" in content.lower()}


def load_inputs(project, speaker, ceilings):
    vc = json.loads((project / "04_voice_profiles/voice_config.json").read_text(encoding="utf-8"))
    name, seen = speaker, set()
    while True:
        if name in seen:
            raise ValueError("speaker alias cycle")
        seen.add(name)
        voice = vc.get(name)
        if not isinstance(voice, dict):
            raise ValueError("speaker has no voice configuration")
        alias = voice.get("alias_of") or voice.get("alias")
        if not alias:
            break
        name = alias
    if voice.get("type") != "clone":
        raise ValueError("this benchmark requires a completed clone voice")
    voice = dict(voice)
    reference = (project / voice["ref_audio"]).resolve()
    if not reference.is_file():
        raise ValueError("reference audio is missing")
    voice["ref_audio"] = str(reference)
    texts = collections.defaultdict(list)
    for path in sorted((project / "03_parsed_json").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            continue  # Directory metadata, not a chapter.
        for row in data:
            txt = (row.get("text") or "").strip()
            if 0 < len(txt) <= ceilings[-1][0]:
                texts[len(txt)].append(txt)
    # Require exact tier endpoints; never silently claim a shorter sample tests a tier.
    for chars, _ in ceilings:
        if not texts[chars]:
            raise ValueError(f"no natural sample of exactly {chars} characters")
    return {speaker: voice}, texts


def load_environment(path):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = (s.strip() for s in line.split("=", 1))
        if re.fullmatch(r"NARRIFY_[A-Z0-9_]+|HF_HOME|HF_HUB_CACHE", key):
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def production_pending():
    # Read-only, fail closed when the application database cannot be checked.
    # Heavy model libraries remain exclusively in the probe subprocess.
    from sqlalchemy import func, select, text
    from backend.platform.database import SessionLocal
    from backend.platform.models import GPURequest
    with SessionLocal() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        return db.scalar(select(func.count()).select_from(GPURequest)) > 0


def stop_child(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=True,
                           stdout=subprocess.DEVNULL)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, subprocess.CalledProcessError):
        if process.poll() is None:
            raise  # Cancellation failure must not be treated as safe cleanup.
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


class Benchmark:
    def __init__(self, args, ceilings, voices, texts):
        self.args, self.ceilings, self.texts = args, ceilings, texts
        # Always a new directory. Cleanup can only target this run's own audio.
        args.output.mkdir(parents=True, exist_ok=True)
        self.base = args.output / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.base.mkdir()
        (self.base / "voice_config.json").write_text(json.dumps(voices, ensure_ascii=False, indent=2), encoding="utf-8")
        self.report = {"started": datetime.now().isoformat(), "status": "running",
                       "speaker": args.speaker, "resident_types": args.resident_types,
                       "seeds": args.seeds, "search_ceilings": ceilings,
                       "scope": "single clone speaker; natural exact-length texts; bounded search; independent child per trial",
                       "python": sys.version,
                       "packages": {name: importlib.metadata.version(name) for name in ("torch", "transformers", "qwen-tts")},
                       "tiers": [], "trials": []}

    def publish(self, message=None):
        tmp = self.base / "results.tmp"
        tmp.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.base / "results.json")
        if message:
            print(datetime.now().isoformat(timespec="seconds"), message, flush=True)

    def nvidia(self, query):
        return subprocess.run(["nvidia-smi", f"--id={self.args.gpu_index}", query,
                               "--format=csv,noheader,nounits"], check=True, capture_output=True, text=True).stdout

    def gpu_pids(self):
        return {int(s.strip()) for s in self.nvidia("--query-compute-apps=pid").splitlines() if s.strip().isdigit()}

    def trial(self, chars, cap, seed):
        while True:
            while production_pending() or self.gpu_pids():
                self.report["status"] = "paused_for_production"
                self.publish()
                time.sleep(5)
            self.report["status"] = "running"
            name = f"{len(self.report['trials'])+1:03d}_{chars}chars_{cap}rows_seed{seed}"
            folder = self.base / name
            folder.mkdir()
            pool = self.texts[chars]
            rows = [{"index": i, "speaker": self.args.speaker,
                     "text": pool[(i+seed) % len(pool)], "instruct": ""} for i in range(cap)]
            (folder / "segments.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            cmd = [sys.executable, str(Path(__file__).with_name("tts_batch_probe.py")),
                   "--probe-max", str(cap), "--resident-types", self.args.resident_types,
                   "--mode", "batch", "--segments-file", str(folder / "segments.json"),
                   "--voice-config", str(self.base / "voice_config.json"),
                   "--out-dir", str(folder / "audio"), "--workspace", str(self.args.project),
                   "--device", "cuda", "--language", "chinese", "--concurrency", str(cap),
                   "--seed", str(seed), "--width", "4"]
            info = {"id": name, "chars": chars, "rows": cap, "seed": seed, "status": "running"}
            self.report["trials"].append(info)
            self.publish(f"TEST {chars} chars x {cap} rows seed={seed}")
            start, peak, interrupted = time.monotonic(), 0, False
            with (folder / "worker.log").open("w", encoding="utf-8") as log:
                proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=os.name != "nt")
                try:
                    while proc.poll() is None:
                        if production_pending() or self.gpu_pids() - {proc.pid}:
                            interrupted = True
                            break
                        peak = max(peak, int(self.nvidia("--query-gpu=memory.used").strip()))
                        if time.monotonic() - start > self.args.timeout:
                            info["reason"] = "external_timeout"
                            break
                        time.sleep(2)
                except BaseException:
                    info.update(status="stopped", seconds=round(time.monotonic()-start, 2),
                                sampled_gpu_peak_mib=peak)
                    self.publish()
                    raise
                finally:
                    stop_child(proc)
            content = (folder / "worker.log").read_text(encoding="utf-8", errors="replace")
            success, details = inspect_trial(content, proc.returncode, cap)
            info.update(details, status="interrupted" if interrupted else ("passed" if success else "failed"),
                        exit_code=proc.returncode, seconds=round(time.monotonic()-start, 2),
                        sampled_gpu_peak_mib=peak)
            self.publish(f"{info['status'].upper()} {chars} chars x {cap} rows")
            if not self.args.keep_audio:
                shutil.rmtree(folder / "audio", ignore_errors=True)
            if interrupted:
                continue
            if not success and not info["oom"] and "[watchdog]" not in content and "reason" not in info:
                raise RuntimeError(f"non-capacity error: {folder / 'worker.log'}")
            return success

    def run(self):
        try:
            self.report["gpu"] = self.nvidia("--query-gpu=name,memory.total,driver_version").strip()
            self.publish(f"Results: {self.base}; production GPU requests take priority")
            for chars, cap in self.ceilings:
                row = {"chars": chars, "search_ceiling": cap, "status": "searching"}
                self.report["tiers"].append(row)
                self.publish()
                value, failed = find_limit(chars, cap, self.trial, self.args.seeds)
                row.update(status="complete", validated_cap=value, observed_failed_boundary=failed,
                           capped_at_search_ceiling=value == cap, safe_even_cap=safe_even_cap(value))
                self.publish(f"TIER {chars}: validated={value}, headroom_even={row['safe_even_cap']}")
            self.report.update(status="complete", finished=datetime.now().isoformat())
            self.publish("ALL TIERS COMPLETE")
        except BaseException as exc:
            self.report.update(status="stopped" if isinstance(exc, KeyboardInterrupt) else "error", error=str(exc))
            self.publish()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="project workspace; read-only")
    parser.add_argument("--speaker", required=True, help="one completed clone voice (aliases resolved)")
    parser.add_argument("--output", type=Path, default=ROOT / ".narrify/benchmarks/tts")
    parser.add_argument("--ceilings", default=DEFAULT_CEILINGS, help="bounded chars:rows search ceilings")
    parser.add_argument("--seeds", type=int, nargs="+", default=[41, 42, 43])
    parser.add_argument("--resident-types", default="custom,clone", help="custom,clone,design; must include clone")
    parser.add_argument("--hf-home", type=Path, help="existing model cache; otherwise inherit HF_HOME")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--gpu-index", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=3600, help="seconds per trial")
    parser.add_argument("--keep-audio", action="store_true")
    args = parser.parse_args()
    ceilings = parse_ceilings(args.ceilings)
    if len(set(args.seeds)) < 3 or len(set(args.seeds)) != len(args.seeds):
        parser.error("provide at least three distinct seeds")
    if args.timeout < 1 or args.gpu_index < 0:
        parser.error("timeout must be positive; GPU index nonnegative")
    types = set(args.resident_types.split(","))
    if "clone" not in types or not types <= {"custom", "clone", "design"}:
        parser.error("resident types must include clone and only custom/clone/design")
    args.project, args.output = args.project.resolve(), args.output.resolve()
    load_environment(args.env_file)
    if args.hf_home:
        os.environ["HF_HOME"] = str(args.hf_home.resolve())
        os.environ["HF_HUB_CACHE"] = str(args.hf_home.resolve() / "hub")
        os.environ["HUGGINGFACE_HUB_CACHE"] = os.environ["HF_HUB_CACHE"]
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu_index)
    sys.path.insert(0, str(ROOT))
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, interrupt)
    voices, texts = load_inputs(args.project, args.speaker, ceilings)
    Benchmark(args, ceilings, voices, texts).run()


if __name__ == "__main__":
    main()
