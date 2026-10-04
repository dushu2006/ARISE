# Independent-subsystem audit

> **Follow-up completed:** [Independent-gap follow-up](independent-gap-follow-up.md) records the subsequent five-workstream changes and current validation: **523 passed, 1 skipped, 115 subtests passed**, including the polluted-environment run. The original 477-test audit below is retained as the historical baseline; its utterance, settled-partial, descendant-cancellation, episodic-consent, and browser-adapter findings are superseded by the follow-up's fixes and explicitly stated remaining boundaries.

Date: 2026-10-04  
Branch: `arena/01a10724-arise`  
Environment: Arena/Linux, Python 3.11.2; fake model/audio providers and local test subprocesses.

## Scope and evidence boundary

This audit retains the accumulated Windows production-path, planner-contract, and pytest-isolation work. It does not replace or restart Windows UIA implementation. No commit, push, merge, or branch closure was performed.

Classification:

- **A — INDEPENDENT:** contracts, state handling, policy, persistence, provider normalization, simulated execution, and control-plane behavior testable without Windows.
- **B — DEPENDENT:** behavior whose correctness requires the pending real Windows execution path.
- **C — INTEGRATION:** composition requiring the user's laptop, native devices/GUI, installed SDKs, credentials, or real services.

Findings are classified separately as **REAL BUG**, **TEST GAP**, **ARCHITECTURAL GAP**, **ENVIRONMENT-BLOCKED**, or **ALREADY CORRECT**. Passing a mocked test establishes only the tested contract. Provider dispatch, a readiness signal, task admission, and generated speech are not evidence that a desktop action was executed or verified.

No real Chrome, Windows UIA, microphone, speaker, Vosk/Kokoro inference, Gemini Live session, NVIDIA endpoint, Brave endpoint, or Tauri desktop window was exercised here. The Windows Python 3.14 environment remains untested. See also [the retained Windows execution audit](windows-production-execution-audit.md).

## Automated results on the final code

| Check | Result |
|---|---|
| `.venv/bin/pytest -q` | **477 passed; 115 subtests passed**, 15.30 seconds |
| Full suite from a temporary cwd containing synthetic cloud-enabled `.env` values, with synthetic inherited NVIDIA/Gemini environment values | **477 passed; 115 subtests passed**, 14.77 seconds |
| Focused audit + process lifecycle + phase/scenario tests | **53 passed**, 4.47 seconds |
| `.venv/bin/ruff check .` | Passed |
| Python compilation of source/tests/isolation/live-planner script | Passed |
| `git diff --check` | Passed |
| Frontend `npm run typecheck` | Passed |
| Frontend `npm run build` | Passed; Vite production build |

The suite reports one existing Starlette/httpx TestClient deprecation warning. Frontend compilation is not a browser interaction or native-shell test.

An earlier contaminated-environment run had **1 failure / 473 passes**: the real local backend restart test timed out. An isolated rerun and a full rerun passed. Inspection identified a readiness/HTTP-binding race, which was fixed and given deterministic coverage before both final 477-test runs. The original transient failure is not hidden by the successful reruns.

Normal collection uses `tests/`. The root `test_nvidia_gateway.py` invokes a live check on import and was intentionally not collected. No real credentials were used for contamination testing, and production `.env`, settings defaults, and Gemini cloud/security validators were not changed. No pre-fix red/green evidence is claimed for the regression suite as a whole.

## Fixed independent findings

All entries below are **A / REAL BUG**, fixed with focused regression coverage in `tests/test_independent_subsystems.py`.

| ID | Finding and correction |
|---|---|
| A01 | **Stream corruption and false success.** The router now checks request/provider/model identity and contiguous sequence numbers, requires a final chunk, and closes the producer on completion, failure, or cancellation. Fallback is allowed before output, never after output has been emitted. A truncated stream is not reported as successful. Explicit model pinning is respected. |
| A02 | **Complete-response identity.** A completion from a different provider or model is rejected rather than accepted as the selected provider's answer. |
| A03 | **Admission, timeouts, and circuit cancellation.** Semaphore admission is inside the request timeout for complete and stream. Streaming has bounded queue admission. Queue timeouts do not count as provider-health failures. Cancellation releases a half-open probe neutrally; cancellation of an older CLOSED-state request cannot release another request's current probe. Queue counters are restored. These are per-candidate timeouts, not a new global fallback deadline. |
| A04 | **SSE termination and malformed packets.** OpenAI-compatible streams no longer produce a second final for `finish_reason` followed by `[DONE]`; premature EOF fails. Invalid event/choice/delta/content/finish-reason shapes are sanitized typed failures, not accidental success or raw payload errors. |
| A05 | **Working-memory principal collision.** A foreign principal cannot inherit or replace an existing task's observations and scratchpad through `upsert`. Read/clear scoping is preserved. |
| A06 | **Cancellation between steps.** Parent cancellation includes a still-active child whose intermediate state is `PARTIALLY_COMPLETED`. Conversational cancellation similarly includes active intermediate-partial tasks. A small public `TaskEngine.is_active()` query avoids inferring execution liveness solely from the snapshot status. Ownership checks remain in force. |
| A07 | **Premature voice monitoring termination.** Voice watchers and AudioHub active-task tracking no longer treat every partial snapshot as terminal, so a later verified completion/failure is not silently missed. Settled-partial ambiguity remains an architectural issue described below. |
| A08 | **Gemini packet ordering and duplicate TTS.** A packet's turn-complete event is emitted after its other content, preserving turn guards through audio/tool processing. An additive `LiveEvent.is_audio_transcript` flag marks provider-audio transcriptions; AudioHub does not synthesize them as a second utterance. Transcript finality is preserved. This also covers an output transcription arriving before audio in a separate packet. It is not a universal utterance-deduplication protocol. |
| A09 | **Premature acceptance claims.** Pre-admission local acknowledgement now says: “ARISE heard your action request; admission is not yet confirmed.” Only after successful submission does the bridge say the task was accepted for planning, explicitly without execution verification. |
| A10 | **Memory PATCH bypass.** The existing update endpoint now requires exact one-time consent and honors per-principal disablement before optional embedding. Consent fingerprints now include confidence, sensitivity, and expiration policy as well as content/ownership/source/retention. Storage and update recheck enablement after embedding. Deletion during an awaited update is not returned as a successful write. Naive update expiries return validation errors instead of a server exception. |
| A11 | **Exception-content leakage in diagnostics.** Two conversational persistence warnings no longer interpolate arbitrary exception messages. They retain safe context and exception class, not potentially secret-bearing backend details. |
| A12 | **Supervisor handshake race and cancellation cleanup.** A bounded health retry tolerates the lifespan-ready signal arriving before the HTTP listener binds. The whole handshake has a timeout. An unpublished child process is killed/reaped when the handshake fails, times out, or is cancelled. This addresses local process lifecycle only, not Windows packaging verification. |

### Memory API compatibility note

`PATCH /api/v1/memory/{record_id}` accepts `consent_reference`. Omitting it now fails closed with HTTP 403. Obtain a fresh grant through the existing `/api/v1/memory/consents` endpoint for the **complete intended resulting content and metadata**, then supply that reference with the patch. Replayed, mismatched, or disabled writes fail before embedding. This is deliberate security hardening, not an automatic approval or migration of existing grants.

Adding metadata to the fingerprint invalidates previously issued short-lived grants; obtain a fresh grant. Existing stored records are not migrated or deleted. The frontend's replacement workflow already obtains consent and creates the replacement before deleting the original; it does not call this PATCH endpoint and required no change.

## Eight-domain review

### 1. Voice

**A — INDEPENDENT**

- **ALREADY CORRECT:** AudioHub has explicit lifecycle states; microphone capture is opt-in and preflight-gated. Cloud Live composition requires its separate cloud/security configuration. Status/config routes do not start capture implicitly.
- **ALREADY CORRECT:** local ASR admission requires confidence-qualified final text. Overflow/failure clears trusted transcript state. Local capture/ASR/TTS/playback adapters have bounded queues or admission and cancellation/close paths. Tests exercise fake device failures, resampling, wake handoff, inference failure sanitization, barge-in, playback stop, and cleanup.
- **REAL BUG, fixed:** A07–A09 cover partial-task monitoring, Gemini packet ordering/transcript audio provenance, and truthful acknowledgement.
- **REAL BUG / ARCHITECTURAL GAP, remaining:** local execute-call identity is derived from session plus normalized command text. An intentional identical command in the same session can be conflated with a retry. Repair needs an explicit utterance identity retained across retries, not naive randomization that weakens idempotency.
- **ARCHITECTURAL GAP:** a settled partial outcome and an in-flight partial snapshot share a status. Watchers now favor not missing later progress, but a settled partial may continue polling until lifecycle cleanup. Child admission also still rejects a partial-status parent, even when its worker is active; that remaining inconsistency needs the same coherent settled/running contract.

**C — INTEGRATION / ENVIRONMENT-BLOCKED:** actual wake/dormant transitions, acoustic echo, barge-in latency, utterance loss, native-vs-local acknowledgement overlap, microphone permission changes, device removal/recovery, SDK turn boundaries, session resume, and speaker cleanup. The current Gemini receive iterator's exhaustion triggers reconnect; whether the installed SDK supplies a per-turn or connection-lifetime iterator must be established before changing that policy. Tagged transcripts prevent one known duplication path, not every cross-channel or reconnect replay.

Evidence: `core/voice.py`, `core/voice_bridge.py`, `adapters/gemini_live.py`, local audio adapters; `test_voice.py`, `test_voice_bridge.py`, `test_gemini_live.py`, `test_audio_local.py`, `test_windows_voice_validation.py`, new audit tests. The last file uses a fake harness and is not Windows evidence.

### 2. Task engine and runtime

**A — INDEPENDENT**

- **ALREADY CORRECT:** admission and execution are distinct; durable task/event state, bounded scheduling, authorization, request fingerprints, restart recovery, confirmations, resource leases, and verifier-derived outcomes are exercised by engine/runtime/SQLite/scenario tests. Unknown/interrupted results are not promoted to completed merely because a tool returned.
- **REAL BUG, fixed:** A06 reaches active partial children and session-scoped text cancellation; A12 addresses local supervisor readiness/cleanup.
- **ARCHITECTURAL GAP:** parent cancellation scans at most 5,000 principal records. Very large histories can hide an older live descendant. A paginated child lookup or explicit descendant index is preferable to removing the bound or doing an unbounded recursive scan.
- **TEST GAP:** more adversarial multi-step cancellation, child admission/cancellation concurrency, repeated restarts under load, and exactly-once event/side-effect recovery should be tested. Existing idempotency protects admission; it is not universal exactly-once execution of external applications.

**B — DEPENDENT / ENVIRONMENT-BLOCKED:** interruption after a native side effect but before observation, strict postcondition evidence, focus leases spanning native operations, and recovery from a partly executed Windows plan.

**C — INTEGRATION / ENVIRONMENT-BLOCKED:** production sidecar launch/shutdown, user closing the application mid-task, OS sleep/resume, and real background-task UI continuity.

Evidence: `core/engine.py`, `core/runtime.py`, `core/tasks.py`, `core/resources.py`, `supervisor.py`, SQLite repositories; engine/runtime/resources/recovery/process-lifecycle and scenario tests.

### 3. Model gateway and planner

**A — INDEPENDENT**

- **REAL BUG, fixed:** A01–A04 cover stream identity/termination/fallback, cancellation, circuit probes, queue admission, deadlines, and SSE parsing.
- **ALREADY CORRECT:** retained planner schema/domain parity and sanitized bounded planner diagnostics remain in place. Structured plans still pass capability, resource, policy, semantic-target, and postcondition validation. Saved memory/research is context, not tool authority. Cloud model use remains explicitly gated.
- **ARCHITECTURAL GAP:** stream routing has less event telemetry than complete routing; the informational server route currently requests `stream=False`. This audit does **not** claim frontend token streaming.
- **TEST GAP:** fallback-chain wall-clock budgets, multi-provider aggregate load, and breaker transitions across overlapping generations need dedicated stress coverage. Current request timeout applies per candidate, and concurrency semaphores are per provider.

**B — DEPENDENT / ENVIRONMENT-BLOCKED:** resolving an actual Windows observation into executable semantic targets, native window identity, and verifier-backed desktop postconditions.

**C — INTEGRATION / ENVIRONMENT-BLOCKED:** the real NVIDIA model's schema adherence, supported response format, model identity, latency, throttling, actual SSE behavior, and sanitized authentication/network errors. Mocked OpenAI-compatible responses are not proof of NVIDIA service behavior.

Evidence: `core/model_gateway.py`, `core/retry.py`, planner/contract modules, `adapters/openai_compatible.py`; gateway/planner/contract/production-path and new audit tests.

### 4. Memory and personalization

**A — INDEPENDENT**

- **ALREADY CORRECT:** short-term context is principal/session scoped; working-memory reads and clears are scoped; long-term retrieval checks enablement, expiry, and ownership. Public writes consume one-time consent before embedding. Principal-wide long-term deletion removes records and consent grants. Personalization remains untrusted context and does not grant execution authority; cloud egress has separate gates.
- **REAL BUG, fixed:** A05 prevents working-memory cross-principal replacement; A10 closes update consent/disablement/metadata and awaited-mutation gaps.
- **ARCHITECTURAL GAP:** `record_episodic_task_summary()` still mints its own consent internally. No production caller was found for that helper. Do not wire it into automatic task completion as if it represented explicit user consent; it needs an explicit opt-in/authorization contract first.
- **ARCHITECTURAL GAP:** saved-memory deletion, conversation-history deletion, ephemeral short/working memory, and personalization reset are separate controls/stores, not one atomic “erase everything” operation. Do not describe long-term memory deletion as clearing every context source.
- **TEST GAP:** concurrent disable/delete/clear across every embedding and context-retrieval path, durable migration compatibility, and large-memory retention/load. The new tests cover specific write/update races, not all interleavings.

**C — INTEGRATION / ENVIRONMENT-BLOCKED:** real embedding endpoint egress, OS secret-store behavior, disk/backup deletion expectations, and user-visible personalization across native restarts.

Evidence: `core/personalization.py`, `core/extensions.py`, SQLite memory repository, server memory routes and frontend replacement workflow; memory/consent/planner-context/personalization/server and audit tests.

### 5. Research and web retrieval

**A — INDEPENDENT**

- **ALREADY CORRECT:** the conversational path requires one-time request opt-in; global research/network opt-ins and credential availability remain separate gates. Research-only questions do not themselves admit execution tasks.
- **ALREADY CORRECT:** retrieved sources are bounded untrusted context with provenance/citations. Brave adapter checks HTTPS/default port, public DNS answers, redirects, and domain constraints; tests cover mixed private/public DNS rejection, source extraction, ignored script/style/navigation content, and sanitized failures.
- **ALREADY CORRECT:** unavailable research does not become a fabricated successful search. Citation/source metadata is carried separately from tool authority.
- **TEST GAP:** provenance is not a proof that every generated claim is entailed by its cited source. Contradictory/live-changing sources and citation fidelity need more answer-level adversarial coverage.

**C — INTEGRATION / ENVIRONMENT-BLOCKED:** real Brave responses, public-page fetches, DNS/redirect/network behavior, rate limits, and latency with the user's permitted network configuration. Browser/UIA interaction with a research result additionally belongs to B and remains unverified.

Evidence: `adapters/brave_research.py`, planner-context and server research paths; `test_brave_research.py`, `test_planner_context.py`, server and scenario tests.

### 6. Security and configuration

**A — INDEPENDENT**

- **ALREADY CORRECT:** bearer comparisons are constant-time; control routes/WebSocket admission require configured authentication; task/session/principal ownership is enforced. Production configuration cannot disable API authentication or enable development environment-secret shortcuts.
- **ALREADY CORRECT:** risk classification, explicit high-risk confirmation, capability/resource validation, PolicyEngine enforcement, and untrusted-context boundaries were not weakened. No automatic approvals, arbitrary-shell path, or fabricated focus/window evidence was introduced.
- **ALREADY CORRECT:** centralized pytest isolation clears inherited ARISE/provider configuration before collection, resets cached settings, disables ambient dotenv loading in tests, and uses a clean per-test cwd for subprocesses. Intentional test overrides remain possible; production loaders/validators are unchanged.
- **REAL BUG, fixed:** A05/A10/A11 cover principal isolation, update consent, and two raw-exception diagnostic leaks.
- **TEST GAP:** these changes are not an exhaustive penetration test or a proof that every diagnostic path is secret-free. Installed dependency/native credential-store behavior and multi-client adversarial fuzzing remain outside this automated result.

**C — INTEGRATION / ENVIRONMENT-BLOCKED:** Windows keyring/credential lookup, native permissions, firewall/network policy, and packaged application's authentication/reconnection experience.

Evidence: settings, secret adapters, policy/authorization, redaction, server auth/WS paths, root `conftest.py`; security/settings/isolation/server/memory and policy tests.

### 7. Desktop architecture, excluding a new UIA implementation cycle

**A — INDEPENDENT**

- **ALREADY CORRECT:** capability/provider interfaces, observation contracts, strict fact verification, policy evaluation, resource leases, and simulator/native composition are independently testable. Existing production-path tests retain semantic target requirements and do not equate generic HWND success with Chrome omnibox focus.
- **ALREADY CORRECT:** lease tests cover exclusive acquisition, timeout cleanup, cancellation, and sorted atomic resource sets. The simulator remains evidence for contracts only.

**B — DEPENDENT / ENVIRONMENT-BLOCKED:** native application discovery and launch freshness, real process/window identity, UIA control trees/patterns, foreground/focus, browser address-bar identification, text entry/readback, and native postconditions. Prior fixes and R3 `uia.click` confirmation requirements are retained, not reimplemented here.

**C — INTEGRATION / ENVIRONMENT-BLOCKED:** actual installed Chrome/version/profile, desktop elevation boundaries, Windows permissions, window switching, and multi-monitor/DPI behavior.

Evidence: retained Windows audit, `test_production_desktop_path.py`, capabilities/contracts/runtime/resources tests. No new native execution claim follows from their success on Linux.

### 8. Frontend and control plane

**A — INDEPENDENT**

- **ALREADY CORRECT:** frontend source uses task snapshots for authoritative task state, handles event cursors/replay resets/deduplication, and refreshes task state rather than treating arbitrary events as verified success. Backend EventBroker queues are bounded and durable events precede publication.
- **ALREADY CORRECT:** API/WebSocket tests cover authentication, hello/versioning, replay, task acknowledgements, errors, and ownership. Voice status and explicit-start preflight are tested without opening hardware.
- **REAL BUG, fixed:** conversational cancellation now recognizes active intermediate-partial tasks; accepted/verified voice wording is separated; persistence logs are sanitized.
- **TEST GAP:** React browser-level interactions, DOM rendering of task/voice errors, duplicate WebSocket delivery, reconnect timing, button cancellation, and browser storage behavior have not been end-to-end exercised. TypeScript/Vite success establishes compilation, not GUI correctness.

**B — DEPENDENT / ENVIRONMENT-BLOCKED:** displaying actual native task evidence without overstating completion.

**C — INTEGRATION / ENVIRONMENT-BLOCKED:** Tauri sidecar binding, native window close/restart, live authenticated WebSocket reconnect, voice permission/status controls, and real task progress/cancellation visible in the user's desktop UI.

Evidence: `frontend/src/App.tsx`, `frontend/src/api.ts`, server routes/EventBroker, server/WebSocket/process lifecycle tests and new audit regressions.

## Test changes and files

`tests/test_independent_subsystems.py` adds **40 parametrized cases** covering:

- partial stream failure vs safe pre-output fallback, malformed/mismatched/out-of-order/truncated chunks, pinning, close/cancel, queue backpressure/timeouts, complete identity, and half-open cancellation ownership;
- duplicate/missing SSE termination and malformed JSON event shapes;
- principal-scoped working-memory updates;
- active partial child cancellation, conversational cancellation, voice monitor continuity;
- Gemini combined-packet ordering and native-audio transcription suppression, including transcription-before-audio;
- truthful pre-admission acknowledgement;
- memory PATCH one-time consent/disablement/replay/expiry validation, consent metadata, mutation during embedding, and safe persistence warnings;
- delayed HTTP readiness and supervisor timeout/cancellation cleanup.

Existing expectations changed in `tests/test_voice.py` for truthful acknowledgement and `tests/test_phase4_and_phase5_and_scenarios.py` to supply explicit consent before updating memory. Existing settings-isolation and production-desktop-path test files are retained.

Audit source changes are concentrated in gateway/retry, OpenAI/Gemini adapters, voice/voice bridge, working memory, memory consent/SQLite, task activity/cancellation, server control routes, and supervisor lifecycle. The earlier Windows/planner/runtime changes remain in the same working tree. Generated frontend artifacts, installed dependencies, and ephemeral test logs are not deliverables to merge.

## Remaining independent work, prioritized

1. **REAL BUG / ARCHITECTURAL GAP:** introduce stable utterance IDs that distinguish intentional repeated commands from retry delivery without weakening task idempotency.
2. **ARCHITECTURAL GAP:** distinguish settled-partial outcomes from an active multi-step task consistently across watcher termination, child admission, status UI, and cancellation.
3. **ARCHITECTURAL GAP:** replace the parent cancellation history horizon with a bounded/paginated descendant query; test large histories and cancellation/admission races.
4. **ARCHITECTURAL GAP:** define explicit consent for the currently unused automatic episodic-summary helper before enabling it; clarify separate memory/history/profile erasure controls.
5. **TEST GAP / ARCHITECTURAL GAP:** browser-level frontend/replay tests, stream telemetry and optional UI streaming, multi-provider load/deadline/circuit-generation stress, and answer-level source attribution tests.

These are not claimed fixed or verified. The first four require a coherent lifecycle/identity/consent boundary rather than silently changing idempotency, inventing approvals, or broad architectural replacement during this audit.

## Recommended real-Windows validation order

Run on the user's laptop; do not replace evidence with simulator results.

1. **Environment and isolation:** run the complete Python suite using the actual Windows interpreter with the existing local `.env` present. Confirm isolation, retain the summary, and do not print secrets. Check frontend typecheck/build and installed native/SDK prerequisites separately.
2. **Security and control-plane preflight:** start the real app without capture/action execution. Check authenticated API/WS access, truthful voice/config status, cloud opt-ins, secret-store availability, and denial paths. Confirm no microphone opens before explicit start.
3. **Read-only native observation and launch:** inspect actual installed Chrome and process/window identity. Validate launch/cold-vs-existing-window behavior, foreground and UIA observation evidence. Missing/ambiguous targets must stop honestly.
4. **Live NVIDIA planning, separately from execution:** use the retained concise diagnostic script. Establish actual endpoint/model/schema behavior and safe failure output. The existing command is `python scripts/live_nvidia_planner_check.py --diagnostics --request "Open Chrome, click the address bar, and type example.com"`. A valid plan is not desktop verification.
5. **Controlled desktop execution:** with explicit required confirmations, validate R3 `uia.click`, observed address-bar target, foreground/focus, type/readback, and strict postconditions. Reject invented omnibox HWNDs or internal dispatch success as evidence. Record only bounded sanitized diagnostics.
6. **Native lifecycle:** cancel before dispatch, while waiting for confirmation/resources, and between steps; exercise parent/child behavior, background progress, app shutdown/crash/restart, and failure after a native side effect. Inspect truthful partial/unknown/interrupted outcomes.
7. **Local audio first:** microphone opt-in/start/stop, wake/dormant, confidence-qualified final ASR, local TTS, acoustic barge-in, device unplug/replug, playback abort, and repeated-command behavior. Check for both skipped and duplicated utterances; retain the known utterance-identity limitation.
8. **Gemini Live next:** with explicit cloud consent, validate the installed SDK receive contract, text/audio packet ordering, no duplicate local synthesis of native transcriptions, reconnect/resume/timeouts, interruption, guarded task claims, and complete device/client cleanup.
9. **Memory/research and final UI integration:** test one-time write/update consent, disabled memory, deletion/profile boundaries, permitted embedding/research egress, citations and provider failures. Then validate the combined voice → admission → confirmation → native verification → task events → UI/voice outcome path, including authenticated WS reconnect and shutdown.

Mark an item REAL/VERIFIED only after actual laptop evidence for that item. Nothing in this report grants a blanket Windows, hardware, cloud-provider, or end-to-end verification status.
