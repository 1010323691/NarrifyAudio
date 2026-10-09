"""Task admission policies and registry-to-executor consistency."""
from __future__ import annotations

import pytest

from backend.platform import engine_task_executor, task_worker
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


def test_registry_policy_and_dispatch_contract():
    # table has all types with policy sets pinned
    assert len(TASK_TYPES) == 19
    assert set(TASK_TYPES) == SUPPORTED_TASK_TYPES
    # 策略集合按批次 2 的字面量钉扎——单一事实源即注册表，集合只能从表导出。
    assert BILLABLE_TASK_TYPES == frozenset({
        "script.parse", "voices.foundation", "voices.clone", "tts.batch",
        "tts.preview_render", "bgm.segment", "music.suggest_tags",
    })
    assert ADMIN_ONLY_TASK_TYPES == frozenset({"music.suggest_tags"})
    assert LEGACY_ENGINE_TASK_TYPES == frozenset({
        "voices.foundation", "voices.clone", "tts.batch", "tts.merge",
        "tts.preview_render", "bgm.segment", "bgm.mix", "bgm.match", "bgm.package",
        "music.suggest_tags", "tts.reset",
    })
    # Direct executors cover the remaining admitted task types.
    assert set(SUPPORTED_TASK_TYPES) - LEGACY_ENGINE_TASK_TYPES == frozenset({
        "text.format", "book.analyze", "book.split", "script.parse",
        "resources.scan", "resources.package", "resources.cleanup",
        "project.progress",
    })

    # every type binds to a live executor in its dispatcher
    # 注册表 executor 名必须与分发器函数表里的活函数一致（影子双跑清退后这是
    # 表与运行时分发之间的一致性保证，替代旧的运行时交叉核对）。
    for name, spec in TASK_TYPES.items():
        if spec.legacy_engine:
            runner = engine_task_executor.ENGINE_BRANCHES[name]
            assert runner.__name__ == spec.executor, name
        else:
            runner = task_worker.DIRECT_EXECUTORS[name]
            assert runner.__name__ == spec.executor, name
    assert len(engine_task_executor.ENGINE_BRANCHES) == 11
    assert len(task_worker.DIRECT_EXECUTORS) == 8


def test_unsupported_type_still_rejected_by_execute_claim():
    assert TASK_TYPES.get("bogus.type") is None
    with pytest.raises(TaskExecutionError) as ei:
        task_worker.execute_claim(_claim("bogus.type"))
    assert ei.value.code == "unsupported_task_type"


def test_retired_audio_split_types_are_labelled_unretryable_and_fail_clearly():
    import pytest

    from backend.platform.task_contracts import TaskExecutionError
    from backend.platform.task_registry import RETIRED_TASK_TYPES, TASK_TYPES, task_worker_group
    from backend.platform.task_worker import execute_claim
    from backend.services.task_views import task_display_label

    assert set(RETIRED_TASK_TYPES) == {"audio.silences", "audio.cut", "audio.zip", "audio.export"}
    assert not set(RETIRED_TASK_TYPES) & set(TASK_TYPES)
    for task_type in RETIRED_TASK_TYPES:
        assert "已下线" in task_display_label(task_type, {})
        assert task_worker_group(task_type) == "audio"

    class Claim:
        task_type = "audio.cut"
        payload: dict = {}

    with pytest.raises(TaskExecutionError) as caught:
        execute_claim(Claim())
    assert caught.value.code == "task_type_retired"
