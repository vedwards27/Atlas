import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from kernel import AtlasRuntimeKernel

DB_PATH = str(Path(__file__).parent / "atlas_runtime.db")
DIST_PATH = Path(__file__).parent / "dashboard" / "dist"

kernel = AtlasRuntimeKernel(db_path=DB_PATH)
_start_time = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    kernel.log_event("SYSTEM_START", "SERVER", {"version": "1.0.0"})
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


# ── Serve built dashboard (production) ───────────────────────────────────────

if DIST_PATH.exists():
    app.mount("/", StaticFiles(directory=str(DIST_PATH), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8081, reload=False)
