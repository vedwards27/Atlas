"""
Atlas Memory Engine — Tier 16
Persistent institutional continuity memory: directive lineage, decision history,
FTS retrieval, compression snapshots, and startup hydration.
"""
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from kernel import AtlasRuntimeKernel

SNAPSHOT_INTERVAL_S = 3600   # auto-snapshot every hour
COMPRESSION_AGE_DAYS = 7     # events older than this are candidates for compression


class MemoryEngine:
    def __init__(self, kernel: AtlasRuntimeKernel):
        self.kernel = kernel
        self._lock = threading.Lock()
        self._last_snapshot: datetime | None = None
        self._hydration_state: dict = {}
        self._hydrate()

    # ── Startup hydration ─────────────────────────────────────────────────────

    def _hydrate(self):
        """Load prior operational state into memory on startup — no manual step required."""
        snap = self.kernel.get_latest_snapshot()
        recent_events = self.kernel._query(
            'SELECT event_id, timestamp, type, source, payload, trace_id FROM event_ledger ORDER BY event_id DESC LIMIT 50'
        )
        active_directives = self.kernel.get_directives(state="ACTIVE")
        recent_decisions = self.kernel.get_decisions(limit=20)

        self._hydration_state = {
            "hydrated_at": datetime.now().isoformat(),
            "last_snapshot": snap,
            "active_directives": len(active_directives),
            "recent_event_count": len(recent_events),
            "recent_decisions": len(recent_decisions),
            "operational_since": recent_events[-1][1] if recent_events else None,
        }

        self.kernel.log_event(
            "MEMORY_HYDRATED", "MEMORY_ENGINE",
            {
                "active_directives": len(active_directives),
                "recent_events": len(recent_events),
                "last_snapshot_id": snap["snapshot_id"] if snap else None,
            }
        )

    def get_hydration_state(self) -> dict:
        return self._hydration_state

    # ── Directive lineage ─────────────────────────────────────────────────────

    def open_directive(self, name: str, description: str, tier: int = None, parent_id: str = None) -> str:
        directive_id = self.kernel.register_directive(name, description, tier=tier, parent_id=parent_id)
        return directive_id

    def close_directive(self, directive_id: str, outcome: str = "SUCCESS"):
        self.kernel.complete_directive(directive_id, outcome)

    def get_lineage(self, directive_id: str) -> list[dict]:
        rows = self.kernel._query(
            'SELECT directive_id, name, state, tier, parent_id, created_at, completed_at, outcome, lineage_json FROM directive_registry WHERE directive_id = ?',
            (directive_id,)
        )
        if not rows:
            return []
        row = rows[0]
        ancestors = json.loads(row[8])
        lineage = []
        for anc_id in ancestors:
            anc = self.kernel._query(
                'SELECT directive_id, name, state, tier, created_at, outcome FROM directive_registry WHERE directive_id = ?',
                (anc_id,)
            )
            if anc:
                a = anc[0]
                lineage.append({"directive_id": a[0], "name": a[1], "state": a[2], "tier": a[3], "created_at": a[4], "outcome": a[5]})
        lineage.append({
            "directive_id": row[0], "name": row[1], "state": row[2],
            "tier": row[3], "created_at": row[5], "outcome": row[7], "is_current": True
        })
        return lineage

    def get_all_directives(self) -> list[dict]:
        rows = self.kernel.get_directives()
        return [
            {
                "directive_id": r[0], "name": r[1], "description": r[2],
                "state": r[3], "tier": r[4], "parent_id": r[5],
                "created_at": r[6], "completed_at": r[7], "outcome": r[8],
                "lineage": json.loads(r[9]),
            }
            for r in rows
        ]

    # ── Decision history ──────────────────────────────────────────────────────

    def record_decision(self, directive_id: str, worker_id: str, context: str, decision: str, rationale: str, trace_id: str = None) -> str:
        return self.kernel.log_decision(directive_id, worker_id, context, decision, rationale, trace_id)

    def resolve_decision(self, decision_id: str, outcome: str):
        self.kernel.update_decision_outcome(decision_id, outcome)

    def get_decision_history(self, directive_id: str = None, limit: int = 50) -> list[dict]:
        rows = self.kernel.get_decisions(directive_id=directive_id, limit=limit)
        return [
            {
                "decision_id": r[0], "timestamp": r[1], "directive_id": r[2],
                "worker_id": r[3], "context": r[4], "decision": r[5],
                "rationale": r[6], "outcome": r[7], "trace_id": r[8],
            }
            for r in rows
        ]

    # ── Timeline reconstruction ───────────────────────────────────────────────

    def reconstruct_timeline(self, directive_id: str = None, limit: int = 100) -> list[dict]:
        """Merge events and decisions into a unified chronological timeline."""
        if directive_id:
            events = self.kernel._query(
                'SELECT timestamp, "EVENT" as kind, type, source, payload, trace_id FROM event_ledger WHERE trace_id = ? ORDER BY timestamp DESC LIMIT ?',
                (directive_id, limit)
            )
            decisions = self.kernel.get_decisions(directive_id=directive_id, limit=limit)
        else:
            events = self.kernel._query(
                'SELECT timestamp, "EVENT" as kind, type, source, payload, trace_id FROM event_ledger ORDER BY event_id DESC LIMIT ?',
                (limit,)
            )
            decisions = self.kernel.get_decisions(limit=limit)

        timeline = []
        for r in events:
            timeline.append({
                "timestamp": r[0], "kind": "EVENT", "type": r[2],
                "source": r[3], "detail": r[4], "trace_id": r[5],
            })
        for r in decisions:
            timeline.append({
                "timestamp": r[1], "kind": "DECISION", "type": "DECISION",
                "source": r[3], "detail": json.dumps({"context": r[4], "decision": r[5], "rationale": r[6]}),
                "trace_id": r[8],
            })
        timeline.sort(key=lambda x: x["timestamp"], reverse=True)
        return timeline[:limit]

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 20) -> dict:
        events = self.kernel.search_events(query, limit=limit)
        decisions = self.kernel.search_decisions(query, limit=limit)
        return {
            "query": query,
            "events": [
                {"event_id": r[0], "timestamp": r[1], "type": r[2], "source": r[3], "payload": r[4], "trace_id": r[5]}
                for r in events
            ],
            "decisions": [
                {"decision_id": r[0], "timestamp": r[1], "directive_id": r[2], "worker_id": r[3],
                 "context": r[4], "decision": r[5], "rationale": r[6], "outcome": r[7]}
                for r in decisions
            ],
            "total": len(events) + len(decisions),
        }

    # ── Memory compression + snapshots ────────────────────────────────────────

    def checkpoint(self, scope: str = "global") -> dict:
        with self._lock:
            snapshot_id, data = self.kernel.create_snapshot(scope=scope)
            self._last_snapshot = datetime.now()
            return {"snapshot_id": snapshot_id, **data}

    def maybe_auto_checkpoint(self):
        """Call periodically — takes a snapshot if interval has elapsed."""
        if self._last_snapshot is None or (datetime.now() - self._last_snapshot).total_seconds() > SNAPSHOT_INTERVAL_S:
            self.checkpoint(scope="auto")

    def get_snapshots(self, limit: int = 10) -> list[dict]:
        rows = self.kernel.get_snapshots(limit=limit)
        return [
            {"snapshot_id": r[0], "created_at": r[1], "scope": r[2],
             "event_id_from": r[3], "event_id_to": r[4], "decision_count": r[5]}
            for r in rows
        ]

    # ── Operational state summary ─────────────────────────────────────────────

    def get_operational_summary(self) -> dict:
        directives = self.get_all_directives()
        active = [d for d in directives if d["state"] == "ACTIVE"]
        completed = [d for d in directives if d["state"] == "COMPLETED"]
        event_count = self.kernel._query('SELECT COUNT(*) FROM event_ledger')[0][0]
        decision_count = self.kernel._query('SELECT COUNT(*) FROM decision_log')[0][0]
        snapshot_count = self.kernel._query('SELECT COUNT(*) FROM memory_snapshots')[0][0]
        latest_snap = self.kernel.get_latest_snapshot()
        return {
            "generated_at": datetime.now().isoformat(),
            "hydration": self._hydration_state,
            "directives": {"total": len(directives), "active": len(active), "completed": len(completed)},
            "events": {"total": event_count},
            "decisions": {"total": decision_count},
            "snapshots": {"total": snapshot_count, "latest": latest_snap["created_at"] if latest_snap else None},
            "active_directives": active,
        }
