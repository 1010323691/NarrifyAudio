"""The unified registry of the 19 durable task types (plan S1).

One explicit declaration per type: name, executor binding, billing, permission.
Both dispatchers (``task_worker.execute_claim`` and
``engine_task_executor.execute_engine_task``) resolve their dispatch decision
through :data:`TASK_TYPES`; the policy frozensets that used to live in
``task_types.py`` are derived from this table, so the table is the single
source of truth.

The executor column names the binding entry in the owning dispatcher's
explicit function map (``task_worker.DIRECT_EXECUTORS`` for the six platform
direct types, ``engine_task_executor.ENGINE_BRANCHES`` for the thirteen
legacy engine types). The table deliberately holds names, not live function
references: a single literal table holding the references would create a
module cycle (both dispatchers import the type frozensets at module level),
and the plan rules out decorator-based implicit registration. Each dispatch
cross-checks the table against the retained legacy if-chain (shadow
double-run, fail-closed) for one version cycle, after which the old chains
are deleted.
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


_SPECS: tuple[TaskTypeSpec, ...] = (
    # 平台直连（6）：task_worker.DIRECT_EXECUTORS
    TaskTypeSpec("text.format", "_execute_text_format"),
    TaskTypeSpec("book.analyze", "_execute_book_analyze"),
    TaskTypeSpec("book.split", "_execute_book_split"),
    TaskTypeSpec("script.parse", "_execute_script_parse", billable=True),
    TaskTypeSpec("audio.silences", "_execute_audio_silences"),
    TaskTypeSpec("audio.cut", "_execute_audio_cut"),
    # legacy 引擎（13）：engine_task_executor.ENGINE_BRANCHES
    TaskTypeSpec("voices.foundation", "_run_voices_foundation", billable=True, legacy_engine=True),
    TaskTypeSpec("voices.clone", "_run_voices_clone", billable=True, legacy_engine=True),
    TaskTypeSpec("tts.batch", "_run_tts_batch", billable=True, legacy_engine=True),
    TaskTypeSpec("tts.merge", "_run_tts_merge", legacy_engine=True),
    TaskTypeSpec("bgm.analysis", "_run_bgm_analysis", billable=True, legacy_engine=True),
    TaskTypeSpec("bgm.segment", "_run_bgm_segment", billable=True, legacy_engine=True),
    TaskTypeSpec("bgm.mix", "_run_bgm_mix", legacy_engine=True),
    TaskTypeSpec("bgm.match", "_run_bgm_match", legacy_engine=True),
    TaskTypeSpec("bgm.package", "_run_bgm_package", legacy_engine=True),
    TaskTypeSpec("music.suggest_tags", "_run_music_suggest_tags", billable=True, admin_only=True, legacy_engine=True),
    TaskTypeSpec("audio.zip", "_run_audio_zip", legacy_engine=True),
    TaskTypeSpec("audio.export", "_run_audio_export", legacy_engine=True),
    TaskTypeSpec("tts.reset", "_run_tts_reset", legacy_engine=True),
)

TASK_TYPES: dict[str, TaskTypeSpec] = {spec.name: spec for spec in _SPECS}

SUPPORTED_TASK_TYPES = frozenset(TASK_TYPES)
BILLABLE_TASK_TYPES = frozenset(spec.name for spec in _SPECS if spec.billable)
ADMIN_ONLY_TASK_TYPES = frozenset(spec.name for spec in _SPECS if spec.admin_only)
LEGACY_ENGINE_TASK_TYPES = frozenset(spec.name for spec in _SPECS if spec.legacy_engine)
