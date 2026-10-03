from __future__ import annotations

import asyncio
import json
import struct
import threading
import unittest
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import patch

from arise.adapters.audio_local import (
    KokoroSpeechSynthesis,
    LocalAudioAdapterFailure,
    SoundDeviceAudioPlayback,
    SoundDeviceMicrophone,
    VoskModel,
    VoskSpeechRecognizer,
    VoskWakeWordDetector,
    WebRtcVadAdapter,
)
from arise.core.extensions import AudioChunk
from arise.core.voice import MicrophoneUnavailable


def pcm_chunk(sequence: int, samples: int = 320, value: int = 0) -> AudioChunk:
    return AudioChunk(
        sequence=sequence,
        codec="pcm_s16le",
        sample_rate_hz=16_000,
        channels=1,
        data=struct.pack("<h", value) * samples,
    )


class FakeInputStream:
    def __init__(
        self, *, callback, blocks=(), fail_start: BaseException | None = None, **kwargs
    ) -> None:
        del kwargs
        self.callback = callback
        self.blocks = tuple(blocks)
        self.fail_start = fail_start
        self.active = False
        self.closed = False
        self.aborts = 0

    def start(self) -> None:
        if self.fail_start is not None:
            raise self.fail_start
        self.active = True
        for data, status in self.blocks:
            self.callback(data, len(data) // 2, None, status)

    def abort(self) -> None:
        self.aborts += 1
        self.active = False

    def close(self) -> None:
        self.closed = True
        self.active = False


class FakeSoundDevice:
    def __init__(self, devices, *, default=(0, 0), input_stream_factory=None) -> None:
        self.devices = devices
        self.default = SimpleNamespace(device=default)
        self.input_stream_factory = input_stream_factory
        self.input_streams: list[FakeInputStream] = []
        self.output_streams: list[FakeOutputStream] = []
        self.block_output = False
        self.fail_first_output = False

    def query_devices(self, device=None):
        if device is None:
            return list(self.devices)
        return self.devices[device]

    def check_input_settings(self, *, device, channels, samplerate, dtype):
        del device, dtype
        if (channels, samplerate) not in {(1, 16_000), (2, 16_000)}:
            raise RuntimeError("unsupported test input settings")

    def check_output_settings(self, *, device, channels, samplerate, dtype):
        del device, dtype
        if (channels, samplerate) not in {(1, 24_000), (1, 48_000), (2, 48_000)}:
            raise RuntimeError("unsupported test output settings")

    def RawInputStream(self, **kwargs):
        index = len(self.input_streams)
        stream = (
            self.input_stream_factory(index, **kwargs)
            if self.input_stream_factory is not None
            else FakeInputStream(**kwargs)
        )
        self.input_streams.append(stream)
        return stream

    def RawOutputStream(self, **kwargs):
        stream = FakeOutputStream(**kwargs)
        stream.block = self.block_output
        stream.fail_write = self.fail_first_output and not self.output_streams
        self.output_streams.append(stream)
        return stream


class FakeArray:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def astype(self, dtype, *, copy=False):
        del dtype, copy
        return self

    def tobytes(self) -> bytes:
        return self.data

    def reshape(self, shape):
        del shape
        return self


class FakeNumpy:
    @staticmethod
    def frombuffer(data, dtype):
        del dtype
        return FakeArray(bytes(data))


class FakeSoxr:
    def __init__(self) -> None:
        self.streams = []

    def ResampleStream(self, input_rate, output_rate, *, num_channels, dtype, quality):
        stream = FakeResampleStream(input_rate, output_rate, num_channels, dtype, quality)
        self.streams.append(stream)
        return stream


class FakeResampleStream:
    def __init__(self, input_rate, output_rate, channels, dtype, quality) -> None:
        self.spec = (input_rate, output_rate, channels, dtype, quality)

    def resample_chunk(self, samples):
        return samples


class FakeOutputStream:
    def __init__(self, **kwargs) -> None:
        self.kwargs = dict(kwargs)
        self.active = False
        self.closed = False
        self.aborted = threading.Event()
        self.writes: list[bytes] = []
        self.block = False
        self.fail_write = False

    def start(self) -> None:
        self.active = True

    def write(self, data: bytes) -> bool:
        if self.fail_write:
            raise RuntimeError("private playback device error")
        if self.block:
            self.aborted.wait(timeout=2)
        if self.aborted.is_set():
            raise RuntimeError("output aborted")
        self.writes.append(bytes(data))
        return False

    def abort(self) -> None:
        self.aborted.set()
        self.active = False

    def close(self) -> None:
        self.closed = True
        self.active = False


class AudioLocalAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_portaudio_import_failure_is_reported_without_leaking_details(
        self,
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def import_without_portaudio(name, *args, **kwargs):
            if name == "sounddevice":
                raise OSError("private shared-library error")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_portaudio):
            with self.assertRaisesRegex(MicrophoneUnavailable, "PortAudio runtime"):
                SoundDeviceMicrophone()._sounddevice()
            with self.assertRaisesRegex(RuntimeError, "PortAudio runtime"):
                SoundDeviceAudioPlayback()._sounddevice()

    async def test_device_discovery_filters_channels_and_marks_input_default(self) -> None:
        sd = FakeSoundDevice(
            [
                {"name": "speaker", "max_input_channels": 0, "max_output_channels": 2},
                {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
            ],
            default=(1, 0),
        )
        microphone = SoundDeviceMicrophone(sounddevice_module=sd)
        devices = await microphone.list_devices()
        self.assertEqual(
            [(item.device_id, item.name, item.is_default) for item in devices], [("1", "mic", True)]
        )

    async def test_capture_is_bounded_drops_oldest_and_cleans_up(self) -> None:
        marker_blocks = [
            (struct.pack("<h", marker) * 320, SimpleNamespace(input_overflow=marker == 4))
            for marker in (1, 2, 3, 4)
        ]
        sd = FakeSoundDevice(
            [{"name": "mic", "max_input_channels": 1, "default_samplerate": 16_000}],
            input_stream_factory=lambda _index, **kwargs: FakeInputStream(
                **kwargs, blocks=marker_blocks
            ),
        )
        microphone = SoundDeviceMicrophone(
            queue_frames=2,
            reconnect_attempts=0,
            sounddevice_module=sd,
        )
        capture = microphone.capture("0")
        chunk = await anext(capture)
        self.assertEqual(struct.unpack_from("<h", chunk.data)[0], 3)
        self.assertEqual(chunk.sample_rate_hz, 16_000)
        self.assertEqual(chunk.channels, 1)
        await microphone.close()
        await capture.aclose()
        diagnostics = microphone.diagnostics()
        self.assertEqual(diagnostics["capture_queue_drops"], 2)
        self.assertEqual(diagnostics["capture_input_overflows"], 1)
        self.assertTrue(sd.input_streams[0].closed)

    async def test_capture_downmixes_and_resamples_unsupported_native_rate(self) -> None:
        stereo = struct.pack("<hh", 1000, -1000) * 882
        sd = FakeSoundDevice(
            [
                {
                    "name": "44k stereo mic",
                    "max_input_channels": 2,
                    "default_samplerate": 44_100,
                }
            ],
            input_stream_factory=lambda _index, **kwargs: FakeInputStream(
                **kwargs, blocks=((stereo, SimpleNamespace(input_overflow=False)),)
            ),
        )

        def check_input_settings(*, device, channels, samplerate, dtype):
            del device, dtype
            if (channels, samplerate) != (2, 44_100):
                raise RuntimeError("unsupported")

        sd.check_input_settings = check_input_settings
        soxr = FakeSoxr()
        microphone = SoundDeviceMicrophone(
            reconnect_attempts=0,
            sounddevice_module=sd,
            numpy_module=FakeNumpy,
            soxr_module=soxr,
        )
        capture = microphone.capture("0")
        chunk = await anext(capture)
        self.assertEqual(chunk.sample_rate_hz, 16_000)
        self.assertEqual(chunk.channels, 1)
        self.assertEqual(microphone.diagnostics()["selected_sample_rate_hz"], 44_100)
        self.assertEqual(microphone.diagnostics()["selected_channels"], 2)
        self.assertEqual(soxr.streams[0].spec[:3], (44_100, 16_000, 1))
        self.assertEqual(set(chunk.data), {0})
        await microphone.close()
        await capture.aclose()

    async def test_capture_retries_transient_stream_failure_but_redacts_error(self) -> None:
        marker = struct.pack("<h", 9) * 320

        def factory(index, **kwargs):
            if index == 0:
                return FakeInputStream(**kwargs, fail_start=RuntimeError("private device detail"))
            return FakeInputStream(**kwargs, blocks=((marker, None),))

        sd = FakeSoundDevice(
            [{"name": "mic", "max_input_channels": 1, "default_samplerate": 16_000}],
            input_stream_factory=factory,
        )
        microphone = SoundDeviceMicrophone(
            reconnect_attempts=1,
            reconnect_backoff_seconds=0,
            sounddevice_module=sd,
        )
        capture = microphone.capture("0")
        chunk = await asyncio.wait_for(anext(capture), timeout=1)
        self.assertEqual(chunk.data, marker)
        self.assertEqual(microphone.diagnostics()["capture_reconnects"], 1)
        await microphone.close()
        await capture.aclose()

    async def test_vad_buffers_exact_web_rtc_frames_and_reports_ratio(self) -> None:
        class Vad:
            def __init__(self, mode):
                self.mode = mode
                self.calls = 0

            def is_speech(self, frame, sample_rate):
                self.calls += 1
                self.asserted_rate = sample_rate
                return frame[:2] == b"\x01\x00"

        made = []

        def factory(mode):
            vad = Vad(mode)
            made.append(vad)
            return vad

        adapter = WebRtcVadAdapter(vad_factory=factory, speech_frame_ratio=0.5)
        data = struct.pack("<h", 1) * 320 + struct.pack("<h", 0) * 320
        chunk = AudioChunk(0, "pcm_s16le", 16_000, 1, data)
        activity = await adapter.analyze(chunk)
        self.assertTrue(activity.speech)
        self.assertEqual(activity.confidence, 0.5)
        self.assertEqual(made[0].calls, 2)
        self.assertEqual(made[0].asserted_rate, 16_000)

    async def test_vosk_wake_requires_confidence_and_hands_off_only_post_keyword_audio(
        self,
    ) -> None:
        result = {
            "text": "hello arise open chrome",
            "result": [
                {"word": "hello", "start": 0.0, "end": 0.02, "conf": 0.99},
                {"word": "arise", "start": 0.03, "end": 0.05, "conf": 0.94},
                {"word": "open", "start": 0.055, "end": 0.08, "conf": 0.92},
            ],
        }

        class Recognizer:
            def __init__(self, *, accepted_after=4, result_value=result):
                self.calls = 0
                self.accepted_after = accepted_after
                self.result_value = result_value
                self.words_enabled = False
                self.resets = 0

            def SetWords(self, enabled):
                self.words_enabled = enabled

            def AcceptWaveform(self, data):
                del data
                self.calls += 1
                return self.calls >= self.accepted_after

            def Result(self):
                return json.dumps(self.result_value)

            def FinalResult(self):
                return json.dumps(self.result_value)

            def Reset(self):
                self.resets += 1
                self.calls = 0

        recognizers = []

        def factory(model, rate, grammar):
            self.assertIsNotNone(model)
            self.assertEqual(rate, 16_000)
            self.assertIn("ARISE", grammar)
            recognizer = Recognizer()
            recognizers.append(recognizer)
            return recognizer

        detector = VoskWakeWordDetector(
            VoskModel(model=object(), locale="en"),
            wake_word="ARISE",
            min_confidence=0.8,
            recognizer_factory=factory,
        )
        detections = [await detector.accept(pcm_chunk(i, value=i)) for i in range(1, 5)]
        detection = detections[-1]
        self.assertTrue(detection.matched)
        self.assertAlmostEqual(detection.confidence, 0.94)
        self.assertEqual(len(detection.activation_audio), 1)
        handed_off = detection.activation_audio[0]
        samples = struct.unpack("<" + "h" * (len(handed_off.data) // 2), handed_off.data)
        self.assertEqual(samples[:160], (3,) * 160)
        self.assertEqual(samples[160:], (4,) * 320)
        self.assertTrue(recognizers[0].words_enabled)
        self.assertGreater(recognizers[0].resets, 0)

        low_confidence = dict(result)
        low_confidence["result"] = [{"word": "arise", "start": 0.0, "end": 0.02, "conf": 0.2}]
        low_detector = VoskWakeWordDetector(
            VoskModel(model=object()),
            min_confidence=0.8,
            recognizer_factory=lambda *_: Recognizer(accepted_after=1, result_value=low_confidence),
        )
        rejected = await low_detector.accept(pcm_chunk(5))
        self.assertFalse(rejected.matched)
        self.assertGreaterEqual(low_detector.diagnostics()["wake_confidence_rejections"], 1)

    async def test_vosk_asr_emits_partials_and_final_with_confidence(self) -> None:
        class Recognizer:
            def __init__(self):
                self.calls = 0
                self.words_enabled = False

            def SetWords(self, enabled):
                self.words_enabled = enabled

            def AcceptWaveform(self, data):
                del data
                self.calls += 1
                return self.calls == 2

            def PartialResult(self):
                return json.dumps({"partial": "open"})

            def Result(self):
                return json.dumps(
                    {
                        "text": "open chrome",
                        "result": [
                            {"word": "open", "conf": 0.9},
                            {"word": "chrome", "conf": 0.8},
                        ],
                    }
                )

            def FinalResult(self):
                return json.dumps({"text": "", "result": []})

        recognizers = []

        def factory(model, rate):
            self.assertIsNotNone(model)
            self.assertEqual(rate, 16_000)
            instance = Recognizer()
            recognizers.append(instance)
            return instance

        async def input_audio():
            yield pcm_chunk(1)
            yield pcm_chunk(2)

        asr = VoskSpeechRecognizer(
            VoskModel(model=object(), locale="en"), recognizer_factory=factory
        )
        segments = [
            segment
            async for segment in asr.transcribe(
                input_audio(), locale="en-US", correlation_id="voice-session"
            )
        ]
        self.assertEqual([segment.is_final for segment in segments], [False, True])
        self.assertEqual(segments[0].confidence, 0.0)
        self.assertEqual(segments[1].text, "open chrome")
        self.assertAlmostEqual(segments[1].confidence, 0.85)
        self.assertTrue(recognizers[0].words_enabled)
        self.assertEqual(asr.diagnostics()["asr_active_streams"], 0)

    async def test_vosk_model_and_inference_failures_have_safe_codes(self) -> None:
        def broken_model_factory(path: str):
            del path
            raise RuntimeError("private model path and provider detail")

        model = VoskModel(model_path=".", model_factory=broken_model_factory)
        with self.assertRaises(LocalAudioAdapterFailure) as model_error:
            await model.load()
        self.assertEqual(model_error.exception.error_code, "VOSK_MODEL_LOAD_FAILED")
        self.assertNotIn("private", str(model_error.exception))

        class BrokenRecognizer:
            def AcceptWaveform(self, data):
                del data
                raise RuntimeError("sensitive transcript")

        asr = VoskSpeechRecognizer(
            VoskModel(model=object()), recognizer_factory=lambda *_: BrokenRecognizer()
        )
        with self.assertRaises(LocalAudioAdapterFailure) as asr_error:
            _ = [
                segment
                async for segment in asr.transcribe(
                    _one_chunk(), locale="en", correlation_id="safe-model-session"
                )
            ]
        self.assertEqual(asr_error.exception.error_code, "VOSK_ASR_INFERENCE_FAILED")
        self.assertNotIn("sensitive", str(asr_error.exception))

    async def test_vosk_asr_bounds_concurrent_streams_and_releases_slot(self) -> None:
        class Recognizer:
            def SetWords(self, enabled):
                self.words_enabled = enabled

            def AcceptWaveform(self, data):
                del data
                return True

            def Result(self):
                return json.dumps(
                    {
                        "text": "open chrome",
                        "result": [{"word": "open", "conf": 0.9}],
                    }
                )

            def FinalResult(self):
                return json.dumps({"text": "", "result": []})

        asr = VoskSpeechRecognizer(
            VoskModel(model=object()),
            recognizer_factory=lambda *_: Recognizer(),
            max_concurrent_streams=1,
            admission_timeout_seconds=0.1,
        )

        async def held_audio():
            yield pcm_chunk(1)
            await asyncio.Event().wait()

        first = asr.transcribe(held_audio(), locale="en", correlation_id="first-session")
        segment = await anext(first)
        self.assertTrue(segment.is_final)
        self.assertEqual(asr.diagnostics()["asr_active_streams"], 1)

        class CloseTrackingAudio:
            def __init__(self):
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                return pcm_chunk(2)

            async def aclose(self):
                self.closed = True

        rejected_audio = CloseTrackingAudio()
        second = asr.transcribe(rejected_audio, locale="en", correlation_id="second-session")
        with self.assertRaisesRegex(RuntimeError, "ASR capacity"):
            await anext(second)
        self.assertTrue(rejected_audio.closed)
        await first.aclose()
        self.assertEqual(asr.diagnostics()["asr_active_streams"], 0)
        recovered = [
            item
            async for item in asr.transcribe(
                _one_chunk(), locale="en", correlation_id="recovered-session"
            )
        ]
        self.assertEqual(recovered[0].text, "open chrome")

    async def test_vosk_asr_rejects_wrong_locale_and_sanitizes_runtime_failure(self) -> None:
        model = VoskModel(model=object(), locale="fr")
        asr = VoskSpeechRecognizer(model, recognizer_factory=lambda *_: None)
        with self.assertRaisesRegex(ValueError, "locale"):
            _ = [
                segment
                async for segment in asr.transcribe(
                    _empty_audio(), locale="en-US", correlation_id="session"
                )
            ]

        class BrokenRecognizer:
            def AcceptWaveform(self, data):
                del data
                raise RuntimeError("secret transcript and device details")

        broken = VoskSpeechRecognizer(
            VoskModel(model=object()), recognizer_factory=lambda *_: BrokenRecognizer()
        )
        with self.assertRaisesRegex(RuntimeError, "local streaming") as error:
            _ = [
                segment
                async for segment in broken.transcribe(
                    _one_chunk(), locale="en", correlation_id="safe-session"
                )
            ]
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("device details", str(error.exception))

    async def test_kokoro_streaming_tts_chunks_and_closes_on_cancellation(self) -> None:
        class FakeKokoro:
            def __init__(self):
                self.closed = False
                self.received = None

            def create_stream(self, text, *, voice, lang):
                self.received = (text, voice, lang)

                async def generate():
                    try:
                        yield ([0.0, 0.5, -0.5, 1.2], 24_000)
                        await asyncio.Event().wait()
                    finally:
                        self.closed = True

                return generate()

        model = FakeKokoro()
        tts = KokoroSpeechSynthesis(
            model,
            default_voice_id="af_heart",
            output_chunk_bytes=4,
        )
        output = tts.synthesize("Hello ARISE", locale="en-US", correlation_id="session")
        first = await anext(output)
        self.assertEqual(first.sample_rate_hz, 24_000)
        self.assertEqual(first.channels, 1)
        self.assertEqual(len(first.data), 4)
        self.assertEqual(model.received, ("Hello ARISE", "af_heart", "en-us"))
        await output.aclose()
        self.assertTrue(model.closed)
        self.assertEqual(tts.diagnostics()["tts_active_streams"], 0)

    async def test_kokoro_inference_failure_reports_a_safe_code(self) -> None:
        class BrokenKokoro:
            def create_stream(self, text, *, voice, lang):
                del text, voice, lang

                async def generate():
                    raise RuntimeError("private inference details")
                    yield (), 24_000

                return generate()

        tts = KokoroSpeechSynthesis(
            BrokenKokoro(), default_voice_id="af_heart", output_chunk_bytes=4
        )
        with self.assertRaises(LocalAudioAdapterFailure) as error:
            _ = [chunk async for chunk in tts.synthesize("safe probe", correlation_id="tts-probe")]
        self.assertEqual(error.exception.error_code, "KOKORO_TTS_INFERENCE_FAILED")
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(tts.diagnostics()["tts_active_streams"], 0)

    async def test_tts_bounds_concurrent_streams_and_releases_slot_after_cancel(self) -> None:
        class FakeKokoro:
            def __init__(self):
                self.calls = 0

            def create_stream(self, text, *, voice, lang):
                del text, voice, lang
                self.calls += 1

                async def generate():
                    yield ([0.1, 0.2], 24_000)
                    await asyncio.Event().wait()

                return generate()

        model = FakeKokoro()
        tts = KokoroSpeechSynthesis(
            model,
            default_voice_id="af_heart",
            output_chunk_bytes=4,
            max_concurrent_streams=1,
            admission_timeout_seconds=0.1,
        )
        first = tts.synthesize("one", correlation_id="one-session")
        await anext(first)
        second = tts.synthesize("two", correlation_id="two-session")
        with self.assertRaisesRegex(RuntimeError, "TTS capacity"):
            await anext(second)
        self.assertEqual(model.calls, 1)
        await first.aclose()
        recovered = tts.synthesize("three", correlation_id="three-session")
        await anext(recovered)
        self.assertEqual(model.calls, 2)
        await recovered.aclose()
        self.assertEqual(tts.diagnostics()["tts_active_streams"], 0)

    async def test_playback_device_default_stream_and_barge_in_abort(self) -> None:
        sd = FakeSoundDevice(
            [
                {
                    "name": "default output",
                    "max_input_channels": 0,
                    "max_output_channels": 2,
                    "default_samplerate": 48_000,
                },
            ],
            default=(0, 0),
        )
        playback = SoundDeviceAudioPlayback(sounddevice_module=sd)
        devices = await playback.list_devices()
        self.assertEqual(len(devices), 1)
        self.assertTrue(devices[0].is_default)
        chunk = AudioChunk(0, "pcm_s16le", 24_000, 1, struct.pack("<h", 12) * 320)
        await playback.play(chunk)
        self.assertEqual(sd.output_streams[0].writes, [chunk.data])
        await playback.stop()
        self.assertTrue(sd.output_streams[0].closed)
        self.assertEqual(playback.diagnostics()["playback_interruptions"], 1)

    async def test_playback_resamples_when_output_device_rejects_source_rate(self) -> None:
        sd = FakeSoundDevice(
            [
                {
                    "name": "48k output",
                    "max_input_channels": 0,
                    "max_output_channels": 1,
                    "default_samplerate": 48_000,
                }
            ],
            default=(0, 0),
        )

        def check_output_settings(*, device, channels, samplerate, dtype):
            del device, dtype
            if (channels, samplerate) != (1, 48_000):
                raise RuntimeError("unsupported output settings")

        sd.check_output_settings = check_output_settings
        soxr = FakeSoxr()
        playback = SoundDeviceAudioPlayback(
            sounddevice_module=sd,
            numpy_module=FakeNumpy,
            soxr_module=soxr,
        )
        chunk = AudioChunk(0, "pcm_s16le", 24_000, 1, struct.pack("<h", 5) * 320)
        await playback.play(chunk)
        self.assertEqual(playback.diagnostics()["selected_sample_rate_hz"], 48_000)
        self.assertEqual(sd.output_streams[0].kwargs["samplerate"], 48_000)
        self.assertEqual(soxr.streams[0].spec[:3], (24_000, 48_000, 1))
        self.assertEqual(sd.output_streams[0].writes, [chunk.data])
        await playback.close()

    async def test_playback_failure_closes_broken_stream_and_recovers_on_next_chunk(self) -> None:
        sd = FakeSoundDevice(
            [
                {
                    "name": "output",
                    "max_input_channels": 0,
                    "max_output_channels": 1,
                    "default_samplerate": 24_000,
                }
            ],
            default=(0, 0),
        )
        sd.fail_first_output = True
        playback = SoundDeviceAudioPlayback(sounddevice_module=sd)
        chunk = AudioChunk(0, "pcm_s16le", 24_000, 1, struct.pack("<h", 5) * 320)
        with self.assertRaisesRegex(RuntimeError, "audio output stream failed"):
            await playback.play(chunk)
        self.assertTrue(sd.output_streams[0].closed)
        await playback.play(chunk)
        self.assertEqual(len(sd.output_streams), 2)
        self.assertEqual(sd.output_streams[1].writes, [chunk.data])
        self.assertEqual(playback.diagnostics()["playback_errors"], 1)
        await playback.close()

    async def test_playback_stop_interrupts_a_blocking_write_without_racing_task_state(
        self,
    ) -> None:
        sd = FakeSoundDevice(
            [
                {
                    "name": "output",
                    "max_input_channels": 0,
                    "max_output_channels": 1,
                    "default_samplerate": 24_000,
                }
            ],
            default=(0, 0),
        )
        sd.block_output = True
        playback = SoundDeviceAudioPlayback(sounddevice_module=sd)
        chunk = AudioChunk(0, "pcm_s16le", 24_000, 1, struct.pack("<h", 5) * 320)
        play_task = asyncio.create_task(playback.play(chunk))
        deadline = asyncio.get_running_loop().time() + 1
        while not sd.output_streams:
            if asyncio.get_running_loop().time() > deadline:
                self.fail("output stream was not created")
            await asyncio.sleep(0.005)
        stream = sd.output_streams[0]
        await asyncio.sleep(0.02)
        await playback.stop()
        await asyncio.wait_for(play_task, timeout=1)
        self.assertTrue(stream.aborted.is_set())
        self.assertEqual(playback.diagnostics()["playback_errors"], 0)


async def _empty_audio() -> AsyncIterator[AudioChunk]:
    if False:
        yield pcm_chunk(0)


async def _one_chunk() -> AsyncIterator[AudioChunk]:
    yield pcm_chunk(1)
