"""
Atlas Provider Mesh — Tier 18
Capability-aware, health-scored, local-first provider arbitration.
Supports: Ollama (local), Claude (cloud), OpenAI-compat (cloud/local).
Degrades gracefully when providers are offline.
All routing decisions are logged and explainable.
"""
import threading
import time
import json
from datetime import datetime
from typing import Optional

from providers.base_provider import BaseProvider, ProviderHealth
from providers.ollama_provider import OllamaProvider
from providers.claude_provider import ClaudeProvider
from providers.openai_compat_provider import OpenAICompatProvider

HEALTH_CHECK_INTERVAL = 30   # seconds between provider health probes
LOCAL_PREFERENCE_BONUS = 0.2  # boost local providers to prefer them over cloud

# Task type → preferred provider tags (in priority order)
TASK_TAG_MAP = {
    "coding":    ["coding", "local", "general"],
    "reasoning": ["reasoning", "long_context", "general"],
    "fast":      ["local", "general"],
    "general":   ["general", "local"],
}


class ProviderMesh:
    def __init__(self, kernel=None):
        self.kernel = kernel  # optional — for logging routing decisions
        self._lock = threading.Lock()
        self._providers: list[BaseProvider] = []
        self._health_cache: dict[str, ProviderHealth] = {}
        self._last_health_check = 0.0
        self._routing_log: list[dict] = []

        self._register_providers()
        self._refresh_health()

    # ── Provider registration ─────────────────────────────────────────────────

    def _register_providers(self):
        """Register all providers. Ollama models are auto-discovered."""
        # Always register Ollama — it's local-first
        ollama_base = OllamaProvider()
        discovered = ollama_base.list_models()
        if discovered:
            for model in discovered[:8]:  # cap at 8 to avoid noise
                self._providers.append(OllamaProvider(model=model))
        else:
            # Ollama reachable but no models loaded — register default anyway
            self._providers.append(OllamaProvider())

        # Cloud providers (gracefully offline if keys missing)
        self._providers.append(ClaudeProvider())
        self._providers.append(OpenAICompatProvider())

    def get_provider_ids(self) -> list[str]:
        return [p.provider_id for p in self._providers]

    # ── Health management ─────────────────────────────────────────────────────

    def _refresh_health(self):
        """Run health checks on all providers (parallel via threads)."""
        results = {}
        threads = []

        def check(p: BaseProvider):
            try:
                h = p.health_check()
                results[p.provider_id] = h
            except Exception as e:
                results[p.provider_id] = ProviderHealth(
                    provider_id=p.provider_id,
                    score=0.0,
                    latency_ms_avg=0,
                    error_rate=1.0,
                    last_check=datetime.now().isoformat(),
                    online=False,
                    reason=str(e),
                )

        for provider in self._providers:
            t = threading.Thread(target=check, args=(provider,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=5)

        with self._lock:
            self._health_cache = results
            self._last_health_check = time.time()

    def _maybe_refresh_health(self):
        if time.time() - self._last_health_check > HEALTH_CHECK_INTERVAL:
            self._refresh_health()

    def get_health(self) -> list[dict]:
        self._maybe_refresh_health()
        return [h.to_dict() for h in self._health_cache.values()]

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, task_type: str = "general", require_online: bool = True) -> Optional[BaseProvider]:
        """
        Select best provider for task_type.
        Scoring: health_score + local_preference_bonus, filtered by required tags.
        Returns None if no provider available (caller must handle offline degradation).
        """
        self._maybe_refresh_health()
        preferred_tags = TASK_TAG_MAP.get(task_type, ["general"])

        candidates = []
        for provider in self._providers:
            health = self._health_cache.get(provider.provider_id)
            if health is None:
                continue
            if require_online and not health.online:
                continue

            # Tag match score
            tag_match = sum(1 for tag in preferred_tags if tag in provider.tags)
            if tag_match == 0:
                continue

            # Composite score
            score = health.score
            if "local" in provider.tags or "offline_capable" in provider.tags:
                score += LOCAL_PREFERENCE_BONUS
            score += tag_match * 0.05

            candidates.append((score, provider, health))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_provider, best_health = candidates[0]

        self._record_routing(task_type, best_provider, best_score, candidates)
        return best_provider

    def _record_routing(self, task_type: str, chosen: BaseProvider, score: float, candidates: list):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "task_type": task_type,
            "chosen": chosen.provider_id,
            "score": round(score, 3),
            "all_candidates": [
                {"provider": p.provider_id, "score": round(s, 3), "online": h.online}
                for s, p, h in candidates
            ],
            "rationale": f"highest composite score (health+tags+locality) for task_type={task_type}",
        }
        self._routing_log.append(entry)
        if len(self._routing_log) > 500:
            self._routing_log = self._routing_log[-500:]

        if self.kernel:
            self.kernel.set_state("mesh_last_routing", entry)

    def get_routing_log(self, limit: int = 20) -> list[dict]:
        return self._routing_log[-limit:]

    # ── Inference with fallback ───────────────────────────────────────────────

    def generate(self, prompt: str, task_type: str = "general", trace_id: str = None) -> dict:
        """
        Run inference with automatic fallback.
        Returns: {"response": str, "provider": str, "elapsed_ms": int, "fallback": bool}
        """
        tried = []
        self._maybe_refresh_health()

        # Try providers in score order until one succeeds
        preferred_tags = TASK_TAG_MAP.get(task_type, ["general"])
        ordered = self._score_all(preferred_tags)

        for provider in ordered:
            tried.append(provider.provider_id)
            t0 = time.time()
            try:
                response = provider.generate(prompt, task_type=task_type)
                elapsed_ms = int((time.time() - t0) * 1000)
                provider.record_call(elapsed_ms, error=False)
                return {
                    "response": response,
                    "provider": provider.provider_id,
                    "elapsed_ms": elapsed_ms,
                    "fallback": len(tried) > 1,
                    "tried": tried,
                }
            except Exception as e:
                provider.record_call(int((time.time() - t0) * 1000), error=True)
                health = self._health_cache.get(provider.provider_id)
                if health:
                    health.online = False
                continue  # try next provider

        raise RuntimeError(f"All providers failed for task_type={task_type}. Tried: {tried}")

    def _score_all(self, preferred_tags: list[str]) -> list[BaseProvider]:
        scored = []
        for provider in self._providers:
            health = self._health_cache.get(provider.provider_id)
            if health is None or not health.online:
                continue
            tag_match = sum(1 for tag in preferred_tags if tag in provider.tags)
            score = health.score + tag_match * 0.05
            if "local" in provider.tags:
                score += LOCAL_PREFERENCE_BONUS
            scored.append((score, provider))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored]

    # ── Offline survivability ─────────────────────────────────────────────────

    def is_offline_capable(self) -> bool:
        """True if at least one local provider is online."""
        for provider in self._providers:
            if "local" in provider.tags or "offline_capable" in provider.tags:
                h = self._health_cache.get(provider.provider_id)
                if h and h.online:
                    return True
        return False

    def offline_summary(self) -> dict:
        local_online = []
        cloud_online = []
        for provider in self._providers:
            h = self._health_cache.get(provider.provider_id)
            online = h.online if h else False
            if "local" in provider.tags or "offline_capable" in provider.tags:
                if online:
                    local_online.append(provider.provider_id)
            else:
                if online:
                    cloud_online.append(provider.provider_id)
        return {
            "offline_capable": len(local_online) > 0,
            "local_online": local_online,
            "cloud_online": cloud_online,
            "degraded_mode": len(cloud_online) == 0,
        }
