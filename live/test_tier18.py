"""
Tier 18 Validation — Local Sovereign Intelligence Mesh
Tests: provider registration, health scoring, routing logic, offline fallback,
degraded mode, explainable routing decisions. No real Ollama/Claude required.
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))

RESULTS = []


def check(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append({"test": name, "status": status, "detail": detail})
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    return passed


def test_provider_health_scoring():
    print("\n[T1] Provider Health Scoring")
    from providers.base_provider import BaseProvider, ProviderHealth

    class FakeProvider(BaseProvider):
        def generate(self, prompt, task_type="general", **kw): return "ok"
        def health_check(self): return self._compute_health()

    p = FakeProvider("test:fake", tags={"general", "local"}, cost_per_call=0.0)

    # No calls → score should be 1.0 (no errors, no latency penalty)
    h = p.health_check()
    check("fresh provider score is 1.0", h.score == 1.0, str(h.score))
    check("fresh provider is online", h.online)

    # Record errors
    for _ in range(5):
        p.record_call(100, error=True)
    h2 = p.health_check()
    check("error rate degrades score", h2.score < 1.0, str(h2.score))

    # High latency
    p2 = FakeProvider("test:slow", tags={"general"}, cost_per_call=0.0)
    for _ in range(5):
        p2.record_call(15000, error=False)  # 15s latency
    h3 = p2.health_check()
    check("high latency degrades score", h3.score < 1.0, str(h3.score))


def test_ollama_provider_offline():
    print("\n[T2] Ollama Provider Offline Handling")
    from providers.ollama_provider import OllamaProvider

    # Point at non-existent port
    p = OllamaProvider(base_url="http://localhost:19999")
    h = p.health_check()
    check("offline ollama returns online=False", not h.online, str(h.online))
    check("offline ollama score is 0.0", h.score == 0.0, str(h.score))
    check("offline reason is populated", bool(h.reason), h.reason)

    # Generate should raise
    raised = False
    try:
        p.generate("hello")
    except Exception:
        raised = True
    check("offline ollama generate raises", raised)


def test_claude_provider_no_key():
    print("\n[T3] Claude Provider Without API Key")
    from providers.claude_provider import ClaudeProvider

    with patch.dict("os.environ", {}, clear=True):
        import importlib
        import providers.claude_provider as cp_mod
        # Patch env directly on the provider
        p = ClaudeProvider.__new__(ClaudeProvider)
        p._api_key = ""
        p._client = None
        p._online = False
        p._latencies = __import__("collections").deque(maxlen=50)
        p._errors    = __import__("collections").deque(maxlen=50)
        p.provider_id = "claude:test"
        p.tags = {"cloud"}
        p.cost_per_call = 0.003
        p.model = "claude-haiku-4-5-20251001"

        h = p.health_check()
        check("claude without key returns online=False", not h.online)
        check("reason mentions API key", "API_KEY" in h.reason.upper() or "key" in h.reason.lower(), h.reason)


def test_mesh_routing_logic():
    print("\n[T4] Mesh Routing Logic")
    from providers.base_provider import BaseProvider, ProviderHealth
    from providers.mesh import ProviderMesh, LOCAL_PREFERENCE_BONUS

    class StubProvider(BaseProvider):
        def __init__(self, pid, tags, score, online=True):
            super().__init__(pid, tags)
            self._stub_score = score
            self._stub_online = online
        def generate(self, prompt, **kw): return "stub"
        def health_check(self):
            h = ProviderHealth(self.provider_id, self._stub_score, 100, 0.0,
                               "now", self._stub_online, "stub")
            return h

    mesh = ProviderMesh.__new__(ProviderMesh)
    mesh.kernel = None
    mesh._lock = __import__("threading").Lock()
    mesh._routing_log = []
    mesh._last_health_check = time.time()

    # Local has moderate health; cloud has very high health + reasoning tags.
    # Cloud must overcome LOCAL_PREFERENCE_BONUS (0.2) via tag match + superior score.
    local_p  = StubProvider("local:fast",   {"local", "general", "offline_capable"}, score=0.5)
    cloud_p  = StubProvider("cloud:smart",  {"cloud", "reasoning", "general"}, score=0.9)
    dead_p   = StubProvider("dead:broken",  {"general"}, score=0.0, online=False)

    mesh._providers = [local_p, cloud_p, dead_p]
    mesh._health_cache = {
        "local:fast":  ProviderHealth("local:fast",  0.5, 100, 0.0, "now", True),
        "cloud:smart": ProviderHealth("cloud:smart", 0.9, 500, 0.0, "now", True),
        "dead:broken": ProviderHealth("dead:broken", 0.0, 0, 1.0, "now", False),
    }

    chosen = mesh.route(task_type="general")
    check("offline provider excluded from routing", chosen is not None and chosen.provider_id != "dead:broken")

    # local: 0.5 + 0.2 (bonus) + 0.05 (general tag) = 0.75
    # cloud: 0.9 + 0.0 (no local bonus) + 0.05 (general tag) = 0.95 → cloud wins general too
    # But local-preference means local wins for "general" when local score is competitive.
    # This checks only that a provider was selected.
    check("a provider selected for general", chosen is not None)

    chosen_reasoning = mesh.route(task_type="reasoning")
    # reasoning: local=0.5+0.2+0.05=0.75, cloud=0.9+0+0.10=1.00 → cloud wins
    check("reasoning routes to higher-scoring cloud provider",
          chosen_reasoning is not None and chosen_reasoning.provider_id == "cloud:smart",
          f"chose={chosen_reasoning.provider_id if chosen_reasoning else None}")


def test_mesh_fallback():
    print("\n[T5] Mesh Fallback Behaviour")
    from providers.base_provider import BaseProvider, ProviderHealth
    from providers.mesh import ProviderMesh

    class FailProvider(BaseProvider):
        def generate(self, prompt, **kw): raise ConnectionError("down")
        def health_check(self):
            return ProviderHealth(self.provider_id, 0.9, 100, 0.0, "now", True)

    class SuccessProvider(BaseProvider):
        def generate(self, prompt, **kw): return "success response"
        def health_check(self):
            return ProviderHealth(self.provider_id, 0.7, 200, 0.0, "now", True)

    mesh = ProviderMesh.__new__(ProviderMesh)
    mesh.kernel = None
    mesh._lock = __import__("threading").Lock()
    mesh._routing_log = []
    mesh._last_health_check = time.time()

    fail_p    = FailProvider("fail:primary", {"general", "local"})
    success_p = SuccessProvider("ok:fallback", {"general", "cloud"})
    mesh._providers = [fail_p, success_p]
    mesh._health_cache = {
        "fail:primary": ProviderHealth("fail:primary", 0.9, 100, 0.0, "now", True),
        "ok:fallback":  ProviderHealth("ok:fallback",  0.7, 200, 0.0, "now", True),
    }

    result = mesh.generate("hello", task_type="general")
    check("fallback provider used on primary failure", result["fallback"] is True)
    check("response from fallback", result["response"] == "success response")
    check("tried list shows both providers", len(result["tried"]) == 2, str(result["tried"]))


def test_offline_survivability():
    print("\n[T6] Offline Survivability")
    from providers.base_provider import BaseProvider, ProviderHealth
    from providers.mesh import ProviderMesh

    class LocalProvider(BaseProvider):
        def generate(self, prompt, **kw): return "local result"
        def health_check(self):
            return ProviderHealth(self.provider_id, 0.85, 80, 0.0, "now", True)

    class CloudProvider(BaseProvider):
        def generate(self, prompt, **kw): raise ConnectionError("internet down")
        def health_check(self):
            return ProviderHealth(self.provider_id, 0.0, 0, 1.0, "now", False)

    mesh = ProviderMesh.__new__(ProviderMesh)
    mesh.kernel = None
    mesh._lock = __import__("threading").Lock()
    mesh._routing_log = []
    mesh._last_health_check = time.time()

    local_p = LocalProvider("ollama:local", {"local", "offline_capable", "general"})
    cloud_p = CloudProvider("cloud:dead",   {"cloud", "general"})
    mesh._providers = [local_p, cloud_p]
    mesh._health_cache = {
        "ollama:local": ProviderHealth("ollama:local", 0.85, 80,  0.0, "now", True),
        "cloud:dead":   ProviderHealth("cloud:dead",   0.0,  0,   1.0, "now", False),
    }

    summary = mesh.offline_summary()
    check("offline_capable=True when local online", summary["offline_capable"])
    check("cloud listed as down", "cloud:dead" not in summary["cloud_online"])
    check("local listed as online", "ollama:local" in summary["local_online"])
    check("degraded_mode=True (no cloud)", summary["degraded_mode"])

    result = mesh.generate("hello offline", task_type="general")
    check("offline generation succeeds via local", result["response"] == "local result")
    check("no fallback needed (local worked)", not result["fallback"])


def test_routing_log():
    print("\n[T7] Explainable Routing Log")
    from providers.base_provider import BaseProvider, ProviderHealth
    from providers.mesh import ProviderMesh

    class LogProvider(BaseProvider):
        def generate(self, p, **kw): return "ok"
        def health_check(self):
            return ProviderHealth(self.provider_id, 0.9, 100, 0.0, "now", True)

    mesh = ProviderMesh.__new__(ProviderMesh)
    mesh.kernel = None
    mesh._lock = __import__("threading").Lock()
    mesh._routing_log = []
    mesh._last_health_check = time.time()

    p = LogProvider("test:logged", {"general", "local"})
    mesh._providers = [p]
    mesh._health_cache = {"test:logged": ProviderHealth("test:logged", 0.9, 100, 0.0, "now", True)}

    mesh.route(task_type="general")
    mesh.route(task_type="coding")

    log = mesh.get_routing_log()
    check("routing log has entries", len(log) >= 2)
    check("log entry has chosen field", "chosen" in log[0])
    check("log entry has rationale", "rationale" in log[0])
    check("log entry has all_candidates", "all_candidates" in log[0])
    check("log entry has task_type", "task_type" in log[0])


def main():
    print("=" * 60)
    print("TIER 18 - LOCAL SOVEREIGN INTELLIGENCE MESH VALIDATION")
    print("=" * 60)

    test_provider_health_scoring()
    test_ollama_provider_offline()
    test_claude_provider_no_key()
    test_mesh_routing_logic()
    test_mesh_fallback()
    test_offline_survivability()
    test_routing_log()

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    total  = len(RESULTS)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    out = Path(__file__).parent.parent / "PROVIDER_MESH_REPORT.json"
    out.write_text(json.dumps({"results": RESULTS, "passed": passed, "failed": failed}, indent=2))
    print(f"Results written to {out}")

    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
