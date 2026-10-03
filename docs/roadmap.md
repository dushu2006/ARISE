# ARISE roadmap

Every phase preserves the core contract: models propose; deterministic policy/executors decide; the environment grounds; a separate verifier establishes results. A capability is not advertised as available until its real adapter and tests exist. Phases can overlap in implementation; exit gates, not phase labels, determine release claims.

Status vocabulary used here: **IMPLEMENTED** means code exists; **TESTED** means automated tests exercise it; **RUNTIME VERIFIED** means exercised against the real integration; **ARCHITECTURALLY PREPARED** means only contracts/adapters exist; **ENVIRONMENT-LIMITED** means required hardware/platform/provider validation was unavailable; **NOT IMPLEMENTED** means no usable feature is present.

## Phase 0 — Safe runtime foundation (implemented/tested; real desktop actions remain gated)

- Typed action/target/condition/authority contracts, policy/risk floors, action-scoped approval grants.
- Explicit task/step state machines including blocked, interrupted, user-input, unknown, partial, cancelled, and verified completion.
- Resource leases, observation leases, executor/verifier ports, event envelopes, in-memory fixtures and SQLite tasks/events.
- Deterministic tests for trust, policy, stale targets, replay refusal, timeout/cancellation, resource contention, and SQLite concurrency.

## Phase 1 — Local control plane and desktop shell (implemented in this checkout; Windows validation pending)

- **Python:** configuration boundary, FastAPI local API, HTTP authentication/origin/request limits, protocol-v1 WebSocket, health/diagnostics/capability discovery.
- **Task engine:** bounded asyncio workers, typed plan validation/topological ordering, task correlation, parent/child lineage scoped to the same principal/session, cancellation/timeouts/recovery, explicit clarification and scoped approval flow. Parent cancellation cascades to active descendants. The backend is authoritative for state.
- **Persistence:** SQLite migrations for tasks, sessions/turns, causal event journal, optimistic versions, redaction, optional memory vectors, and request-ID tombstones. Authenticated task-history export is size-capped; confirmed deletion and opt-in retention preserve active/ambiguous tasks and tombstone replay IDs. WebSocket event replay is paged and capped per connection, with reconnect cursors, future-cursor errors, and a durable replay floor to detect pruned history. `arise backup` provides a user-initiated, integrity-checked SQLite snapshot while the backend is stopped.
- **Model boundary:** `ModelGateway`/router/provider contracts, privacy-aware provider selection, OpenAI-compatible adapter, secret-by-reference credentials. No provider calls by default.
- **Frontend/shell:** React/TypeScript/Vite control room and minimal Tauri 2 shell with a local backend launcher and token bridge; explicit memory and task-history controls are integrated.
- **No fake capabilities:** the production API registers no simulator or host-control action tools. Windows UIA, browser DOM/CDP, and vision/OCR remain unavailable. Playwright is not registered by default. Voice can be composed only behind explicit local-model/microphone and Gemini/cloud gates; memory writes are consent-gated and research is egress-gated. Optional capabilities do not imply runtime validation.

**Phase 1 validation still required:** run the configured backend/frontend/Windows CI jobs, package and launch on supported Windows/WebView2, and inspect Windows token/data-directory ACL behavior before any support claim. No claim of Windows automation or installer support before real Windows validation.

## Phase 2 — Deterministic Windows and browser adapters (in progress; environment-limited)

**Current status:** authenticated, user-requested diagnostics now reports bounded process names/PIDs, Windows display-adapter/monitor/effective-DPI/foreground metadata, installed-app names, browser/terminal classifications, and optional audio-device names without opening streams or reading process arguments. Tests include real psutil process enumeration plus FAKE Windows/PortAudio probes; Win32/Windows-host behavior remains unvalidated. An optional isolated Playwright prototype reuses the existing runtime, with bounded DOM snapshots, page-scoped target identities and resource leases, URL redaction/validation, secret references, pre-dispatch freshness checks, scroll freshness, and popup-page registration. It is not registered by the API; tests use FAKE Playwright objects and a real browser has not been exercised. Windows UIA and real platform integration remain outstanding.

- Validate the new environment probes on supported Windows, then implement a `DesktopAutomationAdapter` using UI Automation/accessibility first, with process/window identity, display/DPI/focus tracking, target uniqueness, state invalidation, modal/interrupt detection, and human-interference checks.
- Complete the browser adapter's release gates: real Playwright/Chromium compatibility, account/profile scope, robust navigation egress controls, authenticated approval preview, failure reconciliation, production registration, and capability reporting.
- Renew/invalidate observation leases on UIA/DOM events; declare physical input/window/browser/profile/clipboard resources; keep physical input serialized.
- Keep OCR/vision/coordinates as explicitly lower-confidence fallbacks; never allow a model to click directly.

**Exit checks:** Windows CI/sandbox tests for stale/wrong window-tab-account, duplicate controls, monitor/DPI/focus changes, modal interruption, cancellation, expiry, and human interference. Test real supported Windows builds.

## Phase 3 — Dormant-first voice and conversation bridge (gated composition implemented; runtime validation pending)

**Current status:** provider-neutral `AudioHub`, PortAudio capture/playback, WebRTC VAD, Vosk wake/streaming ASR, Kokoro ONNX TTS, typed voice contracts, optional Gemini Live, and `VoiceConversationBridge` through the existing `TaskEngine` are implemented. The server now composes them only when voice/microphone settings, a user-supplied local Vosk path and dependencies, keyring credential/SDK, and both voice/security cloud opt-ins pass. Authenticated `/api/v1/voice/listening/start` and `/stop` controls are explicit; start checks local dependencies and loads the model before audio capture, stop closes monitoring without cancelling an admitted task, and lifecycle journal payloads contain only state/error codes. The app remains dormant until the authenticated explicit start; cloud audio remains behind local wake. In addition to fake lifecycle/preflight tests, a bridge integration test runs the real in-process TaskEngine/AgentRuntime/PolicyEngine/resource/verifier chain against a FAKE in-memory environment/tool and confirms verified status is withheld until the verifier passes. No real device, local inference, Gemini session, host action, or Windows run has been validated.

`arise-voice-check` reports 21 public stages with nested probes and explicit `REAL`/`FAKE`/`REPLAY` evidence plus `PASS`/`PARTIAL`/`FAILED`/`SKIPPED`/`BLOCKED` outcomes. The Linux harness is host-guarded and does not use real hardware/provider adapters. Its task-admission exercise uses a fake port and intentionally does not invoke a real TaskEngine task; treat it as bridge behavior evidence only. See [the voice validation guide](voice-validation.md) for validation scopes and limits.

- Keep all pre-wake capture local and ephemeral. Open a cloud session only after a local wake detector accepts; Vosk enforces a configurable confidence threshold and bounded wake handoff. Configure inactivity shutdown and keep playback interruption distinct from task cancellation.
- Treat Gemini as replaceable conversational I/O only. Its narrow bridge can submit/query/clarify/cancel through the existing `TaskEngine`; it cannot invoke shell/browser/desktop tools or bypass policy, resources, executor, or verifier. Deterministic intent and transcript agreement gate task admission; explicit intent gates cancellation. Spoken completion requires the runtime's verified `COMPLETED` state.
- Poll task state through the bridge for bounded progress summaries. Coalesce updates while a conversational turn is in flight; never cancel AgentRuntime work merely because speech playback or a provider session stops.
- Keep raw audio/transcripts/resumption tokens out of logs and persistence by default. Retain a conservative speech-output gate for task/control claims.

**Exit checks:** run Linux CI and Windows fake/synthetic tests separately from Windows real-device and live-Gemini evidence; audit the sanitized report; validate permission denial, bounded capture/playback, local VAD/wake before cloud audio, local-model behavior, streaming response, repeated barge-in, cancellation and cleanup, and Gemini quotas/auth errors. Add network-failure and device-loss injection before claiming recovery. Keep voice dormant by default and capability reporting truthful. No voice runtime claim is `RUNTIME VERIFIED` until tested on the intended Windows audio/model/provider configuration.

## Phase 4 — Planning, routing, and research (software implemented/tested; live providers not verified)

**Current status:** typed `ModelProvider`/router contracts, privacy/cloud policy, the OpenAI-compatible adapter, task-plan validation, and a deterministic advisory intent classifier are implemented. The text composer now calls `/api/v1/interactions`: clear commands enter the existing TaskEngine; questions never create tasks and use `FAST_REASONER` through ModelRouter. Current-information questions require one-time web consent; retrieved sources are sent as untrusted context and returned with provenance. Conversation/session writes and standalone research queries/source fields receive best-effort credential-shaped redaction. Tests exercise this path through the real API/session/TaskEngine composition with fake providers; no live informational/planning provider or Brave request has been verified. The router journals redacted request-started, response-completed, failure, fallback, and cancellation events; the complete-response provider contract cannot report a separately timed response-start event. The bounded Brave research adapter is gated by both research and network-egress opt-ins plus an OS-keyring credential. Queries are user initiated; bounded results are provenance-tagged untrusted context and never action authority.

- Improve event-driven planning with budgets, pre/postconditions, progress/no-progress detection, replanning checkpoints, robust clarification, and deterministic fallback/routing.
- Continue prompt/tool-output injection, schema fuzzing, outage/fallback, spend/latency, provenance/freshness, and independent fact-verification checks before expanding any live provider path.
- Keep secrets by reference, explicit cloud opt-ins, and research content untrusted. Do not allow model or retrieved content to directly authorize or execute an action.

**Exit checks:** deterministic provider replay, prompt/tool-output injection suite, schema fuzzing, outage/fallback, spend/latency budgets, provenance/freshness evaluation, and independent fact verification. Mark provider/research runtime **ENVIRONMENT-LIMITED** until exercised against the intended provider with valid opt-in and safe test data.

## Phase 5 — Consent-governed memory (local product controls implemented; optional embeddings runtime unverified)

**Current status:** local SQLite memory records, principal-scoped single-use consent bound to exact content/expiry, bounded retention, lexical retrieval, and user-facing inspect/search/export/delete/clear controls are implemented and tested. Writes require an explicit user action and consent; automatic memory suggestions/writes remain disabled. Optional embedding vectors are persisted with model IDs and used only when compatible; failures/incompatible vectors fall back to lexical ranking. Cloud memory context/embeddings require layered opt-ins and redaction. No live embedding endpoint or cloud memory request has been verified.

- Keep each write principal-scoped, unexpired, single-use, explicit, allowlisted, bounded, and redacted; preserve expiry and user-controlled deletion.
- Return retrieved memories with provenance and confidence as untrusted context. Never turn a retrieved memory into authority or a new action without current user intent and policy checks.
- Continue consent replay/concurrency/expiry/wrong-owner, retention/deletion, prompt-injection, embedding compatibility, and privacy tests. Review database-at-rest protection/access controls before deployment.

**Exit checks:** consent and deletion tests, data-minimization/security review, prompt-injection boundaries, compatible-vector/fallback tests, and user-visible history/revocation. Local lexical memory is implemented; optional embedding/cloud execution remains **ENVIRONMENT-LIMITED** until an actual endpoint run.

## Phase 6 — Tool catalog, filesystem, terminal, and recovery

- Add schemas, static risk floors, capability scopes, resources, side effects, idempotency, and independent verification descriptors to real tool registrations.
- Add safe filesystem/process/terminal tools with preview/dry-run, path/link validation, bounded output, secret redaction, timeouts, and exact destructive approvals.
- Add operation IDs, reconciliation for external side effects, checkpoints, safe recovery, and external receipts.

**Exit checks:** fault injection for timeout-after-side-effect, duplicate operation, crash, locked files, symlink/junction escape, command output leak, and lease expiry. Do not wire tools into the production API before policy and verifier gates pass.

## Phase 7 — Production hardening and release

- Build/supervise a signed Windows installer/sidecar, crash-loop protection, update/rollback, database backup/restore, database/key protection, retention policy, and controlled logs. The existing task/memory exports and user deletion controls do not replace database-level recovery or release hardening.
- Run multi-hour soak, resource leaks, load/fairness, real GPU/display/browser/audio validation, and independent security review.

**Release gate:** no claim of supported Windows automation, browser control, voice, provider, memory, research, or installer behavior without matching Windows/CI/hardware/security evidence.
