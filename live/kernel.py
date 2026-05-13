import sqlite3
import json
import uuid
import threading
from datetime import datetime, timedelta

class AtlasRuntimeKernel:
    def __init__(self, db_path="atlas_runtime.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self.initialize_db()

    def initialize_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        c = conn.cursor()
        c.executescript('''
            CREATE TABLE IF NOT EXISTS worker_registry (
                worker_id TEXT PRIMARY KEY,
                role TEXT,
                provider_type TEXT,
                execution_scope TEXT,
                permission_boundary TEXT,
                runtime_state TEXT,
                last_heartbeat DATETIME,
                replay_lineage TEXT
            );
            CREATE TABLE IF NOT EXISTS task_queue (
                task_id TEXT PRIMARY KEY,
                directive_id TEXT,
                state TEXT,
                priority INTEGER,
                payload TEXT,
                worker_id TEXT,
                retry_count INTEGER DEFAULT 0,
                created_at DATETIME,
                last_updated DATETIME,
                checkpoint_id TEXT
            );
            CREATE TABLE IF NOT EXISTS event_ledger (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                type TEXT,
                source TEXT,
                payload TEXT,
                trace_id TEXT
            );
            CREATE TABLE IF NOT EXISTS kernel_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                last_updated DATETIME
            );
            CREATE TABLE IF NOT EXISTS directive_registry (
                directive_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                state TEXT DEFAULT "ACTIVE",
                tier INTEGER,
                parent_id TEXT,
                created_at DATETIME,
                completed_at DATETIME,
                outcome TEXT,
                lineage_json TEXT DEFAULT "[]"
            );
            CREATE TABLE IF NOT EXISTS decision_log (
                decision_id TEXT PRIMARY KEY,
                timestamp DATETIME,
                directive_id TEXT,
                worker_id TEXT,
                context TEXT,
                decision TEXT,
                rationale TEXT,
                outcome TEXT,
                trace_id TEXT
            );
            CREATE TABLE IF NOT EXISTS memory_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                created_at DATETIME,
                scope TEXT,
                event_id_from INTEGER,
                event_id_to INTEGER,
                decision_count INTEGER,
                compressed_data TEXT
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS event_fts USING fts5(
                type, source, payload, trace_id,
                content=event_ledger,
                content_rowid=event_id
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS decision_fts USING fts5(
                context, decision, rationale, outcome,
                content=decision_log,
                content_rowid=rowid
            );
        ''')
        conn.commit()
        conn.close()

    def _query(self, query, params=(), commit=False):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(query, params)
            res = c.fetchall()
            if commit:
                conn.commit()
            conn.close()
            return [tuple(r) for r in res]

    def log_event(self, event_type, source, payload, trace_id=None):
        self._query(
            'INSERT INTO event_ledger (timestamp, type, source, payload, trace_id) VALUES (?, ?, ?, ?, ?)',
            (datetime.now().isoformat(), event_type, source, json.dumps(payload), trace_id),
            commit=True
        )

    def register_worker(self, worker_id, role, provider, scope, boundary):
        ts = datetime.now().isoformat()
        self._query(
            'INSERT OR REPLACE INTO worker_registry (worker_id, role, provider_type, execution_scope, permission_boundary, runtime_state, last_heartbeat) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (worker_id, role, provider, scope, boundary, "ONLINE", ts),
            commit=True
        )
        self.log_event("WORKER_REGISTERED", "KERNEL", {"worker_id": worker_id, "role": role})

    def update_worker_heartbeat(self, worker_id):
        self._query(
            'UPDATE worker_registry SET last_heartbeat = ?, runtime_state = "ACTIVE" WHERE worker_id = ?',
            (datetime.now().isoformat(), worker_id),
            commit=True
        )

    def set_worker_state(self, worker_id, state):
        self._query(
            'UPDATE worker_registry SET runtime_state = ? WHERE worker_id = ?',
            (state, worker_id),
            commit=True
        )

    def add_task(self, directive_id, payload, priority=1):
        task_id = f"TSK-{uuid.uuid4().hex[:8].upper()}"
        ts = datetime.now().isoformat()
        self._query(
            'INSERT INTO task_queue (task_id, directive_id, state, priority, payload, created_at, last_updated) VALUES (?, ?, "QUEUED", ?, ?, ?, ?)',
            (task_id, directive_id, priority, json.dumps(payload), ts, ts),
            commit=True
        )
        self.log_event("TASK_CREATED", "KERNEL", {"task_id": task_id, "directive_id": directive_id}, trace_id=directive_id)
        return task_id

    def claim_task(self, worker_id):
        res = self._query('SELECT task_id, payload FROM task_queue WHERE state = "QUEUED" ORDER BY priority DESC, created_at ASC LIMIT 1')
        if not res:
            return None, None
        task_id, payload = res[0]
        ts = datetime.now().isoformat()
        self._query('UPDATE task_queue SET state = "RUNNING", worker_id = ?, last_updated = ? WHERE task_id = ?', (worker_id, ts, task_id), commit=True)
        self.log_event("TASK_ASSIGNED", "KERNEL", {"task_id": task_id, "worker_id": worker_id})
        return task_id, json.loads(payload)

    def complete_task(self, task_id, result):
        self._query('UPDATE task_queue SET state = "COMPLETED", last_updated = ? WHERE task_id = ?', (datetime.now().isoformat(), task_id), commit=True)
        self.log_event("TASK_COMPLETED", "KERNEL", {"task_id": task_id, "result": result})

    def fail_task(self, task_id, error):
        self._query('UPDATE task_queue SET state = "FAILED", last_updated = ? WHERE task_id = ?', (datetime.now().isoformat(), task_id), commit=True)
        self.log_event("TASK_FAILED", "KERNEL", {"task_id": task_id, "error": str(error)})

    def detect_timeouts(self, timeout_seconds=60):
        ts_limit = (datetime.now() - timedelta(seconds=timeout_seconds)).isoformat()
        stalled = self._query('SELECT worker_id FROM worker_registry WHERE last_heartbeat < ? AND runtime_state NOT IN ("OFFLINE", "STALLED")', (ts_limit,))
        for (w_id,) in stalled:
            self._query('UPDATE worker_registry SET runtime_state = "STALLED" WHERE worker_id = ?', (w_id,), commit=True)
            self._query('UPDATE task_queue SET state = "QUEUED", worker_id = NULL WHERE worker_id = ? AND state = "RUNNING"', (w_id,), commit=True)
            self.log_event("WORKER_TIMEOUT", "KERNEL", {"worker_id": w_id})

    def set_state(self, key, value):
        self._query(
            'INSERT OR REPLACE INTO kernel_state (key, value, last_updated) VALUES (?, ?, ?)',
            (key, json.dumps(value), datetime.now().isoformat()),
            commit=True
        )

    def get_state(self, key, default=None):
        res = self._query('SELECT value FROM kernel_state WHERE key = ?', (key,))
        return json.loads(res[0][0]) if res else default

    # ── Directive registry ────────────────────────────────────────────────────

    def register_directive(self, name, description, tier=None, parent_id=None):
        directive_id = f"DIR-{uuid.uuid4().hex[:8].upper()}"
        ts = datetime.now().isoformat()
        lineage = []
        if parent_id:
            row = self._query('SELECT lineage_json FROM directive_registry WHERE directive_id = ?', (parent_id,))
            if row:
                lineage = json.loads(row[0][0]) + [parent_id]
        self._query(
            'INSERT INTO directive_registry (directive_id, name, description, state, tier, parent_id, created_at, lineage_json) VALUES (?, ?, ?, "ACTIVE", ?, ?, ?, ?)',
            (directive_id, name, description, tier, parent_id, ts, json.dumps(lineage)),
            commit=True
        )
        self.log_event("DIRECTIVE_REGISTERED", "KERNEL", {"directive_id": directive_id, "name": name, "tier": tier}, trace_id=directive_id)
        return directive_id

    def complete_directive(self, directive_id, outcome="SUCCESS"):
        ts = datetime.now().isoformat()
        self._query(
            'UPDATE directive_registry SET state = "COMPLETED", completed_at = ?, outcome = ? WHERE directive_id = ?',
            (ts, outcome, directive_id),
            commit=True
        )
        self.log_event("DIRECTIVE_COMPLETED", "KERNEL", {"directive_id": directive_id, "outcome": outcome}, trace_id=directive_id)

    def get_directives(self, state=None):
        if state:
            return self._query('SELECT directive_id, name, description, state, tier, parent_id, created_at, completed_at, outcome, lineage_json FROM directive_registry WHERE state = ? ORDER BY created_at DESC', (state,))
        return self._query('SELECT directive_id, name, description, state, tier, parent_id, created_at, completed_at, outcome, lineage_json FROM directive_registry ORDER BY created_at DESC')

    # ── Decision log ──────────────────────────────────────────────────────────

    def log_decision(self, directive_id, worker_id, context, decision, rationale, trace_id=None):
        decision_id = f"DEC-{uuid.uuid4().hex[:8].upper()}"
        ts = datetime.now().isoformat()
        self._query(
            'INSERT INTO decision_log (decision_id, timestamp, directive_id, worker_id, context, decision, rationale, trace_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (decision_id, ts, directive_id, worker_id, context, decision, rationale, trace_id),
            commit=True
        )
        # Keep FTS index in sync
        self._query(
            'INSERT INTO decision_fts (rowid, context, decision, rationale, outcome) SELECT rowid, context, decision, rationale, outcome FROM decision_log WHERE decision_id = ?',
            (decision_id,),
            commit=True
        )
        return decision_id

    def update_decision_outcome(self, decision_id, outcome):
        self._query('UPDATE decision_log SET outcome = ? WHERE decision_id = ?', (outcome, decision_id), commit=True)

    def get_decisions(self, directive_id=None, limit=50):
        if directive_id:
            return self._query('SELECT decision_id, timestamp, directive_id, worker_id, context, decision, rationale, outcome, trace_id FROM decision_log WHERE directive_id = ? ORDER BY timestamp DESC LIMIT ?', (directive_id, limit))
        return self._query('SELECT decision_id, timestamp, directive_id, worker_id, context, decision, rationale, outcome, trace_id FROM decision_log ORDER BY timestamp DESC LIMIT ?', (limit,))

    # ── Memory snapshots ──────────────────────────────────────────────────────

    def create_snapshot(self, scope="global"):
        snapshot_id = f"SNAP-{uuid.uuid4().hex[:8].upper()}"
        ts = datetime.now().isoformat()
        event_range = self._query('SELECT MIN(event_id), MAX(event_id), COUNT(*) FROM event_ledger')
        id_from, id_to, total_events = event_range[0] if event_range else (0, 0, 0)
        decision_count = self._query('SELECT COUNT(*) FROM decision_log')[0][0]
        worker_count = self._query('SELECT COUNT(*) FROM worker_registry')[0][0]
        task_stats = self._query('SELECT state, COUNT(*) FROM task_queue GROUP BY state')
        compressed = {
            "scope": scope,
            "timestamp": ts,
            "total_events": total_events,
            "decision_count": decision_count,
            "worker_count": worker_count,
            "task_stats": {r[0]: r[1] for r in task_stats},
            "directives": [{"id": r[0], "name": r[1], "state": r[3]} for r in self.get_directives()],
        }
        self._query(
            'INSERT INTO memory_snapshots (snapshot_id, created_at, scope, event_id_from, event_id_to, decision_count, compressed_data) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (snapshot_id, ts, scope, id_from or 0, id_to or 0, decision_count, json.dumps(compressed)),
            commit=True
        )
        self.log_event("MEMORY_SNAPSHOT", "KERNEL", {"snapshot_id": snapshot_id, "scope": scope, "event_count": total_events})
        return snapshot_id, compressed

    def get_latest_snapshot(self):
        res = self._query('SELECT snapshot_id, created_at, scope, compressed_data FROM memory_snapshots ORDER BY created_at DESC LIMIT 1')
        if not res:
            return None
        sid, ts, scope, data = res[0]
        return {"snapshot_id": sid, "created_at": ts, "scope": scope, **json.loads(data)}

    def get_snapshots(self, limit=10):
        return self._query('SELECT snapshot_id, created_at, scope, event_id_from, event_id_to, decision_count FROM memory_snapshots ORDER BY created_at DESC LIMIT ?', (limit,))

    # ── FTS search ────────────────────────────────────────────────────────────

    def search_events(self, query, limit=20):
        try:
            res = self._query(
                'SELECT e.event_id, e.timestamp, e.type, e.source, e.payload, e.trace_id FROM event_ledger e JOIN event_fts f ON e.event_id = f.rowid WHERE event_fts MATCH ? ORDER BY e.event_id DESC LIMIT ?',
                (query, limit)
            )
        except Exception:
            # FTS index may be stale — fall back to LIKE
            res = self._query(
                'SELECT event_id, timestamp, type, source, payload, trace_id FROM event_ledger WHERE type LIKE ? OR source LIKE ? OR payload LIKE ? ORDER BY event_id DESC LIMIT ?',
                (f'%{query}%', f'%{query}%', f'%{query}%', limit)
            )
        return res

    def search_decisions(self, query, limit=20):
        try:
            res = self._query(
                'SELECT d.decision_id, d.timestamp, d.directive_id, d.worker_id, d.context, d.decision, d.rationale, d.outcome FROM decision_log d JOIN decision_fts f ON d.rowid = f.rowid WHERE decision_fts MATCH ? ORDER BY d.timestamp DESC LIMIT ?',
                (query, limit)
            )
        except Exception:
            res = self._query(
                'SELECT decision_id, timestamp, directive_id, worker_id, context, decision, rationale, outcome FROM decision_log WHERE context LIKE ? OR decision LIKE ? OR rationale LIKE ? ORDER BY timestamp DESC LIMIT ?',
                (f'%{query}%', f'%{query}%', f'%{query}%', limit)
            )
        return res

    def rebuild_fts(self):
        self._query("INSERT INTO event_fts(event_fts) VALUES('rebuild')", commit=True)
        self._query("INSERT INTO decision_fts(decision_fts) VALUES('rebuild')", commit=True)
