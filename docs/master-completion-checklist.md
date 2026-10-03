# ARISE Master Completion Checklist

**Authority:** This is the working completion tracker for the current ARISE worktree, created 2026-10-03. It does not replace the repository or imply that an item is done. Re-read it after every implementation cycle; only mark `[x] COMPLETE` when code is integrated in the intended runtime and verification evidence has been rerun and reviewed. Historical audit claims are not evidence for current status.

**Status key (use only these):** `[ ] NOT STARTED` · `[-] IN PROGRESS` · `[x] COMPLETE` · `[!] BLOCKED — ENVIRONMENT` · `[?] NEEDS EXTERNAL CONFIGURATION`.

**Non-negotiable architecture:** preserve React/TypeScript → Tauri 2 → FastAPI/asyncio → `TaskEngine` → planner → `PolicyEngine` → resources → executor → verifier → durable event store. Model, Gemini, memory, and web content are untrusted proposals/context; they do not grant authority or bypass the chain. Gemini Live remains replaceable conversation I/O, not the executor. Keep secrets referenced/redacted, retain consent and egress gates, distinguish planned/executed/verified, and never infer REAL behavior from a fake, replay, or simulator.

## Phase 1 — Core runtime

- [-] IN PROGRESS — Core runtime starts correctly.
- [-] IN PROGRESS — Task lifecycle implemented.
- [-] IN PROGRESS — Task IDs/request IDs stable.
- [-] IN PROGRESS — Idempotency implemented.
- [-] IN PROGRESS — Concurrent task handling implemented.
- [x] COMPLETE — Parent/child task lineage is persisted and included in idempotency fingerprints; authenticated child admission/listing is principal/session scoped, parent cancellation cascades to active descendants, and engine/server/frontend-contract tests pass.
- [-] IN PROGRESS — Task cancellation implemented.
- [-] IN PROGRESS — Task interruption implemented.
- [-] IN PROGRESS — Waiting states implemented.
- [-] IN PROGRESS — Retry boundaries implemented.
- [-] IN PROGRESS — Unknown-outcome handling implemented.
- [-] IN PROGRESS — Verification required for appropriate completion.
- [-] IN PROGRESS — Resource leasing implemented.
- [-] IN PROGRESS — Lease expiry handled.
- [-] IN PROGRESS — Event journal durable-before-publish.
- [x] COMPLETE — Event replay implemented (paged durable replay is verified by SQLite/server tests).
- [x] COMPLETE — Event cursors implemented (monotonic high-water survives history deletion; future and pruned cursors are rejected with recovery metadata).
- [x] COMPLETE — WebSocket streaming implemented (authenticated protocol and durable replay tests pass).
- [x] COMPLETE — Slow subscriber handling implemented (bounded-queue overflow/backpressure and reconnect cursor handling are covered by broker/server tests and the frontend build).
- [x] COMPLETE — Replay overflow/cursor recovery implemented (cumulative per-connection cap is tested across repeated `events.subscribe` replay requests; last-delivered cursor, reconnect continuation, invalid-future and pruned-cursor tests pass).
- [x] COMPLETE — Event sanitization implemented at the `EventEnvelope` boundary; recursively redacts credential-like keys and values before storage/publication, verified by event-bus persistence/publish tests.
- [x] COMPLETE — SQLite migrations implemented, including v8 history-deletion tombstones and v9 durable event replay-floor state; migration/rollback tests pass.
- [x] COMPLETE — SQLite/task crash recovery is verified across closing/reopening the real SQLite database and a fresh FastAPI/TaskEngine lifespan; pending approval becomes requires-user-input and running work becomes interrupted, with recovery events persisted.
- [x] COMPLETE — File-backed SQLite is verified to use WAL plus `synchronous=NORMAL`; the close/reopen TaskEngine recovery integration test also passes.
- [x] COMPLETE — History retention implemented (opt-in daily retention prunes only settled tasks; startup pass/repository tests pass; disabled by default).
- [x] COMPLETE — History deletion implemented (settled-history purge preserves unresolved/recoverable tasks, their required events, and idempotency tombstones; tests pass).
- [x] COMPLETE — History export implemented (bounded task/event JSON export with truncation indicators; tests pass).
- [x] COMPLETE — History backup strategy implemented (`arise backup` takes a locked SQLite snapshot, validates integrity, and publishes without overwriting; CLI/adapter tests pass).
- [-] IN PROGRESS — Bounded history/replay implemented (WebSocket replay is capped per connection and pruned cursors fail explicitly; settled-task retention is opt-in; active/unresolved history and default retention remain unbounded).
- [-] IN PROGRESS — Single-instance protection implemented.
- [-] IN PROGRESS — Stale-lock recovery implemented (OS lock release/reacquisition needs platform-specific validation).
- [-] IN PROGRESS — Process lifecycle management implemented.
- [-] IN PROGRESS — Port conflict handling implemented.
- [-] IN PROGRESS — Startup/shutdown cleanup implemented.
- [-] IN PROGRESS — Configuration system implemented.
- [-] IN PROGRESS — Secret handling implemented.
- [-] IN PROGRESS — No secrets in frontend bundle.
- [-] IN PROGRESS — No secrets in logs.
- [-] IN PROGRESS — Best-effort redaction covers common credential-shaped values in event envelopes, persisted conversation turns, research egress/results, and secret-like metadata keys; arbitrary unlabelled secret text cannot be identified generically.
- [-] IN PROGRESS — Request limits implemented.
- [-] IN PROGRESS — Timeout limits implemented.
- [-] IN PROGRESS — Provider configuration implemented.
- [-] IN PROGRESS — Capability health is truthful.
- [-] IN PROGRESS — Security boundary tests pass.
- [x] COMPLETE — Frontend/backend contract tests pass, including replay recovery and the typed text-interaction endpoint/response; backend integration suite and frontend production build pass.
- [!] BLOCKED — ENVIRONMENT — Validate Tauri child-process/token/data-directory behavior and Windows ACL/lock behavior on supported Windows/WebView2.

## Phase 2 — Windows, browser, and perception

### Environment discovery
- [x] COMPLETE — Authenticated diagnostics integrates bounded environment collection; implementation is unit-tested with REAL local psutil process enumeration and FAKE Windows probes. Real Win32 execution is separately blocked below.
- [x] COMPLETE — CPU/RAM use psutil; GPU display-adapter names use the Windows API. Tests cover REAL CPU/RAM/process reads and FAKE Windows adapter results.
- [x] COMPLETE — Display enumeration uses Win32 `EnumDisplayMonitors` for monitor dimensions/primary status; FAKE probe-contract tests pass, with actual Windows execution blocked below.
- [x] COMPLETE — Effective DPI is queried via `GetDpiForMonitor` and typed `dpi_x`/`dpi_y`/scale fields; FAKE probe tests pass, with real mixed-DPI validation blocked below.
- [x] COMPLETE — Foreground HWND/title/process metadata is discovered and credential-pattern redacted; FAKE probe test and authenticated diagnostics route test pass, with actual Windows runtime blocked below.
- [x] COMPLETE — Installed-app display names are read from bounded Windows uninstall-registry keys without install paths; FAKE probe tests pass, with actual registry execution blocked below.
- [x] COMPLETE — Browser discovery classifies bounded running/installed app names; process inventory uses REAL psutil and deterministic classification tests.
- [x] COMPLETE — Terminal discovery classifies bounded running/installed app names; process inventory uses REAL psutil and deterministic classification tests.
- [x] COMPLETE — Optional `sounddevice` query lists bounded input/output names without opening streams; FAKE module test passes and package/device runtime remains configuration/environment limited.
- [x] COMPLETE — `EnvironmentSnapshot` is returned by authenticated diagnostics and consumed by the manually requested Capabilities-page panel; backend/frontend contract, route, and build tests pass.
- [!] BLOCKED — ENVIRONMENT — Exercise the actual Win32 display/DPI/foreground/registry probes in an authenticated Windows session, including multi-monitor mixed-DPI behavior; this workspace is Linux.
- [?] NEEDS EXTERNAL CONFIGURATION — Install the optional `sounddevice`/PortAudio stack and supply working devices to verify REAL input/output discovery; the current environment has no device package/hardware.

### Windows UI Automation
- [ ] NOT STARTED — Windows UIA adapter implemented.
- [ ] NOT STARTED — Window discovery implemented.
- [ ] NOT STARTED — Control-tree discovery implemented.
- [ ] NOT STARTED — Role/name/automation-ID support.
- [ ] NOT STARTED — Enabled/visible/focused state handling.
- [ ] NOT STARTED — Semantic target identity implemented.
- [ ] NOT STARTED — UIA click/invoke implemented.
- [ ] NOT STARTED — UIA text/set-value support implemented.
- [ ] NOT STARTED — UIA focus implemented.
- [ ] NOT STARTED — UIA keyboard interaction implemented.
- [ ] NOT STARTED — Mixed-DPI handling implemented.
- [ ] NOT STARTED — DPI normalization implemented.
- [ ] NOT STARTED — Display changes handled.
- [ ] NOT STARTED — Focus changes detected.
- [ ] NOT STARTED — Stale coordinate rejection implemented.
- [ ] NOT STARTED — Human interference detection implemented.
- [!] BLOCKED — ENVIRONMENT — Execute and verify the real Windows UIA adapter on a supported Windows desktop.

### Browser automation and network boundaries
- [-] IN PROGRESS — Playwright/CDP integration implemented (optional Playwright prototype exists; production registration and real-browser validation remain open).
- [ ] NOT STARTED — Browser discovery integrated.
- [-] IN PROGRESS — Navigation implemented.
- [-] IN PROGRESS — DOM snapshot implemented.
- [-] IN PROGRESS — DOM semantic targeting implemented.
- [-] IN PROGRESS — Click implemented.
- [-] IN PROGRESS — Fill implemented.
- [-] IN PROGRESS — Select implemented.
- [-] IN PROGRESS — Scroll is implemented in the optional Playwright adapter and FAKE locator tests verify fresh-target validation; production registration and REAL Chromium execution remain open.
- [-] IN PROGRESS — Keyboard actions implemented.
- [-] IN PROGRESS — Popup pages are discovered by the optional Playwright adapter and FAKE context tests cover registration; production registration and REAL Chromium popup validation remain open.
- [-] IN PROGRESS — Target leases implemented.
- [ ] NOT STARTED — Stale browser-target re-resolution implemented.
- [-] IN PROGRESS — Sensitive browser fields protected.
- [-] IN PROGRESS — SecretRef browser filling implemented.
- [-] IN PROGRESS — URL validation implemented.
- [-] IN PROGRESS — Redirect validation implemented.
- [-] IN PROGRESS — Private-network protection implemented.
- [ ] NOT STARTED — DNS-rebinding protections implemented.
- [ ] NOT STARTED — WebSocket/network-egress controls implemented.
- [ ] NOT STARTED — Screenshot capture implemented.
- [ ] NOT STARTED — Active-window capture implemented.
- [ ] NOT STARTED — Region capture implemented.
- [ ] NOT STARTED — Crop/downscale implemented.
- [ ] NOT STARTED — Screenshot deduplication implemented.
- [ ] NOT STARTED — OCR fallback implemented.
- [ ] NOT STARTED — Multimodal grounding implemented.
- [ ] NOT STARTED — Vision confidence handling implemented.
- [ ] NOT STARTED — Vision target verification implemented.
- [ ] NOT STARTED — UIA → DOM → OCR → Vision → Coordinate hierarchy implemented.
- [ ] NOT STARTED — Coordinate fallback fails closed when unsafe.
- [-] IN PROGRESS — Browser/UIA replay tests pass (browser fakes exist; UIA adapter/tests do not).
- [ ] NOT STARTED — Failure-injection tests pass for real browser/UIA failure modes.
- [!] BLOCKED — ENVIRONMENT — Run a real Playwright/Chromium installation and supported-Windows browser/UIA integration suite.

## Phase 3 — Voice

### Audio pipeline and lifecycle
- [-] IN PROGRESS — AudioHub implemented/integrated (optional composition is gated; runtime remains unverified).
- [-] IN PROGRESS — Typed voice ports preserved.
- [-] IN PROGRESS — Microphone lifecycle implemented.
- [-] IN PROGRESS — Playback lifecycle implemented.
- [-] IN PROGRESS — Device discovery implemented.
- [-] IN PROGRESS — PCM handling implemented.
- [-] IN PROGRESS — VAD integrated.
- [-] IN PROGRESS — Dormant-first state implemented.
- [-] IN PROGRESS — Wake/activation logic implemented.
- [-] IN PROGRESS — Streaming ASR integrated.
- [-] IN PROGRESS — Transcript pipeline integrated.
- [-] IN PROGRESS — Intent integration implemented.
- [-] IN PROGRESS — TTS integrated (local adapter exists; verify server composition/streaming path).
- [-] IN PROGRESS — Audio playback integrated.
- [-] IN PROGRESS — Audio cleanup implemented.
- [-] IN PROGRESS — Voice concurrency limits implemented.
- [-] IN PROGRESS — Voice cancellation implemented.
- [-] IN PROGRESS — Voice interruption implemented.
- [-] IN PROGRESS — Generation fence implemented.
- [-] IN PROGRESS — Stale voice output rejected.
- [-] IN PROGRESS — Barge-in stops playback.
- [-] IN PROGRESS — Barge-in forwards first user speech correctly.
- [ ] NOT STARTED — Device-loss recovery implemented.
- [-] IN PROGRESS — Voice reconnect implemented (fake path only; injected network/device failures remain untested).
- [-] IN PROGRESS — Voice shutdown implemented.
- [!] BLOCKED — ENVIRONMENT — Validate microphone/speaker permissions, real devices, local models, and voice cleanup on Windows hardware.

### Bridge and Gemini boundary
- [-] IN PROGRESS — Production VoiceConversationBridge composed behind explicit settings and authenticated lifecycle routes.
- [x] COMPLETE — VoiceConversationBridge submits through `TaskEngineVoiceAdapter` to the real TaskEngine; integration test runs the actual AgentRuntime/PolicyEngine/resource/verifier chain with a FAKE in-memory environment/tool, not real audio or host actions.
- [x] COMPLETE — Voice question tool-call is rejected without task admission through the real TaskEngine adapter; integrated fake/replay boundary test confirms the task repository remains unchanged.
- [x] COMPLETE — Voice command creates a task and reaches verified completion through the real TaskEngine/runtime in the simulated integration test; no real voice capture or host action is claimed.
- [-] IN PROGRESS — Ambiguous requests request clarification.
- [-] IN PROGRESS — Voice task progress integrated.
- [x] COMPLETE — Integrated bridge/TaskEngine test observes no success at admission, then only reports `verified=true` after the runtime verifier completes; execution evidence is FAKE simulator scope.
- [-] IN PROGRESS — Voice failure states integrated.
- [-] IN PROGRESS — Gemini Live adapter implemented.
- [-] IN PROGRESS — Gemini Live remains optional.
- [-] IN PROGRESS — Gemini Live requires explicit consent/configuration.
- [-] IN PROGRESS — Gemini Live cannot directly execute OS actions.
- [-] IN PROGRESS — Gemini tool boundary implemented.
- [-] IN PROGRESS — `execute_task` interface implemented.
- [-] IN PROGRESS — `ask_user` interface implemented.
- [-] IN PROGRESS — `get_task_status` implemented.
- [-] IN PROGRESS — `cancel_task` implemented.
- [x] COMPLETE — `report_status` implemented as an alias of the same principal-scoped authoritative status read; voice bridge tests pass.
- [-] IN PROGRESS — Gemini cannot claim unverified success.
- [-] IN PROGRESS — Gemini generation interruption implemented.
- [-] IN PROGRESS — Gemini reconnect implemented (test evidence is fake; real provider path not exercised).
- [-] IN PROGRESS — Gemini stale output blocked.
- [ ] NOT STARTED — Immediate local/template acknowledgement implemented.
- [ ] NOT STARTED — Cloud round-trip not required for acknowledgement.
- [ ] NOT STARTED — Voice responses can stream into TTS.
- [-] IN PROGRESS — AudioHub owns actual audio resources.

### Voice evidence requirements
- [-] IN PROGRESS — 21-stage voice validation retained.
- [-] IN PROGRESS — REAL/FAKE/REPLAY distinction preserved.
- [-] IN PROGRESS — PASS/PARTIAL/FAILED/SKIPPED/BLOCKED distinction preserved.
- [-] IN PROGRESS — Environment status preserved.
- [-] IN PROGRESS — Sanitized diagnostics preserved.
- [-] IN PROGRESS — No raw audio persisted.
- [-] IN PROGRESS — No raw secrets persisted.
- [!] BLOCKED — ENVIRONMENT — Run REAL Windows microphone/speaker/local-model/Gemini validation; do not substitute fake/replay evidence.
- [?] NEEDS EXTERNAL CONFIGURATION — Supply a supported device, user-supplied Vosk/Kokoro model files, optional Gemini SDK, and OS-keyring credential for the consented runtime checks.

## Phase 4 — Agent intelligence

### Intent and task planning
- [-] IN PROGRESS — Intent engine fully integrated.
- [x] COMPLETE — QUESTION classification implemented and routed to the tool-free FAST_REASONER path; authenticated API test returns an informational answer without admitting a task.
- [x] COMPLETE — COMMAND classification implemented and routed through the real TaskEngine/SQLite admission path; server integration test passes.
- [x] COMPLETE — CONVERSATION classification implemented; non-action text routes to the informational provider path without granting execution authority.
- [x] COMPLETE — AMBIGUOUS classification implemented; low-confidence action-like text receives clarification and creates no task, covered by server integration test.
- [x] COMPLETE — Intent confidence implemented as an advisory deterministic score; classifier tests pass and it grants no execution authority.
- [ ] NOT STARTED — Structured entities implemented.
- [ ] NOT STARTED — Structured command representation implemented.
- [-] IN PROGRESS — Planner implemented.
- [-] IN PROGRESS — Single-step planning implemented.
- [-] IN PROGRESS — Multi-step planning implemented.
- [-] IN PROGRESS — Dependency handling implemented.
- [ ] NOT STARTED — Conditional steps implemented.
- [ ] NOT STARTED — Parallel-safe steps implemented.
- [-] IN PROGRESS — Verification checkpoints implemented.
- [-] IN PROGRESS — Retry strategy implemented.
- [ ] NOT STARTED — Fallback strategy implemented.
- [-] IN PROGRESS — Cancellation-aware planning implemented.

### Per-step plan contract
- [-] IN PROGRESS — Every plan step supports Action.
- [-] IN PROGRESS — Every plan step supports Arguments.
- [-] IN PROGRESS — Every plan step supports Target.
- [-] IN PROGRESS — Every plan step supports Preconditions.
- [-] IN PROGRESS — Every plan step supports Expected postconditions.
- [-] IN PROGRESS — Every plan step supports Verification strategy.
- [-] IN PROGRESS — Every plan step supports Risk.
- [-] IN PROGRESS — Every plan step supports Timeout.
- [-] IN PROGRESS — Every plan step supports Retry policy.
- [-] IN PROGRESS — Every plan step supports Resource requirements.
- [-] IN PROGRESS — Planner cannot bypass PolicyEngine.
- [-] IN PROGRESS — Planner cannot directly execute arbitrary OS commands.
- [-] IN PROGRESS — Planner cannot directly execute arbitrary Python.
- [-] IN PROGRESS — Planner cannot directly execute arbitrary mouse coordinates.

### Model gateway and runtime optimization
- [-] IN PROGRESS — Dynamic model routing implemented.
- [-] IN PROGRESS — Planner/reasoning model routing implemented.
- [ ] NOT STARTED — Vision model routing implemented.
- [ ] NOT STARTED — ASR model routing implemented.
- [ ] NOT STARTED — OCR routing implemented.
- [-] IN PROGRESS — Embedding routing implemented.
- [ ] NOT STARTED — TTS routing implemented.
- [ ] NOT STARTED — Optional deep-reasoning escalation implemented.
- [-] IN PROGRESS — Provider health implemented.
- [-] IN PROGRESS — Provider timeout implemented.
- [-] IN PROGRESS — Provider cancellation implemented.
- [-] IN PROGRESS — Provider fallback implemented.
- [-] IN PROGRESS — Provider cooldown implemented.
- [-] IN PROGRESS — Concurrency limits implemented.
- [ ] NOT STARTED — Streaming implemented where supported.
- [ ] NOT STARTED — Request deduplication implemented where safe.
- [-] IN PROGRESS — Sanitized provider telemetry implemented.
- [ ] NOT STARTED — Persistent model connections used where appropriate.
- [ ] NOT STARTED — Connection pooling implemented where appropriate.
- [ ] NOT STARTED — Backpressure implemented.
- [ ] NOT STARTED — Response streaming implemented.
- [ ] NOT STARTED — Unnecessary sequential model calls reduced.
- [ ] NOT STARTED — Safe parallel model calls implemented.
- [-] IN PROGRESS — Progress updates integrated.
- [-] IN PROGRESS — Progress updates are not spammy.
- [ ] NOT STARTED — Immediate acknowledgement implemented.
- [-] IN PROGRESS — Current task state visible to assistant.
- [?] NEEDS EXTERNAL CONFIGURATION — Configure a supported local/cloud planning provider and credentials to validate provider behavior against a real endpoint.

## Phase 4 — Web research
- [-] IN PROGRESS — Research subsystem implemented (Brave adapter is gated; live request not verified).
- [-] IN PROGRESS — Search integration implemented.
- [-] IN PROGRESS — Source discovery implemented.
- [-] IN PROGRESS — Page retrieval implemented.
- [-] IN PROGRESS — Source extraction implemented.
- [-] IN PROGRESS — Source metadata tracked.
- [-] IN PROGRESS — Source timestamps tracked.
- [-] IN PROGRESS — Provenance tracked.
- [-] IN PROGRESS — Citation data preserved.
- [-] IN PROGRESS — Relevance filtering implemented.
- [ ] NOT STARTED — Conflicting-source handling implemented.
- [-] IN PROGRESS — Retrieval bounds implemented.
- [-] IN PROGRESS — Research timeout implemented.
- [ ] NOT STARTED — Research cancellation implemented.
- [x] COMPLETE — Current-information surface forms require explicit one-time web consent and route through bounded research when enabled; API tests verify no implicit search/task and the consented provenance-bearing path (fake provider evidence).
- [-] IN PROGRESS — Static model knowledge not falsely presented as current research.
- [-] IN PROGRESS — Web content treated as untrusted.
- [-] IN PROGRESS — Prompt injection isolation implemented.
- [-] IN PROGRESS — Retrieved instructions cannot override policy.
- [-] IN PROGRESS — Retrieved instructions cannot directly execute actions.
- [-] IN PROGRESS — Research output isolated from privileged control path.
- [-] IN PROGRESS — Research synthesis integration tests now exercise the authenticated API with fake search/model providers and preserved source metadata; live Brave/answer-provider behavior is unverified.
- [-] IN PROGRESS — API prompt-injection regression sends hostile source text only as untrusted context to a no-tools fake model path; actual model adherence and research-to-planner isolation tests remain open.
- [-] IN PROGRESS — Source-provenance tests pass (fixture/fake scope only; re-run pending).
- [?] NEEDS EXTERNAL CONFIGURATION — Configure Brave key in OS keyring and both research/egress opt-ins for a live request.

## Phase 5 — Memory
- [-] IN PROGRESS — Working memory implemented.
- [-] IN PROGRESS — Short-term memory implemented (task/session scope and bounds need end-to-end verification).
- [-] IN PROGRESS — Persistent semantic memory implemented (local persistent records exist; semantic embeddings are optional).
- [ ] NOT STARTED — Episodic memory implemented.
- [ ] NOT STARTED — Procedural memory implemented.
- [-] IN PROGRESS — Working memory expires correctly.
- [-] IN PROGRESS — Short-term memory is bounded.
- [-] IN PROGRESS — Semantic memory stores only explicitly approved information.
- [ ] NOT STARTED — Episodic memory stores useful task history.
- [ ] NOT STARTED — Procedural memory stores reusable workflows.
- [-] IN PROGRESS — Memory provenance implemented.
- [-] IN PROGRESS — Memory consent implemented.
- [-] IN PROGRESS — Consent reference stored safely.
- [-] IN PROGRESS — Memory timestamp stored.
- [-] IN PROGRESS — Memory confidence stored.
- [-] IN PROGRESS — Memory expiry implemented where appropriate.
- [-] IN PROGRESS — Memory list UI/API implemented.
- [-] IN PROGRESS — Memory inspection implemented.
- [-] IN PROGRESS — Memory deletion implemented.
- [-] IN PROGRESS — Memory clearing implemented.
- [-] IN PROGRESS — Memory disabling implemented.
- [-] IN PROGRESS — Secrets excluded from normal memory.
- [-] IN PROGRESS — Passwords excluded from normal memory.
- [-] IN PROGRESS — API keys excluded from normal memory.
- [-] IN PROGRESS — Authentication tokens excluded from normal memory.
- [-] IN PROGRESS — Embedding integration implemented (optional adapter; live endpoint not verified).
- [-] IN PROGRESS — Embedding generation implemented.
- [-] IN PROGRESS — Vector/semantic storage implemented.
- [-] IN PROGRESS — Semantic search implemented.
- [-] IN PROGRESS — Relevance filtering implemented.
- [-] IN PROGRESS — Memory retrieval integrated with planner.
- [-] IN PROGRESS — Current user request takes priority over stale memory.
- [-] IN PROGRESS — Memory cannot weaken security.
- [-] IN PROGRESS — Memory cannot override policy.
- [-] IN PROGRESS — Memory cannot bypass approvals.
- [?] NEEDS EXTERNAL CONFIGURATION — Configure an embedding endpoint/model (and any required keyring secret) to validate real vector generation/retrieval.

## Phase 5 — Personalization and learning
- [ ] NOT STARTED — Preferred browser can be remembered.
- [ ] NOT STARTED — Preferred application can be remembered.
- [ ] NOT STARTED — Response preferences can be remembered.
- [ ] NOT STARTED — Approved workflow preferences can be remembered.
- [ ] NOT STARTED — TTS preferences can be remembered.
- [ ] NOT STARTED — Procedural workflow retrieval implemented.
- [ ] NOT STARTED — Procedural workflow execution integrated.
- [ ] NOT STARTED — Procedure provenance implemented.
- [ ] NOT STARTED — Stale-procedure detection implemented.
- [ ] NOT STARTED — Re-grounding of stale procedures implemented.
- [ ] NOT STARTED — Successful workflow adaptation implemented.
- [ ] NOT STARTED — Failed workflow does not blindly repeat forever.
- [ ] NOT STARTED — Learned workflow remains editable.
- [ ] NOT STARTED — Learned workflow remains deletable.
- [ ] NOT STARTED — Learned workflow never bypasses policy.

## Security checklist
- [x] COMPLETE — Task-list APIs/repositories scope history to the authenticated principal (server and in-memory repository tests pass).
- [-] IN PROGRESS — Model cannot bypass policy.
- [-] IN PROGRESS — Model cannot directly execute privileged tools.
- [-] IN PROGRESS — Tool output cannot override system policy.
- [-] IN PROGRESS — Web content cannot override system policy.
- [-] IN PROGRESS — Memory cannot override current instructions.
- [-] IN PROGRESS — Voice cannot bypass task admission.
- [-] IN PROGRESS — Destructive actions use appropriate risk controls.
- [-] IN PROGRESS — External communication uses appropriate risk controls.
- [-] IN PROGRESS — Credential handling is isolated.
- [-] IN PROGRESS — Common credential-shaped values are redacted from events, conversation persistence, and research queries/results; coverage is heuristic and does not catch arbitrary unlabelled secrets.
- [x] COMPLETE — Sensitive event payloads are recursively redacted at the event-envelope boundary before persistence/publication; event-bus regression tests pass.
- [-] IN PROGRESS — Shell execution is policy-controlled.
- [-] IN PROGRESS — Network access is controlled.
- [-] IN PROGRESS — Browser navigation is validated.
- [-] IN PROGRESS — Unknown action results are handled safely.
- [-] IN PROGRESS — Non-idempotent actions are not blindly retried.

## Recovery checklist
- [-] IN PROGRESS — Model timeout recovery.
- [-] IN PROGRESS — Model disconnect recovery.
- [-] IN PROGRESS — Gemini disconnect recovery (fake path only; real provider fault injection pending).
- [ ] NOT STARTED — Browser crash recovery.
- [ ] NOT STARTED — Browser navigation recovery.
- [-] IN PROGRESS — UI target disappearance recovery.
- [ ] NOT STARTED — Application closure recovery.
- [ ] NOT STARTED — Focus-change recovery.
- [ ] NOT STARTED — Network-loss recovery.
- [ ] NOT STARTED — Voice-device-loss recovery.
- [-] IN PROGRESS — Task cancellation recovery.
- [-] IN PROGRESS — Voice interruption recovery.
- [-] IN PROGRESS — Unknown-outcome reconciliation.
- [-] IN PROGRESS — Safe retry logic.

## Frontend and desktop checklist
- [-] IN PROGRESS — Conversation UI reflects actual runtime state.
- [-] IN PROGRESS — Dormant state visible.
- [-] IN PROGRESS — Listening state visible.
- [-] IN PROGRESS — Thinking state visible.
- [-] IN PROGRESS — Working state visible.
- [-] IN PROGRESS — Speaking state visible.
- [-] IN PROGRESS — Interrupted state visible.
- [-] IN PROGRESS — Waiting state visible.
- [-] IN PROGRESS — Approval state visible.
- [-] IN PROGRESS — Completed state visible.
- [-] IN PROGRESS — Failed state visible.
- [-] IN PROGRESS — Cancellation state visible.
- [-] IN PROGRESS — Current task visible.
- [-] IN PROGRESS — Current step visible.
- [-] IN PROGRESS — Progress visible.
- [-] IN PROGRESS — Model/provider health visible.
- [-] IN PROGRESS — Voice health visible.
- [-] IN PROGRESS — Memory controls visible.
- [-] IN PROGRESS — Research state visible.
- [-] IN PROGRESS — History visible.
- [-] IN PROGRESS — Reconnect works.
- [-] IN PROGRESS — Stale state is cleared correctly.
- [-] IN PROGRESS — Backend errors are sanitized for users.
- [!] BLOCKED — ENVIRONMENT — Exercise the integrated frontend inside real Tauri/WebView2 on supported Windows.

## Production and packaging checklist
- [x] COMPLETE — Tauri 2 configuration and Rust shell compile with the Windows CI `cargo check`; installer/runtime validation remains separate below.
- [-] IN PROGRESS — React production build valid.
- [ ] NOT STARTED — Python backend packaging path valid.
- [-] IN PROGRESS — Sidecar architecture valid.
- [-] IN PROGRESS — Backend process supervision implemented.
- [-] IN PROGRESS — Startup sequencing implemented.
- [-] IN PROGRESS — Health-check startup gate implemented.
- [-] IN PROGRESS — Shutdown implemented.
- [ ] NOT STARTED — Crash handling implemented.
- [ ] NOT STARTED — Restart policy implemented.
- [-] IN PROGRESS — Single-instance integration implemented.
- [-] IN PROGRESS — Configuration setup documented.
- [-] IN PROGRESS — First-run capability discovery implemented.
- [-] IN PROGRESS — Optional providers remain optional.
- [-] IN PROGRESS — Capability states are truthful.
- [!] BLOCKED — ENVIRONMENT — Package and run the Tauri sidecar against real WebView2; validate per-user ACLs on a supported Windows desktop.

## Integrated acceptance-path checklist
- [-] IN PROGRESS — Voice command → real intent → real task admission → TaskEngine → planner → policy → executor → verifier → completion (bridge path exists; no real voice/runtime evidence and no production action tools).
- [x] COMPLETE — Question → informational answer → NO task admission is integrated through FastAPI, ModelRouter, session persistence, and real TaskEngine storage; server tests inject a fake model provider and prove no task exists. Live provider behavior remains external/unverified.
- [-] IN PROGRESS — Browser task → browser adapter → semantic target → execution → verification (optional adapter exists; production registration/real browser absent).
- [-] IN PROGRESS — Research → search → sources → provenance → synthesis → citations (adapter exists; live provider and integrated synthesis unverified).
- [-] IN PROGRESS — Memory → consent → persistence → embedding → retrieval → planner integration (local consent/persistence exists; live embedding and full planner integration require verification).
- [ ] NOT STARTED — Procedural memory → retrieve workflow → execute semantically → verify → adapt when stale.
- [-] IN PROGRESS — Voice interruption → VAD → barge-in → playback stop → generation cancellation → stale-output rejection → new request (fake/replay scope only; real path unverified).
- [-] IN PROGRESS — Provider failure → failure detection → fallback/recovery → user-visible status (provider fakes/tests exist; integrated real provider path unverified).

## Acceptance scenarios (must remain general; do not hard-code them)
- [-] IN PROGRESS — Scenario 1: “Open Chrome.” is classified as a command; task is planned, policy-checked, executed, independently verified, and only then reported complete/spoken. Real desktop execution is not available here.
- [-] IN PROGRESS — Scenario 2: “Tell me how to open Chrome.” is classified as a question; no OS task is admitted and an informational response is returned.
- [-] IN PROGRESS — Scenario 3: multi-step ChatGPT/browser task uses browser discovery, navigation, DOM/UI grounding, actions, verification, extraction, and response. Browser runtime is not validated.
- [-] IN PROGRESS — Scenario 4: current-information request uses official-source research, provenance, synthesis, file creation, and verification without trusting web instructions.
- [ ] NOT STARTED — Scenario 5: “Start my development environment.” retrieves an approved procedure where available, discovers the environment, executes scoped steps, verifies results, and creates a normally planned workflow if none exists.

## Test checklist
- [-] IN PROGRESS — Unit tests for task lifecycle.
- [-] IN PROGRESS — Unit tests for policy.
- [ ] NOT STARTED — Unit tests for target identity (full Windows/UIA identity coverage).
- [x] COMPLETE — Environment diagnostics tests include REAL psutil discovery of the local process, FAKE Windows/audio probe contracts, redaction/failure states, authenticated API exposure, and typed frontend integration; no Win32 runtime claim.
- [-] IN PROGRESS — Unit tests for resource leases.
- [-] IN PROGRESS — Unit tests for cancellation.
- [-] IN PROGRESS — Unit tests for generation fences.
- [-] IN PROGRESS — Unit tests for model routing.
- [-] IN PROGRESS — Unit tests for memory consent.
- [-] IN PROGRESS — Unit tests for semantic retrieval.
- [-] IN PROGRESS — Unit tests for research provenance.
- [-] IN PROGRESS — API-level research prompt-injection regression exists; planner and real-provider behavior still need testing.
- [x] COMPLETE — Voice integration tests include a real in-process TaskEngine/AgentRuntime/PolicyEngine/resource/verifier path; environment/tool are FAKE and no microphone or host automation is claimed.
- [x] COMPLETE — Voice → TaskEngine integration test exercises real TaskEngine/AgentRuntime/PolicyEngine/resource/verifier components and proves verified status gating; `InMemoryEnvironment`/`SetFactTool` are FAKE evidence and do not verify physical voice or host automation.
- [x] COMPLETE — Question → no task API integration test (real TaskEngine/SQLite, fake answer provider).
- [x] COMPLETE — Command → task API integration test (real TaskEngine/SQLite admission; no host action is claimed).
- [x] COMPLETE — Ambiguous → clarification API integration test proves no task is admitted.
- [-] IN PROGRESS — Task → policy test.
- [-] IN PROGRESS — Policy → executor test.
- [-] IN PROGRESS — Executor → verifier test.
- [-] IN PROGRESS — Verifier → completion test.
- [-] IN PROGRESS — Browser integration tests (fakes only; real Playwright/Chromium unverified).
- [ ] NOT STARTED — UIA integration tests.
- [-] IN PROGRESS — Memory integration tests.
- [-] IN PROGRESS — Research integration tests.
- [-] IN PROGRESS — Recovery tests.
- [-] IN PROGRESS — Failure-injection tests.
- [-] IN PROGRESS — Replay tests.
- [x] COMPLETE — Frontend/backend contract tests cover task/event models, bounded replay recovery, and the `/interactions` TypeScript API/UI contract; relevant tests and build pass.
- [-] IN PROGRESS — Provider failure tests.
- [-] IN PROGRESS — Provider timeout tests.
- [-] IN PROGRESS — Provider cancellation tests.
- [-] IN PROGRESS — Stale-target tests.
- [ ] NOT STARTED — Focus-change tests.
- [ ] NOT STARTED — Network-failure tests.
- [ ] NOT STARTED — Browser-crash tests.
- [ ] NOT STARTED — Device-loss tests.
- [-] IN PROGRESS — Unknown-outcome tests.
- [-] IN PROGRESS — Duplicate-request tests.

## Quality checklist
- [x] COMPLETE — Ruff check passes (`.venv/bin/ruff check .`).
- [x] COMPLETE — Changed Python formatting passes (`ruff format --check` on all changed/untracked Python files; 45 files already formatted).
- [x] COMPLETE — Python compile passes (`python -m compileall -q src tests`).
- [x] COMPLETE — Full pytest passes (280 tests and 64 subtests; one upstream Starlette/httpx deprecation warning).
- [x] COMPLETE — Frontend typecheck passes (`npm run typecheck`).
- [x] COMPLETE — Frontend production build passes (`npm run build`).
- [x] COMPLETE — Secret/token scan passes (no high-confidence credential patterns in source/config/docs; dependency/build dirs excluded).
- [x] COMPLETE — `git diff --check` passes.
- [x] COMPLETE — Whitespace checks pass.
- [x] COMPLETE — Simulator passes (`arise demo`; FAKE/test-only execution, not host automation).
- [x] COMPLETE — Replay suites pass (pytest replay/failure tests pass; harness replay probes ran under host guard).
- [x] COMPLETE — Voice harness runs correctly and reports `ENVIRONMENT-LIMITED`/`BLOCKED` honestly on Linux (21 checks; no device/provider access).
- [x] COMPLETE — No real-only claims made from replay tests (evidence labels and limitations remain explicit).
- [x] COMPLETE — GitHub Actions run 37135316889 passes Ubuntu/Windows backend (Python 3.11/3.12), frontend, and Windows Tauri `cargo check`. Windows path/order/approval fixes and the NSIS install-mode correction are covered; installer and desktop runtime validation remain separate.
- [-] IN PROGRESS — No fake success responses remain in production paths.
- [-] IN PROGRESS — No accidental debug code remains.
- [-] IN PROGRESS — No obsolete placeholders remain in production paths.

## Documentation checklist
- [x] COMPLETE — README reflects current task-history retention, backup, replay, and environment limits.
- [x] COMPLETE — Architecture documentation records the v9 replay floor, retention, backup, and authority boundaries.
- [x] COMPLETE — Roadmap updated with current replay/backup/retention state.
- [x] COMPLETE — Voice validation documentation records the 21-check harness scope and non-REAL limitations.
- [x] COMPLETE — Security documentation added at `docs/security.md` with current controls, open gaps, and validation commands.
- [x] COMPLETE — Memory documentation updated in README/architecture/security docs with consent and deletion behavior.
- [x] COMPLETE — Research documentation updated in README/architecture/security docs with trust and egress gates.
- [x] COMPLETE — Provider configuration documented in README, `.env.example`, and security docs.
- [-] IN PROGRESS — Windows setup documented (general setup exists; supported-Windows installer/UIA/audio validation guide remains incomplete).
- [-] IN PROGRESS — Troubleshooting documented (common setup notes exist; production recovery matrix remains incomplete).
- [x] COMPLETE — Remaining environment limitations documented honestly; no Windows/provider runtime claim is inferred from FAKE/REPLAY checks.

## Mandatory forensic search
- [x] COMPLETE — Search TODO, FIXME, `pass`, `NotImplementedError`, stub, placeholder, fake, mock, unimplemented, and unsupported across the repository; source/docs/tests matches were classified in `docs/forensic-audit.md`.
- [-] IN PROGRESS — Implement every software-remediable production gap found by that search without removing legitimate tests; remaining gaps are enumerated in this checklist and `docs/forensic-audit.md`.

## Required final report
- [x] COMPLETE — Re-opened and recounted the full checklist for this report after the Windows CI rerun: 461 total; 73 COMPLETE, 282 IN PROGRESS, 93 NOT STARTED, 8 BLOCKED — ENVIRONMENT, and 5 NEEDS EXTERNAL CONFIGURATION (including the two closed final-report rows).
- [x] COMPLETE — Final report records changes/defects, exact test totals, REAL vs FAKE/REPLAY evidence, environment/external setup needs, Git status, and the commit/push/PR outcome. The user's later PR request authorizes committing and pushing this branch despite the earlier no-commit/no-push instruction.
