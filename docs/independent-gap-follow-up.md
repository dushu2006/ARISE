# Independent-gap follow-up

Date: 2026-10-04  
Branch: `arena/01a10724-arise`  
Baseline: 477 passed + 115 subtests from the independent audit.

This follow-up addresses only the five requested independent workstreams. The earlier Windows UIA/app-launch/planner/security/isolation changes remain in the working tree. No Windows UIA or app-launch implementation was changed in this follow-up. Nothing was committed, pushed, merged, or closed.

## Final validation

| Check | Result |
|---|---|
| Focused new regressions + engine/server/scenario tests | **109 passed, 1 skipped, 4 subtests passed** |
| Complete suite, normal environment | **523 passed, 1 skipped, 115 subtests passed**, 15.08 s |
| Complete suite, synthetic polluted environment and dotenv cwd | **523 passed, 1 skipped, 115 subtests passed**, 15.68 s |
| Ruff lint across repository | Passed |
| Ruff formatting check of 11 changed subsystem/test files | Passed |
| Python compilation: source, tests, conftest, live-planner script | Passed |
| `git diff --check` | Passed |
| Frontend typecheck and production build | Passed |

One existing Starlette/httpx TestClient deprecation warning remains.

**The skip is explicit, not fake verification:** `tests/test_browser_headless.py` needs the optional Playwright package and its Chromium executable. The package was installed in the local virtualenv, but Chromium download failed with `ECONNRESET` before TLS establishment at `cdn.playwright.dev`. No system Chromium/Chrome was found. The test does not download software itself or access a public website. It is ready to exercise locally fulfilled fixture HTML when the executable is available; it was **not executed successfully here**.

Repository-wide `ruff format --check .` additionally reports pre-existing formatting drift in four files: `src/arise/adapters/windows_app_launch.py`, `src/arise/server.py`, `test_nvidia_gateway.py`, and `tests/test_windows_app_launch.py`. Unrelated formatting was deliberately not rewritten. The newly changed server journal payload is formatted; the remaining reported server regions belong to earlier work.

## 1. Utterance identity — REAL BUG fixes

Files: `core/voice_bridge.py`, `core/voice.py`, `adapters/gemini_live.py`, and the voice event journal in `server.py`.

- Removed normalized command text as the identity of a local spoken request. `process_utterance` and `process_spoken_utterance` accept an explicit `utterance_id`; a new ingress without one gets a new UUID. A retry must retain its original ID.
- Admission and informational responses are serialized; AudioHub also serializes the corresponding local speech path. Rapid requests cannot race their responses into a different interaction's speech context.
- Replay handling is scoped to principal + session + utterance ID. A conflicting reuse is rejected; a matching replay does not readmit the task, speak again, or emit acceptance again within the retained replay window. A replayed task response is refreshed from current task state, rather than returning an obsolete queued snapshot.
- In-memory replay entries retain a text digest rather than retaining the raw input again. Existing bounded retention is preserved.
- Explicit ingress IDs become task `request_id` values. The existing engine uses that request ID for task correlation/causation and durable admission idempotency. Responses, voice events, event journal payloads, local TTS correlation, and deferred runtime-update messages carry the identity.
- `LiveEvent` and `LiveToolCall` have additive optional utterance identity fields. Gemini normalization tags all normalized content in a turn consistently and rotates the identity at turn completion, including when the next input repeats the same words.
- AudioHub rejects settled/obsolete tagged turns, does not attach an older tagged tool call to newer input, ignores duplicate final text and non-increasing audio sequences, and does not let a duplicate turn completion drain another pending response.
- Runtime-generated responses retain the originating task/request identity even when another task is currently active.

**Deliberate semantics:** a newer identified live input supersedes unsubmitted input from an older live turn. Late tools from the superseded turn are rejected, not reassigned to the newer command. An admission receipt is still not execution verification. Already admitted tasks are not implicitly rolled back by a new utterance.

## 2. Settled partial lifecycle — REAL BUG fixes

Files: `core/engine.py`, `core/voice_bridge.py`, `core/voice.py`.

- Added an engine-owned `is_settled()` query. An intermediate `PARTIALLY_COMPLETED` snapshot remains in flight while its plan worker is active; a partial result with no running worker is settled. Other existing terminal statuses remain terminal.
- Child admission now uses the same settled/cancelling boundary: an active partial parent may admit an owned child; a settled partial parent may not.
- Voice watcher signatures include settlement. They can emit the final settled-partial update even when the visible status did not change, then terminate instead of polling indefinitely.
- Voice status results carry `settled` and repository `version`. Settled partial remains **not verified complete**.
- AudioHub rejects older revisions and duplicate/late updates after settlement. Such updates cannot reactivate a completed/cancelled/unknown/settled-partial task.
- Deferred runtime announcements are queued per task in insertion order, rather than one global slot that overwrote another interaction's result. Same-task pending progress can be replaced by its newer result. Overflow remains bounded and produces an explicit error event.
- The existing model stream final boundary is regression-tested: a producer is closed at its first final, and later partial/final output is not read. No new stream success semantics or policy bypass was introduced.

## 3. Descendant cancellation — REAL BUG fixes

Files: `core/tasks.py`, `adapters/sqlite.py`, `core/engine.py`.

- Added cursor-paginated, principal-scoped `list_children()` to both task repositories and the repository protocol. Cancellation no longer searches only the latest 5,000 history records.
- SQLite has an additive `IF NOT EXISTS` expression index over principal, parent ID, and task ID for child queries. Existing task records and schema payloads are unchanged.
- Tree traversal is iterative with a visited set. It follows only matching parent links, authenticated ownership, and the root session. Unrelated tasks, foreign-principal records, and cross-session links are excluded.
- Traversal passes through completed ancestors to reach still-live owned descendants without changing those completed ancestors' outcomes.
- Overlapping cancellation requests are serialized. The owned subtree is marked before awaiting cancellation, preventing new child admission while workers stop.
- Queue workers also check that cancellation barrier: a queued descendant cannot begin dispatch during another descendant's cancellation cleanup.
- All active owned workers are cancelled together. Finalization preserves completed/unknown/settled-partial evidence and drops confirmations without claiming rollback. Cleanup still finalizes queued descendants if the cancellation caller itself is cancelled.

Tests use both SQLite and in-memory repositories, multiple pages of siblings, nested descendants, completed children, foreign/unrelated/cross-session records, overlapping cancellation, child admission during cancellation, queued-worker dispatch races, caller cancellation, and completion/unknown-result races. They assert the old history query is never used.

## 4. Episodic-memory consent — REAL BUG fix

File: `adapters/sqlite.py`; updated scenario expectation in `tests/test_phase4_and_phase5_and_scenarios.py`.

The existing `MemoryEntry`/`MemoryPort` architecture requires an exact, one-time write grant before persistence or optional embedding. The old helper manufactured that grant internally; no production automatic-summary caller or alternate consent authority was found. A task's execution authorization is **not** authorization to save its summary in long-term memory or send it to an embedding service.

- `propose_episodic_task_summary()` creates the bounded proposal without storage or embedding. It rejects active or ambiguous partial statuses and disabled memory.
- `record_episodic_task_summary(..., consent_reference=...)` consumes an externally issued exact grant through the existing `store()` path. Missing consent fails closed; the helper no longer issues consent for itself.
- Default retention is anchored to the task timestamp so preparing and later submitting the same proposal does not accidentally change its consent scope. An explicit `now` remains available. Retention is bounded.
- Existing grant semantics are preserved: an exact valid grant can store once; missing, mismatched, expired, replayed, foreign-principal, and disabled writes cannot embed or persist.
- The existing scenario now explicitly prepares the proposal, grants consent, and supplies that grant. It no longer treats automatic grant issuance as evidence of user consent.

No consent is inferred from a task finishing, a verifier passing, or voice admission. The helper is still not wired into automatic completion.

## 5. Browser adapter — REAL BUG fixes and TEST GAP coverage

File: `adapters/browser_playwright.py`; Windows adapters unchanged.

- Startup cancellation now closes every allocated context/browser/SDK resource before propagating cancellation.
- Start/close lifecycle operations are serialized so concurrent starts cannot allocate multiple isolated contexts and concurrent closes remain idempotent.
- Verification no longer returns success for empty postconditions, unrelated target evidence, or expired/stale observation evidence; those cases return UNKNOWN.
- Navigation rechecks its resource lease after the initial awaited observation validation, before entering navigation dispatch.
- Deterministic adapter tests cover semantic DOM resolution, ambiguous/hidden/disabled targets, dispatch versus verified postconditions, failed and fresh verification, stale navigation observations, unknown timeout outcomes, cancellation propagation, resource release, and SDK lifecycle cleanup.
- An optional real headless DOM test covers fixture navigation, actual DOM snapshotting/targeting, secret-value exclusion, click dispatch, fresh title postcondition verification, and cleanup. It is environment-blocked here as described above.

Direct adapter tests establish adapter contracts, not user authorization. The existing runtime still owns PolicyEngine evaluation, risk floors, confirmations, and resource validation. In particular, browser clicking remains R3 and Windows `uia.click` confirmation requirements are untouched.

## Tests added/adjusted

- **46 new deterministic cases** in `tests/test_remaining_independent_gaps.py`.
- **1 optional real headless DOM test** in `tests/test_browser_headless.py`, currently skipped with its explicit environment reason.
- Updated two prior audit fixtures to model real active partial workers and same-session parent/child ownership, rather than ambiguous settled snapshots or cross-session children.
- Updated the existing episodic-summary scenario to obtain explicit exact consent.

Focused tests ran before the full suite. No test invokes live NVIDIA/Gemini/Brave services or audio hardware. Frontend validation establishes compilation only.

## Remaining issues and boundaries

### Independent

- **ARCHITECTURAL GAP:** bounded replay/status caches are not a durable, infinite exactly-once speech-delivery ledger. Task admission idempotency remains durable in the existing repository, but historical informational speech replay suppression across process restart/cache eviction is not claimed. Callers must preserve IDs on retries; inventing a new ID for a retry is a new request by contract.
- **ARCHITECTURAL GAP:** Gemini's SDK messages do not supply the same physical utterance identity as local ASR. The adapter assigns IDs at normalized turn boundaries. It cannot prove whether an unidentifiable server replay after reconnect is the same physical utterance. Cross-ASR/SDK acoustic alignment and transport replay semantics still need an explicit provider contract and integration evidence; text equality is not treated as an utterance ID.
- **TEST GAP:** React/Tauri browser-level UI, multi-provider stress/circuit generations, and answer-level source attribution from the original audit were outside these five workstreams. The new browser coverage is for the browser adapter, not React UI end-to-end testing.
- **ARCHITECTURAL GAP:** memory/history/profile deletion remain separate controls; no atomic erase-every-context operation was added.
- **ENVIRONMENT-BLOCKED:** the real headless Chromium test could not execute because the browser binary download failed. The mock adapter coverage does not substitute for that test.

These limitations are not concealed by the passing count. No known failing regression remains in the implemented independent fixes.

### Windows-dependent — ENVIRONMENT-BLOCKED

Actual application discovery/launch, UIA trees and patterns, Chrome address-bar identity, native focus/readback, R3 confirmation, and strict native execution postconditions still require the user's laptop. The previous Windows audit and recommended validation order remain applicable.

### Integration — ENVIRONMENT-BLOCKED

Physical microphone/speaker behavior, rapid acoustic utterances, local ASR/cloud alignment, SDK reconnect/resume replay semantics, real NVIDIA/Gemini/Brave/embedding endpoints, Windows Python 3.14, and packaged Tauri UI/sidecar behavior remain unverified. No hardware, live Chrome/Windows GUI, or end-to-end production verification is claimed.
