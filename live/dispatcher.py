"""
Atlas Dispatcher — polls the task queue and routes tasks to local Ollama models
via the ComputeGovernor. Logs routing decisions to the MemoryEngine.
Runs as a standalone process alongside server.py.
"""
import json
import time
import signal
import sys
from pathlib import Path

import requests

from kernel import AtlasRuntimeKernel
from governor import ComputeGovernor
from memory import MemoryEngine

DB_PATH     = str(Path(__file__).parent / "atlas_runtime.db")
OLLAMA_URL  = "http://localhost:11434/api/generate"
WORKER_ID   = "DISPATCHER-001"
POLL_DELAY  = 3   # seconds between queue checks
HEARTBEAT_S = 10  # seconds between heartbeats

kernel   = AtlasRuntimeKernel(db_path=DB_PATH)
governor = ComputeGovernor()
memory   = MemoryEngine(kernel)
running  = True

# Directive tracking for this dispatcher session
_session_directive_id: str | None = None


def handle_signal(sig, frame):
    global running
    print("\n[DISPATCHER] Shutdown signal received.")
    running = False


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def call_ollama(model: str, prompt: str, timeout: int = 180) -> str:
    resp = requests.post(
        OLLAMA_URL,
        json={"model": model, "prompt": prompt, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["response"].strip()


def process_task(task_id: str, payload: dict):
    prompt    = payload.get("prompt", "")
    task_type = payload.get("task_type", "general")
    directive_id = payload.get("directive_id", _session_directive_id or "MANUAL")

    if not prompt:
        kernel.fail_task(task_id, "No prompt in payload")
        return

    model = governor.route(task_type)
    print(f"[DISPATCHER] {task_id} → {model} (type={task_type})")

    # Record the routing decision in memory
    decision_id = memory.record_decision(
        directive_id=directive_id,
        worker_id=WORKER_ID,
        context=f"task={task_id} type={task_type}",
        decision=f"route_to:{model}",
        rationale=f"governor selected {model} for task_type={task_type}",
        trace_id=task_id,
    )

    kernel.set_worker_state(WORKER_ID, "BUSY")
    t0 = time.time()
    try:
        response = call_ollama(model, prompt)
        elapsed_ms = int((time.time() - t0) * 1000)
        governor.record(model, elapsed_ms)
        kernel.set_state("governor_metrics", governor.get_metrics())
        kernel.complete_task(task_id, {"model": model, "response": response[:500], "elapsed_ms": elapsed_ms})
        memory.resolve_decision(decision_id, outcome=f"COMPLETED in {elapsed_ms}ms")
        print(f"[DISPATCHER] {task_id} completed in {elapsed_ms}ms")
    except requests.Timeout:
        err = f"Ollama timeout after {int(time.time()-t0)}s"
        kernel.fail_task(task_id, err)
        memory.resolve_decision(decision_id, outcome=f"FAILED:{err}")
        print(f"[DISPATCHER] {task_id} timed out")
    except Exception as e:
        kernel.fail_task(task_id, str(e))
        memory.resolve_decision(decision_id, outcome=f"FAILED:{e}")
        print(f"[DISPATCHER] {task_id} failed: {e}")
    finally:
        kernel.set_worker_state(WORKER_ID, "ACTIVE")


def main():
    global _session_directive_id

    kernel.register_worker(WORKER_ID, "orchestrator", "ollama", "local", "sandboxed")

    # Register this run as a directive so memory tracks each session
    _session_directive_id = memory.open_directive(
        name="Dispatcher Session",
        description=f"Dispatcher runtime session started at {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        tier=1,
    )

    kernel.log_event("DISPATCHER_START", "DISPATCHER", {"worker_id": WORKER_ID, "directive_id": _session_directive_id})
    print(f"[DISPATCHER] Online. Polling every {POLL_DELAY}s. Ctrl-C to stop.")

    last_heartbeat = time.time()
    last_checkpoint = time.time()

    while running:
        now = time.time()

        if now - last_heartbeat >= HEARTBEAT_S:
            kernel.update_worker_heartbeat(WORKER_ID)
            kernel.detect_timeouts()
            last_heartbeat = now

        # Auto-checkpoint memory every hour
        if now - last_checkpoint >= 3600:
            memory.checkpoint(scope="dispatcher_auto")
            last_checkpoint = now

        task_id, payload = kernel.claim_task(WORKER_ID)
        if task_id:
            process_task(task_id, payload)
        else:
            time.sleep(POLL_DELAY)

    kernel.set_worker_state(WORKER_ID, "OFFLINE")
    memory.close_directive(_session_directive_id, outcome="CLEAN_SHUTDOWN")
    kernel.log_event("DISPATCHER_STOP", "DISPATCHER", {"worker_id": WORKER_ID})
    print("[DISPATCHER] Shutdown complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
