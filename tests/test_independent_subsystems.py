"""Independent audit regressions: fake providers/audio, no Windows or cloud calls."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_gemini_live import Blob, FakeClient, FakeLiveEndpoint, FakeSdkSession, FunctionResponse
from test_voice import FakePlayback

from arise.adapters.gemini_live import GeminiLiveProvider
from arise.adapters.memory import InMemoryEnvironment
from arise.adapters.openai_compatible import OpenAICompatibleProvider
from arise.adapters.secrets import MemorySecretProvider
from arise.core.contracts import AuthorizationContext
from arise.core.engine import TaskEngine
from arise.core.errors import ProviderUnavailableError
from arise.core.events import InMemoryEventStore
from arise.core.model_gateway import ModelRouter
from arise.core.models import ModelMessage, ModelRequest, ModelResponse, ModelRole, ModelStreamChunk
from arise.core.personalization import WorkingMemoryStore
from arise.core.policy import PolicyEngine
from arise.core.ports import ToolRegistry
from arise.core.resources import ResourceManager
from arise.core.retry import CircuitBreaker, CircuitState
from arise.core.runtime import AgentRuntime, FactVerifier
from arise.core.tasks import InMemoryTaskRepository, TaskRecord, TaskStatus
from arise.core.voice import AudioHub, LiveEventType, LiveSessionConfig, VoiceState
from arise.core.voice_bridge import TaskEngineVoiceAdapter, VoiceConversationBridge


def request(**kwargs):
    return ModelRequest(
        role=ModelRole.PLANNER, messages=(ModelMessage(role="user", content="test"),), **kwargs
    )


class StreamProvider:
    model_ids = ("test-model",)
    is_cloud = False
    max_concurrent_requests = 1
    supports_streaming = True

    def __init__(self, name, *, mode="ok"):
        self.provider_id = name
        self.mode = mode
        self.calls = 0
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def supports(self, role, modalities):
        return role is ModelRole.PLANNER

    async def complete(self, req):
        self.started.set()
        if self.mode == "block":
            await self.release.wait()
        return ModelResponse(
            request_id=req.request_id,
            provider_id=self.provider_id,
            model_id="test-model",
            content="answer",
            latency_ms=1,
        )

    async def stream(self, req):
        self.calls += 1
        self.started.set()
        try:
            if self.mode == "before":
                raise RuntimeError("private provider error")
            if self.mode == "block":
                await self.release.wait()
            yield ModelStreamChunk(
                request_id=req.request_id,
                provider_id=self.provider_id,
                model_id="test-model",
                sequence=0,
                text_delta="first",
                is_final=self.mode == "ok",
            )
            if self.mode == "after":
                raise RuntimeError("private provider error")
        finally:
            self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["after", "truncated"])
async def test_partial_stream_never_appends_fallback_or_claims_success(mode):
    router = ModelRouter()
    first, second = StreamProvider("a-first", mode=mode), StreamProvider("z-second")
    router.register(first)
    router.register(second)
    stream = router.stream(request())
    assert (await anext(stream)).text_delta == "first"
    with pytest.raises(ProviderUnavailableError, match="interrupted after output") as error:
        await anext(stream)
    assert "private provider error" not in str(error.value)
    assert first.closed
    assert second.calls == 0
    assert router._providers[first.provider_id].last_success_at is None


@pytest.mark.asyncio
async def test_stream_fallback_is_still_allowed_before_any_output():
    router = ModelRouter()
    first, second = StreamProvider("a-first", mode="before"), StreamProvider("z-second")
    router.register(first)
    router.register(second)
    chunks = [chunk async for chunk in router.stream(request())]
    assert len(chunks) == 1 and chunks[0].is_final
    assert chunks[0].provider_id == second.provider_id
    assert first.closed and second.closed


@pytest.mark.asyncio
async def test_stream_model_pin_does_not_dispatch_to_wrong_provider():
    router = ModelRouter()
    provider = StreamProvider("local")
    router.register(provider)
    with pytest.raises(ProviderUnavailableError):
        _ = [chunk async for chunk in router.stream(request(model_id="other-model"))]
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("provider_id", "wrong"), ("request_id", "wrong"), ("model_id", "wrong"), ("sequence", 9)],
)
async def test_stream_rejects_mismatched_chunk_identity(field, value):
    class Wrong(StreamProvider):
        async def stream(self, req):
            chunk = ModelStreamChunk(
                request_id=req.request_id,
                provider_id=self.provider_id,
                model_id="test-model",
                sequence=0,
                is_final=True,
            )
            yield chunk.model_copy(update={field: value})

    router = ModelRouter()
    router.register(Wrong("local"))
    with pytest.raises(ProviderUnavailableError):
        _ = [chunk async for chunk in router.stream(request())]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_cancellation_releases_half_open_probe_without_claiming_recovery(streaming):
    router = ModelRouter()
    provider = StreamProvider("local", mode="block")
    router.register(provider)
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=1, clock=lambda: now[0])
    breaker.record_failure()
    now[0] = 2.0
    router._providers[provider.provider_id].breaker = breaker
    stream = router.stream(request()) if streaming else None
    pending = asyncio.create_task(anext(stream) if streaming else router.complete(request()))
    await asyncio.wait_for(provider.started.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.allow_request(), "cancelled half-open probe must not occupy the only slot"
    assert router._queued_requests == 0
    assert router._providers[provider.provider_id].last_success_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_queue_wait_honors_request_deadline_and_restores_capacity(streaming):
    router = ModelRouter(max_queued_requests=1)
    provider = StreamProvider("local")
    router.register(provider)
    semaphore = router._providers[provider.provider_id].semaphore
    await semaphore.acquire()
    try:
        req = request(timeout_seconds=0.02)
        with pytest.raises(ProviderUnavailableError):
            if streaming:
                await asyncio.wait_for(anext(router.stream(req)), 1)
            else:
                await asyncio.wait_for(router.complete(req), 1)
        assert router._queued_requests == 0
        assert router._providers[provider.provider_id].failures == 0
        assert router._providers[provider.provider_id].breaker.state is CircuitState.CLOSED
    finally:
        semaphore.release()
    assert [chunk async for chunk in router.stream(request())]


@pytest.mark.asyncio
async def test_stream_aclose_closes_provider_generator_and_releases_lease():
    router = ModelRouter()
    provider = StreamProvider("local", mode="truncated")
    router.register(provider)
    stream = router.stream(request())
    await anext(stream)
    await stream.aclose()
    assert provider.closed
    assert not router._providers[provider.provider_id].semaphore.locked()
    assert router._providers[provider.provider_id].last_success_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [True, False])
async def test_sse_has_exactly_one_terminal_chunk_or_reports_truncation(terminal):
    frames = 'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
    if terminal:
        frames += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=frames.encode()))
    ) as client:
        provider = OpenAICompatibleProvider(
            provider_id="local",
            base_url="http://127.0.0.1:1234/v1",
            model_id="test-model",
            api_key_secret_name="TEST_KEY",
            secret_provider=MemorySecretProvider(),
            is_cloud=False,
            supports_streaming=True,
            client=client,
        )
        if terminal:
            chunks = [chunk async for chunk in provider.stream(request())]
            assert sum(chunk.is_final for chunk in chunks) == 1
            assert "".join(chunk.text_delta for chunk in chunks) == "Hello"
        else:
            with pytest.raises(ProviderUnavailableError):
                _ = [chunk async for chunk in provider.stream(request())]


def test_working_memory_never_merges_or_reassigns_other_principal_data():
    store = WorkingMemoryStore()
    original = store.upsert(
        task_id="task",
        principal_id="alice",
        goal="private goal",
        observations={"private": "alice-only"},
        note="alice note",
    )
    with pytest.raises(PermissionError):
        store.upsert(task_id="task", principal_id="bob", goal="replace")
    assert store.get("task", principal_id="bob") is None
    assert store.get("task", principal_id="alice") == original
    updated = store.upsert(task_id="task", principal_id="alice", goal="updated", note="new note")
    assert updated.observations["private"] == "alice-only"


def engine_fixture():
    tasks = InMemoryTaskRepository()
    events, tools, policy = InMemoryEventStore(), ToolRegistry(), PolicyEngine()
    env = InMemoryEnvironment({})
    runtime = AgentRuntime(
        tasks=tasks,
        events=events,
        tools=tools,
        policy=policy,
        environment=env,
        verifier=FactVerifier(env),
        resources=ResourceManager(),
    )
    return TaskEngine(tasks=tasks, events=events, tools=tools, policy=policy, runtime=runtime)


def task_fixture(engine, *, parent=None):
    task = TaskRecord.new(
        "test",
        authorization=AuthorizationContext("alice", "request"),
        parent_task_id=parent,
        session_id=engine.tasks.get(parent).session_id if parent else None,
    )
    task.status = TaskStatus.PARTIALLY_COMPLETED  # simulated between-step snapshot
    engine.tasks.save(task)
    return task


@pytest.mark.asyncio
async def test_parent_cancel_reaches_child_between_steps():
    engine = engine_fixture()
    parent = task_fixture(engine)
    child = task_fixture(engine, parent=parent.task_id)
    started = asyncio.Event()

    async def active_child():
        started.set()
        await asyncio.Event().wait()

    active = asyncio.create_task(active_child())
    engine._active[child.task_id] = active
    await started.wait()
    try:
        await engine.cancel(parent.task_id, principal_id="alice")
        assert active.cancelled()
        assert engine.tasks.get(child.task_id).status is TaskStatus.CANCELLED
    finally:
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)


@pytest.mark.asyncio
async def test_voice_watcher_continues_past_partial_completion():
    engine = engine_fixture()
    task = task_fixture(engine)
    engine._active[task.task_id] = SimpleNamespace(done=lambda: False)
    watch = TaskEngineVoiceAdapter(engine).watch(
        task.task_id, principal_id="alice", poll_interval_seconds=0
    )
    assert (await anext(watch)).status is TaskStatus.PARTIALLY_COMPLETED
    task = engine.tasks.get(task.task_id)
    task.status = TaskStatus.COMPLETED  # simulated verifier-completed snapshot
    engine.tasks.save(task)
    assert (await asyncio.wait_for(anext(watch), 1)).status is TaskStatus.COMPLETED
    with pytest.raises(StopAsyncIteration):
        await anext(watch)


@pytest.mark.asyncio
async def test_gemini_packet_preserves_turn_guard_and_avoids_double_synthesis():
    sdk = FakeSdkSession(
        [
            SimpleNamespace(
                server_content=SimpleNamespace(
                    input_transcription=SimpleNamespace(text="Hello"),
                    output_transcription=SimpleNamespace(text="Hello there"),
                    turn_complete=True,
                ),
                data=b"\x01\x00" * 8,
            )
        ]
    )
    endpoint = FakeLiveEndpoint([sdk])
    provider = GeminiLiveProvider(
        secret_provider=MemorySecretProvider({"GEMINI_API_KEY": "fake"}),
        enabled=True,
        allow_cloud=True,
        security_allows_cloud=True,
        client_factory=lambda **_: FakeClient(endpoint),
        blob_factory=Blob,
        function_response_factory=FunctionResponse,
    )
    session = await provider.connect(LiveSessionConfig("session"))
    hub = AudioHub(
        microphone=None,
        vad=None,
        wake_word_detector=None,
        provider=None,
        playback=FakePlayback(),
        speech_synthesizer=object(),
    )
    hub._state = VoiceState.LISTENING
    hub.speak_text = AsyncMock()
    try:
        events = [event async for event in session.receive()]
        assert events[-1].type is LiveEventType.TURN_COMPLETE
        transcript = next(
            event for event in events if event.type is LiveEventType.OUTPUT_TRANSCRIPT
        )
        assert transcript.is_final and transcript.is_audio_transcript
        for event in events:
            await hub._handle_live_event(session, event)
        hub.speak_text.assert_not_awaited()
        assert len(hub.playback.played) == 1
        assert hub.state is VoiceState.LISTENING
        hub._record_task_state("task", "partially_completed")
        assert hub._active_task_id == "task"
    finally:
        await hub.close()
        await provider.close()


def test_pre_admission_voice_ack_does_not_claim_a_task_exists():
    tasks = SimpleNamespace(submit=AsyncMock(side_effect=RuntimeError("queue full")))
    bridge = VoiceConversationBridge(tasks)
    ack = bridge.immediate_acknowledgement("Open Chrome")
    assert "admission is not yet confirmed" in ack
    tasks.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_transcription_before_separate_audio_is_not_synthesized_as_another_utterance():
    sdk = FakeSdkSession(
        [
            SimpleNamespace(
                server_content=SimpleNamespace(
                    output_transcription=SimpleNamespace(text="Hello there")
                )
            )
        ]
    )
    endpoint = FakeLiveEndpoint([sdk])
    provider = GeminiLiveProvider(
        secret_provider=MemorySecretProvider({"GEMINI_API_KEY": "fake"}),
        enabled=True,
        allow_cloud=True,
        security_allows_cloud=True,
        client_factory=lambda **_: FakeClient(endpoint),
        blob_factory=Blob,
        function_response_factory=FunctionResponse,
    )
    session = await provider.connect(LiveSessionConfig("session"))
    hub = AudioHub(
        microphone=None,
        vad=None,
        wake_word_detector=None,
        provider=None,
        playback=FakePlayback(),
        speech_synthesizer=object(),
    )
    hub.speak_text = AsyncMock()
    try:
        async for event in session.receive():
            await hub._handle_live_event(session, event)
        hub.speak_text.assert_not_awaited()
    finally:
        await hub.close()
        await provider.close()


@pytest.mark.asyncio
async def test_cancelling_old_closed_circuit_request_does_not_release_another_probe():
    provider = StreamProvider("local", mode="block")
    router = ModelRouter()
    router.register(provider)
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=1, clock=lambda: now[0])
    router._providers[provider.provider_id].breaker = breaker
    pending = asyncio.create_task(router.complete(request()))
    await provider.started.wait()
    breaker.record_failure()
    now[0] = 2.0
    assert breaker.allow_request()  # an independent half-open probe owns the slot
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not breaker.allow_request()


def test_text_cancellation_finds_active_task_between_steps(tmp_path):
    from fastapi.testclient import TestClient

    from arise.config.settings import AppSettings, SecuritySettings
    from arise.server import create_app

    app = create_app(
        AppSettings(
            data_dir=tmp_path, security=SecuritySettings(environment="test", require_api_auth=False)
        )
    )
    with TestClient(app) as client:
        services = app.state.services
        session_id = client.post("/api/v1/sessions", json={}).json()["session_id"]
        task = TaskRecord.new(
            "multi-step",
            session_id=session_id,
            authorization=AuthorizationContext("local-user", "request"),
        )
        task.status = TaskStatus.PARTIALLY_COMPLETED
        services.tasks.save(task)
        cancelled = TaskRecord.new("cancelled", task_id=task.task_id, session_id=session_id)
        cancelled.status = TaskStatus.CANCELLED
        original_cancel = services.engine.cancel
        services.engine.cancel = AsyncMock(return_value=cancelled)
        services.engine._active[task.task_id] = SimpleNamespace(done=lambda: False)
        try:
            response = client.post(
                "/api/v1/interactions",
                json={
                    "text": "cancel the task",
                    "session_id": session_id,
                },
            )
            assert response.status_code == 200
            services.engine.cancel.assert_awaited_once_with(task.task_id, principal_id="local-user")
            assert response.json()["task"]["state"] == "cancelled"
        finally:
            services.engine._active.pop(task.task_id, None)
            services.engine.cancel = original_cancel


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["provider_id", "model_id"])
async def test_complete_rejects_response_from_other_provider_or_model(field):
    class Wrong(StreamProvider):
        async def complete(self, req):
            response = await super().complete(req)
            return response.model_copy(update={field: "wrong"})

    provider = Wrong("local")
    router = ModelRouter()
    router.register(provider)
    with pytest.raises(ProviderUnavailableError):
        await router.complete(request())
    assert router._providers[provider.provider_id].last_success_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["[]", '{"choices":[null]}', '{"choices":[{"delta":[]}]}'])
async def test_malformed_sse_events_are_typed_failures(event):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=f"data: {event}\n\ndata: [DONE]\n\n".encode())
        )
    ) as client:
        provider = OpenAICompatibleProvider(
            provider_id="local",
            base_url="http://127.0.0.1:1234/v1",
            model_id="test-model",
            api_key_secret_name="TEST_KEY",
            secret_provider=MemorySecretProvider(),
            is_cloud=False,
            supports_streaming=True,
            client=client,
        )
        with pytest.raises(ProviderUnavailableError):
            _ = [chunk async for chunk in provider.stream(request())]


@pytest.mark.asyncio
async def test_stream_queue_backpressure_does_not_admit_unbounded_waiters():
    router = ModelRouter(max_queued_requests=1)
    provider = StreamProvider("local")
    router.register(provider)
    semaphore = router._providers[provider.provider_id].semaphore
    await semaphore.acquire()
    queued = asyncio.create_task(anext(router.stream(request())))
    try:
        await asyncio.sleep(0)  # allow the first request to enter the semaphore queue
        assert router._queued_requests == 1
        with pytest.raises(ProviderUnavailableError, match="backpressure"):
            await anext(router.stream(request()))
        assert provider.calls == 0
    finally:
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        semaphore.release()
    assert router._queued_requests == 0


def test_memory_patch_requires_exact_one_time_consent_and_honors_disabled_state(tmp_path):
    from datetime import UTC, datetime, timedelta

    from fastapi.testclient import TestClient

    from arise.config.settings import AppSettings, SecuritySettings
    from arise.server import create_app

    app = create_app(
        AppSettings(
            data_dir=tmp_path, security=SecuritySettings(environment="test", require_api_auth=False)
        )
    )
    with TestClient(app) as client:
        draft = {
            "text": "Prefer concise answers",
            "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        }
        consent = client.post("/api/v1/memory/consents", json=draft).json()["consent_reference"]
        created = client.post("/api/v1/memory", json={**draft, "consent_reference": consent})
        assert created.status_code == 201
        url = f"/api/v1/memory/{created.json()['record_id']}"
        assert client.patch(url, json={"expires_at": "2030-01-01T00:00:00"}).status_code == 422
        embedding = AsyncMock()
        app.state.services.memory.embedding = SimpleNamespace(embed=embedding)
        edit = {**draft, "text": "Prefer detailed answers"}
        assert client.patch(url, json={"text": edit["text"]}).status_code == 403
        consent = client.post("/api/v1/memory/consents", json=edit).json()["consent_reference"]
        assert (
            client.patch(
                url, json={"text": "wrong scope", "consent_reference": consent}
            ).status_code
            == 403
        )
        embedding.assert_not_awaited()
        from arise.core.extensions import EmbeddingResult

        embedding.return_value = EmbeddingResult("fake", (1.0, 0.0))
        patch = {"text": edit["text"], "consent_reference": consent}
        assert client.patch(url, json=patch).status_code == 200
        assert client.patch(url, json=patch).status_code == 403
        embedding.assert_awaited_once()
        consent = client.post("/api/v1/memory/consents", json=edit).json()["consent_reference"]
        assert client.put("/api/v1/memory/settings", json={"enabled": False}).status_code == 200
        assert client.patch(url, json={**patch, "consent_reference": consent}).status_code == 403
        embedding.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("confidence", 0.5), ("sensitivity", "public"), ("expiration_policy", "pinned")]
)
async def test_memory_consent_cannot_be_reused_for_changed_governance_metadata(field, value):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from arise.adapters.sqlite import SQLiteDatabase, SQLiteMemoryRepository
    from arise.core.extensions import MemoryConsentError, MemoryEntry

    database = SQLiteDatabase(":memory:")
    repo = SQLiteMemoryRepository(database)
    try:
        draft = MemoryEntry(
            "alice", "Prefer concise answers", "pending", datetime.now(UTC) + timedelta(days=1)
        )
        consent, _ = await repo.issue_write_consent(draft)
        with pytest.raises(MemoryConsentError):
            await repo.store(replace(draft, consent_reference=consent, **{field: value}))
        assert repo.list_records(principal_id="alice") == []
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["store", "update", "delete_during_update"])
async def test_memory_mutation_during_embedding_does_not_report_a_false_write(operation):
    from dataclasses import replace
    from datetime import UTC, datetime, timedelta

    from arise.adapters.sqlite import SQLiteDatabase, SQLiteMemoryRepository
    from arise.core.extensions import EmbeddingResult, MemoryDisabledError, MemoryEntry

    database = SQLiteDatabase(":memory:")
    repo = SQLiteMemoryRepository(database)
    try:
        draft = MemoryEntry(
            "alice", "Prefer concise answers", "pending", datetime.now(UTC) + timedelta(days=1)
        )
        consent, _ = await repo.issue_write_consent(draft)
        record_id = await repo.store(replace(draft, consent_reference=consent))

        async def embed(*args, **kwargs):
            if operation == "delete_during_update":
                await repo.delete(principal_id="alice", record_id=record_id)
            else:
                repo.set_enabled(principal_id="alice", enabled=False)
            return EmbeddingResult("fake", (1.0, 0.0))

        repo.embedding = SimpleNamespace(embed=embed)
        draft = replace(draft, text="Prefer detailed answers")
        consent, _ = await repo.issue_write_consent(draft)
        error = LookupError if operation == "delete_during_update" else MemoryDisabledError
        with pytest.raises(error):
            if operation == "store":
                await repo.store(replace(draft, consent_reference=consent))
            else:
                await repo.update_record(
                    principal_id="alice",
                    record_id=record_id,
                    text=draft.text,
                    consent_reference=consent,
                )
        assert all(record.text != draft.text for record in repo.list_records(principal_id="alice"))
    finally:
        database.close()


def test_persistence_warning_does_not_echo_exception_secrets(tmp_path, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from arise.config.settings import AppSettings, SecuritySettings
    from arise.server import create_app

    app = create_app(
        AppSettings(
            data_dir=tmp_path, security=SecuritySettings(environment="test", require_api_auth=False)
        )
    )
    with TestClient(app) as client:

        def fail(*args, **kwargs):
            raise RuntimeError("private-content-sentinel")

        monkeypatch.setattr("arise.server._persist_text_interaction", fail)
        response = client.post("/api/v1/interactions", json={"text": "hello"})
        assert response.status_code == 200
        assert "not fully persisted" in caplog.text
        assert "private-content-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_supervisor_tolerates_http_binding_after_lifespan_ready_signal():
    from unittest.mock import Mock

    from arise.supervisor import BackendSupervisor, SupervisorConfig

    proc = SimpleNamespace(
        returncode=None,
        wait_ready=AsyncMock(return_value=True),
        wait_exit=AsyncMock(return_value=0),
        close_stdin=AsyncMock(),
        terminate_forcefully=Mock(),
    )
    health = Mock(side_effect=[False, False, True])
    supervisor = BackendSupervisor(
        SupervisorConfig(startup_timeout_seconds=1, health_check_interval_seconds=0.01),
        spawner=lambda: proc,
        health_probe=health,
    )
    try:
        await supervisor.start()
        assert supervisor.is_running
        assert health.call_count == 3
        proc.terminate_forcefully.assert_not_called()
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_supervisor_cleans_unpublished_process_on_cancel_or_health_timeout(cancel):
    from unittest.mock import Mock

    from arise.supervisor import BackendSupervisor, SupervisorConfig

    started = asyncio.Event()

    async def health():
        started.set()
        await asyncio.Event().wait()

    proc = SimpleNamespace(
        returncode=None,
        wait_ready=AsyncMock(return_value=True),
        wait_exit=AsyncMock(return_value=0),
        close_stdin=AsyncMock(),
        terminate_forcefully=Mock(),
    )
    supervisor = BackendSupervisor(
        SupervisorConfig(startup_timeout_seconds=10 if cancel else 0.03),
        spawner=lambda: proc,
        health_probe=health,
    )
    pending = asyncio.create_task(supervisor.start())
    await started.wait()
    if cancel:
        pending.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await pending
    assert not supervisor.is_running
    proc.terminate_forcefully.assert_called_once()
    proc.wait_exit.assert_awaited_once()
    await supervisor.stop()
