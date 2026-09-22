"""Adaptive batch ceiling curve for several fixed text lengths.

Each probe runs in a fresh CUDA process and uses two complete model batches.  The
search assumes only that shorter text is a useful lower-cost prior; every final
point is still measured independently.  Reports are written after every probe so
an interrupted run can be resumed with --output pointing at the same directory.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from types import SimpleNamespace

from benchmark import trial


def align8(value: float) -> int:
    return max(8, int(round(value / 8.0)) * 8)


def first_guess(chars: int, *, anchor_chars=200, anchor_batch=80, fixed_overhead=200) -> int:
    # A conservative effective-overhead model, anchored to the observed 80 x 200 pass.
    return align8(anchor_batch * (anchor_chars + fixed_overhead) / (chars + fixed_overhead))


def next_probe(low: int, high: int) -> int | None:
    if high - low <= 8:
        return None
    mid = align8((low + high) / 2)
    if mid <= low:
        mid = low + 8
    if mid >= high:
        return None
    return mid


def quadratic_guess(chars: int) -> int:
    """Quadratic fitted through the measured (200,80), (150,96), (100,128) points."""
    return min(512, align8(0.0032 * chars * chars - 1.44 * chars + 240))


def _status_ok(result):
    return result.get("status") == "ok"


def _new_probe(size, chars, output, *, stall_seconds):
    index = len(list(output.glob("probe-*")))
    folder = output / f"probe-{chars}-{size}-{index}"
    while folder.exists():
        index += 1
        folder = output / f"probe-{chars}-{size}-{index}"
    folder.mkdir(parents=True, exist_ok=False)
    args = SimpleNamespace(
        samples=size * 2,
        lengths=str(chars),
        max_chars=size * chars * 2,
        ratio=1.0,
        vocoder_batch_size=8,
        stall_seconds=stall_seconds,
        run_seconds=1800,
    )
    return trial(args, size, 0, folder)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--lengths", default="20,50,100,150,200")
    ap.add_argument("--ceiling", type=int, default=512)
    ap.add_argument("--stall-seconds", type=float, default=240)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--seed-report", type=Path,
                    help="reuse prior benchmark_boundary report entries for matching 200-char probes")
    ap.add_argument("--verify-quadratic", action="store_true",
                    help="probe only points from the fitted quadratic and skip boundary expansion")
    args = ap.parse_args()
    lengths = sorted({int(x) for x in args.lengths.split(",")}, reverse=True)
    if not lengths or any(not 1 <= n <= 200 for n in lengths) or args.ceiling < 80 or args.ceiling > 512:
        ap.error("lengths must be 1..200 and ceiling must be 80..512")
    if args.output.exists() and not args.resume:
        ap.error("output exists; pass --resume to continue")
    args.output.mkdir(parents=True, exist_ok=True)
    report_file = args.output / "report.json"
    report = json.loads(report_file.read_text("utf-8")) if report_file.exists() else {
        "parameters": {"lengths": lengths, "ceiling": args.ceiling,
                       "vocoder_batch_size": 8, "samples_per_probe": "2x batch"},
        "runs": [], "curve": {},
    }
    runs = report.setdefault("runs", [])
    curve = report.setdefault("curve", {})
    if args.seed_report and not runs:
        seeded = json.loads(args.seed_report.read_text("utf-8"))
        for item in seeded.get("screened", []) + seeded.get("validated", []):
            if item.get("size") in (80, 88) and item.get("status"):
                row = dict(item)
                row["chars"] = 200
                runs.append(row)
    seen = {(r["chars"], r["size"]): r for r in runs}

    def save():
        report["curve"] = curve
        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def probe(chars, size):
        size = min(args.ceiling, align8(size))
        key = (chars, size)
        if key in seen:
            return seen[key]
        print(json.dumps({"event": "probe_start", "chars": chars, "size": size}), flush=True)
        result = _new_probe(size, chars, args.output, stall_seconds=args.stall_seconds)
        result["chars"] = chars
        seen[key] = result
        runs.append(result)
        save()
        print(json.dumps({"event": "probe_end", "chars": chars, "size": size,
                          "status": result["status"],
                          "chars_per_second": result["chars_per_second"],
                          "vram_peak_mib": result["vram_peak_mib"]}), flush=True)
        return result

    if args.verify_quadratic:
        for chars in lengths:
            size = quadratic_guess(chars)
            result = probe(chars, size)
            entry = curve.setdefault(str(chars), {})
            entry.update({
                "quadratic_batch": size,
                "status": result.get("status"),
                "chars_per_second": result.get("chars_per_second"),
                "vram_peak_mib": result.get("vram_peak_mib"),
            })
            save()
            print(json.dumps({"event": "quadratic_done", "chars": chars, "size": size,
                              "status": result.get("status")}), flush=True)
        report["quadratic"] = {
            "formula": "0.0032*chars^2 - 1.44*chars + 240",
            "anchors": [[200, 80], [150, 96], [100, 128]],
            "aligned_to": 8,
        }
        report["recommended_curve"] = [
            {"chars": int(chars),
             "batch": data.get("max_passing_batch", data.get("quadratic_batch"))}
            for chars, data in sorted(curve.items(), key=lambda item: int(item[0]))
            if data.get("max_passing_batch", data.get("quadratic_batch")) is not None
        ]
        save()
        return

    for chars in lengths:
        points = {size: r for (c, size), r in seen.items() if c == chars}
        # Reuse the already validated 200x80/88 boundary when present in the prior report.
        if chars == 200 and 80 not in points:
            points[80] = probe(chars, 80)
        if chars == 200 and 88 not in points:
            points[88] = probe(chars, 88)

        prior_safe = [size for size, result in points.items() if _status_ok(result)]
        # A shorter row should not need to restart from the global heuristic when a
        # longer tested length already established a safe lower bound.
        prior_safe.extend(
            data["max_passing_batch"]
            for other_chars, data in curve.items()
            if int(other_chars) > chars and data.get("max_passing_batch") is not None
        )
        guess = min(args.ceiling, max(prior_safe, default=first_guess(chars)))
        if guess not in points:
            points[guess] = probe(chars, guess)
        if _status_ok(points[guess]):
            low = guess
            high = min(args.ceiling, align8(guess * 1.5))
            while high > low and high not in points:
                points[high] = probe(chars, high)
                if not _status_ok(points[high]):
                    break
                low = high
                if high == args.ceiling:
                    break
                high = min(args.ceiling, align8(high * 1.5))
        else:
            high = guess
            if 80 not in points:
                points[80] = probe(chars, 80)
            if not _status_ok(points[80]):
                curve[str(chars)] = {"status": "no_safe_probe", "points": sorted(points)}
                save()
                continue
            low = 80

        while True:
            candidate = next_probe(low, high)
            if candidate is None:
                break
            points[candidate] = probe(chars, candidate)
            if _status_ok(points[candidate]):
                low = candidate
            else:
                high = candidate
        curve[str(chars)] = {
            "max_passing_batch": low,
            "failed_or_unchecked_upper": high,
            "points": [
                {"size": size, "status": points[size].get("status"),
                 "chars_per_second": points[size].get("chars_per_second"),
                 "vram_peak_mib": points[size].get("vram_peak_mib")}
                for size in sorted(points)
            ],
        }
        save()
        print(json.dumps({"event": "length_done", "chars": chars,
                          "max_passing_batch": low, "upper": high}), flush=True)

    # Report the measured monotonic points; production applies them as upward-matched tiers.
    measured = [(int(chars), data["max_passing_batch"])
                for chars, data in curve.items() if "max_passing_batch" in data]
    measured.sort()
    report["recommended_curve"] = [
        {"chars": chars, "batch": batch} for chars, batch in measured
    ]
    save()


if __name__ == "__main__":
    main()
