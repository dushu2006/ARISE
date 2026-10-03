# Phase 1 forensic engineering audit

**Scope:** the complete checked-out workspace at the time of the Phase 1 baseline, including the then-new/untracked source tree. This is a historical Phase 1 audit, not the current capability report. Phase 2 work is now tracked in `docs/architecture.md`, `docs/roadmap.md`, and ADR-0002; this document preserves the earlier baseline.

**Current-state correction (2026-10-03):** Baseline statements below such as “Phase 2–6 work was not started” and “memory, voice, and research remain unimplemented” describe the original audit snapshot only and are superseded. Subsequent code adds consented local SQLite memory, gated Brave research, a dormant-first optional voice runtime/TaskEngine bridge, bounded history/backup, diagnostics, and an optional Playwright prototype. The current `docs/master-completion-checklist.md` is authoritative for exact status: real Windows UIA/browser/audio/provider execution and several memory/recovery/packaging paths remain incomplete or environment/configuration limited.

## A. Executive verdict and evidence labels

ARISE has a tested Phase 1 local control plane and a deliberately small desktop shell boundary. It is **not** a working Windows/browser automation agent: production registers no host-control tools, and the simulator is isolated to the demo/tests. The control plane fails closed when planning or action capabilities are absent.

This report uses these distinctions:

- **Implemented:** code exists in this checkout.
- **Tested:** automated tests or static checks exercise it.
- **Runtime-verified:** the behavior was actually executed in this sandbox; this does not imply Windows or hardware validation.
- **Architecturally prepared:** typed ports/contracts exist, but no usable adapter/feature is registered.
- **Not implemented:** there is no implementation in this checkout.
- **Environment-limited:** the required OS, toolchain, provider, browser, or hardware was unavailable here.

## B. Architecture and import boundaries

The composition is React/TypeScript → local FastAPI/HTTP/WebSocket → task engine/runtime → ports → adapters. Domain/runtime modules do not import FastAPI, SQLite, Tauri, Windows automation libraries, browser frameworks, HTTP clients, or model-vendor SDKs. SQLite, HTTP, provider, diagnostics, simulator, and shell concerns remain at adapter/composition boundaries. A Python AST regression test now enforces the core import boundary.

Typed contracts and explicit task/action state machines are present. API response schemas carry `schema_version`; WebSocket frames carry protocol version 1. A backend-to-TypeScript contract test checks response property sets, nested session turns/diagnostics providers, and status/availability unions.

**Gate:** pass for the Phase 1 control-plane architecture. File-backed database ownership is restricted to one current backend process; Windows lock behavior remains a separate validation gate.

## C. Task lifecycle, races, and action safety

The engine uses bounded workers/admission, task/request/session/principal correlation, plan validation and topological ordering, cancellation, deadlines, clarification, and one-action scoped approvals. New idempotency mappings persist a normalized/redacted request fingerprint; conflicting reuse is rejected. Legacy migrated mappings have no fingerprint and cannot be retroactively checked.

A failure-injection regression covers the durable-admission edge: if the task row is committed but the first `TASK_ACCEPTED` event append fails, an idempotent retry reschedules the queued record and writes one accepted event. SQLite cross-connection concurrency tests verify one task per request key. The browser now retains the same request ID for a retry of the same text/session in the current page lifetime, so a lost HTTP response does not immediately create a second task.

Policy/execution checks include trusted tool risk floors, task-owned authority, capabilities, parameter validation, resources, fresh target/observation leases, preconditions, approval expiry/one-use semantics, timeouts, and independent verification. Unknown or interrupted post-dispatch outcomes are non-successes and are not blindly retried. A new regression makes a verifier's evidence-free `PASSED` result fail closed as `UNKNOWN`; another injects resource-lease expiry after dispatch and checks that completion is not claimed.

Approvals and clarification context are in memory only. They are not restored after restart; they return to user-input/replan rather than being silently replayed.

**Gate:** pass for simulated/test execution and Phase 1 task control. **Not a live-action release gate:** Windows validation of single-instance locking and durable, inspectable verification evidence remain blockers before registering real tools (E/L).

## D. Event journal, fanout, and WebSocket recovery

Events are appended durably before broker publication. Subscriber queues are bounded; a slow subscriber is detached and must reconnect/replay. Tests inject append failure/overflow and verify durable replay from cursor zero. WebSocket replay pages the journal and fills sequence gaps before forwarding live events.

The frontend's future cursor handling had a recovery hole: it reset its local cursor when the server sequence was lower but did not ask the server to replay from zero. It now issues `events.subscribe(after_sequence=0)` on that condition; a WebSocket test verifies the reset/replay path. Duplicate replay/live events are filtered by sequence/event identity on the client.

There is no journal retention or snapshot/compaction strategy. Replaying from sequence zero and loading complete session transcripts scale with accumulated history; the on-disk event/session footprint is unbounded until local state is removed.

**Gate:** pass for single-process journal/fanout/replay correctness under tests; conditional for long-running use until retention and replay/snapshot policy is designed.

## E. Process lifecycle, SQLite, and recovery

SQLite uses WAL for file-backed databases, bounded busy timeouts, serialized shared-connection access, short transactions, optimistic task versions, and atomic schema migrations through v5. Recovery tests distinguish safe queued replanning from interrupted/non-runnable work; approvals/clarifications are not restored as authority. A process test starts the supervised Linux backend, waits for its readiness marker, closes the parent pipe, and verifies clean shutdown.

Server lifespan cleanup is nested so router/provider and database cleanup are still attempted if engine shutdown raises; lifecycle failure injection covers this. The database owns its optional instance lock and releases it only after the SQLite connection closes successfully. Action dispatch coordination, resource leases, confirmations, and worker limits are **process-local**, but the database-adjacent OS lock permits only one current backend process per file-backed database. This is not a durable task claim. The Tauri launcher separately refuses an already-occupied default API port.

The API backend opts into a non-blocking OS lock adjacent to file-backed SQLite, acquired before opening the connection or running schema migrations and released after shutdown cleanup. Tests verify that a second process/backend is refused, lock contention happens before a second SQLite connection is opened, failed database initialization releases ownership, and shutdown failure still closes/releases resources. This prevents two current ARISE backends (including different API ports) from dispatching against one database; it is not a durable task claim and Windows/network-filesystem lock semantics remain unvalidated. There is no automatic retention, backup/restore, export, or deletion workflow. Local SQLite data is not encrypted by ARISE; it relies on OS user-directory protections.

**Gate:** single-instance ownership is implemented and Linux-tested; conditional on Windows validation and storage lifecycle controls before long-running/live use.

## F. Configuration, authentication, and secrets

Configuration validates loopback-only API binding, API token length (32–512 visible ASCII characters), bounded request sizes/timeouts, cloud opt-ins, and provider URL restrictions. Configured/provider URLs reject userinfo, query, and fragment data; cloud endpoints require HTTPS. API request bodies are checked against declared and actual length, buffered only within the configured cap, and bounded by a body-read deadline. Malformed lengths, false/small lengths, unread oversize bodies, and body timeouts have tests.

Generated API tokens are held outside SQLite/logs. Existing token files are checked against the same 32–512 visible-ASCII contract used by config/protocol, and POSIX permissions are reset to `0600`. Provider credentials are references resolved through the optional OS keyring; environment secret lookup is disabled unless explicitly enabled for development. Common credential-shaped text is redacted before conversation persistence, but this is explicitly best-effort, not a secret scanner. Cloud routing requires opt-in in both model and security settings.

The Tauri token bridge now follows process-environment `ARISE__DATA_DIR` and `ARISE__API__AUTH_TOKEN`, and the Rust token length check matches the Python/protocol bound. **The Rust shell does not parse Python `.env` files**: `.env`-only token/data-directory overrides can make the backend and shell disagree. For desktop overrides, pass those settings in the process environment or use the generated default token file. This is documented; Windows behavior still requires validation.

Browser `VITE_API_TOKEN` is read only in Vite development and never preferred inside Tauri. A production build with a fake canary token was inspected; the canary was absent from assets. CI now repeats this regression check.

**Gate:** tested configuration/API boundaries; conditional for packaged desktop use until Windows ACL/token/data-path behavior and the documented `.env` limitation are verified or closed. No independent security review has occurred.

## G. Capability discovery and model gateway

Capabilities report implemented orchestration/storage/model status truthfully and mark UI Automation, browser DOM/CDP, vision/OCR, voice, semantic memory, and research unavailable/deferred. Host discovery is limited to basic OS/release/architecture/CPU/memory facts; it does not scrape windows, processes, installed apps, or browser profiles.

The model router has provider-neutral contracts, privacy/capability selection, shared bounded concurrency, circuit status, request timeouts, and sanitized failures. The OpenAI-compatible HTTP adapter is non-streaming, limits response reads to 2 MiB, does not follow redirects by default, disables ambient proxy lookup, and uses secret-by-reference credentials. A concurrency test verifies its shared limit; HTTP adapter tests use mock transport. No provider was called live in this audit, and no provider is configured by default. Even a healthy planner cannot execute side effects because production has no registered action tools.

**Gate:** mock/unit-tested; environment-limited for a live local/cloud provider. No model-provider quality, privacy, latency, or availability claim is made.

## H. Security, consent, and authorization

HTTP uses bearer authentication with constant-time comparison; WebSocket requires a versioned hello and token; origins are restricted, with only a narrow development preview exception. API errors avoid arbitrary exception text, and database/internal/policy/retryable failures map to server/forbidden/unavailable classes rather than being mislabeled as ordinary bad requests. The unauthenticated `/healthz` intentionally exposes only a local health snapshot. The local token maps to one principal; multi-user isolation is not implemented.

Models/external content are proposals/data, not authorization. Exact action/principal/fingerprint/expiry-scoped approvals are consumed once. Memory write consent requires a valid, unexpired, principal-scoped, unused reference; tests cover missing, invalid, expired, wrong-owner, expired proposal, replay, and concurrent consumption. This is only a consent guard/port contract—there is no semantic memory store or product memory feature.

The shell exposes a narrow token command and no arbitrary shell bridge. The production UI CSP has no inline script/eval allowance; inline styles remain allowed for the UI. A complete threat model, Windows ACL review, and independent security review are outstanding.

**Gate:** tested for current single-user local boundaries; conditional for release pending Windows and security review. Memory, voice, and research remain unimplemented.

## I. Frontend/backend contracts and async/error handling

TypeScript field and enum contracts are checked against backend models, and strict typecheck/build pass. Submission retries reuse an idempotency key within the page lifetime; the future-cursor WebSocket recovery path is covered on the backend protocol. `App.tsx`'s submit busy state was reviewed: it has one `setBusy(true)` and a `finally` reset. Production bundle token exclusion has a canary check.

There are no browser end-to-end tests, accessibility audit, or Tauri WebView runtime tests. TypeScript types are compile-time declarations; the frontend does not runtime-validate every JSON frame. Session responses include all turns, so large histories may stress the UI/API.

**Gate:** type/contract-tested and production-built; conditional until browser reconnect, approvals, errors, and native token flow are exercised end-to-end.

## J. Ports and Phase 2–6 extensibility

Typed future-facing ports exist for environment/actions/verifiers, model providers, speech recognition/synthesis, consent-gated memory, and provenance-tagged research. These are **architectural preparation only**. There is no real Windows UI Automation adapter, browser adapter, OCR/vision, voice/audio, semantic memory, autonomous research, filesystem/terminal integration, or external integration. The in-memory simulator is test/demo-only, not a Windows execution path.

Phase 2–6 work was not started. Preserve official API → browser DOM/CDP → Windows accessibility/UIA → OCR/layout → vision → coordinates as the preferred grounding order; model proposals remain untrusted and do not dispatch directly.

**Gate:** ports only for deferred subsystems; no capability/release claim until a real adapter, platform tests, policy/resource scope, recovery design, and independent verification exist.

## K. Tests, CI, performance, and documentation

Local checks after the final code changes:

- `python -m pytest -q`: **98 passed, 28 subtests on each of three consecutive database-lock-hardened runs**, with one Starlette/httpx `TestClient` deprecation warning per run.
- `python -m ruff check src/arise tests`: passed.
- `npm ci`: passed; npm reported **0 vulnerabilities**.
- `npm run typecheck` and `npm run build`: passed.
- Production token-canary bundle check: passed; fake `VITE_API_TOKEN` absent from generated assets.
- `python -m arise demo`: runtime-verified simulator task completed and passed its fact verifier, with 16 events. **This is simulation only.**
- `git diff --check`: passed; an additional whitespace/NUL scan passed across **79 UTF-8 files**.
- Final Git status review: **76 files are untracked** (the application, tests, CI, and docs in this checkout); none were staged or committed. `git diff --stat` alone therefore does not represent the full workspace, and the audit read those files directly.

The workflow is configured for Python 3.11/3.12 backend tests/lint/demo on Linux and Windows, frontend build plus token-canary exclusion, and a Windows Tauri `cargo check`. Remote CI results were not queried. `cargo`/`rustc` and a Windows host/WebView2 environment are unavailable here, so Tauri/Rust compilation, Windows backend behavior, ACLs, installer/sidecar, UIA, browser control, and real hardware are **environment-limited and unvalidated**.

Queue sizes, workers, model concurrency, request body bytes/deadline, WebSocket frames/queues, provider response bytes, and tool results are bounded. No load/fairness/soak benchmark was run; SQLite/event/session growth and replay remain unbounded. README, architecture, roadmap, `.env.example`, and CI were reviewed/updated to state those limitations.

## L. Release-gate matrix and exact blockers before Phase 2

| Subsystem | Phase 1 evidence/status | Gate classification |
|---|---|---|
| Domain architecture/import boundaries | Implemented; AST regression test passes | **PASS** for control-plane work |
| Task admission, idempotency, lifecycle, cancellation | Implemented; failure/concurrency/recovery tests pass; a file-backed DB lock prevents a second current backend from starting | **PASS** on Linux for single-instance ownership; Windows lock behavior remains environment-limited |
| Runtime policy, approvals, resource/verification | Implemented/tested with simulator; fail-closed evidence and lease-expiry tests pass | **CONDITIONAL**; no live action tools; durable evidence receipts required |
| Durable events, fanout, WebSocket replay | Implemented/tested, including overflow and future-cursor reset | **PASS** single-process; **CONDITIONAL** on retention/replay limits |
| FastAPI/auth/request limits/shutdown | Implemented/tested; supervised Linux process runtime-tested | **CONDITIONAL** on Windows/Tauri runtime and independent security review |
| SQLite migrations/recovery | Implemented/tested through v5; cross-connection idempotency exercised; second backend lock tested on Linux | **CONDITIONAL** on Windows lock validation and retention/backup policy |
| Configuration, token, provider secrets | Implemented/tested; production bundle canary verified | **CONDITIONAL** on Windows ACL/data-path validation and Tauri `.env` handling |
| Model gateway/provider adapter | Implemented; mock HTTP and concurrency-tested | **CONDITIONAL**; no live provider validation and no action tool |
| Capabilities/health/diagnostics | Implemented/tested; unavailable capabilities reported truthfully | **PASS** for Phase 1 reporting only |
| Frontend/backend contracts/UI | Implemented; field tests, TypeScript typecheck, build pass | **CONDITIONAL**; no browser/Tauri end-to-end or accessibility run |
| Windows UIA/browser/vision/voice/memory/research | Ports/contracts only or absent; no real adapter registered | **NOT IMPLEMENTED**; no capability/release claim |
| CI/Windows/Tauri | Workflow configured; local Linux checks pass | **ENVIRONMENT-LIMITED**; remote jobs and Windows shell compile/runtime not verified |
| Retention/load/soak/production hardening | Bounded per-operation controls; no retention or soak | **BLOCKED** for long-running/production claim |

Do not enter Phase 2 until these **preflight blockers** are resolved or explicitly accepted with owners and test gates:

1. **Windows shell baseline:** run the Windows backend matrix and Tauri compile job, then smoke-test the real WebView2 shell, child shutdown, generated/custom token paths, and per-user data/token ACLs. Resolve the documented `.env` versus inherited-process-environment behavior for desktop token/data overrides.
2. **Windows single-instance validation:** a non-blocking OS lock adjacent to the SQLite file is implemented and tested with a second process on Linux. Run the Windows matrix to verify `msvcrt` lock behavior and confirm the packaged backend refuses a second process using the same database. Do not rely on this lock on unsupported/network filesystems without separate validation.
3. **History lifecycle:** choose retention/deletion/export/backup policy and bounded replay/snapshot behavior before real tools can create long-running event/session history. Current history and replay-from-zero grow without bound.
4. **Auditable success evidence:** persist privacy-reviewed evidence references/summaries and expose them to task inspection. Runtime now rejects an evidence-free pass, but task records/events do not retain the evidence artifacts.
5. **Live-adapter execution contract:** for each Phase 2 tool, specify idempotency, target/process/profile identity, human-interference invalidation, cancellation/timeout-after-dispatch reconciliation, resource lease scope, and independent verifier evidence. Add failure-injection tests before registering the tool as available.
6. **Frontend/native recovery validation:** add a browser/Tauri end-to-end test for lost submit response/idempotent retry, future-cursor reset, event overflow/reconnect, and approval state. Current backend protocol tests and frontend build do not exercise a real WebView.

Voice, vision, memory, web research, long-term memory, filesystem/terminal tools, and external integrations were explicitly out of scope for this Phase 1 audit. At the time this baseline was recorded, no Phase 2 work had begun; the current Phase 2 scope and gates are documented separately.
