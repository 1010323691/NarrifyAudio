"""压测引擎 —— 音频合成批处理边界的临时测试入口（前端「压测」子窗口）。

用**机器自动生成**的自然语句（不借助 LLM、不需要解析脚本）探测批量合成的批内边界：
固定「批内行数」（``--concurrency`` 上限），每行字数从用户起点开始**每轮递增**（步长用户
设定，默认 +10），用任意一个已有克隆音色一轮一轮跑下去，直到某一轮失败为止。

失败判定（吞吐标准）：**1 秒必须出 10 个字** —— 每轮限时 = 总字数 / 10 秒
（例：64 行 × 50 字 = 3200 字 → 320 秒内必须完成）。计时从 worker 打印「模型就绪。」
（模型加载完成）到进程退出：只计真实合成时间，不含模型加载。超时（在 ``on_line`` 里抛
异常，``run_worker`` 的 finally 杀掉进程树）/ 看门狗 124 / 引擎非零退出 → 该轮判定失败，
压测停止（不再重试、不再缩批——崩溃 / 超慢即边界）。

每轮报告：处理量（行数 × 字数）、合成耗时、真实吞吐量（字/秒）、判定。全部结果写入
工作空间 ``stress_test/`` 目录（每次压测运行一个 JSON，含逐轮明细 + 运行日志路径）；
每轮的临时产物（段表 + 音频）在 ``00_temp/`` 里用完即删，不触碰管线目录。
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from ..core import pathio
from ..core.config import get_config
from ..core.paths import get_layout
from ..engines.tts import WorkerWatchdogTimeout, resolve_engine, run_worker
from .tts_batch import _build_cmd, clamp_concurrency

# 吞吐标准：1 秒必须出 10 个字（每轮限时 = 总字数 / 10 秒）。
MIN_THROUGHPUT_CHARS_PER_SEC = 10
# 限时判满后允许排空的宽限（秒）：让 worker 把已在飞的 [segment] 行打完再杀进程，
# 段计数更完整；不会让判定本身放宽（结束后仍按实测合成耗时复核）。
DEADLINE_GRACE_SECONDS = 5.0
# worker 侧单行字符上限（MAX_SEQ_CHARS）：压测递增到此为止（再长独批且解码被 MAX_NEW_TOKENS 截断）。
MAX_STRESS_CHARS = 2500
# 轮数硬上限（防病态无限循环；UI 留空 = 用此上限，实际远先因失败停止）。
MAX_STRESS_ROUNDS = 10000
# worker batch 模式打印的「模型就绪」行 —— 合成计时的起点（模型加载完成时刻）。
MODEL_READY_LINE = "模型就绪。"
# 测试结果目录（工作空间根下，非管线目录；每次运行一个 JSON 文件）。
RESULT_DIR_NAME = "stress_test"


class _RoundDeadline(Exception):
    """Internal: the round's throughput deadline elapsed (raised from ``on_line`` so
    ``run_worker``'s finally kills the process tree and the round settles as a failure)."""


def round_deadline_seconds(total_chars: int) -> float:
    """The round's throughput budget: ``total_chars / MIN_THROUGHPUT_CHARS_PER_SEC`` (pure)."""
    return max(0, int(total_chars)) / MIN_THROUGHPUT_CHARS_PER_SEC


def judge_round(synth_seconds: float | None, deadline_seconds: float) -> tuple[bool, str]:
    """The round's verdict (pure): pass iff the synthesis finished (process exit after the
    「模型就绪」 line) within the throughput budget. ``None`` (killed before the ready line /
    before completion) is a fail — the standard can't be measured, and a killed round never
    passed."""
    if synth_seconds is None:
        return False, "无法测得合成耗时（超时被杀或未见「模型就绪」）"
    if synth_seconds <= deadline_seconds:
        return True, ""
    return False, (f"超时：合成 {synth_seconds:.0f}s 超过限时 {deadline_seconds:.0f}s"
                   f"（未达 {MIN_THROUGHPUT_CHARS_PER_SEC} 字/秒）")


# 自然、口语化的中文句池（每句以「。」收尾）。压测文本从由这些句子顺序拼成的长段落里
# 切片而来——程序自动生成、可复现、行与行之间靠切片起点错开，全程不碰 LLM。
_STRESS_SENTENCES = (
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
    "车轮碾过青石路，发出沉闷而有节奏的声响。",
    "孩子们追着灯笼跑，笑声一路洒在胡同里。",
)

# 足够长的段落：任何允许的字数码（≤ MAX_STRESS_CHARS）加任意切片起点都能完整切出。
_PARAGRAPH = "".join(_STRESS_SENTENCES) * 20


def generate_text(length: int, offset: int) -> str:
    """Exactly ``length`` characters of natural Chinese text (pure).

    Sliced from the paragraph pool starting at a per-row rotation ``offset`` — the rows of
    one run differ from each other while the text stays machine-generated and stable.
    """
    length = max(0, int(length))
    if length <= 0:
        return ""
    para = _PARAGRAPH + _PARAGRAPH  # wrap-safe: offset lands in the first copy
    off = int(offset) % len(_PARAGRAPH)
    return para[off:off + length]


def pick_stress_speaker(voice_config: dict, speaker: str | None = None) -> str:
    """The character that renders the stress rows (pure).

    An explicit ``speaker`` must exist and be a usable clone (``type == "clone"`` with a
    non-empty ``ref_audio`` and ``ref_text`` — the worker's ``_build_clone_prompt`` requires
    both). Without one, the FIRST such clone entry is used — the test voice is arbitrary
    (「随便任意都可以」), any existing clone works. Raises a clear ``RuntimeError`` when no
    clone is available at all.
    """
    def _usable(entry) -> bool:
        return (isinstance(entry, dict) and entry.get("type") == "clone"
                and bool(entry.get("ref_audio")) and bool((entry.get("ref_text") or "").strip()))

    if speaker:
        entry = voice_config.get(speaker)
        if not _usable(entry):
            raise RuntimeError(f"角色 {speaker} 不是可用的克隆音色（需要 ref_audio 与 ref_text）——"
                               f"请先在「角色配音」页制作克隆音频。")
        return speaker
    for name, entry in voice_config.items():
        if _usable(entry):
            return name
    raise RuntimeError("没有可用的克隆音色——请先在「角色配音」页制作至少一个克隆音频。")


class _RoundHandle:
    """One round's view of the task handle: progress is pinned at 0 (an unbounded round run
    has no global fraction — the bar would saw-tooth as each round's worker progress resets)
    but the step label always names the round; ``log`` / ``check`` (and the unused LLM
    telemetry methods) forward to the parent untouched, so cancel/pause semantics are
    identical to a single-file run.
    """

    def __init__(self, parent, label: str):
        self._parent = parent
        self._label = label

    def progress(self, frac: float, current: str = "") -> None:
        self._parent.progress(0.0, f"{self._label} · {current}" if current else self._label)

    def log(self, msg: str, level: str = "INFO") -> None:
        self._parent.log(msg, level)

    def check(self) -> None:
        self._parent.check()

    def llm_chunk(self, text: str) -> None:
        self._parent.llm_chunk(text)

    def llm_rate(self, chars: int, cps: float) -> None:
        self._parent.llm_rate(chars, cps)

    def llm_chars(self, chars: int, secs: float) -> None:
        self._parent.llm_chars(chars, secs)


def stress_test(handle, rows, start_chars, step_chars, max_rounds=None,
                speaker=None, seed=None) -> dict:
    """Task worker: fixed 批内行数, per-row char count starting at ``start_chars`` and
    growing by ``step_chars`` EVERY round, each round one engine subprocess, running until
    a round fails the throughput standard (``MIN_THROUGHPUT_CHARS_PER_SEC`` chars/second) —
    timeout, watchdog 124, or a non-zero engine exit. No retries, no batch shrinking: the
    first failing round IS the boundary.

    Each round's synthesis time is measured from the worker's 「模型就绪。」 line (model
    load done) to process exit — load time never counts against the 10 字/秒 standard. The
    deadline is enforced per line (``_RoundDeadline`` raised from ``on_line`` → ``run_worker``
    kills the process tree) and re-checked on the measured time after a clean exit, so a
    round that finishes one line past the budget still fails.

    All per-round artifacts are throwaway (``00_temp/``, deleted per round); the full
    per-round report is written to ``<workspace>/stress_test/stress_<ts>.json``.
    """
    rows = clamp_concurrency(rows)
    start_chars = max(1, min(MAX_STRESS_CHARS, int(start_chars)))
    step_chars = max(1, int(step_chars))
    max_rounds = max(1, min(MAX_STRESS_ROUNDS, int(max_rounds or MAX_STRESS_ROUNDS)))

    layout = get_layout()
    ws = layout.workspace

    vc_path = layout.voice_profiles / "voice_config.json"
    voice_config: dict = {}
    if vc_path.exists():
        try:
            loaded = json.loads(vc_path.read_text("utf-8"))
            if isinstance(loaded, dict):
                voice_config = loaded
        except Exception:  # noqa: BLE001
            voice_config = {}
        # Same lazy migration as the batch path: the worker resolves the relative
        # ``ref_audio`` against ``--workspace``, so the file must be rewritten before spawn.
        _n, migrated = pathio.migrate_entries_in(vc_path, ws, "dict", ("ref_audio",))
        if isinstance(migrated, dict):
            voice_config = migrated

    sp_name = pick_stress_speaker(voice_config, speaker)
    handle.log(f"压测音色：{sp_name}（克隆）· 批内 {rows} 行 · 每行 {start_chars} 字起 · "
               f"每轮 +{step_chars} 字 · 吞吐标准 {MIN_THROUGHPUT_CHARS_PER_SEC} 字/秒")

    cfg = get_config()
    t = cfg.tts
    python, worker = resolve_engine()
    seed = seed if seed is not None else t.batch_seed
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1

    run_log = layout.logs / f"tts_stress_{time.strftime('%Y%m%d_%H%M%S')}.log"
    handle.log(f"运行日志（排障用，含每轮完整引擎输出）：{run_log}")

    results: list[dict] = []
    failed_at_chars: int | None = None
    stopped = "失败（未达吞吐标准 / 看门狗 / 引擎错误）"
    L = start_chars
    for k in range(1, max_rounds + 1):
        handle.check()  # cancel / pause point between rounds
        deadline = round_deadline_seconds(L * rows)
        label = f"第 {k} 轮 · {rows} 行 × {L} 字"
        sub = _RoundHandle(handle, label)
        sub.log(f"—— {label} = {L * rows} 字（限时 {deadline:.0f}s @ "
                f"{MIN_THROUGHPUT_CHARS_PER_SEC} 字/秒，计时自「模型就绪」起）——")

        segments = [
            {"index": r, "speaker": sp_name, "text": generate_text(L, r * 13),
             "instruct": "", "pause_after": None}
            for r in range(rows)
        ]
        seg_file = layout.temp / f"stress_segments_{uuid.uuid4().hex[:12]}.json"
        out_dir = layout.temp / f"stress_out_{time.strftime('%H%M%S')}_{L}"
        seg_file.write_text(json.dumps(segments, ensure_ascii=False), encoding="utf-8")
        out_dir.mkdir(parents=True, exist_ok=True)

        ok = [0]
        fail = [0]
        ready_at: list[float | None] = [None]  # monotonic() of the 「模型就绪」 line
        budget = deadline + DEADLINE_GRACE_SECONDS

        def on_line(line: str, _sub=sub) -> None:
            # Deadline enforcement per line: once the synthesis clock (model-ready → now)
            # passes the budget + grace, raise so run_worker's finally kills the process
            # tree (the drain is single-threaded, so the clock and the kill are exact).
            if ready_at[0] is not None and time.monotonic() - ready_at[0] > budget:
                raise _RoundDeadline(f"合成超过限时 {deadline:.0f}s（+{DEADLINE_GRACE_SECONDS:.0f}s 宽限）")
            if ready_at[0] is None and line == MODEL_READY_LINE:
                ready_at[0] = time.monotonic()
            if line.startswith("[segment]"):
                parts = line[len("[segment]"):].split(None, 2)
                if len(parts) >= 2 and parts[1] == "ok":
                    ok[0] += 1
                    _sub.log(f"第 {ok[0] + fail[0]}/{rows} 行：完成")
                else:
                    fail[0] += 1
                    _sub.log(f"第 {ok[0] + fail[0]}/{rows} 行：失败（{parts[2] if len(parts) > 2 else ''}）",
                             "ERROR")
            elif line.startswith("[watchdog]"):
                _sub.log(line, "WARNING")
            else:
                _sub.log(line)

        cmd = _build_cmd(
            python, worker, seg_file, vc_path, out_dir,
            language=t.language, device=t.device,
            model=t.model, base_model=t.base_model, design_model=t.design_model,
            ffmpeg_path=cfg.ffmpeg.ffmpeg_path, concurrency=rows, seed=seed,
            workspace=ws,
        )

        synth_secs: float | None = None
        reason = ""
        try:
            run_worker(cmd, sub, on_line, temp_files=(seg_file,),
                       fail_prefix="压测引擎", watchdog_code=124, log_file=run_log)
            if ready_at[0] is not None:
                synth_secs = time.monotonic() - ready_at[0]
                # Re-check on the MEASURED time: a clean exit just past the budget still fails
                # (the per-line check only fires when the worker emits another line).
                passed, reason = judge_round(synth_secs, deadline)
                if not passed:
                    sub.log(f"{label}：{reason}", "WARNING")
            else:
                # The ready line never arrived (a clean exit without it is malformed output
                # for batch mode) — the standard can't be measured.
                passed, reason = judge_round(None, deadline)
                sub.log(f"{label}：{reason}", "WARNING")
        except _RoundDeadline as e:
            synth_secs = None
            passed = False
            reason = f"超时（{e}）"
            sub.log(f"{label}：{reason} → 杀掉进程树，压测停止", "WARNING")
        except WorkerWatchdogTimeout:
            synth_secs = None
            passed = False
            reason = "看门狗超时（子批超出预算，进程被杀，退出码 124）"
            sub.log(f"{label}：{reason}（此前完成 {ok[0]} 行）→ 压测停止", "WARNING")
        except RuntimeError as e:
            synth_secs = None
            passed = False
            reason = str(e)
            sub.log(f"{label}：引擎失败 → 压测停止", "WARNING")
        finally:
            # Our own temp artifacts (段表已由 run_worker 清理) — never pipeline output.
            shutil.rmtree(out_dir, ignore_errors=True)

        throughput = (round(L * rows / synth_secs, 1) if (passed and synth_secs) else None)
        sub.log(f"{label} 结束：成功 {ok[0]} / 失败 {fail[0]} / 共 {rows} 行 · "
                f"合成 {'—' if synth_secs is None else f'{synth_secs:.0f}s'} · "
                f"吞吐 {'—' if throughput is None else f'{throughput} 字/秒'} · "
                f"{'通过' if passed else '失败（' + reason + '）'}")

        results.append({
            "round": k,
            "chars_per_line": L,
            "rows": rows,
            "total_chars": L * rows,
            "ok": ok[0],
            "failed": fail[0],
            "synth_seconds": round(synth_secs, 1) if synth_secs is not None else None,
            "deadline_seconds": round(deadline, 1),
            "throughput_chars_per_sec": throughput,
            "passed": passed,
            "reason": reason,
        })
        handle.check()

        if not passed:
            failed_at_chars = L
            break
        if L + step_chars > MAX_STRESS_CHARS:
            stopped = f"达到单行字数上限（{MAX_STRESS_CHARS} 字，worker 侧 MAX_SEQ_CHARS）"
            break
        L += step_chars
    else:
        stopped = f"达到轮数上限（{max_rounds} 轮，均未失败）"

    # 结果落盘：工作空间 stress_test/ 目录，每次运行一个 JSON（含逐轮明细 + 日志路径）。
    result = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "speaker": sp_name,
        "rows": rows,
        "start_chars": start_chars,
        "step_chars": step_chars,
        "min_throughput_chars_per_sec": MIN_THROUGHPUT_CHARS_PER_SEC,
        "seed": seed,
        "results": results,
        "failed_at_chars": failed_at_chars,
        "stopped_reason": stopped,
        "run_log": str(run_log),
    }
    result_dir = ws / RESULT_DIR_NAME
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / f"stress_{time.strftime('%Y%m%d_%H%M%S')}.json"
    result_path.write_bytes(json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
    result["result_path"] = str(result_path)

    handle.log(f"压测结束：共 {len(results)} 轮"
               + (f"，第 {len(results)} 轮（{failed_at_chars} 字/行）失败 → 边界" if failed_at_chars else "")
               + f" · {stopped} · 结果：{result_path.name}")
    handle.progress(1.0, "完成")
    return result
