# ARISE

ARISE is a Windows-first desktop-agent project built around a deliberately strict rule:

> **The model proposes what. Policy and verified capabilities decide how.**

The repository has an authenticated local control plane: a Python/FastAPI task service, SQLite task/session/event persistence, a versioned WebSocket protocol, a bounded asynchronous task engine, a React/Vite control-room UI, a minimal Tauri 2 shell, structured health/capability reporting, and a provider-neutral model router with an OpenAI-compatible HTTP adapter. Phase 2 Windows/browser adapters remain in progress. The dormant-first voice path includes optional PortAudio, WebRTC VAD, Vosk, Kokoro ONNX, and Gemini Live adapters, with a narrow bridge to the existing `TaskEngine`. The server can compose this path only when explicit microphone, local-model, dependency, Gemini credential, and cloud-policy gates are satisfied; an authenticated user must still explicitly start listening. Tests cover fake paths and local preflight failures only—no real Windows audio device, local inference, or Gemini session has been validated. Consent-governed local memory and gated Brave research are implemented, while semantic embeddings are optional; no live provider/research or Windows UIA/browser runtime has been verified.

## What works in this phase

- **Text interaction routing:** the overview composer distinguishes clear commands from questions/conversation before admission. Commands enter the existing `TaskEngine`; questions never create tasks and receive an informational response only through a configured model. Time-sensitive answers require the per-request research opt-in; retrieved sources remain untrusted and are shown with provenance. Missing providers yield an explicit unavailable response rather than fake success.
- **Task control:** action requests become durable queued tasks; typed plans are dependency-checked, receive authority only from the trusted request boundary, and flow through the existing deterministic runtime. Authenticated callers can associate child tasks with a live parent in the same session, list direct children, and parent cancellation propagates to active descendants; lineage is persisted in task snapshots and request fingerprints.
- **No fake execution:** the production app registers no simulator or host-control tools. With no planning provider, tasks move to `requires_user_input`; with no desktop/browser executor, a proposal is blocked. A task cannot be reported complete without independent postcondition verification.
- **Safety lifecycle:** explicit queue, wait, approval/input, interruption, unknown, blocked, failed, cancelled, partial, and verified-complete states; bounded timeouts; cancellation-safe resource leases; no blind replay after dispatch may have begun.
- **Scoped confirmation:** approval is bound to one exact action, principal, contract fingerprint, and expiry, then consumed once. Model output and external content do not carry authority.
- **Authenticated API:** HTTP endpoints use a local bearer credential; WebSockets require a protocol-v1 hello and token. Origins are restricted to local/Tauri origins, with a narrow development-preview allowlist. The credential is kept outside SQLite and logs.
- **Persistence and traceability:** SQLite migrations cover tasks, causal event IDs/sequences, sessions, redacted conversation turns, principal/session-scoped request-ID mappings, optional memory embeddings, and history-deletion tombstones. New request IDs are bound to a digest of normalized, redacted request content; replays reuse the task, while conflicting content gets HTTP 409. Cleared task request IDs remain tombstoned so a replay cannot recreate purged history. Task records intentionally do not persist raw action parameters or credentials.
- **User-controlled history:** the authenticated UI can export a bounded JSON archive of task snapshots and their events, or clear terminal task history after confirmation. Active and recoverable tasks—including `PARTIALLY_COMPLETED`—are retained. Export is capped at 8 MiB and reports truncation; deleting terminal tasks also removes their events/turns and only removes an otherwise-empty session when no active/recoverable task remains.
- **Capability discovery:** authenticated diagnostics reports bounded process names/PIDs and, on Windows, attached display adapters, monitor geometry/effective DPI, sanitized foreground-window metadata, and installed-app names. Optional PortAudio discovery lists device names without opening streams; command lines, browser profiles, screenshots, and raw audio are not collected. Windows UIA, vision/OCR, and host actions remain unavailable; Windows discovery/runtime behavior still needs supported-host validation. Local memory requires explicit per-write consent; research requires explicit egress settings and task-level consent; semantic embeddings are optional. Optional Playwright remains unregistered by the API.
- **Model gateway:** provider-neutral contracts/router, privacy-aware local/cloud selection, bounded concurrency, provider circuit status, secret-by-reference credentials, and a non-streaming OpenAI-compatible adapter. Cloud use requires explicit opt-in in both model and security settings.
- **UI and shell:** React/TypeScript/Vite task dashboard, inspector, approvals/clarification surface, event trace, capability health page, and a Tauri shell that starts the local Python backend and reads its token through a native command.

## Phase 1 limitations

- No live Windows UI Automation, filesystem/terminal, screenshot, OCR/vision, or desktop/browser action tools are registered. The optional voice path is composed only behind explicit settings, local Vosk/dependency/credential preflight, and an authenticated explicit start/stop control; it is not runtime-verified against real audio or Gemini. Playwright remains unregistered and fake tests do not prove browser compatibility.
- A model provider can return a structured plan, but this build has no registered production action tools, so side-effecting plans cannot execute successfully. The control plane can accept, inspect, cancel, and safely hold work.
- The Tauri shell/backend launcher is an implementation boundary, not a signed or production installer. Rust and Windows/WebView2 packaging could not be exercised in this environment.
- SQLite is local and conversation/task descriptions may contain personal data; common credential-shaped strings are redacted before conversation persistence, and research queries/source fields are redacted at the egress/response boundary. This is best-effort, not a secret scanner. Do not paste credentials into prompts.
- Automatic task-history retention is disabled by default. An explicit `ARISE__RUNTIME__TASK_HISTORY_RETENTION_DAYS` opt-in prunes only old settled outcomes; active, partially completed, interrupted, unknown, and other unresolved tasks remain, and replay tombstones persist. Task history also has an 8 MiB-bounded JSON export and confirmed deletion. The `arise backup` command provides a user-initiated SQLite snapshot; backup scheduling/rotation remains an operator responsibility. Standalone session management, database encryption, multi-user authentication, signing, and production security review remain future hardening. WebSocket replay is paged and capped at 5,000 events per connection; clients can reconnect from their last sequence to continue. When deletion prunes a requested cursor, the server reports `EVENT_CURSOR_EXPIRED` so the UI refreshes authoritative task state before reconnecting from the replay floor. Stored history still grows unless opt-in retention is configured.
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

Create an online, integrity-checked SQLite snapshot with `arise backup`. By default it writes a timestamped file under `<DATA_DIR>/backups`; `arise backup --destination <path>` selects a different path. The destination is never overwritten, and POSIX backup files are mode `0600`. The command takes the database's single-instance lock, so stop the API/Tauri backend before running it. Backups contain local task, event, session, and memory data; protect and remove them according to your retention policy.

## Optional Playwright adapter (experimental, not registered by the API)

Install the optional Python package and the Chromium binary explicitly:

```bash
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

`PlaywrightBrowserProvider` starts a fresh, non-persistent Chromium context only when `await provider.start()` is called. It does not attach to an existing user profile; downloads are disabled, navigation is limited to HTTP(S), and private/loopback destinations are blocked unless the host application opts in. The DOM snapshot is bounded and omits form values. Sensitive fields require a `SecretRef` backed by an explicitly supplied secret provider. Operation-specific tools apply trusted risk floors and a per-page runtime resource lease.

A Python composition root may call `register_playwright_tools(registry, provider)` and pass the same provider as its `AgentRuntime` environment. The built-in API does not do this: capability reporting remains unavailable until an application explicitly wires, authorizes, and independently verifies the adapter. This sandbox tests the adapter with fakes only; a real Playwright/Chromium runtime and Windows behavior remain unverified.

## Voice/conversation adapters (optional; dormant by default and not runtime-verified)

The provider-neutral `AudioHub` remains dormant until an authenticated user explicitly starts listening and local VAD/wake detection accepts a wake word. Optional adapters provide bounded PortAudio capture/playback, WebRTC VAD, Vosk wake/streaming ASR, and Kokoro ONNX TTS. Only a sufficiently confident final local transcript that agrees with the provider transcript can enter `VoiceConversationBridge`; questions and ambiguous input do not become tasks. Barge-in interrupts conversational output, not a running `TaskEngine` task. Gemini is replaceable conversational I/O only, never ARISE's planner, policy, memory, or executor. The TaskEngine → policy → resource management → executor → verifier authority chain is unchanged.

Install dependencies with `python -m pip install -e ".[voice-local,voice,secure-secrets]"`; models are user-supplied and are not downloaded or bundled. Configure the Vosk model path and explicit gates in the process environment or a local, uncommitted `.env`:

```text
ARISE__VOICE__ENABLED=true
ARISE__VOICE__MICROPHONE_ENABLED=true
ARISE__VOICE__LOCAL_MODEL_PATH=<local Vosk model directory>
ARISE__VOICE__ALLOW_CLOUD=true
ARISE__SECURITY__ALLOW_CLOUD_MODELS=true
```

Also store Gemini's key in the OS keyring under the configured secret name. Both cloud opt-ins, SDK/dependencies, keyring credential, local model, and microphone opt-in must pass preflight. These settings only make the controls available: the authenticated UI or `POST /api/v1/voice/listening/start` is still required to open the device. The start route checks local dependencies and loads the model before capture; `POST /api/v1/voice/listening/stop` explicitly shuts capture down. `GET /api/v1/voice/status` reports current state. Audio is kept local until wake detection; raw audio, transcript bodies, credentials, and provider resumption handles are not logged or persisted by default. Merely installing extras never starts capture.

Tests cover fake lifecycle behavior, authenticated server controls, content-free lifecycle events, and preflight failure before audio capture. They do not prove real hardware, local inference, or Gemini operation. Real Windows device/model behavior and Gemini Live remain **ENVIRONMENT-LIMITED**; do not report voice as verified until tested on the intended Windows configuration.

### Voice validation scopes

`arise-voice-check` reports exactly 21 stages with nested probes, `REAL`/`FAKE`/`REPLAY` evidence modes, and `PASS`/`PARTIAL`/`FAILED`/`SKIPPED`/`BLOCKED` outcomes. The report omits raw audio, transcripts, device names, model paths, secrets, and provider response bodies. Linux is host-guarded: it does not access devices or contact Gemini, though an installed VAD may run against synthetic PCM. It is not Windows verification. Windows real capture/playback requires separate explicit consent flags; live Gemini has additional settings, keyring, command-line opt-ins, and aggregate traffic/time limits. Clean reopen is not proof of hot-unplug or network-failure recovery; those fault paths remain unverified. No TaskEngine task is submitted by the harness; server voice composition remains opt-in and is not validated by this harness.

See the [voice validation guide](docs/voice-validation.md) for prerequisites, Windows setup, commands, troubleshooting, status interpretation, and the distinct Linux CI / Windows synthetic / Windows real-device / live Gemini scopes. See [ADR-0003](docs/adr/0003-dormant-first-voice-runtime.md) and the [roadmap](docs/roadmap.md) for remaining release gates.

## Consent-governed memory and research

Memory writes are local, explicitly initiated by the user, and require a short-lived single-use consent bound to the exact record content and expiry. The UI supports listing, search, export, individual deletion, and confirmed clear; records have bounded retention. Semantic embeddings are optional, compatible vectors are used only when configured, and lexical ranking remains the fallback. Cloud embeddings require the security, embedding, and memory-context opt-ins; memory text is redacted before an opted-in provider request. Automatic memory inference/writes are disabled.

Brave research is implemented but disabled by default. Enable both `ARISE__RESEARCH__ENABLED=true` and `ARISE__SECURITY__ALLOW_WEB_RESEARCH=true`, and configure the provider credential through the OS keyring. Requests are user-initiated; returned pages are bounded, provenance-tagged **untrusted context**, never action authority or proof of task completion. No live Brave request has been verified in this environment.

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
- `GET /api/v1/tasks/export?limit=...`, `DELETE /api/v1/tasks/history` — bounded task/event export and confirmed deletion of terminal history (active/recoverable work is retained).
- `GET /api/v1/voice/status`, `POST /api/v1/voice/listening/start|stop` — authenticated gated local voice lifecycle controls.
- `/api/v1/memory/*`, `POST /api/v1/research/search` — consent-gated local memory and explicitly opted-in untrusted research.
- `GET /api/v1/events?after=<sequence>` — durable audit/event replay; returns HTTP 410 with `EVENT_CURSOR_EXPIRED` below the replay floor.
- `WS /ws/v1` — protocol v1 hello/authentication, task commands, heartbeat, and bounded event stream.

## Validation

The test suite covers contracts, policy, action lifecycle, retries/resources, task orchestration/clarification, provider routing/credential boundaries, SQLite migrations/redaction/history deletion and idempotency tombstones, explicit memory consent, gated research fixtures, authenticated voice lifecycle/preflight, health/capabilities, WebSocket protocol behavior, and fake-based Playwright safeguards. `.github/workflows/tests.yml` is configured to run backend tests on Linux and Windows (Python 3.11/3.12), build the frontend, and compile the Tauri shell on Windows. This sandbox has not run the Windows CI job. **Real Chromium/Playwright behavior, Windows UIA, voice devices/providers, and installer behavior remain unvalidated; fake adapter tests do not constitute browser or voice runtime support.**
