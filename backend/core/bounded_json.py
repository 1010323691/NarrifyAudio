"""Read JSON inputs without allocating an unbounded file buffer."""
import json
from functools import lru_cache


def read_json(path, maximum=8 * 1024 * 1024):
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError('文件超过 8MiB 安全读取上限')
    return json.loads(data.decode('utf-8'))


def is_script_data(data):
    """Script identity includes empty scripts, but excludes analysis reports."""
    return isinstance(data, list) and all(
        isinstance(entry, dict) and isinstance(entry.get('text', ''), str)
        for entry in data
    )


@lru_cache(maxsize=4096)
def _script_file_identity(path, modified, size):
    try:
        return is_script_data(read_json(path))
    except (OSError, ValueError, UnicodeError):
        return False


def is_script_file(path):
    """Cache only identity, invalidating whenever the source fingerprint changes."""
    try:
        stat = path.stat()
    except OSError:
        return False
    return _script_file_identity(path, stat.st_mtime_ns, stat.st_size)
