"""Offline scheduling, stage admission, lifecycle and administrator regressions."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from backend.core.managed_process import spawn_owned, terminate_owned, close_owned, tree_exited, process_identity, identity_alive, script_command
from backend.platform.database import SessionLocal
from backend.platform.models import GPURequest, GPUSchedulerState, SystemConfig, Task, utcnow, new_id
from backend.platform.gpu_scheduler.config import GPUConfig, load_config, service_fingerprint
from backend.platform.gpu_scheduler.policy import SchedulingPolicy, QueueStats
from backend.platform.gpu_scheduler.store import transaction, read_state, QueueMonitor, process_reap, stamp
from backend.platform.gpu_scheduler.admission import gpu_permit, claim_allowed
from backend.platform.gpu_scheduler.runtime import Scheduler, status_snapshot


@pytest.fixture(autouse=True)
def clean_gpu(monkeypatch, tmp_path):
    from backend.platform.gpu_scheduler import store
    monkeypatch.setattr(store, "PROJECT_ROOT", tmp_path)
    with SessionLocal.begin() as db:
        db.execute(delete(GPURequest))
        db.execute(delete(GPUSchedulerState))
        db.execute(delete(SystemConfig).where(SystemConfig.key == "gpu_scheduler"))
    yield
    with SessionLocal.begin() as db:
        db.execute(delete(GPURequest))
        db.execute(delete(GPUSchedulerState))
        db.execute(delete(SystemConfig).where(SystemConfig.key == "gpu_scheduler"))


def enabled_config(tmp_path, **kwargs):
    script = tmp_path / ("LLM 启动 脚本.ps1" if os.name == "nt" else "LLM 启动 脚本.sh")
    script.write_text("exit 0", encoding="utf-8")
    return GPUConfig(enabled=True, llm_start_script_path=str(script), **kwargs)


def activate(config, side="LLM"):
    with transaction() as (db, state):
        row = db.get(SystemConfig, "gpu_scheduler")
        if row is None:
            db.add(SystemConfig(key="gpu_scheduler", value=config.model_dump()))
        else:
            row.value = config.model_dump()
        state.update(state=f"{side}_ACTIVE", current=side, managed=True, served=True,
                     service_config=service_fingerprint(config), active_since=stamp()-100, last_switch=stamp()-100)


def test_queue_examples():
    for current, llm, tts, target in [
        ("LLM", 10, 0, "LLM"), ("LLM", 0, 10, "TTS"), ("LLM", 10, 11, "LLM"),
        ("LLM", 10, 30, "TTS"), ("TTS", 30, 10, "LLM"), ("TTS", 0, 0, "TTS"),
        (None, 10, 0, "LLM"), (None, 0, 10, "TTS"), (None, 0, 0, None),
    ]:
        result = SchedulingPolicy().decide(GPUConfig(), current, QueueStats(llm), QueueStats(tts),
                                           runtime=300, since_switch=300, idle_time=0, served=True)
        assert result.target == target, (current, llm, tts, target,)


def test_time_guards():
    for runtime, cooldown in [(299,1000), (300,29)]:
        result = SchedulingPolicy().decide(GPUConfig(), "LLM", QueueStats(1), QueueStats(100),
                                           runtime=runtime, since_switch=cooldown, idle_time=0, served=True)
        assert result.target == "LLM", (runtime, cooldown,)


def test_starvation_respects_minimum_stay_and_requires_service():
    policy = SchedulingPolicy()
    for runtime, served, target in [(299, True, "TTS"), (300, True, "LLM"), (300, False, "TTS")]:
        result = policy.decide(GPUConfig(), "TTS", QueueStats(3, oldest_wait=301), QueueStats(100, oldest_wait=301),
                               runtime=runtime, since_switch=0, idle_time=0, served=served)
        assert result.target == target


def test_idle_and_initial_oldest_tie():
    policy = SchedulingPolicy()
    assert policy.decide(GPUConfig(), None, QueueStats(2, oldest_wait=5), QueueStats(2, oldest_wait=10),
                         runtime=0, since_switch=0, idle_time=0, served=False).target == "TTS"
    for running, target in [(0, None), (1, "LLM")]:
        assert policy.decide(GPUConfig(shutdown_when_idle=True), "LLM", QueueStats(running=running), QueueStats(),
                             runtime=1000, since_switch=1000, idle_time=301, served=True).target == target


def test_policy_pressure_is_replaceable():
    class Weighted(SchedulingPolicy):
        def calculate_llm_pressure(self, queue):
            return queue.waiting * 30
    assert Weighted().decide(GPUConfig(), "TTS", QueueStats(2), QueueStats(20),
                             runtime=300, since_switch=300, idle_time=0, served=True).target == "LLM"


def test_defaults_disabled_and_validation(tmp_path):
    assert not load_config().enabled
    for payload in [{"enabled": True}, {"scheduler_interval": 0}, {"startup_retry_count": 0},
                    {"unknown": True}, {"llm_start_script_path": "relative.ps1"}]:
        with pytest.raises(ValueError):
            GPUConfig.model_validate(payload)
    path = str(tmp_path / "中文 空格.ps1")
    assert script_command(path)[-1] == path
    with pytest.raises(ValueError):
        script_command(str(tmp_path / "unsafe&script.cmd"))


def test_cpu_claims_continue_and_inactive_gpu_claims_wait(tmp_path):
    activate(enabled_config(tmp_path))
    assert claim_allowed("text.format")
    assert claim_allowed("tts.merge")
    assert claim_allowed("bgm.match")
    assert claim_allowed("script.parse")
    assert claim_allowed("bgm.segment")
    assert not claim_allowed("tts.batch")
    with transaction() as (_db, state):
        state.update(state="SWITCHING_TO_TTS", phase="DRAINING")
    assert not claim_allowed("script.parse")
    assert not claim_allowed("tts.batch")


def test_registry_gpu_metadata_matches_actual_engines():
    from backend.platform.task_registry import TASK_TYPES
    assert {name for name, spec in TASK_TYPES.items() if spec.gpu_initial == "LLM"} == {
        "script.parse", "voices.foundation", "bgm.segment", "music.suggest_tags"}
    assert {name for name, spec in TASK_TYPES.items() if spec.gpu_initial == "TTS"} == {
        "voices.clone", "tts.batch", "tts.preview_render"}


def test_new_config_atomically_closes_admission(tmp_path):
    config = enabled_config(tmp_path)
    activate(config)
    with transaction() as (db, _state):
        row = db.get(SystemConfig, "gpu_scheduler")
        row.value = {**row.value, "enabled": False}
    assert not claim_allowed("script.parse")
    assert claim_allowed("text.format")


def test_llm_request_uses_settings_pinned_to_its_permit(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from backend.core.config import LLMConfig
    from backend.platform.gpu_scheduler import admission
    config = enabled_config(tmp_path)
    activate(config)
    old = LLMConfig(base_url="http://127.0.0.1:1111/v1", model_name="active-model", api_key="active-key")
    with transaction() as (_db, state): state["llm_runtime"] = old.model_dump()
    original_permit = admission.gpu_permit
    @contextmanager
    def edit_after_grant(*args, **kwargs):
        with original_permit(*args, **kwargs) as request_id:
            monkeypatch.setattr(admission, "platform_llm", lambda: LLMConfig(base_url="http://127.0.0.1:2222/v1", model_name="new-model"))
            yield request_id
    monkeypatch.setattr(admission, "gpu_permit", edit_after_grant)
    @admission.llm_admitted
    def call(base_url, api_key, model): return base_url, api_key, model
    assert call("http://task-snapshot.invalid", "historical-key", "historical-model") == (old.base_url, old.api_key, old.model_name)


def test_config_edit_during_switch_cannot_open_wrong_service_admission(tmp_path, monkeypatch):
    from backend.core.config import LLMConfig
    from backend.platform.gpu_scheduler import runtime, config as config_module
    selected = [LLMConfig(model_name="old-model")]
    monkeypatch.setattr(runtime, "platform_llm", lambda: selected[0])
    monkeypatch.setattr(config_module, "platform_llm", lambda db=None: selected[0])
    config = enabled_config(tmp_path)
    with transaction() as (db, _state): db.add(SystemConfig(key="gpu_scheduler", value=config.model_dump()))
    class EditingManager(FakeManager):
        def start_llm(self, config, llm=None):
            assert llm.model_name == "old-model"
            super().start_llm(config, llm)
            selected[0] = LLMConfig(model_name="new-model")
    Scheduler(threading.Event(), EditingManager()).switch("LLM", "test change", config)
    assert read_state()["llm_runtime"]["model_name"] == "old-model"
    assert not claim_allowed("script.parse")


def test_cross_service_permits_wait_for_activation(tmp_path):
    config = enabled_config(tmp_path)
    activate(config)
    entered = threading.Event()
    release = threading.Event()
    errors = []
    def tts():
        try:
            with gpu_permit("TTS"):
                entered.set()
                release.wait(5)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=tts)
    thread.start()
    try:
        assert not entered.wait(0.3)
        with gpu_permit("LLM"):
            assert QueueMonitor().snapshot()["LLM"].running == 1
            assert not entered.is_set()
        activate(config, "TTS")
        assert entered.wait(3)
        assert QueueMonitor().snapshot()["TTS"].running == 1
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors


def test_tts_permits_are_serialized(tmp_path):
    activate(enabled_config(tmp_path), "TTS")
    count = 0
    maximum = 0
    guard = threading.Lock()
    errors = []
    def work():
        nonlocal count, maximum
        try:
            with gpu_permit("TTS"):
                with guard:
                    count += 1
                    maximum = max(maximum, count)
                time.sleep(0.15)
                with guard:
                    count -= 1
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=work) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert maximum == 1 and not errors and all(not thread.is_alive() for thread in threads)


def test_waiting_permit_cancellation_removes_request(tmp_path):
    activate(enabled_config(tmp_path))
    class Cancel:
        def check(self):
            from backend.core.task_control import TaskCancelled
            raise TaskCancelled()
    from backend.core.task_control import TaskCancelled
    with pytest.raises(TaskCancelled):
        with gpu_permit("TTS", Cancel()):
            pytest.fail("inactive TTS must not run")
    with SessionLocal() as db:
        assert not db.scalars(select(GPURequest)).all()


def test_queue_view_deduplicates_stages_and_excludes_ineligible_tasks():
    from backend.platform.models import User, Project
    baseline = QueueMonitor().snapshot()
    owner, project = new_id(), new_id()
    try:
        with SessionLocal.begin() as db:
            db.add(User(id=owner, username="gpu"+owner[:8], email=f"{owner}@example.test", password_hash="unused-test-hash"))
            db.flush()
            db.add(Project(id=project, owner_id=owner, name="queue test", directory_key=project))
            db.flush()
            staged = new_id()
            db.add_all([
                Task(id=staged, owner_id=owner, project_id=project, task_type="voices.foundation", status="running"),
                Task(owner_id=owner, project_id=project, task_type="script.parse", status="pending"),
                Task(owner_id=owner, project_id=project, task_type="tts.batch", status="retrying", next_attempt_at=utcnow()+timedelta(minutes=5)),
                Task(owner_id=owner, project_id=project, task_type="bgm.match", status="pending"),
                Task(owner_id=owner, project_id=project, task_type="script.parse", status="paused", error_code="manual_pause"),
                Task(owner_id=owner, project_id=project, task_type="script.parse", status="paused", error_code="llm_unavailable"),
            ])
            db.add_all([GPURequest(task_id=staged, service="TTS", status="waiting", owner_pid=os.getpid(), created_at=utcnow()-timedelta(seconds=40)) for _ in range(2)])
        result = QueueMonitor().snapshot()
        assert result["TTS"].waiting - baseline["TTS"].waiting == 1
        assert result["LLM"].waiting - baseline["LLM"].waiting == 2
        assert result["TTS"].oldest_wait >= 40
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(GPURequest).where(GPURequest.task_id == staged))
            db.execute(delete(Task).where(Task.owner_id == owner))
            db.execute(delete(Project).where(Project.id == project))
            db.execute(delete(User).where(User.id == owner))


def test_stale_waiting_request_of_retrying_task_still_counts_the_task():
    from backend.platform.models import User, Project
    baseline = QueueMonitor().snapshot()
    owner, project, task = new_id(), new_id(), new_id()
    try:
        with SessionLocal.begin() as db:
            db.add(User(id=owner, username="gpu"+owner[:8], email=f"{owner}@example.test", password_hash="unused-test-hash"))
            db.flush()
            db.add(Project(id=project, owner_id=owner, name="queue test", directory_key=project))
            db.flush()
            db.add(Task(id=task, owner_id=owner, project_id=project, task_type="script.parse", status="retrying"))
            db.add(GPURequest(task_id=task, service="LLM", status="waiting", owner_pid=os.getpid()))
        result = QueueMonitor().snapshot()
        assert result["LLM"].waiting - baseline["LLM"].waiting == 1
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(GPURequest).where(GPURequest.task_id == task))
            db.execute(delete(Task).where(Task.owner_id == owner))
            db.execute(delete(Project).where(Project.id == project))
            db.execute(delete(User).where(User.id == owner))


def test_scheduler_lifetime_lock_allows_only_one_coordinator(tmp_path, monkeypatch):
    from backend.platform.gpu_scheduler import runtime
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    stop_a, stop_b = threading.Event(), threading.Event()
    first = Scheduler(stop_a, FakeManager())
    second = Scheduler(stop_b, FakeManager())
    entered_a, entered_b = threading.Event(), threading.Event()
    def tick_a(): entered_a.set()
    def tick_b(): entered_b.set()
    monkeypatch.setattr(first, "tick", tick_a)
    monkeypatch.setattr(second, "tick", tick_b)
    a = threading.Thread(target=first.run)
    b = threading.Thread(target=second.run)
    a.start()
    try:
        assert entered_a.wait(3)
        b.start()
        assert not entered_b.wait(0.4)
        stop_a.set()
        a.join(3)
        assert entered_b.wait(3)
    finally:
        stop_a.set()
        stop_b.set()
        a.join(3)
        if b.ident: b.join(3)
    assert not a.is_alive() and not b.is_alive()


class FakeManager:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.health = self
    def _record(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError(f"failure {name}")
    def stop_tts(self): self._record("stop_tts")
    def stop_llm(self, config, pulse): self._record("stop_llm")
    def wait_gpu_released(self, config, pulse): self._record("release")
    def start_tts(self): self._record("start_tts")
    def start_llm(self, config, llm=None): self._record("start_llm")
    def wait_llm_ready(self, config, pulse): self._record("ready")
    def cleanup_failed_llm_start(self, config): self._record("cleanup")
    def is_llm_running(self): return True
    def llm_ready(self, llm=None): return True


def test_switch_order_and_new_tasks_during_drain(tmp_path):
    config = enabled_config(tmp_path, gpu_release_wait=0)
    activate(config)
    manager = FakeManager()
    scheduler = Scheduler(threading.Event(), manager)
    errors = []
    def switch():
        try:
            scheduler.switch("TTS", "test", config)
        except Exception as exc:
            errors.append(exc)
    with gpu_permit("LLM"):
        thread = threading.Thread(target=switch)
        thread.start()
        for _ in range(100):
            if read_state()["phase"] == "DRAINING": break
            time.sleep(0.01)
        assert read_state()["state"] == "SWITCHING_TO_TTS"
        assert not manager.calls
        assert not claim_allowed("script.parse")
    thread.join(5)
    assert not errors and not thread.is_alive()
    assert manager.calls == ["stop_tts", "stop_llm", "release", "start_tts"]
    assert read_state()["state"] == "TTS_ACTIVE"


@pytest.mark.parametrize("failure", ["stop_llm", "stop_tts", "release"])
def test_failure_never_starts_other_service(tmp_path, failure):
    config = enabled_config(tmp_path)
    activate(config)
    manager = FakeManager(failure)
    with pytest.raises(RuntimeError):
        Scheduler(threading.Event(), manager).switch("TTS", "test", config)
    assert "start_tts" not in manager.calls
    assert not claim_allowed("script.parse")


def test_startup_retries_are_bounded(tmp_path):
    config = enabled_config(tmp_path, startup_retry_count=3)
    manager = FakeManager("ready")
    with pytest.raises(RuntimeError):
        Scheduler(threading.Event(), manager).switch("LLM", "test", config)
    assert manager.calls.count("start_llm") == 3
    assert manager.calls.count("cleanup") == 3
    assert not claim_allowed("script.parse")


def test_drain_timeout_preserves_running_permit(tmp_path):
    config = enabled_config(tmp_path, drain_timeout=0.02)
    activate(config)
    manager = FakeManager()
    with gpu_permit("LLM"):
        with pytest.raises(TimeoutError):
            Scheduler(threading.Event(), manager).switch("TTS", "test", config)
        assert QueueMonitor().snapshot()["LLM"].running == 1
        assert not manager.calls


def test_dead_owner_with_live_gpu_process_is_not_reaped():
    with transaction() as (db, state):
        state["llm_process"] = process_identity(os.getpid())
        db.add(GPURequest(service="LLM", status="running", owner_pid=999999,
                          process={"owner": {"pid": 999999, "created": 0}}))
    with transaction() as (db, state):
        process_reap(db, state)
        assert state["state"] == "ERROR"
        assert db.scalar(select(GPURequest.id))


def test_dead_waiting_owner_is_reaped():
    with transaction() as (db, state):
        db.add(GPURequest(service="TTS", status="waiting", owner_pid=999999,
                          process={"owner": {"pid": 999999, "created": 0}}))
    with transaction() as (db, state):
        process_reap(db, state)
        assert db.scalar(select(GPURequest.id)) is None


class RestartManager:
    """A freshly started coordinator: it owns no in-process LLM service."""
    def __init__(self, port_open):
        self.calls = []
        self.health = self
        self.port_open_value = port_open
    def is_llm_running(self):
        return False
    def llm_port_open(self, llm=None):
        return self.port_open_value
    def llm_ready(self, llm=None):
        return False
    def stop_tts(self):
        self.calls.append("stop_tts")
    def stop_llm(self, config, pulse):
        self.calls.append("stop_llm")
    def wait_gpu_released(self, config, pulse):
        self.calls.append("release")


@pytest.mark.parametrize("llm_process,port_open,clean", [
    ({"pid": 999999, "created": 0}, False, True),   # proven dead: auto-clean to IDLE
    ({"pid": 999999, "created": 0}, True, False),   # port still held: stay fail-closed
    (None, False, False),                            # live identity: stay fail-closed
])
def test_coordinator_restart_cleans_verified_llm_exit(tmp_path, monkeypatch, llm_process, port_open, clean):
    from backend.core.config import LLMConfig
    from backend.platform.gpu_scheduler import config as config_module, runtime
    if llm_process is None:
        llm_process = process_identity(os.getpid())
    llm = LLMConfig(base_url="http://127.0.0.1:9999/v1", model_name="restarted")
    for module in (config_module, runtime):
        monkeypatch.setattr(module, "platform_llm", lambda db=None: llm)
    config = enabled_config(tmp_path, gpu_release_wait=0)
    activate(config, side="LLM")
    with transaction() as (_db, state):
        state["llm_process"] = llm_process
        state["llm_runtime"] = llm.model_dump()
    scheduler = Scheduler(threading.Event(), RestartManager(port_open))
    if clean:
        scheduler.tick()
        assert read_state()["state"] == "IDLE"
        assert scheduler.manager.calls == ["stop_tts", "stop_llm", "release"]
    else:
        with pytest.raises(RuntimeError):
            scheduler.tick()
        assert read_state()["state"] == "LLM_ACTIVE"


def test_failed_tts_tree_confirmation_retains_permit(tmp_path, monkeypatch):
    from backend.platform.gpu_scheduler.manager import GPUServiceManager
    from types import SimpleNamespace
    activate(enabled_config(tmp_path), "TTS")
    def unknown(proc):
        raise RuntimeError("job exit status unknown")
    monkeypatch.setattr(GPUServiceManager, "_finish_tts", unknown)
    with gpu_permit("TTS") as request_id:
        with pytest.raises(RuntimeError):
            GPUServiceManager.finish_tts(SimpleNamespace(_gpu_request_id=request_id))
    assert read_state()["state"] == "ERROR"
    with SessionLocal() as db:
        request = db.get(GPURequest, request_id)
        assert request.status == "running" and request.process["exit_unconfirmed"]
    assert not claim_allowed("script.parse")


def test_process_identity_rejects_reused_pid():
    identity = process_identity(os.getpid())
    assert identity_alive(identity)
    assert not identity_alive({**identity, "created": identity["created"] - 100})


def test_owned_windows_tree_lifecycle(tmp_path):
    script = tmp_path / "中文 子进程.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    proc = spawn_owned([sys.executable, str(script)], stdout=subprocess.DEVNULL)
    try:
        assert not tree_exited(proc)
        terminate_owned(proc)
        proc.wait(timeout=5)
        for _ in range(100):
            if tree_exited(proc): break
            time.sleep(0.02)
        assert tree_exited(proc)
    finally:
        if not tree_exited(proc): terminate_owned(proc)
        proc.wait(timeout=5)
        close_owned(proc)


def test_status_omits_paths_and_credentials(tmp_path):
    activate(enabled_config(tmp_path))
    with transaction() as (_db, state):
        state["llm_runtime"] = {"api_key": "private-key"}
        state["heartbeat"] = stamp() - 100
    result = status_snapshot()
    assert result["stale"]
    assert "private-key" not in str(result) and "llm_start_script_path" not in result


def test_tts_startup_failure_preserves_inputs_and_stops_after_budget(tmp_path, monkeypatch):
    from backend.engines import tts
    activate(enabled_config(tmp_path), "TTS")
    source = tmp_path / "segments.json"
    source.write_text("[]", encoding="utf-8")
    calls = []
    def fail(*args, **kwargs):
        assert source.exists()
        calls.append(1)
        raise tts.TTSStartupError("model failed to load")
    monkeypatch.setattr(tts, "_run_tts_subprocess_once", fail)
    class Handle:
        def check(self): pass
    with pytest.raises(tts.TTSStartupError):
        tts.run_tts_subprocess([sys.executable, "worker.py", "--mode", "batch"], Handle(), lambda line: None,
                               temp_files=(source,))
    assert len(calls) == 3 and not source.exists()
    assert read_state()["state"] == "ERROR"
    with SessionLocal() as db:
        assert db.scalar(select(GPURequest.id)) is None


@pytest.mark.parametrize("signal", ["[ready] tts", "[noop] tts"])
def test_tts_readiness_protocol(tmp_path, signal):
    from backend.engines import tts
    activate(enabled_config(tmp_path), "TTS")
    script = tmp_path / "ready worker.py"
    script.write_text(f"print({signal!r}, flush=True)\nprint('[result] done.wav', flush=True)\n", encoding="utf-8")
    class Handle:
        def check(self): pass
        def log(self, *args): pass
        def progress(self, *args): pass
    lines = []
    tts.run_tts_subprocess([sys.executable, str(script), "--mode", "batch"], Handle(), lines.append)
    assert lines == ["[result] done.wav"]
    assert read_state()["state"] == "TTS_ACTIVE"


def test_tts_health_timeout_preserves_queued_work(tmp_path):
    from backend.engines import tts
    activate(enabled_config(tmp_path, startup_timeout=0.1, startup_retry_count=1), "TTS")
    script = tmp_path / "loading.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    class Handle:
        def check(self): pass
        def log(self, *args): pass
    with pytest.raises(tts.TTSStartupError):
        tts.run_tts_subprocess([sys.executable, str(script), "--mode", "batch"], Handle(), lambda line: None)
    assert read_state()["state"] == "ERROR"
    with SessionLocal() as db:
        assert db.scalar(select(GPURequest.id)) is None


def test_owned_job_tracks_descendants_after_launcher_exits(tmp_path):
    marker = tmp_path / "child.pid"
    code = f"import subprocess,sys\np=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\nopen({str(marker)!r},'w').write(str(p.pid))\n"
    proc = spawn_owned([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=5)
        assert marker.exists() and not tree_exited(proc)
        child = process_identity(int(marker.read_text()))
        assert identity_alive(child)
        terminate_owned(proc)
        for _ in range(100):
            if tree_exited(proc): break
            time.sleep(0.02)
        assert tree_exited(proc) and not identity_alive(child)
    finally:
        if not tree_exited(proc): terminate_owned(proc)
        close_owned(proc)


@pytest.mark.skipif(os.name != "nt", reason="Windows foreground scripts and Job Object")
def test_real_foreground_llm_script_switches_both_directions(tmp_path, monkeypatch):
    import socket
    from backend.core.config import LLMConfig
    from backend.platform.gpu_scheduler import config as config_module, manager as manager_module, runtime, admission
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    worker = tmp_path / "LLM 模拟服务.py"
    worker.write_text('''import json,sys,threading
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({"data":[{"id":"fake-model"}]}).encode())
        if self.path=="/shutdown": threading.Thread(target=self.server.shutdown).start()
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length",0)))
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps({"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}).encode())
server=ThreadingHTTPServer(("127.0.0.1",int(sys.argv[1])),Handler)
server.serve_forever(); server.server_close()
''', encoding="utf-8")
    start = tmp_path / "LLM 前台启动.cmd"
    stop = tmp_path / "LLM 停止.cmd"
    start.write_text(f'@echo off\nchcp 65001 >nul\n"{sys.executable}" "{worker}" {port}\nexit /b %errorlevel%\n', encoding="utf-8")
    stop.write_text(f'@echo off\nchcp 65001 >nul\n"{sys.executable}" -c "import urllib.request; urllib.request.urlopen(\'http://127.0.0.1:{port}/shutdown\').read()"\n', encoding="utf-8")
    llm = LLMConfig(base_url=f"http://127.0.0.1:{port}/v1", model_name="fake-model", api_key="")
    for module in (config_module, manager_module, runtime, admission):
        monkeypatch.setattr(module, "platform_llm", lambda db=None: llm)
    config = GPUConfig(enabled=True, llm_start_script_path=str(start), llm_stop_script_path=str(stop),
                       startup_timeout=10, service_stop_timeout=10, health_check_interval=0.1, gpu_release_wait=0)
    with transaction() as (db, _state):
        db.add(SystemConfig(key="gpu_scheduler", value=config.model_dump()))
    scheduler = Scheduler(threading.Event())
    try:
        scheduler.switch("LLM", "test boot", config)
        assert read_state()["state"] == "LLM_ACTIVE"
        from backend.engines.llm_transport import request_chat_completion
        result = request_chat_completion("http://historical.invalid/v1", "old", "old-model", [],
                                         temperature=0.3, top_p=1, presence_penalty=0, max_tokens=8)
        assert result[0] == "ok"  # The historical endpoint was replaced by live platform config.
        scheduler.switch("TTS", "test backlog", config)
        assert read_state()["state"] == "TTS_ACTIVE"
        assert not scheduler.manager.health.llm_port_open()
        scheduler.switch("LLM", "test reverse", config)
        assert read_state()["state"] == "LLM_ACTIVE"
    finally:
        scheduler.switch(None, "test cleanup", config)
    assert read_state()["state"] == "IDLE"


def test_admin_permissions_csrf_config_and_recovery():
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.platform.models import User, AuditLog
    with TestClient(app) as client:
        registration = client.post("/api/auth/register", json={"email": f"{new_id()}@example.test",
                                   "username": "gpu"+new_id().replace("-", "")[:12], "password": "test-pass-1234"})
        assert registration.status_code == 201
        user_id = registration.json()["user"]["id"]
        headers = {"X-CSRF-Token": registration.json()["csrf_token"]}
        assert client.get("/api/v1/admin/gpu-scheduler/status").status_code == 403
        assert client.patch("/api/v1/admin/settings/gpu-scheduler", json={}, headers=headers).status_code == 403
        with SessionLocal.begin() as db:
            db.get(User, user_id).role = "admin"
        assert client.get("/api/v1/admin/settings/gpu-scheduler").json()["config"]["enabled"] is False
        assert client.patch("/api/v1/admin/settings/gpu-scheduler", json={"max_wait_time": 30}).status_code == 403
        assert client.patch("/api/v1/admin/settings/gpu-scheduler", json={"enabled": True}, headers=headers).status_code == 422
        assert client.patch("/api/v1/admin/settings/gpu-scheduler", json={"max_wait_time": 30}, headers=headers).status_code == 200
        with transaction() as (_db, state): state["state"] = "ERROR"
        assert client.post("/api/v1/admin/gpu-scheduler/recover", headers=headers).status_code == 202
        assert read_state()["recover_requested"]
        with SessionLocal() as db:
            assert db.scalar(select(AuditLog).where(AuditLog.action == "admin.gpu_scheduler_changed"))


@pytest.mark.parametrize("name", ["中文 空格.sh", "literal $(touch x);&.sh"])
def test_bash_script_uses_literal_argv(tmp_path, name):
    path = tmp_path / name
    path.write_text("exit 0", encoding="utf-8")
    config = GPUConfig(llm_start_script_path=str(path), llm_stop_script_path=str(path))
    assert script_command(config.llm_start_script_path) == ["/bin/bash", "--", str(path)]
    with pytest.raises(ValueError):
        script_command(str(tmp_path / "unsupported.py"))


def test_enabled_rejects_wrong_os_script(tmp_path):
    from backend.platform.gpu_scheduler.config import validate_enabled
    suffix = ".sh" if os.name == "nt" else ".cmd"
    path = tmp_path / ("wrong-platform" + suffix)
    path.write_text("exit 0", encoding="utf-8")
    with pytest.raises(ValueError, match="当前操作系统"):
        validate_enabled(GPUConfig(enabled=True, llm_start_script_path=str(path)))


def test_linux_platform_requires_bash_and_rejects_windows_scripts(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from backend.core import managed_process as module
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix"))
    with pytest.raises(ValueError, match="当前操作系统"):
        module.validate_script_platform(str(tmp_path / "start.ps1"))
    monkeypatch.setattr(module.Path, "is_file", lambda self: False)
    with pytest.raises(ValueError, match="/bin/bash"):
        module.validate_script_platform(str(tmp_path / "start.sh"))
    monkeypatch.setattr(module.Path, "is_file", lambda self: True)
    module.validate_script_platform(str(tmp_path / "start.sh"))


@pytest.mark.parametrize("status,exited", [("zombie", True), ("sleeping", False)])
def test_linux_tree_confirmation_ignores_only_zombies(monkeypatch, status, exited):
    from types import SimpleNamespace
    from backend.core import managed_process as module
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix", killpg=lambda *args: None, getpgid=lambda pid: 123))
    monkeypatch.setattr(module.psutil, "process_iter", lambda: [SimpleNamespace(pid=456, status=lambda: status)])
    assert module.tree_exited(SimpleNamespace(pid=123, poll=lambda: 0)) is exited


def test_linux_tree_confirmation_fails_closed_on_permission_error(monkeypatch):
    from types import SimpleNamespace
    from backend.core import managed_process as module
    def denied(pid):
        raise PermissionError("cannot inspect group")
    monkeypatch.setattr(module, "os", SimpleNamespace(name="posix", killpg=lambda *args: None, getpgid=denied))
    monkeypatch.setattr(module.psutil, "process_iter", lambda: [SimpleNamespace(pid=456)])
    with pytest.raises(PermissionError):
        module.tree_exited(SimpleNamespace(pid=123, poll=lambda: 0))


@pytest.mark.skipif(os.name == "nt", reason="requires native Linux Bash and process groups")
def test_linux_bash_foreground_script_with_unicode_spaces(tmp_path):
    import signal
    script = tmp_path / "中文 服务 启动.sh"
    script.write_text('exec "' + sys.executable + '" -c "from pathlib import Path; import time; Path(\'ready\').touch(); time.sleep(30)"\n', encoding="utf-8")
    proc = spawn_owned(script_command(str(script)), cwd=tmp_path)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail("Bash service exited before readiness")
            time.sleep(0.02)
        assert (tmp_path / "ready").exists()
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
        assert tree_exited(proc)
    finally:
        terminate_owned(proc)
        proc.wait(timeout=5)
        close_owned(proc)


def test_empty_side_switches_immediately_without_time_pressure_or_service_guards():
    for current in ["LLM", "TTS"]:
        other = "TTS" if current == "LLM" else "LLM"
        queues = {current: QueueStats(), other: QueueStats(waiting=1)}
        result = SchedulingPolicy().decide(GPUConfig(min_service_runtime=900, switch_cooldown=900),
            current, queues["LLM"], queues["TTS"], runtime=0, since_switch=0, idle_time=0, served=False)
        assert result.target == other, (current,)
        assert result.reason == "current queue empty", (current,)


def test_backlog_or_active_calls_respect_configurable_minimum():
    for current in ["LLM", "TTS"]:
        for waiting, running in [(1, 0), (0, 1)]:
            for minimum in [60, 300, 600]:
                other = "TTS" if current == "LLM" else "LLM"
                queues = {current: QueueStats(waiting=waiting, running=running),
                          other: QueueStats(waiting=100, oldest_wait=10000)}
                for runtime, target in [(minimum - 0.01, current), (minimum, other)]:
                    result = SchedulingPolicy().decide(GPUConfig(min_service_runtime=minimum),
                        current, queues["LLM"], queues["TTS"], runtime=runtime, since_switch=0, idle_time=0, served=True)
                    assert result.target == target, (current, waiting, running, minimum,)


def test_default_minimum_stay_is_five_minutes():
    assert GPUConfig().min_service_runtime == 300


def test_all_llm_task_channels_share_one_concurrency_limit(tmp_path, monkeypatch):
    from backend.platform.gpu_scheduler import admission
    activate(enabled_config(tmp_path), "LLM")
    monkeypatch.setattr(admission, "parse_worker_concurrency", lambda **kwargs: 2)
    count = 0
    maximum = 0
    completed = 0
    guard = threading.Lock()
    release = threading.Event()
    full = threading.Event()
    errors = []

    def work():
        nonlocal count, maximum, completed
        try:
            with gpu_permit("LLM"):
                with guard:
                    count += 1
                    maximum = max(maximum, count)
                    if count == 2:
                        full.set()
                release.wait(5)
                with guard:
                    count -= 1
                    completed += 1
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(5)]
    for thread in threads:
        thread.start()
    try:
        assert full.wait(3), "independent LLM channels should overlap"
        time.sleep(0.3)
        with SessionLocal() as db:
            requests = db.scalars(select(GPURequest)).all()
            assert sum(r.status == "running" for r in requests) == 2
            assert sum(r.status == "waiting" for r in requests) == 3
    finally:
        release.set()
        for thread in threads:
            thread.join(8)
    assert maximum == 2 and completed == 5 and not errors
    assert all(not thread.is_alive() for thread in threads)


def test_llm_limit_is_shared_across_worker_processes(tmp_path):
    activate(enabled_config(tmp_path), "LLM")
    script = r'''
import sys, time
from pathlib import Path
from backend.platform.gpu_scheduler import admission, store
root, slot = Path(sys.argv[1]), sys.argv[2]
store.PROJECT_ROOT = root
admission.parse_worker_concurrency = lambda **kwargs: 2
with admission.gpu_permit("LLM"):
    (root / ("entered-" + slot)).touch()
    deadline = time.monotonic() + 15
    while not (root / "release").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test release timed out")
        time.sleep(.05)
'''
    children = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(slot)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for slot in range(3)]
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                requests = db.scalars(select(GPURequest)).all()
            if len(requests) == 3 and len(list(tmp_path.glob("entered-*"))) == 2:
                break
            assert all(child.poll() is None for child in children)
            time.sleep(.05)
        else:
            pytest.fail("worker processes did not reach the shared LLM limit")
        assert sum(r.status == "running" for r in requests) == 2
        assert sum(r.status == "waiting" for r in requests) == 1
        (tmp_path / "release").touch()
        for child in children:
            _, err = child.communicate(timeout=8)
            assert child.returncode == 0, err.decode()
        assert len(list(tmp_path.glob("entered-*"))) == 3
    finally:
        (tmp_path / "release").touch()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize("pool_size", [1, 4])
def test_concurrent_admission_reuses_connections_without_waiting_on_cache(tmp_path, monkeypatch, pool_size):
    """All Worker connections may be held by concurrent claim selectors."""
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from backend.core.config import LLMConfig
    from backend.platform.models import Base
    from backend.platform import system_config
    from backend.platform.gpu_scheduler import config as config_module, store

    engine = create_engine(f"sqlite:///{tmp_path / 'small-pool.db'}",
                           pool_size=pool_size, max_overflow=0, pool_timeout=0.2)
    sessions = sessionmaker(engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    config = enabled_config(tmp_path)
    llm = LLMConfig(model_name="stored-model")
    fingerprint = service_fingerprint(config, llm)
    with sessions.begin() as db:
        db.add_all([
            SystemConfig(key="application.features", value={"llm": llm.model_dump()}),
            SystemConfig(key="gpu_scheduler", value=config.model_dump()),
            GPUSchedulerState(id="local", value={"state": "LLM_ACTIVE", "current": "LLM", "managed": True,
                                                 "service_config": fingerprint}),
        ])
    monkeypatch.setattr(system_config, "SessionLocal", sessions)
    monkeypatch.setattr(config_module, "SessionLocal", sessions)
    monkeypatch.setattr(store, "SessionLocal", sessions)
    barrier = threading.Barrier(pool_size)

    def admit():
        with sessions() as db:
            db.execute(text("SELECT 1"))
            barrier.wait(timeout=5)
            return claim_allowed("script.parse", db)

    try:
        # Simulate a cache refresh owning its lock. Admission must use its own
        # transaction, even when the cache is stale or another thread refreshes.
        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            with system_config._lock:
                futures = [executor.submit(admit) for _ in range(pool_size)]
                assert all(future.result(timeout=5) for future in futures)
        # Legacy scheduler state lacks llm_runtime. Granting a permit must also
        # read its fallback settings using the connection it already holds.
        with gpu_permit("LLM") as request_id:
            with sessions() as db:
                request = db.get(GPURequest, request_id)
                assert request.process["llm_runtime"]["model_name"] == llm.model_name
        with sessions.begin() as db:
            db.get(SystemConfig, "application.features").value = {
                "llm": {**llm.model_dump(), "model_name": "edited-model"}}
        with sessions() as db:
            assert not claim_allowed("script.parse", db)
            assert claim_allowed("text.format", db)
    finally:
        engine.dispose()


def test_llm_waiter_is_woken_by_a_release_instead_of_sleeping_out_its_backoff(tmp_path, monkeypatch):
    from backend.platform.gpu_scheduler import admission
    activate(enabled_config(tmp_path), "LLM")
    monkeypatch.setattr(admission, "parse_worker_concurrency", lambda **kwargs: 1)
    monkeypatch.setattr(admission, "_LLM_POLL_MAX", 10.0)      # a wake-up-less waiter would sleep far longer
    holding, release, admitted = threading.Event(), threading.Event(), {}

    def holder():
        with gpu_permit("LLM"):
            holding.set()
            release.wait(10)

    def waiter():
        with gpu_permit("LLM"):
            admitted["at"] = time.monotonic()

    first = threading.Thread(target=holder)
    first.start()
    assert holding.wait(3)
    second = threading.Thread(target=waiter)
    second.start()
    time.sleep(2.0)                                            # its next un-woken poll would be at ~3.15 s
    released_at = time.monotonic()
    release.set()
    first.join(5)
    second.join(5)
    assert "at" in admitted and admitted["at"] - released_at < 0.5


def test_llm_permits_are_admitted_oldest_first(tmp_path, monkeypatch):
    from backend.platform.gpu_scheduler import admission
    activate(enabled_config(tmp_path), "LLM")
    monkeypatch.setattr(admission, "parse_worker_concurrency", lambda **kwargs: 1)
    holding, release, order = threading.Event(), threading.Event(), []

    def holder():
        with gpu_permit("LLM"):
            holding.set()
            release.wait(10)

    def waiter(name):
        with gpu_permit("LLM"):
            order.append(name)

    first = threading.Thread(target=holder)
    first.start()
    assert holding.wait(3)
    threads = []
    for name in ("a", "b", "c"):
        thread = threading.Thread(target=waiter, args=(name,))
        thread.start()
        threads.append(thread)
        time.sleep(0.15)                                       # fix the arrival order
    release.set()
    for thread in [first, *threads]:
        thread.join(8)
    assert order == ["a", "b", "c"]


def test_llm_admission_logs_wait_and_run_time(tmp_path, monkeypatch, caplog):
    import logging
    from backend.platform.gpu_scheduler import admission
    activate(enabled_config(tmp_path), "LLM")
    monkeypatch.setattr(admission, "parse_worker_concurrency", lambda **kwargs: 4)

    @admission.llm_admitted
    def call(*args, **kwargs):
        return "ok"

    with caplog.at_level(logging.INFO, logger="audiobook.llm_trace"):
        assert call("http://x", "key", "model") == "ok"
    assert any("llm_call" in r.getMessage() and "wait=" in r.getMessage() and "run=" in r.getMessage()
               for r in caplog.records)


def test_llm_free_slots_are_filled_in_one_round_without_exceeding_the_limit(tmp_path, monkeypatch):
    from backend.platform.gpu_scheduler import admission
    activate(enabled_config(tmp_path), "LLM")
    monkeypatch.setattr(admission, "parse_worker_concurrency", lambda **kwargs: 3)
    monkeypatch.setattr(admission, "_LLM_POLL_MAX", 10.0)
    guard = threading.Lock()
    running = peak = 0
    release_holders, release_waiters = threading.Event(), threading.Event()
    held = threading.Semaphore(0)
    admitted_at, order, errors = {}, [], []

    def enter():
        nonlocal running, peak
        with guard:
            running += 1
            peak = max(peak, running)

    def leave():
        nonlocal running
        with guard:
            running -= 1

    def holder():
        try:
            with gpu_permit("LLM"):
                enter()
                held.release()
                release_holders.wait(10)
                leave()
        except Exception as exc:
            errors.append(exc)

    def waiter(name):
        try:
            with gpu_permit("LLM"):
                enter()
                admitted_at[name] = time.monotonic()
                order.append(name)
                release_waiters.wait(10)
                leave()
        except Exception as exc:
            errors.append(exc)

    holders = [threading.Thread(target=holder) for _ in range(3)]
    for thread in holders:
        thread.start()
    for _ in range(3):
        assert held.acquire(timeout=3)
    waiters = []
    for index in range(6):
        thread = threading.Thread(target=waiter, args=(f"w{index + 1}",))
        thread.start()
        waiters.append(thread)
        time.sleep(0.1)                                        # fix the arrival order
    time.sleep(1.5)                                            # every waiter is deep into its backoff
    released_at = time.monotonic()
    release_holders.set()                                      # all three slots free up together
    deadline = time.monotonic() + 3
    while len(admitted_at) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert set(order[:3]) == {"w1", "w2", "w3"}            # the three OLDEST, not later arrivals
        assert max(admitted_at.values()) - released_at < 0.6   # one wake chain, not three backoff rounds
        assert len(admitted_at) == 3                           # the limit still holds: w4..w6 keep waiting
    finally:
        release_waiters.set()
        for thread in [*holders, *waiters]:
            thread.join(8)
    assert peak <= 3 and not errors
    assert set(order) == {f"w{i}" for i in range(1, 7)}


def test_llm_wake_is_handed_to_the_next_waiter_in_line():
    from backend.platform.gpu_scheduler import admission
    a, b, c = threading.Event(), threading.Event(), threading.Event()
    with admission._llm_waiters_lock:
        admission._llm_waiters.extend([a, b, c])
    try:
        admission._llm_wake(1)
        assert a.is_set() and not b.is_set()
        admission._llm_wake_after(a)                           # a was woken but could not take the slot
        assert b.is_set() and not c.is_set()
        admission._llm_wake_after(c)                           # nobody behind the last one: no error
        assert not c.is_set()
        admission._llm_wake_after(threading.Event())           # not in the queue: no error
    finally:
        with admission._llm_waiters_lock:
            for event in (a, b, c):
                if event in admission._llm_waiters:
                    admission._llm_waiters.remove(event)
