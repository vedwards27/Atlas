"""
Tier 21 Validation - Continuity Preservation Engine
Tests: genesis branch, multi-generation checkpoint tree, lineage chain,
replay from snapshot, temporal reconstruction, institutional packet,
operational replay simulator, and restart persistence.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kernel import AtlasRuntimeKernel
from memory import MemoryEngine
from continuity import ContinuityEngine

RESULTS = []


def check(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append({"test": name, "status": status, "detail": detail})
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    return passed


def fresh(db_path):
    k = AtlasRuntimeKernel(db_path=db_path)
    m = MemoryEngine(k)
    c = ContinuityEngine(k, m)
    return k, m, c


def test_genesis_branch(db_path):
    print("\n[T1] Genesis Branch Creation")
    k, m, c = fresh(db_path)

    branches = c.get_branch_tree()
    main_branches = [b for b in branches if b["label"] == "main"]
    check("main branch exists", len(main_branches) >= 1, f"count={len(main_branches)}")
    check("genesis has generation 0", main_branches[0]["generation"] == 0 if main_branches else False)
    check("genesis has no parent", main_branches[0]["parent_branch_id"] is None if main_branches else False)
    check("genesis branch_id stored in engine", c._genesis_branch_id is not None)

    # Idempotent: calling fresh again on same DB should not create a second main
    _, _, c2 = fresh(db_path)
    branches2 = c2.get_branch_tree()
    mains2 = [b for b in branches2 if b["label"] == "main" and b["parent_branch_id"] is None]
    check("genesis is idempotent (no duplicate root)", len(mains2) == 1, f"count={len(mains2)}")


def test_multi_generation_checkpoint(db_path):
    print("\n[T2] Multi-Generation Checkpoint Tree")
    k, m, c = fresh(db_path)

    # Three successive checkpoints off main
    r1 = c.checkpoint(label="gen1")
    r2 = c.checkpoint(label="gen2", parent_branch_id=r1["branch_id"])
    r3 = c.checkpoint(label="gen3", parent_branch_id=r2["branch_id"])

    check("gen1 created", "branch_id" in r1, r1.get("label"))
    check("gen2 created", "branch_id" in r2, r2.get("label"))
    check("gen3 created", "branch_id" in r3, r3.get("label"))
    check("gen1 generation is 1", r1["generation"] == 1, str(r1["generation"]))
    check("gen2 generation is 2", r2["generation"] == 2, str(r2["generation"]))
    check("gen3 generation is 3", r3["generation"] == 3, str(r3["generation"]))

    # Each checkpoint creates a snapshot
    check("gen1 has snapshot_id", r1.get("snapshot_id") is not None)
    check("gen3 has summary data", isinstance(r3.get("summary"), dict))

    # Branch tree includes all generations
    tree = c.get_branch_tree()
    labels = {b["label"] for b in tree}
    check("tree contains gen1", "gen1" in labels)
    check("tree contains gen3", "gen3" in labels)


def test_lineage_chain(db_path):
    print("\n[T3] Lineage Chain Walk to Genesis")
    k, m, c = fresh(db_path)

    r1 = c.checkpoint(label="lev1")
    r2 = c.checkpoint(label="lev2", parent_branch_id=r1["branch_id"])
    r3 = c.checkpoint(label="lev3", parent_branch_id=r2["branch_id"])

    chain = c.get_lineage_chain(r3["branch_id"])
    check("chain length is 4 (genesis+lev1+lev2+lev3)", len(chain) == 4, str(len(chain)))
    check("chain starts at genesis (gen 0)", chain[0]["generation"] == 0, str(chain[0]["generation"]))
    check("chain ends at lev3", chain[-1]["label"] == "lev3")
    check("chain is ordered oldest-first", chain[0]["generation"] < chain[-1]["generation"])

    # Walk an orphan branch_id: should return just one entry or empty
    orphan = c.get_lineage_chain("DOES-NOT-EXIST")
    check("orphan branch_id returns empty chain", len(orphan) == 0)


def test_replay_from_snapshot(db_path):
    print("\n[T4] Replay From Snapshot")
    k, m, c = fresh(db_path)

    # Create some events and a snapshot
    k.log_event("TASK_CREATED", "TEST", {"task_id": "TSK-REPLAY-001"})
    k.log_event("TASK_ASSIGNED", "TEST", {"task_id": "TSK-REPLAY-001", "worker_id": "W1"})
    k.log_event("TASK_COMPLETED", "TEST", {"task_id": "TSK-REPLAY-001"})
    k.log_event("WORKER_REGISTERED", "TEST", {"worker_id": "W-REPLAY-001", "role": "tester"})

    snap_id, _ = k.create_snapshot(scope="replay_test")
    result = c.replay_from_snapshot(snap_id)

    check("replay has snapshot_id", result.get("snapshot_id") == snap_id)
    check("replay has events_replayed count", isinstance(result.get("events_replayed"), int))
    check("replay events_replayed > 0", result.get("events_replayed", 0) > 0, str(result.get("events_replayed")))
    check("reconstructed_state present", "reconstructed_state" in result)
    check("baseline_summary present", "baseline_summary" in result)
    check("tasks reconstructed", isinstance(result["reconstructed_state"].get("tasks"), dict))
    check("workers reconstructed", isinstance(result["reconstructed_state"].get("workers"), dict))

    # Test missing snapshot
    bad = c.replay_from_snapshot("SNAP-DOES-NOT-EXIST")
    check("missing snapshot returns error key", "error" in bad)


def test_replay_until_timestamp(db_path):
    print("\n[T5] Replay With until_timestamp Filter")
    k, m, c = fresh(db_path)

    import time as _time
    # Use a unique task id so prior test runs don't pollute the shared DB
    unique_id = f"TSK-AFTER-{int(_time.time() * 1000)}"

    snap_id, _ = k.create_snapshot(scope="ts_test")
    # Capture mid-point AFTER the snapshot so the snapshot is anchored
    mid_ts = datetime.now().isoformat()

    # Ensure at least 2ms gap so the event timestamp is strictly after mid_ts
    _time.sleep(0.002)
    # Log the new event AFTER mid_ts was captured
    k.log_event("TASK_CREATED", "TEST", {"task_id": unique_id})

    # Replay only up to mid_ts — unique_id should not appear (logged after mid_ts)
    result_before = c.replay_from_snapshot(snap_id, until_timestamp=mid_ts)
    tasks_before = result_before["reconstructed_state"]["tasks"]
    check("TSK-AFTER absent when replayed before its creation",
          unique_id not in tasks_before, f"found={unique_id in tasks_before}")

    # Replay to now — unique_id should appear
    result_all = c.replay_from_snapshot(snap_id)
    tasks_all = result_all["reconstructed_state"]["tasks"]
    check("TSK-AFTER present when replayed to now", unique_id in tasks_all)


def test_temporal_reconstruction(db_path):
    print("\n[T6] Temporal Reconstruction (reconstruct_at)")
    import tempfile

    tmp = tempfile.mktemp(suffix=".db")
    k2 = AtlasRuntimeKernel(db_path=tmp)
    m2 = MemoryEngine(k2)
    c2 = ContinuityEngine(k2, m2)

    # Use a timestamp before the genesis snapshot (far in the past)
    result_no_snap = c2.reconstruct_at("1970-01-01T00:00:00")
    check("reconstruct_at returns error when no prior snapshot",
          "error" in result_no_snap, str(result_no_snap.get("error", "")[:50]))

    # Take a snapshot
    m2.checkpoint(scope="temporal_test")
    future = (datetime.now() + timedelta(seconds=5)).isoformat()
    result = c2.reconstruct_at(future)

    check("reconstruct_at succeeds with snapshot", "error" not in result, str(result.get("error")))
    check("result has reconstruction_method", result.get("reconstruction_method") == "snapshot_plus_event_replay")
    check("result has target_timestamp", result.get("target_timestamp") == future)
    check("result has snapshot_id", "snapshot_id" in result)

    os.unlink(tmp)
    for ext in ("-wal", "-shm"):
        try:
            os.unlink(tmp + ext)
        except Exception:
            pass


def test_simulate_replay(db_path):
    print("\n[T7] Operational Replay Simulator")
    k, m, c = fresh(db_path)

    k.log_event("TASK_CREATED", "TEST", {"task_id": "TSK-SIM-001"})
    k.log_event("TASK_ASSIGNED", "TEST", {"task_id": "TSK-SIM-001", "worker_id": "W1"})
    k.log_event("TASK_COMPLETED", "TEST", {"task_id": "TSK-SIM-001"})

    # Get event id range
    rows = k._query("SELECT MIN(event_id), MAX(event_id) FROM event_ledger")
    min_id, max_id = rows[0]

    result = c.simulate_replay(min_id, max_id)

    check("simulate_replay has steps", isinstance(result.get("steps"), list))
    check("step_count > 0", result.get("step_count", 0) > 0, str(result.get("step_count")))
    check("unique_event_types present", isinstance(result.get("unique_event_types"), list))
    check("from_event_id matches", result.get("from_event_id") == min_id)
    check("to_event_id matches", result.get("to_event_id") == max_id)

    # Each step has required fields
    if result["steps"]:
        step = result["steps"][0]
        check("step has event_id", "event_id" in step)
        check("step has simulated_action", "simulated_action" in step)
        check("step has type", "type" in step)

    # Out-of-range: should return empty steps
    result_empty = c.simulate_replay(999999, 999998)
    check("empty range returns 0 steps", result_empty.get("step_count", -1) == 0)


def test_institutional_packet(db_path):
    print("\n[T8] Institutional Inheritance Packet")
    k, m, c = fresh(db_path)

    k.insert_policy("IP-POL-001", 22, "execution", "Test Packet Policy", "No agent may delete lineage.", 100, "TEST")
    m.checkpoint(scope="packet_test")
    k.log_event("PACKET_TEST_EVENT", "TEST", {"note": "test"})

    packet = c.export_institutional_packet()

    check("packet has generated_at", "generated_at" in packet)
    check("packet has version", packet.get("version") == "1.0")
    check("packet has operational_summary", "operational_summary" in packet)
    check("packet has active_directives", "active_directives" in packet)
    check("packet has continuity_branches", "continuity_branches" in packet)
    check("packet has recent_decisions", "recent_decisions" in packet)
    check("packet has governance_policies", "governance_policies" in packet)
    check("packet has recovery_instructions", "recovery_instructions" in packet)

    ri = packet["recovery_instructions"]
    check("recovery_instructions has 5 steps", len(ri) == 5, str(len(ri)))
    check("step_1 mentions server.py", "server" in ri.get("step_1", "").lower())

    # Packet stored in kernel_state
    stored = k.get_state("institutional_packet")
    check("packet persisted to kernel_state", stored is not None)
    check("stored packet has version", stored.get("version") == "1.0" if stored else False)


def test_branch_persistence(db_path):
    print("\n[T9] Branch Persistence Across Restarts")
    # Simulate a server restart by recreating engine on same DB
    k, m, c = fresh(db_path)

    r = c.checkpoint(label="persist_test")
    branch_id = r["branch_id"]

    # Restart: new instances on same DB
    k2, m2, c2 = fresh(db_path)
    tree = c2.get_branch_tree()
    ids = {b["branch_id"] for b in tree}
    check("checkpointed branch survives restart", branch_id in ids, branch_id[:16])

    lineage = c2.get_lineage_chain(branch_id)
    check("lineage chain rebuilt after restart", len(lineage) >= 2)
    check("genesis branch_id re-hydrated", c2._genesis_branch_id is not None)


def test_infer_action_coverage(db_path):
    print("\n[T10] Event Inference Coverage")
    k, m, c = fresh(db_path)

    # All mapped event types should produce non-template strings
    mapped = [
        ("TASK_CREATED",         {"task_id": "T1"}),
        ("TASK_ASSIGNED",        {"task_id": "T1", "worker_id": "W1"}),
        ("TASK_COMPLETED",       {"task_id": "T1"}),
        ("TASK_FAILED",          {"task_id": "T1"}),
        ("WORKER_REGISTERED",    {"worker_id": "W1", "role": "exec"}),
        ("DIRECTIVE_REGISTERED", {"directive_id": "D1"}),
        ("DIRECTIVE_COMPLETED",  {"directive_id": "D1"}),
        ("MEMORY_SNAPSHOT",      {"snapshot_id": "S1"}),
        ("GOVERNANCE_VIOLATION", {"violation_id": "V1"}),
    ]
    all_ok = True
    for ev_type, payload in mapped:
        action = c._infer_action(ev_type, payload)
        if "{" in action or "}" in action:
            all_ok = False
            break
    check("all mapped event types produce filled templates", all_ok)

    # Unknown event type
    action_unknown = c._infer_action("UNKNOWN_EVENT_XYZ", {})
    check("unknown event type returns fallback string", "UNKNOWN_EVENT_XYZ" in action_unknown)


def main():
    print("=" * 60)
    print("TIER 21 - CONTINUITY PRESERVATION ENGINE VALIDATION")
    print("=" * 60)

    db_path = str(Path(__file__).parent / "atlas_runtime.db")
    print(f"Database: {db_path}\n")

    test_genesis_branch(db_path)
    test_multi_generation_checkpoint(db_path)
    test_lineage_chain(db_path)
    test_replay_from_snapshot(db_path)
    test_replay_until_timestamp(db_path)
    test_temporal_reconstruction(db_path)
    test_simulate_replay(db_path)
    test_institutional_packet(db_path)
    test_branch_persistence(db_path)
    test_infer_action_coverage(db_path)

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    total  = len(RESULTS)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    out = Path(__file__).parent.parent / "CONTINUITY_PRESERVATION_REPORT.json"
    out.write_text(json.dumps({"results": RESULTS, "passed": passed, "failed": failed, "total": total}, indent=2))
    print(f"Results written to {out}")

    return failed == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
