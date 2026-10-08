"""Entry and byte bounded caches for computed workspace summaries."""
from collections import OrderedDict
import sys


def retained_size(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(retained_size(k, seen) + retained_size(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple, set)):
        size += sum(retained_size(v, seen) for v in value)
    return size


class BoundedCache(OrderedDict):
    """Caller owns synchronization. Oversized values are returned but never cached."""
    def __init__(self, max_bytes, max_entries):
        super().__init__()
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.weights = {}
        self.retained_bytes = 0

    def __setitem__(self, key, value):
        if key in self:
            del self[key]
        weight = retained_size((key, value))
        if weight > self.max_bytes:
            return
        super().__setitem__(key, value)
        self.weights[key] = weight
        self.retained_bytes += weight
        while len(self) > self.max_entries or self.retained_bytes > self.max_bytes:
            self.popitem(last=False)

    def __delitem__(self, key):
        super().__delitem__(key)
        self.retained_bytes -= self.weights.pop(key)

    def popitem(self, last=True):
        key, value = super().popitem(last=last)
        self.retained_bytes -= self.weights.pop(key)
        return key, value

    def pop(self, key, *default):
        if key not in self:
            if default:
                return default[0]
            raise KeyError(key)
        value = self[key]
        del self[key]
        return value

    def clear(self):
        super().clear()
        self.weights.clear()
        self.retained_bytes = 0
