"""Task types accepted by the durable task API and executable by its Worker.

S1（批次 3）起，类型知识的单一事实源是 :mod:`backend.platform.task_registry`
（19 种类型的显式注册表）；本模块只做 re-export，保持既有导入面不变。
"""
from .task_registry import (  # noqa: F401
    ADMIN_ONLY_TASK_TYPES,
    BILLABLE_TASK_TYPES,
    LEGACY_ENGINE_TASK_TYPES,
    SUPPORTED_TASK_TYPES,
)
