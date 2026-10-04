"""Pure scheduling decisions. No database, process or clock side effects."""
from dataclasses import dataclass

from .config import GPUConfig


@dataclass(frozen=True)
class QueueStats:
    waiting: int = 0
    running: int = 0
    oldest_wait: float = 0


@dataclass(frozen=True)
class Decision:
    target: str | None
    reason: str


class SchedulingPolicy:
    def calculate_llm_pressure(self, queue: QueueStats) -> float:
        return float(queue.waiting)

    def calculate_tts_pressure(self, queue: QueueStats) -> float:
        return float(queue.waiting)

    def decide(self, config: GPUConfig, current: str | None, llm: QueueStats, tts: QueueStats,
               *, runtime: float, since_switch: float, idle_time: float, served: bool) -> Decision:
        queues = {"LLM": llm, "TTS": tts}
        pressures = {"LLM": self.calculate_llm_pressure(llm), "TTS": self.calculate_tts_pressure(tts)}
        if not llm.waiting and not tts.waiting:
            if config.shutdown_when_idle and not llm.running and not tts.running and idle_time >= config.idle_shutdown_timeout:
                return Decision(None, "idle timeout")
            return Decision(current, "no waiting tasks")
        if current is None:
            target = max(queues, key=lambda side: (pressures[side], queues[side].oldest_wait))
            return Decision(target, "initial backlog")
        other = "TTS" if current == "LLM" else "LLM"
        # An empty side can yield immediately, but active GPU calls still count as work.
        if queues[other].waiting and not queues[current].waiting and not queues[current].running:
            return Decision(other, "current queue empty")
        # Backlog or active work guarantees the configured minimum residence time.
        if runtime < config.min_service_runtime:
            return Decision(current, "minimum service runtime")
        if queues[other].waiting and queues[other].oldest_wait >= config.max_wait_time and served:
            return Decision(other, "maximum wait exceeded")
        if since_switch < config.switch_cooldown:
            return Decision(current, "switch cooldown")
        if pressures[other] - pressures[current] >= config.queue_difference_threshold:
            return Decision(other, "pressure difference exceeds threshold")
        return Decision(current, "keep current service")
