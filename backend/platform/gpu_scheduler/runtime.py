"""Worker-owned coordinator. API processes only save config/control requests."""
from __future__ import annotations

import logging
import os
import threading
import time

from sqlalchemy import select

from ..models import GPURequest
from ...core.file_lock import exclusive_file_lock
from ...core.paths import PROJECT_ROOT
from ...core.managed_process import identity_alive
from .config import load_config, platform_llm, validate_enabled, service_fingerprint
from .store import QueueMonitor, transaction, read_state, process_reap, stamp
from .policy import SchedulingPolicy
from .manager import GPUServiceManager

logger = logging.getLogger("audiobook.gpu_scheduler")


class Scheduler:
    def __init__(self, stop: threading.Event, manager=None):
        self.stop = stop
        self.manager = manager or GPUServiceManager()
        self.monitor = QueueMonitor()
        self.policy = SchedulingPolicy()
        self.active_config = None
        self.switch_lock = threading.Lock()

    def pulse(self):
        with transaction() as (_db, state):
            state["heartbeat"] = stamp()
            state["owner_pid"] = os.getpid()

    def phase(self, name):
        with transaction() as (_db, state):
            state["phase"] = name
        logger.info("[GPU-SCHEDULER] phase=%s", name)

    def drain(self, config):
        deadline = time.monotonic() + config.drain_timeout
        while True:
            with transaction() as (db, state):
                process_reap(db, state)
                state["heartbeat"] = stamp()
                active = db.scalar(select(GPURequest.id).where(GPURequest.status == "running").limit(1))
                error = state["state"] == "ERROR"
            if error:
                raise RuntimeError("排空期间发现无法确认退出的 GPU 调用")
            if active is None:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("GPU 调用排空超时，保留当前服务和任务")
            time.sleep(0.2)

    def switch(self, target, reason, config):
        with self.switch_lock:
            self._switch(target, reason, config)

    def _switch(self, target, reason, config):
        started = time.monotonic()
        # Pin lifecycle settings across drain/start; edits made during switching
        # close admission through the fingerprint and apply on the next tick.
        llm_snapshot = platform_llm()
        fingerprint = service_fingerprint(config, llm_snapshot)
        with transaction() as (_db, state):
            current = state["current"]
            state.update(state=f"SWITCHING_TO_{target}" if target else "SWITCHING_TO_IDLE",
                         phase="DRAINING", managed=True, reason=reason, error="")
        logger.info("[GPU-SCHEDULER] switch %s -> %s reason=%s", current, target, reason)
        self.drain(config)
        self.phase("STOPPING")
        self.manager.stop_tts()
        # A failed/restarted coordinator may have an LLM process even without current.
        self.manager.stop_llm(self.active_config or config, self.pulse)
        self.phase("RELEASE_WAIT")
        self.manager.wait_gpu_released(config, self.pulse)
        if target:
            self.phase("STARTING")
            for attempt in range(config.startup_retry_count):
                try:
                    if target == "LLM":
                        self.manager.start_llm(config, llm_snapshot)
                        self.phase("HEALTH_CHECK")
                        self.manager.wait_llm_ready(config, self.pulse)
                    else:
                        self.manager.start_tts()
                    break
                except Exception:
                    if target == "LLM":
                        self.manager.cleanup_failed_llm_start(config)
                    if attempt + 1 == config.startup_retry_count:
                        raise
                    self.phase("STARTING")
                    self.manager.wait_gpu_released(config, self.pulse)
        now = stamp()
        with transaction() as (_db, state):
            state.update(state=f"{target}_ACTIVE" if target else "IDLE", current=target, phase="",
                         active_since=now if target else 0, last_switch=now, served=False,
                         idle_since=0, managed=config.enabled, service_config=fingerprint,
                         llm_runtime=llm_snapshot.model_dump(),
                         error="", reason=reason, heartbeat=now)
        self.active_config = config
        logger.info("[GPU-SCHEDULER] switch completed target=%s duration=%.2fs", target, time.monotonic() - started)

    def tick(self):
        config = load_config()
        self.pulse()
        with transaction() as (db, state):
            process_reap(db, state)
            snapshot = self.monitor.snapshot(db)
            now = stamp()
            busy = any(item.waiting or item.running for item in snapshot.values())
            state["idle_since"] = 0 if busy else (state["idle_since"] or now)
            recovering = state["recover_requested"]
            state["recover_requested"] = False
            saved = dict(state)
        if saved["state"] == "ERROR":
            if recovering:
                # Orphaned work must be proven finished before any lifecycle action.
                with transaction() as (db, state):
                    process_reap(db, state)
                    if db.scalar(select(GPURequest.id).where(GPURequest.status == "running").limit(1)):
                        state["error"] = "GPU 调用仍在运行，请等待结束后再次恢复"
                        return
                self.switch(None, "administrator recovery", config)
            return
        if not config.enabled:
            if saved["managed"]:
                self.switch(None, "scheduler disabled", config)
            return
        validate_enabled(config)
        if not saved["managed"]:
            with transaction() as (_db, state):
                state["managed"] = True
            self.switch(None, "initialize managed GPU", config)
            saved = read_state()
        if saved["state"].startswith("SWITCHING"):
            raise RuntimeError("上次服务切换未完成，请恢复后重试")
        if saved["current"] and saved["service_config"] != service_fingerprint(config):
            self.switch(saved["current"], "service configuration changed", config)
            return
        if saved["current"] == "LLM" and (not self.manager.is_llm_running() or not self.manager.health.llm_ready(getattr(self.manager, "llm_settings", None))):
            raise RuntimeError("LLM 服务异常退出或健康检查失败")
        decision = self.policy.decide(config, saved["current"], snapshot["LLM"], snapshot["TTS"],
                                      runtime=now - saved["active_since"], since_switch=now - saved["last_switch"],
                                      idle_time=now - saved["idle_since"] if saved["idle_since"] else 0,
                                      served=saved["served"])
        logger.debug("[GPU-SCHEDULER] current=%s llm=%s tts=%s decision=%s", saved["current"], snapshot["LLM"], snapshot["TTS"], decision)
        if decision.target != saved["current"]:
            self.switch(decision.target, decision.reason, config)
        else:
            with transaction() as (_db, state):
                state["reason"] = decision.reason

    def run(self):
        while not self.stop.is_set():
            try:
                # Lifetime ownership, independent from the shorter admission lock.
                with exclusive_file_lock(PROJECT_ROOT / ".narrify" / "gpu-leader.lock", timeout=0.1):
                    while not self.stop.is_set():
                        try:
                            self.tick()
                        except Exception as exc:
                            logger.exception("[GPU-SCHEDULER] coordinator failed")
                            with transaction() as (_db, state):
                                state.update(state="ERROR", error=str(exc), reason="scheduler exception", phase="", managed=True)
                        self.stop.wait(load_config().scheduler_interval)
                    if read_state()["managed"]:
                        try:
                            self.switch(None, "worker shutdown", load_config())
                        except Exception as exc:
                            with transaction() as (_db, state):
                                state.update(state="ERROR", error=str(exc), reason="worker shutdown incomplete")
                    return
            except TimeoutError:
                self.stop.wait(1)
            except Exception:
                logger.exception("[GPU-SCHEDULER] coordinator storage unavailable")
                self.stop.wait(2)


def status_snapshot() -> dict:
    config = load_config()
    state = read_state()
    queues = QueueMonitor().snapshot()
    policy = SchedulingPolicy()
    now = stamp()
    heartbeat_age = max(0, now - state["heartbeat"]) if state["heartbeat"] else None
    phase = state["phase"]
    if state["state"] == "TTS_ACTIVE":
        from ..database import SessionLocal
        with SessionLocal() as db:
            loading = any(not request.process.get("ready") for request in db.scalars(
                select(GPURequest).where(GPURequest.service == "TTS", GPURequest.status == "running")).all())
        if loading:
            phase = "MODEL_LOADING"
    return {"enabled": config.enabled, "state": state["state"], "phase": phase, "current": state["current"],
            "llm": {**queues["LLM"].__dict__, "pressure": policy.calculate_llm_pressure(queues["LLM"])},
            "tts": {**queues["TTS"].__dict__, "pressure": policy.calculate_tts_pressure(queues["TTS"])},
            "runtime": max(0, now - state["active_since"]) if state["active_since"] else 0,
            "last_switch": state["last_switch"] or None, "reason": state["reason"], "error": state["error"],
            "heartbeat_age": heartbeat_age, "stale": heartbeat_age is None or heartbeat_age > max(15, config.scheduler_interval * 3)}
