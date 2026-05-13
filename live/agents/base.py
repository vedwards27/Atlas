"""
Base agent class — all Atlas agents inherit from this.
Provides: heartbeat, checkpoint, event logging, state recovery, telemetry.
"""
import threading
import time
import json
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
from kernel import AtlasRuntimeKernel
from memory import MemoryEngine

HEARTBEAT_INTERVAL = 10   # seconds
CHECKPOINT_INTERVAL = 300 # seconds (5 min)


class BaseAgent:
    def __init__(self, agent_id: str, role: str, scope: str, boundary: str, db_path: str):
        self.agent_id = agent_id
        self.role = role
        self.scope = scope
        self.boundary = boundary
        self.db_path = db_path

        self.kernel = AtlasRuntimeKernel(db_path=db_path)
        self.memory = MemoryEngine(self.kernel)

        self._running = False
        self._last_heartbeat = 0.0
        self._last_checkpoint = 0.0
        self._directive_id: str | None = None

        self._register()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def _register(self):
        self.kernel.register_worker(self.agent_id, self.role, "internal", self.scope, self.boundary)
        self._directive_id = self.memory.open_directive(
            name=f"{self.role} Session",
            description=f"{self.agent_id} started at {datetime.now().isoformat()}",
            tier=17,
        )
        self._log(f"AGENT_START", {"role": self.role, "directive_id": self._directive_id})

    def start(self):
        self._running = True
        self._last_heartbeat = time.time()
        self._last_checkpoint = time.time()
        try:
            self.run()
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def stop(self):
        self._running = False

    def _shutdown(self):
        self.kernel.set_worker_state(self.agent_id, "OFFLINE")
        if self._directive_id:
            self.memory.close_directive(self._directive_id, outcome="CLEAN_SHUTDOWN")
        self._log("AGENT_STOP", {"role": self.role})

    # ── Heartbeat + checkpoint ─────────────────────────────────────────────────

    def _tick(self):
        """Call at the top of every loop iteration."""
        now = time.time()
        if now - self._last_heartbeat >= HEARTBEAT_INTERVAL:
            self.kernel.update_worker_heartbeat(self.agent_id)
            self.kernel.set_state(f"telemetry:{self.agent_id}", self.telemetry())
            self._last_heartbeat = now
        if now - self._last_checkpoint >= CHECKPOINT_INTERVAL:
            self.memory.checkpoint(scope=f"agent:{self.agent_id}")
            self._last_checkpoint = now

    # ── Telemetry ──────────────────────────────────────────────────────────────

    def telemetry(self) -> dict:
        """Override in subclasses to add agent-specific metrics."""
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "running": self._running,
            "timestamp": datetime.now().isoformat(),
        }

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, event_type: str, payload: dict):
        self.kernel.log_event(event_type, self.agent_id, payload, trace_id=self._directive_id)

    def _decide(self, context: str, decision: str, rationale: str) -> str:
        return self.memory.record_decision(
            directive_id=self._directive_id or "UNKNOWN",
            worker_id=self.agent_id,
            context=context,
            decision=decision,
            rationale=rationale,
        )

    # ── Abstract ──────────────────────────────────────────────────────────────

    def run(self):
        raise NotImplementedError
