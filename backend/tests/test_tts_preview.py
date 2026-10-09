"""Tests for the chapter preview (整章预览) endpoints: detail / line re-render /
staged audio / save-apply (gate, commit, rollback, downstream) / purge-stale."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.api import tts as api_tts
from backend.core import config as core_config
from backend.core import file_lock
from backend.core import paths as core_paths
from backend.engines import tts_batch as Batch
from backend.engines.merge import boundary_gap_ms


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    ws = tmp_path / "Book"
    ws.mkdir()
    core_config.set_workspace_pointer(str(ws))
    yield ws
    core_config.reset_config_cache()


def _ctx():
    return SimpleNamespace(user=SimpleNamespace(id="user-1"), session=object())


def _default_manifest(n_lines=3, pkg="s") -> list:
    entries = []
    for i in range(n_lines):
        entries.append({
            "index": i,
            "speaker": "A" if i % 2 == 0 else "B",
            "text": f"line {i}",
            "path": f"05_audio_chunk/{pkg}/{i + 1:04d}.mp3",
            "ok": True,
            "reason": "",
        })
    return entries


def _seed(ws, name="s.json", pkg="s", n_lines=3, *, manifest=None,
          audio=None, voice_config=None, merged=False, mixed=False, timeline=False):
    """A chapter on disk: 03 script + optional 05 files / manifest + optional 06 / 08 artifacts."""
    (ws / "03_parsed_json").mkdir(parents=True, exist_ok=True)
    lines = [{"speaker": "A" if i % 2 == 0 else "B", "text": f"line {i}", "instruct": ""}
             for i in range(n_lines)]
    (ws / "03_parsed_json" / name).write_text(
        json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    pkg_dir = ws / "05_audio_chunk" / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for fname in audio or ():
        (pkg_dir / fname).write_bytes(b"A" * 16)
    if manifest is not None:
        (pkg_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    if voice_config is not None:
        (ws / "04_voice_profiles").mkdir(parents=True, exist_ok=True)
        (ws / "04_voice_profiles" / "voice_config.json").write_text(
            json.dumps(voice_config, ensure_ascii=False), encoding="utf-8")
    if merged:
        (ws / "06_audio_merge").mkdir(parents=True, exist_ok=True)
        (ws / "06_audio_merge" / f"{pkg}.mp3").write_bytes(b"M" * 8)
    if mixed:
        (ws / "08_bgm").mkdir(parents=True, exist_ok=True)
        (ws / "08_bgm" / f"{pkg}.mp3").write_bytes(b"X" * 8)
    if timeline:
        (ws / "08_bgm" / "timelines").mkdir(parents=True, exist_ok=True)
        (ws / "08_bgm" / "timelines" / f"{pkg}.json").write_text("{}", encoding="utf-8")


def _seed_staged(ws, pkg, index, *, ok=True, file_name=None, triple=("t", "s", "i"), reason=""):
    """One staged state.json line (+ the produced file on disk) — merges into existing state."""
    root = ws / "00_temp" / "chapter_preview" / pkg
    root.mkdir(parents=True, exist_ok=True)
    if file_name:
        (root / file_name).write_bytes(b"S" * 16)
    state_path = root / "state.json"
    state = (json.loads(state_path.read_text("utf-8"))
             if state_path.exists()
             else {"script": f"{pkg}.json", "updated_at": 1.0, "lines": {}})
    state["lines"][str(index)] = {
        "ok": ok,
        "reason": reason,
        "file": file_name or "",
        "text": triple[0],
        "speaker": triple[1],
        "instruct": triple[2],
        "rendered_at": "2026-01-01T00:00:00",
        "fingerprint": "fp",
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _no_conflicts(monkeypatch):
    monkeypatch.setattr(api_tts, "active_durable_payloads", lambda **kw: [])
    monkeypatch.setattr(api_tts, "active_durable_targets", lambda **kw: set())


# ---------------------------------------------------------------------------
# GET /preview/chapter/{name}
# ---------------------------------------------------------------------------

def test_preview_chapter_detail_fields(workspace, monkeypatch):
    _seed(workspace, manifest=_default_manifest(), audio=("0001.mp3", "0002.mp3", "0003.mp3"),
          merged=True, mixed=True, timeline=True)
    monkeypatch.setattr(api_tts, "probe_duration", lambda path, ffprobe_path="", timeout=120.0: (74.3, ""))

    detail = api_tts.preview_chapter("s.json")

    assert detail["name"] == "s.json"
    assert detail["package"] == "s"
    assert len(detail["lines"]) == 3
    line0 = detail["lines"][0]
    assert line0["ok"] is True
    assert line0["audio"] == "05_audio_chunk/s/0001.mp3"
    assert line0["audio_mtime_ns"] is not None
    assert line0["staged"] is None
    # 单句真实时长（ffprobe 恒 74.3）——与 chapter_audio 解耦，可试听句也回传。
    assert all(line["duration"] == 74.3 for line in detail["lines"])
    # The backend never computes a preview_ready field — the frontend derives it.
    assert "preview_ready" not in detail
    assert detail["chapter_audio"] == {"path": "06_audio_merge/s.mp3", "duration": 74.3}
    # start_offset 按 merge 口径累加：probe 恒 74.3s，A→B / B→A 均为换人 gap。
    cfg = core_config.get_config()
    pause_ms = cfg.tts.pause_between_speakers_ms or 500
    same_ms = cfg.tts.pause_same_speaker_ms or 250
    assert detail["lines"][0]["start_offset"] == 0.0
    assert detail["lines"][1]["start_offset"] == pytest.approx(
        74.3 + boundary_gap_ms(None, "A", "B", pause_ms, same_ms) / 1000.0)
    assert detail["lines"][2]["start_offset"] == pytest.approx(
        2 * 74.3 + (boundary_gap_ms(None, "A", "B", pause_ms, same_ms)
                    + boundary_gap_ms(None, "B", "A", pause_ms, same_ms)) / 1000.0)
    assert detail["timeline_exists"] is True
    assert detail["downstream"] == {"merged": True, "mixed": True, "timeline": True,
                                     "segment_stale": False}


def test_preview_chapter_duration_cached_by_mtime(workspace, monkeypatch):
    """ffprobe 按 (path, mtime) 缓存：同文件重拉零新 probe；mtime 变化才重算。"""
    _seed(workspace, manifest=_default_manifest(),
          audio=("0001.mp3", "0002.mp3", "0003.mp3"), merged=True)
    calls = []

    def fake(path, ffprobe_path="", timeout=120.0):
        calls.append(str(path))
        return (10.0, "")

    monkeypatch.setattr(api_tts, "probe_duration", fake)

    d1 = api_tts.preview_chapter("s.json")
    first = len(calls)  # 章节 1 次 + 3 句
    assert first == 4
    assert d1["lines"][0]["duration"] == 10.0

    api_tts.preview_chapter("s.json")
    assert len(calls) == first  # 全命中缓存，零新 probe

    p = workspace / "05_audio_chunk" / "s" / "0001.mp3"
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # 显式改 mtime，防同刻写入
    calls.clear()
    d2 = api_tts.preview_chapter("s.json")
    assert len(calls) == 1  # 仅 mtime 变的那句（0001）重 probe
    assert d2["lines"][0]["duration"] == 10.0
    assert d2["lines"][1]["duration"] == 10.0  # 0002 命中缓存


def test_preview_chapter_line_without_audio_is_not_ok(workspace, monkeypatch):
    _seed(workspace, manifest=_default_manifest(), audio=("0001.mp3", "0002.mp3"))
    # 未合并（无 06）仍要 ffprobe 单句时长——必须 stub，避免测试起真实子进程依赖本机 ffprobe。
    monkeypatch.setattr(api_tts, "probe_duration", lambda path, ffprobe_path="", timeout=120.0: (2.5, ""))

    detail = api_tts.preview_chapter("s.json")

    assert detail["lines"][2]["ok"] is False
    assert detail["lines"][2]["audio"] == ""
    assert detail["chapter_audio"] is None
    assert detail["downstream"]["merged"] is False
    # 无 06 → 没有章节时间轴，所有句的起点一律 null。
    assert all(line["start_offset"] is None for line in detail["lines"])
    # B 项独立于章节合并：有 05 的句有单句时长，无 05 的句为 None。
    assert detail["lines"][0]["duration"] == 2.5
    assert detail["lines"][1]["duration"] == 2.5
    assert detail["lines"][2]["duration"] is None


def test_preview_chapter_start_offset_skips_missing_and_respects_pause_after(workspace, monkeypatch):
    """缺失 05 的句子被跳过（无起点、不贡献时长与 gap）；pause_after 覆盖说话人规则。"""
    _seed(workspace, manifest=_default_manifest(), audio=("0001.mp3", "0003.mp3"), merged=True)
    lines = json.loads((workspace / "03_parsed_json" / "s.json").read_text("utf-8"))
    lines[0]["pause_after"] = 1000  # line0(A) 的句尾停顿覆盖 line0→line2 的间隔
    (workspace / "03_parsed_json" / "s.json").write_text(
        json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(api_tts, "probe_duration", lambda path, ffprobe_path="", timeout=120.0: (10.0, ""))

    detail = api_tts.preview_chapter("s.json")

    assert detail["chapter_audio"] is not None
    assert detail["lines"][0]["start_offset"] == 0.0
    assert detail["lines"][1]["start_offset"] is None  # 05 文件缺失 → 无起点
    # line1 被跳过：line2 起点 = line0 时长 + line0 的 pause_after（1000ms 覆盖同人 250ms）
    assert detail["lines"][2]["start_offset"] == pytest.approx(11.0)


def test_preview_chapter_includes_staged_state(workspace):
    _seed(workspace, manifest=_default_manifest(), audio=("0001.mp3",))
    _seed_staged(workspace, "s", 0, file_name="0001.mp3", triple=("line 0改", "B", "愤怒地"))

    detail = api_tts.preview_chapter("s.json")

    assert detail["lines"][0]["staged"] == {
        "ok": True, "reason": "", "text": "line 0改", "speaker": "B", "instruct": "愤怒地",
        "rendered_at": "2026-01-01T00:00:00", "fingerprint": "fp", "file": "0001.mp3",
    }
    assert detail["lines"][1]["staged"] is None


def test_preview_chapter_name_validation(workspace):
    with pytest.raises(HTTPException) as e:
        api_tts.preview_chapter("../evil.json")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        api_tts.preview_chapter("ghost.json")
    assert e.value.status_code == 404


def test_preview_chapter_no_workspace_degrades(monkeypatch, tmp_path):
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8")
    core_config.reset_config_cache()
    try:
        detail = api_tts.preview_chapter("s.json")
        assert detail["lines"] == []
        assert detail["chapter_audio"] is None
        assert detail["downstream"]["merged"] is False
    finally:
        core_config.reset_config_cache()


# ---------------------------------------------------------------------------
# POST /preview/line-rerender
# ---------------------------------------------------------------------------

def test_line_rerender_submits_staged_render_task(workspace, monkeypatch):
    _seed(workspace)
    _no_conflicts(monkeypatch)
    submitted = []
    monkeypatch.setattr(
        api_tts, "submit_legacy_engine_task",
        lambda **kw: (submitted.append(kw), {"id": "task-1"})[1])

    res = api_tts.preview_line_rerender(
        api_tts.PreviewLineRerenderRequest(script="s.json", index=0, text="line 0改", speaker="B"),
        ctx=_ctx(), db=object(),
    )

    assert res == {"task_id": "task-1"}
    call = submitted[0]
    assert call["task_type"] == "tts.preview_render"
    # 全角冒号契约：任务中心/页面按 {script}·第N句 解析 label key
    assert call["label"] == "整章预览·单句重渲染：s.json·第1句"
    assert call["idempotency_prefix"] == "tts-preview"
    payload = call["payload"]
    assert payload["script"] == "s.json"
    assert payload["index"] == 0
    # render 为数组；只携带被修改的字段
    assert payload["render"] == [{"index": 0, "text": "line 0改", "speaker": "B"}]


def test_line_rerender_rejects_out_of_range_and_empty_text(workspace):
    _seed(workspace)
    for req in (
        api_tts.PreviewLineRerenderRequest(script="s.json", index=99),
        api_tts.PreviewLineRerenderRequest(script="s.json", index=0, text="   "),
    ):
        with pytest.raises(HTTPException) as e:
            api_tts.preview_line_rerender(req, ctx=_ctx(), db=object())
        assert e.value.status_code == 400


def test_line_rerender_conflicts(workspace, monkeypatch):
    _seed(workspace)
    cases = [
        # 在途 tts.batch（scripts 在列表中 → 必须用 payloads 版本检查）
        (lambda **kw: [{"scripts": ["s.json"]}], lambda **kw: set()),
        # 在途同章 preview_render
        (lambda **kw: [],
         lambda **kw: {"s.json"} if kw.get("task_type") == "tts.preview_render" else set()),
        # 在途同包 tts.merge
        (lambda **kw: [],
         lambda **kw: {"s"} if kw.get("task_type") == "tts.merge" else set()),
    ]
    for payloads_fn, targets_fn in cases:
        monkeypatch.setattr(api_tts, "active_durable_payloads", payloads_fn)
        monkeypatch.setattr(api_tts, "active_durable_targets", targets_fn)
        monkeypatch.setattr(api_tts, "submit_legacy_engine_task",
                            lambda **kw: pytest.fail("must not submit"))
        with pytest.raises(HTTPException) as e:
            api_tts.preview_line_rerender(
                api_tts.PreviewLineRerenderRequest(script="s.json", index=0),
                ctx=_ctx(), db=object(),
            )
        assert e.value.status_code == 409


# ---------------------------------------------------------------------------
# GET /preview/audio/{name}
# ---------------------------------------------------------------------------

def test_preview_audio_serves_staged_file(workspace):
    root = workspace / "00_temp" / "chapter_preview" / "s"
    root.mkdir(parents=True)
    (root / "0001.mp3").write_bytes(b"M" * 16)

    resp = api_tts.preview_audio("s/0001.mp3", SimpleNamespace(headers={}))

    assert resp.media_type == "audio/mpeg"


def test_preview_audio_rejects_traversal_bad_ext_and_missing(workspace):
    root = workspace / "00_temp" / "chapter_preview" / "s"
    root.mkdir(parents=True)
    (root / "state.json").write_text("{}", encoding="utf-8")
    for name in ("../escape.mp3", "s/../escape.mp3", "s/state.json"):
        with pytest.raises(HTTPException) as e:
            api_tts.preview_audio(name, SimpleNamespace(headers={}))
        assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        api_tts.preview_audio("s/0001.mp3", SimpleNamespace(headers={}))
    assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# POST /preview/apply
# ---------------------------------------------------------------------------

def _seed_apply_scene(ws, *, voice_config=None, staged_triples=None,
                      audio=("0001.mp3", "0002.mp3", "0003.mp3"), merged=True,
                      mixed=True, timeline=True):
    """Full save scene: chapter + downstream artifacts + other-chapter artifacts + staged state."""
    _seed(ws, manifest=_default_manifest(), audio=audio, voice_config=voice_config,
          merged=merged, mixed=mixed, timeline=timeline)
    # 其他章节产物（保存不得触碰）
    (ws / "06_audio_merge" / "other.mp3").write_bytes(b"O")
    (ws / "08_bgm" / "other.mp3").write_bytes(b"O")
    for index, (file_name, triple) in (staged_triples or {}).items():
        _seed_staged(ws, "s", index, file_name=file_name, triple=triple)


def test_apply_commit_replaces_manifest_script_and_invalidates(workspace, monkeypatch):
    voice_config = {
        "A": {"type": "foundation", "ref_audio": "a.wav", "ref_text": "ref",
              "description": "", "voice": "", "instruct": ""},
        "B": {"type": "foundation", "ref_audio": "b.wav", "ref_text": "ref",
              "description": "", "voice": "", "instruct": ""},
    }
    # 第 1 句（index 0）换角色 A→B（wav 回退产物）；第 2 句（index 1）只改 instruct（部分三元组）
    _seed_apply_scene(
        workspace, voice_config=voice_config,
        staged_triples={0: ("0001.wav", ("line 0", "B", "")),
                        1: ("0002.mp3", ("line 1", "B", "悄悄地说"))},
    )
    _no_conflicts(monkeypatch)
    before_03 = (workspace / "03_parsed_json" / "s.json").read_bytes()
    before_other = (workspace / "08_bgm" / "other.mp3").read_bytes()
    old_manifest_bytes = (workspace / "05_audio_chunk" / "s" / "manifest.json").read_bytes()

    req = api_tts.PreviewApplyRequest(
        script="s.json",
        edits=[
            api_tts.PreviewEdit(index=0, speaker="B"),
            api_tts.PreviewEdit(index=1, instruct="悄悄地说"),
        ],
    )
    res = api_tts.apply_preview_edits(req, ctx=_ctx(), db=object())

    assert res["ok"] is True
    assert res["edited"] == [0, 1]
    assert res["invalidated"] == ["merged", "mixed", "timeline"]
    assert res["failures"] == []
    assert res["downstream_dirty"] is False

    # 05 替换：wav 产物按 .wav 名落位，旧 .mp3 被清掉；未改行（0003）不动
    pkg_dir = workspace / "05_audio_chunk" / "s"
    assert (pkg_dir / "0001.wav").read_bytes() == b"S" * 16
    assert not (pkg_dir / "0001.mp3").exists()
    assert (pkg_dir / "0002.mp3").read_bytes() == b"S" * 16
    assert (pkg_dir / "0003.mp3").read_bytes() == b"A" * 16

    # manifest：被改行更新（path 随实际落位 + voice_used 随新角色），voice_versions 保留，
    # 未改行原样
    manifest = json.loads((pkg_dir / "manifest.json").read_text("utf-8"))
    by_index = {e["index"]: e for e in manifest}
    assert by_index[0]["path"] == "05_audio_chunk/s/0001.wav"
    assert by_index[0]["speaker"] == "B" and by_index[0]["ok"] is True
    assert by_index[0]["voice_used"] == Batch.voice_params("B", voice_config)
    assert by_index[1]["path"] == "05_audio_chunk/s/0002.mp3"
    assert by_index[1]["speaker"] == "B"
    assert by_index[2]["path"] == "05_audio_chunk/s/0003.mp3"
    assert by_index[2]["ok"] is True

    # 03 字段级更新（只动被改字段）
    lines = json.loads((workspace / "03_parsed_json" / "s.json").read_text("utf-8"))
    assert lines[0]["speaker"] == "B" and lines[0]["text"] == "line 0"
    assert lines[1]["instruct"] == "悄悄地说" and lines[1]["text"] == "line 1"
    assert lines[2] == {"speaker": "A", "text": "line 2", "instruct": ""}

    # 本章节下游被删；其他章节产物逐字节不动
    assert not (workspace / "06_audio_merge" / "s.mp3").exists()
    assert not (workspace / "08_bgm" / "s.mp3").exists()
    assert not (workspace / "08_bgm" / "timelines" / "s.json").exists()
    assert (workspace / "06_audio_merge" / "other.mp3").read_bytes() == before_other
    assert (workspace / "08_bgm" / "other.mp3").read_bytes() == before_other

    # 备份与暂存都被清理
    assert not (workspace / "00_temp" / "preview_apply_backup").exists()
    assert not (workspace / "00_temp" / "chapter_preview" / "s").exists()


def test_apply_preserves_voice_versions(workspace, monkeypatch):
    voice_config = {"A": {"type": "foundation", "ref_audio": "a.wav", "ref_text": "",
                          "description": "", "voice": "", "instruct": ""},
                    "B": {"type": "foundation", "ref_audio": "b.wav", "ref_text": "",
                          "description": "", "voice": "", "instruct": ""}}
    _seed_apply_scene(
        workspace, voice_config=voice_config,
        staged_triples={0: ("0001.mp3", ("line 0", "B", ""))},
    )
    manifest_path = workspace / "05_audio_chunk" / "s" / "manifest.json"
    entries = _default_manifest()
    entries[0]["voice_versions"] = ["v-old-1", "v-old-2"]
    entries[0]["voice_used"] = Batch.voice_params("A", voice_config)
    entries[0]["voice_signature"] = Batch.voice_signature("A", voice_config)
    manifest_path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    _no_conflicts(monkeypatch)

    res = api_tts.apply_preview_edits(
        api_tts.PreviewApplyRequest(
            script="s.json", edits=[api_tts.PreviewEdit(index=0, speaker="B")]),
        ctx=_ctx(), db=object(),
    )
    assert res["ok"] is True
    entry0 = {e["index"]: e for e in json.loads(manifest_path.read_text("utf-8"))}[0]
    assert entry0["voice_versions"] == ["v-old-1", "v-old-2"]
    assert entry0["voice_used"] == Batch.voice_params("B", voice_config)


def test_apply_gate_rejects_missing_or_mismatched_staged(workspace, monkeypatch):
    voice_config = {"B": {"type": "foundation", "ref_audio": "b.wav", "ref_text": "",
                          "description": "", "voice": "", "instruct": ""}}
    _seed_apply_scene(workspace, voice_config=voice_config)  # 无暂存
    _no_conflicts(monkeypatch)
    edits = [api_tts.PreviewEdit(index=0, speaker="B")]

    # 修改句无暂存 → 409
    with pytest.raises(HTTPException) as e:
        api_tts.apply_preview_edits(
            api_tts.PreviewApplyRequest(script="s.json", edits=edits), ctx=_ctx(), db=object())
    assert e.value.status_code == 409
    assert "尚未完成重渲染" in e.value.detail

    # 暂存失败（ok=false）→ 409
    _seed_staged(workspace, "s", 0, ok=False, reason="超时（已隔离）", triple=("line 0", "B", ""))
    with pytest.raises(HTTPException) as e:
        api_tts.apply_preview_edits(
            api_tts.PreviewApplyRequest(script="s.json", edits=edits), ctx=_ctx(), db=object())
    assert e.value.status_code == 409

    # 暂存文件缺失 → 409
    _seed_staged(workspace, "s", 0, triple=("line 0", "B", ""))
    with pytest.raises(HTTPException) as e:
        api_tts.apply_preview_edits(
            api_tts.PreviewApplyRequest(script="s.json", edits=edits), ctx=_ctx(), db=object())
    assert e.value.status_code == 409

    # 暂存与有效三元组不一致 → 409
    _seed_staged(workspace, "s", 0, file_name="0001.mp3", triple=("另一版台词", "B", ""))
    with pytest.raises(HTTPException) as e:
        api_tts.apply_preview_edits(
            api_tts.PreviewApplyRequest(script="s.json", edits=edits), ctx=_ctx(), db=object())
    assert e.value.status_code == 409

    # 匹配 → 通过（wav 回退产物同样过门禁）
    _seed_staged(workspace, "s", 0, file_name="0001.wav", triple=("line 0", "B", ""))
    res = api_tts.apply_preview_edits(
        api_tts.PreviewApplyRequest(script="s.json", edits=edits), ctx=_ctx(), db=object())
    assert res["ok"] is True


def test_apply_gate_effective_triple_with_partial_edits(workspace, monkeypatch):
    # edit 只改 speaker（部分三元组）：有效三元组 = 磁盘 03 的 text/instruct + 新 speaker
    _seed_apply_scene(workspace, staged_triples={1: ("0002.mp3", ("line 1", "A", "原语气"))})
    _no_conflicts(monkeypatch)
    res = api_tts.apply_preview_edits(
        api_tts.PreviewApplyRequest(
            script="s.json",
            edits=[api_tts.PreviewEdit(index=1, speaker="A", instruct="原语气")]),
        ctx=_ctx(), db=object(),
    )
    assert res["ok"] is True


def test_apply_manifest_failure_rolls_back_bytes(workspace, monkeypatch):
    _seed_apply_scene(workspace, staged_triples={0: ("0001.mp3", ("line 0", "B", ""))})
    _no_conflicts(monkeypatch)
    before = {
        "script": (workspace / "03_parsed_json" / "s.json").read_bytes(),
        "manifest": (workspace / "05_audio_chunk" / "s" / "manifest.json").read_bytes(),
        "0001": (workspace / "05_audio_chunk" / "s" / "0001.mp3").read_bytes(),
    }

    def _boom(manifest_path, manifest, handle=None):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(Batch, "write_manifest_file", _boom)
    with pytest.raises(HTTPException) as e:
        api_tts.apply_preview_edits(
            api_tts.PreviewApplyRequest(
                script="s.json", edits=[api_tts.PreviewEdit(index=0, speaker="B")]),
            ctx=_ctx(), db=object(),
        )
    assert e.value.status_code == 500
    assert e.value.detail["stage"] == "update_manifest"

    # 字节级还原：05 / manifest / 03 与保存前一致
    pkg_dir = workspace / "05_audio_chunk" / "s"
    assert (workspace / "03_parsed_json" / "s.json").read_bytes() == before["script"]
    assert (pkg_dir / "manifest.json").read_bytes() == before["manifest"]
    assert (pkg_dir / "0001.mp3").read_bytes() == before["0001"]
    # 备份已清理；暂存保留（可重试）
    assert not (workspace / "00_temp" / "preview_apply_backup").exists()
    assert (workspace / "00_temp" / "chapter_preview" / "s" / "state.json").exists()


def test_apply_replace_failure_rolls_back(workspace, monkeypatch):
    _seed_apply_scene(workspace, staged_triples={0: ("0001.mp3", ("line 0", "B", ""))})
    _no_conflicts(monkeypatch)
    before = (workspace / "05_audio_chunk" / "s" / "0001.mp3").read_bytes()

    real_copy2 = api_tts.shutil.copy2

    def _copy2(src, dst, *a, **kw):
        raise OSError("磁盘故障")

    monkeypatch.setattr(api_tts.shutil, "copy2", _copy2)
    with pytest.raises(HTTPException) as e:
        api_tts.apply_preview_edits(
            api_tts.PreviewApplyRequest(
                script="s.json", edits=[api_tts.PreviewEdit(index=0, speaker="B")]),
            ctx=_ctx(), db=object(),
        )
    assert e.value.status_code == 500
    assert e.value.detail["stage"] == "replace_05"
    assert (workspace / "05_audio_chunk" / "s" / "0001.mp3").read_bytes() == before
    monkeypatch.setattr(api_tts.shutil, "copy2", real_copy2)


def test_apply_downstream_failure_reports_dirty_without_rollback(workspace, monkeypatch):
    _seed_apply_scene(workspace, staged_triples={0: ("0001.mp3", ("line 0", "B", ""))})
    _no_conflicts(monkeypatch)
    bad = MagicMock()
    bad.is_file.return_value = True
    bad.unlink.side_effect = OSError("权限不足")
    monkeypatch.setattr(api_tts.Batch, "merged_output_paths", lambda layout, pkg: [bad])

    res = api_tts.apply_preview_edits(
        api_tts.PreviewApplyRequest(
            script="s.json", edits=[api_tts.PreviewEdit(index=0, speaker="B")]),
        ctx=_ctx(), db=object(),
    )

    assert res["ok"] is True
    assert res["downstream_dirty"] is True
    assert res["failures"] == [{"stage": "downstream", "artifact": "merged",
                                "error": "权限不足"}]
    # 保存已成立：03 已更新；暂存保留供排查
    lines = json.loads((workspace / "03_parsed_json" / "s.json").read_text("utf-8"))
    assert lines[0]["speaker"] == "B"
    assert (workspace / "00_temp" / "chapter_preview" / "s" / "state.json").exists()


def test_apply_mutually_exclusive_with_chapter_lock(workspace, monkeypatch):
    _seed_apply_scene(workspace, staged_triples={0: ("0001.mp3", ("line 0", "B", ""))})
    _no_conflicts(monkeypatch)
    monkeypatch.setattr(api_tts, "PREVIEW_LOCK_TIMEOUT", 0.2)  # 生产 5s：测的是“锁被占即 409”，不必真等
    layout = core_paths.get_or_prepare_layout()
    lock_path = Batch.preview_lock_path(layout, "s")

    with file_lock.exclusive_file_lock(lock_path):
        with pytest.raises(HTTPException) as e:
            api_tts.apply_preview_edits(
                api_tts.PreviewApplyRequest(
                    script="s.json", edits=[api_tts.PreviewEdit(index=0, speaker="B")]),
                ctx=_ctx(), db=object(),
            )
        assert e.value.status_code == 409


# ---------------------------------------------------------------------------
# purge-stale + /merge 锁探测
# ---------------------------------------------------------------------------

def test_purge_stale_retries_deletions(workspace, monkeypatch):
    _seed(workspace, manifest=_default_manifest(), merged=True, mixed=True, timeline=True)
    _no_conflicts(monkeypatch)

    res = api_tts.purge_preview_stale(
        api_tts.PreviewPurgeRequest(script="s.json"), ctx=_ctx(), db=object())

    assert res["ok"] is True
    assert res["invalidated"] == ["merged", "mixed", "timeline"]
    assert res["downstream_dirty"] is False
    assert not (workspace / "06_audio_merge" / "s.mp3").exists()


def test_merge_submission_rejected_while_chapter_lock_held(workspace, monkeypatch):
    _seed(workspace)
    _no_conflicts(monkeypatch)
    monkeypatch.setattr(api_tts, "submit_legacy_engine_task",
                        lambda **kw: pytest.fail("must not submit"))
    monkeypatch.setattr(api_tts, "submit_merge_tasks", lambda **kwargs: kwargs["preflight"](kwargs["packages"]))
    layout = core_paths.get_or_prepare_layout()
    lock_path = Batch.preview_lock_path(layout, "s")

    with file_lock.exclusive_file_lock(lock_path):
        with pytest.raises(HTTPException) as e:
            api_tts.run_merge(api_tts.MergeRequest(packages=["s"]), ctx=_ctx(), db=object())
    assert e.value.status_code == 409
