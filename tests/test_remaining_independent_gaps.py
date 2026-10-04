"""Deterministic independent contracts; no Windows, devices, or cloud calls."""

import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_browser_playwright import FakeContext, FakePage
from test_independent_subsystems import engine_fixture
from test_voice import FakeLiveSession, FakePlayback, FakeVoiceEventSink
from test_voice_bridge import FakeTasks

from arise.adapters.browser_playwright import PlaywrightActionTool, PlaywrightBrowserProvider
from arise.adapters.sqlite import SQLiteDatabase, SQLiteMemoryRepository, SQLiteTaskRepository
from arise.core.computer import ComputerFailureCode, ResolutionStatus, TargetQuery
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    Condition,
    ConditionOperator,
    RiskLevel,
)
from arise.core.extensions import EmbeddingResult, MemoryConsentError, MemoryDisabledError
from arise.core.models import UserRequest
from arise.core.ports import ExecutionStatus, VerificationStatus
from arise.core.resources import ResourceManager
from arise.core.tasks import TaskRecord, TaskStatus
from arise.core.voice import AudioHub, LiveEvent, LiveEventType, LiveToolCall, VoiceState
from arise.core.voice_bridge import TaskEngineVoiceAdapter, VoiceConversationBridge


def hub_fixture(bridge=None):
    return AudioHub(
        microphone=None,
        vad=None,
        wake_word_detector=None,
        provider=None,
        playback=FakePlayback(),
        conversation_bridge=bridge,
        event_sink=FakeVoiceEventSink(),
    )


@pytest.mark.asyncio
async def test_identical_commands_are_distinct_but_exact_retries_admit_once():
    tasks = FakeTasks()
    bridge = VoiceConversationBridge(tasks)
    args = {"principal_id": "alice", "session_id": "session"}
    replies = await asyncio.gather(
        *[
            bridge.process_utterance("Open Chrome", utterance_id=identity, **args)
            for identity in ("utterance-1", "utterance-2", "utterance-1")
        ]
    )
    assert [request.request_id for request in tasks.requests] == ["utterance-1", "utterance-2"]
    assert replies[0]["task_id"] != replies[1]["task_id"]
    assert replies[2]["task_id"] == replies[0]["task_id"]
    assert replies[2]["replayed"]
    conflict = await bridge.process_utterance("Open Notepad", utterance_id="utterance-1", **args)
    assert conflict["status"] == "not_authorized"
    assert len(tasks.requests) == 2
    fresh = await bridge.process_utterance("Open Chrome", **args)
    assert fresh["utterance_id"] not in {"utterance-1", "utterance-2"}


@pytest.mark.asyncio
async def test_same_utterance_token_does_not_cross_principal_or_session():
    tasks = FakeTasks()
    bridge = VoiceConversationBridge(tasks)
    for principal, session in (("alice", "one"), ("bob", "one"), ("alice", "two")):
        response = await bridge.process_utterance(
            "Open Chrome", principal_id=principal, session_id=session, utterance_id="same-token"
        )
        assert not response["replayed"]
    assert len(tasks.requests) == 3
    assert len(tasks.records) == 3


@pytest.mark.asyncio
async def test_replayed_admission_returns_current_settled_state_not_old_queue_snapshot():
    tasks = FakeTasks()
    bridge = VoiceConversationBridge(tasks)
    args = {"principal_id": "alice", "session_id": "session", "utterance_id": "utterance"}
    first = await bridge.process_utterance("Open Chrome", **args)
    tasks.records[first["task_id"]].status = TaskStatus.COMPLETED
    replay = await bridge.process_utterance("Open Chrome", **args)
    assert replay["state"] == "completed" and replay["settled"]
    assert replay["verified"] and replay["replayed"]
    assert len(tasks.requests) == 1


@pytest.mark.asyncio
async def test_rapid_informational_replies_and_speech_preserve_order_and_identity():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def answer(text, session, locale):
        calls.append(text)
        if text == "What is first?":
            entered.set()
            await release.wait()
        return text

    bridge = VoiceConversationBridge(FakeTasks(), informational_responder=answer)
    hub = hub_fixture(bridge)
    hub.speech_synthesizer = object()
    spoken = []

    async def speak(text):
        spoken.append((hub._utterance_context.get(), text))

    hub.speak_text = speak
    first = asyncio.create_task(hub.process_spoken_utterance("What is first?", utterance_id="one"))
    await entered.wait()
    second = asyncio.create_task(
        hub.process_spoken_utterance("What is second?", utterance_id="two")
    )
    await asyncio.sleep(0)
    assert calls == ["What is first?"]
    release.set()
    results = await asyncio.gather(first, second)
    await hub.process_spoken_utterance("What is first?", utterance_id="one")
    assert [result["utterance_id"] for result in results] == ["one", "two"]
    assert spoken == [("one", "What is first?"), ("two", "What is second?")]
    await hub.close()


@pytest.mark.asyncio
async def test_voice_task_request_response_and_event_keep_exact_ingress_identity():
    tasks = FakeTasks()
    hub = hub_fixture(VoiceConversationBridge(tasks))
    hub._start_task_monitor = Mock()
    try:
        result = await hub.process_spoken_utterance(
            "Open Chrome", utterance_id="exact-id", speak_response=False
        )
        assert result["utterance_id"] == tasks.requests[0].request_id == "exact-id"
        accepted = [event for event in hub.event_sink.events if event.task_id == result["task_id"]]
        assert accepted and all(event.utterance_id == "exact-id" for event in accepted)
        await hub.process_spoken_utterance(
            "Open Chrome", utterance_id="exact-id", speak_response=False
        )
        assert len(tasks.requests) == 1
        assert len(hub.event_sink.events) == len(accepted)
    finally:
        await hub.close()


@pytest.mark.asyncio
async def test_live_late_turn_and_duplicate_final_cannot_borrow_new_input_authority():
    hub = hub_fixture()
    session = FakeLiveSession()
    hub._state = VoiceState.LISTENING
    hub.speech_synthesizer = object()
    hub.speak_text = AsyncMock()
    hub._handle_tool_call = AsyncMock()
    try:
        await hub._handle_live_event(
            session,
            LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text="Open Chrome", utterance_id="old"),
        )
        final = LiveEvent(type=LiveEventType.OUTPUT_TRANSCRIPT, text="Hello", utterance_id="old")
        await hub._handle_live_event(session, final)
        await hub._handle_live_event(session, final)
        hub.speak_text.assert_awaited_once()
        await hub._handle_live_event(
            session, LiveEvent(type=LiveEventType.TURN_COMPLETE, utterance_id="old")
        )
        await hub._handle_live_event(
            session,
            LiveEvent(type=LiveEventType.INPUT_TRANSCRIPT, text="Open Notepad", utterance_id="new"),
        )
        await hub._handle_live_event(
            session,
            LiveEvent(
                type=LiveEventType.TOOL_CALL,
                tool_call=LiveToolCall("late-call", "execute_task", {"text": "Open Notepad"}),
                utterance_id="old",
            ),
        )
        await hub._handle_live_event(
            session, LiveEvent(type=LiveEventType.TURN_COMPLETE, utterance_id="old")
        )
        assert hub._last_user_transcript == "Open Notepad"
        assert hub._turn_in_flight
        hub._handle_tool_call.assert_not_awaited()
        await hub._handle_live_event(
            session,
            LiveEvent(
                type=LiveEventType.TOOL_CALL,
                tool_call=LiveToolCall("new-call", "execute_task", {"text": "Open Notepad"}),
                utterance_id="new",
            ),
        )
        assert hub._handle_tool_call.call_args.args[1].utterance_id == "new"
    finally:
        await hub.close()


def owned_task(
    repo, *, task_id, parent=None, owner="alice", session="session", status=TaskStatus.QUEUED
):
    task = TaskRecord.new(
        "test",
        task_id=task_id,
        parent_task_id=parent,
        authorization=AuthorizationContext(owner, "request"),
        session_id=session,
    )
    task.status = status
    return repo.save(task)


@pytest.mark.asyncio
async def test_partial_watcher_emits_settlement_once_and_stops_without_claiming_success():
    engine = engine_fixture()
    task = owned_task(engine.tasks, task_id="partial", status=TaskStatus.PARTIALLY_COMPLETED)
    running = asyncio.create_task(asyncio.Event().wait())
    engine._active[task.task_id] = running
    adapter = TaskEngineVoiceAdapter(engine)
    bridge = VoiceConversationBridge(adapter)
    watcher = adapter.watch(task.task_id, principal_id="alice", poll_interval_seconds=0)
    try:
        first = await anext(watcher)
        assert not bridge._status_result(first)["settled"]
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        second = await anext(watcher)
        result = bridge._status_result(second)
        assert result["settled"] and not result["verified"]
        with pytest.raises(StopAsyncIteration):
            await anext(watcher)
        assert (
            await engine.cancel(task.task_id, principal_id="alice")
        ).status is TaskStatus.PARTIALLY_COMPLETED
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        engine._active.clear()


@pytest.mark.parametrize("final", ["completed", "cancelled", "unknown", "partially_completed"])
def test_settled_task_snapshot_cannot_be_overwritten_by_partial_or_older_version(final):
    hub = hub_fixture()
    assert hub._record_task_state("task", "running", version=4)
    assert not hub._record_task_state("task", "queued", version=3)
    assert hub._record_task_state("task", final, version=5, settled=True)
    assert not hub._record_task_state("task", "partially_completed", version=4)
    assert not hub._record_task_state("task", final, version=5, settled=True)
    assert hub._active_task_id is None


def test_pending_voice_results_are_kept_per_task_instead_of_overwriting_other_interactions():
    hub = hub_fixture()
    hub._queue_runtime_update("one", {"state": "running", "utterance_id": "first"})
    hub._queue_runtime_update("two", {"state": "completed", "utterance_id": "second"})
    hub._queue_runtime_update("one", {"state": "completed", "utterance_id": "first"})
    assert list(hub._pending_runtime_updates) == ["one", "two"]
    assert hub._pending_runtime_updates["one"]["utterance_id"] == "first"
    assert hub._pending_runtime_updates["two"]["utterance_id"] == "second"


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlite", [False, True])
async def test_paginated_descendants_preserve_completed_foreign_and_unrelated_tasks(sqlite):
    database = SQLiteDatabase(":memory:") if sqlite else None
    engine = engine_fixture()
    if database:
        engine.tasks = SQLiteTaskRepository(database)
    repo = engine.tasks
    owned_task(repo, task_id="root")
    # More than a page of siblings, plus grandchildren through a completed child.
    for number in range(205):
        owned_task(repo, task_id=f"child-{number:03}", parent="root")
    owned_task(repo, task_id="done", parent="root", status=TaskStatus.COMPLETED)
    owned_task(repo, task_id="grandchild", parent="done")
    owned_task(repo, task_id="great-grandchild", parent="grandchild")
    owned_task(repo, task_id="unrelated")
    owned_task(repo, task_id="foreign", parent="root", owner="bob")
    owned_task(repo, task_id="other-session", parent="root", session="other")
    # Old implementation used this capped history query; cancellation must not call it.
    repo.list_for_principal = Mock(side_effect=AssertionError("history horizon used"))
    try:
        await asyncio.gather(
            engine.cancel("root", principal_id="alice"),
            engine.cancel("child-000", principal_id="alice"),
        )
        for name in ("root", "child-000", "child-204", "grandchild", "great-grandchild"):
            assert repo.get(name).status is TaskStatus.CANCELLED
        assert repo.get("done").status is TaskStatus.COMPLETED
        for name in ("unrelated", "foreign", "other-session"):
            assert repo.get(name).status is TaskStatus.QUEUED
        with pytest.raises(PermissionError):
            await engine.cancel("foreign", principal_id="alice")
    finally:
        if database:
            database.close()


@pytest.mark.asyncio
async def test_cancellation_admission_barrier_and_cancelled_caller_cleanup():
    engine = engine_fixture()
    owned_task(engine.tasks, task_id="root", status=TaskStatus.RUNNING)
    owned_task(engine.tasks, task_id="child", parent="root")
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def active():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            raise

    worker = asyncio.create_task(active())
    engine._active["root"] = worker
    engine._workers = [SimpleNamespace()]  # avoid starting unrelated fixture workers on admission
    await entered.wait()
    cancel = asyncio.create_task(engine.cancel("root", principal_id="alice"))
    await cancelling.wait()
    try:
        with pytest.raises(ValueError, match="cancelling"):
            await engine.submit(
                UserRequest(text="Open Chrome", session_id="session"),
                principal_id="alice",
                parent_task_id="root",
            )
        cancel.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancel
        assert engine.tasks.get("child").status is TaskStatus.CANCELLED
        assert not engine._cancelling_tasks
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        engine._workers.clear()
        engine._active.clear()


@pytest.mark.asyncio
async def test_active_partial_parent_accepts_children_but_settled_partial_does_not():
    engine = engine_fixture()
    parent = owned_task(engine.tasks, task_id="parent", status=TaskStatus.PARTIALLY_COMPLETED)
    engine._workers = [SimpleNamespace()]
    engine._active[parent.task_id] = SimpleNamespace(done=lambda: False)
    try:
        child = await engine.submit(
            UserRequest(text="Open Chrome", session_id="session"),
            principal_id="alice",
            parent_task_id=parent.task_id,
        )
        assert child.parent_task_id == parent.task_id
        engine._active.clear()
        with pytest.raises(ValueError, match="settled"):
            await engine.submit(
                UserRequest(text="Open Chrome", session_id="session"),
                principal_id="alice",
                parent_task_id=parent.task_id,
            )
    finally:
        engine._workers.clear()
        engine._active.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial", ["missing", "mismatch", "replay", "disabled", "expired", "foreign"]
)
async def test_episodic_consent_precedes_persistence_and_embedding(denial):
    database = SQLiteDatabase(":memory:")
    embedding = SimpleNamespace(embed=AsyncMock(return_value=EmbeddingResult("fake", (1.0, 0.0))))
    repo = SQLiteMemoryRepository(database, embedding=embedding)
    task = TaskRecord.new(
        "Organize documents", authorization=AuthorizationContext("alice", "request")
    )
    task.status = TaskStatus.COMPLETED
    try:
        proposal = repo.propose_episodic_task_summary(task)
        assert not repo.list_records(principal_id="alice")
        embedding.embed.assert_not_awaited()
        reference = None
        if denial != "missing":
            grant = (
                replace(proposal, text="Different content") if denial == "mismatch" else proposal
            )
            if denial == "foreign":
                grant = replace(grant, principal_id="bob")
            options = (
                {"now": datetime.now(UTC) - timedelta(minutes=10)} if denial == "expired" else {}
            )
            reference, _ = await repo.issue_write_consent(grant, **options)
        if denial == "replay":
            record_id = await repo.record_episodic_task_summary(task, consent_reference=reference)
            assert (
                repo.get_record(principal_id="alice", record_id=record_id).source_task_id
                == task.task_id
            )
            embedding.embed.reset_mock()
        if denial == "disabled":
            repo.set_enabled(principal_id="alice", enabled=False)
        with pytest.raises((MemoryConsentError, MemoryDisabledError)):
            await repo.record_episodic_task_summary(task, consent_reference=reference)
        embedding.embed.assert_not_awaited()
        assert len(repo.list_records(principal_id="alice")) == (1 if denial == "replay" else 0)
    finally:
        database.close()


@pytest.mark.parametrize(
    "status", [TaskStatus.RUNNING, TaskStatus.PARTIALLY_COMPLETED, TaskStatus.QUEUED]
)
def test_episodic_proposal_refuses_unsettled_or_ambiguous_task(status):
    database = SQLiteDatabase(":memory:")
    try:
        repo = SQLiteMemoryRepository(database)
        task = TaskRecord.new("test", authorization=AuthorizationContext("alice", "request"))
        task.status = status
        with pytest.raises(ValueError, match="settled"):
            repo.propose_episodic_task_summary(task)
    finally:
        database.close()


def browser_fixture():
    row = {
        "role": "button",
        "name": "Save",
        "tag": "button",
        "id": "save",
        "test_id": "",
        "label": "",
        "placeholder": "",
        "text": "Save",
        "input_type": "",
        "sensitive": False,
        "contenteditable": False,
        "visible": True,
        "enabled": True,
        "checked": None,
        "hierarchy": [],
        "bounds": [0, 0, 50, 20],
    }
    page = FakePage([row])
    provider = PlaywrightBrowserProvider()
    provider._context = FakeContext(page)
    page_id = provider._register_page(page)
    provider._default_page_id = page_id
    return provider, page, page_id


@pytest.mark.asyncio
async def test_browser_dom_resolution_dispatch_and_fresh_verification_are_separate():
    provider, page, page_id = browser_fixture()
    try:
        result = await provider.resolve(page_id, TargetQuery(role="button", semantic_name="Save"))
        assert result.status is ResolutionStatus.RESOLVED
        candidate = (await provider.inspect(page_id))[0]
        action = ActionContract(
            risk=RiskLevel.R3,
            authority=AuthorizationContext("alice", "request"),
            task_id="task",
            tool_name="browser.click",
            target=candidate.descriptor.identity,
            postconditions=(Condition("browser.title", ConditionOperator.EQUALS, "Saved"),),
        )
        tool = PlaywrightActionTool(provider, "click")
        before = await provider.observe(action)
        async with ResourceManager().acquire_many(
            "task", tool.resources_for(action), lease_seconds=2
        ) as lease:
            outcome = await tool.execute(action, before, lease)
        assert (
            outcome.status is ExecutionStatus.SUCCEEDED and page.locator_instance.click_count == 1
        )
        assert (await provider.verify(action, outcome=outcome)).status is VerificationStatus.FAILED
        page.title = AsyncMock(return_value="Saved")
        assert (await provider.verify(action, outcome=outcome)).status is VerificationStatus.PASSED
        assert (
            await provider.verify(action, post_observation=before)
        ).status is VerificationStatus.UNKNOWN
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["empty", "foreign", "expired"])
async def test_browser_verifier_rejects_vacuous_unrelated_or_expired_evidence(case):
    provider, page, page_id = browser_fixture()
    try:
        target = provider.page_identity(page_id)
        action = ActionContract(
            risk=RiskLevel.R3,
            authority=AuthorizationContext("alice", "request"),
            task_id="task",
            tool_name="browser.navigate",
            target=target,
            postconditions=()
            if case == "empty"
            else (Condition("browser.title", ConditionOperator.EQUALS, "Sign in"),),
        )
        observation = await provider.observe(action)
        if case == "foreign":
            observation = replace(observation, target_fingerprint="wrong")
        elif case == "expired":
            observation = replace(observation, monotonic_deadline=0)
        assert (
            await provider.verify(action, post_observation=observation)
        ).status is VerificationStatus.UNKNOWN
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["click", "navigate"])
async def test_browser_dispatch_timeout_is_unknown_and_cancellation_propagates(operation):
    provider, page, page_id = browser_fixture()
    entered = asyncio.Event()

    async def dispatch(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    page.goto = dispatch
    page.locator_instance.click = dispatch
    candidate = (await provider.inspect(page_id))[0]

    async def run(timeout):
        if operation == "click":
            return await provider.click(candidate, timeout_seconds=timeout)
        return await provider.navigate(
            page_id, "https://example.test/next", timeout_seconds=timeout
        )

    try:
        with pytest.raises(ComputerAdapterError) as caught:
            await run(0.01)
        assert caught.value.code is ComputerFailureCode.ACTION_UNKNOWN_OUTCOME
        entered.clear()
        pending = asyncio.create_task(run(5))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        await provider.close()
    assert not provider.started and not provider._observations and not provider._pages


@pytest.mark.asyncio
async def test_browser_navigation_invalidates_previous_dom_lease():
    provider, page, page_id = browser_fixture()
    try:
        action = ActionContract(
            risk=RiskLevel.R3,
            authority=AuthorizationContext("alice", "request"),
            task_id="task",
            tool_name="browser.navigate",
            target=provider.page_identity(page_id),
        )
        observation = await provider.observe(action)

        async def goto(url, **kwargs):
            page.url = url

        page.goto = goto
        tab = await provider.navigate(
            page_id,
            "https://example.test/next",
            timeout_seconds=1,
            expected_observation=observation,
        )
        assert tab.url == "https://example.test/next"
        assert not await provider.is_current(observation)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_browser_startup_cancellation_closes_every_allocated_resource(monkeypatch):
    entered = asyncio.Event()

    async def new_page():
        entered.set()
        await asyncio.Event().wait()

    context = SimpleNamespace(route=AsyncMock(), on=Mock(), new_page=new_page, close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    sdk = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock()
    )
    monkeypatch.setitem(
        sys.modules,
        "playwright.async_api",
        SimpleNamespace(
            async_playwright=lambda: SimpleNamespace(start=AsyncMock(return_value=sdk))
        ),
    )
    provider = PlaywrightBrowserProvider()
    pending = asyncio.create_task(provider.start())
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    sdk.stop.assert_awaited_once()
    await provider.close()
    assert not provider.started
    assert context.close.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("settled", [TaskStatus.COMPLETED, TaskStatus.UNKNOWN])
async def test_cancellation_racing_worker_settlement_does_not_overwrite_evidence(settled):
    engine = engine_fixture()
    owned_task(engine.tasks, task_id="root", status=TaskStatus.RUNNING)
    entered = asyncio.Event()

    async def worker():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            record = engine.tasks.get("root")
            record.status = settled  # deterministic stand-in for runtime's persisted evidence
            engine.tasks.save(record)
            raise

    active = asyncio.create_task(worker())
    engine._active["root"] = active
    await entered.wait()
    result = await engine.cancel("root", principal_id="alice")
    assert result.status is settled
    assert active.cancelled()


@pytest.mark.asyncio
async def test_stream_settles_once_and_never_reads_late_partial_or_second_final():
    from test_independent_subsystems import StreamProvider, request

    from arise.core.model_gateway import ModelRouter
    from arise.core.models import ModelStreamChunk

    class Provider(StreamProvider):
        async def stream(self, req):
            try:
                yield ModelStreamChunk(
                    request_id=req.request_id,
                    provider_id=self.provider_id,
                    model_id="test-model",
                    sequence=0,
                    text_delta="done",
                    is_final=True,
                )
                raise AssertionError("router advanced past the settled final")
            finally:
                self.closed = True

    provider = Provider("local")
    router = ModelRouter()
    router.register(provider)
    chunks = [chunk async for chunk in router.stream(request())]
    assert len(chunks) == 1 and chunks[0].is_final
    assert provider.closed


@pytest.mark.asyncio
async def test_browser_concurrent_start_creates_only_one_context(monkeypatch):
    page = FakePage([])
    context = SimpleNamespace(
        route=AsyncMock(), on=Mock(), new_page=AsyncMock(return_value=page), close=AsyncMock()
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    sdk = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock()
    )
    factory = AsyncMock(return_value=sdk)
    monkeypatch.setitem(
        sys.modules,
        "playwright.async_api",
        SimpleNamespace(async_playwright=lambda: SimpleNamespace(start=factory)),
    )
    provider = PlaywrightBrowserProvider()
    try:
        await asyncio.gather(provider.start(), provider.start())
        factory.assert_awaited_once()
        browser.new_context.assert_awaited_once()
    finally:
        await asyncio.gather(provider.close(), provider.close())
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    sdk.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigation_rechecks_resource_lease_after_observation_await():
    from arise.core.resources import ResourceLeaseLost

    provider, page, page_id = browser_fixture()
    action = ActionContract(
        task_id="task",
        tool_name="browser.navigate",
        target=provider.page_identity(page_id),
        risk=RiskLevel.R2,
        authority=AuthorizationContext("alice", "request"),
        parameters={"url": "https://example.test/next"},
    )
    observation = await provider.observe(action)
    lease = SimpleNamespace(ensure_valid=AsyncMock(side_effect=[None, ResourceLeaseLost("lost")]))
    provider.navigate = AsyncMock()
    try:
        result = await PlaywrightActionTool(provider, "navigate").execute(
            action, observation, lease
        )
        assert result.status is ExecutionStatus.FAILED
        assert not result.side_effect_may_have_occurred
        provider.navigate.assert_not_awaited()
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_gemini_normalizer_keeps_identity_within_turn_and_rotates_for_repeated_input():
    from test_gemini_live import (
        Blob,
        FakeClient,
        FakeLiveEndpoint,
        FakeSdkSession,
        FunctionResponse,
    )

    from arise.adapters.gemini_live import GeminiLiveProvider
    from arise.adapters.secrets import MemorySecretProvider
    from arise.core.voice import LiveSessionConfig

    packet = SimpleNamespace(
        server_content=SimpleNamespace(
            input_transcription=SimpleNamespace(text="Open Chrome"), turn_complete=True
        )
    )
    sdk = FakeSdkSession([packet, packet])
    provider = GeminiLiveProvider(
        secret_provider=MemorySecretProvider({"GEMINI_API_KEY": "fake"}),
        enabled=True,
        allow_cloud=True,
        security_allows_cloud=True,
        client_factory=lambda **_: FakeClient(FakeLiveEndpoint([sdk])),
        blob_factory=Blob,
        function_response_factory=FunctionResponse,
    )
    try:
        session = await provider.connect(LiveSessionConfig("session"))
        events = [event async for event in session.receive()]
        assert events[0].utterance_id == events[1].utterance_id
        assert events[2].utterance_id == events[3].utterance_id
        assert events[0].utterance_id != events[2].utterance_id
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_runtime_response_preserves_original_interaction_when_another_task_is_active():
    hub = hub_fixture()
    session = FakeLiveSession()
    hub._session = session
    hub._state = VoiceState.LISTENING
    hub._active_task_id = "another-task"
    update = {
        "task_id": "finished-task",
        "utterance_id": "original-input",
        "state": "completed",
        "summary": "ARISE reports completion after its task verifier passed.",
        "verified": True,
    }
    try:
        await hub._send_runtime_update(session, "finished-task", update)
        assert "utterance_id=original-input" in session.sent_text[0]
        event = hub.event_sink.events[-1]
        assert event.task_id == "finished-task"
        assert event.utterance_id == "original-input"
    finally:
        await hub.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["ambiguous", "disabled", "hidden"])
async def test_browser_refuses_nonunique_or_unavailable_dom_target_before_dispatch(invalid):
    provider, page, page_id = browser_fixture()
    candidate = (await provider.inspect(page_id))[0]
    if invalid == "ambiguous":
        page.locator_instance.count = AsyncMock(return_value=2)
    elif invalid == "disabled":
        page.locator_instance.is_enabled = AsyncMock(return_value=False)
    else:
        page.locator_instance.is_visible = AsyncMock(return_value=False)
    try:
        with pytest.raises(ComputerAdapterError):
            await provider.click(candidate, timeout_seconds=1)
        assert page.locator_instance.click_count == 0
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_browser_cancelled_dispatch_releases_runtime_resource_lease():
    provider, page, page_id = browser_fixture()
    entered = asyncio.Event()

    async def click(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    page.locator_instance.click = click
    target = (await provider.inspect(page_id))[0].descriptor.identity
    action = ActionContract(
        task_id="one",
        tool_name="browser.click",
        target=target,
        risk=RiskLevel.R3,
        authority=AuthorizationContext("alice", "request"),
    )
    tool = PlaywrightActionTool(provider, "click")
    observation = await provider.observe(action)
    resources = ResourceManager()

    async def dispatch():
        async with resources.acquire_many(
            "one", tool.resources_for(action), lease_seconds=2
        ) as lease:
            return await tool.execute(action, observation, lease)

    pending = asyncio.create_task(dispatch())
    try:
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with asyncio.timeout(1):
            async with resources.acquire_many(
                "two", tool.resources_for(action), lease_seconds=2
            ) as lease:
                await lease.ensure_valid()
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_worker_does_not_dispatch_queued_descendant_during_cancel_barrier():
    engine = engine_fixture()
    owned_task(engine.tasks, task_id="child")
    engine._cancelling_tasks.add("child")
    engine._process = AsyncMock()
    engine._queue.put_nowait(("child", UserRequest(text="Open Chrome")))
    engine._queue.put_nowait(None)
    await engine._worker(0)
    await engine._queue.join()
    engine._process.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_transcript_and_duplicate_settlement_do_not_emit_or_drain_again():
    hub = hub_fixture()
    session = FakeLiveSession()
    hub._state = VoiceState.LISTENING
    hub._send_runtime_update = AsyncMock()
    try:
        await hub._handle_live_event(
            session,
            LiveEvent(
                type=LiveEventType.INPUT_TRANSCRIPT,
                text="Open Chrome",
                utterance_id="turn",
                is_final=True,
            ),
        )
        await hub._handle_live_event(
            session,
            LiveEvent(
                type=LiveEventType.INPUT_TRANSCRIPT,
                text="Open Notepad",
                utterance_id="turn",
                is_final=False,
            ),
        )
        assert hub._last_user_transcript == "Open Chrome"
        hub._queue_runtime_update("one", {"state": "completed"})
        hub._queue_runtime_update("two", {"state": "completed"})
        end = LiveEvent(type=LiveEventType.TURN_COMPLETE, utterance_id="turn")
        await hub._handle_live_event(session, end)
        await hub._handle_live_event(session, end)
        hub._send_runtime_update.assert_awaited_once()
        assert list(hub._pending_runtime_updates) == ["two"]
    finally:
        await hub.close()
