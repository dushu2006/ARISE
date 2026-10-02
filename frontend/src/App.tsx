import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  BadgeCheck,
  Check,
  ChevronRight,
  CircleHelp,
  Clock3,
  Command,
  Cpu,
  Database,
  FileCheck2,
  HeartPulse,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  MessageSquareText,
  Monitor,
  RefreshCw,
  SendHorizontal,
  Shield,
  ShieldAlert,
  Sparkles,
  Square,
  Wifi,
  X,
  Zap,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { AriseApi, connectProtocol, resolveApiToken } from './api';
import type {
  Capability,
  EventRecord,
  HealthSnapshot,
  TaskDetail,
  TaskSnapshot,
} from './types';

type Page = 'overview' | 'tasks' | 'capabilities';

const STATE_LABELS: Record<string, string> = {
  created: 'Created',
  queued: 'Queued',
  understanding: 'Understanding',
  planning: 'Planning',
  ready: 'Ready',
  running: 'Running',
  waiting: 'Waiting',
  waiting_model: 'Waiting for model',
  waiting_resource: 'Waiting for resource',
  waiting_user: 'Needs approval',
  waiting_auth: 'Needs authorization',
  requires_user_input: 'Needs your input',
  verifying: 'Verifying',
  recovering: 'Recovering',
  interrupted: 'Interrupted',
  partially_completed: 'Partially complete',
  unknown: 'Outcome unknown',
  failed: 'Failed',
  cancelled: 'Cancelled',
  blocked: 'Blocked',
  completed: 'Verified complete',
};

const isBusy = (state: string) =>
  ['queued', 'understanding', 'planning', 'ready', 'running', 'waiting_resource', 'verifying'].includes(state);

const relativeTime = (value: string) => {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)} hr ago`;
  return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};

const shortId = (value: string) => value.slice(0, 8).toUpperCase();

function App() {
  const [page, setPage] = useState<Page>('overview');
  const [api, setApi] = useState<AriseApi | null>(null);
  const [token, setToken] = useState('');
  const [authError, setAuthError] = useState('');
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [tasks, setTasks] = useState<TaskSnapshot[]>([]);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [connected, setConnected] = useState(false);
  const [booting, setBooting] = useState(true);
  const [busy, setBusy] = useState(false);
  const [composer, setComposer] = useState('');
  const [reply, setReply] = useState('');
  const [toast, setToast] = useState('');
  const [detailRefresh, setDetailRefresh] = useState(0);
  const lastSequence = useRef(0);
  const selectedTaskRef = useRef('');
  const toastTimer = useRef<number | undefined>(undefined);
  const pendingSubmission = useRef<{
    text: string;
    sessionId: string;
    requestId: string;
  } | null>(null);

  const notify = useCallback((message: string) => {
    setToast(message);
    if (toastTimer.current !== undefined) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(''), 3600);
  }, []);

  const refreshTasks = useCallback(async (client: AriseApi) => {
    try {
      const latest = await client.listTasks();
      setTasks(latest);
    } catch (error) {
      if (error instanceof Error) notify(error.message);
    }
  }, [notify]);

  const refreshDetail = useCallback(async (client: AriseApi, taskId: string) => {
    try {
      setDetail(await client.getTask(taskId));
    } catch (error) {
      if (error instanceof Error) notify(error.message);
    }
  }, [notify]);

  useEffect(() => {
    selectedTaskRef.current = selectedTaskId;
  }, [selectedTaskId]);

  useEffect(() => {
    let disposed = false;
    const bootstrap = async () => {
      try {
        const resolvedToken = await resolveApiToken();
        if (disposed) return;
        setToken(resolvedToken);
        const client = new AriseApi(resolvedToken);
        const [healthResult, capabilitiesResult, taskResult, sessionResult] = await Promise.all([
          client.health(),
          client.capabilities(),
          client.listTasks(),
          client.listSessions(),
        ]);
        if (disposed) return;
        const activeSession = sessionResult[0] ?? await client.createSession();
        setApi(client);
        setHealth(healthResult);
        setCapabilities(capabilitiesResult);
        setTasks(taskResult);
        setSessionId(activeSession.session_id);
        if (taskResult.length > 0) setSelectedTaskId(taskResult[0].task_id);
      } catch (error) {
        if (!disposed) setAuthError(error instanceof Error ? error.message : 'Unable to connect to ARISE.');
      } finally {
        if (!disposed) setBooting(false);
      }
    };
    void bootstrap();
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    if (!api || !token) return;
    const disconnect = connectProtocol(api, token, lastSequence.current, {
      onConnection: setConnected,
      onError: notify,
      onReplayReset: () => {
        lastSequence.current = 0;
        setEvents([]);
      },
      onFrame: (unknownFrame) => {
        const frame = unknownFrame as {
          type?: string;
          payload?: Record<string, unknown>;
        };
        const payload = frame.payload ?? {};
        if (frame.type === 'server.hello') {
          const reportedHealth = payload.health as HealthSnapshot | undefined;
          if (reportedHealth) setHealth(reportedHealth);
          void api.health().then(setHealth).catch(() => setConnected(false));
          void api.capabilities().then(setCapabilities).catch(() => undefined);
          void refreshTasks(api);
          if (selectedTaskRef.current) void refreshDetail(api, selectedTaskRef.current);
        }
        if (frame.type === 'event') {
          const event = payload.event as EventRecord | undefined;
          if (event) {
            const previousSequence = lastSequence.current;
            if (event.sequence && event.sequence > previousSequence + 1) {
              // A dropped/retention gap means event history is incomplete. Refresh
              // authoritative task state instead of deriving state from events.
              void refreshTasks(api);
              if (selectedTaskRef.current) void refreshDetail(api, selectedTaskRef.current);
              void api.health().then(setHealth).catch(() => setConnected(false));
            }
            if (event.sequence && event.sequence > previousSequence) {
              lastSequence.current = event.sequence;
            }
            setEvents((current) => {
              if (current.some((existing) => existing.event_id === event.event_id)) return current;
              return [...current, event].slice(-120);
            });
            if (event.task_id) {
              window.setTimeout(() => {
                void refreshTasks(api);
                if (event.task_id === selectedTaskRef.current) void refreshDetail(api, event.task_id);
              }, 150);
            }
          }
        }
        if (frame.type === 'task.accepted' || frame.type === 'task.updated') {
          const snapshot = payload.task as TaskSnapshot | undefined;
          if (snapshot) {
            setSelectedTaskId(snapshot.task_id);
            setTasks((current) => {
              const next = current.filter((task) => task.task_id !== snapshot.task_id);
              return [snapshot, ...next].slice(0, 100);
            });
            void refreshDetail(api, snapshot.task_id);
          }
        }
        if (frame.type === 'protocol.error') {
          const code = String(payload.code ?? 'PROTOCOL_ERROR');
          notify(code === 'TASK_QUEUE_FULL' ? 'The task queue is full. Try again shortly.' : `Backend: ${code}`);
        }
      },
    });
    const refreshTimer = window.setInterval(() => {
      void refreshTasks(api);
      if (selectedTaskRef.current) void refreshDetail(api, selectedTaskRef.current);
      void api.health().then(setHealth).catch(() => setConnected(false));
    }, 8000);
    return () => {
      disconnect();
      window.clearInterval(refreshTimer);
    };
  }, [api, token, refreshTasks, refreshDetail, notify]);

  useEffect(() => {
    if (!api || !selectedTaskId) {
      setDetail(null);
      return;
    }
    void refreshDetail(api, selectedTaskId);
  }, [api, selectedTaskId, detailRefresh, refreshDetail]);

  useEffect(() => () => {
    if (toastTimer.current !== undefined) window.clearTimeout(toastTimer.current);
  }, []);

  const sortedTasks = useMemo(
    () => [...tasks].sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
    [tasks],
  );
  const runningCount = sortedTasks.filter((task) => isBusy(task.state)).length;
  const completedCount = sortedTasks.filter((task) => task.state === 'completed').length;
  const availableCapabilities = capabilities.filter((item) => item.status === 'available').length;
  const latestEvents = events
    .filter((event) => !selectedTaskId || event.task_id === selectedTaskId)
    .slice(-5)
    .reverse();

  const submitTask = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!api || !sessionId || !composer.trim() || busy) return;
    const text = composer.trim();
    // Reuse the idempotency key after a lost response; admission may already be durable.
    const pending = pendingSubmission.current;
    const stableRequestId =
      pending?.text === text && pending.sessionId === sessionId
        ? pending.requestId
        : crypto.randomUUID();
    pendingSubmission.current = { text, sessionId, requestId: stableRequestId };
    setBusy(true);
    try {
      const submitted = await api.submitTask(text, sessionId, stableRequestId);
      pendingSubmission.current = null;
      setComposer('');
      setSelectedTaskId(submitted.task_id);
      setTasks((current) => [submitted, ...current.filter((task) => task.task_id !== submitted.task_id)]);
      notify('Request recorded. ARISE will only proceed when a configured plan and verified tool are available.');
      await refreshTasks(api);
      setDetailRefresh((value) => value + 1);
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Task could not be submitted.');
    } finally {
      setBusy(false);
    }
  };

  const cancelTask = async () => {
    if (!api || !selectedTaskId) return;
    try {
      await api.cancelTask(selectedTaskId);
      notify('Cancellation requested. The final state reflects whether dispatch had begun.');
      setDetailRefresh((value) => value + 1);
      await refreshTasks(api);
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Cancellation failed.');
    }
  };

  const approveTask = async (confirmationId: string) => {
    if (!api || !selectedTaskId) return;
    try {
      await api.approveTask(selectedTaskId, confirmationId);
      notify('Approval recorded for that exact action only.');
      setDetailRefresh((value) => value + 1);
      await refreshTasks(api);
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Approval could not be applied.');
    }
  };

  const respondToTask = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!api || !selectedTaskId || !reply.trim()) return;
    try {
      await api.respondToTask(selectedTaskId, reply.trim());
      setReply('');
      notify('Clarification received and queued for replanning.');
      setDetailRefresh((value) => value + 1);
      await refreshTasks(api);
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Clarification could not be submitted.');
    }
  };

  const contentTitle = page === 'overview' ? 'Overview' : page === 'tasks' ? 'Task history' : 'Capabilities';

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark"><span>A</span><i /></div>
          <div className="brand-copy"><strong>ARISE</strong><small>LOCAL CONTROL</small></div>
        </div>

        <div className="workspace-switcher">
          <div className="workspace-avatar"><Command size={15} /></div>
          <div><span>Personal workspace</span><small>Local instance</small></div>
          <span className="workspace-chevron"><ChevronRight size={14} /></span>
        </div>

        <div className="nav-label">WORKSPACE</div>
        <nav className="primary-nav" aria-label="Main navigation">
          <button className={page === 'overview' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('overview')}>
            <Layers3 size={17} /><span>Overview</span><kbd>⌘1</kbd>
          </button>
          <button className={page === 'tasks' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('tasks')}>
            <Activity size={17} /><span>Task history</span>{tasks.length > 0 && <b className="nav-count">{tasks.length}</b>}
          </button>
          <button className={page === 'capabilities' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('capabilities')}>
            <Cpu size={17} /><span>Capabilities</span><span className="nav-ping" />
          </button>
        </nav>

        <div className="sidebar-section-heading"><span>RECENT TASKS</span><button aria-label="Refresh task list" onClick={() => api && void refreshTasks(api)}><RefreshCw size={13} /></button></div>
        <div className="sidebar-task-list">
          {sortedTasks.slice(0, 5).map((task) => (
            <button
              key={task.task_id}
              className={selectedTaskId === task.task_id ? 'sidebar-task selected' : 'sidebar-task'}
              onClick={() => setSelectedTaskId(task.task_id)}
            >
              <TaskStateDot state={task.state} />
              <span className="sidebar-task-text">{task.goal || 'Untitled task'}</span>
            </button>
          ))}
          {sortedTasks.length === 0 && <div className="sidebar-empty">Your task history will appear here.</div>}
        </div>

        <div className="sidebar-footer">
          <div className="sidebar-foot-status"><span className={connected ? 'tiny-dot live' : 'tiny-dot'} />
            <span>{connected ? 'Backend connected' : 'Backend disconnected'}</span>
          </div>
          <div className="profile-row">
            <div className="profile-avatar">A</div>
            <div className="profile-meta"><strong>Local user</strong><small>Private workspace</small></div>
            <LockKeyhole size={15} className="profile-lock" />
          </div>
        </div>
      </aside>

      <main className="main-shell">
        <header className="topbar">
          <div className="breadcrumbs"><span>Workspace</span><ChevronRight size={14} /><strong>{contentTitle}</strong></div>
          <div className="topbar-actions">
            <span className="runtime-chip"><span className={connected ? 'tiny-dot live' : 'tiny-dot'} />{connected ? 'LOCAL RUNTIME ONLINE' : 'LOCAL RUNTIME'}</span>
            <button className="icon-button topbar-icon" title="Health status" onClick={() => setPage('capabilities')}>
              <HeartPulse size={17} />
              <span className={health?.status === 'healthy' ? 'health-indicator good' : 'health-indicator'} />
            </button>
            <div className="topbar-avatar">A</div>
          </div>
        </header>

        {booting ? (
          <div className="loading-state"><LoaderCircle className="spin" size={24} /><span>Connecting to your local runtime…</span></div>
        ) : authError ? (
          <div className="setup-state">
            <div className="setup-icon"><LockKeyhole size={23} /></div>
            <span className="eyebrow">LOCAL CONNECTION REQUIRED</span>
            <h1>Connect to your ARISE runtime</h1>
            <p>{authError}</p>
            <div className="setup-checks">
              <div><span className="check-number">01</span><span>Start the authenticated Python backend.</span></div>
              <div><span className="check-number">02</span><span>Open this UI inside Tauri, or configure <code>VITE_API_TOKEN</code> for local development.</span></div>
              <div><span className="check-number">03</span><span>Requests stay local until a model provider is explicitly configured.</span></div>
            </div>
            <button className="button secondary" onClick={() => window.location.reload()}><RefreshCw size={15} /> Try again</button>
          </div>
        ) : (
          <div className="content-layout">
            <section className="page-content">
              {page === 'overview' && (
                <OverviewPage
                  health={health}
                  tasks={sortedTasks}
                  runningCount={runningCount}
                  completedCount={completedCount}
                  availableCapabilities={availableCapabilities}
                  composer={composer}
                  setComposer={setComposer}
                  onSubmit={submitTask}
                  busy={busy}
                  connected={connected}
                  onOpenCapabilities={() => setPage('capabilities')}
                  onSelectTask={setSelectedTaskId}
                />
              )}
              {page === 'tasks' && (
                <TasksPage tasks={sortedTasks} selectedTaskId={selectedTaskId} onSelect={setSelectedTaskId} />
              )}
              {page === 'capabilities' && (
                <CapabilitiesPage health={health} capabilities={capabilities} onRefresh={() => api && void api.diagnostics().then((snapshot) => {
                  setCapabilities(snapshot.capabilities);
                }).catch((error: unknown) => notify(error instanceof Error ? error.message : 'Diagnostics unavailable.'))} />
              )}
            </section>

            <aside className="inspector-column">
              <TaskInspector
                detail={detail}
                events={latestEvents}
                connected={connected}
                onCancel={cancelTask}
                onApprove={approveTask}
                reply={reply}
                setReply={setReply}
                onRespond={respondToTask}
                onRefresh={() => setDetailRefresh((value) => value + 1)}
              />
            </aside>
          </div>
        )}
      </main>

      {toast && <div className="toast"><span className="toast-mark"><Check size={14} /></span>{toast}<button onClick={() => setToast('')} aria-label="Dismiss"><X size={14} /></button></div>}
    </div>
  );
}

interface OverviewPageProps {
  health: HealthSnapshot | null;
  tasks: TaskSnapshot[];
  runningCount: number;
  completedCount: number;
  availableCapabilities: number;
  composer: string;
  setComposer: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  busy: boolean;
  connected: boolean;
  onOpenCapabilities: () => void;
  onSelectTask: (taskId: string) => void;
}

function OverviewPage(props: OverviewPageProps) {
  const { health, tasks, runningCount, completedCount, availableCapabilities } = props;
  const hour = new Date().getHours();
  const greeting = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  const newest = tasks.slice(0, 4);
  const planningUnavailable = health?.model_status !== 'available';

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow"><span className="eyebrow-line" />YOUR LOCAL WORKSPACE</div>
          <h1>{greeting}<span className="heading-period">.</span></h1>
          <p className="page-subtitle">A grounded view of what ARISE can safely do right now.</p>
        </div>
        <div className="date-chip"><span className="date-dot" />{new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}</div>
      </div>

      <section className="hero-card">
        <div className="hero-grid-glow" />
        <div className="hero-orbit orbit-one" /><div className="hero-orbit orbit-two" />
        <div className="hero-topline"><div className="hero-icon"><Sparkles size={18} /></div><span>ARISE ASSISTANT</span><div className="hero-live"><span />PHASE 1</div></div>
        <div className="hero-content">
          <h2>What would you like<br />to <em>work on?</em></h2>
          <p>Describe an intention. The local runtime will create a task record and only proceed when planning, policy, and a real execution capability are available.</p>
          <form className="task-composer" onSubmit={props.onSubmit}>
            <MessageSquareText size={17} className="composer-icon" />
            <input
              value={props.composer}
              onChange={(event) => props.setComposer(event.target.value)}
              placeholder="Describe a task for ARISE…"
              maxLength={16_384}
              aria-label="Describe a task"
            />
            <span className="composer-shortcut">↵</span>
            <button className="composer-send" type="submit" disabled={!props.composer.trim() || props.busy} aria-label="Submit task">
              {props.busy ? <LoaderCircle className="spin" size={16} /> : <SendHorizontal size={16} />}
            </button>
          </form>
          <div className="hero-footnote"><Shield size={13} /><span>Task recording is available. Live desktop actions are not enabled in this build.</span></div>
        </div>
        <div className="hero-art" aria-hidden="true">
          <div className="art-ring ring-outer" /><div className="art-ring ring-mid" /><div className="art-ring ring-inner" />
          <div className="art-core"><span>A</span><i /></div>
          <div className="art-node node-one"><Activity size={13} /></div>
          <div className="art-node node-two"><Shield size={13} /></div>
          <div className="art-node node-three"><Zap size={13} /></div>
          <span className="art-axis axis-x" /><span className="art-axis axis-y" />
        </div>
      </section>

      {planningUnavailable && (
        <section className="notice-card">
          <div className="notice-symbol"><AlertTriangle size={17} /></div>
          <div className="notice-copy"><strong>Planning is not configured</strong><span>No model provider or live desktop executor is active. New requests are recorded as tasks and will wait for configuration—nothing is simulated.</span></div>
          <button className="notice-link" onClick={props.onOpenCapabilities}>Review status <ArrowUpRight size={14} /></button>
        </section>
      )}

      <div className="section-title-row"><div><span className="eyebrow">AT A GLANCE</span><h2>Runtime snapshot</h2></div><span className="updated-tag"><span className="tiny-dot live" />{props.connected ? 'Live updates on' : 'Waiting for connection'}</span></div>
      <div className="metrics-grid">
        <MetricCard icon={<Activity size={17} />} label="ACTIVE TASKS" value={String(runningCount).padStart(2, '0')} note="Queued or in progress" tone="lavender" arrow={<ArrowUpRight size={13} />} />
        <MetricCard icon={<BadgeCheck size={17} />} label="VERIFIED COMPLETE" value={String(completedCount).padStart(2, '0')} note="Evidence-backed outcomes" tone="green" arrow={<Check size={13} />} />
        <MetricCard icon={<Cpu size={17} />} label="AVAILABLE CAPABILITIES" value={String(availableCapabilities).padStart(2, '0')} note="Explicitly reported by runtime" tone="blue" arrow={<ArrowDownRight size={13} />} />
      </div>

      <div className="section-title-row recent-title"><div><span className="eyebrow">WORK QUEUE</span><h2>Recent tasks <span className="muted-count">{tasks.length}</span></h2></div><span className="quiet-label">SORTED BY RECENT ACTIVITY</span></div>
      <div className="recent-tasks-card">
        {newest.length === 0 ? (
          <div className="empty-queue"><div className="empty-icon"><Layers3 size={19} /></div><strong>No tasks yet</strong><span>Start with an intention above. The backend remains the source of truth for every state.</span></div>
        ) : newest.map((task) => <TaskRow key={task.task_id} task={task} onClick={() => props.onSelectTask(task.task_id)} />)}
      </div>
      <div className="overview-footer"><span><Database size={13} /> Local SQLite persistence</span><span><Shield size={13} /> Policy-gated runtime</span><span><Wifi size={13} /> Authenticated control API</span></div>
    </>
  );
}

function MetricCard({ icon, label, value, note, tone, arrow }: { icon: ReactNode; label: string; value: string; note: string; tone: string; arrow: ReactNode }) {
  return (
    <div className="metric-card">
      <div className={`metric-icon ${tone}`}>{icon}</div>
      <div className="metric-label">{label}</div>
      <div className="metric-value-row"><strong>{value}</strong><span className={`metric-arrow ${tone}`}>{arrow}</span></div>
      <div className="metric-note">{note}</div>
    </div>
  );
}

function TaskRow({ task, onClick }: { task: TaskSnapshot; onClick: () => void }) {
  return (
    <button className="task-row" onClick={onClick}>
      <div className="task-row-state"><TaskStateDot state={task.state} /></div>
      <div className="task-row-main"><strong>{task.goal || 'Untitled task'}</strong><span>#{shortId(task.task_id)} <b>·</b> {task.steps.length} {task.steps.length === 1 ? 'step' : 'steps'}</span></div>
      <span className={`status-pill ${task.state}`}><span />{STATE_LABELS[task.state] ?? task.state}</span>
      <span className="task-row-time"><Clock3 size={13} />{relativeTime(task.updated_at)}</span>
      <ChevronRight className="task-row-chevron" size={16} />
    </button>
  );
}

function TasksPage({ tasks, selectedTaskId, onSelect }: { tasks: TaskSnapshot[]; selectedTaskId: string; onSelect: (id: string) => void }) {
  const states = ['all', 'running', 'completed', 'requires_user_input', 'failed'] as const;
  const [filter, setFilter] = useState<(typeof states)[number]>('all');
  const visibleTasks = filter === 'all' ? tasks : tasks.filter((task) => task.state === filter);
  return (
    <>
      <div className="page-heading compact-heading"><div><div className="eyebrow"><span className="eyebrow-line" />AUDITABLE WORK</div><h1>Task history<span className="heading-period">.</span></h1><p className="page-subtitle">Backend-owned state, step outcomes, and evidence status.</p></div><div className="history-counter"><strong>{tasks.length.toString().padStart(2, '0')}</strong><span>TOTAL TASKS</span></div></div>
      <div className="filter-bar">
        {states.map((state) => <button key={state} className={filter === state ? 'filter-chip active' : 'filter-chip'} onClick={() => setFilter(state)}>{state === 'all' ? 'All tasks' : STATE_LABELS[state] ?? state.replaceAll('_', ' ')}</button>)}
        <span className="filter-spacer" /><span className="filter-caption">{visibleTasks.length} RESULTS</span>
      </div>
      <div className="task-history-list">
        {visibleTasks.length === 0 ? <div className="empty-queue history-empty"><div className="empty-icon"><Layers3 size={19} /></div><strong>No matching tasks</strong><span>Tasks appear here as the backend accepts them.</span></div> : visibleTasks.map((task) => <TaskRow key={task.task_id} task={task} onClick={() => onSelect(task.task_id)} />)}
      </div>
      <div className="history-footnote"><ShieldAlert size={15} /><span>Unknown, interrupted, or blocked outcomes are not treated as success. Reconcile them before creating a follow-up action.</span></div>
      {selectedTaskId && <div className="selected-task-note">Selected task <code>#{shortId(selectedTaskId)}</code> in the inspector.</div>}
    </>
  );
}

function CapabilitiesPage({ health, capabilities, onRefresh }: { health: HealthSnapshot | null; capabilities: Capability[]; onRefresh: () => void }) {
  const groups = [
    { title: 'CORE RUNTIME', names: ['task.orchestration', 'storage.sqlite', 'model.planning'] },
    { title: 'DESKTOP & BROWSER', names: ['desktop.ui_automation', 'browser.dom', 'vision.ocr'] },
    { title: 'FUTURE ADAPTERS', names: ['voice.asr', 'voice.tts', 'memory.semantic', 'web.research'] },
  ];
  const map = new Map(capabilities.map((capability) => [capability.name, capability]));
  const environment = health;
  return (
    <>
      <div className="page-heading compact-heading"><div><div className="eyebrow"><span className="eyebrow-line" />HONEST BY DESIGN</div><h1>Capabilities<span className="heading-period">.</span></h1><p className="page-subtitle">Only adapters that report real availability are shown as ready.</p></div><button className="button secondary small" onClick={onRefresh}><RefreshCw size={14} /> Refresh</button></div>
      <div className={`system-health-card ${health?.status ?? 'degraded'}`}>
        <div className="system-health-icon"><HeartPulse size={19} /></div><div className="system-health-copy"><span className="eyebrow">SYSTEM HEALTH</span><strong>{health?.status === 'healthy' ? 'Runtime operational' : health?.status === 'unavailable' ? 'Runtime unavailable' : 'Runtime degraded'}</strong><span>{health?.degraded_reasons?.join(' · ') || 'Local API and persistence are responding.'}</span></div><div className="health-orb"><span /></div>
      </div>
      <div className="capability-groups">
        {groups.map((group) => <section className="capability-group" key={group.title}><div className="capability-group-title">{group.title}<span>{group.names.length.toString().padStart(2, '0')}</span></div>{group.names.map((name) => <CapabilityCard key={name} capability={map.get(name)} name={name} />)}</section>)}
      </div>
      <div className="diagnostics-strip"><div><Database size={15} /><span>Persistence</span><strong>{environment?.database_status ?? 'unknown'}</strong></div><div><Cpu size={15} /><span>Model planning</span><strong>{environment?.model_status ?? 'unknown'}</strong></div><div><Activity size={15} /><span>Task queue</span><strong>{environment?.queued_tasks ?? 0} queued</strong></div></div>
      <div className="capability-note"><CircleHelp size={15} /><span>Windows UI Automation, OCR, voice, semantic memory, and web research remain unavailable. The isolated Playwright adapter is experimental, not registered by the API, and is therefore not advertised as a live capability.</span></div>
    </>
  );
}

function CapabilityCard({ capability, name }: { capability?: Capability; name: string }) {
  const displayName = name.split('.').slice(1).join(' ').replaceAll('_', ' ');
  const Icon = name.includes('storage') ? Database : name.includes('desktop') ? Monitor : name.includes('voice') ? MessageSquareText : name.includes('web') ? Wifi : name.includes('model') ? Cpu : Shield;
  const status = capability?.status ?? 'unavailable';
  return (
    <article className="capability-card">
      <div className={`capability-icon ${status}`}><Icon size={17} /></div>
      <div className="capability-copy"><strong>{displayName}</strong><span>{capability?.limitations?.[0] ?? 'Capability details are unavailable.'}</span>{capability?.requirements?.length ? <small>Requires {capability.requirements.join(' · ')}</small> : null}</div>
      <span className={`capability-status ${status}`}><i />{status.replaceAll('_', ' ')}</span>
    </article>
  );
}

function TaskInspector({ detail, events, connected, onCancel, onApprove, reply, setReply, onRespond, onRefresh }: {
  detail: TaskDetail | null;
  events: EventRecord[];
  connected: boolean;
  onCancel: () => void;
  onApprove: (confirmationId: string) => void;
  reply: string;
  setReply: (value: string) => void;
  onRespond: (event: FormEvent<HTMLFormElement>) => void;
  onRefresh: () => void;
}) {
  const task = detail?.task;
  const cancellable = task && !['completed', 'failed', 'cancelled'].includes(task.state);
  const visibleEvents = events.slice(0, 5);

  return (
    <div className="inspector-card">
      <div className="inspector-header"><div><span className="eyebrow">LIVE INSPECTOR</span><h2>Task details</h2></div><button className="icon-button" title="Refresh task detail" onClick={onRefresh}><RefreshCw size={15} /></button></div>
      {!task ? (
        <div className="inspector-empty"><div className="empty-icon"><FileCheck2 size={18} /></div><strong>Select a task</strong><span>State transitions, confirmation scope, and event trace will appear here.</span></div>
      ) : (
        <>
          <div className="inspector-task-heading"><div className="inspector-task-id"><span className="tiny-dot live" />TASK #{shortId(task.task_id)}</div><span className={`status-pill ${task.state}`}><span />{STATE_LABELS[task.state] ?? task.state}</span></div>
          <h3 className="inspector-goal">{task.goal}</h3>
          {task.status_reason && <div className="task-reason"><span className="reason-bar" />{task.status_reason}</div>}
          <div className="detail-meta"><div><span>CREATED</span><strong>{new Date(task.created_at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</strong></div><div><span>VERSION</span><strong>v{task.version}</strong></div><div><span>STEPS</span><strong>{task.steps.length.toString().padStart(2, '0')}</strong></div></div>

          {detail.confirmations.length > 0 && <div className="confirmation-stack"><div className="inspector-section-title"><span>SCOPED APPROVAL</span><ShieldAlert size={14} /></div>{detail.confirmations.map((confirmation) => <div className="confirmation-card" key={confirmation.confirmation_id}><div className="confirmation-top"><LockKeyhole size={14} /><span>Risk R{confirmation.risk} · expires {new Date(confirmation.expires_at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}</span></div><strong>{confirmation.action_summary}</strong><span>Target: {confirmation.target_summary}</span><code>{confirmation.contract_fingerprint.slice(0, 16)}…</code><button className="button approve-button" onClick={() => onApprove(confirmation.confirmation_id)}>Approve this action <ArrowUpRight size={14} /></button></div>)}</div>}

          {detail.accepts_user_input && <form className="clarification-form" onSubmit={onRespond}><label htmlFor="clarification">CLARIFICATION REQUESTED</label><textarea id="clarification" value={reply} onChange={(event) => setReply(event.target.value)} placeholder="Answer the planner's question…" maxLength={16_384} rows={3} /><button className="button secondary small" disabled={!reply.trim()} type="submit"><SendHorizontal size={14} /> Send clarification</button></form>}

          <div className="inspector-section-title steps-title"><span>EXECUTION STEPS</span><span className="step-count">{task.steps.length.toString().padStart(2, '0')}</span></div>
          {task.steps.length === 0 ? <div className="no-steps"><span className="no-step-icon"><CircleHelp size={14} /></span><span>No executable steps are stored for this task.</span></div> : <div className="step-list">{task.steps.map((step, index) => <div className="step-item" key={step.action_id}><div className="step-track"><span className={`step-status-icon ${step.status}`}>{step.status === 'succeeded' ? <Check size={12} /> : step.status === 'running' || step.status === 'verifying' ? <LoaderCircle size={12} className="spin" /> : <span>{index + 1}</span>}</span>{index < task.steps.length - 1 && <i />}</div><div className="step-copy"><strong>{step.tool_name}</strong><span>{step.status_reason ?? step.status.replaceAll('_', ' ')}</span><small>Risk R{step.risk}{step.verification_status ? ` · verification ${step.verification_status}` : ''}</small></div></div>)}</div>}

          <div className="inspector-section-title trace-title"><span>EVENT TRACE</span><span className="trace-live"><i className={connected ? 'live' : ''} />{connected ? 'LIVE' : 'RECONNECTING'}</span></div>
          {visibleEvents.length === 0 ? <div className="trace-empty">No live events for the selected task yet.</div> : <div className="event-trace">{visibleEvents.map((event) => <div className="trace-row" key={event.event_id}><span className={`trace-marker ${event.severity}`} /><div><strong>{event.event_type.replaceAll('_', ' ').toLowerCase()}</strong><span>#{event.sequence ?? '—'} · {relativeTime(event.timestamp)}</span></div></div>)}</div>}

          {cancellable && <button className="button cancel-button" onClick={onCancel}><Square size={13} /> Cancel task</button>}
        </>
      )}
      <div className="inspector-footer"><span><span className={connected ? 'tiny-dot live' : 'tiny-dot'} />Event stream {connected ? 'connected' : 'disconnected'}</span><span>Protocol v1</span></div>
    </div>
  );
}

function TaskStateDot({ state }: { state: string }) {
  const kind = state === 'completed' ? 'done' : ['failed', 'blocked', 'unknown', 'interrupted'].includes(state) ? 'warning' : isBusy(state) ? 'active' : 'idle';
  return <span className={`task-state-dot ${kind}`} />;
}

export default App;
