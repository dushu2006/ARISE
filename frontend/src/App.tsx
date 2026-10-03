import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  BadgeCheck,
  Brain,
  Check,
  ChevronRight,
  CircleHelp,
  Clock3,
  Command,
  Cpu,
  Database,
  Download,
  FileCheck2,
  Globe2,
  HeartPulse,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  MessageSquareText,
  Mic,
  Monitor,
  Radio,
  RefreshCw,
  Search,
  SendHorizontal,
  Shield,
  ShieldAlert,
  Sparkles,
  Square,
  Trash2,
  Wifi,
  X,
  Zap,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { AriseApi, connectProtocol, resolveApiToken } from './api';
import type {
  Capability,
  DiagnosticsSnapshot,
  EventRecord,
  HealthSnapshot,
  MemoryKind,
  MemoryRecord,
  MemoryWriteDraft,
  TaskDetail,
  TaskSnapshot,
  TextInteractionResponse,
  VoiceStatusSnapshot,
  WebResearchResponse,
} from './types';

type Page = 'overview' | 'tasks' | 'capabilities' | 'memory' | 'research';

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
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatusSnapshot | null>(null);
  const [diagnostics, setDiagnostics] = useState<DiagnosticsSnapshot | null>(null);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [tasks, setTasks] = useState<TaskSnapshot[]>([]);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [connected, setConnected] = useState(false);
  const [booting, setBooting] = useState(true);
  const [busy, setBusy] = useState(false);
  const [voiceBusy, setVoiceBusy] = useState(false);
  const [composer, setComposer] = useState('');
  const [interaction, setInteraction] = useState<TextInteractionResponse | null>(null);
  const [allowWebResearch, setAllowWebResearch] = useState(false);
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
    allowWebResearch: boolean;
  } | null>(null);

  const notify = useCallback((message: string) => {
    setToast(message);
    if (toastTimer.current !== undefined) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(''), 3600);
  }, []);

  const controlVoice = useCallback(async (listen: boolean) => {
    if (!api || voiceBusy) return;
    setVoiceBusy(true);
    try {
      const snapshot = listen
        ? await api.startVoiceListening()
        : await api.stopVoiceListening();
      setVoiceStatus(snapshot);
      if (listen) {
        notify(snapshot.wake_word_enabled
          ? 'Local microphone and wake-word monitoring started.'
          : `Voice listening is not active (${snapshot.last_error_code ?? snapshot.microphone_status}).`);
      } else {
        notify('Microphone monitoring stopped. Any admitted task continues independently.');
      }
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Voice control is unavailable.');
    } finally {
      setVoiceBusy(false);
    }
  }, [api, notify, voiceBusy]);

  const refreshTasks = useCallback(async (client: AriseApi) => {
    try {
      const latest = await client.listTasks();
      setTasks(latest);
    } catch (error) {
      if (error instanceof Error) notify(error.message);
    }
  }, [notify]);

  const clearSelectedTaskHistory = useCallback(async () => {
    if (!api) return;
    await refreshTasks(api);
    setSelectedTaskId('');
    setDetail(null);
    setEvents([]);
  }, [api, refreshTasks]);

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
        const [healthResult, voiceResult, capabilitiesResult, taskResult, sessionResult] = await Promise.all([
          client.health(),
          client.voiceStatus(),
          client.capabilities(),
          client.listTasks(),
          client.listSessions(),
        ]);
        if (disposed) return;
        const activeSession = sessionResult[0] ?? await client.createSession();
        setApi(client);
        setHealth(healthResult);
        setVoiceStatus(voiceResult);
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
    if (!api || page !== 'capabilities') return;
    let disposed = false;
    const refreshVoice = () => {
      void api.voiceStatus().then((snapshot) => {
        if (!disposed) setVoiceStatus(snapshot);
      }).catch(() => undefined);
    };
    const timer = window.setInterval(refreshVoice, 5000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [api, page]);

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
          if (code === 'EVENT_REPLAY_LIMIT') return;
          if (code === 'EVENT_CURSOR_EXPIRED') {
            void refreshTasks(api);
            if (selectedTaskRef.current) void refreshDetail(api, selectedTaskRef.current);
            void api.health().then(setHealth).catch(() => setConnected(false));
            notify('Older event history was pruned; current task state is being refreshed.');
          } else if (code === 'INVALID_EVENT_CURSOR') {
            void refreshTasks(api);
            if (selectedTaskRef.current) void refreshDetail(api, selectedTaskRef.current);
          } else {
            notify(code === 'TASK_QUEUE_FULL' ? 'The task queue is full. Try again shortly.' : `Backend: ${code}`);
          }
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
      pending?.text === text
        && pending.sessionId === sessionId
        && pending.allowWebResearch === allowWebResearch
        ? pending.requestId
        : crypto.randomUUID();
    pendingSubmission.current = { text, sessionId, requestId: stableRequestId, allowWebResearch };
    setBusy(true);
    try {
      const result = await api.interact(text, sessionId, stableRequestId, allowWebResearch);
      pendingSubmission.current = null;
      setComposer('');
      setAllowWebResearch(false);
      setInteraction(result);
      if (result.task) {
        setSelectedTaskId(result.task.task_id);
        setTasks((current) => [
          result.task!,
          ...current.filter((task) => task.task_id !== result.task!.task_id),
        ]);
        setDetailRefresh((value) => value + 1);
        await refreshTasks(api);
      }
      if (result.outcome === 'task') {
        notify('Task admitted. ARISE will report completion only after verification passes.');
      } else if (result.outcome === 'answer') {
        notify('Informational response received. No task was created.');
      } else if (result.outcome === 'unavailable') {
        notify('No task was created. Review the informational response for configuration details.');
      } else {
        notify('Response received. No task was created.');
      }
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

  const contentTitle = page === 'overview' ? 'Overview' : page === 'tasks' ? 'Task history' : page === 'memory' ? 'Memory' : page === 'research' ? 'Research' : 'Capabilities';

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
          <button className={page === 'memory' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('memory')}>
            <Brain size={17} /><span>Memory</span>
          </button>
          <button className={page === 'research' ? 'nav-item active' : 'nav-item'} onClick={() => setPage('research')}>
            <Globe2 size={17} /><span>Research</span>
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
          <div className={page === 'memory' || page === 'research' ? 'content-layout memory-layout' : 'content-layout'}>
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
                  interaction={interaction}
                  allowWebResearch={allowWebResearch}
                  setAllowWebResearch={setAllowWebResearch}
                  onSubmit={submitTask}
                  busy={busy}
                  connected={connected}
                  onOpenCapabilities={() => setPage('capabilities')}
                  onSelectTask={setSelectedTaskId}
                />
              )}
              {page === 'tasks' && api && (
                <TasksPage
                  api={api}
                  tasks={sortedTasks}
                  selectedTaskId={selectedTaskId}
                  onSelect={setSelectedTaskId}
                  onHistoryCleared={() => void clearSelectedTaskHistory()}
                  notify={notify}
                />
              )}
              {page === 'memory' && api && <MemoryPage api={api} notify={notify} semanticMemoryEnabled={capabilities.some((item) => item.name === 'memory.semantic' && item.status === 'available')} />}
              {page === 'research' && api && <ResearchPage api={api} notify={notify} />}
              {page === 'capabilities' && (
                <CapabilitiesPage
                  health={health}
                  diagnostics={diagnostics}
                  capabilities={capabilities}
                  voice={voiceStatus}
                  voiceBusy={voiceBusy}
                  onStartVoice={() => void controlVoice(true)}
                  onStopVoice={() => void controlVoice(false)}
                  onRefresh={() => {
                    if (!api) return;
                    void Promise.all([api.diagnostics(), api.voiceStatus()]).then(([snapshot, voice]) => {
                      setDiagnostics(snapshot);
                      setCapabilities(snapshot.capabilities);
                      setVoiceStatus(voice);
                    }).catch((error: unknown) => notify(error instanceof Error ? error.message : 'Diagnostics unavailable.'));
                  }}
                />
              )}
            </section>

            {page !== 'memory' && page !== 'research' && (
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
            )}
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
  interaction: TextInteractionResponse | null;
  allowWebResearch: boolean;
  setAllowWebResearch: (value: boolean) => void;
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
          <p>Ask for information or describe an action. Questions use an informational model when configured; action requests enter the task runtime and require policy checks and verified execution.</p>
          <form className="task-composer" onSubmit={props.onSubmit}>
            <MessageSquareText size={17} className="composer-icon" />
            <input
              value={props.composer}
              onChange={(event) => props.setComposer(event.target.value)}
              placeholder="Ask a question or describe a task for ARISE…"
              maxLength={16_384}
              aria-label="Ask ARISE a question or describe a task"
            />
            <span className="composer-shortcut">↵</span>
            <button className="composer-send" type="submit" disabled={!props.composer.trim() || props.busy} aria-label="Submit task">
              {props.busy ? <LoaderCircle className="spin" size={16} /> : <SendHorizontal size={16} />}
            </button>
          </form>
          <label className="task-research-consent"><input type="checkbox" checked={props.allowWebResearch} onChange={(event) => props.setAllowWebResearch(event.target.checked)} /><span>Allow one-time web research for this request</span><small>Query text may be sent to Brave Search; results stay untrusted.</small></label>
          {props.interaction && (
            <section className={`interaction-response-card ${props.interaction.outcome}`} aria-live="polite">
              <div className="interaction-response-heading">
                <div><span className="eyebrow">{props.interaction.outcome === 'task' ? 'TASK ADMITTED' : 'NO TASK CREATED'}</span><strong>{props.interaction.provider_id ? `Response · ${props.interaction.provider_id}` : 'ARISE response'}</strong></div>
                {props.interaction.task && <button className="notice-link" type="button" onClick={() => props.onSelectTask(props.interaction!.task!.task_id)}>View task <ArrowUpRight size={13} /></button>}
              </div>
              <p>{props.interaction.answer ?? 'ARISE returned no informational response.'}</p>
              {props.interaction.sources.length > 0 && (
                <div className="interaction-source-list">
                  <div className="interaction-source-label"><strong>Cited source material</strong><span>UNTRUSTED</span></div>
                  {props.interaction.sources.map((source, index) => {
                    const href = safeHttpsLink(source.source_id);
                    return <article className="interaction-source" key={`${source.source_id}-${index}`}><div className="interaction-source-heading"><span>SOURCE {String(index + 1).padStart(2, '0')}</span><strong>{source.provenance}</strong>{href && <a href={href} target="_blank" rel="noopener noreferrer" aria-label="Open cited source"><ArrowUpRight size={12} /></a>}</div><span>{source.text}</span></article>;
                  })}
                </div>
              )}
            </section>
          )}
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
          <div className="notice-copy"><strong>Planning is not configured</strong><span>No model provider or live desktop executor is active. Clear action requests can be admitted, but planning and informational answers require a configured model. Nothing is simulated.</span></div>
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

function safeHttpsLink(value: string): string | null {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' ? parsed.href : null;
  } catch {
    return null;
  }
}

function ResearchPage({ api, notify }: { api: AriseApi; notify: (message: string) => void }) {
  const [query, setQuery] = useState('');
  const [domainFilter, setDomainFilter] = useState('');
  const [response, setResponse] = useState<WebResearchResponse | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);

  const runSearch = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!query.trim() || busy) return;
    const allowedDomains = domainFilter.split(',').map((value) => value.trim()).filter(Boolean);
    setBusy(true);
    setSubmitted(true);
    setResponse(null);
    try {
      setResponse(await api.searchWebResearch(query.trim(), 8, allowedDomains));
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Web research is unavailable.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-heading compact-heading research-heading">
        <div><div className="eyebrow"><span className="eyebrow-line" />USER-INITIATED WEB LOOKUP</div><h1>Research<span className="heading-period">.</span></h1><p className="page-subtitle">Search public sources with explicit egress controls and visible citations.</p></div>
        <div className="research-mode"><Globe2 size={15} /><span>OPTIONAL NETWORK</span></div>
      </div>
      <div className="memory-privacy-note research-privacy-note"><ShieldAlert size={15} /><span>Submitting sends your query to Brave Search. ARISE may fetch selected public HTTPS sources using DNS-pinned connections. Results are untrusted data, are not saved automatically, and can never execute actions.</span></div>
      <section className="memory-panel research-panel">
        <div className="memory-panel-heading"><div><span className="eyebrow">SEARCH THE WEB</span><h2>What do you want to investigate?</h2></div><Globe2 size={18} /></div>
        <form className="research-form" onSubmit={runSearch}>
          <label className="memory-field"><span>Research question</span><textarea value={query} onChange={(event) => setQuery(event.target.value)} rows={3} maxLength={16_384} placeholder="Ask for current information or compare public sources…" /></label>
          <label className="memory-field"><span>Optional domain allowlist</span><input value={domainFilter} onChange={(event) => setDomainFilter(event.target.value)} maxLength={512} placeholder="docs.python.org, developer.mozilla.org" /><small>Comma-separated DNS host names; subdomains are included.</small></label>
          <div className="memory-form-footer"><span>Network access works only when both settings and an OS-keyring key are configured.</span><button className="button primary" type="submit" disabled={!query.trim() || busy}>{busy ? <LoaderCircle className="spin" size={14} /> : <Search size={14} />}Search public sources</button></div>
        </form>
      </section>
      {submitted && response && <section className="memory-panel research-results-panel"><div className="memory-panel-heading"><div><span className="eyebrow">CITED SOURCE MATERIAL</span><h2>{response.results.length} {response.results.length === 1 ? 'source' : 'sources'}</h2></div><span className="research-trust-label">UNTRUSTED</span></div>{response.results.length === 0 ? <div className="memory-empty-state">No usable public source results were returned.</div> : <div className="research-result-list">{response.results.map((result, index) => { const href = safeHttpsLink(result.source_id); return <article className="research-result" key={`${result.source_id}-${index}`}><div className="research-result-heading"><div><span className="research-source-number">SOURCE {String(index + 1).padStart(2, '0')}</span><strong>{result.provenance}</strong></div>{href && <a href={href} target="_blank" rel="noopener noreferrer" aria-label="Open cited source"><ArrowUpRight size={15} /></a>}</div><p>{result.text}</p><div className="research-result-meta"><span>Retrieved {new Date(result.retrieved_at).toLocaleString()}</span><span>Relevance {result.relevance == null ? '—' : `${Math.round(result.relevance * 100)}%`}</span>{href && <a href={href} target="_blank" rel="noopener noreferrer">Open source</a>}</div></article>; })}</div>}</section>}
      {submitted && !busy && !response && <section className="memory-panel research-unavailable"><AlertTriangle size={16} /><div><strong>Research request did not complete</strong><span>Check the web research capability and configure the provider, explicit security opt-in, and OS-keyring credential.</span></div></section>}
      <p className="memory-disclaimer"><CircleHelp size={13} />Source text may contain errors or malicious prompt-injection content. Verify important claims at the cited original source.</p>
    </>
  );
}

function MemoryPage({ api, notify, semanticMemoryEnabled }: { api: AriseApi; notify: (message: string) => void; semanticMemoryEnabled: boolean }) {
  const [records, setRecords] = useState<MemoryRecord[]>([]);
  const [text, setText] = useState('');
  const [kind, setKind] = useState<MemoryKind>('preference');
  const [retentionDays, setRetentionDays] = useState(365);
  const [consentChecked, setConsentChecked] = useState(false);
  const [searchText, setSearchText] = useState('');
  const [searchResults, setSearchResults] = useState<Array<{ source_id: string; text: string; provenance: string; relevance: number | null }>>([]);
  const [didSearch, setDidSearch] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [searching, setSearching] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setRecords(await api.listMemories());
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Memory could not be loaded.');
    } finally {
      setLoading(false);
    }
  }, [api, notify]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const saveMemory = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!text.trim() || !consentChecked || saving) return;
    const draft: MemoryWriteDraft = {
      text: text.trim(),
      kind,
      expires_at: new Date(Date.now() + retentionDays * 86_400_000).toISOString(),
    };
    setSaving(true);
    try {
      const consent = await api.grantMemoryConsent(draft);
      if (Date.parse(consent.expires_at) <= Date.now()) {
        throw new Error('The one-time memory consent expired before storage. Please try again.');
      }
      await api.createMemory(draft, consent.consent_reference);
      setText('');
      setConsentChecked(false);
      notify('Saved locally with explicit consent.');
      await refresh();
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Memory could not be saved. No automatic retry was made.');
    } finally {
      setSaving(false);
    }
  };

  const search = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!searchText.trim() || searching) return;
    setSearching(true);
    try {
      setSearchResults(await api.searchMemories(searchText.trim()));
      setDidSearch(true);
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Memory search failed.');
    } finally {
      setSearching(false);
    }
  };

  const removeMemory = async (record: MemoryRecord) => {
    if (!window.confirm('Delete this saved memory permanently?')) return;
    try {
      await api.deleteMemory(record.record_id);
      setRecords((current) => current.filter((item) => item.record_id !== record.record_id));
      notify('Memory deleted.');
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Memory could not be deleted.');
    }
  };

  const clearAll = async () => {
    if (!window.confirm('Delete every saved memory and any outstanding write consents? This cannot be undone.')) return;
    setDeleting(true);
    try {
      const count = await api.clearMemories();
      setRecords([]);
      setSearchResults([]);
      setDidSearch(false);
      notify(`Deleted ${count} saved ${count === 1 ? 'memory' : 'memories'}.`);
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Memories could not be deleted.');
    } finally {
      setDeleting(false);
    }
  };

  const exportMemories = async () => {
    try {
      const data = await api.exportMemories();
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = 'arise-memory-export.json';
      anchor.click();
      URL.revokeObjectURL(url);
      notify('Memory export downloaded.');
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Memory export failed.');
    }
  };

  return (
    <>
      <div className="page-heading compact-heading memory-heading">
        <div><div className="eyebrow"><span className="eyebrow-line" />USER-CONTROLLED CONTEXT</div><h1>Memory<span className="heading-period">.</span></h1><p className="page-subtitle">Local notes you choose to save, inspect, export, and delete.</p></div>
        <div className="memory-counter"><strong>{records.length.toString().padStart(2, '0')}</strong><span>ACTIVE RECORDS</span></div>
      </div>

      <div className="memory-privacy-note"><LockKeyhole size={15} /><span>Memory is never inferred from model output. Each write requires a one-time, exact-content consent; records stay in local SQLite, are redacted on storage, and expire automatically.</span></div>

      <section className="memory-panel">
        <div className="memory-panel-heading"><div><span className="eyebrow">EXPLICIT SAVE</span><h2>Add a memory</h2></div><Brain size={18} /></div>
        <form className="memory-form" onSubmit={saveMemory}>
          <label className="memory-field"><span>What should ARISE remember?</span><textarea value={text} onChange={(event) => setText(event.target.value)} maxLength={16_384} rows={3} placeholder="For example: I prefer concise status updates." /></label>
          <div className="memory-form-options">
            <label className="memory-field"><span>Category</span><select value={kind} onChange={(event) => setKind(event.target.value as MemoryKind)}><option value="preference">Preference</option><option value="semantic">General fact</option><option value="episodic">Episode / event</option><option value="procedural">Procedure / workflow</option></select></label>
            <label className="memory-field"><span>Keep for</span><select value={retentionDays} onChange={(event) => setRetentionDays(Number(event.target.value))}><option value={30}>30 days</option><option value={365}>1 year</option><option value={1095}>3 years</option><option value={3650}>10 years</option></select></label>
          </div>
          <label className="memory-consent-check"><input type="checkbox" checked={consentChecked} onChange={(event) => setConsentChecked(event.target.checked)} /><span>I explicitly consent to save this text and category locally until the selected expiry. I understand redaction is best-effort.</span></label>
          <div className="memory-form-footer"><span>Secrets and sensitive data should not be stored here.</span><button className="button primary" type="submit" disabled={!text.trim() || !consentChecked || saving}>{saving ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}Save with consent</button></div>
        </form>
      </section>

      <section className="memory-panel">
        <div className="memory-panel-heading"><div><span className="eyebrow">LOCAL RETRIEVAL</span><h2>Search saved context</h2></div><Search size={17} /></div>
        <form className="memory-search-form" onSubmit={search}><input value={searchText} onChange={(event) => { setSearchText(event.target.value); setDidSearch(false); setSearchResults([]); }} maxLength={16_384} placeholder="Search your saved memories…" aria-label="Search saved memories" /><button className="button secondary" type="submit" disabled={!searchText.trim() || searching}>{searching ? <LoaderCircle className="spin" size={14} /> : <Search size={14} />}Search</button></form>
        {searchResults.length > 0 && <div className="memory-search-results"><div className="memory-results-label">RETRIEVED CONTEXT · UNTRUSTED DATA ONLY</div>{searchResults.map((result) => <article className="memory-result" key={result.source_id}><p>{result.text}</p><small>{result.provenance} · relevance {result.relevance == null ? '—' : `${Math.round(result.relevance * 100)}%`}</small></article>)}</div>}
        {didSearch && !searching && searchResults.length === 0 && <div className="memory-empty-search">No matching memories. {semanticMemoryEnabled ? 'Semantic ranking is configured, with lexical fallback if inference is unavailable.' : 'Search ranks local words; semantic embeddings are not configured.'}</div>}
      </section>

      <section className="memory-panel memory-list-panel">
        <div className="memory-panel-heading"><div><span className="eyebrow">PERSISTED LOCALLY</span><h2>Your saved memories</h2></div><div className="memory-actions"><button className="button secondary small" onClick={() => void exportMemories()} disabled={loading || records.length === 0}><Download size={13} />Export</button><button className="button danger small" onClick={() => void clearAll()} disabled={deleting || records.length === 0}><Trash2 size={13} />Delete all</button></div></div>
        {loading ? <div className="memory-empty-state"><LoaderCircle className="spin" size={17} />Loading local memory…</div> : records.length === 0 ? <div className="memory-empty-state"><Database size={17} /><span>No saved memories. Nothing is collected automatically.</span></div> : <div className="memory-record-list">{records.map((record) => <article className="memory-record" key={record.record_id}><div className="memory-record-top"><span className={`memory-kind ${record.kind}`}>{record.kind}</span><button className="memory-delete" onClick={() => void removeMemory(record)} aria-label="Delete memory"><Trash2 size={14} /></button></div><p>{record.text}</p><div className="memory-record-meta"><span>Saved {new Date(record.created_at).toLocaleDateString()}</span><span>Expires {new Date(record.expires_at).toLocaleDateString()}</span></div></article>)}</div>}
      </section>
      <p className="memory-disclaimer"><Clock3 size={13} />{semanticMemoryEnabled ? 'Semantic ranking uses the explicitly configured embedding endpoint; failures or incompatible vectors fall back to local lexical ranking. Automatic memory suggestions remain disabled.' : 'Retrieval currently uses local lexical ranking. Optional semantic embeddings are not configured, and automatic memory suggestions remain disabled.'}</p>
    </>
  );
}

function TasksPage({ api, tasks, selectedTaskId, onSelect, onHistoryCleared, notify }: { api: AriseApi; tasks: TaskSnapshot[]; selectedTaskId: string; onSelect: (id: string) => void; onHistoryCleared: () => void | Promise<void>; notify: (message: string) => void }) {
  const states = ['all', 'running', 'completed', 'requires_user_input', 'failed'] as const;
  const [filter, setFilter] = useState<(typeof states)[number]>('all');
  const [historyBusy, setHistoryBusy] = useState(false);
  const visibleTasks = filter === 'all' ? tasks : tasks.filter((task) => task.state === filter);
  const exportHistory = async () => {
    setHistoryBusy(true);
    try {
      const history = await api.exportTaskHistory();
      const file = new Blob([JSON.stringify(history, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(file);
      const link = document.createElement('a');
      link.href = url;
      link.download = `arise-task-history-${new Date().toISOString().replace(/[:.]/g, '-')}.json`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      notify(history.truncated.tasks || history.truncated.events
        ? 'Task history exported with a size-limit truncation; see the JSON metadata.'
        : `Exported ${history.tasks.length} task(s) and ${history.events.length} event(s).`);
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Task history export failed.');
    } finally {
      setHistoryBusy(false);
    }
  };
  const clearHistory = async () => {
    const confirmed = window.confirm(
      'Delete settled completed, failed, cancelled, and blocked task history for this account? Active, partially completed, interrupted, unknown, and otherwise unresolved tasks are retained. This cannot be undone.',
    );
    if (!confirmed) return;
    setHistoryBusy(true);
    try {
      const result = await api.clearTaskHistory();
      await onHistoryCleared();
      notify(`Deleted ${result.deleted_tasks} settled task(s); retained ${result.retained_recoverable_tasks} active or unresolved task(s).`);
    } catch (error) {
      notify(error instanceof Error ? error.message : 'Task history could not be cleared.');
    } finally {
      setHistoryBusy(false);
    }
  };
  return (
    <>
      <div className="page-heading compact-heading"><div><div className="eyebrow"><span className="eyebrow-line" />AUDITABLE WORK</div><h1>Task history<span className="heading-period">.</span></h1><p className="page-subtitle">Backend-owned state, step outcomes, and evidence status.</p></div><div className="history-counter"><strong>{tasks.length.toString().padStart(2, '0')}</strong><span>TOTAL TASKS</span></div></div>
      <div className="history-actions">
        <button className="button secondary small" type="button" onClick={() => void exportHistory()} disabled={historyBusy || tasks.length === 0}><Download size={13} />Export JSON</button>
        <button className="button danger small" type="button" onClick={() => void clearHistory()} disabled={historyBusy || tasks.length === 0}><Trash2 size={13} />Clear terminal history</button>
      </div>
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

function CapabilitiesPage({ health, diagnostics, capabilities, voice, voiceBusy, onStartVoice, onStopVoice, onRefresh }: { health: HealthSnapshot | null; diagnostics: DiagnosticsSnapshot | null; capabilities: Capability[]; voice: VoiceStatusSnapshot | null; voiceBusy: boolean; onStartVoice: () => void; onStopVoice: () => void; onRefresh: () => void }) {
  const groups = [
    { title: 'CORE RUNTIME', names: ['task.orchestration', 'storage.sqlite', 'model.planning'] },
    { title: 'DESKTOP & BROWSER', names: ['desktop.ui_automation', 'browser.dom', 'vision.ocr'] },
    { title: 'VOICE PIPELINE', names: ['voice.audio_capture', 'voice.local_vad', 'voice.wake_word', 'voice.gemini_live', 'voice.asr', 'voice.tts'] },
    { title: 'MEMORY & RESEARCH', names: ['memory.local', 'memory.semantic', 'web.research'] },
  ];
  const map = new Map(capabilities.map((capability) => [capability.name, capability]));
  return (
    <>
      <div className="page-heading compact-heading"><div><div className="eyebrow"><span className="eyebrow-line" />HONEST BY DESIGN</div><h1>Capabilities<span className="heading-period">.</span></h1><p className="page-subtitle">Only adapters that report real availability are shown as ready.</p></div><button className="button secondary small" onClick={onRefresh}><RefreshCw size={14} /> Refresh status &amp; local facts</button></div>
      <div className={`system-health-card ${health?.status ?? 'degraded'}`}>
        <div className="system-health-icon"><HeartPulse size={19} /></div><div className="system-health-copy"><span className="eyebrow">SYSTEM HEALTH</span><strong>{health?.status === 'healthy' ? 'Runtime operational' : health?.status === 'unavailable' ? 'Runtime unavailable' : 'Runtime degraded'}</strong><span>{health?.degraded_reasons?.join(' · ') || 'Local API and persistence are responding.'}</span></div><div className="health-orb"><span /></div>
      </div>
      <div className="capability-groups">
        {groups.map((group) => <section className="capability-group" key={group.title}><div className="capability-group-title">{group.title}<span>{group.names.length.toString().padStart(2, '0')}</span></div>{group.names.map((name) => <CapabilityCard key={name} capability={map.get(name)} name={name} />)}</section>)}
      </div>
      <VoiceRuntimeCard status={voice} busy={voiceBusy} onStart={onStartVoice} onStop={onStopVoice} />
      <div className="diagnostics-strip"><div><Database size={15} /><span>Persistence</span><strong>{health?.database_status ?? 'unknown'}</strong></div><div><Cpu size={15} /><span>Model planning</span><strong>{health?.model_status ?? 'unknown'}</strong></div><div><Activity size={15} /><span>Task queue</span><strong>{health?.queued_tasks ?? 0} queued</strong></div></div>
      <EnvironmentDiagnosticsPanel snapshot={diagnostics} />
      <div className="capability-note"><CircleHelp size={15} /><span>Memory writes are local and explicitly consent-gated; semantic embeddings are optional, with lexical fallback. Research is available only behind explicit egress settings and an OS-keyring credential; results remain untrusted. Voice, browser, and Windows automation are not runtime-verified here: no physical Windows audio device, local voice model, Gemini session, or Chromium runtime has been validated.</span></div>
    </>
  );
}

function EnvironmentDiagnosticsPanel({ snapshot }: { snapshot: DiagnosticsSnapshot | null }) {
  const environment = snapshot?.environment;
  const unavailable = new Set(environment?.unavailable_fields ?? []);
  const value = (field: string, text: string) => unavailable.has(field) ? 'Unavailable on this host' : text;
  const memory = environment?.total_memory_bytes == null
    ? 'Not reported'
    : `${(environment.total_memory_bytes / (1024 ** 3)).toFixed(1)} GiB`;
  const displays = environment?.displays.length
    ? environment.displays.map((display) => {
      const dimensions = display.width && display.height ? `${display.width} × ${display.height}` : 'size unavailable';
      const dpi = display.dpi_x && display.dpi_y ? `${display.dpi_x} × ${display.dpi_y} DPI` : 'DPI unavailable';
      return `${display.display_id}: ${dimensions}, ${dpi}${display.primary ? ' · primary' : ''}`;
    }).join(' · ')
    : 'No display data';
  const foreground = environment?.active_window?.available
    ? [environment.active_window.application, environment.active_window.title].filter(Boolean).join(' — ')
    : 'No active-window data';
  return (
    <section className="environment-panel" aria-label="Local environment diagnostics">
      <div className="environment-panel-heading">
        <div><span className="eyebrow">LOCAL HOST FACTS</span><strong>Environment snapshot</strong></div>
        <span>{snapshot ? `Updated ${relativeTime(snapshot.checked_at)}` : 'Not loaded'}</span>
      </div>
      {!environment ? (
        <p className="environment-empty">Select “Refresh status &amp; local facts” to request a read-only snapshot. It may include process names, display metadata, the foreground-window title, installed-app names, and audio-device names; it does not capture screens or open audio streams.</p>
      ) : (
        <div className="environment-grid">
          <div><span>Operating system</span><strong>{environment.operating_system} {environment.os_version} · {environment.architecture}</strong></div>
          <div><span>CPU &amp; memory</span><strong>{environment.cpu_count} logical CPUs · {memory}</strong></div>
          <div><span>Graphics</span><strong>{value('gpu_names', environment.gpu_names.join(', ') || 'No adapter names reported')}</strong></div>
          <div><span>Displays</span><strong>{value('displays', displays)}</strong></div>
          <div><span>Foreground window</span><strong>{value('active_window', foreground)}</strong></div>
          <div><span>Applications</span><strong>{value('running_applications', `${environment.running_applications.length} running · ${unavailable.has('installed_applications') ? 'installed list unavailable' : `${environment.installed_applications.length} installed`}`)}</strong></div>
          <div><span>Browsers &amp; terminals</span><strong>{[...environment.browsers, ...environment.terminals].join(', ') || 'None detected'}</strong></div>
          <div><span>Audio endpoints</span><strong>{unavailable.has('audio_input_devices') || unavailable.has('audio_output_devices') ? 'Device discovery unavailable' : `${environment.audio_input_devices.length} input · ${environment.audio_output_devices.length} output`}</strong></div>
          <div><span>Network</span><strong>{environment.network_status}</strong></div>
        </div>
      )}
      <p className="environment-footnote">A snapshot is collected only when requested. No process arguments or raw audio are read; unsupported probes stay unavailable.</p>
    </section>
  );
}

function VoiceRuntimeCard({ status, busy, onStart, onStop }: { status: VoiceStatusSnapshot | null; busy: boolean; onStart: () => void; onStop: () => void }) {
  const state = status?.state ?? 'unknown';
  const configured = Boolean(status?.provider_id);
  const listening = status?.wake_word_enabled ?? false;
  const metrics = [
    { key: 'time_to_first_audio_ms', label: 'First audio' },
    { key: 'interruption_latency_ms', label: 'Barge-in' },
    { key: 'task_acknowledgement_latency_ms', label: 'Task acknowledgement' },
    { key: 'session_reconnect_ms', label: 'Reconnect' },
  ];
  return (
    <section className="voice-runtime-panel" aria-label="Voice runtime status">
      <div className="voice-runtime-heading">
        <div className="voice-runtime-title"><div className="voice-runtime-icon"><Mic size={17} /></div><div><span className="eyebrow">VOICE DIAGNOSTICS</span><strong>ARISE conversation layer</strong></div></div>
        <span className={`voice-state-badge ${state}`}><i />{state.replaceAll('_', ' ')}</span>
      </div>
      <div className="voice-runtime-grid">
        <div><span>Microphone</span><strong>{status?.microphone_status.replaceAll('_', ' ') ?? 'unknown'}</strong></div>
        <div><span>Gemini Live</span><strong>{status?.provider_status.replaceAll('_', ' ') ?? 'unknown'}</strong></div>
        <div><span>Wake word</span><strong>{status?.wake_word_enabled ? `${status.wake_word} armed` : `${status?.wake_word ?? 'ARISE'} inactive`}</strong></div>
        <div><span>Idle timeout</span><strong>{status ? `${status.inactivity_timeout_seconds}s` : '—'}</strong></div>
      </div>
      <div className="voice-controls">
        <button className="button primary small" type="button" onClick={onStart} disabled={!configured || busy || listening}>
          {busy ? <LoaderCircle className="spin" size={13} /> : <Mic size={13} />}
          Start local listening
        </button>
        <button className="button secondary small" type="button" onClick={onStop} disabled={!configured || busy || (!listening && !status?.active_session_id)}>
          <Square size={13} />Stop listening
        </button>
        {!configured && <small>Enable voice and configure local Vosk plus Gemini before microphone controls are available.</small>}
      </div>
      {status?.active_task_id && <div className="voice-active-task"><Activity size={13} /><span>Current task</span><code>{shortId(status.active_task_id)}</code></div>}
      <div className="voice-telemetry">
        {metrics.map((metric) => {
          const sample = status?.telemetry[metric.key];
          return <div key={metric.key}><span>{metric.label}</span><strong>{sample?.last_latency_ms == null ? '—' : `${sample.last_latency_ms.toFixed(0)} ms`}</strong><small>{sample?.count ? `${sample.count} samples` : 'no samples'}</small></div>;
        })}
      </div>
      {status?.last_error_code && <div className="voice-error-code"><Radio size={13} /><span>Diagnostic</span><code>{status.last_error_code}</code></div>}
      <p>{status?.wake_word_enabled ? `Local wake-word monitoring is armed. Audio reaches Gemini Live only after “${status.wake_word}” is detected.` : configured ? 'Microphone capture is stopped. ARISE is not listening or sending ambient audio until you start it.' : 'Microphone capture and local wake-word detection are not configured. ARISE is not listening or sending ambient audio.'}</p>
    </section>
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
