"""Administrator-only GPU configuration; never part of a task snapshot."""
from pathlib import Path
import hashlib
import json
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..database import SessionLocal
from ..models import SystemConfig
from ..system_config import load_feature_defaults
from ...core.config import LLMConfig


class GPUConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    scheduler_interval: float = Field(default=5, ge=0.1, le=300)
    min_service_runtime: float = Field(default=300, ge=0, le=86400)
    switch_cooldown: float = Field(default=30, ge=0, le=86400)
    queue_difference_threshold: float = Field(default=3, gt=0, le=100000)
    max_wait_time: float = Field(default=300, gt=0, le=86400)
    shutdown_when_idle: bool = False
    idle_shutdown_timeout: float = Field(default=300, ge=0, le=86400)
    startup_timeout: float = Field(default=300, gt=0, le=3600)
    service_stop_timeout: float = Field(default=30, gt=0, le=600)
    drain_timeout: float = Field(default=1800, gt=0, le=86400)
    health_check_interval: float = Field(default=2, ge=0.1, le=60)
    gpu_release_wait: float = Field(default=3, ge=0, le=60)
    startup_retry_count: int = Field(default=3, ge=1, le=10)
    llm_start_script_path: str = ""
    llm_stop_script_path: str = ""

    @model_validator(mode="after")
    def validate_scripts(self):
        for value in (self.llm_start_script_path, self.llm_stop_script_path):
            if value and (not Path(value).is_absolute() or Path(value).suffix.lower() not in {".ps1", ".cmd", ".bat", ".sh"}):
                raise ValueError("LLM 脚本必须是绝对路径的 .ps1、.cmd、.bat 或 .sh 文件")
            if value and Path(value).suffix.lower() in {".cmd", ".bat"}:
                from ...core.managed_process import script_command
                script_command(value)
        if self.enabled and not self.llm_start_script_path:
            raise ValueError("启用 GPU 调度前请配置 LLM 启动脚本路径")
        return self


def load_config(db=None) -> GPUConfig:
    if db is None:
        with SessionLocal() as session:
            return load_config(session)
    row = db.get(SystemConfig, "gpu_scheduler")
    return GPUConfig.model_validate(row.value if row else {})


def platform_llm() -> LLMConfig:
    return LLMConfig.model_validate(load_feature_defaults().get("llm", {}))


def service_fingerprint(config, llm=None):
    llm = llm or platform_llm()
    data = [config.enabled, config.llm_start_script_path, config.llm_stop_script_path,
            llm.base_url, llm.api_key, llm.model_name]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def validate_enabled(config: GPUConfig, llm: LLMConfig | None = None) -> None:
    if not config.enabled:
        return
    for value in (config.llm_start_script_path, config.llm_stop_script_path):
        if value:
            from ...core.managed_process import validate_script_platform
            validate_script_platform(value)
            if not Path(value).is_file():
                raise ValueError("LLM 脚本文件不存在")
    llm = llm or platform_llm()
    url = urlsplit(llm.base_url)
    if url.scheme not in {"http", "https"} or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("单 GPU 调度仅支持本机 LLM 服务地址")
    if not llm.model_name.strip():
        raise ValueError("启用 GPU 调度前请配置 LLM 模型名称")
