"""
Claude (Anthropic) provider — cloud inference via the Anthropic API.
Requires ANTHROPIC_API_KEY environment variable.
Falls back gracefully if key is absent or API is unreachable.
"""
import os
import time
from datetime import datetime

from providers.base_provider import BaseProvider, ProviderHealth

MODEL = "claude-haiku-4-5-20251001"   # fastest/cheapest Anthropic model


class ClaudeProvider(BaseProvider):
    def __init__(self, model: str = MODEL):
        super().__init__(
            provider_id=f"claude:{model}",
            tags={"cloud", "reasoning", "general", "coding", "long_context"},
            cost_per_call=0.003,  # approximate USD per call
        )
        self.model = model
        self._api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = None
        self._init_client()

    def _init_client(self):
        if not self._api_key:
            self._online = False
            return
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
            self._online = True
        except ImportError:
            self._online = False

    def generate(self, prompt: str, task_type: str = "general", **kwargs) -> str:
        if not self._online or not self._client:
            raise RuntimeError("Claude provider unavailable (no API key or anthropic package missing)")
        t0 = time.time()
        try:
            msg = self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            elapsed = int((time.time() - t0) * 1000)
            self.record_call(elapsed, error=False)
            return msg.content[0].text
        except Exception as e:
            self.record_call(int((time.time() - t0) * 1000), error=True)
            raise

    def health_check(self) -> ProviderHealth:
        if not self._api_key:
            self._online = False
            h = self._compute_health()
            h.reason = "ANTHROPIC_API_KEY not set"
            return h
        if not self._client:
            self._online = False
            h = self._compute_health()
            h.reason = "anthropic package not installed"
            return h
        # Lightweight check — just verify the client is initialised
        self._online = True
        h = self._compute_health()
        h.reason = "api_key_present"
        return h
