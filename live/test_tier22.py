"""
Tier 22 Validation - Governance Constitution
Tests: constitutional boot, law hierarchy, permission checks, dangerous payload blocking,
budget ceiling enforcement, canonical truth arbitration, violation logging,
task enforcement, sovereignty report, and tamper detection.
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kernel import AtlasRuntimeKernel
from memory import MemoryEngine
from constitution import GovernanceConstitution, CONSTITUTION

RESULTS = []


def check(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append({"test": name, "status": status, "detail": detail})
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    return passed


def fresh(db_path):
    k = AtlasRuntimeKernel(db_path=db_path)
    m = MemoryEngine(k)
    g = GovernanceConstitution(k, m)
    return k, m, g


def test_constitutional_boot(db_path):
    print("\n[T1] Constitutional Boot and Policy Loading")
    k, m, g = fresh(db_path)

    status = g.get_status()
    check("constitution status is CONSTITUTION_ACTIVE", status["status"] == "CONSTITUTION_ACTIVE")
    check("policy_count >= 10", status["policy_count"] >= 10, str(status["policy_count"]))
    check("integrity_ok on fresh boot", status["integrity_ok"], str(status.get("tampered_policies")))
    check("load_count is 1", status["load_count"] == 1)

    # Idempotent boot: second GovernanceConstitution on same DB should not double-load
    g2 = GovernanceConstitution(k, m)
    status2 = g2.get_status()
    check("policy count unchanged after second boot", status2["policy_count"] == status["policy_count"],
          f"{status['policy_count']} -> {status2['policy_count']}")


def test_law_hierarchy(db_path):
    print("\n[T2] Law Hierarchy Structure")
    k, m, g = fresh(db_path)

    hierarchy = g.get_law_hierarchy()
    check("hierarchy is non-empty", len(hierarchy) > 0, str(len(hierarchy)))

    # Verify ordering: all higher authority_level policies come before lower
    levels = [p["authority_level"] for p in hierarchy]
    check("hierarchy sorted by authority descending", levels == sorted(levels, reverse=True))

    # L1 absolute policies present
    l1 = g.get_policies_by_level("L1_ABSOLUTE")
    check("L1_ABSOLUTE policies present", len(l1) >= 3, str(len(l1)))
    check("all L1 have authority_level 1000", all(p["authority_level"] == 1000 for p in l1))
    check("L1 violation_action is BLOCK", all(p["violation_action"] == "BLOCK" for p in l1))

    # L4 advisory policies present
    l4 = g.get_policies_by_level("L4_ADVISORY")
    check("L4_ADVISORY policies present", len(l4) >= 1, str(len(l4)))
    check("L4 violation_action is AUDIT", all(p["violation_action"] == "AUDIT" for p in l4))


def test_dangerous_payload_blocking(db_path):
    print("\n[T3] Dangerous Payload Blocking (L1-003 + L3-003)")
    k, m, g = fresh(db_path)

    dangerous_prompts = [
        "rm -rf /home/user",
        "please DROP TABLE users cascade",
        "format disk C:",
        "DELETE FROM everything",
        "dd if=/dev/zero of=/dev/sda",
    ]
    for prompt in dangerous_prompts:
        result = g.check_permission("task_execute", {"prompt": prompt}, actor="TEST-AGENT")
        check(f"blocked: {prompt[:30]!r}", not result["allowed"])
        check(f"violation logged for: {prompt[:20]!r}",
              any(v["action"] == "BLOCK" for v in result.get("violations", [])))

    # Safe prompt should pass
    safe = g.check_permission("task_execute", {"prompt": "summarize this document"}, actor="TEST-AGENT")
    check("safe prompt is allowed", safe["allowed"])
    check("safe prompt has no violations", len(safe["violations"]) == 0)


def test_budget_ceiling(db_path):
    print("\n[T4] Budget Ceiling Enforcement (L2-001)")
    k, m, g = fresh(db_path)

    # Simulating a payload that would exceed daily budget
    over_budget = {
        "estimated_cost_cu": 100.0,
        "daily_budget_cu": 50.0,
        "day_cost": 40.0,
    }
    result = g.check_permission("task_execute", over_budget, actor="DISPATCH")
    check("over-budget request is blocked", not result["allowed"])

    # Under budget: should pass
    under_budget = {
        "estimated_cost_cu": 5.0,
        "daily_budget_cu": 50.0,
        "day_cost": 10.0,
    }
    result2 = g.check_permission("task_execute", under_budget, actor="DISPATCH")
    check("under-budget request is allowed", result2["allowed"])


def test_lineage_erasure_blocked(db_path):
    print("\n[T5] Lineage Erasure Blocked (L1-001)")
    k, m, g = fresh(db_path)

    for dangerous_action in ["drop_table", "truncate", "delete_events", "wipe_snapshots"]:
        result = g.check_permission(dangerous_action, {}, actor="ROGUE-AGENT")
        check(f"action={dangerous_action!r} is blocked", not result["allowed"])


def test_permission_structure(db_path):
    print("\n[T6] Permission Result Structure")
    k, m, g = fresh(db_path)

    result = g.check_permission("task_execute", {"prompt": "rm -rf /tmp"}, actor="W-001")
    check("result has 'allowed' key", "allowed" in result)
    check("result has 'violations' list", isinstance(result.get("violations"), list))
    check("result has 'blocking_policy'", "blocking_policy" in result)
    check("result has 'actor'", result.get("actor") == "W-001")
    check("result has 'evaluated_at'", "evaluated_at" in result)
    check("blocking_policy is a string", isinstance(result.get("blocking_policy"), str))

    if result["violations"]:
        v = result["violations"][0]
        check("violation has policy_id", "policy_id" in v)
        check("violation has law_level", "law_level" in v)
        check("violation has action", "action" in v)
        check("violation has reason", "reason" in v)
        check("violation has violation_id", "violation_id" in v)


def test_canonical_truth_arbitration(db_path):
    print("\n[T7] Canonical Truth Arbitration")
    k, m, g = fresh(db_path)

    # EVENT_LEDGER source always wins
    claims_with_ledger = [
        {"source": "AGENT_A", "claim": "task_state", "value": "FAILED", "timestamp": "2026-01-01T12:00:00", "authority": 500},
        {"source": "EVENT_LEDGER", "claim": "task_state", "value": "COMPLETED", "timestamp": "2026-01-01T11:59:00", "authority": 1000},
        {"source": "AGENT_B", "claim": "task_state", "value": "RUNNING", "timestamp": "2026-01-01T12:01:00", "authority": 300},
    ]
    result = g.arbitrate_truth(claims_with_ledger)
    check("EVENT_LEDGER wins arbitration", result["winner"]["source"] == "EVENT_LEDGER")
    check("method is event_ledger_authority", result["method"] == "event_ledger_authority")
    check("confidence is 1.0", result["confidence"] == 1.0)

    # No ledger: highest authority wins
    claims_no_ledger = [
        {"source": "AGENT_A", "claim": "x", "value": "a", "timestamp": "2026-01-01T10:00:00", "authority": 300},
        {"source": "AGENT_B", "claim": "x", "value": "b", "timestamp": "2026-01-01T09:00:00", "authority": 800},
    ]
    result2 = g.arbitrate_truth(claims_no_ledger)
    check("highest authority wins (no ledger)", result2["winner"]["source"] == "AGENT_B")
    check("method is authority_level", result2["method"] == "authority_level")

    # Tie in authority: most recent wins
    claims_tied = [
        {"source": "AGENT_X", "claim": "x", "value": "old", "timestamp": "2026-01-01T08:00:00", "authority": 500},
        {"source": "AGENT_Y", "claim": "x", "value": "new", "timestamp": "2026-01-01T12:00:00", "authority": 500},
    ]
    result3 = g.arbitrate_truth(claims_tied)
    check("most recent wins on tie", result3["winner"]["source"] == "AGENT_Y")
    check("method is recency", result3["method"] == "recency")

    # Empty claims
    result4 = g.arbitrate_truth([])
    check("empty claims returns no winner", result4["winner"] is None)


def test_task_enforcement(db_path):
    print("\n[T8] Task Enforcement (enforce_on_task)")
    k, m, g = fresh(db_path)

    # Create a task with a dangerous prompt
    dangerous_task_id = k.add_task("CONST-TEST", {"prompt": "rm -rf /", "task_type": "general"})
    result = g.enforce_on_task(dangerous_task_id, {"prompt": "rm -rf /"}, worker_id="W-CONST")
    check("dangerous task is blocked", not result["allowed"])

    # Verify task state is now BLOCKED in DB
    rows = k._query("SELECT state FROM task_queue WHERE task_id=?", (dangerous_task_id,))
    check("task moved to BLOCKED state in DB", rows[0][0] == "BLOCKED" if rows else False)

    # Safe task should not be blocked
    safe_task_id = k.add_task("CONST-TEST", {"prompt": "explain recursion", "task_type": "general"})
    result2 = g.enforce_on_task(safe_task_id, {"prompt": "explain recursion"}, worker_id="W-CONST")
    check("safe task is allowed", result2["allowed"])
    rows2 = k._query("SELECT state FROM task_queue WHERE task_id=?", (safe_task_id,))
    check("safe task remains QUEUED", rows2[0][0] == "QUEUED" if rows2 else False)


def test_violation_logging(db_path):
    print("\n[T9] Violation Logging and Persistence")
    k, m, g = fresh(db_path)

    before_count = k._query("SELECT COUNT(*) FROM governance_violations")[0][0]
    # Trigger a violation
    g.check_permission("task_execute", {"prompt": "DROP TABLE users"}, actor="ROGUE")
    after_count = k._query("SELECT COUNT(*) FROM governance_violations")[0][0]
    check("violation count increased", after_count > before_count, f"{before_count} -> {after_count}")

    # Violation logged to event ledger
    events = k._query(
        "SELECT type FROM event_ledger WHERE type='GOVERNANCE_VIOLATION' ORDER BY event_id DESC LIMIT 3"
    )
    check("GOVERNANCE_VIOLATION event logged", len(events) > 0)

    # Session counter increments
    check("session_violations counter increments", g._violation_count > 0, str(g._violation_count))


def test_sovereignty_report(db_path):
    print("\n[T10] Sovereignty Report Generation")
    k, m, g = fresh(db_path)
    m.checkpoint(scope="sovereignty_test")

    report = g.generate_sovereignty_report()
    check("report has generated_at", "generated_at" in report)
    check("report tier is 22", report.get("tier") == 22)
    check("report has constitution_status", "constitution_status" in report)
    check("report has law_hierarchy", "law_hierarchy" in report)
    check("report has L1_ABSOLUTE entries", len(report["law_hierarchy"].get("L1_ABSOLUTE", [])) >= 3)
    check("report has arbitration_model", "arbitration_model" in report)
    check("report has enforcement_chain", isinstance(report.get("enforcement_chain"), list))
    check("enforcement_chain has 5 steps", len(report.get("enforcement_chain", [])) == 5)
    check("report has policy_count_by_level", "policy_count_by_level" in report)

    # Report persisted to kernel_state
    stored = k.get_state("sovereignty_report")
    check("sovereignty report persisted to kernel_state", stored is not None)
    check("stored report has tier 22", stored.get("tier") == 22 if stored else False)

    # Sovereignty event logged
    rows = k._query(
        "SELECT type FROM event_ledger WHERE type='SOVEREIGNTY_REPORT_GENERATED' ORDER BY event_id DESC LIMIT 1"
    )
    check("SOVEREIGNTY_REPORT_GENERATED event logged", len(rows) > 0)


def test_tamper_detection(db_path):
    print("\n[T11] Tamper Detection")
    import tempfile, os
    tmp = tempfile.mktemp(suffix=".db")
    k2 = AtlasRuntimeKernel(db_path=tmp)
    m2 = MemoryEngine(k2)
    g2 = GovernanceConstitution(k2, m2)

    # Integrity should be fine on fresh boot
    tampers = g2._verify_integrity()
    check("no tamper on fresh boot", len(tampers) == 0, str(len(tampers)))

    # Manually corrupt a policy body
    k2._query(
        "UPDATE governance_policies SET body='TAMPERED BODY' WHERE policy_id='CONST-L1-001'",
        commit=True
    )
    tampers2 = g2._verify_integrity()
    check("tamper detected after body mutation", len(tampers2) > 0, str(len(tampers2)))
    check("tampered policy is CONST-L1-001",
          any(t["policy_id"] == "CONST-L1-001" for t in tampers2))

    os.unlink(tmp)
    for ext in ("-wal", "-shm"):
        try:
            os.unlink(tmp + ext)
        except Exception:
            pass


def test_constitution_completeness():
    print("\n[T12] Constitution Definition Completeness")
    required_levels = {"L1_ABSOLUTE", "L2_SOVEREIGN", "L3_OPERATIONAL", "L4_ADVISORY"}
    required_actions = {"BLOCK", "WARN", "AUDIT"}
    present_levels = {pol["law_level"] for pol in CONSTITUTION}
    present_actions = {pol["violation_action"] for pol in CONSTITUTION}

    check("all law levels represented", required_levels.issubset(present_levels),
          str(present_levels))
    check("all violation actions used", required_actions.issubset(present_actions),
          str(present_actions))
    check("all policies have policy_id", all("policy_id" in p for p in CONSTITUTION))
    check("all policies have tier=22", all(p["tier"] == 22 for p in CONSTITUTION))
    check("all policies have body", all(len(p.get("body", "")) > 20 for p in CONSTITUTION))

    # authority_level ordering: L1 > L2 > L3 > L4
    l1_min = min(p["authority_level"] for p in CONSTITUTION if p["law_level"] == "L1_ABSOLUTE")
    l2_max = max(p["authority_level"] for p in CONSTITUTION if p["law_level"] == "L2_SOVEREIGN")
    l3_max = max(p["authority_level"] for p in CONSTITUTION if p["law_level"] == "L3_OPERATIONAL")
    l4_max = max(p["authority_level"] for p in CONSTITUTION if p["law_level"] == "L4_ADVISORY")
    check("L1 authority > L2", l1_min > l2_max, f"{l1_min} > {l2_max}")
    check("L2 authority > L3", l2_max > l3_max, f"{l2_max} > {l3_max}")
    check("L3 authority > L4", l3_max > l4_max, f"{l3_max} > {l4_max}")


def main():
    print("=" * 60)
    print("TIER 22 - GOVERNANCE CONSTITUTION VALIDATION")
    print("=" * 60)

    db_path = str(Path(__file__).parent / "atlas_runtime.db")
    print(f"Database: {db_path}\n")

    test_constitution_completeness()
    test_constitutional_boot(db_path)
    test_law_hierarchy(db_path)
    test_dangerous_payload_blocking(db_path)
    test_budget_ceiling(db_path)
    test_lineage_erasure_blocked(db_path)
    test_permission_structure(db_path)
    test_canonical_truth_arbitration(db_path)
    test_task_enforcement(db_path)
    test_violation_logging(db_path)
    test_sovereignty_report(db_path)
    test_tamper_detection(db_path)

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    total  = len(RESULTS)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    out = Path(__file__).parent.parent / "SOVEREIGNTY_VALIDATION_REPORT.json"
    out.write_text(json.dumps({"results": RESULTS, "passed": passed, "failed": failed, "total": total}, indent=2))
    print(f"Results written to {out}")

    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
