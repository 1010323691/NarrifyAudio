"""Regression test for the tts_worker multi-role sub-batch failure path (plan Q1).

The module-level ``log()`` takes a single argument, but the multi-role sub-batch retry path
inside ``_run_batch`` used to call it with a second ``"WARNING"`` argument — so a failing
multi-role sub-batch raised ``TypeError`` inside the except block (masking the original
error, skipping the concurrency halving, and failing the whole batch). This drives the
promoted ``run_planned_group`` with a synth that fails exactly once, asserting the retry
behavior: halved concurrency, regrouped pending rows, warning line, and completion.
``tts_worker``'s top-level imports are stdlib only (torch / pydub are lazily imported in
functions), so it can be imported directly in the backend venv.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tts-engine"))

import tts_worker  # noqa: E402


def _call(group_kwargs):
    return tts_worker.run_planned_group(
        model=None, vtype="custom",
        args=types.SimpleNamespace(auto_batch=False),
        clone_prompts=None, device="cpu", seed=0, sub_counter=[0],
        out_dir=None, width=None, report=None, pipeline=None,
        clear_gpu_cache=lambda device: None,
        **group_kwargs)


def test_failed_multi_role_sub_batch_halves_concurrency_and_recovers():
    logs = []
    synth_rows = []          # rows handed to the (faked) synth, in order
    planner_inputs = []      # (remaining, concurrency) handed to the (faked) planner

    def fake_synth(model, vtype, rows, *, args, clone_prompts, device, seed,
                   sub_counter, out_dir, width, report, pipeline):
        synth_rows.append(list(rows))
        if len(synth_rows) == 1:
            raise RuntimeError("simulated OOM")

    def fake_planner(remaining, concurrency, restore_stack):
        planner_inputs.append((list(remaining), concurrency))
        take = max(1, min(concurrency, len(remaining)))
        return remaining[:take], remaining[take:], None, None

    def fake_log(msg):  # single argument only — a 2-arg call would TypeError right here
        logs.append(msg)

    rows = [{"index": i, "speaker": "a" if i % 2 == 0 else "b", "chars": 10}
            for i in range(4)]

    successful = _call({
        "planned_rows": rows, "planned_concurrency": 4,
        "synth_sub_batch": fake_synth, "log": fake_log, "planner": fake_planner,
    })

    # First multi-role sub-batch (2 rows) fails; the loop halves 2 -> 1 and regroups the
    # failed rows with the rest; single-row sub-batches then complete one at a time.
    assert len(synth_rows) == 5
    assert len(synth_rows[0]) == 2
    assert all(len(batch) == 1 for batch in synth_rows[1:])
    assert planner_inputs[0][1] == 2                       # initial temporary concurrency
    assert [c for _remaining, c in planner_inputs[1:]] == [1, 1, 1, 1]
    assert planner_inputs[1][0] == planner_inputs[0][0]    # regrouped: failed half + untried rest
    # The single-arg warning line proves the retry path ran (the old code raised here).
    warning_lines = [line for line in logs if line.startswith("警告：多角色子批失败")]
    assert warning_lines == ["警告：多角色子批失败：临时并发 2 → 1，保留未完成段继续重试"]
    assert successful == 4                                 # all 4 rows settled after recovery


def test_single_role_group_runs_untouched():
    logs = []
    synth_rows = []

    def fake_synth(model, vtype, rows, *, args, clone_prompts, device, seed,
                   sub_counter, out_dir, width, report, pipeline):
        synth_rows.append(list(rows))

    rows = [{"index": i, "speaker": "a", "chars": 5} for i in range(3)]
    successful = _call({
        "planned_rows": rows, "planned_concurrency": 3,
        "synth_sub_batch": fake_synth, "log": logs.append,
    })

    # A single-speaker group is one sub-batch, no splitting, no halving.
    assert successful == 1
    assert synth_rows == [rows]
    assert "子批（custom）：3 段（当前预定并发 3）" in logs


def test_empty_planned_group_is_a_noop():
    assert _call({"planned_rows": [], "planned_concurrency": 0,
                  "synth_sub_batch": None, "log": lambda msg: None}) == 0
