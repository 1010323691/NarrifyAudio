"""The unified registry of durable task types.

One explicit declaration per type: name, executor binding, billing, permission.
Both dispatchers (``task_worker.execute_claim`` and
``engine_task_executor.execute_engine_task``) resolve their dispatch decision
through :data:`TASK_TYPES`; the policy frozensets that used to live in
``task_types.py`` are derived from this table, so the table is the single
source of truth.

The executor column names the binding entry in the owning dispatcher's
explicit function map (``task_worker.DIRECT_EXECUTORS`` for the platform
direct types, ``engine_task_executor.ENGINE_BRANCHES`` for the eleven
legacy engine types). The table deliberately holds names, not live function
references: a single literal table holding the references would create a
module cycle (both dispatchers import the type frozensets at module level),
and the plan rules out decorator-based implicit registration. Dispatch
resolves through the dispatchers' explicit function maps;
``test_task_registry`` pins every table row's executor name to the live
function in the owning map.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskTypeSpec:
    name: str
    executor: str  # binding name in the owning dispatcher's explicit executor map
    billable: bool = False
    admin_only: bool = False
    legacy_engine: bool = False
    # Payload keys that name the run's entry (one row per entry in the task
    # centre). The display label is never an identity key: batch labels are
    # counts / display text (「音频合成（N 段）」), so different entries of the
    # same shape share one. Empty = whole-book type: every run of the type in
    # a project is the same entry. Keys absent from (or NULL in) both rows
    # compare equal; a row whose identity keys are all NULL/absent has no
    # subject and is never superseded.
    entry_identity: tuple[str, ...] = ()
    gpu_initial: str | None = None
    gpu_stages: tuple[str, ...] = ()


_SPECS: tuple[TaskTypeSpec, ...] = (
    TaskTypeSpec("project.progress", "_execute_project_progress"),
    # 平台直连（含 project.progress 共 8）：task_worker.DIRECT_EXECUTORS
    TaskTypeSpec("text.format", "_execute_text_format"),
    TaskTypeSpec("book.analyze", "_execute_book_analyze"),
    TaskTypeSpec("book.split", "_execute_book_split"),
    TaskTypeSpec("script.parse", "_execute_script_parse", billable=True, gpu_initial="LLM", gpu_stages=("LLM",),
                 entry_identity=("source_name",)),
    TaskTypeSpec("resources.scan", "_execute_resource_scan", entry_identity=("scan_scope",)),
    TaskTypeSpec("resources.package", "_execute_resource_package", entry_identity=("export_id",)),
    TaskTypeSpec("resources.cleanup", "_execute_resource_cleanup", entry_identity=("cleanup_id",)),
    # legacy 引擎（11）：engine_task_executor.ENGINE_BRANCHES
    TaskTypeSpec("voices.foundation", "_run_voices_foundation", billable=True, legacy_engine=True, gpu_initial="LLM", gpu_stages=("LLM",),
                 entry_identity=("speakers", "script")),
    TaskTypeSpec("voices.clone", "_run_voices_clone", billable=True, legacy_engine=True, gpu_initial="TTS", gpu_stages=("TTS",),
                 entry_identity=("speakers", "script")),
    TaskTypeSpec("tts.batch", "_run_tts_batch", billable=True, legacy_engine=True, gpu_initial="TTS", gpu_stages=("TTS",),
                 entry_identity=("scripts", "indices")),
    TaskTypeSpec("tts.merge", "_run_tts_merge", legacy_engine=True, entry_identity=("package",)),
    TaskTypeSpec("tts.preview_render", "_run_tts_preview_render", billable=True, legacy_engine=True, gpu_initial="TTS", gpu_stages=("TTS",),
                 entry_identity=("script", "index")),
    TaskTypeSpec("bgm.segment", "_run_bgm_segment", billable=True, legacy_engine=True, gpu_initial="LLM", gpu_stages=("LLM",),
                 entry_identity=("stem",)),
    TaskTypeSpec("bgm.mix", "_run_bgm_mix", legacy_engine=True, entry_identity=("stem",)),
    TaskTypeSpec("bgm.match", "_run_bgm_match", legacy_engine=True,
                 entry_identity=("chapters", "mode")),
    TaskTypeSpec("bgm.package", "_run_bgm_package", legacy_engine=True, entry_identity=("base",)),
    TaskTypeSpec("music.suggest_tags", "_run_music_suggest_tags", billable=True, admin_only=True, gpu_initial="LLM", gpu_stages=("LLM",),
                 legacy_engine=True, entry_identity=("name",)),
    TaskTypeSpec("tts.reset", "_run_tts_reset", legacy_engine=True, entry_identity=("scripts",)),
)

TASK_TYPES: dict[str, TaskTypeSpec] = {spec.name: spec for spec in _SPECS}

SUPPORTED_TASK_TYPES = frozenset(TASK_TYPES)
BILLABLE_TASK_TYPES = frozenset(spec.name for spec in _SPECS if spec.billable)
ADMIN_ONLY_TASK_TYPES = frozenset(spec.name for spec in _SPECS if spec.admin_only)
LEGACY_ENGINE_TASK_TYPES = frozenset(spec.name for spec in _SPECS if spec.legacy_engine)


# Coarse execution group per task prefix for the admin console.
# A different axis from the user task list's module label (group of workers,
# not display label); the metrics sampler and admin analytics share it.
_WORKER_GROUPS = {
    "script": "llm",
    "music": "llm",
    "tts": "tts",
    "voices": "tts",
    "bgm": "audio",
    "book": "system",
    "text": "system",
    "resources": "system",
}
WORKER_GROUP_NAMES = ("llm", "tts", "audio", "system", "worker")

# 已下线的任务类型（「音频分集」）：库里可能还留着历史行或升级时在途的行。
# 不再有执行器，但要给出诚实的展示名、拒绝重试，并让在途行带着明确原因失败。
RETIRED_TASK_TYPES = {
    "audio.silences": "音频分集（停顿检测，已下线）",
    "audio.cut": "音频分集（切分，已下线）",
    "audio.zip": "音频分集（打包，已下线）",
    "audio.export": "音频分集（导出，已下线）",
}


def task_worker_group(task_type: str) -> str:
    """Coarse worker group (llm/tts/audio/system/worker) for the admin console."""
    if task_type in RETIRED_TASK_TYPES:
        return "audio"
    return _WORKER_GROUPS.get(task_type.split(".", 1)[0], "worker")
