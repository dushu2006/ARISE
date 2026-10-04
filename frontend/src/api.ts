import type {
  Capability,
  DiagnosticsSnapshot,
  HealthSnapshot,
  MemoryConsent,
  MemoryRecord,
  MemorySearchResult,
  MemoryWriteDraft,
  PersonalizationProfile,
  ProceduralWorkflowSummary,
  Session,
  TaskDetail,
  TaskHistoryClearResult,
  TaskHistoryExport,
  TaskSnapshot,
  TextInteractionResponse,
  VoiceStatusSnapshot,
  WebResearchResponse,
} from './types';

const API_PREFIX = '/api/v1';

function requestId(): string {
  return crypto.randomUUID();
}

export async function resolveApiToken(): Promise<string> {
  const isDesktop = '__TAURI_INTERNALS__' in window;
  const configured = import.meta.env.DEV && !isDesktop
    ? import.meta.env.VITE_API_TOKEN?.trim()
    : undefined;
  if (configured) return configured;

  if (isDesktop) {
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const token = await invoke<string>('get_api_token');
      if (token.trim()) return token.trim();
    } catch {
      throw new Error('ARISE could not read its local API credential from the desktop shell.');
    }
  }

  throw new Error('Local API token is not configured. Start the desktop shell or set VITE_API_TOKEN for development.');
}

export class AriseApi {
  constructor(private readonly token: string) {}

  async health(): Promise<HealthSnapshot> {
    return this.fetchJson<HealthSnapshot>('/healthz', { method: 'GET' }, false);
  }

  async capabilities(): Promise<Capability[]> {
    return this.get<Capability[]>('/capabilities');
  }

  async voiceStatus(): Promise<VoiceStatusSnapshot> {
    return this.get<VoiceStatusSnapshot>('/voice/status');
  }

  async startVoiceListening(): Promise<VoiceStatusSnapshot> {
    return this.post<VoiceStatusSnapshot>('/voice/listening/start', {});
  }

  async stopVoiceListening(): Promise<VoiceStatusSnapshot> {
    return this.post<VoiceStatusSnapshot>('/voice/listening/stop', {});
  }

  async diagnostics(): Promise<DiagnosticsSnapshot> {
    return this.get<DiagnosticsSnapshot>('/diagnostics');
  }

  async searchWebResearch(
    query: string,
    maxResults: number = 8,
    allowedDomains: string[] = [],
  ): Promise<WebResearchResponse> {
    return this.post<WebResearchResponse>('/research/search', {
      query,
      max_results: maxResults,
      allowed_domains: allowedDomains,
    });
  }

  async listMemories(): Promise<MemoryRecord[]> {
    return this.get<MemoryRecord[]>('/memory');
  }

  async grantMemoryConsent(draft: MemoryWriteDraft): Promise<MemoryConsent> {
    return this.post<MemoryConsent>('/memory/consents', draft);
  }

  async createMemory(draft: MemoryWriteDraft, consentReference: string): Promise<MemoryRecord> {
    return this.post<MemoryRecord>('/memory', {
      ...draft,
      consent_reference: consentReference,
    });
  }

  async searchMemories(query: string): Promise<MemorySearchResult[]> {
    const params = new URLSearchParams({ query });
    const result = await this.get<{ results: MemorySearchResult[]; authority: string }>(
      `/memory/search?${params.toString()}`,
    );
    return result.results;
  }

  async exportMemories(): Promise<{ exported_at: string; memories: MemoryRecord[] }> {
    return this.get<{ exported_at: string; memories: MemoryRecord[] }>('/memory/export');
  }

  async deleteMemory(recordId: string): Promise<void> {
    await this.fetchJson<{ deleted: boolean }>(
      `${API_PREFIX}/memory/${encodeURIComponent(recordId)}`,
      { method: 'DELETE' },
    );
  }

  async clearMemories(): Promise<number> {
    const result = await this.fetchJson<{ deleted: number }>(`${API_PREFIX}/memory`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
    return result.deleted;
  }

  async getMemorySettings(): Promise<{ enabled: boolean }> {
    return this.get<{ enabled: boolean }>('/memory/settings');
  }

  async updateMemorySettings(enabled: boolean): Promise<{ enabled: boolean }> {
    return this.fetchJson<{ enabled: boolean }>(`${API_PREFIX}/memory/settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
  }

  async getPersonalization(): Promise<PersonalizationProfile> {
    return this.get<PersonalizationProfile>('/personalization');
  }

  async updatePersonalization(
    patch: Partial<Omit<PersonalizationProfile, 'principal_id' | 'updated_at'>>,
  ): Promise<PersonalizationProfile> {
    return this.fetchJson<PersonalizationProfile>(`${API_PREFIX}/personalization`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
  }

  async listWorkflows(): Promise<ProceduralWorkflowSummary[]> {
    const res = await this.get<{ workflows: ProceduralWorkflowSummary[] }>('/workflows');
    return res.workflows;
  }

  async deleteWorkflow(workflowId: string): Promise<void> {
    await this.fetchJson<{ deleted: boolean }>(
      `${API_PREFIX}/workflows/${encodeURIComponent(workflowId)}`,
      { method: 'DELETE' },
    );
  }

  async listTasks(): Promise<TaskSnapshot[]> {
    return this.get<TaskSnapshot[]>('/tasks');
  }

  async exportTaskHistory(): Promise<TaskHistoryExport> {
    return this.get<TaskHistoryExport>('/tasks/export');
  }

  async clearTaskHistory(): Promise<TaskHistoryClearResult> {
    return this.fetchJson<TaskHistoryClearResult>(`${API_PREFIX}/tasks/history`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm: true }),
    });
  }

  async getTask(taskId: string): Promise<TaskDetail> {
    return this.get<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}`);
  }

  async listChildTasks(parentTaskId: string): Promise<TaskSnapshot[]> {
    return this.get<TaskSnapshot[]>(`/tasks/${encodeURIComponent(parentTaskId)}/children`);
  }

  async submitChildTask(
    parentTaskId: string,
    text: string,
    sessionId: string,
    stableRequestId: string = requestId(),
  ): Promise<TaskSnapshot> {
    return this.post<TaskSnapshot>(`/tasks/${encodeURIComponent(parentTaskId)}/children`, {
      request_id: stableRequestId,
      session_id: sessionId,
      text,
      source: 'text',
      locale: navigator.language || 'en',
      allow_web_research: false,
    });
  }

  async listSessions(): Promise<Session[]> {
    return this.get<Session[]>('/sessions');
  }

  async createSession(): Promise<Session> {
    return this.post<Session>('/sessions', { locale: navigator.language || 'en' });
  }

  async interact(
    text: string,
    sessionId: string,
    stableRequestId: string = requestId(),
    allowWebResearch: boolean = false,
  ): Promise<TextInteractionResponse> {
    return this.post<TextInteractionResponse>('/interactions', {
      request_id: stableRequestId,
      session_id: sessionId,
      text,
      source: 'text',
      locale: navigator.language || 'en',
      allow_web_research: allowWebResearch,
    });
  }

  async submitTask(
    text: string,
    sessionId: string,
    stableRequestId: string = requestId(),
    allowWebResearch: boolean = false,
  ): Promise<TaskSnapshot> {
    return this.post<TaskSnapshot>('/tasks', {
      request_id: stableRequestId,
      session_id: sessionId,
      text,
      source: 'text',
      locale: navigator.language || 'en',
      allow_web_research: allowWebResearch,
    });
  }

  async cancelTask(taskId: string): Promise<TaskSnapshot> {
    return this.post<TaskSnapshot>(`/tasks/${encodeURIComponent(taskId)}/cancel`, {});
  }

  async approveTask(taskId: string, confirmationId: string): Promise<TaskSnapshot> {
    return this.post<TaskSnapshot>(`/tasks/${encodeURIComponent(taskId)}/approve`, {
      confirmation_id: confirmationId,
    });
  }

  async respondToTask(taskId: string, text: string): Promise<TaskSnapshot> {
    return this.post<TaskSnapshot>(`/tasks/${encodeURIComponent(taskId)}/respond`, { text });
  }

  websocketUrl(): string {
    const url = new URL('/ws/v1', window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    return url.toString();
  }

  private async get<T>(path: string, authenticated = true): Promise<T> {
    return this.fetchJson<T>(`${API_PREFIX}${path}`, { method: 'GET' }, authenticated);
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    return this.fetchJson<T>(`${API_PREFIX}${path}`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  }

  private async fetchJson<T>(path: string, init: RequestInit, authenticated = true): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set('Accept', 'application/json');
    if (init.body !== undefined) headers.set('Content-Type', 'application/json');
    if (authenticated) headers.set('Authorization', `Bearer ${this.token}`);

    let response: Response;
    try {
      response = await fetch(path, { ...init, headers, credentials: 'omit' });
    } catch {
      throw new Error('The local ARISE backend is not reachable.');
    }
    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      try {
        const payload = (await response.json()) as { detail?: string; error?: { message?: string } };
        message = payload.error?.message ?? payload.detail ?? message;
      } catch {
        // Do not surface an unstructured provider or server response body.
      }
      throw new Error(message);
    }
    return (await response.json()) as T;
  }
}

export interface ConnectionCallbacks {
  onFrame: (frame: Record<string, unknown>) => void;
  onConnection: (connected: boolean) => void;
  onError: (message: string) => void;
  onReplayReset?: () => void;
}

export function connectProtocol(
  api: AriseApi,
  token: string,
  lastEventSequence: number,
  callbacks: ConnectionCallbacks,
): () => void {
  let closedByUser = false;
  let reconnectTimer: number | undefined;
  let heartbeatTimer: number | undefined;
  let socket: WebSocket | undefined;
  let attempts = 0;
  let replayCursor = Math.max(0, lastEventSequence);

  const clearHeartbeat = () => {
    if (heartbeatTimer !== undefined) {
      window.clearInterval(heartbeatTimer);
      heartbeatTimer = undefined;
    }
  };

  const open = () => {
    if (closedByUser) return;
    socket = new WebSocket(api.websocketUrl());
    let welcomed = false;
    socket.onopen = () => {
      socket?.send(
        JSON.stringify({
          protocol_version: 1,
          message_id: requestId(),
          type: 'client.hello',
          auth_token: token,
          last_event_sequence: replayCursor,
        }),
      );
    };
    socket.onmessage = (event) => {
      try {
        const frame = JSON.parse(String(event.data)) as Record<string, unknown>;
        if (frame.protocol_version !== 1) {
          callbacks.onError('The local backend uses an unsupported WebSocket protocol version.');
          socket?.close(1002, 'Unsupported protocol version');
          return;
        }
        if (frame.type === 'server.hello') {
          const payload = frame.payload as Record<string, unknown> | undefined;
          const currentSequence = Number(payload?.current_event_sequence);
          if (Number.isFinite(currentSequence) && replayCursor > currentSequence) {
            replayCursor = Math.max(0, currentSequence);
            callbacks.onReplayReset?.();
          }
          const heartbeatSeconds = Number(payload?.heartbeat_interval_seconds);
          welcomed = true;
          attempts = 0;
          clearHeartbeat();
          if (Number.isFinite(heartbeatSeconds) && heartbeatSeconds > 0) {
            const interval = Math.max(1000, heartbeatSeconds * 750);
            heartbeatTimer = window.setInterval(() => {
              if (socket?.readyState !== WebSocket.OPEN) return;
              socket.send(
                JSON.stringify({
                  protocol_version: 1,
                  message_id: requestId(),
                  type: 'ping',
                }),
              );
            }, interval);
          }
          callbacks.onConnection(true);
        }
        if (frame.type === 'event') {
          const payload = frame.payload as Record<string, unknown> | undefined;
          const eventRecord = payload?.event as Record<string, unknown> | undefined;
          const sequence = Number(eventRecord?.sequence);
          if (Number.isFinite(sequence) && sequence > replayCursor) replayCursor = sequence;
        }
        if (frame.type === 'protocol.error') {
          const payload = frame.payload as Record<string, unknown> | undefined;
          const code = String(payload?.code ?? '');
          if (code === 'EVENT_CURSOR_EXPIRED') {
            const floor = Number(payload?.replay_floor);
            if (Number.isSafeInteger(floor) && floor >= 0) {
              replayCursor = floor;
              callbacks.onReplayReset?.();
            }
          } else if (code === 'INVALID_EVENT_CURSOR') {
            const currentSequence = Number(payload?.latest_event_sequence);
            if (Number.isSafeInteger(currentSequence) && currentSequence >= 0) {
              replayCursor = currentSequence;
              callbacks.onReplayReset?.();
            }
          } else if (code === 'EVENT_REPLAY_LIMIT') {
            const afterSequence = Number(payload?.after_sequence);
            if (Number.isSafeInteger(afterSequence) && afterSequence > replayCursor) {
              replayCursor = afterSequence;
            }
          }
        }
        callbacks.onFrame(frame);
      } catch {
        callbacks.onError('A WebSocket frame could not be parsed.');
      }
    };
    socket.onerror = () => callbacks.onConnection(false);
    socket.onclose = () => {
      clearHeartbeat();
      callbacks.onConnection(false);
      if (closedByUser) return;
      attempts = Math.min(attempts + 1, 6);
      const baseDelay = Math.min(15_000, 500 * 2 ** Math.min(attempts - 1, 5));
      const jitteredDelay = Math.round(baseDelay * (0.8 + Math.random() * 0.4));
      reconnectTimer = window.setTimeout(open, jitteredDelay);
      // A close after a successful hello should restart quickly, but a failed
      // handshake keeps its accumulated exponential backoff.
      if (welcomed) attempts = 0;
    };
  };

  open();
  return () => {
    closedByUser = true;
    clearHeartbeat();
    if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
    socket?.close(1000, 'UI unmounted');
  };
}
