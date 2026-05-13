"""
Diagnostics Agent — system health monitoring and telemetry aggregation.
Collects and publishes: agent telemetry, queue depth trends, DB health,
execution latency, and system resource usage.
"""
import time
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
from collections import deque

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.base import BaseAgent

POLL_INTERVAL    = 15    # seconds
HISTORY_WINDOW   = 60    # retain last N readings for trend analysis


class DiagnosticsAgent(BaseAgent):
    def __init__(self, db_path: str):
        super().__init__("DIAGNOSTICS-001", "diagnostics_agent", "local", "read_only", db_path)
        self._queue_history: deque = deque(maxlen=HISTORY_WINDOW)
        self._latency_history: deque = deque(maxlen=HISTORY_WINDOW)
        self._ticks = 0

    def _collect_queue_metrics(self) -> dict:
        rows = self.kernel._query("SELECT state, COUNT(*) FROM task_queue GROUP BY state")
        counts = {r[0]: r[1] for r in rows}
        self._queue_history.append({"ts": datetime.now().isoformat(), "counts": counts})
        return counts

    def _collect_agent_telemetry(self) -> list[dict]:
        rows = self.kernel._query(
            "SELECT key, value, last_updated FROM kernel_state WHERE key LIKE 'telemetry:%'"
        )
        agents = []
        for key, value_raw, updated in rows:
            try:
                agents.append({"key": key, "data": json.loads(value_raw), "updated": updated})
            except Exception:
                pass
        return agents

    def _collect_latency(self) -> dict:
        rows = self.kernel._query(
            "SELECT payload FROM event_ledger WHERE type = 'TASK_COMPLETED' ORDER BY event_id DESC LIMIT 20"
        )
        latencies = []
        for (payload_raw,) in rows:
            try:
                result = json.loads(payload_raw)
                ms = result.get("result", {}).get("elapsed_ms")
                if isinstance(ms, int):
                    latencies.append(ms)
            except Exception:
                pass
        if latencies:
            avg = sum(latencies) / len(latencies)
            p95 = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[0]
        else:
            avg, p95 = 0, 0
        summary = {"avg_ms": round(avg), "p95_ms": p95, "sample_count": len(latencies)}
        self._latency_history.append({"ts": datetime.now().isoformat(), **summary})
        return summary

    def _collect_db_health(self) -> dict:
        db_path = Path(self.db_path)
        size_bytes = db_path.stat().st_size if db_path.exists() else 0
        event_count = self.kernel._query("SELECT COUNT(*) FROM event_ledger")[0][0]
        worker_count = self.kernel._query("SELECT COUNT(*) FROM worker_registry")[0][0]
        snapshot_count = self.kernel._query("SELECT COUNT(*) FROM memory_snapshots")[0][0]
        return {
            "db_size_bytes": size_bytes,
            "event_count": event_count,
            "worker_count": worker_count,
            "snapshot_count": snapshot_count,
        }

    def _publish_diagnostics(self):
        report = {
            "generated_at": datetime.now().isoformat(),
            "tick": self._ticks,
            "queue": self._collect_queue_metrics(),
            "latency": self._collect_latency(),
            "db": self._collect_db_health(),
            "agents": self._collect_agent_telemetry(),
        }
        self.kernel.set_state("diagnostics_report", report)
        self._ticks += 1
        return report

    def run(self):
        print("[DIAGNOSTICS] Online. Publishing system health.")
        while self._running:
            self._tick()
            report = self._publish_diagnostics()
            self._log("DIAGNOSTICS_TICK", {
                "tick": report["tick"],
                "queue_total": sum(report["queue"].values()),
                "avg_latency_ms": report["latency"]["avg_ms"],
                "db_size_bytes": report["db"]["db_size_bytes"],
            })
            time.sleep(POLL_INTERVAL)

    def telemetry(self) -> dict:
        base = super().telemetry()
        base["ticks"] = self._ticks
        base["latency_samples"] = len(self._latency_history)
        return base


if __name__ == "__main__":
    db = str(Path(__file__).parent.parent / "atlas_runtime.db")
    DiagnosticsAgent(db_path=db).start()
