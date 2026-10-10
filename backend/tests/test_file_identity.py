import os

import pytest
from types import SimpleNamespace

from backend.core.safe_filesystem import file_identity, identity_matches


def test_posix_identity_ignores_device_number(tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"x")
    stat = path.stat()
    moved = SimpleNamespace(st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns,
                            st_ctime_ns=stat.st_ctime_ns, st_dev=stat.st_dev + 2, st_ino=stat.st_ino)
    if os.name != "nt":
        assert file_identity(moved) == file_identity(stat)


@pytest.mark.skipif(os.name == "nt", reason="Windows compares the volume serial")
def test_legacy_identity_with_real_device_still_matches():
    current = [10, 1, 2, 0, 99]
    assert identity_matches(current, [10, 1, 2, 66308, 99])
    assert not identity_matches(current, [10, 1, 3, 66308, 99])
    assert not identity_matches(current, [10, 1, 2, 66308, 100])


@pytest.mark.skipif(os.name == "nt", reason="Windows compares the volume serial")
def test_identity_matches_handles_legacy_and_missing_records(tmp_path):
    path = tmp_path / "a.mp3"
    path.write_bytes(b"audio")
    legacy = list(file_identity(path.stat()))
    legacy[3] = 66308
    assert identity_matches(file_identity(path.stat()), legacy)
    assert not identity_matches(None, legacy)
    assert identity_matches(None, None)


@pytest.mark.skipif(os.name == "nt", reason="Windows compares the volume serial")
def test_attach_deliveries_matches_legacy_device_record():
    import sqlite3
    from backend.platform.resource_inventory import attach_deliveries
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE entries(relative_path TEXT, size_bytes INT, mtime_ns INT, ctime_ns INT, device TEXT, inode TEXT)")
    connection.execute("INSERT INTO entries VALUES('08_bgm/a.mp3', 10, 1, 2, '0', '99')")
    attach_deliveries(connection, {"08_bgm/a.mp3": {"identity": [10, 1, 2, 66308, 99]}})
    assert connection.execute("SELECT relative_path FROM eligible_deliveries").fetchall() == [("08_bgm/a.mp3",)]
