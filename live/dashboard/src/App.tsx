import { useState, useEffect, useCallback } from 'react'
import {
  Shield, Database, Cpu, Activity, Zap, Send,
  CheckCircle, XCircle, Clock, Loader2,
} from 'lucide-react'

// ── Types ─────────────────────────────────────────────────────────────────────

type Status = {
  status: string; version: string; uptime_seconds: number
  workers: number
  tasks: { queued: number; running: number; completed: number; failed: number }
}
type Event = { id: number; timestamp: string; type: string; source: string; payload: Record<string, unknown> }
type Worker = { id: string; role: string; provider: string; scope: string; state: string; heartbeat: string }
type Task   = { id: string; directive: string; state: string; priority: number; payload: Record<string, unknown>; worker: string; created_at: string }
type Provider = { id: string; size: string; family: string; status: string }
type GovMetrics = {
  total_routed: number; total_cost: number; day_cost: number; daily_budget_cu: number
  by_provider: Record<string, { count: number; cost: number; avg_ms: number }>
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatUptime(s: number): string {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h ${m}m`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m ${s % 60}s`
}

const EVENT_COLOR: Record<string, string> = {
  SYSTEM_START:         'text-blue-400',
  SYSTEM_STOP:          'text-zinc-500',
  WORKER_REGISTERED:    'text-emerald-400',
  TASK_CREATED:         'text-cyan-400',
  TASK_ASSIGNED:        'text-yellow-400',
  TASK_COMPLETED:       'text-emerald-400',
  TASK_FAILED:          'text-red-400',
  WORKER_TIMEOUT:       'text-red-400',
  ORPHAN_TASK_RECOVERED:'text-orange-400',
  DISPATCHER_START:     'text-blue-300',
  DISPATCHER_STOP:      'text-zinc-500',
  KERNEL_SHUTDOWN:      'text-red-500',
}

const STATE_COLOR: Record<string, string> = {
  OPERATIONAL: 'text-emerald-400',
  ONLINE:      'text-emerald-400',
  ACTIVE:      'text-emerald-400',
  BUSY:        'text-yellow-400',
  IDLE:        'text-zinc-400',
  QUEUED:      'text-cyan-400',
  RUNNING:     'text-yellow-400',
  COMPLETED:   'text-emerald-400',
  FAILED:      'text-red-400',
  STALLED:     'text-red-400',
  OFFLINE:     'text-zinc-600',
}

function stateColor(s: string) { return STATE_COLOR[s] ?? 'text-zinc-400' }

// ── Sub-components ────────────────────────────────────────────────────────────

function Panel({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="bg-zinc-900/60 border border-zinc-800 rounded-sm p-5 space-y-4">
      <h2 className="flex items-center gap-2 text-[10px] font-black uppercase tracking-widest text-white">
        <span className="text-blue-400">{icon}</span>{title}
      </h2>
      {children}
    </section>
  )
}

function Stat({ label, value, color = 'text-white' }: { label: string; value: string | number; color?: string }) {
  return (
    <div className="flex justify-between items-center">
      <span className="text-zinc-500 text-[10px]">{label}</span>
      <span className={`text-xs font-bold ${color}`}>{value}</span>
    </div>
  )
}

function TaskStateIcon({ state }: { state: string }) {
  if (state === 'COMPLETED') return <CheckCircle className="w-3 h-3 text-emerald-400" />
  if (state === 'FAILED')    return <XCircle className="w-3 h-3 text-red-400" />
  if (state === 'RUNNING')   return <Loader2 className="w-3 h-3 text-yellow-400 animate-spin" />
  return <Clock className="w-3 h-3 text-cyan-400" />
}

// ── Main App ──────────────────────────────────────────────────────────────────

export default function App() {
  const [status,     setStatus]     = useState<Status | null>(null)
  const [events,     setEvents]     = useState<Event[]>([])
  const [workers,    setWorkers]    = useState<Worker[]>([])
  const [tasks,      setTasks]      = useState<Task[]>([])
  const [providers,  setProviders]  = useState<Provider[]>([])
  const [govMetrics, setGovMetrics] = useState<GovMetrics | null>(null)
  const [connected,  setConnected]  = useState(false)

  const [prompt,       setPrompt]       = useState('')
  const [taskType,     setTaskType]     = useState('general')
  const [submitting,   setSubmitting]   = useState(false)
  const [submitResult, setSubmitResult] = useState<{ ok: boolean; task_id?: string } | null>(null)

  const fetchAll = useCallback(async () => {
    try {
      const [s, e, w, t, p, g] = await Promise.all([
        fetch('/api/status').then(r => r.json()),
        fetch('/api/events?limit=30').then(r => r.json()),
        fetch('/api/workers').then(r => r.json()),
        fetch('/api/tasks?limit=15').then(r => r.json()),
        fetch('/api/providers').then(r => r.json()),
        fetch('/api/governor/metrics').then(r => r.json()),
      ])
      setStatus(s); setEvents(e); setWorkers(w); setTasks(t); setProviders(p); setGovMetrics(g)
      setConnected(true)
    } catch {
      setConnected(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const id = setInterval(fetchAll, 3000)
    return () => clearInterval(id)
  }, [fetchAll])

  async function submitTask() {
    if (!prompt.trim()) return
    setSubmitting(true)
    setSubmitResult(null)
    try {
      const res = await fetch('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directive_id: 'MANUAL', payload: { prompt, task_type: taskType }, priority: 1 }),
      })
      const data = await res.json()
      setSubmitResult({ ok: true, task_id: data.task_id })
      setPrompt('')
    } catch {
      setSubmitResult({ ok: false })
    }
    setSubmitting(false)
  }

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-200 font-mono text-xs">

      {/* ── Header ── */}
      <header className="sticky top-0 z-10 border-b border-zinc-800 bg-zinc-950/90 backdrop-blur px-8 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Shield className="w-5 h-5 text-blue-400" />
          <span className="text-sm font-black tracking-widest text-white uppercase">Atlas Runtime</span>
          <span className="text-zinc-700">v{status?.version ?? '...'}</span>
        </div>
        <div className="flex items-center gap-6 text-[10px]">
          {status && (
            <>
              <span>↑ {formatUptime(status.uptime_seconds)}</span>
              <span>Workers: <strong className="text-white">{status.workers}</strong></span>
            </>
          )}
          <span className={`flex items-center gap-1.5 font-bold ${connected ? 'text-emerald-400' : 'text-red-400'}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${connected ? 'bg-emerald-400 animate-pulse' : 'bg-red-400'}`} />
            {connected ? 'LIVE' : 'DISCONNECTED'}
          </span>
        </div>
      </header>

      {/* ── Task count bar ── */}
      {status && (
        <div className="border-b border-zinc-800/50 px-8 py-2 flex gap-6 text-[10px] text-zinc-500">
          <span>Queued <strong className="text-cyan-400">{status.tasks.queued}</strong></span>
          <span>Running <strong className="text-yellow-400">{status.tasks.running}</strong></span>
          <span>Completed <strong className="text-emerald-400">{status.tasks.completed}</strong></span>
          <span>Failed <strong className="text-red-400">{status.tasks.failed}</strong></span>
        </div>
      )}

      <div className="p-6 grid grid-cols-12 gap-5">

        {/* ── Left: Event Ledger ── */}
        <div className="col-span-12 lg:col-span-4 space-y-5">
          <Panel title="Event Ledger" icon={<Database className="w-3.5 h-3.5" />}>
            <div className="space-y-px max-h-[520px] overflow-y-auto pr-1">
              {events.length === 0 && <p className="text-zinc-600">No events yet.</p>}
              {events.map(e => (
                <div key={e.id} className="flex gap-2 py-1 border-b border-zinc-800/40 last:border-0">
                  <span className="text-zinc-600 shrink-0 tabular-nums">{e.timestamp.slice(11, 19)}</span>
                  <span className={`shrink-0 font-bold ${EVENT_COLOR[e.type] ?? 'text-zinc-400'}`}>{e.type}</span>
                </div>
              ))}
            </div>
          </Panel>
        </div>

        {/* ── Middle: Workers + Tasks ── */}
        <div className="col-span-12 lg:col-span-4 space-y-5">
          <Panel title="Workers" icon={<Cpu className="w-3.5 h-3.5" />}>
            {workers.length === 0
              ? <p className="text-zinc-600">No workers registered.</p>
              : workers.map(w => (
                  <div key={w.id} className="flex items-center justify-between py-1">
                    <div>
                      <div className="text-zinc-200">{w.id}</div>
                      <div className="text-zinc-600 text-[10px]">{w.role} · {w.provider}</div>
                    </div>
                    <span className={`font-bold ${stateColor(w.state)}`}>{w.state}</span>
                  </div>
                ))
            }
          </Panel>

          <Panel title="Task Queue" icon={<Activity className="w-3.5 h-3.5" />}>
            <div className="space-y-1 max-h-64 overflow-y-auto pr-1">
              {tasks.length === 0
                ? <p className="text-zinc-600">No tasks.</p>
                : tasks.map(t => (
                    <div key={t.id} className="flex items-center gap-2 py-1 border-b border-zinc-800/40 last:border-0">
                      <TaskStateIcon state={t.state} />
                      <span className="text-zinc-400 tabular-nums">{t.id}</span>
                      <span className="text-zinc-600 flex-1 truncate">{String(t.payload?.task_type ?? 'general')}</span>
                      <span className={`font-bold ${stateColor(t.state)}`}>{t.state}</span>
                    </div>
                  ))
              }
            </div>
          </Panel>
        </div>

        {/* ── Right: Providers + Governor + Submit ── */}
        <div className="col-span-12 lg:col-span-4 space-y-5">
          <Panel title="Provider Mesh (Ollama)" icon={<Zap className="w-3.5 h-3.5" />}>
            {providers.length === 0
              ? <p className="text-zinc-600">No providers online.</p>
              : providers.map(p => (
                  <div key={p.id} className="flex items-center justify-between py-0.5">
                    <div>
                      <span className="text-zinc-200">{p.id}</span>
                      <span className="text-zinc-600 ml-2">{p.size}</span>
                    </div>
                    <span className="text-emerald-400 font-bold">{p.status}</span>
                  </div>
                ))
            }
          </Panel>

          {govMetrics && (
            <Panel title="ComputeGovernor" icon={<Activity className="w-3.5 h-3.5" />}>
              <Stat label="Tasks Routed"  value={govMetrics.total_routed} />
              <Stat label="Total Cost (CU)" value={govMetrics.total_cost.toFixed(2)} />
              <Stat label="Today's Cost"  value={govMetrics.day_cost.toFixed(2)} />
              {Object.keys(govMetrics.by_provider).length > 0 && (
                <div className="border-t border-zinc-800 pt-3 space-y-1">
                  {Object.entries(govMetrics.by_provider).map(([model, m]) => (
                    <div key={model} className="flex justify-between text-[10px]">
                      <span className="text-zinc-500 truncate max-w-[140px]">{model}</span>
                      <span className="text-zinc-400 ml-2">{m.count}× · {m.avg_ms}ms avg</span>
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          )}

          <Panel title="Dispatch Task" icon={<Send className="w-3.5 h-3.5" />}>
            <select
              value={taskType}
              onChange={e => setTaskType(e.target.value)}
              className="w-full bg-zinc-800 border border-zinc-700 rounded-sm px-2 py-1.5 text-zinc-200 text-xs focus:outline-none focus:border-blue-500"
            >
              <option value="general">General</option>
              <option value="coding">Coding</option>
              <option value="reasoning">Reasoning</option>
              <option value="fast">Fast (1B)</option>
            </select>
            <textarea
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submitTask() }}
              placeholder="Enter a prompt… (Ctrl+Enter to send)"
              rows={4}
              className="w-full bg-zinc-800 border border-zinc-700 rounded-sm px-2 py-1.5 text-zinc-200 text-xs resize-none focus:outline-none focus:border-blue-500"
            />
            <button
              onClick={submitTask}
              disabled={submitting || !prompt.trim()}
              className="w-full bg-blue-600 hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed text-white rounded-sm px-3 py-2 text-xs font-black uppercase tracking-widest transition-colors"
            >
              {submitting ? 'Dispatching…' : 'Dispatch →'}
            </button>
            {submitResult && (
              <p className={`text-[10px] ${submitResult.ok ? 'text-emerald-400' : 'text-red-400'}`}>
                {submitResult.ok ? `Queued: ${submitResult.task_id}` : 'Submit failed — is the server running?'}
              </p>
            )}
          </Panel>
        </div>

      </div>
    </div>
  )
}
