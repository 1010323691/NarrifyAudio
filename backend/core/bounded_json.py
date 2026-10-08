"""Read JSON inputs without allocating an unbounded file buffer."""
import json


def read_json(path, maximum=8 * 1024 * 1024):
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError('文件超过 8MiB 安全读取上限')
    return json.loads(data.decode('utf-8'))
