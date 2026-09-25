"""S1: the unified 19-type registry is the single source of truth, and the
retained legacy if-chains (shadow double-run) agree with it for every type."""
from __future__ import annotations

import dataclasses

import pytest

from backend.platform import engine_task_executor, task_worker, task_types
from backend.platform.task_contracts import TaskClaim, TaskExecutionError
from backend.platform.task_registry import (
    ADMIN_ONLY_TASK_TYPES,
    BILLABLE_TASK_TYPES,
    LEGACY_ENGINE_TASK_TYPES,
    SUPPORTED_TASK_TYPES,
    TASK_TYPES,
)


def _claim(task_type: str) -> TaskClaim:
    return TaskClaim(
        task_id="t-1", attempt_id="a-1", attempt_no=1, lease_token="tok",
        worker_id="w", owner_id="u-1", project_id="p-1", task_type=task_type, payload={},
    )


def test_table_has_all_19_types_with_policy_sets_pinned():
    assert len(TASK_TYPES) == 19
    assert set(TASK_TYPES) == SUPPORTED_TASK_TYPES
    # 策略集合按批次 2 的字面量钉扎——单一事实源即注册表，集合只能从表导出。
    assert BILLABLE_TASK_TYPES == frozenset({
        "script.parse", "voices.foundation", "voices.clone", "tts.batch",
        "bgm.analysis", "bgm.segment", "music.suggest_tags",
    })
    assert ADMIN_ONLY_TASK_TYPES == frozenset({"music.suggest_tags"})
    assert LEGACY_ENGINE_TASK_TYPES == frozenset({
        "voices.foundation", "voices.clone", "tts.batch", "tts.merge",
        "bgm.analysis", "bgm.segment", "bgm.mix", "bgm.match", "bgm.package",
        "music.suggest_tags", "audio.zip", "audio.export", "tts.reset",
    })


def test_types_module_reexports_the_registry_sets():
    assert task_types.SUPPORTED_TASK_TYPES is SUPPORTED_TASK_TYPES
    assert task_types.BILLABLE_TASK_TYPES is BILLABLE_TASK_TYPES
    assert task_types.ADMIN_ONLY_TASK_TYPES is ADMIN_ONLY_TASK_TYPES
    assert task_types.LEGACY_ENGINE_TASK_TYPES is LEGACY_ENGINE_TASK_TYPES


def test_every_type_binds_to_a_live_executor_in_its_dispatcher():
    for name, spec in TASK_TYPES.items():
        if spec.legacy_engine:
            runner = engine_task_executor.ENGINE_BRANCHES[name]
            assert runner.__name__ == spec.executor, name
            assert task_worker._shadow_dispatch_kind(name) == "legacy"
            assert engine_task_executor._shadow_engine_kind(name) == spec.executor
        else:
            runner = task_worker.DIRECT_EXECUTORS[name]
            assert runner.__name__ == spec.executor, name
            assert task_worker._shadow_dispatch_kind(name) == spec.executor
            assert engine_task_executor._shadow_engine_kind(name) == "<unsupported>"
    assert len(engine_task_executor.ENGINE_BRANCHES) == 13
    assert len(task_worker.DIRECT_EXECUTORS) == 6


def test_shadow_agrees_with_registry_for_unknown_types():
    assert task_worker._shadow_dispatch_kind("bogus.type") == "<unsupported>"
    assert engine_task_executor._shadow_engine_kind("bogus.type") == "<unsupported>"
    assert TASK_TYPES.get("bogus.type") is None


def test_unsupported_type_still_rejected_by_execute_claim():
    with pytest.raises(TaskExecutionError) as ei:
        task_worker.execute_claim(_claim("bogus.type"))
    assert ei.value.code == "unsupported_task_type"


def test_registry_mismatch_fails_closed(monkeypatch):
    # 篡改注册表（executor 名与旧链裁决不符）→ execute_claim 在执行前 fail closed。
    tampered = dict(TASK_TYPES)
    tampered["text.format"] = dataclasses.replace(
        tampered["text.format"], executor="_execute_wrong_binding")
    monkeypatch.setattr(task_worker, "TASK_TYPES", tampered)
    with pytest.raises(TaskExecutionError) as ei:
        task_worker.execute_claim(_claim("text.format"))
    assert ei.value.code == "registry_mismatch"
