"""
Atlas Orchestrator — launches and supervises all Tier 17 agents.
Each agent runs in its own thread. If an agent crashes, it is restarted
after a backoff period. Ctrl-C stops all agents cleanly.
"""
import signal
import sys
import time
import threading
from pathlib import Path

DB_PATH = str(Path(__file__).parent / "atlas_runtime.db")

# Import all agents
sys.path.insert(0, str(Path(__file__).parent))
from agents.planner_agent     import PlannerAgent
from agents.execution_agent   import ExecutionAgent
from agents.verifier_agent    import VerifierAgent
from agents.recovery_agent    import RecoveryAgent
from agents.governance_agent  import GovernanceAgent
from agents.diagnostics_agent import DiagnosticsAgent

AGENT_SPECS = [
    ("planner",     lambda: PlannerAgent(db_path=DB_PATH)),
    ("execution-1", lambda: ExecutionAgent(agent_id="EXEC-001", db_path=DB_PATH)),
    ("execution-2", lambda: ExecutionAgent(agent_id="EXEC-002", db_path=DB_PATH)),
    ("verifier",    lambda: VerifierAgent(db_path=DB_PATH)),
    ("recovery",    lambda: RecoveryAgent(db_path=DB_PATH)),
    ("governance",  lambda: GovernanceAgent(db_path=DB_PATH)),
    ("diagnostics", lambda: DiagnosticsAgent(db_path=DB_PATH)),
]

_stop_event = threading.Event()


def run_agent(name: str, factory, backoff: float = 5.0):
    """Run an agent in a supervised loop — restart on crash."""
    while not _stop_event.is_set():
        try:
            agent = factory()
            print(f"[ORCHESTRATOR] Starting {name}")
            agent.start()
        except Exception as e:
            print(f"[ORCHESTRATOR] {name} crashed: {e}")
        if _stop_event.is_set():
            break
        print(f"[ORCHESTRATOR] Restarting {name} in {backoff}s")
        _stop_event.wait(timeout=backoff)
    print(f"[ORCHESTRATOR] {name} exited cleanly.")


def handle_signal(sig, frame):
    print("\n[ORCHESTRATOR] Shutdown signal — stopping all agents.")
    _stop_event.set()


def main():
    signal.signal(signal.SIGINT,  handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threads = []
    for name, factory in AGENT_SPECS:
        t = threading.Thread(target=run_agent, args=(name, factory), daemon=True, name=f"agent-{name}")
        t.start()
        threads.append(t)
        time.sleep(0.5)  # stagger starts to avoid DB write contention

    print(f"[ORCHESTRATOR] All {len(AGENT_SPECS)} agents started.")

    # Block until stop
    while not _stop_event.is_set():
        alive = sum(1 for t in threads if t.is_alive())
        print(f"[ORCHESTRATOR] Alive: {alive}/{len(threads)}")
        _stop_event.wait(timeout=30)

    # Give agents time to finish shutdown
    for t in threads:
        t.join(timeout=15)
    print("[ORCHESTRATOR] Shutdown complete.")


if __name__ == "__main__":
    main()
