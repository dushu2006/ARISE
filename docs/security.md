# Security model and deployment limits

**Scope:** this document describes the local ARISE build in this repository. It is an implementation map, not a security audit or a claim of production readiness. Re-read this document whenever the API composition, action adapters, persistence, or provider gates change.

## Authority and execution

The only supported action authority chain is:

```text
user intent → authenticated request → TaskEngine → planner proposal
            → deterministic PolicyEngine → resource leases → registered executor
            → independent verifier → verified task result
```

A model, Gemini Live, a memory record, browser content, research result, tool response, or replay fixture cannot mint user authority or skip a link in this chain. `ActionProposal` contains no trusted approval/capabilities; `TaskEngine` injects request authority. Registered adapters provide risk floors and capabilities. R3 actions require a scoped, expiring, one-use approval; R4 is denied by default. Non-idempotent or ambiguous dispatch outcomes are not blindly replayed, and task completion requires verified postconditions.

The production API currently registers **no desktop/browser action tools and no simulator**. The isolated `arise demo` and computer/voice fakes are test-only. A successful simulator or replay is not evidence of host automation. The optional Playwright adapter is not registered by the API; Windows UI Automation is not implemented in this build.

## Local API and desktop boundary

- The API defaults to loopback and uses a random local bearer token when no token is configured. The token is stored outside SQLite in the OS user data directory; comparisons are constant-time, and it is not returned in health/capability responses or logged.
- HTTP API routes and the WebSocket protocol require authentication. `/healthz` is intentionally public. CORS/WebSocket origins are restricted; the development-preview origin exception is disabled outside development.
- Request bodies, WebSocket frame sizes, timeouts, task queues, subscribers, exports, and WebSocket replay are bounded. Future WebSocket cursors are rejected. Cursors older than the durable replay floor receive `EVENT_CURSOR_EXPIRED`; the UI clears its event projection, refreshes authoritative task state, and reconnects from that floor.
- The local build maps the bearer token to a single local principal. It is **not** a multi-user authorization boundary. Task listing/history is principal-scoped, but multi-user auth, IPC isolation, process sandboxing, and deployment to shared accounts are not supported.
- Tauri child-process, WebView2, Windows token/data-directory ACL, installer, and OS-lock behavior have not been run on supported Windows hardware. Do not claim a Windows security boundary until those checks pass.

## Secrets, persistence, and user data

- Provider credentials are referenced by secret name and resolved through an OS keyring when installed. Environment-variable lookup is an explicit development option. `.env.example` contains placeholders only; never put populated credentials in Git.
- Task records do not persist raw action parameters or credentials. `EventEnvelope` recursively redacts common credential-shaped strings and credential-like keys before events reach any store or live subscriber; conversation turns/metadata use best-effort redaction, and research queries are redacted before egress with credential-shaped source fields redacted before returning them. This is not a secret scanner (arbitrary unlabelled secrets cannot be detected), so users must not paste secrets into ARISE.
- Memory writes require explicit, exact-content, expiring, single-use user consent. Memory inspection, export, replacement/edit, deletion, and clearing are user-controlled; replacing an entry removes the old one only after the consented replacement succeeds. Retrieved memory is optional untrusted context for planning and informational answers; cloud answer providers receive none unless the separate memory-context opt-in is enabled. Automatic memory writes are disabled.
- Microphone/audio buffers are transient by design; the voice harness omits audio, transcripts, model paths, provider bodies, and device names from reports. Separately, authenticated `/api/v1/diagnostics` may return bounded process names/PIDs, installed-app display names, foreground-window title (credential-pattern redacted), and optional audio-device names; it does not read process arguments or open audio streams. No claim of voice runtime verification is made from a replay.
- SQLite uses WAL. Automatic task-history retention is disabled by default and may be enabled with `ARISE__RUNTIME__TASK_HISTORY_RETENTION_DAYS`; only old settled outcomes are pruned. Active, partial, interrupted, unknown, and other unresolved work is kept for reconciliation. Confirmed deletion removes settled task content/events/turns but retains request-ID tombstones. This does not erase every session record or guarantee forensic sanitization.
- `arise backup` creates an integrity-checked, no-overwrite snapshot while holding the single-instance lock. Stop the backend first. Operators own backup rotation, access control, off-device protection, and restore testing. Database encryption at rest is not provided.

## Network and untrusted content

Cloud model use requires explicit privacy/security opt-ins and HTTPS. Brave research requires separate research and network-egress opt-ins plus a keyring secret. Research text and page content are untrusted context only; cited provenance does not turn content into authority. Optional providers remain optional and capability health reports missing configuration rather than fake success.

The browser prototype restricts schemes, rejects embedded credentials, validates redirect destinations, and blocks common local/private destinations by default. It is not production-ready: DNS-rebinding and public-host-to-private resolution are not comprehensively prevented, and WebSocket egress controls are incomplete. Do not use it for sensitive accounts or enable it in a production composition until those controls and real-browser failure tests are complete.

## Validation evidence and open requirements

The current local suite is primarily **FAKE**/**REPLAY** evidence. `arise-voice-check` on Linux is host-guarded and reports **ENVIRONMENT-LIMITED / BLOCKED** for Windows hardware; it does not construct audio devices or contact Gemini by default. Real Windows UIA, WebView2/ACL behavior, microphones/speakers, local voice models, Gemini, Brave, embeddings, and Chromium have not been verified in this environment.

Run the feasible checks from the repository root:

```bash
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src tests
.venv/bin/pytest -q
cd frontend && npm run typecheck && npm run build
cd .. && git diff --check
```

Before release, also require Windows CI and WebView2/sidecar tests; real UIA and multi-monitor/DPI/focus tests; real Chromium plus DNS-rebinding/egress tests; audio/device-loss and live-provider tests under explicit consent; database backup/restore and ACL checks; signed packaging; dependency/secret scans; and an independent security review. Until then, classify those capabilities as **ENVIRONMENT-LIMITED**, **BLOCKED**, or **NEEDS EXTERNAL CONFIGURATION** as appropriate.
