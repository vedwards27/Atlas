"""
Recovery Agent — detects and recovers from runtime failures.
Responsibilities: stalled workers, failed tasks (with retry), orphaned sessions,
duplicate root detection, dead-letter handling.
"""
import time
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.base import BaseAgent

POLL_INTERVAL      = 20   # seconds
WORKER_TIMEOUT_S   = 30   # stalled if no heartbeat for this long
MAX_RETRIES        = 3    # max task retry before DLQ
DLQ_DIRECTIVE      = "DLQ-SYSTEM"


class RecoveryAgent(BaseAgent):
    def __init__(self, db_path: str):
        super().__init__("RECOVERY-001", "recovery_agent", "local", "recovery_only", db_path)
        self._recoveries  = 0
        self._dlq_count   = 0
        self._ensure_dlq()

    def _ensure_dlq(self):
        existing = self.kernel._query("SELECT directive_id FROM directive_registry WHERE name = ?", (DLQ_DIRECTIVE,))
        if not existing:
            self.kernel.register_directive(DLQ_DIRECTIVE, "Dead-letter queue for unrecoverable tasks", tier=17)

    def _recover_stalled_workers(self):
        threshold = (datetime.now() - timedelta(seconds=WORKER_TIMEOUT_S)).isoformat()
        stalled = self.kernel._query(
            "SELECT worker_id FROM worker_registry WHERE last_heartbeat < ? AND runtime_state NOT IN ('OFFLINE','STALLED')",
            (threshold,)
        )
        for (worker_id,) in stalled:
            self.kernel._query("UPDATE worker_registry SET runtime_state = 'STALLED' WHERE worker_id = ?", (worker_id,), commit=True)
            # Re-queue tasks owned by stalled worker
            stuck_tasks = self.kernel._query(
                "SELECT task_id FROM task_queue WHERE worker_id = ? AND state = 'RUNNING'", (worker_id,)
            )
            for (task_id,) in stuck_tasks:
                self.kernel._query(
                    "UPDATE task_queue SET state = 'QUEUED', worker_id = NULL, last_updated = ? WHERE task_id = ?",
                    (datetime.now().isoformat(), task_id), commit=True
                )
                self._log("TASK_RECOVERED_FROM_STALL", {"task_id": task_id, "from_worker": worker_id})
                self._recoveries += 1
            self._log("WORKER_STALL_RECOVERED", {"worker_id": worker_id, "tasks_requeued": len(stuck_tasks)})

    def _retry_failed_tasks(self):
        failed = self.kernel._query(
            "SELECT task_id, directive_id, payload, retry_count FROM task_queue WHERE state = 'FAILED' AND retry_count < ?",
            (MAX_RETRIES,)
        )
        for row in failed:
            task_id, directive_id, payload_raw, retry_count = row
            new_count = retry_count + 1
            dec_id = self._decide(
                context=f"task={task_id} retry={new_count}/{MAX_RETRIES}",
                decision="retry_task",
                rationale=f"below max retries ({MAX_RETRIES}), re-queuing",
            )
            self.kernel._query(
                "UPDATE task_queue SET state = 'QUEUED', worker_id = NULL, retry_count = ?, last_updated = ? WHERE task_id = ?",
                (new_count, datetime.now().isoformat(), task_id), commit=True
            )
            self._log("TASK_RETRIED", {"task_id": task_id, "retry": new_count})
            self.memory.resolve_decision(dec_id, outcome=f"requeued attempt {new_count}")
            self._recoveries += 1

    def _move_to_dlq(self):
        """Tasks that have exhausted retries go to the DLQ directive."""
        exhausted = self.kernel._query(
            "SELECT task_id, payload FROM task_queue WHERE state = 'FAILED' AND retry_count >= ?",
            (MAX_RETRIES,)
        )
        dlq_row = self.kernel._query("SELECT directive_id FROM directive_registry WHERE name = ?", (DLQ_DIRECTIVE,))
        dlq_id = dlq_row[0][0] if dlq_row else None
        for row in exhausted:
            task_id, payload_raw = row
            self.kernel._query(
                "UPDATE task_queue SET state = 'DLQ', directive_id = ?, last_updated = ? WHERE task_id = ?",
                (dlq_id, datetime.now().isoformat(), task_id), commit=True
            )
            self._log("TASK_MOVED_TO_DLQ", {"task_id": task_id})
            self._dlq_count += 1

    def _detect_orphan_runtimes(self):
        """Workers ONLINE but never seen a heartbeat in threshold — mark OFFLINE."""
        threshold = (datetime.now() - timedelta(seconds=WORKER_TIMEOUT_S * 3)).isoformat()
        orphans = self.kernel._query(
            "SELECT worker_id FROM worker_registry WHERE runtime_state = 'ONLINE' AND last_heartbeat < ?",
            (threshold,)
        )
        for (worker_id,) in orphans:
            self.kernel._query("UPDATE worker_registry SET runtime_state = 'OFFLINE' WHERE worker_id = ?", (worker_id,), commit=True)
            self._log("ORPHAN_WORKER_CLEANED", {"worker_id": worker_id})

    def run(self):
        print("[RECOVERY] Online. Monitoring for failures.")
        while self._running:
            self._tick()
            self._recover_stalled_workers()
            self._retry_failed_tasks()
            self._move_to_dlq()
            self._detect_orphan_runtimes()
            time.sleep(POLL_INTERVAL)

    def telemetry(self) -> dict:
        base = super().telemetry()
        base.update({"recoveries": self._recoveries, "dlq_count": self._dlq_count})
        return base


if __name__ == "__main__":
    db = str(Path(__file__).parent.parent / "atlas_runtime.db")
    RecoveryAgent(db_path=db).start()
