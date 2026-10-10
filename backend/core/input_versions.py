"""Cheap version-bound input references and explicit execution checkpoints."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path

from .safe_filesystem import file_identity, is_link_or_junction, safe_regular_path

_validator = ContextVar("narrify_input_validator", default=None)
_metadata = ContextVar("narrify_input_metadata", default=None)


def bind_metadata(value):
    return _metadata.set(value)


def reset_metadata(token):
    _metadata.reset(token)


def input_metadata():
    return _metadata.get() or {}


def value_version(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _identity(root, name):
    if is_link_or_junction(Path(root)):
        raise ValueError("链接输入目录不可访问")
    if not isinstance(name, str) or not name or any(part in ("", ".", "..") for part in name.split("/")) or "\\" in name or ":" in name:
        raise ValueError("输入引用路径无效")
    path = Path(root) / name
    # Missing optional inputs are versions too: their later appearance changes
    # the selected source (e.g. MP3 taking precedence over a WAV fallback).
    if not path.exists():
        if is_link_or_junction(path):
            raise ValueError("链接输入不可访问")
        path.resolve().relative_to(Path(root).resolve())
        return None
    path = safe_regular_path(Path(root), name)
    return list(file_identity(path.stat()))


def capture_files(root, names):
    return [{"path": name, "identity": _identity(root, name)} for name in dict.fromkeys(names)]


def _same_identity(current, recorded):
    # Identities recorded before st_dev was dropped from file_identity still
    # carry a (reboot-unstable) device number in slot 3; ignore it for them.
    if current and recorded and len(current) == len(recorded) == 5:
        return current[:3] == recorded[:3] and current[4] == recorded[4]
    return current == recorded


def validate_files(root, records):
    for record in records:
        if not _same_identity(_identity(root, record["path"]), record["identity"]):
            raise RuntimeError("任务输入已变更，请重新提交。")


def check_bound_inputs():
    validator = _validator.get()
    if validator is not None:
        validator()


@contextmanager
def validating_inputs(validator):
    token = _validator.set(validator)
    try:
        validator()
        yield
        validator()
    finally:
        _validator.reset(token)
