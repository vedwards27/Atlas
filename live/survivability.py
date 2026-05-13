"""
Atlas Survivability Engine — Tier 19
Auto-recovery, checkpoint snapshots, drift detection, corruption detection,
canonical truth enforcement, and operational integrity scoring.
"""
import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from kernel import AtlasRuntimeKernel
from memory import MemoryEngine

CHECKPOINT_INTERVAL_S  = 300   # auto-checkpoint every 5 minutes
INTEGRITY_CHECK_INTERVAL = 60  # DB integrity check every minute
DLQ_WARN_THRESHOLD     = 5     # warn when DLQ exceeds this
DRIFT_WINDOW_S         = 120   # flag if event rate drops to zero for this long


class SurvivabilityEngine:
    def __init__(self, kernel: AtlasRuntimeKernel, memory: MemoryEngine, db_path: str):
        self.kernel   = kernel
        self.memory   = memory
        self.db_path  = db_path
        self._lock    = threading.Lock()
        self._last_checkpoint_s  = time.time()
        self._last_integrity_s   = time.time()
        self._last_event_ts: str | None = None
        self._integrity_score    = 1.0
        self._recovery_count     = 0
        self._checkpoint_count   = 0

    # ── Checkpoint snapshots ──────────────────────────────────────────────────

    def take_checkpoint(self, reason: str = "scheduled") -> dict:
        """Snapshot current state and persist a DB-level backup."""
        snap = self.memory.checkpoint(scope=f"survivability:{reason}")
        self._checkpoint_count += 1
        self.kernel.log_event("SURVIVABILITY_CHECKPOINT", "SURVIVABILITY", {
            "snapshot_id": snap["snapshot_id"],
            "reason": reason,
            "checkpoint_count": self._checkpoint_count,
        })
        return snap

    def maybe_checkpoint(self):
        now = time.time()
        if now - self._last_checkpoint_s >= CHECKPOINT_INTERVAL_S:
            self.take_checkpoint(reason="auto")
            self._last_checkpoint_s = now

    # ── Queue restoration ─────────────────────────────────────────────────────

    def restore_interrupted_tasks(self) -> int:
        """
        On startup: any task stuck in RUNNING state from a prior crashed process
        is re-queued. Returns count of restored tasks.
        """
        stuck = self.kernel._query(
            "SELECT task_id, worker_id FROM task_queue WHERE state = 'RUNNING'"
        )
        for task_id, worker_id in stuck:
            self.kernel._query(
                "UPDATE task_queue SET state = 'QUEUED', worker_id = NULL, last_updated = ? WHERE task_id = ?",
                (datetime.now().isoformat(), task_id), commit=True
            )
            self.kernel.log_event("TASK_RESTORED_ON_STARTUP", "SURVIVABILITY", {
                "task_id": task_id, "from_worker": worker_id
            })
        if stuck:
            self._recovery_count += len(stuck)
        return len(stuck)

    def restore_stalled_workers(self) -> int:
        """Mark all non-OFFLINE workers as STALLED on startup (they died without clean shutdown)."""
        stalled = self.kernel._query(
            "SELECT worker_id FROM worker_registry WHERE runtime_state NOT IN ('OFFLINE', 'STALLED')"
        )
        for (worker_id,) in stalled:
            self.kernel._query(
                "UPDATE worker_registry SET runtime_state = 'STALLED' WHERE worker_id = ?",
                (worker_id,), commit=True
            )
        return len(stalled)

    # ── DB integrity ──────────────────────────────────────────────────────────

    def check_db_integrity(self) -> dict:
        """Run SQLite PRAGMA integrity_check and verify critical tables exist."""
        try:
            conn = sqlite3.connect(self.db_path)
            result = conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            integrity_ok = result[0] == "ok"
        except Exception as e:
            return {"ok": False, "error": str(e), "score": 0.0}

        # Verify tables exist
        required_tables = {
            "worker_registry", "task_queue", "event_ledger",
            "kernel_state", "directive_registry", "decision_log", "memory_snapshots"
        }
        existing = {r[0] for r in self.kernel._query(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing = required_tables - existing

        score = 1.0
        if not integrity_ok:
            score -= 0.5
        score -= len(missing) * 0.1
        score = max(0.0, score)

        self._integrity_score = score
        return {
            "ok": integrity_ok and not missing,
            "integrity_check": "ok" if integrity_ok else "FAILED",
            "missing_tables": list(missing),
            "score": round(score, 2),
        }

    def maybe_integrity_check(self):
        now = time.time()
        if now - self._last_integrity_s >= INTEGRITY_CHECK_INTERVAL:
            result = self.check_db_integrity()
            if not result["ok"]:
                self.kernel.log_event("DB_INTEGRITY_FAILURE", "SURVIVABILITY", result)
            self._last_integrity_s = now
            return result
        return None

    # ── Drift detection ───────────────────────────────────────────────────────

    def check_event_drift(self) -> dict:
        """Flag if no new events have been logged for DRIFT_WINDOW_S seconds."""
        row = self.kernel._query(
            "SELECT timestamp FROM event_ledger ORDER BY event_id DESC LIMIT 1"
        )
        if not row:
            return {"drift": False, "last_event": None}

        last_ts = row[0][0]
        try:
            last_dt = datetime.fromisoformat(last_ts)
        except ValueError:
            return {"drift": False, "last_event": last_ts}

        age_s = (datetime.now() - last_dt).total_seconds()
        drift = age_s > DRIFT_WINDOW_S

        if drift:
            self.kernel.log_event("EVENT_DRIFT_DETECTED", "SURVIVABILITY", {
                "last_event_ts": last_ts,
                "age_seconds": round(age_s),
                "threshold": DRIFT_WINDOW_S,
            })
        return {"drift": drift, "last_event": last_ts, "age_seconds": round(age_s)}

    # ── Canonical truth enforcement ───────────────────────────────────────────

    def compute_state_fingerprint(self) -> str:
        """Hash the current canonical state for change detection."""
        rows = self.kernel._query(
            "SELECT task_id, state, worker_id FROM task_queue ORDER BY task_id"
        )
        workers = self.kernel._query(
            "SELECT worker_id, runtime_state FROM worker_registry ORDER BY worker_id"
        )
        payload = json.dumps({"tasks": list(rows), "workers": list(workers)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def check_for_duplicates(self) -> dict:
        """Detect duplicate worker registrations for the same role."""
        rows = self.kernel._query(
            "SELECT role, COUNT(*) as cnt FROM worker_registry WHERE runtime_state NOT IN ('OFFLINE','STALLED') GROUP BY role HAVING cnt > 1"
        )
        duplicates = [{"role": r[0], "count": r[1]} for r in rows]
        if duplicates:
            self.kernel.log_event("DUPLICATE_WORKER_DETECTED", "SURVIVABILITY", {"duplicates": duplicates})
        return {"duplicates": duplicates, "ok": len(duplicates) == 0}

    # ── Operational integrity score ───────────────────────────────────────────

    def get_integrity_score(self) -> dict:
        """Composite score 0–100 covering DB health, queue health, worker health."""
        task_rows = self.kernel._query("SELECT state, COUNT(*) FROM task_queue GROUP BY state")
        counts = {r[0]: r[1] for r in task_rows}
        total = sum(counts.values()) or 1
        dlq_pct   = counts.get("DLQ", 0) / total
        failed_pct = counts.get("FAILED", 0) / total
        blocked_pct = counts.get("BLOCKED", 0) / total

        worker_rows = self.kernel._query("SELECT runtime_state, COUNT(*) FROM worker_registry GROUP BY runtime_state")
        wstates = {r[0]: r[1] for r in worker_rows}
        total_workers = sum(wstates.values()) or 1
        stalled_pct = wstates.get("STALLED", 0) / total_workers

        score = 100
        score -= dlq_pct    * 30
        score -= failed_pct * 20
        score -= blocked_pct * 10
        score -= stalled_pct * 20
        score -= (1.0 - self._integrity_score) * 20
        score = max(0, round(score))

        return {
            "score": score,
            "grade": "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 50 else "F",
            "task_counts": counts,
            "worker_states": wstates,
            "db_integrity": round(self._integrity_score, 2),
            "checkpoints_taken": self._checkpoint_count,
            "tasks_recovered": self._recovery_count,
            "generated_at": datetime.now().isoformat(),
        }

    # ── Recovery simulation harness ───────────────────────────────────────────

    def simulate_recovery(self, scenario: str) -> dict:
        """
        Simulate a failure scenario and verify Atlas recovers.
        Returns {"scenario", "steps", "recovered": bool}
        """
        steps = []

        if scenario == "queue_corruption":
            # Inject orphaned RUNNING tasks (simulating a process kill)
            t1 = self.kernel.add_task("DIR-SIM", {"prompt": "sim task", "task_type": "general"})
            self.kernel._query("UPDATE task_queue SET state = 'RUNNING', worker_id = 'DEAD-WORKER' WHERE task_id = ?", (t1,), commit=True)
            steps.append(f"injected orphaned RUNNING task {t1}")
            count = self.restore_interrupted_tasks()
            steps.append(f"restore_interrupted_tasks recovered {count} tasks")
            state = self.kernel._query("SELECT state FROM task_queue WHERE task_id = ?", (t1,))
            recovered = state[0][0] == "QUEUED" if state else False
            steps.append(f"task state is now: {state[0][0] if state else 'missing'}")

        elif scenario == "stale_worker":
            self.kernel.register_worker("SIM-WORKER-STALE", "test", "sim", "local", "none")
            steps.append("registered SIM-WORKER-STALE")
            count = self.restore_stalled_workers()
            steps.append(f"restore_stalled_workers marked {count} workers STALLED")
            state = self.kernel._query("SELECT runtime_state FROM worker_registry WHERE worker_id = 'SIM-WORKER-STALE'")
            recovered = state[0][0] == "STALLED" if state else False
            steps.append(f"worker state: {state[0][0] if state else 'missing'}")

        elif scenario == "checkpoint_restore":
            snap = self.take_checkpoint(reason="sim_test")
            steps.append(f"checkpoint taken: {snap['snapshot_id']}")
            snaps = self.memory.get_snapshots()
            recovered = any(s["snapshot_id"] == snap["snapshot_id"] for s in snaps)
            steps.append(f"snapshot retrievable: {recovered}")

        elif scenario == "integrity_check":
            result = self.check_db_integrity()
            steps.append(f"integrity_check: {result}")
            recovered = result["ok"]

        elif scenario == "duplicate_root":
            # Register two workers with same role
            self.kernel.register_worker("DUP-A", "dup_role", "sim", "local", "none")
            self.kernel.register_worker("DUP-B", "dup_role", "sim", "local", "none")
            steps.append("registered two workers with dup_role")
            result = self.check_for_duplicates()
            steps.append(f"duplicate check: {result['duplicates']}")
            recovered = len(result["duplicates"]) > 0  # detection = recovery step 1
            steps.append("duplicates detected (operator must deregister one)")

        else:
            return {"scenario": scenario, "steps": ["unknown scenario"], "recovered": False}

        self.kernel.log_event("FAILURE_SIMULATION", "SURVIVABILITY", {
            "scenario": scenario, "steps": steps, "recovered": recovered
        })
        return {"scenario": scenario, "steps": steps, "recovered": recovered}

    # ── Continuous run ────────────────────────────────────────────────────────

    def run_once(self) -> dict:
        """Run one survivability tick — checkpoint, integrity, drift. Returns status."""
        self.maybe_checkpoint()
        integrity = self.maybe_integrity_check()
        drift = self.check_event_drift()
        score = self.get_integrity_score()
        return {
            "integrity": integrity,
            "drift": drift,
            "integrity_score": score,
            "timestamp": datetime.now().isoformat(),
        }
