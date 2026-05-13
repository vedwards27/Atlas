# LIVE_FAILURE_ROOT_CAUSE.md
Generated: 2026-05-13

## Why localhost:8080 returned "Not Found"

Port 8080 is owned by the **main Atlas Life OS app** (`C:\Atlas`), configured in
`C:\Atlas\vite.config.js` with `server: { port: 8080, strictPort: true }`. At the
time of investigation a stale or crashed node process (PID 21452, 8 MB RSS) held
the port but was no longer serving content, returning a bare "Not Found" string
with no HTML body — indicating the Vite process had lost its internal state.

## Full Port Inventory at Failure Time

| Port | PID | Process | Size | Actual content |
|------|-----|---------|------|----------------|
| 8080 | 21452 | node.exe | 8 MB | `Not Found` (stale/crashed) |
| 8081 | 32172 | node.exe | 845 MB | Main Atlas Life OS Vite dev server |
| 8082 | — | — | — | **Free** (chosen for FastAPI) |
| 5173 | — | — | — | Not running |

## Root Causes

1. **FastAPI (`server.py`) was not running.** Nothing had started it. The runtime
   had been stopped and not relaunched since the last session.

2. **Port 8081 was occupied by the main Atlas Life OS Vite.** The main Atlas app
   (`C:\Atlas`) had drifted from port 8080 to 8081 (possibly because a prior Vite
   instance already held 8080), blocking FastAPI from binding its intended port.

3. **`live/dashboard/dist` did not exist.** The dashboard had never been built, so
   even if FastAPI had started on 8081 it would have served API-only (no UI).

4. **`start.ps1` only launched server.py + dispatcher.py + npm dev.** It did not
   launch the orchestrator (multi-agent supervisor), and pointed to the now-blocked
   port 8081.

5. **No process supervision.** Services were launched as one-shot processes with no
   watchdog. A single crash leaves the runtime dark with no recovery.

## Resolution Applied

| Step | Action |
|------|--------|
| Port conflict | Changed FastAPI to port **8082** (free, no conflict) |
| Proxy update | Updated `live/dashboard/vite.config.ts` proxy target to `:8082` |
| Dashboard build | Ran `npm run build` — produced `dist/` (156 kB JS, 11 kB CSS) |
| Server start | Launched `uvicorn server:app --port 8082` as background process |
| Vite dev server | Launched via Preview tool on port 5173 (proxies `/api` → `:8082`) |
| `start.ps1` | Rewritten: port 8082, adds orchestrator, builds dashboard before serving |

## Verified Live State

```
GET http://localhost:8082/api/status
→ {"status":"OPERATIONAL","version":"1.0.0","uptime_seconds":300+,
   "workers":17,"tasks":{"queued":14,"running":0,"completed":1,"failed":0}}

GET http://localhost:5173/
→ Atlas Runtime dashboard — LIVE, streaming events, workers visible

Proxy chain proven:
  Browser (5173) → Vite proxy → FastAPI (8082) → SQLite (atlas_runtime.db)
```

## Permanent Ports

| Service | Port | URL |
|---------|------|-----|
| FastAPI (prod + dashboard) | 8082 | http://localhost:8082 |
| Dashboard dev (Vite HMR) | 5173 | http://localhost:5173 |
| Main Atlas Life OS | 8080 | http://localhost:8080 (separate app) |
