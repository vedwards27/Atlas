"""
Atlas Dispatcher — polls the task queue and routes tasks to local Ollama models
via the ComputeGovernor. Runs as a standalone process alongside server.py.
"""
import json
import time
import signal
import sys
from pathlib import Path

import requests

from kernel import AtlasRuntimeKernel
from governor import ComputeGovernor

DB_PATH     = str(Path(__file__).parent / "atlas_runtime.db")
OLLAMA_URL  = "http://localhost:11434/api/generate"
WORKER_ID   = "DISPATCHER-001"
POLL_DELAY  = 3  # seconds between queue checks
HEARTBEAT_S = 10 # heartbeat interval

kernel   = AtlasRuntimeKernel(db_path=DB_PATH)
governor = ComputeGovernor()
running  = True


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

    if not prompt:
        kernel.fail_task(task_id, "No prompt in payload")
        return

    model = governor.route(task_type)
    print(f"[DISPATCHER] {task_id} → {model} (type={task_type})")

    kernel.set_worker_state(WORKER_ID, "BUSY")
    t0 = time.time()
    try:
        response = call_ollama(model, prompt)
        elapsed_ms = int((time.time() - t0) * 1000)
        governor.record(model, elapsed_ms)
        kernel.set_state("governor_metrics", governor.get_metrics())
        kernel.complete_task(task_id, {"model": model, "response": response[:500], "elapsed_ms": elapsed_ms})
        print(f"[DISPATCHER] {task_id} completed in {elapsed_ms}ms")
    except requests.Timeout:
        kernel.fail_task(task_id, f"Ollama timeout after {int(time.time()-t0)}s")
        print(f"[DISPATCHER] {task_id} timed out")
    except Exception as e:
        kernel.fail_task(task_id, str(e))
        print(f"[DISPATCHER] {task_id} failed: {e}")
    finally:
        kernel.set_worker_state(WORKER_ID, "ACTIVE")


def main():
    kernel.register_worker(WORKER_ID, "orchestrator", "ollama", "local", "sandboxed")
    kernel.log_event("DISPATCHER_START", "DISPATCHER", {"worker_id": WORKER_ID})
    print(f"[DISPATCHER] Online. Polling every {POLL_DELAY}s. Ctrl-C to stop.")

    last_heartbeat = time.time()

    while running:
        now = time.time()

        # Heartbeat
        if now - last_heartbeat >= HEARTBEAT_S:
            kernel.update_worker_heartbeat(WORKER_ID)
            kernel.detect_timeouts()
            last_heartbeat = now

        # Claim and process one task
        task_id, payload = kernel.claim_task(WORKER_ID)
        if task_id:
            process_task(task_id, payload)
        else:
            time.sleep(POLL_DELAY)

    kernel.set_worker_state(WORKER_ID, "OFFLINE")
    kernel.log_event("DISPATCHER_STOP", "DISPATCHER", {"worker_id": WORKER_ID})
    print("[DISPATCHER] Shutdown complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
