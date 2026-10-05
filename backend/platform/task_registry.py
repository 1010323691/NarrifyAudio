"""The unified registry of durable task types.

One explicit declaration per type: name, executor binding, billing, permission.
Both dispatchers (``task_worker.execute_claim`` and
``engine_task_executor.execute_engine_task``) resolve their dispatch decision
through :data:`TASK_TYPES`; the policy frozensets that used to live in
``task_types.py`` are derived from this table, so the table is the single
source of truth.

The executor column names the binding entry in the owning dispatcher's
explicit function map (``task_worker.DIRECT_EXECUTORS`` for the platform
direct types, ``engine_task_executor.ENGINE_BRANCHES`` for the thirteen
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
    # 平台直连（6）：task_worker.DIRECT_EXECUTORS
    TaskTypeSpec("text.format", "_execute_text_format"),
    TaskTypeSpec("book.analyze", "_execute_book_analyze"),
    TaskTypeSpec("book.split", "_execute_book_split"),
    TaskTypeSpec("script.parse", "_execute_script_parse", billable=True, gpu_initial="LLM", gpu_stages=("LLM",),
                 entry_identity=("source_name",)),
    TaskTypeSpec("audio.silences", "_execute_audio_silences",
                 entry_identity=("source_name",)),
    TaskTypeSpec("audio.cut", "_execute_audio_cut", entry_identity=("source_name",)),
    TaskTypeSpec("resources.scan", "_execute_resource_scan", entry_identity=("scan_scope",)),
    TaskTypeSpec("resources.package", "_execute_resource_package", entry_identity=("export_id",)),
    TaskTypeSpec("resources.cleanup", "_execute_resource_cleanup", entry_identity=("cleanup_id",)),
    # legacy 引擎（13）：engine_task_executor.ENGINE_BRANCHES
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
    TaskTypeSpec("audio.zip", "_run_audio_zip", legacy_engine=True, entry_identity=("base",)),
    TaskTypeSpec("audio.export", "_run_audio_export", legacy_engine=True,
                 entry_identity=("source_relative",)),
    TaskTypeSpec("tts.reset", "_run_tts_reset", legacy_engine=True, entry_identity=("scripts",)),
)

TASK_TYPES: dict[str, TaskTypeSpec] = {spec.name: spec for spec in _SPECS}

SUPPORTED_TASK_TYPES = frozenset(TASK_TYPES)
BILLABLE_TASK_TYPES = frozenset(spec.name for spec in _SPECS if spec.billable)
ADMIN_ONLY_TASK_TYPES = frozenset(spec.name for spec in _SPECS if spec.admin_only)
LEGACY_ENGINE_TASK_TYPES = frozenset(spec.name for spec in _SPECS if spec.legacy_engine)
