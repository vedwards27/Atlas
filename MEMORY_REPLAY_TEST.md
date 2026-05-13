# MEMORY REPLAY TEST — TIER 16
Generated: 2026-05-13  
Test suite: live/test_tier16.py

---

## Result: 25/25 PASSED

| # | Test | Result |
|---|---|---|
| T1.1 | directive survives restart | PASS |
| T1.2 | task survives restart | PASS |
| T1.3 | decision survives restart | PASS |
| T1.4 | snapshot survives restart | PASS |
| T1.5 | hydration state loaded on restart | PASS |
| T2.1 | parent appears in grandchild lineage | PASS |
| T2.2 | child appears in grandchild lineage | PASS |
| T2.3 | grandchild is current node | PASS |
| T2.4 | lineage depth is 3 | PASS |
| T2.5 | completed directive state persisted | PASS |
| T3.1 | decisions recorded | PASS |
| T3.2 | decision outcome updated | PASS |
| T3.3 | decision without outcome has None | PASS |
| T4.1 | timeline contains events | PASS |
| T4.2 | timeline contains decisions | PASS |
| T4.3 | timeline is reverse-chronological | PASS |
| T5.1 | FTS finds unique event token | PASS |
| T5.2 | FTS finds decision with 'llama' | PASS |
| T6.1 | snapshot has id | PASS |
| T6.2 | snapshot has event count | PASS |
| T6.3 | snapshot retrievable | PASS |
| T6.4 | get_latest_snapshot works | PASS |
| T7.1 | summary has all required keys | PASS |
| T7.2 | directive counts are integers | PASS |
| T7.3 | event total is integer | PASS |

---

## What Was Proven

**Restart persistence** — MemoryEngine was instantiated, data written, process
object deleted (simulating shutdown), then a fresh instance opened the same DB
and verified all records were intact.

**Directive lineage** — 3-level parent → child → grandchild hierarchy created.
`get_lineage(grandchild)` returned all 3 levels with correct ordering and
`is_current` flag on the terminal node.

**Decision history** — Two decisions recorded, one outcome resolved. Query by
directive_id returned both; resolved decision showed updated outcome.

**Timeline reconstruction** — Events and decisions merged and sorted in reverse
chronological order. Both kinds confirmed present.

**FTS search** — Unique token injected into event payload, FTS index rebuilt,
search confirmed exact match. Keyword search across decisions also confirmed.

**Memory snapshots** — Checkpoint created, retrieved by ID, confirmed as
latest snapshot. Snapshot includes event ranges and directive inventory.

**Operational summary** — All required summary keys present with correct types.
