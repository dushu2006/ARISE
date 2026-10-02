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
       ├── CapabilityService / HealthService / EnvironmentDiscovery
       └── SQLite adapters: tasks, sessions/turns, event journal
```

The domain/runtime code does not depend on FastAPI, SQLite, Tauri, Windows APIs, browser libraries, or a model vendor. API models and domain contracts are typed; adapters implement the ports.

The production API registers **no simulator and no desktop/browser action tools**. If planning is unavailable, a task becomes `REQUIRES_USER_INPUT`; if no real tool/environment capability exists, the deterministic runtime blocks rather than claiming success. The separate `arise demo` command is explicitly a test simulator.

Worker limits, resource leases, and confirmation grants are process-local. Each file-backed database can opt into a non-blocking OS lock adjacent to the SQLite file; the API backend acquires it before opening the SQLite connection or running migrations and releases it after database shutdown cleanup. A second current ARISE backend using the same database path is refused, even if it chooses a different API port. The lock file contains no task data and the OS releases its lock after process exit. The Tauri launcher separately refuses an occupied default API port. This is single-process ownership, not a durable per-task claim, and Windows/network-filesystem locking behavior still requires platform validation.

## 2. Authority, trust, and model boundary

- `AuthorizationContext` is minted by `TaskEngine.submit` from the authenticated local principal and request ID. Capabilities are supplied by trusted backend configuration (empty by default).
- A model receives registered tool descriptions and returns a strict `TaskPlan`; model-provided task ID, goal, planner attribution, authority, approval, and execution results are not trusted.
- Each `ActionProposal` is converted to a domain `ActionContract` with task authority injected by the engine. Tool-owned risk floors, required capabilities/resources, exact authority matching, target identity, preconditions, policy, and verification still apply.
- Model output and external/user-quoted content are untrusted data. Neither can grant capability, authorize side effects, waive confirmation, or establish evidence.
- The router defaults to local-only selection. Cloud requests require both model and security opt-ins and HTTPS. Credentials resolve by secret name through OS keyring or explicitly enabled development environment lookup; they are not stored in SQLite or included in events.

## 3. Task orchestration and lifecycle

`TaskEngine` owns bounded task admission, asynchronous workers, request/task correlation, plan structure checks, cancellation, crash recovery, clarification, and one-action approval resume. Runtime state is stored on `TaskRecord`; the UI is only a client projection.

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

The current API has no live environment adapter or action tools, so these checks fail closed for live automation. `InMemoryEnvironment` and `SetFactTool` remain development fixtures only. A verifier cannot return `PASSED` without at least one traceable `OBSERVED`/`RETRIEVED` evidence record; malformed/evidence-free pass claims become `UNKNOWN`. Full evidence records are currently returned only in-process: task snapshots persist verification status and events record level/count, not evidence artifacts. Before live adapters, add privacy-reviewed durable evidence references/summaries and expose enough of them for a user to inspect why completion was claimed.

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

Task snapshots contain state, identity/correlation IDs, safe status reasons, and step status/fingerprints, not raw action parameters. Conversation turns and task goals use a best-effort redactor for common credential shapes before persistence. The redactor is not a secret scanner; users should not paste credentials.

Audit events carry event/task/step/session IDs, correlation/causation IDs, UTC and monotonic timestamps, runtime/source/severity, bounded payload, and store-assigned sequence. Important task transitions are appended before live broker fanout. WebSocket subscribers have bounded queues; an overflow disconnects the client, which can replay from its last durable sequence. There is no retention, pruning, backup, or export policy yet: reconnect replay from sequence zero and session transcript reads scale with accumulated history, and local disk growth is unbounded until the user removes local state.

The API bearer token is kept outside SQLite and logs. If no token is configured, a random token is created in the local user data directory. CORS and WebSocket origins are restricted; the development-preview origin rule is narrow and disabled in production. `/healthz` is the only unauthenticated HTTP endpoint.

## 7. HTTP and WebSocket contracts

HTTP API routes are under `/api/v1`. Request sizes are bounded; validation failures use generic safe responses. Authentication uses constant-time bearer comparison. The one-user local build maps the token to a local principal; multi-user auth/IPC isolation is future hardening.

WebSocket `/ws/v1` requires a protocol version 1 `client.hello` frame with the token and last event sequence. Commands include task submit/respond/cancel/approve, event subscribe, ping, and goodbye. Frames are Pydantic-validated; unknown/invalid frames do not reach domain execution. Credentials are not sent in URLs or logged.

Frontend API calls use relative paths. Vite proxies HTTP/WebSocket routes to the backend; browser code never calls a hard-coded loopback address.

## 8. Model gateway/router

`ModelRouter` is provider-neutral. It selects registered provider/model pairs by role, modality, privacy, cloud opt-in, preferred provider, bounded concurrency, and circuit status. An unprobed provider is reported degraded; provider failures are sanitized. `OpenAICompatibleProvider` is one replaceable HTTP adapter; HTTP errors, response shapes, timeouts, and credentials are bounded and not exposed to users.

`GatewayTaskPlanner` requests JSON and validates it as a `TaskPlan`; it assigns canonical task identity/goal/planner attribution. It cannot execute tools. The default deployment has no provider configured and no production tools, so task acceptance never implies a model response or successful desktop operation.

## 9. Health, environment, and capability discovery

`EnvironmentDiscovery` reports basic OS/release/architecture/CPU/memory facts using `platform` and `psutil`. It deliberately does not enumerate windows, processes, installed apps, displays, browser profiles, or network history. Such fields are marked unavailable.

Capabilities are truthful and versioned. Core task orchestration and SQLite may be available; model planning requires configuration or is degraded until a successful provider request; Windows UIA and OCR/vision are unavailable; voice, semantic memory, and web research are deferred/disabled. An optional Playwright adapter is present in the source tree but is not registered by the API, so the default browser capability remains unavailable. Capability status is not inferred from OS names alone.

## 10. Tauri shell and Windows boundary

The Tauri 2 shell has a minimal permission set, per-user Windows installer targets, a local backend launcher, and a Tauri command that reads the local API token without exposing credentials to logs. The backend binds loopback for the desktop shell. The launcher waits for an explicit readiness signal from its own backend child; if the API port is already occupied, it refuses to reuse an unverified process rather than forwarding the local credential to it. The native token bridge follows `ARISE__DATA_DIR` and `ARISE__API__AUTH_TOKEN` from the process environment, but it does not parse the Python backend's `.env`; desktop token/data-directory overrides must be provided in the process environment or the generated default token file must be used. The shell is not a shell-execution bridge and exposes no arbitrary command API to React.

Cargo and Windows/WebView2 tooling are unavailable in this environment. CI is configured to compile the shell on `windows-latest`, but that remote job has not run here. Therefore **Tauri compilation, sidecar packaging, Windows ACLs, installer behavior, and actual Windows execution remain unvalidated**. A Python/backend/React test pass must not be described as Windows support.

## 11. Future adapter ports

Extension boundaries exist for `EnvironmentPort`, `ActionTool`, `VerifierPort`, `ModelProvider`, `TaskPlanner`, `SessionRepository`, `EventStore`, resource coordination, and the future-facing `SpeechRecognitionPort`, `SpeechSynthesisPort`, `MemoryPort`, and `WebResearchPort` contracts in `core.extensions`. Voice payload chunks are bounded and ephemeral; memory writes require a consent reference and expiry; retrieved memory/research content is provenance-tagged, untrusted context with no action authority. No voice, memory, or research adapter is registered in Phase 1. Later work may add:

- Windows UI Automation/accessibility with stable process/window identity and current-state leases.
- Production registration, browser account scopes, authenticated approval UX, and real-browser runtime validation for the optional Playwright adapter described below.
- OCR/layout/vision only after redacted captures; visual models propose targets but never click.
- Voice ASR/TTS with cancellation/barge-in; no microphone/speaker access in Phase 1.
- Memory/research/external integrations as untrusted, provenance-tagged retrieval adapters.

Preferred grounding remains official API → browser DOM/CDP → Windows UI Automation/accessibility → OCR/layout → vision → coordinates. Consequential actions must remain policy-gated, target-grounded, approval-scoped, and independently verified.

## 12. Phase 2 in progress: optional Playwright adapter

`adapters/browser_playwright.py` is an optional adapter, not a production capability claim. Importing it does not import Playwright or start a browser. The caller must explicitly install the `browser` extra and call `await PlaywrightBrowserProvider.start()`. The provider launches a new, non-persistent Chromium context, disables downloads and service workers, uses opaque page IDs, and caps pages, DOM elements, inspected text, and observation-cache entries. It never attaches to the user's normal browser profile.

The DOM snapshot produces semantic role/name/label/test-id candidates with visibility and enabled state, selector quality, CSS viewport bounds, and page-scoped identities. It does not read form values; password/sensitive field metadata includes the HTML input type and a sensitive flag. `fill_secret` requires both a `SecretRef` and a candidate marked sensitive. Browser URLs are HTTP(S)-only, embedded credentials are rejected, private/loopback destinations are denied by default, and tab/results expose redacted URLs. Applications that opt into private hosts assume the resulting network-security risk.

Each page observation is short-lived and tied to a hash of its URL, title, and bounded DOM state. The adapter rechecks that lease, target identity, uniqueness, visibility, and enabled state before dispatch. Browser action tools have operation-specific static risk floors and capabilities; the runtime acquires an adapter-computed `browser.page.<id>` resource in addition to contract/tool resources. Click, fill, select, key, and navigation timeouts after dispatch may have begun are reported as unknown; they must be reconciled, never replayed blindly. Postconditions are still verified through the runtime's independent `VerifierPort`.

Tests use fake page/locator/route objects and validate contracts, privacy guards, staleness, secret references, risk floors, and resource wiring. They do not prove Playwright protocol compatibility or real Chromium behavior. The API composition root does not register these tools, no user-facing approval summary currently previews browser targets, DNS rebinding/public-host-to-private resolution and WebSocket egress are not comprehensively blocked, and Windows/browser behavior has not been run on a supported Windows host. Treat the adapter as experimental until those gates are addressed.
