"""Optional Gemini Live adapter behind ARISE's provider-neutral voice session ports.

The Google SDK is imported lazily. A cloud session is never opened unless both application and
security cloud policies are enabled and a credential is available from the configured secret
provider. Audio/transcript payloads and resumption handles are kept in memory only.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from typing import Any

from arise.adapters.secrets import SecretProvider, SecretUnavailable
from arise.core.extensions import AudioChunk
from arise.core.models import VoiceProviderStatus
from arise.core.voice import (
    LiveConversationSession,
    LiveEvent,
    LiveEventType,
    LiveSessionConfig,
    LiveToolCall,
    VoiceProviderFailure,
)

_GEMINI_OUTPUT_SAMPLE_RATE_HZ = 24_000
_MAX_AUDIO_CHUNK_BYTES = 256 * 1024


def build_live_config(
    config: LiveSessionConfig, *, resume_handle: str | None = None
) -> dict[str, Any]:
    """Build the supported Gemini Live setup shape without leaking provider fields elsewhere."""

    live_config: dict[str, Any] = {
        "response_modalities": ["AUDIO"],
        "system_instruction": config.system_instruction,
    }
    if config.tool_declarations:
        live_config["tools"] = [
            {"function_declarations": [dict(item) for item in config.tool_declarations]}
        ]
    if config.enable_input_transcription:
        live_config["input_audio_transcription"] = {}
    if config.enable_output_transcription:
        live_config["output_audio_transcription"] = {}
    if config.enable_session_resumption:
        session_resumption: dict[str, str] = {}
        if resume_handle:
            session_resumption["handle"] = resume_handle
        live_config["session_resumption"] = session_resumption
    return live_config


class GeminiLiveProvider:
    """Google GenAI Live API adapter; optional and deliberately not server-registered by default."""

    provider_id = "gemini-live"

    def __init__(
        self,
        *,
        secret_provider: SecretProvider,
        api_key_secret_name: str = "GEMINI_API_KEY",
        model_id: str = "gemini-3.8-live",
        enabled: bool = False,
        allow_cloud: bool = False,
        security_allows_cloud: bool = False,
        client_factory: Callable[..., Any] | None = None,
        blob_factory: Callable[..., Any] | None = None,
        function_response_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not api_key_secret_name or not api_key_secret_name.replace("_", "").isalnum():
            raise ValueError("Gemini secret name must be an environment/keyring identifier")
        if not model_id.strip() or len(model_id) > 256:
            raise ValueError("Gemini Live model ID must be non-empty and bounded")
        self.secret_provider = secret_provider
        self.api_key_secret_name = api_key_secret_name
        self.model_id = model_id
        self.enabled = enabled
        self.allow_cloud = allow_cloud
        self.security_allows_cloud = security_allows_cloud
        self._client_factory = client_factory
        self._blob_factory = blob_factory
        self._function_response_factory = function_response_factory
        self._sessions: set[_GeminiLiveSession] = set()
        self._resume_handles: dict[str, str] = {}
        self._pending_connections = 0

    async def connect(self, config: LiveSessionConfig) -> LiveConversationSession:
        if not self.enabled:
            raise VoiceProviderFailure(
                VoiceProviderStatus.UNCONFIGURED,
                retryable=False,
                error_code="GEMINI_LIVE_DISABLED",
            )
        if not (self.allow_cloud and self.security_allows_cloud):
            raise VoiceProviderFailure(
                VoiceProviderStatus.UNCONFIGURED,
                retryable=False,
                error_code="GEMINI_CLOUD_POLICY_DISABLED",
            )
        try:
            api_key = self.secret_provider.get_secret(self.api_key_secret_name)
        except SecretUnavailable:
            raise VoiceProviderFailure(
                VoiceProviderStatus.AUTHENTICATION_FAILURE,
                retryable=False,
                error_code="GEMINI_CREDENTIAL_UNAVAILABLE",
            ) from None
        except Exception:
            raise VoiceProviderFailure(
                VoiceProviderStatus.AUTHENTICATION_FAILURE,
                retryable=False,
                error_code="GEMINI_CREDENTIAL_LOOKUP_FAILED",
            ) from None
        client: Any | None = None
        manager: Any | None = None
        self._pending_connections += 1
        try:
            client_factory, blob_factory, response_factory = self._sdk_factories()
            client = client_factory(api_key=api_key)
            resume_handle = self._resume_handles.get(config.session_id)
            manager = client.aio.live.connect(
                model=self.model_id,
                config=build_live_config(config, resume_handle=resume_handle),
            )
            session = await manager.__aenter__()
        except asyncio.CancelledError as exc:
            await self._cleanup_connect_attempt(manager, client, exc)
            raise
        except ImportError:
            await self._cleanup_connect_attempt(manager, client)
            raise VoiceProviderFailure(
                VoiceProviderStatus.UNCONFIGURED,
                retryable=False,
                error_code="GEMINI_SDK_NOT_INSTALLED",
            ) from None
        except Exception as exc:
            await self._cleanup_connect_attempt(manager, client)
            raise _provider_failure(exc, "GEMINI_CONNECT_FAILED") from None
        finally:
            self._pending_connections = max(0, self._pending_connections - 1)
        wrapper = _GeminiLiveSession(
            provider=self,
            session_id=config.session_id,
            client=client,
            manager=manager,
            session=session,
            blob_factory=blob_factory,
            function_response_factory=response_factory,
        )
        self._sessions.add(wrapper)
        return wrapper

    def diagnostics(self) -> dict[str, int]:
        """Expose resource counts only; session IDs and resume handles remain private."""

        return {
            "active_sessions": len(self._sessions),
            "pending_connections": self._pending_connections,
        }

    async def _cleanup_connect_attempt(
        self, manager: Any | None, client: Any | None, error: BaseException | None = None
    ) -> None:
        if manager is not None:
            try:
                await manager.__aexit__(
                    type(error) if error is not None else None,
                    error,
                    error.__traceback__ if error is not None else None,
                )
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        aio = getattr(client, "aio", None)
        close = getattr(aio, "aclose", None)
        if callable(close):
            try:
                await close()
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def close(self) -> None:
        sessions = tuple(self._sessions)
        if sessions:
            await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
        self._resume_handles.clear()

    def _sdk_factories(self) -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
        if (
            self._client_factory is not None
            and self._blob_factory is not None
            and self._function_response_factory is not None
        ):
            return (
                self._client_factory,
                self._blob_factory,
                self._function_response_factory,
            )
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ImportError("optional google-genai package is unavailable") from exc
        return (
            self._client_factory or genai.Client,
            self._blob_factory or types.Blob,
            self._function_response_factory or types.FunctionResponse,
        )

    def _save_resume_handle(self, session_id: str, handle: str | None) -> None:
        if handle:
            # Resumption handles are ephemeral provider state; never write them to disk/events.
            self._resume_handles[session_id] = handle


class _GeminiLiveSession:
    def __init__(
        self,
        *,
        provider: GeminiLiveProvider,
        session_id: str,
        client: Any,
        manager: Any,
        session: Any,
        blob_factory: Callable[..., Any],
        function_response_factory: Callable[..., Any],
    ) -> None:
        self.provider = provider
        self.session_id = session_id
        self.client = client
        self.manager = manager
        self.session = session
        self.blob_factory = blob_factory
        self.function_response_factory = function_response_factory
        self._utterance_id = str(uuid.uuid4())
        self._audio_sequence = 0
        self._output_generation = 0
        self._awaiting_interrupt_ack = False
        self._closed = False

    @property
    def output_generation(self) -> int:
        return self._output_generation

    async def send_audio(self, chunk: AudioChunk) -> None:
        if (
            chunk.codec not in {"pcm_s16le", "pcm16"}
            or chunk.channels != 1
            or chunk.sample_rate_hz != 16_000
        ):
            raise VoiceProviderFailure(
                VoiceProviderStatus.PROVIDER_ERROR,
                retryable=False,
                error_code="GEMINI_AUDIO_FORMAT_UNSUPPORTED",
            )
        try:
            await self.session.send_realtime_input(
                audio=self.blob_factory(
                    data=chunk.data,
                    mime_type=f"audio/pcm;rate={chunk.sample_rate_hz}",
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _provider_failure(exc, "GEMINI_AUDIO_SEND_FAILED") from None

    async def interrupt(self, first_user_audio: AudioChunk) -> int:
        """Send the first locally detected speech frame to trigger Gemini's server VAD barge-in.

        The generation fence moves before network I/O so already queued model audio becomes stale
        immediately. Gemini Live cancels an in-progress response when realtime user speech arrives;
        an interruption acknowledgement fences any late audio before the next model response.
        """

        if self._closed:
            raise VoiceProviderFailure(
                VoiceProviderStatus.NETWORK_FAILURE,
                retryable=True,
                error_code="GEMINI_SESSION_CLOSED",
            )
        self._output_generation += 1
        self._awaiting_interrupt_ack = True
        generation = self._output_generation
        await self.send_audio(first_user_audio)
        return generation

    async def send_text(self, text: str) -> None:
        if not text.strip() or len(text) > 16_384:
            raise ValueError("live text input must contain 1 to 16384 characters")
        try:
            await self.session.send_realtime_input(text=text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _provider_failure(exc, "GEMINI_TEXT_SEND_FAILED") from None

    async def send_tool_response(self, call: LiveToolCall, response: Mapping[str, Any]) -> None:
        try:
            function_response = self.function_response_factory(
                id=call.call_id,
                name=call.name,
                response=dict(response),
            )
            await self.session.send_tool_response(function_responses=[function_response])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _provider_failure(exc, "GEMINI_TOOL_RESPONSE_FAILED") from None

    async def receive(self) -> AsyncIterator[LiveEvent]:
        try:
            async for message in self.session.receive():
                events = self._normalize_message(message)
                for event in events:
                    yield event
        except asyncio.CancelledError:
            raise
        except VoiceProviderFailure:
            raise
        except Exception as exc:
            raise _provider_failure(exc, "GEMINI_RECEIVE_FAILED") from None

    def _normalize_message(self, message: Any) -> tuple[LiveEvent, ...]:
        events: list[LiveEvent] = []
        message_generation = (
            max(0, self._output_generation - 1)
            if self._awaiting_interrupt_ack
            else self._output_generation
        )
        audio_transcript: str | None = None
        go_away = _get(message, "go_away")
        if go_away is not None:
            events.append(LiveEvent(type=LiveEventType.GO_AWAY))
        resume_update = _get(message, "session_resumption_update")
        if resume_update is not None:
            resumable = bool(_get(resume_update, "resumable"))
            handle = _get(resume_update, "new_handle")
            if resumable and isinstance(handle, str):
                self.provider._save_resume_handle(self.session_id, handle)
                events.append(LiveEvent(type=LiveEventType.SESSION_RESUMPTION))
        content = _get(message, "server_content")
        if content is not None:
            if bool(_get(content, "interrupted")):
                events.append(
                    LiveEvent(
                        type=LiveEventType.INTERRUPTED,
                        generation_id=self._output_generation,
                    )
                )
                self._awaiting_interrupt_ack = False
            input_transcription = _get(content, "input_transcription")
            input_text = _get(input_transcription, "text")
            if isinstance(input_text, str) and input_text:
                events.append(
                    LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text=input_text[:16_384])
                )
            output_transcription = _get(content, "output_transcription")
            output_text = _get(output_transcription, "text")
            if isinstance(output_text, str) and output_text:
                audio_transcript = output_text[:16_384]
                events.append(
                    LiveEvent(
                        type=LiveEventType.OUTPUT_TRANSCRIPT,
                        text=audio_transcript,
                        is_audio_transcript=True,
                    )
                )
        text = _get(message, "text")
        if (
            isinstance(text, str)
            and text
            and not any(event.type is LiveEventType.OUTPUT_TRANSCRIPT for event in events)
        ):
            audio_transcript = text[:16_384]
            events.append(LiveEvent(type=LiveEventType.OUTPUT_TRANSCRIPT, text=audio_transcript))
        audio_data = _get(message, "data")
        if isinstance(audio_data, (bytes, bytearray)) and audio_data:
            events.extend(self._audio_events(bytes(audio_data), transcript=audio_transcript))
        tool_call = _get(message, "tool_call")
        function_calls = _get(tool_call, "function_calls") if tool_call is not None else None
        if isinstance(function_calls, (list, tuple)):
            for function_call in function_calls:
                try:
                    call = LiveToolCall(
                        call_id=str(_get(function_call, "id") or ""),
                        name=str(_get(function_call, "name") or ""),
                        arguments=_get(function_call, "args") or {},
                    )
                except (TypeError, ValueError):
                    events.append(
                        LiveEvent(
                            type=LiveEventType.ERROR,
                            error_status=VoiceProviderStatus.PROVIDER_ERROR,
                            retryable=False,
                            error_code="GEMINI_MALFORMED_TOOL_CALL",
                        )
                    )
                else:
                    events.append(LiveEvent(type=LiveEventType.TOOL_CALL, tool_call=call))
        if isinstance(audio_data, (bytes, bytearray)) and len(audio_data) >= 2:
            # Same-packet transcripts describe provider audio, not another TTS utterance.
            events = [
                replace(event, is_audio_transcript=True)
                if event.type is LiveEventType.OUTPUT_TRANSCRIPT
                else event
                for event in events
            ]
        if content is not None and bool(_get(content, "turn_complete")):
            # Keep transcript authorization and task-claim guards until every event
            # in this server packet has been handled, including calls and final audio.
            events.append(LiveEvent(type=LiveEventType.TURN_COMPLETE))
        identity = self._utterance_id
        if any(event.type is LiveEventType.TURN_COMPLETE for event in events):
            self._utterance_id = str(uuid.uuid4())
        return tuple(
            replace(
                event,
                utterance_id=identity,
                generation_id=event.generation_id
                if event.generation_id is not None
                else message_generation,
            )
            for event in events
        )

    def _audio_events(self, data: bytes, *, transcript: str | None = None) -> tuple[LiveEvent, ...]:
        if len(data) % 2:
            data = data[:-1]
        events: list[LiveEvent] = []
        for offset in range(0, len(data), _MAX_AUDIO_CHUNK_BYTES):
            part = data[offset : offset + _MAX_AUDIO_CHUNK_BYTES]
            if len(part) % 2:
                part = part[:-1]
            if not part:
                continue
            self._audio_sequence += 1
            audio = AudioChunk(
                sequence=self._audio_sequence,
                codec="pcm_s16le",
                sample_rate_hz=_GEMINI_OUTPUT_SAMPLE_RATE_HZ,
                channels=1,
                data=part,
            )
            events.append(LiveEvent(type=LiveEventType.OUTPUT_AUDIO, audio=audio, text=transcript))
        return tuple(events)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.provider._sessions.discard(self)
        try:
            await self.manager.__aexit__(None, None, None)
        except Exception:
            pass
        aio = getattr(self.client, "aio", None)
        close = getattr(aio, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass


def _get(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _provider_failure(exc: BaseException, fallback_code: str) -> VoiceProviderFailure:
    status_code = _get(exc, "status_code") or _get(exc, "code")
    if status_code in {401, 403, "401", "403"}:
        return VoiceProviderFailure(
            VoiceProviderStatus.AUTHENTICATION_FAILURE,
            retryable=False,
            error_code="GEMINI_AUTHENTICATION_FAILED",
        )
    if status_code in {429, "429"}:
        return VoiceProviderFailure(
            VoiceProviderStatus.QUOTA_LIMITED,
            retryable=False,
            error_code="GEMINI_QUOTA_LIMITED",
        )
    class_name = type(exc).__name__.lower()
    if any(token in class_name for token in ("timeout", "connect", "network", "websocket")):
        return VoiceProviderFailure(
            VoiceProviderStatus.NETWORK_FAILURE,
            retryable=True,
            error_code=fallback_code,
        )
    return VoiceProviderFailure(
        VoiceProviderStatus.PROVIDER_ERROR,
        retryable=False,
        error_code=fallback_code,
    )


__all__ = ["GeminiLiveProvider", "build_live_config"]
