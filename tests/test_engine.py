from __future__ import annotations

import asyncio
import unittest

from arise.adapters.memory import InMemoryEnvironment, SetFactTool
from arise.core.contracts import AuthorizationContext, TargetIdentity, TrustLevel
from arise.core.engine import (
    TaskEngine,
    TaskEngineConfig,
    TaskInputNotAccepted,
    TaskPlanner,
    UnavailablePlanner,
)
from arise.core.events import InMemoryEventStore
from arise.core.models import (
    ActionProposal,
    ConditionModel,
    PlanStep,
    TargetModel,
    TaskPlan,
    UserRequest,
)
from arise.core.policy import PolicyEngine
from arise.core.ports import ToolRegistry
from arise.core.resources import ResourceManager
from arise.core.runtime import AgentRuntime, FactVerifier
from arise.core.tasks import (
    DuplicateTaskRequestError,
    InMemoryTaskRepository,
    TaskRecord,
    TaskStatus,
)


class StaticPlanner(TaskPlanner):
    def __init__(self, *, clarification_first: bool = False, risk: int = 1) -> None:
        self.clarification_first = clarification_first
        self.risk = risk
        self.calls = 0
        self.requests: list[UserRequest] = []

    async def create_plan(self, request: UserRequest, task: TaskRecord) -> TaskPlan:
        self.calls += 1
        self.requests.append(request)
        if self.clarification_first and self.calls == 1:
            return TaskPlan(
                task_id=task.task_id,
                goal=task.goal,
                steps=(),
                planner_id="test-planner",
                needs_clarification=True,
                clarification_question="Which project should be opened?",
            )
        action = ActionProposal(
            action_id=f"open-{self.calls}",
            tool_name="simulator.set_fact",
            target=TargetModel(
                platform="simulator",
                application="demo-workspace",
                object_id="demo-project",
                semantic_name="Demo project",
            ),
            risk=self.risk,
            parameters={"key": "project.open", "value": True},
            preconditions=(ConditionModel(key="application.ready", expected=True),),
            postconditions=(ConditionModel(key="project.open", expected=True),),
        )
        return TaskPlan(
            task_id=task.task_id,
            goal=task.goal,
            steps=(PlanStep(step_id=f"step-{self.calls}", title="Open project", action=action),),
            planner_id="test-planner",
        )


class TaskEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        target = TargetIdentity(
            platform="simulator",
            application="demo-workspace",
            object_id="demo-project",
            semantic_name="Demo project",
        )
        self.environment = InMemoryEnvironment(
            {"application.ready": True, "project.open": False},
            target=target,
        )
        self.tasks = InMemoryTaskRepository()
        self.events = InMemoryEventStore()
        self.tools = ToolRegistry()
        self.tools.register(SetFactTool(self.environment))
        self.policy = PolicyEngine()
        self.runtime = AgentRuntime(
            tasks=self.tasks,
            events=self.events,
            tools=self.tools,
            policy=self.policy,
            environment=self.environment,
            resources=ResourceManager(),
            verifier=FactVerifier(self.environment),
        )
        self.planner = StaticPlanner()
        self.engine = TaskEngine(
            tasks=self.tasks,
            events=self.events,
            runtime=self.runtime,
            tools=self.tools,
            policy=self.policy,
            planner=self.planner,
            capability_grants=lambda _: frozenset({"simulator.write"}),
        )
        await self.engine.start()
        self.addAsyncCleanup(self.engine.close)

    async def wait_for_status(self, task_id: str, *statuses: TaskStatus) -> TaskRecord:
        for _ in range(200):
            task = self.tasks.get(task_id)
            if task is not None and task.status in statuses:
                return task
            await asyncio.sleep(0.005)
        self.fail(f"task {task_id} did not reach {[status.value for status in statuses]}")

    async def test_typed_plan_executes_and_verifies_through_runtime(self) -> None:
        request = UserRequest(text="Open the demo project")
        accepted = await self.engine.submit(request, principal_id="test-user")
        completed = await self.wait_for_status(accepted.task_id, TaskStatus.COMPLETED)
        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        self.assertEqual(completed.steps[0].status.value, "succeeded")
        self.assertTrue((await self.environment.snapshot())["project.open"])
        events = self.events.read_after(task_id=accepted.task_id)
        self.assertTrue(events)
        self.assertTrue(all(event.correlation_id == request.request_id for event in events))
        self.assertTrue(all(event.causation_id == request.request_id for event in events))
        self.assertIn("TASK_COMPLETED", [event.event_type for event in events])

    async def test_child_task_lineage_is_persisted_and_scoped_to_live_parent(self) -> None:
        authority = AuthorizationContext(
            principal_id="test-user",
            user_intent_id="parent-request",
            trust=TrustLevel.USER_INSTRUCTION,
        )
        parent = TaskRecord.new(
            "Parent workflow",
            authorization=authority,
            request_id="parent-request",
            session_id="parent-session",
        )
        parent.transition_to(TaskStatus.QUEUED)
        parent.transition_to(TaskStatus.UNDERSTANDING)
        parent.transition_to(TaskStatus.WAITING_USER)
        self.tasks.save(parent)

        child_request = UserRequest(
            request_id="child-request",
            session_id=parent.session_id,
            text="Open the demo project",
        )
        accepted = await self.engine.submit(
            child_request,
            principal_id="test-user",
            session_id=parent.session_id,
            parent_task_id=parent.task_id,
        )
        child = await self.wait_for_status(accepted.task_id, TaskStatus.COMPLETED)
        self.assertEqual(child.parent_task_id, parent.task_id)
        self.assertEqual(TaskRecord.from_dict(child.to_dict()).parent_task_id, parent.task_id)
        replay = await self.engine.submit(
            child_request,
            principal_id="test-user",
            session_id=parent.session_id,
            parent_task_id=parent.task_id,
        )
        self.assertEqual(replay.task_id, child.task_id)

        with self.assertRaises(ValueError):
            await self.engine.submit(
                UserRequest(
                    request_id="child-wrong-session",
                    session_id="another-session",
                    text="Open the demo project",
                ),
                principal_id="test-user",
                session_id="another-session",
                parent_task_id=parent.task_id,
            )

    async def test_unavailable_planner_never_reports_success(self) -> None:
        engine = TaskEngine(
            tasks=self.tasks,
            events=self.events,
            runtime=self.runtime,
            tools=self.tools,
            policy=self.policy,
            planner=UnavailablePlanner(),
        )
        await engine.start()
        self.addAsyncCleanup(engine.close)
        accepted = await engine.submit(UserRequest(text="Do something"), principal_id="test-user")
        result = await self.wait_for_status(accepted.task_id, TaskStatus.REQUIRES_USER_INPUT)
        self.assertIn("No planning model", result.status_reason)
        self.assertFalse(result.steps)
        self.assertNotIn(
            "TASK_COMPLETED",
            [event.event_type for event in self.events.read_after(task_id=accepted.task_id)],
        )
        with self.assertRaises(TaskInputNotAccepted):
            await engine.provide_input(accepted.task_id, "more detail", principal_id="test-user")

    async def test_clarification_is_explicit_then_replanned(self) -> None:
        self.planner.clarification_first = True
        accepted = await self.engine.submit(
            UserRequest(text="Open the right project"), principal_id="test-user"
        )
        waiting = await self.wait_for_status(accepted.task_id, TaskStatus.REQUIRES_USER_INPUT)
        self.assertIn("clarification", waiting.status_reason)
        queued = await self.engine.provide_input(
            accepted.task_id,
            "Open the demo project with api_key=sk-123456789012345678901234",
            principal_id="test-user",
        )
        self.assertIn(
            queued.status, {TaskStatus.QUEUED, TaskStatus.UNDERSTANDING, TaskStatus.PLANNING}
        )
        completed = await self.wait_for_status(accepted.task_id, TaskStatus.COMPLETED)
        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        self.assertEqual(self.planner.calls, 2)
        self.assertNotIn("sk-123456789012345678901234", self.planner.requests[1].text)

    async def test_restart_discards_pending_approval_and_accepts_a_fresh_instruction(self) -> None:
        tasks = InMemoryTaskRepository()
        events = InMemoryEventStore()
        tools = ToolRegistry()
        tools.register(SetFactTool(self.environment))
        policy = PolicyEngine()
        authority = AuthorizationContext(
            principal_id="test-user",
            user_intent_id="restart-request",
            trust=TrustLevel.USER_INSTRUCTION,
            capabilities=frozenset({"simulator.write"}),
        )
        task = TaskRecord.new(
            "Persisted approval task",
            authorization=authority,
            request_id="restart-request",
            session_id="restart-session",
        )
        for status in (
            TaskStatus.QUEUED,
            TaskStatus.UNDERSTANDING,
            TaskStatus.PLANNING,
            TaskStatus.READY,
            TaskStatus.RUNNING,
            TaskStatus.WAITING_USER,
        ):
            task.transition_to(status)
        tasks.save(task)
        dispatched = TaskRecord.new(
            "Persisted dispatched task",
            authorization=authority,
            request_id="dispatched-request",
            session_id="restart-session",
        )
        for status in (
            TaskStatus.QUEUED,
            TaskStatus.UNDERSTANDING,
            TaskStatus.PLANNING,
            TaskStatus.READY,
            TaskStatus.RUNNING,
        ):
            dispatched.transition_to(status)
        tasks.save(dispatched)
        planner = StaticPlanner()
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=tools,
            policy=policy,
            environment=self.environment,
            resources=ResourceManager(),
            verifier=FactVerifier(self.environment),
        )
        engine = TaskEngine(
            tasks=tasks,
            events=events,
            runtime=runtime,
            tools=tools,
            policy=policy,
            planner=planner,
            capability_grants=lambda _: frozenset({"simulator.write"}),
        )
        await engine.start()
        self.addAsyncCleanup(engine.close)

        recovered = tasks.get(task.task_id)
        self.assertEqual(recovered.status, TaskStatus.REQUIRES_USER_INPUT)
        self.assertTrue(engine.can_accept_input(task.task_id))
        self.assertEqual(tasks.get(dispatched.task_id).status, TaskStatus.INTERRUPTED)
        self.assertFalse(engine.can_accept_input(dispatched.task_id))
        queued = await engine.provide_input(
            task.task_id,
            "Please continue after checking the project",
            principal_id="test-user",
        )
        self.assertNotEqual(queued.request_id, "restart-request")
        for _ in range(200):
            latest = tasks.get(task.task_id)
            if latest.status is TaskStatus.COMPLETED:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(latest.status, TaskStatus.COMPLETED)
        self.assertEqual(planner.calls, 1)
        self.assertIn("Please continue after checking the project", planner.requests[0].text)

    async def test_high_risk_approval_is_displayed_exactly_and_consumed_once(self) -> None:
        self.planner.risk = 3
        accepted = await self.engine.submit(
            UserRequest(text="Set the project open flag"), principal_id="test-user"
        )
        waiting = await self.wait_for_status(accepted.task_id, TaskStatus.WAITING_USER)
        self.assertEqual(waiting.status, TaskStatus.WAITING_USER)
        confirmations = self.engine.pending_confirmations(accepted.task_id)
        self.assertEqual(len(confirmations), 1)
        confirmation = confirmations[0]
        self.assertIn("simulator.set_fact", confirmation.action_summary)
        self.assertIn("project.open", confirmation.action_summary)
        with self.assertRaises(PermissionError):
            await self.engine.approve(
                task_id=accepted.task_id,
                confirmation_id=confirmation.confirmation_id,
                approved_by="other-user",
            )
        accepted_approval = await self.engine.approve(
            task_id=accepted.task_id,
            confirmation_id=confirmation.confirmation_id,
            approved_by="test-user",
        )
        self.assertEqual(accepted_approval.status, TaskStatus.WAITING_USER)
        completed = await self.wait_for_status(accepted.task_id, TaskStatus.COMPLETED)
        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        with self.assertRaises(PermissionError):
            await self.engine.approve(
                task_id=accepted.task_id,
                confirmation_id=confirmation.confirmation_id,
                approved_by="test-user",
            )

    async def test_request_secrets_are_redacted_before_planner_input(self) -> None:
        request = UserRequest(
            text="Use api_key=sk-123456789012345678901234 to open the demo project"
        )
        accepted = await self.engine.submit(request, principal_id="test-user")
        completed = await self.wait_for_status(accepted.task_id, TaskStatus.COMPLETED)
        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        self.assertNotIn("sk-123456789012345678901234", completed.goal)
        self.assertNotIn("sk-123456789012345678901234", self.planner.requests[-1].text)
        self.assertIn("api_key=[REDACTED]", self.planner.requests[-1].text)

    async def test_event_append_failure_does_not_strand_durable_queued_request(self) -> None:
        class FailOnceEventStore(InMemoryEventStore):
            def __init__(self) -> None:
                super().__init__()
                self.failed = False

            def append(self, event):
                if event.event_type == "TASK_ACCEPTED" and not self.failed:
                    self.failed = True
                    raise OSError("injected transient event-store failure")
                return super().append(event)

        tasks = InMemoryTaskRepository()
        events = FailOnceEventStore()
        tools = ToolRegistry()
        tools.register(SetFactTool(self.environment))
        policy = PolicyEngine()
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=tools,
            policy=policy,
            environment=self.environment,
            resources=ResourceManager(),
            verifier=FactVerifier(self.environment),
        )
        engine = TaskEngine(
            tasks=tasks,
            events=events,
            runtime=runtime,
            tools=tools,
            policy=policy,
            planner=StaticPlanner(),
            capability_grants=lambda _: frozenset({"simulator.write"}),
        )
        await engine.start()
        self.addAsyncCleanup(engine.close)
        request = UserRequest(
            request_id="event-retry-id",
            session_id="event-retry-session",
            text="Open the demo project",
        )
        with self.assertRaisesRegex(OSError, "injected transient"):
            await engine.submit(request, principal_id="test-user")
        stranded = tasks.get_by_request_id(
            principal_id="test-user",
            session_id="event-retry-session",
            request_id="event-retry-id",
        )
        self.assertEqual(stranded.status, TaskStatus.QUEUED)
        self.assertEqual(engine.queued_count, 0)

        replay = await engine.submit(request, principal_id="test-user")
        self.assertEqual(replay.task_id, stranded.task_id)
        completed = None
        for _ in range(200):
            completed = tasks.get(replay.task_id)
            if completed.status is TaskStatus.COMPLETED:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(completed.status, TaskStatus.COMPLETED)
        accepted = [
            event
            for event in events.read_after(task_id=replay.task_id)
            if event.event_type == "TASK_ACCEPTED"
        ]
        self.assertEqual(len(accepted), 1)

    async def test_duplicate_request_id_returns_existing_task_without_requeue(self) -> None:
        request = UserRequest(
            request_id="request-retry-1",
            session_id="session-retry-1",
            text="Open the demo project",
        )
        first = await self.engine.submit(request, principal_id="test-user")
        replay = await self.engine.submit(request, principal_id="test-user")
        self.assertEqual(first.task_id, replay.task_id)
        changed_request = request.model_copy(update={"text": "Close another project"})
        with self.assertRaises(DuplicateTaskRequestError):
            await self.engine.submit(changed_request, principal_id="test-user")
        await self.wait_for_status(first.task_id, TaskStatus.COMPLETED)
        accepted_events = [
            event
            for event in self.events.read_after(task_id=first.task_id)
            if event.event_type == "TASK_ACCEPTED"
        ]
        self.assertEqual(len(accepted_events), 1)
        self.assertEqual(len(self.tasks.list_recent(limit=100)), 1)

    async def test_approved_continuations_share_the_bounded_worker_pool(self) -> None:
        class BlockingSetFactTool(SetFactTool):
            def __init__(self, environment: InMemoryEnvironment) -> None:
                super().__init__(environment)
                self.release = asyncio.Event()
                self.started = asyncio.Queue()
                self.active = 0
                self.max_active = 0

            async def execute(self, action, observation, resources):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await self.started.put(action.action_id)
                try:
                    await self.release.wait()
                    return await super().execute(action, observation, resources)
                finally:
                    self.active -= 1

        environment = InMemoryEnvironment(
            {"application.ready": True, "project.open": False},
            target=self.environment.target,
        )
        tasks = InMemoryTaskRepository()
        events = InMemoryEventStore()
        tools = ToolRegistry()
        tool = BlockingSetFactTool(environment)
        tools.register(tool)
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
        engine = TaskEngine(
            tasks=tasks,
            events=events,
            runtime=runtime,
            tools=tools,
            policy=policy,
            planner=StaticPlanner(risk=3),
            config=TaskEngineConfig(max_concurrent_tasks=1, max_queued_tasks=4),
            capability_grants=lambda _: frozenset({"simulator.write"}),
        )
        await engine.start()
        self.addAsyncCleanup(engine.close)

        async def wait_for_local_status(task_id: str, expected: TaskStatus) -> TaskRecord:
            for _ in range(200):
                record = tasks.get(task_id)
                if record is not None and record.status is expected:
                    return record
                await asyncio.sleep(0.005)
            self.fail(f"task {task_id} did not reach {expected.value}")

        first = await engine.submit(UserRequest(text="First approval"), principal_id="test-user")
        await wait_for_local_status(first.task_id, TaskStatus.WAITING_USER)
        second = await engine.submit(UserRequest(text="Second approval"), principal_id="test-user")
        await wait_for_local_status(second.task_id, TaskStatus.WAITING_USER)
        first_confirmation = engine.pending_confirmations(first.task_id)[0]
        second_confirmation = engine.pending_confirmations(second.task_id)[0]

        await engine.approve(
            task_id=first.task_id,
            confirmation_id=first_confirmation.confirmation_id,
            approved_by="test-user",
        )
        await asyncio.wait_for(tool.started.get(), timeout=1)
        self.assertEqual(engine.active_count, 1)
        await engine.approve(
            task_id=second.task_id,
            confirmation_id=second_confirmation.confirmation_id,
            approved_by="test-user",
        )
        self.assertEqual(engine.active_count, 1)
        self.assertEqual(engine.queued_count, 1)

        tool.release.set()
        await wait_for_local_status(first.task_id, TaskStatus.COMPLETED)
        await wait_for_local_status(second.task_id, TaskStatus.COMPLETED)
        self.assertEqual(tool.max_active, 1)
        for _ in range(200):
            if engine.active_count == 0:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(engine.active_count, 0)

    async def test_plan_cycle_is_rejected_without_dispatch(self) -> None:
        class CyclicPlanner:
            async def create_plan(inner_self, request: UserRequest, task: TaskRecord) -> TaskPlan:
                del request
                first = PlanStep(
                    step_id="step-a",
                    title="A",
                    depends_on=("step-b",),
                    action=ActionProposal(tool_name="simulator.set_fact", risk=1),
                )
                second = PlanStep(
                    step_id="step-b",
                    title="B",
                    depends_on=("step-a",),
                    action=ActionProposal(tool_name="simulator.set_fact", risk=1),
                )
                return TaskPlan(
                    task_id=task.task_id,
                    goal=task.goal,
                    steps=(first, second),
                    planner_id="cycle-test",
                )

        engine = TaskEngine(
            tasks=self.tasks,
            events=self.events,
            runtime=self.runtime,
            tools=self.tools,
            policy=self.policy,
            planner=CyclicPlanner(),
        )
        await engine.start()
        self.addAsyncCleanup(engine.close)
        accepted = await engine.submit(UserRequest(text="Cycle"), principal_id="test-user")
        failed = await self.wait_for_status(accepted.task_id, TaskStatus.FAILED)
        self.assertIn("InvalidPlan", failed.status_reason)
        self.assertFalse(failed.steps)
