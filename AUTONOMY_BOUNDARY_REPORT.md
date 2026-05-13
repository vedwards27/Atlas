# AUTONOMY BOUNDARY REPORT — TIER 17
Generated: 2026-05-13

---

## What Atlas Does Autonomously

| Action | Agent | Condition |
|---|---|---|
| Decompose directive into tasks | PlannerAgent | New ACTIVE directive with no tasks |
| Route task to best model | ExecutionAgent + Governor | Task claimed from queue |
| Block dangerous payloads | GovernanceAgent | Pattern match on QUEUED task payload |
| Retry failed tasks | RecoveryAgent | state=FAILED, retry_count < 3 |
| Move to DLQ | RecoveryAgent | state=FAILED, retry_count >= 3 |
| Re-queue stuck RUNNING tasks | VerifierAgent + RecoveryAgent | last_updated > threshold |
| Mark stalled workers | RecoveryAgent | heartbeat > 30s stale |
| Publish telemetry | DiagnosticsAgent | Every 15s |
| Take memory snapshots | All agents (BaseAgent) | Every 5 min per agent |
| Flag budget overruns | GovernanceAgent | day_cost > 80% of budget |
| Flag high failure rate | GovernanceAgent | >50% tasks failed |

## Hard Boundaries (what Atlas does NOT do autonomously)

| Boundary | Enforcement |
|---|---|
| Cannot modify governance rules | GovernanceAgent is read-only of config |
| Cannot delete event_ledger rows | append-only by convention; no DELETE path |
| Cannot exceed MAX_RETRIES | hard-coded at 3; DLQ is final state |
| Cannot execute tasks outside permission_boundary | permission_boundary field on each worker |
| Dangerous payloads are blocked, not modified | state → BLOCKED; requires human review |
| DLQ tasks require human intervention to clear | no auto-DLQ-retry agent |

## Human Escalation Points

- `GOVERNANCE_HIGH_FAILURE_RATE` — human should investigate upstream failure cause
- `GOVERNANCE_LOW_WORKER_REDUNDANCY` — human should start additional execution agents
- `GOVERNANCE_BUDGET_WARNING` — human should review cost or raise budget
- `TASK_BLOCKED` — human must review blocked task payload before releasing
- `DLQ` state — human must diagnose why task exceeded retry limit
