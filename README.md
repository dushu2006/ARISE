# ARISE

ARISE is a Windows-first desktop-agent project built around a deliberately strict rule:

> **The model proposes what. Policy and verified capabilities decide how.**

Phase 1 provides an authenticated local control plane: a Python/FastAPI task service, SQLite task/session/event persistence, a versioned WebSocket protocol, a bounded asynchronous task engine, a React/Vite control-room UI, a minimal Tauri 2 shell, structured health/capability reporting, and a provider-neutral model router with an OpenAI-compatible HTTP adapter. Phase 2 is in progress: an optional isolated Playwright adapter now exists, but the production API does **not** register it or any host-control tools. Windows UIA, voice, semantic memory, and autonomous research adapters are not implemented.

## What works in this phase

- **Task control:** requests become durable queued tasks; typed plans are dependency-checked, receive authority only from the trusted request boundary, and flow through the existing deterministic runtime.
- **No fake execution:** the production app registers no simulator or host-control tools. With no planning provider, tasks move to `requires_user_input`; with no desktop/browser executor, a proposal is blocked. A task cannot be reported complete without independent postcondition verification.
- **Safety lifecycle:** explicit queue, wait, approval/input, interruption, unknown, blocked, failed, cancelled, partial, and verified-complete states; bounded timeouts; cancellation-safe resource leases; no blind replay after dispatch may have begun.
- **Scoped confirmation:** approval is bound to one exact action, principal, contract fingerprint, and expiry, then consumed once. Model output and external content do not carry authority.
- **Authenticated API:** HTTP endpoints use a local bearer credential; WebSockets require a protocol-v1 hello and token. Origins are restricted to local/Tauri origins, with a narrow development-preview allowlist. The credential is kept outside SQLite and logs.
- **Persistence and traceability:** SQLite migrations cover tasks, causal event IDs/sequences, sessions, redacted conversation turns, and principal/session-scoped request-ID mappings. New request IDs are bound to a digest of normalized, redacted request content; replays reuse the task, while conflicting content gets HTTP 409. Legacy mappings are retained without retroactive fingerprints. Task records intentionally do not persist raw action parameters or credentials.
- **Capability discovery:** safe host facts are reported; Windows UIA, vision/OCR, voice, semantic memory, and research remain unavailable/deferred. The optional Playwright adapter is not included in default capability registration.
- **Model gateway:** provider-neutral contracts/router, privacy-aware local/cloud selection, bounded concurrency, provider circuit status, secret-by-reference credentials, and a non-streaming OpenAI-compatible adapter. Cloud use requires explicit opt-in in both model and security settings.
- **UI and shell:** React/TypeScript/Vite task dashboard, inspector, approvals/clarification surface, event trace, capability health page, and a Tauri shell that starts the local Python backend and reads its token through a native command.

## Phase 1 limitations

- No live Windows UI Automation, filesystem/terminal, screenshot, OCR/vision, microphone, or speaker tools are registered. A source-level Playwright adapter is available as an opt-in experiment, but it is not composed into the production API and has not been validated against a real browser runtime.
- A model provider can return a structured plan, but this build has no registered production action tools, so side-effecting plans cannot execute successfully. The control plane can accept, inspect, cancel, and safely hold work.
- The Tauri shell/backend launcher is an implementation boundary, not a signed or production installer. Rust and Windows/WebView2 packaging could not be exercised in this environment.
- SQLite is local and conversation/task descriptions may contain personal data; common credential-shaped strings are redacted before conversation persistence, but the redactor is best-effort, not a secret scanner. Do not paste credentials into prompts.
- SQLite task, session, and event records have no automatic retention or purge; WebSocket replay from sequence zero and returned session transcripts can grow with history. Backup/restore, export/delete controls, database encryption, multi-user authentication, signing, and production security review remain future hardening.
- Runtime queues, resource leases, and confirmations are process-local. A non-blocking OS lock adjacent to each file-backed SQLite database is acquired before the API opens or migrates that database, refusing a second current ARISE backend using the same database; Tauri also refuses an occupied default API port. Windows/network-filesystem lock behavior remains unvalidated, and this single-instance lock is not a durable per-task claim.
- Successful verifier results require traceable observed/retrieved evidence in memory, but full evidence records are not persisted or shown in task details yet. Add privacy-reviewed evidence receipts before consequential live tools can claim completion.

## Quick start: Python backend

Python 3.11+ is required.

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux:       source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest -q
arise demo
arise-backend
```

The backend defaults to a loopback bind, uses a per-user local data directory, and creates a random API token at `api.token` on first start. The token value is never printed. On Windows the default data directory is `%LOCALAPPDATA%\ARISE`; elsewhere it follows the XDG data-directory convention. Set `ARISE__DATA_DIR` or `ARISE__DATABASE__PATH` to choose another local location. `.env.example` lists Python backend settings and safe defaults; copy it to `.env` only when needed. The native Tauri bridge does not parse `.env`: desktop token/data-directory overrides must be present in the process environment, or the generated default token file must be used. Do not put real credentials in a committed file.

`arise demo` is explicitly an isolated in-memory simulator test. The API application does not register that simulator as a real desktop capability.

## Optional Playwright adapter (experimental, not registered by the API)

Install the optional Python package and the Chromium binary explicitly:

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

`PlaywrightBrowserProvider` starts a fresh, non-persistent Chromium context only when `await provider.start()` is called. It does not attach to an existing user profile; downloads are disabled, navigation is limited to HTTP(S), and private/loopback destinations are blocked unless the host application opts in. The DOM snapshot is bounded and omits form values. Sensitive fields require a `SecretRef` backed by an explicitly supplied secret provider. Operation-specific tools apply trusted risk floors and a per-page runtime resource lease.

A Python composition root may call `register_playwright_tools(registry, provider)` and pass the same provider as its `AgentRuntime` environment. The built-in API does not do this: capability reporting remains unavailable until an application explicitly wires, authorizes, and independently verifies the adapter. This sandbox tests the adapter with fakes only; a real Playwright/Chromium runtime and Windows behavior remain unverified.

## React/Vite development UI

Run the backend on port 8765 and the UI on port 5173. The browser uses relative `/api` and `/ws` URLs; Vite proxies them to the backend. Start the backend first; on first run it creates a random, private token file without printing the value. In a second PowerShell window:

```powershell
$env:VITE_API_TOKEN = (Get-Content "$env:LOCALAPPDATA\ARISE\api.token" -Raw).Trim()
cd frontend
npm ci
npm run dev
```

The local Tauri webview obtains its API token through a native Tauri command; browser-only development uses `VITE_API_TOKEN`. Never commit `.env.local` or credentials.

## Tauri 2 desktop shell

Rust, Cargo, and the platform prerequisites listed in the official Tauri documentation are required. From `frontend/`:

```bash
npm ci
npm run tauri:dev
```

The shell starts `python -m arise.server` on loopback unless an `ARISE_BACKEND_EXECUTABLE` is supplied. The shell waits for a startup signal from its own backend child and, if port 8765 is already occupied, refuses to reuse that process rather than sending the local API credential to an unverified listener. A parent-owned stdin pipe is monitored by the backend so an unexpected desktop-shell exit triggers graceful server shutdown; the shell also kills/waits for its child during normal teardown. For a packaged Windows deployment, the Python backend still needs to be built and installed as an executable/sidecar; this checkout does not claim a signed or self-contained installer. The Python pipe-shutdown behavior has a subprocess test; the Tauri/Rust lifecycle is not locally compiled because Cargo is unavailable.

## Model configuration

No provider is called by default. A local OpenAI-compatible endpoint may be configured with:

```text
ARISE__MODEL__BASE_URL=http://127.0.0.1:1234/v1
ARISE__MODEL__PROVIDER_ID=local-model
ARISE__MODEL__MODEL_ID=<local-model-id>
```

A local loopback endpoint can run without an API key. For remote providers, use HTTPS, put the credential in the OS keyring under `ARISE__MODEL__API_KEY_SECRET_NAME`, and explicitly set both `ARISE__MODEL__ALLOW_CLOUD=true` and `ARISE__SECURITY__ALLOW_CLOUD_MODELS=true`. Environment-variable secret lookup is disabled unless explicitly enabled for development. The router reports unprobed or unhealthy providers as degraded.

Even with a model configured, the current server has no real desktop action tools. That boundary is visible in `/api/v1/capabilities`; no completion is fabricated.

## Main routes

- `GET /healthz` — minimal health/readiness snapshot.
- `GET /api/v1/health`, `/api/v1/diagnostics`, `/api/v1/capabilities` — authenticated system and capability status.
- `POST /api/v1/sessions`, `GET /api/v1/sessions` — local session persistence.
- `POST /api/v1/tasks`, `GET /api/v1/tasks`, `GET /api/v1/tasks/{id}` — task lifecycle and inspection.
- `POST /api/v1/tasks/{id}/respond`, `/cancel`, `/approve` — explicit task controls.
- `GET /api/v1/events?after=<sequence>` — durable audit/event replay.
- `WS /ws/v1` — protocol v1 hello/authentication, task commands, heartbeat, and bounded event stream.

## Validation

The test suite covers contracts, policy, action lifecycle, retries/resources, task orchestration/clarification, provider routing/credential boundaries, SQLite migrations/redaction, HTTP authentication, health/capabilities, WebSocket protocol behavior, and fake-based Playwright adapter safeguards. `.github/workflows/tests.yml` is configured to run backend tests on Linux and Windows (Python 3.11/3.12), build the frontend, and compile the Tauri shell on Windows. This sandbox has not run the Windows CI job. **Real Chromium/Playwright behavior, Windows UIA, and installer behavior remain unvalidated; fake adapter tests do not constitute browser support.**
