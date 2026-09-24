from __future__ import annotations

from multiprocessing import get_context

import pytest

from backend.core.file_lock import exclusive_file_lock


def _hold_lock(path, ready, release):
    with exclusive_file_lock(path):
        ready.set()
        release.wait(10)


def test_exclusive_file_lock_blocks_a_second_process(tmp_path):
    context = get_context("spawn")
    ready = context.Event()
    release = context.Event()
    path = tmp_path / "shared.lock"
    child = context.Process(target=_hold_lock, args=(path, ready, release))
    child.start()
    try:
        assert ready.wait(10)
        with pytest.raises(TimeoutError):
            with exclusive_file_lock(path, timeout=0.2):
                pass
    finally:
        release.set()
        child.join(10)
        if child.is_alive():
            child.terminate()
            child.join(2)
    assert child.exitcode == 0
    with exclusive_file_lock(path, timeout=1):
        pass
