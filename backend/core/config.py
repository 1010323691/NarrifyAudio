"""Unified, persistent configuration.

Two files, one pointer:

* ``<project>/app.json`` — the root file. It is both the *generic default
  template* (its non-pointer fields seed every new workspace's config) and the
  *bootstrap pointer*: ``paths.working_dir`` names the active workspace. The
  pointer is the ONLY field the app ever writes there — all other values are
  read-only at runtime.
* ``<workspace>/config/app.json`` — the active project's config, created by
  copying the root template when a workspace is set (never overwritten if it
  already exists). Once a workspace exists, every config read and write targets
  this file, and it is rewritten on each save (requirement #4).

The pointer must live in the root file: to load the workspace config the app
must first know *which* workspace is active, and that can only be read from a
stable location that does not itself depend on the workspace (otherwise it is a
chicken-and-egg loop — the config file chasing its own setting).
"""
from __future__ import annotations

import json
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .request_context import _UNSET, bound_workspace
from .paths import PROJECT_ROOT


class PathsConfig(BaseModel):
    working_dir: str = ""  # the workspace pointer (empty -> no workspace, pipeline locked)


class TextConfig(BaseModel):
    # 10 formatting toggles (ported from TextFormatter's ``tf.config``).
    keep_single_space: bool = False
    sentence_break: bool = True
    dialogue_separate: bool = True
    detect_chapters: bool = True
    punct_ellipsis: bool = True
    punct_repeated: bool = True
    punct_lone_ascii: bool = False
    punct_quotes: bool = False
    punct_dash: bool = False
    live: bool = True  # UI-only: reformat immediately on change


class AudioConfig(BaseModel):
    target_duration: str = "10:00"
    naming_format: str = "第 {} 集"
    start_number: str = "1"
    smart_align: bool = True
    align_tolerance: int = 15


class TTSConfig(BaseModel):
    # Local Qwen3-TTS engine — runs in the shared ``.venv`` as a one-shot subprocess
    # (see ``backend/engines/tts.py``); the app backend never imports torch directly.
    enabled: bool = True
    model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"  # CustomVoice model id
    # The other two Qwen3-TTS 1.7B variants, loaded by the worker for clone / design.
    base_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"  # voice cloning
    design_model: str = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"  # text -> voice
    speaker: str = "serena"  # default voice / speaker
    language: str = "chinese"  # default language
    device: str = "auto"  # auto | cuda | cpu | mps
    # Merge pause defaults (ported from the source project's TTS config): silence
    # inserted between segments, per-speaker vs. speaker-change (ms).
    pause_between_speakers_ms: int = 500
    pause_same_speaker_ms: int = 250
    # 正常组包仅按长度排序后按此上限切批；保留超时减半与后续恢复。
    batch_concurrency: int = 80
    # When enabled, the worker selects the per-batch row cap from measured safety tiers.
    batch_auto: bool = False
    # 可复现 seed；不是组包限制。
    batch_seed: int = -1


class BGMConfig(BaseModel):
    # 背景音乐系统（阶段 7）：章节级 BGM 匹配 + 最终混音（engines/bgm.py）。
    # 混音 = ffmpeg 把 06_audio_merge/<章>.mp3（旁白）与音乐库曲目混成 08_bgm/<章>.mp3。
    volume: float = 0.18  # BGM 增益（0~2，1 = 音乐文件自身电平、2 = 2 倍/+6 dB）；amix（normalize=0）前对 BGM 侧 1:1 施加
    fade_in: float = 1.5  # 淡入秒数；混音时钳 min(fade_in, 时长/2)
    fade_out: float = 3.0  # 淡出秒数；混音时钳 min(fade_out, 时长/2)
    loop: bool = True  # 循环策略 = 重复策略：True = -stream_loop -1 循环铺满；False = 只播一遍，其余静音
    min_match_score: int = 1  # 匹配最低分：低于此分不进候选（使用处钳 ≥1）
    analysis_chars: int = 6000  # 章节 LLM 分析采样字数（头/中/尾三窗各 1/3）
    # 段落级 BGM（segment 模式，engines/bgm.py）——两字段均隐藏（UI 不露出，同
    # generation.check_batch_size 先例）：
    segment_batch_size: int = 20  # 段落分析每批送 LLM 的条目数（条目 ≤200 字硬保证 → 20 条约 4k 字）
    segment_volume_tiers: list = Field(default_factory=lambda: [0.5, 1.0, 1.5])
    # intensity 1/2/3 → volume 的倍率（tier index = intensity-1；越界/缺失按档 2；结果 clamp ≤2.0）


class PersonaPromptsConfig(BaseModel):
    # Voice-design (persona) prompts for the "角色配音" stage. Empty values fall back
    # to the bundled defaults in ``backend/engines/persona_prompts.py``.
    system_prompt: str = ""
    user_prompt: str = ""


class FFmpegConfig(BaseModel):
    ffmpeg_path: str = ""  # empty -> resolve from PATH
    ffprobe_path: str = ""  # empty -> resolve from PATH


class LogConfig(BaseModel):
    level: str = "INFO"


class UIConfig(BaseModel):
    theme: str = "system"  # system | light | dark
    # 解析页「解析进度」日志区（每文件实时日志 + 流式反馈）是否显示；默认关。
    # 关时三性能指标移到「开始处理」按钮下方（每文件行内的进度/速度/状态不受影响）。
    show_parse_logs: bool = False
    # 侧边栏是否显示「音频分集」导航项；默认关（隐藏）。
    # 关 = 导航栏隐藏该项（音频合并页的「前往音频分集」按钮随之隐藏），
    # 页面路由保留——直接访问 URL 仍可打开；开 = 导航栏显示该项。
    show_audio_split: bool = False


class LLMConfig(BaseModel):
    # OpenAI-compatible ``chat/completions`` endpoint (default: a local Ollama server).
    # The LLM HTTP call uses stdlib ``urllib`` — see ``backend/engines/script.py``.
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "local"  # local servers ignore it; remote APIs need the real key
    model_name: str = ""  # left blank on purpose — the user sets their own model
    # Stream the completion (``stream: true``) so the 文本解析 page can show the model's
    # raw output token-by-token (the 「流式反馈」 panel). When a server rejects
    # ``stream: true``, set this false in ``config/app.json`` to fall back to the
    # non-streaming call (the stream panel then stays empty).
    stream: bool = True


class PromptsConfig(BaseModel):
    # Empty values fall back to the bundled defaults
    # (``backend/resources/default_prompts.txt``) at read time — see ``backend/api/config.py``.
    system_prompt: str = ""
    user_prompt: str = ""


class GenerationConfig(BaseModel):
    chunk_size: int = 3000  # chars per chunk sent to the LLM
    max_tokens: int = 4096  # max completion tokens per call
    temperature: float = 0.6
    top_p: float = 0.8
    top_k: int = 0  # 0 -> not sent (OpenAI ignores it; some local servers use it)
    min_p: float = 0.0  # 0 -> not sent
    presence_penalty: float = 0.0
    banned_tokens: list = Field(default_factory=list)
    # Max files parsed in parallel (LLM jobs); the rest of a batch queue behind a
    # shared gate (see ``core/concurrency.py``). 0 / negative is clamped to 1.
    max_concurrency: int = 3
    # 解析后的「归属抽样」比例：全量条目中重判 speaker 的抽样率（0 = 关闭）。
    # 分两桶（不相交）：~1/3 纯随机（整书错误率"仪表"——唯一可据以判断"采样率能不能
    # 降"的读数）+ ~2/3 风险加权（无归属标签 / ≤10 字 / 多角色场景，按特征数级联）。
    # 每本的纯随机桶读数记入任务日志 + <workspace>/config/spot_check_history.json；
    # 冷启动比例；启用自适应后由历史随机桶读数自动微调。
    spot_check_rate: float = 0.05
    # 自适应抽样：以 spot_check_rate 为冷启动比例，根据历史纯随机桶错误率
    # 自动在 min/max 之间收缩或扩张；关闭后保持固定比例。
    spot_check_adaptive: bool = True
    spot_check_min_rate: float = 0.01
    spot_check_max_rate: float = 0.10
    # 解析内「断句失败校验」开关（设置页可切换，默认开 = 现有行为不变）：外层双引号包裹
    # 且引号内含「…道：」标签的条目逐条重跑解析 LLM 校验。关 = 解析任务跳过该阶段并留
    # 一行日志（结果字段 suspicious / suspicious_fixed 为 0）。
    revalidate_splits: bool = True
    # 解析内「纯归属标签条清理」开关（设置页可切换，默认开）：整条即纯归属标签的短
    # NARRATOR 条确定性删除（零 LLM 成本）。关 = 跳过该阶段并留一行日志（tags_deleted 为 0）。
    delete_saying_tags: bool = True
    # 解析内「角色匹配检查」开关（设置页可切换，默认开）：chunk 切割会切断跨段上下文，
    # 边界两侧的条目在解析时看不到另一侧的对话/角色上下文 → 用跨边界窗口
    # （[前置上下文]+[目标]+[后置上下文]，上下文仅辅助、只有目标可被修改）重判
    # 每个内部 chunk 边界两侧 check_context_window 条。关 = 跳过该阶段并留一行日志
    # （boundary_checked / boundary_fixed 为 0）。
    check_boundary_speakers: bool = True
    # 解析内重判阶段的批几何（自已退役的 speaker_check 段迁入）：每次 LLM 调用重判的
    # 目标条目数 / 目标块两侧的上下文条数。角色匹配检查与归属抽样两者都用；
    # 断句失败校验只用 context_window。设置页不露出（config/app.json 可编辑）。
    check_batch_size: int = 20
    check_context_window: int = 4

    # 超长段落检查（解析内第五阶段，LLM 重切 + 机械分段兜底）：
    # 超过 ``max_paragraph_chars`` 字的条目 = 疑似切割失败 → 先带上下文窗口重跑 LLM
    # 语义重切（复用重判批协议；同人长独白过不了忠实性门 = 保留原样），之后无论是否
    # 改过都过一道确定性机械分段，硬保证最终没有任何条目超过该字数。
    check_long_paragraphs: bool = True
    # 硬上限（字数 = strip 后 Unicode 码点数）：任何条目（含 LLM 重切后的单条）
    # 最终不得超过此值——机械分段兜底保证。
    max_paragraph_chars: int = 200
    # 纯标点条目吸收（解析内第六阶段，确定性零 LLM 成本）：整条无任何词字符的条目
    # （独立「……」/「？」等）并入相邻 NARRATOR 条目（标题守卫），无 NARRATOR 邻接则删除。
    absorb_punct_entries: bool = True
    # 同人段落合并（解析内机械后处理，确定性零 LLM 成本；置于超长机械分段**之前**）：
    # 连续同 speaker 条目按词字符数合并（块+段 ≤100 或较短一方 ≤10 强制），边界无收尾
    # 标点补「。」，instruct 取词字符多者，章标题两侧不合并。≤10 强制合并可造出 >
    # max_paragraph_chars 的块，由随后的机械分段切回（200 字硬保证不变）。
    merge_same_speaker: bool = True


class AppConfig(BaseModel):
    # NOTE: the ``book`` section (target_chars) was removed with the switch to
    # strict per-chapter splitting. Old config files may still carry it — the
    # default ``extra='ignore'`` drops it on load; the next save removes it from
    # disk. Reads therefore never fail on legacy configs.
    paths: PathsConfig = Field(default_factory=PathsConfig)
    text: TextConfig = Field(default_factory=TextConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    persona_prompts: PersonaPromptsConfig = Field(default_factory=PersonaPromptsConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    ffmpeg: FFmpegConfig = Field(default_factory=FFmpegConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    bgm: BGMConfig = Field(default_factory=BGMConfig)


# The root file: generic default config template + the workspace pointer (the only writable field).
TEMPLATE_FILE = PROJECT_ROOT / "app.json"

_lock = threading.RLock()
_config_cache: dict[str, AppConfig] = {}
_task_config: ContextVar[AppConfig | None] = ContextVar("task_config", default=None)


def bind_task_config(config: AppConfig):
    """Use the submitted task's immutable settings during engine execution."""
    return _task_config.set(config)


def reset_task_config(token) -> None:
    _task_config.reset(token)
_platform_config_cache: dict[str, Any] = {"expires": 0.0, "value": {}}
_PLATFORM_CONFIG_TTL = 3.0


def _platform_config() -> dict[str, Any]:
    """Load the administrator-managed feature defaults shared by all workspaces."""
    now = time.monotonic()
    with _lock:
        if now < _platform_config_cache["expires"]:
            return _platform_config_cache["value"]
        try:
            # Lazy imports keep the standalone config engine usable without the
            # platform database (for example in unit tests and migration tools).
            from ..platform.database import SessionLocal
            from ..platform.models import SystemConfig

            with SessionLocal() as db:
                row = db.get(SystemConfig, "application.features")
                value = row.value if row and isinstance(row.value, dict) else {}
        except Exception:
            value = {}
        _platform_config_cache.update(value=value, expires=now + _PLATFORM_CONFIG_TTL)
        return value


def set_platform_config_cache(value: dict[str, Any]) -> None:
    """Update the local effective-config cache after an admin save."""
    with _lock:
        _platform_config_cache.update(value=value, expires=time.monotonic() + _PLATFORM_CONFIG_TTL)


class WorkspaceNotSetError(RuntimeError):
    """A config write was attempted with no workspace selected (-> HTTP 409)."""


# -- bootstrap pointer (the only thing read from the root before the workspace) -- #

def _read_root_pointer() -> str:
    """Read ``paths.working_dir`` from the root file (empty when absent/invalid)."""
    if not TEMPLATE_FILE.exists():
        return ""
    try:
        data = json.loads(TEMPLATE_FILE.read_text("utf-8"))
    except Exception:
        return ""
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, dict):
        return ""
    return str(paths.get("working_dir") or "")


def _workspace_path() -> Path | None:
    """The active workspace as an absolute path, or ``None`` when unset.

    This is the single bootstrap-safe source of truth for "which workspace".
    Relative pointers resolve against ``PROJECT_ROOT``.
    """
    scoped = bound_workspace()
    if scoped is not _UNSET:
        return scoped
    working = _read_root_pointer().strip()
    if not working:
        return None
    ws = Path(working)
    if not ws.is_absolute():
        ws = PROJECT_ROOT / ws
    return ws


def _active_config_file() -> Path:
    """The file that is the source of truth for the full config."""
    ws = _workspace_path()
    if ws is None:
        return TEMPLATE_FILE  # unset -> the (read-only) root template
    return ws / "config" / "app.json"


# -- loading ------------------------------------------------------------------ #

def _load_config_file(file: Path) -> AppConfig | None:
    if not file.exists():
        return None
    try:
        return AppConfig.model_validate(json.loads(file.read_text("utf-8")))
    except Exception:
        return None


def _load_unlocked() -> AppConfig:
    """Resolve the active config without acquiring the lock (callers hold it).

    Workspace set -> the workspace's ``config/app.json``; otherwise the root
    template (a read-only view). A missing workspace file falls back to the root
    template's values, then to pure code defaults — reads never write.
    """
    return (
        _load_config_file(_active_config_file())
        or _load_config_file(TEMPLATE_FILE)
        or AppConfig()
    )


def get_config() -> AppConfig:
    """Return the in-memory config, loading it on first access (see ``_load_unlocked``).

    The workspace bookkeeping field is self-healed in memory on every read: the
    root pointer is the source of truth, so a stale ``paths.working_dir`` left in
    the workspace config (e.g. the workspace folder was moved and re-selected)
    must not leak into the UI. The next settings save rewrites it persistently
    (``update_config`` already forces the field to the live workspace).
    """
    task_config = _task_config.get()
    if task_config is not None:
        return task_config
    with _lock:
        ws = _workspace_path()
        key = str(ws.resolve()) if ws is not None else "<template>"
        config = _config_cache.get(key)
        if config is None:
            config = _load_unlocked()
            _config_cache[key] = config
        if ws is not None and config.paths.working_dir != str(ws):
            config = config.model_copy(update={
                "paths": config.paths.model_copy(update={"working_dir": str(ws)})
            })
            _config_cache[key] = config
        platform_config = _platform_config()
        if platform_config:
            data = config.model_dump()
            _deep_update(data, platform_config)
            config = AppConfig.model_validate(data)
        return config


def reset_config_cache() -> None:
    """Drop the in-memory config; the next read re-resolves the source file.

    Called after the workspace pointer changes (set / clear) so reads switch from
    the old workspace's config to the new one (or the root template).
    """
    with _lock:
        _config_cache.clear()


# -- writing ------------------------------------------------------------------ #

def _write_config_file(file: Path, config: AppConfig) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        json.dumps(config.model_dump(), ensure_ascii=False, indent=2), "utf-8"
    )


def _ensure_template() -> None:
    """Seed the root ``app.json`` from code defaults on a fresh clone (missing file)."""
    if not TEMPLATE_FILE.exists():
        _write_config_file(TEMPLATE_FILE, AppConfig())


def set_workspace_pointer(path: str) -> None:
    """Persist the workspace pointer in the ROOT file (its only writable field).

    Every other template value is preserved verbatim; the config cache is reset so
    subsequent reads target the (new) workspace.
    """
    with _lock:
        _ensure_template()
        try:
            data = json.loads(TEMPLATE_FILE.read_text("utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data.setdefault("paths", {})["working_dir"] = path
        _write_config_file(TEMPLATE_FILE, AppConfig.model_validate(data))
        reset_config_cache()


def clear_workspace() -> None:
    """Clear the pointer (re-locks the pipeline); config falls back to the template."""
    set_workspace_pointer("")


def init_workspace_config(ws: Path) -> None:
    """Give a fresh workspace its own ``config/app.json`` — a copy of the root
    template with ``working_dir`` set to the workspace. NEVER overwrites an
    existing workspace config."""
    with _lock:
        _ensure_template()
        target = ws / "config" / "app.json"
        if target.exists():
            return
        base = _load_config_file(TEMPLATE_FILE) or AppConfig()
        base.paths.working_dir = str(ws)
        _write_config_file(target, base)


def _deep_update(base: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def update_config(patch: dict[str, Any]) -> AppConfig:
    """Merge a (possibly partial) update into the ACTIVE (workspace) config and
    persist it there. Requires a workspace (raises ``WorkspaceNotSetError`` ->
    HTTP 409). ``paths.working_dir`` is forced to the workspace itself, so the
    workspace can only be changed via the workspace endpoint, never a settings
    write. The root template is never touched."""
    with _lock:
        ws = _workspace_path()
        if ws is None:
            raise WorkspaceNotSetError(
                "尚未设置工作空间——配置随工程，请先在「开始」页选择文件夹。"
            )
        key = str(ws.resolve())
        current = _config_cache.get(key) or _load_unlocked()
        data = current.model_dump()
        _deep_update(data, patch)
        new = AppConfig.model_validate(data)
        new.paths.working_dir = str(ws)
        _config_cache[key] = new
        _write_config_file(ws / "config" / "app.json", new)
        return new
