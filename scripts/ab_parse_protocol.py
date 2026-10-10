"""A/B harness: legacy JSON parse protocol vs the numbered-unit protocol, on real chapters.

Runs ``parse_script_file`` (the real pipeline, every check stage included) once per protocol
for each sampled chapter against the LLM configured in the platform settings, then reports:

* speaker agreement between the two outputs, aligned per source character;
* each protocol's attribution-audit "random bucket" disagreement rate (the repo's existing
  whole-book error gauge; ``--spot-rate`` is raised so the bucket is large enough to compare);
* wall time and completion tokens of the parse stage, edit / fallback counters;
* PASS/FAIL against the acceptance gates in design/文本解析提速计划.md.

Nothing is written to a workspace or the database: outputs go to ``--out``. Quota accounting
is stubbed out. Usage (repo root)::

    set -a; . ./.env; set +a     # NARRIFY_DATABASE_URL: the LLM endpoint is read from platform settings
    .venv/bin/python scripts/ab_parse_protocol.py --src-dir "workspace/amater/SAO/02_split_text" \
        --files 30 --concurrency 8 --out design/ab-parse
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.concurrency import set_concurrency  # noqa: E402
from backend.core.config import GenerationConfig, LLMConfig, PromptsConfig  # noqa: E402
from backend.core.task_control import TaskCancelled  # noqa: E402
from backend.engines import script as script_engine  # noqa: E402

GATES = {
    "random_rate_margin": 0.01,      # units may exceed json's random-bucket rate by 1pp (sample noise)
    "fallback_chunks_max": 0.03,     # fraction of chunks that fell back to the JSON protocol
    "edit_reject_max": 0.30,         # rejected / (applied + rejected)
    "edit_min_sample": 20,           # fewer edits than this: the rate is noise, gate not evaluated
    "parse_time_ratio_max": 1 / 3,   # parse-stage wall time, units / json
}


class Handle:
    """Minimal task handle: collects logs, never cancels."""

    cancelled = False

    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.parse_started = self.parse_ended = None

    def log(self, msg, level=None):
        self.logs.append((level or "INFO", str(msg)))

    def check(self):
        return None

    def progress(self, *a, **k):
        return None

    def llm_chunk(self, *a, **k):
        return None

    def phase(self, name):
        now = time.monotonic()
        if name == "parse":
            self.parse_started = now
        elif name == "check" and self.parse_started is not None:
            self.parse_ended = now

    def completion_tokens(self) -> int:
        total = 0
        for _lv, msg in self.logs:
            m = re.search(r"completion=(\d+)", msg)
            if m and msg.startswith("chunk "):
                total += int(m.group(1))
        return total

    def parse_seconds(self) -> float:
        if self.parse_started is None or self.parse_ended is None:
            return 0.0
        return self.parse_ended - self.parse_started


def load_llm(args) -> LLMConfig:
    if args.base_url:
        return LLMConfig(base_url=args.base_url, api_key=os.environ.get(args.api_key_env, "local"),
                         model_name=args.model, stream=False)
    import sqlalchemy as sa
    engine = sa.create_engine(os.environ["NARRIFY_DATABASE_URL"])
    with engine.connect() as conn:
        value = conn.execute(sa.text(
            "select value from system_config where key='application.features'")).scalar()
    cfg = dict((value or {}).get("llm") or {})
    cfg["stream"] = False
    if args.model:
        cfg["model_name"] = args.model
    return LLMConfig(**cfg)


def skeleton_speakers(entries: list) -> tuple[str, list[str]]:
    """The word-character skeleton of an output and one speaker per skeleton character."""
    chars: list[str] = []
    speakers: list[str] = []
    for e in entries:
        sp = e.get("speaker") or "NARRATOR"
        for ch in e.get("text", ""):
            if ch.isalnum():
                chars.append(ch)
                speakers.append(sp)
    return "".join(chars), speakers


def agreement(a_entries: list, b_entries: list) -> dict:
    """Speaker agreement over the characters both outputs kept, aligned by text (a
    dropped or altered stretch in one output must not shift everything after it)."""
    import difflib
    ta, sa = skeleton_speakers(a_entries)
    tb, sb = skeleton_speakers(b_entries)
    matcher = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    n = same = a_char_b_narr = a_narr_b_char = both_char_differ = 0
    for i, j, size in matcher.get_matching_blocks():
        for k in range(size):
            n += 1
            x, y = sa[i + k], sb[j + k]
            if x == y:
                same += 1
            elif x == "NARRATOR":
                a_narr_b_char += 1
            elif y == "NARRATOR":
                a_char_b_narr += 1
            else:
                both_char_differ += 1
    return {"aligned": n, "a_chars": len(ta), "b_chars": len(tb), "same": same,
            "a_char_b_narr": a_char_b_narr, "a_narr_b_char": a_narr_b_char,
            "both_char_differ": both_char_differ}


def run_one(protocol: str, path: Path, llm, gen_kwargs: dict, out_dir: Path) -> dict:
    handle = Handle()
    generation = GenerationConfig(parse_protocol=protocol, **gen_kwargs)
    started = time.monotonic()
    result = script_engine.parse_script_file(
        handle, str(path), llm, PromptsConfig(), generation, rng=random.Random(7),
        output_path=out_dir / protocol / f"{path.stem}.json",
        spot_history_path=out_dir / "spot_history.json",
    )
    return {
        "result": {k: v for k, v in result.items() if k != "entries"},
        "entries": result["entries"],
        "seconds": time.monotonic() - started,
        "parse_seconds": handle.parse_seconds(),
        "completion_tokens": handle.completion_tokens(),
        "chunks": sum(1 for _lv, m in handle.logs if re.match(r"处理第 \d+/\d+ 段", m)),
        "edit_log": [m for _lv, m in handle.logs if "edit 被拒" in m],
        "rejudge_calls": sum(1 for _lv, m in handle.logs
                             if re.match(r"(角色匹配检查|归属抽样)第 \d+/\d+ 组", m)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src-dir", required=True, help="a 02_split_text directory of chapter .txt files")
    ap.add_argument("--files", type=int, default=30)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--min-chars", type=int, default=0,
                    help="only sample chapters of at least this many characters (utf-8 size / 3)")
    ap.add_argument("--max-chars", type=int, default=0, help="... and at most this many (0 = no limit)")
    ap.add_argument("--concurrency", type=int, default=8, help="LLM gate size (parallel chapters)")
    ap.add_argument("--spot-rate", type=float, default=0.30,
                    help="attribution-audit rate (raised so the random bucket is statistically usable)")
    ap.add_argument("--out", default="design/ab-parse")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--api-key-env", default="AB_LLM_API_KEY")
    ap.add_argument("--model", default="")
    ap.add_argument("--no-checks", action="store_true",
                    help="disable every check stage (measure the parse stage alone; gates will not all evaluate)")
    ap.add_argument("--baseline", default="",
                    help="a previous run's --out directory: reuse its json-protocol outputs and "
                         "aggregates instead of re-running them (use with --only-protocol units)")
    ap.add_argument("--chunk-size", type=int, default=0, help="generation.chunk_size override (0 = default)")
    ap.add_argument("--gen", action="append", default=[], metavar="KEY=VALUE",
                    help="extra GenerationConfig override (repeatable), e.g. --gen check_pack_targets=0")
    ap.add_argument("--only-protocol", choices=("json", "units"), default="")
    args = ap.parse_args()

    # No database / quota in this harness.
    import backend.platform.quota as quota
    quota.consume_llm_output = lambda *a, **k: None

    files = sorted(Path(args.src_dir).glob("*.txt"))
    random.Random(args.seed).shuffle(files)
    def approx_chars(f: Path) -> float:
        return f.stat().st_size / 3

    files = [f for f in files if f.stat().st_size > 2000
             and approx_chars(f) >= args.min_chars
             and (not args.max_chars or approx_chars(f) <= args.max_chars)][: args.files]
    if not files:
        print("no chapter files found", file=sys.stderr)
        return 2
    llm = load_llm(args)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    set_concurrency(max(1, args.concurrency))
    gen_kwargs = dict(spot_check_rate=args.spot_rate, spot_check_adaptive=False)
    for item in args.gen:
        key, _, raw = item.partition("=")
        gen_kwargs[key] = json.loads(raw) if raw[:1] in "0123456789-[{\"tf" else raw
    if args.chunk_size:
        gen_kwargs["chunk_size"] = args.chunk_size
    if args.no_checks:
        gen_kwargs.update(spot_check_enabled=False, check_boundary_speakers=False,
                          revalidate_splits=False, validate_instructs=False,
                          check_long_paragraphs=False)
    protocols = [args.only_protocol] if args.only_protocol else ["json", "units"]

    rows: dict[str, dict[str, dict]] = {p: {} for p in protocols}
    lock = threading.Lock()

    def job(protocol: str, path: Path) -> None:
        try:
            row = run_one(protocol, path, llm, gen_kwargs, out_dir)
        except TaskCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — one failed chapter must not sink the run
            row = {"error": f"{type(exc).__name__}: {exc}"}
        with lock:
            rows[protocol][path.name] = row
            print(f"[{protocol}] {path.name}: " + (row.get("error") or f"{row['seconds']:.0f}s"), flush=True)

    for protocol in protocols:  # run protocols one after the other so timings are comparable
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            list(pool.map(lambda f, p=protocol: job(p, f), files))

    report = summarize(rows, files)
    if args.baseline and "units" in rows:
        merge_baseline(report, rows, Path(args.baseline))
    for protocol, by_file in rows.items():
        report["protocols"][protocol]["edit_rejections"] = [
            m for r in by_file.values() if "error" not in r for m in r["edit_log"]]
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(render(report))
    return 0 if report.get("gates_passed", True) else 1


def summarize(rows: dict, files: list[Path]) -> dict:
    report: dict = {"files": len(files), "protocols": {}}
    for protocol, by_file in rows.items():
        ok = [r for r in by_file.values() if "error" not in r]
        random_n = sum(r["result"].get("spot_random_n", 0) for r in ok)
        random_err = sum(r["result"].get("spot_random_errors", 0) for r in ok)
        chunks = sum(r["chunks"] for r in ok)
        applied = sum(r["result"].get("unit_edit_applied", 0) for r in ok)
        rejected = sum(r["result"].get("unit_edit_rejected", 0) for r in ok)
        report["protocols"][protocol] = {
            "chapters_ok": len(ok),
            "chapters_failed": [n for n, r in by_file.items() if "error" in r],
            "chars": sum(r["result"].get("input_chars", 0) for r in ok),
            "entries": sum(r["result"].get("count", 0) for r in ok),
            "parse_seconds": round(sum(r["parse_seconds"] for r in ok), 1),
            "total_seconds": round(sum(r["seconds"] for r in ok), 1),
            "completion_tokens": sum(r["completion_tokens"] for r in ok),
            "random_n": random_n,
            "random_errors": random_err,
            "random_rate": (random_err / random_n) if random_n else None,
            "chunks": chunks,
            "fallback_chunks": sum(r["result"].get("unit_fallback_chunks", 0) for r in ok),
            "edit_applied": applied,
            "edit_rejected": rejected,
            "unit_labels": sum(r["result"].get("unit_labels", 0) for r in ok),
            "rejudge_calls": sum(r["rejudge_calls"] for r in ok),
            "boundary_checked": sum(r["result"].get("boundary_checked", 0) for r in ok),
            "boundary_fixed": sum(r["result"].get("boundary_fixed", 0) for r in ok),
            "spot_checked": sum(r["result"].get("spot_checked", 0) for r in ok),
            "spot_fixed": sum(r["result"].get("spot_fixed", 0) for r in ok),
        }
    if len(rows) == 2:
        common = set(rows["json"]) & set(rows["units"])
        total = {"aligned": 0, "same": 0, "a_char_b_narr": 0, "a_narr_b_char": 0, "both_char_differ": 0}
        per_file = {}
        for name in sorted(common):
            a, b = rows["json"][name], rows["units"][name]
            if "error" in a or "error" in b:
                continue
            ag = agreement(a["entries"], b["entries"])
            for key in total:
                total[key] += ag[key]
            per_file[name] = round(ag["same"] / ag["aligned"], 4) if ag["aligned"] else None
        n = total["aligned"]
        report["agreement"] = {
            "aligned_chars": n,
            "speaker_match": (total["same"] / n) if n else None,
            "json_char_units_narr": (total["a_char_b_narr"] / n) if n else None,
            "json_narr_units_char": (total["a_narr_b_char"] / n) if n else None,
            "both_char_differ": (total["both_char_differ"] / n) if n else None,
            "per_file": per_file,
        }
        report["gates"], report["gates_passed"] = evaluate_gates(report)
    return report


def merge_baseline(report: dict, rows: dict, base: Path) -> None:
    """Fill the json side of the report from an earlier run's saved outputs/aggregates."""
    old = json.loads((base / "report.json").read_text(encoding="utf-8"))
    report["protocols"]["json"] = old["protocols"]["json"]
    total = {"aligned": 0, "same": 0, "a_char_b_narr": 0, "a_narr_b_char": 0, "both_char_differ": 0}
    per_file = {}
    for name, row in rows["units"].items():
        path = base / "json" / (Path(name).stem + ".json")
        if "error" in row or not path.is_file():
            continue
        ag = agreement(json.loads(path.read_text(encoding="utf-8")), row["entries"])
        for key in total:
            total[key] += ag[key]
        per_file[name] = round(ag["same"] / ag["aligned"], 4) if ag["aligned"] else None
    n = total["aligned"]
    report["agreement"] = {
        "aligned_chars": n,
        "speaker_match": (total["same"] / n) if n else None,
        "json_char_units_narr": (total["a_char_b_narr"] / n) if n else None,
        "json_narr_units_char": (total["a_narr_b_char"] / n) if n else None,
        "both_char_differ": (total["both_char_differ"] / n) if n else None,
        "per_file": per_file,
    }
    report["gates"], report["gates_passed"] = evaluate_gates(report)


def evaluate_gates(report: dict) -> tuple[dict, bool]:
    j, u = report["protocols"]["json"], report["protocols"]["units"]
    gates = {}
    if j["random_rate"] is not None and u["random_rate"] is not None:
        gates["random_bucket_error"] = (
            u["random_rate"] <= j["random_rate"] + GATES["random_rate_margin"],
            f"units {u['random_rate']:.1%} vs json {j['random_rate']:.1%} (margin {GATES['random_rate_margin']:.0%})")
    else:
        gates["random_bucket_error"] = (False, "no random-bucket sample")
    fb = u["fallback_chunks"] / u["chunks"] if u["chunks"] else 0.0
    gates["fallback_chunks"] = (fb < GATES["fallback_chunks_max"], f"{fb:.1%} of chunks")
    edits = u["edit_applied"] + u["edit_rejected"]
    rr = u["edit_rejected"] / edits if edits else 0.0
    if edits < GATES["edit_min_sample"]:
        gates["edit_reject_rate"] = (True, f"not evaluated: only {edits} edits (< {GATES['edit_min_sample']}); "
                                           f"rejected {u['edit_rejected']}")
    else:
        gates["edit_reject_rate"] = (rr < GATES["edit_reject_max"], f"{rr:.1%} of {edits} edits")
    ratio = u["parse_seconds"] / j["parse_seconds"] if j["parse_seconds"] else 1.0
    gates["parse_time"] = (ratio <= GATES["parse_time_ratio_max"], f"units/json = {ratio:.2f}")
    return {k: {"pass": v[0], "detail": v[1]} for k, v in gates.items()}, all(v[0] for v in gates.values())


def render(report: dict) -> str:
    lines = [f"\nchapters: {report['files']}"]
    for protocol, p in report["protocols"].items():
        rate = f"{p['random_rate']:.1%}" if p["random_rate"] is not None else "n/a"
        lines.append(
            f"[{protocol}] ok={p['chapters_ok']} failed={len(p['chapters_failed'])} chars={p['chars']} "
            f"entries={p['entries']} parse={p['parse_seconds']}s total={p['total_seconds']}s "
            f"completion_tokens={p['completion_tokens']} random_bucket={p['random_errors']}/{p['random_n']} ({rate}) "
            f"chunks={p['chunks']} fallback={p['fallback_chunks']} edits={p['edit_applied']}+{p['edit_rejected']}rej "
            f"rejudge_calls={p['rejudge_calls']} boundary={p['boundary_fixed']}/{p['boundary_checked']} "
            f"spot={p['spot_fixed']}/{p['spot_checked']}")
    if "agreement" in report:
        a = report["agreement"]
        if a["speaker_match"] is not None:
            per = [v for v in a["per_file"].values() if v is not None]
            lines.append(
                f"speaker agreement (text-aligned): {a['speaker_match']:.2%} of {a['aligned_chars']} chars; "
                f"json=character/units=narrator {a['json_char_units_narr']:.2%}; "
                f"json=narrator/units=character {a['json_narr_units_char']:.2%}; "
                f"both characters but different {a['both_char_differ']:.2%}; "
                f"worst chapter {min(per):.2%}, median {statistics.median(per):.2%}")
        for name, g in report.get("gates", {}).items():
            lines.append(f"  gate {name}: {'PASS' if g['pass'] else 'FAIL'} — {g['detail']}")
        lines.append("ALL GATES PASSED" if report["gates_passed"] else "GATES FAILED — keep parse_protocol=json")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
