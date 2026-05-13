# LOCAL INTELLIGENCE VALIDATION — TIER 18
Generated: 2026-05-13

---

## Validation Matrix

| Scenario | Expected | Verified |
|---|---|---|
| Ollama at wrong port → health_check | online=False, score=0.0 | PASS |
| Ollama offline → generate | raises ConnectionError | PASS |
| Claude no API key → health_check | online=False, reason="key not set" | PASS |
| Local provider + cloud both online | local gets 0.20 routing bonus | PASS |
| Cloud has superior health for reasoning | cloud selected for reasoning task | PASS |
| Primary provider fails → generate | fallback to secondary, fallback=True | PASS |
| All cloud offline → generate | local succeeds, no fallback needed | PASS |
| route() called → log entry | log has chosen, score, rationale, candidates | PASS |

## Local-First Guarantee

The `LOCAL_PREFERENCE_BONUS = 0.2` in `providers/mesh.py` ensures that a local
Ollama model must be outscored by at least 0.20 health points for a cloud
provider to be selected. A healthy local Ollama (score=1.0) is essentially
unbeatable unless the cloud provider has perfect health and perfect tag match.

## Provider Auto-Discovery

On startup, `ProviderMesh._register_providers()` calls
`OllamaProvider().list_models()` and creates one `OllamaProvider` instance per
discovered model. No manual configuration required.
