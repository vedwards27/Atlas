"""
Tier 20 Validation — Recursive Self-Observability
Tests: stale telemetry detection, dead worker detection, duplicate root detection,
stuck task detection, checkpoint gap detection, event drift, memory integrity,
governance integrity, coherence scoring, and publish/report cycle.
"""
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kernel import AtlasRuntimeKernel
from memory import MemoryEngine
from observer import RuntimeObserver

RESULTS = []


def check(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append({"test": name, "status": status, "detail": detail})
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    return passed


def fresh(db_path):
    k = AtlasRuntimeKernel(db_path=db_path)
    m = MemoryEngine(k)
    o = RuntimeObserver(k, m, db_path)
    return k, m, o


def test_stale_telemetry(db_path):
    print("\n[T1] Stale Telemetry Detection")
    k, m, o = fresh(db_path)

    # Inject stale telemetry
    stale_ts = (datetime.now() - timedelta(seconds=120)).isoformat()
    k._query(
        "INSERT OR REPLACE INTO kernel_state (key, value, last_updated) VALUES ('telemetry:STALE-WORKER', '{}', ?)",
        (stale_ts,), commit=True
    )
    findings = o._check_stale_telemetry()
    stale = [f for f in findings if f.check == "stale_telemetry" and "STALE-WORKER" in f.detail.get("key", "")]
    check("stale telemetry detected", len(stale) > 0, f"findings={len(stale)}")
    check("severity is WARN", stale[0].severity == "WARN" if stale else False)


def test_dead_worker_detection(db_path):
    print("\n[T2] Dead Worker Detection")
    k, m, o = fresh(db_path)

    # Register a worker and set its heartbeat to old
    k.register_worker("DEAD-OBSERVE-001", "test_role", "sim", "local", "none")
    old_hb = (datetime.now() - timedelta(seconds=120)).isoformat()
    k._query("UPDATE worker_registry SET runtime_state='ACTIVE', last_heartbeat=? WHERE worker_id='DEAD-OBSERVE-001'", (old_hb,), commit=True)

    findings = o._check_dead_workers()
    dead = [f for f in findings if "DEAD-OBSERVE-001" in f.detail.get("worker_id", "")]
    check("dead worker detected", len(dead) > 0, f"found={len(dead)}")
    check("severity is ERROR", dead[0].severity == "ERROR" if dead else False)

    # Cleanup
    k._query("UPDATE worker_registry SET runtime_state='OFFLINE' WHERE worker_id='DEAD-OBSERVE-001'", commit=True)


def test_duplicate_root_detection(db_path):
    print("\n[T3] Duplicate Root Detection")
    k, m, o = fresh(db_path)

    k.register_worker("DUP-OBS-A", "dup_obs_role", "sim", "local", "none")
    k.register_worker("DUP-OBS-B", "dup_obs_role", "sim", "local", "none")
    k._query("UPDATE worker_registry SET runtime_state='ACTIVE' WHERE worker_id IN ('DUP-OBS-A','DUP-OBS-B')", commit=True)

    findings = o._check_duplicate_roots()
    dups = [f for f in findings if f.check == "duplicate_root" and f.detail.get("role") == "dup_obs_role"]
    check("duplicate root detected", len(dups) > 0)
    check("severity is CRITICAL", dups[0].severity == "CRITICAL" if dups else False)

    # Cleanup
    k._query("UPDATE worker_registry SET runtime_state='OFFLINE' WHERE worker_id IN ('DUP-OBS-A','DUP-OBS-B')", commit=True)


def test_stuck_task_detection(db_path):
    print("\n[T4] Stuck Task Detection")
    k, m, o = fresh(db_path)

    task_id = k.add_task("DIR-OBS-TEST", {"prompt": "stuck", "task_type": "general"})
    old_ts = (datetime.now() - timedelta(seconds=300)).isoformat()
    k._query("UPDATE task_queue SET state='RUNNING', last_updated=?, worker_id='FAKE-WORKER' WHERE task_id=?", (old_ts, task_id), commit=True)

    findings = o._check_stuck_tasks()
    stuck = [f for f in findings if f.detail.get("task_id") == task_id]
    check("stuck task detected", len(stuck) > 0)
    check("severity is ERROR", stuck[0].severity == "ERROR" if stuck else False)
    check("action suggests requeue", "requeue" in (stuck[0].detail.get("action", "") if stuck else ""))


def test_checkpoint_gap(db_path):
    print("\n[T5] Checkpoint Gap Detection")
    k, m, o = fresh(db_path)

    # Use an isolated DB to guarantee no snapshots
    import tempfile, os
    tmp = tempfile.mktemp(suffix=".db")
    k2 = AtlasRuntimeKernel(db_path=tmp)
    m2 = MemoryEngine(k2)
    o2 = RuntimeObserver(k2, m2, tmp)

    findings = o2._check_checkpoint_gap()
    no_snap = [f for f in findings if f.check == "no_checkpoint"]
    check("no checkpoint detected on fresh DB", len(no_snap) > 0)
    check("severity is CRITICAL", no_snap[0].severity == "CRITICAL" if no_snap else False)

    # Now take a snapshot — gap should clear
    m2.checkpoint(scope="test")
    findings2 = o2._check_checkpoint_gap()
    still_missing = [f for f in findings2 if f.check in ("no_checkpoint", "checkpoint_gap")]
    check("no gap after fresh checkpoint", len(still_missing) == 0, str(len(still_missing)))

    os.unlink(tmp)
    try:
        os.unlink(tmp + "-wal")
        os.unlink(tmp + "-shm")
    except Exception:
        pass


def test_memory_integrity_check(db_path):
    print("\n[T6] Memory Integrity Self-Check")
    k, m, o = fresh(db_path)

    findings = o._check_memory_integrity()
    integrity_failures = [f for f in findings if f.check == "memory_integrity"]
    check("no integrity failures on healthy DB", len(integrity_failures) == 0, str(len(integrity_failures)))


def test_governance_integrity_no_policies(db_path):
    print("\n[T7] Governance Integrity — No Policies")
    import tempfile, os
    tmp = tempfile.mktemp(suffix=".db")
    k2 = AtlasRuntimeKernel(db_path=tmp)
    m2 = MemoryEngine(k2)
    o2 = RuntimeObserver(k2, m2, tmp)

    findings = o2._check_governance_integrity()
    empty = [f for f in findings if f.check == "governance_empty"]
    check("governance_empty detected when no policies loaded", len(empty) > 0)
    check("severity is ERROR", empty[0].severity == "ERROR" if empty else False)

    os.unlink(tmp)
    try:
        os.unlink(tmp + "-wal")
        os.unlink(tmp + "-shm")
    except Exception:
        pass


def test_governance_integrity_with_policies(db_path):
    print("\n[T8] Governance Integrity — Policies Loaded and Hash-Verified")
    k, m, o = fresh(db_path)

    # Insert a policy
    k.insert_policy("TEST-POL-001", 22, "execution", "Test Policy", "No agent may erase lineage.", 100, "TEST")

    findings = o._check_governance_integrity()
    tamper = [f for f in findings if f.check == "governance_tamper"]
    empty  = [f for f in findings if f.check == "governance_empty"]
    check("no tamper detected on fresh policy", len(tamper) == 0, str(len(tamper)))
    check("no governance_empty when policy loaded", len(empty) == 0, str(len(empty)))


def test_coherence_scoring(db_path):
    print("\n[T9] Coherence Scoring")
    from observer import ObservationFinding

    k, m, o = fresh(db_path)

    # No findings → score should be 100
    score = o._compute_coherence([])
    check("empty findings = 100.0 coherence", score == 100.0, str(score))

    # One WARN → 95
    w = ObservationFinding("test", "WARN", {})
    score2 = o._compute_coherence([w])
    check("one WARN deducts 5", score2 == 95.0, str(score2))

    # One CRITICAL → 70
    c = ObservationFinding("test", "CRITICAL", {})
    score3 = o._compute_coherence([c])
    check("one CRITICAL deducts 30", score3 == 70.0, str(score3))

    # Score cannot go below 0
    many = [ObservationFinding("test", "CRITICAL", {}) for _ in range(10)]
    score4 = o._compute_coherence(many)
    check("score floors at 0.0", score4 == 0.0, str(score4))


def test_full_report_cycle(db_path):
    print("\n[T10] Full Audit Cycle and Report")
    k, m, o = fresh(db_path)

    # Load a policy so governance check passes
    k.insert_policy("CYCLE-POL-001", 22, "execution", "Cycle Test Policy", "Test body.", 100, "TEST")
    m.checkpoint(scope="test_cycle")
    k.log_event("CYCLE_TEST_EVENT", "TEST", {"note": "fresh"})

    report = o.run_once()
    check("report has generated_at", "generated_at" in report)
    check("report has coherence", "coherence" in report)
    check("report has by_severity", "by_severity" in report)
    check("report has cycle count", report.get("cycle", 0) >= 1)
    check("report written to kernel_state", k.get_state("observer_report") is not None)

    trend = o.get_coherence_trend()
    check("coherence trend has current score", "current" in trend)
    check("coherence trend has trend direction", trend.get("trend") in ("stable", "improving", "degrading"))


def test_queue_health(db_path):
    print("\n[T11] Queue Health Observation")
    k, m, o = fresh(db_path)

    # Inject DLQ tasks
    for i in range(6):
        tid = k.add_task("DIR-QH-TEST", {"prompt": f"dlq {i}", "task_type": "general"})
        k._query("UPDATE task_queue SET state='DLQ' WHERE task_id=?", (tid,), commit=True)

    findings = o._check_queue_health()
    dlq_warn = [f for f in findings if f.check == "queue_dlq_high"]
    check("high DLQ count flagged", len(dlq_warn) > 0, f"dlq_count={dlq_warn[0].detail.get('dlq_count') if dlq_warn else 0}")

    # Inject blocked task
    btid = k.add_task("DIR-QH-TEST", {"prompt": "dangerous rm -rf", "task_type": "general"})
    k._query("UPDATE task_queue SET state='BLOCKED' WHERE task_id=?", (btid,), commit=True)

    findings2 = o._check_queue_health()
    blocked_info = [f for f in findings2 if f.check == "queue_blocked_tasks"]
    check("blocked tasks reported", len(blocked_info) > 0)
    check("blocked finding is INFO severity", blocked_info[0].severity == "INFO" if blocked_info else False)


def main():
    print("=" * 60)
    print("TIER 20 - RECURSIVE SELF-OBSERVABILITY VALIDATION")
    print("=" * 60)

    db_path = str(Path(__file__).parent / "atlas_runtime.db")
    print(f"Database: {db_path}\n")

    test_stale_telemetry(db_path)
    test_dead_worker_detection(db_path)
    test_duplicate_root_detection(db_path)
    test_stuck_task_detection(db_path)
    test_checkpoint_gap(db_path)
    test_memory_integrity_check(db_path)
    test_governance_integrity_no_policies(db_path)
    test_governance_integrity_with_policies(db_path)
    test_coherence_scoring(db_path)
    test_full_report_cycle(db_path)
    test_queue_health(db_path)

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    total  = len(RESULTS)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    out = Path(__file__).parent.parent / "SELF_OBSERVABILITY_REPORT.json"
    out.write_text(json.dumps({"results": RESULTS, "passed": passed, "failed": failed}, indent=2))
    print(f"Results written to {out}")

    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
