"""Tests for the BGM engine (背景音乐系统 · 匹配链):

* ``engines.bgm`` pure functions (``score_track`` / ``match_chapter`` — seeded
  rng, adjacent de-dup with staged relaxation, generic fallback, random mode)
  and ``build_mix_cmd`` (element-pinned, including the short-chapter fade clamp
  ``min(fade, duration/2)``);
* the two ``08_bgm`` JSON caches (missing-not-written / corrupt downgrade /
  ``write_bytes`` no CRLF / atomic save);
* ``match_stems`` (ordered pass, locked-skip, single-chapter re-match boundary,
  mode persistence, empty tag buckets — chapter analysis is gone);
* ``mix_chapter`` e2e (fake Popen: success / rc≠0 / <1 KiB / cancel kills /
  narration missing / music missing / ``music=None`` copy2 byte-identical /
  ``merge_gate`` balance).

Everything is sandboxed like ``test_music.py`` (project root + music library dir
monkeypatched into a tmp dir); tasks run through the test-only thread executor
(``backend.tests.task_support``).
"""
from __future__ import annotations

import io
import json
import os
import random
import re
import threading
import time
from pathlib import Path

import types

import pytest

from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core import concurrency
from backend.tests.task_support import TERMINAL, TaskStatus, get_task_manager
from backend.engines import bgm as bgm_engine
from backend.engines import bgm_storage
from backend.engines import merge as merge_engine
from backend.engines import music as music_engine
from backend.engines import tts_batch

STEM = "第 001 章 测试"
STEM2 = "第 002 章 夜袭"


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Throwaway project root + workspace (with 02 chapter files) + music library."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    (ws / "02_split_text").mkdir(parents=True)
    for stem in (STEM, STEM2):
        (ws / "02_split_text" / f"{stem}.txt").write_bytes(
            ("本章节内容。" * 40).encode("utf-8"))
    core_config.set_workspace_pointer(str(ws))
    lib = tmp_path / "music_library"
    lib.mkdir(parents=True, exist_ok=True)
    for name in ("battle.mp3", "calm.mp3"):
        (lib / name).write_bytes(b"fake-music-bytes")
    # seed the library index with one tagged + one generic track
    music_engine.update_index(
        lambda idx: idx["tracks"].update({
            "battle.mp3": {"duration": 120.0, "enabled": True, "description": "",
                           "tags": {"scene": ["战斗"], "mood": ["紧张", "热血"],
                                    "emotion": [], "custom": []},
                           "added_at": ""},
            "calm.mp3": {"duration": 90.0, "enabled": True, "description": "",
                         "tags": {"scene": [], "mood": [], "emotion": [], "custom": []},
                         "added_at": ""},
        }))
    mgr = get_task_manager()
    yield {"root": tmp_path, "ws": ws, "lib": lib, "mgr": mgr}
    # drain any leaked tasks / gate slots (cooperative cancel honours by the engine)
    for t in list(mgr.list()):
        if t.module in ("bgm-segment", "bgm-mix") and t.status not in TERMINAL:
            try:
                mgr.control(t.id, "cancel")
            except KeyError:
                pass
    deadline = time.time() + 5
    while time.time() < deadline:
        stuck = [t for t in mgr.list()
                 if t.module in ("bgm-segment", "bgm-mix")
                 and t.status not in TERMINAL]
        if not stuck and concurrency.gate().active == 0 and concurrency.merge_gate().active == 0:
            break
        time.sleep(0.05)
    assert not stuck, f"bgm tasks leaked: {[t.label for t in stuck]}"
    assert concurrency.gate().active == 0 and concurrency.merge_gate().active == 0
    core_config.reset_config_cache()


def _wait_terminal(mgr, tid: str, timeout: float = 8.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = mgr.get(tid)
        if t.status in TERMINAL:
            return t.status
        time.sleep(0.02)
    raise AssertionError(f"task {tid} not terminal within {timeout}s")


def _wait_until(pred, timeout: float = 8.0, step: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(step)
    raise AssertionError(f"condition not met within {timeout}s")


# --------------------------------------------------------------------------- #
# score_track
# --------------------------------------------------------------------------- #

def test_track_scoring_contract():
    # score track weights and reason pinned
    s, r = bgm_engine.score_track(
        {"mood": ["紧张", "热血"], "scene": ["战斗"]},
        {"mood": ["紧张", "热血"], "scene": ["战斗"], "emotion": ["孤独"]},
    )
    assert s == 8  # 2×3 + 1×2
    assert r == "mood 命中 紧张, 热血(+6)；scene 命中 战斗(+2)"

    # score track cross bucket names do not hit
    # 悲伤 lives in BOTH mood and emotion — bucket identity disambiguates.
    s, r = bgm_engine.score_track({"mood": ["悲伤"]}, {"emotion": ["悲伤"]})
    assert (s, r) == (0, "")
    s2, r2 = bgm_engine.score_track({"emotion": ["悲伤"]}, {"emotion": ["悲伤"]})
    assert (s2, r2) == (1, "emotion 命中 悲伤(+1)")

    # score track custom weight and garbage input
    s, r = bgm_engine.score_track({"custom": ["我的"]}, {"custom": ["我的", "别的"]})
    assert (s, r) == (1, "custom 命中 我的(+1)")
    assert bgm_engine.score_track(None, {}) == (0, "")
    assert bgm_engine.score_track({}, None) == (0, "")
    assert bgm_engine.score_track({"mood": ["紧张", "紧张"]}, {"mood": ["紧张"]}) == (3, "mood 命中 紧张(+3)")


# --------------------------------------------------------------------------- #
# match_chapter (seeded)
# --------------------------------------------------------------------------- #

def _track(name, enabled=True, **tags):
    base = {"scene": [], "mood": [], "emotion": [], "custom": []}
    base.update(tags)
    return {"enabled": enabled, "tags": base}


def test_chapter_match_ties_and_deduplication():
    # match chapter highest score with tie random
    tracks = [
        ("a.mp3", _track("a.mp3", mood=["紧张"])),
        ("b.mp3", _track("b.mp3", mood=["紧张"])),
        ("c.mp3", _track("c.mp3", mood=["轻松"])),
    ]
    picks = {bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, None, None,
                                      rng=random.Random(i))["music"] for i in range(20)}
    assert picks == {"a.mp3", "b.mp3"}  # the higher tier wins; ties are random
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, None, None, rng=random.Random(7))
    assert res["via"] == "tags" and res["score"] == 3

    # match chapter adjacent dedupe then relax
    tracks = [
        ("a.mp3", _track("a.mp3", mood=["紧张"])),
        ("b.mp3", _track("b.mp3", mood=["紧张"])),
    ]
    # one neighbour blocked → the other is picked (no relaxation note)
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, "a.mp3", None, rng=random.Random(3))
    assert res["music"] == "b.mp3" and "放宽" not in res["reason"]
    # both blocked → relax inside the top tier (never fall through to generic)
    res2 = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, "a.mp3", "b.mp3", rng=random.Random(3))
    assert res2["music"] in ("a.mp3", "b.mp3") and "放宽" in res2["reason"]
    # generic pool is de-duped the same way
    tracks2 = [("g.mp3", _track("g.mp3"))]
    res3 = bgm_engine.match_chapter({"mood": ["不存在"]}, tracks2, 1, "g.mp3", None, rng=random.Random(3))
    assert res3["music"] == "g.mp3" and "放宽" in res3["reason"]


def test_chapter_match_fallbacks():
    # match chapter min score filter falls to generic
    tracks = [
        ("a.mp3", _track("a.mp3", mood=["紧张"])),          # score 3 < 5
        ("g.mp3", _track("g.mp3")),                          # generic
    ]
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 5, None, None, rng=random.Random(1))
    assert res == {"music": "g.mp3", "via": "generic", "score": 0,
                   "reason": "无标签命中，使用通用音乐"}

    # match chapter enabled only
    tracks = [("a.mp3", _track("a.mp3", enabled=False, mood=["紧张"]))]
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, None, None, rng=random.Random(1))
    assert res["via"] == "none" and res["music"] is None

    # match chapter no candidates
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, [], 1, None, None, rng=random.Random(1))
    assert res["music"] is None and res["via"] == "none"
    assert "无候选" in res["reason"]


def test_match_chapter_random_mode():
    tracks = [("a.mp3", _track("a.mp3", mood=["紧张"])), ("b.mp3", _track("b.mp3"))]
    res = bgm_engine.match_chapter({"mood": ["紧张"]}, tracks, 1, None, None,
                                   rng=random.Random(0), mode="random")
    assert res["via"] == "random" and res["score"] == 0
    assert res["reason"].startswith("全章节随机")
    # random mode still de-dupes neighbours (both blocked → relaxed note)
    res2 = bgm_engine.match_chapter({}, tracks, 1, "a.mp3", "b.mp3",
                                    rng=random.Random(0), mode="random")
    assert res2["music"] in ("a.mp3", "b.mp3") and "放宽" in res2["reason"]
    # random mode with an empty enabled pool
    res3 = bgm_engine.match_chapter({}, [("x.mp3", _track("x.mp3", enabled=False))],
                                    1, None, None, rng=random.Random(0), mode="random")
    assert res3["music"] is None and res3["via"] == "none"


# --------------------------------------------------------------------------- #
# build_mix_cmd
# --------------------------------------------------------------------------- #

def _cfg(volume=0.18, fade_in=1.5, fade_out=3.0, loop=True):
    return types.SimpleNamespace(volume=volume, fade_in=fade_in,
                                 fade_out=fade_out, loop=loop)


def test_mix_command_contract():
    # build mix cmd default shape
    cfg = _cfg()
    cmd = bgm_engine.build_mix_cmd("ffmpeg", Path("n.mp3"), Path("m.mp3"),
                                   Path("o.mp3"), 100.0, cfg, 60.0)
    assert cmd[:3] == ["ffmpeg", "-y", "-i"]
    assert cmd[3] == str(Path("n.mp3"))
    assert cmd[4:6] == ["-stream_loop", "-1"]
    assert cmd[6:8] == ["-i", str(Path("m.mp3"))]
    assert cmd[8] == "-filter_complex"
    fc = cmd[9]
    assert fc == (
        "[0:a]aresample=44100[nar];"
        "[1:a]aresample=44100,atrim=0:100.000,"
        "afade=t=in:d=1.500,afade=t=out:st=97.000:d=3.000,volume=0.18[bgm];"
        "[nar][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]"
    )
    assert cmd[10:] == ["-map", "[out]", "-c:a", "libmp3lame", str(Path("o.mp3"))]

    # build mix cmd threads
    # threads=N → `-threads N` 紧跟 -y（全局位，管住 libmp3lame 编码线程）
    cfg = _cfg()
    cmd = bgm_engine.build_mix_cmd("ffmpeg", Path("n.mp3"), Path("m.mp3"),
                                   Path("o.mp3"), 100.0, cfg, 60.0, threads=2)
    assert cmd[:5] == ["ffmpeg", "-y", "-threads", "2", "-i"]
    assert cmd[5] == str(Path("n.mp3"))
    # threads 段之后整体偏移 +2，尾部与缺省形态一致
    assert cmd[12:] == ["-map", "[out]", "-c:a", "libmp3lame", str(Path("o.mp3"))]
    # threads=0 / 省略 = 不发 flag（缺省命令与旧口径逐字节一致）
    for kwargs in ({}, {"threads": 0}):
        plain = bgm_engine.build_mix_cmd("ffmpeg", Path("n.mp3"), Path("m.mp3"),
                                         Path("o.mp3"), 100.0, cfg, 60.0, **kwargs)
        assert "-threads" not in plain
        assert plain[:3] == ["ffmpeg", "-y", "-i"]

    # build mix cmd no loop
    cfg = _cfg(volume=0.5, loop=False)
    cmd = bgm_engine.build_mix_cmd("ffmpeg", Path("n.mp3"), Path("m.mp3"),
                                   Path("o.mp3"), 50.0, cfg, 30.0)
    assert cmd[4:6] == ["-stream_loop", "-0"]
    fc = cmd[9]
    assert "volume=0.5[bgm]" in fc and "st=47.000:d=3.000" in fc

    # build mix cmd short chapter fade clamp
    # duration 4 s: fade_out 3.0 → clamped to 2.0 (min(3.0, 4/2)); st = 4 − 2 = 2.
    cfg = _cfg()
    cmd = bgm_engine.build_mix_cmd("ffmpeg", Path("n.mp3"), Path("m.mp3"),
                                   Path("o.mp3"), 4.0, cfg, 60.0)
    fc = cmd[9]
    assert "afade=t=in:d=1.500" in fc            # min(1.5, 2.0) = 1.5 (unchanged)
    assert "afade=t=out:st=2.000:d=2.000" in fc
    # extreme: 1 s chapter — both fades clamp to 0.5, st = 0.5 (never negative)
    cmd2 = bgm_engine.build_mix_cmd("ffmpeg", Path("n.mp3"), Path("m.mp3"),
                                    Path("o.mp3"), 1.0, cfg, 60.0)
    fc2 = cmd2[9]
    assert "afade=t=in:d=0.500" in fc2 and "afade=t=out:st=0.500:d=0.500" in fc2


# --------------------------------------------------------------------------- #
# 08_bgm JSON caches
# --------------------------------------------------------------------------- #

def test_analysis_missing_not_written(sandbox):
    layout = core_paths.get_or_prepare_layout()
    data = bgm_engine.load_analysis(layout)
    assert data == {"version": 1, "model": "", "chapters": {}}
    assert not (sandbox["ws"] / "08_bgm" / bgm_engine.ANALYSIS_NAME).exists()
    data2 = bgm_engine.load_assignments(layout)
    assert data2["chapters"] == {} and data2["mode"] == "random"


def test_analysis_corrupt_downgrades(sandbox):
    ws = sandbox["ws"] / "08_bgm"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / bgm_engine.ANALYSIS_NAME).write_bytes(b"{not json")
    layout = core_paths.get_or_prepare_layout()
    assert bgm_engine.load_analysis(layout)["chapters"] == {}
    (ws / bgm_engine.ASSIGNMENTS_NAME).write_bytes(b"[1, 2]")
    assert bgm_engine.load_assignments(layout)["chapters"] == {}


def test_analysis_save_roundtrip_no_crlf(sandbox):
    layout = core_paths.get_or_prepare_layout()
    data = bgm_engine.load_analysis(layout)
    data["model"] = "test-model"
    data["chapters"][STEM] = {"scene": ["战斗"], "mood": ["紧张"], "emotion": [],
                              "custom": [], "analyzed_at": "t", "edited": False}
    bgm_engine.save_analysis(layout, data)
    raw = (sandbox["ws"] / "08_bgm" / bgm_engine.ANALYSIS_NAME).read_bytes()
    assert b"\r\n" not in raw
    again = bgm_engine.load_analysis(layout)
    assert again["chapters"][STEM]["mood"] == ["紧张"]
    assert again["model"] == "test-model"


def test_analysis_cache_update_uses_task_publication_handle(sandbox):
    layout = core_paths.get_or_prepare_layout()
    staged = []

    class _JournalHandle:
        def stage_workspace_file(self, final_path, data):
            staged.append((final_path, json.loads(data)))

    bgm_engine.update_analysis(
        layout,
        lambda data: data["chapters"].update({STEM: {"scene": ["test"]}}),
        handle=_JournalHandle(),
    )

    assert staged == [(
        sandbox["ws"] / "08_bgm" / bgm_engine.ANALYSIS_NAME,
        {"version": 1, "model": "", "chapters": {STEM: {"scene": ["test"]}}},
    )]
    assert not staged[0][0].exists()


def test_bgm_analysis_rmw_is_exclusive_across_processes(sandbox):
    """Q2: the API process (tag propagation) and the Worker process (analysis
    cache writes) are separate OS processes — the in-process ``_BGMS_LOCK``
    cannot coordinate them, so every read → modify → publish cycle holds the
    cross-process ``storage_lock`` instead. A real child process plays the
    worker: it takes the lock, publishes its own chapter entry and keeps the
    lock held; the parent-side tag rewrite must block until that write
    lands, then rewrite on top of it (no lost writes either way)."""
    import subprocess
    import sys

    from backend.api import music as api_music

    layout = core_paths.get_or_prepare_layout()
    bgm_storage.save_analysis(layout, {
        "version": 1, "model": "seed", "chapters": {
            STEM: {"scene": [], "mood": ["紧张"], "emotion": [],
                   "custom": ["旧标签"], "analyzed_at": "t", "edited": False},
        },
    })
    child = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from backend.engines import bgm_storage\n"
        "bgm = Path(sys.argv[1])\n"
        "layout = SimpleNamespace(bgm=bgm)\n"
        "with bgm_storage.storage_lock(layout):\n"
        "    data = bgm_storage.load_analysis(layout)\n"
        "    data['chapters']['worker_chapter'] = {'scene': [], 'mood': [], "
        "'emotion': [], 'custom': ['worker-tag'], 'analyzed_at': 'w', 'edited': False}\n"
        "    bgm_storage.save_analysis(layout, data)\n"
        "    (bgm / 'child.locked').touch()\n"
        "    time.sleep(1.5)  # hold the lock across a (slow) model call\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child, str(layout.bgm)],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        marker = layout.bgm / "child.locked"
        deadline = time.time() + 30
        while not marker.exists() and proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "child process never acquired the storage lock"
        started = time.monotonic()
        api_music._propagate_chapter_analysis("旧标签", "新标签", "custom")
        blocked = time.monotonic() - started
    finally:
        proc.wait(timeout=30)
        output = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
    assert proc.returncode == 0, output
    # The API-side rewrite really waited for the other process's critical
    # section — without storage_lock it would have skipped straight through.
    assert blocked >= 1.0
    # No lost writes in either direction: the rename landed on top of the
    # worker's write, and the worker's entry survived the parent's rewrite.
    final = bgm_storage.load_analysis(layout)
    assert final["chapters"][STEM]["custom"] == ["新标签"]
    assert final["chapters"]["worker_chapter"]["custom"] == ["worker-tag"]


def test_bgm_rollback_keeps_concurrent_api_write_across_processes(sandbox):
    """M1 scenario (plan second-round objection): a Worker process publishes
    the shared analysis cache through the REAL publication journal (guarded),
    the API process rewrites the SAME cache via the real tag-propagation
    path, then the worker's failure rollback runs. The rollback must not
    restore over the API's edit — the fingerprint compare under the
    cross-process storage lock lets the concurrent writer win."""
    import subprocess
    import sys

    from backend.api import music as api_music

    layout = core_paths.get_or_prepare_layout()
    bgm_dir = layout.bgm
    bgm_storage.save_analysis(layout, {
        "version": 1, "model": "", "chapters": {"chapter-1": {"mood": ["original-tag"]}},
    })
    child = (
        "import logging, sys, time\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "logging.basicConfig(level=logging.WARNING)\n"
        "bgm = Path(sys.argv[1])\n"
        "from backend.platform.database import SessionLocal, initialize_schema\n"
        "initialize_schema()\n"
        "from backend.platform.models import User\n"
        "with SessionLocal() as db:\n"
        "    db.add(User(id='guard-worker-u', email='gw@example.com', username='guard-worker', password_hash='x'))\n"
        "    db.commit()\n"
        "from backend.platform.task_context import PersistentTaskHandle\n"
        "from backend.engines import bgm_storage\n"
        "layout = SimpleNamespace(bgm=bgm)\n"
        "claim = SimpleNamespace(task_id='guard-worker-t', attempt_id='guard-worker-a', "
        "owner_id='guard-worker-u', project_id='guard-p')\n"
        "handle = PersistentTaskHandle(claim)\n"
        "bgm_storage.update_analysis(layout, "
        "lambda d: d['chapters'].setdefault('chapter-1', {}).update({'mood': ['old-tag']}), "
        "handle=handle)\n"
        "(bgm / 'worker_published').touch()\n"
        "for _ in range(600):\n"
        "    if (bgm / 'api_wrote').exists():\n"
        "        break\n"
        "    time.sleep(0.1)\n"
        "handle.rollback_publications()\n"
        "(bgm / 'done').touch()\n"
    )
    # The child worker gets its OWN storage root (the sandbox dir, so the
    # workspace paths it publishes are inside it) while the API process
    # above keeps the suite's storage root — the 08_bgm dir is the shared
    # physical ground between them.
    env = {**os.environ, "NARRIFY_STORAGE_ROOT": str(sandbox["root"])}
    proc = subprocess.Popen(
        [sys.executable, "-c", child, str(bgm_dir)],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        marker = bgm_dir / "worker_published"
        deadline = time.time() + 30
        while not marker.exists() and proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "child worker never published its analysis update"
        # The API process rewrites the same cache through the real propagation path.
        api_music._propagate_chapter_analysis("old-tag", "new-tag", "mood")
        assert bgm_storage.load_analysis(layout)["chapters"]["chapter-1"]["mood"] == ["new-tag"]
        (bgm_dir / "api_wrote").touch()
        proc.wait(timeout=30)
        output = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, output
    # The API's rename survives the worker's failure rollback.
    assert bgm_storage.load_analysis(layout)["chapters"]["chapter-1"]["mood"] == ["new-tag"]
    # The worker logged the guarded skip (its version would have clobbered the API's).
    assert "Concurrent writer modified" in output
    # The worker's journal and backups are cleaned up (under the child's own storage root).
    attempt = sandbox["root"] / "guard-worker" / "guard-p" / ".tasks" / "guard-worker-t" / "guard-worker-a"
    assert not (attempt / "publication.json").exists()
    assert not list(attempt.glob("publication-backup-*"))


def test_list_chapter_stems(sandbox):
    layout = core_paths.get_or_prepare_layout()
    assert bgm_engine.list_chapter_stems(layout) == [STEM, STEM2]
    (sandbox["ws"] / "02_split_text" / "子目录").mkdir()
    (sandbox["ws"] / "02_split_text" / "notes.md").write_bytes(b"x")
    assert bgm_engine.list_chapter_stems(layout) == [STEM, STEM2]  # *.txt files only


# --------------------------------------------------------------------------- #
# match_stems
# --------------------------------------------------------------------------- #

def test_match_stems_random_pass_prev_is_fresh_result(sandbox):
    # Random mode: both enabled tracks are candidates; the second chapter's
    # neighbour is the first's FRESH result → excluded → the other track.
    layout = core_paths.get_or_prepare_layout()
    res = bgm_engine.match_stems(layout, [STEM, STEM2], "random", 1, rng=random.Random(11))
    assert res["mode"] == "random" and res["matched"] == 2 and res["no_bgm"] == 0
    a1 = res["assignments"]["chapters"][STEM]
    a2 = res["assignments"]["chapters"][STEM2]
    assert {a1["music"], a2["music"]} == {"battle.mp3", "calm.mp3"}
    # no chapter analysis anymore → empty tag buckets in the entry
    assert a1["tags"] == {"scene": [], "mood": [], "emotion": [], "custom": []}
    assert a1["score"] == 0 and a1["manual"] is False and a1["locked"] is False
    assert "随机" in a1["reason"] and a1["matched_at"]
    on_disk = bgm_engine.load_assignments(layout)
    assert on_disk["mode"] == "random" and on_disk["chapters"][STEM2]["music"] == a2["music"]


def test_match_stems_locked_skipped_whole(sandbox):
    layout = core_paths.get_or_prepare_layout()
    # Pre-seed: STEM locked with calm.mp3, STEM2 fresh.
    data = bgm_engine.load_assignments(layout)
    data["chapters"][STEM] = {"tags": {}, "music": "calm.mp3", "locked": True,
                              "manual": False, "score": None, "reason": "手动指定",
                              "matched_at": "old-t"}
    bgm_engine.save_assignments(layout, data)
    res = bgm_engine.match_stems(layout, [STEM, STEM2], "random", 1, rng=random.Random(1))
    assert res["skipped_locked"] == 1 and res["matched"] == 1
    locked = res["assignments"]["chapters"][STEM]
    assert locked["music"] == "calm.mp3" and locked["matched_at"] == "old-t"  # verbatim
    # the locked neighbour constrains STEM2: prev = calm.mp3 → excluded → battle
    assert res["assignments"]["chapters"][STEM2]["music"] == "battle.mp3"


def test_match_stems_single_chapter_boundary_uses_existing_neighbours(sandbox):
    # Re-matching ONLY STEM2 must read prev/next from the EXISTING assignments and
    # never touch the neighbours' entries.
    layout = core_paths.get_or_prepare_layout()
    data = bgm_engine.load_assignments(layout)
    data["chapters"][STEM] = {"tags": {}, "music": "battle.mp3", "locked": False,
                              "manual": False, "score": 0, "reason": "…", "matched_at": "t1"}
    bgm_engine.save_assignments(layout, data)
    # one enabled track left → the prev exclusion fully blocks the pool → relaxed
    music_engine.update_index(lambda idx: idx["tracks"].update(
        {n: {**t, "enabled": n == "battle.mp3"} for n, t in idx["tracks"].items()}))
    before = json.loads((sandbox["ws"] / "08_bgm" / bgm_engine.ASSIGNMENTS_NAME)
                        .read_bytes().decode("utf-8"))
    res = bgm_engine.match_stems(layout, [STEM2], "random", 1, rng=random.Random(1))
    after = json.loads((sandbox["ws"] / "08_bgm" / bgm_engine.ASSIGNMENTS_NAME)
                       .read_bytes().decode("utf-8"))
    assert after["chapters"][STEM] == before["chapters"][STEM]  # neighbour untouched
    # prev = battle.mp3 → blocked; only enabled track → relaxed re-pick of battle.mp3
    assert res["assignments"]["chapters"][STEM2]["music"] == "battle.mp3"
    assert "放宽" in res["assignments"]["chapters"][STEM2]["reason"]


def test_match_stems_no_bgm_and_random_mode(sandbox):
    layout = core_paths.get_or_prepare_layout()
    # disable everything → every chapter gets music=None (still a matched entry)
    music_engine.update_index(lambda idx: idx["tracks"].update(
        {n: {**t, "enabled": False} for n, t in idx["tracks"].items()}))
    res = bgm_engine.match_stems(layout, [STEM], "random", 1, rng=random.Random(1))
    assert res["no_bgm"] == 1 and res["matched"] == 0
    e = res["assignments"]["chapters"][STEM]
    assert e["music"] is None and e["matched_at"]  # matched_at distinguishes 判无 vs 从未
    # random mode over an empty enabled pool → same None outcome
    res2 = bgm_engine.match_stems(layout, [STEM2], "random", 1, rng=random.Random(1))
    assert res2["assignments"]["chapters"][STEM2]["music"] is None
    assert res2["mode"] == "random"
    # re-enabling the library: random mode draws from ALL enabled tracks
    music_engine.update_index(lambda idx: idx["tracks"].update(
        {n: {**t, "enabled": True} for n, t in idx["tracks"].items()}))
    res3 = bgm_engine.match_stems(layout, [STEM], "random", 1, rng=random.Random(2))
    e3 = res3["assignments"]["chapters"][STEM]
    assert e3["music"] in ("battle.mp3", "calm.mp3")
    assert e3["score"] == 0 and "随机" in e3["reason"]


# --------------------------------------------------------------------------- #
# mix_chapter e2e (fake Popen)
# --------------------------------------------------------------------------- #

def _seed_mix_inputs(sandbox, stem=STEM, music="battle.mp3", narration_bytes=b"NARR" * 256):
    ws = sandbox["ws"]
    (ws / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    (ws / "06_audio_merge" / f"{stem}.mp3").write_bytes(narration_bytes)
    data = bgm_engine.load_assignments(core_paths.get_or_prepare_layout())
    data["chapters"][stem] = {"tags": {}, "music": music, "locked": False,
                              "manual": False, "score": 3, "reason": "r", "matched_at": "t"}
    bgm_engine.save_assignments(core_paths.get_or_prepare_layout(), data)


class _FakeProc:
    def __init__(self, cmd, out_path: Path, out_size: int = 4096, rc: int = 0):
        self.cmd = cmd
        self._rc = rc
        self.killed = False
        self.stderr = io.BytesIO(b"fake stderr tail" if rc else b"")
        if rc == 0 and out_size:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"A" * out_size)

    def poll(self):
        return self._rc

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self._rc


def test_mix_success_and_gate_balance(sandbox, monkeypatch):
    _seed_mix_inputs(sandbox)
    procs = []

    def fake_popen(cmd, **kw):
        p = _FakeProc(cmd, core_paths.get_or_prepare_layout().bgm / f"{STEM}.mp3")
        procs.append(p)
        return p

    monkeypatch.setattr(bgm_engine.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    t = mgr.get(tid)
    assert t.result["music"] == "battle.mp3" and t.result["duration"] == 100.0
    out = sandbox["ws"] / "08_bgm" / f"{STEM}.mp3"
    assert out.exists() and out.stat().st_size == 4096
    sl = procs[0].cmd.index("-stream_loop")
    assert procs[0].cmd[sl + 1] == "-1"  # the real command shape reached Popen
    # encoder thread bound: `-threads N` right after `-y`, a positive int
    assert procs[0].cmd[2:3] == ["-threads"] and int(procs[0].cmd[3]) >= 1
    assert concurrency.merge_gate().active == 0


def test_mix_rc_nonzero_fails_with_stderr(sandbox, monkeypatch):
    _seed_mix_inputs(sandbox)
    monkeypatch.setattr(bgm_engine.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(cmd, core_paths.get_or_prepare_layout().bgm / f"{STEM}.mp3", rc=1))
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    err = mgr.get(tid).error
    assert "退出码 1" in err and "fake stderr tail" in err
    assert concurrency.merge_gate().active == 0


def test_mix_output_too_small_fails(sandbox, monkeypatch):
    _seed_mix_inputs(sandbox)
    monkeypatch.setattr(bgm_engine.subprocess, "Popen",
                        lambda cmd, **kw: _FakeProc(cmd, core_paths.get_or_prepare_layout().bgm / f"{STEM}.mp3", out_size=100))
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "输出异常" in mgr.get(tid).error


def test_mix_cancel_kills_process(sandbox, monkeypatch):
    _seed_mix_inputs(sandbox)
    started = threading.Event()

    class SlowProc(_FakeProc):
        def poll(self):
            if self.killed:
                return 1
            started.set()
            return None  # still running

    def fake_popen(cmd, **kw):
        p = SlowProc(cmd, core_paths.get_or_prepare_layout().bgm / f"{STEM}.mp3")
        return p

    monkeypatch.setattr(bgm_engine.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    g = concurrency.merge_gate()
    held_slots = g.limit
    for _ in range(held_slots):
        g.acquire()  # fill the CPU-sized gate so the task parks in the cooperative wait
    try:
        cfg = core_config.get_config()
        mgr = sandbox["mgr"]
        tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                         bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
        _wait_until(lambda: mgr.get(tid).status is TaskStatus.RUNNING, timeout=3)
        mgr.control(tid, "cancel")
        _wait_until(lambda: mgr.get(tid).status is TaskStatus.CANCELLED, timeout=3)
        assert g.active == held_slots  # queued worker took no slot
        assert not (sandbox["ws"] / "08_bgm" / f"{STEM}.mp3").exists()
    finally:
        for _ in range(held_slots):
            g.release()


def test_mix_stderr_pumped_while_running(sandbox, monkeypatch):
    """Regression (2026-09 时间轴混音卡死): a timeline mix's ffmpeg stderr
    (banner + per-input metadata × K+1 inputs) exceeds the OS pipe capacity
    (4KB on Windows, 64KB on Linux) WHILE the process is still running. The
    engine must DRAIN stderr concurrently — otherwise ffmpeg blocks on its
    stderr write before it ever opens the output file, ``proc.poll()`` never
    returns and the task hangs at 5% forever.

    The fake models exactly that block on a REAL pipe: its writer thread only
    finishes (and ``poll`` returns the rc) once >64KB of stderr has been fully
    consumed. With the pump, the pipe drains and the mix succeeds; without it,
    the writer stays blocked and the task never reaches a terminal state
    (``_wait_terminal`` asserts the timeout → the old code fails this test)."""
    _seed_mix_inputs(sandbox)
    out = core_paths.get_or_prepare_layout().bgm / f"{STEM}.mp3"
    rd_fd, wr_fd = os.pipe()
    stderr_file = os.fdopen(rd_fd, "rb")
    payload = b"ffmpeg stderr line \n" * 5000  # 80KB > 64KB / > 4KB
    done = threading.Event()
    killed = threading.Event()

    class LiveProc(_FakeProc):
        def __init__(self, cmd, out_path: Path):
            super().__init__(cmd, out_path)
            self.stderr = stderr_file  # real pipe, not the BytesIO

        def poll(self):
            if killed.is_set() or done.is_set():
                return self._rc
            return None  # "still running" while the writer sits on the pipe

        def kill(self):
            super().kill()
            killed.set()

        def wait(self, timeout=None):
            killed.set()
            return self._rc

    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]

    def writer() -> None:
        try:
            view = memoryview(payload)
            for off in range(0, len(payload), 8192):
                if killed.is_set():
                    break
                _write_all(wr_fd, view[off:off + 8192])
                time.sleep(0.05)
            done.set()
        finally:
            try:
                os.close(wr_fd)
            except OSError:
                pass

    threading.Thread(target=writer, daemon=True).start()
    monkeypatch.setattr(bgm_engine.subprocess, "Popen",
                        lambda cmd, **kw: LiveProc(cmd, out))
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid, timeout=10) == "succeeded"
    assert out.exists() and out.stat().st_size == 4096
    assert concurrency.merge_gate().active == 0


def test_mix_narration_missing(sandbox):
    layout = core_paths.get_or_prepare_layout()
    data = bgm_engine.load_assignments(layout)
    data["chapters"][STEM] = {"tags": {}, "music": "battle.mp3", "locked": False,
                              "manual": False, "score": 3, "reason": "r", "matched_at": "t"}
    bgm_engine.save_assignments(layout, data)
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "未找到旁白音频" in mgr.get(tid).error


def test_mix_music_missing(sandbox, monkeypatch):
    _seed_mix_inputs(sandbox, music="ghost.mp3")  # not in the library
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "音乐库中找不到 ghost.mp3" in mgr.get(tid).error
    assert not (sandbox["ws"] / "08_bgm" / f"{STEM}.mp3").exists()


def test_mix_music_none_copies_narration_verbatim(sandbox):
    _seed_mix_inputs(sandbox, music=None, narration_bytes=b"VERBATIM-BYTES-123")
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    out = sandbox["ws"] / "08_bgm" / f"{STEM}.mp3"
    assert out.read_bytes() == b"VERBATIM-BYTES-123"  # 字节一致
    assert mgr.get(tid).result["music"] is None


def test_mix_never_matched_fails(sandbox):
    (sandbox["ws"] / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    (sandbox["ws"] / "06_audio_merge" / f"{STEM}.mp3").write_bytes(b"NARR")
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "从未匹配" in mgr.get(tid).error


# --------------------------------------------------------------------------- #
# 停顿规则移植（merge 模块 = 后端唯一事实源，worker 1:1 镜像）
# --------------------------------------------------------------------------- #

def test_pause_port_matches_worker_semantics():
    # normalize_pause_ms：None→None / 数值直传（负数钳 0）/ 非数字→None
    assert merge_engine.normalize_pause_ms(None) is None
    assert merge_engine.normalize_pause_ms(300) == 300
    assert merge_engine.normalize_pause_ms("300") == 300
    assert merge_engine.normalize_pause_ms(-5) == 0
    assert merge_engine.normalize_pause_ms("fast") is None
    assert merge_engine.normalize_pause_ms({}) is None
    # boundary_gap_ms：override（含显式 0）> 同人同音 > 换人
    assert merge_engine.boundary_gap_ms(0, "A", "B", 500, 250) == 0  # 显式 0 也是 override
    assert merge_engine.boundary_gap_ms(700, "A", "A", 500, 250) == 700  # override 同人亦胜
    assert merge_engine.boundary_gap_ms(None, "A", "A", 500, 250) == 250
    assert merge_engine.boundary_gap_ms(None, "A", "B", 500, 250) == 500
    assert merge_engine.boundary_gap_ms("fast", "A", "B", 500, 250) == 500  # 非数字降级默认路径


# --------------------------------------------------------------------------- #
# segment_fingerprint（陈旧度锚点：条目数 + 文本，与 speaker 无关）
# --------------------------------------------------------------------------- #

def test_segment_fingerprint_text_only_and_count_sensitive():
    e1 = [{"speaker": "NARRATOR", "text": "alpha"}, {"speaker": "老道", "text": "beta"}]
    e2 = [{"speaker": "杜尘", "text": "alpha"}, {"speaker": "史蒂夫", "text": "beta"}]
    assert bgm_engine.segment_fingerprint(e1) == bgm_engine.segment_fingerprint(e2)  # 与 speaker 无关
    assert bgm_engine.segment_fingerprint(e1) != bgm_engine.segment_fingerprint(e1[:1])  # 计数敏感
    assert bgm_engine.segment_fingerprint([{"speaker": "A", "text": "x"}]) \
        == bgm_engine.segment_fingerprint([{"speaker": "B", "text": "x"}])
    assert bgm_engine.segment_fingerprint([{"text": "x"}]) \
        != bgm_engine.segment_fingerprint([{"text": "y"}])
    # 非 dict 条目按 "" 参与（与空 text 条目同口径）
    assert bgm_engine.segment_fingerprint([{"text": ""}]) == bgm_engine.segment_fingerprint(["junk"])
    # v2 盐：与无盐 payload 的哈希不同（逐条 music_action → 场景块的一次性迁移锚点）
    import hashlib
    unsalted = hashlib.sha256(b"2\nalpha\nbeta").hexdigest()
    assert bgm_engine.segment_fingerprint(e1) != unsalted


# --------------------------------------------------------------------------- #
# parse_segment_blocks_reply（一批 LLM 回复 → 场景块列表；空隙 = 有意静音）
# --------------------------------------------------------------------------- #

def _block_reply(items) -> str:
    return json.dumps(items, ensure_ascii=False)


def _tags(scene=None, mood=None, emotion=None, custom=None) -> dict:
    return {"scene": list(scene or []), "mood": list(mood or []),
            "emotion": list(emotion or []), "custom": list(custom or [])}


def _blk(start, end, **kw):
    """一个场景块 JSON（缺省：空标签 / 强度 2 / 空描述）。"""
    item = {"start_segment": start, "end_segment": end,
            "scene": "", "mood": "", "music_tags": _tags(),
            "intensity": 2, "reason": ""}
    item.update(kw)
    return item


def test_parse_segment_blocks_reply_valid():
    """双批场景：批 1（0~9）两个新块 + 批 2（10~14）extend 首块 + 1 新块。"""
    r1 = _block_reply([
        _blk(0, 5, scene="森林战斗", mood="紧张",
             music_tags=_tags(scene=["战斗"], mood=["紧张", "热血"]),
             intensity=3, reason="战斗开始"),
        _blk(6, 9, music_tags=_tags(mood=["压抑"])),
    ])
    got = bgm_engine.parse_segment_blocks_reply(r1, list(range(10)), None)
    assert got is not None and len(got) == 2
    assert got[0] == {"start": 0, "end": 5, "scene": "森林战斗", "mood": "紧张",
                      "music_tags": _tags(scene=["战斗"], mood=["紧张", "热血"]),
                      "intensity": 3, "reason": "战斗开始"}
    assert got[1] == {"start": 6, "end": 9, "scene": "", "mood": "",
                      "music_tags": _tags(mood=["压抑"]), "intensity": 2, "reason": ""}
    # 批 2：首块 extend（延续批 1 末尾开放场景 6~9 → 止于 9）+ 一个新块
    prev = {"start": 6, "end": 9, "scene": "森林战斗", "mood": "紧张",
            "music_tags": _tags(mood=["压抑"]), "intensity": 2, "reason": ""}
    r2 = _block_reply([
        {"start_segment": 10, "end_segment": 12, "extend": True,
         "scene": "应被丢弃", "intensity": 9},
        _blk(13, 14, scene="转场", music_tags=_tags(scene=["营地"])),
    ])
    got2 = bgm_engine.parse_segment_blocks_reply(r2, list(range(10, 15)), prev)
    assert got2 == [
        {"start": 10, "end": 12, "extend": True},  # 其余字段一律丢弃
        {"start": 13, "end": 14, "scene": "转场", "mood": "",
         "music_tags": _tags(scene=["营地"]), "intensity": 2, "reason": ""},
    ]
    # 全静默批 = 合法空数组
    assert bgm_engine.parse_segment_blocks_reply("[]", [20, 24], prev) == []
    # 围栏数组也通过
    got3 = bgm_engine.parse_segment_blocks_reply(
        "```json\n" + _block_reply([_blk(0, 9)]) + "\n```", list(range(10)), None)
    assert got3 and got3[0]["start"] == 0 and got3[0]["end"] == 9
    # 数字串下标可解析
    got4 = bgm_engine.parse_segment_blocks_reply(
        _block_reply([{"start_segment": "3", "end_segment": "5",
                       "music_tags": _tags()}]), [3, 4, 5], None)
    assert got4 and got4[0]["start"] == 3 and got4[0]["end"] == 5


def test_parse_segment_blocks_reply_extend_rules():
    """extend 块规则：仅首位 / 需 prev_scene / start==b0 / prev_scene 止于本批之前
    （end < b0：直接邻接 end==b0-1 或隔着全静音批 end 更早均合法）/ end >= b0 拒。"""
    prev = {"end": 9}
    b1 = [10, 11, 12]
    # 缺省 prev_scene → None
    assert bgm_engine.parse_segment_blocks_reply(
        _block_reply([{"start_segment": 10, "end_segment": 11, "extend": True}]),
        b1, None) is None
    # 非首块 → None
    assert bgm_engine.parse_segment_blocks_reply(
        _block_reply([_blk(10, 10),
                      {"start_segment": 11, "end_segment": 12, "extend": True}]),
        b1, prev) is None
    # start != b0 → None
    assert bgm_engine.parse_segment_blocks_reply(
        _block_reply([{"start_segment": 11, "end_segment": 12, "extend": True}]),
        b1, prev) is None
    # prev_scene 止于本批之前但隔全静音批（止于 8 < b0-1=9）→ 合法（静音批不
    # 封闭场景）→ 字段丢弃，只留 start/end/extend
    got_gap = bgm_engine.parse_segment_blocks_reply(
        _block_reply([{"start_segment": 10, "end_segment": 11, "extend": True,
                       "scene": "丢", "music_tags": _tags()}]),
        b1, {"end": 8})
    assert got_gap == [{"start": 10, "end": 11, "extend": True}]
    # prev_scene 未止于本批之前（end >= b0 = 状态错乱）→ None
    assert bgm_engine.parse_segment_blocks_reply(
        _block_reply([{"start_segment": 10, "end_segment": 11, "extend": True}]),
        b1, {"end": 10}) is None
    # prev_scene 缺 end 键（防御）→ None
    assert bgm_engine.parse_segment_blocks_reply(
        _block_reply([{"start_segment": 10, "end_segment": 11, "extend": True}]),
        b1, {}) is None
    # 合法 → 字段丢弃，只留 start/end/extend
    got = bgm_engine.parse_segment_blocks_reply(
        _block_reply([{"start_segment": 10, "end_segment": 11, "extend": True,
                       "scene": "丢", "music_tags": _tags(), "intensity": 3,
                       "reason": "丢"}]),
        b1, prev)
    assert got == [{"start": 10, "end": 11, "extend": True}]
    # extend 后接新块（起点 > extend 终点）合法
    got2 = bgm_engine.parse_segment_blocks_reply(
        _block_reply([{"start_segment": 10, "end_segment": 11, "extend": True},
                      _blk(12, 12)]),
        b1, prev)
    assert got2 and len(got2) == 2


def test_parse_segment_blocks_reply_rejection_matrix():
    b1 = [0, 9]
    good = _block_reply([_blk(0, 4), _blk(5, 9)])
    assert bgm_engine.parse_segment_blocks_reply(good, b1, None) is not None
    # 注：旧矩阵的「end 越上界」（_blk(0, 10)，b1=[0,9]）已翻转为**钳制**
    # 断言——模型写出场景真实终点（场景延续到下一批）不再拒收，钳到本批末条、
    # 由下一批 extend 接住（2026-09 实锤：章末批次写出章末下标 → 3 次同形
    # 拒收 → 稳定失败；见 test_parse_segment_blocks_end_past_batch_clamped）。
    bad_replies = [
        ("不是 JSON", "这不是 JSON"),
        ("非数组（对象）", '{"scene": []}'),
        ("非对象元素", _block_reply([_blk(0, 4), "x"])),
        ("浮点 start", _block_reply([_blk(0.5, 4)])),
        ("bool start", _block_reply([_blk(True, 4)])),
        ("start 越下界", _block_reply([_blk(-1, 4)])),
        ("start > end", _block_reply([_blk(4, 3)])),
        ("重叠（第 2 块起点 ≤ 第 1 块终点）", _block_reply([_blk(0, 4), _blk(4, 9)])),
        ("缺 start_segment", _block_reply([{"end_segment": 4, "music_tags": _tags()}])),
        ("缺 music_tags", _block_reply([{"start_segment": 0, "end_segment": 4}])),
        ("music_tags 非 dict", _block_reply([_blk(0, 4, music_tags=["战斗"])])),
        ("截断 JSON", _block_reply([_blk(0, 4), _blk(5, 9)])[:-1] + "  "),
    ]
    for name, reply in bad_replies:
        assert bgm_engine.parse_segment_blocks_reply(reply, b1, None) is None, name
    # 标签规范化：非字符串丢弃 / strip / 去重 / 四桶上限 2/3/2/2
    got = bgm_engine.parse_segment_blocks_reply(_block_reply([_blk(
        0, 9, music_tags={"scene": ["a", "a", "b", "c", 7],
                          "mood": ["x", "y", "z", "w"],
                          "emotion": ["e", " f "], "custom": []})]),
        b1, None)
    assert got[0]["music_tags"] == _tags(scene=["a", "b"], mood=["x", "y", "z"],
                                         emotion=["e", "f"])
    # scene/mood/reason 非 str → 空串
    got2 = bgm_engine.parse_segment_blocks_reply(_block_reply([_blk(
        0, 9, scene=7, mood=None, reason=["x"])]), b1, None)
    assert got2[0]["scene"] == "" and got2[0]["mood"] == "" and got2[0]["reason"] == ""


def test_parse_segment_blocks_end_past_batch_clamped():
    """end 越过本批上界 = 模型写出了场景的真实终点（场景延续到下一批）→
    钳制到本批末条而非拒收（语义修复，非数据丢失）：开放场景由下一批的
    extend 块接住（标准协议）。2026-09 实锤：第 315 章批 3（40~59）的
    死牢块 end_segment=67（章末）→ 旧硬拒收 → 3 次同形拒收 → 稳定失败。
    start 越下界不钳制（会吞上一批条目 = 静默丢数据）→ 带 extend 提示拒收。"""
    b1 = [0, 9]
    blocks, note = bgm_engine.parse_segment_blocks_reply_with_note(
        _block_reply([_blk(0, 10)]), b1, None)
    assert blocks == [{"start": 0, "end": 9, "scene": "", "mood": "",
                       "music_tags": _tags(), "intensity": 2, "reason": ""}]
    assert "end_segment=10" in note and "已钳制到 9" in note
    # 公开包装保持旧契约（blocks / None，note 忽略）
    assert bgm_engine.parse_segment_blocks_reply(
        _block_reply([_blk(0, 10)]), b1, None) == blocks
    # 无钳制 → note 为空串
    blocks2, note2 = bgm_engine.parse_segment_blocks_reply_with_note(
        _block_reply([_blk(0, 9)]), b1, None)
    assert blocks2 and note2 == ""
    # start 越下界 → 拒收（钳制 = 吞上一批条目）+ extend 提示
    rejected, reason = bgm_engine.parse_segment_blocks_reply_with_note(
        _block_reply([_blk(-1, 4)]), b1, None)
    assert rejected is None
    assert "start_segment=-1" in reason and "extend" in reason
    # extend 块 end 越上界同样钳制（延续到本批末条）
    got3, note3 = bgm_engine.parse_segment_blocks_reply_with_note(
        _block_reply([{"start_segment": 0, "end_segment": 99, "extend": True}]),
        b1, {"end": -1})
    assert got3 == [{"start": 0, "end": 9, "extend": True}]
    assert "已钳制到 9" in note3
    # 整块在本批之外（start 越过本批上界）→ 拒收——钳制起点会吞掉下一批的
    # 条目（且钳到 b1 后任何后续块都 start > b1，多钳制 note 拼接收不可达）
    rejected4, reason4 = bgm_engine.parse_segment_blocks_reply_with_note(
        _block_reply([_blk(0, 9), _blk(10, 15)]), b1, None)
    assert rejected4 is None
    assert "start_segment=10 超出本批上界 9" in reason4
    # 钳制后的块仍须满足升序不重叠（钳到 9 后又与下一块重叠 → 拒收）
    rejected5, reason5 = bgm_engine.parse_segment_blocks_reply_with_note(
        _block_reply([_blk(0, 99), _blk(9, 9)]), b1, None)
    assert rejected5 is None and "重叠" in reason5


def test_parse_segment_blocks_rejection_reasons():
    """每条拒收路径都返回具体、模型可执行的中文原因（经 ParseRejected 进
    last_err +【重试】反馈——确定性模型可自我纠正而非同形重掷）。"""
    b1 = [0, 9]
    cases = [
        ("这不是 JSON", "JSON 数组"),
        ('{"scene": []}', "JSON 数组"),
        (_block_reply([_blk(0, 4), "x"]), "不是 JSON 对象"),
        (_block_reply([_blk(0.5, 4)]), "start_segment=0.5"),
        (_block_reply([{"end_segment": 4, "music_tags": _tags()}]),
         "start_segment=None"),
        (_block_reply([_blk(4, 3)]), "start_segment=4 > end_segment=3"),
        (_block_reply([_blk(10, 15)]), "超出本批上界"),
        (_block_reply([_blk(0, 4), _blk(4, 9)]), "重叠"),
        (_block_reply([{"start_segment": 0, "end_segment": 4}]), "music_tags"),
        (_block_reply([_blk(0, 4, music_tags=["战斗"])]), "music_tags"),
    ]
    for reply, kw in cases:
        rejected, reason = bgm_engine.parse_segment_blocks_reply_with_note(
            reply, b1, None)
        assert rejected is None
        assert kw in reason, (kw, reason)
    # extend 违规各带不同的具体原因
    prev = {"end": 9}
    b2 = [10, 11, 12]
    ext = _block_reply([{"start_segment": 10, "end_segment": 12, "extend": True}])
    _, r1 = bgm_engine.parse_segment_blocks_reply_with_note(ext, b2, None)
    assert "没有可延续的" in r1
    _, r2 = bgm_engine.parse_segment_blocks_reply_with_note(
        _block_reply([_blk(10, 10),
                      {"start_segment": 11, "end_segment": 12, "extend": True}]),
        b2, prev)
    assert "首位" in r2
    _, r3 = bgm_engine.parse_segment_blocks_reply_with_note(
        _block_reply([{"start_segment": 11, "end_segment": 12, "extend": True}]),
        b2, prev)
    assert "须等于本批首条 10" in r3
    _, r4 = bgm_engine.parse_segment_blocks_reply_with_note(ext, b2, {"end": 10})
    assert "状态错乱" in r4


def test_parse_segment_blocks_reply_intensity_defaults():
    def intensity(item) -> int:
        got = bgm_engine.parse_segment_blocks_reply(_block_reply([item]), [0, 9], None)
        return got[0]["intensity"]
    assert intensity(_blk(0, 9)) == 2                      # 缺失 → 2
    assert intensity(_blk(0, 9, intensity=0)) == 1         # 钳 1..3
    assert intensity(_blk(0, 9, intensity=9)) == 3
    assert intensity(_blk(0, 9, intensity="3")) == 3       # 数字串可用
    assert intensity(_blk(0, 9, intensity=True)) == 2      # bool 拒绝 → 2
    assert intensity(_blk(0, 9, intensity="loud")) == 2    # 非数 → 2


# --------------------------------------------------------------------------- #
# intensity_volume（intensity 1..3 → bgm.volume × 档位，clamp ≤ 2.0）
# --------------------------------------------------------------------------- #

def test_intensity_volume_default_tiers_and_clamp():
    assert bgm_engine.intensity_volume(1, 0.18) == 0.09
    assert bgm_engine.intensity_volume(2, 0.18) == 0.18
    assert bgm_engine.intensity_volume(3, 0.18) == 0.27
    assert bgm_engine.intensity_volume(1, 0.18, [0.2, 1.0, 2.0]) == 0.036  # 自定义档位
    assert bgm_engine.intensity_volume(3, 0.18, [0.2, 1.0, 2.0]) == 0.36
    for v in (0, 4, None, "loud"):  # 越界 / 缺失 / 非数字 → 档 2
        assert bgm_engine.intensity_volume(v, 0.18) == 0.18
    assert bgm_engine.intensity_volume("3", 0.18) == 0.27  # 字符串数字可用
    assert bgm_engine.intensity_volume(3, 2.0) == 2.0  # 3.0 → 钳 2.0
    assert bgm_engine.intensity_volume(3, 0.18, [1.0]) == 0.27  # 档位非 3 元 → 回默认
    assert bgm_engine.intensity_volume(2, 0.18, [0.5, "x", 1.5]) == 0.18  # 档位元素坏 → 1.0


# --------------------------------------------------------------------------- #
# build_timeline_mix_cmd（时间轴混音命令，逐元素钉死）
# --------------------------------------------------------------------------- #

def test_timeline_mix_command_contract():
    # build timeline mix cmd elementwise
    cfg = _cfg()  # fade_in 1.5 / fade_out 3.0 / loop True
    spans = [
        {"start": 0.0, "end": 2.0, "music_id": "battle.mp3", "volume": 0.09},
        {"start": 10.0, "end": 30.0, "music_id": "calm.mp3", "volume": 0.27},
    ]
    narration = Path("/ws/06/narr.mp3")
    out = Path("/ws/08/out.mp3")
    lib = Path("/lib")
    cmd = bgm_engine.build_timeline_mix_cmd(
        "ffmpeg", narration, out, spans, lib, 100.0, cfg)
    # 输入序：ffmpeg -y -i 旁白，每 span 一个 -stream_loop -1 -i 曲目
    #（路径经 str()——Windows 下为反斜杠，引擎逐字拼入命令）
    assert cmd[0:5] == ["ffmpeg", "-y", "-i", str(narration), "-stream_loop"]
    assert cmd[5:8] == ["-1", "-i", str(lib / "battle.mp3")]
    assert cmd[8:12] == ["-stream_loop", "-1", "-i", str(lib / "calm.mp3")]
    fc = cmd[cmd.index("-filter_complex") + 1]
    # span 0：长 2s → 双侧 fade 钳 min(fade, 1.0)；adelay=0ms
    assert fc.split(";")[1] == (
        "[1:a]aresample=44100,atrim=0:2.000,afade=t=in:d=1.000,"
        "afade=t=out:st=1.000:d=1.000,volume=0.045,adelay=0:all=1[m0]")
    # span 1：长 20s → fade 原值；adelay=10000ms
    assert fc.split(";")[2] == (
        "[2:a]aresample=44100,atrim=0:20.000,afade=t=in:d=1.500,"
        "afade=t=out:st=17.000:d=3.000,volume=0.135,adelay=10000:all=1[m1]")
    # 已保存的时间轴音量只在混音时减半，重复混音不会修改或累积衰减。
    assert [span["volume"] for span in spans] == [0.09, 0.27]
    # 首尾：旁白 resample + amix inputs=K+1 normalize=0（1/N 归一化必须关）
    assert fc.split(";")[0] == "[0:a]aresample=44100[nar]"
    assert fc.endswith("[nar][m0][m1]amix=inputs=3:duration=first:"
                       "dropout_transition=0:normalize=0[out]")
    assert cmd[-5:] == ["-map", "[out]", "-c:a", "libmp3lame", str(out)]
    assert "-threads" not in cmd  # threads 缺省 = 省略
    # threads=2 → -threads 2 紧跟 -y
    cmd2 = bgm_engine.build_timeline_mix_cmd(
        "ffmpeg", narration, out, spans, lib, 100.0, cfg, threads=2)
    assert cmd2[0:5] == ["ffmpeg", "-y", "-threads", "2", "-i"]
    assert cmd2[cmd2.index("-filter_complex") + 1] == fc
    # loop=False → -stream_loop -0
    cmd3 = bgm_engine.build_timeline_mix_cmd(
        "ffmpeg", narration, out, spans, lib, 100.0, _cfg(loop=False))
    assert all(x == "-0" for x in cmd3 if x in ("-1", "-0"))
    assert cmd3.count("-stream_loop") == 2

    # build timeline mix cmd zero volume span
    # volume 缺失/0 → 0.0（:g → "0"）；start 缺失 → 0
    cmd = bgm_engine.build_timeline_mix_cmd(
        "ffmpeg", Path("/n.mp3"), Path("/o.mp3"),
        [{"start": 0.0, "end": 5.0, "music_id": "a.mp3"}], Path("/lib"), 5.0, _cfg())
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "volume=0," in fc and "adelay=0:all=1" in fc


# --------------------------------------------------------------------------- #
# recompute_segment_timelines（纯、零 LLM：游标走查 / 分段 / 机械匹配 / 合并 / 落盘）
# --------------------------------------------------------------------------- #

def _segment_block(start: int, end: int, intensity: int = 2,
                   scene_desc: str = "", mood_desc: str = "", reason: str = "",
                   **tags) -> dict:
    """一个缓存的场景块（新场景形态：四桶标签 + 强度 + 描述字段）。
    描述字段用 scene_desc/mood_desc/reason 命名，标签桶经 **tags 传（scene/mood/…）。"""
    return {
        "start": start, "end": end,
        "scene": scene_desc, "mood": mood_desc, "reason": reason,
        "music_tags": {c: list(tags.get(c) or []) for c in music_engine.TAG_CATEGORIES},
        "intensity": intensity,
    }


def _seed_segment_inputs(sandbox, stem=STEM, n=6, speakers=None,
                         pause_after=None, with_05=True, with_06=True) -> list[dict]:
    """03 脚本（n 条）+ 05 清单（工作空间相对 path + 假段 mp3）+ 06 旁白。
    返回 03 条目列表（指纹播种 / 文本断言用）。"""
    ws = sandbox["ws"]
    speakers = list(speakers) if speakers else ["老道"] * n
    entries = [{"speaker": speakers[i % len(speakers)],
                "text": f"段落{i}的文本。"} for i in range(n)]
    if pause_after is not None:
        for i, pa in enumerate(pause_after):
            entries[i]["pause_after"] = pa
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    (ws / "03_parsed_json" / f"{stem}.json").write_bytes(
        json.dumps(entries, ensure_ascii=False).encode("utf-8"))
    if with_05:
        package = tts_batch.package_for(Path(f"{stem}.json"))
        pkg_dir = ws / "05_audio_chunk" / package
        pkg_dir.mkdir(parents=True, exist_ok=True)
        manifest = []
        for i, e in enumerate(entries):
            fname = f"{i + 1:04d}.mp3"
            (pkg_dir / fname).write_bytes(b"SEG" * 100)
            pa = pause_after[i] if pause_after is not None else None
            manifest.append({"index": i, "speaker": e["speaker"],
                             "text": e["text"], "pause_after": pa,
                             "path": f"05_audio_chunk/{package}/{fname}",
                             "ok": True, "reason": ""})
        (pkg_dir / "manifest.json").write_bytes(
            json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    if with_06:
        (ws / "06_audio_merge").mkdir(parents=True, exist_ok=True)
        (ws / "06_audio_merge" / f"{stem}.mp3").write_bytes(b"NARR" * 256)
    return entries


def _seed_segment_analysis(sandbox, stem, entries, blocks,
                           fingerprint=None, model="test-model"):
    layout = core_paths.get_or_prepare_layout()
    data = bgm_engine.load_segment_analysis(layout)
    data["chapters"][stem] = {
        "fingerprint": fingerprint or bgm_engine.segment_fingerprint(entries),
        "entry_count": len(entries),
        "blocks": blocks,
        "model": model,
        "analyzed_at": "t0",
        "edited": False,
    }
    bgm_engine.save_segment_analysis(layout, data)


def _fake_probe(segs_dur: dict, total: float):
    """probe_duration 替身：05 段文件（000X.mp3）按 segs_dur（键 = 段 index），
    其余路径（06 旁白）返回 total。"""
    def probe(path, ffprobe=""):
        m = re.fullmatch(r"(\d{4})\.mp3", Path(path).name)
        if m:
            return segs_dur.get(int(m.group(1)) - 1, float("nan")), None
        return total, None
    return probe


def _write_timeline(sandbox, stem, spans, duration=14.25, source_duration=None):
    layout = core_paths.get_or_prepare_layout()
    tl = {"version": 2, "stem": stem, "generated_at": "t0", "model": "test-model",
          "fingerprint": "fp", "entry_count": 6,
          "duration": duration, "timeline": spans}
    if source_duration is not None:
        entries = json.loads(
            (sandbox["ws"] / "03_parsed_json" / f"{stem}.json").read_text("utf-8")
        )
        manifest = tts_batch.load_manifest(
            layout.audio_chunk / tts_batch.package_for(Path(f"{stem}.json"))
        )
        segs, _missing = merge_engine.collect_segments(
            list(manifest.values()), layout.workspace
        )
        tl["source"] = bgm_engine._segment_source_snapshot(
            entries, manifest, segs,
            {s["index"]: source_duration for s in segs},
            layout.audio_merge / f"{stem}.mp3",
            500, 250,
        )
    bgm_engine._atomic_write_json(bgm_engine._timeline_path(layout, stem), tl)
    return tl


def _seed_segment_assignment(sandbox, stem, locked=False, music=None):
    layout = core_paths.get_or_prepare_layout()
    data = bgm_engine.load_assignments(layout)
    data["chapters"][stem] = {
        "tags": {c: [] for c in music_engine.TAG_CATEGORIES},
        "music": music, "segment": True, "locked": locked, "manual": False,
        "score": None, "reason": "r", "matched_at": "t0",
    }
    bgm_engine.save_assignments(layout, data)


def _assignments_before(sandbox):
    layout = core_paths.get_or_prepare_layout()
    p = layout.bgm / bgm_engine.ASSIGNMENTS_NAME
    return p.read_bytes() if p.is_file() else None


def _assert_zero_drop(sandbox, stem, before):
    layout = core_paths.get_or_prepare_layout()
    assert not bgm_engine._timeline_path(layout, stem).exists()  # 无时间轴文件
    p = layout.bgm / bgm_engine.ASSIGNMENTS_NAME
    assert (p.read_bytes() if p.is_file() else None) == before  # assignments 字节不变


def test_recompute_cursor_walk_and_scene_spans(sandbox, monkeypatch):
    """游标走查（override 750 / 同人 250 / 换人 500）+ 场景块 → 时间 span
    （span 末 = 块末条音频结束，边界停顿归入间隙）+ 机械匹配（battle 标签 /
    空标签 → 通用池）+ v2 描述字段透传 + 无相邻去重放宽注记。"""
    speakers = ["老道", "杜尘", "杜尘", "老道", "老道", "杜尘"]
    # override 落在 seg2（2→3 号段间隙，杜尘→老道）：显式值胜同人/换人默认
    pause_after = [None, None, 750, None, None, None]
    entries = _seed_segment_inputs(sandbox, n=6, speakers=speakers,
                                   pause_after=pause_after)
    blocks = [
        _segment_block(0, 1, intensity=2, scene_desc="战场对峙", mood_desc="紧张",
                       reason="开场战斗", scene=["战斗"], mood=["紧张", "热血"]),
        _segment_block(2, 4, intensity=2),  # 空标签 → 通用池
        _segment_block(5, 5, intensity=3, scene=["战斗"], mood=["紧张", "热血"]),
    ]
    _seed_segment_analysis(sandbox, STEM, entries, blocks)
    # 游标：0.0 / 30.5（换人 500）/ 60.75（同人 250）/ 91.5（override 750）/
    #       121.75（同人 250）/ 152.25（换人 500）；6 段 × 30s + 2.25 间隙
    #       = 总时长 182.25
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe({i: 30.0 for i in range(6)}, 182.25))
    layout = core_paths.get_or_prepare_layout()
    res = bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    assert res == {"mode": "segment", "matched": 1, "no_bgm": 0, "skipped_locked": 0}
    tl = bgm_engine.load_timeline(layout, STEM)
    assert tl["duration"] == 182.25 and tl["entry_count"] == 6
    assert tl["version"] == 2
    # span 末 = 块末条音频结束（30.5+30 / 121.75+30 / 152.25+30），不是下一块起点
    assert tl["timeline"] == [
        {"start": 0.0, "end": 60.5, "music_id": "battle.mp3", "intensity": 2,
         "volume": 0.18, "tags": {"scene": ["战斗"], "mood": ["紧张", "热血"],
                                  "emotion": [], "custom": []},
         "score": 8,
         "reason": "mood 命中 紧张, 热血(+6)；scene 命中 战斗(+2)",
         "scene_desc": "战场对峙", "mood_desc": "紧张", "switch_reason": "开场战斗"},
        {"start": 60.75, "end": 151.75, "music_id": "calm.mp3", "intensity": 2,
         "volume": 0.18, "tags": {c: [] for c in music_engine.TAG_CATEGORIES},
         "score": 0, "reason": "无标签命中，使用通用音乐",
         "scene_desc": "", "mood_desc": "", "switch_reason": ""},
        {"start": 152.25, "end": 182.25, "music_id": "battle.mp3", "intensity": 3,
         "volume": 0.27, "tags": {"scene": ["战斗"], "mood": ["紧张", "热血"],
                                  "emotion": [], "custom": []},
         "score": 8,
         "reason": "mood 命中 紧张, 热血(+6)；scene 命中 战斗(+2)",
         "scene_desc": "", "mood_desc": "", "switch_reason": ""},
    ]
    # 相邻去重已移除 → 全部 reason 无放宽注记
    assert not any("相邻章去重放宽" in sp["reason"] for sp in tl["timeline"])
    # assignment 一次事务：mode=segment + segment 标记 + 场景标签并集
    asg = bgm_engine.load_assignments(layout)
    assert asg["mode"] == "segment"
    e = asg["chapters"][STEM]
    assert e["segment"] is True and e["music"] is None and e["locked"] is False
    assert e["reason"] == "段落级时间轴（3 段 BGM）" and e["matched_at"]
    assert e["tags"]["scene"] == ["战斗"] and e["tags"]["mood"] == ["紧张", "热血"]


def test_recompute_trailing_and_leading_gap(sandbox, monkeypatch):
    """span 末 = 块末条音频结束（尾部空隙 = 有意静音，不收口到总时长）；
    首部空隙 = 前导静音；06 总时长 < 游标末端 → span 末 clamp 到 total。"""
    entries = _seed_segment_inputs(sandbox, n=3, speakers=["老道"] * 3)
    # 3 段同人 × 30s + 两条同人间隙 0.25s = 总时长 90.5
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe(
        {0: 30.0, 1: 30.0, 2: 30.0}, 90.5))
    layout = core_paths.get_or_prepare_layout()
    # ① 尾部空隙：块只到条目 1 → span 末 = starts[1]+30 = 60.25（≠ total 90.5）
    _seed_segment_analysis(sandbox, STEM, entries,
                           [_segment_block(0, 1, intensity=3, scene=["战斗"])])
    res = bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    assert res["matched"] == 1
    sp = bgm_engine.load_timeline(layout, STEM)["timeline"][0]
    assert sp["start"] == 0.0 and sp["end"] == 60.25  # 条目 2 的音频是静音
    assert sp["intensity"] == 3 and sp["volume"] == 0.27
    # ② 漂移子例：同一块、06 总时长 50.0 < 游标末端 60.5 → span 末 clamp 到 total
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe(
        {0: 30.0, 1: 30.0, 2: 30.0}, 50.0))
    bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    sp2 = bgm_engine.load_timeline(layout, STEM)["timeline"][0]
    assert sp2["start"] == 0.0 and sp2["end"] == 50.0
    # ③ 首部空隙：块从条目 1 起 → 前导静音，span 起点 = starts[1] = 30.25
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe(
        {0: 30.0, 1: 30.0, 2: 30.0}, 90.5))
    _seed_segment_analysis(sandbox, STEM, entries, [_segment_block(1, 2)])
    bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    assert bgm_engine.load_timeline(layout, STEM)["timeline"] == [
        {"start": 30.25, "end": 90.5, "music_id": "calm.mp3", "intensity": 2,
         "volume": 0.18, "tags": {c: [] for c in music_engine.TAG_CATEGORIES},
         "score": 0, "reason": "无标签命中，使用通用音乐",
         "scene_desc": "", "mood_desc": "", "switch_reason": ""},
    ]


def test_recompute_merges_adjacent_same_track(sandbox, monkeypatch):
    """相邻同曲目场景合并为一个 span（相同标签短路复用 pick、零 rng 消耗）：
    音量取最大强度、tags 并集、reason 加（合并 N 段）注记、无放宽注记。"""
    entries = _seed_segment_inputs(sandbox, n=4, speakers=["老道"] * 4)
    blocks = [_segment_block(0, 0, intensity=1, scene=["战斗"]),
              _segment_block(1, 1, intensity=3, scene=["战斗"]),
              _segment_block(2, 2, intensity=2, scene=["战斗"])]  # 条目 3 = 尾部静音
    _seed_segment_analysis(sandbox, STEM, entries, blocks)
    # 4 段同人 × 30s + 三条同人间隙 0.25s = 总时长 120.75
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe({i: 30.0 for i in range(4)}, 120.75))
    layout = core_paths.get_or_prepare_layout()
    res = bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    assert res["matched"] == 1
    tl = bgm_engine.load_timeline(layout, STEM)
    assert len(tl["timeline"]) == 1  # 三场景同 battle（相同标签短路）→ 一个 span
    sp = tl["timeline"][0]
    assert sp["start"] == 0.0 and sp["end"] == 90.5  # 块末条（条目 2）音频结束
    assert sp["music_id"] == "battle.mp3"
    assert sp["intensity"] == 3 and sp["volume"] == 0.27  # run 内最大强度
    assert sp["score"] == 2  # 场景标签只有 scene 战斗 → 仅 scene 命中
    assert sp["reason"] == "scene 命中 战斗(+2)（合并 3 段）"
    assert sp["tags"] == {"scene": ["战斗"], "mood": [], "emotion": [], "custom": []}
    assert "_run" not in sp and "_short" not in sp and "_e0" not in sp


def test_recompute_min_score_generic_and_no_candidate(sandbox, monkeypatch):
    """min_score 滤除标签候选 → 通用池（无相邻去重 = 无放宽注记）；
    通用池禁用 → 标签候选胜出；库全禁用 → 零 span → 全章无 BGM 文案。"""
    entries = _seed_segment_inputs(sandbox, n=4, speakers=["老道"] * 4)
    blocks = [_segment_block(0, 1, scene=["战斗"]), _segment_block(2, 3, scene=["战斗"])]
    _seed_segment_analysis(sandbox, STEM, entries, blocks)
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe({i: 30.0 for i in range(4)}, 120.75))
    layout = core_paths.get_or_prepare_layout()
    # ① min_score=99 滤掉全部标签候选（战斗 仅 2 分）→ 两场景落通用池 calm；
    #    相同标签短路 + 相邻同曲合并 → 一个 span，无放宽注记
    res = bgm_engine.recompute_segment_timelines(layout, [STEM], 99, rng=random.Random(5))
    assert res["matched"] == 1
    sp = bgm_engine.load_timeline(layout, STEM)["timeline"][0]
    assert sp["music_id"] == "calm.mp3" and sp["score"] == 0
    assert sp["reason"] == "无标签命中，使用通用音乐（合并 2 段）"
    # ② 通用池禁用 → 标签候选 battle（2 ≥ 1）胜出；两场景同选 battle → 合并
    music_engine.update_index(lambda idx: idx["tracks"].update(
        {"calm.mp3": {**idx["tracks"]["calm.mp3"], "enabled": False}}))
    res2 = bgm_engine.recompute_segment_timelines(layout, [STEM], 1, rng=random.Random(5))
    sp2 = bgm_engine.load_timeline(layout, STEM)["timeline"][0]
    assert sp2["music_id"] == "battle.mp3" and sp2["score"] == 2
    assert sp2["reason"] == "scene 命中 战斗(+2)（合并 2 段）"
    assert "相邻章去重放宽" not in sp2["reason"]
    # ③ 库全禁用 → 无候选 → 零 span → 全章无 BGM（assignment 仍写 segment 条目）
    music_engine.update_index(lambda idx: idx["tracks"].update(
        {n: {**t, "enabled": False} for n, t in idx["tracks"].items()}))
    res3 = bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    assert res3 == {"mode": "segment", "matched": 0, "no_bgm": 1, "skipped_locked": 0}
    tl3 = bgm_engine.load_timeline(layout, STEM)
    assert tl3["timeline"] == []
    asg = bgm_engine.load_assignments(layout)
    assert asg["chapters"][STEM]["reason"] == "段落级（全章无 BGM）"
    assert asg["chapters"][STEM]["segment"] is True


def test_recompute_error_paths_zero_drop(sandbox, monkeypatch):
    """任一章节错误中止整个调用、任何写之前：无时间轴文件 + assignments 字节不变。
    ⑦ 升级前的旧 entries 格式缓存（指纹仍有效）也判「已失效」。"""
    layout = core_paths.get_or_prepare_layout()
    monkeypatch.setattr(bgm_engine, "probe_duration",
                        _fake_probe({i: 30.0 for i in range(6)}, 152.25))
    recs6 = [_segment_block(0, 5)]

    # ① 段落分析缺失（未播种）
    before = _assignments_before(sandbox)
    _seed_segment_inputs(sandbox, n=6)
    try:
        bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "段落分析缺失或已失效" in str(e)
    _assert_zero_drop(sandbox, STEM, before)

    # ② 指纹失配（03 变化）
    entries = _seed_segment_inputs(sandbox, n=6)
    _seed_segment_analysis(sandbox, STEM, entries, recs6, fingerprint="deadbeef")
    before = _assignments_before(sandbox)
    try:
        bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "段落分析已失效（03 脚本变化）" in str(e)
    _assert_zero_drop(sandbox, STEM, before)

    # ③ 05 清单缺失（② 的 05 包目录仍留盘——with_05=False 只不写、不删，须清掉）
    pkg_dir = layout.audio_chunk / tts_batch.package_for(Path(f"{STEM}.json"))
    if pkg_dir.is_dir():
        for f in pkg_dir.iterdir():
            f.unlink()
        pkg_dir.rmdir()
    entries = _seed_segment_inputs(sandbox, n=6, with_05=False)
    _seed_segment_analysis(sandbox, STEM, entries, recs6)
    before = _assignments_before(sandbox)
    try:
        bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "未找到合成结果清单" in str(e) and "05_audio_chunk" in str(e)
    _assert_zero_drop(sandbox, STEM, before)

    # ④ 05 清单 6 段 > 03 条目 3 条（清单 index 越界）
    entries = _seed_segment_inputs(sandbox, n=6)  # 05 清单 6 段
    entries3 = entries[:3]  # 03 改写为 3 条（清单 index 3..5 越界）
    (sandbox["ws"] / "03_parsed_json" / f"{STEM}.json").write_bytes(
        json.dumps(entries3, ensure_ascii=False).encode("utf-8"))
    _seed_segment_analysis(sandbox, STEM, entries3, [_segment_block(0, 2)])
    before = _assignments_before(sandbox)
    try:
        bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "合成清单与段落分析不一致" in str(e)
    _assert_zero_drop(sandbox, STEM, before)

    # ⑤ 段时长探测失败
    entries = _seed_segment_inputs(sandbox, n=6)
    _seed_segment_analysis(sandbox, STEM, entries, recs6)

    def bad_probe(path, ffprobe=""):
        if Path(path).name == "0003.mp3":
            return float("nan"), "probe boom"
        return 30.0, None

    monkeypatch.setattr(bgm_engine, "probe_duration", bad_probe)
    before = _assignments_before(sandbox)
    try:
        bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "无法探测第 2 段时长" in str(e) and "probe boom" in str(e)
    _assert_zero_drop(sandbox, STEM, before)

    # ⑥ 06 旁白缺失（前一 case 的 06 旁白仍留盘——with_06=False 只不写、不删，须清掉）
    narr = layout.audio_merge / f"{STEM}.mp3"
    if narr.is_file():
        narr.unlink()
    entries = _seed_segment_inputs(sandbox, n=6, with_06=False)
    _seed_segment_analysis(sandbox, STEM, entries, recs6)
    monkeypatch.setattr(bgm_engine, "probe_duration",
                        _fake_probe({i: 30.0 for i in range(6)}, 152.25))
    before = _assignments_before(sandbox)
    try:
        bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "未找到旁白音频" in str(e)
    _assert_zero_drop(sandbox, STEM, before)

    # ⑦ 升级前的旧 entries 格式缓存（无 blocks 键，指纹仍有效）→ 判「已失效」
    entries = _seed_segment_inputs(sandbox, n=6)
    data = bgm_engine.load_segment_analysis(layout)
    data["chapters"][STEM] = {
        "fingerprint": bgm_engine.segment_fingerprint(entries),
        "entry_count": 6,
        "entries": [{"index": i, "music_action": "none"} for i in range(6)],
        "model": "old-model",
    }
    bgm_engine.save_segment_analysis(layout, data)
    before = _assignments_before(sandbox)
    try:
        bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "段落分析缺失或已失效" in str(e)
    _assert_zero_drop(sandbox, STEM, before)


def test_recompute_locked_preserved_and_rng_determinism(sandbox, monkeypatch):
    """locked 章整条保留（skipped_locked 计数、条目逐字节不动）；rng 种子 → 跨次同形。"""
    layout = core_paths.get_or_prepare_layout()
    entries = _seed_segment_inputs(sandbox, n=4, speakers=["老道"] * 4)
    _seed_segment_analysis(sandbox, STEM, entries, [_segment_block(0, 3, scene=["战斗"])])
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe({i: 30.0 for i in range(4)}, 120.75))
    locked = {"tags": {"scene": ["x"], "mood": [], "emotion": [], "custom": []},
              "music": "battle.mp3", "segment": False, "locked": True, "manual": True,
              "score": 9, "reason": "手动指定", "matched_at": "old"}
    data = bgm_engine.load_assignments(layout)
    data["chapters"][STEM] = locked
    bgm_engine.save_assignments(layout, data)
    res = bgm_engine.recompute_segment_timelines(layout, [STEM], 1, rng=random.Random(5))
    assert res == {"mode": "segment", "matched": 1, "no_bgm": 0, "skipped_locked": 1}
    asg = bgm_engine.load_assignments(layout)
    assert asg["chapters"][STEM] == locked  # 锁定章逐字节保留
    assert asg["mode"] == "segment"  # mode 仍切换（不影响锁定条目）
    tl = bgm_engine.load_timeline(layout, STEM)
    assert tl["timeline"][0]["music_id"] == "battle.mp3"
    assert tl["timeline"][0]["start"] == 0.0 and tl["timeline"][0]["end"] == 120.75
    # rng 确定性：同种子重跑 → span 列表逐字段一致（generated_at 不参与比对）
    bgm_engine.recompute_segment_timelines(layout, [STEM], 1, rng=random.Random(5))
    tl2 = bgm_engine.load_timeline(layout, STEM)
    assert tl2["timeline"] == tl["timeline"] and tl2["duration"] == tl["duration"]


def test_recompute_no_adjacent_exclusion_reuses_track(sandbox, monkeypatch):
    """相邻同标签场景 + 库内两曲同分 → 相同标签短路直接复用上一 pick：
    match_chapter 只调一次（prev/next 恒 None）、两场景同曲、合并为一个 span。"""
    # 第三首曲与 battle 同标签 → 同分平手（rng 参与场景 0 的选曲）
    music_engine.update_index(lambda idx: idx["tracks"].update(
        {"siege.mp3": {"duration": 100.0, "enabled": True, "description": "",
                       "tags": {"scene": ["战斗"], "mood": ["紧张", "热血"],
                                "emotion": [], "custom": []},
                       "added_at": ""}}))
    (sandbox["lib"] / "siege.mp3").write_bytes(b"fake-music-bytes")
    entries = _seed_segment_inputs(sandbox, n=2, speakers=["老道", "杜尘"])
    blocks = [_segment_block(0, 0, scene=["战斗"], mood=["紧张", "热血"]),
              _segment_block(1, 1, scene=["战斗"], mood=["紧张", "热血"])]
    _seed_segment_analysis(sandbox, STEM, entries, blocks)
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe({0: 30.0, 1: 30.0}, 60.5))
    # 计数 + 断言 prev/next 恒 None（相邻去重已移除）
    calls = []
    orig = bgm_engine.match_chapter

    def spy(chapter_tags, tracks, min_score=1, prev_music=None, next_music=None,
            rng=None, mode="llm"):
        calls.append((dict(chapter_tags), prev_music, next_music))
        return orig(chapter_tags, tracks, min_score, prev_music, next_music,
                    rng=rng, mode=mode)

    monkeypatch.setattr(bgm_engine, "match_chapter", spy)
    layout = core_paths.get_or_prepare_layout()
    bgm_engine.recompute_segment_timelines(layout, [STEM], 1, rng=random.Random(42))
    assert len(calls) == 1  # 场景 1 的相同标签短路未消耗第二次选曲
    assert calls[0][1] is None and calls[0][2] is None
    tl = bgm_engine.load_timeline(layout, STEM)
    assert len(tl["timeline"]) == 1  # 两场景同曲 + 条目邻接 → 合并
    sp = tl["timeline"][0]
    assert sp["music_id"] in ("battle.mp3", "siege.mp3")
    assert sp["start"] == 0.0 and sp["end"] == 60.5
    assert sp["reason"].endswith("（合并 2 段）")
    # 同种子重跑 → 同曲（确定性）
    bgm_engine.recompute_segment_timelines(layout, [STEM], 1, rng=random.Random(42))
    assert bgm_engine.load_timeline(layout, STEM)["timeline"][0]["music_id"] == sp["music_id"]


def test_recompute_gap_blocks_same_track_merge(sandbox, monkeypatch):
    """同曲场景中间隔一条条目空隙 → 绝不合并（条目邻接是合并判据）：
    两 span 之间保留有意静音。"""
    entries = _seed_segment_inputs(sandbox, n=4, speakers=["老道"] * 4)
    blocks = [_segment_block(0, 0, scene=["战斗"]),
              _segment_block(2, 3, scene=["战斗"])]  # 条目 1 = 有意静音
    _seed_segment_analysis(sandbox, STEM, entries, blocks)
    # 4 段同人 × 30s + 三条同人间隙 0.25s = 总时长 120.75
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe({i: 30.0 for i in range(4)}, 120.75))
    layout = core_paths.get_or_prepare_layout()
    res = bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    assert res["matched"] == 1
    tl = bgm_engine.load_timeline(layout, STEM)
    assert len(tl["timeline"]) == 2  # 相同标签短路 → 同曲，但条目不邻接 → 不合并
    assert tl["timeline"][0] == {
        "start": 0.0, "end": 30.0, "music_id": "battle.mp3", "intensity": 2,
        "volume": 0.18, "tags": {"scene": ["战斗"], "mood": [], "emotion": [],
                                 "custom": []},
        "score": 2, "reason": "scene 命中 战斗(+2)",
        "scene_desc": "", "mood_desc": "", "switch_reason": ""}
    assert tl["timeline"][1]["start"] == 60.5 and tl["timeline"][1]["end"] == 120.75
    assert tl["timeline"][1]["music_id"] == "battle.mp3"
    # 静音区间 = span0 末（30.0）→ span1 首（60.5）
    assert tl["timeline"][0]["end"] < tl["timeline"][1]["start"]


def test_recompute_short_span_merge(sandbox, monkeypatch):
    """校验 A 短段并入（< _MIN_SPAN_S = 20s）：条目邻接 → 时域并；
    条目有隙 → 邻居保留原时域（短段区间变静音）；孤立短段原样保留；
    reason 加（短段并入）注记。"""
    layout = core_paths.get_or_prepare_layout()
    # ① 条目邻接的两个短 span（5s 段）→ 并入邻域并域
    entries = _seed_segment_inputs(sandbox, n=2, speakers=["老道"] * 2)
    _seed_segment_analysis(sandbox, STEM, entries,
                           [_segment_block(0, 0, scene=["战斗"]),
                            _segment_block(1, 1)])
    # 2 段同人 × 5s + 一条同人间隙 0.25s = 总时长 10.25
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe({0: 5.0, 1: 5.0}, 10.25))
    bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    tl = bgm_engine.load_timeline(layout, STEM)
    assert len(tl["timeline"]) == 1  # battle(5s) 并入 calm（唯一邻居）
    sp = tl["timeline"][0]
    assert sp["start"] == 0.0 and sp["end"] == 10.25  # 条目邻接 → 时域并域
    assert sp["music_id"] == "calm.mp3"
    assert sp["tags"]["scene"] == ["战斗"]  # 被并方标签并入
    assert sp["reason"] == "无标签命中，使用通用音乐（合并 2 段）（短段并入）"
    # ② 短 span 与邻居间有条目空隙 → 邻居保留原时域（短段区间 = 静音）
    entries = _seed_segment_inputs(sandbox, n=3, speakers=["老道"] * 3)
    _seed_segment_analysis(sandbox, STEM, entries,
                           [_segment_block(0, 0, scene=["战斗"]),
                            _segment_block(2, 2)])  # 条目 1 = 空隙
    # 3 段同人 × 5s + 两条同人间隙 0.25s = 总时长 15.5
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe(
        {0: 5.0, 1: 5.0, 2: 5.0}, 15.5))
    bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    tl = bgm_engine.load_timeline(layout, STEM)
    assert len(tl["timeline"]) == 1
    sp = tl["timeline"][0]
    assert sp["start"] == 10.5 and sp["end"] == 15.5  # 邻居原时域（battle 区间静音）
    assert sp["music_id"] == "calm.mp3"
    assert sp["reason"] == "无标签命中，使用通用音乐（短段并入）"  # 非邻接 → 不记合并
    # ③ 孤立短 span（无邻居）→ 原样保留
    entries = _seed_segment_inputs(sandbox, n=1)
    _seed_segment_analysis(sandbox, STEM, entries, [_segment_block(0, 0)])
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe({0: 5.0}, 5.0))
    bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    tl = bgm_engine.load_timeline(layout, STEM)
    assert tl["timeline"] == [
        {"start": 0.0, "end": 5.0, "music_id": "calm.mp3", "intensity": 2,
         "volume": 0.18, "tags": {c: [] for c in music_engine.TAG_CATEGORIES},
         "score": 0, "reason": "无标签命中，使用通用音乐",
         "scene_desc": "", "mood_desc": "", "switch_reason": ""}]


def test_recompute_cap_merge(sandbox, monkeypatch):
    """校验 B 时长上限（cap = max(3, ceil(分钟数))）：超 cap 只合并标签最相似
    （平手取条目邻接、再取最早）的一对、保留较长 span；绝不凑下限。"""
    layout = core_paths.get_or_prepare_layout()
    # 六首互异标签曲目（各场景命中各自唯一曲目 → 六 span 六不同曲目，
    # 先于校验 B 的同曲合并不生效）
    _tags = ["探索", "情感", "悬疑", "静谧", "史诗"]
    music_engine.update_index(lambda idx: idx["tags"]["scene"].extend(_tags))
    for name, tag in [("explore.mp3", "探索"), ("emotion.mp3", "情感"),
                      ("suspense.mp3", "悬疑"), ("chill.mp3", "静谧"),
                      ("epic.mp3", "史诗")]:
        music_engine.update_index(
            lambda idx, n=name, t=tag: idx["tracks"].update(
                {n: {"duration": 100.0, "enabled": True, "description": "",
                     "tags": {"scene": [t], "mood": [], "emotion": [], "custom": []},
                     "added_at": ""}}))
    # ① total = 271.25 → cap 5；6 个异标签场景 → 恰合并最早一对 → 5 span
    entries = _seed_segment_inputs(sandbox, n=6, speakers=["老道"] * 6)
    _seed_segment_analysis(sandbox, STEM, entries, [
        _segment_block(0, 0, scene=["探索"]),
        _segment_block(1, 1, scene=["情感"]),
        _segment_block(2, 2, scene=["悬疑"]),
        _segment_block(3, 3, scene=["静谧"]),
        _segment_block(4, 4, scene=["史诗"]),
        _segment_block(5, 5, mood=["紧张"]),  # → battle（mood 紧张 +3）
    ])
    # 6 段同人 × 45s + 五条同人间隙 0.25s = 总时长 271.25（cap = max(3, 5) = 5）
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe(
        {i: 45.0 for i in range(6)}, 271.25))
    res = bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    assert res["matched"] == 1
    tl = bgm_engine.load_timeline(layout, STEM)
    assert len(tl["timeline"]) == 5  # 6 → 5（只合并一对）
    # 标签重叠全 0 平手 + 全条目邻接平手 → 最早一对（场景 0+1）被并、保留较长（平手取早）
    sp0 = tl["timeline"][0]
    assert sp0["music_id"] == "explore.mp3"
    assert sp0["tags"]["scene"] == ["探索", "情感"]  # 标签并集（被留方在前）
    assert sp0["reason"] == "scene 命中 探索(+2)（合并 2 段）"
    assert sp0["start"] == 0.0 and sp0["end"] == 90.25  # starts[1]=45.25 + 45
    # ② 不凑下限：3 异标签场景 total 270.5s（cap 5 > 3）→ 3 span 原样
    entries = _seed_segment_inputs(sandbox, n=3, speakers=["老道"] * 3)
    _seed_segment_analysis(sandbox, STEM, entries, [
        _segment_block(0, 0, scene=["探索"]),
        _segment_block(1, 1, scene=["情感"]),
        _segment_block(2, 2, scene=["悬疑"]),
    ])
    # 3 段同人 × 90s + 两条同人间隙 0.25s = 总时长 270.5（cap = max(3, 5) = 5 > 3）
    monkeypatch.setattr(bgm_engine, "probe_duration", _fake_probe(
        {i: 90.0 for i in range(3)}, 270.5))
    bgm_engine.recompute_segment_timelines(layout, [STEM], 1)
    tl = bgm_engine.load_timeline(layout, STEM)
    assert len(tl["timeline"]) == 3  # 宁少勿多——不合并、不凑数
    assert [sp["start"] for sp in tl["timeline"]] == [0.0, 90.25, 180.5]
    assert [sp["music_id"] for sp in tl["timeline"]] == [
        "explore.mp3", "emotion.mp3", "suspense.mp3"]


# --------------------------------------------------------------------------- #
# analyze_segment_chapter e2e（fake LLM · 分批 · 反馈重试 · 零落盘 · 自动时间轴）
# --------------------------------------------------------------------------- #

def _segment_llm(n: int):
    """逐批回复的假 LLM：记录 (messages, kwargs)；每批返回一个覆盖整批 index 的
    场景块（start_segment/end_segment + music_tags scene=战斗 / mood=紧张、强度 2）
    ——各批标签相同 → 时间轴相同标签短路 + 相邻同曲合并为单 span。"""
    calls: list[tuple[list[dict], dict]] = []

    def fake(base_url, api_key, model, messages, temperature, top_p,
             presence_penalty, max_tokens, **kw):
        calls.append((messages, {"max_tokens": max_tokens, **kw}))
        user = messages[1]["content"]
        m = re.search(r"条目 (\d+)~(\d+)", user)
        b0, b1 = int(m.group(1)), int(m.group(2))
        reply = [{
            "start_segment": b0, "end_segment": b1,
            "scene": "战场对峙", "mood": "紧张",
            "music_tags": {"scene": ["战斗"], "mood": ["紧张"],
                           "emotion": [], "custom": []},
            "intensity": 2, "reason": "开场",
        }]
        return (json.dumps(reply, ensure_ascii=False), "stop", None)

    return fake, calls


def test_segment_llm_exchange_logs_prompt_and_raw_reply(caplog):
    """The full prompt and raw reply remain available for semantic failures."""
    caplog.set_level("INFO", logger="audiobook.bgm")
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "chapter text"},
    ]

    bgm_engine._log_segment_llm_exchange(
        STEM, 2, 1, messages,
        content="[]", finish="stop", usage={"completion_tokens": 1},
    )

    output = caplog.text
    assert "段落分析 LLM 原始交换" in output
    assert "system prompt" in output
    assert "chapter text" in output
    assert "[]" in output
    assert "finish_reason='stop'" in output


def test_analyze_segment_success_batches_and_pin_kwargs(sandbox, monkeypatch):
    """45 条 @ 批 20 → 恰 3 次 LLM 调用（20/20/5）；钉死提示词 K 行（06 实测
    50s ≈ 0.83 分钟 → 理论曲目数参考约 3 首）+ extra_body 关思考 + max_tokens=4096；
    第 2 批 user 带上一批开放场景上下文 + 末 3 条原文；写 blocks 缓存 + 自动时间轴
    （三批同标签 → 短路复用 + 相邻同曲合并 → 单 span [0, 50]）。"""
    core_config.update_config({"llm": {"model_name": "test-model"}})
    layout = core_paths.get_or_prepare_layout()
    n = 45
    entries = _seed_segment_inputs(sandbox, n=n)
    fake, calls = _segment_llm(n)
    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", fake)
    monkeypatch.setattr(bgm_engine, "probe_duration",
                        lambda path, ffprobe="": (1.0, None) if re.fullmatch(
                            r"\d{4}\.mp3", Path(path).name) else (50.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-segment", f"段落分析：{STEM}",
                     bgm_engine.analyze_segment_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    t = mgr.get(tid)
    assert len(calls) == 3  # 20 / 20 / 5
    batch_sizes = []
    for messages, kw in calls:
        user = messages[1]["content"]
        batch_sizes.append(len(re.findall(r"^\[\d+\] ", user[user.index("本批段落：\n") + 1:], re.M)))
        assert kw["max_tokens"] == 4096
        assert kw.get("extra_body") == {"enable_thinking": False}
    assert batch_sizes == [20, 20, 5]
    # 第 1 批 user：45 段 / 旁白约 0.83 分钟（06 实测 50s）/ K = 3 首参考行
    user1 = calls[0][0][1]["content"]
    assert "本章共 45 段，旁白约 0.83 分钟，本批是第 1/3 批（条目 0~19）。" in user1
    assert "理论曲目数参考约 3 首（即平均约 1 曲/分钟）" in user1
    assert "章节时长未知" not in user1
    # 第 2 批 user：开放场景上下文（止于 index 19）+ 上一批末 3 条原文（只读）
    user2 = calls[1][0][1]["content"]
    assert "上一批结束时的场景（仍开放，可用 extend 延续）：止于 index 19" in user2
    assert "[17] 段落17的文本。" in user2 and "[19] 段落19的文本。" in user2
    assert "本批是第 2/3 批（条目 20~39）。" in user2
    # 缓存条目：v2 指纹、45 条、blocks 格式（无旧 entries 键）
    seg = bgm_engine.load_segment_analysis(layout)["chapters"][STEM]
    assert seg["fingerprint"] == bgm_engine.segment_fingerprint(entries)
    assert seg["entry_count"] == 45
    assert "entries" not in seg and len(seg["blocks"]) == 3
    assert seg["blocks"][0] == {"start": 0, "end": 19, "scene": "战场对峙",
                                "mood": "紧张",
                                "music_tags": {"scene": ["战斗"], "mood": ["紧张"],
                                               "emotion": [], "custom": []},
                                "intensity": 2, "reason": "开场"}
    assert (seg["blocks"][1]["start"], seg["blocks"][1]["end"]) == (20, 39)
    assert (seg["blocks"][2]["start"], seg["blocks"][2]["end"]) == (40, 44)
    assert seg["model"] == "test-model" and seg["edited"] is False
    # 自动时间轴（05 齐备 → 零 LLM 重算）：三批同标签 → 相同标签短路 + 条目邻接
    # 同曲合并 → 恰 1 span [0, 50]（span 末 = 块末条音频结束，clamp 到 06 总时长）
    res = t.result
    assert res["stem"] == STEM and res["timeline"] is True and res["timeline_note"] == ""
    assert res["new_tags"] == []  # 战斗/紧张 均已在词表
    assert len(res["blocks"]) == 3 and "entries" not in res
    tl = bgm_engine.load_timeline(layout, STEM)
    assert tl is not None and tl["version"] == 2 and tl["duration"] == 50.0
    assert len(tl["timeline"]) == 1
    sp = tl["timeline"][0]
    assert (sp["start"], sp["end"]) == (0.0, 50.0)
    assert sp["music_id"] == "battle.mp3" and sp["score"] == 5
    assert sp["scene_desc"] == "战场对峙" and sp["mood_desc"] == "紧张"
    assert sp["switch_reason"] == "开场" and sp["volume"] == 0.18
    assert sp["reason"] == "mood 命中 紧张(+3)；scene 命中 战斗(+2)（合并 3 段）"
    assert any("时间轴已生成" in e["msg"] for e in t.logs)
    assert concurrency.gate().active == 0


def test_analyze_segment_silent_batch_then_extend(sandbox, monkeypatch):
    """全静音批之后隔批 extend（用户实锤事故回归）：80 条 → 4 批（20/20/20/20）；
    批 3 全静默（[]）不封闭开放场景（open_scene 止于 39 保持前值）→ 批 4 的
    extend（60~79）按提示词「仍开放，可用 extend 延续：止于 index 39」必须被
    解析器接受（prev_scene 止于 39 < b0=60）——旧口径要求严格邻接
    ``prev_scene["end"] == b0-1`` → 批 4 稳定失败「回复不可解析」
    （2026-09 实锤：第 4/11 批，模型回复恰为 ``[{"start_segment": 60,
    "end_segment": 79, "extend": true}]``）。
    时间轴重算：隔静音批的延展以原场景身份开新场景（间隙 40~59 保留静音、
    绝不被同曲合并桥接）→ 恰 2 span（0~39 两场景同曲邻接合并 + 60~79 独立），
    全 battle.mp3（相同标签短路复用上一 pick）。"""
    core_config.update_config({"llm": {"model_name": "test-model"}})
    layout = core_paths.get_or_prepare_layout()
    n = 80
    entries = _seed_segment_inputs(sandbox, n=n)

    users: list[str] = []

    def fake(base_url, api_key, model, messages, temperature, top_p,
             presence_penalty, max_tokens, **kw):
        user = messages[1]["content"]
        users.append(user)
        m = re.search(r"条目 (\d+)~(\d+)", user)
        b0, b1 = int(m.group(1)), int(m.group(2))
        if b0 == 40:  # 批 3：全静默批（平静对话/纯过渡 = LLM 有意静音）
            return ("[]", "stop", None)
        if b0 == 60:  # 批 4：用户实锤回复——隔批延续开放场景
            return (json.dumps(
                [{"start_segment": 60, "end_segment": 79, "extend": True}]),
                "stop", None)
        return (json.dumps([{
            "start_segment": b0, "end_segment": b1,
            "scene": "战场对峙", "mood": "紧张",
            "music_tags": {"scene": ["战斗"], "mood": ["紧张"],
                           "emotion": [], "custom": []},
            "intensity": 2, "reason": "开场" if b0 == 0 else "推进",
        }], ensure_ascii=False), "stop", None)

    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", fake)
    # 80 段同人 × 1.0s + 79 条同人间隙 0.25s = 总时长 99.75（06 同值，无漂移）
    monkeypatch.setattr(bgm_engine, "probe_duration",
                        lambda path, ffprobe="": (1.0, None) if re.fullmatch(
                            r"\d{4}\.mp3", Path(path).name) else (99.75, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-segment", f"段落分析：{STEM}",
                     bgm_engine.analyze_segment_chapter, STEM, cfg.llm,
                     cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "succeeded"  # 修复前：failed（第 4/4 批）
    t = mgr.get(tid)
    assert len(users) == 4  # 批 4 一次通过——无【重试】
    # 批 4 user：开放场景跨静默批延续（批 2 止于 39，批 3 全静默未封闭）
    user4 = users[3]
    assert "本批是第 4/4 批（条目 60~79）。" in user4
    assert "仍开放，可用 extend 延续）：止于 index 39" in user4
    # 缓存 blocks：批 1/批 2 新块 + 批 4 extend 块（描述/标签字段丢弃）
    seg = bgm_engine.load_segment_analysis(layout)["chapters"][STEM]
    assert seg["entry_count"] == 80 and seg["edited"] is False
    assert seg["blocks"][0]["start"] == 0 and seg["blocks"][0]["end"] == 19
    assert seg["blocks"][1]["start"] == 20 and seg["blocks"][1]["end"] == 39
    assert seg["blocks"][2] == {"start": 60, "end": 79, "extend": True}
    assert len(seg["blocks"]) == 3
    # 自动时间轴：场景 0~19 + 20~39 同曲邻接合并为 1 span；隔静音批的延展
    # 重开场景 60~79（身份 = 批 2 块字段）→ 条目不邻接 → 独立 span；
    # 间隙 40~59（49.75s 静音）保留——绝不被同曲合并桥接
    res = t.result
    assert res["timeline"] is True and res["timeline_note"] == ""
    assert res["new_tags"] == []
    tl = bgm_engine.load_timeline(layout, STEM)
    assert tl is not None and tl["version"] == 2 and tl["duration"] == 99.75
    assert len(tl["timeline"]) == 2
    sp0, sp1 = tl["timeline"]
    # starts[i] = i × (1.0 + 0.25)；span0 = 条目 0~39（合并 2 场景）
    assert (sp0["start"], sp0["end"]) == (0.0, 49.75)
    assert sp0["music_id"] == "battle.mp3"
    assert sp0["scene_desc"] == "战场对峙" and sp0["mood_desc"] == "紧张"
    assert sp0["switch_reason"] == "开场"  # 合并保留首场景字段
    assert sp0["reason"] == "mood 命中 紧张(+3)；scene 命中 战斗(+2)（合并 2 段）"
    # span1 = 延展重开的场景（身份沿用批 2 块：reason「推进」），标签相同 →
    # 短路复用上一 pick（battle.mp3，零 rng）
    assert (sp1["start"], sp1["end"]) == (75.0, 99.75)
    assert sp1["music_id"] == "battle.mp3"
    assert sp1["scene_desc"] == "战场对峙" and sp1["mood_desc"] == "紧张"
    assert sp1["switch_reason"] == "推进"
    assert sp1["reason"] == "mood 命中 紧张(+3)；scene 命中 战斗(+2)"
    assert sp0["volume"] == 0.18 and sp1["volume"] == 0.18
    assert concurrency.gate().active == 0


def test_analyze_segment_scene_continues_past_batch_end(sandbox, monkeypatch):
    """场景真实终点越过本批上界 = 钳制而非拒收（2026-09 第 315 章实锤回归）：
    68 条 → 4 批（20/20/20/8）；批 3 回复 = 偏厅 extend（40~58）+ 死牢块
    ``end_segment=67``（章末——场景延续到下一批，模型写出了真实终点）→ 旧
    口径硬拒收 ``end > b1`` → 3 次同形拒收 → 「段落分析失败（第 3/4 批）」
    （确定性模型每轮吐同形回复）。修复 = 钳到本批末条 59 + 任务日志留痕；
    批 4 的 extend（60~67）按「止于 index 59」接住钳制后的开放场景。
    时间轴重算：偏厅场景被 extend 延展为 20~58（身份 = 批 2 块字段）、死牢
    场景 = 钳制块 59~59 + extend 延展 = 59~67 → 3 span（battle/calm/battle
    无同曲邻接不合并、全 ≥20s 不并入、cap = max(3, ceil(3.11)) = 4 ≥ 3）。"""
    core_config.update_config({"llm": {"model_name": "test-model"}})
    n = 68
    _seed_segment_inputs(sandbox, n=n)

    users: list[str] = []

    def fake(base_url, api_key, model, messages, temperature, top_p,
             presence_penalty, max_tokens, **kw):
        user = messages[1]["content"]
        users.append(user)
        m = re.search(r"条目 (\d+)~(\d+)", user)
        b0, b1 = int(m.group(1)), int(m.group(2))
        if b0 == 0:  # 批 1：广场场景
            return (json.dumps([{
                "start_segment": 0, "end_segment": 19,
                "scene": "广场对峙", "mood": "紧张",
                "music_tags": {"scene": ["战斗"], "mood": ["紧张"],
                               "emotion": [], "custom": []},
                "intensity": 2, "reason": "开场",
            }], ensure_ascii=False), "stop", None)
        if b0 == 20:  # 批 2：偏厅夜话（轻松 = 词表内但无曲目命中 → 通用池）
            return (json.dumps([{
                "start_segment": 20, "end_segment": 39,
                "scene": "偏厅夜话", "mood": "轻松",
                "music_tags": {"scene": [], "mood": ["轻松"],
                               "emotion": [], "custom": []},
                "intensity": 2, "reason": "转入夜话",
            }], ensure_ascii=False), "stop", None)
        if b0 == 40:  # 批 3：第 315 章实锤形状——偏厅 extend + 死牢块
            # end_segment=67 越过本批上界 59（章末真实终点）
            return (json.dumps([
                {"start_segment": 40, "end_segment": 58, "extend": True},
                {"start_segment": 59, "end_segment": 67,
                 "scene": "死牢潜入", "mood": "肃杀",
                 "music_tags": {"scene": ["战斗"], "mood": ["紧张"],
                                "emotion": [], "custom": []},
                 "intensity": 3, "reason": "潜入地下死牢",
            }], ensure_ascii=False), "stop", None)
        if b0 == 60:  # 批 4：死牢场景 extend 接住（钳制后止于 59 < 60）
            return (json.dumps(
                [{"start_segment": 60, "end_segment": 67, "extend": True}]),
                "stop", None)
        raise AssertionError(f"unexpected batch: {b0}~{b1}")

    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", fake)
    # 68 段同人 × 2.5s + 67 条同人间隙 0.25s = 总时长 186.75（06 同值，无漂移）
    monkeypatch.setattr(bgm_engine, "probe_duration",
                        lambda path, ffprobe="": (2.5, None) if re.fullmatch(
                            r"\d{4}\.mp3", Path(path).name) else (186.75, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-segment", f"段落分析：{STEM}",
                     bgm_engine.analyze_segment_chapter, STEM, cfg.llm,
                     cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "succeeded"  # 修复前：failed（第 3/4 批）
    t = mgr.get(tid)
    assert len(users) == 4  # 批 3 钳制成功一次通过——无【重试】
    # 批 3 user：偏厅开放场景止于 39（批 2 末条）；批 4 user：钳制后的死牢
    # 端点 59——钳制值经 open_scene 状态机传播进下一批提示词
    assert "本批是第 3/4 批（条目 40~59）。" in users[2]
    assert "止于 index 39" in users[2]
    user4 = users[3]
    assert "本批是第 4/4 批（条目 60~67）。" in user4
    assert "止于 index 59" in user4
    # 钳制 note 任务日志留痕（终端/app.log 同步经 log.info 双通道）
    assert any("已钳制到 59" in e["msg"] for e in t.logs)
    # 缓存 blocks = 5 块：广场新块 + 偏厅新块 + 偏厅 extend + 死牢钳制块
    # （end 67 → 59）+ 死牢 extend
    layout = core_paths.get_or_prepare_layout()
    seg = bgm_engine.load_segment_analysis(layout)["chapters"][STEM]
    assert seg["entry_count"] == 68 and seg["edited"] is False
    assert [(b["start"], b["end"]) for b in seg["blocks"]] == \
        [(0, 19), (20, 39), (40, 58), (59, 59), (60, 67)]
    assert seg["blocks"][2] == {"start": 40, "end": 58, "extend": True}
    assert seg["blocks"][4] == {"start": 60, "end": 67, "extend": True}
    assert seg["blocks"][3]["scene"] == "死牢潜入"
    assert seg["blocks"][3]["intensity"] == 3
    # 时间轴：3 场景 → 3 span（tags 全词表内 → new_tags 空）
    res = t.result
    assert res["timeline"] is True and res["timeline_note"] == ""
    assert res["new_tags"] == []
    tl = bgm_engine.load_timeline(layout, STEM)
    assert tl is not None and tl["version"] == 2 and tl["duration"] == 186.75
    assert len(tl["timeline"]) == 3
    sp0, sp1, sp2 = tl["timeline"]
    # starts[i] = i × (2.5 + 0.25)
    assert (sp0["start"], sp0["end"]) == (0.0, 54.75)
    assert sp0["music_id"] == "battle.mp3"
    assert sp0["scene_desc"] == "广场对峙" and sp0["mood_desc"] == "紧张"
    assert sp0["switch_reason"] == "开场"
    assert sp0["reason"] == "mood 命中 紧张(+3)；scene 命中 战斗(+2)"
    assert sp0["volume"] == 0.18
    # span1 = 偏厅场景（extend 延展 20~58、身份沿用批 2 块）→ 轻松无曲目命中
    # → 通用池 calm.mp3（score 0）
    assert (sp1["start"], sp1["end"]) == (55.0, 162.0)
    assert sp1["music_id"] == "calm.mp3"
    assert sp1["scene_desc"] == "偏厅夜话" and sp1["mood_desc"] == "轻松"
    assert sp1["switch_reason"] == "转入夜话"
    assert sp1["reason"] == "无标签命中，使用通用音乐"
    assert sp1["volume"] == 0.18
    # span2 = 死牢场景（钳制块 59~59 + extend 延展 = 59~67、身份 = 批 3 块）
    # → 战斗/紧张命中 battle.mp3；intensity 3 → volume 档位 1.5×
    assert (sp2["start"], sp2["end"]) == (162.25, 186.75)
    assert sp2["music_id"] == "battle.mp3"
    assert sp2["scene_desc"] == "死牢潜入" and sp2["mood_desc"] == "肃杀"
    assert sp2["switch_reason"] == "潜入地下死牢"
    assert sp2["reason"] == "mood 命中 紧张(+3)；scene 命中 战斗(+2)"
    assert sp2["volume"] == pytest.approx(0.18 * 1.5)
    assert concurrency.gate().active == 0


def test_analyze_segment_retry_feedback_within_batch(sandbox, monkeypatch):
    """批 2 首次回复不可解析 → 带【重试】反馈重试成功：system 逐字节不变、
    user2 含【重试】+「问题：回复不可解析」+ 上次回复原文；共 4 次调用。"""
    core_config.update_config({"llm": {"model_name": "test-model"}})
    n = 45  # 3 批（20 + 20 + 5）
    _seed_segment_inputs(sandbox, n=n)
    base, calls = _segment_llm(n)

    fired = []  # 只在批 2 首次失败一次（批 3 首次须正常回复）

    def fake(base_url, api_key, model, messages, temperature, top_p,
             presence_penalty, max_tokens, **kw):
        user = messages[1]["content"]
        if ("上一批结束时的场景（仍开放，可用 extend 延续）" in user
                and "【重试】" not in user and not fired):
            fired.append(True)
            calls.append((messages, {"max_tokens": max_tokens, **kw}))
            return ("批二首次：这不是 JSON", "stop", None)
        return base(base_url, api_key, model, messages, temperature, top_p,
                    presence_penalty, max_tokens, **kw)

    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", fake)
    monkeypatch.setattr(bgm_engine, "probe_duration",
                        lambda path, ffprobe="": (1.0, None) if re.fullmatch(
                            r"\d{4}\.mp3", Path(path).name) else (30.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-segment", f"段落分析：{STEM}",
                     bgm_engine.analyze_segment_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    assert len(calls) == 4  # 批1 + 批2失败 + 批2重试 + 批3
    sys1, user1 = calls[0][0][0]["content"], calls[0][0][1]["content"]
    bad, fixed = calls[1][0], calls[2][0]
    assert bad[0]["content"] == fixed[0]["content"]  # system 跨尝试逐字节不变
    assert "【重试】" not in user1
    assert "【重试】" in fixed[1]["content"]
    assert "问题：回复不可解析" in fixed[1]["content"]
    assert "批二首次：这不是 JSON" in fixed[1]["content"]  # 上次回复原文节选
    assert bgm_engine._SEGMENT_FORMAT_HINT in fixed[1]["content"]
    assert mgr.get(tid).result["timeline"] is True


def test_analyze_segment_all_failures_zero_writes(sandbox, monkeypatch):
    """某批 3 次全败 → 任务 failed、错误带「段落分析失败（第 k/M 批）」、
    零落盘（分析缓存 / 词表 / 时间轴 / assignments 全不动）。"""
    core_config.update_config({"llm": {"model_name": "test-model"}})
    layout = core_paths.get_or_prepare_layout()
    _seed_segment_inputs(sandbox, n=45)
    seen = []

    def fake(base_url, api_key, model, messages, temperature, top_p,
             presence_penalty, max_tokens, **kw):
        seen.append(messages[1]["content"])
        raise RuntimeError("boom")

    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", fake)
    before_idx = (sandbox["lib"] / "music_index.json").read_bytes()
    before_asg = _assignments_before(sandbox)
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-segment", f"段落分析：{STEM}",
                     bgm_engine.analyze_segment_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "failed"
    err = mgr.get(tid).error
    assert "段落分析失败（第 1/3 批）" in err and "boom" in err
    assert len(seen) == 3  # 第 1 批耗尽即中止，不再跑第 2/3 批
    assert not (sandbox["ws"] / "08_bgm" / bgm_engine.SEGMENT_ANALYSIS_NAME).exists()  # 零落盘
    assert (sandbox["lib"] / "music_index.json").read_bytes() == before_idx  # 词表未动
    p = layout.bgm / bgm_engine.ASSIGNMENTS_NAME
    assert (p.read_bytes() if p.is_file() else None) == before_asg
    assert not bgm_engine._timeline_path(layout, STEM).exists()
    assert concurrency.gate().active == 0


def test_analyze_segment_all_empty_replies_fail_without_writing(sandbox, monkeypatch):
    """A whole chapter of ``[]`` replies is a model refusal, not valid analysis."""
    core_config.update_config({"llm": {"model_name": "test-model"}})
    layout = core_paths.get_or_prepare_layout()
    _seed_segment_inputs(sandbox, n=45)
    calls = []

    def fake(base_url, api_key, model, messages, temperature, top_p,
             presence_penalty, max_tokens, **kw):
        calls.append(messages)
        return "[]", "stop", {"completion_tokens": 1}

    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", fake)
    monkeypatch.setattr(
        bgm_engine, "probe_duration",
        lambda path, ffprobe="": (1.0, None) if re.fullmatch(
            r"\d{4}\.mp3", Path(path).name) else (50.0, None),
    )
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create(
        "bgm-segment", f"段落分析：{STEM}",
        bgm_engine.analyze_segment_chapter, STEM, cfg.llm, cfg.bgm,
    ).id

    assert _wait_terminal(mgr, tid) == "failed"
    assert len(calls) == 3
    assert "所有段落分析批次均返回空数组" in mgr.get(tid).error
    assert not (layout.bgm / bgm_engine.SEGMENT_ANALYSIS_NAME).exists()
    assert not bgm_engine._timeline_path(layout, STEM).exists()


def test_analyze_segment_missing_05_still_succeeds(sandbox, monkeypatch):
    """05 缺失 → 自动时间轴失败但任务仍成功（分析是持久产物），result 带
    timeline=False + 可操作注记；缓存正常写出。"""
    core_config.update_config({"llm": {"model_name": "test-model"}})
    layout = core_paths.get_or_prepare_layout()
    entries = _seed_segment_inputs(sandbox, n=3, with_05=False)
    fake, calls = _segment_llm(3)
    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", fake)
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-segment", f"段落分析：{STEM}",
                     bgm_engine.analyze_segment_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    t = mgr.get(tid)
    assert len(calls) == 1
    res = t.result
    assert res["timeline"] is False
    assert "时间轴未生成" in res["timeline_note"] and "匹配（时间轴）" in res["timeline_note"]
    seg = bgm_engine.load_segment_analysis(layout)["chapters"][STEM]
    assert seg["fingerprint"] == bgm_engine.segment_fingerprint(entries)  # 缓存照写
    assert not bgm_engine._timeline_path(layout, STEM).exists()


def test_analyze_segment_missing_06_degrades_prompt(sandbox, monkeypatch):
    """06 缺失 → 06 探测降级 minutes=None：user 无「分钟」/K 参考行、含「章节时长
    未知」降级行；分析仍成功、缓存照写（探测 hiccup 不得杀死分析）。"""
    core_config.update_config({"llm": {"model_name": "test-model"}})
    layout = core_paths.get_or_prepare_layout()
    entries = _seed_segment_inputs(sandbox, n=3, with_06=False)
    fake, calls = _segment_llm(3)
    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", fake)
    monkeypatch.setattr(bgm_engine, "probe_duration",
                        lambda path, ffprobe="": (1.0, None) if re.fullmatch(
                            r"\d{4}\.mp3", Path(path).name) else (30.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-segment", f"段落分析：{STEM}",
                     bgm_engine.analyze_segment_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    user1 = calls[0][0][1]["content"]
    assert "本章共 3 段，本批是第 1/1 批（条目 0~2）。" in user1
    assert "章节时长未知，请按常规 3-7 个场景块把握" in user1
    assert "分钟" not in user1 and "理论曲目数参考" not in user1
    seg = bgm_engine.load_segment_analysis(layout)["chapters"][STEM]
    assert seg["fingerprint"] == bgm_engine.segment_fingerprint(entries)
    # 时间轴重算因 06 缺失失败 → result timeline=False（分析本身成功）
    res = mgr.get(tid).result
    assert res["timeline"] is False and "时间轴未生成" in res["timeline_note"]


def test_analyze_segment_new_tags_registered(sandbox, monkeypatch):
    """场景块的新标签（词表外保留）并入全局词表：新名入首桶 + 日志留痕。"""
    core_config.update_config({"llm": {"model_name": "test-model"}})
    _seed_segment_inputs(sandbox, n=2)

    def fake(base_url, api_key, model, messages, temperature, top_p,
             presence_penalty, max_tokens, **kw):
        return (json.dumps([{
                    "start_segment": 0, "end_segment": 1,
                    "scene": "赛博都市", "mood": "霓虹",
                    "music_tags": {"scene": ["赛博"], "mood": ["霓虹"],
                                   "emotion": [], "custom": []},
                    "intensity": 3, "reason": "开场"}], ensure_ascii=False),
                "stop", None)

    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", fake)
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-segment", f"段落分析：{STEM}",
                     bgm_engine.analyze_segment_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    t = mgr.get(tid)
    assert set(t.result["new_tags"]) == {"霓虹", "赛博"}
    assert any(e["msg"].startswith("音乐库新增标签") for e in t.logs)
    idx = music_engine.load_index()
    assert "赛博" in idx["tags"]["scene"] and "霓虹" in idx["tags"]["mood"]


def test_analyze_segment_model_empty_and_missing_script(sandbox):
    """快速失败：model 空 / 03 脚本缺失 → failed + 可操作文案（零 LLM 调用）。"""
    mgr = sandbox["mgr"]
    cfg = core_config.get_config()
    assert not cfg.llm.model_name
    tid = mgr.create("bgm-segment", f"段落分析：{STEM}",
                     bgm_engine.analyze_segment_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "尚未配置 LLM 模型" in mgr.get(tid).error
    # model 配好但 03 缺失
    core_config.update_config({"llm": {"model_name": "test-model"}})
    cfg = core_config.get_config()
    tid2 = mgr.create("bgm-segment", f"段落分析：{STEM}",
                      bgm_engine.analyze_segment_chapter, STEM, cfg.llm, cfg.bgm).id
    assert _wait_terminal(mgr, tid2) == "failed"
    assert "未找到脚本 JSON" in mgr.get(tid2).error
    assert concurrency.gate().active == 0


def test_analyze_segment_cancel_while_queued(sandbox, monkeypatch):
    """排队中被取消 ≤~1s：未取槽、零落盘（gate 无下溢）。"""
    core_config.update_config({"llm": {"model_name": "test-model"}})
    layout = core_paths.get_or_prepare_layout()
    _seed_segment_inputs(sandbox, n=3)
    fake, _calls = _segment_llm(3)
    monkeypatch.setattr(bgm_engine, "_llm_chat_completion", fake)
    g = concurrency.gate()
    g.acquire()  # 测试持有唯一槽位
    try:
        cfg = core_config.get_config()
        mgr = sandbox["mgr"]
        t0 = time.time()
        tid = mgr.create("bgm-segment", f"段落分析：{STEM}",
                         bgm_engine.analyze_segment_chapter, STEM, cfg.llm, cfg.bgm).id
        _wait_until(lambda: mgr.get(tid).status is TaskStatus.RUNNING, timeout=3)
        mgr.control(tid, "cancel")
        _wait_until(lambda: mgr.get(tid).status is TaskStatus.CANCELLED, timeout=3)
        assert time.time() - t0 < 2.5  # 一个 stop_check 轮询（0.2 s）+ 开销
        assert g.active == 1  # worker 未取槽
        assert not (sandbox["ws"] / "08_bgm" / bgm_engine.SEGMENT_ANALYSIS_NAME).exists()
        assert not bgm_engine._timeline_path(layout, STEM).exists()
    finally:
        g.release()
        assert g.active == 0


# --------------------------------------------------------------------------- #
# mix_chapter 时间轴分支（fake Popen + fake probe）
# --------------------------------------------------------------------------- #

def test_mix_timeline_fresh_success(sandbox, monkeypatch):
    """segment 章 + 新鲜时间轴 → 时间轴图命令（adelay 定位 / amix K+1 /
    normalize=0），result music = 「段落级时间轴（N 段 BGM）」。"""
    spans = [{"start": 0.0, "end": 50.0, "music_id": "battle.mp3",
              "intensity": 2, "volume": 0.18,
              "tags": {"scene": ["战斗"], "mood": ["紧张"], "emotion": [], "custom": []},
              "score": 8, "reason": "r"}]
    _seed_segment_inputs(sandbox, n=6)
    (sandbox["ws"] / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    (sandbox["ws"] / "06_audio_merge" / f"{STEM}.mp3").write_bytes(b"NARR" * 256)
    _write_timeline(sandbox, STEM, spans, duration=100.0, source_duration=100.0)
    _seed_segment_assignment(sandbox, STEM, music=None)
    procs = []

    def fake_popen(cmd, **kw):
        p = _FakeProc(cmd, core_paths.get_or_prepare_layout().bgm / f"{STEM}.mp3")
        procs.append(p)
        return p

    monkeypatch.setattr(bgm_engine.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    t = mgr.get(tid)
    assert t.result["music"] == "段落级时间轴（1 段 BGM）"
    assert t.result["duration"] == 100.0
    out = sandbox["ws"] / "08_bgm" / f"{STEM}.mp3"
    assert out.exists() and out.stat().st_size == 4096
    cmd = procs[0].cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "adelay=0:all=1" in fc and "normalize=0" in fc and "inputs=2" in fc
    assert str(sandbox["lib"] / "battle.mp3") in cmd
    assert "-threads" in cmd  # 编码器线程预算（merge.thread_budget）
    assert concurrency.merge_gate().active == 0


def test_mix_timeline_stale_fails(sandbox, monkeypatch):
    """旁白现探时长 vs 时间轴 duration 漂移 > 0.5s → 「时间轴已过期」，无输出。"""
    _seed_segment_inputs(sandbox, n=1)  # 06 旁白
    _write_timeline(sandbox, STEM, [{"start": 0.0, "end": 10.0,
                                     "music_id": "battle.mp3", "volume": 0.18}],
                    duration=99.0, source_duration=100.0)
    _seed_segment_assignment(sandbox, STEM, music=None)
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "时间轴已过期" in mgr.get(tid).error
    assert not (sandbox["ws"] / "08_bgm" / f"{STEM}.mp3").exists()


def test_mix_timeline_span_music_missing_fails(sandbox, monkeypatch):
    """时间轴 span 指向已出库曲目 → 「音乐库中找不到」，无输出。"""
    _seed_segment_inputs(sandbox, n=1)
    # duration 须与旁白现探时长（100.0）一致，否则新鲜度检查先触发（「时间轴已过期」
    # 会掩盖 span 曲目缺失分支）。
    _write_timeline(sandbox, STEM, [{"start": 0.0, "end": 10.0,
                                     "music_id": "ghost.mp3", "volume": 0.18}],
                    duration=100.0, source_duration=100.0)
    _seed_segment_assignment(sandbox, STEM, music=None)
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "failed"
    assert "音乐库中找不到 ghost.mp3" in mgr.get(tid).error
    assert not (sandbox["ws"] / "08_bgm" / f"{STEM}.mp3").exists()


def test_mix_timeline_zero_spans_copies_narration(sandbox, monkeypatch):
    """零 span（全章无 BGM）时间轴 → copy2 旁白（成品，与 music=None 同径）。"""
    _seed_segment_inputs(sandbox, n=6)
    (sandbox["ws"] / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    (sandbox["ws"] / "06_audio_merge" / f"{STEM}.mp3").write_bytes(b"SEG-VERBATIM-999")
    _write_timeline(sandbox, STEM, [], duration=42.0, source_duration=42.0)
    _seed_segment_assignment(sandbox, STEM, music=None)
    # 时间轴分支在零 span 检查**前**先探测旁白时长（新鲜度锚点）——旁白是
    # 垃圾字节，真实 ffprobe 必失败；钉死与时间轴 duration 一致的时长。
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (42.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    out = sandbox["ws"] / "08_bgm" / f"{STEM}.mp3"
    assert out.read_bytes() == b"SEG-VERBATIM-999"  # 字节一致
    assert mgr.get(tid).result["music"] is None and mgr.get(tid).result["duration"] is None
    assert any("无 BGM 段" in e["msg"] for e in mgr.get(tid).logs)


def test_mix_non_segment_entry_ignores_leftover_timeline(sandbox, monkeypatch):
    """章节模式条目（segment 标记 false/无）+ 遗留时间轴文件 → 陈旧时间轴惰性：
    走旧路径（build_mix_cmd 单 BGM 图，无 adelay），result music = 原曲目名。"""
    (sandbox["ws"] / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    (sandbox["ws"] / "06_audio_merge" / f"{STEM}.mp3").write_bytes(b"NARR" * 256)
    _write_timeline(sandbox, STEM, [{"start": 0.0, "end": 50.0,
                                     "music_id": "calm.mp3", "volume": 0.18}])
    _seed_mix_inputs(sandbox)  # 章节模式条目（music=battle.mp3，无 segment 键）
    procs = []

    def fake_popen(cmd, **kw):
        p = _FakeProc(cmd, core_paths.get_or_prepare_layout().bgm / f"{STEM}.mp3")
        procs.append(p)
        return p

    monkeypatch.setattr(bgm_engine.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bgm_engine, "probe_duration", lambda path, ffprobe="": (100.0, None))
    cfg = core_config.get_config()
    mgr = sandbox["mgr"]
    tid = mgr.create("bgm-mix", f"背景音乐混音：{STEM}",
                     bgm_engine.mix_chapter, STEM, cfg.bgm, cfg.ffmpeg).id
    assert _wait_terminal(mgr, tid) == "succeeded"
    assert mgr.get(tid).result["music"] == "battle.mp3"  # 旧路径的曲目名
    fc = procs[0].cmd[procs[0].cmd.index("-filter_complex") + 1]
    assert "adelay" not in fc  # 非时间轴图
    assert str(sandbox["lib"] / "battle.mp3") in procs[0].cmd
