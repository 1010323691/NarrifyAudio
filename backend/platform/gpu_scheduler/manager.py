"""The sole lifecycle boundary for LLM and TTS model processes."""
from __future__ import annotations

import os
import logging
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..models import GPURequest
from ...core.managed_process import (spawn_owned, terminate_owned, close_owned, tree_exited,
                                     process_identity, identity_alive, script_command)
from ...engines.llm_transport import llm_server_is_alive
from .config import GPUConfig, platform_llm
from .store import transaction

logger = logging.getLogger("audiobook.gpu_scheduler")


class HealthChecker:
    def llm_ready(self, llm=None) -> bool:
        llm = llm or platform_llm()
        return llm_server_is_alive(llm.base_url, llm.api_key, model_name=llm.model_name or None, timeout=2)

    def llm_port_open(self, llm=None) -> bool:
        url = urlsplit((llm or platform_llm()).base_url)
        try:
            with socket.create_connection((url.hostname, url.port or (443 if url.scheme == "https" else 80)), timeout=1):
                return True
        except OSError:
            return False


class GPUServiceManager:
    def __init__(self, health=None):
        self.health = health or HealthChecker()
        self.llm = None
        self.llm_settings = None

    def is_llm_running(self) -> bool:
        return self.llm is not None and not tree_exited(self.llm)

    def start_llm(self, config: GPUConfig, llm=None):
        self.llm_settings = llm or platform_llm()
        if self.health.llm_port_open(self.llm_settings):
            raise RuntimeError("LLM 地址已有未托管服务监听，请先停止该服务")
        if self.llm is not None:
            if not tree_exited(self.llm):
                raise RuntimeError("上一 LLM 进程树尚未退出")
            close_owned(self.llm)
            self.llm = None
        self.llm = spawn_owned(script_command(config.llm_start_script_path),
                               cwd=str(Path(config.llm_start_script_path).parent),
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with transaction() as (_db, state):
            state["llm_process"] = process_identity(self.llm.pid)

    def wait_llm_ready(self, config: GPUConfig, pulse):
        deadline = time.monotonic() + config.startup_timeout
        while time.monotonic() < deadline:
            pulse()
            if self.llm is None or self.llm.poll() is not None:
                code = self.llm.returncode if self.llm else "unknown"
                raise RuntimeError(f"LLM 启动脚本提前退出（退出码 {code}）；必须保持前台运行，请检查脚本及系统执行策略")
            if self.health.llm_ready(self.llm_settings):
                return
            time.sleep(config.health_check_interval)
        raise TimeoutError("LLM 模型健康检查超时")

    def stop_llm(self, config: GPUConfig, pulse):
        if self.llm is None:
            from .store import read_state
            saved = read_state()
            if identity_alive(saved["llm_process"]):
                raise RuntimeError("无法确认已有 LLM 进程树归属，请手动停止后恢复")
            from ...core.config import LLMConfig
            previous = LLMConfig.model_validate(saved["llm_runtime"]) if saved.get("llm_runtime") else None
            if self.health.llm_port_open(previous):
                raise RuntimeError("LLM 端口仍在监听，不能确认服务已退出")
            return
        if not tree_exited(self.llm):
            if config.llm_stop_script_path:
                stopper = spawn_owned(script_command(config.llm_stop_script_path),
                                      cwd=str(Path(config.llm_stop_script_path).parent),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + config.service_stop_timeout
                    while stopper.poll() is None:
                        pulse()
                        if time.monotonic() >= deadline:
                            raise TimeoutError("LLM 停止脚本超时")
                        time.sleep(0.1)
                    if stopper.returncode != 0 or not tree_exited(stopper):
                        raise RuntimeError("LLM 停止脚本未正常结束")
                finally:
                    if not tree_exited(stopper):
                        terminate_owned(stopper)
                        stopper.wait(timeout=config.service_stop_timeout)
                    close_owned(stopper)
            else:
                try:
                    if os.name == "nt":
                        self.llm.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        os.killpg(self.llm.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + config.service_stop_timeout
        while not tree_exited(self.llm):
            pulse()
            if time.monotonic() >= deadline:
                raise TimeoutError("LLM 停止超时；请配置停止脚本或手动停止服务后恢复")
            time.sleep(0.1)
        self.llm.wait()
        close_owned(self.llm)
        self.llm = None
        if self.health.llm_port_open(self.llm_settings):
            raise RuntimeError("LLM 进程退出后端口仍在监听，禁止切换")
        with transaction() as (_db, state):
            state["llm_process"] = {}

    def cleanup_failed_llm_start(self, config):
        if self.llm:
            terminate_owned(self.llm)  # No requests are admitted during startup.
            self.llm.wait(timeout=config.service_stop_timeout)
            if not tree_exited(self.llm):
                raise RuntimeError("失败的 LLM 启动进程树未退出")
            close_owned(self.llm)
            self.llm = None
            with transaction() as (_db, state):
                state["llm_process"] = {}
        if self.health.llm_port_open(self.llm_settings):
            raise RuntimeError("LLM 端口未释放，禁止重试启动")

    def start_tts(self):
        # Model loading remains task-specific; allocation is ready to launch a child.
        from ...engines.tts import resolve_engine
        resolve_engine()

    def stop_tts(self):
        with transaction() as (db, _state):
            requests = db.query(GPURequest).filter_by(service="TTS", status="running").all()
            if requests:
                raise RuntimeError("TTS GPU 调用尚未排空")

    def wait_gpu_released(self, config: GPUConfig, pulse):
        deadline = time.monotonic() + config.gpu_release_wait
        while time.monotonic() < deadline:
            pulse()
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    @staticmethod
    def spawn_tts(cmd, request_id=None, **kwargs):
        proc = None
        try:
            with transaction() as (db, _state):
                if request_id:
                    request = db.get(GPURequest, request_id)
                    if request is None or request.status != "running":
                        raise RuntimeError("TTS GPU 许可已失效")
                proc = spawn_owned(cmd, **kwargs)
                proc._gpu_request_id = request_id
                if request_id:
                    request.process = {**request.process, "child": process_identity(proc.pid), "ready": False}
            return proc
        except BaseException:
            if proc:
                terminate_owned(proc)
                proc.wait(timeout=30)
                close_owned(proc)
            raise

    @staticmethod
    def tts_ready(request_id):
        if request_id:
            with transaction() as (db, _state):
                request = db.get(GPURequest, request_id)
                if request:
                    request.process = {**request.process, "ready": True}
            logger.info("[GPU-SCHEDULER] TTS model ready request=%s", request_id)

    @staticmethod
    def cancel_tts(proc):
        terminate_owned(proc)

    @staticmethod
    def finish_tts(proc):
        try:
            GPUServiceManager._finish_tts(proc)
        except BaseException:
            request_id = getattr(proc, "_gpu_request_id", None)
            if request_id:
                with transaction() as (db, state):
                    state.update(state="ERROR", error="TTS 进程树退出确认失败", reason="TTS exit unconfirmed")
                    request = db.get(GPURequest, request_id)
                    if request:
                        request.process = {**request.process, "exit_unconfirmed": True}
            raise

    @staticmethod
    def _finish_tts(proc):
        if not tree_exited(proc):
            terminate_owned(proc)  # Existing cancel/pause/error cleanup contract.
        proc.wait(timeout=30)
        deadline = time.monotonic() + 30
        while not tree_exited(proc):
            if time.monotonic() >= deadline:
                raise RuntimeError("TTS 进程树未完全退出")
            time.sleep(0.05)
        close_owned(proc)
