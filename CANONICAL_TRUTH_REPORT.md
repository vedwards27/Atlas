# CANONICAL TRUTH REPORT — TIER 19
Generated: 2026-05-13

---

## Single Source of Truth

All Atlas state lives in one place: `live/atlas_runtime.db` (SQLite, WAL mode).

| Data | Location | Mutability |
|---|---|---|
| Workers | worker_registry | UPDATE allowed (heartbeat, state) |
| Tasks | task_queue | State machine: QUEUED→RUNNING→COMPLETED/FAILED/DLQ/BLOCKED |
| Events | event_ledger | APPEND ONLY — no UPDATE or DELETE |
| Directives | directive_registry | State machine: ACTIVE→COMPLETED |
| Decisions | decision_log | INSERT + UPDATE outcome only |
| Snapshots | memory_snapshots | APPEND ONLY |
| K-V State | kernel_state | UPSERT (governor_metrics, telemetry, etc.) |

## Enforcement Mechanisms

1. **WAL journal mode** — atomic writes, safe concurrent reads
2. **Threading lock on kernel._query** — no partial writes from concurrent threads
3. **Append-only event_ledger** — no code path calls DELETE or UPDATE on this table
4. **State machine transitions** — task state transitions are explicit UPDATE statements with WHERE guards
5. **Startup restore** — SurvivabilityEngine repairs any state drift from abrupt termination
6. **Duplicate detection** — GovernanceAgent + SurvivabilityEngine flag duplicate roles

## Lineage Verification

Every event and decision carries a `trace_id` linking it to its directive.
The `directive_registry.lineage_json` array records the full ancestor chain.
`memory.reconstruct_timeline(directive_id)` returns a complete, ordered,
auditable record of everything that happened under any directive.

## Fingerprint Monitoring

`SurvivabilityEngine.compute_state_fingerprint()` produces a 16-char SHA256
digest of all task states and worker states. Any unauthorised mutation changes
the fingerprint. This can be polled via `GET /api/survivability/fingerprint`.
