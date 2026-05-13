# EVENT LEDGER VALIDATION — TIER 16
Generated: 2026-05-13

---

## Schema

```sql
CREATE TABLE event_ledger (
    event_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME,
    type      TEXT,
    source    TEXT,
    payload   TEXT,   -- JSON
    trace_id  TEXT
);

CREATE VIRTUAL TABLE event_fts USING fts5(
    type, source, payload, trace_id,
    content=event_ledger,
    content_rowid=event_id
);
```

## Properties Verified

| Property | Status | Notes |
|---|---|---|
| Append-only writes | PASS | No UPDATE/DELETE on event_ledger |
| Autoincrement ID | PASS | Monotonically increasing event_id |
| JSON payload | PASS | All payloads are valid JSON |
| FTS5 index | PASS | Unique token search returns exact match |
| Trace ID linkage | PASS | Events link to directive/task via trace_id |
| WAL mode | PASS | PRAGMA journal_mode=WAL active |
| Concurrent read safety | PASS | Threading lock on all writes |

## Event Types Observed

| Type | Source | Description |
|---|---|---|
| MEMORY_HYDRATED | MEMORY_ENGINE | Emitted at each MemoryEngine init |
| DIRECTIVE_REGISTERED | KERNEL | New directive opened |
| DIRECTIVE_COMPLETED | KERNEL | Directive marked done |
| TASK_CREATED | KERNEL | Task added to queue |
| TASK_ASSIGNED | KERNEL | Task claimed by worker |
| TASK_COMPLETED | KERNEL | Task finished successfully |
| TASK_FAILED | KERNEL | Task failed with error |
| WORKER_REGISTERED | KERNEL | Worker joined registry |
| WORKER_TIMEOUT | KERNEL | Worker missed heartbeat |
| MEMORY_SNAPSHOT | KERNEL | Compression checkpoint taken |
| SYSTEM_START | SERVER | FastAPI lifespan begin |
| SYSTEM_STOP | SERVER | FastAPI lifespan end |
| DISPATCHER_START | DISPATCHER | Dispatcher process started |
| DISPATCHER_STOP | DISPATCHER | Dispatcher clean shutdown |

## FTS Rebuild

`kernel.rebuild_fts()` triggers a full index rebuild from source tables.
Called automatically after bulk inserts in tests.
