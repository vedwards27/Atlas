"""
Tier 19 Validation — Survivability + Institutional Hardening
Tests: restart recovery, checkpoint restore, queue restoration, worker resurrection,
corruption detection, drift detection, duplicate root detection, integrity scoring,
and all failure simulation scenarios.
"""
import json
import sys
import time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from kernel import AtlasRuntimeKernel
from memory import MemoryEngine
from survivability import SurvivabilityEngine

RESULTS = []


def check(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append({"test": name, "status": status, "detail": detail})
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    return passed


def fresh(db_path):
    k = AtlasRuntimeKernel(db_path=db_path)
    m = MemoryEngine(k)
    s = SurvivabilityEngine(k, m, db_path)
    return k, m, s


def test_queue_restoration(db_path):
    print("\n[T1] Queue Restoration After Crash")
    k, m, s = fresh(db_path)

    # Simulate crash: tasks stuck in RUNNING with dead worker
    t1 = k.add_task("DIR-CRASH-TEST", {"prompt": "crashed task", "task_type": "general"})
    t2 = k.add_task("DIR-CRASH-TEST", {"prompt": "another crash", "task_type": "general"})
    k._query("UPDATE task_queue SET state = 'RUNNING', worker_id = 'DEAD-001' WHERE task_id IN (?,?)", (t1, t2), commit=True)

    # Simulate fresh startup
    k2, m2, s2 = fresh(db_path)
    restored = s2.restore_interrupted_tasks()

    check("orphaned RUNNING tasks detected", restored >= 2, str(restored))

    states = k2._query("SELECT state FROM task_queue WHERE task_id IN (?,?)", (t1, t2))
    check("tasks re-queued", all(r[0] == "QUEUED" for r in states), str([r[0] for r in states]))

    events = k2._query("SELECT type FROM event_ledger WHERE type = 'TASK_RESTORED_ON_STARTUP'")
    check("TASK_RESTORED_ON_STARTUP event logged", len(events) >= 2)


def test_worker_resurrection(db_path):
    print("\n[T2] Worker Resurrection on Restart")
    k, m, s = fresh(db_path)

    # Workers left ACTIVE (clean shutdown never happened)
    k.register_worker("ZOMBIE-001", "execution_agent", "sim", "local", "none")
    k.register_worker("ZOMBIE-002", "planner_agent",   "sim", "local", "none")
    k._query("UPDATE worker_registry SET runtime_state = 'ACTIVE' WHERE worker_id IN ('ZOMBIE-001','ZOMBIE-002')", commit=True)

    # Fresh startup
    k2, m2, s2 = fresh(db_path)
    count = s2.restore_stalled_workers()

    check("zombie workers detected", count >= 2, str(count))
    states = k2._query("SELECT runtime_state FROM worker_registry WHERE worker_id IN ('ZOMBIE-001','ZOMBIE-002')")
    check("zombie workers marked STALLED", all(r[0] == "STALLED" for r in states))


def test_checkpoint_restore(db_path):
    print("\n[T3] Checkpoint Snapshot & Restore")
    k, m, s = fresh(db_path)

    snap = s.take_checkpoint(reason="test_restore")
    check("checkpoint created", "snapshot_id" in snap, snap.get("snapshot_id", "missing"))
    check("checkpoint_count incremented", s._checkpoint_count == 1)

    # Simulate restart and verify snapshot survives
    k2, m2, s2 = fresh(db_path)
    snaps = m2.get_snapshots()
    check("snapshot survives restart", any(x["snapshot_id"] == snap["snapshot_id"] for x in snaps))

    latest = k2.get_latest_snapshot()
    check("get_latest_snapshot returns data", latest is not None)
    check("latest snapshot has event count", "total_events" in (latest or {}))


def test_db_integrity(db_path):
    print("\n[T4] Database Integrity Check")
    k, m, s = fresh(db_path)

    result = s.check_db_integrity()
    check("integrity_check passes on healthy DB", result["ok"], str(result))
    check("score is 1.0 on clean DB", result["score"] == 1.0, str(result["score"]))
    check("no missing tables", result["missing_tables"] == [], str(result["missing_tables"]))


def test_drift_detection(db_path):
    print("\n[T5] Event Drift Detection")
    k, m, s = fresh(db_path)

    # Log a fresh event now
    k.log_event("DRIFT_TEST", "TEST", {"note": "fresh"})
    result = s.check_event_drift()
    check("no drift when event just logged", not result["drift"], str(result))
    check("last_event is populated", result["last_event"] is not None)
    check("age_seconds is small", result["age_seconds"] < 10, str(result["age_seconds"]))


def test_duplicate_detection(db_path):
    print("\n[T6] Duplicate Root Detection")
    k, m, s = fresh(db_path)

    k.register_worker("DUP-X", "execution_agent", "sim", "local", "none")
    k.register_worker("DUP-Y", "execution_agent", "sim", "local", "none")
    k._query("UPDATE worker_registry SET runtime_state = 'ACTIVE' WHERE worker_id IN ('DUP-X','DUP-Y')", commit=True)

    result = s.check_for_duplicates()
    dup_roles = [d["role"] for d in result["duplicates"]]
    check("duplicate execution_agent detected", "execution_agent" in dup_roles, str(dup_roles))
    check("ok=False when duplicates present", not result["ok"])


def test_state_fingerprint(db_path):
    print("\n[T7] Canonical Truth Fingerprint")
    k, m, s = fresh(db_path)

    fp1 = s.compute_state_fingerprint()
    check("fingerprint is a string", isinstance(fp1, str))
    check("fingerprint is 16 chars", len(fp1) == 16, str(len(fp1)))

    # Change state and verify fingerprint changes
    k.add_task("DIR-FP-TEST", {"prompt": "fp change test", "task_type": "general"})
    fp2 = s.compute_state_fingerprint()
    check("fingerprint changes after state mutation", fp1 != fp2, f"{fp1} vs {fp2}")


def test_integrity_score(db_path):
    print("\n[T8] Operational Integrity Score")
    k, m, s = fresh(db_path)

    score = s.get_integrity_score()
    check("score is a number", isinstance(score["score"], (int, float)))
    check("score has grade", score["grade"] in ("A", "B", "C", "F"))
    check("score has task_counts", "task_counts" in score)
    check("score has worker_states", "worker_states" in score)
    check("healthy system scores >= 80", score["score"] >= 80, str(score["score"]))


def test_failure_simulations(db_path):
    print("\n[T9] Failure Simulation Suite")
    k, m, s = fresh(db_path)

    scenarios = ["queue_corruption", "stale_worker", "checkpoint_restore", "integrity_check", "duplicate_root"]
    for scenario in scenarios:
        result = s.simulate_recovery(scenario)
        check(f"simulation:{scenario} recovered", result["recovered"],
              " | ".join(result["steps"][-1:]))

    # Verify FAILURE_SIMULATION events logged
    events = k._query("SELECT type FROM event_ledger WHERE type = 'FAILURE_SIMULATION'")
    check("FAILURE_SIMULATION events logged", len(events) >= len(scenarios), str(len(events)))


def test_restart_proof(db_path):
    print("\n[T10] Full Restart Proof")
    # Create data in session 1
    k1, m1, s1 = fresh(db_path)
    dir_id = m1.open_directive("Restart Proof Directive", "Survives process death", tier=19)
    task_id = k1.add_task(dir_id, {"prompt": "persist me", "task_type": "general"})
    dec_id  = m1.record_decision(dir_id, "SURV-001", "persist", "store_in_sqlite", "WAL mode")
    snap    = s1.take_checkpoint(reason="pre_restart")
    score1  = s1.get_integrity_score()
    del k1, m1, s1

    # Session 2 — cold start, verify everything
    k2, m2, s2 = fresh(db_path)
    dirs = k2.get_directives()
    check("directive survives process death", any(r[0] == dir_id for r in dirs))

    tasks = k2._query("SELECT state FROM task_queue WHERE task_id = ?", (task_id,))
    check("task survives process death", len(tasks) > 0)

    decs = k2.get_decisions(directive_id=dir_id)
    check("decision survives process death", any(d[0] == dec_id for d in decs))

    snaps = m2.get_snapshots()
    check("checkpoint snapshot survives process death", any(x["snapshot_id"] == snap["snapshot_id"] for x in snaps))

    hydration = m2.get_hydration_state()
    check("hydration loaded without manual step", hydration["active_directives"] >= 0)

    integrity = s2.check_db_integrity()
    check("DB integrity intact after restart", integrity["ok"])

    score2 = s2.get_integrity_score()
    check("integrity score available after restart", isinstance(score2["score"], (int, float)))


def main():
    print("=" * 60)
    print("TIER 19 - SURVIVABILITY + INSTITUTIONAL HARDENING")
    print("=" * 60)

    db_path = str(Path(__file__).parent / "atlas_runtime.db")
    print(f"Database: {db_path}\n")

    test_queue_restoration(db_path)
    test_worker_resurrection(db_path)
    test_checkpoint_restore(db_path)
    test_db_integrity(db_path)
    test_drift_detection(db_path)
    test_duplicate_detection(db_path)
    test_state_fingerprint(db_path)
    test_integrity_score(db_path)
    test_failure_simulations(db_path)
    test_restart_proof(db_path)

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    total  = len(RESULTS)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    out = Path(__file__).parent.parent / "SURVIVABILITY_REPORT.json"
    out.write_text(json.dumps({"results": RESULTS, "passed": passed, "failed": failed, "total": total}, indent=2))
    print(f"Results written to {out}")

    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
