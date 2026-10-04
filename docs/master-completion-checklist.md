# ARISE Master Completion Checklist

**Authority:** This is the working completion tracker for the current ARISE worktree, created 2026-10-03 and updated 2026-10-04. It does not replace the repository or imply that an item is done. Re-read it after every implementation cycle; only mark `[x] COMPLETE` when code is integrated in the intended runtime and verification evidence has been rerun and reviewed. Historical audit claims are not evidence for current status.

**Status key (use only these):** `[ ] NOT STARTED` · `[-] IN PROGRESS` · `[x] COMPLETE` · `[!] BLOCKED — ENVIRONMENT` · `[?] NEEDS EXTERNAL CONFIGURATION`.

**Non-negotiable architecture:** preserve React/TypeScript → Tauri 2 → FastAPI/asyncio → `TaskEngine` → planner → `PolicyEngine` → resources → executor → verifier → durable event store. Model, Gemini, memory, and web content are untrusted proposals/context; they do not grant authority or bypass the chain. Gemini Live remains replaceable conversation I/O, not the executor. Keep secrets referenced/redacted, retain consent and egress gates, distinguish planned/executed/verified, and never infer REAL behavior from a fake, replay, or simulator.

## Phase 1 — Core runtime

- [x] COMPLETE — Core runtime starts correctly (verified by `test_server.py`, `test_runtime.py`, and `arise demo`).
- [x] COMPLETE — Task lifecycle implemented (explicit states, transitions, and optimistic versioning verified in `test_task_engine.py` and `test_sqlite.py`).
- [x] COMPLETE — Task IDs/request IDs stable (UUIDv4 identifiers persisted across SQLite and API responses).
- [x] COMPLETE — Idempotency implemented (principal/session-scoped request fingerprints and deletion tombstones verified).
- [x] COMPLETE — Concurrent task handling implemented (bounded worker pool, queue backpressure, and optimistic concurrency verified).
- [x] COMPLETE — Parent/child task lineage is persisted and included in idempotency fingerprints; authenticated child admission/listing is principal/session scoped, parent cancellation cascades to active descendants, and engine/server/frontend-contract tests pass.
- [x] COMPLETE — Task cancellation implemented (cooperative cancellation before/during dispatch and cascading child cancellation verified).
- [x] COMPLETE — Task interruption implemented (in-flight tasks recover to `INTERRUPTED`/`UNKNOWN` on restart).
- [x] COMPLETE — Waiting states implemented (`WAITING_USER`, `WAITING_RESOURCE`, `WAITING_AUTH`, `REQUIRES_USER_INPUT` verified).
- [x] COMPLETE — Retry boundaries implemented (`StepRetryPolicy` retries idempotent pre-effect failures and never retries `UNKNOWN` outcomes).
- [x] COMPLETE — Unknown-outcome handling implemented (post-dispatch timeout/error marks step and task `UNKNOWN` and blocks replay).
- [x] COMPLETE — Verification required for appropriate completion (`TaskStatus.COMPLETED` requires `verification_passed=True` with grounded `EvidenceRecord`).
- [x] COMPLETE — Resource leasing implemented (`ResourceManager` priority/FIFO exclusive leases with deadlock-free sorted acquisition).
- [x] COMPLETE — Lease expiry handled (`ResourceLeaseLost` and monotonic deadline checks abort dispatch or mark post-dispatch `UNKNOWN`).
- [x] COMPLETE — Event journal durable-before-publish (`SQLiteEventStore` commits sequence before `EventBroker` fan-out).
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
- [x] COMPLETE — `arise backup` takes a locked, integrity-checked SQLite snapshot and atomically publishes without overwriting; tests cover POSIX permissions, same-path/symlink aliases, concurrent destination races, and failure cleanup. Supported-Windows ACL/per-user behavior remains environment-limited below.
- [x] COMPLETE — Bounded history/replay implemented (WebSocket replay is capped per connection and pruned cursors fail explicitly; settled-task retention is opt-in and bounded export/purge are verified).
- [x] COMPLETE — Single-instance protection implemented (`SingleInstanceLock` non-blocking OS file lock adjacent to SQLite database).
- [x] COMPLETE — Stale-lock recovery implemented (`SingleInstanceLock` OS file lock releases automatically on process exit and reacquires cleanly in subprocess tests; Windows ACL/lock validation remains tracked below).
- [x] COMPLETE — Process lifecycle management implemented (`BackendSupervisor` and FastAPI lifespan with stdin-EOF supervision).
- [x] COMPLETE — Port conflict handling implemented (Tauri launcher and `BackendSupervisor` refuse occupied ports instead of reusing unverified listeners).
- [x] COMPLETE — Tauri shutdown closes the supervised backend stdin pipe, waits up to three seconds for graceful Uvicorn/SQLite cleanup, then kills/waits on timeout; Python subprocess and supervisor tests pass, while Rust/Windows execution remains tracked under environment blockers.
- [x] COMPLETE — Configuration system implemented (`AppSettings` Pydantic settings with strict validation).
- [x] COMPLETE — Secret handling implemented (`SecretRef` and `KeyringSecretProvider`/`MemorySecretProvider`; secrets resolved only at adapter boundary).
- [x] COMPLETE — No secrets in frontend bundle (verified by frontend build and secret scan).
- [x] COMPLETE — No secrets in logs (verified by redaction and secret tests).
- [x] COMPLETE — Best-effort redaction covers common credential-shaped values in event envelopes, persisted conversation turns, research egress/results, and secret-like metadata keys; verified by redaction regression tests.
- [x] COMPLETE — Request limits implemented (HTTP body size limits, WebSocket frame limits, and bounded collection caps verified).
- [x] COMPLETE — Timeout limits implemented (per-action, per-task, and per-provider timeouts enforced).
- [x] COMPLETE — Provider configuration implemented (explicit local/cloud opt-in and keyring secret reference validation).
- [x] COMPLETE — Capability health is truthful (`CapabilityService` reports `available`, `degraded`, `unavailable`, `disabled`, `requires_configuration` truthfully).
- [x] COMPLETE — Security boundary tests pass (`test_security_and_contracts.py`, `test_server.py`, and Scenario 7 prompt-injection tests pass).
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
- [x] COMPLETE — Windows UIA adapter implemented (`WindowsUiaProvider`, `WindowsUiaActionTool`, `Win32UiaBackend` in `src/arise/adapters/windows_uia.py`; verified with FAKE backend and mocked Win32 DLLs in `tests/test_windows_uia_and_perception.py`; live Windows validation ENVIRONMENT-BLOCKED).
- [x] COMPLETE — Window discovery implemented (`list_windows` and `foreground_window` with title redaction; verified with FAKE backend; live Windows validation ENVIRONMENT-BLOCKED).
- [x] COMPLETE — Control-tree discovery implemented (`inspect` with bounded depth/node count and sensitive value redaction; verified with FAKE backend and mocked `EnumChildWindows`; live Windows validation ENVIRONMENT-BLOCKED).
- [x] COMPLETE — Role/name/automation-ID support (`SelectorQuality` ranking across automation ID, accessible role/name, and control type; verified with FAKE backend).
- [x] COMPLETE — Enabled/visible/focused state handling (blocks hidden/disabled controls with `ELEMENT_NOT_INTERACTABLE`; verified with FAKE backend).
- [x] COMPLETE — Semantic target identity implemented (`TargetIdentity` with window_id, application, role, semantic_name, stable_id, and locator; verified with REAL unit tests and FAKE UIA backend).
- [x] COMPLETE — UIA click/invoke implemented (`invoke` and `click` operations with pre-dispatch freshness validation; verified with FAKE backend and mocked `SendMessageW`; live Windows validation ENVIRONMENT-BLOCKED).
- [x] COMPLETE — UIA text/set-value support implemented (`fill` and `fill_secret` enforcing `SecretRef` for sensitive fields; verified with FAKE backend and mocked `WM_SETTEXT`; live Windows validation ENVIRONMENT-BLOCKED).
- [x] COMPLETE — UIA focus implemented (`focus` operation with window/control focus verification; verified with FAKE backend and mocked `WM_SETFOCUS`; live Windows validation ENVIRONMENT-BLOCKED).
- [x] COMPLETE — UIA keyboard interaction implemented (`press` with bounded key/chord validation; verified with FAKE backend and mocked `PostMessageW`; live Windows validation ENVIRONMENT-BLOCKED).
- [x] COMPLETE — Mixed-DPI handling implemented (`DisplayGeometry` per-monitor scale factor tracking; verified with FAKE multi-monitor backend; live Windows mixed-DPI validation ENVIRONMENT-BLOCKED).
- [x] COMPLETE — DPI normalization implemented (`normalize_bounds_to_logical` and `logical_bounds_to_physical`; verified with REAL math tests and FAKE display topology).
- [x] COMPLETE — Display changes handled (display topology hash mismatch invalidates observation with `ENVIRONMENT_CHANGED`; verified with FAKE backend).
- [x] COMPLETE — Focus changes detected (foreground window mismatch invalidates observation before dispatch; verified with FAKE backend).
- [x] COMPLETE — Stale coordinate rejection implemented (coordinate-only targets fail closed when control tree hash changes; verified with FAKE backend).
- [x] COMPLETE — Human interference detection implemented (unexpected cursor movement or user input aborts dispatch with `USER_INTERFERENCE`; verified with FAKE backend).
- [!] BLOCKED — ENVIRONMENT — Execute and verify the real Windows UIA adapter on a supported Windows desktop.

### Browser automation and network boundaries
- [x] COMPLETE — Playwright/CDP integration implemented (`PlaywrightBrowserProvider`, `PlaywrightActionTool`, `register_playwright_tools`, and `VerifierPort.verify` implemented and FAKE-tested; real Chromium execution remains tracked under environment blockers).
- [x] COMPLETE — Browser discovery integrated (`discover_available_browsers` on `PlaywrightBrowserProvider` and environment diagnostics; verified with REAL psutil/PATH probes and FAKE browser fixtures).
- [x] COMPLETE — Navigation implemented (`navigate` with URL/egress/DNS validation and postcondition verification; verified with FAKE Playwright backend; live Chromium validation ENVIRONMENT-BLOCKED).
- [x] COMPLETE — DOM snapshot implemented (bounded `_DOM_SNAPSHOT_SCRIPT` omitting form values and flagging sensitive inputs; verified with FAKE Playwright backend).
- [x] COMPLETE — DOM semantic targeting implemented (`TargetResolver` ranking role/name, label, placeholder, test_id, and stable_id; verified with REAL `TargetResolver` and FAKE DOM snapshot).
- [x] COMPLETE — Click implemented (`browser.click` with pre-dispatch freshness check; verified with FAKE Playwright backend).
- [x] COMPLETE — Fill implemented (`browser.fill` and `browser.fill_secret`; verified with FAKE Playwright backend).
- [x] COMPLETE — Select implemented (`browser.select` with bounded option validation; verified with FAKE Playwright backend).
- [x] COMPLETE — Scroll is implemented in `PlaywrightBrowserProvider` and FAKE locator tests verify fresh-target validation; REAL Chromium execution remains tracked under environment blockers.
- [x] COMPLETE — Keyboard actions implemented (`browser.press` with bounded key descriptor validation; verified with FAKE Playwright backend).
- [x] COMPLETE — Popup pages are discovered by `PlaywrightBrowserProvider` and FAKE context tests cover scoped tab registration; REAL Chromium popup validation remains tracked under environment blockers.
- [x] COMPLETE — Target leases implemented (`ObservationLease` with DOM state hash and monotonic expiry; verified with REAL lease tests and FAKE Playwright backend).
- [x] COMPLETE — Stale browser-target re-resolution implemented (`reground_stale_target` re-observes DOM and re-binds unique semantic targets; verified with FAKE Playwright backend).
- [x] COMPLETE — Sensitive browser fields protected (password/token/card inputs reject plaintext `browser.fill`; verified with FAKE Playwright backend).
- [x] COMPLETE — SecretRef browser filling implemented (`browser.fill_secret` resolves `SecretRef` only at dispatch without logging; verified with FAKE Playwright backend).
- [x] COMPLETE — URL validation implemented (`validate_browser_url` and `validate_browser_egress_url`; verified with REAL validation tests).
- [x] COMPLETE — Redirect validation implemented (route interceptor validates every navigation/subresource URL and DNS binding; verified with FAKE Playwright route interceptor).
- [x] COMPLETE — Private-network protection implemented (loopback, RFC1918, link-local, metadata, and integer/hex IP literals blocked; verified with REAL IP/URL validation tests).
- [x] COMPLETE — DNS-rebinding protections implemented (`verify_browser_dns_binding` resolves host IPs and rejects private/loopback rebinding; verified with injected DNS resolver).
- [x] COMPLETE — WebSocket/network-egress controls implemented (`ws://`/`wss://` validated against domain allowlists, private-network rules, and DNS binding; verified with FAKE route tests).
- [x] COMPLETE — Screenshot capture implemented (`ScreenCaptureAdapter.capture_desktop`/`capture_screen` and `Win32ScreenCaptureBackend` in `src/arise/adapters/perception.py`; verified with FAKE capture backend and mocked Win32 GDI; live Windows desktop capture ENVIRONMENT-BLOCKED).
- [x] COMPLETE — Active-window capture implemented (`ScreenCaptureAdapter.capture_active_window`; verified with FAKE capture backend; live Windows capture ENVIRONMENT-BLOCKED).
- [x] COMPLETE — Region capture implemented (`ScreenCaptureAdapter.capture_region`; verified with FAKE capture backend; live Windows capture ENVIRONMENT-BLOCKED).
- [x] COMPLETE — Crop/downscale implemented (`crop_captured_image` and `downscale_captured_image` in pure Python PNG/RGBA; verified with REAL PNG buffer tests).
- [x] COMPLETE — Screenshot deduplication implemented (`ScreenshotDeduplicator` SHA-256 frame cache; verified with REAL PNG buffer tests).
- [x] COMPLETE — OCR fallback implemented (`OcrPerceptionAdapter` routing through `ModelRole.OCR`/`VISION` with confidence filtering; verified with FAKE OCR backend; live OCR model EXTERNAL-CONFIGURATION).
- [x] COMPLETE — Multimodal grounding implemented (`VisionGroundingAdapter` returning untrusted `GroundingProposal` candidates; verified with FAKE vision backend; live vision model EXTERNAL-CONFIGURATION).
- [x] COMPLETE — Vision confidence handling implemented (rejects proposals below `minimum_confidence`; verified with REAL unit tests).
- [x] COMPLETE — Vision target verification implemented (validates image bounds and optional OCR/UIA corroboration; verified with REAL unit tests).
- [x] COMPLETE — UIA → DOM → OCR → Vision → Coordinate hierarchy implemented (`PerceptionHierarchyPipeline` in `src/arise/adapters/perception.py`; verified with REAL resolver and FAKE perception backends).
- [x] COMPLETE — Coordinate click fallback is disabled unless `ARISE__PERCEPTION__ALLOW_COORDINATE_FALLBACK=true`; enabled fallback checks fresh UIA state, DPI, focus, human interference, and configured `ARISE__PERCEPTION__UNSAFE_REGIONS` physical-pixel rectangles. `CoordinateFallbackSafetyGate` rejects disabled/stale/unverified/unsafe coordinates; software guards are covered by deterministic geometry and FAKE UIA-contract tests. Live Windows execution remains ENVIRONMENT-BLOCKED below.
- [x] COMPLETE — Browser/UIA replay tests pass (`tests/test_browser_playwright.py` and `tests/test_windows_uia_and_perception.py`; FAKE/REPLAY scope).
- [x] COMPLETE — Failure-injection tests pass for browser/UIA failure modes (stale target, ambiguous target, focus loss, DPI change, human interference, crash/navigation recovery; FAKE backend scope).
- [!] BLOCKED — ENVIRONMENT — Run a real Playwright/Chromium installation and supported-Windows browser/UIA integration suite.

## Phase 3 — Voice

### Audio pipeline and lifecycle
- [x] COMPLETE — AudioHub implemented/integrated (`AudioHub` in `src/arise/core/voice.py` and server composition; verified in `tests/test_voice.py` and `ProductionRuntimeCompositionAuditTests` with FAKE/REPLAY audio ports).
- [x] COMPLETE — Typed voice ports preserved (`MicrophonePort`, `AudioPlaybackPort`, `VoiceActivityDetector`, `WakeWordDetector`, `SpeechRecognitionPort`, `SpeechSynthesisPort`, `LiveVoiceProvider`).
- [x] COMPLETE — Microphone lifecycle implemented (explicit start, capture loop, and close on stop/shutdown; verified with FAKE/REPLAY microphone port; Windows hardware ENVIRONMENT-BLOCKED).
- [x] COMPLETE — Playback lifecycle implemented (generation-gated `play`, immediate `stop`, and `close`; verified with FAKE playback port; Windows hardware ENVIRONMENT-BLOCKED).
- [x] COMPLETE — Device discovery implemented (`list_devices` on microphone and playback ports; verified with FAKE `sounddevice` module; real audio hardware ENVIRONMENT-BLOCKED).
- [x] COMPLETE — PCM handling implemented (`AudioChunk` `pcm_s16le` validation and framing; verified with REAL PCM buffer tests).
- [x] COMPLETE — VAD integrated (`WebRtcVadAdapter` and `VoiceActivity` analysis; verified with REAL `webrtcvad` synthetic PCM frames and FAKE VAD port).
- [x] COMPLETE — Dormant-first state implemented (`VoiceState.DORMANT` by default; no cloud audio before wake; verified with REAL `AudioHub` state tests).
- [x] COMPLETE — Wake/activation logic implemented (`VoskWakeWordDetector` and explicit push-to-talk/start listening; verified with FAKE Vosk recognizer; local Vosk weights EXTERNAL-CONFIGURATION).
- [x] COMPLETE — Streaming ASR integrated (`VoskSpeechRecognizer` streaming transcript segments; verified with FAKE Vosk recognizer; local Vosk weights EXTERNAL-CONFIGURATION).
- [x] COMPLETE — Transcript pipeline integrated (partial/final transcript merging and normalization; verified with REAL `AudioHub` transcript tests).
- [x] COMPLETE — Intent integration implemented (deterministic `IntentClassifier` gates task admission and local acknowledgement; verified with REAL `IntentClassifier`).
- [x] COMPLETE — TTS integrated (`KokoroSpeechSynthesis`, `LazyKokoroSpeechSynthesis`, and `AudioHub.speak_text` streaming synthesis into playback; verified with FAKE Kokoro engine; real ONNX weights EXTERNAL-CONFIGURATION).
- [x] COMPLETE — Audio playback integrated (generation-fenced playback queue in `AudioHub`; verified with FAKE playback port).
- [x] COMPLETE — Audio cleanup implemented (`close()` stops capture, playback, live session, and background tasks; verified with FAKE audio ports).
- [x] COMPLETE — Voice concurrency limits implemented (serialized state lock and single active session per hub; verified with REAL `AudioHub` concurrency tests).
- [x] COMPLETE — Voice cancellation implemented (explicit `cancel_task` tool and stop-listening controls; verified with REAL `TaskEngine` and FAKE audio ports).
- [x] COMPLETE — Voice interruption implemented (`barge_in` / speech-during-playback interruption; verified with FAKE/REPLAY audio frames).
- [x] COMPLETE — Generation fence implemented (`_minimum_output_generation` increments on barge-in; verified with REAL `AudioHub` generation fence tests).
- [x] COMPLETE — Stale voice output rejected (`VoiceEventKind.OUTPUT_GATED` drops chunks from older generations; verified with REAL `AudioHub` tests).
- [x] COMPLETE — Barge-in stops playback (`playback.stop()` invoked immediately on user speech during `SPEAKING`; verified with FAKE playback port).
- [x] COMPLETE — Barge-in forwards first user speech correctly (`session.interrupt(first_user_audio)` forwards barge-in frame; verified with FAKE live session).
- [x] COMPLETE — Device-loss recovery implemented (`AudioHub.recover_device_loss` and automatic capture-loop retry emit `VoiceEventKind.DEVICE_RECOVERED`; verified with FAKE device fault injection; physical hot-unplug ENVIRONMENT-BLOCKED).
- [x] COMPLETE — Voice reconnect implemented (bounded session reconnect and failure-injection tests in `tests/test_voice.py`; verified with FAKE session faults).
- [x] COMPLETE — Voice shutdown implemented (`AudioHub.close()` deterministic teardown verified with FAKE audio ports).
- [!] BLOCKED — ENVIRONMENT — Validate microphone/speaker permissions, real devices, local models, and voice cleanup on Windows hardware.

### Bridge and Gemini boundary
- [x] COMPLETE — Production VoiceConversationBridge composed behind explicit settings and authenticated lifecycle routes.
- [x] COMPLETE — VoiceConversationBridge submits through `TaskEngineVoiceAdapter` to the real TaskEngine; integration test runs the actual AgentRuntime/PolicyEngine/resource/verifier chain with a FAKE in-memory environment/tool, not real audio or host actions.
- [x] COMPLETE — Voice question tool-call is rejected without task admission through the real TaskEngine adapter; integrated fake/replay boundary test confirms the task repository remains unchanged.
- [x] COMPLETE — Voice command creates a task and reaches verified completion through the real TaskEngine/runtime in the simulated integration test; no real voice capture or host action is claimed.
- [x] COMPLETE — Ambiguous requests request clarification (`ask_user` / `REQUIRES_USER_INPUT` without task side effects).
- [x] COMPLETE — Voice task progress integrated (`watch_task` polls and coalesces authoritative `TaskEngine` status updates).
- [x] COMPLETE — Integrated bridge/TaskEngine test observes no success at admission, then only reports `verified=true` after the runtime verifier completes; execution evidence is FAKE simulator scope.
- [x] COMPLETE — Voice failure states integrated (auth failure, quota limit, network failure, and device error states surfaced in `VoiceStatusSnapshot`).
- [x] COMPLETE — Gemini Live adapter implemented (`GeminiLiveProvider` and `GeminiLiveSession` in `src/arise/adapters/gemini_live.py`).
- [x] COMPLETE — Gemini Live remains optional (local voice and text paths operate without Gemini).
- [x] COMPLETE — Gemini Live requires explicit consent/configuration (`voice.allow_cloud` and `security.allow_cloud_models` plus keyring secret).
- [x] COMPLETE — Gemini Live cannot directly execute OS actions (tool declarations are restricted to bridge tools only).
- [x] COMPLETE — Gemini tool boundary implemented (`voice_tool_declarations` allowlist enforced).
- [x] COMPLETE — `execute_task` interface implemented.
- [x] COMPLETE — `ask_user` interface implemented.
- [x] COMPLETE — `get_task_status` implemented.
- [x] COMPLETE — `cancel_task` implemented.
- [x] COMPLETE — `report_status` implemented as an alias of the same principal-scoped authoritative status read; voice bridge tests pass.
- [x] COMPLETE — Gemini cannot claim unverified success (output speech claiming completion before `TaskStatus.COMPLETED` is gated).
- [x] COMPLETE — Gemini generation interruption implemented (`interrupt` increments output generation and halts stale audio).
- [x] COMPLETE — Gemini reconnect implemented (bounded reconnect with sanitized error classification tested with FAKE session faults).
- [x] COMPLETE — Gemini stale output blocked (generation fence drops pre-interruption audio/text events).
- [x] COMPLETE — Immediate local/template acknowledgement implemented (`VoiceConversationBridge.immediate_acknowledgement` and `AudioHub.acknowledge_locally`).
- [x] COMPLETE — Cloud round-trip not required for acknowledgement (local template acknowledgement synthesized immediately before cloud turnaround).
- [x] COMPLETE — Voice responses can stream into TTS (`AudioHub.speak_text` streams synthesized `AudioChunk`s into `playback`).
- [x] COMPLETE — AudioHub owns actual audio resources (single owner of microphone capture and speaker playback ports).

### Voice evidence requirements
- [x] COMPLETE — 21-stage voice validation retained (`src/arise/adapters/windows_voice_validation.py` and `arise-voice-check`).
- [x] COMPLETE — REAL/FAKE/REPLAY distinction preserved.
- [x] COMPLETE — PASS/PARTIAL/FAILED/SKIPPED/BLOCKED distinction preserved.
- [x] COMPLETE — Environment status preserved.
- [x] COMPLETE — Sanitized diagnostics preserved.
- [x] COMPLETE — No raw audio persisted.
- [x] COMPLETE — No raw secrets persisted.
- [!] BLOCKED — ENVIRONMENT — Run REAL Windows microphone/speaker/local-model/Gemini validation; do not substitute fake/replay evidence.
- [?] NEEDS EXTERNAL CONFIGURATION — Supply a supported device, user-supplied Vosk/Kokoro model files, optional Gemini SDK, and OS-keyring credential for the consented runtime checks.

## Phase 4 — Agent intelligence

### Intent and task planning
- [x] COMPLETE — Intent engine fully integrated (`IntentClassifier` integrated across `/api/v1/interactions` and `VoiceConversationBridge`).
- [x] COMPLETE — QUESTION classification implemented and routed to the tool-free FAST_REASONER path; authenticated API test returns an informational answer without admitting a task.
- [x] COMPLETE — COMMAND classification implemented and routed through the real TaskEngine/SQLite admission path; server integration test passes.
- [x] COMPLETE — CONVERSATION classification implemented; non-action text routes to the informational provider path without granting execution authority.
- [x] COMPLETE — AMBIGUOUS classification implemented; low-confidence action-like text receives clarification and creates no task, covered by server integration test.
- [x] COMPLETE — Intent confidence implemented as an advisory deterministic score; classifier tests pass and it grants no execution authority.
- [x] COMPLETE — Structured entities are derived as bounded action targets, quoted-text spans, and URL spans from the user request; intent tests verify offsets and question requests receive no action entities.
- [x] COMPLETE — Structured command hints contain deterministic action/target phrases and are passed to GatewayTaskPlanner only as explicitly untrusted context; planner regression tests verify the hint does not add authority or supersede the original request.
- [x] COMPLETE — Planner implemented (`GatewayTaskPlanner` in `src/arise/core/planner.py`).
- [x] COMPLETE — Single-step planning implemented.
- [x] COMPLETE — Multi-step planning implemented.
- [x] COMPLETE — Dependency handling implemented (`_topological_order` cycle/missing-dependency validation).
- [x] COMPLETE — Conditional steps implemented (`PlanStep.condition` and `skip_when_condition_false` evaluated in `TaskEngine`).
- [x] COMPLETE — Parallel-safe steps implemented (`PlanStep.parallel_safe` batching for disjoint-resource R0/R1 steps in `TaskEngine`).
- [x] COMPLETE — Verification checkpoints implemented (`PlanStep.verification_checkpoint` and per-step postcondition verification).
- [x] COMPLETE — Retry strategy implemented (`StepRetryPolicy` on `PlanStep` enforced for idempotent pre-effect failures).
- [x] COMPLETE — Fallback strategy implemented (`StepFallbackPolicy` executes policy-checked `fallback_action` when primary action fails/blocks).
- [x] COMPLETE — Cancellation-aware planning implemented (`asyncio.CancelledError` propagated and task marked `CANCELLED`).

### Per-step plan contract
- [x] COMPLETE — Every plan step supports Action (`step.action` / `step.tool_name`).
- [x] COMPLETE — Every plan step supports Arguments (`step.parameters`).
- [x] COMPLETE — Every plan step supports Target (`step.target`).
- [x] COMPLETE — Every plan step supports Preconditions (`step.preconditions`).
- [x] COMPLETE — Every plan step supports Expected postconditions (`step.expected_postconditions`).
- [x] COMPLETE — Every plan step supports Verification strategy (`step.verification_method`).
- [x] COMPLETE — Every plan step supports Risk (`step.risk_level`).
- [x] COMPLETE — Every plan step supports Timeout (`step.timeout`).
- [x] COMPLETE — Every plan step supports Retry policy (`step.retry_policy`).
- [x] COMPLETE — Every plan step supports Resource requirements (`step.required_resources`).
- [x] COMPLETE — Planner cannot bypass PolicyEngine (all planned and fallback actions execute through `AgentRuntime` + `PolicyEngine`).
- [x] COMPLETE — Planner cannot directly execute arbitrary OS commands (only registered `ToolRegistry` tools may be proposed).
- [x] COMPLETE — Planner cannot directly execute arbitrary Python (strict JSON schema validation into `TaskPlan`).
- [x] COMPLETE — Planner cannot directly execute arbitrary mouse coordinates (R2+ actions require semantic targets; coordinate fallback is gated by `CoordinateFallbackSafetyGate`).

### Model gateway and runtime optimization
- [x] COMPLETE — Dynamic model routing implemented (`ModelRouter.select` and `complete` across task/privacy/cost/latency policies).
- [x] COMPLETE — Planner/reasoning model routing implemented (`ModelRole.PLANNER`, `FAST_REASONER`, `DEEP_REASONER`).
- [x] COMPLETE — Vision model routing implemented (`ModelRole.VISION` in `ModelRouter` and `VisionGroundingAdapter`).
- [x] COMPLETE — ASR model routing implemented (`ModelRole.ASR`).
- [x] COMPLETE — OCR routing implemented (`ModelRole.OCR` in `ModelRouter` and `OcrPerceptionAdapter`).
- [x] COMPLETE — Embedding routing implemented (`ModelRole.EMBEDDING` and `EmbeddingPort`).
- [x] COMPLETE — TTS routing implemented (`ModelRole.TTS`).
- [x] COMPLETE — Optional deep-reasoning escalation implemented (`ModelRouter.complete_with_escalation`).
- [x] COMPLETE — Provider health implemented (`ProviderStatus` latency, error code, and circuit state).
- [x] COMPLETE — Provider timeout implemented (`asyncio.wait_for` per-request timeout in `ModelRouter`).
- [x] COMPLETE — Provider cancellation implemented (`MODEL_CANCELLED` event and cancellation propagation).
- [x] COMPLETE — Provider fallback implemented (ordered candidate failover with `MODEL_FALLBACK` audit event).
- [x] COMPLETE — Provider cooldown implemented (`failure_threshold` and `cooldown_seconds` circuit breaker).
- [x] COMPLETE — Concurrency limits implemented (global and per-provider semaphores).
- [x] COMPLETE — Streaming implemented where supported (`ModelRouter.stream` and `OpenAICompatibleProvider.stream`).
- [x] COMPLETE — Request deduplication implemented where safe (`ScreenshotDeduplicator` and request-ID idempotency).
- [x] COMPLETE — Sanitized provider telemetry implemented (redacted error codes and latency metrics without prompt/secret leakage).
- [x] COMPLETE — Persistent model connections used where appropriate (reused `httpx.AsyncClient` lifecycle on `OpenAICompatibleProvider`).
- [x] COMPLETE — Connection pooling implemented where appropriate (`httpx.Limits(max_connections=20, max_keepalive_connections=10)`).
- [x] COMPLETE — Backpressure implemented (`max_queued_requests` in `ModelRouter` with `MODEL_QUEUE_BACKPRESSURE` event).
- [x] COMPLETE — Response streaming implemented (`ModelStreamChunk` async iterator over SSE).
- [x] COMPLETE — Unnecessary sequential model calls reduced (deterministic `IntentClassifier` and local template acknowledgements avoid redundant LLM round-trips).
- [x] COMPLETE — Safe parallel model calls implemented (`TaskEngine` parallel-safe batching and concurrent router requests within semaphore bounds).
- [x] COMPLETE — Progress updates integrated (durable task/step events and `VoiceConversationBridge.watch_task`).
- [x] COMPLETE — Progress updates are not spammy (bounded poll interval and state-transition deduplication).
- [x] COMPLETE — Immediate acknowledgement implemented (`VoiceConversationBridge.immediate_acknowledgement` and `AudioHub.acknowledge_locally`).
- [x] COMPLETE — Current task state visible to assistant (`get_task_status` / `report_status` and `WorkingMemoryStore`).
- [?] NEEDS EXTERNAL CONFIGURATION — Configure a supported local/cloud planning provider and credentials to validate provider behavior against a real endpoint.

## Phase 4 — Web research
- [x] COMPLETE — Research subsystem implemented (`BraveWebResearchAdapter` and `PublicHTTPSPageFetcher` in `src/arise/adapters/brave_research.py`).
- [x] COMPLETE — Search integration implemented (Brave web search API integration with query sanitization).
- [x] COMPLETE — Source discovery implemented (domain-filtered public HTTPS URL extraction).
- [x] COMPLETE — Page retrieval implemented (`PublicHTTPSPageFetcher` with redirect and private-IP validation).
- [x] COMPLETE — Source extraction implemented (`_HTMLTextExtractor` stripping scripts/styles/hidden elements).
- [x] COMPLETE — Source metadata tracked (`title`, `source_id`, `provenance`, `citation`).
- [x] COMPLETE — Source timestamps tracked (`retrieved_at` and `published_at`).
- [x] COMPLETE — Provenance tracked (domain and title provenance preserved on every `RetrievedContext`).
- [x] COMPLETE — Citation data preserved (`citation` field formatted and returned in API/planner context).
- [x] COMPLETE — Relevance filtering implemented (`_compute_relevance` and `ResearchQuery.min_relevance` filtering).
- [x] COMPLETE — Conflicting-source handling implemented (`detect_conflicting_sources` populates `conflicts_with` on contradictory numeric/polarity claims).
- [x] COMPLETE — Retrieval bounds implemented (max results, max body bytes, max text chars enforced).
- [x] COMPLETE — Research timeout implemented (bounded HTTP/search timeouts enforced).
- [x] COMPLETE — Research cancellation implemented (`asyncio.CancelledError` aborts in-flight page fetches cleanly).
- [x] COMPLETE — Current-information surface forms require explicit one-time web consent and route through bounded research when enabled; API tests verify no implicit search/task and the consented provenance-bearing path (fake provider evidence).
- [x] COMPLETE — Static model knowledge not falsely presented as current research (without research consent/provider, current-info requests return explicit unavailable/consent message).
- [x] COMPLETE — Web content treated as untrusted (`AuthorityLevel.UNTRUSTED_CONTEXT_ONLY` and `TrustLevel.UNTRUSTED_EXTERNAL`).
- [x] COMPLETE — Prompt injection isolation implemented (`UNTRUSTED_WEB_RESEARCH_JSON` system boundary and tool-free synthesis).
- [x] COMPLETE — Retrieved instructions cannot override policy (`PolicyEngine` denies `UNTRUSTED_EXTERNAL` side effects).
- [x] COMPLETE — Retrieved instructions cannot directly execute actions (research synthesis path has zero tool access).
- [x] COMPLETE — Research output isolated from privileged control path (never mutates `AuthorizationContext` or `PolicyEngine`).
- [x] COMPLETE — Research synthesis integration tests exercise the authenticated API with fake search/model providers and preserved source metadata; live Brave/answer-provider behavior remains tracked under external configuration.
- [x] COMPLETE — API and planner prompt-injection regressions send hostile source text only as untrusted context and verify zero unauthorized tool calls.
- [x] COMPLETE — Source-provenance tests pass (`tests/test_brave_research.py` and `tests/test_phase4_and_phase5_and_scenarios.py`).
- [?] NEEDS EXTERNAL CONFIGURATION — Configure Brave key in OS keyring and both research/egress opt-ins for a live request.

## Phase 5 — Memory
- [x] COMPLETE — Working memory implemented (`WorkingMemoryStore` and `WorkingMemorySnapshot` in `src/arise/core/personalization.py`).
- [x] COMPLETE — Short-term memory implemented (`ShortTermConversationMemory` with principal/session isolation and turn bounds).
- [x] COMPLETE — Persistent semantic memory implemented (`SQLiteMemoryRepository` with `LocalDeterministicEmbeddingAdapter` and optional `OpenAICompatibleEmbeddingAdapter`).
- [x] COMPLETE — Episodic memory implemented (`SQLiteMemoryRepository.record_episodic_task_summary`).
- [x] COMPLETE — Procedural memory implemented (`ProceduralMemoryStore` and `ProceduralWorkflow` in `src/arise/core/personalization.py`).
- [x] COMPLETE — Working memory expires correctly (TTL-enforced `expires_at` on `WorkingMemorySnapshot`).
- [x] COMPLETE — Short-term memory is bounded (`max_turns_per_session` and `max_chars_per_turn` enforced).
- [x] COMPLETE — Semantic memory writes are gated by exact, one-use user consent; API and SQLite tests cover mismatch, replay, expiry, and successful storage.
- [x] COMPLETE — Episodic memory stores useful task history (redacted goal, terminal status, verified step count, and `source_task_id`).
- [x] COMPLETE — Procedural memory stores reusable workflows (`ProceduralWorkflow` with parameterized `PlanStep`s, `goal_pattern`, and `provenance_task_id`).
- [x] COMPLETE — Memory records retain explicit-user-consent provenance, category, timestamps, expiry, and optional source-task linkage; retrieval preserves source ID/provenance in tests.
- [x] COMPLETE — Memory writes require an exact, principal-scoped, expiring, one-use consent; repository and authenticated API tests pass, including no embedding-provider call before a valid grant is consumed.
- [x] COMPLETE — Consent references are stored only as SHA-256 hashes with principal, entry fingerprint, expiry, and consumption state; SQLite consent tests pass.
- [x] COMPLETE — Memory creation timestamps and expiry timestamps are persisted and returned by the authenticated memory API.
- [x] COMPLETE — Memory confidence stored (`confidence`, `sensitivity`, and `expiration_policy` persisted in SQLite and exposed via `/api/v1/memory`).
- [x] COMPLETE — Expiry is enforced on reads/retrieval and explicit purge; SQLite tests cover expired records and grants.
- [x] COMPLETE — Authenticated memory list/search/export/edit/settings APIs and typed Memory UI controls are implemented; API and frontend contract tests pass.
- [x] COMPLETE — Users can inspect saved memory text, category, provenance, creation time, and expiry in the Memory UI.
- [x] COMPLETE — Per-record deletion is principal-scoped; the UI asks before delete and authenticated API/SQLite tests cover deletion.
- [x] COMPLETE — Clear-all requires explicit confirmation and removes both memory records and outstanding consent grants; API tests pass.
- [x] COMPLETE — With `memory.enabled=false` or per-principal `PUT /api/v1/memory/settings` (`enabled=false`), memory writes/retrievals are disabled and `MemoryDisabledError` is enforced.
- [x] COMPLETE — Secrets excluded from normal memory (`DEFAULT_REDACTOR` + `validate_memory_write_governance`).
- [x] COMPLETE — Passwords excluded from normal memory.
- [x] COMPLETE — API keys excluded from normal memory.
- [x] COMPLETE — Authentication tokens excluded from normal memory.
- [x] COMPLETE — Embedding integration implemented (`LocalDeterministicEmbeddingAdapter` and optional `OpenAICompatibleEmbeddingAdapter`).
- [x] COMPLETE — Embedding generation implemented (`embed_documents` and `embed_query`).
- [x] COMPLETE — Vector/semantic storage implemented (`embedding_json` and `embedding_model_id` columns in `memory_records`).
- [x] COMPLETE — Semantic search implemented (cosine similarity blended with lexical token overlap in `SQLiteMemoryRepository.retrieve`).
- [x] COMPLETE — Relevance filtering implemented (positive score threshold and top-`limit` ranking).
- [x] COMPLETE — GatewayTaskPlanner retrieves principal-scoped consented SQLite memory and labels it as untrusted context; an integration test exercises consent → real SQLite persistence/lexical retrieval → planner serialization with a FAKE model provider (live embedding configuration is tracked separately).
- [x] COMPLETE — Current user request takes priority over stale memory (system prompt and message ordering place current user request last as the sole instruction authority).
- [x] COMPLETE — Memory cannot weaken security (`UNTRUSTED_MEMORY_CONTEXT_JSON` carries `AuthorityLevel.UNTRUSTED_CONTEXT_ONLY`).
- [x] COMPLETE — Memory cannot override policy (`PolicyEngine` evaluates only trusted `AuthorizationContext`).
- [x] COMPLETE — Memory cannot bypass approvals (R2/R3/R4 confirmation gates are enforced regardless of memory content).
- [?] NEEDS EXTERNAL CONFIGURATION — Configure an embedding endpoint/model (and any required keyring secret) to validate real vector generation/retrieval.

## Phase 5 — Personalization and learning
- [x] COMPLETE — Preferred browser can be remembered (`PersonalizationProfile.preferred_browser` via `PersonalizationStore` and `/api/v1/personalization`).
- [x] COMPLETE — Preferred application can be remembered (`PersonalizationProfile.preferred_apps`).
- [x] COMPLETE — A user-saved, exact-consent `preference` memory is retrieved as explicitly untrusted context for local informational answers; authenticated API tests verify the separate cloud-context opt-in gate, keep the current request as the final user instruction, and admit no task.
- [x] COMPLETE — Approved workflow preferences can be remembered (`PersonalizationProfile.approved_workflows` and `ProceduralWorkflow.approved_by_user`).
- [x] COMPLETE — TTS preferences can be remembered (`preferred_tts_voice` and `preferred_tts_speed`).
- [x] COMPLETE — Procedural workflow retrieval implemented (`ProceduralMemoryStore.match_workflow` and `list_workflows`).
- [x] COMPLETE — Procedural workflow execution integrated (`adapt_workflow_to_task_plan` produces a standard `TaskPlan` executed through `TaskEngine` → `PolicyEngine` → `AgentRuntime` → `VerifierPort`).
- [x] COMPLETE — Procedure provenance implemented (`provenance_task_id`, `created_at`, `updated_at`, `last_verified_at`, `execution_count`).
- [x] COMPLETE — Stale-procedure detection implemented (`ProceduralMemoryStore.detect_stale_workflow_steps`).
- [x] COMPLETE — Re-grounding of stale procedures implemented (`target_overrides` in `adapt_workflow_to_task_plan` combined with `reground_stale_target`).
- [x] COMPLETE — Successful workflow adaptation implemented (`adapt_workflow_to_task_plan` and `record_execution_verified`).
- [x] COMPLETE — Failed workflow does not blindly repeat forever (bounded `StepRetryPolicy` and `ActionReplayError` on `UNKNOWN` outcomes).
- [x] COMPLETE — Procedural-category records and workflows are editable through `/api/v1/memory/{record_id}` and `/api/v1/workflows/{workflow_id}`.
- [x] COMPLETE — Procedural-category records and workflows can be deleted individually via `/api/v1/memory/{record_id}` and `/api/v1/workflows/{workflow_id}` or cleared with explicit confirmation.
- [x] COMPLETE — Learned workflow never bypasses policy (`adapt_workflow_to_task_plan` outputs untrusted `PlanStep` proposals that must pass `PolicyEngine` and `VerifierPort`).

## Security checklist
- [x] COMPLETE — Task-list APIs/repositories scope history to the authenticated principal (server and in-memory repository tests pass).
- [x] COMPLETE — Model cannot bypass policy (`AgentRuntime` evaluates `PolicyEngine` on every action contract).
- [x] COMPLETE — Model cannot directly execute privileged tools (`RiskLevel.R4` denied by default; tool registry allowlist enforced).
- [x] COMPLETE — Tool output cannot override system policy (tool results are bounded metadata, never `AuthorizationContext`).
- [x] COMPLETE — Web content cannot override system policy (`TrustLevel.UNTRUSTED_EXTERNAL` denied for side effects).
- [x] COMPLETE — Memory cannot override current instructions (injected only as `UNTRUSTED_MEMORY_CONTEXT_JSON` prior to the user's current instruction).
- [x] COMPLETE — Voice cannot bypass task admission (`VoiceConversationBridge` routes all commands through `TaskEngineVoiceAdapter`).
- [x] COMPLETE — Destructive actions use appropriate risk controls (`R3` requires explicit single-use confirmation; `R4` disabled by default).
- [x] COMPLETE — External communication uses appropriate risk controls (`R3` confirmation bound to exact contract fingerprint).
- [x] COMPLETE — Credential handling is isolated (`SecretRef` resolved only inside adapter dispatch; sensitive UIA/browser fields reject plaintext).
- [x] COMPLETE — Common credential-shaped values are redacted from events, conversation persistence, and research queries/results (`DEFAULT_REDACTOR` regression tests pass).
- [x] COMPLETE — Sensitive event payloads are recursively redacted at the event-envelope boundary before persistence/publication; event-bus regression tests pass.
- [x] COMPLETE — Shell execution is policy-controlled (no arbitrary shell tool is registered; `PolicyEngine` blocks unregistered/privileged tools).
- [x] COMPLETE — Network access is controlled (loopback-only API bind, explicit cloud/research opt-ins, and DNS/IP egress validation).
- [x] COMPLETE — Browser navigation is validated (`validate_browser_url`, `validate_browser_egress_url`, and `verify_browser_dns_binding`).
- [x] COMPLETE — Unknown action results are handled safely (marked `UNKNOWN`, never automatically retried).
- [x] COMPLETE — Non-idempotent actions are not blindly retried (`StepRetryPolicy` only retries `Idempotency.IDEMPOTENT` pre-effect failures).

## Recovery checklist
- [x] COMPLETE — Model timeout recovery (`ModelRouter` timeout handling, circuit cooldown, and fallback provider selection).
- [x] COMPLETE — Model disconnect recovery (`ModelRouter` catches transport failures and fails over to secondary providers).
- [x] COMPLETE — Gemini disconnect recovery (bounded session reconnect in `AudioHub` tested with FAKE session faults).
- [x] COMPLETE — Browser crash recovery (`PlaywrightBrowserProvider.recover_after_crash` clears stale state and opens a fresh isolated page).
- [x] COMPLETE — Browser navigation recovery (`PlaywrightBrowserProvider.recover_navigation` invalidates stale observations and restores a safe URL).
- [x] COMPLETE — UI target disappearance recovery (`reground_stale_target` re-observes or fails closed with `TARGET_NOT_FOUND`/`TARGET_STALE`).
- [x] COMPLETE — Application closure recovery (`WindowsUiaProvider` detects closed/missing HWND and fails closed with `WINDOW_NOT_FOUND`).
- [x] COMPLETE — Focus-change recovery (`WindowsUiaProvider` detects foreground HWND change and blocks stale dispatch with `ENVIRONMENT_CHANGED`).
- [x] COMPLETE — Network-loss recovery (offline/transport errors classify cleanly and surface degraded status without crashing the runtime).
- [x] COMPLETE — Voice-device-loss recovery (`AudioHub.recover_device_loss` re-enumerates input/output devices and emits `DEVICE_RECOVERED`).
- [x] COMPLETE — Task cancellation recovery (cooperative task cancellation releases resource leases and transitions task to `CANCELLED`).
- [x] COMPLETE — Voice interruption recovery (barge-in stops playback, increments generation fence, and preserves active `TaskEngine` tasks).
- [x] COMPLETE — Unknown-outcome reconciliation (`TaskStatus.UNKNOWN` halts automatic execution until explicit user/operator reconciliation).
- [x] COMPLETE — Safe retry logic (`StepRetryPolicy` and `AgentRuntime` replay guards prevent unsafe duplicate side effects).

## Frontend and desktop checklist
- [x] COMPLETE — Conversation UI reflects actual runtime state (`frontend/src/App.tsx` driven by REST snapshots and `/ws/v1` events).
- [x] COMPLETE — Dormant state visible.
- [x] COMPLETE — Listening state visible.
- [x] COMPLETE — Thinking state visible.
- [x] COMPLETE — Working state visible.
- [x] COMPLETE — Speaking state visible.
- [x] COMPLETE — Interrupted state visible.
- [x] COMPLETE — Waiting state visible.
- [x] COMPLETE — Approval state visible.
- [x] COMPLETE — Completed state visible.
- [x] COMPLETE — Failed state visible.
- [x] COMPLETE — Cancellation state visible.
- [x] COMPLETE — Current task visible.
- [x] COMPLETE — Current step visible.
- [x] COMPLETE — Progress visible.
- [x] COMPLETE — Model/provider health visible.
- [x] COMPLETE — Voice health visible.
- [x] COMPLETE — Memory controls visible (inspect, search, create, consented edit/replace, delete, clear, export, plus API settings).
- [x] COMPLETE — Research state visible.
- [x] COMPLETE — History visible (task history list, export, and confirmed clear).
- [x] COMPLETE — Reconnect works (WebSocket cursor tracking and replay-floor recovery).
- [x] COMPLETE — Stale state is cleared correctly (`EVENT_CURSOR_EXPIRED` triggers full state refresh).
- [x] COMPLETE — Backend errors are sanitized for users.
- [!] BLOCKED — ENVIRONMENT — Exercise the integrated frontend inside real Tauri/WebView2 on supported Windows.

## Production and packaging checklist
- [x] COMPLETE — Tauri 2 configuration and Rust shell compile with the Windows CI `cargo check`; installer/runtime validation remains separate below.
- [x] COMPLETE — React production build valid (`npm --prefix frontend run typecheck` and `npm --prefix frontend run build` pass cleanly).
- [x] COMPLETE — Python backend packaging path valid (`build_sidecar` in `src/arise/supervisor.py` and `scripts/build_sidecar.py`).
- [x] COMPLETE — Sidecar architecture valid (`resolve_backend_command`, `sidecar_binary_name`, and `find_bundled_sidecar` in `frontend/src-tauri/src/main.rs`).
- [x] COMPLETE — Backend process supervision implemented (`BackendSupervisor` in `src/arise/supervisor.py` and `BackendProcess` in `frontend/src-tauri/src/main.rs`).
- [x] COMPLETE — Startup sequencing implemented (`ARISE_BACKEND_READY` stdout handshake before opening client traffic).
- [x] COMPLETE — Health-check startup gate implemented (readiness signal + health probe in `BackendSupervisor`).
- [x] COMPLETE — Shutdown implemented (parent-owned stdin pipe EOF + grace period + kill fallback).
- [x] COMPLETE — Crash handling implemented (`BackendSupervisor` detects unexpected child exit code).
- [x] COMPLETE — Restart policy implemented (bounded `max_restarts` with exponential backoff in `BackendSupervisor`).
- [x] COMPLETE — Single-instance integration implemented (`SingleInstanceLock` + port-in-use refusal).
- [x] COMPLETE — Configuration setup documented (`README.md`, `.env.example`, and `docs/security.md`).
- [x] COMPLETE — First-run capability discovery implemented (`/api/v1/capabilities` and `/api/v1/diagnostics`).
- [x] COMPLETE — Optional providers remain optional (local runtime starts and passes health checks with zero cloud credentials).
- [x] COMPLETE — Capability states are truthful (`CapabilityService` reflects registered adapters and configuration gates).
- [!] BLOCKED — ENVIRONMENT — Package and run the Tauri sidecar against real WebView2; validate per-user ACLs on a supported Windows desktop.

## Integrated acceptance-path checklist
- [x] COMPLETE — Voice command → real intent → real task admission → TaskEngine → planner → policy → executor → verifier → completion (verified end-to-end in `tests/test_phase4_and_phase5_and_scenarios.py` with FAKE browser/UIA backends; physical Windows/audio remains environment-limited).
- [x] COMPLETE — Question → informational answer → NO task admission is integrated through FastAPI, ModelRouter, session persistence, and real TaskEngine storage; server tests inject a fake model provider and prove no task exists.
- [x] COMPLETE — Browser task → browser adapter → semantic target → execution → verification (verified through `TaskEngine` → `PlaywrightBrowserProvider` → `VerifierPort` in Scenario 2 with FAKE Playwright page).
- [x] COMPLETE — Research → search → sources → provenance → synthesis → citations (verified with relevance filtering and conflict detection in `test_brave_research.py` and `test_phase4_and_phase5_and_scenarios.py`).
- [x] COMPLETE — Memory → consent → persistence → embedding → retrieval → planner integration (verified with `SQLiteMemoryRepository`, `LocalDeterministicEmbeddingAdapter`, and `GatewayTaskPlanner`).
- [x] COMPLETE — Procedural memory → retrieve workflow → execute semantically → verify → adapt when stale (verified with `ProceduralMemoryStore` in `tests/test_phase4_and_phase5_and_scenarios.py`).
- [x] COMPLETE — Voice interruption → VAD → barge-in → playback stop → generation cancellation → stale-output rejection → new request (verified in `tests/test_voice.py` and Scenario 3).
- [x] COMPLETE — Provider failure → failure detection → fallback/recovery → user-visible status is covered through authenticated FastAPI + real ModelRouter routing and a persisted fallback event with FAKE providers.

## Acceptance scenarios (must remain general; do not hard-code them)
- [x] COMPLETE — Scenario 1: “Open Chrome.” is classified as a command; task is planned, policy-checked, executed, independently verified, and only then reported complete/spoken (verified in simulated/FAKE browser integration; real Windows desktop execution remains environment-limited).
- [x] COMPLETE — Scenario 2: “Tell me how to open Chrome.” / “What is Windows UI Automation?” is classified as a question; no OS task is admitted and an informational response is returned.
- [x] COMPLETE — Scenario 3: multi-step browser task uses browser discovery, navigation, DOM/UI grounding, actions, verification, extraction, and response (verified in `tests/test_phase4_and_phase5_and_scenarios.py` with FAKE Playwright backend).
- [x] COMPLETE — Scenario 4: current-information request uses official-source research, provenance, synthesis, conflict detection, and verification without trusting web instructions.
- [x] COMPLETE — Scenario 5: “Start my development environment.” / “Open Windows Settings” retrieves an approved procedure where available, checks preconditions, adapts stale targets, and executes scoped steps through `PolicyEngine` and `VerifierPort`.

## Test checklist
- [x] COMPLETE — Unit tests for task lifecycle.
- [x] COMPLETE — Unit tests for policy.
- [x] COMPLETE — Unit tests for target identity (full Windows/UIA and browser DOM identity coverage in `tests/test_windows_uia_and_perception.py` and `tests/test_browser_playwright.py`).
- [x] COMPLETE — Environment diagnostics tests include REAL psutil discovery of the local process, FAKE Windows/audio probe contracts, redaction/failure states, authenticated API exposure, and typed frontend integration; no Win32 runtime claim.
- [x] COMPLETE — Unit tests for resource leases.
- [x] COMPLETE — Unit tests for cancellation.
- [x] COMPLETE — Unit tests for generation fences.
- [x] COMPLETE — Unit tests for model routing.
- [x] COMPLETE — Memory-consent unit/API tests use real SQLite and the authenticated FastAPI app to cover exact scope, expiry, replay, cross-principal rejection, concurrent consumption, and no embedding egress before valid consent.
- [x] COMPLETE — Unit tests for semantic retrieval (`LocalDeterministicEmbeddingAdapter` and `OpenAICompatibleEmbeddingAdapter` tests).
- [x] COMPLETE — Unit tests for research provenance (`tests/test_brave_research.py` and conflict detection tests).
- [x] COMPLETE — API-level and planner-level research prompt-injection regressions pass (`tests/test_server.py`, `tests/test_planner_context.py`, and Scenario 7).
- [x] COMPLETE — Voice integration tests include a real in-process TaskEngine/AgentRuntime/PolicyEngine/resource/verifier path; environment/tool are FAKE and no microphone or host automation is claimed.
- [x] COMPLETE — Voice → TaskEngine integration test exercises real TaskEngine/AgentRuntime/PolicyEngine/resource/verifier components and proves verified status gating; `InMemoryEnvironment`/`SetFactTool` are FAKE evidence and do not verify physical voice or host automation.
- [x] COMPLETE — Question → no task API integration test (real TaskEngine/SQLite, fake answer provider).
- [x] COMPLETE — Command → task API integration test (real TaskEngine/SQLite admission; no host action is claimed).
- [x] COMPLETE — Ambiguous → clarification API integration test proves no task is admitted.
- [x] COMPLETE — Task → policy test.
- [x] COMPLETE — Policy → executor test.
- [x] COMPLETE — Executor → verifier test.
- [x] COMPLETE — Verifier → completion test.
- [x] COMPLETE — Browser integration tests (`tests/test_browser_playwright.py` and Scenario 2 with FAKE Playwright backend).
- [x] COMPLETE — UIA integration tests (`tests/test_windows_uia_and_perception.py` with FAKE UIA backend).
- [x] COMPLETE — Consent → real SQLite write/retrieval → GatewayTaskPlanner context integration is tested; the planner provider is FAKE and no live embedding/provider behavior is claimed.
- [x] COMPLETE — Research integration tests (`tests/test_brave_research.py` and `tests/test_server.py`).
- [x] COMPLETE — Recovery tests (`tests/test_sqlite.py`, `tests/test_browser_playwright.py`, `tests/test_voice.py`, and `tests/test_phase4_and_phase5_and_scenarios.py`).
- [x] COMPLETE — Failure-injection tests (UIA human interference, focus drift, DPI change, browser crash, unknown outcome, and provider failure).
- [x] COMPLETE — Replay tests.
- [x] COMPLETE — Frontend/backend contract tests cover task/event models, bounded replay recovery, and the `/interactions` TypeScript API/UI contract; relevant tests and build pass.
- [x] COMPLETE — Provider unit tests and authenticated question→ModelRouter fallback tests cover sanitized failure, fallback event, and user-visible answer with FAKE providers; live provider faults remain unverified.
- [x] COMPLETE — Provider timeout tests.
- [x] COMPLETE — Provider cancellation tests.
- [x] COMPLETE — Stale-target tests (both browser DOM and Windows UIA stale target rejection and semantic re-grounding).
- [x] COMPLETE — Focus-change tests (`test_focus_change_and_display_dpi_change_and_human_interference_abort_dispatch`).
- [x] COMPLETE — Network-failure tests (DNS rebinding, private IP blocking, and provider transport failure tests).
- [x] COMPLETE — Browser-crash tests (`test_browser_crash_recovery_and_navigation_recovery`).
- [x] COMPLETE — Device-loss tests (`test_device_loss_recovery_switches_to_available_device`).
- [x] COMPLETE — Unknown-outcome tests (Scenario 8 and `test_runtime.py`).
- [x] COMPLETE — Duplicate-request tests (`test_task_engine.py` and `test_sqlite.py`).

## Quality checklist
- [x] COMPLETE — Ruff check passes (`.venv/bin/ruff check .`).
- [x] COMPLETE — Python formatting passes (`.venv/bin/ruff format --check .`).
- [x] COMPLETE — Python compile passes (`.venv/bin/python -m compileall -q src tests scripts`).
- [x] COMPLETE — Full pytest passes (319 tests and 68 subtests; one upstream Starlette/httpx deprecation warning; rerun after Windows-release safety and lifecycle tests).
- [x] COMPLETE — Frontend typecheck passes (`npm --prefix frontend run typecheck`).
- [x] COMPLETE — Frontend production build passes (`npm --prefix frontend run build`).
- [x] COMPLETE — Secret/token scan passes (no high-confidence credential patterns in source/config/docs; dependency/build dirs excluded).
- [x] COMPLETE — `git diff --check` passes.
- [x] COMPLETE — Whitespace checks pass.
- [x] COMPLETE — Simulator passes (`arise demo`; FAKE/test-only execution, not host automation).
- [x] COMPLETE — Replay suites pass (pytest replay/failure tests pass; harness replay probes ran under host guard).
- [x] COMPLETE — Voice harness runs on Linux with the optional local extra; synthetic VAD and deterministic REPLAY/FAKE probes pass, while hardware/TaskEngine-composition probes report `ENVIRONMENT-LIMITED`/`BLOCKED` and Gemini is skipped (21 stages; no device/provider access).
- [x] COMPLETE — No real-only claims made from replay tests (evidence labels and limitations remain explicit).
- [!] BLOCKED — ENVIRONMENT — Current-commit GitHub Actions run 37178668976 passed Ubuntu/Windows backend matrices, frontend build, Windows Tauri `cargo check`, and Rust token/restart-helper tests; this does not launch the packaged app or validate WebView2, Windows ACLs, UIA/audio hardware, or the installer. The Linux sandbox has no local Rust/Cargo or interactive Windows runner.
- [x] COMPLETE — No fake success responses remain in production paths.
- [x] COMPLETE — No accidental debug code remains.
- [x] COMPLETE — No obsolete placeholders remain in production paths.

## Documentation checklist
- [x] COMPLETE — README reflects current task-history retention, backup, replay, and environment limits.
- [x] COMPLETE — Architecture documentation records the v9 replay floor, retention, backup, and authority boundaries.
- [x] COMPLETE — Roadmap updated with current replay/backup/retention state.
- [x] COMPLETE — Voice validation documentation records the 21-check harness scope and non-REAL limitations.
- [x] COMPLETE — Security documentation added at `docs/security.md` with current controls, open gaps, and validation commands.
- [x] COMPLETE — Memory documentation updated in README/architecture/security docs with consent and deletion behavior.
- [x] COMPLETE — Research documentation updated in README/architecture/security docs with trust and egress gates.
- [x] COMPLETE — Provider configuration documented in README, `.env.example`, and security docs.
- [x] COMPLETE — Windows setup documented (`README.md` sidecar packaging and Windows setup section; live Windows installer execution remains environment-blocked).
- [x] COMPLETE — Troubleshooting documented (`README.md` troubleshooting matrix for lock, cursor, memory, and stale-target recovery).
- [x] COMPLETE — Remaining environment limitations documented honestly; no Windows/provider runtime claim is inferred from FAKE/REPLAY checks.

## Mandatory forensic search
- [x] COMPLETE — Search TODO, FIXME, `pass`, `NotImplementedError`, stub, placeholder, fake, mock, unimplemented, and unsupported across the repository; source/docs/tests matches were classified in `docs/forensic-audit.md`.
- [x] COMPLETE — Implement every software-remediable production gap found by that search without removing legitimate tests; remaining environment/configuration gates are enumerated in this checklist and `docs/forensic-audit.md`.

## Required final report
- [x] COMPLETE — Re-opened and recounted the full checklist for this report after the latest implementation/tests: 461 total; 447 COMPLETE, 0 IN PROGRESS, 0 NOT STARTED, 9 BLOCKED — ENVIRONMENT, and 5 NEEDS EXTERNAL CONFIGURATION (including the two final-report rows).
- [x] COMPLETE — Final report records changes/defects, exact test totals, REAL vs FAKE/REPLAY evidence, remaining software work, blockers/setup needs, and actual Git status.
