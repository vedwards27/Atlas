# FAILURE SIMULATION REPORT — TIER 19
Generated: 2026-05-13

---

## Simulations Run

All simulations executed via `SurvivabilityEngine.simulate_recovery(scenario)`
and also accessible at `POST /api/survivability/simulate/{scenario}`.

---

### queue_corruption
**Simulates**: abrupt process kill leaving tasks in RUNNING state  
**Mechanism**: inject task with state=RUNNING, worker=DEAD-WORKER  
**Recovery**: `restore_interrupted_tasks()` re-queues all orphaned RUNNING tasks  
**Result**: RECOVERED — task state→QUEUED, event TASK_RESTORED_ON_STARTUP logged  

---

### stale_worker
**Simulates**: workers that didn't receive SIGINT before process died  
**Mechanism**: register worker, set state=ACTIVE, simulate restart  
**Recovery**: `restore_stalled_workers()` marks all non-OFFLINE workers STALLED  
**Result**: RECOVERED — worker state→STALLED  

---

### checkpoint_restore
**Simulates**: need to verify state from a prior snapshot  
**Mechanism**: `take_checkpoint()` writes snapshot, fresh instance reads it back  
**Recovery**: snapshot found in memory_snapshots table, all data intact  
**Result**: RECOVERED — snapshot_id found in fresh session  

---

### integrity_check
**Simulates**: potential DB corruption or schema drift  
**Mechanism**: `PRAGMA integrity_check` + verify all 7 required tables exist  
**Recovery**: reported as ok=True, score=1.0 on healthy DB  
**Result**: RECOVERED — no corruption detected  

---

### duplicate_root
**Simulates**: duplicate workers registered for same role  
**Mechanism**: two workers with role=execution_agent, both ACTIVE  
**Recovery**: `check_for_duplicates()` detects and logs; operator must deregister one  
**Result**: DETECTED (detection is recovery step 1; deregistration is operator action)  

---

## Additional Scenarios Covered By Existing Agents

| Scenario | Agent | Mechanism |
|---|---|---|
| Worker heartbeat miss | RecoveryAgent | detect timeout → mark STALLED → re-queue tasks |
| Task retry after failure | RecoveryAgent | FAILED → QUEUED (up to MAX_RETRIES=3) |
| Dangerous payload | GovernanceAgent | QUEUED → BLOCKED before any worker claims it |
| Stuck RUNNING task | VerifierAgent | re-queues tasks stuck >120s in RUNNING |
| Provider outage | ProviderMesh | mark offline, fall through to next provider |
| All cloud providers down | ProviderMesh | route to local Ollama only (degraded mode) |
| High failure rate | GovernanceAgent | logs GOVERNANCE_HIGH_FAILURE_RATE flag |
