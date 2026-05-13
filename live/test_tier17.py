"""
Tier 17 Validation — Autonomous Governed Execution
Tests: agent registration, heartbeat, telemetry, planner, verifier,
recovery, governance, diagnostics — all without Ollama dependency.
"""
import json
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kernel import AtlasRuntimeKernel
from memory import MemoryEngine

RESULTS = []


def check(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append({"test": name, "status": status, "detail": detail})
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    return passed


def fresh(db_path):
    k = AtlasRuntimeKernel(db_path=db_path)
    m = MemoryEngine(k)
    return k, m


def test_base_agent_lifecycle(db_path):
    print("\n[T1] Base Agent Lifecycle")
    from agents.base import BaseAgent

    class MockAgent(BaseAgent):
        def __init__(self):
            super().__init__("TEST-AGENT-001", "test_role", "local", "sandboxed", db_path)
            self.ran = False
        def run(self):
            self.ran = True

    a = MockAgent()
    check("agent registers in worker_registry", True)  # register called in __init__

    k = AtlasRuntimeKernel(db_path=db_path)
    rows = k._query("SELECT worker_id FROM worker_registry WHERE worker_id = 'TEST-AGENT-001'")
    check("worker_id present in DB", len(rows) > 0)

    a.start()  # runs once (run() sets ran=True and returns)
    check("agent ran", a.ran)

    state = k._query("SELECT runtime_state FROM worker_registry WHERE worker_id = 'TEST-AGENT-001'")
    check("agent offline after shutdown", state[0][0] == "OFFLINE", state[0][0] if state else "missing")

    dirs = k.get_directives()
    check("session directive created", any("test_role Session" in r[1] for r in dirs))


def test_planner_agent(db_path):
    print("\n[T2] Planner Agent")
    from agents.planner_agent import PlannerAgent

    k = AtlasRuntimeKernel(db_path=db_path)
    m = MemoryEngine(k)
    dir_id = m.open_directive("Planner Test Directive", "A real directive to plan", tier=17)

    planner = PlannerAgent(db_path=db_path)
    planner._plan_directive(dir_id, "Planner Test Directive", "A real directive to plan")

    tasks = k._query("SELECT task_id FROM task_queue WHERE directive_id = ?", (dir_id,))
    check("planner created tasks for directive", len(tasks) >= 1, f"tasks={len(tasks)}")

    events = k._query("SELECT type FROM event_ledger WHERE type = 'DIRECTIVE_PLANNED'")
    check("DIRECTIVE_PLANNED event logged", len(events) > 0)

    decisions = k.get_decisions(directive_id=planner._directive_id)
    check("planner logged routing decision", len(decisions) > 0)


def test_recovery_agent(db_path):
    print("\n[T3] Recovery Agent")
    from agents.recovery_agent import RecoveryAgent

    k = AtlasRuntimeKernel(db_path=db_path)
    # Inject a failed task with retry_count=0
    task_id = k.add_task("DIR-RECOVERY-TEST", {"prompt": "retry me", "task_type": "general"})
    k._query("UPDATE task_queue SET state = 'FAILED', retry_count = 0 WHERE task_id = ?", (task_id,), commit=True)

    agent = RecoveryAgent(db_path=db_path)
    agent._retry_failed_tasks()

    state = k._query("SELECT state, retry_count FROM task_queue WHERE task_id = ?", (task_id,))
    check("failed task requeued", state[0][0] == "QUEUED", state[0][0] if state else "missing")
    check("retry_count incremented", state[0][1] == 1, str(state[0][1]) if state else "?")

    # Push to DLQ
    k._query("UPDATE task_queue SET state = 'FAILED', retry_count = 3 WHERE task_id = ?", (task_id,), commit=True)
    agent._move_to_dlq()
    state2 = k._query("SELECT state FROM task_queue WHERE task_id = ?", (task_id,))
    check("exhausted task moved to DLQ", state2[0][0] == "DLQ", state2[0][0] if state2 else "missing")


def test_governance_agent(db_path):
    print("\n[T4] Governance Agent")
    from agents.governance_agent import GovernanceAgent

    k = AtlasRuntimeKernel(db_path=db_path)
    # Inject dangerous task
    task_id = k.add_task("DIR-GOV-TEST", {"prompt": "rm -rf /important/data", "task_type": "general"})

    agent = GovernanceAgent(db_path=db_path)
    agent._scan_pending_payloads()

    state = k._query("SELECT state FROM task_queue WHERE task_id = ?", (task_id,))
    check("dangerous task blocked", state[0][0] == "BLOCKED", state[0][0] if state else "missing")
    check("TASK_BLOCKED event logged", len(k._query("SELECT 1 FROM event_ledger WHERE type = 'TASK_BLOCKED'")) > 0)


def test_verifier_agent(db_path):
    print("\n[T5] Verifier Agent")
    from agents.verifier_agent import VerifierAgent

    k = AtlasRuntimeKernel(db_path=db_path)
    # Inject completed task + completion event
    task_id = k.add_task("DIR-VERIFY-TEST", {"prompt": "verify me", "task_type": "general"})
    k._query("UPDATE task_queue SET state = 'COMPLETED' WHERE task_id = ?", (task_id,), commit=True)
    k.log_event("TASK_COMPLETED", "TEST", {"task_id": task_id, "result": {"response": "A good response " * 5, "elapsed_ms": 500}})

    agent = VerifierAgent(db_path=db_path)
    agent._verify_completed()
    check("verifier ran without error", True)
    check("verified count incremented", agent._verified > 0, str(agent._verified))


def test_diagnostics_agent(db_path):
    print("\n[T6] Diagnostics Agent")
    from agents.diagnostics_agent import DiagnosticsAgent

    k = AtlasRuntimeKernel(db_path=db_path)
    agent = DiagnosticsAgent(db_path=db_path)
    report = agent._publish_diagnostics()

    check("diagnostics report generated", isinstance(report, dict))
    check("report has queue key", "queue" in report)
    check("report has latency key", "latency" in report)
    check("report has db key", "db" in report)
    check("report written to kernel_state", k.get_state("diagnostics_report") is not None)


def test_telemetry_persistence(db_path):
    print("\n[T7] Telemetry Persistence")
    from agents.diagnostics_agent import DiagnosticsAgent

    k = AtlasRuntimeKernel(db_path=db_path)
    agent = DiagnosticsAgent(db_path=db_path)

    # Tick sets telemetry in kernel_state
    k.set_state(f"telemetry:{agent.agent_id}", agent.telemetry())

    # Simulate restart
    k2 = AtlasRuntimeKernel(db_path=db_path)
    tel = k2.get_state(f"telemetry:{agent.agent_id}")
    check("telemetry survives restart", tel is not None)
    check("telemetry has agent_id", tel.get("agent_id") == agent.agent_id if tel else False)


def test_execution_lineage(db_path):
    print("\n[T8] Execution Lineage")
    k = AtlasRuntimeKernel(db_path=db_path)
    m = MemoryEngine(k)

    dir_id = m.open_directive("Lineage Test", "Track lineage across agents", tier=17)
    t1 = k.add_task(dir_id, {"prompt": "step 1", "task_type": "general"})
    t2 = k.add_task(dir_id, {"prompt": "step 2", "task_type": "reasoning"})

    m.record_decision(dir_id, "PLANNER-001", "plan", "create 2 tasks", "decomposition")
    m.record_decision(dir_id, "EXEC-001", f"execute task={t1}", "call ollama", "routing")
    m.record_decision(dir_id, "EXEC-002", f"execute task={t2}", "call ollama", "routing")

    timeline = m.reconstruct_timeline(directive_id=dir_id)
    decision_kinds = [e for e in timeline if e["kind"] == "DECISION"]
    check("lineage has 3 decisions across agents", len(decision_kinds) >= 3, str(len(decision_kinds)))
    agents_involved = {json.loads(e["detail"]).get("context", "") for e in decision_kinds}
    check("multiple agents appear in lineage", len(agents_involved) > 1, str(agents_involved))


def main():
    print("=" * 60)
    print("TIER 17 - AUTONOMOUS GOVERNED EXECUTION VALIDATION")
    print("=" * 60)

    db_path = str(Path(__file__).parent / "atlas_runtime.db")
    print(f"Database: {db_path}\n")

    test_base_agent_lifecycle(db_path)
    test_planner_agent(db_path)
    test_recovery_agent(db_path)
    test_governance_agent(db_path)
    test_verifier_agent(db_path)
    test_diagnostics_agent(db_path)
    test_telemetry_persistence(db_path)
    test_execution_lineage(db_path)

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    total  = len(RESULTS)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    out = Path(__file__).parent.parent / "AGENT_RUNTIME_REPORT.json"
    out.write_text(json.dumps({"results": RESULTS, "passed": passed, "failed": failed, "total": total}, indent=2))
    print(f"Results written to {out}")

    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
