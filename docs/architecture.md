# ARISE architecture and safety contract

## 1. Phase 1 implementation boundary

Phase 1 supplies a local control plane and a minimal desktop shell, not a general-purpose autonomous computer-use agent.

```text
React/TypeScript UI (Vite; Tauri WebView in desktop mode)
       │ relative HTTP and WebSocket v1 frames
       ▼
FastAPI local gateway ─ auth/origin/size limits ─ event replay/fanout
       │
       ├── TaskEngine ─ bounded queue/workers ─ PlannerPort
       │                                    └── typed plan validation
       ├── AgentRuntime ─ policy ─ resource leases ─ observe/execute/verify ports
       ├── ModelRouter ─ provider capability/privacy filters ─ ModelProvider
       ├── Optional AudioHub ─ local VAD/wake ─ provider-neutral live session
       │          └── VoiceConversationBridge ───────────────► TaskEngine
       ├── CapabilityService / HealthService / EnvironmentDiscovery
       └── SQLite adapters: tasks, sessions/turns, event journal
```

The domain/runtime code does not depend on FastAPI, SQLite, Tauri, Windows APIs, browser libraries, or a model vendor. API models and domain contracts are typed; adapters implement the ports. The optional `AudioHub`/`VoiceConversationBridge` path is not composed by default. It is composed only when the explicit voice/microphone gates, local Vosk model path and dependencies, Gemini keyring credential, SDK, and both cloud-policy settings are present. Authenticated listening start/stop routes remain explicit; start preflights dependencies and loads the local model before opening audio. This is tested only at fake/preflight level, not real-device/provider validation.

The production API never registers the simulator. It conditionally registers Win32 UIA tools (`ARISE__DESKTOP__ENABLED=true`), Playwright tools (`ARISE__BROWSER__ENABLED=true` plus allowed domains), and a screenshot/OCR/vision resolver (`ARISE__PERCEPTION__ENABLED=true`); all are disabled by default. If planning is unavailable, a task becomes `REQUIRES_USER_INPUT`; if no configured tool/environment capability exists, the deterministic runtime blocks rather than claiming success. The separate `arise demo` command is explicitly a test simulator. Conditional registration and fake-backend tests do not establish live Windows/browser capability.

Worker limits, resource leases, and confirmation grants are process-local. Each file-backed database can opt into a non-blocking OS lock adjacent to the SQLite file; the API backend acquires it before opening the SQLite connection or running migrations and releases it after database shutdown cleanup. A second current ARISE backend using the same database path is refused, even if it chooses a different API port. The lock file contains no task data and the OS releases its lock after process exit. The Tauri launcher separately refuses an occupied default API port. This is single-process ownership, not a durable per-task claim, and Windows/network-filesystem locking behavior still requires platform validation.

## 2. Authority, trust, and model boundary

- `AuthorizationContext` is minted by `TaskEngine.submit` from the authenticated local principal and request ID. Capabilities are supplied by trusted backend configuration (empty by default).
- A model receives registered tool descriptions and returns a strict `TaskPlan`; model-provided task ID, goal, planner attribution, authority, approval, and execution results are not trusted.
- Each `ActionProposal` is converted to a domain `ActionContract` with task authority injected by the engine. Tool-owned risk floors, required capabilities/resources, exact authority matching, target identity, preconditions, policy, and verification still apply.
- Model output and external/user-quoted content are untrusted data. Neither can grant capability, authorize side effects, waive confirmation, or establish evidence.
- The router defaults to local-only selection. Cloud requests require both model and security opt-ins and HTTPS. Credentials resolve by secret name through OS keyring or explicitly enabled development environment lookup; they are not stored in SQLite or included in events.

## 3. Task orchestration and lifecycle

`TaskEngine` owns bounded task admission, asynchronous workers, request/task correlation, plan structure checks, cancellation, crash recovery, clarification, and one-action approval resume. A child request is admitted only under a live parent owned by the same principal and session; `parent_task_id` is persisted with the child, included in the child request fingerprint, exposed in typed snapshots, and active descendants are cancelled before their parent. Runtime state is stored on `TaskRecord`; the UI is only a client projection.

The plan validator rejects duplicate/missing IDs, self-dependencies, cycles, mismatched task/goal, oversized plans, and invalid typed action contracts. Plans run in topological order. Each action delegates to `AgentRuntime`, which owns policy, grounding, resources, dispatch, and independent verification.

Task/step results remain semantically distinct:

- `COMPLETED` requires verified final postconditions.
- `PARTIALLY_COMPLETED` means a verified step is not the plan's final step.
- `WAITING_USER` means an exact, scoped approval is pending.
- `REQUIRES_USER_INPUT` means planning needs a clarification or a dependency/configuration.
- `BLOCKED`, `FAILED`, `CANCELLED`, `INTERRUPTED`, and `UNKNOWN` are not successes.
- A timeout/cancellation after dispatch may have begun becomes `UNKNOWN`; it is never blindly retried.

Action parameters and full plans are deliberately not persisted. On restart, queued work without side effects can be replanned from a redacted request summary; work that may have dispatched becomes interrupted/non-runnable and must be reconciled/replanned.

## 4. Runtime execution invariants

An action reaches a tool only after:

1. A registered adapter is found and supplies trusted `ToolSpec` metadata.
2. The proposed authority exactly matches the task-owned authority.
3. Tool parameters pass adapter validation and deterministic policy permits/has exact current approval.
4. Resources are acquired atomically with bounded waits and expiring leases.
5. A fresh observed/retrieved environment lease matches target identity and satisfies preconditions.
6. The observation and resource lease remain current immediately before dispatch.
7. The tool executes under a bounded timeout; an ambiguous external outcome becomes `UNKNOWN`.
8. A separate verifier observes declared postconditions; tool/model success claims are ignored.

With explicit settings, the production composition can register real Win32 UIA and Playwright providers; this Linux workspace has not executed them against Windows or Chromium. `InMemoryEnvironment` and `SetFactTool` remain test fixtures only and are never registered as production desktop capabilities. The perception endpoint resolves and returns an untrusted target proposal; it does not itself authorize or dispatch actions, and the current end-to-end action path still needs a real supported-host validation. A verifier cannot return `PASSED` without at least one traceable `OBSERVED`/`RETRIEVED` evidence record; malformed/evidence-free pass claims become `UNKNOWN`. Full evidence records are currently returned only in-process: task snapshots persist verification status and events record level/count, not evidence artifacts. Before consequential live tools are released, add privacy-reviewed durable evidence references/summaries and expose enough of them for a user to inspect why completion was claimed.

## 5. Policy and approvals

Policy uses the greater of proposed risk and trusted adapter minimum risk. `R3` always requires confirmation; `R4` is denied by default. Consequential actions require a semantic target and explicit postconditions. User approval binds to exact task/action IDs, contract fingerprint, authenticated owner, issue/expiry time, and one-time consumption immediately before dispatch.

The Web UI receives only confirmation metadata (action/target summary, risk, expiry, fingerprint), never the opaque grant. The server verifies the principal from the local bearer token and issues/consumes the grant in-process. Pending approvals are not restored after restart; they expire and require a fresh user instruction/replan.

## 6. Persistence, event traceability, and privacy

SQLite uses WAL on file-backed databases, short transactions, optimistic task versions, and schema migrations:

- **v1:** task snapshots and event journal.
- **v2:** event correlation and causation IDs.
- **v3:** sessions and conversation turns.
- **v4:** principal/session-scoped request-ID mappings for idempotent task admission.
- **v5:** optional request-content fingerprints on new idempotency mappings. New admissions reject reuse of an ID for different normalized, redacted content; legacy mappings migrate with a null fingerprint, so their original content cannot be compared retroactively.
- **v6:** principal-scoped memory records and single-use consent grants.
- **v7:** optional embedding vectors and model IDs for semantic memory ranking.
- **v8:** task-request tombstones, so deleting a terminal task does not allow a replayed request ID to recreate its task.
- **v9:** a durable event replay floor, so reconnects detect when history deletion made an older cursor incomplete.

Task snapshots contain state, identity/correlation IDs, safe status reasons, and step status/fingerprints, not raw action parameters. Conversation turns and task goals use a best-effort redactor for common credential shapes before persistence. The redactor is not a secret scanner; users should not paste credentials.

Audit events carry event/task/step/session IDs, correlation/causation IDs, UTC and monotonic timestamps, runtime/source/severity, bounded payload, and store-assigned sequence. Important task transitions are appended before live broker fanout. WebSocket subscribers have bounded queues; an overflow disconnects the client, which can replay from its last durable sequence. Durable replay is paged and capped at 5,000 events per connection by default; on overflow the server returns the last replayed sequence and closes so the client can reconnect and continue. A cursor ahead of the durable high-water mark returns `INVALID_EVENT_CURSOR`; a cursor below the durable replay floor returns `EVENT_CURSOR_EXPIRED`, prompting task-state refresh before reconnect. The high-water mark and replay floor survive event-history deletion. The authenticated task-history export returns task snapshots plus task-scoped events, with an 8 MiB/10,000-event cap and explicit truncation flags. Confirmed history deletion removes only settled `COMPLETED`, `CANCELLED`, `FAILED`, and `BLOCKED` tasks, their events, and their task-linked conversation turns; active, `PARTIALLY_COMPLETED`, `UNKNOWN`, `INTERRUPTED`, and otherwise unresolved tasks are preserved for continuation/reconciliation. An otherwise-empty session is deleted only when it has no remaining task mappings, turns, or events. Tombstones retain request identity—not task content—to prevent replay recreation. Automatic retention is disabled by default; `ARISE__RUNTIME__TASK_HISTORY_RETENTION_DAYS` opts into daily pruning of settled outcomes only. This is not a general session/transcript purge. `arise backup` uses SQLite's online backup API, verifies the snapshot, and publishes a no-overwrite same-directory file while holding the single-instance lock; the operator must stop the backend first and protect the resulting copy. Replay from sequence zero and remaining session history still scale with stored data.

The API bearer token is kept outside SQLite and logs. If no token is configured, a random token is created in the local user data directory. CORS and WebSocket origins are restricted; the development-preview origin rule is narrow and disabled in production. `/healthz` is the only unauthenticated HTTP endpoint.

## 7. HTTP and WebSocket contracts

HTTP API routes are under `/api/v1`. Request sizes are bounded; validation failures use generic safe responses. Authentication uses constant-time bearer comparison. The one-user local build maps the token to a local principal; multi-user auth/IPC isolation is future hardening.

The text composer calls `/api/v1/interactions`. Deterministic intent classification sends only sufficiently clear action requests to the existing `TaskEngine`; questions and conversation requests use `FAST_REASONER` through `ModelRouter` and have no tools or execution authority. The route persists length-bounded, redacted user/assistant turns and is idempotent by request ID. Status requests query authoritative session-scoped task state; cancellation requires a unique active task in that session and goes through `TaskEngine.cancel`. Time-sensitive questions without one-time research consent ask the user to opt in. With consent, the route retrieves bounded public sources, sends them as untrusted context for synthesis, and returns their provenance; if either the search or answer provider is unavailable, it returns a truthful no-task response. The standalone research endpoint also applies credential-shaped redaction before sending the query and before returning source fields. This is not real-provider validation; see the capability/environment checklist.

WebSocket `/ws/v1` requires a protocol version 1 `client.hello` frame with the token and last event sequence. Commands include task submit/respond/cancel/approve, event subscribe, ping, and goodbye. Frames are Pydantic-validated; unknown/invalid frames do not reach domain execution. Credentials are not sent in URLs or logged.

Frontend API calls use relative paths. Vite proxies HTTP/WebSocket routes to the backend; browser code never calls a hard-coded loopback address.

## 8. Model gateway/router

`ModelRouter` is provider-neutral. It selects registered provider/model pairs by role, modality, privacy, cloud opt-in, preferred provider, bounded concurrency, and circuit status. An unprobed provider is reported degraded; provider failures are sanitized. When the server supplies the event store, the router records correlated request-started, response-completed, failed, fallback-selected, and cancelled events using provider/model IDs and safe error codes only; prompt/response bodies and credentials are excluded. The current provider protocol returns complete responses rather than streaming chunks, so it cannot report an independently timed `MODEL_RESPONSE_STARTED` event. `OpenAICompatibleProvider` is one replaceable HTTP adapter; HTTP errors, response shapes, timeouts, and credentials are bounded and not exposed to users.

`GatewayTaskPlanner` requests JSON and validates it as a `TaskPlan`; it assigns canonical task identity/goal/planner attribution. It cannot execute tools. The advisory surface-form intent classifier keeps instructional questions such as “Tell me how to open Chrome” out of task admission even when they contain action verbs; it is not an authorization mechanism. The default deployment has no provider configured and no production tools, so task acceptance never implies a model response or successful desktop operation.

## 9. Health, environment, and capability discovery

The authenticated diagnostics endpoint runs bounded host discovery off the event loop. `EnvironmentDiscovery` reports OS/release/architecture/CPU/memory, a capped process-name/PID inventory, installed-app display names from the Windows uninstall registry, and browser/terminal classifications. On Windows, a Win32 probe reports attached display-adapter names, monitor dimensions/primary status/effective DPI, and a redacted foreground-window summary. If `sounddevice` is installed, it enumerates input/output device names without opening streams. It never reads process arguments, environment variables, registry install paths, browser profiles, or network history. Unsupported or failed probes stay in `unavailable_fields`; network status remains `unknown` without an egress probe. This is read-only discovery—not UI Automation, screenshot capture, or action capability—and Windows runtime/DPI behavior still needs supported-host validation.

Capabilities are truthful and versioned. Core task orchestration and SQLite may be available; model planning requires configuration or is degraded until a successful provider request. Windows UIA and the perception resolver are disabled by default and become registered only under their explicit settings; a missing Windows host or OCR/vision model/provider prevents real execution. The voice path is disabled/configuration-required by default. If all explicit composition gates pass it can be controlled through authenticated start/stop routes, but that availability is configuration—not runtime evidence—and no real audio/provider run has been verified here. Local memory is available only for explicitly consented writes; semantic embeddings remain optional with lexical fallback. Brave research is unavailable unless both egress opt-ins and the keyring credential are configured; results remain untrusted context. Playwright tools are registered only when enabled with allowed domains; the default browser capability remains unavailable. Capability status is not inferred from OS names alone.

## 10. Tauri shell and Windows boundary

The Tauri 2 shell has a minimal permission set, per-user Windows installer targets, a local backend launcher, and a Tauri command that supplies the API token without exposing it to logs. On each desktop launch the shell generates a fresh token with the OS CSPRNG, passes it to the backend through the child environment, and retains it in memory for bounded backend restarts. The backend binds loopback; the launcher waits for an explicit readiness signal from its child and refuses to reuse an occupied API port. On shutdown, the shell closes the backend stdin pipe, waits up to three seconds for graceful server/database cleanup, then kills and reaps a hung child. The native shell does not parse the Python backend's `.env`; desktop data-directory overrides must be provided in the process environment. The shell is not a shell-execution bridge and exposes no arbitrary command API to React. Windows runtime behavior remains unvalidated here.

Cargo and Windows/WebView2 tooling are unavailable in this Linux environment. Windows CI on the current branch head (run `37185186839` on `e4b81d6`, and run `37184893771` on `928bc88`) passed all six jobs: `backend (windows-latest, 3.11)`, `backend (windows-latest, 3.12)`, both Ubuntu backend jobs, `frontend`, and `windows-tauri-compile` (which compiles the Tauri shell and runs the Rust CSPRNG token/restart-helper tests). The immediately preceding run `37184468477` on `bc437f8` failed all four backend jobs and is what exposed a browser-discovery defect that local Linux runs had passed; that history is recorded in `docs/windows-release-validation-checklist.md` item N7. CI did not launch the Tauri desktop or exercise real UIA/audio/Chromium hardware. Sidecar build, native ACLs, WebView2 runtime, installer execution, and interactive Windows behavior remain unvalidated. A Windows CI test pass or compile-only check must not be described as full Windows support.

## 11. Future adapter ports

Extension boundaries exist for `EnvironmentPort`, `ActionTool`, `VerifierPort`, `ModelProvider`, `TaskPlanner`, `SessionRepository`, `EventStore`, resource coordination, voice adapters in `core.voice`, and the `MemoryPort`/`WebResearchPort` contracts in `core.extensions`. Voice payload chunks are bounded and ephemeral; memory writes require a consent reference and expiry; retrieved memory/research content is provenance-tagged, untrusted context with no action authority. Informational answers may receive relevant saved memory as local untrusted context; cloud-eligible routing omits it unless the separate memory-context opt-in and model/security cloud gates are all enabled. Voice, memory, and research remain opt-in/disabled in the default API composition. The voice architecture is described in ADR-0003; its real microphone, local wake/VAD, playback, and Gemini runtime gates remain open. Local memory and gated Brave research are implemented in separate adapters, with optional embeddings and remaining live-provider/security validation documented in the roadmap. Remaining release work is validation rather than an assumption of capability:

- Run real Win32 UIA and mixed-DPI/focus/interference tests on a supported interactive Windows desktop.
- Install Playwright/Chromium and validate navigation, egress, recovery, verification, and cleanup on the target OS.
- Validate screen capture and local/provider OCR/vision with actual images; the resolver returns untrusted proposals and is not an action-authority path.
- Run real Windows microphone/speaker/device-loss tests with user-supplied Vosk/Kokoro files; run Gemini/Brave/model checks only after explicit keyring configuration.
- Build and launch the packaged Tauri app, test per-launch token/readiness/restart/shutdown, WebView2, clean-user installation, and native ACL behavior.
- Database-at-rest protection, deeper memory/research privacy review, and independent security review remain separate hardening needs; automatic memory writing remains disabled.

Preferred grounding remains official API → browser DOM/CDP → Windows UI Automation/accessibility → OCR/layout → vision → coordinates. Consequential actions must remain policy-gated, target-grounded, approval-scoped, and independently verified.

## 12. Phase 2 adapter: optional Playwright browser automation

`adapters/browser_playwright.py` is an optional adapter, not a production capability claim. Importing it does not import Playwright or start a browser. The caller must explicitly install the `browser` extra and call `await PlaywrightBrowserProvider.start()`. The provider launches a new, non-persistent Chromium context, disables downloads and service workers, uses opaque page IDs, and caps pages, DOM elements, inspected text, and observation-cache entries. It never attaches to the user's normal browser profile.

The DOM snapshot produces semantic role/name/label/test-id candidates with visibility and enabled state, selector quality, CSS viewport bounds, and page-scoped identities. It does not read form values; password/sensitive field metadata includes the HTML input type and a sensitive flag. `fill_secret` requires both a `SecretRef` and a candidate marked sensitive. Browser URLs are HTTP(S)-only, embedded credentials are rejected, private/loopback destinations are denied by default, and tab/results expose redacted URLs. Applications that opt into private hosts assume the resulting network-security risk.

Each page observation is short-lived and tied to a hash of its URL, title, and bounded DOM state. The adapter rechecks that lease, target identity, uniqueness, visibility, and enabled state before dispatch. Browser action tools have operation-specific static risk floors and capabilities; the runtime acquires an adapter-computed `browser.page.<id>` resource in addition to contract/tool resources. Click, fill, select, key, and navigation timeouts after dispatch may have begun are reported as unknown; they must be reconciled, never replayed blindly. Postconditions are still verified through the runtime's independent `VerifierPort`.

Tests use fake page/locator/route objects and validate contracts, privacy guards, staleness, secret references, risk floors, resource wiring, DNS checks, and WebSocket route validation. They do not prove Playwright protocol compatibility or real Chromium behavior. The API composition root conditionally registers these tools when browser settings and allowed domains are configured. DNS binding and WebSocket egress checks are implemented but their actual network enforcement is not yet validated with Chromium; Windows/browser behavior has not been run on a supported Windows host. Treat the adapter as unvalidated for sensitive accounts until those gates pass.

## 13. Phase 3 voice and conversation architecture

The voice path is an optional composition, not another task runtime:

```text
local microphone → VAD → local wake detector → AudioHub → LiveConversationProvider
                                                   │           (optional Gemini adapter)
                                                   ├── local playback / barge-in
                                                   └── VoiceConversationBridge → TaskEngine → AgentRuntime
```

`AudioHub` stays dormant before a local wake result. It does not send pre-wake audio to any provider, and wake handoff is bounded/ephemeral. The controller owns microphone lifecycle, inactivity shutdown, reconnect handling, playback interruption, stage-specific progress updates, and redacted latency/state telemetry. On barge-in it suppresses output immediately, stops playback, and asks the live provider to interrupt generation while forwarding the first locally detected speech frame. Provider output is tagged with a per-session generation; stale or untagged audio/transcript events from before the interruption are discarded. Provider/session shutdown clears the fence and stops audio but leaves the independent task watcher running while the hub remains alive, so task state can continue to update while voice is dormant. Closing `AudioHub` cancels its watcher tasks; neither action implies rollback or cancels a dispatched `TaskEngine` operation. Only the explicit `cancel_task` bridge call asks the task engine to cancel, and its returned state remains authoritative.

`GeminiLiveProvider` is optional, lazy-imported, and behind `LiveConversationProvider`. It needs an application `SecretProvider` credential and explicit voice/cloud plus security/cloud opt-ins. The bridge's narrow tool vocabulary can submit a typed voice request or query/clarify/cancel a task; it grants no shell, browser, desktop, or direct executor access. Task admission requires deterministic task intent plus exact normalized agreement with the current spoken transcript, and the bridge submits that transcript rather than trusting the model's paraphrase. Explicit cancellation likewise requires a deterministic cancellation intent; `AudioHub` accepts at most one task submission per spoken turn. Runtime-derived status summaries are the sole allowlist for guarded task speech; verified completion is relayed only when the runtime returns `COMPLETED` after verification. A supplementary deterministic heuristic catches common completion claims even when the intent classifier misses a control request; audio without an associated transcript is muted. Task progress messages are coalesced until a current model turn completes so they do not interrupt speech. Raw audio, transcript text, and Gemini resumption handles are not written to logs or persistent storage by default.

Authenticated `/api/v1/voice/status`, `/api/v1/voice/listening/start`, and `/api/v1/voice/listening/stop` routes report and control voice lifecycle. Composition requires explicit voice and microphone settings, a user-supplied Vosk model path, local dependencies, a Gemini keyring credential/SDK, and both voice/security cloud opt-ins. Merely configuring or composing the hub does not start capture. The start route checks `sounddevice`, `webrtcvad`, and `vosk`, loads the model before opening audio, and returns a bounded error code without capturing on preflight failure; lifecycle events contain state/error codes only. Stop closes microphone monitoring without cancelling admitted TaskEngine work. Capability health distinguishes disabled, configuration-required, and unavailable states; configuration is not runtime evidence. ASR partials and low-confidence finals do not authorize task admission; a final provider transcript must agree with the final local segment after normalization, and the local segment is canonical. Capture callback overflow drops stale frames with diagnostics, while ASR queue overflow invalidates local transcript admission. Model files remain user-supplied.

The test suite uses fakes/replay streams, including repeated barge-in and stale-generation replay. It does not validate Windows permissions/audio hardware, real local model behavior/performance, Gemini sessions, or provider transcript/audio segmentation; these remain **ENVIRONMENT-LIMITED**. `arise-voice-check` reports exactly 21 stages with nested probes, distinguishes `REAL`/`FAKE`/`REPLAY`, and uses `PASS`/`PARTIAL`/`FAILED`/`SKIPPED`/`BLOCKED`. A Linux run is host-guarded: it does not construct hardware/provider adapters or stream audio, but can report deterministic replay and installed WebRTC VAD synthetic-fixture results; Windows-required probes are blocked rather than impersonated. The harness intentionally does not invoke a real TaskEngine task, so its task-admission result is not evidence of production server wiring. Server tests cover authenticated lifecycle routes and preflight failure only. The aggregate Gemini harness limits are four session attempts, 1 MiB input audio, 2 MiB output audio, 16,384 transient transcript characters, and a 60-second end-to-end turn. The reconnect probe may exercise a fresh session after clean close, and the audio probe may exercise clean device close/reopen; neither injects network failure nor hot-unplug. No real Windows audio device or Gemini session has been run here. See [the voice validation guide](voice-validation.md) and [ADR-0003](adr/0003-dormant-first-voice-runtime.md) for prerequisites, commands, evidence scope, and open gates.
