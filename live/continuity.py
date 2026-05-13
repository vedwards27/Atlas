"""
Atlas Continuity Preservation Engine — Tier 21
Multi-generation checkpointing, operational replay, temporal reconstruction,
institutional inheritance, and continuity branching logic.

Key concepts:
  Branch  — a named lineage of snapshots (like a git branch)
  Generation — how many checkpoints deep from the genesis branch
  Replay  — reconstructing what state looked like at a prior point in time
  Temporal reconstruction — walking events forward from a known snapshot
"""
import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

from kernel import AtlasRuntimeKernel
from memory import MemoryEngine


class ContinuityEngine:
    def __init__(self, kernel: AtlasRuntimeKernel, memory: MemoryEngine):
        self.kernel = kernel
        self.memory = memory
        self._genesis_branch_id: str | None = self._ensure_genesis()

    # ── Genesis branch ────────────────────────────────────────────────────────

    def _ensure_genesis(self) -> str:
        """Ensure there is always a 'main' branch as the root of all continuity."""
        rows = self.kernel._query(
            "SELECT branch_id FROM continuity_branches WHERE label='main' AND active=1 LIMIT 1"
        )
        if rows:
            return rows[0][0]
        # Create genesis snapshot + branch
        snap_id, _ = self.kernel.create_snapshot(scope="genesis")
        branch_id = self.kernel.create_branch(
            snapshot_id=snap_id,
            label="main",
            parent_branch_id=None,
            generation=0,
        )
        self.kernel.log_event("CONTINUITY_GENESIS", "CONTINUITY_ENGINE",
                              {"branch_id": branch_id, "snapshot_id": snap_id})
        return branch_id

    # ── Multi-generation checkpointing ────────────────────────────────────────

    def checkpoint(self, label: str = "auto", parent_branch_id: str = None) -> dict:
        """
        Take a snapshot and record it as a new generation on a branch.
        If parent_branch_id is None, appends to the main branch lineage.
        """
        parent_id = parent_branch_id or self._genesis_branch_id
        parent_row = self.kernel._query(
            "SELECT generation FROM continuity_branches WHERE branch_id=?", (parent_id,)
        )
        generation = (parent_row[0][0] + 1) if parent_row else 0

        snap_id, snap_data = self.kernel.create_snapshot(scope=f"continuity:{label}")
        branch_id = self.kernel.create_branch(
            snapshot_id=snap_id,
            label=label,
            parent_branch_id=parent_id,
            generation=generation,
        )
        self.kernel.log_event("CONTINUITY_CHECKPOINT", "CONTINUITY_ENGINE", {
            "branch_id": branch_id,
            "snapshot_id": snap_id,
            "generation": generation,
            "label": label,
        })
        return {
            "branch_id": branch_id,
            "snapshot_id": snap_id,
            "generation": generation,
            "label": label,
            "created_at": datetime.now().isoformat(),
            "summary": snap_data,
        }

    def get_branch_tree(self) -> list[dict]:
        """Return the full branch tree for display."""
        rows = self.kernel.get_branches(active_only=False)
        return [
            {"branch_id": r[0], "parent_branch_id": r[1], "created_at": r[2],
             "snapshot_id": r[3], "label": r[4], "generation": r[5]}
            for r in rows
        ]

    def get_lineage_chain(self, branch_id: str) -> list[dict]:
        """Walk from branch_id up to genesis — full ancestor chain."""
        chain = []
        current = branch_id
        visited = set()
        while current and current not in visited:
            visited.add(current)
            row = self.kernel._query(
                "SELECT branch_id, parent_branch_id, created_at, snapshot_id, label, generation FROM continuity_branches WHERE branch_id=?",
                (current,)
            )
            if not row:
                break
            r = row[0]
            chain.append({"branch_id": r[0], "parent_branch_id": r[1], "created_at": r[2],
                           "snapshot_id": r[3], "label": r[4], "generation": r[5]})
            current = r[1]  # move to parent
        chain.reverse()
        return chain

    # ── Replay simulator ──────────────────────────────────────────────────────

    def replay_from_snapshot(self, snapshot_id: str, until_timestamp: str = None) -> dict:
        """
        Reconstruct operational state at a point in time:
        1. Load the snapshot as baseline
        2. Walk events forward from snapshot.event_id_from up to until_timestamp
        3. Return the reconstructed state

        This is read-only — it does not mutate the live DB.
        """
        snap_row = self.kernel._query(
            "SELECT created_at, scope, event_id_from, event_id_to, compressed_data FROM memory_snapshots WHERE snapshot_id=?",
            (snapshot_id,)
        )
        if not snap_row:
            return {"error": f"snapshot {snapshot_id} not found"}

        snap_ts, scope, id_from, id_to, compressed_raw = snap_row[0]
        baseline = json.loads(compressed_raw)

        # Walk events from snapshot forward
        if until_timestamp:
            events = self.kernel._query(
                "SELECT event_id, timestamp, type, source, payload FROM event_ledger WHERE event_id >= ? AND timestamp <= ? ORDER BY event_id ASC",
                (id_from or 0, until_timestamp)
            )
        else:
            events = self.kernel._query(
                "SELECT event_id, timestamp, type, source, payload FROM event_ledger WHERE event_id >= ? ORDER BY event_id ASC",
                (id_from or 0,)
            )

        # Reconstruct state by replaying events
        reconstructed_tasks = {}
        reconstructed_workers = {}
        for ev_id, ev_ts, ev_type, ev_source, ev_payload_raw in events:
            try:
                payload = json.loads(ev_payload_raw)
            except Exception:
                payload = {}

            if ev_type == "TASK_CREATED":
                tid = payload.get("task_id")
                if tid:
                    reconstructed_tasks[tid] = {"state": "QUEUED", "created_at": ev_ts}
            elif ev_type == "TASK_ASSIGNED":
                tid = payload.get("task_id")
                if tid and tid in reconstructed_tasks:
                    reconstructed_tasks[tid]["state"] = "RUNNING"
                    reconstructed_tasks[tid]["worker_id"] = payload.get("worker_id")
            elif ev_type == "TASK_COMPLETED":
                tid = payload.get("task_id")
                if tid and tid in reconstructed_tasks:
                    reconstructed_tasks[tid]["state"] = "COMPLETED"
                    reconstructed_tasks[tid]["completed_at"] = ev_ts
            elif ev_type == "TASK_FAILED":
                tid = payload.get("task_id")
                if tid and tid in reconstructed_tasks:
                    reconstructed_tasks[tid]["state"] = "FAILED"
            elif ev_type == "WORKER_REGISTERED":
                wid = payload.get("worker_id")
                if wid:
                    reconstructed_workers[wid] = {"state": "ONLINE", "registered_at": ev_ts, "role": payload.get("role")}
            elif ev_type in ("AGENT_STOP", "DISPATCHER_STOP"):
                wid = ev_source
                if wid in reconstructed_workers:
                    reconstructed_workers[wid]["state"] = "OFFLINE"

        return {
            "snapshot_id": snapshot_id,
            "snapshot_timestamp": snap_ts,
            "replay_until": until_timestamp or "now",
            "events_replayed": len(events),
            "baseline_summary": {
                "total_events_at_snap": baseline.get("total_events"),
                "directives_at_snap": len(baseline.get("directives", [])),
                "worker_count_at_snap": baseline.get("worker_count"),
            },
            "reconstructed_state": {
                "tasks": reconstructed_tasks,
                "workers": reconstructed_workers,
                "task_count": len(reconstructed_tasks),
                "worker_count": len(reconstructed_workers),
            },
        }

    # ── Temporal reconstruction ───────────────────────────────────────────────

    def reconstruct_at(self, target_timestamp: str) -> dict:
        """
        Find the best snapshot before target_timestamp, then replay forward.
        This is the primary entry point for 'what did Atlas look like at T?'
        """
        # Find best snapshot at or before target_timestamp
        snap_row = self.kernel._query(
            "SELECT snapshot_id, created_at FROM memory_snapshots WHERE created_at <= ? ORDER BY created_at DESC LIMIT 1",
            (target_timestamp,)
        )
        if not snap_row:
            return {"error": "no snapshot exists before target_timestamp", "target": target_timestamp}

        snap_id, snap_ts = snap_row[0]
        result = self.replay_from_snapshot(snap_id, until_timestamp=target_timestamp)
        result["reconstruction_method"] = "snapshot_plus_event_replay"
        result["target_timestamp"] = target_timestamp
        return result

    # ── Institutional inheritance ─────────────────────────────────────────────

    def export_institutional_packet(self) -> dict:
        """
        Produce a human-readable continuity packet for institutional inheritance.
        Contains: current operational state, active directives, branch tree,
        governance policies, recent decisions, recovery instructions.
        """
        summary   = self.memory.get_operational_summary()
        branches  = self.get_branch_tree()
        decisions = self.memory.get_decision_history(limit=20)
        policies  = self.kernel.get_policies()

        active_dirs = summary.get("active_directives", [])

        packet = {
            "generated_at": datetime.now().isoformat(),
            "version": "1.0",
            "operational_summary": {
                "events": summary["events"],
                "decisions": summary["decisions"],
                "directives": summary["directives"],
                "snapshots": summary["snapshots"],
            },
            "active_directives": active_dirs,
            "continuity_branches": branches,
            "recent_decisions": decisions[:10],
            "governance_policies": [
                {"policy_id": r[0], "tier": r[1], "category": r[2], "name": r[3],
                 "authority_level": r[6], "created_at": r[8]}
                for r in policies
            ],
            "recovery_instructions": {
                "step_1": "Start server: python live/server.py",
                "step_2": "Start agents: python live/orchestrator.py",
                "step_3": "Verify: GET /api/survivability/score",
                "step_4": "Check observer: GET /api/observer/report",
                "step_5": "Restore queue if needed: POST /api/survivability/restore-queue",
            },
        }

        self.kernel.set_state("institutional_packet", packet)
        self.kernel.log_event("INSTITUTIONAL_PACKET_GENERATED", "CONTINUITY_ENGINE",
                              {"branch_count": len(branches), "directive_count": len(active_dirs)})
        return packet

    # ── Operational replay simulator ──────────────────────────────────────────

    def simulate_replay(self, from_event_id: int, to_event_id: int) -> dict:
        """
        Walk events in [from_event_id, to_event_id] and simulate what would have
        happened if Atlas processed them in order. Returns a step-by-step log.
        """
        events = self.kernel._query(
            "SELECT event_id, timestamp, type, source, payload FROM event_ledger WHERE event_id BETWEEN ? AND ? ORDER BY event_id ASC",
            (from_event_id, to_event_id)
        )
        steps = []
        for ev_id, ev_ts, ev_type, ev_source, ev_payload_raw in events:
            try:
                payload = json.loads(ev_payload_raw)
            except Exception:
                payload = {}
            steps.append({
                "event_id": ev_id,
                "timestamp": ev_ts,
                "type": ev_type,
                "source": ev_source,
                "simulated_action": self._infer_action(ev_type, payload),
            })

        return {
            "from_event_id": from_event_id,
            "to_event_id": to_event_id,
            "steps": steps,
            "step_count": len(steps),
            "unique_event_types": list({s["type"] for s in steps}),
        }

    def _infer_action(self, event_type: str, payload: dict) -> str:
        mapping = {
            "TASK_CREATED":     "queue task {task_id}",
            "TASK_ASSIGNED":    "assign {task_id} to {worker_id}",
            "TASK_COMPLETED":   "mark {task_id} COMPLETED",
            "TASK_FAILED":      "mark {task_id} FAILED",
            "WORKER_REGISTERED": "register worker {worker_id} as {role}",
            "DIRECTIVE_REGISTERED": "open directive {directive_id}",
            "DIRECTIVE_COMPLETED":  "close directive {directive_id}",
            "MEMORY_SNAPSHOT":  "take snapshot {snapshot_id}",
            "GOVERNANCE_VIOLATION": "log violation {violation_id}",
        }
        template = mapping.get(event_type, f"process {event_type}")
        try:
            return template.format(**payload)
        except Exception:
            return template
