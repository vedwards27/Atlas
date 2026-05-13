"""
Base provider interface — all Atlas inference providers implement this.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from collections import deque


@dataclass
class ProviderHealth:
    provider_id: str
    score: float          # 0.0 (dead) to 1.0 (perfect)
    latency_ms_avg: float
    error_rate: float
    last_check: str
    online: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "score": round(self.score, 3),
            "latency_ms_avg": round(self.latency_ms_avg),
            "error_rate": round(self.error_rate, 3),
            "last_check": self.last_check,
            "online": self.online,
            "reason": self.reason,
        }


class BaseProvider(ABC):
    def __init__(self, provider_id: str, tags: set[str], cost_per_call: float = 0.1):
        self.provider_id    = provider_id
        self.tags           = tags
        self.cost_per_call  = cost_per_call
        self._latencies: deque = deque(maxlen=50)
        self._errors: deque   = deque(maxlen=50)
        self._online: bool    = True

    @abstractmethod
    def generate(self, prompt: str, task_type: str = "general", **kwargs) -> str:
        """Run inference and return the response string."""

    @abstractmethod
    def health_check(self) -> ProviderHealth:
        """Probe liveness and return a ProviderHealth snapshot."""

    def record_call(self, elapsed_ms: int, error: bool = False):
        self._latencies.append(elapsed_ms)
        self._errors.append(1 if error else 0)

    def _compute_health(self) -> ProviderHealth:
        avg_lat = sum(self._latencies) / len(self._latencies) if self._latencies else 0
        err_rate = sum(self._errors) / len(self._errors) if self._errors else 0
        # Score: starts at 1.0, penalised by latency (>5s → -0.3) and error rate
        score = max(0.0, 1.0 - err_rate - min(0.3, avg_lat / 20000))
        if not self._online:
            score = 0.0
        return ProviderHealth(
            provider_id=self.provider_id,
            score=score,
            latency_ms_avg=avg_lat,
            error_rate=err_rate,
            last_check=datetime.now().isoformat(),
            online=self._online,
        )
