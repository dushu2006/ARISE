from __future__ import annotations

import asyncio
import time
import unittest
from collections.abc import AsyncIterator, Sequence

from arise.core.extensions import AudioChunk, TranscriptSegment
from arise.core.models import (
    MicrophoneStatus,
    VoiceProviderStatus,
    VoiceState,
)
from arise.core.voice import (
    AudioDevice,
    AudioHub,
    LiveEvent,
    LiveEventType,
    LiveSessionConfig,
    LiveToolCall,
    MicrophonePermissionDenied,
    VoiceActivity,
    VoiceConfig,
    VoiceEvent,
    VoiceEventKind,
    VoiceProviderFailure,
    WakeWordDetection,
    _normalize_spoken_text,
    voice_tool_declarations,
)


def audio(sequence: int, data: bytes = b"\x00\x00") -> AudioChunk:
    return AudioChunk(
        sequence=sequence,
        codec="pcm_s16le",
        sample_rate_hz=16_000,
        channels=1,
        data=data,
        captured_at_monotonic_ns=time.monotonic_ns(),
    )


class FakeMicrophone:
    def __init__(self, devices: Sequence[AudioDevice] | None = None) -> None:
        self.devices = tuple(devices or (AudioDevice("mic-1", "Test microphone", True),))
        self.queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue()
        self.closed = False

    async def list_devices(self) -> Sequence[AudioDevice]:
        return self.devices

    async def capture(self, device_id: str) -> AsyncIterator[AudioChunk]:
        assert device_id == self.devices[0].device_id
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                return
            yield chunk

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)


class DeniedMicrophone(FakeMicrophone):
    async def list_devices(self) -> Sequence[AudioDevice]:
        raise MicrophonePermissionDenied("permission denied")


class FakeVAD:
    def __init__(self, speech: bool = True) -> None:
        self.speech = speech

    async def analyze(self, chunk: AudioChunk) -> VoiceActivity:
        del chunk
        return VoiceActivity(self.speech, 0.99 if self.speech else 0.01)


class FakeWakeWord:
    def __init__(
        self,
        matches: Sequence[WakeWordDetection] = (),
        final_matches: Sequence[WakeWordDetection] = (),
    ) -> None:
        self.matches = list(matches)
        self.final_matches = list(final_matches)
        self.calls: list[AudioChunk] = []
        self.resets = 0
        self.end_calls = 0

    async def accept(self, chunk: AudioChunk) -> WakeWordDetection:
        self.calls.append(chunk)
        if self.matches:
            return self.matches.pop(0)
        return WakeWordDetection(matched=False)

    async def end_utterance(self) -> WakeWordDetection:
        self.end_calls += 1
        if self.final_matches:
            return self.final_matches.pop(0)
        return WakeWordDetection(matched=False)

    async def reset(self) -> None:
        self.resets += 1


class FakeSpeechRecognizer:
    def __init__(self, segments: Sequence[TranscriptSegment]) -> None:
        self.segments = tuple(segments)
        self.seen_audio: list[AudioChunk] = []
        self.correlation_ids: list[str] = []

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        locale: str | None = None,
        correlation_id: str,
    ) -> AsyncIterator[TranscriptSegment]:
        del locale
        self.correlation_ids.append(correlation_id)
        async for chunk in audio:
            self.seen_audio.append(chunk)
            for segment in self.segments:
                yield segment


class BlockingSpeechRecognizer:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        locale: str | None = None,
        correlation_id: str,
    ) -> AsyncIterator[TranscriptSegment]:
        del locale, correlation_id
        async for _chunk in audio:
            self.started.set()
            await asyncio.Event().wait()
            yield TranscriptSegment("unreachable", 0, 1, 1.0, True)


class FakeLiveSession:
    def __init__(self) -> None:
        self.sent_audio: list[AudioChunk] = []
        self.sent_text: list[str] = []
        self.tool_responses: list[tuple[LiveToolCall, dict[str, object]]] = []
        self.events: asyncio.Queue[LiveEvent | None | BaseException] = asyncio.Queue()
        self.output_generation = 0
        self.interruptions = 0
        self.closed = False

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent_audio.append(chunk)

    async def interrupt(self, first_user_audio: AudioChunk) -> int:
        self.interruptions += 1
        self.output_generation += 1
        await self.send_audio(first_user_audio)
        return self.output_generation

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_tool_response(self, call: LiveToolCall, response: dict[str, object]) -> None:
        self.tool_responses.append((call, response))

    async def receive(self) -> AsyncIterator[LiveEvent]:
        while True:
            event = await self.events.get()
            if event is None:
                return
            if isinstance(event, BaseException):
                raise event
            yield event

    async def close(self) -> None:
        self.closed = True
        self.events.put_nowait(None)


class FakeProvider:
    provider_id = "fake-live"

    def __init__(self, sessions: Sequence[FakeLiveSession] | None = None, failure=None) -> None:
        self.sessions = list(sessions or ())
        self.failure = failure
        self.configs: list[LiveSessionConfig] = []
        self.closed = False

    async def connect(self, config: LiveSessionConfig) -> FakeLiveSession:
        self.configs.append(config)
        if self.failure is not None:
            raise self.failure
        if self.sessions:
            return self.sessions.pop(0)
        return FakeLiveSession()

    async def close(self) -> None:
        self.closed = True


class FakePlayback:
    def __init__(self) -> None:
        self.played: list[AudioChunk] = []
        self.stops = 0
        self.closed = False

    async def play(self, chunk: AudioChunk) -> None:
        self.played.append(chunk)

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        self.closed = True


class FakeBridge:
    def __init__(self) -> None:
        self.result: dict[str, object] = {
            "status": "accepted",
            "task_id": "task-123",
            "verified": False,
            "acknowledgement": "ARISE accepted the task and is working on it.",
        }
        self.calls: list[tuple[LiveToolCall, str, str, str | None]] = []
        self.updates: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self.watch_calls: list[tuple[str, str, str, float]] = []
        self.watch_closed = False

    async def handle_tool_call(
        self,
        call: LiveToolCall,
        *,
        principal_id: str,
        session_id: str,
        locale: str,
        user_text: str | None = None,
    ) -> dict[str, object]:
        del locale
        self.calls.append((call, principal_id, session_id, user_text))
        return dict(self.result)

    async def watch_task(
        self,
        task_id: str,
        *,
        principal_id: str,
        session_id: str,
        poll_interval_seconds: float,
    ) -> AsyncIterator[dict[str, object]]:
        self.watch_calls.append((task_id, principal_id, session_id, poll_interval_seconds))
        try:
            while True:
                update = await self.updates.get()
                if update is None:
                    return
                yield update
        finally:
            self.watch_closed = True


class AdmissionGateBridge(FakeBridge):
    async def handle_tool_call(
        self,
        call: LiveToolCall,
        *,
        principal_id: str,
        session_id: str,
        locale: str,
        user_text: str | None = None,
    ) -> dict[str, object]:
        if call.name == "execute_task" and user_text is None:
            self.calls.append((call, principal_id, session_id, user_text))
            return {"status": "not_authorized", "verified": False}
        return await super().handle_tool_call(
            call,
            principal_id=principal_id,
            session_id=session_id,
            locale=locale,
            user_text=user_text,
        )


class FakeVoiceEventSink:
    def __init__(self) -> None:
        self.events: list[VoiceEvent] = []

    def emit(self, event: VoiceEvent) -> None:
        self.events.append(event)


class AudioHubTests(unittest.IsolatedAsyncioTestCase):
    def test_spoken_text_normalization_preserves_unicode_words(self) -> None:
        self.assertEqual(_normalize_spoken_text("नमस्ते, संसार!"), "नमस्ते संसार")

    def test_live_event_generation_id_must_be_a_nonnegative_integer(self) -> None:
        with self.assertRaises(ValueError):
            LiveEvent(type=LiveEventType.OUTPUT_TRANSCRIPT, generation_id=-1)
        with self.assertRaises(ValueError):
            LiveEvent(type=LiveEventType.OUTPUT_TRANSCRIPT, generation_id=True)
        self.assertEqual(
            LiveEvent(type=LiveEventType.OUTPUT_TRANSCRIPT, generation_id=0).generation_id,
            0,
        )

    def test_task_progress_poll_interval_is_bounded(self) -> None:
        for interval in (0.09, 10.01):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                VoiceConfig(task_status_poll_interval_seconds=interval)
        self.assertEqual(
            VoiceConfig(task_status_poll_interval_seconds=1.25).task_status_poll_interval_seconds,
            1.25,
        )

    async def make_hub(
        self,
        *,
        wake: FakeWakeWord | None = None,
        provider: FakeProvider | None = None,
        vad: FakeVAD | None = None,
        bridge: FakeBridge | None = None,
        config: VoiceConfig | None = None,
        event_sink: FakeVoiceEventSink | None = None,
        speech_recognizer: FakeSpeechRecognizer | None = None,
    ) -> tuple[AudioHub, FakeMicrophone, FakePlayback]:
        microphone = FakeMicrophone()
        playback = FakePlayback()
        hub = AudioHub(
            microphone=microphone,
            vad=vad or FakeVAD(),
            wake_word_detector=wake or FakeWakeWord(),
            provider=provider or FakeProvider(),
            playback=playback,
            conversation_bridge=bridge,
            config=config,
            event_sink=event_sink,
            speech_recognizer=speech_recognizer,
        )
        await hub.start()
        return hub, microphone, playback

    async def wait_for(self, predicate, timeout: float = 1.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("condition did not become true before timeout")
            await asyncio.sleep(0.005)

    async def test_dormant_audio_is_local_until_wake_word_match(self) -> None:
        session = FakeLiveSession()
        wake = FakeWakeWord(
            [
                WakeWordDetection(matched=False),
                WakeWordDetection(matched=True),
            ]
        )
        provider = FakeProvider([session])
        hub, microphone, _ = await self.make_hub(wake=wake, provider=provider)
        try:
            self.assertTrue(hub.snapshot().wake_word_enabled)
            self.assertEqual(hub.snapshot().microphone_status, MicrophoneStatus.AVAILABLE)
            await hub.process_chunk(audio(1))
            self.assertEqual(provider.configs, [])
            self.assertEqual(session.sent_audio, [])
            self.assertEqual(hub.state, VoiceState.DORMANT)

            trigger = audio(2, b"wake-and-request")
            await hub.process_chunk(trigger)
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            self.assertEqual(len(provider.configs), 1)
            self.assertIn(
                "not the computer-control authority", provider.configs[0].system_instruction
            )
            self.assertEqual(len(voice_tool_declarations()), 6)
            self.assertEqual(session.sent_audio, [trigger])
            self.assertGreater(hub.snapshot().telemetry["wake_word_latency_ms"].count, 0)
        finally:
            await hub.close()
        self.assertTrue(microphone.closed)

    async def test_dormant_wake_detector_finalizes_only_after_vad_silence(self) -> None:
        session = FakeLiveSession()
        tail = audio(99, b"\x00\x00" * 160)
        wake = FakeWakeWord(
            [WakeWordDetection(matched=False)],
            final_matches=[
                WakeWordDetection(matched=True, confidence=0.9, activation_audio=(tail,))
            ],
        )
        provider = FakeProvider([session])
        vad = FakeVAD(speech=True)
        hub, _, _ = await self.make_hub(
            wake=wake,
            provider=provider,
            vad=vad,
            config=VoiceConfig(wake_silence_timeout_seconds=0.1),
        )
        try:
            await hub.process_chunk(audio(1, b"\x01\x00" * 320))
            self.assertEqual(provider.configs, [])
            vad.speech = False
            await hub.process_chunk(audio(2, b"\x00\x00" * 3200))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            self.assertEqual(wake.end_calls, 1)
            self.assertEqual(session.sent_audio, [tail])
        finally:
            await hub.close()

    async def test_explicit_stop_releases_local_capture_and_allows_a_fresh_start(self) -> None:
        hub, microphone, _ = await self.make_hub()
        self.assertTrue(hub.snapshot().wake_word_enabled)
        self.assertIsNotNone(hub._capture_task)

        stopped = await hub.stop_listening()
        self.assertFalse(stopped.wake_word_enabled)
        self.assertEqual(stopped.state, VoiceState.DORMANT)
        self.assertIsNone(hub._capture_task)
        self.assertFalse(microphone.closed)

        restarted = await hub.start()
        self.assertTrue(restarted.wake_word_enabled)
        self.assertIsNotNone(hub._capture_task)
        await hub.close()
        self.assertTrue(microphone.closed)

    async def test_permission_failure_is_reported_without_crashing_runtime(self) -> None:
        microphone = DeniedMicrophone()
        hub = AudioHub(
            microphone=microphone,
            vad=FakeVAD(),
            wake_word_detector=FakeWakeWord(),
            provider=FakeProvider(),
            playback=FakePlayback(),
        )
        status = await hub.start()
        self.assertEqual(status.microphone_status, MicrophoneStatus.PERMISSION_DENIED)
        self.assertEqual(status.state, VoiceState.ERROR)
        self.assertEqual(status.last_error_code, "MICROPHONE_PERMISSION_DENIED")
        await hub.close()

    async def test_false_wake_does_not_open_provider(self) -> None:
        wake = FakeWakeWord([WakeWordDetection(matched=False)])
        provider = FakeProvider()
        hub, _, _ = await self.make_hub(wake=wake, provider=provider)
        try:
            await hub.process_chunk(audio(1))
            self.assertEqual(provider.configs, [])
            self.assertEqual(hub.state, VoiceState.DORMANT)
        finally:
            await hub.close()

    async def test_provider_auth_failure_is_truthful_and_does_not_raise(self) -> None:
        provider = FakeProvider(
            failure=VoiceProviderFailure(
                VoiceProviderStatus.AUTHENTICATION_FAILURE,
                retryable=False,
                error_code="GEMINI_AUTHENTICATION_FAILED",
            )
        )
        wake = FakeWakeWord([WakeWordDetection(matched=True)])
        hub, _, _ = await self.make_hub(wake=wake, provider=provider)
        try:
            await hub.process_chunk(audio(1))
            self.assertEqual(hub.state, VoiceState.ERROR)
            self.assertEqual(
                hub.snapshot().provider_status, VoiceProviderStatus.AUTHENTICATION_FAILURE
            )
            self.assertEqual(hub.snapshot().last_error_code, "GEMINI_AUTHENTICATION_FAILED")
        finally:
            await hub.close()

    async def test_barge_in_stops_playback_but_does_not_cancel_agent_task(self) -> None:
        session = FakeLiveSession()
        wake = FakeWakeWord([WakeWordDetection(matched=True)])
        bridge = FakeBridge()
        provider = FakeProvider([session])
        hub, _, playback = await self.make_hub(wake=wake, provider=provider, bridge=bridge)
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            call = LiveToolCall("call-1", "execute_task", {"text": "Open Chrome"})
            await hub._handle_tool_call(session, call)
            self.assertEqual(hub.snapshot().active_task_id, "task-123")
            session.events.put_nowait(
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(2),
                    text="ARISE accepted the task and is working on it.",
                )
            )
            await self.wait_for(lambda: hub.state is VoiceState.SPEAKING)
            await hub.process_chunk(audio(3))
            self.assertGreaterEqual(playback.stops, 1)
            self.assertEqual(hub.state, VoiceState.EXECUTING)
            self.assertEqual(session.closed, False)
            self.assertEqual(bridge.calls[0][0].name, "execute_task")
        finally:
            await hub.close()

    async def test_barge_in_interrupts_model_and_fences_stale_output_across_repeats(self) -> None:
        session = FakeLiveSession()
        event_sink = FakeVoiceEventSink()
        hub, _, playback = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
            event_sink=event_sink,
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(2),
                    text="Here is the answer.",
                    generation_id=0,
                ),
            )
            self.assertEqual([item.sequence for item in playback.played], [2])

            first_barge_audio = audio(3)
            await hub.process_chunk(first_barge_audio)
            self.assertEqual(session.interruptions, 1)
            self.assertEqual(session.sent_audio[-1], first_barge_audio)
            self.assertEqual(hub._minimum_output_generation, 1)
            played_before_stale = len(playback.played)
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(4),
                    text="stale response audio",
                    generation_id=0,
                ),
            )
            self.assertEqual(len(playback.played), played_before_stale)
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(8),
                    text="output missing a generation tag",
                ),
            )
            self.assertEqual(len(playback.played), played_before_stale)
            self.assertEqual(event_sink.events[-1].kind, VoiceEventKind.OUTPUT_GATED)

            fresh_audio = audio(5)
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=fresh_audio,
                    text="A fresh response.",
                    generation_id=1,
                ),
            )
            self.assertEqual(playback.played[-1].sequence, fresh_audio.sequence)

            hub.vad.speech = False
            await hub.process_chunk(audio(6))
            hub.vad.speech = True
            await hub.process_chunk(audio(7))
            self.assertEqual(session.interruptions, 2)
            self.assertEqual(hub._minimum_output_generation, 2)
        finally:
            await hub.close()
        self.assertTrue(playback.closed)

    async def test_task_submission_uses_merged_transcript_once_per_turn(self) -> None:
        session = FakeLiveSession()
        bridge = FakeBridge()
        hub, _, _ = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
            bridge=bridge,
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub._handle_live_event(
                session,
                LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text="ARISE, open"),
            )
            await hub._handle_live_event(
                session,
                LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text="ARISE, open Chrome"),
            )
            await hub._handle_tool_call(
                session,
                LiveToolCall("call-authorized", "execute_task", {"text": "open Chrome"}),
            )
            await hub._handle_tool_call(
                session,
                LiveToolCall("call-duplicate", "execute_task", {"text": "Delete everything"}),
            )

            self.assertEqual(len(bridge.calls), 1)
            self.assertEqual(bridge.calls[0][3], "open Chrome")
            self.assertEqual(session.tool_responses[-1][1]["status"], "not_authorized")
        finally:
            await hub.close()

    async def test_partial_transcripts_never_authorize_voice_task_admission(self) -> None:
        session = FakeLiveSession()
        bridge = AdmissionGateBridge()
        asr = FakeSpeechRecognizer(
            [
                TranscriptSegment(
                    "open Chrome",
                    start_offset_ms=0,
                    end_offset_ms=200,
                    confidence=0.0,
                    is_final=False,
                    locale="en",
                )
            ]
        )
        hub, _, _ = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
            bridge=bridge,
            speech_recognizer=asr,
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub.process_chunk(audio(2))
            await self.wait_for(lambda: len(asr.seen_audio) >= 2)
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.INPUT_TRANSCRIPT,
                    text="Open Chrome",
                    is_final=False,
                ),
            )
            await hub._handle_tool_call(
                session,
                LiveToolCall("partial-only", "execute_task", {"text": "Open Chrome"}),
            )
            self.assertIsNone(hub._local_final_transcript)
            self.assertIsNone(bridge.calls[0][3])
            self.assertEqual(session.tool_responses[-1][1]["status"], "not_authorized")
        finally:
            await hub.close()

    async def test_local_final_asr_is_canonical_for_voice_task_admission(self) -> None:
        session = FakeLiveSession()
        bridge = FakeBridge()
        asr = FakeSpeechRecognizer(
            [
                TranscriptSegment(
                    "open Chrome",
                    start_offset_ms=0,
                    end_offset_ms=400,
                    confidence=0.94,
                    is_final=True,
                    locale="en",
                )
            ]
        )
        hub, _, _ = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
            bridge=bridge,
            speech_recognizer=asr,
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub.process_chunk(audio(2))
            await self.wait_for(lambda: hub._local_final_transcript == "open Chrome")
            await hub._handle_live_event(
                session,
                LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text="Open Chrome"),
            )
            await hub._handle_tool_call(
                session,
                LiveToolCall("local-final", "execute_task", {"text": "open Chrome"}),
            )
            self.assertEqual(bridge.calls[0][3], "open Chrome")
            self.assertEqual(bridge.calls[0][0].arguments["text"], "open Chrome")
        finally:
            await hub.close()

    async def test_mismatched_provider_and_local_final_transcripts_fail_closed(self) -> None:
        session = FakeLiveSession()
        bridge = AdmissionGateBridge()
        asr = FakeSpeechRecognizer(
            [TranscriptSegment("open Chrome", 0, 400, 0.94, True, locale="en")]
        )
        hub, _, _ = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
            bridge=bridge,
            speech_recognizer=asr,
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub.process_chunk(audio(2))
            await self.wait_for(lambda: hub._local_final_transcript == "open Chrome")
            await hub._handle_live_event(
                session,
                LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text="delete all files"),
            )
            await hub._handle_tool_call(
                session,
                LiveToolCall("mismatched-local", "execute_task", {"text": "delete all files"}),
            )
            self.assertIsNone(hub._validated_user_text())
            self.assertIsNone(bridge.calls[0][3])
            self.assertEqual(session.tool_responses[-1][1]["status"], "not_authorized")
        finally:
            await hub.close()

    async def test_local_asr_queue_overflow_fails_closed_for_task_admission(self) -> None:
        session = FakeLiveSession()
        recognizer = BlockingSpeechRecognizer()
        bridge = FakeBridge()
        hub, _, _ = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
            bridge=bridge,
            speech_recognizer=recognizer,
            config=VoiceConfig(local_asr_queue_chunks=1),
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await asyncio.wait_for(recognizer.started.wait(), timeout=1)
            await hub.process_chunk(audio(2))
            await hub.process_chunk(audio(3))
            self.assertFalse(hub._local_asr_healthy)
            self.assertIsNone(hub._validated_user_text())
            self.assertEqual(hub.snapshot().last_error_code, "VOICE_ASR_BACKPRESSURE")
        finally:
            await hub.close()

    async def test_unverified_task_claims_are_gated_even_for_misclassified_questions(self) -> None:
        session = FakeLiveSession()
        hub, _, playback = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.INPUT_TRANSCRIPT,
                    text="Would you be able to open Chrome?",
                ),
            )
            self.assertFalse(hub._task_claim_guard)
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(2),
                    text="Chrome is open.",
                ),
            )
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(3),
                    text="I opened Chrome.",
                ),
            )
            self.assertEqual(playback.played, [])
            await hub._handle_live_event(
                session,
                LiveEvent(type=LiveEventType.OUTPUT_AUDIO, audio=audio(4), text=None),
            )
            self.assertEqual(playback.played, [])

            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(5),
                    text="To open Chrome, select its icon from the app launcher.",
                ),
            )
            self.assertEqual([chunk.sequence for chunk in playback.played], [5])
        finally:
            await hub.close()

    async def test_unverified_completion_audio_is_gated_until_runtime_verifies(self) -> None:
        session = FakeLiveSession()
        bridge = FakeBridge()
        hub, _, playback = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
            bridge=bridge,
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub._handle_live_event(
                session,
                LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text="Open Chrome"),
            )
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(2),
                    text="Chrome is open.",
                ),
            )
            self.assertEqual(playback.played, [])

            call = LiveToolCall("call-2", "execute_task", {"text": "Open Chrome"})
            await hub._handle_tool_call(session, call)
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(3),
                    text="ARISE accepted the task and is working on it.",
                ),
            )
            self.assertEqual([item.sequence for item in playback.played], [3])
            await hub._handle_live_event(
                session,
                LiveEvent(type=LiveEventType.OUTPUT_AUDIO, audio=audio(4), text=None),
            )
            self.assertEqual([item.sequence for item in playback.played], [3])
        finally:
            await hub.close()

    async def test_verified_task_summary_is_the_only_authorized_completion_response(self) -> None:
        session = FakeLiveSession()
        bridge = FakeBridge()
        hub, _, playback = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
            bridge=bridge,
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub._handle_live_event(
                session,
                LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text="Open Chrome"),
            )
            bridge.result = {
                "status": "completed",
                "task_id": "task-123",
                "verified": True,
                "summary": "ARISE reports completion after its task verifier passed.",
            }
            await hub._handle_tool_call(
                session, LiveToolCall("call-3", "get_task_status", {"task_id": "task-123"})
            )
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(5),
                    text="Chrome is open.",
                ),
            )
            self.assertEqual(playback.played, [])
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(6),
                    text="ARISE reports completion after its task verifier passed.",
                ),
            )
            self.assertEqual([item.sequence for item in playback.played], [6])
        finally:
            await hub.close()

    async def test_task_progress_is_announced_only_from_runtime_and_verified_completion_is_gated(
        self,
    ) -> None:
        session = FakeLiveSession()
        bridge = FakeBridge()
        event_sink = FakeVoiceEventSink()
        hub, _, playback = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
            bridge=bridge,
            event_sink=event_sink,
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub._handle_tool_call(
                session, LiveToolCall("call-progress", "execute_task", {"text": "Open Chrome"})
            )
            await self.wait_for(lambda: len(bridge.watch_calls) == 1)
            task_id, principal_id, session_id, interval = bridge.watch_calls[0]
            self.assertEqual(task_id, "task-123")
            self.assertEqual(principal_id, "local-user")
            self.assertEqual(session_id, hub.snapshot().active_session_id)
            self.assertEqual(interval, hub.config.task_status_poll_interval_seconds)

            await hub._handle_live_event(session, LiveEvent(type=LiveEventType.TURN_COMPLETE))
            bridge.updates.put_nowait(
                {
                    "task_id": task_id,
                    "status": "queued",
                    "state": "queued",
                    "verified": False,
                    "summary": "ARISE queued your task.",
                }
            )
            await self.wait_for(lambda: bool(session.sent_text))
            queued_message = session.sent_text[-1]
            self.assertIn("ARISE_RUNTIME_UPDATE", queued_message)
            self.assertIn("state=queued; verified=false", queued_message)
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(2),
                    text="ARISE queued your task.",
                ),
            )
            self.assertEqual([chunk.sequence for chunk in playback.played], [2])

            bridge.updates.put_nowait(
                {
                    "task_id": task_id,
                    "status": "running",
                    "state": "running",
                    "verified": False,
                    "summary": "ARISE is still working on the task.",
                }
            )
            bridge.updates.put_nowait(
                {
                    "task_id": task_id,
                    "status": "completed",
                    "state": "completed",
                    "verified": True,
                    "summary": "ARISE reports completion after its task verifier passed.",
                }
            )
            await self.wait_for(
                lambda: (
                    hub._pending_runtime_update is not None
                    and hub._pending_runtime_update[1].get("state") == "completed"
                )
            )
            self.assertIsNone(hub.snapshot().active_task_id)
            await hub._handle_live_event(session, LiveEvent(type=LiveEventType.TURN_COMPLETE))
            await self.wait_for(lambda: len(session.sent_text) == 2)
            runtime_message = session.sent_text[-1]
            self.assertIn("ARISE_RUNTIME_UPDATE", runtime_message)
            self.assertIn("state=completed; verified=true", runtime_message)

            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(3),
                    text="Chrome is open.",
                ),
            )
            self.assertEqual([chunk.sequence for chunk in playback.played], [2])
            await hub._handle_live_event(
                session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=audio(4),
                    text="ARISE reports completion after its task verifier passed.",
                ),
            )
            self.assertEqual([chunk.sequence for chunk in playback.played], [2, 4])
            self.assertTrue(
                any(event.kind is VoiceEventKind.TASK_STATUS for event in event_sink.events)
            )
        finally:
            await hub.close()
        self.assertTrue(bridge.watch_closed)

    async def test_deactivation_preserves_task_watch_then_close_cancels_it(self) -> None:
        session = FakeLiveSession()
        resumed_session = FakeLiveSession()
        provider = FakeProvider([session, resumed_session])
        bridge = FakeBridge()
        wake = FakeWakeWord([WakeWordDetection(matched=True), WakeWordDetection(matched=True)])
        hub, _, _ = await self.make_hub(
            wake=wake,
            provider=provider,
            bridge=bridge,
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub._handle_tool_call(
                session, LiveToolCall("call-cleanup", "execute_task", {"text": "Open Chrome"})
            )
            await self.wait_for(lambda: len(bridge.watch_calls) == 1)
            self.assertEqual(hub.snapshot().active_task_id, "task-123")
            await hub.deactivate()
            self.assertEqual(hub.state, VoiceState.DORMANT)
            self.assertEqual(hub.snapshot().active_task_id, "task-123")
            self.assertEqual(len(hub._task_monitor_tasks), 1)
            self.assertFalse(bridge.watch_closed)
            self.assertTrue(session.closed)

            await hub.process_chunk(audio(2))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            self.assertEqual(len(provider.configs), 2)
            bridge.updates.put_nowait(
                {
                    "task_id": "task-123",
                    "status": "running",
                    "state": "running",
                    "verified": False,
                    "summary": "ARISE is still working on the task.",
                }
            )
            await self.wait_for(
                lambda: (
                    hub._pending_runtime_update is not None
                    and hub._pending_runtime_update[1].get("state") == "running"
                )
            )
            await hub._handle_live_event(
                resumed_session, LiveEvent(type=LiveEventType.TURN_COMPLETE)
            )
            await self.wait_for(lambda: bool(resumed_session.sent_text))
            self.assertIn("state=running; verified=false", resumed_session.sent_text[0])

            await hub.close()
            self.assertEqual(hub._task_monitor_tasks, {})
            self.assertTrue(bridge.watch_closed)
        finally:
            await hub.close()

    async def test_inactivity_timeout_returns_conversation_to_dormant(self) -> None:
        session = FakeLiveSession()
        wake = FakeWakeWord([WakeWordDetection(matched=True)])
        hub, _, _ = await self.make_hub(
            wake=wake,
            provider=FakeProvider([session]),
            config=VoiceConfig(inactivity_timeout_seconds=5, reconnect_backoff_seconds=0),
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            hub._last_activity = time.monotonic() - 6
            watchdog = asyncio.create_task(hub._idle_watchdog())
            await self.wait_for(lambda: hub.state is VoiceState.DORMANT, timeout=2)
            self.assertTrue(session.closed)
            self.assertEqual(wake.resets, 1)
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        finally:
            await hub.close()

    async def test_receive_disconnect_reconnects_with_same_conversation_context(self) -> None:
        first = FakeLiveSession()
        second = FakeLiveSession()
        provider = FakeProvider([first, second])
        hub, _, _ = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=provider,
            config=VoiceConfig(max_reconnect_attempts=1, reconnect_backoff_seconds=0),
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            first.events.put_nowait(LiveEvent(type=LiveEventType.GO_AWAY))
            await self.wait_for(lambda: len(provider.configs) == 2)
            await self.wait_for(
                lambda: hub.snapshot().provider_status is VoiceProviderStatus.CONNECTED
            )
            self.assertTrue(first.closed)
            self.assertEqual(provider.configs[0].session_id, provider.configs[1].session_id)
            self.assertEqual(hub.state, VoiceState.LISTENING)
        finally:
            await hub.close()

    async def test_task_cancellation_tool_result_does_not_claim_success(self) -> None:
        session = FakeLiveSession()
        bridge = FakeBridge()
        hub, _, _ = await self.make_hub(
            wake=FakeWakeWord([WakeWordDetection(matched=True)]),
            provider=FakeProvider([session]),
            bridge=bridge,
        )
        try:
            await hub.process_chunk(audio(1))
            await self.wait_for(lambda: hub.state is VoiceState.LISTENING)
            await hub._handle_tool_call(
                session,
                LiveToolCall("call-4", "cancel_task", {"task_id": "task-123"}),
            )
            call, response = session.tool_responses[-1]
            self.assertEqual(call.name, "cancel_task")
            self.assertFalse(response["verified"])
        finally:
            await hub.close()

    async def test_immediate_local_acknowledgement_and_tts_streaming_without_cloud_round_trip(
        self,
    ) -> None:
        class FakeSynthesizer:
            def __init__(self) -> None:
                self.synthesized_texts: list[str] = []

            async def synthesize(
                self,
                text: str,
                *,
                locale: str | None = None,
                correlation_id: str,
            ) -> AsyncIterator[AudioChunk]:
                del locale, correlation_id
                self.synthesized_texts.append(text)
                yield audio(101, b"\x01\x02" * 160)
                yield audio(102, b"\x03\x04" * 160)

        synthesizer = FakeSynthesizer()
        microphone = FakeMicrophone()
        playback = FakePlayback()
        event_sink = FakeVoiceEventSink()
        hub = AudioHub(
            microphone=microphone,
            vad=FakeVAD(),
            wake_word_detector=FakeWakeWord(),
            provider=None,
            playback=playback,
            speech_synthesizer=synthesizer,
            event_sink=event_sink,
        )
        await hub.start()
        try:
            ack = await hub.acknowledge_locally("Open Chrome and search NVIDIA", speak=True)
            self.assertEqual(
                ack, "ARISE heard your action request; admission is not yet confirmed."
            )
            self.assertEqual(synthesizer.synthesized_texts, [ack])
            self.assertEqual([c.sequence for c in playback.played], [101, 102])
            self.assertIn("local_acknowledgement_latency_ms", hub.snapshot().telemetry)
            self.assertTrue(
                any(e.kind is VoiceEventKind.LOCAL_ACKNOWLEDGED for e in event_sink.events)
            )
            # Unverified claim cannot be spoken via speak_text
            unverified_chunks = await hub.speak_text("I have opened Chrome and completed the task.")
            self.assertEqual(unverified_chunks, 0)
            self.assertEqual([c.sequence for c in playback.played], [101, 102])
        finally:
            await hub.close()

    async def test_device_loss_recovery_rebinds_to_fallback_microphone_device(self) -> None:
        from arise.core.voice import MicrophoneUnavailable

        class FlakyHotSwapMicrophone(FakeMicrophone):
            def __init__(self) -> None:
                super().__init__((AudioDevice("usb-mic-1", "USB Mic", True),))
                self.capture_attempts = 0

            async def list_devices(self) -> Sequence[AudioDevice]:
                if self.capture_attempts >= 1:
                    return (AudioDevice("builtin-mic-2", "Built-in Array", True),)
                return self.devices

            async def capture(self, device_id: str) -> AsyncIterator[AudioChunk]:
                self.capture_attempts += 1
                if self.capture_attempts == 1:
                    assert device_id == "usb-mic-1"
                    yield audio(1, b"\x01\x00" * 160)
                    raise MicrophoneUnavailable("USB device unplugged")
                assert device_id == "builtin-mic-2"
                while True:
                    chunk = await self.queue.get()
                    if chunk is None:
                        return
                    yield chunk

        microphone = FlakyHotSwapMicrophone()
        playback = FakePlayback()
        event_sink = FakeVoiceEventSink()
        hub = AudioHub(
            microphone=microphone,
            vad=FakeVAD(),
            wake_word_detector=FakeWakeWord(),
            provider=FakeProvider(),
            playback=playback,
            config=VoiceConfig(
                microphone_device_id="usb-mic-1",
                max_device_recovery_attempts=2,
                device_recovery_backoff_seconds=0.0,
            ),
            event_sink=event_sink,
        )
        await hub.start()
        try:
            await self.wait_for(lambda: microphone.capture_attempts >= 2)
            self.assertEqual(hub._device_id, "builtin-mic-2")
            self.assertEqual(hub.snapshot().microphone_status, MicrophoneStatus.AVAILABLE)
            self.assertIn("device_loss_recovery_ms", hub.snapshot().telemetry)
            self.assertTrue(
                any(e.kind is VoiceEventKind.DEVICE_RECOVERED for e in event_sink.events)
            )
        finally:
            await hub.close()


if __name__ == "__main__":
    unittest.main()
