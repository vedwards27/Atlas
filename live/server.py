import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from kernel import AtlasRuntimeKernel
from memory import MemoryEngine
from providers.mesh import ProviderMesh
from survivability import SurvivabilityEngine

DB_PATH = str(Path(__file__).parent / "atlas_runtime.db")
DIST_PATH = Path(__file__).parent / "dashboard" / "dist"

kernel       = AtlasRuntimeKernel(db_path=DB_PATH)
memory       = MemoryEngine(kernel)
mesh         = ProviderMesh(kernel=kernel)
survivability = SurvivabilityEngine(kernel, memory, DB_PATH)
_start_time  = time.time()

# On startup: restore any tasks/workers left RUNNING/ACTIVE from a prior crash
survivability.restore_interrupted_tasks()
survivability.restore_stalled_workers()


@asynccontextmanager
async def lifespan(app: FastAPI):
    kernel.log_event("SYSTEM_START", "SERVER", {"version": "2.0.0"})
    yield
    kernel.log_event("SYSTEM_STOP", "SERVER", {})


app = FastAPI(title="Atlas Runtime API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Status ────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    worker_count = kernel._query("SELECT COUNT(*) FROM worker_registry")[0][0]
    task_rows = kernel._query("SELECT state, COUNT(*) FROM task_queue GROUP BY state")
    task_counts = {r[0]: r[1] for r in task_rows}
    return {
        "status": "OPERATIONAL",
        "version": "1.0.0",
        "uptime_seconds": int(time.time() - _start_time),
        "workers": worker_count,
        "tasks": {
            "queued":    task_counts.get("QUEUED", 0),
            "running":   task_counts.get("RUNNING", 0),
            "completed": task_counts.get("COMPLETED", 0),
            "failed":    task_counts.get("FAILED", 0),
        },
    }


# ── Event Ledger ──────────────────────────────────────────────────────────────

@app.get("/api/events")
def get_events(limit: int = 30):
    rows = kernel._query(
        "SELECT event_id, timestamp, type, source, payload FROM event_ledger ORDER BY event_id DESC LIMIT ?",
        (limit,),
    )
    return [
        {"id": r[0], "timestamp": r[1], "type": r[2], "source": r[3], "payload": json.loads(r[4])}
        for r in rows
    ]


# ── Workers ───────────────────────────────────────────────────────────────────

@app.get("/api/workers")
def get_workers():
    rows = kernel._query(
        "SELECT worker_id, role, provider_type, execution_scope, runtime_state, last_heartbeat FROM worker_registry"
    )
    return [
        {"id": r[0], "role": r[1], "provider": r[2], "scope": r[3], "state": r[4], "heartbeat": r[5]}
        for r in rows
    ]


# ── Task Queue ────────────────────────────────────────────────────────────────

@app.get("/api/tasks")
def get_tasks(limit: int = 20):
    rows = kernel._query(
        "SELECT task_id, directive_id, state, priority, payload, worker_id, created_at FROM task_queue ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [
        {
            "id":         r[0],
            "directive":  r[1],
            "state":      r[2],
            "priority":   r[3],
            "payload":    json.loads(r[4]),
            "worker":     r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


@app.post("/api/tasks")
def submit_task(body: dict):
    directive_id = body.get("directive_id", "MANUAL")
    payload      = body.get("payload", {})
    priority     = int(body.get("priority", 1))
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    task_id = kernel.add_task(directive_id, payload, priority)
    return {"task_id": task_id}


# ── Provider Mesh ─────────────────────────────────────────────────────────────

@app.get("/api/providers")
async def get_providers():
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:11434/api/tags", timeout=4)
            models = resp.json().get("models", [])
        return [
            {"id": m["name"], "size": m["details"]["parameter_size"], "family": m["details"]["family"], "status": "ONLINE"}
            for m in models
        ]
    except Exception:
        return []


# ── ComputeGovernor metrics (written by dispatcher) ───────────────────────────

@app.get("/api/governor/metrics")
def get_governor_metrics():
    return kernel.get_state("governor_metrics", default={
        "total_routed": 0,
        "total_cost": 0.0,
        "day_cost": 0.0,
        "daily_budget_cu": 0,
        "by_provider": {},
    })


# ── Memory Engine ─────────────────────────────────────────────────────────────

@app.get("/api/memory/summary")
def get_memory_summary():
    return memory.get_operational_summary()

@app.get("/api/memory/hydration")
def get_hydration():
    return memory.get_hydration_state()

@app.get("/api/memory/timeline")
def get_timeline(directive_id: str = None, limit: int = 100):
    return memory.reconstruct_timeline(directive_id=directive_id, limit=limit)

@app.get("/api/memory/directives")
def get_directives(state: str = None):
    rows = kernel.get_directives(state=state)
    return [
        {"directive_id": r[0], "name": r[1], "description": r[2], "state": r[3],
         "tier": r[4], "parent_id": r[5], "created_at": r[6], "completed_at": r[7],
         "outcome": r[8], "lineage": json.loads(r[9])}
        for r in rows
    ]

@app.post("/api/memory/directives")
def create_directive(body: dict):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    directive_id = memory.open_directive(
        name=name,
        description=body.get("description", ""),
        tier=body.get("tier"),
        parent_id=body.get("parent_id"),
    )
    return {"directive_id": directive_id}

@app.post("/api/memory/directives/{directive_id}/complete")
def complete_directive(directive_id: str, body: dict = {}):
    memory.close_directive(directive_id, outcome=body.get("outcome", "SUCCESS"))
    return {"status": "completed"}

@app.get("/api/memory/directives/{directive_id}/lineage")
def get_lineage(directive_id: str):
    return memory.get_lineage(directive_id)

@app.get("/api/memory/decisions")
def get_decisions(directive_id: str = None, limit: int = 50):
    return memory.get_decision_history(directive_id=directive_id, limit=limit)

@app.post("/api/memory/decisions")
def record_decision(body: dict):
    required = {"directive_id", "worker_id", "context", "decision", "rationale"}
    if not required.issubset(body):
        raise HTTPException(status_code=400, detail=f"Required fields: {required}")
    decision_id = memory.record_decision(
        directive_id=body["directive_id"],
        worker_id=body["worker_id"],
        context=body["context"],
        decision=body["decision"],
        rationale=body["rationale"],
        trace_id=body.get("trace_id"),
    )
    return {"decision_id": decision_id}

@app.get("/api/memory/search")
def search_memory(q: str = Query(..., min_length=1), limit: int = 20):
    return memory.search(q, limit=limit)

@app.get("/api/memory/snapshots")
def get_snapshots(limit: int = 10):
    return memory.get_snapshots(limit=limit)

@app.post("/api/memory/checkpoint")
def take_checkpoint(body: dict = {}):
    result = memory.checkpoint(scope=body.get("scope", "manual"))
    return result


# ── Provider Mesh (Tier 18) ───────────────────────────────────────────────────

@app.get("/api/mesh/health")
def get_mesh_health():
    return mesh.get_health()

@app.get("/api/mesh/offline")
def get_mesh_offline():
    return mesh.offline_summary()

@app.get("/api/mesh/routing-log")
def get_routing_log(limit: int = 20):
    return mesh.get_routing_log(limit=limit)

@app.get("/api/mesh/providers")
def get_mesh_providers():
    return mesh.get_provider_ids()

@app.post("/api/mesh/generate")
async def mesh_generate(body: dict):
    prompt    = body.get("prompt", "").strip()
    task_type = body.get("task_type", "general")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt required")
    try:
        result = mesh.generate(prompt, task_type=task_type)
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

@app.post("/api/mesh/refresh")
def refresh_mesh():
    mesh._refresh_health()
    return {"status": "refreshed", "providers": len(mesh.get_provider_ids())}


# ── Agent Telemetry (Tier 17) ─────────────────────────────────────────────────

@app.get("/api/agents/telemetry")
def get_agent_telemetry():
    rows = kernel._query(
        "SELECT key, value, last_updated FROM kernel_state WHERE key LIKE 'telemetry:%'"
    )
    result = {}
    for key, value_raw, updated in rows:
        try:
            result[key.replace("telemetry:", "")] = {"data": json.loads(value_raw), "updated": updated}
        except Exception:
            pass
    return result

@app.get("/api/agents/diagnostics")
def get_diagnostics():
    return kernel.get_state("diagnostics_report", default={})

@app.get("/api/agents/governance")
def get_governance_events(limit: int = 20):
    rows = kernel._query(
        "SELECT event_id, timestamp, type, source, payload FROM event_ledger WHERE type LIKE 'GOVERNANCE_%' ORDER BY event_id DESC LIMIT ?",
        (limit,)
    )
    return [{"id": r[0], "timestamp": r[1], "type": r[2], "source": r[3], "payload": json.loads(r[4])} for r in rows]

@app.get("/api/agents/recovery")
def get_recovery_events(limit: int = 20):
    recovery_types = ("TASK_RECOVERED_FROM_STALL", "TASK_RETRIED", "TASK_MOVED_TO_DLQ", "WORKER_STALL_RECOVERED", "ORPHAN_WORKER_CLEANED")
    placeholders = ",".join("?" * len(recovery_types))
    rows = kernel._query(
        f"SELECT event_id, timestamp, type, source, payload FROM event_ledger WHERE type IN ({placeholders}) ORDER BY event_id DESC LIMIT ?",
        (*recovery_types, limit)
    )
    return [{"id": r[0], "timestamp": r[1], "type": r[2], "source": r[3], "payload": json.loads(r[4])} for r in rows]


# ── Survivability (Tier 19) ───────────────────────────────────────────────────

@app.get("/api/survivability/integrity")
def get_integrity():
    return survivability.check_db_integrity()

@app.get("/api/survivability/score")
def get_integrity_score():
    return survivability.get_integrity_score()

@app.get("/api/survivability/drift")
def get_drift():
    return survivability.check_event_drift()

@app.get("/api/survivability/duplicates")
def get_duplicates():
    return survivability.check_for_duplicates()

@app.get("/api/survivability/fingerprint")
def get_fingerprint():
    return {"fingerprint": survivability.compute_state_fingerprint(), "timestamp": __import__("datetime").datetime.now().isoformat()}

@app.post("/api/survivability/checkpoint")
def force_checkpoint(body: dict = {}):
    return survivability.take_checkpoint(reason=body.get("reason", "manual"))

@app.post("/api/survivability/simulate/{scenario}")
def simulate(scenario: str):
    allowed = {"queue_corruption", "stale_worker", "checkpoint_restore", "integrity_check", "duplicate_root"}
    if scenario not in allowed:
        raise HTTPException(status_code=400, detail=f"scenario must be one of {allowed}")
    return survivability.simulate_recovery(scenario)

@app.post("/api/survivability/restore-queue")
def restore_queue():
    count = survivability.restore_interrupted_tasks()
    return {"restored": count}


# ── Serve built dashboard (production) ───────────────────────────────────────

if DIST_PATH.exists():
    app.mount("/", StaticFiles(directory=str(DIST_PATH), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8081, reload=False)
