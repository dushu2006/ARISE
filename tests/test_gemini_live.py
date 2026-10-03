from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator
from types import SimpleNamespace

from arise.adapters.gemini_live import GeminiLiveProvider, build_live_config
from arise.adapters.secrets import MemorySecretProvider
from arise.core.extensions import AudioChunk
from arise.core.models import VoiceProviderStatus
from arise.core.voice import (
    LiveEventType,
    LiveSessionConfig,
    LiveToolCall,
    VoiceProviderFailure,
)


class Blob:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class FunctionResponse:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class FakeSdkSession:
    def __init__(self, messages=()) -> None:
        self.messages = list(messages)
        self.audio: list[Blob] = []
        self.text: list[str] = []
        self.tool_responses: list[FunctionResponse] = []

    async def send_realtime_input(self, *, audio=None, text=None) -> None:
        if audio is not None:
            self.audio.append(audio)
        if text is not None:
            self.text.append(text)

    async def send_tool_response(self, *, function_responses) -> None:
        self.tool_responses.extend(function_responses)

    async def receive(self) -> AsyncIterator[object]:
        for message in self.messages:
            yield message


class FakeManager:
    def __init__(self, session: FakeSdkSession) -> None:
        self.session = session
        self.closed = False

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args) -> None:
        del args
        self.closed = True


class FakeLiveEndpoint:
    def __init__(self, sessions) -> None:
        self.sessions = list(sessions)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.managers: list[FakeManager] = []

    def connect(self, *, model: str, config: dict[str, object]):
        self.calls.append((model, config))
        manager = FakeManager(self.sessions.pop(0))
        self.managers.append(manager)
        return manager


class FakeClient:
    def __init__(self, endpoint: FakeLiveEndpoint) -> None:
        self.aio = SimpleNamespace(live=endpoint, aclose=self._close)
        self.closed = False

    async def _close(self) -> None:
        self.closed = True


class GeminiLiveAdapterTests(unittest.IsolatedAsyncioTestCase):
    def make_provider(self, sessions, *, secret="test-gemini-key", **kwargs):
        endpoint = FakeLiveEndpoint(sessions)
        clients: list[FakeClient] = []

        def client_factory(*, api_key: str):
            if api_key != secret:
                raise AssertionError("secret provider result was not used")
            client = FakeClient(endpoint)
            clients.append(client)
            return client

        provider = GeminiLiveProvider(
            secret_provider=MemorySecretProvider({"GEMINI_API_KEY": secret}),
            enabled=True,
            allow_cloud=True,
            security_allows_cloud=True,
            client_factory=client_factory,
            blob_factory=Blob,
            function_response_factory=FunctionResponse,
            **kwargs,
        )
        return provider, endpoint, clients

    async def test_provider_requires_enabled_cloud_policy_and_secret(self) -> None:
        disabled = GeminiLiveProvider(
            secret_provider=MemorySecretProvider({"GEMINI_API_KEY": "key"}),
            enabled=False,
            allow_cloud=True,
            security_allows_cloud=True,
            client_factory=lambda **_: None,
            blob_factory=Blob,
            function_response_factory=FunctionResponse,
        )
        with self.assertRaises(VoiceProviderFailure) as disabled_error:
            await disabled.connect(LiveSessionConfig("session-disabled"))
        self.assertEqual(disabled_error.exception.status, VoiceProviderStatus.UNCONFIGURED)

        no_policy = GeminiLiveProvider(
            secret_provider=MemorySecretProvider({"GEMINI_API_KEY": "key"}),
            enabled=True,
            allow_cloud=False,
            security_allows_cloud=True,
            client_factory=lambda **_: None,
            blob_factory=Blob,
            function_response_factory=FunctionResponse,
        )
        with self.assertRaises(VoiceProviderFailure) as policy_error:
            await no_policy.connect(LiveSessionConfig("session-policy"))
        self.assertEqual(policy_error.exception.error_code, "GEMINI_CLOUD_POLICY_DISABLED")

        missing_secret = GeminiLiveProvider(
            secret_provider=MemorySecretProvider(),
            enabled=True,
            allow_cloud=True,
            security_allows_cloud=True,
            client_factory=lambda **_: None,
            blob_factory=Blob,
            function_response_factory=FunctionResponse,
        )
        with self.assertRaises(VoiceProviderFailure) as secret_error:
            await missing_secret.connect(LiveSessionConfig("session-secret"))
        self.assertEqual(secret_error.exception.status, VoiceProviderStatus.AUTHENTICATION_FAILURE)
        self.assertNotIn("key", str(secret_error.exception))

    async def test_cancelled_connection_closes_sdk_manager_and_client(self) -> None:
        started = asyncio.Event()

        class BlockingManager(FakeManager):
            async def __aenter__(self):
                started.set()
                await asyncio.Future()

        manager = BlockingManager(FakeSdkSession())
        endpoint = SimpleNamespace(connect=lambda **_: manager)
        clients: list[FakeClient] = []

        def client_factory(*, api_key: str):
            self.assertEqual(api_key, "test-gemini-key")
            client = FakeClient(endpoint)
            clients.append(client)
            return client

        provider = GeminiLiveProvider(
            secret_provider=MemorySecretProvider({"GEMINI_API_KEY": "test-gemini-key"}),
            enabled=True,
            allow_cloud=True,
            security_allows_cloud=True,
            client_factory=client_factory,
            blob_factory=Blob,
            function_response_factory=FunctionResponse,
        )
        connecting = asyncio.create_task(provider.connect(LiveSessionConfig("cancel-connect")))
        await asyncio.wait_for(started.wait(), timeout=1)
        connecting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await connecting

        self.assertTrue(manager.closed)
        self.assertTrue(clients[0].closed)
        self.assertEqual(provider.diagnostics(), {"active_sessions": 0, "pending_connections": 0})

    async def test_supported_config_audio_tools_and_transient_event_mapping(self) -> None:
        function_call = SimpleNamespace(
            id="provider-call-1", name="execute_task", args={"text": "Open Chrome"}
        )
        message = SimpleNamespace(
            server_content=SimpleNamespace(
                interrupted=True,
                turn_complete=True,
                input_transcription=SimpleNamespace(text="Open Chrome"),
                output_transcription=SimpleNamespace(text="ARISE accepted the task."),
            ),
            data=b"\x01\x00" * 8,
            tool_call=SimpleNamespace(function_calls=[function_call]),
            go_away=None,
            session_resumption_update=SimpleNamespace(
                resumable=True, new_handle="ephemeral-resumption-handle"
            ),
        )
        sdk_session = FakeSdkSession([message])
        provider, endpoint, clients = self.make_provider([sdk_session])
        config = LiveSessionConfig("session-live", locale="en-US")
        session = await provider.connect(config)

        sent = AudioChunk(1, "pcm_s16le", 16_000, 1, b"\x00\x00" * 4)
        await session.send_audio(sent)
        await session.send_text("What is the task status?")
        tool_call = LiveToolCall("provider-call-1", "execute_task", {"text": "Open Chrome"})
        await session.send_tool_response(tool_call, {"status": "accepted", "verified": False})

        events = [event async for event in session.receive()]
        event_types = [event.type for event in events]
        self.assertIn(LiveEventType.INPUT_TRANSCRIPT, event_types)
        self.assertIn(LiveEventType.OUTPUT_TRANSCRIPT, event_types)
        self.assertIn(LiveEventType.OUTPUT_AUDIO, event_types)
        self.assertIn(LiveEventType.TOOL_CALL, event_types)
        self.assertIn(LiveEventType.INTERRUPTED, event_types)
        self.assertIn(LiveEventType.TURN_COMPLETE, event_types)
        self.assertIn(LiveEventType.SESSION_RESUMPTION, event_types)

        model, live_config = endpoint.calls[0]
        self.assertEqual(model, "gemini-3.8-live")
        self.assertIn("not the computer-control authority", live_config["system_instruction"])
        self.assertTrue(live_config["input_audio_transcription"] == {})
        self.assertTrue(live_config["output_audio_transcription"] == {})
        tools = live_config["tools"][0]["function_declarations"]
        self.assertEqual(
            {tool["name"] for tool in tools},
            {
                "execute_task",
                "ask_user",
                "request_clarification",
                "get_task_status",
                "report_status",
                "cancel_task",
            },
        )
        self.assertEqual(sdk_session.audio[0].kwargs["mime_type"], "audio/pcm;rate=16000")
        self.assertEqual(sdk_session.text, ["What is the task status?"])
        self.assertFalse(clients[0].closed)
        self.assertEqual(provider._resume_handles["session-live"], "ephemeral-resumption-handle")
        await session.close()
        self.assertTrue(endpoint.managers[0].closed)
        self.assertTrue(clients[0].closed)

    async def test_interrupt_generation_fences_late_audio_until_provider_ack(self) -> None:
        stale = SimpleNamespace(
            server_content=SimpleNamespace(
                interrupted=False,
                output_transcription=SimpleNamespace(text="old response"),
            ),
            data=b"\x01\x00" * 4,
        )
        acknowledgement = SimpleNamespace(
            server_content=SimpleNamespace(
                interrupted=True,
                output_transcription=SimpleNamespace(text="late old response"),
            ),
            data=b"\x02\x00" * 4,
        )
        fresh = SimpleNamespace(
            server_content=SimpleNamespace(
                interrupted=False,
                output_transcription=SimpleNamespace(text="new response"),
            ),
            data=b"\x03\x00" * 4,
        )
        sdk_session = FakeSdkSession([stale, acknowledgement, fresh])
        provider, _, _ = self.make_provider([sdk_session])
        session = await provider.connect(LiveSessionConfig("session-interrupt"))
        first_speech = AudioChunk(1, "pcm_s16le", 16_000, 1, b"\x00\x00" * 160)

        generation = await session.interrupt(first_speech)
        events = [event async for event in session.receive()]
        audio_events = [event for event in events if event.type is LiveEventType.OUTPUT_AUDIO]
        transcript_events = [
            event for event in events if event.type is LiveEventType.OUTPUT_TRANSCRIPT
        ]
        interruption = next(event for event in events if event.type is LiveEventType.INTERRUPTED)
        self.assertEqual(generation, 1)
        self.assertEqual(session.output_generation, 1)
        self.assertEqual([event.generation_id for event in audio_events], [0, 0, 1])
        self.assertEqual([event.generation_id for event in transcript_events], [0, 0, 1])
        self.assertEqual(interruption.generation_id, 1)
        self.assertEqual(len(sdk_session.audio), 1)
        await session.close()

    async def test_resumption_handle_is_reused_only_in_memory(self) -> None:
        provider, endpoint, _ = self.make_provider([FakeSdkSession(), FakeSdkSession()])
        config = LiveSessionConfig("session-resume")
        first = await provider.connect(config)
        provider._save_resume_handle(config.session_id, "resume-token")
        await first.close()
        second = await provider.connect(config)
        self.assertEqual(endpoint.calls[1][1]["session_resumption"], {"handle": "resume-token"})
        self.assertNotIn("resume-token", repr(provider))
        await second.close()
        await provider.close()
        self.assertEqual(provider._resume_handles, {})

    async def test_unsupported_input_audio_format_fails_closed(self) -> None:
        provider, _, _ = self.make_provider([FakeSdkSession()])
        session = await provider.connect(LiveSessionConfig("session-format"))
        with self.assertRaises(VoiceProviderFailure) as context:
            await session.send_audio(AudioChunk(1, "pcm_s16le", 48_000, 2, b"\x00\x00"))
        self.assertEqual(context.exception.error_code, "GEMINI_AUDIO_FORMAT_UNSUPPORTED")
        await session.close()

    async def test_auth_error_is_sanitized(self) -> None:
        class Unauthorized(Exception):
            status_code = 401

        class BrokenEndpoint(FakeLiveEndpoint):
            def connect(self, *, model: str, config: dict[str, object]):
                del model, config
                raise Unauthorized("api_key=do-not-log")

        endpoint = BrokenEndpoint([])
        provider = GeminiLiveProvider(
            secret_provider=MemorySecretProvider({"GEMINI_API_KEY": "secret-value"}),
            enabled=True,
            allow_cloud=True,
            security_allows_cloud=True,
            client_factory=lambda **_: FakeClient(endpoint),
            blob_factory=Blob,
            function_response_factory=FunctionResponse,
        )
        with self.assertRaises(VoiceProviderFailure) as context:
            await provider.connect(LiveSessionConfig("session-auth"))
        self.assertEqual(context.exception.status, VoiceProviderStatus.AUTHENTICATION_FAILURE)
        self.assertNotIn("do-not-log", str(context.exception))
        self.assertNotIn("secret-value", str(context.exception))

    async def test_config_has_no_resume_handle_until_provider_supplies_one(self) -> None:
        config = build_live_config(LiveSessionConfig("session-fresh"))
        self.assertEqual(config["session_resumption"], {})
        self.assertNotIn("resume-token", repr(config))

    async def test_empty_diagnostic_tool_set_is_omitted_from_provider_config(self) -> None:
        config = build_live_config(LiveSessionConfig("session-diagnostic", tool_declarations=()))
        self.assertNotIn("tools", config)


if __name__ == "__main__":
    unittest.main()
