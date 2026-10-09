"""Small managed-write epochs, shared across processes; no directory scans on reads."""
from contextlib import contextmanager
import json
import os
import threading
import time
from pathlib import Path
import uuid

from .file_lock import exclusive_file_lock
from .managed_process import identity_alive, process_identity
from .request_context import bound_workspace, _UNSET
from .safe_filesystem import is_link_or_junction, file_identity

MODULES = tuple(f'{index:02}_{name}' for index, name in enumerate(
    ('input', 'split_text', 'parsed_json', 'voice_profiles', 'audio_chunk', 'audio_merge', 'output', 'bgm'), 1))
LIMIT = 65536
RELEASE_RETRIES = 3
# Writer tokens whose release failed in this (long-lived) process; the next
# advance on the same workspace drops them so `busy` cannot stay true forever.
_unreleased = {}
_unreleased_guard = threading.Lock()


def _paths(root):
    root = Path(root)
    temporary = root / '00_temp'
    path, lock = temporary / 'workspace-epochs.json', temporary / 'workspace-epochs.lock'
    if any(is_link_or_junction(item) for item in (root, temporary, path, lock)):
        raise ValueError('成果版本路径不可使用链接')
    return path, lock


def _read(path):
    try:
        with path.open('rb') as stream:
            encoded = stream.read(LIMIT + 1)
        if len(encoded) > LIMIT:
            raise ValueError('成果版本记录过大')
        data = json.loads(encoded)
        if not isinstance(data, dict) or not isinstance(data.get('versions'), dict) or not isinstance(data.get('writers'), dict):
            raise ValueError('成果版本记录无效')
        return data
    except FileNotFoundError:
        return {'versions': {}, 'writers': {}}


def _alive(identity):
    try:
        return not identity or identity_alive(identity)
    except (OSError, RuntimeError):
        return True


def versions(root, modules=MODULES):
    """Return versions and active-writer flag; corrupted metadata forces strict readers."""
    path, _lock = _paths(root)
    try:
        data = _read(path)
        selected = tuple(data['versions'].get(module, 'legacy') for module in modules)
        busy = any(writer.get('module') in modules and _alive(writer.get('identity'))
                   for writer in data['writers'].values() if isinstance(writer, dict))
        return selected, busy
    except (OSError, ValueError, TypeError):
        try:
            identity = file_identity(path.stat())
        except OSError:
            identity = None
        return ('unreadable', identity), True


def workspace_for(path):
    path = Path(path).absolute()
    bound = bound_workspace()
    if bound not in (None, _UNSET):
        root = Path(bound).absolute()
        if path.is_relative_to(root):
            relative = path.relative_to(root)
            return (root, relative.parts[0]) if relative.parts and relative.parts[0] in MODULES else None
    # Publication also runs without an engine context. Only recognize an
    # existing workspace anchor; never create metadata in arbitrary user dirs.
    candidates = [(parent.parent, parent.name) for parent in (path, *path.parents) if parent.name in MODULES
                  and (parent.parent / '00_temp').is_dir()]
    return candidates[-1] if candidates else None


def _advance(root, module, token, *, starting):
    path, lock = _paths(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(lock):
        try:
            data = _read(path)
        except (ValueError, TypeError):
            data = {'versions': {}, 'writers': {}}
        data['writers'] = {key: value for key, value in data['writers'].items()
                           if isinstance(value, dict) and _alive(value.get('identity'))}
        with _unreleased_guard:
            stale_tokens = set(_unreleased.get(str(path), ()))
        for stale in stale_tokens:
            data['writers'].pop(stale, None)
        if starting:
            data['writers'][token] = {'module': module, 'identity': process_identity(os.getpid())}
        else:
            data['writers'].pop(token, None)
        data['versions'][module] = uuid.uuid4().hex
        encoded = json.dumps(data, separators=(',', ':')).encode()
        if len(encoded) > LIMIT:
            raise ValueError('同时写入成果的操作过多')
        pending = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
        try:
            with pending.open('xb') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
        if stale_tokens:
            # Forget them only once the cleaned file is actually on disk.
            with _unreleased_guard:
                remaining = _unreleased.get(str(path), set()) - stale_tokens
                if remaining:
                    _unreleased[str(path)] = remaining
                else:
                    _unreleased.pop(str(path), None)


@contextmanager
def managed_mutation(path):
    """Invalidate before and after writes, including failure/rollback; expose in-flight writers."""
    selected = workspace_for(path)
    if selected is None:
        yield
        return
    root, module = selected
    token = uuid.uuid4().hex
    _advance(root, module, token, starting=True)
    try:
        yield
    finally:
        _release(root, module, token)


def _release(root, module, token):
    error = None
    for attempt in range(RELEASE_RETRIES):
        try:
            _advance(root, module, token, starting=False)
            return
        except Exception as caught:
            error = caught
            if attempt + 1 < RELEASE_RETRIES:
                time.sleep(0.05 * (attempt + 1))
    with _unreleased_guard:
        _unreleased.setdefault(str(Path(root) / '00_temp' / 'workspace-epochs.json'), set()).add(token)
    raise error
