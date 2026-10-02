# ARISE roadmap

Every phase preserves the core contract: models propose; deterministic policy/executors decide; the environment grounds; a separate verifier establishes results. A capability is not advertised as available until its real adapter and tests exist.

## Phase 0 — Safe runtime foundation (implemented)

- Typed action/target/condition/authority contracts, policy/risk floors, action-scoped approval grants.
- Explicit task/step state machines including blocked, interrupted, user-input, unknown, partial, cancelled, and verified completion.
- Resource leases, observation leases, executor/verifier ports, event envelopes, in-memory fixtures and SQLite tasks/events.
- Deterministic tests for trust, policy, stale targets, replay refusal, timeout/cancellation, resource contention, and SQLite concurrency.

## Phase 1 — Local control plane and desktop shell (implemented in this checkout; Windows validation pending)

- **Python:** configuration boundary, FastAPI local API, HTTP authentication/origin/request limits, protocol-v1 WebSocket, health/diagnostics/capability discovery.
- **Task engine:** bounded asyncio workers, typed plan validation/topological ordering, task correlation, cancellation/timeouts/recovery, explicit clarification and scoped approval flow. The backend is authoritative for state.
- **Persistence:** SQLite migrations for tasks, sessions/turns, correlation/causation event journal, optimistic versions, common-credential redaction before conversation persistence.
- **Model boundary:** `ModelGateway`/router/provider contracts, privacy-aware provider selection, OpenAI-compatible adapter, secret-by-reference credentials. No provider calls by default.
- **Frontend/shell:** React/TypeScript/Vite control room and minimal Tauri 2 shell with a local backend launcher and token bridge.
- **No fake capabilities:** no real desktop/browser tools are registered. Windows UIA, browser DOM/CDP, vision/OCR, voice, semantic memory, and research are explicitly unavailable or deferred.

**Phase 1 validation still required:** the CI workflow is configured for backend tests on Linux/Windows, frontend builds, and a Windows Tauri compile check, but those remote jobs still need to run and pass. Package and launch on supported Windows/WebView2, and inspect Windows token/data-directory ACL behavior before any support claim. No claim of Windows automation or installer support before real Windows validation.

## Phase 2 — Deterministic Windows and browser adapters (in progress)

**Current status:** the shared contracts now include display DPI availability and descriptor visibility/enabled state. An optional isolated Playwright prototype reuses the Phase 1 runtime, with bounded DOM snapshots, page-scoped target identities and resource leases, URL redaction/validation, secret references, and pre-dispatch freshness checks. It is not registered by the API; tests use fakes and a real browser has not been exercised. Windows discovery/UIA and real platform integration remain outstanding.

- Implement Windows environment discovery and a `DesktopAutomationAdapter` using UI Automation/accessibility first, with process/window identity, display/DPI/focus tracking, target uniqueness, state invalidation, modal/interrupt detection, and human-interference checks.
- Complete the browser adapter's release gates: real Playwright/Chromium compatibility, account/profile scope, robust navigation egress controls, authenticated approval preview, failure reconciliation, production registration, and capability reporting.
- Renew/invalidate observation leases on UIA/DOM events; declare physical input/window/browser/profile/clipboard resources; keep physical input serialized.
- Keep OCR/vision/coordinates as explicitly lower-confidence fallbacks; never allow a model to click directly.

**Exit checks:** Windows CI/sandbox tests for stale/wrong window-tab-account, duplicate controls, monitor/DPI/focus changes, modal interruption, cancellation, expiry, and human interference. Test real supported Windows builds.

## Phase 3 — Tool catalog, filesystem, terminal, and recovery

- Add schemas, static risk floors, capability scopes, resources, side effects, idempotency, and independent verification descriptors to real tool registrations.
- Add safe filesystem/process/terminal tools with preview/dry-run, path/link validation, bounded output, secret redaction, timeouts, and exact destructive approvals.
- Add operation IDs, reconciliation for external side effects, checkpoints, safe recovery and external receipts.

**Exit checks:** fault injection for timeout-after-side-effect, duplicate operation, crash, locked files, symlink/junction escape, command output leak, and lease expiry.

## Phase 4 — Model quality, planning, and research

- Add local/cloud provider adapters per role and modality behind `ModelProvider`; benchmark latency, privacy, quality, and capability status.
- Improve event-driven planning with budgets, pre/postconditions, progress/no-progress detection, replanning checkpoints, and robust clarification.
- Add current research retrieval with provenance/freshness/ranking and untrusted-content boundaries. Research content never supplies authority.

**Exit checks:** deterministic provider replay, prompt/tool-output injection suite, schema fuzzing, outage/fallback, spend/latency budgets.

## Phase 5 — Voice and desktop experience

- Add streaming ASR/TTS, VAD/barge-in, cancellation propagation, privacy and text fallback; no voice claims before real audio hardware validation.
- Expand Tauri/React with accessible approvals, event replay, privacy/data controls, capability settings, diagnostics, and history management.

**Exit checks:** microphone/speaker tests on Windows, noise/technical vocabulary/interruption coverage, frontend reconnect/rehydration, shell security review.

## Phase 6 — Production hardening and release

- Build/supervise a signed Windows installer/sidecar, crash-loop protection, update/rollback, backup/export/delete, database/key protection, and controlled logs.
- Run multi-hour soak, resource leaks, load/fairness, real GPU/display/browser/audio validation, and independent security review.

**Release gate:** no claim of supported Windows automation, browser control, voice, provider, or installer behavior without matching Windows/CI/hardware/security evidence.
