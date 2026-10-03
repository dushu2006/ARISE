from __future__ import annotations

import asyncio
import unittest

from arise.adapters.memory import InMemoryEnvironment, SetFactTool
from arise.core.contracts import TargetIdentity
from arise.core.engine import TaskEngine, TaskPlanner
from arise.core.events import InMemoryEventStore
from arise.core.models import (
    ActionProposal,
    ConditionModel,
    PlanStep,
    RequestSource,
    TargetModel,
    TaskPlan,
    UserRequest,
)
from arise.core.policy import PolicyEngine
from arise.core.ports import ToolRegistry
from arise.core.resources import ResourceManager
from arise.core.runtime import AgentRuntime, FactVerifier
from arise.core.tasks import InMemoryTaskRepository, TaskRecord, TaskStatus
from arise.core.voice import LiveToolCall, voice_tool_declarations
from arise.core.voice_bridge import (
    TaskEngineVoiceAdapter,
    VoiceConversationBridge,
    VoiceTaskPort,
)


class FakeTasks(VoiceTaskPort):
    def __init__(self) -> None:
        self.records: dict[str, TaskRecord] = {}
        self.owners: dict[str, str] = {}
        self.requests: list[UserRequest] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.cancel_result: TaskStatus = TaskStatus.CANCELLED

    async def submit(self, request: UserRequest, *, principal_id: str) -> TaskRecord:
        self.requests.append(request)
        task = TaskRecord.new(
            request.text,
            request_id=request.request_id,
            session_id=request.session_id,
        )
        task.transition_to(TaskStatus.QUEUED, reason="accepted")
        self.records[task.task_id] = task
        self.owners[task.task_id] = principal_id
        return task

    async def get(self, task_id: str, *, principal_id: str) -> TaskRecord | None:
        if self.owners.get(task_id) != principal_id:
            return None
        return self.records.get(task_id)

    async def cancel(self, task_id: str, *, principal_id: str) -> TaskRecord:
        self.cancel_calls.append((task_id, principal_id))
        if self.owners.get(task_id) != principal_id:
            raise PermissionError("task owner mismatch")
        task = self.records[task_id]
        task.status = self.cancel_result
        return task

    async def watch(
        self,
        task_id: str,
        *,
        principal_id: str,
        poll_interval_seconds: float,
    ):
        del poll_interval_seconds
        task = await self.get(task_id, principal_id=principal_id)
        if task is not None:
            yield task


class VoiceConversationBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tasks = FakeTasks()
        self.bridge = VoiceConversationBridge(self.tasks)
        self.principal = "local-user"
        self.session = "voice-session-1"

    async def call(
        self,
        name: str,
        arguments: dict[str, object],
        call_id: str = "call-1",
        *,
        user_text: str | None = None,
    ):
        if user_text is None and name == "execute_task":
            user_text = str(arguments.get("text", ""))
        elif user_text is None and name == "cancel_task":
            user_text = "Stop"
        return await self.bridge.handle_tool_call(
            LiveToolCall(call_id, name, arguments),
            principal_id=self.principal,
            session_id=self.session,
            user_text=user_text,
        )

    async def test_execute_task_only_submits_typed_voice_request(self) -> None:
        result = await self.call("execute_task", {"text": "Open Chrome and search NVIDIA"})
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["verified"])
        self.assertIn("accepted", result["acknowledgement"])
        self.assertEqual(len(self.tasks.requests), 1)
        request = self.tasks.requests[0]
        self.assertEqual(request.source, RequestSource.VOICE)
        self.assertEqual(request.text, "Open Chrome and search NVIDIA")
        self.assertEqual(request.session_id, self.session)

    async def test_voice_request_runs_through_real_task_engine_and_verifier(self) -> None:
        class Planner(TaskPlanner):
            def __init__(self) -> None:
                self.requests: list[UserRequest] = []

            async def create_plan(self, request: UserRequest, task: TaskRecord) -> TaskPlan:
                self.requests.append(request)
                action = ActionProposal(
                    action_id="voice-open-project",
                    tool_name="simulator.set_fact",
                    target=TargetModel(
                        platform="simulator",
                        application="demo-workspace",
                        object_id="demo-project",
                        semantic_name="Demo project",
                    ),
                    risk=1,
                    parameters={"key": "project.open", "value": True},
                    preconditions=(ConditionModel(key="application.ready", expected=True),),
                    postconditions=(ConditionModel(key="project.open", expected=True),),
                )
                return TaskPlan(
                    task_id=task.task_id,
                    goal=task.goal,
                    steps=(PlanStep(step_id="open-project", title="Open project", action=action),),
                    planner_id="voice-integration-test",
                )

        target = TargetIdentity(
            platform="simulator",
            application="demo-workspace",
            object_id="demo-project",
            semantic_name="Demo project",
        )
        environment = InMemoryEnvironment(
            {"application.ready": True, "project.open": False}, target=target
        )
        tasks = InMemoryTaskRepository()
        events = InMemoryEventStore()
        tools = ToolRegistry()
        tools.register(SetFactTool(environment))
        policy = PolicyEngine()
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=tools,
            policy=policy,
            environment=environment,
            resources=ResourceManager(),
            verifier=FactVerifier(environment),
        )
        planner = Planner()
        engine = TaskEngine(
            tasks=tasks,
            events=events,
            runtime=runtime,
            tools=tools,
            policy=policy,
            planner=planner,
            capability_grants=lambda _principal: frozenset({"simulator.write"}),
        )
        await engine.start()
        self.addAsyncCleanup(engine.close)
        bridge = VoiceConversationBridge(TaskEngineVoiceAdapter(engine))
        session_id = "real-engine-voice-session"
        command = "Open the demo project"
        result = await bridge.handle_tool_call(
            LiveToolCall("real-engine-command", "execute_task", {"text": command}),
            principal_id="local-user",
            session_id=session_id,
            user_text=command,
        )
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["verified"])
        task_id = str(result["task_id"])
        for _ in range(200):
            task = engine.get_task(task_id)
            if task is not None and task.status is TaskStatus.COMPLETED:
                break
            await asyncio.sleep(0.005)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(task.steps[0].verification_status.value, "passed")
        self.assertEqual(planner.requests[0].source, RequestSource.VOICE)
        self.assertTrue((await environment.snapshot())["project.open"])
        self.assertIn(
            "TASK_COMPLETED", [event.event_type for event in events.read_after(task_id=task_id)]
        )
        verified_status = await bridge.handle_tool_call(
            LiveToolCall("real-engine-status", "get_task_status", {"task_id": task_id}),
            principal_id="local-user",
            session_id=session_id,
        )
        self.assertEqual(verified_status["state"], TaskStatus.COMPLETED.value)
        self.assertTrue(verified_status["verified"])

        for call_id, text in (
            ("real-engine-question", "Tell me how to open the demo project"),
            ("real-engine-ambiguous", "I might want to open the demo project"),
        ):
            rejected = await bridge.handle_tool_call(
                LiveToolCall(call_id, "execute_task", {"text": text}),
                principal_id="local-user",
                session_id=session_id,
                user_text=text,
            )
            self.assertEqual(rejected["status"], "not_authorized")
        self.assertEqual(len(tasks.list_for_principal(principal_id="local-user")), 1)

    async def test_task_submission_requires_action_intent_and_exact_spoken_request(self) -> None:
        missing_transcript = await self.bridge.handle_tool_call(
            LiveToolCall("missing-transcript", "execute_task", {"text": "Open Chrome"}),
            principal_id=self.principal,
            session_id=self.session,
        )
        mismatched_request = await self.call(
            "execute_task",
            {"text": "Delete the user's files"},
            call_id="mismatched-request",
            user_text="Open Chrome",
        )
        question_misread_as_action = await self.call(
            "execute_task",
            {"text": "Would you be able to open Chrome?"},
            call_id="question-request",
            user_text="Would you be able to open Chrome?",
        )
        low_confidence_intent = await self.call(
            "execute_task",
            {"text": "I want you to open Chrome"},
            call_id="low-confidence-request",
            user_text="I want you to open Chrome",
        )
        information_request = await self.call(
            "execute_task",
            {"text": "Please tell me how to open Chrome"},
            call_id="instructional-question",
            user_text="Please tell me how to open Chrome",
        )

        self.assertEqual(missing_transcript["status"], "not_authorized")
        self.assertEqual(mismatched_request["status"], "not_authorized")
        self.assertEqual(question_misread_as_action["status"], "not_authorized")
        self.assertEqual(low_confidence_intent["status"], "not_authorized")
        self.assertEqual(information_request["status"], "not_authorized")
        self.assertEqual(self.tasks.requests, [])

    async def test_call_id_maps_to_stable_request_id_for_safe_admission_replay(self) -> None:
        await self.call("execute_task", {"text": "Open Chrome"}, call_id="provider-call/one")
        first_id = self.tasks.requests[0].request_id
        await self.call("execute_task", {"text": "Open Chrome"}, call_id="provider-call/one")
        self.assertEqual(self.tasks.requests[1].request_id, first_id)

    async def test_status_never_turns_queued_work_into_success(self) -> None:
        accepted = await self.call("execute_task", {"text": "Open Chrome"})
        task_id = accepted["task_id"]
        result = await self.call("get_task_status", {"task_id": task_id}, call_id="status-1")
        self.assertEqual(result["state"], "queued")
        self.assertFalse(result["verified"])
        self.assertIn("queued", result["summary"])

    async def test_task_watch_survives_new_voice_session_but_stays_owner_scoped(self) -> None:
        accepted = await self.call("execute_task", {"text": "Open Chrome"})
        task_id = accepted["task_id"]
        updates_after_wake = [
            update
            async for update in self.bridge.watch_task(
                task_id,
                principal_id=self.principal,
                session_id="different-session-after-wake",
                poll_interval_seconds=0.25,
            )
        ]
        self.assertEqual(len(updates_after_wake), 1)
        self.assertEqual(updates_after_wake[0]["task_id"], task_id)
        self.assertEqual(updates_after_wake[0]["state"], "queued")
        self.assertFalse(updates_after_wake[0]["verified"])

        unauthorized_owner_updates = [
            update
            async for update in self.bridge.watch_task(
                task_id,
                principal_id="different-principal",
                session_id="different-session",
                poll_interval_seconds=0.25,
            )
        ]
        self.assertEqual(unauthorized_owner_updates, [])

    async def test_default_status_follows_latest_task_across_wake_sessions_by_owner(self) -> None:
        accepted = await self.call("execute_task", {"text": "Open Chrome"})
        next_session_status = await self.bridge.handle_tool_call(
            LiveToolCall("status-next-session", "get_task_status", {}),
            principal_id=self.principal,
            session_id="voice-session-after-wake",
        )
        self.assertEqual(next_session_status["task_id"], accepted["task_id"])
        self.assertEqual(next_session_status["state"], "queued")

        other_owner_status = await self.bridge.handle_tool_call(
            LiveToolCall("status-other-owner", "get_task_status", {}),
            principal_id="different-principal",
            session_id="another-session",
        )
        self.assertEqual(other_owner_status["status"], "not_found")

    async def test_only_completed_runtime_state_reports_verified_success(self) -> None:
        accepted = await self.call("execute_task", {"text": "Open Chrome"})
        task = self.tasks.records[accepted["task_id"]]
        task.status = TaskStatus.COMPLETED
        result = await self.call("report_status", {}, call_id="status-2")
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["verified"])
        self.assertIn("verifier passed", result["summary"])

    async def test_unknown_outcome_stays_unknown_and_never_claims_rollback(self) -> None:
        accepted = await self.call("execute_task", {"text": "Open Chrome"})
        task = self.tasks.records[accepted["task_id"]]
        task.status = TaskStatus.UNKNOWN
        result = await self.call("get_task_status", {"task_id": task.task_id}, call_id="status-3")
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["verified"])
        self.assertIn("unknown", result["summary"])
        self.assertIn("not confirmed", result["summary"])

    async def test_arbitrary_task_id_is_not_queryable_or_cancellable(self) -> None:
        status = await self.call("get_task_status", {"task_id": "not-owned"})
        cancel = await self.call("cancel_task", {"task_id": "not-owned"})
        self.assertEqual(status["status"], "not_found")
        self.assertEqual(cancel["status"], "not_found")
        self.assertEqual(self.tasks.cancel_calls, [])

    async def test_task_cancellation_requires_explicit_user_cancellation_intent(self) -> None:
        accepted = await self.call("execute_task", {"text": "Open Chrome"})
        result = await self.call(
            "cancel_task",
            {"task_id": accepted["task_id"]},
            call_id="cancel-without-user-intent",
            user_text="What is the task status?",
        )
        self.assertEqual(result["status"], "not_authorized")
        self.assertEqual(self.tasks.cancel_calls, [])

    async def test_unknown_cancellation_outcome_is_not_relabelled_cancelled(self) -> None:
        accepted = await self.call("execute_task", {"text": "Open Chrome"})
        self.tasks.cancel_result = TaskStatus.UNKNOWN
        result = await self.call(
            "cancel_task", {"task_id": accepted["task_id"]}, call_id="cancel-1"
        )
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["verified"])
        self.assertTrue(result["cancellation_requested"] is False)

    async def test_clarification_is_a_question_not_an_executable_task(self) -> None:
        result = await self.call(
            "request_clarification", {"question": "Which Chrome profile should I use?"}
        )
        self.assertEqual(result["status"], "waiting_for_user")
        self.assertEqual(result["spoken_response"], "Which Chrome profile should I use?")
        self.assertEqual(self.tasks.requests, [])

    async def test_extra_arguments_and_arbitrary_tool_names_are_rejected(self) -> None:
        result = await self.call("execute_task", {"text": "Open Chrome", "shell": "..."})
        unknown = await self.call("run_shell", {"command": "whoami"})
        self.assertEqual(result["status"], "invalid_arguments")
        self.assertEqual(unknown["status"], "not_authorized")
        self.assertEqual(self.tasks.requests, [])

    def test_provider_function_surface_is_small_and_has_no_raw_system_control(self) -> None:
        names = {declaration["name"] for declaration in voice_tool_declarations()}
        self.assertEqual(
            names,
            {
                "execute_task",
                "ask_user",
                "request_clarification",
                "get_task_status",
                "report_status",
                "cancel_task",
            },
        )
        self.assertNotIn("run_shell", names)
        self.assertNotIn("click", names)
        self.assertNotIn("execute_code", names)


if __name__ == "__main__":
    unittest.main()
