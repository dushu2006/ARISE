"""Coherence-milestone tests: environment questions, event-driven waits, task
lifecycle wait states, and streaming-voice segment delivery.

These tests are platform-independent: the Windows adapters are replaced by fakes
that implement the same ports. They prove the *logic* (routing, waiting,
ordering, fail-closed reporting); real Windows behaviour is still validated
separately on a Windows host (see docs/coherence-milestone-audit.md, section I).
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

from arise.adapters.environment_wait import EnvironmentWaitTool
from arise.config.settings import AppSettings, DatabaseSettings, SecuritySettings
from arise.core.computer import InstalledApplication, RunningApplication, WindowRecord
from arise.core.contracts import ActionContract, AuthorizationContext, RiskLevel, TrustLevel
from arise.core.environment_questions import EnvironmentQuestionKind, EnvironmentQuestionService
from arise.core.extensions import AudioChunk
from arise.core.ports import ExecutionStatus
from arise.core.tasks import WAITING_STATUSES, TaskRecord, TaskStatus
from arise.core.voice import AudioHub, LiveEvent, LiveEventType, VoiceState
from arise.core.waits import WaitCoordinator, WaitOutcome
from arise.server import create_app


def _audio(sequence: int = 0, data: bytes = b"\x01\x02") -> AudioChunk:
    return AudioChunk(
        sequence=sequence, codec="pcm16", sample_rate_hz=16_000, channels=1, data=data
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeApplications:
    running: list[RunningApplication] = field(default_factory=list)
    installed: list[InstalledApplication] = field(default_factory=list)
    failure: Exception | None = None

    async def running_applications(self) -> list[RunningApplication]:
        if self.failure is not None:
            raise self.failure
        return list(self.running)

    async def installed_applications(self, *, limit: int = 512) -> list[InstalledApplication]:
        if self.failure is not None:
            raise self.failure
        return list(self.installed)[:limit]


@dataclass(slots=True)
class FakeWindows:
    foreground: WindowRecord | None = None
    failure: Exception | None = None

    async def foreground_window(self) -> WindowRecord | None:
        if self.failure is not None:
            raise self.failure
        return self.foreground


def _window(title: str = "Untitled", *, foreground: bool = True) -> WindowRecord:
    return WindowRecord(
        window_id="window-1",
        process_id=4321,
        title=title,
        application="excel.exe",
        visible=True,
        minimized=False,
        maximized=False,
        foreground=foreground,
    )


def _action(
    task_id: str = "task-1",
    parameters: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = 30.0,
) -> ActionContract:
    return ActionContract(
        task_id=task_id,
        tool_name="system.wait_for_condition",
        target=None,
        risk=RiskLevel.R0,
        authority=AuthorizationContext(
            principal_id="principal-1",
            user_intent_id="intent-1",
            trust=TrustLevel.USER_INSTRUCTION,
            capabilities=frozenset({"desktop.ui_automation", "desktop.launch"}),
        ),
        parameters=dict(parameters or {}),
        idempotency_key="idem-1",
        timeout_seconds=timeout_seconds,
    )


class _NullLease:
    """Stand-in for an always-valid resource lease."""

    async def ensure_valid(self) -> None:
        return None


class _FakeSubscription:
    def __init__(self, queue: asyncio.Queue[Any]) -> None:
        self.queue = queue


class _FakeBroker:
    def __init__(self) -> None:
        self.queues: list[asyncio.Queue[Any]] = []
        self.unsubscribed = 0

    def subscribe(self, *, task_id: str | None = None, max_queue_size: int = 128) -> Any:
        del task_id
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_queue_size)
        self.queues.append(queue)
        return _FakeSubscription(queue)

    def unsubscribe(self, subscription: Any) -> None:
        del subscription
        self.unsubscribed += 1

    def publish(self, payload: Any = "event") -> None:
        for queue in self.queues:
            if not queue.full():
                queue.put_nowait(payload)


class _FactObserver:
    def __init__(self, facts: dict[str, Any] | None = None) -> None:
        self.facts = dict(facts or {})
        self.calls = 0

    async def __call__(self, action: ActionContract) -> dict[str, Any]:
        del action
        self.calls += 1
        return dict(self.facts)


class _Sink:
    def __init__(self) -> None:
        self.begins: list[tuple[str, TaskStatus, str]] = []
        self.ends: list[str] = []

    def begin_wait(self, task_id: str, status: TaskStatus, *, waiting_reason: str) -> None:
        self.begins.append((task_id, status, waiting_reason))

    def end_wait(self, task_id: str) -> None:
        self.ends.append(task_id)


class FakePlayback:
    def __init__(self) -> None:
        self.played: list[AudioChunk] = []
        self.stops = 0

    async def play(self, chunk: AudioChunk) -> None:
        self.played.append(chunk)

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 1. Deterministic environment questions (no model call, fail-closed)
# ---------------------------------------------------------------------------


class EnvironmentQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_question_is_not_treated_as_an_environment_question(self) -> None:
        service = EnvironmentQuestionService()
        for question in (
            "What is the capital of France?",
            "Explain how TCP handshakes work.",
            "Write a poem about the sea.",
        ):
            with self.subTest(question=question):
                self.assertIsNone(await service.answer(question))
                self.assertIs(service.classify(question)[0], EnvironmentQuestionKind.NONE)

    async def test_commands_are_never_mistaken_for_environment_questions(self) -> None:
        service = EnvironmentQuestionService(
            applications=FakeApplications([RunningApplication(100, "chrome.exe")])
        )
        for command in ("open Chrome", "please start Notepad for me", "close Spotify"):
            with self.subTest(command=command):
                self.assertIsNone(await service.answer(command))

    async def test_is_x_running_is_answered_from_observed_processes(self) -> None:
        applications = FakeApplications(
            [
                RunningApplication(100, "chrome.exe", "C:\\chrome.exe"),
                RunningApplication(200, "notepad.exe"),
            ]
        )
        service = EnvironmentQuestionService(applications=applications)
        answer = await service.answer("Is Chrome running?")
        assert answer is not None
        self.assertIs(answer.kind, EnvironmentQuestionKind.APPLICATION_RUNNING)
        self.assertTrue(answer.available)
        self.assertIn("chrome", answer.answer.lower())
        self.assertTrue(answer.facts["running"])
        self.assertIn("chrome", str(answer.subject))

        missing = await service.answer("Is Spotify running?")
        assert missing is not None
        self.assertTrue(missing.available)
        self.assertFalse(missing.facts["running"])
        self.assertIn("not", missing.answer.lower())

    async def test_process_names_without_paths_match_the_asked_name(self) -> None:
        # Windows process names carry ".exe"; a user asking about "Chrome" must
        # still match the "chrome.exe" process even when no path is observed.
        applications = FakeApplications([RunningApplication(100, "chrome.exe")])
        service = EnvironmentQuestionService(applications=applications)
        answer = await service.answer("Is Chrome running?")
        assert answer is not None
        self.assertTrue(answer.available)
        self.assertTrue(answer.facts["running"])

        other = FakeApplications([RunningApplication(7, "msedge.exe")])
        answer = await EnvironmentQuestionService(applications=other).answer("Is Chrome running?")
        assert answer is not None
        self.assertFalse(answer.facts["running"])

    async def test_which_applications_are_running_lists_observed_processes(self) -> None:
        applications = FakeApplications(
            [
                RunningApplication(100, "chrome.exe"),
                RunningApplication(200, "notepad.exe"),
                RunningApplication(300, "chrome.exe"),
            ]
        )
        service = EnvironmentQuestionService(applications=applications)
        answer = await service.answer("What applications are currently running?")
        assert answer is not None
        self.assertIs(answer.kind, EnvironmentQuestionKind.RUNNING_APPLICATIONS)
        self.assertTrue(answer.available)
        self.assertEqual(answer.facts["count"], 2)
        self.assertIn("chrome", answer.answer.lower())

    async def test_foreground_window_question_uses_the_window_adapter(self) -> None:
        service = EnvironmentQuestionService(windows=FakeWindows(_window(title="Report - Excel")))
        answer = await service.answer("Which window is currently in focus?")
        assert answer is not None
        self.assertIs(answer.kind, EnvironmentQuestionKind.FOREGROUND_WINDOW)
        self.assertTrue(answer.available)
        self.assertIn("Report", answer.answer)

    async def test_installed_question_reads_the_catalog_not_a_model(self) -> None:
        applications = FakeApplications(
            installed=[InstalledApplication("pkg-1", "Visual Studio Code", "package", "package")]
        )
        service = EnvironmentQuestionService(applications=applications)
        answer = await service.answer("Do I have Visual Studio Code installed?")
        assert answer is not None
        self.assertIs(answer.kind, EnvironmentQuestionKind.APPLICATION_INSTALLED)
        self.assertTrue(answer.available)

    async def test_unavailable_adapters_are_reported_and_never_guessed(self) -> None:
        service = EnvironmentQuestionService(
            applications=FakeApplications(failure=RuntimeError("adapter unavailable")),
            windows=FakeWindows(failure=RuntimeError("adapter unavailable")),
        )
        for question in (
            "Is Chrome running?",
            "What applications are currently running?",
            "Which window is in focus?",
        ):
            with self.subTest(question=question):
                answer = await service.answer(question)
                assert answer is not None
                self.assertFalse(answer.available)
                self.assertIsNotNone(answer.unavailable_reason)
                self.assertIn("will not guess", answer.answer)

    async def test_no_adapters_configured_means_no_environment_claims(self) -> None:
        service = EnvironmentQuestionService()
        self.assertFalse(service.can_inspect)
        answer = await service.answer("Is Chrome running?")
        assert answer is not None
        self.assertFalse(answer.available)


class EnvironmentQuestionRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Environment questions must be answered from adapters, never by the model."""

    def _app(self, service: EnvironmentQuestionService) -> Any:
        self._temp = TemporaryDirectory()
        root = Path(self._temp.name)
        app = create_app(
            AppSettings(
                data_dir=root,
                database=DatabaseSettings(path=root / "server.sqlite3"),
                security=SecuritySettings(environment="test", require_api_auth=False),
            )
        )
        app.state.services.environment_questions = service
        self._client_context = TestClient(app)
        return self._client_context.__enter__()

    def tearDown(self) -> None:
        context = getattr(self, "_client_context", None)
        if context is not None:
            context.__exit__(None, None, None)
        temp = getattr(self, "_temp", None)
        if temp is not None:
            temp.cleanup()

    async def test_environment_question_is_answered_without_the_model(self) -> None:
        class ExplodingModel:
            """Any model call would be a defect for this request."""

            async def complete(self, *args: Any, **kwargs: Any) -> Any:
                raise AssertionError("the model must not answer environment questions")

        service = EnvironmentQuestionService(
            applications=FakeApplications([RunningApplication(100, "chrome.exe")])
        )
        client = self._app(service)
        client.app.state.services.router.providers = [ExplodingModel()]
        response = client.post(
            "/api/v1/interactions",
            json={"text": "Is Chrome running?", "session_id": "session-1"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["outcome"], "answer")
        self.assertIn("chrome", payload["answer"].lower())

    async def test_commands_still_become_tasks(self) -> None:
        service = EnvironmentQuestionService(
            applications=FakeApplications([RunningApplication(100, "chrome.exe")])
        )
        client = self._app(service)
        response = client.post(
            "/api/v1/interactions",
            json={"text": "Please summarize this page", "session_id": "session-1"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        # Not answered from the environment: it either becomes a task or is
        # answered by the configured model/router, never as an environment fact.
        self.assertNotEqual(payload["outcome"], "answer")


# ---------------------------------------------------------------------------
# 2. Event-driven waiting with adaptive polling fallback
# ---------------------------------------------------------------------------


class WaitCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_condition_resolves_without_any_polling(self) -> None:
        coordinator = WaitCoordinator()
        result = await coordinator.wait_until(lambda: True, timeout_seconds=5.0)
        self.assertIs(result.outcome, WaitOutcome.RESOLVED)
        self.assertEqual(result.poll_wakeups, 0)
        self.assertEqual(result.attempts, 1)

    async def test_event_wakes_the_wait_before_the_poll_interval(self) -> None:
        broker = _FakeBroker()
        coordinator = WaitCoordinator(broker=broker)
        state = {"ready": False}

        async def _predicate() -> bool:
            return bool(state["ready"])

        async def _publish_soon() -> None:
            await asyncio.sleep(0.05)
            state["ready"] = True
            broker.publish("window-opened")

        waiter = asyncio.ensure_future(
            coordinator.wait_until(
                _predicate, timeout_seconds=5.0, min_interval_seconds=30.0, task_id="task-1"
            )
        )
        publisher = asyncio.ensure_future(_publish_soon())
        result = await waiter
        await publisher
        self.assertIs(result.outcome, WaitOutcome.RESOLVED)
        self.assertGreaterEqual(result.event_wakeups, 1)
        self.assertEqual(result.poll_wakeups, 0)
        self.assertEqual(broker.unsubscribed, 1)

    async def test_polling_backs_off_instead_of_hammering_the_environment(self) -> None:
        sleeper_calls: list[float] = []
        clock = {"now": 0.0}

        async def _sleep(seconds: float) -> None:
            sleeper_calls.append(seconds)
            clock["now"] += seconds

        coordinator = WaitCoordinator(clock=lambda: clock["now"], sleeper=_sleep)
        result = await coordinator.wait_until(
            lambda: False,
            timeout_seconds=2.0,
            min_interval_seconds=0.25,
            max_interval_seconds=1.0,
            backoff_factor=2.0,
        )
        self.assertIs(result.outcome, WaitOutcome.TIMEOUT)
        self.assertGreater(len(sleeper_calls), 1)
        self.assertEqual(sleeper_calls[0], 0.25)
        # The interval grows instead of repeating the same minimum poll forever.
        self.assertGreater(sleeper_calls[1], sleeper_calls[0])
        self.assertLessEqual(max(sleeper_calls), 1.0)
        # Bounded work: it never polls at the minimum interval for the whole wait.
        self.assertLess(len(sleeper_calls), 2.0 / 0.25)

    async def test_cancellation_returns_a_cancelled_outcome(self) -> None:
        coordinator = WaitCoordinator()
        waiter = asyncio.ensure_future(
            coordinator.wait_until(lambda: False, timeout_seconds=30.0, min_interval_seconds=5.0)
        )
        await asyncio.sleep(0)
        waiter.cancel()
        result = await waiter
        self.assertIs(result.outcome, WaitOutcome.CANCELLED)
        self.assertFalse(result.resolved)

    async def test_repeated_predicate_failures_are_reported_not_assumed(self) -> None:
        def _broken() -> bool:
            raise RuntimeError("observation failed")

        coordinator = WaitCoordinator()
        result = await coordinator.wait_until(
            _broken, timeout_seconds=5.0, min_interval_seconds=0.001
        )
        self.assertIs(result.outcome, WaitOutcome.PREDICATE_UNAVAILABLE)
        self.assertFalse(result.resolved)


# ---------------------------------------------------------------------------
# 3. The wait tool: bounded, side-effect free, and task-status aware
# ---------------------------------------------------------------------------


class EnvironmentWaitToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_resolves_when_the_fact_becomes_true(self) -> None:
        observer = _FactObserver({"download.complete": False})
        tool = EnvironmentWaitTool((observer,), WaitCoordinator())
        sink = _Sink()
        tool.set_status_sink(sink)
        action = _action(
            parameters={
                "condition_key": "download.complete",
                "operator": "equals",
                "expected": True,
                "timeout_seconds": 5.0,
                "poll_interval_seconds": 0.01,
                "wait_target": "external",
            }
        )

        async def _flip() -> None:
            await asyncio.sleep(0.05)
            observer.facts["download.complete"] = True

        waiter = asyncio.ensure_future(tool.execute(action, None, _NullLease()))
        flipper = asyncio.ensure_future(_flip())
        outcome = await waiter
        await flipper
        self.assertIs(outcome.status, ExecutionStatus.SUCCEEDED)
        self.assertFalse(outcome.side_effect_may_have_occurred)
        self.assertIs(sink.begins[0][1], TaskStatus.WAITING_FOR_EXTERNAL_RESULT)
        self.assertEqual(sink.begins[0][2], "download.complete equals")
        self.assertEqual(sink.ends, ["task-1"])
        assert tool.last_result is not None
        self.assertTrue(tool.last_result.resolved)

    async def test_timeout_fails_without_claiming_completion(self) -> None:
        observer = _FactObserver({})
        tool = EnvironmentWaitTool((observer,), WaitCoordinator())
        action = _action(
            parameters={
                "condition_key": "result.ready",
                "operator": "exists",
                "expected": None,
                "timeout_seconds": 1.0,
                "poll_interval_seconds": 0.25,
            }
        )
        outcome = await tool.execute(action, None, _NullLease())
        self.assertIs(outcome.status, ExecutionStatus.FAILED)
        self.assertIn("No completion is claimed", outcome.summary)
        self.assertFalse(outcome.side_effect_may_have_occurred)

    async def test_wait_target_selects_the_matching_task_waiting_state(self) -> None:
        for target, expected in (
            ("application", TaskStatus.WAITING_FOR_APPLICATION),
            ("browser", TaskStatus.WAITING_FOR_BROWSER),
            ("verification", TaskStatus.WAITING_FOR_VERIFICATION),
        ):
            with self.subTest(target=target):
                observer = _FactObserver({"state.ready": False})
                tool = EnvironmentWaitTool((observer,), WaitCoordinator())
                sink = _Sink()
                tool.set_status_sink(sink)
                action = _action(
                    parameters={
                        "condition_key": "state.ready",
                        "operator": "equals",
                        "expected": True,
                        "timeout_seconds": 1.0,
                        "poll_interval_seconds": 0.25,
                        "wait_target": target,
                    }
                )
                await tool.execute(action, None, _NullLease())
                self.assertIs(sink.begins[0][1], expected)

    async def test_parameters_are_validated_before_dispatch(self) -> None:
        tool = EnvironmentWaitTool((_FactObserver(),), WaitCoordinator())
        for parameters in (
            {},
            {"condition_key": "a.b", "operator": "not_an_operator", "expected": 1},
            {"condition_key": "a.b", "operator": "equals"},
            {
                "condition_key": "a.b",
                "operator": "equals",
                "expected": 1,
                "timeout_seconds": 10_000,
            },
            {"condition_key": "a.b", "operator": "equals", "expected": 1, "wait_target": "moon"},
        ):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ValueError):
                    tool.validate_parameters(parameters)

    async def test_wait_never_exceeds_the_action_timeout(self) -> None:
        observer = _FactObserver({"never": False})
        tool = EnvironmentWaitTool((observer,), WaitCoordinator())
        action = _action(
            parameters={
                "condition_key": "never",
                "operator": "equals",
                "expected": True,
                "timeout_seconds": 900.0,
                "poll_interval_seconds": 0.25,
            },
            timeout_seconds=1.0,
        )
        outcome = await tool.execute(action, None, _NullLease())
        self.assertIs(outcome.status, ExecutionStatus.FAILED)
        assert tool.last_result is not None
        self.assertLessEqual(tool.last_result.elapsed_seconds, 1.5)

    async def test_broken_observer_is_never_reported_as_success(self) -> None:
        def _broken(action: ActionContract) -> Any:
            del action
            raise RuntimeError("adapter down")

        tool = EnvironmentWaitTool((_broken,), WaitCoordinator())
        action = _action(
            parameters={
                "condition_key": "anything",
                "operator": "equals",
                "expected": True,
                "timeout_seconds": 2.0,
                "poll_interval_seconds": 0.01,
            }
        )
        outcome = await tool.execute(action, None, _NullLease())
        self.assertIsNot(outcome.status, ExecutionStatus.SUCCEEDED)
        self.assertFalse(outcome.side_effect_may_have_occurred)


# ---------------------------------------------------------------------------
# 4. Task lifecycle wait states
# ---------------------------------------------------------------------------


class TaskWaitingStateTests(unittest.TestCase):
    def test_waiting_states_are_distinct_and_named(self) -> None:
        self.assertTrue(
            {
                TaskStatus.WAITING_FOR_APPLICATION,
                TaskStatus.WAITING_FOR_BROWSER,
                TaskStatus.WAITING_FOR_EXTERNAL_RESULT,
                TaskStatus.WAITING_FOR_VERIFICATION,
            }
            <= WAITING_STATUSES
        )
        self.assertIn(TaskStatus.RECEIVED, set(TaskStatus))
        self.assertIn(TaskStatus.RESUMING, set(TaskStatus))

    def test_begin_and_end_wait_record_the_completion_source(self) -> None:
        task = TaskRecord.new(
            task_id="task-9", session_id="session-1", request_id="req-1", goal="wait"
        )
        task.transition_to(TaskStatus.UNDERSTANDING)
        task.transition_to(TaskStatus.PLANNING)
        task.transition_to(TaskStatus.READY)
        task.transition_to(TaskStatus.RUNNING)
        task.begin_wait(TaskStatus.WAITING_FOR_EXTERNAL_RESULT, waiting_reason="code.result ready")
        self.assertIs(task.status, TaskStatus.WAITING_FOR_EXTERNAL_RESULT)
        self.assertEqual(task.waiting_target, "code.result ready")
        self.assertEqual(task.resume_count, 0)
        task.end_wait()
        self.assertEqual(task.resume_count, 1)
        task.transition_to(TaskStatus.RUNNING, reason="resumed")
        self.assertIsNone(task.waiting_reason)
        self.assertIsNone(task.waiting_target)

    def test_waiting_reason_survives_persistence(self) -> None:
        task = TaskRecord.new(task_id="task-10", session_id="s", request_id="r", goal="wait")
        task.transition_to(TaskStatus.UNDERSTANDING)
        task.transition_to(TaskStatus.PLANNING)
        task.transition_to(TaskStatus.READY)
        task.transition_to(TaskStatus.RUNNING)
        task.begin_wait(TaskStatus.WAITING_FOR_BROWSER, waiting_reason="page.loaded equals")
        restored = TaskRecord.from_dict(task.to_dict())
        self.assertIs(restored.status, TaskStatus.WAITING_FOR_BROWSER)
        self.assertEqual(restored.waiting_reason, "page.loaded equals")
        self.assertEqual(restored.resume_count, task.resume_count)

    def test_non_waiting_states_cannot_claim_a_wait_target(self) -> None:
        task = TaskRecord.new(task_id="task-11", session_id="s", request_id="r", goal="x")
        with self.assertRaises(RuntimeError):
            task.begin_wait(TaskStatus.RUNNING, waiting_reason="nope")

    def test_received_is_a_valid_entry_state(self) -> None:
        task = TaskRecord.new(task_id="task-12", session_id="s", request_id="r", goal="x")
        task.transition_to(TaskStatus.RECEIVED, reason="request received")
        task.transition_to(TaskStatus.QUEUED, reason="queued")
        self.assertIs(task.status, TaskStatus.QUEUED)


# ---------------------------------------------------------------------------
# 5. Voice: every streamed segment is delivered, in order
# ---------------------------------------------------------------------------


class VoiceStreamingTests(unittest.IsolatedAsyncioTestCase):
    def _hub(self, synthesizer: Any, playback: Any) -> AudioHub:
        return AudioHub(
            microphone=None,
            vad=None,
            wake_word_detector=None,
            provider=None,
            playback=playback,
            speech_synthesizer=synthesizer,
        )

    async def test_every_segment_of_a_multi_segment_reply_is_spoken(self) -> None:
        spoken: list[str] = []

        class Synthesizer:
            async def synthesize(
                self, text: str, *, locale: str | None = None, correlation_id: str
            ):
                del locale, correlation_id
                spoken.append(text)
                yield _audio(sequence=len(spoken), data=f"audio-{len(spoken)}".encode())

        hub = self._hub(Synthesizer(), FakePlayback())
        await hub.start()
        try:
            # A text-only provider must speak *each* segment, not only the first.
            for segment in ("First part.", "Second part.", "Third part."):
                await hub._apply_live_event(
                    None,
                    LiveEvent(
                        type=LiveEventType.OUTPUT_TRANSCRIPT,
                        text=segment,
                        is_final=True,
                        is_audio_transcript=False,
                    ),
                )
            self.assertEqual(spoken, ["First part.", "Second part.", "Third part."])
        finally:
            await hub.close()

    async def test_provider_audio_is_not_spoken_a_second_time(self) -> None:
        spoken: list[str] = []

        class Synthesizer:
            async def synthesize(
                self, text: str, *, locale: str | None = None, correlation_id: str
            ):
                del locale, correlation_id
                spoken.append(text)
                yield _audio(sequence=1)

        playback = FakePlayback()
        hub = self._hub(Synthesizer(), playback)
        await hub.start()
        hub._state = VoiceState.LISTENING  # provider output requires an active session
        try:
            await hub._apply_live_event(
                None,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=_audio(1, b"provider-audio"),
                    text="Hello there.",
                ),
            )
            await hub._apply_live_event(
                None,
                LiveEvent(
                    type=LiveEventType.OUTPUT_TRANSCRIPT,
                    text="Hello there.",
                    is_final=True,
                    is_audio_transcript=False,
                ),
            )
            self.assertEqual(spoken, [])
            self.assertEqual([chunk.data for chunk in playback.played], [b"provider-audio"])
        finally:
            await hub.close()

    async def test_output_playback_is_serialized(self) -> None:
        events: list[str] = []

        class SlowSynthesizer:
            def __init__(self) -> None:
                self.calls = 0

            async def synthesize(
                self, text: str, *, locale: str | None = None, correlation_id: str
            ):
                del locale, correlation_id
                self.calls += 1
                events.append(f"start:{text}")
                yield _audio(1)
                await asyncio.sleep(0.02)
                yield _audio(2)
                events.append(f"end:{text}")

        class SlowPlayback:
            def __init__(self) -> None:
                self.played: list[bytes] = []

            async def play(self, chunk: AudioChunk) -> None:
                await asyncio.sleep(0.01)
                self.played.append(chunk.data)

            async def stop(self) -> None:
                return None

            async def close(self) -> None:
                return None

        synthesizer = SlowSynthesizer()
        hub = self._hub(synthesizer, SlowPlayback())
        await hub.start()
        try:
            await asyncio.gather(
                hub.speak_text("acknowledgement"),
                hub.speak_text("response body"),
            )
            # No interleaving: one output finishes before the next one starts,
            # so a response can never jump over an acknowledgement (or vice versa).
            self.assertEqual(
                events,
                [
                    "start:acknowledgement",
                    "end:acknowledgement",
                    "start:response body",
                    "end:response body",
                ],
            )
            self.assertEqual(synthesizer.calls, 2)
        finally:
            await hub.close()


if __name__ == "__main__":
    unittest.main()
