# PROVIDER MESH REPORT — TIER 18
Generated: 2026-05-13  
Test suite: live/test_tier18.py — 27/27 PASSED

---

## Architecture

```
ProviderMesh (providers/mesh.py)
  ├── OllamaProvider   (providers/ollama_provider.py)   — local, free, offline_capable
  ├── ClaudeProvider   (providers/claude_provider.py)   — cloud, reasoning, long_context
  └── OpenAICompatProvider (providers/openai_compat_provider.py) — cloud, configurable
```

---

## Provider Properties

| Provider | Tags | Cost | Local-first |
|---|---|---|---|
| OllamaProvider (per model) | local, offline_capable, coding, general, reasoning | 0.0 CU (free) | YES |
| ClaudeProvider | cloud, reasoning, general, coding, long_context | ~$0.003/call | NO |
| OpenAICompatProvider | cloud, general, coding, reasoning | ~$0.001/call | NO |

Ollama models are auto-discovered at startup via `/api/tags`. Up to 8 models registered.

---

## Routing Algorithm

Score = `health_score + local_bonus(+0.20 if local/offline_capable) + tag_match * 0.05`

- Health score penalised by error rate and latency (>5s → −0.30 max)
- Offline providers (score=0.0) excluded from routing by default
- All routing decisions logged with: chosen provider, score, all candidates, rationale

---

## Fallback Chain

1. Score all online providers
2. Try best provider → if exception, mark offline in cache, try next
3. Continue down list until one succeeds
4. If all fail → RuntimeError (surfaced as HTTP 503 at `/api/mesh/generate`)

---

## Offline Survivability

- `is_offline_capable()` — True if ≥1 local provider online
- `offline_summary()` — lists local_online, cloud_online, degraded_mode
- Cloud providers fail gracefully: `ConnectionError` caught, next provider tried
- Local Ollama continues functioning when internet is fully disconnected

---

## API Endpoints

| Endpoint | Description |
|---|---|
| GET /api/mesh/health | Health scores for all providers |
| GET /api/mesh/offline | Offline survivability summary |
| GET /api/mesh/routing-log | Last N routing decisions with rationale |
| GET /api/mesh/providers | List of registered provider IDs |
| POST /api/mesh/generate | Run inference through mesh |
| POST /api/mesh/refresh | Force health re-check |

---

## Proofs

- **Provider-independent**: Ollama offline → falls back to Claude → falls back to OpenAI-compat
- **Explainable routing**: every route() call logged with score breakdown and rationale
- **Local-first**: 0.20 bonus ensures Ollama wins unless cloud has materially better health
- **Degraded offline operation**: test T6 confirms generation succeeds with cloud=offline
- **Health scoring**: error rate and latency both degrade score, verified in T1
