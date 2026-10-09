"""Config defaults, validation, legacy reads and sandboxed workspace persistence."""
from __future__ import annotations

import json

import pytest

from backend.core import config as core_config
from backend.core import paths as core_paths
from backend.core.config import (
    AppConfig,
    BGMConfig,
    GenerationConfig,
    SplitConfig,
    TTSConfig,
    UIConfig,
    _deep_update,
)
from pydantic import ValidationError


# --------------------------------------------------------------------------- #
# TTSConfig defaults
# --------------------------------------------------------------------------- #

def test_tts_config_defaults_and_legacy_constraints():
    # tts config model ids
    t = TTSConfig()
    assert t.model == "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
    assert t.base_model == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    assert t.design_model == "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"

    # tts config pause defaults
    t = TTSConfig()
    assert t.pause_between_speakers_ms == 500
    assert t.pause_same_speaker_ms == 250

    # tts config batch concurrency default
    assert TTSConfig().batch_concurrency == 80

    # legacy batch constraints are ignored and removed on save
    old = dict(planner_length_bands=True, planner_batch_chars=True, planner_seq_chars=True,
               planner_length_ratio=True, planner_vram=True, batch_max_chars=1000,
               batch_length_ratio=1.1, vocoder_batch_size=64, profile_stages=True)
    config = TTSConfig(**old, batch_concurrency=80)
    assert config.batch_concurrency == 80
    assert not set(old) & config.model_dump().keys()


def test_retired_tts_settings_are_dropped_from_app_config():
    config = AppConfig.model_validate({
        "tts": {
            "api_base": "https://legacy.example/v1",
            "api_key": "preserve-user-value",
            "voice": "legacy-voice",
            "concurrency": 3,
            "parallel_workers": 4,
            "enabled": False,
            "speaker": "serena",
        },
        "persona_prompts": {"advanced_prompt": "user-authored prompt"},
    })

    restored = AppConfig.model_validate(config.model_dump())
    retired_tts = {"api_base", "api_key", "voice", "concurrency", "parallel_workers", "enabled", "speaker"}
    assert not retired_tts & restored.tts.model_dump().keys()
    assert "advanced_prompt" not in restored.persona_prompts.model_dump()


# --------------------------------------------------------------------------- #
# GenerationConfig: in-parse check toggles (migrated off the retired check sections)
# --------------------------------------------------------------------------- #

def test_generation_check_config_contract():
    # generation config check stage defaults
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
    # 用户解析页专属的 3 个开关 default ON（= 现有行为不变）；归属抽样总开关与
    # spot_check_rate 分离（率 = 比例、本字段 = 整段开/关）
    assert g.spot_check_enabled is True
    assert g.validate_instructs is True
    assert g.check_chunk_alignment is True
    # round-trip（自定义值不丢，含 False 的开关）
    g2 = GenerationConfig(**json.loads(
        json.dumps(g.model_dump(), ensure_ascii=False)))
    assert g2.check_long_paragraphs is g.check_long_paragraphs
    assert g2.max_paragraph_chars == g.max_paragraph_chars
    assert g2.absorb_punct_entries is g.absorb_punct_entries
    assert g2.merge_same_speaker is g.merge_same_speaker
    assert g2.spot_check_enabled is g.spot_check_enabled
    assert g2.validate_instructs is g.validate_instructs
    assert g2.check_chunk_alignment is g.check_chunk_alignment

    # generation config user owned checks round trip false
    # 开关可显式置 False 且往返不丢（任务快照回放依赖此路径）。
    g = GenerationConfig(
        check_chunk_alignment=False,
        check_boundary_speakers=False,
        validate_instructs=False,
        revalidate_splits=False,
        check_long_paragraphs=False,
        spot_check_enabled=False,
    )
    back = GenerationConfig(**json.loads(json.dumps(g.model_dump(), ensure_ascii=False)))
    assert back.check_chunk_alignment is False
    assert back.check_boundary_speakers is False
    assert back.validate_instructs is False
    assert back.revalidate_splits is False
    assert back.check_long_paragraphs is False
    assert back.spot_check_enabled is False


# --------------------------------------------------------------------------- #
# AppConfig: persona prompts + round-trip
# --------------------------------------------------------------------------- #

def test_app_config_prompts_and_round_trip():
    # app config includes persona prompts
    cfg = AppConfig()
    assert cfg.persona_prompts.system_prompt == ""
    assert cfg.persona_prompts.user_prompt == ""

    # app config round trips
    data = AppConfig().model_dump()
    back = AppConfig.model_validate(data)
    assert back.tts.pause_between_speakers_ms == 500
    assert back.tts.design_model == "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    assert back.persona_prompts is not None


# --------------------------------------------------------------------------- #
# UIConfig: 解析日志显示开关（解析页日志区显隐 + 三指标位置）
# --------------------------------------------------------------------------- #

def test_ui_config_defaults_and_round_trip():
    # ui config show parse logs default off
    # 默认关：解析页隐藏「解析进度」日志区，三指标移到「开始处理」按钮下方。
    u = UIConfig()
    assert u.show_parse_logs is False
    assert u.theme == "system"

    # ui config round trips show parse logs
    data = AppConfig().model_dump()
    data["ui"]["show_parse_logs"] = True
    back = AppConfig.model_validate(data)
    assert back.ui.show_parse_logs is True
    # 再次落盘/重读不丢字段（schema 稳定）。
    again = AppConfig.model_validate(back.model_dump())
    assert again.ui.show_parse_logs is True

    # ui config missing field falls back to default
    # 旧工作空间配置缺该字段 → Pydantic 默认值填充（False），读取链不报错。
    data = AppConfig().model_dump()
    del data["ui"]["show_parse_logs"]
    cfg = AppConfig.model_validate(data)
    assert cfg.ui.show_parse_logs is False

    # 已下线的「音频分集」配置残留在旧工作空间配置里：加载时静默丢弃，不影响读取链。
    data = AppConfig().model_dump()
    data["audio"] = {"target_duration": "10:00", "naming_format": "第 {} 集"}
    data["ui"]["show_audio_split"] = True
    cfg = AppConfig.model_validate(data)
    assert not hasattr(cfg, "audio") and not hasattr(cfg.ui, "show_audio_split")


# --------------------------------------------------------------------------- #
# _deep_update (the merge update_config uses)
# --------------------------------------------------------------------------- #

def test_deep_update_contract():
    # deep update replaces scalars
    base = {"a": 1, "b": 2}
    _deep_update(base, {"b": 20, "c": 3})
    assert base == {"a": 1, "b": 20, "c": 3}

    # deep update merges nested dicts
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    _deep_update(base, {"a": {"y": 20}, "c": 4})
    assert base == {"a": {"x": 1, "y": 20}, "b": 3, "c": 4}

    # deep update replaces dict with scalar
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
    pointer-less root ``setting.json``; reset the in-memory cache around each test."""
    monkeypatch.setattr(core_paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(core_config, "TEMPLATE_FILE", tmp_path / "setting.json")
    (tmp_path / "setting.json").write_text(
        json.dumps({"paths": {"working_dir": ""}}), encoding="utf-8"
    )
    core_config.reset_config_cache()
    yield tmp_path
    core_config.reset_config_cache()


def _read(file):
    return json.loads(file.read_text("utf-8"))


def test_setting_template_and_workspace_config_are_separate(sandbox):
    ws = sandbox / "Book"
    core_config.set_workspace_pointer(str(ws))
    core_config.init_workspace_config(ws)
    core_config.update_config({"text": {"live": False}})
    assert _read(sandbox / "setting.json")["paths"]["working_dir"] == str(ws)
    assert _read(sandbox / "setting.json")["text"]["live"] is True
    assert _read(ws / "config" / "setting.json")["text"]["live"] is False


def test_get_config_unset_returns_root_template(sandbox):
    # With no pointer, the config is the (read-only) root template.
    cfg = core_config.get_config()
    assert cfg.paths.working_dir == ""


def test_legacy_workspace_config_reads_without_writing_and_saves_new_name(sandbox):
    ws = sandbox / "Book"
    legacy = ws / "config" / "app.json"
    legacy.parent.mkdir(parents=True)
    original = json.dumps({"text": {"live": False}, "ui": {"theme": "dark"}})
    legacy.write_text(original, encoding="utf-8")
    core_config.set_workspace_pointer(str(ws))

    assert core_config.get_config().text.live is False
    assert not (ws / "config" / "setting.json").exists()
    core_config.update_config({"ui": {"theme": "light"}})

    saved = _read(ws / "config" / "setting.json")
    assert saved["text"]["live"] is False
    assert saved["ui"]["theme"] == "light"
    assert legacy.read_text("utf-8") == original
    core_config.reset_config_cache()
    assert core_config.get_config().ui.theme == "light"


def test_init_workspace_config_copies_legacy_verbatim_and_keeps_backup(sandbox):
    ws = sandbox / "Book"
    legacy = ws / "config" / "app.json"
    legacy.parent.mkdir(parents=True)
    original = b'{"text":{"live":false},"extension":{"custom":123}}'
    legacy.write_bytes(original)

    core_config.init_workspace_config(ws)

    assert (ws / "config" / "setting.json").read_bytes() == original
    assert legacy.read_bytes() == original


def test_new_workspace_config_takes_precedence_over_legacy(sandbox):
    ws = sandbox / "Book"
    directory = ws / "config"
    directory.mkdir(parents=True)
    (directory / "app.json").write_text('{"text":{"live":false}}', encoding="utf-8")
    target = directory / "setting.json"
    original = '{"text":{"live":true}}'
    target.write_text(original, encoding="utf-8")
    core_config.set_workspace_pointer(str(ws))

    core_config.init_workspace_config(ws)

    assert core_config.get_config().text.live is True
    assert target.read_text("utf-8") == original


def test_set_workspace_pointer_updates_only_pointer(sandbox):
    # A distinct known template value survives the rewrite; the legacy ``book``
    # section (removed with target-chars splitting) is dropped; only the pointer
    # field changes.
    data = _read(sandbox / "setting.json")
    data["text"] = {"live": False}  # a known value distinct from the default
    data["book"] = {"target_chars": 123456}  # legacy section from an old template
    (sandbox / "setting.json").write_text(json.dumps(data), encoding="utf-8")
    core_config.reset_config_cache()

    core_config.set_workspace_pointer(str(sandbox / "MyBook"))

    after = _read(sandbox / "setting.json")
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
    (ws / "config" / "setting.json").write_text(
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
    core_config.init_workspace_config(ws)  # seeds ws/config/setting.json
    workspace_config = ws / "config" / "setting.json"
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
    assert _read(sandbox / "setting.json")["paths"]["working_dir"] == str(ws)


def test_update_config_persists_ui_show_parse_logs(sandbox):
    # 设置页保存「解析日志显示」→ 工作空间配置落盘（根模板不动）。
    ws = sandbox / "MyBook"
    core_config.init_workspace_config(ws)  # seeds ws/config/setting.json
    core_config.set_workspace_pointer(str(ws))

    cfg = core_config.update_config({"ui": {"show_parse_logs": True}})
    assert cfg.ui.show_parse_logs is True
    assert _read(ws / "config" / "setting.json")["ui"]["show_parse_logs"] is True
    # 再读（缓存已更新）保持 True。
    assert core_config.get_config().ui.show_parse_logs is True


def test_init_workspace_config_copies_template_and_sets_pointer(sandbox):
    ws = sandbox / "NewBook"
    core_config.init_workspace_config(ws)
    target = ws / "config" / "setting.json"
    assert target.exists()
    assert _read(target)["paths"]["working_dir"] == str(ws)


def test_init_workspace_config_never_overwrites(sandbox):
    ws = sandbox / "NewBook"
    (ws / "config").mkdir(parents=True)
    # The legacy ``book`` marker stays on disk (never rewritten here) — it is
    # dropped only by an explicit settings save.
    marker = {"paths": {"working_dir": str(ws)}, "book": {"target_chars": 424242}}
    (ws / "config" / "setting.json").write_text(json.dumps(marker), encoding="utf-8")

    core_config.init_workspace_config(ws)  # must NOT overwrite an existing config

    after = _read(ws / "config" / "setting.json")
    assert after["paths"]["working_dir"] == str(ws)
    assert after["book"]["target_chars"] == 424242


def test_template_seeded_from_defaults_when_missing(sandbox):
    # Remove the root file; the next pointer write re-seeds it from code defaults.
    (sandbox / "setting.json").unlink()
    core_config.reset_config_cache()
    core_config.set_workspace_pointer(str(sandbox / "X"))
    assert (sandbox / "setting.json").exists()
    data = _read(sandbox / "setting.json")
    assert data["paths"]["working_dir"] == str(sandbox / "X")
    assert "tts" in data and "book" not in data  # the full model was seeded (no book section)


# --------------------------------------------------------------------------- #
# BGMConfig defaults (背景音乐系统)
# --------------------------------------------------------------------------- #

def test_bgm_config_defaults_and_round_trip():
    # bgm config defaults
    b = BGMConfig()
    assert b.volume == 0.18
    assert b.fade_in == 1.5
    assert b.fade_out == 3.0
    assert b.loop is True
    assert b.min_match_score == 1
    # 段落级两字段（隐藏，UI 不露出）
    assert b.segment_batch_size == 20
    assert b.segment_volume_tiers == [0.5, 1.0, 1.5]

    # app config round trips bgm
    data = AppConfig().model_dump()
    data["bgm"]["volume"] = 0.3
    data["bgm"]["fade_in"] = 0.5
    data["bgm"]["fade_out"] = 5.0
    data["bgm"]["loop"] = False
    data["bgm"]["min_match_score"] = 3
    # 已退役字段：历史配置里的旧键被默认 extra='ignore' 丢弃，读取不报错。
    data["bgm"]["analysis_chars"] = 9000
    back = AppConfig.model_validate(data)
    assert back.bgm.volume == 0.3
    assert back.bgm.fade_in == 0.5
    assert back.bgm.fade_out == 5.0
    assert back.bgm.loop is False
    assert back.bgm.min_match_score == 3
    assert "analysis_chars" not in back.bgm.model_dump()
    # 再次落盘/重读不丢字段（schema 稳定）。
    again = AppConfig.model_validate(back.model_dump())
    assert again.bgm == back.bgm

    # app config missing bgm section falls back to defaults
    # 旧工作空间配置缺整个 bgm 段 → Pydantic 默认值填充，读取链不报错。
    data = AppConfig().model_dump()
    del data["bgm"]
    cfg = AppConfig.model_validate(data)
    assert cfg.bgm == BGMConfig()
    # 缺单个字段同样降级默认。
    data2 = AppConfig().model_dump()
    del data2["bgm"]["volume"]
    assert AppConfig.model_validate(data2).bgm.volume == 0.18

    # bgm config partial round trip
    # 设置页只改一个字段：model_validate 后其余字段保持默认（深合并补丁由 update_config 负责）。
    data = AppConfig().model_dump()
    data["bgm"]["volume"] = 0.5
    cfg = AppConfig.model_validate(data)
    assert cfg.bgm.volume == 0.5
    assert cfg.bgm.fade_in == 1.5  # 其余字段未被波及


def test_bgm_params_are_admin_managed_workspace_values_ignored(sandbox):
    # BGM 音频参数由管理员统一配置：工作空间文件里的 bgm 段读取不生效
    # （强制代码默认值，平台默认值由 get_config 的 platform provider 覆盖）。
    ws = sandbox / "MyBook"
    core_config.init_workspace_config(ws)
    old = _read(ws / "config" / "setting.json")
    old["bgm"]["volume"] = 0.42
    old["bgm"]["analysis_chars"] = 9000  # 退役字段残留
    (ws / "config" / "setting.json").write_text(json.dumps(old), encoding="utf-8")
    core_config.set_workspace_pointer(str(ws))
    core_config.reset_config_cache()

    assert core_config.get_config().bgm == BGMConfig()

    # 用户侧保存（哪怕携带 bgm 段）落盘的始终是代码默认值，历史旧值随保存被清出。
    cfg = core_config.update_config({"log": {"level": "DEBUG"}, "bgm": {"volume": 0.9}})
    assert cfg.bgm == BGMConfig()
    assert cfg.log.level == "DEBUG"
    saved = _read(ws / "config" / "setting.json")
    assert saved["log"]["level"] == "DEBUG"
    assert saved["bgm"]["volume"] == BGMConfig().volume
    assert "analysis_chars" not in saved["bgm"]


# --------------------------------------------------------------------------- #
# SplitConfig（零章节按字数分册目标字数：管理员统一配置，bgm 同款处理）
# --------------------------------------------------------------------------- #

def test_split_config_defaults_and_bounds():
    # split config default and bounds
    assert SplitConfig().length_target == 3000
    assert SplitConfig().smart_split_long_chapters is True
    with pytest.raises(ValidationError):
        SplitConfig(length_target=99)
    with pytest.raises(ValidationError):
        SplitConfig(length_target=200_001)

    # app config missing split section falls back to defaults
    # 旧工作空间配置缺整个 split 段 → Pydantic 默认值填充，读取链不报错。
    data = AppConfig().model_dump()
    del data["split"]
    assert AppConfig.model_validate(data).split == SplitConfig()
    data2 = AppConfig().model_dump()
    del data2["split"]["length_target"]
    assert AppConfig.model_validate(data2).split.length_target == 3000


def test_split_target_is_admin_managed_workspace_values_ignored(sandbox, monkeypatch):
    # 分册目标字数由管理员统一配置：工作空间文件里的 split 段读取不生效
    # （强制代码默认值；平台默认值由 get_config 的 platform provider 覆盖）。
    ws = sandbox / "MyBook"
    core_config.init_workspace_config(ws)
    old = _read(ws / "config" / "setting.json")
    old["split"]["length_target"] = 12345
    old["split"]["smart_split_long_chapters"] = False
    (ws / "config" / "setting.json").write_text(json.dumps(old), encoding="utf-8")
    core_config.set_workspace_pointer(str(ws))
    core_config.reset_config_cache()

    assert core_config.get_config().split == SplitConfig()

    # 平台 provider 覆盖生效（管理员把目标字数改成 5000）。
    monkeypatch.setattr(core_config, "_platform_defaults_provider", lambda: {"split": {"length_target": 5000, "smart_split_long_chapters": False}})
    assert core_config.get_config().split.length_target == 5000
    assert core_config.get_config().split.smart_split_long_chapters is False

    # 用户侧保存（哪怕携带 split 段）落盘的始终是代码默认值，历史旧值随保存被清出。
    cfg = core_config.update_config({"log": {"level": "DEBUG"}, "split": {"length_target": 777, "smart_split_long_chapters": False}})
    assert cfg.split == SplitConfig()
    assert cfg.log.level == "DEBUG"
    saved = _read(ws / "config" / "setting.json")
    assert saved["log"]["level"] == "DEBUG"
    assert saved["split"]["length_target"] == SplitConfig().length_target


def test_continuous_chapter_split_config_defaults_and_project_persistence(sandbox, monkeypatch):
    from backend.core.config import TextConfig

    assert TextConfig().split_long_continuous_chapters is False
    assert TextConfig.model_validate({"sentence_break": False}).split_long_continuous_chapters is False
    ws = sandbox / "continuous-project"
    core_config.init_workspace_config(ws)
    core_config.set_workspace_pointer(str(ws))
    cfg = core_config.update_config({"text": {"split_long_continuous_chapters": True}})
    assert cfg.text.split_long_continuous_chapters is True
    core_config.reset_config_cache()
    assert core_config.get_config().text.split_long_continuous_chapters is True
    monkeypatch.setattr(core_config, "_platform_defaults_provider", lambda: {
        "text": {"split_long_continuous_chapters": False, "sentence_break": False},
    })
    effective = core_config.get_config().text
    assert effective.split_long_continuous_chapters is True
    assert effective.sentence_break is False
