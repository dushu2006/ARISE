# Voice validation guide

This guide describes the optional `arise-voice-check` harness and the separate server lifecycle gates. The harness exercises voice ports but does not exercise the server's gated production composition or submit a real `TaskEngine` task. The server composes voice only behind explicit settings/dependency/model/credential gates and still requires an authenticated explicit listening start. Neither path makes Gemini an executor: `TaskEngine`, policy, resources, action tools, and the verifier remain authoritative.

## Evidence model and report

The version-3 JSON report contains exactly **21 top-level stages**. Its top-level `environment_status` is `WINDOWS` only when the actual process runs on Windows; otherwise it is `ENVIRONMENT-LIMITED`. The pipeline `overall` remains `BLOCKED`/`PARTIAL` as appropriate and does not use that environment label as a test outcome. Stages with multiple independently measured probes (for example VAD, cancellation, and task admission) include a `subchecks` array; use those probe rows for detail rather than inferring success from the aggregate stage.

Each result has a `mode` and an outcome:

- `REAL`: exercised a real host integration in the Windows CLI. A platform fact such as OS detection may also be `REAL`, but does not prove audio.
- `FAKE`: an injected fake port was called. It proves only the fake-backed behavior.
- `REPLAY`: deterministic input, a synthetic PCM fixture, or a replayed captured utterance was used.
- Outcomes are `PASS`, `PARTIAL`, `FAILED`, `SKIPPED`, and `BLOCKED`. Missing optional configuration/consent is normally `SKIPPED` or `BLOCKED`, not a fabricated provider failure.

The 21 stages are: platform; input discovery; output discovery; microphone capture; PCM format; VAD; wake/activation; streaming ASR; transcript; intent classification; task admission; Gemini Live connection; Gemini response streaming; TTS; speaker playback; barge-in; cancellation; reconnect; device loss/recovery; end-to-end voice turn; shutdown/cleanup.

A diagnostic JSON file contains bounded counts, statuses, timing, platform/package facts, and sanitized error codes. It omits raw PCM, transcript text, device names, model paths, secret values, and provider response bodies. Audio and transcript objects are held only in memory while a run is active and are cleared during shutdown. Do not upload the report if your local environment details are sensitive.

The harness task-admission probe checks required examples against `VoiceConversationBridge` using an explicitly fake task port:

- `Tell me how to open Chrome.` → question; no fake task submission.
- `Open Chrome.` → command; one request reaches the fake port only.
- `Chrome.` → ambiguous/conversational; no fake task submission.

No real TaskEngine executor is invoked by this harness, and it cannot claim verified task completion. Production bridge composition is implemented behind server opt-ins; the fake probe is not evidence of actual microphone, local inference, Gemini, or Windows behavior. The server preflight tests verify only safe failure before audio capture.

## Scope 1: Linux CI and local unit tests

From the repository root, install the development extra and run the backend suite:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

For a local synthetic test of the actual WebRTC VAD implementation, install the optional local extra (or only the `webrtcvad-wheels` package) and run:

```bash
python -m pip install -e ".[voice-local]"
arise-voice-check
```

On Linux/macOS, the CLI is host-guarded: the JSON reports `environment_status: ENVIRONMENT-LIMITED`; it never constructs a microphone, speaker, or Gemini adapter and never captures/sends audio. It can run the deterministic intent/admission/tool-boundary replays and, when WebRTC VAD is installed, the synthetic VAD fixture. Hardware stages report `BLOCKED` with `WINDOWS_HOST_REQUIRED`; Gemini remains skipped unless explicitly requested, and an opt-in still cannot bypass the Windows gate. With the synthetic VAD dependency installed, expect platform `PASS / REAL`, synthetic VAD `PASS / REPLAY`, intent examples `PASS / REPLAY`, task-admission replay `PASS / FAKE`, Gemini `SKIPPED`, and overall `BLOCKED` with exit code 2. This is environment-limited evidence, not Windows verification.

The normal pytest suite uses fakes/replays and requires no microphone, audio output, Vosk model, Kokoro model, API key, or Gemini quota. It does not establish hardware behavior.

## Scope 2: Windows synthetic checks (no microphone capture or speaker output)

Prerequisites: Windows, Python 3.11+, and the development dependencies. For the production WebRTC VAD synthetic fixture, install `.[voice-local]` or at least `webrtcvad-wheels`. Then run the pytest suite on Windows:

```powershell
python -m pip install -e ".[dev,voice-local]"
python -m pytest -q
```

The fake/replay tests run without opening devices. The CLI can also be run without `--confirm-microphone` or `--confirm-playback`; it may enumerate PortAudio devices and test the synthetic VAD, but it will not capture microphone audio, play the speaker tone, or contact Gemini by default. A report can still be `PARTIAL`/`BLOCKED` because model configuration, real-device consent, actual server task admission (which the CLI never invokes), network-fault injection, and hot-unplug recovery have not been exercised.

## Scope 3: Windows real-device checks (local audio/models only)

### Install and prepare devices

Install the optional local stack and keyring support:

```powershell
python -m pip install -e ".[dev,voice-local]"
```

The optional dependencies include `sounddevice`/PortAudio, WebRTC VAD, Vosk, NumPy/SoXR, and Kokoro ONNX. If PortAudio cannot load on the machine, the input/output discovery result will be `BLOCKED` with a safe code. The CLI does not download models. OS keyring support is needed only for the separately opted-in Gemini scope.

1. In **Settings → Privacy & security → Microphone**, allow microphone access for desktop apps. Select/enable a working input and output device in **Settings → System → Sound**; close apps that have exclusive access if streams cannot open.
2. Obtain a Vosk acoustic model for the intended locale and a compatible Kokoro ONNX model plus voices file. Review each model's license. Store them locally; do not put paths or model files in a report or commit them.
3. If Windows has multiple devices, inspect local PortAudio indices with `python -c "import sounddevice as sd; print(sd.query_devices())"` and pass `--input-device` / `--output-device`. Device names are displayed only in that local command; the harness report records counts and selected numeric indices, not names.
4. The harness prompts before bounded microphone capture. It asks for the wake word followed by the harmless question `Tell me how to open Chrome.` The default capture is six seconds; `--capture-seconds` is bounded to 2–30 seconds. PCM is not written to disk. The synthetic speaker check is a 440 Hz tone lasting 400 ms. A playback `PASS` means the stream started and the bounded write completed; it does not claim a human heard the signal. If both microphone and playback checks are consented to, the clean-close/reopen probe also uses a 250 ms tone.

Example (PowerShell):

```powershell
arise-voice-check `
  --confirm-microphone `
  --confirm-playback `
  --capture-seconds 6 `
  --vosk-model "C:\models\vosk-model-en-us" `
  --kokoro-model "C:\models\kokoro-v1.0.onnx" `
  --kokoro-voices "C:\models\voices-v1.0.bin" `
  --tts-voice af_heart `
  --report voice-check.json
```

Input/output device discovery is non-capturing and runs even if the explicit capture/playback confirmations are omitted. Capture and tone output are skipped without their respective flags. A Vosk wake/model failure, absent ASR partial/final, PCM mismatch, or real device stream failure is reported as a stage-specific failure; no audio/transcript content is included in the error.

## Scope 4: Explicit live Gemini check (network/quota opt-in)

This scope is optional and deliberately separate from local audio validation. It needs a working Windows audio setup, local VAD and Vosk wake/ASR configuration, `google-genai`, an OS-keyring entry, and both ARISE cloud policy settings. Install:

```powershell
python -m pip install -e ".[dev,voice-local,voice,secure-secrets]"
```

Store the key in the Windows user's keyring under service `ARISE` and the configured secret name (default `GEMINI_API_KEY`). This PowerShell command prompts without echoing the value:

```powershell
python -c "import getpass,keyring; keyring.set_password('ARISE','GEMINI_API_KEY',getpass.getpass('Gemini API key: '))"
```

Set explicit settings in the process environment before starting the check:

```powershell
$env:ARISE__VOICE__ENABLED = "true"
$env:ARISE__VOICE__ALLOW_CLOUD = "true"
$env:ARISE__SECURITY__ALLOW_CLOUD_MODELS = "true"
```

Then use all three explicit command-line opt-ins:

```powershell
arise-voice-check `
  --confirm-microphone `
  --confirm-playback `
  --enable-live-gemini `
  --confirm-cloud `
  --vosk-model "C:\models\vosk-model-en-us" `
  --report voice-gemini-check.json
```

The harness opens Gemini only after local VAD and wake activation pass; it does not stream dormant microphone audio. The first diagnostic Gemini session has **no tool declarations**. A separate replay checks the six narrow ARISE bridge declarations (task request, clarification, task status, cancellation); no shell, browser, desktop, arbitrary Python, or direct executor tool is exposed. The harness never submits a real TaskEngine task.

Live use is bounded: at most four connection attempts per run, 1 MiB aggregate input audio, 2 MiB aggregate output audio, 16,384 transient transcript characters, and a 60-second end-to-end voice-turn window. These are hard harness limits, not a guarantee of provider pricing or zero quota use. A run may open a diagnostic session, a cancellation probe, an end-to-end conversation session, and a clean-close reconnect probe. A user should run this only when they intend to contact the configured provider.

Missing cloud settings, keyring credentials, or SDK dependencies are `BLOCKED`/`SKIPPED`, not reported as successful connections. Authentication/network/provider errors after a configured attempt can be `FAILED`. `NETWORK_FAULT_NOT_INJECTED` means a clean fresh connection was not treated as proof of network recovery. `DEVICE_LOSS_NOT_INJECTED` means hot-unplug recovery was not exercised. Neither condition should be relabeled as verified recovery.

## Status and exit codes

- `0`: every reportable stage passed (not expected while real fault-injection and device-loss gates remain closed; the harness never submits a production task).
- `1`: at least one exercised stage failed (`FAILED`).
- `2`: the run is blocked, partial, or has skipped stages—expected for Linux host-guarded checks and for a Windows run that has not cleared all gates.

`PARTIAL` means some probe passed while another subprobe in the same stage was skipped; a stage containing any `BLOCKED` probe reports `BLOCKED`. Inspect nested subchecks and their `mode` fields instead of treating an aggregate result as hardware proof. The current WebRTC adapter processes each bounded VAD frame synchronously and has no cooperative cancellation point; its live cancellation subcheck is therefore `BLOCKED`, not a claimed cancellation pass. The fake/replay tests cannot establish native frame cancellation either.

## Troubleshooting codes

- `WINDOWS_HOST_REQUIRED`: run the real-hardware CLI on Windows. Linux/macOS do not impersonate Windows.
- `SOUNDDEVICE_NOT_INSTALLED` / `PORTAUDIO_RUNTIME_UNAVAILABLE`: install the local extra and verify the PortAudio runtime can load.
- `MICROPHONE_PERMISSION_DENIED`: grant desktop-app microphone permission in Windows privacy settings.
- `MICROPHONE_SILENT_OR_EMPTY`, `MICROPHONE_CAPTURE_TOO_SHORT`, `MICROPHONE_CAPTURE_TIMEOUT`: check device level/selection, mute state, exclusive access, and that the bounded prompt interval contains speech.
- `VAD_LIVE_SPEECH_NOT_DETECTED` / `VAD_LIVE_SILENCE_OR_TRANSITION_NOT_OBSERVED`: repeat the short non-sensitive phrase with a pause before/after it, and verify that the selected input is not continuously noisy or muted.
- `NO_INPUT_DEVICE` / `NO_OUTPUT_DEVICE` / `REQUESTED_*_DEVICE_NOT_FOUND`: enable or select a valid device index.
- `PLAYBACK_STREAM_WRITE_NOT_COMPLETED`, `AUDIO_OUTPUT_STREAM_FAILED`, `PORTAUDIO_RUNTIME_UNAVAILABLE`: check the selected output device, Windows audio-service state, PortAudio availability, and whether another application holds exclusive access. A stream `PASS` still does not assert that a person heard the test tone.
- `VOSK_MODEL_PATH_REQUIRED`, `VOSK_MODEL_DIRECTORY_NOT_FOUND`, `VOSK_DEPENDENCY_NOT_INSTALLED`, `VOSK_MODEL_LOAD_FAILED`, `VOSK_WAKE_INFERENCE_FAILED`, `VOSK_ASR_INFERENCE_FAILED`, `VOSK_ASR_RECOGNIZER_INIT_FAILED`: supply a compatible local model/dependency or resolve the local inference/runtime issue. Reports include the stable code, never the model path or raw exception.
- `WAKE_WORD_NOT_DETECTED`: ensure capture includes the configured wake word, model locale/grammar matches, and the input level is usable.
- `ASR_FIRST_PARTIAL_NOT_RECEIVED` / `ASR_CONFIDENT_FINAL_NOT_RECEIVED`: check model/locale, capture quality, and confidence; report text is intentionally omitted.
- `KOKORO_MODEL_AND_VOICE_FILES_REQUIRED`, `KOKORO_MODEL_LOAD_FAILED`, `KOKORO_DEPENDENCY_NOT_INSTALLED`, `KOKORO_TTS_INFERENCE_FAILED`: configure compatible local files/dependencies or resolve the local model/inference issue. No audio or raw provider error is included.
- `ARISE_CLOUD_OPT_INS_REQUIRED`, `GEMINI_CREDENTIAL_UNAVAILABLE`, `GEMINI_SDK_NOT_INSTALLED`: check the two settings, keyring account, and optional SDK. Never paste the key into the report or chat.
- `GEMINI_*_AUDIO_BUDGET_EXCEEDED`: the bounded live diagnostic budget was reached; the harness stops rather than sending unlimited audio.
- `NETWORK_FAULT_NOT_INJECTED` / `DEVICE_LOSS_NOT_INJECTED`: expected limitations; do not claim reconnect or hot-unplug recovery.

The authenticated server reports voice as disabled, unavailable, or configuration-required according to settings/prerequisites. When all gates pass, authenticated `POST /api/v1/voice/listening/start` performs dependency/model preflight before opening capture; `POST /api/v1/voice/listening/stop` shuts monitoring down. The harness does not alter or validate that server state. No real Windows microphone, Vosk inference, Gemini session, or voice task admission has been runtime-verified here.
