from __future__ import annotations

import json
import math
import struct
import sys
import unittest
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace

from arise.adapters.windows_voice_validation import (
    MAX_GEMINI_INPUT_AUDIO_BYTES,
    MAX_GEMINI_OUTPUT_AUDIO_BYTES,
    MAX_GEMINI_SESSION_ATTEMPTS,
    MAX_GEMINI_TRANSCRIPT_CHARACTERS,
    CheckMode,
    CheckStatus,
    EnvironmentStatus,
    ValidationProbe,
    ValidationStage,
    WindowsVoiceValidationHarness,
    _BudgetedLiveProvider,
    _CheckBlocked,
    _CheckFailure,
    _GeminiTrafficBudget,
    _HubObserver,
    _PlaybackProbe,
    host_guarded_report,
)
from arise.core.extensions import AudioChunk, TranscriptSegment
from arise.core.voice import (
    AudioDevice,
    LiveEvent,
    LiveEventType,
    LiveSessionConfig,
    VoiceActivity,
    VoiceEvent,
    VoiceEventKind,
    VoiceState,
    WakeWordDetection,
)


def audio(sequence: int, *, speech: bool = True) -> AudioChunk:
    samples = bytearray(320 * 2)
    if speech:
        for index in range(320):
            value = int(3_000 * math.sin(2 * math.pi * 180 * index / 16_000))
            struct.pack_into("<h", samples, index * 2, value)
    return AudioChunk(
        sequence=sequence,
        codec="pcm_s16le",
        sample_rate_hz=16_000,
        channels=1,
        data=bytes(samples),
    )


class FakeMicrophone:
    def __init__(self) -> None:
        self.closed = False
        self.capture_count = 0

    async def list_devices(self) -> Sequence[AudioDevice]:
        return (AudioDevice("0", "Private Microphone Name", True),)

    def capture(self, device_id: str) -> AsyncIterator[AudioChunk]:
        assert device_id == "0"
        self.capture_count += 1
        capture_number = self.capture_count

        async def chunks() -> AsyncIterator[AudioChunk]:
            yield audio(capture_number * 10 + 1, speech=True)
            yield audio(capture_number * 10 + 2, speech=False)

        return chunks()

    async def close(self) -> None:
        self.closed = True


class FakePlayback:
    def __init__(self) -> None:
        self.closed = False
        self.plays = 0
        self.stops = 0

    async def list_devices(self) -> Sequence[AudioDevice]:
        return (AudioDevice("1", "Private Speaker Name", True),)

    async def play(self, chunk: AudioChunk) -> None:
        assert chunk.data
        self.plays += 1

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        self.closed = True

    def diagnostics(self) -> dict[str, int | bool]:
        return {
            "playback_active": False,
            "playback_closed": self.closed,
            "playback_interruptions": self.stops,
        }


class FakeVad:
    async def analyze(self, chunk: AudioChunk) -> VoiceActivity:
        speech = any(chunk.data)
        return VoiceActivity(speech=speech, confidence=0.99 if speech else 0.0)


class FakeWakeDetector:
    async def accept(self, chunk: AudioChunk) -> WakeWordDetection:
        return WakeWordDetection(matched=True, confidence=0.95, activation_audio=(chunk,))

    async def end_utterance(self) -> WakeWordDetection:
        return WakeWordDetection(matched=False)

    async def reset(self) -> None:
        return


class FakeSpeechRecognizer:
    async def transcribe(
        self,
        audio_chunks: AsyncIterator[AudioChunk],
        *,
        locale: str | None = None,
        correlation_id: str,
    ) -> AsyncIterator[TranscriptSegment]:
        del locale, correlation_id
        async for _chunk in audio_chunks:
            yield TranscriptSegment("Tell me how to open Chrome.", 0, 100, 0.93, False, "en-US")
            yield TranscriptSegment("Tell me how to open Chrome.", 0, 500, 0.97, True, "en-US")
            return

    def diagnostics(self) -> dict[str, int]:
        return {"asr_active_streams": 0}


class FakeLiveSession:
    def __init__(
        self,
        output_audio: Sequence[AudioChunk] = (),
        received_events: Sequence[LiveEvent] = (),
    ) -> None:
        self.output_audio = output_audio
        self.received_events = received_events
        self.sent_audio_bytes = 0
        self.sent_text_characters = 0
        self.closed = False

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent_audio_bytes += len(chunk.data)

    async def interrupt(self, first_user_audio: AudioChunk) -> int:
        self.sent_audio_bytes += len(first_user_audio.data)
        return 1

    async def send_text(self, text: str) -> None:
        self.sent_text_characters += len(text)

    async def send_tool_response(self, call, response) -> None:
        del call, response

    def receive(self) -> AsyncIterator[LiveEvent]:
        async def events() -> AsyncIterator[LiveEvent]:
            for chunk in self.output_audio:
                yield LiveEvent(type=LiveEventType.OUTPUT_AUDIO, audio=chunk)
            for event in self.received_events:
                yield event

        return events()

    async def close(self) -> None:
        self.closed = True


class FakeLiveProvider:
    provider_id = "fake-gemini-provider"

    def __init__(
        self,
        output_audio: Sequence[AudioChunk] = (),
        received_events: Sequence[LiveEvent] = (),
    ) -> None:
        self.output_audio = output_audio
        self.received_events = received_events
        self.sessions: list[FakeLiveSession] = []

    async def connect(self, config: LiveSessionConfig) -> FakeLiveSession:
        del config
        session = FakeLiveSession(self.output_audio, self.received_events)
        self.sessions.append(session)
        return session

    async def close(self) -> None:
        for session in self.sessions:
            await session.close()

    def diagnostics(self) -> dict[str, int]:
        return {
            "active_sessions": sum(not session.closed for session in self.sessions),
            "pending_connections": 0,
        }


def large_audio_chunk(sequence: int, size: int = 256 * 1024) -> AudioChunk:
    return AudioChunk(
        sequence=sequence,
        codec="pcm_s16le",
        sample_rate_hz=24_000,
        channels=1,
        data=bytes(size),
    )


def probe(stage_result, probe_id: str):
    return next(item for item in stage_result.subchecks if item.id.value == probe_id)


class WindowsVoiceValidationHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_linux_report_has_21_stages_and_does_not_impersonate_hardware(self) -> None:
        report = await host_guarded_report(host_platform="Linux")
        checks = {check.id: check for check in report.checks}

        self.assertEqual(len(report.checks), 21)
        self.assertEqual(report.runtime, "linux")
        self.assertEqual(report.environment_status, EnvironmentStatus.ENVIRONMENT_LIMITED)
        self.assertEqual(report.to_dict()["environment_status"], "ENVIRONMENT-LIMITED")
        self.assertEqual(report.to_dict()["schema_version"], 3)
        self.assertEqual(report.run_mode, "HOST_GUARDED")
        self.assertEqual(checks[ValidationStage.PLATFORM].status, CheckStatus.PASS)
        self.assertFalse(report.platform_details["target_is_windows"])
        self.assertEqual(checks[ValidationStage.VAD].status, CheckStatus.BLOCKED)
        self.assertEqual(
            probe(checks[ValidationStage.VAD], "vad_synthetic").error,
            "SYNTHETIC_VAD_ADAPTER_NOT_CONFIGURED",
        )
        self.assertEqual(
            probe(checks[ValidationStage.VAD], "vad_live").error,
            "WINDOWS_HOST_REQUIRED",
        )
        self.assertEqual(
            probe(checks[ValidationStage.INTENT_CLASSIFICATION], "intent_classification").status,
            CheckStatus.PASS,
        )
        replay_admission = probe(checks[ValidationStage.TASK_ADMISSION], "task_admission_replay")
        self.assertEqual(replay_admission.status, CheckStatus.PASS)
        self.assertEqual(replay_admission.mode, CheckMode.FAKE)
        self.assertEqual(checks[ValidationStage.TASK_ADMISSION].status, CheckStatus.BLOCKED)
        for stage in (
            ValidationStage.AUDIO_INPUT_DEVICE_DISCOVERY,
            ValidationStage.AUDIO_OUTPUT_DEVICE_DISCOVERY,
            ValidationStage.MICROPHONE_CAPTURE,
            ValidationStage.WAKE_ACTIVATION,
            ValidationStage.STREAMING_ASR,
            ValidationStage.SPEAKER_PLAYBACK,
            ValidationStage.END_TO_END_VOICE_TURN,
            ValidationStage.DEVICE_LOSS_RECOVERY,
        ):
            self.assertEqual(checks[stage].status, CheckStatus.BLOCKED, stage.value)
            self.assertEqual(checks[stage].mode, CheckMode.REAL, stage.value)
            self.assertEqual(checks[stage].error, "WINDOWS_HOST_REQUIRED", stage.value)
        self.assertEqual(checks[ValidationStage.GEMINI_CONNECTION].status, CheckStatus.SKIPPED)
        gemini = probe(checks[ValidationStage.GEMINI_CONNECTION], "gemini_live_connection")
        self.assertEqual(gemini.status, CheckStatus.SKIPPED)
        self.assertEqual(gemini.details["reason"], "LIVE_GEMINI_OPT_IN_NOT_SET")
        self.assertEqual(report.overall, "BLOCKED")

    async def test_live_mode_off_windows_never_constructs_injected_adapters(self) -> None:
        if sys.platform == "win32":
            self.skipTest("host guard is exercised by Windows synthetic and hardware jobs")

        def forbidden_factory():
            raise AssertionError("a live adapter must not be constructed off Windows")

        harness = WindowsVoiceValidationHarness(
            microphone_factory=forbidden_factory,
            playback_factory=forbidden_factory,
            vad_factory=forbidden_factory,
            wake_factory=forbidden_factory,
            asr_factory=forbidden_factory,
            provider_factory=forbidden_factory,
            enable_live_gemini=True,
            cloud_confirmed=True,
            voice_cloud_opt_in=True,
            security_cloud_opt_in=True,
            mode=CheckMode.REAL,
        )
        report = await harness.run()
        gemini = probe(
            next(check for check in report.checks if check.id is ValidationStage.GEMINI_CONNECTION),
            "gemini_live_connection",
        )

        self.assertEqual(report.runtime, "linux")
        self.assertEqual(report.run_mode, "HOST_GUARDED")
        self.assertEqual(report.overall, "BLOCKED")
        self.assertEqual(gemini.error, "WINDOWS_HOST_REQUIRED")

    async def test_live_gemini_without_cloud_consent_is_skipped_even_if_requested(self) -> None:
        if sys.platform == "win32":
            self.skipTest("host guard is exercised by Windows synthetic and hardware jobs")
        harness = WindowsVoiceValidationHarness(
            enable_live_gemini=True,
            cloud_confirmed=False,
            mode=CheckMode.REAL,
        )
        report = await harness.run()
        checks = {check.id: check for check in report.checks}
        gemini_stage = checks[ValidationStage.GEMINI_CONNECTION]
        gemini_connection = probe(gemini_stage, "gemini_live_connection")
        gemini_response = checks[ValidationStage.GEMINI_RESPONSE_STREAMING]

        self.assertEqual(gemini_connection.status, CheckStatus.SKIPPED)
        self.assertEqual(gemini_connection.details["reason"], "CLOUD_CONSENT_NOT_GIVEN")
        response_probe = probe(gemini_response, "gemini_response_streaming")
        self.assertEqual(response_probe.status, CheckStatus.SKIPPED)
        self.assertEqual(response_probe.details["reason"], "CLOUD_CONSENT_NOT_GIVEN")

    async def test_fake_audio_and_replay_evidence_stays_labeled(self) -> None:
        microphones: list[FakeMicrophone] = []
        playbacks: list[FakePlayback] = []

        def new_microphone() -> FakeMicrophone:
            microphone = FakeMicrophone()
            microphones.append(microphone)
            return microphone

        def new_playback() -> FakePlayback:
            playback = FakePlayback()
            playbacks.append(playback)
            return playback

        harness = WindowsVoiceValidationHarness(
            microphone_factory=new_microphone,
            playback_factory=new_playback,
            vad_factory=FakeVad,
            wake_factory=FakeWakeDetector,
            asr_factory=FakeSpeechRecognizer,
            microphone_confirmed=True,
            playback_confirmed=False,
            capture_seconds=2,
            mode=CheckMode.FAKE,
        )
        report = await harness.run()
        checks = {check.id: check for check in report.checks}

        for stage in (
            ValidationStage.AUDIO_INPUT_DEVICE_DISCOVERY,
            ValidationStage.AUDIO_OUTPUT_DEVICE_DISCOVERY,
            ValidationStage.MICROPHONE_CAPTURE,
            ValidationStage.PCM_FORMAT,
            ValidationStage.VAD,
            ValidationStage.WAKE_ACTIVATION,
            ValidationStage.STREAMING_ASR,
            ValidationStage.TRANSCRIPT,
        ):
            self.assertEqual(checks[stage].status, CheckStatus.PASS, checks[stage].to_dict())
            self.assertEqual(checks[stage].mode, CheckMode.FAKE, stage.value)
        intent = checks[ValidationStage.INTENT_CLASSIFICATION]
        self.assertEqual(intent.status, CheckStatus.PASS)
        self.assertEqual(probe(intent, "intent_from_transcript").mode, CheckMode.FAKE)
        admission = probe(checks[ValidationStage.TASK_ADMISSION], "task_admission_replay")
        self.assertEqual(admission.status, CheckStatus.PASS)
        self.assertEqual(admission.mode, CheckMode.FAKE)
        self.assertEqual(checks[ValidationStage.TASK_ADMISSION].status, CheckStatus.BLOCKED)
        self.assertEqual(checks[ValidationStage.GEMINI_CONNECTION].status, CheckStatus.SKIPPED)
        self.assertEqual(checks[ValidationStage.SPEAKER_PLAYBACK].status, CheckStatus.SKIPPED)
        self.assertEqual(checks[ValidationStage.DEVICE_LOSS_RECOVERY].status, CheckStatus.BLOCKED)
        self.assertEqual(checks[ValidationStage.SHUTDOWN_CLEANUP].status, CheckStatus.PASS)
        self.assertTrue(microphones and all(microphone.closed for microphone in microphones))
        self.assertTrue(playbacks and all(playback.closed for playback in playbacks))
        self.assertFalse(
            any(
                subcheck.mode is CheckMode.REAL
                for check in report.checks
                for subcheck in check.subchecks
                if check.id is not ValidationStage.PLATFORM
            )
        )

        serialized = report.to_json()
        self.assertNotIn("Tell me how to open Chrome.", serialized)
        self.assertNotIn("Private Microphone Name", serialized)
        self.assertNotIn("Private Speaker Name", serialized)
        self.assertIn('"raw_audio_or_transcript_persisted": false', serialized)
        self.assertEqual(len(json.loads(serialized)["checks"]), 21)

    async def test_question_command_and_ambiguous_admission_boundary(self) -> None:
        report = await WindowsVoiceValidationHarness(mode=CheckMode.REPLAY).run()
        checks = {check.id: check for check in report.checks}
        intent = checks[ValidationStage.INTENT_CLASSIFICATION]
        admission = probe(checks[ValidationStage.TASK_ADMISSION], "task_admission_replay")

        self.assertEqual(intent.status, CheckStatus.BLOCKED)
        self.assertEqual(
            probe(intent, "intent_classification").status,
            CheckStatus.PASS,
        )
        self.assertEqual(admission.status, CheckStatus.PASS)
        self.assertIs(admission.details["question_no_admission"], True)
        self.assertIs(admission.details["command_admitted_to_fake_port"], True)
        self.assertIs(admission.details["ambiguous_no_admission"], True)
        self.assertEqual(admission.details["fake_submissions"], 1)
        self.assertIs(admission.details["execution_performed"], False)

    async def test_gemini_budget_caps_aggregate_audio_and_connection_attempts(self) -> None:
        raw_provider = FakeLiveProvider()
        budgeted = _BudgetedLiveProvider(raw_provider, _GeminiTrafficBudget())
        config = LiveSessionConfig(session_id="gemini-budget-test", tool_declarations=())
        session = await budgeted.connect(config)

        for sequence in range(MAX_GEMINI_INPUT_AUDIO_BYTES // (256 * 1024)):
            await session.send_audio(large_audio_chunk(sequence))
        self.assertEqual(
            raw_provider.sessions[0].sent_audio_bytes,
            MAX_GEMINI_INPUT_AUDIO_BYTES,
        )
        with self.assertRaises(_CheckFailure) as audio_error:
            await session.send_audio(large_audio_chunk(5))
        self.assertEqual(audio_error.exception.error_code, "GEMINI_INPUT_AUDIO_BUDGET_EXCEEDED")
        self.assertEqual(raw_provider.sessions[0].sent_audio_bytes, MAX_GEMINI_INPUT_AUDIO_BYTES)

        for _ in range(MAX_GEMINI_SESSION_ATTEMPTS - 1):
            await budgeted.connect(config)
        with self.assertRaises(_CheckBlocked) as session_error:
            await budgeted.connect(config)
        self.assertEqual(
            session_error.exception.reason,
            "GEMINI_SESSION_ATTEMPT_BUDGET_EXHAUSTED",
        )
        self.assertEqual(len(raw_provider.sessions), MAX_GEMINI_SESSION_ATTEMPTS)

    async def test_gemini_budget_caps_output_audio_and_transcript_characters(self) -> None:
        output_chunks = tuple(
            large_audio_chunk(sequence)
            for sequence in range(MAX_GEMINI_OUTPUT_AUDIO_BYTES // (256 * 1024) + 1)
        )
        budgeted = _BudgetedLiveProvider(
            FakeLiveProvider(output_audio=output_chunks), _GeminiTrafficBudget()
        )
        session = await budgeted.connect(
            LiveSessionConfig(session_id="gemini-output-budget-test", tool_declarations=())
        )
        with self.assertRaises(_CheckFailure) as audio_error:
            async for _event in session.receive():
                pass
        self.assertEqual(audio_error.exception.error_code, "GEMINI_OUTPUT_AUDIO_BUDGET_EXCEEDED")

        transcript_provider = FakeLiveProvider()
        transcript_budgeted = _BudgetedLiveProvider(transcript_provider, _GeminiTrafficBudget())
        transcript_session = await transcript_budgeted.connect(
            LiveSessionConfig(session_id="gemini-transcript-budget-test", tool_declarations=())
        )
        await transcript_session.send_text("x" * MAX_GEMINI_TRANSCRIPT_CHARACTERS)
        with self.assertRaises(_CheckFailure) as transcript_error:
            await transcript_session.send_text("y")
        self.assertEqual(
            transcript_error.exception.error_code,
            "GEMINI_TRANSCRIPT_BUDGET_EXCEEDED",
        )
        self.assertEqual(
            transcript_provider.sessions[0].sent_text_characters,
            MAX_GEMINI_TRANSCRIPT_CHARACTERS,
        )

        received_events = (
            LiveEvent(
                type=LiveEventType.OUTPUT_AUDIO,
                audio=large_audio_chunk(20),
                text="x" * MAX_GEMINI_TRANSCRIPT_CHARACTERS,
            ),
            LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text="y"),
        )
        received_budget = _GeminiTrafficBudget()
        received_provider = _BudgetedLiveProvider(
            FakeLiveProvider(received_events=received_events), received_budget
        )
        received_session = await received_provider.connect(
            LiveSessionConfig(session_id="gemini-received-transcript-test", tool_declarations=())
        )
        with self.assertRaises(_CheckFailure) as received_error:
            async for _event in received_session.receive():
                pass
        self.assertEqual(
            received_error.exception.error_code,
            "GEMINI_TRANSCRIPT_BUDGET_EXCEEDED",
        )
        self.assertEqual(
            received_budget.transcript_characters,
            MAX_GEMINI_TRANSCRIPT_CHARACTERS,
        )

    def test_barge_failure_path_preserves_observed_latency_and_partial_success(self) -> None:
        harness = WindowsVoiceValidationHarness(mode=CheckMode.REPLAY)
        for check_id in ValidationProbe:
            harness._set(
                check_id,
                CheckStatus.SKIPPED,
                None,
                details={"reason": "NOT_RUN"},
            )
        harness.playback = _PlaybackProbe(FakePlayback())
        harness.playback.play_calls = 3
        harness.playback.stop_calls = 1
        observer = _HubObserver()
        observer.emit(VoiceEvent(VoiceEventKind.BARGE_IN, VoiceState.INTERRUPTED))
        hub = SimpleNamespace(
            snapshot=lambda: SimpleNamespace(
                telemetry={
                    "barge_in_detection_latency_ms": SimpleNamespace(count=1, last_latency_ms=37.5),
                    "playback_stop_latency_ms": SimpleNamespace(count=1, last_latency_ms=12.0),
                    "generation_cancel_latency_ms": SimpleNamespace(count=1, last_latency_ms=24.0),
                }
            )
        )

        harness._record_barge_in_failure(
            hub,
            observer,
            baseline_plays=0,
            baseline_stops=0,
            error_code="SECOND_INTERRUPTION_NOT_COMPLETED",
            timed_out=False,
        )

        self.assertEqual(harness._checks[ValidationProbe.BARGE_IN].status, CheckStatus.PASS)
        self.assertEqual(
            harness._checks[ValidationProbe.BARGE_IN].details["barge_in_detection_latency_ms"],
            37.5,
        )
        self.assertEqual(
            harness._checks[ValidationProbe.REPEATED_BARGE_IN].status,
            CheckStatus.FAILED,
        )
        self.assertEqual(
            harness._checks[ValidationProbe.GEMINI_GENERATION_CANCELLATION].status,
            CheckStatus.PASS,
        )
        self.assertEqual(
            harness._checks[ValidationProbe.FULL_TURN_CANCELLATION].status,
            CheckStatus.FAILED,
        )
        self.assertFalse(
            harness._checks[ValidationProbe.FULL_TURN_CANCELLATION].details[
                "task_engine_task_cancelled"
            ]
        )

    def test_check_result_rejects_unbounded_or_non_scalar_diagnostics(self) -> None:
        from arise.adapters.windows_voice_validation import CheckResult

        with self.assertRaises(ValueError):
            CheckResult(
                ValidationStage.PLATFORM,
                CheckStatus.PASS,
                CheckMode.REAL,
                details={"unsafe": {"nested": "payload"}},  # type: ignore[dict-item]
            )


if __name__ == "__main__":
    unittest.main()
