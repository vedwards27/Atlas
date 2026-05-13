# OFFLINE SURVIVABILITY REPORT — TIER 18
Generated: 2026-05-13

---

## What Survives Internet Disconnection

| Component | Offline Behaviour |
|---|---|
| OllamaProvider | Fully operational (local GPU inference) |
| kernel.py / SQLite | Fully operational (local file) |
| memory.py / MemoryEngine | Fully operational (SQLite-backed) |
| All 6 Tier 17 agents | Fully operational (in-process, no internet needed) |
| ClaudeProvider | Fails gracefully — marked offline, excluded from routing |
| OpenAICompatProvider | Fails gracefully — same |
| server.py / FastAPI | Operational (local port 8081) |
| dashboard | Operational (local port 5173) |

## What Requires Internet

- Claude inference (`ANTHROPIC_API_KEY` + Anthropic API)
- OpenAI-compat inference (external base URL + API key)
- Ollama model downloads (`ollama pull` — only needed once)

## Degraded Mode

When `degraded_mode=True` (all cloud offline):
- All task routing goes to local Ollama models
- GovernanceAgent continues enforcing limits
- RecoveryAgent continues retrying failures
- Memory, events, decisions, snapshots all persist normally
- Dashboard shows `mesh/offline` as `degraded_mode: true`

## Recovery on Reconnection

ProviderMesh runs health checks every 30 seconds. When cloud providers come
back online, they automatically re-enter the routing pool with their current
health score. No restart required.
