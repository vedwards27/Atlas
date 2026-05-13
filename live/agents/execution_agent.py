"""
Execution Agent — claims and executes tasks from the queue.
Supports Ollama for inference tasks; echoes prompt for non-inference tasks.
"""
import time
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.base import BaseAgent
from governor import ComputeGovernor

POLL_INTERVAL = 2
OLLAMA_URL    = "http://localhost:11434/api/generate"


class ExecutionAgent(BaseAgent):
    def __init__(self, agent_id: str, db_path: str):
        super().__init__(agent_id, "execution_agent", "local", "execute_tasks", db_path)
        self.governor = ComputeGovernor()
        self._tasks_done = 0
        self._tasks_failed = 0

    def _call_ollama(self, model: str, prompt: str, timeout: int = 120) -> str:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["response"].strip()

    def _execute(self, task_id: str, payload: dict):
        prompt    = payload.get("prompt", "")
        task_type = payload.get("task_type", "general")
        directive_id = payload.get("directive_id", self._directive_id or "UNKNOWN")

        if not prompt:
            self.kernel.fail_task(task_id, "empty prompt")
            self._tasks_failed += 1
            return

        model = self.governor.route(task_type)
        dec_id = self._decide(
            context=f"task={task_id} type={task_type}",
            decision=f"execute_via:{model}",
            rationale=f"governor routing for {task_type}",
        )

        self.kernel.set_worker_state(self.agent_id, "BUSY")
        t0 = time.time()
        try:
            response = self._call_ollama(model, prompt)
            elapsed_ms = int((time.time() - t0) * 1000)
            self.governor.record(model, elapsed_ms)
            self.kernel.set_state("governor_metrics", self.governor.get_metrics())
            self.kernel.complete_task(task_id, {"model": model, "response": response[:500], "elapsed_ms": elapsed_ms})
            self.memory.resolve_decision(dec_id, outcome=f"COMPLETED:{elapsed_ms}ms")
            self._tasks_done += 1
        except requests.Timeout:
            err = f"timeout after {int(time.time()-t0)}s"
            self.kernel.fail_task(task_id, err)
            self.memory.resolve_decision(dec_id, outcome=f"FAILED:{err}")
            self._tasks_failed += 1
        except requests.ConnectionError:
            # Ollama unavailable — mark failed, do not crash agent
            err = "Ollama unavailable"
            self.kernel.fail_task(task_id, err)
            self.memory.resolve_decision(dec_id, outcome=f"FAILED:{err}")
            self._tasks_failed += 1
        except Exception as e:
            self.kernel.fail_task(task_id, str(e))
            self.memory.resolve_decision(dec_id, outcome=f"FAILED:{e}")
            self._tasks_failed += 1
        finally:
            self.kernel.set_worker_state(self.agent_id, "ACTIVE")

    def run(self):
        print(f"[{self.agent_id}] Execution agent online.")
        while self._running:
            self._tick()
            task_id, payload = self.kernel.claim_task(self.agent_id)
            if task_id:
                self._execute(task_id, payload)
            else:
                time.sleep(POLL_INTERVAL)

    def telemetry(self) -> dict:
        base = super().telemetry()
        base.update({"tasks_done": self._tasks_done, "tasks_failed": self._tasks_failed})
        return base


if __name__ == "__main__":
    agent_id = sys.argv[1] if len(sys.argv) > 1 else "EXEC-001"
    db = str(Path(__file__).parent.parent / "atlas_runtime.db")
    ExecutionAgent(agent_id=agent_id, db_path=db).start()
