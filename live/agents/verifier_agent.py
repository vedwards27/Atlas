"""
Verifier Agent — scans completed tasks for quality and flags anomalies.
Checks: empty results, suspiciously short responses, tasks stuck in RUNNING.
"""
import time
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.base import BaseAgent

POLL_INTERVAL    = 15   # seconds
STUCK_THRESHOLD  = 120  # seconds before a RUNNING task is considered stuck
MAX_VERIFY_BATCH = 20


class VerifierAgent(BaseAgent):
    def __init__(self, db_path: str):
        super().__init__("VERIFIER-001", "verifier_agent", "local", "read_verify", db_path)
        self._verified = 0
        self._flagged  = 0
        self._verified_ids: set[str] = set()

    def _verify_completed(self):
        rows = self.kernel._query(
            "SELECT task_id, directive_id, payload, last_updated FROM task_queue WHERE state = 'COMPLETED' ORDER BY last_updated DESC LIMIT ?",
            (MAX_VERIFY_BATCH,)
        )
        for row in rows:
            task_id, directive_id, payload_raw, updated = row
            if task_id in self._verified_ids:
                continue

            payload = json.loads(payload_raw)
            # Check for suspicious results logged in event_ledger
            events = self.kernel._query(
                "SELECT payload FROM event_ledger WHERE type = 'TASK_COMPLETED' AND payload LIKE ? LIMIT 1",
                (f'%{task_id}%',)
            )
            if events:
                result = json.loads(events[0][0])
                response = result.get("result", {}).get("response", "")
                if not response:
                    self._flag(task_id, directive_id, "empty_response")
                elif len(response) < 10:
                    self._flag(task_id, directive_id, f"suspiciously_short:{len(response)}chars")
                else:
                    self._log("TASK_VERIFIED", {"task_id": task_id, "response_len": len(response)})

            self._verified_ids.add(task_id)
            self._verified += 1

    def _flag(self, task_id: str, directive_id: str, reason: str):
        self._flagged += 1
        self._log("VERIFICATION_FLAG", {"task_id": task_id, "directive_id": directive_id, "reason": reason})
        self._decide(
            context=f"task={task_id}",
            decision=f"flag:{reason}",
            rationale="automated quality gate",
        )

    def _check_stuck_tasks(self):
        threshold = (datetime.now() - timedelta(seconds=STUCK_THRESHOLD)).isoformat()
        stuck = self.kernel._query(
            "SELECT task_id, worker_id, last_updated FROM task_queue WHERE state = 'RUNNING' AND last_updated < ?",
            (threshold,)
        )
        for row in stuck:
            task_id, worker_id, updated = row
            self._log("STUCK_TASK_DETECTED", {"task_id": task_id, "worker_id": worker_id, "last_updated": updated})
            # Re-queue the stuck task
            self.kernel._query(
                "UPDATE task_queue SET state = 'QUEUED', worker_id = NULL, last_updated = ? WHERE task_id = ? AND state = 'RUNNING'",
                (datetime.now().isoformat(), task_id),
                commit=True
            )
            self._log("STUCK_TASK_REQUEUED", {"task_id": task_id})

    def run(self):
        print("[VERIFIER] Online. Scanning completed tasks.")
        while self._running:
            self._tick()
            self._verify_completed()
            self._check_stuck_tasks()
            time.sleep(POLL_INTERVAL)

    def telemetry(self) -> dict:
        base = super().telemetry()
        base.update({"verified": self._verified, "flagged": self._flagged})
        return base


if __name__ == "__main__":
    db = str(Path(__file__).parent.parent / "atlas_runtime.db")
    VerifierAgent(db_path=db).start()
