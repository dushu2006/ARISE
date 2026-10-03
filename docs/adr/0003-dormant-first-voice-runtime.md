# ADR-0003: Add a dormant-first, provider-neutral voice runtime

- **Status:** Accepted; gated server composition and authenticated lifecycle controls are implemented, with fake/preflight tests. Windows/audio/provider runtime validation remains pending.
- **Date:** 2026-10-02

## Context

ARISE's existing task engine, deterministic policy, resource manager, event journal, and verifier already own task authority. Voice adds a new input/output surface, but must not become a parallel execution runtime or promote Gemini into an authority. The optional PortAudio/WebRTC/Vosk/Kokoro adapters have fake/replay coverage, but there is no verified Windows microphone, speaker, model behavior, or live Gemini credential/session in this environment.

A continuously-open cloud microphone stream would violate the intended dormant privacy boundary. Provider callbacks and model speech also cannot establish that a desktop task happened: only the existing runtime's verified state can do that.

## Decision

- Add a provider-neutral `AudioHub` around typed microphone, local VAD, wake detector, playback, ASR, provider, event, and conversation-bridge ports. The optional local adapter package uses bounded PortAudio callback queues, PCM downmix/resampling, WebRTC VAD, confidence-gated Vosk wake/ASR, interruptible PortAudio playback, and streaming Kokoro ONNX TTS. It sends no pre-wake audio to a provider. Only a positive local wake result can activate a session; the detector may hand off only bounded ephemeral post-keyword audio.
- Treat ASR partials as display-only. When local ASR is configured, task admission requires a final local segment meeting the configured confidence threshold and a final provider transcript that agrees after normalization; the local segment is canonical. Queue overflow, low-confidence results, transcript disagreement, and recognizer failure fail closed. Gemini/provider transcription alone cannot substitute for a missing local final in that composition.
- Keep timeout, reconnect, barge-in, playback stop, and provider-session teardown in `AudioHub`. On barge-in, fence provider output immediately, stop playback, and send the first new local speech frame through the provider-neutral interruption method. Tag received generation output and reject stale or untagged output after the fence advances. Stopping voice/session monitoring does **not** cancel an `AgentRuntime` task and is never described as rollback. Task cancellation is an explicit bridge operation, distinct from conversational cancellation and playback interruption.
- Treat Gemini Live as an optional adapter. It is lazy-imported, requires explicit voice and security cloud opt-ins plus a `SecretProvider`, and implements the same session port as any future local/provider adapter. Provider resumption handles remain in memory.
- Expose only task submission, clarification, status, and cancellation functions. `VoiceConversationBridge` requires a deterministic task-intent match and exact normalized agreement between the tool argument and the current spoken transcript, then submits the transcript itself as a typed `UserRequest` with `RequestSource.VOICE` to the existing `TaskEngine`. Explicit cancellation likewise requires a deterministic cancellation intent. `AudioHub` permits at most one task admission per spoken turn. Policy, resource ownership, execution, and verification are unchanged. A successful spoken claim requires `TaskStatus.COMPLETED` from the runtime verifier.
- Watch task progress through the bridge and summarize only deterministic runtime states. Defer an update while the conversational turn is active, coalesce stale stage updates, and cancel watcher tasks only when the `AudioHub` is closed. Deactivation may close audio/provider resources while the independent AgentRuntime task continues.
- Keep raw microphone audio, transcripts, and provider resumption handles out of logs and persistent storage by default. Telemetry is bounded and aggregates latency/state events. A conservative transcript-to-audio gate mutes all audio with no associated transcript, control/task claims that do not exactly match runtime-authorized text, and common completion-claim forms when intent classification misses a control request.
- Keep the default API diagnostics-only. When all explicit voice/microphone, local Vosk path/dependency, keyring credential/SDK, and voice/security cloud gates pass, the server may compose the bridge; it still does not start capture automatically. Authenticated `/api/v1/voice/listening/start` checks local dependencies and loads the model before opening capture; `/stop` shuts monitoring down without cancelling a task. `UnavailableVoiceDiagnostics` and capability health report disabled, configuration-required, or unavailable states from current prerequisites.
- Provide a separate `arise-voice-check` harness with 21 public stages and nested probes. Tag evidence as `REAL`, `FAKE`, or `REPLAY`, and outcomes as `PASS`, `PARTIAL`, `FAILED`, `SKIPPED`, or `BLOCKED`. Its versioned report marks non-Windows runs `ENVIRONMENT-LIMITED`; Linux is host-guarded and may run a deterministic synthetic VAD replay, but never opens devices or sends audio. A real Windows run requires explicit capture/playback consent; Gemini further requires both cloud settings, a keyring credential, SDK, and command-line consent. Bound each run to four Gemini session attempts, 1 MiB input audio, 2 MiB output audio, 16,384 transient transcript characters, and 60 seconds for the end-to-end turn. The harness submits no real TaskEngine task; its task-admission probe remains fake/blocked evidence and does not prove production runtime behavior.

## Consequences

### Positive

- Dormant local detection is a strict boundary before any optional cloud session.
- Audio ownership, lifecycle, telemetry, and provider behavior stay behind replaceable ports.
- Spoken task progress and completion derive from TaskEngine state, not model acknowledgements.
- Provider failure, barge-in, inactivity shutdown, and voice deactivation cannot silently cancel or replay a dispatched desktop side effect.
- Fake-based tests can exercise orchestration without implying working hardware or cloud access.

### Trade-offs and known gaps

- Optional microphone/VAD/wake/ASR/TTS/playback adapters are implemented and fake-tested, including repeated barge-in and stale output-generation replay. `AudioHub` is now server-wired only behind opt-ins and explicit authenticated start; focused tests use fakes and preflight failure. The 21-stage CLI's Windows device and live Gemini modes have not run. Voice is not runtime-verified or a Windows support claim in this checkout.
- The Gemini adapter has contract/fake coverage only. No API key, real Live session, provider quota/network behavior, Windows SDK/audio behavior, or speaker output has been validated.
- Exact transcript matching is intentionally fail-closed but may mute legitimate audio if a provider segments/transcribes speech differently. The supplemental completion-claim heuristic is not exhaustive; neither it nor model instructions prove that every possible unsupported claim will be muted.
- Task progress is summarized at bounded state transitions. Voice monitoring does not provide rollback, and task state/result delivery across provider/session loss remains subject to runtime/UI diagnostics.
- Memory and research remain separate capabilities implemented outside this ADR: retrieved content must remain untrusted, and persistent memory writes require explicit, scoped, one-time consent. This ADR adds no memory or research authority.

## Revisit when

- The local VAD/wake-word/microphone/playback adapters have documented privacy/permission behavior and Windows hardware tests.
- The gated composition, authenticated start/stop lifecycle, and default capability truthfulness have been reviewed on a supported Windows audio host without bypassing the current task runtime.
- A real Gemini Live session has been validated with opt-ins, secret handling, audio formats, tool responses, interruption, reconnect, and quota/error cases.
- Output gating has tests against actual provider transcript/audio segmentation, with a safe text fallback when audio is withheld.
