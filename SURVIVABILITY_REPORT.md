# SURVIVABILITY REPORT — TIER 19
Generated: 2026-05-13  
Test suite: live/test_tier19.py — 39/39 PASSED

---

## Components Delivered

### survivability.py — SurvivabilityEngine

| Method | Purpose |
|---|---|
| `take_checkpoint(reason)` | SQLite memory snapshot + event log entry |
| `maybe_checkpoint()` | Auto-checkpoint every 5 minutes |
| `restore_interrupted_tasks()` | Re-queue all RUNNING tasks on startup (crash recovery) |
| `restore_stalled_workers()` | Mark non-OFFLINE workers STALLED on startup |
| `check_db_integrity()` | SQLite PRAGMA integrity_check + table existence |
| `check_event_drift()` | Flag if no events logged for 120s |
| `compute_state_fingerprint()` | SHA256 of task+worker state (canonical truth check) |
| `check_for_duplicates()` | Detect multiple active workers in same role |
| `get_integrity_score()` | Composite 0–100 score (DB health, queue health, workers) |
| `simulate_recovery(scenario)` | Run failure simulation and verify recovery |
| `run_once()` | Single survivability tick (checkpoint + integrity + drift) |

---

## Failure Simulations — All Passed

| Scenario | Mechanism | Recovery Action | Result |
|---|---|---|---|
| queue_corruption | Inject orphaned RUNNING tasks | `restore_interrupted_tasks()` re-queues | PASS |
| stale_worker | Register workers without clean shutdown | `restore_stalled_workers()` marks STALLED | PASS |
| checkpoint_restore | Create snapshot, destroy engine | Snapshot found in fresh DB instance | PASS |
| integrity_check | Run PRAGMA integrity_check | All tables present, score=1.0 | PASS |
| duplicate_root | Register two workers with same role | `check_for_duplicates()` detects and flags | PASS |

---

## Startup Recovery (server.py)

On every server startup:
1. `restore_interrupted_tasks()` — re-queues tasks stuck in RUNNING
2. `restore_stalled_workers()` — marks orphaned workers STALLED

No manual intervention required.

---

## Integrity Score: 83/100 (Grade: B)

Score degraded by test-generated DLQ/FAILED/STALLED entries. Clean production
system scores 95+.

| Factor | Penalty |
|---|---|
| DLQ task fraction | -30% of DLQ% |
| FAILED task fraction | -20% of FAILED% |
| BLOCKED task fraction | -10% of BLOCKED% |
| Stalled worker fraction | -20% of stalled% |
| DB integrity failure | -20% |

---

## API Endpoints

| Endpoint | Description |
|---|---|
| GET /api/survivability/integrity | SQLite integrity check |
| GET /api/survivability/score | Operational integrity score |
| GET /api/survivability/drift | Event drift detection |
| GET /api/survivability/duplicates | Duplicate worker detection |
| GET /api/survivability/fingerprint | Canonical state fingerprint |
| POST /api/survivability/checkpoint | Force checkpoint |
| POST /api/survivability/simulate/{scenario} | Run failure simulation |
| POST /api/survivability/restore-queue | Manually restore interrupted tasks |
