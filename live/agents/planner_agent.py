"""
Planner Agent — decomposes incoming directives into queued tasks.
Polls directive_registry for ACTIVE directives without tasks, creates task plans.
"""
import time
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.base import BaseAgent

POLL_INTERVAL = 5  # seconds


class PlannerAgent(BaseAgent):
    def __init__(self, db_path: str):
        super().__init__("PLANNER-001", "planner_agent", "local", "plan_only", db_path)
        self._planned: set[str] = self._load_planned()

    def _load_planned(self) -> set[str]:
        rows = self.kernel._query(
            "SELECT DISTINCT directive_id FROM task_queue WHERE directive_id IS NOT NULL"
        )
        return {r[0] for r in rows}

    def _plan_directive(self, directive_id: str, name: str, description: str):
        """Break a directive into concrete tasks and queue them."""
        dec_id = self._decide(
            context=f"directive={directive_id} name={name}",
            decision="create_task_plan",
            rationale="new active directive with no queued tasks",
        )
        tasks = [
            {"prompt": f"Execute: {description}", "task_type": "general", "directive_id": directive_id},
            {"prompt": f"Verify completion of: {name}", "task_type": "reasoning", "directive_id": directive_id},
        ]
        task_ids = []
        for i, payload in enumerate(tasks):
            task_id = self.kernel.add_task(directive_id, payload, priority=10 - i)
            task_ids.append(task_id)

        self.memory.resolve_decision(dec_id, outcome=f"planned {len(task_ids)} tasks: {task_ids}")
        self._log("DIRECTIVE_PLANNED", {"directive_id": directive_id, "task_count": len(task_ids), "task_ids": task_ids})
        self._planned.add(directive_id)

    def run(self):
        print(f"[PLANNER] Online. Scanning every {POLL_INTERVAL}s.")
        while self._running:
            self._tick()
            directives = self.kernel.get_directives(state="ACTIVE")
            for row in directives:
                directive_id, name, description = row[0], row[1], row[2] or ""
                if directive_id not in self._planned and not name.endswith("Session"):
                    self._plan_directive(directive_id, name, description)
            time.sleep(POLL_INTERVAL)

    def telemetry(self) -> dict:
        base = super().telemetry()
        base["planned_directives"] = len(self._planned)
        return base


if __name__ == "__main__":
    from pathlib import Path
    db = str(Path(__file__).parent.parent / "atlas_runtime.db")
    PlannerAgent(db_path=db).start()
