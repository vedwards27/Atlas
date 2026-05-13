"""
Governance Agent — enforces operational boundaries and escalation rules.
Watches for: budget overruns, high failure rates, dangerous task patterns,
unapproved directives, and queues requiring human approval.
"""
import time
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.base import BaseAgent

POLL_INTERVAL        = 30   # seconds
FAILURE_RATE_LIMIT   = 0.5  # flag if >50% tasks failed
BUDGET_WARN_FRACTION = 0.8  # warn at 80% of daily budget
DANGEROUS_PATTERNS   = ["rm -rf", "DROP TABLE", "os.system", "subprocess.call", "__import__"]


class GovernanceAgent(BaseAgent):
    def __init__(self, db_path: str):
        super().__init__("GOVERNANCE-001", "governance_agent", "local", "governance_only", db_path)
        self._flags  = 0
        self._blocks = 0

    def _check_failure_rate(self):
        rows = self.kernel._query(
            "SELECT state, COUNT(*) FROM task_queue GROUP BY state"
        )
        counts = {r[0]: r[1] for r in rows}
        total = sum(counts.values())
        failed = counts.get("FAILED", 0) + counts.get("DLQ", 0)
        if total > 10 and failed / total > FAILURE_RATE_LIMIT:
            self._flag("HIGH_FAILURE_RATE", {
                "failed": failed, "total": total,
                "rate": round(failed / total, 2)
            })

    def _check_budget(self):
        metrics = self.kernel.get_state("governor_metrics", {})
        budget = metrics.get("daily_budget_cu", 0)
        day_cost = metrics.get("day_cost", 0.0)
        if budget > 0 and day_cost >= budget * BUDGET_WARN_FRACTION:
            self._flag("BUDGET_WARNING", {
                "day_cost": day_cost,
                "budget": budget,
                "fraction": round(day_cost / budget, 2)
            })

    def _scan_pending_payloads(self):
        """Block tasks containing dangerous patterns before they reach workers."""
        rows = self.kernel._query(
            "SELECT task_id, payload FROM task_queue WHERE state = 'QUEUED' LIMIT 50"
        )
        for task_id, payload_raw in rows:
            payload_str = payload_raw.lower()
            for pattern in DANGEROUS_PATTERNS:
                if pattern.lower() in payload_str:
                    # Block: move to BLOCKED state
                    self.kernel._query(
                        "UPDATE task_queue SET state = 'BLOCKED', last_updated = ? WHERE task_id = ?",
                        (__import__('datetime').datetime.now().isoformat(), task_id),
                        commit=True
                    )
                    self._log("TASK_BLOCKED", {"task_id": task_id, "pattern": pattern})
                    self._blocks += 1
                    self._decide(
                        context=f"task={task_id}",
                        decision="block_dangerous_task",
                        rationale=f"payload contains pattern: {pattern}",
                    )
                    break

    def _flag(self, flag_type: str, detail: dict):
        self._flags += 1
        self._log(f"GOVERNANCE_{flag_type}", detail)
        self._decide(
            context=flag_type,
            decision="raise_governance_flag",
            rationale=json.dumps(detail),
        )

    def _check_worker_diversity(self):
        """Warn if only one execution worker exists (single point of failure)."""
        exec_workers = self.kernel._query(
            "SELECT COUNT(*) FROM worker_registry WHERE role = 'execution_agent' AND runtime_state NOT IN ('OFFLINE','STALLED')"
        )
        count = exec_workers[0][0] if exec_workers else 0
        if count < 2:
            self._flag("LOW_WORKER_REDUNDANCY", {"execution_workers": count})

    def run(self):
        print("[GOVERNANCE] Online. Enforcing operational boundaries.")
        while self._running:
            self._tick()
            self._check_failure_rate()
            self._check_budget()
            self._scan_pending_payloads()
            self._check_worker_diversity()
            time.sleep(POLL_INTERVAL)

    def telemetry(self) -> dict:
        base = super().telemetry()
        base.update({"flags": self._flags, "blocks": self._blocks})
        return base


if __name__ == "__main__":
    db = str(Path(__file__).parent.parent / "atlas_runtime.db")
    GovernanceAgent(db_path=db).start()
