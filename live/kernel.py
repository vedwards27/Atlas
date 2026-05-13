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
