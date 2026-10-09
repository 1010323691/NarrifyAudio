"""BGM durable task submission and chapter API tests."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.api import bgm as api_bgm
from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.engines import music as music_engine
import backend.engines.bgm as bgm_engine

STEMS = [f"ch{i}" for i in range(1, 9)]


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    ws = tmp_path / "Book"
    (ws / "02_split_text").mkdir(parents=True)
    for stem in STEMS:
        (ws / "02_split_text" / f"{stem}.txt").write_text("chapter", encoding="utf-8")
    core_config.reset_config_cache()
    core_config.set_workspace_pointer(str(ws))
    core_config.update_config({"llm": {"model_name": "test-model"}})
    lib = tmp_path / "music_library"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "t1.mp3").write_bytes(b"music")
    music_engine.update_index(lambda idx: idx["tracks"].update({
        "t1.mp3": {"duration": 120.0, "enabled": True, "description": "",
                   "tags": {"scene": [], "mood": [], "emotion": [], "custom": []}, "added_at": ""}
    }))
    _seed_mix(ws, STEMS)
    yield ws
    core_config.reset_config_cache()


def _api_context():
    return SimpleNamespace(user=SimpleNamespace(id="user-1"), session=SimpleNamespace())


def _install_durable_mocks(monkeypatch, active=None):
    created = []
    monkeypatch.setattr(api_bgm._common, "require_workspace", lambda: None)
    monkeypatch.setattr(api_bgm, "active_durable_targets", lambda **kw: set((active or {}).get(kw["task_type"], set())))
    monkeypatch.setattr(api_bgm, "active_durable_payloads", lambda **kw: [])
    monkeypatch.setattr(api_bgm, "submit_legacy_engine_task", lambda **kw: created.append(kw) or {"id": f"task-{len(created)}"})
    def submit_batch(**kwargs):
        entries, config = kwargs["prepare"]()
        ids = []
        for entry in entries:
            ids.append(api_bgm.submit_legacy_engine_task(task_type=kwargs["task_type"], label=entry["label"],
                payload={**entry["payload"], "config": config})["id"])
        result = {"task_ids": ids, **({"task_id": ids[0]} if len(ids) == 1 else {})}
        if kwargs.get("receipt_field"):
            result[kwargs["receipt_field"]] = [{**entry.get("receipt", {}), "task_id": tid} for entry, tid in zip(entries, ids)]
        return result
    monkeypatch.setattr(api_bgm, "submit_engine_batch", submit_batch)
    return created


def _seed_mix(ws, stems: list[str], music: str | None = "t1.mp3") -> None:
    """06 narration mp3s + a (matched) assignment entry per stem."""
    (ws / "06_audio_merge").mkdir(parents=True, exist_ok=True)
    for s in stems:
        (ws / "06_audio_merge" / f"{s}.mp3").write_bytes(b"NARR" * 16)
    layout = core_paths.get_or_prepare_layout()
    data = bgm_engine.load_assignments(layout)
    for s in stems:
        data["chapters"][s] = {"tags": {}, "music": music, "locked": False,
                               "manual": False, "score": 3, "reason": "r",
                               "matched_at": "t0"}
    bgm_engine.save_assignments(layout, data)


# -- 段落级（bgm-segment）seeding helpers -----------------------------------------

def _segment_entries(n: int, speakers=None) -> list[dict]:
    speakers = list(speakers) if speakers else ["老道"] * n
    return [{"speaker": speakers[i % len(speakers)], "text": f"段落{i}的文本。"}
            for i in range(n)]


def _seed_scripts(ws, stems: list[str], n: int = 6) -> dict[str, list[dict]]:
    """03 脚本 JSON（n 条）per stem — the paragraph-analysis input."""
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    out: dict[str, list[dict]] = {}
    for s in stems:
        e = _segment_entries(n)
        (ws / "03_parsed_json" / f"{s}.json").write_bytes(
            json.dumps(e, ensure_ascii=False).encode("utf-8"))
        out[s] = e
    return out


def _segment_block(start: int, end: int, intensity: int = 2, **tags) -> dict:
    """一个缓存的场景块（新场景形态：四桶标签 + 强度）。"""
    return {
        "start": start, "end": end, "scene": "", "mood": "", "reason": "",
        "music_tags": {c: list(tags.get(c) or [])
                       for c in music_engine.TAG_CATEGORIES},
        "intensity": intensity,
    }


def _seed_segment_analysis(stem: str, entries, blocks,
                           fingerprint: str | None = None) -> None:
    layout = core_paths.get_or_prepare_layout()
    data = bgm_engine.load_segment_analysis(layout)
    data["chapters"][stem] = {
        "fingerprint": fingerprint or bgm_engine.segment_fingerprint(entries),
        "entry_count": len(entries),
        "blocks": blocks,
        "model": "test-model",
        "analyzed_at": "t0",
        "edited": False,
    }
    bgm_engine.save_segment_analysis(layout, data)


def _write_timeline(stem: str, spans: list[dict], duration: float) -> dict:
    layout = core_paths.get_or_prepare_layout()
    for sp in spans:  # v2 时间轴的三个描述字段（缺省空串，旧调用方无需感知）
        sp.setdefault("scene_desc", "")
        sp.setdefault("mood_desc", "")
        sp.setdefault("switch_reason", "")
    tl = {"version": 2, "stem": stem, "generated_at": "t0", "model": "test-model",
          "fingerprint": "fp", "entry_count": 6,
          "duration": duration, "timeline": spans}
    bgm_engine._atomic_write_json(bgm_engine._timeline_path(layout, stem), tl)
    return tl


def test_segment_analysis_submits_durable_tasks_and_checks_audio_conflicts(workspace, monkeypatch):
    _seed_scripts(workspace, ["ch1"])
    created = _install_durable_mocks(monkeypatch)
    result = api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=["ch1"]), _api_context(), object())
    assert result["task_ids"] == ["task-1"]
    assert created[0]["task_type"] == "bgm.segment"
    assert created[0]["payload"]["stem"] == "ch1"

    monkeypatch.setattr(api_bgm, "active_durable_payloads", lambda **kw: [{"script": "ch1.json", "scripts": []}])
    with pytest.raises(HTTPException) as exc:
        api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=["ch1"]), _api_context(), object())
    assert exc.value.status_code == 409


def test_match_submits_durable_tasks_and_rejects_active_conflicts(workspace, monkeypatch):
    created = _install_durable_mocks(monkeypatch)
    result = api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"], mode="random"), _api_context(), object())
    assert result == {"task_id": "task-1", "task_ids": ["task-1"]}
    assert created[0]["task_type"] == "bgm.match"
    assert created[0]["payload"]["mode"] == "random"

    _install_durable_mocks(monkeypatch, {"bgm.match": {"ch1"}})
    with pytest.raises(HTTPException) as exc:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"], mode="random"), _api_context(), object())
    assert exc.value.status_code == 409


def test_match_segment_requires_script_and_submits_durable_task(workspace, monkeypatch):
    _seed_scripts(workspace, ["ch1"])
    created = _install_durable_mocks(monkeypatch)
    result = api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"], mode="segment"), _api_context(), object())
    assert result["task_id"] == "task-1"
    assert created[0]["task_type"] == "bgm.match"
    assert created[0]["payload"]["mode"] == "segment"


def test_mix_submits_durable_tasks_and_rejects_same_chapter_conflict(workspace, monkeypatch):
    created = _install_durable_mocks(monkeypatch)
    result = api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1", "ch2"]), _api_context(), object())
    assert result["task_ids"] == ["task-1", "task-2"]
    assert [call["task_type"] for call in created] == ["bgm.mix", "bgm.mix"]

    _install_durable_mocks(monkeypatch, {"bgm.mix": {"ch1"}})
    with pytest.raises(HTTPException) as exc:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1"]), _api_context(), object())
    assert exc.value.status_code == 409

    _install_durable_mocks(monkeypatch, {"bgm.match": {"ch1"}})
    with pytest.raises(HTTPException) as exc:
        api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1"]), _api_context(), object())
    assert exc.value.status_code == 409


def test_mix_submission_never_loads_or_probes_segment_timeline(workspace, monkeypatch):
    created = _install_durable_mocks(monkeypatch)
    def unexpected(*_args, **_kwargs):
        raise AssertionError("heavy mix preflight must run in the Worker")
    monkeypatch.setattr(bgm_engine, "load_segment_timeline", unexpected)
    monkeypatch.setattr(bgm_engine, "probe_duration", unexpected)
    result = api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1", "ch2"]), _api_context(), object())
    assert len(created) == 2 and result["task_ids"] == ["task-1", "task-2"]



def test_durable_audio_conflicts_include_unscoped_batches(workspace, monkeypatch):
    payloads = [{"script": None, "scripts": []}]
    monkeypatch.setattr(api_bgm, "active_durable_targets", lambda **kw: set())
    monkeypatch.setattr(api_bgm, "active_durable_payloads", lambda **kw: payloads if kw["task_type"] == "tts.batch" else [])
    conflicts = api_bgm._durable_audio_conflicts(["ch1", "ch2"], _api_context(), object())
    assert conflicts == ["ch1", "ch2"]


def test_bgm_routes_keep_input_and_configuration_guards(workspace, monkeypatch):
    _install_durable_mocks(monkeypatch)
    with pytest.raises(HTTPException) as empty:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=[]), _api_context(), object())
    assert empty.value.status_code == 400
    with pytest.raises(HTTPException) as traversal:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=["../escape"]), _api_context(), object())
    assert traversal.value.status_code == 400
    with pytest.raises(HTTPException) as mode:
        api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1"], mode="unknown"), _api_context(), object())
    assert mode.value.status_code == 400


def test_chapters_rows_restore_title_labels_without_changing_stems(workspace):
    stem = "第 001 章 标题_章节内标签_"
    layout = core_paths.get_or_prepare_layout()
    source = layout.split_text / f"{stem}.txt"
    source.write_text("书籍简介\n第1章 标题【章节内标签】！\n正文", encoding="utf-8")
    rows = {row["stem"]: row for row in api_bgm.list_chapters()["chapters"]}
    assert rows[stem]["display_name"] == "第 001 章 标题【章节内标签】！"
    assert rows[stem]["stem"] == stem
    source.write_text("第1章 标题【更新后的标签】？\n正文", encoding="utf-8")
    rows = {row["stem"]: row for row in api_bgm.list_chapters()["chapters"]}
    assert rows[stem]["display_name"] == "第 001 章 标题【更新后的标签】？"


def test_chapters_rows(workspace):
    ws = workspace
    layout = core_paths.get_or_prepare_layout()
    # ch1: full pipeline state (06 + 08 + assignment with live music)
    (ws / "08_bgm").mkdir(parents=True, exist_ok=True)
    (ws / "08_bgm" / "ch1.mp3").write_bytes(b"MIXED")
    # ch2: narration is a .wav (fallback) + assignment points at a deleted music
    (ws / "06_audio_merge" / "ch2.mp3").unlink()
    (ws / "06_audio_merge" / "ch2.wav").write_bytes(b"WAVNARR")
    data = bgm_engine.load_assignments(layout)
    data["chapters"]["ch2"]["music"] = "ghost.mp3"
    data["chapters"]["ghost"] = data["chapters"]["ch1"].copy()  # orphan (no 02 file)
    bgm_engine.save_assignments(layout, data)

    res = api_bgm.list_chapters()
    rows = {r["stem"]: r for r in res["chapters"]}
    assert list(rows) == STEMS  # the 02 files are the row basis (orphan hidden)
    assert "ghost" not in rows

    r1 = rows["ch1"]
    assert r1["narration_exists"] is True and r1["mix_exists"] is True
    assert r1["music_missing"] is False
    assert r1["assignment"]["music"] == "t1.mp3" and r1["assignment"]["score"] == 3

    r2 = rows["ch2"]
    assert r2["narration_exists"] is True  # the .wav fallback counts
    assert r2["mix_exists"] is False
    assert r2["music_missing"] is True     # ⚠ 已删除

    r3 = rows["ch3"]
    assert r3["narration_exists"] is True  # fixture seed
    assert r3["mix_exists"] is False
    assert r3["assignment"]["music"] == "t1.mp3" and r3["music_missing"] is False
    assert res["mode"] == "random"


def test_chapters_legacy_llm_mode_reads_as_random(workspace):
    # 章节级 "llm" 模式已下线：存量数据持久化的旧 mode 值读取时归一为 random。
    layout = core_paths.get_or_prepare_layout()
    data = bgm_engine.load_assignments(layout)
    data["mode"] = "llm"
    bgm_engine.save_assignments(layout, data)
    assert api_bgm.list_chapters()["mode"] == "random"


def test_chapters_segment_row_fields(workspace):
    # segment_analysis: null / fresh (stale=False) / stale (03 deleted after the
    # analysis — the fingerprint must mismatch); a fresh cached timeline remains
    # viewable after a chapter-mode assignment reset, while stale leftovers stay
    # hidden; segment_music_missing = a span's track left the library.
    layout = core_paths.get_or_prepare_layout()
    e1 = _seed_scripts(workspace, ["ch1"])["ch1"]
    e3 = _seed_scripts(workspace, ["ch3"])["ch3"]
    _seed_segment_analysis("ch1", e1, [_segment_block(0, 0, mood=["紧张"])])
    _write_timeline("ch1", [{"start": 0.0, "end": 10.0, "music_id": "t1.mp3",
                             "intensity": 2, "volume": 0.18, "tags": {},
                             "score": 3, "reason": "r"}], duration=14.25)
    # ch3: the 03 script is deleted AFTER the analysis → stale; a leftover
    # timeline file must NOT surface (the entry is not segment-marked)
    _seed_segment_analysis("ch3", e3, [_segment_block(0, 0)])
    (workspace / "03_parsed_json" / "ch3.json").unlink()
    _write_timeline("ch3", [{"start": 0.0, "end": 5.0, "music_id": "t1.mp3",
                             "intensity": 2, "volume": 0.18, "tags": {},
                             "score": 3, "reason": "r"}], duration=9.0)
    # ch5: segment entry + a timeline spanning a track that is not in the library
    data = bgm_engine.load_assignments(layout)
    for s in ("ch1", "ch5"):
        data["chapters"][s] = {"tags": {}, "music": None, "segment": s == "ch5",
                               "locked": False, "manual": False, "score": None,
                               "reason": "r", "matched_at": "t0"}
    bgm_engine.save_assignments(layout, data)
    _write_timeline("ch5", [{"start": 0.0, "end": 8.0, "music_id": "ghost.mp3",
                             "intensity": 2, "volume": 0.18, "tags": {},
                             "score": 0, "reason": "r"}], duration=9.0)

    rows = {r["stem"]: r for r in api_bgm.list_chapters()["chapters"]}
    r1 = rows["ch1"]
    assert r1["segment_analysis"] == {
        "analyzed_at": "t0", "entry_count": 6, "stale": False,
        "tags": {"scene": [], "mood": ["紧张"], "emotion": [], "custom": []},
    }
    assert r1["timeline"] == {"sections": 1, "duration": 14.25,
                              "generated_at": "t0"}
    assert r1["segment_music_missing"] is False
    assert r1["assignment"]["segment"] is False

    r3 = rows["ch3"]
    assert r3["segment_analysis"]["stale"] is True  # 03 已删 → 指纹必不匹配
    assert r3["timeline"] is None  # 遗留时间轴惰性（entry 非 segment）
    assert r3["segment_music_missing"] is False

    r5 = rows["ch5"]
    assert r5["segment_analysis"] is None
    assert r5["timeline"] is not None
    assert r5["segment_music_missing"] is True  # ghost.mp3 已出库

    r2 = rows["ch2"]
    assert r2["segment_analysis"] is None and r2["timeline"] is None
    assert r2["segment_music_missing"] is False


def test_chapters_no_workspace_empty(monkeypatch, tmp_path):
    # Read-only endpoint: no workspace -> empty rows (degrade, never 409).
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        assert api_bgm.list_chapters() == {"chapters": [], "mode": "random"}
    finally:
        core_config.reset_config_cache()


# -- GET /timeline/{stem} ----------------------------------------------------------

def test_timeline_endpoint(workspace):
    _write_timeline("ch1", [{"start": 0.0, "end": 10.0, "music_id": "t1.mp3",
                             "intensity": 2, "volume": 0.18, "tags": {},
                             "score": 3, "reason": "r"}], duration=14.25)
    res = api_bgm.get_timeline("ch1")
    assert res["timeline"]["version"] == 2
    assert res["timeline"]["stem"] == "ch1"
    assert res["timeline"]["duration"] == 14.25
    assert len(res["timeline"]["timeline"]) == 1
    sp = res["timeline"]["timeline"][0]
    # v2 三个描述字段原样往返（_write_timeline 缺省空串）
    assert sp["scene_desc"] == "" and sp["mood_desc"] == ""
    assert sp["switch_reason"] == ""

    with pytest.raises(HTTPException) as e:
        api_bgm.get_timeline("ch2")
    assert e.value.status_code == 404
    assert "该章没有时间轴" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.get_timeline("../evil")
    assert e.value.status_code == 400
    assert "非法章节名" in e.value.detail


def test_timeline_no_workspace(monkeypatch, tmp_path):
    # Read-only endpoint: no workspace -> {"timeline": None} (degrade, never 409).
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        assert api_bgm.get_timeline("ch1") == {"timeline": None}
    finally:
        core_config.reset_config_cache()


# -- PUT /chapters/{stem} -------------------------------------------------------------

def test_update_chapter_music_and_lock(workspace):
    # manual pick
    e = api_bgm.update_chapter(
        "ch1", api_bgm.ChapterUpdateRequest(music="t1.mp3", locked=True))
    assert e["music"] == "t1.mp3" and e["manual"] is True and e["locked"] is True
    assert e["score"] is None and e["reason"] == "手动指定" and e["matched_at"]
    # clear (music present, value null)
    e2 = api_bgm.update_chapter("ch1", api_bgm.ChapterUpdateRequest(music=None))
    assert e2["music"] is None and e2["manual"] is True and e2["reason"] == "手动指定"
    assert e2["locked"] is True  # the lock survived (key absent)
    # an unmatched chapter gets an entry created on demand
    layout = core_paths.get_or_prepare_layout()
    data = bgm_engine.load_assignments(layout)
    data["chapters"].pop("ch3")
    bgm_engine.save_assignments(layout, data)
    e3 = api_bgm.update_chapter("ch3", api_bgm.ChapterUpdateRequest(music="t1.mp3"))
    assert e3["music"] == "t1.mp3" and e3["manual"] is True and e3["locked"] is False


def test_update_chapter_guard_400s(workspace):
    with pytest.raises(HTTPException) as e:
        api_bgm.update_chapter("../evil", api_bgm.ChapterUpdateRequest(locked=True))
    assert e.value.status_code == 400

    with pytest.raises(HTTPException) as e:
        api_bgm.update_chapter("ch1",
                               api_bgm.ChapterUpdateRequest(music="a/b.mp3"))
    assert e.value.status_code == 400
    assert "非法音乐文件名" in e.value.detail

    with pytest.raises(HTTPException) as e:
        api_bgm.update_chapter("ch1",
                               api_bgm.ChapterUpdateRequest(music="ghost.mp3"))
    assert e.value.status_code == 400
    assert "音乐库中找不到 ghost.mp3" in e.value.detail
    # a failed update must not have touched anything
    e0 = bgm_engine.load_assignments(core_paths.get_or_prepare_layout())["chapters"]["ch1"]
    assert e0["music"] == "t1.mp3" and e0["reason"] == "r"


def test_write_endpoints_no_workspace_409(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    monkeypatch.setattr(core_paths, "MUSIC_LIBRARY_DIR", tmp_path / "music_library")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}),
                                       encoding="utf-8")
    core_config.reset_config_cache()
    try:
        for call in (
            lambda: api_bgm.run_analyze_segment(api_bgm.SegmentAnalyzeRequest(chapters=["ch1"])),
            lambda: api_bgm.run_match(api_bgm.MatchRequest()),
            lambda: api_bgm.run_mix(api_bgm.MixRequest(chapters=["ch1"])),
            lambda: api_bgm.update_chapter("ch1", api_bgm.ChapterUpdateRequest(locked=True)),
        ):
            with pytest.raises(HTTPException) as e:
                call()
            assert e.value.status_code == 409
    finally:
        core_config.reset_config_cache()


def test_match_batch_submits_one_task_per_chapter(workspace, monkeypatch):
    created = _install_durable_mocks(monkeypatch)
    result = api_bgm.run_match(api_bgm.MatchRequest(chapters=["ch1", "ch2"], mode="random"), _api_context(), object())
    assert result["task_ids"] == ["task-1", "task-2"]
    assert [row["payload"]["chapters"] for row in created] == [["ch1"], ["ch2"]]
