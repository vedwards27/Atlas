import time

# Cost in compute units per task (local Ollama — reflects relative GPU time)
PROVIDER_TABLE = {
    "deepseek-coder:latest": {"cost": 0.1, "speed": 10, "tags": {"coding", "fast", "general"}},
    "llama3.2:latest":       {"cost": 0.3, "speed": 8,  "tags": {"general", "fast"}},
    "mistral:latest":        {"cost": 0.7, "speed": 6,  "tags": {"general", "reasoning"}},
    "qwen2.5-coder:latest":  {"cost": 0.7, "speed": 6,  "tags": {"coding", "general"}},
    "llama3:latest":         {"cost": 0.8, "speed": 5,  "tags": {"general", "reasoning"}},
    "gemma2:latest":         {"cost": 0.9, "speed": 4,  "tags": {"reasoning", "general"}},
    "phi4:latest":           {"cost": 1.4, "speed": 3,  "tags": {"reasoning", "general"}},
}

TASK_TYPE_PREFERRED = {
    "fast":      ["deepseek-coder:latest", "llama3.2:latest"],
    "coding":    ["qwen2.5-coder:latest", "deepseek-coder:latest", "llama3:latest"],
    "reasoning": ["phi4:latest", "gemma2:latest", "mistral:latest"],
    "general":   ["llama3.2:latest", "llama3:latest", "mistral:latest"],
}

class ComputeGovernor:
    def __init__(self, daily_budget_cu: float = 0):
        self.daily_budget_cu = daily_budget_cu  # 0 = unlimited
        self._day_start = time.time()
        self._day_cost = 0.0
        self._by_provider: dict[str, dict] = {
            p: {"count": 0, "cost": 0.0, "total_ms": 0} for p in PROVIDER_TABLE
        }
        self._total_routed = 0
        self._total_cost = 0.0

    def _reset_day_if_needed(self):
        if time.time() - self._day_start > 86400:
            self._day_start = time.time()
            self._day_cost = 0.0

    def route(self, task_type: str = "general") -> str:
        self._reset_day_if_needed()
        candidates = TASK_TYPE_PREFERRED.get(task_type, TASK_TYPE_PREFERRED["general"])
        for model in candidates:
            if model not in PROVIDER_TABLE:
                continue
            cost = PROVIDER_TABLE[model]["cost"]
            if self.daily_budget_cu > 0 and self._day_cost + cost > self.daily_budget_cu:
                continue
            return model
        return "llama3.2:latest"  # safe fallback

    def record(self, model: str, elapsed_ms: int):
        cost = PROVIDER_TABLE.get(model, {}).get("cost", 0.5)
        self._total_cost += cost
        self._day_cost += cost
        self._total_routed += 1
        if model in self._by_provider:
            self._by_provider[model]["count"] += 1
            self._by_provider[model]["cost"] += cost
            self._by_provider[model]["total_ms"] += elapsed_ms

    def get_metrics(self) -> dict:
        active = {p: v for p, v in self._by_provider.items() if v["count"] > 0}
        return {
            "total_routed": self._total_routed,
            "total_cost": round(self._total_cost, 3),
            "day_cost": round(self._day_cost, 3),
            "daily_budget_cu": self.daily_budget_cu,
            "by_provider": {
                p: {
                    "count": v["count"],
                    "cost": round(v["cost"], 3),
                    "avg_ms": round(v["total_ms"] / v["count"]) if v["count"] else 0,
                }
                for p, v in active.items()
            },
        }
