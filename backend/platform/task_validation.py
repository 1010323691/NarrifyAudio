"""Pure validation shared by public task submission and worker boundaries."""
from __future__ import annotations

from pathlib import PurePath
import re


def is_safe_path_component(value: object) -> bool:
    """Whether a client-provided name can be used as one filename component."""
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
        and PurePath(value).name == value
    )


def is_safe_bgm_stem(value: object) -> bool:
    """Whether a chapter stem can be used as one filename component."""
    return is_safe_path_component(value)


def _safe_name_list(payload: dict, key: str, *, required: bool = False) -> bool:
    names = payload.get(key)
    if names is None:
        return not required
    return isinstance(names, list) and (bool(names) or not required) and all(
        is_safe_path_component(name) for name in names
    )


def _preview_render_items_ok(render: object) -> bool:
    """Shape of ``render``: ``[{index: non-negative int, text?/speaker?/instruct? str}]``.

    The ``render`` entries deliberately avoid a ``{index: {…}}`` dict form — a task payload
    persists as JSON, where integer keys would drift to strings and re-parse differently
    on the Worker. Each entry carries only the fields the caller actually changed.
    """
    if not isinstance(render, list) or not render:
        return False
    for item in render:
        if not isinstance(item, dict):
            return False
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            return False
        for key in ("text", "speaker", "instruct"):
            value = item.get(key)
            if value is not None and not isinstance(value, str):
                return False
    return True


def legacy_task_payload_error(task_type: str, payload: object) -> str | None:
    """Validate client-controlled path components used by legacy task engines."""
    if not isinstance(payload, dict):
        return "任务参数无效"
    if task_type.startswith("resources."):
        return resource_task_payload_error(task_type, payload)
    if task_type in {"bgm.segment", "bgm.mix"}:
        if not is_safe_bgm_stem(payload.get("stem")):
            return "BGM 章节参数无效"
    elif task_type in {"bgm.match", "bgm.package"}:
        chapters = payload.get("chapters")
        if task_type == "bgm.package" and (not isinstance(chapters, list) or not chapters):
            return "BGM 打包章节参数无效"
        if chapters is not None and (
            not isinstance(chapters, list)
            or any(not is_safe_bgm_stem(stem) for stem in chapters)
        ):
            return "BGM 章节参数无效"
    elif task_type in {"voices.foundation", "voices.clone", "tts.batch"}:
        script = payload.get("script")
        if script not in (None, "") and not is_safe_path_component(script):
            return "剧本文件名无效"
        if not _safe_name_list(payload, "scripts"):
            return "剧本文件名无效"
    elif task_type == "tts.reset":
        if not _safe_name_list(payload, "scripts"):
            return "剧本文件名无效"
    elif task_type == "tts.merge":
        package = payload.get("package")
        if package not in (None, "") and not is_safe_path_component(package):
            return "音频包名称无效"
    elif task_type == "tts.preview_render":
        if not is_safe_path_component(payload.get("script")):
            return "剧本文件名无效"
        if not _preview_render_items_ok(payload.get("render")):
            return "预览渲染行参数无效"
    elif task_type == "music.suggest_tags":
        if not is_safe_path_component(payload.get("name")):
            return "音乐文件名无效"
    return None


def resource_task_payload_error(task_type: str, payload: dict) -> str | None:
    def valid_refs(items: object) -> bool:
        return isinstance(items, list) and 0 < len(items) <= 1000 and all(
            isinstance(item, dict) and is_safe_path_component(item.get("project_id"))
            and isinstance(item.get("snapshot_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", item["snapshot_id"]) is not None
            for item in items
        )

    if task_type == "resources.scan":
        ids = payload.get("project_ids")
        if ids is not None and (not isinstance(ids, list) or not 0 < len(ids) <= 1000 or not all(is_safe_path_component(item) for item in ids)):
            return "资源扫描项目参数无效"
        if payload.get("include_trash") not in (None, True, False):
            return "资源扫描范围无效"
    elif task_type == "resources.cleanup":
        if not valid_refs(payload.get("snapshots")):
            return "缓存清理快照参数无效"
    elif task_type == "resources.package":
        files, scope = payload.get("files"), payload.get("scope")
        if bool(files) == bool(scope):
            return "请选择文件或指定打包范围"
        if files:
            if not isinstance(files, list) or len(files) > 1000 or any(
                not isinstance(item, dict) or not isinstance(item.get("resource_id"), str)
                or re.fullmatch(r"[^/:\\]+:[0-9a-f]{64}", item["resource_id"]) is None
                or not isinstance(item.get("snapshot_id"), str)
                or re.fullmatch(r"[0-9a-f]{32}", item["snapshot_id"]) is None
                for item in files
            ):
                return "打包文件参数无效"
        elif not isinstance(scope, dict) or not valid_refs(scope.get("snapshots")):
            return "打包范围快照无效"
        if scope:
            for key in ("category", "path", "query", "extension"):
                if scope.get(key) is not None and (not isinstance(scope[key], str) or len(scope[key]) > 1024):
                    return "打包筛选条件无效"
        if payload.get("name") is not None and (not isinstance(payload["name"], str) or len(payload["name"]) > 160):
            return "打包名称无效"
    return None
