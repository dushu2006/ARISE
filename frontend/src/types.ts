export type MemoryKind = 'semantic' | 'preference' | 'episodic' | 'procedural';

export interface MemoryRecord {
  record_id: string;
  text: string;
  kind: MemoryKind;
  provenance: string;
  created_at: string;
  expires_at: string;
  source_task_id: string | null;
}

export interface MemoryWriteDraft {
  text: string;
  kind: MemoryKind;
  expires_at: string;
  source_task_id?: string | null;
}

export interface MemoryConsent {
  consent_reference: string;
  expires_at: string;
}

export interface MemorySearchResult {
  source: string;
  source_id: string;
  text: string;
  provenance: string;
  retrieved_at: string;
  relevance: number | null;
}

export interface WebResearchResult {
  source: string;
  source_id: string;
  text: string;
  provenance: string;
  retrieved_at: string;
  relevance: number | null;
}

export interface WebResearchResponse {
  provider: string;
  authority: 'untrusted_context_only';
  results: WebResearchResult[];
}

export type TextInteractionOutcome = 'task' | 'answer' | 'clarification' | 'control' | 'unavailable';

export interface TextInteractionSource {
  source_id: string;
  text: string;
  provenance: string;
  retrieved_at: string;
  relevance: number | null;
}

export interface TextInteractionResponse {
  outcome: TextInteractionOutcome;
  intent: string;
  task: TaskSnapshot | null;
  answer: string | null;
  provider_id: string | null;
  sources: TextInteractionSource[];
}

export type CapabilityStatus =
  | 'available'
  | 'degraded'
  | 'unavailable'
  | 'disabled'
  | 'requires_configuration';

export type TaskState =
  | 'created'
  | 'queued'
  | 'understanding'
  | 'planning'
  | 'ready'
  | 'running'
  | 'waiting'
  | 'waiting_model'
  | 'waiting_resource'
  | 'waiting_user'
  | 'waiting_auth'
  | 'requires_user_input'
  | 'verifying'
  | 'recovering'
  | 'interrupted'
  | 'partially_completed'
  | 'unknown'
  | 'failed'
  | 'cancelled'
  | 'blocked'
  | 'completed';

export interface TaskStep {
  schema_version: number;
  action_id: string;
  contract_fingerprint: string;
  tool_name: string;
  risk: number;
  status:
    | 'planned'
    | 'waiting_user'
    | 'waiting_resource'
    | 'running'
    | 'verifying'
    | 'succeeded'
    | 'failed'
    | 'unknown'
    | 'blocked'
    | 'cancelled';
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  verification_status: 'passed' | 'failed' | 'unknown' | null;
  status_reason: string | null;
}

export interface TaskSnapshot {
  schema_version: number;
  task_id: string;
  request_id: string;
  session_id: string;
  parent_task_id: string | null;
  correlation_id: string;
  goal: string;
  state: TaskState;
  created_at: string;
  updated_at: string;
  status_reason: string | null;
  steps: TaskStep[];
  version: number;
}

export interface ConfirmationRequest {
  schema_version: number;
  confirmation_id: string;
  task_id: string;
  action_id: string;
  risk: number;
  target_summary: string;
  action_summary: string;
  expires_at: string;
  contract_fingerprint: string;
}

export interface TaskDetail {
  schema_version: number;
  task: TaskSnapshot;
  confirmations: ConfirmationRequest[];
  accepts_user_input: boolean;
}

export interface TaskHistoryExport {
  format_version: 1;
  exported_at: string;
  tasks: TaskSnapshot[];
  events: EventRecord[];
  truncated: { tasks: boolean; events: boolean };
}

export interface TaskHistoryClearResult {
  deleted_tasks: number;
  retained_recoverable_tasks: number;
  deleted_events: number;
  deleted_sessions: number;
}

export interface HealthSnapshot {
  schema_version: number;
  status: 'healthy' | 'degraded' | 'unavailable';
  checked_at: string;
  app_name: string;
  app_version: string;
  uptime_seconds: number;
  database_status: CapabilityStatus;
  model_status: CapabilityStatus;
  active_tasks: number;
  queued_tasks: number;
  degraded_reasons: string[];
}

export interface Capability {
  schema_version: number;
  name: string;
  version: string;
  status: CapabilityStatus;
  availability: CapabilityStatus | 'deferred';
  health: 'healthy' | 'degraded' | 'unavailable';
  requirements: string[];
  adapter: string | null;
  limitations: string[];
}

export interface VoiceMetricSnapshot {
  schema_version: number;
  count: number;
  last_latency_ms: number | null;
  max_latency_ms: number | null;
}

export interface VoiceStatusSnapshot {
  schema_version: number;
  state:
    | 'dormant'
    | 'activating'
    | 'listening'
    | 'thinking'
    | 'speaking'
    | 'interrupted'
    | 'executing'
    | 'waiting_for_user'
    | 'deactivating'
    | 'disconnected'
    | 'error';
  microphone_status:
    | 'not_configured'
    | 'unknown'
    | 'available'
    | 'permission_denied'
    | 'unavailable'
    | 'error';
  provider_status:
    | 'unconfigured'
    | 'disconnected'
    | 'connecting'
    | 'connected'
    | 'reconnecting'
    | 'authentication_failure'
    | 'quota_limited'
    | 'network_failure'
    | 'provider_error';
  provider_id: string | null;
  wake_word: string;
  wake_word_enabled: boolean;
  active_session_id: string | null;
  active_task_id: string | null;
  inactivity_timeout_seconds: number;
  last_error_code: string | null;
  updated_at: string;
  telemetry: Record<string, VoiceMetricSnapshot>;
}

export interface DisplayInfo {
  schema_version: number;
  display_id: string;
  width: number | null;
  height: number | null;
  scale: number | null;
  dpi_x: number | null;
  dpi_y: number | null;
  primary: boolean | null;
  availability: CapabilityStatus | null;
}

export interface ActiveWindowInfo {
  schema_version: number;
  available: boolean;
  title: string | null;
  application: string | null;
  process_id: number | null;
  window_id: string | null;
  reason_unavailable: string | null;
}

export interface ApplicationInfo {
  schema_version: number;
  name: string;
  process_id: number | null;
  source: 'running_process' | 'installed_registry' | 'user_provided';
  available: boolean;
}

export interface EnvironmentSnapshot {
  schema_version: number;
  snapshot_id: string;
  created_at: string;
  operating_system: string;
  os_version: string;
  architecture: string;
  cpu_count: number;
  total_memory_bytes: number | null;
  gpu_names: string[];
  displays: DisplayInfo[];
  active_window: ActiveWindowInfo | null;
  running_applications: ApplicationInfo[];
  installed_applications: ApplicationInfo[];
  browsers: string[];
  terminals: string[];
  audio_input_devices: string[];
  audio_output_devices: string[];
  network_status: 'online' | 'offline' | 'unknown';
  unavailable_fields: string[];
}

export interface DiagnosticsSnapshot {
  schema_version: number;
  checked_at: string;
  environment: EnvironmentSnapshot;
  capabilities: Capability[];
  providers: Array<{
    schema_version: number;
    provider_id: string;
    status: CapabilityStatus;
    latency_ms: number | null;
    last_success_at: string | null;
    error_code: string | null;
    model_ids: string[];
  }>;
  database_schema_version: number;
  database_path_kind: 'configured' | 'default';
}

export interface EventRecord {
  event_id: string;
  event_type: string;
  timestamp: string;
  monotonic_timestamp_ns: number;
  task_id: string | null;
  step_id: string | null;
  parent_event_id: string | null;
  session_id: string | null;
  correlation_id: string | null;
  causation_id: string | null;
  source: string;
  severity: 'debug' | 'info' | 'warning' | 'error' | 'security';
  payload: Record<string, unknown>;
  runtime_id: string;
  schema_version: number;
  sequence: number;
}

export interface ServerFrame {
  schema_version: number;
  protocol_version: 1;
  message_id: string;
  type: string;
  payload: Record<string, unknown>;
}

export interface Session {
  schema_version: number;
  session_id: string;
  principal_id: string;
  created_at: string;
  updated_at: string;
  locale: string;
  turns: Array<{
    schema_version: number;
    turn_id: string;
    session_id: string;
    speaker: 'user' | 'assistant' | 'system';
    text: string;
    created_at: string;
    task_id: string | null;
    metadata: Record<string, unknown>;
  }>;
}
