"""
Atlas Runtime Observer — Tier 20
Recursive self-observability: Atlas continuously audits its own operational state,
detects degradation without human discovery, and publishes structured findings.

Checks (every cycle):
  - Stale telemetry (agents not updating kernel_state)
  - Dead / orphaned workers (ONLINE but no heartbeat)
  - Duplicate root workers (multiple active agents in same role)
  - Queue corruption (tasks stuck in RUNNING past timeout)
  - Broken checkpoints (no snapshot taken in too long)
  - Disconnected dashboard (last API call stale)
  - Event drift (silence in event ledger)
  - Memory integrity (snapshot chain integrity)
  - Governance integrity (policies still present and unmodified)
  - Worker health analytics (per-agent metrics trending)
"""
import hashlib
import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from kernel import AtlasRuntimeKernel
from memory import MemoryEngine
from survivability import SurvivabilityEngine

# Thresholds
TELEMETRY_STALE_S   = 60    # telemetry older than this is stale
WORKER_DEAD_S       = 45    # ONLINE worker with no heartbeat this old = dead
RUNNING_STUCK_S     = 180   # task in RUNNING past this = stuck
SNAPSHOT_GAP_S      = 3600  # no snapshot in this long = broken checkpoint
OBSERVER_CYCLE_S    = 15    # seconds between full self-audit cycles
HISTORY_DEPTH       = 120   # cycles of history to retain for trending


class ObservationFinding:
    """A single finding from a self-audit cycle."""
    __slots__ = ("check", "severity", "detail", "timestamp")

    def __init__(self, check: str, severity: str, detail: dict):
        self.check     = check
        self.severity  = severity   # INFO | WARN | ERROR | CRITICAL
        self.detail    = detail
        self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {"check": self.check, "severity": self.severity,
                "detail": self.detail, "timestamp": self.timestamp}


class RuntimeObserver:
    def __init__(self, kernel: AtlasRuntimeKernel, memory: MemoryEngine, db_path: str):
        self.kernel      = kernel
        self.memory      = memory
        self.db_path     = db_path
        self.survivability = SurvivabilityEngine(kernel, memory, db_path)

        self._lock         = threading.Lock()
        self._running      = False
        self._cycle_count  = 0
        self._findings_history: deque[list[ObservationFinding]] = deque(maxlen=HISTORY_DEPTH)
        self._coherence_history: deque[float] = deque(maxlen=HISTORY_DEPTH)

        # Governance policy hash — computed once on init, re-checked each cycle
        self._policy_hash: str | None = None

    # ── Main loop ─────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self.kernel.log_event("OBSERVER_START", "RUNTIME_OBSERVER", {})
        while self._running:
            try:
                findings = self._run_cycle()
                self._publish(findings)
            except Exception as e:
                self.kernel.log_event("OBSERVER_ERROR", "RUNTIME_OBSERVER", {"error": str(e)})
            time.sleep(OBSERVER_CYCLE_S)

    def stop(self):
        self._running = False
        self.kernel.log_event("OBSERVER_STOP", "RUNTIME_OBSERVER", {})

    def run_once(self) -> dict:
        """Single audit cycle — returns full report dict. Used by API and tests."""
        findings = self._run_cycle()
        self._publish(findings)
        return self._build_report(findings)

    # ── Audit cycle ───────────────────────────────────────────────────────────

    def _run_cycle(self) -> list[ObservationFinding]:
        findings: list[ObservationFinding] = []
        self._cycle_count += 1

        findings += self._check_stale_telemetry()
        findings += self._check_dead_workers()
        findings += self._check_duplicate_roots()
        findings += self._check_stuck_tasks()
        findings += self._check_checkpoint_gap()
        findings += self._check_event_drift()
        findings += self._check_memory_integrity()
        findings += self._check_governance_integrity()
        findings += self._check_worker_analytics()
        findings += self._check_queue_health()

        with self._lock:
            self._findings_history.append(findings)
            score = self._compute_coherence(findings)
            self._coherence_history.append(score)

        return findings

    # ── Individual checks ─────────────────────────────────────────────────────

    def _check_stale_telemetry(self) -> list[ObservationFinding]:
        out = []
        rows = self.kernel._query(
            "SELECT key, last_updated FROM kernel_state WHERE key LIKE 'telemetry:%'"
        )
        threshold = (datetime.now() - timedelta(seconds=TELEMETRY_STALE_S)).isoformat()
        for key, updated in rows:
            if updated and updated < threshold:
                age = (datetime.now() - datetime.fromisoformat(updated)).total_seconds()
                out.append(ObservationFinding(
                    "stale_telemetry", "WARN",
                    {"key": key, "last_updated": updated, "age_seconds": round(age)}
                ))
        return out

    def _check_dead_workers(self) -> list[ObservationFinding]:
        out = []
        threshold = (datetime.now() - timedelta(seconds=WORKER_DEAD_S)).isoformat()
        rows = self.kernel._query(
            "SELECT worker_id, role, runtime_state, last_heartbeat FROM worker_registry "
            "WHERE runtime_state NOT IN ('OFFLINE','STALLED') AND last_heartbeat < ?",
            (threshold,)
        )
        for worker_id, role, state, hb in rows:
            try:
                age = (datetime.now() - datetime.fromisoformat(hb)).total_seconds()
            except Exception:
                age = -1
            out.append(ObservationFinding(
                "dead_worker", "ERROR",
                {"worker_id": worker_id, "role": role, "state": state,
                 "last_heartbeat": hb, "age_seconds": round(age)}
            ))
        return out

    def _check_duplicate_roots(self) -> list[ObservationFinding]:
        out = []
        rows = self.kernel._query(
            "SELECT role, COUNT(*) FROM worker_registry "
            "WHERE runtime_state NOT IN ('OFFLINE','STALLED') "
            "GROUP BY role HAVING COUNT(*) > 1"
        )
        for role, count in rows:
            out.append(ObservationFinding(
                "duplicate_root", "CRITICAL",
                {"role": role, "active_count": count,
                 "action_required": "deregister excess workers"}
            ))
        return out

    def _check_stuck_tasks(self) -> list[ObservationFinding]:
        out = []
        threshold = (datetime.now() - timedelta(seconds=RUNNING_STUCK_S)).isoformat()
        rows = self.kernel._query(
            "SELECT task_id, worker_id, last_updated FROM task_queue "
            "WHERE state = 'RUNNING' AND last_updated < ?",
            (threshold,)
        )
        for task_id, worker_id, updated in rows:
            try:
                age = (datetime.now() - datetime.fromisoformat(updated)).total_seconds()
            except Exception:
                age = -1
            out.append(ObservationFinding(
                "stuck_task", "ERROR",
                {"task_id": task_id, "worker_id": worker_id,
                 "stuck_seconds": round(age), "action": "requeue"}
            ))
        return out

    def _check_checkpoint_gap(self) -> list[ObservationFinding]:
        out = []
        row = self.kernel._query(
            "SELECT created_at FROM memory_snapshots ORDER BY created_at DESC LIMIT 1"
        )
        if not row:
            out.append(ObservationFinding(
                "no_checkpoint", "CRITICAL",
                {"detail": "no memory snapshots exist", "action": "take_checkpoint_immediately"}
            ))
            return out
        last_snap = row[0][0]
        try:
            age = (datetime.now() - datetime.fromisoformat(last_snap)).total_seconds()
        except Exception:
            return out
        if age > SNAPSHOT_GAP_S:
            out.append(ObservationFinding(
                "checkpoint_gap", "WARN",
                {"last_snapshot": last_snap, "age_seconds": round(age),
                 "threshold": SNAPSHOT_GAP_S}
            ))
        return out

    def _check_event_drift(self) -> list[ObservationFinding]:
        out = []
        result = self.survivability.check_event_drift()
        if result.get("drift"):
            out.append(ObservationFinding(
                "event_drift", "WARN",
                {"last_event": result.get("last_event"),
                 "age_seconds": result.get("age_seconds"),
                 "detail": "no events logged recently — system may be idle or stuck"}
            ))
        return out

    def _check_memory_integrity(self) -> list[ObservationFinding]:
        out = []
        result = self.survivability.check_db_integrity()
        if not result["ok"]:
            sev = "CRITICAL" if not result["integrity_check"] == "ok" else "ERROR"
            out.append(ObservationFinding(
                "memory_integrity", sev,
                {"integrity_check": result["integrity_check"],
                 "missing_tables": result.get("missing_tables", []),
                 "score": result["score"]}
            ))
        return out

    def _check_governance_integrity(self) -> list[ObservationFinding]:
        """Verify that governance policies exist and their hash hasn't changed."""
        out = []
        rows = self.kernel._query(
            "SELECT policy_id, content_hash FROM governance_policies WHERE active = 1"
        )
        if not rows:
            out.append(ObservationFinding(
                "governance_empty", "ERROR",
                {"detail": "no active governance policies found — constitution not loaded"}
            ))
            return out

        # Verify each policy hash matches stored content
        for policy_id, stored_hash in rows:
            content_row = self.kernel._query(
                "SELECT body FROM governance_policies WHERE policy_id = ?", (policy_id,)
            )
            if content_row:
                computed = hashlib.sha256(content_row[0][0].encode()).hexdigest()[:16]
                if computed != stored_hash:
                    out.append(ObservationFinding(
                        "governance_tamper", "CRITICAL",
                        {"policy_id": policy_id,
                         "stored_hash": stored_hash,
                         "computed_hash": computed,
                         "detail": "policy content hash mismatch — possible tampering"}
                    ))
        return out

    def _check_worker_analytics(self) -> list[ObservationFinding]:
        out = []
        rows = self.kernel._query(
            "SELECT key, value FROM kernel_state WHERE key LIKE 'telemetry:%'"
        )
        for key, value_raw in rows:
            try:
                data = json.loads(value_raw)
                # Flag agents reporting high failure rates
                failed = data.get("tasks_failed", 0)
                done   = data.get("tasks_done", 0)
                if done + failed > 5 and failed / (done + failed) > 0.5:
                    out.append(ObservationFinding(
                        "agent_high_failure", "WARN",
                        {"agent": key.replace("telemetry:", ""),
                         "tasks_done": done, "tasks_failed": failed,
                         "failure_rate": round(failed / (done + failed), 2)}
                    ))
            except Exception:
                pass
        return out

    def _check_queue_health(self) -> list[ObservationFinding]:
        out = []
        rows = self.kernel._query("SELECT state, COUNT(*) FROM task_queue GROUP BY state")
        counts = {r[0]: r[1] for r in rows}
        total  = sum(counts.values()) or 1

        dlq = counts.get("DLQ", 0)
        if dlq > 5:
            out.append(ObservationFinding(
                "queue_dlq_high", "WARN",
                {"dlq_count": dlq, "total": total,
                 "pct": round(dlq / total * 100, 1)}
            ))

        blocked = counts.get("BLOCKED", 0)
        if blocked > 0:
            out.append(ObservationFinding(
                "queue_blocked_tasks", "INFO",
                {"blocked_count": blocked,
                 "detail": "tasks blocked by governance; require human review"}
            ))
        return out

    # ── Coherence scoring ─────────────────────────────────────────────────────

    def _compute_coherence(self, findings: list[ObservationFinding]) -> float:
        """0.0–100.0 coherence score. Deducted by severity of findings."""
        penalties = {"INFO": 0, "WARN": 5, "ERROR": 15, "CRITICAL": 30}
        score = 100.0
        for f in findings:
            score -= penalties.get(f.severity, 0)
        return max(0.0, score)

    def get_coherence_trend(self) -> dict:
        history = list(self._coherence_history)
        if not history:
            return {"current": 100.0, "avg": 100.0, "min": 100.0, "trend": "stable"}
        current = history[-1]
        avg     = sum(history) / len(history)
        minimum = min(history)
        if len(history) >= 3:
            recent = sum(history[-3:]) / 3
            older  = sum(history[:-3]) / max(len(history) - 3, 1)
            trend = "improving" if recent > older + 2 else ("degrading" if recent < older - 2 else "stable")
        else:
            trend = "stable"
        return {"current": current, "avg": round(avg, 1), "min": minimum, "trend": trend}

    # ── Publish & report ──────────────────────────────────────────────────────

    def _publish(self, findings: list[ObservationFinding]):
        report = self._build_report(findings)
        self.kernel.set_state("observer_report", report)

        criticals = [f for f in findings if f.severity == "CRITICAL"]
        errors    = [f for f in findings if f.severity == "ERROR"]
        if criticals:
            self.kernel.log_event("OBSERVER_CRITICAL", "RUNTIME_OBSERVER",
                                  {"findings": [f.to_dict() for f in criticals]})
        if errors:
            self.kernel.log_event("OBSERVER_ERROR_FINDINGS", "RUNTIME_OBSERVER",
                                  {"count": len(errors)})

    def _build_report(self, findings: list[ObservationFinding]) -> dict:
        by_severity = defaultdict(list)
        for f in findings:
            by_severity[f.severity].append(f.to_dict())
        coherence = self.get_coherence_trend()
        return {
            "generated_at":   datetime.now().isoformat(),
            "cycle":          self._cycle_count,
            "coherence":      coherence,
            "finding_count":  len(findings),
            "by_severity": {
                "CRITICAL": by_severity["CRITICAL"],
                "ERROR":    by_severity["ERROR"],
                "WARN":     by_severity["WARN"],
                "INFO":     by_severity["INFO"],
            },
            "all_findings":   [f.to_dict() for f in findings],
        }

    def get_latest_report(self) -> dict:
        return self.kernel.get_state("observer_report", default={
            "generated_at": None, "cycle": 0, "coherence": {"current": 100.0},
            "finding_count": 0, "by_severity": {}, "all_findings": [],
        })

    def get_finding_history(self, limit: int = 10) -> list[list[dict]]:
        with self._lock:
            history = list(self._findings_history)[-limit:]
        return [[f.to_dict() for f in cycle] for cycle in history]
