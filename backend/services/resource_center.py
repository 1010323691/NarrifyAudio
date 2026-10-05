"""Read-only resource presentation and safe preview orchestration."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from ..engines.book import decode_buffer
from ..platform.models import Task, TaskResult
from ..platform.resource_inventory import (
    MAX_PREVIEW_BYTES, ResourceError, internal_path, resolve_resource, owned_project, export_project_ids,
)


def _redact_configuration(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            secret = any(part in normalized for part in ("key", "token", "password", "secret", "authorization", "credential"))
            result[key] = "••••••" if secret and item else _redact_configuration(item)
        return result
    if isinstance(value, list):
        return [_redact_configuration(item) for item in value]
    return value


def preview_file(db, user, resource_id: str) -> dict:
    path, description, _record = resolve_resource(db, user, resource_id)
    kind = description["preview_kind"]
    if kind not in {"text", "json", "config"}:
        return {"file": description, "content": None, "truncated": False, "warning": None}
    with path.open("rb") as handle:
        data = handle.read(MAX_PREVIEW_BYTES + 1)
    truncated = len(data) > MAX_PREVIEW_BYTES
    if kind == "config" and truncated:
        return {"file": description, "content": None, "truncated": True, "warning": "配置文件过大，无法保证安全遮蔽，请前往设置检查。"}
    data = data[:MAX_PREVIEW_BYTES]
    if truncated:
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and exc.end == len(data):
                data = data[:exc.start]
    text, encoding = decode_buffer(data)
    warning = "仅展示前1MiB内容，完整检查请前往对应制作工作台。" if truncated else None
    if kind in {"json", "config"}:
        try:
            value = json.loads(text)
            if kind == "config":
                value = _redact_configuration(value)
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except (ValueError, TypeError):
            if kind == "config":
                return {"file": description, "content": None, "truncated": truncated, "warning": "配置无法解析，不能保证凭据已遮蔽；请前往设置检查。"}
            warning = warning or "JSON无法解析，正在展示原文。"
    return {"file": description, "content": text, "encoding": encoding, "truncated": truncated, "warning": warning}


def export_file(db, user, task_id: str):
    row = db.execute(select(Task, TaskResult).join(TaskResult, TaskResult.task_id == Task.id).where(
        Task.id == task_id, Task.owner_id == user.id,
        Task.task_type == "resources.package", Task.status == "succeeded",
    )).first()
    if row is None:
        raise ResourceError("打包结果不存在", 404)
    task, record = row
    from ..platform.resource_delivery import POLICY_VERSION
    if record.result.get("delivery_policy") != POLICY_VERSION:
        raise ResourceError("旧导出包未经过成品校验，请重新下载成品合集", 403)
    for project_id in export_project_ids(task):
        owned_project(db, user, project_id)
    if record.result.get("expires_at", "") <= datetime.now(timezone.utc).isoformat():
        raise ResourceError("打包结果已过期，请重新打包", 410)
    path = internal_path(db, user.id, "exports", task.id, "files.zip")
    if not path.is_file():
        raise ResourceError("打包文件不可用，请重新打包", 404)
    return path, record.result.get("export_name", "项目文件.zip")
