"""
OpenAI-compatible provider — works with any OpenAI-format API endpoint.
Covers: OpenAI, local vLLM, LM Studio, llama.cpp server, Groq, etc.
Configure via environment variables:
  OPENAI_COMPAT_BASE_URL  (default: https://api.openai.com/v1)
  OPENAI_COMPAT_API_KEY   (default: empty)
  OPENAI_COMPAT_MODEL     (default: gpt-4o-mini)
"""
import os
import time

from providers.base_provider import BaseProvider, ProviderHealth

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


class OpenAICompatProvider(BaseProvider):
    def __init__(self):
        self.base_url = os.environ.get("OPENAI_COMPAT_BASE_URL", DEFAULT_BASE)
        self.api_key  = os.environ.get("OPENAI_COMPAT_API_KEY", "")
        self.model    = os.environ.get("OPENAI_COMPAT_MODEL", DEFAULT_MODEL)
        super().__init__(
            provider_id=f"openai_compat:{self.model}@{self.base_url}",
            tags={"cloud", "general", "coding", "reasoning"},
            cost_per_call=0.001,
        )
        self._client = None
        self._init_client()

    def _init_client(self):
        if not self.api_key:
            self._online = False
            return
        try:
            import openai
            self._client = openai.OpenAI(base_url=self.base_url, api_key=self.api_key)
            self._online = True
        except ImportError:
            self._online = False

    def generate(self, prompt: str, task_type: str = "general", **kwargs) -> str:
        if not self._online or not self._client:
            raise RuntimeError("OpenAI-compat provider unavailable")
        t0 = time.time()
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
            )
            elapsed = int((time.time() - t0) * 1000)
            self.record_call(elapsed, error=False)
            return resp.choices[0].message.content
        except Exception as e:
            self.record_call(int((time.time() - t0) * 1000), error=True)
            raise

    def health_check(self) -> ProviderHealth:
        if not self.api_key:
            self._online = False
            h = self._compute_health()
            h.reason = "OPENAI_COMPAT_API_KEY not set"
            return h
        if not self._client:
            self._online = False
            h = self._compute_health()
            h.reason = "openai package not installed"
            return h
        self._online = True
        h = self._compute_health()
        h.reason = "api_key_present"
        return h
