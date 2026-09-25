"""Offline tests for the config model changes backing the voice / batch / merge
stages (``backend/core/config.py``) — the new TTS fields, the persona-prompt block,
and the pure deep-merge that ``update_config`` relies on.

``update_config`` itself writes the real ``config/app.json`` and is therefore exercised
in the manual run, not here; instead this pins the schema defaults and the pure
``_deep_update`` merge in isolation.
"""
from __future__ import annotations

import json

import pytest

from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core.config import (
    AppConfig,
    BGMConfig,
    GenerationConfig,
    TTSConfig,
    UIConfig,
    _deep_update,
)


# --------------------------------------------------------------------------- #
# TTSConfig defaults
# --------------------------------------------------------------------------- #

def test_tts_config_model_ids():
    t = TTSConfig()
    assert t.model == "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    assert t.base_model == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    assert t.design_model == "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"


def test_tts_config_pause_defaults():
    t = TTSConfig()
    assert t.pause_between_speakers_ms == 500
    assert t.pause_same_speaker_ms == 250


def test_tts_config_batch_concurrency_default():
    assert TTSConfig().batch_concurrency == 80


def test_retired_tts_settings_are_dropped_from_app_config():
    config = AppConfig.model_validate({
        "tts": {
            "api_base": "https://legacy.example/v1",
            "api_key": "preserve-user-value",
            "voice": "legacy-voice",
            "concurrency": 3,
            "parallel_workers": 4,
        },
        "persona_prompts": {"advanced_prompt": "user-authored prompt"},
    })

    restored = AppConfig.model_validate(config.model_dump())
    retired_tts = {"api_base", "api_key", "voice", "concurrency", "parallel_workers"}
    assert not retired_tts & restored.tts.model_dump().keys()
    assert "advanced_prompt" not in restored.persona_prompts.model_dump()


# --------------------------------------------------------------------------- #
# GenerationConfig: in-parse check toggles (migrated off the retired check sections)
# --------------------------------------------------------------------------- #

def test_generation_config_check_stage_defaults():
    # 断句失败校验 / 纯归属标签条清理 / 角色匹配检查 default ON (existing behavior
    # unchanged); the re-judgment batch geometry moved here from the deleted
    # ``speaker_check`` section.
    g = GenerationConfig()
    assert g.revalidate_splits is True
    assert g.delete_saying_tags is True
    assert g.check_boundary_speakers is True
    assert g.check_batch_size == 20
    assert g.check_context_window == 4
    assert g.spot_check_rate == 0.05
    # 超长段落检查（LLM 重切 + 机械分段兜底）/ 纯标点条目吸收 default ON + 200 字硬上限
    assert g.check_long_paragraphs is True
    assert g.max_paragraph_chars == 200
    assert g.absorb_punct_entries is True
    # 同人段落合并（机械后处理，置于超长机械分段之前）default ON
    assert g.merge_same_speaker is True
    # round-trip（自定义值不丢）
    g2 = GenerationConfig(**json.loads(
        json.dumps(g.model_dump(), ensure_ascii=False)))
    assert g2.check_long_paragraphs is g.check_long_paragraphs
    assert g2.max_paragraph_chars == g.max_paragraph_chars
    assert g2.absorb_punct_entries is g.absorb_punct_entries
    assert g2.merge_same_speaker is g.merge_same_speaker


# --------------------------------------------------------------------------- #
# AppConfig: persona prompts + round-trip
# --------------------------------------------------------------------------- #

def test_app_config_includes_persona_prompts():
    cfg = AppConfig()
    assert cfg.persona_prompts.system_prompt == ""
    assert cfg.persona_prompts.user_prompt == ""


def test_app_config_round_trips():
    data = AppConfig().model_dump()
    back = AppConfig.model_validate(data)
    assert back.tts.pause_between_speakers_ms == 500
    assert back.tts.design_model == "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    assert back.persona_prompts is not None


# --------------------------------------------------------------------------- #
# UIConfig: 解析日志显示开关（解析页日志区显隐 + 三指标位置）
# --------------------------------------------------------------------------- #

def test_ui_config_show_parse_logs_default_off():
    # 默认关：解析页隐藏「解析进度」日志区，三指标移到「开始处理」按钮下方。
    u = UIConfig()
    assert u.show_parse_logs is False
    assert u.theme == "system"


def test_ui_config_round_trips_show_parse_logs():
    data = AppConfig().model_dump()
    data["ui"]["show_parse_logs"] = True
    back = AppConfig.model_validate(data)
    assert back.ui.show_parse_logs is True
    # 再次落盘/重读不丢字段（schema 稳定）。
    again = AppConfig.model_validate(back.model_dump())
    assert again.ui.show_parse_logs is True


def test_ui_config_missing_field_falls_back_to_default():
    # 旧工作空间配置缺该字段 → Pydantic 默认值填充（False），读取链不报错。
    data = AppConfig().model_dump()
    del data["ui"]["show_parse_logs"]
    cfg = AppConfig.model_validate(data)
    assert cfg.ui.show_parse_logs is False


# --------------------------------------------------------------------------- #
# UIConfig: 音频分集导航项显隐开关（侧边栏「音频分集」项）
# --------------------------------------------------------------------------- #

def test_ui_config_show_audio_split_default_off():
    # 默认关：侧边栏隐藏「音频分集」导航项（页面路由保留，仍可直访）。
    u = UIConfig()
    assert u.show_audio_split is False


def test_ui_config_round_trips_show_audio_split():
    data = AppConfig().model_dump()
    data["ui"]["show_audio_split"] = True
    back = AppConfig.model_validate(data)
    assert back.ui.show_audio_split is True
    # 再次落盘/重读不丢字段（schema 稳定）。
    again = AppConfig.model_validate(back.model_dump())
    assert again.ui.show_audio_split is True


def test_ui_config_missing_show_audio_split_falls_back_to_default():
    # 旧工作空间配置缺该字段 → Pydantic 默认值填充（False），读取链不报错。
    data = AppConfig().model_dump()
    del data["ui"]["show_audio_split"]
    cfg = AppConfig.model_validate(data)
    assert cfg.ui.show_audio_split is False


# --------------------------------------------------------------------------- #
# _deep_update (the merge update_config uses)
# --------------------------------------------------------------------------- #

def test_deep_update_replaces_scalars():
    base = {"a": 1, "b": 2}
    _deep_update(base, {"b": 20, "c": 3})
    assert base == {"a": 1, "b": 20, "c": 3}


def test_deep_update_merges_nested_dicts():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    _deep_update(base, {"a": {"y": 20}, "c": 4})
    assert base == {"a": {"x": 1, "y": 20}, "b": 3, "c": 4}


def test_deep_update_replaces_dict_with_scalar():
    # A dict value replaced by a non-dict is overwritten, not merged into.
    base = {"a": {"x": 1}}
    _deep_update(base, {"a": 5})
    assert base == {"a": 5}


# --------------------------------------------------------------------------- #
# Bootstrap: root pointer + template + per-workspace config
# --------------------------------------------------------------------------- #

@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Point the config module at a throwaway project root with a clean,
    pointer-less root ``app.json``; reset the in-memory cache around each test."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "app.json")
    (tmp_path / "app.json").write_text(
        json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8"
    )
    core_config.reset_config_cache()
    yield tmp_path
    core_config.reset_config_cache()


def _read(file):
    return json.loads(file.read_text("utf-8"))


def test_get_config_unset_returns_root_template(sandbox):
    # With no pointer, the config is the (read-only) root template.
    cfg = core_config.get_config()
    assert cfg.paths.working_dir == ""


def test_set_workspace_pointer_updates_only_pointer(sandbox):
    # A distinct known template value survives the rewrite; the legacy ``book``
    # section (removed with target-chars splitting) is dropped; only the pointer
    # field changes.
    data = _read(sandbox / "app.json")
    data["text"] = {"live": False}  # a known value distinct from the default
    data["book"] = {"target_chars": 123456}  # legacy section from an old template
    (sandbox / "app.json").write_text(json.dumps(data), encoding="utf-8")
    core_config.reset_config_cache()

    core_config.set_workspace_pointer(str(sandbox / "MyBook"))

    after = _read(sandbox / "app.json")
    assert after["paths"]["working_dir"] == str(sandbox / "MyBook")
    assert after["text"]["live"] is False  # known template value preserved
    assert "book" not in after  # legacy section dropped on rewrite


def test_get_config_set_reads_workspace_config(sandbox):
    ws = sandbox / "MyBook"
    core_config.set_workspace_pointer(str(ws))
    # Give the workspace its own (distinct) config:
    ws_cfg = AppConfig()
    ws_cfg.paths.working_dir = str(ws)
    ws_cfg.generation.check_batch_size = 777
    (ws / "config").mkdir(parents=True)
    (ws / "config" / "app.json").write_text(
        json.dumps(ws_cfg.model_dump()), encoding="utf-8"
    )
    core_config.reset_config_cache()

    cfg = core_config.get_config()
    assert cfg.paths.working_dir == str(ws)
    assert cfg.generation.check_batch_size == 777  # from the workspace config, not the template


def test_update_config_requires_workspace(sandbox):
    with pytest.raises(core_config.WorkspaceNotSetError):
        core_config.update_config({"log": {"level": "DEBUG"}})


def test_update_config_writes_workspace_and_forces_pointer(sandbox):
    ws = sandbox / "MyBook"
    core_config.init_workspace_config(ws)  # seeds ws/config/app.json
    workspace_config = ws / "config" / "app.json"
    old = _read(workspace_config)
    old["tts"].update({
        "api_base": "https://legacy.example/v1",
        "api_key": "retired-provider-key",
        "voice": "legacy-voice",
        "concurrency": 3,
        "parallel_workers": 4,
    })
    old["persona_prompts"]["advanced_prompt"] = "retired prompt"
    workspace_config.write_text(json.dumps(old), encoding="utf-8")
    core_config.reset_config_cache()
    core_config.set_workspace_pointer(str(ws))

    cfg = core_config.update_config({"tts": {"batch_concurrency": 5}})
    assert cfg.tts.batch_concurrency == 5
    assert cfg.paths.working_dir == str(ws)  # forced to the workspace itself

    # The value landed in the workspace config; the ROOT template is untouched:
    saved = _read(workspace_config)
    assert saved["tts"]["batch_concurrency"] == 5
    retired_tts = {"api_base", "api_key", "voice", "concurrency", "parallel_workers"}
    assert not retired_tts & saved["tts"].keys()
    assert "advanced_prompt" not in saved["persona_prompts"]
    assert _read(sandbox / "app.json")["paths"]["working_dir"] == str(ws)


def test_update_config_persists_ui_show_parse_logs(sandbox):
    # 设置页保存「解析日志显示」→ 工作空间配置落盘（根模板不动）。
    ws = sandbox / "MyBook"
    core_config.init_workspace_config(ws)  # seeds ws/config/app.json
    core_config.set_workspace_pointer(str(ws))

    cfg = core_config.update_config({"ui": {"show_parse_logs": True}})
    assert cfg.ui.show_parse_logs is True
    assert _read(ws / "config" / "app.json")["ui"]["show_parse_logs"] is True
    # 再读（缓存已更新）保持 True。
    assert core_config.get_config().ui.show_parse_logs is True


def test_update_config_persists_ui_show_audio_split(sandbox):
    # 设置页保存「音频分集导航项」→ 工作空间配置落盘（根模板不动）。
    ws = sandbox / "MyBook"
    core_config.init_workspace_config(ws)  # seeds ws/config/app.json
    core_config.set_workspace_pointer(str(ws))

    cfg = core_config.update_config({"ui": {"show_audio_split": True}})
    assert cfg.ui.show_audio_split is True
    assert _read(ws / "config" / "app.json")["ui"]["show_audio_split"] is True
    # 再读（缓存已更新）保持 True。
    assert core_config.get_config().ui.show_audio_split is True


def test_init_workspace_config_copies_template_and_sets_pointer(sandbox):
    ws = sandbox / "NewBook"
    core_config.init_workspace_config(ws)
    target = ws / "config" / "app.json"
    assert target.exists()
    assert _read(target)["paths"]["working_dir"] == str(ws)


def test_init_workspace_config_never_overwrites(sandbox):
    ws = sandbox / "NewBook"
    (ws / "config").mkdir(parents=True)
    # The legacy ``book`` marker stays on disk (never rewritten here) — it is
    # dropped only by an explicit settings save.
    marker = {"paths": {"working_dir": str(ws)}, "book": {"target_chars": 424242}}
    (ws / "config" / "app.json").write_text(json.dumps(marker), encoding="utf-8")

    core_config.init_workspace_config(ws)  # must NOT overwrite an existing config

    after = _read(ws / "config" / "app.json")
    assert after["paths"]["working_dir"] == str(ws)
    assert after["book"]["target_chars"] == 424242


def test_template_seeded_from_defaults_when_missing(sandbox):
    # Remove the root file; the next pointer write re-seeds it from code defaults.
    (sandbox / "app.json").unlink()
    core_config.reset_config_cache()
    core_config.set_workspace_pointer(str(sandbox / "X"))
    assert (sandbox / "app.json").exists()
    data = _read(sandbox / "app.json")
    assert data["paths"]["working_dir"] == str(sandbox / "X")
    assert "tts" in data and "book" not in data  # the full model was seeded (no book section)


# --------------------------------------------------------------------------- #
# BGMConfig defaults (背景音乐系统)
# --------------------------------------------------------------------------- #

def test_bgm_config_defaults():
    b = BGMConfig()
    assert b.volume == 0.18
    assert b.fade_in == 1.5
    assert b.fade_out == 3.0
    assert b.loop is True
    assert b.min_match_score == 1
    assert b.analysis_chars == 6000


def test_app_config_round_trips_bgm():
    data = AppConfig().model_dump()
    data["bgm"]["volume"] = 0.3
    data["bgm"]["fade_in"] = 0.5
    data["bgm"]["fade_out"] = 5.0
    data["bgm"]["loop"] = False
    data["bgm"]["min_match_score"] = 3
    data["bgm"]["analysis_chars"] = 9000
    back = AppConfig.model_validate(data)
    assert back.bgm.volume == 0.3
    assert back.bgm.fade_in == 0.5
    assert back.bgm.fade_out == 5.0
    assert back.bgm.loop is False
    assert back.bgm.min_match_score == 3
    assert back.bgm.analysis_chars == 9000
    # 再次落盘/重读不丢字段（schema 稳定）。
    again = AppConfig.model_validate(back.model_dump())
    assert again.bgm == back.bgm


def test_app_config_missing_bgm_section_falls_back_to_defaults():
    # 旧工作空间配置缺整个 bgm 段 → Pydantic 默认值填充，读取链不报错。
    data = AppConfig().model_dump()
    del data["bgm"]
    cfg = AppConfig.model_validate(data)
    assert cfg.bgm == BGMConfig()
    # 缺单个字段同样降级默认。
    data2 = AppConfig().model_dump()
    del data2["bgm"]["volume"]
    assert AppConfig.model_validate(data2).bgm.volume == 0.18


def test_bgm_config_partial_round_trip():
    # 设置页只改一个字段：model_validate 后其余字段保持默认（深合并补丁由 update_config 负责）。
    data = AppConfig().model_dump()
    data["bgm"]["volume"] = 0.5
    cfg = AppConfig.model_validate(data)
    assert cfg.bgm.volume == 0.5
    assert cfg.bgm.fade_in == 1.5  # 其余字段未被波及


def test_legacy_batch_constraints_are_ignored_and_removed_on_save():
    old = dict(planner_length_bands=True, planner_batch_chars=True, planner_seq_chars=True,
               planner_length_ratio=True, planner_vram=True, batch_max_chars=1000,
               batch_length_ratio=1.1, vocoder_batch_size=64, profile_stages=True)
    config = TTSConfig(**old, batch_concurrency=80)
    assert config.batch_concurrency == 80
    assert not set(old) & config.model_dump().keys()
