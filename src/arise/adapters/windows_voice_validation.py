"""Real-hardware Windows voice validation for the existing ARISE voice ports.

No hardware/provider adapter is imported or constructed on a non-Windows live run. Deterministic
checks and injected adapters are explicitly labelled REPLAY/FAKE; only the Windows CLI reports
REAL, and it does not register voice in the production server or invoke TaskEngine actions.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import importlib.util
import json
import math
import platform
import struct
import sys
import time
import uuid
from array import array
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from arise.core.extensions import AudioChunk, SpeechRecognitionPort, SpeechSynthesisPort
from arise.core.intent import IntentClassifier, IntentKind
from arise.core.models import VoiceState
from arise.core.voice import (
    AudioDevice,
    AudioHub,
    AudioPlaybackPort,
    LiveConversationProvider,
    LiveConversationSession,
    LiveEvent,
    LiveEventType,
    LiveSessionConfig,
    MicrophonePermissionDenied,
    MicrophonePort,
    MicrophoneUnavailable,
    VoiceActivityDetectorPort,
    VoiceConfig,
    VoiceEvent,
    VoiceEventKind,
    VoiceEventSink,
    VoiceProviderFailure,
    WakeWordDetection,
    WakeWordDetectorPort,
    voice_tool_declarations,
)
from arise.core.voice_bridge import VoiceConversationBridge

REPORT_SCHEMA_VERSION = 3
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_CHUNKS = 1_000
MAX_TTS_AUDIO_BYTES = 2 * 1024 * 1024
MAX_TTS_CHUNKS = 64
MAX_GEMINI_SESSION_ATTEMPTS = 4
MAX_GEMINI_INPUT_AUDIO_BYTES = 1 * 1024 * 1024
MAX_GEMINI_OUTPUT_AUDIO_BYTES = 2 * 1024 * 1024
MAX_GEMINI_TRANSCRIPT_CHARACTERS = 16_384
MAX_GEMINI_E2E_SECONDS = 60.0
MAX_REPORT_DETAILS = 40
_TEST_TTS_TEXT = "This is a local ARISE voice output check."
_GEMINI_TEST_INSTRUCTION = (
    "This is a short ARISE voice validation. Respond to the user's harmless spoken question "
    "with one brief spoken answer. Do not call tools, request computer actions, or claim that "
    "any task was performed."
)


class EnvironmentStatus(StrEnum):
    WINDOWS = "WINDOWS"
    ENVIRONMENT_LIMITED = "ENVIRONMENT-LIMITED"


class CheckStatus(StrEnum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    FAIL = "FAILED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"


class CheckMode(StrEnum):
    REAL = "REAL"
    FAKE = "FAKE"
    REPLAY = "REPLAY"


class ValidationProbe(StrEnum):
    PLATFORM = "platform"
    AUDIO_INPUT_DEVICE_DISCOVERY = "audio_input_device_discovery"
    AUDIO_OUTPUT_DEVICE_DISCOVERY = "audio_output_device_discovery"
    MICROPHONE_CAPTURE = "microphone_capture"
    PCM_FORMAT = "pcm_format"
    VAD_SYNTHETIC = "vad_synthetic"
    VAD_LIVE = "vad_live"
    WAKE_ACTIVATION = "wake_activation"
    STREAMING_ASR = "streaming_asr"
    TRANSCRIPT = "transcript"
    INTENT_CLASSIFICATION = "intent_classification"
    INTENT_FROM_TRANSCRIPT = "intent_from_transcript"
    TASK_ADMISSION_REPLAY = "task_admission_replay"
    TASK_ADMISSION = "task_admission"
    GEMINI_TOOL_BOUNDARY = "gemini_tool_boundary"
    GEMINI_CONNECTION = "gemini_live_connection"
    GEMINI_RESPONSE_STREAMING = "gemini_response_streaming"
    MICROPHONE_CANCELLATION = "microphone_cancellation"
    VAD_CANCELLATION = "vad_cancellation"
    ASR_CANCELLATION = "asr_cancellation"
    GEMINI_CONNECTION_CANCELLATION = "gemini_connection_cancellation"
    GEMINI_GENERATION_CANCELLATION = "gemini_generation_cancellation"
    TTS = "tts"
    TTS_CANCELLATION = "tts_cancellation"
    SPEAKER_PLAYBACK = "speaker_playback"
    PLAYBACK_CANCELLATION = "playback_cancellation"
    BARGE_IN = "barge_in"
    REPEATED_BARGE_IN = "repeated_barge_in"
    FULL_TURN_CANCELLATION = "full_turn_cancellation"
    RECONNECT = "reconnect"
    DEVICE_CLOSE_REOPEN = "device_close_reopen"
    DEVICE_LOSS_RECOVERY = "device_loss_recovery"
    END_TO_END_VOICE_TURN = "end_to_end_voice_turn"
    END_TO_END_TASK = "end_to_end_task"
    PRODUCTION_COMPOSITION_GATE = "production_composition_gate"
    SHUTDOWN_CLEANUP = "shutdown_cleanup"


class ValidationStage(StrEnum):
    """The 21 reportable stages; fine-grained probes are nested in each stage result."""

    PLATFORM = "platform"
    AUDIO_INPUT_DEVICE_DISCOVERY = "audio_input_device_discovery"
    AUDIO_OUTPUT_DEVICE_DISCOVERY = "audio_output_device_discovery"
    MICROPHONE_CAPTURE = "microphone_capture"
    PCM_FORMAT = "pcm_format"
    VAD = "vad"
    WAKE_ACTIVATION = "wake_activation"
    STREAMING_ASR = "streaming_asr"
    TRANSCRIPT = "transcript"
    INTENT_CLASSIFICATION = "intent_classification"
    TASK_ADMISSION = "task_admission"
    GEMINI_CONNECTION = "gemini_live_connection"
    GEMINI_RESPONSE_STREAMING = "gemini_response_streaming"
    TTS = "tts"
    SPEAKER_PLAYBACK = "speaker_playback"
    BARGE_IN = "barge_in"
    CANCELLATION = "cancellation"
    RECONNECT = "reconnect"
    DEVICE_LOSS_RECOVERY = "device_loss_recovery"
    END_TO_END_VOICE_TURN = "end_to_end_voice_turn"
    SHUTDOWN_CLEANUP = "shutdown_cleanup"


_STAGE_PROBES: Mapping[ValidationStage, tuple[ValidationProbe, ...]] = {
    ValidationStage.PLATFORM: (ValidationProbe.PLATFORM,),
    ValidationStage.AUDIO_INPUT_DEVICE_DISCOVERY: (ValidationProbe.AUDIO_INPUT_DEVICE_DISCOVERY,),
    ValidationStage.AUDIO_OUTPUT_DEVICE_DISCOVERY: (ValidationProbe.AUDIO_OUTPUT_DEVICE_DISCOVERY,),
    ValidationStage.MICROPHONE_CAPTURE: (ValidationProbe.MICROPHONE_CAPTURE,),
    ValidationStage.PCM_FORMAT: (ValidationProbe.PCM_FORMAT,),
    ValidationStage.VAD: (ValidationProbe.VAD_SYNTHETIC, ValidationProbe.VAD_LIVE),
    ValidationStage.WAKE_ACTIVATION: (ValidationProbe.WAKE_ACTIVATION,),
    ValidationStage.STREAMING_ASR: (ValidationProbe.STREAMING_ASR,),
    ValidationStage.TRANSCRIPT: (ValidationProbe.TRANSCRIPT,),
    ValidationStage.INTENT_CLASSIFICATION: (
        ValidationProbe.INTENT_CLASSIFICATION,
        ValidationProbe.INTENT_FROM_TRANSCRIPT,
    ),
    ValidationStage.TASK_ADMISSION: (
        ValidationProbe.TASK_ADMISSION_REPLAY,
        ValidationProbe.TASK_ADMISSION,
        ValidationProbe.END_TO_END_TASK,
        ValidationProbe.PRODUCTION_COMPOSITION_GATE,
    ),
    ValidationStage.GEMINI_CONNECTION: (
        ValidationProbe.GEMINI_TOOL_BOUNDARY,
        ValidationProbe.GEMINI_CONNECTION,
    ),
    ValidationStage.GEMINI_RESPONSE_STREAMING: (ValidationProbe.GEMINI_RESPONSE_STREAMING,),
    ValidationStage.TTS: (ValidationProbe.TTS,),
    ValidationStage.SPEAKER_PLAYBACK: (ValidationProbe.SPEAKER_PLAYBACK,),
    ValidationStage.BARGE_IN: (ValidationProbe.BARGE_IN, ValidationProbe.REPEATED_BARGE_IN),
    ValidationStage.CANCELLATION: (
        ValidationProbe.MICROPHONE_CANCELLATION,
        ValidationProbe.VAD_CANCELLATION,
        ValidationProbe.ASR_CANCELLATION,
        ValidationProbe.GEMINI_CONNECTION_CANCELLATION,
        ValidationProbe.GEMINI_GENERATION_CANCELLATION,
        ValidationProbe.TTS_CANCELLATION,
        ValidationProbe.PLAYBACK_CANCELLATION,
        ValidationProbe.FULL_TURN_CANCELLATION,
    ),
    ValidationStage.RECONNECT: (ValidationProbe.RECONNECT,),
    ValidationStage.DEVICE_LOSS_RECOVERY: (
        ValidationProbe.DEVICE_CLOSE_REOPEN,
        ValidationProbe.DEVICE_LOSS_RECOVERY,
    ),
    ValidationStage.END_TO_END_VOICE_TURN: (ValidationProbe.END_TO_END_VOICE_TURN,),
    ValidationStage.SHUTDOWN_CLEANUP: (ValidationProbe.SHUTDOWN_CLEANUP,),
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: ValidationStage | ValidationProbe
    status: CheckStatus
    mode: CheckMode | None
    duration_ms: float = 0.0
    details: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)
    error: str | None = None
    subchecks: tuple[CheckResult, ...] = ()

    def __post_init__(self) -> None:
        if not math.isfinite(self.duration_ms) or self.duration_ms < 0:
            raise ValueError("check duration must be finite and non-negative")
        if len(self.details) > MAX_REPORT_DETAILS:
            raise ValueError("check has too many detail fields")
        if self.error is not None and not _safe_code(self.error):
            raise ValueError("check error must be a sanitized uppercase code")
        for key, value in self.details.items():
            if not key or len(key) > 64:
                raise ValueError("check detail key must be short and non-empty")
            if not isinstance(value, (str, int, float, bool, type(None))):
                raise ValueError("check details must contain scalar values only")
            if isinstance(value, str) and len(value) > 128:
                raise ValueError("check detail string exceeds its configured limit")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("check detail value must be finite")
        if len(self.subchecks) > len(ValidationProbe):
            raise ValueError("check contains too many probe results")
        if any(subcheck.subchecks for subcheck in self.subchecks):
            raise ValueError("probe results cannot contain nested probe results")

    @property
    def stage(self) -> ValidationStage | ValidationProbe:
        """Compatibility accessor for the earlier staged report shape."""

        return self.id

    @property
    def reason_code(self) -> str:
        """Compatibility accessor; SKIPPED results have a reason in ``details``."""

        return self.error or str(self.details.get("reason", "CHECK_PASSED"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id.value,
            "status": self.status.value,
            "mode": self.mode.value if self.mode is not None else None,
            "duration_ms": round(self.duration_ms, 3),
            "details": dict(self.details),
            "error": self.error,
            "subchecks": [subcheck.to_dict() for subcheck in self.subchecks],
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    runtime: str
    environment_status: EnvironmentStatus
    timestamp: str
    platform_details: Mapping[str, str | int | float | bool | None]
    run_mode: str
    checks: tuple[CheckResult, ...]
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def overall(self) -> str:
        if any(
            check.status is CheckStatus.FAILED
            or any(subcheck.status is CheckStatus.FAILED for subcheck in check.subchecks)
            for check in self.checks
        ):
            return "FAILED"
        real_runtime_passed = any(
            check.id is not ValidationStage.PLATFORM
            and (
                (check.status is CheckStatus.PASS and check.mode is CheckMode.REAL)
                or any(
                    subcheck.status is CheckStatus.PASS and subcheck.mode is CheckMode.REAL
                    for subcheck in check.subchecks
                )
            )
            for check in self.checks
        )
        if not real_runtime_passed:
            return "BLOCKED"
        if any(
            check.status in {CheckStatus.PARTIAL, CheckStatus.SKIPPED, CheckStatus.BLOCKED}
            for check in self.checks
        ):
            return "PARTIAL"
        return "PASS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "runtime": self.runtime,
            "timestamp": self.timestamp,
            "overall": self.overall,
            "environment_status": self.environment_status.value,
            "run_mode": self.run_mode,
            "platform": dict(self.platform_details),
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)


class _CheckFailure(RuntimeError):
    def __init__(self, error_code: str, **details: str | int | float | bool | None) -> None:
        self.error_code = error_code
        self.details = details
        super().__init__(error_code)


class _CheckBlocked(RuntimeError):
    def __init__(self, reason: str, **details: str | int | float | bool | None) -> None:
        self.reason = reason
        self.details = details
        super().__init__(reason)


class _HubObserver(VoiceEventSink):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[VoiceEvent] = asyncio.Queue(maxsize=128)
        self.events: list[VoiceEvent] = []

    def emit(self, event: VoiceEvent) -> None:
        self.events.append(event)
        if len(self.events) > 256:
            del self.events[: len(self.events) - 256]
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)

    def count(self, kind: VoiceEventKind) -> int:
        return sum(event.kind is kind for event in self.events)

    def state_count(self, state: VoiceState) -> int:
        return sum(
            event.kind is VoiceEventKind.STATE_CHANGED and event.state is state
            for event in self.events
        )


class _GeminiTrafficBudget:
    """Hard aggregate limits across all optional Gemini probes in one harness run."""

    def __init__(self) -> None:
        self.session_attempts = 0
        self.input_audio_bytes = 0
        self.output_audio_bytes = 0
        self.transcript_characters = 0

    def open_session(self) -> None:
        if self.session_attempts >= MAX_GEMINI_SESSION_ATTEMPTS:
            raise _CheckBlocked("GEMINI_SESSION_ATTEMPT_BUDGET_EXHAUSTED")
        self.session_attempts += 1

    def add_input_audio(self, byte_count: int) -> None:
        if self.input_audio_bytes + byte_count > MAX_GEMINI_INPUT_AUDIO_BYTES:
            raise _CheckFailure(
                "GEMINI_INPUT_AUDIO_BUDGET_EXCEEDED",
                input_audio_bytes=self.input_audio_bytes,
                input_audio_limit_bytes=MAX_GEMINI_INPUT_AUDIO_BYTES,
            )
        self.input_audio_bytes += byte_count

    def add_output_audio(self, byte_count: int) -> None:
        if self.output_audio_bytes + byte_count > MAX_GEMINI_OUTPUT_AUDIO_BYTES:
            raise _CheckFailure(
                "GEMINI_OUTPUT_AUDIO_BUDGET_EXCEEDED",
                output_audio_bytes=self.output_audio_bytes,
                output_audio_limit_bytes=MAX_GEMINI_OUTPUT_AUDIO_BYTES,
            )
        self.output_audio_bytes += byte_count

    def add_transcript(self, character_count: int) -> None:
        if self.transcript_characters + character_count > MAX_GEMINI_TRANSCRIPT_CHARACTERS:
            raise _CheckFailure("GEMINI_TRANSCRIPT_BUDGET_EXCEEDED")
        self.transcript_characters += character_count

    def diagnostics(self) -> dict[str, int]:
        return {
            "session_attempts": self.session_attempts,
            "input_audio_bytes": self.input_audio_bytes,
            "input_audio_limit_bytes": MAX_GEMINI_INPUT_AUDIO_BYTES,
            "output_audio_bytes": self.output_audio_bytes,
            "output_audio_limit_bytes": MAX_GEMINI_OUTPUT_AUDIO_BYTES,
            "transcript_characters": self.transcript_characters,
            "session_attempt_limit": MAX_GEMINI_SESSION_ATTEMPTS,
        }


class _BudgetedLiveProvider(LiveConversationProvider):
    """Harness-only decorator that caps sessions and traffic without altering the provider port."""

    def __init__(self, provider: LiveConversationProvider, budget: _GeminiTrafficBudget) -> None:
        self.provider = provider
        self.budget = budget
        self.provider_id = str(getattr(provider, "provider_id", "unknown"))[:96]

    async def connect(self, config: LiveSessionConfig) -> LiveConversationSession:
        self.budget.open_session()
        session = await self.provider.connect(config)
        return _BudgetedLiveSession(session, self.budget)

    async def close(self) -> None:
        await self.provider.close()

    def diagnostics(self) -> dict[str, int]:
        native = _safe_diagnostics(self.provider)
        return {**native, **self.budget.diagnostics()}


class _BudgetedLiveSession(LiveConversationSession):
    def __init__(self, session: LiveConversationSession, budget: _GeminiTrafficBudget) -> None:
        self.session = session
        self.budget = budget

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.budget.add_input_audio(len(chunk.data))
        await self.session.send_audio(chunk)

    async def interrupt(self, first_user_audio: AudioChunk) -> int:
        self.budget.add_input_audio(len(first_user_audio.data))
        return await self.session.interrupt(first_user_audio)

    async def send_text(self, text: str) -> None:
        self.budget.add_transcript(len(text))
        await self.session.send_text(text)

    async def send_tool_response(self, call: Any, response: Mapping[str, Any]) -> None:
        await self.session.send_tool_response(call, response)

    async def receive(self) -> AsyncIterator[LiveEvent]:
        async for event in self.session.receive():
            if event.type is LiveEventType.OUTPUT_AUDIO and event.audio is not None:
                self.budget.add_output_audio(len(event.audio.data))
            if (
                event.type
                in {
                    LiveEventType.INPUT_TRANSCRIPT,
                    LiveEventType.OUTPUT_TRANSCRIPT,
                    LiveEventType.OUTPUT_AUDIO,
                }
                and event.text
            ):
                self.budget.add_transcript(len(event.text))
            yield event

    async def close(self) -> None:
        await self.session.close()


class _PlaybackProbe(AudioPlaybackPort):
    """Content-free measurement wrapper around the production playback port."""

    def __init__(self, playback: AudioPlaybackPort) -> None:
        self.playback = playback
        self.play_calls = 0
        self.active_calls = 0
        self.stop_calls = 0
        self.last_stop_latency_ms: float | None = None
        self.close_completed = False

    async def list_devices(self) -> Sequence[AudioDevice]:
        list_devices = getattr(self.playback, "list_devices", None)
        if not callable(list_devices):
            raise _CheckBlocked("OUTPUT_DEVICE_DISCOVERY_NOT_SUPPORTED")
        return await list_devices()

    async def play(self, chunk: AudioChunk) -> None:
        self.active_calls += 1
        try:
            await self.playback.play(chunk)
        finally:
            self.active_calls = max(0, self.active_calls - 1)
        self.play_calls += 1

    async def stop(self) -> None:
        started = time.perf_counter_ns()
        await self.playback.stop()
        self.last_stop_latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        self.stop_calls += 1

    async def close(self) -> None:
        await self.playback.close()
        self.close_completed = True

    def diagnostics(self) -> dict[str, Any]:
        diagnostics = getattr(self.playback, "diagnostics", None)
        native = diagnostics() if callable(diagnostics) else {}
        return {
            **native,
            "probe_play_calls": self.play_calls,
            "probe_active_calls": self.active_calls,
            "probe_stop_calls": self.stop_calls,
            "probe_last_stop_latency_ms": self.last_stop_latency_ms,
            "probe_close_completed": self.close_completed,
        }


class _ObservedSpeechRecognizer(SpeechRecognitionPort):
    """Forward stream results while retaining counts/timing only, never transcript text."""

    def __init__(self, recognizer: SpeechRecognitionPort) -> None:
        self.recognizer = recognizer
        self.partial_count = 0
        self.final_count = 0
        self.final_character_count = 0
        self.first_partial_ms: float | None = None
        self.first_final_ms: float | None = None
        self._started: float | None = None

    def transcribe(self, audio: AsyncIterator[AudioChunk], **kwargs: Any) -> AsyncIterator[Any]:
        async def observed() -> AsyncIterator[Any]:
            self._started = time.perf_counter()
            async for segment in self.recognizer.transcribe(audio, **kwargs):
                elapsed_ms = (time.perf_counter() - self._started) * 1000
                if segment.is_final and segment.text.strip():
                    self.final_count += 1
                    self.final_character_count += len(segment.text)
                    if self.first_final_ms is None:
                        self.first_final_ms = elapsed_ms
                elif segment.text.strip():
                    self.partial_count += 1
                    if self.first_partial_ms is None:
                        self.first_partial_ms = elapsed_ms
                yield segment

        return observed()

    def diagnostics(self) -> Mapping[str, int]:
        diagnostics = getattr(self.recognizer, "diagnostics", None)
        return diagnostics() if callable(diagnostics) else {}


class _AdmissionReplayTasks:
    """Explicit fake task port used only by the REPLAY admission check."""

    def __init__(self) -> None:
        self.submissions = 0

    async def submit(self, request: Any, *, principal_id: str) -> Any:
        del request, principal_id
        self.submissions += 1
        return SimpleNamespace(
            task_id="replay-task",
            status=SimpleNamespace(value="queued"),
        )


Prompt = Callable[[str], Awaitable[None]]
Notifier = Callable[[str], None]
MicrophoneFactory = Callable[[], MicrophonePort]
PlaybackFactory = Callable[[], AudioPlaybackPort]
AdapterFactory = Callable[[], Any]
ProviderFactory = Callable[[], LiveConversationProvider]


class WindowsVoiceValidationHarness:
    """Staged validator over ARISE's existing typed audio/provider ports.

    ``mode=REAL`` is reserved for the CLI on Windows. Injected adapters must explicitly use
    ``FAKE`` or ``REPLAY`` and can never be promoted into a real-hardware report.
    """

    def __init__(
        self,
        *,
        microphone_factory: MicrophoneFactory | None = None,
        playback_factory: PlaybackFactory | None = None,
        vad_factory: AdapterFactory | None = None,
        wake_factory: AdapterFactory | None = None,
        asr_factory: AdapterFactory | None = None,
        tts_factory: AdapterFactory | None = None,
        provider_factory: ProviderFactory | None = None,
        wake_word: str = "ARISE",
        locale: str = "en-US",
        input_device_id: str | None = None,
        output_device_id: str | None = None,
        microphone_confirmed: bool = False,
        playback_confirmed: bool = False,
        enable_live_gemini: bool = False,
        cloud_confirmed: bool = False,
        voice_cloud_opt_in: bool = False,
        security_cloud_opt_in: bool = False,
        gemini_block_reason: str = "GEMINI_CREDENTIAL_CONFIGURATION_REQUIRED",
        vad_block_reason: str = "WEBRTC_VAD_CONFIGURATION_REQUIRED",
        wake_block_reason: str = "VOSK_WAKE_MODEL_REQUIRED",
        asr_block_reason: str = "VOSK_ASR_MODEL_REQUIRED",
        tts_block_reason: str = "KOKORO_TTS_MODEL_REQUIRED",
        capture_seconds: float = 6.0,
        stage_timeout_seconds: float = 45.0,
        mode: CheckMode = CheckMode.FAKE,
        prompt: Prompt | None = None,
        notify: Notifier | None = None,
        host_platform_override: str | None = None,
        run_host_synthetic_vad: bool = False,
    ) -> None:
        if not 2 <= capture_seconds <= 30:
            raise ValueError("capture duration must be between 2 and 30 seconds")
        if not 1 <= stage_timeout_seconds <= 180:
            raise ValueError("stage timeout must be between one and 180 seconds")
        if not wake_word.strip() or len(wake_word) > 32:
            raise ValueError("wake word must contain one to 32 characters")
        if not locale.strip() or len(locale) > 32:
            raise ValueError("locale must contain one to 32 characters")
        if mode not in set(CheckMode):
            raise ValueError("validation mode must be REAL, FAKE, or REPLAY")
        self.microphone_factory = microphone_factory
        self.playback_factory = playback_factory
        self.vad_factory = vad_factory
        self.wake_factory = wake_factory
        self.asr_factory = asr_factory
        self.tts_factory = tts_factory
        self.provider_factory = provider_factory
        self.wake_word = wake_word.strip()
        self.locale = locale
        self.input_device_id = input_device_id
        self.output_device_id = output_device_id
        self.microphone_confirmed = microphone_confirmed
        self.playback_confirmed = playback_confirmed
        self.enable_live_gemini = enable_live_gemini
        self.cloud_confirmed = cloud_confirmed
        self.voice_cloud_opt_in = voice_cloud_opt_in
        self.security_cloud_opt_in = security_cloud_opt_in
        self.gemini_block_reason = _normalise_code(gemini_block_reason)
        self.vad_block_reason = _normalise_code(vad_block_reason)
        self.wake_block_reason = _normalise_code(wake_block_reason)
        self.asr_block_reason = _normalise_code(asr_block_reason)
        self.tts_block_reason = _normalise_code(tts_block_reason)
        self.capture_seconds = capture_seconds
        self.stage_timeout_seconds = stage_timeout_seconds
        self.mode = mode
        self.prompt = prompt
        self.notify = notify
        self.run_host_synthetic_vad = run_host_synthetic_vad

        self.host_platform = host_platform_override or platform.system()
        self.is_windows_host = sys.platform == "win32" and self.host_platform.casefold().startswith(
            "win"
        )
        self.runtime = _runtime_name(self.host_platform)
        self.platform_details = _platform_snapshot(self.host_platform)
        self._checks: dict[ValidationProbe, CheckResult] = {}
        self._gemini_budget = _GeminiTrafficBudget()
        self.microphone: MicrophonePort | None = None
        self.playback: _PlaybackProbe | None = None
        self.provider: LiveConversationProvider | None = None
        self.gemini_session: LiveConversationSession | None = None
        self.active_hub: AudioHub | None = None
        self.input_devices: tuple[AudioDevice, ...] = ()
        self.output_devices: tuple[AudioDevice, ...] = ()
        self.selected_input: AudioDevice | None = None
        self.selected_output: AudioDevice | None = None
        self.captured_audio: list[AudioChunk] = []
        self.wake_detection: WakeWordDetection | None = None
        self.asr: SpeechRecognitionPort | None = None
        self.tts: SpeechSynthesisPort | None = None
        self.first_tts_chunk: AudioChunk | None = None
        self.canonical_transcript: str | None = None
        self._observed_asr: _ObservedSpeechRecognizer | None = None
        self._hub_observer: _HubObserver | None = None
        self._resources_closed = False

    async def run(self) -> ValidationReport:
        timestamp = datetime.now(UTC).isoformat()
        for check_id in ValidationProbe:
            self._set(
                check_id,
                CheckStatus.SKIPPED,
                None,
                details={"reason": "NOT_RUN"},
            )

        await self._run_check(ValidationProbe.PLATFORM, self._platform_check, mode=CheckMode.REAL)
        await self._run_deterministic_checks()
        self._set(
            ValidationProbe.TASK_ADMISSION,
            CheckStatus.BLOCKED,
            None,
            error="PRODUCTION_VOICE_TASK_BRIDGE_NOT_COMPOSED",
            details={"task_engine_submitted": False, "reason": "PRODUCTION_COMPOSITION_CLOSED"},
        )
        self._set(
            ValidationProbe.END_TO_END_TASK,
            CheckStatus.BLOCKED,
            None,
            error="NO_REGISTERED_SAFE_ACTION_TOOL",
            details={"task_engine_invoked": False, "verified_task_completion": False},
        )
        self._set(
            ValidationProbe.PRODUCTION_COMPOSITION_GATE,
            CheckStatus.BLOCKED,
            None,
            error="VOICE_RUNTIME_NOT_REGISTERED_OR_WINDOWS_VALIDATED",
            details={
                "default_server_voice_registered": False,
                "voice_task_bridge_registered": False,
                "capability_available": False,
            },
        )

        if self.mode is CheckMode.REAL and not self.is_windows_host:
            if self.run_host_synthetic_vad:
                await self._run_vad_synthetic()
            else:
                self._block(
                    ValidationProbe.VAD_SYNTHETIC,
                    "SYNTHETIC_VAD_ADAPTER_NOT_CONFIGURED",
                    CheckMode.REPLAY,
                )
            self._block_live_checks("WINDOWS_HOST_REQUIRED")
            self._set(
                ValidationProbe.SHUTDOWN_CLEANUP,
                CheckStatus.SKIPPED,
                None,
                details={"reason": "NO_HARDWARE_RESOURCES_OPENED"},
            )
            return self._report(timestamp, run_mode="HOST_GUARDED")

        baseline_tasks = {id(task) for task in asyncio.all_tasks()}
        try:
            await self._run_check(
                ValidationProbe.AUDIO_INPUT_DEVICE_DISCOVERY,
                self._discover_input_devices,
                mode=self.mode,
            )
            await self._run_check(
                ValidationProbe.AUDIO_OUTPUT_DEVICE_DISCOVERY,
                self._discover_output_devices,
                mode=self.mode,
            )
            await self._run_microphone_capture()
            await self._run_check(
                ValidationProbe.PCM_FORMAT, self._validate_pcm_format, mode=self.mode
            )
            await self._run_vad_synthetic()
            await self._run_vad_live()
            await self._run_wake_activation()
            await self._run_streaming_asr()
            await self._run_transcript_check()
            await self._run_intent_from_transcript()
            await self._run_microphone_cancellation()
            await self._run_vad_cancellation()
            await self._run_asr_cancellation()
            await self._run_gemini_connection()
            await self._run_gemini_response_streaming()
            await self._run_gemini_connection_cancellation()
            await self._run_tts()
            await self._run_tts_cancellation()
            await self._run_speaker_playback()
            await self._run_playback_cancellation()
            await self._run_audiohub_end_to_end()
            await self._run_reconnect_probe()
            await self._run_device_close_reopen()
            self._set(
                ValidationProbe.DEVICE_LOSS_RECOVERY,
                CheckStatus.BLOCKED,
                self.mode,
                error="DEVICE_LOSS_NOT_INJECTED",
                details={
                    "microphone_disconnection_injected": False,
                    "speaker_disconnection_injected": False,
                    "clean_close_reopen_is_not_device_recovery": True,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._set(
                ValidationProbe.PRODUCTION_COMPOSITION_GATE,
                CheckStatus.FAIL,
                self.mode,
                error="HARNESS_ORCHESTRATION_FAILED",
                details={"raw_exception_emitted": False},
            )
        finally:
            await self._shutdown_cleanup(baseline_tasks)
            self.captured_audio.clear()
            self.wake_detection = None
            self.canonical_transcript = None
            self.first_tts_chunk = None
        return self._report(timestamp, run_mode=self.mode.value)

    async def _run_deterministic_checks(self) -> None:
        await self._run_check(
            ValidationProbe.INTENT_CLASSIFICATION,
            self._verify_intent_examples,
            mode=CheckMode.REPLAY,
        )
        await self._run_check(
            ValidationProbe.TASK_ADMISSION_REPLAY,
            self._verify_task_admission_replay,
            mode=CheckMode.FAKE,
        )
        await self._run_check(
            ValidationProbe.GEMINI_TOOL_BOUNDARY,
            self._verify_gemini_tool_boundary,
            mode=CheckMode.REPLAY,
        )

    async def _platform_check(self) -> Mapping[str, str | int | float | bool | None]:
        is_windows = self.is_windows_host
        return {
            "os_name": self.host_platform,
            "os_version": str(platform.version())[:128],
            "architecture": str(platform.machine())[:64],
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "target_is_windows": is_windows,
            "sounddevice_installed": bool(self.platform_details["sounddevice_installed"]),
            "portaudio_runtime_probed": False,
            "audio_backend": "PortAudio"
            if self.platform_details["sounddevice_installed"]
            else None,
        }

    async def _verify_intent_examples(self) -> Mapping[str, str | int | float | bool | None]:
        classifier = IntentClassifier()
        question = classifier.classify("Tell me how to open Chrome.")
        command = classifier.classify("Open Chrome.")
        ambiguous = classifier.classify("Chrome.")
        passed = (
            question.kind is IntentKind.QUESTION
            and not question.may_require_runtime_task
            and command.kind is IntentKind.COMMAND
            and command.may_require_runtime_task
            and ambiguous.kind
            in {IntentKind.CLARIFICATION, IntentKind.CASUAL_CONVERSATION, IntentKind.QUESTION}
            and not ambiguous.may_require_runtime_task
        )
        if not passed:
            raise _CheckFailure("INTENT_EXPECTATION_MISMATCH")
        return {
            "question_kind": question.kind.value,
            "question_task_intent": question.may_require_runtime_task,
            "command_kind": command.kind.value,
            "command_task_intent": command.may_require_runtime_task,
            "ambiguous_kind": ambiguous.kind.value,
            "ambiguous_task_intent": ambiguous.may_require_runtime_task,
        }

    async def _verify_task_admission_replay(self) -> Mapping[str, str | int | float | bool | None]:
        tasks = _AdmissionReplayTasks()
        bridge = VoiceConversationBridge(tasks)

        async def execute(text: str, call_id: str) -> Mapping[str, Any]:
            from arise.core.voice import LiveToolCall

            return await bridge.handle_tool_call(
                LiveToolCall(call_id, "execute_task", {"text": text}),
                principal_id="validation-user",
                session_id="voice-validation-replay",
                user_text=text,
            )

        question = await execute("Tell me how to open Chrome.", "question")
        command = await execute("Open Chrome.", "command")
        ambiguous = await execute("Chrome.", "ambiguous")
        if (
            question.get("status") != "not_authorized"
            or command.get("status") != "accepted"
            or ambiguous.get("status") != "not_authorized"
            or tasks.submissions != 1
        ):
            raise _CheckFailure("VOICE_ADMISSION_REPLAY_FAILED")
        return {
            "mode_is_fake": True,
            "question_no_admission": True,
            "command_admitted_to_fake_port": True,
            "ambiguous_no_admission": True,
            "fake_submissions": tasks.submissions,
            "execution_performed": False,
            "verified_task_completion": False,
        }

    async def _verify_gemini_tool_boundary(self) -> Mapping[str, str | int | float | bool | None]:
        allowed = {
            "execute_task",
            "ask_user",
            "request_clarification",
            "get_task_status",
            "report_status",
            "cancel_task",
        }
        declarations = voice_tool_declarations()
        names = {item.get("name") for item in declarations}
        if names != allowed or len(declarations) != len(allowed):
            raise _CheckFailure("VOICE_TOOL_ALLOWLIST_MISMATCH")
        if any(
            name in {"shell", "powershell", "run_python", "delete_file", "click"} for name in names
        ):
            raise _CheckFailure("ARBITRARY_TOOL_DECLARED")
        return {
            "declared_tool_count": len(names),
            "only_allowlisted_arise_operations": True,
            "arbitrary_shell_filesystem_or_python_tools": False,
            "live_provider_tools_in_diagnostic_session": 0,
        }

    async def _discover_input_devices(self) -> Mapping[str, str | int | float | bool | None]:
        if self.microphone_factory is None:
            raise _CheckBlocked("MICROPHONE_ADAPTER_NOT_CONFIGURED")
        if self.mode is CheckMode.REAL and not self.platform_details["sounddevice_installed"]:
            raise _CheckBlocked("SOUNDDEVICE_NOT_INSTALLED")
        self.microphone = self.microphone_factory()
        try:
            self.input_devices = tuple(await self.microphone.list_devices())
        except MicrophonePermissionDenied:
            raise _CheckBlocked("MICROPHONE_PERMISSION_DENIED") from None
        except MicrophoneUnavailable as exc:
            raise _mapped_blocker(exc.error_code) from None
        if not self.input_devices:
            raise _CheckFailure("NO_INPUT_DEVICE")
        self.selected_input = self._select_device(self.input_devices, self.input_device_id)
        if self.selected_input is None:
            raise _CheckFailure("REQUESTED_INPUT_DEVICE_NOT_FOUND")
        self._update_portaudio_details()
        return {
            "input_device_count": len(self.input_devices),
            "default_input_count": sum(device.is_default for device in self.input_devices),
            "selected_input_index": _device_index(self.selected_input.device_id),
        }

    async def _discover_output_devices(self) -> Mapping[str, str | int | float | bool | None]:
        if self.playback_factory is None:
            raise _CheckBlocked("PLAYBACK_ADAPTER_NOT_CONFIGURED")
        if self.mode is CheckMode.REAL and not self.platform_details["sounddevice_installed"]:
            raise _CheckBlocked("SOUNDDEVICE_NOT_INSTALLED")
        raw_playback = self.playback_factory()
        self.playback = (
            raw_playback
            if isinstance(raw_playback, _PlaybackProbe)
            else _PlaybackProbe(raw_playback)
        )
        try:
            self.output_devices = tuple(await self.playback.list_devices())
        except Exception as exc:
            raise _raise_mapped_failure(exc, output=True) from None
        if not self.output_devices:
            raise _CheckFailure("NO_OUTPUT_DEVICE")
        self.selected_output = self._select_device(self.output_devices, self.output_device_id)
        if self.selected_output is None:
            raise _CheckFailure("REQUESTED_OUTPUT_DEVICE_NOT_FOUND")
        self._update_portaudio_details()
        return {
            "output_device_count": len(self.output_devices),
            "default_output_count": sum(device.is_default for device in self.output_devices),
            "selected_output_index": _device_index(self.selected_output.device_id),
        }

    async def _run_microphone_capture(self) -> None:
        if not self.microphone_confirmed:
            self._set(
                ValidationProbe.MICROPHONE_CAPTURE,
                CheckStatus.SKIPPED,
                None,
                details={"reason": "MICROPHONE_CONSENT_NOT_GIVEN"},
            )
            return
        if self.selected_input is None or self.microphone is None:
            self._block(ValidationProbe.MICROPHONE_CAPTURE, "INPUT_DEVICE_NOT_AVAILABLE", self.mode)
            return
        await self._run_check(
            ValidationProbe.MICROPHONE_CAPTURE, self._capture_test_audio, mode=self.mode
        )

    async def _capture_test_audio(self) -> Mapping[str, str | int | float | bool | None]:
        await self._ask(
            "Microphone check: after Enter, say the wake word, pause, then say "
            '"Tell me how to open Chrome." Use a non-sensitive voice. Capture remains local.'
        )
        self.captured_audio = await self._capture_from(
            self.microphone, self.selected_input.device_id, self.capture_seconds
        )
        sample_count, rms, peak, nonzero_samples = _pcm_level(self.captured_audio)
        captured_seconds = _chunks_duration(self.captured_audio)
        diagnostics = _safe_diagnostics(self.microphone)
        if diagnostics.get("capture_active") is True:
            raise _CheckFailure("MICROPHONE_STREAM_ACTIVE_AFTER_CAPTURE")
        if sample_count <= 0 or nonzero_samples == 0:
            raise _CheckFailure("MICROPHONE_SILENT_OR_EMPTY")
        if self.mode is CheckMode.REAL and captured_seconds < self.capture_seconds * 0.75:
            raise _CheckFailure(
                "MICROPHONE_CAPTURE_TOO_SHORT",
                captured_duration_ms=round(captured_seconds * 1000, 1),
            )
        return {
            "chunks": len(self.captured_audio),
            "sample_count": sample_count,
            "captured_duration_ms": round(captured_seconds * 1000, 1),
            "rms_amplitude": round(rms, 2),
            "peak_amplitude": peak,
            "nonzero_sample_count": nonzero_samples,
            "device_input_sample_rate_hz": diagnostics.get("selected_sample_rate_hz"),
            "device_input_channels": diagnostics.get("selected_channels"),
            "normalized_sample_rate_hz": self.captured_audio[0].sample_rate_hz,
            "normalized_channels": self.captured_audio[0].channels,
            "capture_stream_active_after_capture": diagnostics.get("capture_active"),
            "capture_queue_drops": _int_or_zero(diagnostics.get("capture_queue_drops")),
            "capture_input_overflows": _int_or_zero(diagnostics.get("capture_input_overflows")),
            "capture_reconnects": _int_or_zero(diagnostics.get("capture_reconnects")),
            "audio_persisted": False,
        }

    async def _capture_from(
        self, microphone: MicrophonePort | None, device_id: str, duration_seconds: float
    ) -> list[AudioChunk]:
        if microphone is None:
            raise _CheckBlocked("MICROPHONE_ADAPTER_NOT_CONFIGURED")
        source = microphone.capture(device_id)
        chunks: list[AudioChunk] = []
        total_bytes = 0
        deadline = time.monotonic() + duration_seconds

        async def collect() -> None:
            nonlocal total_bytes
            async with _aclosing_async_iterator(source):
                async for chunk in source:
                    chunks.append(chunk)
                    total_bytes += len(chunk.data)
                    if len(chunks) > MAX_CAPTURE_CHUNKS or total_bytes > MAX_CAPTURE_BYTES:
                        raise _CheckFailure("CAPTURE_BUFFER_LIMIT_EXCEEDED")
                    if time.monotonic() >= deadline:
                        return

        try:
            await asyncio.wait_for(collect(), timeout=duration_seconds + 3.0)
        except TimeoutError:
            if not chunks:
                raise _CheckFailure("MICROPHONE_CAPTURE_TIMEOUT") from None
        if not chunks:
            raise _CheckFailure("MICROPHONE_CAPTURE_EMPTY")
        return chunks

    def _validate_pcm_format(self) -> Mapping[str, str | int | float | bool | None]:
        if not self.captured_audio:
            raise _CheckBlocked("MICROPHONE_CAPTURE_NOT_AVAILABLE")
        supported = all(
            chunk.codec in {"pcm_s16le", "pcm16"}
            and chunk.sample_rate_hz == 16_000
            and chunk.channels == 1
            and len(chunk.data) % 2 == 0
            for chunk in self.captured_audio
        )
        if not supported:
            raise _CheckFailure("PCM_FORMAT_NOT_16KHZ_MONO_S16LE")
        return {
            "codec": "pcm_s16le",
            "sample_rate_hz": 16_000,
            "channels": 1,
            "chunk_count": len(self.captured_audio),
            "frame_alignment_valid": True,
        }

    async def _run_vad_synthetic(self) -> None:
        if self.vad_factory is None:
            self._block(ValidationProbe.VAD_SYNTHETIC, self.vad_block_reason, CheckMode.REPLAY)
            return
        if self.mode is CheckMode.REAL and not self.platform_details["webrtcvad_installed"]:
            self._block(ValidationProbe.VAD_SYNTHETIC, "WEBRTC_VAD_NOT_INSTALLED", CheckMode.REPLAY)
            return
        await self._run_check(
            ValidationProbe.VAD_SYNTHETIC,
            self._check_vad_synthetic_fixtures,
            mode=CheckMode.REPLAY if self.mode is CheckMode.REAL else self.mode,
        )

    async def _check_vad_synthetic_fixtures(self) -> Mapping[str, str | int | float | bool | None]:
        vad = self.vad_factory()
        sequence = _vad_synthetic_fixture()
        flags = [bool((await vad.analyze(chunk)).speech) for chunk in sequence]
        silence_prefix = flags[:6]
        voiced = flags[6:26]
        silence_suffix = flags[26:]
        transitions = sum(left != right for left, right in zip(flags, flags[1:], strict=False))
        trailing_silence_rejected = any(not flag for flag in silence_suffix) and not any(
            silence_suffix[-4:]
        )
        if (
            any(silence_prefix)
            or not any(voiced)
            or not trailing_silence_rejected
            or transitions < 2
        ):
            raise _CheckFailure(
                "VAD_SYNTHETIC_FIXTURE_EXPECTATIONS_FAILED",
                silence_rejected=not any(silence_prefix),
                voice_like_fixture_detected=any(voiced),
                silence_after_speech_rejected=trailing_silence_rejected,
                transition_count=transitions,
            )
        diagnostics = _safe_diagnostics(vad)
        return {
            "fixture": "synthetic_harmonic_voice_like_pcm",
            "silence_rejected": True,
            "voice_like_fixture_detected": True,
            "silence_after_speech_rejected": True,
            "transition_count": transitions,
            "vad_frame_count": _int_or_zero(diagnostics.get("vad_frames")),
        }

    async def _run_vad_live(self) -> None:
        if not self.captured_audio:
            self._block(ValidationProbe.VAD_LIVE, "MICROPHONE_CAPTURE_NOT_AVAILABLE", self.mode)
            return
        if self.vad_factory is None:
            self._block(ValidationProbe.VAD_LIVE, self.vad_block_reason, self.mode)
            return
        await self._run_check(ValidationProbe.VAD_LIVE, self._check_live_vad, mode=self.mode)

    async def _check_live_vad(self) -> Mapping[str, str | int | float | bool | None]:
        vad: VoiceActivityDetectorPort = self.vad_factory()
        flags: list[bool] = []
        for chunk in self.captured_audio:
            flags.append(bool((await vad.analyze(chunk)).speech))
        speech_chunks = sum(flags)
        silence_chunks = len(flags) - speech_chunks
        transitions = sum(left != right for left, right in zip(flags, flags[1:], strict=False))
        if not speech_chunks:
            raise _CheckFailure("VAD_LIVE_SPEECH_NOT_DETECTED")
        if not silence_chunks or transitions < 1:
            raise _CheckFailure(
                "VAD_LIVE_SILENCE_OR_TRANSITION_NOT_OBSERVED",
                speech_chunks=speech_chunks,
                silence_chunks=silence_chunks,
                transition_count=transitions,
            )
        return {
            "chunks_analyzed": len(flags),
            "speech_chunks": speech_chunks,
            "silence_chunks": silence_chunks,
            "transition_count": transitions,
            "vad_frames": _int_or_zero(_safe_diagnostics(vad).get("vad_frames")),
        }

    async def _run_wake_activation(self) -> None:
        if not self.captured_audio:
            self._block(
                ValidationProbe.WAKE_ACTIVATION, "MICROPHONE_CAPTURE_NOT_AVAILABLE", self.mode
            )
            return
        if self.wake_factory is None:
            self._block(ValidationProbe.WAKE_ACTIVATION, self.wake_block_reason, self.mode)
            return
        await self._run_check(
            ValidationProbe.WAKE_ACTIVATION, self._check_wake_activation, mode=self.mode
        )

    async def _check_wake_activation(self) -> Mapping[str, str | int | float | bool | None]:
        detector: WakeWordDetectorPort = self.wake_factory()
        detection: WakeWordDetection | None = None
        for chunk in self.captured_audio:
            candidate = await detector.accept(chunk)
            if candidate.matched:
                detection = candidate
                break
        if detection is None:
            detection = await detector.end_utterance()
        if not detection.matched:
            raise _CheckFailure("WAKE_WORD_NOT_DETECTED")
        if not detection.activation_audio:
            raise _CheckFailure("WAKE_ACTIVATION_AUDIO_EMPTY")
        self.wake_detection = detection
        return {
            "wake_detected": True,
            "confidence": round(detection.confidence, 3),
            "activation_audio_chunks": len(detection.activation_audio),
            "activation_audio_bytes": sum(len(chunk.data) for chunk in detection.activation_audio),
            "pre_wake_audio_forwarded": False,
        }

    async def _run_streaming_asr(self) -> None:
        if not self.wake_detection or not self.wake_detection.activation_audio:
            self._block(ValidationProbe.STREAMING_ASR, "LOCAL_WAKE_ACTIVATION_REQUIRED", self.mode)
            return
        if self.asr_factory is None:
            self._block(ValidationProbe.STREAMING_ASR, self.asr_block_reason, self.mode)
            return
        await self._run_check(
            ValidationProbe.STREAMING_ASR,
            self._transcribe_wake_audio,
            mode=CheckMode.REPLAY if self.mode is CheckMode.REAL else self.mode,
        )

    async def _transcribe_wake_audio(self) -> Mapping[str, str | int | float | bool | None]:
        self.asr = self.asr_factory()
        chunks = self.wake_detection.activation_audio if self.wake_detection else ()
        started = time.perf_counter()
        first_partial_ms: float | None = None
        first_final_ms: float | None = None
        partial_count = 0
        final_count = 0
        confident_final: str | None = None
        final_confidence = 0.0
        stream = self.asr.transcribe(
            self._replay_audio(chunks),
            locale=self.locale,
            correlation_id=f"voice-check-{uuid.uuid4().hex}",
        )
        async with _aclosing_async_iterator(stream):
            async for segment in stream:
                elapsed_ms = (time.perf_counter() - started) * 1000
                if segment.is_final and segment.text.strip():
                    final_count += 1
                    if first_final_ms is None:
                        first_final_ms = elapsed_ms
                    if segment.confidence >= 0.65:
                        confident_final = segment.text.strip()
                        final_confidence = segment.confidence
                elif segment.text.strip():
                    partial_count += 1
                    if first_partial_ms is None:
                        first_partial_ms = elapsed_ms
        if first_partial_ms is None:
            raise _CheckFailure(
                "ASR_FIRST_PARTIAL_NOT_RECEIVED",
                final_segments=final_count,
            )
        if confident_final is None:
            raise _CheckFailure(
                "ASR_CONFIDENT_FINAL_NOT_RECEIVED",
                partial_segments=partial_count,
                final_segments=final_count,
            )
        self.canonical_transcript = confident_final
        diagnostics = _safe_diagnostics(self.asr)
        return {
            "time_to_first_partial_ms": round(first_partial_ms, 1),
            "time_to_final_ms": round(first_final_ms or 0.0, 1),
            "stream_duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "partial_segments": partial_count,
            "final_segments": final_count,
            "final_character_count": len(confident_final),
            "final_confidence": round(final_confidence, 3),
            "asr_active_streams_after_final": _int_or_zero(diagnostics.get("asr_active_streams")),
            "transcript_text_emitted": False,
        }

    async def _replay_audio(self, chunks: Sequence[AudioChunk]) -> AsyncIterator[AudioChunk]:
        previous_timestamp: int | None = None
        for chunk in chunks:
            if self.mode is CheckMode.REAL and previous_timestamp is not None:
                delta = (chunk.captured_at_monotonic_ns - previous_timestamp) / 1_000_000_000
                if 0 < delta <= 0.1:
                    await asyncio.sleep(delta)
            previous_timestamp = chunk.captured_at_monotonic_ns
            yield chunk

    async def _run_transcript_check(self) -> None:
        if not self.canonical_transcript:
            self._block(ValidationProbe.TRANSCRIPT, "FINAL_TRANSCRIPT_NOT_AVAILABLE", self.mode)
            return
        await self._run_check(
            ValidationProbe.TRANSCRIPT,
            self._validate_transcript,
            mode=CheckMode.REPLAY if self.mode is CheckMode.REAL else self.mode,
        )

    def _validate_transcript(self) -> Mapping[str, str | int | float | bool | None]:
        text = self.canonical_transcript or ""
        if not text.strip():
            raise _CheckFailure("FINAL_TRANSCRIPT_EMPTY")
        return {
            "final_transcript_present": True,
            "character_count": len(text),
            "confidence_gate_passed": True,
            "transcript_text_emitted": False,
        }

    async def _run_intent_from_transcript(self) -> None:
        if not self.canonical_transcript:
            self._block(
                ValidationProbe.INTENT_FROM_TRANSCRIPT,
                "FINAL_TRANSCRIPT_NOT_AVAILABLE",
                self.mode,
            )
            return
        await self._run_check(
            ValidationProbe.INTENT_FROM_TRANSCRIPT,
            self._classify_transcript,
            mode=CheckMode.REPLAY if self.mode is CheckMode.REAL else self.mode,
        )

    def _classify_transcript(self) -> Mapping[str, str | int | float | bool | None]:
        classification = IntentClassifier().classify(self.canonical_transcript or "")
        if (
            classification.kind is not IntentKind.QUESTION
            or classification.may_require_runtime_task
        ):
            raise _CheckFailure(
                "SPOKEN_INFORMATION_REQUEST_NOT_CLASSIFIED_AS_QUESTION",
                intent=classification.kind.value,
                confidence=round(classification.confidence, 3),
            )
        return {
            "intent": classification.kind.value,
            "confidence": round(classification.confidence, 3),
            "task_admission_permitted": False,
            "canonical_transcript_emitted": False,
        }

    async def _run_microphone_cancellation(self) -> None:
        if not self.microphone_confirmed:
            self._skip(ValidationProbe.MICROPHONE_CANCELLATION, "MICROPHONE_CONSENT_NOT_GIVEN")
            return
        if self.microphone is None or self.selected_input is None:
            self._block(
                ValidationProbe.MICROPHONE_CANCELLATION,
                "INPUT_DEVICE_NOT_AVAILABLE",
                self.mode,
            )
            return
        await self._run_check(
            ValidationProbe.MICROPHONE_CANCELLATION,
            self._cancel_microphone_capture,
            mode=self.mode,
        )

    async def _cancel_microphone_capture(self) -> Mapping[str, str | int | float | bool | None]:
        assert self.microphone is not None and self.selected_input is not None
        iterator = self.microphone.capture(self.selected_input.device_id).__aiter__()
        first = await asyncio.wait_for(anext(iterator), timeout=3.0)
        del first
        pending = asyncio.create_task(anext(iterator), name="arise-validation-mic-cancel")
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await _close_async_iterator(iterator)
        diagnostics = _safe_diagnostics(self.microphone)
        capture_active = diagnostics.get("capture_active")
        if capture_active is True:
            raise _CheckFailure("MICROPHONE_STREAM_ACTIVE_AFTER_CANCEL")
        if self.mode is CheckMode.REAL and capture_active is not False:
            raise _CheckBlocked("MICROPHONE_CANCELLATION_DIAGNOSTICS_UNAVAILABLE")
        return {
            "capture_task_cancelled": True,
            "capture_stream_inactive_after_cancel": False
            if capture_active is None
            else not capture_active,
            "capture_activity_diagnostics_available": isinstance(capture_active, bool),
            "raw_audio_retained": False,
        }

    async def _run_vad_cancellation(self) -> None:
        if self.vad_factory is None:
            self._block(ValidationProbe.VAD_CANCELLATION, self.vad_block_reason, self.mode)
            return
        if self.mode is CheckMode.REAL:
            self._block(
                ValidationProbe.VAD_CANCELLATION,
                "VAD_FRAME_ANALYSIS_HAS_NO_ASYNC_CANCELLATION_POINT",
                self.mode,
                details={"frame_processing_is_bounded": True},
            )
            return
        self._skip(
            ValidationProbe.VAD_CANCELLATION,
            "FAKE_OR_REPLAY_CANNOT_VERIFY_NATIVE_VAD_CANCELLATION",
            mode=self.mode,
        )

    async def _run_asr_cancellation(self) -> None:
        if self.mode is CheckMode.FAKE:
            self._skip(
                ValidationProbe.ASR_CANCELLATION,
                "FAKE_ASR_CANNOT_VERIFY_STREAM_CANCELLATION",
                mode=self.mode,
            )
            return
        if self.asr is None or not self.wake_detection or not self.wake_detection.activation_audio:
            self._block(ValidationProbe.ASR_CANCELLATION, "STREAMING_ASR_NOT_AVAILABLE", self.mode)
            return
        await self._run_check(
            ValidationProbe.ASR_CANCELLATION,
            self._cancel_asr_stream,
            mode=CheckMode.REPLAY if self.mode is CheckMode.REAL else self.mode,
        )

    async def _cancel_asr_stream(self) -> Mapping[str, str | int | float | bool | None]:
        consumed = asyncio.Event()
        chunks = self.wake_detection.activation_audio if self.wake_detection else ()

        async def blocked_audio() -> AsyncIterator[AudioChunk]:
            if chunks:
                yield chunks[0]
            consumed.set()
            await asyncio.Future()

        stream = self.asr.transcribe(
            blocked_audio(), locale=self.locale, correlation_id=f"voice-cancel-{uuid.uuid4().hex}"
        )

        async def consume() -> None:
            async with _aclosing_async_iterator(stream):
                async for _segment in stream:
                    pass

        task = asyncio.create_task(consume(), name="arise-validation-asr-cancel")
        try:
            await asyncio.wait_for(consumed.wait(), timeout=self.stage_timeout_seconds)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        diagnostics = _safe_diagnostics(self.asr)
        active = diagnostics.get("asr_active_streams")
        if active not in (None, 0):
            raise _CheckFailure("ASR_STREAM_ACTIVE_AFTER_CANCEL")
        return {
            "asr_task_cancelled": True,
            "active_streams_after_cancel": _int_or_zero(active),
            "audio_iterator_closed": True,
        }

    async def _run_gemini_connection(self) -> None:
        gate = self._gemini_gate()
        if gate is not None:
            if gate.startswith("SKIP:"):
                self._skip(ValidationProbe.GEMINI_CONNECTION, gate[5:])
            else:
                self._block(ValidationProbe.GEMINI_CONNECTION, gate, self.mode)
            return
        await self._run_check(
            ValidationProbe.GEMINI_CONNECTION, self._connect_gemini, mode=self.mode
        )

    def _gemini_gate(self) -> str | None:
        if not self.enable_live_gemini:
            return "SKIP:LIVE_GEMINI_OPT_IN_NOT_SET"
        if not self.cloud_confirmed:
            return "SKIP:CLOUD_CONSENT_NOT_GIVEN"
        if not (self.voice_cloud_opt_in and self.security_cloud_opt_in):
            return "ARISE_CLOUD_OPT_INS_REQUIRED"
        if self.mode is CheckMode.REAL and not self.is_windows_host:
            return "WINDOWS_HOST_REQUIRED"
        if self.vad_factory is None or self.wake_factory is None:
            return "LOCAL_VAD_AND_WAKE_CONFIGURATION_REQUIRED"
        if self.provider_factory is None:
            return self.gemini_block_reason
        if self._checks[ValidationProbe.VAD_LIVE].status is not CheckStatus.PASS:
            return "LOCAL_VAD_NOT_VALIDATED"
        if self._checks[ValidationProbe.WAKE_ACTIVATION].status is not CheckStatus.PASS:
            return "LOCAL_WAKE_NOT_VALIDATED"
        if self.wake_detection is None or not self.wake_detection.activation_audio:
            return "POST_WAKE_AUDIO_NOT_AVAILABLE"
        return None

    async def _connect_gemini(self) -> Mapping[str, str | int | float | bool | None]:
        assert self.provider_factory is not None
        self.provider = _BudgetedLiveProvider(self.provider_factory(), self._gemini_budget)
        self.gemini_session = await asyncio.wait_for(
            self.provider.connect(self._diagnostic_session_config()),
            timeout=self.stage_timeout_seconds,
        )
        return {
            "session_connected": True,
            "provider_id": str(getattr(self.provider, "provider_id", "unknown"))[:96],
            "diagnostic_tool_declarations": 0,
            "cloud_audio_sent_before_local_wake": False,
            "session_attempt_budget": MAX_GEMINI_SESSION_ATTEMPTS,
            "input_audio_budget_bytes": MAX_GEMINI_INPUT_AUDIO_BYTES,
            "output_audio_budget_bytes": MAX_GEMINI_OUTPUT_AUDIO_BYTES,
            "transcript_character_budget": MAX_GEMINI_TRANSCRIPT_CHARACTERS,
        }

    async def _run_gemini_response_streaming(self) -> None:
        if self._checks[ValidationProbe.GEMINI_CONNECTION].status is not CheckStatus.PASS:
            self._block(
                ValidationProbe.GEMINI_RESPONSE_STREAMING,
                "GEMINI_CONNECTION_NOT_PASSED",
                self.mode,
            )
            return
        if self.wake_detection is None or not self.wake_detection.activation_audio:
            self._block(
                ValidationProbe.GEMINI_RESPONSE_STREAMING,
                "POST_WAKE_AUDIO_NOT_AVAILABLE",
                self.mode,
            )
            return
        await self._run_check(
            ValidationProbe.GEMINI_RESPONSE_STREAMING,
            self._stream_gemini_response,
            mode=self.mode,
        )

    async def _stream_gemini_response(self) -> Mapping[str, str | int | float | bool | None]:
        assert self.gemini_session is not None and self.wake_detection is not None
        started = time.perf_counter()
        output_audio_bytes = 0
        output_audio_chunks = 0
        transcript_events = 0
        transcript_characters = 0
        first_output_ms: float | None = None
        turn_complete = False
        response_output_seen = False
        for chunk in self.wake_detection.activation_audio:
            await self.gemini_session.send_audio(chunk)
        iterator = self.gemini_session.receive().__aiter__()
        deadline = time.monotonic() + min(self.stage_timeout_seconds, 45.0)
        async with _aclosing_async_iterator(iterator):
            while time.monotonic() < deadline:
                try:
                    event: LiveEvent = await asyncio.wait_for(
                        anext(iterator), timeout=max(0.05, deadline - time.monotonic())
                    )
                except StopAsyncIteration:
                    break
                if event.type is LiveEventType.ERROR:
                    raise _CheckFailure(event.error_code or "GEMINI_PROVIDER_EVENT_ERROR")
                if event.type is LiveEventType.TOOL_CALL:
                    raise _CheckFailure("UNDECLARED_GEMINI_TOOL_CALL")
                if event.type is LiveEventType.OUTPUT_AUDIO:
                    output_audio_chunks += 1
                    output_audio_bytes += len(event.audio.data) if event.audio is not None else 0
                    response_output_seen = True
                elif event.type is LiveEventType.OUTPUT_TRANSCRIPT and event.text:
                    transcript_events += 1
                    transcript_characters += len(event.text)
                    response_output_seen = True
                if first_output_ms is None and event.type in {
                    LiveEventType.OUTPUT_AUDIO,
                    LiveEventType.OUTPUT_TRANSCRIPT,
                }:
                    first_output_ms = (time.perf_counter() - started) * 1000
                if event.type is LiveEventType.TURN_COMPLETE:
                    turn_complete = True
                    break
        if not response_output_seen:
            raise _CheckFailure("GEMINI_RESPONSE_NOT_RECEIVED")
        if not output_audio_chunks or not output_audio_bytes:
            raise _CheckFailure("GEMINI_OUTPUT_AUDIO_NOT_RECEIVED")
        if not turn_complete:
            raise _CheckFailure("GEMINI_TURN_COMPLETION_NOT_RECEIVED")
        budget = self._gemini_budget.diagnostics()
        return {
            "input_audio_chunks_sent": len(self.wake_detection.activation_audio),
            "input_audio_bytes_sent": sum(
                len(chunk.data) for chunk in self.wake_detection.activation_audio
            ),
            "output_audio_chunks_received": output_audio_chunks,
            "output_audio_bytes_received": output_audio_bytes,
            "output_transcript_events": transcript_events,
            "output_transcript_character_count": transcript_characters,
            "time_to_first_response_chunk_ms": round(first_output_ms or 0.0, 1),
            "turn_complete": turn_complete,
            "aggregate_input_audio_bytes": budget["input_audio_bytes"],
            "aggregate_output_audio_bytes": budget["output_audio_bytes"],
            "aggregate_transcript_characters": budget["transcript_characters"],
            "transcript_character_limit": MAX_GEMINI_TRANSCRIPT_CHARACTERS,
            "transcript_text_emitted": False,
        }

    def _diagnostic_session_config(self) -> LiveSessionConfig:
        return LiveSessionConfig(
            session_id=f"voice-check-{uuid.uuid4().hex}",
            locale=self.locale,
            system_instruction=_GEMINI_TEST_INSTRUCTION,
            tool_declarations=(),
        )

    async def _run_gemini_connection_cancellation(self) -> None:
        if (
            self.provider is None
            or self._checks[ValidationProbe.GEMINI_CONNECTION].status is not CheckStatus.PASS
        ):
            self._skip(
                ValidationProbe.GEMINI_CONNECTION_CANCELLATION, "GEMINI_SESSION_NOT_AVAILABLE"
            )
            return
        await self._run_check(
            ValidationProbe.GEMINI_CONNECTION_CANCELLATION,
            self._cancel_pending_gemini_connection,
            mode=self.mode,
        )

    async def _cancel_pending_gemini_connection(
        self,
    ) -> Mapping[str, str | int | float | bool | None]:
        assert self.provider is not None
        before = _safe_diagnostics(self.provider)
        pending = asyncio.create_task(
            self.provider.connect(self._diagnostic_session_config()),
            name="arise-validation-gemini-connect-cancel",
        )
        await asyncio.sleep(0.05)
        if pending.done():
            try:
                session = pending.result()
            except Exception:
                raise _CheckFailure("GEMINI_CANCEL_PROBE_CONNECT_FAILED") from None
            await session.close()
            raise _CheckBlocked(
                "GEMINI_CONNECT_COMPLETED_BEFORE_CANCEL_PROBE",
                cancelled_in_flight=False,
            )
        pending.cancel()
        try:
            await asyncio.wait_for(pending, timeout=3.0)
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            raise _CheckFailure("GEMINI_CONNECT_CANCEL_DID_NOT_SETTLE") from None
        except Exception:
            pass
        after = _safe_diagnostics(self.provider)
        if _int_or_zero(after.get("pending_connections")) != 0:
            raise _CheckFailure("GEMINI_PENDING_CONNECTION_LEAK")
        if _int_or_zero(after.get("active_sessions")) != _int_or_zero(
            before.get("active_sessions")
        ):
            raise _CheckFailure("GEMINI_SESSION_LEAK_AFTER_CONNECT_CANCEL")
        return {
            "connection_cancelled_in_flight": True,
            "pending_connections_after_cancel": 0,
            "active_session_count_unchanged": True,
        }

    async def _run_tts(self) -> None:
        if self.tts_factory is None:
            self._block(ValidationProbe.TTS, self.tts_block_reason, self.mode)
            return
        await self._run_check(ValidationProbe.TTS, self._synthesize_tts, mode=self.mode)

    async def _synthesize_tts(self) -> Mapping[str, str | int | float | bool | None]:
        if self.tts is None:
            result = self.tts_factory()
            self.tts = await result if isinstance(result, Awaitable) else result
        stream = self.tts.synthesize(
            _TEST_TTS_TEXT, locale=self.locale, correlation_id=f"voice-tts-{uuid.uuid4().hex}"
        )
        started = time.perf_counter()
        count = 0
        total_bytes = 0
        duration_seconds = 0.0
        first: AudioChunk | None = None
        first_chunk_ms: float | None = None
        async with _aclosing_async_iterator(stream):
            async for chunk in stream:
                count += 1
                total_bytes += len(chunk.data)
                duration_seconds += _chunk_duration_seconds(chunk)
                if first is None:
                    first = chunk
                    first_chunk_ms = (time.perf_counter() - started) * 1000
                if count > MAX_TTS_CHUNKS or total_bytes > MAX_TTS_AUDIO_BYTES:
                    raise _CheckFailure("TTS_OUTPUT_LIMIT_EXCEEDED")
        if first is None:
            raise _CheckFailure("TTS_RETURNED_NO_AUDIO")
        self.first_tts_chunk = first
        return {
            "audio_chunks": count,
            "audio_bytes": total_bytes,
            "duration_ms": round(duration_seconds * 1000, 1),
            "time_to_first_chunk_ms": round(first_chunk_ms or 0.0, 1),
            "synthesis_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "sample_rate_hz": first.sample_rate_hz,
            "audio_persisted": False,
        }

    async def _run_tts_cancellation(self) -> None:
        if self.tts is None:
            self._block(ValidationProbe.TTS_CANCELLATION, "TTS_NOT_INITIALIZED", self.mode)
            return
        await self._run_check(
            ValidationProbe.TTS_CANCELLATION, self._cancel_tts_stream, mode=self.mode
        )

    async def _cancel_tts_stream(self) -> Mapping[str, str | int | float | bool | None]:
        assert self.tts is not None
        stream = self.tts.synthesize(
            "This is a bounded cancellation probe. " * 24,
            locale=self.locale,
            correlation_id=f"voice-tts-cancel-{uuid.uuid4().hex}",
        )
        iterator = stream.__aiter__()
        try:
            await asyncio.wait_for(anext(iterator), timeout=self.stage_timeout_seconds)
            pending = asyncio.create_task(anext(iterator), name="arise-validation-tts-cancel")
            await asyncio.sleep(0.05)
            if pending.done():
                await asyncio.gather(pending, return_exceptions=True)
                raise _CheckBlocked("TTS_STREAM_FINISHED_BEFORE_CANCELLATION")
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        except StopAsyncIteration:
            raise _CheckFailure("TTS_RETURNED_NO_AUDIO") from None
        finally:
            await _close_async_iterator(iterator)
        active = _safe_diagnostics(self.tts).get("tts_active_streams")
        if active not in (None, 0):
            raise _CheckFailure("TTS_STREAM_ACTIVE_AFTER_CANCEL")
        return {
            "stream_cancelled": True,
            "active_tts_streams_after_cancel": _int_or_zero(active),
        }

    async def _run_speaker_playback(self) -> None:
        if not self.playback_confirmed:
            self._skip(ValidationProbe.SPEAKER_PLAYBACK, "PLAYBACK_CONSENT_NOT_GIVEN")
            return
        if self.playback is None or not self.output_devices:
            self._block(ValidationProbe.SPEAKER_PLAYBACK, "OUTPUT_DEVICE_NOT_AVAILABLE", self.mode)
            return
        await self._run_check(
            ValidationProbe.SPEAKER_PLAYBACK, self._play_safe_tone, mode=self.mode
        )

    async def _play_safe_tone(self) -> Mapping[str, str | int | float | bool | None]:
        assert self.playback is not None
        await self._ask("Speaker check: a short 440 Hz tone will play after Enter.")
        tone = _tone_chunk(seconds=0.4)
        previous_calls = self.playback.play_calls
        await asyncio.wait_for(self.playback.play(tone), timeout=3.0)
        if self.playback.play_calls <= previous_calls:
            raise _CheckFailure("PLAYBACK_STREAM_WRITE_NOT_COMPLETED")
        return {
            "playback_stream_started": True,
            "playback_stream_write_completed": True,
            "signal_duration_ms": 400,
            "human_hearing_confirmed": False,
        }

    async def _run_playback_cancellation(self) -> None:
        if not self.playback_confirmed:
            self._skip(ValidationProbe.PLAYBACK_CANCELLATION, "PLAYBACK_CONSENT_NOT_GIVEN")
            return
        if self.playback is None:
            self._block(
                ValidationProbe.PLAYBACK_CANCELLATION, "PLAYBACK_ADAPTER_NOT_CONFIGURED", self.mode
            )
            return
        if self.mode is not CheckMode.REAL:
            self._skip(
                ValidationProbe.PLAYBACK_CANCELLATION,
                "INJECTED_PLAYBACK_CANNOT_VERIFY_DEVICE_CANCELLATION",
                mode=self.mode,
            )
            return
        await self._run_check(
            ValidationProbe.PLAYBACK_CANCELLATION,
            self._cancel_playback,
            mode=self.mode,
        )

    async def _cancel_playback(self) -> Mapping[str, str | int | float | bool | None]:
        assert self.playback is not None
        playback_task = asyncio.create_task(
            self.playback.play(_tone_chunk(seconds=3.0)), name="arise-validation-playback-cancel"
        )
        try:
            await asyncio.sleep(0.1)
            if playback_task.done():
                await asyncio.gather(playback_task, return_exceptions=True)
                raise _CheckBlocked("PLAYBACK_FINISHED_BEFORE_CANCEL")
            await asyncio.wait_for(self.playback.stop(), timeout=2.0)
            playback_task.cancel()
            await asyncio.wait_for(
                asyncio.gather(playback_task, return_exceptions=True), timeout=2.0
            )
        finally:
            if not playback_task.done():
                playback_task.cancel()
                await asyncio.gather(playback_task, return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.playback.stop()
        diagnostics = self.playback.diagnostics()
        if diagnostics.get("playback_active") is True:
            raise _CheckFailure("PLAYBACK_ACTIVE_AFTER_CANCEL")
        return {
            "playback_task_cancelled": True,
            "playback_stream_inactive_after_cancel": diagnostics.get("playback_active") is False,
            "task_engine_task_cancelled": False,
        }

    async def _run_audiohub_end_to_end(self) -> None:
        prerequisites = self._audiohub_prerequisites()
        if prerequisites:
            reason = prerequisites[0]
            for check_id in (
                ValidationProbe.BARGE_IN,
                ValidationProbe.REPEATED_BARGE_IN,
                ValidationProbe.GEMINI_GENERATION_CANCELLATION,
                ValidationProbe.FULL_TURN_CANCELLATION,
                ValidationProbe.END_TO_END_VOICE_TURN,
            ):
                self._block(check_id, reason, self.mode)
            return
        await self._run_check(
            ValidationProbe.END_TO_END_VOICE_TURN,
            self._run_repeated_barge_in_turn,
            mode=self.mode,
            timeout_seconds=MAX_GEMINI_E2E_SECONDS,
        )

    def _audiohub_prerequisites(self) -> list[str]:
        missing: list[str] = []
        if not self.microphone_confirmed:
            missing.append("MICROPHONE_CONSENT_NOT_GIVEN")
        if not self.playback_confirmed:
            missing.append("PLAYBACK_CONSENT_NOT_GIVEN")
        if not self.enable_live_gemini or not self.cloud_confirmed:
            missing.append("LIVE_GEMINI_OPT_IN_AND_CLOUD_CONSENT_REQUIRED")
        if self.provider_factory is None:
            missing.append(self.gemini_block_reason)
        if self.microphone is None or self.selected_input is None:
            missing.append("INPUT_DEVICE_NOT_AVAILABLE")
        if self.playback is None or not self.output_devices:
            missing.append("OUTPUT_DEVICE_NOT_AVAILABLE")
        if self.vad_factory is None or self.wake_factory is None or self.asr_factory is None:
            missing.append("LOCAL_VAD_WAKE_ASR_CONFIGURATION_REQUIRED")
        for check_id in (
            ValidationProbe.MICROPHONE_CAPTURE,
            ValidationProbe.VAD_LIVE,
            ValidationProbe.WAKE_ACTIVATION,
            ValidationProbe.STREAMING_ASR,
            ValidationProbe.TRANSCRIPT,
            ValidationProbe.INTENT_FROM_TRANSCRIPT,
        ):
            if self._checks[check_id].status is not CheckStatus.PASS:
                missing.append(f"{check_id.value.upper()}_NOT_PASSED")
        if not self.voice_cloud_opt_in or not self.security_cloud_opt_in:
            missing.append("ARISE_CLOUD_OPT_INS_REQUIRED")
        if self._checks[ValidationProbe.GEMINI_CONNECTION].status is not CheckStatus.PASS:
            missing.append("GEMINI_CONNECTION_NOT_PASSED")
        if self._checks[ValidationProbe.GEMINI_RESPONSE_STREAMING].status is not CheckStatus.PASS:
            missing.append("GEMINI_RESPONSE_NOT_PASSED")
        return missing

    async def _run_repeated_barge_in_turn(self) -> Mapping[str, str | int | float | bool | None]:
        assert self.microphone is not None and self.selected_input is not None
        assert self.playback is not None and self.vad_factory is not None
        assert self.wake_factory is not None and self.asr_factory is not None
        assert self.provider_factory is not None
        if self.provider is not None:
            await self.provider.close()
        self.provider = _BudgetedLiveProvider(self.provider_factory(), self._gemini_budget)
        observer = _HubObserver()
        recognizer = _ObservedSpeechRecognizer(self.asr_factory())
        self._observed_asr = recognizer
        hub = AudioHub(
            microphone=self.microphone,
            vad=self.vad_factory(),
            wake_word_detector=self.wake_factory(),
            provider=self.provider,
            playback=self.playback,
            speech_recognizer=recognizer,
            config=VoiceConfig(
                wake_word=self.wake_word,
                locale=self.locale,
                microphone_device_id=self.selected_input.device_id,
                inactivity_timeout_seconds=15,
            ),
            event_sink=observer,
        )
        self.active_hub = hub
        baseline_plays = self.playback.play_calls
        baseline_stops = self.playback.stop_calls
        baseline_speaking_transitions = observer.state_count(VoiceState.SPEAKING)
        try:
            status = await hub.start()
            if status.state is VoiceState.ERROR or status.microphone_status.value != "available":
                raise _CheckFailure("AUDIOHUB_FAILED_TO_START")
            baseline_plays = self.playback.play_calls
            baseline_stops = self.playback.stop_calls
            baseline_speaking_transitions = observer.state_count(VoiceState.SPEAKING)
            await self._ask(
                f"Speak {self.wake_word}, then ask: 'What is two plus two?' After the response "
                "starts, interrupt it with 'What is three plus three?' When that response starts, "
                "interrupt a second time with 'What is four plus four?'."
            )
            await self._wait_for_output(
                observer,
                baseline_plays + 1,
                baseline_speaking_transitions + 1,
                timeout=15.0,
            )
            for interruption_number in (1, 2):
                self._notify(
                    f"Interrupt the current answer now (interruption {interruption_number} of 2)."
                )
                await self._wait_for_hub_event(
                    observer,
                    lambda event, target=interruption_number: (
                        observer.count(VoiceEventKind.BARGE_IN) >= target
                    ),
                    timeout=15.0,
                )
                await self._wait_for_output(
                    observer,
                    baseline_plays + interruption_number + 1,
                    baseline_speaking_transitions + interruption_number + 1,
                    timeout=15.0,
                )
            metrics = hub.snapshot().telemetry
            barge_count = observer.count(VoiceEventKind.BARGE_IN)
            stop_count = self.playback.stop_calls - baseline_stops
            detection = metrics.get("barge_in_detection_latency_ms")
            playback_stop = metrics.get("playback_stop_latency_ms")
            generation_cancel = metrics.get("generation_cancel_latency_ms")
            cancel_acks = generation_cancel.count if generation_cancel is not None else 0
            response_starts = (
                observer.state_count(VoiceState.SPEAKING) - baseline_speaking_transitions
            )
            responses_after_interruptions = max(0, response_starts - 1)
            if barge_count < 2 or stop_count < 2 or stop_count < barge_count:
                raise _CheckFailure(
                    "REPEATED_BARGE_IN_NOT_OBSERVED",
                    barge_in_count=barge_count,
                    playback_stop_count=stop_count,
                )
            if cancel_acks < 2:
                raise _CheckFailure(
                    "GEMINI_INTERRUPTION_ACK_NOT_OBSERVED",
                    barge_in_count=barge_count,
                    interruption_ack_count=cancel_acks,
                )
            if responses_after_interruptions < 2:
                raise _CheckFailure(
                    "FOLLOWUP_RESPONSE_COUNT_MISMATCH",
                    response_starts=response_starts,
                    expected_followup_responses=2,
                )
            self._set(
                ValidationProbe.BARGE_IN,
                CheckStatus.PASS,
                self.mode,
                details={
                    "barge_in_detection_latency_ms": detection.last_latency_ms
                    if detection is not None
                    else None,
                    "playback_stop_latency_ms": playback_stop.last_latency_ms
                    if playback_stop is not None
                    else self.playback.last_stop_latency_ms,
                    "generation_cancel_latency_ms": generation_cancel.last_latency_ms
                    if generation_cancel is not None
                    else None,
                    "interruption_count": barge_count,
                    "task_engine_task_cancelled": False,
                },
            )
            self._set(
                ValidationProbe.REPEATED_BARGE_IN,
                CheckStatus.PASS,
                self.mode,
                details={
                    "interruptions_observed": barge_count,
                    "assistant_response_starts": response_starts,
                    "assistant_responses_after_interruptions": responses_after_interruptions,
                    "generation_cancel_acknowledgements": cancel_acks,
                    "stale_output_injection_performed": False,
                    "stale_output_fence_replay_is_unit_tested": True,
                },
            )
            self._set(
                ValidationProbe.GEMINI_GENERATION_CANCELLATION,
                CheckStatus.PASS,
                self.mode,
                details={
                    "generation_cancel_acknowledgements": cancel_acks,
                    "last_cancel_latency_ms": generation_cancel.last_latency_ms
                    if generation_cancel is not None
                    else None,
                },
            )
            self._set(
                ValidationProbe.FULL_TURN_CANCELLATION,
                CheckStatus.PASS,
                self.mode,
                details={
                    "voice_output_cancelled_by_barge_in": True,
                    "task_engine_task_cancelled": False,
                    "followup_response_received": True,
                },
            )
            return {
                "audiohub_started": True,
                "local_wake_required_before_provider": True,
                "local_asr_partial_count": recognizer.partial_count,
                "local_asr_final_count": recognizer.final_count,
                "local_asr_final_character_count": recognizer.final_character_count,
                "assistant_audio_play_calls": self.playback.play_calls - baseline_plays,
                "barge_in_events": barge_count,
                "assistant_response_starts": response_starts,
                "assistant_responses_after_interruptions": responses_after_interruptions,
                "generation_cancel_acknowledgements": cancel_acks,
                "task_engine_invoked": False,
                "task_completion_claimed": False,
                "gemini_input_audio_budget_bytes": MAX_GEMINI_INPUT_AUDIO_BYTES,
                "gemini_output_audio_budget_bytes": MAX_GEMINI_OUTPUT_AUDIO_BYTES,
                "gemini_transcript_character_budget": MAX_GEMINI_TRANSCRIPT_CHARACTERS,
                "e2e_wall_clock_budget_seconds": MAX_GEMINI_E2E_SECONDS,
                "raw_audio_or_transcript_persisted": False,
            }
        except _CheckFailure as exc:
            self._record_barge_in_failure(
                hub,
                observer,
                baseline_plays=baseline_plays,
                baseline_stops=baseline_stops,
                error_code=exc.error_code,
                timed_out=False,
            )
            raise
        except TimeoutError:
            self._record_barge_in_failure(
                hub,
                observer,
                baseline_plays=baseline_plays,
                baseline_stops=baseline_stops,
                error_code="BARGE_IN_OR_RESPONSE_TIMEOUT",
                timed_out=True,
            )
            raise _CheckFailure("END_TO_END_VOICE_TURN_TIMEOUT") from None
        except Exception as exc:
            self._record_barge_in_failure(
                hub,
                observer,
                baseline_plays=baseline_plays,
                baseline_stops=baseline_stops,
                error_code=_safe_failure_code(exc),
                timed_out=False,
            )
            raise
        finally:
            if self._checks[ValidationProbe.BARGE_IN].details.get("reason") == "NOT_RUN":
                self._record_barge_in_failure(
                    hub,
                    observer,
                    baseline_plays=baseline_plays,
                    baseline_stops=baseline_stops,
                    error_code="END_TO_END_VOICE_TURN_CANCELLED",
                    timed_out=True,
                )
            await hub.close()
            self.active_hub = None

    def _record_barge_in_failure(
        self,
        hub: AudioHub,
        observer: _HubObserver,
        *,
        baseline_plays: int,
        baseline_stops: int,
        error_code: str,
        timed_out: bool,
    ) -> None:
        metrics = hub.snapshot().telemetry
        barge_count = observer.count(VoiceEventKind.BARGE_IN)
        stop_count = self.playback.stop_calls - baseline_stops if self.playback is not None else 0
        cancellation = metrics.get("generation_cancel_latency_ms")
        detection = metrics.get("barge_in_detection_latency_ms")
        playback_stop = metrics.get("playback_stop_latency_ms")
        cancel_acks = cancellation.count if cancellation is not None else 0
        response_starts = observer.state_count(VoiceState.SPEAKING)
        responses_after_interruptions = max(0, response_starts - 1)
        details: dict[str, str | int | float | bool | None] = {
            "barge_in_events": barge_count,
            "playback_stop_count": stop_count,
            "barge_in_detection_latency_ms": detection.last_latency_ms
            if detection is not None
            else None,
            "playback_stop_latency_ms": playback_stop.last_latency_ms
            if playback_stop is not None
            else self.playback.last_stop_latency_ms
            if self.playback is not None
            else None,
            "generation_cancel_acknowledgements": cancel_acks,
            "generation_cancel_latency_ms": cancellation.last_latency_ms
            if cancellation is not None
            else None,
            "assistant_audio_play_calls": self.playback.play_calls - baseline_plays
            if self.playback is not None
            else 0,
            "assistant_response_starts": response_starts,
            "assistant_responses_after_interruptions": responses_after_interruptions,
            "task_engine_task_cancelled": False,
        }
        safe_error = _normalise_code(error_code)
        barge_status = (
            CheckStatus.PASS
            if barge_count > 0 and stop_count > 0
            else CheckStatus.FAIL
            if timed_out
            else CheckStatus.BLOCKED
        )
        self._set(
            ValidationProbe.BARGE_IN,
            barge_status,
            self.mode,
            details=details,
            error=None if barge_status is CheckStatus.PASS else safe_error,
        )
        repeated_pass = (
            barge_count >= 2
            and stop_count >= 2
            and cancel_acks >= 2
            and responses_after_interruptions >= 2
        )
        repeated_status = (
            CheckStatus.PASS
            if repeated_pass
            else CheckStatus.FAIL
            if timed_out or barge_count > 0
            else CheckStatus.BLOCKED
        )
        self._set(
            ValidationProbe.REPEATED_BARGE_IN,
            repeated_status,
            self.mode,
            details={**details, "required_interruptions": 2},
            error=None if repeated_status is CheckStatus.PASS else safe_error,
        )
        cancellation_status = (
            CheckStatus.PASS
            if barge_count > 0 and cancel_acks >= barge_count
            else CheckStatus.FAIL
            if barge_count > 0
            else CheckStatus.BLOCKED
        )
        self._set(
            ValidationProbe.GEMINI_GENERATION_CANCELLATION,
            cancellation_status,
            self.mode,
            details={**details, "generation_cancel_acknowledgements": cancel_acks},
            error=None if cancellation_status is CheckStatus.PASS else safe_error,
        )
        full_turn_status = (
            CheckStatus.PASS
            if repeated_pass
            else CheckStatus.FAIL
            if timed_out or barge_count > 0
            else CheckStatus.BLOCKED
        )
        self._set(
            ValidationProbe.FULL_TURN_CANCELLATION,
            full_turn_status,
            self.mode,
            details={
                **details,
                "voice_output_cancelled_by_barge_in": cancel_acks > 0,
                "followup_response_received": responses_after_interruptions >= 2,
            },
            error=None if full_turn_status is CheckStatus.PASS else safe_error,
        )

    async def _wait_for_output(
        self,
        observer: _HubObserver,
        minimum_plays: int,
        minimum_speaking_transitions: int,
        *,
        timeout: float,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            latest_state = next(
                (
                    event.state
                    for event in reversed(observer.events)
                    if event.kind is VoiceEventKind.STATE_CHANGED
                ),
                None,
            )
            if (
                self.playback is not None
                and self.playback.play_calls >= minimum_plays
                and observer.state_count(VoiceState.SPEAKING) >= minimum_speaking_transitions
                and latest_state is VoiceState.SPEAKING
            ):
                return
            await asyncio.sleep(0.02)
        raise TimeoutError

    async def _wait_for_hub_event(
        self,
        observer: _HubObserver,
        predicate: Callable[[VoiceEvent], bool],
        *,
        timeout: float,
    ) -> VoiceEvent:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            event = await asyncio.wait_for(observer.queue.get(), timeout=remaining)
            if predicate(event):
                return event
        raise TimeoutError

    async def _run_reconnect_probe(self) -> None:
        if self.provider is None or not self.enable_live_gemini or not self.cloud_confirmed:
            self._skip(ValidationProbe.RECONNECT, "LIVE_GEMINI_NOT_ACTIVE")
            return
        await self._run_check(
            ValidationProbe.RECONNECT, self._clean_close_reconnect_probe, mode=self.mode
        )

    async def _clean_close_reconnect_probe(self) -> Mapping[str, str | int | float | bool | None]:
        assert self.provider is not None
        if self.gemini_session is not None:
            await self.gemini_session.close()
            self.gemini_session = None
        fresh = await asyncio.wait_for(
            self.provider.connect(self._diagnostic_session_config()),
            timeout=self.stage_timeout_seconds,
        )
        await fresh.close()
        raise _CheckBlocked(
            "NETWORK_FAULT_NOT_INJECTED",
            fresh_session_after_clean_close=True,
            reconnect_after_transport_loss_verified=False,
        )

    async def _run_device_close_reopen(self) -> None:
        if not self.microphone_confirmed:
            self._skip(ValidationProbe.DEVICE_CLOSE_REOPEN, "MICROPHONE_CONSENT_NOT_GIVEN")
            return
        if not self.playback_confirmed:
            self._skip(ValidationProbe.DEVICE_CLOSE_REOPEN, "PLAYBACK_CONSENT_NOT_GIVEN")
            return
        if self.microphone_factory is None or self.playback_factory is None:
            self._block(
                ValidationProbe.DEVICE_CLOSE_REOPEN, "AUDIO_ADAPTER_NOT_CONFIGURED", self.mode
            )
            return
        await self._run_check(
            ValidationProbe.DEVICE_CLOSE_REOPEN, self._close_reopen_devices, mode=self.mode
        )

    async def _close_reopen_devices(self) -> Mapping[str, str | int | float | bool | None]:
        if self.microphone is not None:
            await self.microphone.close()
        assert self.microphone_factory is not None
        replacement_microphone = self.microphone_factory()
        try:
            input_devices = tuple(await replacement_microphone.list_devices())
            selected_input = self._select_device(input_devices, self.input_device_id)
            if selected_input is None:
                raise _CheckFailure("NO_INPUT_DEVICE_AFTER_CLEAN_REOPEN")
            chunks = await self._capture_from(replacement_microphone, selected_input.device_id, 0.5)
        finally:
            await replacement_microphone.close()

        if self.playback is not None:
            await self.playback.close()
        assert self.playback_factory is not None
        raw_playback = self.playback_factory()
        replacement_playback = (
            raw_playback
            if isinstance(raw_playback, _PlaybackProbe)
            else _PlaybackProbe(raw_playback)
        )
        try:
            output_devices = tuple(await replacement_playback.list_devices())
            selected_output = self._select_device(output_devices, self.output_device_id)
            if selected_output is None:
                raise _CheckFailure("NO_OUTPUT_DEVICE_AFTER_CLEAN_REOPEN")
            await replacement_playback.play(_tone_chunk(seconds=0.25))
        finally:
            await replacement_playback.close()
        self.microphone = replacement_microphone
        self.playback = replacement_playback
        return {
            "microphone_clean_close_reopen_passed": bool(chunks),
            "output_clean_close_reopen_stream_write_passed": True,
            "device_loss_injected": False,
            "hotplug_recovery_claimed": False,
        }

    async def _shutdown_cleanup(self, baseline_tasks: set[int]) -> None:
        started = time.perf_counter()
        cleanup_errors: list[str] = []
        hub = self.active_hub
        if hub is not None:
            try:
                await hub.close()
            except Exception:
                cleanup_errors.append("AUDIOHUB_CLOSE_FAILED")
            self.active_hub = None
        if self.gemini_session is not None:
            try:
                await self.gemini_session.close()
            except Exception:
                cleanup_errors.append("GEMINI_SESSION_CLOSE_FAILED")
            self.gemini_session = None
        for resource, error_code in (
            (self.provider, "GEMINI_PROVIDER_CLOSE_FAILED"),
            (self.playback, "PLAYBACK_CLOSE_FAILED"),
            (self.microphone, "MICROPHONE_CLOSE_FAILED"),
        ):
            if resource is None:
                continue
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                result = close()
                if isinstance(result, Awaitable):
                    await result
            except Exception:
                cleanup_errors.append(error_code)
        self._resources_closed = True

        mic_diag = _safe_diagnostics(self.microphone)
        playback_diag = _safe_diagnostics(self.playback)
        asr_diag = _safe_diagnostics(self.asr)
        tts_diag = _safe_diagnostics(self.tts)
        provider_diag = _safe_diagnostics(self.provider)
        hub_tasks = _hub_pending_task_count(hub)
        named_tasks = sum(
            1
            for task in asyncio.all_tasks()
            if not task.done()
            and task is not asyncio.current_task()
            and id(task) not in baseline_tasks
            and task.get_name().startswith("arise-")
        )
        active_resources = any(
            value is True
            for value in (
                mic_diag.get("capture_active"),
                playback_diag.get("playback_active"),
            )
        )
        active_resources |= _int_or_zero(asr_diag.get("asr_active_streams")) > 0
        active_resources |= _int_or_zero(tts_diag.get("tts_active_streams")) > 0
        active_resources |= _int_or_zero(provider_diag.get("active_sessions")) > 0
        active_resources |= _int_or_zero(provider_diag.get("pending_connections")) > 0
        active_resources |= hub_tasks > 0 or named_tasks > 0
        details: dict[str, str | int | float | bool | None] = {
            "microphone_capture_active": mic_diag.get("capture_active"),
            "microphone_adapter_closed": mic_diag.get("capture_closed"),
            "playback_active": playback_diag.get("playback_active"),
            "playback_adapter_closed": playback_diag.get("playback_closed"),
            "asr_active_streams": _int_or_zero(asr_diag.get("asr_active_streams")),
            "tts_active_streams": _int_or_zero(tts_diag.get("tts_active_streams")),
            "gemini_active_sessions": _int_or_zero(provider_diag.get("active_sessions")),
            "gemini_pending_connections": _int_or_zero(provider_diag.get("pending_connections")),
            "audiohub_pending_tasks": hub_tasks,
            "new_arise_named_async_tasks": named_tasks,
            "task_resource_leases": "NOT_APPLICABLE_NO_TASK_RUNTIME",
            "raw_audio_or_transcript_persisted": False,
            "cleanup_error_count": len(cleanup_errors),
        }
        status = CheckStatus.FAIL if cleanup_errors or active_resources else CheckStatus.PASS
        error = (
            cleanup_errors[0]
            if cleanup_errors
            else "ACTIVE_RESOURCE_AFTER_SHUTDOWN"
            if active_resources
            else None
        )
        self._set(
            ValidationProbe.SHUTDOWN_CLEANUP,
            status,
            self.mode,
            duration_ms=(time.perf_counter() - started) * 1000,
            details=details,
            error=error,
        )

    async def _run_check(
        self,
        check_id: ValidationProbe,
        operation: Callable[[], Awaitable[Mapping[str, str | int | float | bool | None]]]
        | Callable[[], Mapping[str, str | int | float | bool | None]],
        *,
        mode: CheckMode | None,
        timeout_seconds: float | None = None,
    ) -> None:
        started = time.perf_counter()
        try:
            result = operation()
            if isinstance(result, Awaitable):
                result = await asyncio.wait_for(
                    result, timeout=timeout_seconds or self.stage_timeout_seconds
                )
        except asyncio.CancelledError:
            raise
        except _CheckBlocked as exc:
            self._set(
                check_id,
                CheckStatus.BLOCKED,
                mode,
                duration_ms=(time.perf_counter() - started) * 1000,
                details={"reason": exc.reason, **exc.details},
                error=exc.reason,
            )
        except _CheckFailure as exc:
            self._set(
                check_id,
                CheckStatus.FAIL,
                mode,
                duration_ms=(time.perf_counter() - started) * 1000,
                details=exc.details,
                error=_normalise_code(exc.error_code),
            )
        except Exception as exc:
            if isinstance(exc, VoiceProviderFailure) and _provider_error_is_blocked(exc):
                self._set(
                    check_id,
                    CheckStatus.BLOCKED,
                    mode,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    details={"reason": exc.error_code},
                    error=_normalise_code(exc.error_code),
                )
            else:
                self._set(
                    check_id,
                    CheckStatus.FAIL,
                    mode,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    details={
                        "exception_type": type(exc).__name__[:64],
                        "raw_exception_emitted": False,
                    },
                    error=_safe_failure_code(exc),
                )
        else:
            self._set(
                check_id,
                CheckStatus.PASS,
                mode,
                duration_ms=(time.perf_counter() - started) * 1000,
                details=result,
            )

    def _set(
        self,
        check_id: ValidationProbe,
        status: CheckStatus,
        mode: CheckMode | None,
        *,
        duration_ms: float = 0.0,
        details: Mapping[str, str | int | float | bool | None] | None = None,
        error: str | None = None,
    ) -> None:
        self._checks[check_id] = CheckResult(
            id=check_id,
            status=status,
            mode=mode,
            duration_ms=max(0.0, duration_ms),
            details=details or {},
            error=error,
        )

    def _skip(
        self, check_id: ValidationProbe, reason: str, *, mode: CheckMode | None = None
    ) -> None:
        self._set(check_id, CheckStatus.SKIPPED, mode, details={"reason": _normalise_code(reason)})

    def _block(
        self,
        check_id: ValidationProbe,
        reason: str,
        mode: CheckMode | None,
        *,
        details: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> None:
        self._set(
            check_id,
            CheckStatus.BLOCKED,
            mode,
            details={"reason": _normalise_code(reason), **(details or {})},
            error=_normalise_code(reason),
        )

    def _block_live_checks(self, reason: str) -> None:
        for check_id in _LIVE_CHECKS:
            if check_id in {
                ValidationProbe.PRODUCTION_COMPOSITION_GATE,
                ValidationProbe.END_TO_END_TASK,
                ValidationProbe.TASK_ADMISSION,
            }:
                continue
            if self._checks[check_id].details.get("reason") != "NOT_RUN":
                continue
            if check_id in {
                ValidationProbe.GEMINI_CONNECTION,
                ValidationProbe.GEMINI_RESPONSE_STREAMING,
                ValidationProbe.GEMINI_CONNECTION_CANCELLATION,
                ValidationProbe.GEMINI_GENERATION_CANCELLATION,
                ValidationProbe.RECONNECT,
            }:
                if not self.enable_live_gemini:
                    self._skip(check_id, "LIVE_GEMINI_OPT_IN_NOT_SET")
                    continue
                if not self.cloud_confirmed:
                    self._skip(check_id, "CLOUD_CONSENT_NOT_GIVEN")
                    continue
            self._block(check_id, reason, self.mode)

    def _report(self, timestamp: str, *, run_mode: str) -> ValidationReport:
        for check_id, result in tuple(self._checks.items()):
            if result.details.get("reason") == "NOT_RUN":
                self._block(check_id, "HARNESS_STAGE_NOT_REACHED", self.mode)
        stages = tuple(
            _aggregate_stage(stage, tuple(self._checks[probe] for probe in probes))
            for stage, probes in _STAGE_PROBES.items()
        )
        return ValidationReport(
            runtime=self.runtime,
            environment_status=(
                EnvironmentStatus.WINDOWS
                if self.is_windows_host
                else EnvironmentStatus.ENVIRONMENT_LIMITED
            ),
            timestamp=timestamp,
            platform_details=dict(self.platform_details),
            run_mode=run_mode,
            checks=stages,
        )

    async def _ask(self, message: str) -> None:
        if self.prompt is not None:
            await self.prompt(message)

    def _notify(self, message: str) -> None:
        if self.notify is not None:
            self.notify(message)

    def _select_device(
        self, devices: Sequence[AudioDevice], configured_device_id: str | None
    ) -> AudioDevice | None:
        if configured_device_id is not None:
            return next(
                (device for device in devices if device.device_id == configured_device_id), None
            )
        return next((device for device in devices if device.is_default), None) or next(
            iter(devices), None
        )

    def _update_portaudio_details(self) -> None:
        adapters = (self.microphone, self.playback.playback if self.playback else None)
        for adapter in adapters:
            if adapter is None:
                continue
            loader = getattr(adapter, "_sounddevice", None)
            if not callable(loader):
                continue
            try:
                sounddevice = loader()
                version = sounddevice.get_portaudio_version()
                host_apis = sounddevice.query_hostapis()
            except Exception:
                self.platform_details["audio_backend"] = "PortAudio probe failed"
                self.platform_details["portaudio_runtime_probed"] = False
                return
            self.platform_details["audio_backend"] = "PortAudio"
            self.platform_details["portaudio_version"] = str(version[0])[:96]
            self.platform_details["portaudio_host_api_count"] = len(host_apis)
            self.platform_details["portaudio_runtime_probed"] = True
            return


async def host_guarded_report(
    *, host_platform: str | None = None, live_gemini_requested: bool = False
) -> ValidationReport:
    """Return a non-Windows report without importing or constructing hardware/provider adapters."""

    harness = WindowsVoiceValidationHarness(
        mode=CheckMode.REAL,
        enable_live_gemini=live_gemini_requested,
        host_platform_override=host_platform,
    )
    return await harness.run()


def _platform_snapshot(host_platform: str) -> dict[str, str | int | float | bool | None]:
    package_names = {
        "sounddevice": "sounddevice",
        "webrtcvad": "webrtcvad-wheels",
        "vosk": "vosk",
        "kokoro_onnx": "kokoro-onnx",
        "google_genai": "google-genai",
        "keyring": "keyring",
    }
    result: dict[str, str | int | float | bool | None] = {
        "os_name": host_platform,
        "os_release": str(platform.release())[:64],
        "os_version": str(platform.version())[:128],
        "architecture": str(platform.machine())[:64],
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "target_is_windows": host_platform.casefold().startswith("win"),
        "audio_backend": None,
        "portaudio_runtime_probed": False,
    }
    module_names = {
        "sounddevice": "sounddevice",
        "webrtcvad": "webrtcvad",
        "vosk": "vosk",
        "kokoro_onnx": "kokoro_onnx",
        "google_genai": "google.genai",
        "keyring": "keyring",
    }
    for key, module in module_names.items():
        try:
            installed = importlib.util.find_spec(module) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            installed = False
        result[f"{key}_installed"] = installed
        distribution = package_names[key]
        try:
            result[f"{key}_version"] = importlib.metadata.version(distribution)[:64]
        except importlib.metadata.PackageNotFoundError:
            result[f"{key}_version"] = None
    if result["sounddevice_installed"]:
        result["audio_backend"] = "PortAudio (not yet probed)"
    return result


def _aggregate_stage(stage: ValidationStage, subchecks: tuple[CheckResult, ...]) -> CheckResult:
    statuses = {check.status for check in subchecks}
    live_gemini_check = next(
        (check for check in subchecks if check.id is ValidationProbe.GEMINI_CONNECTION),
        None,
    )
    if CheckStatus.FAILED in statuses:
        status = CheckStatus.FAILED
    elif (
        stage is ValidationStage.GEMINI_CONNECTION
        and live_gemini_check is not None
        and live_gemini_check.status is CheckStatus.SKIPPED
    ):
        status = CheckStatus.SKIPPED
    elif CheckStatus.BLOCKED in statuses:
        status = CheckStatus.BLOCKED
    elif CheckStatus.PASS in statuses and CheckStatus.SKIPPED in statuses:
        status = CheckStatus.PARTIAL
    elif statuses == {CheckStatus.SKIPPED}:
        status = CheckStatus.SKIPPED
    else:
        status = CheckStatus.PASS
    modes = {check.mode for check in subchecks}
    mode = next(iter(modes)) if len(modes) == 1 else None
    error_check = next(
        (check for check in subchecks if check.status in {CheckStatus.FAILED, CheckStatus.BLOCKED}),
        None,
    )
    details = {
        "probe_count": len(subchecks),
        "passed_probe_count": sum(check.status is CheckStatus.PASS for check in subchecks),
        "failed_probe_count": sum(check.status is CheckStatus.FAILED for check in subchecks),
        "blocked_probe_count": sum(check.status is CheckStatus.BLOCKED for check in subchecks),
        "skipped_probe_count": sum(check.status is CheckStatus.SKIPPED for check in subchecks),
    }
    return CheckResult(
        id=stage,
        status=status,
        mode=mode,
        duration_ms=sum(check.duration_ms for check in subchecks),
        details=details,
        error=error_check.error if error_check is not None else None,
        subchecks=subchecks,
    )


def _runtime_name(host_platform: str) -> str:
    normalized = host_platform.casefold()
    if normalized.startswith("win"):
        return "windows"
    if normalized == "linux":
        return "linux"
    if normalized in {"darwin", "macos"}:
        return "macos"
    return "other"


def _vad_synthetic_fixture() -> list[AudioChunk]:
    sample_rate = 16_000
    frame_samples = 320
    silence = [b"\x00\x00" * frame_samples for _ in range(6)]
    trailing_silence = [b"\x00\x00" * frame_samples for _ in range(16)]
    voice: list[bytes] = []
    amplitude = 4_500
    for frame_index in range(20):
        data = bytearray(frame_samples * 2)
        frame_start = frame_index * frame_samples
        envelope = 0.75 + 0.25 * math.sin(frame_index * 0.41)
        for sample_index in range(frame_samples):
            absolute = frame_start + sample_index
            seconds = absolute / sample_rate
            formant = (
                math.sin(2 * math.pi * 120 * seconds)
                + 0.62 * math.sin(2 * math.pi * 240 * seconds)
                + 0.33 * math.sin(2 * math.pi * 720 * seconds)
                + 0.18 * math.sin(2 * math.pi * 1_180 * seconds)
            )
            value = int(max(-32_000, min(32_000, amplitude * envelope * formant / 2.13)))
            struct.pack_into("<h", data, sample_index * 2, value)
        voice.append(bytes(data))
    payloads = silence + voice + trailing_silence
    return [
        AudioChunk(index, "pcm_s16le", sample_rate, 1, payload)
        for index, payload in enumerate(payloads)
    ]


def _pcm_level(chunks: Sequence[AudioChunk]) -> tuple[int, float, int, int]:
    total_samples = 0
    square_sum = 0
    peak = 0
    nonzero = 0
    for chunk in chunks:
        samples = array("h")
        samples.frombytes(chunk.data[: len(chunk.data) - (len(chunk.data) % 2)])
        if sys.byteorder == "big":
            samples.byteswap()
        for sample in samples:
            magnitude = abs(sample)
            peak = max(peak, magnitude)
            nonzero += int(sample != 0)
            square_sum += sample * sample
        total_samples += len(samples)
    rms = math.sqrt(square_sum / total_samples) if total_samples else 0.0
    return total_samples, rms, peak, nonzero


def _chunks_duration(chunks: Sequence[AudioChunk]) -> float:
    return sum(_chunk_duration_seconds(chunk) for chunk in chunks)


def _chunk_duration_seconds(chunk: AudioChunk) -> float:
    return len(chunk.data) / (chunk.sample_rate_hz * chunk.channels * 2)


def _tone_chunk(*, seconds: float, sample_rate_hz: int = 16_000) -> AudioChunk:
    frames = max(1, min(int(seconds * sample_rate_hz), 4 * sample_rate_hz))
    samples = bytearray(frames * 2)
    for index in range(frames):
        value = int(1_500 * math.sin(2 * math.pi * 440 * index / sample_rate_hz))
        struct.pack_into("<h", samples, index * 2, value)
    return AudioChunk(0, "pcm_s16le", sample_rate_hz, 1, bytes(samples))


async def _close_async_iterator(iterator: Any) -> None:
    close = getattr(iterator, "aclose", None)
    if callable(close):
        with contextlib.suppress(Exception):
            await close()


@contextlib.asynccontextmanager
async def _aclosing_async_iterator(iterator: Any):
    try:
        yield iterator
    finally:
        await _close_async_iterator(iterator)


def _safe_diagnostics(resource: Any | None) -> dict[str, Any]:
    diagnostics = getattr(resource, "diagnostics", None)
    if not callable(diagnostics):
        return {}
    try:
        values = diagnostics()
    except Exception:
        return {}
    if not isinstance(values, Mapping):
        return {}
    return {
        str(key): value
        for key, value in values.items()
        if isinstance(value, (str, int, float, bool, type(None)))
        and (not isinstance(value, str) or len(value) <= 128)
        and (not isinstance(value, float) or math.isfinite(value))
    }


def _hub_pending_task_count(hub: AudioHub | None) -> int:
    if hub is None:
        return 0
    task_attributes = (
        "_capture_task",
        "_idle_task",
        "_reader_task",
        "_local_asr_task",
    )
    active = sum(
        1
        for attribute in task_attributes
        if (task := getattr(hub, attribute, None)) is not None and not task.done()
    )
    monitors = getattr(hub, "_task_monitor_tasks", {})
    if isinstance(monitors, Mapping):
        active += sum(not task.done() for task in monitors.values())
    return active


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _device_index(device_id: str) -> int | None:
    try:
        index = int(device_id)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def _safe_code(value: str) -> bool:
    return (
        value.isascii()
        and value.isupper()
        and value.replace("_", "").isalnum()
        and len(value) <= 64
    )


def _normalise_code(value: str) -> str:
    candidate = "".join(char if char.isalnum() or char == "_" else "_" for char in value.upper())
    candidate = "_".join(part for part in candidate.split("_") if part)[:64]
    return candidate if _safe_code(candidate) else "UNCLASSIFIED_CHECK_BLOCKER"


def _mapped_blocker(code: str) -> _CheckBlocked:
    safe = _normalise_code(code)
    return _CheckBlocked(safe)


def _raise_mapped_failure(error: BaseException, *, output: bool = False) -> BaseException:
    code = getattr(error, "error_code", None)
    if isinstance(code, str) and _safe_code(code):
        if code in {
            "SOUNDDEVICE_NOT_INSTALLED",
            "PORTAUDIO_RUNTIME_UNAVAILABLE",
            "OUTPUT_DEVICE_DISCOVERY_FAILED",
            "INPUT_DEVICE_DISCOVERY_FAILED",
            "MICROPHONE_PERMISSION_DENIED",
        }:
            return _CheckBlocked(code)
        return _CheckFailure(code)
    if isinstance(error, MicrophonePermissionDenied):
        return _CheckBlocked("MICROPHONE_PERMISSION_DENIED")
    if isinstance(error, (MicrophoneUnavailable,)):
        return _CheckBlocked(_safe_failure_code(error))
    if isinstance(error, TimeoutError):
        return _CheckFailure("DEVICE_OPERATION_TIMEOUT")
    if output and isinstance(error, RuntimeError):
        return _CheckFailure("AUDIO_OUTPUT_OPERATION_FAILED")
    return _CheckFailure(_safe_failure_code(error))


def _safe_failure_code(error: BaseException) -> str:
    code = getattr(error, "error_code", None)
    if isinstance(code, str) and _safe_code(code):
        return code
    if isinstance(error, VoiceProviderFailure):
        return _normalise_code(error.error_code)
    if isinstance(error, MicrophonePermissionDenied):
        return "MICROPHONE_PERMISSION_DENIED"
    if isinstance(error, TimeoutError):
        return "STAGE_TIMEOUT"
    if isinstance(error, PermissionError):
        return "DEVICE_PERMISSION_DENIED"
    if isinstance(error, ImportError):
        return "OPTIONAL_DEPENDENCY_UNAVAILABLE"
    if isinstance(error, OSError):
        return "DEVICE_OPERATION_FAILED"
    return "CHECK_FAILED"


def _provider_error_is_blocked(error: VoiceProviderFailure) -> bool:
    return error.error_code in {
        "GEMINI_CREDENTIAL_UNAVAILABLE",
        "GEMINI_CREDENTIAL_LOOKUP_FAILED",
        "GEMINI_SDK_NOT_INSTALLED",
        "GEMINI_LIVE_DISABLED",
        "GEMINI_CLOUD_POLICY_DISABLED",
    } or error.status.value in {"unconfigured", "quota_limited"}


_LIVE_CHECKS = frozenset(
    check_id
    for check_id in ValidationProbe
    if check_id
    not in {
        ValidationProbe.PLATFORM,
        ValidationProbe.INTENT_CLASSIFICATION,
        ValidationProbe.TASK_ADMISSION_REPLAY,
        ValidationProbe.GEMINI_TOOL_BOUNDARY,
        ValidationProbe.TASK_ADMISSION,
        ValidationProbe.END_TO_END_TASK,
        ValidationProbe.PRODUCTION_COMPOSITION_GATE,
        ValidationProbe.SHUTDOWN_CLEANUP,
    }
)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="arise-voice-check",
        description=(
            "Validate the real Windows ARISE voice stack. Linux runs a host-guarded report; "
            "the JSON output separates REAL, FAKE, and REPLAY evidence."
        ),
    )
    parser.add_argument(
        "--confirm-microphone", action="store_true", help="allow bounded mic capture"
    )
    parser.add_argument(
        "--confirm-playback", action="store_true", help="allow short speaker output"
    )
    parser.add_argument(
        "--enable-live-gemini",
        "--enable-gemini",
        dest="enable_live_gemini",
        action="store_true",
        help="explicitly opt in to a bounded live Gemini check",
    )
    parser.add_argument(
        "--confirm-cloud",
        action="store_true",
        help="separately consent to Gemini network/quota use",
    )
    parser.add_argument("--vosk-model", type=Path, help="user-supplied local Vosk model directory")
    parser.add_argument("--wake-word", default="ARISE")
    parser.add_argument("--locale", default="en-US")
    parser.add_argument("--input-device", help="sounddevice input index; default device if omitted")
    parser.add_argument(
        "--output-device", help="sounddevice output index; default device if omitted"
    )
    parser.add_argument("--capture-seconds", type=float, default=6.0)
    parser.add_argument("--kokoro-model", type=Path, help="user-supplied Kokoro ONNX model file")
    parser.add_argument("--kokoro-voices", type=Path, help="user-supplied Kokoro voices file")
    parser.add_argument("--tts-voice", default="af_heart")
    parser.add_argument("--report", type=Path, help="optional path for the sanitized JSON report")
    return parser.parse_args(argv)


def _console_prompt(message: str) -> Awaitable[None]:
    async def prompt() -> None:
        print(f"\n[ARISE voice check] {message}", file=sys.stderr, flush=True)
        await asyncio.to_thread(input, "Press Enter when ready: ")

    return prompt()


def _console_notify(message: str) -> None:
    print(f"\n[ARISE voice check] {message}", file=sys.stderr, flush=True)


def _build_live_harness(args: argparse.Namespace) -> WindowsVoiceValidationHarness:
    if sys.platform != "win32":
        raise RuntimeError("live Windows adapters can only be composed on Windows")
    from arise.adapters.audio_local import (
        LocalAudioAdapterFailure,
        SoundDeviceAudioPlayback,
        SoundDeviceMicrophone,
        VoskModel,
        VoskSpeechRecognizer,
        VoskWakeWordDetector,
        WebRtcVadAdapter,
    )

    model: Any | None = None
    wake_factory: AdapterFactory | None = None
    asr_factory: AdapterFactory | None = None
    wake_block_reason = "VOSK_MODEL_PATH_REQUIRED"
    asr_block_reason = "VOSK_MODEL_PATH_REQUIRED"
    if args.vosk_model is not None:
        if not args.vosk_model.expanduser().is_dir():
            wake_block_reason = "VOSK_MODEL_DIRECTORY_NOT_FOUND"
            asr_block_reason = "VOSK_MODEL_DIRECTORY_NOT_FOUND"
        elif not _module_available("vosk"):
            wake_block_reason = "VOSK_DEPENDENCY_NOT_INSTALLED"
            asr_block_reason = "VOSK_DEPENDENCY_NOT_INSTALLED"
        else:
            model = VoskModel(args.vosk_model, locale=args.locale)

            def build_wake_detector() -> Any:
                return VoskWakeWordDetector(model, wake_word=args.wake_word)

            def build_speech_recognizer() -> Any:
                return VoskSpeechRecognizer(model)

            wake_factory = build_wake_detector
            asr_factory = build_speech_recognizer

    vad_factory: AdapterFactory | None = None
    vad_block_reason = "WEBRTC_VAD_DEPENDENCY_NOT_INSTALLED"
    if _module_available("webrtcvad"):
        vad_factory = WebRtcVadAdapter

    tts_factory: AdapterFactory | None = None
    tts_block_reason = "KOKORO_MODEL_AND_VOICE_FILES_REQUIRED"
    if args.kokoro_model is not None or args.kokoro_voices is not None:
        if args.kokoro_model is None or args.kokoro_voices is None:
            tts_block_reason = "KOKORO_MODEL_AND_VOICE_FILES_MUST_BE_PAIRED"
        elif (
            not args.kokoro_model.expanduser().is_file()
            or not args.kokoro_voices.expanduser().is_file()
        ):
            tts_block_reason = "KOKORO_MODEL_OR_VOICE_FILE_NOT_FOUND"
        elif not _module_available("kokoro_onnx"):
            tts_block_reason = "KOKORO_DEPENDENCY_NOT_INSTALLED"
        else:

            def build_tts() -> SpeechSynthesisPort:
                from kokoro_onnx import Kokoro

                try:
                    kokoro = Kokoro(
                        str(args.kokoro_model.expanduser()), str(args.kokoro_voices.expanduser())
                    )
                except Exception:
                    raise LocalAudioAdapterFailure(
                        "Kokoro local model initialization failed",
                        error_code="KOKORO_MODEL_LOAD_FAILED",
                    ) from None
                from arise.adapters.audio_local import KokoroSpeechSynthesis

                return KokoroSpeechSynthesis(
                    kokoro,
                    default_voice_id=args.tts_voice,
                    default_locale=args.locale,
                )

            tts_factory = build_tts

    provider_factory: ProviderFactory | None = None
    gemini_block_reason = "GEMINI_CREDENTIAL_CONFIGURATION_REQUIRED"
    voice_cloud_opt_in = False
    security_cloud_opt_in = False
    if args.enable_live_gemini and args.confirm_cloud:
        from arise.config.settings import AppSettings

        settings = AppSettings()
        voice_cloud_opt_in = settings.voice.enabled and settings.voice.allow_cloud
        security_cloud_opt_in = settings.security.allow_cloud_models
        if not voice_cloud_opt_in or not security_cloud_opt_in:
            gemini_block_reason = "ARISE_CLOUD_OPT_INS_REQUIRED"
        elif not _module_available("google.genai"):
            gemini_block_reason = "GEMINI_SDK_NOT_INSTALLED"
        else:
            from arise.adapters.gemini_live import GeminiLiveProvider
            from arise.adapters.secrets import KeyringSecretProvider

            def build_provider() -> LiveConversationProvider:
                return GeminiLiveProvider(
                    secret_provider=KeyringSecretProvider(),
                    api_key_secret_name=settings.voice.api_key_secret_name,
                    model_id=settings.voice.model_id,
                    enabled=True,
                    allow_cloud=True,
                    security_allows_cloud=True,
                )

            provider_factory = build_provider
    elif args.enable_live_gemini:
        gemini_block_reason = "CLOUD_CONSENT_NOT_GIVEN"

    return WindowsVoiceValidationHarness(
        microphone_factory=SoundDeviceMicrophone,
        playback_factory=lambda: SoundDeviceAudioPlayback(device_id=args.output_device),
        vad_factory=vad_factory,
        wake_factory=wake_factory,
        asr_factory=asr_factory,
        tts_factory=tts_factory,
        provider_factory=provider_factory,
        wake_word=args.wake_word,
        locale=args.locale,
        input_device_id=args.input_device,
        output_device_id=args.output_device,
        microphone_confirmed=args.confirm_microphone,
        playback_confirmed=args.confirm_playback,
        enable_live_gemini=args.enable_live_gemini,
        cloud_confirmed=args.confirm_cloud,
        voice_cloud_opt_in=voice_cloud_opt_in,
        security_cloud_opt_in=security_cloud_opt_in,
        gemini_block_reason=gemini_block_reason,
        vad_block_reason=vad_block_reason,
        wake_block_reason=wake_block_reason,
        asr_block_reason=asr_block_reason,
        tts_block_reason=tts_block_reason,
        capture_seconds=args.capture_seconds,
        mode=CheckMode.REAL,
        prompt=_console_prompt,
        notify=_console_notify,
    )


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def startup_failure_report(
    *, host_platform: str | None = None, error_code: str = "HARNESS_STARTUP_FAILED"
) -> ValidationReport:
    host = host_platform or platform.system()
    timestamp = datetime.now(UTC).isoformat()
    startup_probes: dict[ValidationProbe, CheckResult] = {}
    safe_error = _normalise_code(error_code)
    for probe in ValidationProbe:
        if probe is ValidationProbe.PLATFORM:
            startup_probes[probe] = CheckResult(
                probe,
                CheckStatus.PASS,
                CheckMode.REAL,
                details=_platform_snapshot(host),
            )
        else:
            startup_probes[probe] = CheckResult(
                probe,
                CheckStatus.BLOCKED,
                None,
                details={"reason": safe_error},
                error=safe_error,
            )
    checks = tuple(
        _aggregate_stage(stage, tuple(startup_probes[probe] for probe in probes))
        for stage, probes in _STAGE_PROBES.items()
    )
    is_windows_host = sys.platform == "win32" and host.casefold().startswith("win")
    return ValidationReport(
        runtime=_runtime_name(host),
        environment_status=(
            EnvironmentStatus.WINDOWS if is_windows_host else EnvironmentStatus.ENVIRONMENT_LIMITED
        ),
        timestamp=timestamp,
        platform_details=_platform_snapshot(host),
        run_mode="STARTUP_BLOCKED",
        checks=checks,
    )


async def _run_cli(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        host_vad_factory = None
        host_vad_block_reason = "WEBRTC_VAD_DEPENDENCY_NOT_INSTALLED"
        if _module_available("webrtcvad"):
            from arise.adapters.audio_local import WebRtcVadAdapter

            host_vad_factory = WebRtcVadAdapter
        harness = WindowsVoiceValidationHarness(
            vad_factory=host_vad_factory,
            microphone_confirmed=args.confirm_microphone,
            playback_confirmed=args.confirm_playback,
            enable_live_gemini=args.enable_live_gemini,
            cloud_confirmed=args.confirm_cloud,
            mode=CheckMode.REAL,
            capture_seconds=args.capture_seconds,
            vad_block_reason=host_vad_block_reason,
            run_host_synthetic_vad=True,
        )
        report = await harness.run()
    else:
        try:
            report = await _build_live_harness(args).run()
        except Exception:
            report = startup_failure_report()
    output = report.to_json()
    if args.report is not None:
        try:
            target = args.report.expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(output + "\n", encoding="utf-8")
        except OSError:
            print("Could not write the requested sanitized report path.", file=sys.stderr)
            return 1
    print(output)
    return {"PASS": 0, "FAILED": 1, "PARTIAL": 2, "BLOCKED": 2}[report.overall]


def main() -> None:
    args = _parse_args()
    raise SystemExit(asyncio.run(_run_cli(args)))


__all__ = [
    "CheckMode",
    "CheckResult",
    "CheckStatus",
    "EnvironmentStatus",
    "ValidationReport",
    "ValidationStage",
    "WindowsVoiceValidationHarness",
    "host_guarded_report",
    "main",
    "startup_failure_report",
]
