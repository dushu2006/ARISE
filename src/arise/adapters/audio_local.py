"""Optional, provider-neutral local audio adapters.

All device/model imports are lazy. Audio is bounded and ephemeral, and this module deliberately
never logs samples, transcripts, device error strings, model paths, or secrets. Model weights are
user-provided and are not bundled by ARISE.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import threading
import time
from array import array
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arise.core.contracts import validate_safe_token
from arise.core.extensions import (
    MAX_AUDIO_CHUNK_BYTES,
    AudioChunk,
    SpeechSynthesisPort,
    TranscriptSegment,
)
from arise.core.voice import (
    MAX_WAKE_HANDOFF_BYTES,
    MAX_WAKE_HANDOFF_CHUNKS,
    AudioDevice,
    AudioPlaybackUnavailable,
    MicrophonePermissionDenied,
    MicrophoneUnavailable,
    VoiceActivity,
    WakeWordDetection,
)

_PCM_CODECS = frozenset({"pcm_s16le", "pcm16"})
_VAD_SAMPLE_RATES = frozenset({8_000, 16_000, 32_000, 48_000})
_VAD_FRAME_DURATIONS_MS = frozenset({10, 20, 30})
_CAPTURE_ERROR = "MICROPHONE_CAPTURE_FAILED"


class LocalAudioAdapterFailure(RuntimeError):
    """Sanitized local audio/model failure with a stable diagnostic code."""

    def __init__(self, message: str, *, error_code: str) -> None:
        self.error_code = error_code[:64]
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _CapturedFrame:
    data: bytes
    captured_at_monotonic_ns: int


class _CallbackFrameBuffer:
    """A bounded PortAudio callback buffer with coalesced event-loop notifications."""

    def __init__(self, loop: asyncio.AbstractEventLoop, capacity: int) -> None:
        self.loop = loop
        self.capacity = capacity
        self.event = asyncio.Event()
        self._frames: deque[_CapturedFrame] = deque()
        self._lock = threading.Lock()
        self._notification_pending = False
        self._closed = False
        self._error_code: str | None = None
        self.dropped_frames = 0
        self.input_overflows = 0
        self.callback_errors = 0

    def push(self, data: bytes, *, input_overflow: bool = False) -> None:
        if not data:
            return
        notify = False
        with self._lock:
            if self._closed:
                return
            if input_overflow:
                self.input_overflows += 1
            if len(self._frames) >= self.capacity:
                self._frames.popleft()
                self.dropped_frames += 1
            self._frames.append(_CapturedFrame(data, time.monotonic_ns()))
            if not self._notification_pending:
                self._notification_pending = True
                notify = True
        if notify:
            self._notify_loop()

    def fail(self, error_code: str) -> None:
        notify = False
        with self._lock:
            if self._closed:
                return
            self.callback_errors += 1
            self._error_code = error_code[:64]
            if not self._notification_pending:
                self._notification_pending = True
                notify = True
        if notify:
            self._notify_loop()

    def close(self) -> None:
        notify = False
        with self._lock:
            self._closed = True
            self._frames.clear()
            if not self._notification_pending:
                self._notification_pending = True
                notify = True
        if notify:
            self._notify_loop()

    def _notify_loop(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:
            # The event loop is already gone; the capture owner will close the stream.
            pass

    async def wait_and_drain(self, timeout_seconds: float) -> tuple[_CapturedFrame, ...]:
        try:
            await asyncio.wait_for(self.event.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return ()
        self.event.clear()
        with self._lock:
            frames = tuple(self._frames)
            self._frames.clear()
            self._notification_pending = False
            return frames

    def status(self) -> tuple[bool, str | None]:
        with self._lock:
            return self._closed, self._error_code

    def diagnostics(self) -> dict[str, int]:
        with self._lock:
            return {
                "capture_queue_drops": self.dropped_frames,
                "capture_input_overflows": self.input_overflows,
                "capture_callback_errors": self.callback_errors,
            }


class _Pcm16Normalizer:
    """Downmix and stream-resample signed 16-bit PCM to a stable mono sample rate."""

    def __init__(
        self,
        target_rate_hz: int,
        *,
        numpy_module: Any | None = None,
        soxr_module: Any | None = None,
    ) -> None:
        self.target_rate_hz = target_rate_hz
        self._numpy = numpy_module
        self._soxr = soxr_module
        self._resampler: Any | None = None
        self._input_rate_hz: int | None = None

    def convert(self, chunk: AudioChunk) -> bytes:
        if chunk.codec not in _PCM_CODECS:
            raise ValueError("local audio adapters require signed 16-bit PCM")
        mono = _downmix_pcm16(chunk.data, chunk.channels)
        if chunk.sample_rate_hz == self.target_rate_hz:
            self._resampler = None
            self._input_rate_hz = None
            return mono
        if self._resampler is None or self._input_rate_hz != chunk.sample_rate_hz:
            numpy_module, soxr_module = self._resampling_modules()
            try:
                self._resampler = soxr_module.ResampleStream(
                    chunk.sample_rate_hz,
                    self.target_rate_hz,
                    num_channels=1,
                    dtype="int16",
                    quality="HQ",
                )
            except Exception:
                raise ValueError("sample-rate conversion could not be initialized") from None
            self._input_rate_hz = chunk.sample_rate_hz
            self._numpy = numpy_module
        try:
            source = self._numpy.frombuffer(mono, dtype="<i2")
            converted = self._resampler.resample_chunk(source)
            return converted.astype("<i2", copy=False).tobytes()
        except Exception:
            raise ValueError("sample-rate conversion failed") from None

    def _resampling_modules(self) -> tuple[Any, Any]:
        try:
            numpy_module = self._numpy
            if numpy_module is None:
                import numpy as numpy_module
            soxr_module = self._soxr
            if soxr_module is None:
                import soxr as soxr_module
        except ImportError:
            raise ValueError(
                "install the optional voice-local extra for sample-rate conversion"
            ) from None
        return numpy_module, soxr_module


class SoundDeviceMicrophone:
    """Bounded callback-based capture using python-sounddevice/PortAudio.

    Device indices are exposed as opaque string IDs. Capture prefers a supported 16 kHz mono
    format, falls back to another common/native rate, downmixes to mono, and uses SoXR when needed.
    Transient stream failures get a small bounded reconnect window; permission errors fail closed.
    """

    def __init__(
        self,
        *,
        target_sample_rate_hz: int = 16_000,
        frame_duration_ms: int = 20,
        queue_frames: int = 32,
        reconnect_attempts: int = 2,
        reconnect_backoff_seconds: float = 0.25,
        sounddevice_module: Any | None = None,
        numpy_module: Any | None = None,
        soxr_module: Any | None = None,
    ) -> None:
        if not 8_000 <= target_sample_rate_hz <= 192_000:
            raise ValueError("target microphone sample rate is out of range")
        if not 10 <= frame_duration_ms <= 100:
            raise ValueError("microphone frame duration must be between 10 and 100 ms")
        if not 1 <= queue_frames <= 256:
            raise ValueError("microphone callback queue must contain between 1 and 256 frames")
        if not 0 <= reconnect_attempts <= 10:
            raise ValueError("microphone reconnect attempts must be between zero and ten")
        if not 0 <= reconnect_backoff_seconds <= 10:
            raise ValueError("microphone reconnect backoff must be between zero and ten seconds")
        self.target_sample_rate_hz = target_sample_rate_hz
        self.frame_duration_ms = frame_duration_ms
        self.queue_frames = queue_frames
        self.reconnect_attempts = reconnect_attempts
        self.reconnect_backoff_seconds = reconnect_backoff_seconds
        self._sounddevice_module = sounddevice_module
        self._numpy_module = numpy_module
        self._soxr_module = soxr_module
        self._state_lock = threading.RLock()
        self._active_stream: Any | None = None
        self._active_buffer: _CallbackFrameBuffer | None = None
        self._last_buffer: _CallbackFrameBuffer | None = None
        self._closed = False
        self._capture_guard = asyncio.Lock()
        self._selected_rate_hz: int | None = None
        self._selected_channels: int | None = None
        self._capture_reconnects = 0
        self._discovery_errors = 0

    async def list_devices(self) -> Sequence[AudioDevice]:
        try:
            sd = self._sounddevice()
            devices, default = await asyncio.to_thread(_query_devices, sd)
        except MicrophonePermissionDenied:
            raise
        except Exception:
            self._discovery_errors += 1
            raise MicrophoneUnavailable(
                "microphone discovery failed", error_code="INPUT_DEVICE_DISCOVERY_FAILED"
            ) from None
        default_input = _default_device_index(default, 0)
        return tuple(
            AudioDevice(str(index), _device_name(info, index), index == default_input)
            for index, info in enumerate(devices)
            if _device_channel_count(info, "max_input_channels") > 0
        )

    async def capture(self, device_id: str) -> AsyncIterator[AudioChunk]:
        if not device_id or len(device_id) > 256:
            raise MicrophoneUnavailable("microphone device identifier is invalid")
        async with self._capture_guard:
            if self._closed:
                raise MicrophoneUnavailable("microphone adapter is closed")
            for attempt in range(self.reconnect_attempts + 1):
                try:
                    async for chunk in self._capture_once(device_id):
                        yield chunk
                    if self._closed:
                        return
                    raise MicrophoneUnavailable("microphone stream ended")
                except asyncio.CancelledError:
                    raise
                except MicrophonePermissionDenied:
                    raise
                except Exception:
                    if self._closed:
                        return
                    if attempt >= self.reconnect_attempts:
                        raise MicrophoneUnavailable(_CAPTURE_ERROR) from None
                    self._capture_reconnects += 1
                    if self.reconnect_backoff_seconds:
                        await asyncio.sleep(self.reconnect_backoff_seconds * (attempt + 1))

    async def _capture_once(self, device_id: str) -> AsyncIterator[AudioChunk]:
        loop = asyncio.get_running_loop()
        sd = self._sounddevice()
        try:
            device_index = _parse_device_id(device_id)
            device_info = await asyncio.to_thread(sd.query_devices, device_index)
            sample_rate_hz, channels = await asyncio.to_thread(
                self._choose_input_format, sd, device_index, device_info
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _raise_microphone_error(exc)
        frame_count = max(1, round(sample_rate_hz * self.frame_duration_ms / 1000))
        frame_buffer = _CallbackFrameBuffer(loop, self.queue_frames)
        self._last_buffer = frame_buffer
        normalizer = _Pcm16Normalizer(
            self.target_sample_rate_hz,
            numpy_module=self._numpy_module,
            soxr_module=self._soxr_module,
        )

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            del frames, time_info
            try:
                frame_buffer.push(
                    bytes(indata), input_overflow=bool(getattr(status, "input_overflow", False))
                )
            except Exception:
                frame_buffer.fail("PORTAUDIO_CALLBACK_FAILED")

        stream: Any | None = None
        try:
            stream = sd.RawInputStream(
                device=device_index,
                samplerate=sample_rate_hz,
                blocksize=frame_count,
                channels=channels,
                dtype="int16",
                callback=callback,
            )
            with self._state_lock:
                if self._closed:
                    raise MicrophoneUnavailable("microphone adapter is closed")
                self._active_stream = stream
                self._active_buffer = frame_buffer
                self._selected_rate_hz = sample_rate_hz
                self._selected_channels = channels
            await asyncio.to_thread(_start_stream, stream)
            sequence = 0
            while not self._closed:
                closed, error_code = frame_buffer.status()
                if error_code is not None:
                    raise MicrophoneUnavailable(error_code)
                if closed:
                    return
                captured_frames = await frame_buffer.wait_and_drain(0.5)
                if not captured_frames:
                    if not bool(getattr(stream, "active", True)) and not self._closed:
                        raise MicrophoneUnavailable(_CAPTURE_ERROR)
                    continue
                for captured in captured_frames:
                    mono = _downmix_pcm16(captured.data, channels)
                    if sample_rate_hz == self.target_sample_rate_hz:
                        converted = mono
                    else:
                        converted = normalizer.convert(
                            AudioChunk(
                                sequence=sequence,
                                codec="pcm_s16le",
                                sample_rate_hz=sample_rate_hz,
                                channels=1,
                                data=mono,
                                captured_at_monotonic_ns=captured.captured_at_monotonic_ns,
                            )
                        )
                    if not converted:
                        continue
                    yield AudioChunk(
                        sequence=sequence,
                        codec="pcm_s16le",
                        sample_rate_hz=self.target_sample_rate_hz,
                        channels=1,
                        data=converted,
                        captured_at_monotonic_ns=captured.captured_at_monotonic_ns,
                    )
                    sequence += 1
        except asyncio.CancelledError:
            raise
        except (MicrophonePermissionDenied, MicrophoneUnavailable):
            raise
        except Exception as exc:
            _raise_microphone_error(exc)
        finally:
            frame_buffer.close()
            with self._state_lock:
                if self._active_stream is stream:
                    self._active_stream = None
                    self._active_buffer = None
            if stream is not None:
                await asyncio.to_thread(_abort_and_close, stream)

    def _choose_input_format(self, sd: Any, device_index: int, info: Any) -> tuple[int, int]:
        max_channels = _device_channel_count(info, "max_input_channels")
        if max_channels <= 0:
            raise MicrophoneUnavailable("selected device has no input channels")
        native_rate = _device_default_rate(info)
        rates = list(dict.fromkeys((16_000, 48_000, 32_000, 8_000, native_rate)))
        channels_to_try = [1]
        if max_channels >= 2:
            channels_to_try.append(2)
        checker = getattr(sd, "check_input_settings", None)
        for channels in channels_to_try:
            for rate in rates:
                if rate < 8_000 or rate > 192_000:
                    continue
                if checker is None:
                    return rate, channels
                try:
                    checker(device=device_index, channels=channels, samplerate=rate, dtype="int16")
                    return rate, channels
                except Exception:
                    continue
        raise MicrophoneUnavailable("selected microphone has no supported PCM input format")

    async def close(self) -> None:
        with self._state_lock:
            self._closed = True
            stream, self._active_stream = self._active_stream, None
            frame_buffer, self._active_buffer = self._active_buffer, None
        if frame_buffer is not None:
            frame_buffer.close()
        if stream is not None:
            await asyncio.to_thread(_abort_and_close, stream)

    def diagnostics(self) -> dict[str, int | bool | None]:
        with self._state_lock:
            frame_buffer = self._active_buffer or self._last_buffer
            stream = self._active_stream
            result: dict[str, int | bool | None] = {
                "capture_active": bool(stream is not None and getattr(stream, "active", True)),
                "capture_closed": self._closed,
                "capture_reconnects": self._capture_reconnects,
                "discovery_errors": self._discovery_errors,
                "selected_sample_rate_hz": self._selected_rate_hz,
                "selected_channels": self._selected_channels,
                "capture_queue_drops": 0,
                "capture_input_overflows": 0,
                "capture_callback_errors": 0,
            }
        if frame_buffer is not None:
            result.update(frame_buffer.diagnostics())
        return result

    def _sounddevice(self) -> Any:
        if self._sounddevice_module is not None:
            return self._sounddevice_module
        try:
            import sounddevice
        except ImportError:
            raise MicrophoneUnavailable(
                "install the optional voice-local extra for microphone support",
                error_code="SOUNDDEVICE_NOT_INSTALLED",
            ) from None
        except OSError:
            raise MicrophoneUnavailable(
                "PortAudio runtime library is unavailable",
                error_code="PORTAUDIO_RUNTIME_UNAVAILABLE",
            ) from None
        self._sounddevice_module = sounddevice
        return sounddevice


class WebRtcVadAdapter:
    """Local WebRTC VAD for mono signed 16-bit PCM; no audio leaves the process."""

    def __init__(
        self,
        *,
        aggressiveness: int = 2,
        sample_rate_hz: int = 16_000,
        frame_duration_ms: int = 20,
        speech_frame_ratio: float = 0.5,
        vad_factory: Callable[[int], Any] | None = None,
        numpy_module: Any | None = None,
        soxr_module: Any | None = None,
    ) -> None:
        if aggressiveness not in range(4):
            raise ValueError("WebRTC VAD aggressiveness must be between zero and three")
        if sample_rate_hz not in _VAD_SAMPLE_RATES:
            raise ValueError("WebRTC VAD sample rate must be 8, 16, 32, or 48 kHz")
        if frame_duration_ms not in _VAD_FRAME_DURATIONS_MS:
            raise ValueError("WebRTC VAD frame duration must be 10, 20, or 30 ms")
        if not 0 <= speech_frame_ratio <= 1:
            raise ValueError("speech frame ratio must be between zero and one")
        self.aggressiveness = aggressiveness
        self.sample_rate_hz = sample_rate_hz
        self.frame_duration_ms = frame_duration_ms
        self.speech_frame_ratio = speech_frame_ratio
        self._vad_factory = vad_factory
        self._vad: Any | None = None
        self._normalizer = _Pcm16Normalizer(
            sample_rate_hz, numpy_module=numpy_module, soxr_module=soxr_module
        )
        self._buffer = bytearray()
        self._frame_count = 0
        self._speech_frame_count = 0

    async def analyze(self, chunk: AudioChunk) -> VoiceActivity:
        try:
            normalized = self._normalizer.convert(chunk)
        except ValueError:
            raise LocalAudioAdapterFailure(
                "local VAD audio normalization failed",
                error_code="WEBRTC_VAD_AUDIO_NORMALIZATION_FAILED",
            ) from None
        vad = self._get_vad()
        self._buffer.extend(normalized)
        frame_bytes = self.sample_rate_hz * self.frame_duration_ms // 1000 * 2
        if len(self._buffer) > 2 * frame_bytes:
            # The capture contract emits small chunks; reject an unexpectedly huge adapter input.
            raise ValueError("VAD frame buffer exceeded its bounded limit")
        speech_frames = 0
        total_frames = 0
        while len(self._buffer) >= frame_bytes:
            frame = bytes(self._buffer[:frame_bytes])
            del self._buffer[:frame_bytes]
            try:
                is_speech = bool(vad.is_speech(frame, self.sample_rate_hz))
            except Exception:
                raise LocalAudioAdapterFailure(
                    "local VAD frame evaluation failed",
                    error_code="WEBRTC_VAD_FRAME_EVALUATION_FAILED",
                ) from None
            self._frame_count += 1
            self._speech_frame_count += int(is_speech)
            speech_frames += int(is_speech)
            total_frames += 1
        confidence = speech_frames / total_frames if total_frames else 0.0
        return VoiceActivity(
            speech=total_frames > 0 and confidence >= self.speech_frame_ratio,
            confidence=confidence,
        )

    def diagnostics(self) -> dict[str, int | float]:
        return {
            "vad_frames": self._frame_count,
            "vad_speech_frames": self._speech_frame_count,
            "vad_aggressiveness": self.aggressiveness,
        }

    def _get_vad(self) -> Any:
        if self._vad is not None:
            return self._vad
        factory = self._vad_factory
        if factory is None:
            try:
                import webrtcvad
            except ImportError:
                raise LocalAudioAdapterFailure(
                    "install the optional voice-local extra for local VAD",
                    error_code="WEBRTC_VAD_DEPENDENCY_NOT_INSTALLED",
                ) from None
            factory = webrtcvad.Vad
        try:
            self._vad = factory(self.aggressiveness)
        except Exception:
            raise LocalAudioAdapterFailure(
                "local VAD initialization failed",
                error_code="WEBRTC_VAD_INITIALIZATION_FAILED",
            ) from None
        return self._vad


class VoskModel:
    """Lazily load one local Vosk model so wake detection and ASR can share its weights."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        model: Any | None = None,
        model_factory: Callable[[str], Any] | None = None,
        locale: str | None = None,
    ) -> None:
        if model is None and model_path is None:
            raise ValueError("a Vosk model path or loaded model is required")
        if locale is not None and (not locale.strip() or len(locale) > 32):
            raise ValueError("Vosk model locale must be non-empty and bounded")
        self.model_path = Path(model_path).expanduser() if model_path is not None else None
        self.locale = locale
        self._model = model
        self._model_factory = model_factory
        self._load_lock = asyncio.Lock()

    async def load(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is not None:
                return self._model
            assert self.model_path is not None
            if not self.model_path.is_dir():
                raise LocalAudioAdapterFailure(
                    "configured Vosk model directory is unavailable",
                    error_code="VOSK_MODEL_DIRECTORY_NOT_FOUND",
                )
            factory = self._model_factory
            if factory is None:
                try:
                    from vosk import Model
                except ImportError:
                    raise LocalAudioAdapterFailure(
                        "install the optional voice-local extra for Vosk speech models",
                        error_code="VOSK_DEPENDENCY_NOT_INSTALLED",
                    ) from None
                factory = Model
            try:
                self._model = await asyncio.to_thread(factory, str(self.model_path))
            except asyncio.CancelledError:
                raise
            except Exception:
                raise LocalAudioAdapterFailure(
                    "Vosk model could not be loaded",
                    error_code="VOSK_MODEL_LOAD_FAILED",
                ) from None
            return self._model


class VoskWakeWordDetector:
    """Dormant-only Vosk keyword recognizer with confidence and post-wake audio filtering."""

    def __init__(
        self,
        model: VoskModel,
        *,
        wake_word: str = "ARISE",
        sample_rate_hz: int = 16_000,
        min_confidence: float = 0.72,
        recognizer_factory: Callable[..., Any] | None = None,
        history_chunks: int = 256,
    ) -> None:
        if not wake_word.strip() or len(wake_word) > 32:
            raise ValueError("wake word must contain one to 32 characters")
        if sample_rate_hz not in _VAD_SAMPLE_RATES:
            raise ValueError("Vosk wake-word sample rate must be a supported local audio rate")
        if not 0 <= min_confidence <= 1:
            raise ValueError("wake-word confidence threshold must be between zero and one")
        if not 32 <= history_chunks <= 1024:
            raise ValueError("wake-word history must contain between 32 and 1024 chunks")
        tokens = _word_tokens(wake_word)
        if not tokens:
            raise ValueError("wake word must contain at least one letter or number")
        self.model = model
        self.wake_word = wake_word.strip()
        self._wake_tokens = tokens
        self.sample_rate_hz = sample_rate_hz
        self.min_confidence = min_confidence
        self.history_chunks = history_chunks
        self._recognizer_factory = recognizer_factory
        self._recognizer: Any | None = None
        self._history: deque[tuple[int, AudioChunk]] = deque()
        self._history_bytes = 0
        self._sample_cursor = 0
        self._lock = asyncio.Lock()
        self._false_matches = 0
        self._accepted_matches = 0

    async def accept(self, chunk: AudioChunk) -> WakeWordDetection:
        _validate_wake_audio(chunk, self.sample_rate_hz)
        async with self._lock:
            recognizer = await self._get_recognizer()
            self._remember(chunk)
            try:
                accepted = await asyncio.to_thread(recognizer.AcceptWaveform, chunk.data)
                if not accepted:
                    return WakeWordDetection(matched=False)
                result = await asyncio.to_thread(recognizer.Result)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._reset_recognizer_locked()
                raise LocalAudioAdapterFailure(
                    "local wake-word recognition failed",
                    error_code="VOSK_WAKE_INFERENCE_FAILED",
                ) from None
            detection = self._match_result(result)
            await self._reset_recognizer_locked()
            return detection

    async def end_utterance(self) -> WakeWordDetection:
        """Finalize a VAD-bounded dormant utterance and discard its local working buffer."""
        async with self._lock:
            if self._recognizer is None:
                self._clear_history()
                return WakeWordDetection(matched=False)
            try:
                result = await asyncio.to_thread(self._recognizer.FinalResult)
                detection = self._match_result(result)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._reset_recognizer_locked()
                raise LocalAudioAdapterFailure(
                    "local wake-word recognition failed",
                    error_code="VOSK_WAKE_INFERENCE_FAILED",
                ) from None
            await self._reset_recognizer_locked()
            return detection

    async def reset(self) -> None:
        async with self._lock:
            await self._reset_recognizer_locked()

    def diagnostics(self) -> dict[str, int | float]:
        return {
            "wake_matches": self._accepted_matches,
            "wake_confidence_rejections": self._false_matches,
            "wake_history_bytes": self._history_bytes,
        }

    async def _get_recognizer(self) -> Any:
        if self._recognizer is not None:
            return self._recognizer
        model = await self.model.load()
        factory = self._recognizer_factory
        if factory is None:
            try:
                from vosk import KaldiRecognizer
            except ImportError:
                raise LocalAudioAdapterFailure(
                    "install the optional voice-local extra for wake detection",
                    error_code="VOSK_DEPENDENCY_NOT_INSTALLED",
                ) from None
            factory = KaldiRecognizer
        grammar = json.dumps([self.wake_word], ensure_ascii=False)
        try:
            self._recognizer = await asyncio.to_thread(factory, model, self.sample_rate_hz, grammar)
            set_words = getattr(self._recognizer, "SetWords", None)
            if callable(set_words):
                await asyncio.to_thread(set_words, True)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._recognizer = None
            raise LocalAudioAdapterFailure(
                "Vosk wake-word recognizer could not be initialized",
                error_code="VOSK_WAKE_RECOGNIZER_INIT_FAILED",
            ) from None
        return self._recognizer

    def _remember(self, chunk: AudioChunk) -> None:
        start = self._sample_cursor
        sample_count = len(chunk.data) // 2
        self._history.append((start, chunk))
        self._history_bytes += len(chunk.data)
        self._sample_cursor += sample_count
        while self._history and (
            len(self._history) > self.history_chunks or self._history_bytes > MAX_WAKE_HANDOFF_BYTES
        ):
            _, removed = self._history.popleft()
            self._history_bytes -= len(removed.data)

    def _match_result(self, raw_result: str) -> WakeWordDetection:
        result = _json_object(raw_result)
        words = result.get("result")
        if not isinstance(words, list):
            return WakeWordDetection(matched=False)
        parsed: list[tuple[str, float, float, float]] = []
        for item in words:
            if not isinstance(item, dict):
                continue
            word = item.get("word")
            start = item.get("start")
            end = item.get("end")
            confidence = item.get("conf")
            if (
                not isinstance(word, str)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(start))
                or not math.isfinite(float(end))
                or not math.isfinite(float(confidence))
            ):
                continue
            parsed.append((word, float(start), float(end), float(confidence)))
        for index in range(0, len(parsed) - len(self._wake_tokens) + 1):
            candidates = parsed[index : index + len(self._wake_tokens)]
            if tuple(_word_tokens(item[0]) for item in candidates) != tuple(
                (token,) for token in self._wake_tokens
            ):
                continue
            confidence = min(item[3] for item in candidates)
            if confidence < self.min_confidence:
                self._false_matches += 1
                return WakeWordDetection(matched=False)
            wake_end_sample = max(0, math.ceil(candidates[-1][2] * self.sample_rate_hz))
            handoff = self._post_wake_audio(wake_end_sample)
            self._accepted_matches += 1
            return WakeWordDetection(
                matched=True,
                confidence=min(1.0, confidence),
                activation_audio=handoff,
            )
        return WakeWordDetection(matched=False)

    def _post_wake_audio(self, wake_end_sample: int) -> tuple[AudioChunk, ...]:
        pieces: list[tuple[int, int, bytes, int]] = []
        for start_sample, chunk in self._history:
            end_sample = start_sample + len(chunk.data) // 2
            if end_sample <= wake_end_sample:
                continue
            skip_samples = max(0, wake_end_sample - start_sample)
            data = chunk.data[skip_samples * 2 :]
            if data:
                captured_at = chunk.captured_at_monotonic_ns + round(
                    skip_samples * 1_000_000_000 / self.sample_rate_hz
                )
                pieces.append((chunk.sequence, captured_at, data, len(data) // 2))
        if not pieces:
            return ()
        packet_limit = min(
            MAX_AUDIO_CHUNK_BYTES,
            max(2, MAX_WAKE_HANDOFF_BYTES // MAX_WAKE_HANDOFF_CHUNKS),
        )
        output: list[AudioChunk] = []
        payload = bytearray()
        packet_sequence = pieces[0][0]
        packet_timestamp = pieces[0][1]
        total_bytes = 0
        for sequence, captured_at, data, _ in pieces:
            if len(output) >= MAX_WAKE_HANDOFF_CHUNKS or total_bytes >= MAX_WAKE_HANDOFF_BYTES:
                break
            cursor = 0
            while cursor < len(data):
                remaining = min(packet_limit - len(payload), MAX_WAKE_HANDOFF_BYTES - total_bytes)
                if remaining <= 0:
                    break
                remaining -= remaining % 2
                take = min(remaining, len(data) - cursor)
                take -= take % 2
                if take <= 0:
                    break
                if not payload:
                    packet_sequence = sequence
                    packet_timestamp = captured_at + round(
                        cursor * 1_000_000_000 / (2 * self.sample_rate_hz)
                    )
                payload.extend(data[cursor : cursor + take])
                total_bytes += take
                cursor += take
                if len(payload) >= packet_limit:
                    output.append(
                        AudioChunk(
                            sequence=packet_sequence,
                            codec="pcm_s16le",
                            sample_rate_hz=self.sample_rate_hz,
                            channels=1,
                            data=bytes(payload),
                            captured_at_monotonic_ns=packet_timestamp,
                        )
                    )
                    payload.clear()
                    if len(output) >= MAX_WAKE_HANDOFF_CHUNKS:
                        break
        if payload and len(output) < MAX_WAKE_HANDOFF_CHUNKS:
            output.append(
                AudioChunk(
                    sequence=packet_sequence,
                    codec="pcm_s16le",
                    sample_rate_hz=self.sample_rate_hz,
                    channels=1,
                    data=bytes(payload),
                    captured_at_monotonic_ns=packet_timestamp,
                )
            )
        return tuple(output)

    async def _reset_recognizer_locked(self) -> None:
        recognizer = self._recognizer
        if recognizer is not None:
            reset = getattr(recognizer, "Reset", None)
            if callable(reset):
                try:
                    await asyncio.to_thread(reset)
                except Exception:
                    self._recognizer = None
        self._clear_history()

    def _clear_history(self) -> None:
        self._history.clear()
        self._history_bytes = 0
        self._sample_cursor = 0


class VoskSpeechRecognizer:
    """Provider-independent Vosk streaming ASR with non-authoritative partials and finals."""

    def __init__(
        self,
        model: VoskModel,
        *,
        sample_rate_hz: int = 16_000,
        recognizer_factory: Callable[..., Any] | None = None,
        numpy_module: Any | None = None,
        soxr_module: Any | None = None,
        max_concurrent_streams: int = 1,
        admission_timeout_seconds: float = 2.0,
    ) -> None:
        if sample_rate_hz not in _VAD_SAMPLE_RATES:
            raise ValueError("Vosk ASR sample rate must be 8, 16, 32, or 48 kHz")
        if not 1 <= max_concurrent_streams <= 8:
            raise ValueError("ASR concurrency must be between one and eight streams")
        if not 0.1 <= admission_timeout_seconds <= 30:
            raise ValueError("ASR admission timeout must be between 0.1 and 30 seconds")
        self.model = model
        self.sample_rate_hz = sample_rate_hz
        self._recognizer_factory = recognizer_factory
        self._numpy_module = numpy_module
        self._soxr_module = soxr_module
        self._active_streams = 0
        self._admission_timeout_seconds = admission_timeout_seconds
        self._stream_slots = asyncio.Semaphore(max_concurrent_streams)

    async def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        locale: str | None = None,
        correlation_id: str,
    ) -> AsyncIterator[TranscriptSegment]:
        validate_safe_token(correlation_id, "speech recognition correlation_id")
        if locale is not None and (not locale.strip() or len(locale) > 32):
            raise ValueError("speech recognition locale must be non-empty and bounded")
        if self.model.locale and locale and not _locales_compatible(self.model.locale, locale):
            raise ValueError("configured Vosk model locale does not match the requested locale")
        try:
            await asyncio.wait_for(
                self._stream_slots.acquire(), timeout=self._admission_timeout_seconds
            )
        except TimeoutError:
            close = getattr(audio, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass
            raise LocalAudioAdapterFailure(
                "local ASR capacity is busy",
                error_code="VOSK_ASR_CAPACITY_BUSY",
            ) from None
        active = False
        try:
            model = await self.model.load()
            recognizer = await self._create_recognizer(model)
            normalizer = _Pcm16Normalizer(
                self.sample_rate_hz,
                numpy_module=self._numpy_module,
                soxr_module=self._soxr_module,
            )
            self._active_streams += 1
            active = True
            total_samples = 0
            segment_start_sample = 0
            last_partial = ""
            async for chunk in audio:
                try:
                    pcm = normalizer.convert(chunk)
                except ValueError:
                    raise LocalAudioAdapterFailure(
                        "local ASR audio normalization failed",
                        error_code="VOSK_ASR_AUDIO_NORMALIZATION_FAILED",
                    ) from None
                if not pcm:
                    continue
                accepted = await asyncio.to_thread(recognizer.AcceptWaveform, pcm)
                total_samples += len(pcm) // 2
                end_ms = total_samples * 1000 // self.sample_rate_hz
                start_ms = segment_start_sample * 1000 // self.sample_rate_hz
                if accepted:
                    final_text, confidence = _parse_asr_text(
                        await asyncio.to_thread(recognizer.Result)
                    )
                    if final_text:
                        yield TranscriptSegment(
                            text=final_text,
                            start_offset_ms=start_ms,
                            end_offset_ms=max(start_ms, end_ms),
                            confidence=confidence,
                            is_final=True,
                            locale=locale or self.model.locale,
                        )
                    segment_start_sample = total_samples
                    last_partial = ""
                    continue
                partial = _parse_partial(await asyncio.to_thread(recognizer.PartialResult))
                if partial and partial != last_partial:
                    yield TranscriptSegment(
                        text=partial,
                        start_offset_ms=start_ms,
                        end_offset_ms=max(start_ms, end_ms),
                        confidence=0.0,
                        is_final=False,
                        locale=locale or self.model.locale,
                    )
                    last_partial = partial
            final_text, confidence = _parse_asr_text(
                await asyncio.to_thread(recognizer.FinalResult)
            )
            if final_text:
                end_ms = total_samples * 1000 // self.sample_rate_hz
                start_ms = segment_start_sample * 1000 // self.sample_rate_hz
                yield TranscriptSegment(
                    text=final_text,
                    start_offset_ms=start_ms,
                    end_offset_ms=max(start_ms, end_ms),
                    confidence=confidence,
                    is_final=True,
                    locale=locale or self.model.locale,
                )
        except asyncio.CancelledError:
            raise
        except ValueError:
            raise
        except LocalAudioAdapterFailure:
            raise
        except Exception:
            raise LocalAudioAdapterFailure(
                "local streaming speech recognition failed",
                error_code="VOSK_ASR_INFERENCE_FAILED",
            ) from None
        finally:
            if active:
                self._active_streams = max(0, self._active_streams - 1)
            self._stream_slots.release()
            close = getattr(audio, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass

    def diagnostics(self) -> dict[str, int]:
        return {"asr_active_streams": self._active_streams}

    async def _create_recognizer(self, model: Any) -> Any:
        factory = self._recognizer_factory
        if factory is None:
            try:
                from vosk import KaldiRecognizer
            except ImportError:
                raise LocalAudioAdapterFailure(
                    "install the optional voice-local extra for local ASR",
                    error_code="VOSK_DEPENDENCY_NOT_INSTALLED",
                ) from None
            factory = KaldiRecognizer
        try:
            recognizer = await asyncio.to_thread(factory, model, self.sample_rate_hz)
            set_words = getattr(recognizer, "SetWords", None)
            if callable(set_words):
                await asyncio.to_thread(set_words, True)
            return recognizer
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LocalAudioAdapterFailure(
                "Vosk speech recognizer could not be initialized",
                error_code="VOSK_ASR_RECOGNIZER_INIT_FAILED",
            ) from None


class KokoroSpeechSynthesis(SpeechSynthesisPort):
    """Local, chunked TTS adapter for kokoro-onnx; accepts an already-loaded model object."""

    def __init__(
        self,
        kokoro: Any,
        *,
        default_voice_id: str,
        default_locale: str = "en-us",
        max_text_chars: int = 16_384,
        output_chunk_bytes: int = 64 * 1024,
        max_concurrent_streams: int = 1,
        admission_timeout_seconds: float = 2.0,
    ) -> None:
        if not callable(getattr(kokoro, "create_stream", None)):
            raise ValueError("Kokoro model must expose create_stream")
        if not default_voice_id.strip() or len(default_voice_id) > 128:
            raise ValueError("default TTS voice identifier must be non-empty and bounded")
        if not default_locale.strip() or len(default_locale) > 32:
            raise ValueError("default TTS locale must be non-empty and bounded")
        if not 1 <= max_text_chars <= 16_384:
            raise ValueError("TTS text limit must be between one and 16384 characters")
        if not 2 <= output_chunk_bytes <= MAX_AUDIO_CHUNK_BYTES:
            raise ValueError("TTS output chunks must be between 2 bytes and 256 KiB")
        if not 1 <= max_concurrent_streams <= 4:
            raise ValueError("TTS concurrency must be between one and four streams")
        if not 0.1 <= admission_timeout_seconds <= 30:
            raise ValueError("TTS admission timeout must be between 0.1 and 30 seconds")
        self.kokoro = kokoro
        self.default_voice_id = default_voice_id
        self.default_locale = default_locale
        self.max_text_chars = max_text_chars
        self.output_chunk_bytes = output_chunk_bytes - output_chunk_bytes % 2
        self._syntheses = 0
        self._admission_timeout_seconds = admission_timeout_seconds
        self._stream_slots = asyncio.Semaphore(max_concurrent_streams)

    async def synthesize(
        self,
        text: str,
        *,
        locale: str | None = None,
        voice_id: str | None = None,
        correlation_id: str,
    ) -> AsyncIterator[AudioChunk]:
        validate_safe_token(correlation_id, "speech synthesis correlation_id")
        if not isinstance(text, str) or not text.strip() or len(text) > self.max_text_chars:
            raise ValueError("TTS text must be non-empty and within the configured limit")
        selected_voice = voice_id or self.default_voice_id
        selected_locale = locale or self.default_locale
        if len(selected_voice) > 128 or not selected_locale.strip() or len(selected_locale) > 32:
            raise ValueError("TTS voice or locale is invalid")
        try:
            await asyncio.wait_for(
                self._stream_slots.acquire(), timeout=self._admission_timeout_seconds
            )
        except TimeoutError:
            raise LocalAudioAdapterFailure(
                "local TTS capacity is busy",
                error_code="KOKORO_TTS_CAPACITY_BUSY",
            ) from None
        stream: Any | None = None
        active = False
        try:
            stream = self.kokoro.create_stream(
                text,
                voice=selected_voice,
                lang=selected_locale.lower(),
            )
            if not hasattr(stream, "__aiter__"):
                raise LocalAudioAdapterFailure(
                    "Kokoro stream did not return an asynchronous iterator",
                    error_code="KOKORO_TTS_STREAM_INVALID",
                )
            self._syntheses += 1
            active = True
            sequence = 0
            async for samples, sample_rate_hz in stream:
                if not 8_000 <= int(sample_rate_hz) <= 192_000:
                    raise LocalAudioAdapterFailure(
                        "TTS model returned an unsupported sample rate",
                        error_code="KOKORO_TTS_SAMPLE_RATE_UNSUPPORTED",
                    )
                pcm = await asyncio.to_thread(_float_samples_to_pcm16, samples)
                step = self.output_chunk_bytes
                for offset in range(0, len(pcm), step):
                    data = pcm[offset : offset + step]
                    if not data:
                        continue
                    yield AudioChunk(
                        sequence=sequence,
                        codec="pcm_s16le",
                        sample_rate_hz=int(sample_rate_hz),
                        channels=1,
                        data=data,
                        captured_at_monotonic_ns=time.monotonic_ns(),
                    )
                    sequence += 1
        except asyncio.CancelledError:
            raise
        except LocalAudioAdapterFailure:
            raise
        except Exception:
            raise LocalAudioAdapterFailure(
                "local streaming TTS failed",
                error_code="KOKORO_TTS_INFERENCE_FAILED",
            ) from None
        finally:
            if active:
                self._syntheses = max(0, self._syntheses - 1)
            if stream is not None:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:
                        pass
            self._stream_slots.release()

    def diagnostics(self) -> dict[str, int]:
        return {"tts_active_streams": self._syntheses}


class LazyKokoroSpeechSynthesis(SpeechSynthesisPort):
    """Lazily load Kokoro ONNX model and voices on first synthesis or explicit load()."""

    def __init__(
        self,
        *,
        model_path: Path,
        voices_path: Path,
        default_voice_id: str = "af_heart",
        default_locale: str = "en-us",
        kokoro_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.model_path = Path(model_path).expanduser()
        self.voices_path = Path(voices_path).expanduser()
        self.default_voice_id = default_voice_id
        self.default_locale = default_locale
        self._kokoro_factory = kokoro_factory
        self._delegate: KokoroSpeechSynthesis | None = None
        self._load_lock = asyncio.Lock()

    async def load(self) -> KokoroSpeechSynthesis:
        async with self._load_lock:
            if self._delegate is not None:
                return self._delegate
            if not self.model_path.is_file() or not self.voices_path.is_file():
                raise LocalAudioAdapterFailure(
                    "Kokoro model or voices file was not found",
                    error_code="KOKORO_MODEL_OR_VOICE_FILE_NOT_FOUND",
                )
            factory = self._kokoro_factory
            if factory is None:
                try:
                    from kokoro_onnx import Kokoro
                except ImportError:
                    raise LocalAudioAdapterFailure(
                        "install the optional voice-local extra for local TTS",
                        error_code="KOKORO_DEPENDENCY_NOT_INSTALLED",
                    ) from None
                factory = Kokoro
            try:
                kokoro = await asyncio.to_thread(
                    factory, str(self.model_path), str(self.voices_path)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                raise LocalAudioAdapterFailure(
                    "Kokoro local model initialization failed",
                    error_code="KOKORO_MODEL_LOAD_FAILED",
                ) from None
            self._delegate = KokoroSpeechSynthesis(
                kokoro,
                default_voice_id=self.default_voice_id,
                default_locale=self.default_locale,
            )
            return self._delegate

    async def synthesize(
        self,
        text: str,
        *,
        locale: str | None = None,
        voice_id: str | None = None,
        correlation_id: str,
    ) -> AsyncIterator[AudioChunk]:
        delegate = await self.load()
        async for chunk in delegate.synthesize(
            text,
            locale=locale,
            voice_id=voice_id,
            correlation_id=correlation_id,
        ):
            yield chunk


class SoundDeviceAudioPlayback:
    """Interruptible PortAudio output with device discovery and sample-rate fallback."""

    def __init__(
        self,
        *,
        device_id: str | None = None,
        sounddevice_module: Any | None = None,
        numpy_module: Any | None = None,
        soxr_module: Any | None = None,
    ) -> None:
        if device_id is not None and (not device_id.strip() or len(device_id) > 256):
            raise ValueError("playback device identifier must be non-empty and bounded")
        self.device_id = device_id
        self._sounddevice_module = sounddevice_module
        self._numpy_module = numpy_module
        self._soxr_module = soxr_module
        self._state_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._stream: Any | None = None
        self._stream_spec: tuple[int | None, int, int] | None = None
        self._resampler: Any | None = None
        self._resampler_spec: tuple[int, int, int] | None = None
        self._generation = 0
        self._closed = False
        self._playback_errors = 0
        self._interruptions = 0
        self._output_underflows = 0
        self._selected_rate_hz: int | None = None
        self._selected_channels: int | None = None

    async def list_devices(self) -> Sequence[AudioDevice]:
        sd = self._sounddevice()
        try:
            devices, default = await asyncio.to_thread(_query_devices, sd)
        except Exception:
            raise AudioPlaybackUnavailable(
                "audio output device discovery failed",
                error_code="OUTPUT_DEVICE_DISCOVERY_FAILED",
            ) from None
        default_output = _default_device_index(default, 1)
        return tuple(
            AudioDevice(str(index), _device_name(info, index), index == default_output)
            for index, info in enumerate(devices)
            if _device_channel_count(info, "max_output_channels") > 0
        )

    async def play(self, chunk: AudioChunk) -> None:
        if chunk.codec not in _PCM_CODECS:
            raise ValueError("audio playback requires signed 16-bit PCM")
        async with self._write_lock:
            generation = self._generation
            stream: Any | None = None
            try:
                stream, data = await self._ensure_stream(chunk)
                if generation != self._generation or not data:
                    return
                underflow = await asyncio.to_thread(stream.write, data)
                if underflow:
                    self._output_underflows += 1
            except asyncio.CancelledError:
                await self.stop()
                raise
            except Exception:
                if generation != self._generation:
                    return
                self._playback_errors += 1
                async with self._state_lock:
                    failed_stream, self._stream = self._stream, None
                    self._stream_spec = None
                    self._resampler = None
                    self._resampler_spec = None
                if failed_stream is None:
                    failed_stream = stream
                if failed_stream is not None:
                    await asyncio.to_thread(_abort_and_close, failed_stream)
                raise AudioPlaybackUnavailable(
                    "audio output stream failed", error_code="AUDIO_OUTPUT_STREAM_FAILED"
                ) from None

    async def stop(self) -> None:
        async with self._state_lock:
            self._generation += 1
            self._interruptions += 1
            stream, self._stream = self._stream, None
            self._stream_spec = None
            self._resampler = None
            self._resampler_spec = None
        if stream is not None:
            await asyncio.to_thread(_abort_and_close, stream)

    async def close(self) -> None:
        async with self._state_lock:
            self._closed = True
        await self.stop()

    def diagnostics(self) -> dict[str, int | bool | None]:
        return {
            "playback_active": bool(
                self._stream is not None and getattr(self._stream, "active", True)
            ),
            "playback_closed": self._closed,
            "playback_errors": self._playback_errors,
            "playback_interruptions": self._interruptions,
            "playback_output_underflows": self._output_underflows,
            "selected_sample_rate_hz": self._selected_rate_hz,
            "selected_channels": self._selected_channels,
        }

    async def _ensure_stream(self, chunk: AudioChunk) -> tuple[Any, bytes]:
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("audio playback adapter is closed")
            sd = self._sounddevice()
            output_device, info = await self._resolve_output_device(sd)
            max_channels = _device_channel_count(info, "max_output_channels")
            if max_channels <= 0:
                raise RuntimeError("selected output device has no output channels")
            channels = chunk.channels if chunk.channels <= max_channels else 1
            rate = self._choose_output_rate(sd, output_device, info, channels, chunk.sample_rate_hz)
            spec = (output_device, rate, channels)
            if self._stream is None or self._stream_spec != spec:
                old, self._stream = self._stream, None
                self._stream_spec = None
                self._resampler = None
                self._resampler_spec = None
                if old is not None:
                    await asyncio.to_thread(_abort_and_close, old)
                stream = sd.RawOutputStream(
                    device=output_device,
                    samplerate=rate,
                    channels=channels,
                    dtype="int16",
                    blocksize=0,
                )
                self._stream = stream
                self._stream_spec = spec
                self._selected_rate_hz = rate
                self._selected_channels = channels
                try:
                    await asyncio.to_thread(_start_stream, stream)
                except BaseException:
                    self._stream = None
                    self._stream_spec = None
                    await asyncio.to_thread(_abort_and_close, stream)
                    raise
            converted = _convert_channel_count(chunk.data, chunk.channels, channels)
            if chunk.sample_rate_hz != rate:
                converted = self._resample_output(
                    converted,
                    input_rate=chunk.sample_rate_hz,
                    output_rate=rate,
                    channels=channels,
                )
            return self._stream, converted

    async def _resolve_output_device(self, sd: Any) -> tuple[int | None, Any]:
        devices, default = await asyncio.to_thread(_query_devices, sd)
        if self.device_id is not None:
            index = _parse_device_id(self.device_id)
            if index >= len(devices):
                raise RuntimeError("configured audio output device is unavailable")
            info = devices[index]
        else:
            index = _default_device_index(default, 1)
            if index is None or index >= len(devices):
                index = next(
                    (
                        position
                        for position, item in enumerate(devices)
                        if _device_channel_count(item, "max_output_channels") > 0
                    ),
                    None,
                )
            if index is None:
                raise RuntimeError("no audio output device is available")
            info = devices[index]
        if _device_channel_count(info, "max_output_channels") <= 0:
            raise RuntimeError("selected output device has no output channels")
        return index, info

    def _choose_output_rate(
        self, sd: Any, device: int | None, info: Any, channels: int, source_rate: int
    ) -> int:
        native_rate = _device_default_rate(info)
        candidates = list(dict.fromkeys((source_rate, native_rate, 48_000, 44_100, 24_000, 16_000)))
        checker = getattr(sd, "check_output_settings", None)
        for rate in candidates:
            if not 8_000 <= rate <= 192_000:
                continue
            if checker is None:
                return rate
            try:
                checker(device=device, channels=channels, samplerate=rate, dtype="int16")
                return rate
            except Exception:
                continue
        raise RuntimeError("selected output device has no supported PCM format")

    def _resample_output(
        self, data: bytes, *, input_rate: int, output_rate: int, channels: int
    ) -> bytes:
        spec = (input_rate, output_rate, channels)
        if self._resampler is None or self._resampler_spec != spec:
            try:
                numpy_module, soxr_module = _load_resampling_modules(
                    self._numpy_module, self._soxr_module
                )
                self._resampler = soxr_module.ResampleStream(
                    input_rate,
                    output_rate,
                    num_channels=channels,
                    dtype="int16",
                    quality="HQ",
                )
                self._numpy_module = numpy_module
                self._resampler_spec = spec
            except Exception:
                raise RuntimeError(
                    "install the optional voice-local extra for output resampling"
                ) from None
        try:
            source = self._numpy_module.frombuffer(data, dtype="<i2")
            if channels > 1:
                source = source.reshape((-1, channels))
            converted = self._resampler.resample_chunk(source)
            return converted.astype("<i2", copy=False).tobytes()
        except Exception:
            raise RuntimeError("audio output resampling failed") from None

    def _sounddevice(self) -> Any:
        if self._sounddevice_module is not None:
            return self._sounddevice_module
        try:
            import sounddevice
        except ImportError:
            raise AudioPlaybackUnavailable(
                "install the optional voice-local extra for audio playback",
                error_code="SOUNDDEVICE_NOT_INSTALLED",
            ) from None
        except OSError:
            raise AudioPlaybackUnavailable(
                "PortAudio runtime library is unavailable",
                error_code="PORTAUDIO_RUNTIME_UNAVAILABLE",
            ) from None
        self._sounddevice_module = sounddevice
        return sounddevice


def _downmix_pcm16(data: bytes, channels: int) -> bytes:
    if channels <= 0 or len(data) % (2 * channels) != 0:
        raise ValueError("PCM frame is not aligned to its channel count")
    if channels == 1:
        return data
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    mono = array("h")
    for offset in range(0, len(samples), channels):
        total = sum(samples[offset : offset + channels])
        mono.append(max(-32768, min(32767, round(total / channels))))
    if sys.byteorder == "big":
        mono.byteswap()
    return mono.tobytes()


def _convert_channel_count(data: bytes, source_channels: int, target_channels: int) -> bytes:
    if source_channels == target_channels:
        if len(data) % (2 * target_channels):
            raise ValueError("PCM playback buffer is not frame-aligned")
        return data
    if source_channels <= 0 or target_channels not in {1, 2}:
        raise ValueError("unsupported audio channel conversion")
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    if len(samples) % source_channels:
        raise ValueError("PCM playback buffer is not frame-aligned")
    output = array("h")
    for offset in range(0, len(samples), source_channels):
        frame = samples[offset : offset + source_channels]
        if target_channels == 1:
            output.append(max(-32768, min(32767, round(sum(frame) / source_channels))))
        elif source_channels == 1:
            output.extend((frame[0], frame[0]))
        else:
            output.extend((frame[0], frame[1]))
    if sys.byteorder == "big":
        output.byteswap()
    return output.tobytes()


def _float_samples_to_pcm16(samples: Any) -> bytes:
    output = array("h")
    try:
        iterator = iter(samples)
    except TypeError:
        raise RuntimeError("TTS model returned an invalid audio block") from None
    for sample in iterator:
        value = float(sample)
        if not math.isfinite(value):
            value = 0.0
        value = max(-1.0, min(1.0, value))
        output.append(int(round(value * 32767.0)))
    if sys.byteorder == "big":
        output.byteswap()
    return output.tobytes()


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_asr_text(raw: str) -> tuple[str, float]:
    result = _json_object(raw)
    text = result.get("text")
    if not isinstance(text, str):
        return "", 0.0
    normalized = " ".join(text.split())
    words = result.get("result")
    confidences = (
        [
            float(item["conf"])
            for item in words
            if isinstance(item, dict)
            and isinstance(item.get("conf"), (int, float))
            and math.isfinite(float(item["conf"]))
            and 0 <= float(item["conf"]) <= 1
        ]
        if isinstance(words, list)
        else []
    )
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return normalized[:4096], confidence


def _parse_partial(raw: str) -> str:
    partial = _json_object(raw).get("partial")
    return " ".join(partial.split())[:4096] if isinstance(partial, str) else ""


def _locales_compatible(configured: str, requested: str) -> bool:
    configured_language = configured.casefold().replace("_", "-").split("-", 1)[0]
    requested_language = requested.casefold().replace("_", "-").split("-", 1)[0]
    return bool(configured_language and configured_language == requested_language)


def _word_tokens(text: str) -> tuple[str, ...]:
    import unicodedata

    normalized = unicodedata.normalize("NFKC", text).casefold()
    token = ""
    result: list[str] = []
    for char in normalized:
        if char.isalnum():
            token += char
        elif token:
            result.append(token)
            token = ""
    if token:
        result.append(token)
    return tuple(result)


def _validate_wake_audio(chunk: AudioChunk, sample_rate_hz: int) -> None:
    if (
        chunk.codec not in _PCM_CODECS
        or chunk.sample_rate_hz != sample_rate_hz
        or chunk.channels != 1
        or len(chunk.data) % 2
    ):
        raise ValueError("wake detector requires mono signed 16-bit PCM at its configured rate")


def _device_channel_count(info: Any, key: str) -> int:
    try:
        return max(0, int(info.get(key, 0)))
    except (AttributeError, TypeError, ValueError):
        return 0


def _device_default_rate(info: Any) -> int:
    try:
        rate = int(round(float(info.get("default_samplerate", 48_000))))
    except (AttributeError, TypeError, ValueError, OverflowError):
        rate = 48_000
    return min(192_000, max(8_000, rate))


def _device_name(info: Any, index: int) -> str:
    try:
        value = str(info.get("name", f"Audio device {index}"))
    except Exception:
        value = f"Audio device {index}"
    return value.strip()[:256] or f"Audio device {index}"


def _query_devices(sd: Any) -> tuple[list[Any], Any]:
    devices = sd.query_devices()
    default = getattr(getattr(sd, "default", None), "device", None)
    return list(devices), default


def _default_device_index(default: Any, channel: int) -> int | None:
    try:
        value = default[channel] if isinstance(default, (tuple, list)) else default
        index = int(value)
        return index if index >= 0 else None
    except (TypeError, ValueError, IndexError):
        return None


def _parse_device_id(device_id: str) -> int:
    try:
        index = int(device_id)
    except (TypeError, ValueError):
        raise MicrophoneUnavailable("microphone device identifier is invalid") from None
    if index < 0:
        raise MicrophoneUnavailable("microphone device identifier is invalid")
    return index


def _start_stream(stream: Any) -> None:
    if not bool(getattr(stream, "active", False)):
        stream.start()


def _abort_and_close(stream: Any) -> None:
    abort = getattr(stream, "abort", None)
    stop = getattr(stream, "stop", None)
    try:
        if callable(abort):
            abort()
        elif callable(stop):
            stop()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


def _raise_microphone_error(error: BaseException) -> None:
    if isinstance(error, PermissionError) or _looks_like_permission_error(error):
        raise MicrophonePermissionDenied("operating system denied microphone access") from None
    raise MicrophoneUnavailable(_CAPTURE_ERROR) from None


def _looks_like_permission_error(error: BaseException) -> bool:
    # Error text is inspected only for classification and is never returned or logged.
    message = str(error).casefold()
    return any(
        token in message for token in ("permission denied", "access denied", "not permitted")
    )


def _load_resampling_modules(numpy_module: Any | None, soxr_module: Any | None) -> tuple[Any, Any]:
    try:
        if numpy_module is None:
            import numpy as numpy_module
        if soxr_module is None:
            import soxr as soxr_module
    except ImportError:
        raise RuntimeError("optional sample-rate conversion package is unavailable") from None
    return numpy_module, soxr_module
