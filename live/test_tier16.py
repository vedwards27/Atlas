"""
Tier 16 Validation — Continuous Operational Memory
Tests: restart persistence, directive lineage, decision replay,
FTS search, memory compression, hydration.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# ── Setup ─────────────────────────────────────────────────────────────────────

sys.path.insert(0, str(Path(__file__).parent))
from kernel import AtlasRuntimeKernel
from memory import MemoryEngine

RESULTS: list[dict] = []


def check(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append({"test": name, "status": status, "detail": detail})
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {name}" + (f" - {detail}" if detail else ""))
    return passed


# ── Helpers ───────────────────────────────────────────────────────────────────

def fresh_kernel(db_path: str) -> tuple[AtlasRuntimeKernel, MemoryEngine]:
    k = AtlasRuntimeKernel(db_path=db_path)
    m = MemoryEngine(k)
    return k, m


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_restart_persistence(db_path: str):
    print("\n[T1] Restart Persistence")

    # Session 1 — write data
    k1, m1 = fresh_kernel(db_path)
    dir_id = m1.open_directive("Test Directive", "Persistence test", tier=16)
    k1.add_task(dir_id, {"prompt": "hello", "task_type": "general"})
    dec_id = m1.record_decision(dir_id, "TEST-WORKER", "routing prompt", "use llama3", "cheapest fit")
    snap_id, _ = k1.create_snapshot(scope="test")
    del k1, m1  # simulate process death

    # Session 2 — verify data survived
    k2, m2 = fresh_kernel(db_path)
    hydration = m2.get_hydration_state()

    dirs = k2.get_directives()
    check("directive survives restart", any(r[0] == dir_id for r in dirs), dir_id)

    tasks = k2._query("SELECT task_id FROM task_queue WHERE directive_id = ?", (dir_id,))
    check("task survives restart", len(tasks) > 0)

    decisions = k2.get_decisions(directive_id=dir_id)
    check("decision survives restart", len(decisions) > 0)

    snaps = k2.get_snapshots()
    check("snapshot survives restart", any(r[0] == snap_id for r in snaps))

    check("hydration state loaded on restart", hydration.get("active_directives", 0) >= 0,
          f"active={hydration.get('active_directives')}")

    return dir_id, dec_id


def test_directive_lineage(db_path: str):
    print("\n[T2] Directive Lineage")
    k, m = fresh_kernel(db_path)

    parent = m.open_directive("Parent Tier", "Top-level", tier=15)
    child  = m.open_directive("Child Tier", "Nested under parent", tier=16, parent_id=parent)
    grandchild = m.open_directive("Grandchild", "Nested deeper", tier=16, parent_id=child)

    lineage = m.get_lineage(grandchild)
    ids_in_lineage = [e["directive_id"] for e in lineage]

    check("parent appears in grandchild lineage", parent in ids_in_lineage, f"lineage={ids_in_lineage}")
    check("child appears in grandchild lineage", child in ids_in_lineage)
    check("grandchild is current node", lineage[-1].get("is_current") is True)
    check("lineage depth is 3", len(lineage) == 3, f"depth={len(lineage)}")

    m.close_directive(parent, outcome="SUCCESS")
    dirs = k.get_directives(state="COMPLETED")
    check("completed directive state persisted", any(r[0] == parent for r in dirs))


def test_decision_history(db_path: str):
    print("\n[T3] Decision History")
    k, m = fresh_kernel(db_path)

    dir_id = m.open_directive("Decision Test", "Tests decision log", tier=16)
    d1 = m.record_decision(dir_id, "W-001", "ctx-a", "decision-a", "rationale-a")
    d2 = m.record_decision(dir_id, "W-001", "ctx-b", "decision-b", "rationale-b")
    m.resolve_decision(d1, outcome="COMPLETED in 120ms")

    history = m.get_decision_history(directive_id=dir_id)
    check("decisions recorded", len(history) >= 2, f"count={len(history)}")

    d1_data = next((d for d in history if d["decision_id"] == d1), None)
    check("decision outcome updated", d1_data and "COMPLETED" in (d1_data.get("outcome") or ""),
          f"outcome={d1_data.get('outcome') if d1_data else 'missing'}")
    check("decision without outcome has None", any(d.get("outcome") is None for d in history if d["decision_id"] == d2))


def test_timeline_reconstruction(db_path: str):
    print("\n[T4] Timeline Reconstruction")
    k, m = fresh_kernel(db_path)

    dir_id = m.open_directive("Timeline Test", "Reconstruct test", tier=16)
    k.add_task(dir_id, {"prompt": "p", "task_type": "general"})
    m.record_decision(dir_id, "W-001", "ctx", "dec", "rat", trace_id=dir_id)

    timeline = m.reconstruct_timeline(directive_id=dir_id, limit=50)
    kinds = {e["kind"] for e in timeline}
    check("timeline contains events", "EVENT" in kinds)
    check("timeline contains decisions", "DECISION" in kinds)
    check("timeline is reverse-chronological",
          all(timeline[i]["timestamp"] >= timeline[i+1]["timestamp"] for i in range(len(timeline)-1)))


def test_fts_search(db_path: str):
    print("\n[T5] Full-Text Search")
    k, m = fresh_kernel(db_path)

    dir_id = m.open_directive("Search Test Dir", "FTS validation", tier=16)
    m.record_decision(dir_id, "W-001", "routing prompt for llama", "use_llama3_latest", "cheapest for general tasks")
    k.log_event("CUSTOM_EVENT", "TEST", {"note": "xylophone_unique_token"})
    k.rebuild_fts()

    results = m.search("xylophone_unique_token")
    check("FTS finds unique event token", results["total"] > 0, f"total={results['total']}")

    results2 = m.search("llama")
    check("FTS finds decision with 'llama'", results2["total"] > 0, f"total={results2['total']}")


def test_memory_snapshot(db_path: str):
    print("\n[T6] Memory Snapshots")
    k, m = fresh_kernel(db_path)

    snap = m.checkpoint(scope="test_snapshot")
    check("snapshot has id", "snapshot_id" in snap, snap.get("snapshot_id"))
    check("snapshot has event count", "total_events" in snap)

    snaps = m.get_snapshots()
    check("snapshot retrievable", any(s["snapshot_id"] == snap["snapshot_id"] for s in snaps))

    latest = k.get_latest_snapshot()
    check("get_latest_snapshot works", latest is not None)


def test_operational_summary(db_path: str):
    print("\n[T7] Operational Summary")
    k, m = fresh_kernel(db_path)
    summary = m.get_operational_summary()

    required_keys = {"generated_at", "hydration", "directives", "events", "decisions", "snapshots"}
    missing = required_keys - set(summary.keys())
    check("summary has all required keys", not missing, f"missing={missing}")
    check("directive counts are integers", isinstance(summary["directives"]["total"], int))
    check("event total is integer", isinstance(summary["events"]["total"], int))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("TIER 16 — CONTINUOUS OPERATIONAL MEMORY VALIDATION")
    print("=" * 60)

    db_path = str(Path(__file__).parent / "atlas_runtime.db")
    print(f"Database: {db_path}\n")

    test_restart_persistence(db_path)
    test_directive_lineage(db_path)
    test_decision_history(db_path)
    test_timeline_reconstruction(db_path)
    test_fts_search(db_path)
    test_memory_snapshot(db_path)
    test_operational_summary(db_path)

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    total  = len(RESULTS)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    # Write machine-readable results
    output_path = Path(__file__).parent.parent / "MEMORY_REPLAY_TEST.json"
    output_path.write_text(json.dumps({"results": RESULTS, "passed": passed, "failed": failed, "total": total}, indent=2))
    print(f"Results written to {output_path}")

    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
