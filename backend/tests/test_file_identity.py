import os
from types import SimpleNamespace

from backend.core.input_versions import _same_identity
from backend.core.safe_filesystem import file_identity


def test_posix_identity_ignores_device_number(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    stat = path.stat()
    moved = SimpleNamespace(st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns,
                            st_ctime_ns=stat.st_ctime_ns, st_dev=stat.st_dev + 2, st_ino=stat.st_ino)
    if os.name != "nt":
        assert file_identity(moved) == file_identity(stat)


def test_legacy_identity_with_real_device_still_matches():
    current = [10, 1, 2, 0, 99]
    assert _same_identity(current, [10, 1, 2, 66308, 99])
    assert not _same_identity(current, [10, 1, 3, 66308, 99])
    assert not _same_identity(current, [10, 1, 2, 66308, 100])
