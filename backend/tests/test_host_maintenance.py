"""Shared maintenance cadence, independent failures and real-process takeover."""
import multiprocessing
from pathlib import Path
from queue import Empty
import time

import pytest
from backend.platform import delivery_maintenance as maintenance


def test_schedule_bounds_recovery_and_keeps_cursor_across_failure(monkeypatch):
    clock = [100.]
    monkeypatch.setattr(maintenance.time, 'monotonic', lambda: clock[0])
    recovered, published, retained = [], [], []
    def recover(**kwargs):
        recovered.append(kwargs)
        if len(recovered) == 2: raise RuntimeError('database unavailable')
        return 0, 'cursor-100'
    def publish(**kwargs):
        published.append(kwargs)
        raise RuntimeError('Redis unavailable')
    def retention(): retained.append(True); return 0
    schedule = maintenance.MaintenanceSchedule(recover=recover, publish=publish, retention=retention)
    schedule.tick()
    for _ in range(100): schedule.tick()
    assert len(recovered) == len(published) == len(retained) == 1
    clock[0] += 10
    schedule.tick()
    assert schedule.cursor == 'cursor-100'
    clock[0] += 10
    schedule.tick()
    assert recovered == [dict(limit=100, after_id=''), dict(limit=100, after_id='cursor-100'), dict(limit=100, after_id='cursor-100')]
    clock[0] = 160
    schedule.tick()
    assert len(retained) == 2
    clock[0] = 161
    schedule.tick()
    assert len(retained) == 2


class EmptyDatabase:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def commit(self): pass


def test_schedule_continues_incomplete_retention_rounds_on_next_pass(monkeypatch):
    clock = [100.]
    monkeypatch.setattr(maintenance.time, 'monotonic', lambda: clock[0])
    rounds = []
    def retention():
        rounds.append(round(clock[0], 1))
        return (20, True) if len(rounds) <= 2 else (5, False)
    schedule = maintenance.MaintenanceSchedule(retention=retention)
    schedule.tick()
    clock[0] += 59.9
    schedule.tick()
    assert len(rounds) == 1
    clock[0] += 0.1
    schedule.tick()
    assert len(rounds) == 2
    clock[0] += 60
    schedule.tick()
    assert len(rounds) == 3
    clock[0] += 3600
    schedule.tick()
    assert len(rounds) == 3


def test_schedule_keeps_legacy_int_retention_contract(monkeypatch):
    clock = [100.]
    monkeypatch.setattr(maintenance.time, 'monotonic', lambda: clock[0])
    purged = []
    def retention():
        purged.append(0)
        return 0
    schedule = maintenance.MaintenanceSchedule(retention=retention)
    schedule.tick()
    clock[0] += 60
    schedule.tick()
    assert len(purged) == 2


def _coordinate(root, index, stopped, messages):
    maintenance.PROJECT_ROOT = Path(root)
    maintenance.SessionLocal = EmptyDatabase
    maintenance.lock_storage_migration = lambda *args, **kwargs: True
    maintenance.storage_migration = lambda *args: None
    maintenance.backfill_pending_project = lambda *args: False
    maintenance.retire_legacy_artifacts = lambda *args: ('', [])
    class Stop:
        def is_set(self): return bool(stopped.value)
        def wait(self, seconds):
            time.sleep(min(seconds, .1))
            return self.is_set()
    def publish(**kwargs): messages.put(index)
    maintenance.run(Stop(), publish=publish)


def test_two_processes_elect_one_owner_and_take_over_after_owner_exit(tmp_path):
    context = multiprocessing.get_context('spawn')
    messages = context.Queue()
    # A force-killed process may die inside Event.wait holding its shared lock.
    # Lock-free flags keep the harness cleanup independent of the killed owner.
    stops = [context.Value('b', 0, lock=False), context.Value('b', 0, lock=False)]
    processes = [context.Process(target=_coordinate, args=(str(tmp_path), index, stops[index], messages)) for index in range(2)]
    try:
        for process in processes: process.start()
        owner = messages.get(timeout=15)
        seen = {owner}
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            try: seen.add(messages.get(timeout=.1))
            except Empty: pass
        assert seen == {owner}
        processes[owner].terminate()
        processes[owner].join(timeout=5)
        deadline = time.monotonic() + 10
        while True:
            successor = messages.get(timeout=max(.1, deadline - time.monotonic()))
            if successor != owner: break
            assert time.monotonic() < deadline
        assert successor == 1 - owner
    finally:
        for stop in stops: stop.value = 1
        for process in processes:
            process.join(timeout=5)
            if process.is_alive(): process.terminate(); process.join(timeout=5)


def test_one_shot_dispatch_respects_existing_owner(tmp_path, monkeypatch):
    from backend.core.file_lock import exclusive_file_lock
    monkeypatch.setattr(maintenance, 'PROJECT_ROOT', tmp_path)
    calls = []
    with exclusive_file_lock(tmp_path / '.narrify' / 'host-maintenance.lock'):
        assert not maintenance.run_dispatch_once(recover=lambda **kw: calls.append('recover'),
                                                  publish=lambda **kw: calls.append('publish'))
    assert calls == []
    assert maintenance.run_dispatch_once(recover=lambda **kw: calls.append(('recover', kw)),
                                          publish=lambda **kw: calls.append(('publish', kw)))
    assert calls == [('recover', {'limit': 100}), ('publish', {'limit': 100})]
