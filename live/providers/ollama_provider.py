"""
Ollama provider — local-first inference via Ollama HTTP API.
Supports model selection, health check, and graceful degradation when offline.
"""
import time
import requests
from datetime import datetime

from providers.base_provider import BaseProvider, ProviderHealth

OLLAMA_BASE = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2:latest"


class OllamaProvider(BaseProvider):
    def __init__(self, model: str = DEFAULT_MODEL, base_url: str = OLLAMA_BASE):
        super().__init__(
            provider_id=f"ollama:{model}",
            tags={"local", "offline_capable", "coding", "general", "reasoning"},
            cost_per_call=0.0,  # free (local GPU)
        )
        self.model    = model
        self.base_url = base_url

    def generate(self, prompt: str, task_type: str = "general", timeout: int = 120, **kwargs) -> str:
        t0 = time.time()
        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=timeout,
            )
            resp.raise_for_status()
            elapsed = int((time.time() - t0) * 1000)
            self.record_call(elapsed, error=False)
            self._online = True
            return resp.json()["response"].strip()
        except requests.Timeout:
            self.record_call(int((time.time() - t0) * 1000), error=True)
            raise
        except requests.ConnectionError:
            self._online = False
            self.record_call(0, error=True)
            raise

    def health_check(self) -> ProviderHealth:
        try:
            t0 = time.time()
            resp = requests.get(f"{self.base_url}/api/tags", timeout=3)
            elapsed = int((time.time() - t0) * 1000)
            models = [m["name"] for m in resp.json().get("models", [])]
            self._online = self.model in models or len(models) > 0
            self.record_call(elapsed, error=not self._online)
            h = self._compute_health()
            h.reason = f"models_available={len(models)}"
            return h
        except Exception as e:
            self._online = False
            h = self._compute_health()
            h.reason = str(e)
            return h

    def list_models(self) -> list[str]:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=3)
            return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            return []
