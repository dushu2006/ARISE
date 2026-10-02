from __future__ import annotations

import asyncio
import unittest
import uuid
from datetime import UTC, datetime

from arise.adapters.computer_simulator import (
    SimulatedComputerEnvironment,
    SimulatedComputerTool,
    SimulatorFailure,
)
from arise.core.computer import (
    ComputerFailureCode,
    CoordinateSpace,
    PerceptionSource,
    Rect,
    SelectorQuality,
    TargetCandidate,
    TargetDescriptor,
)
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    Condition,
    Idempotency,
    RiskLevel,
    TargetIdentity,
    TrustLevel,
)
from arise.core.events import InMemoryEventStore
from arise.core.policy import PolicyEngine
from arise.core.ports import ToolRegistry
from arise.core.resources import ResourceManager
from arise.core.runtime import ActionReplayError, AgentRuntime, FactVerifier
from arise.core.tasks import InMemoryTaskRepository, TaskRecord, TaskStatus


def make_target(*, visible: bool = True, enabled: bool = True) -> TargetCandidate:
    identity = TargetIdentity(
        platform="simulator",
        application="sample-app",
        process_id=200,
        window_id="window-1",
        object_id="save-button",
        stable_id="save-button",
        role="button",
        semantic_name="Save",
    )
    descriptor = TargetDescriptor(
        identity=identity,
        source=PerceptionSource.UI_AUTOMATION,
        observed_at=datetime.now(UTC),
        observation_id=str(uuid.uuid4()),
        bounds=Rect(20, 30, 100, 32),
        coordinate_space=CoordinateSpace.PHYSICAL_DESKTOP,
        selector_quality=SelectorQuality.EXACT_ACCESSIBLE_ROLE_NAME,
        visible=visible,
        enabled=enabled,
        automation_id="save-button",
        runtime_id=(1, 2, 3),
    )
    return TargetCandidate(descriptor, 0.99, ("automation id", "control type", "name"))


class ComputerSimulatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_moved_or_disappeared_target_invalidates_previous_observation(self) -> None:
        target = make_target()
        environment = SimulatedComputerEnvironment((target,))
        action = ActionContract(
            task_id="task-1",
            action_id="action-1",
            tool_name="simulator.computer_action",
            target=target.descriptor.identity,
            risk=RiskLevel.R1,
            authority=AuthorizationContext(
                principal_id="user-1",
                user_intent_id="request-1",
                trust=TrustLevel.USER_INSTRUCTION,
            ),
        )
        observation = await environment.observe(action)
        await environment.move_target(
            target.descriptor.identity.fingerprint, Rect(200, 180, 100, 32)
        )
        self.assertFalse(await environment.is_current(observation))
        current = await environment.observe(action)
        await environment.remove_target(target.descriptor.identity.fingerprint)
        self.assertFalse(await environment.is_current(current))

    async def test_target_disappearance_is_a_typed_pre_dispatch_failure(self) -> None:
        target = make_target()
        environment = SimulatedComputerEnvironment((target,))
        tool = SimulatedComputerTool(environment, failure=SimulatorFailure.TARGET_DISAPPEARS)
        action = ActionContract(
            task_id="task-1",
            action_id="action-1",
            tool_name=tool.spec.name,
            target=target.descriptor.identity,
            risk=RiskLevel.R1,
            authority=AuthorizationContext(
                principal_id="user-1",
                user_intent_id="request-1",
                trust=TrustLevel.USER_INSTRUCTION,
                capabilities=frozenset({"simulator.computer"}),
            ),
            parameters={"operation": "click"},
        )
        observation = await environment.observe(action)
        resources = ResourceManager()
        async with resources.acquire_many(
            action.task_id,
            tool.spec.required_resources,
            lease_seconds=2,
        ) as lease:
            with self.assertRaises(ComputerAdapterError) as caught:
                await tool.execute(action, observation, lease)
        self.assertIs(caught.exception.code, ComputerFailureCode.TARGET_STALE)
        self.assertEqual(tool.dispatched_action_ids, set())

    async def test_move_target_preserves_visibility_and_enabled_state(self) -> None:
        target = make_target(visible=False, enabled=False)
        environment = SimulatedComputerEnvironment((target,))
        await environment.move_target(
            target.descriptor.identity.fingerprint, Rect(200, 180, 100, 32)
        )
        moved, _facts = await environment._snapshot()
        self.assertEqual(len(moved), 1)
        self.assertFalse(moved[0].descriptor.visible)
        self.assertFalse(moved[0].descriptor.enabled)

    async def test_unknown_after_dispatch_is_not_retried_by_agent_runtime(self) -> None:
        target = make_target()
        environment = SimulatedComputerEnvironment((target,), facts={"window.ready": True})
        tasks = InMemoryTaskRepository()
        events = InMemoryEventStore()
        authority = AuthorizationContext(
            principal_id="user-1",
            user_intent_id="request-1",
            trust=TrustLevel.USER_INSTRUCTION,
            capabilities=frozenset({"simulator.computer"}),
        )
        task = TaskRecord.planned("Click Save", authorization=authority)
        tasks.save(task)
        registry = ToolRegistry()
        simulator = SimulatedComputerTool(
            environment,
            failure=SimulatorFailure.UNKNOWN_AFTER_DISPATCH,
        )
        registry.register(simulator)
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=registry,
            policy=PolicyEngine(),
            environment=environment,
            resources=ResourceManager(),
            verifier=FactVerifier(environment),
        )
        action = ActionContract(
            task_id=task.task_id,
            action_id="save-action",
            tool_name=simulator.spec.name,
            target=target.descriptor.identity,
            risk=RiskLevel.R1,
            authority=authority,
            parameters={"operation": "click"},
            preconditions=(Condition("window.ready", expected=True),),
            postconditions=(Condition("simulator.last_operation", expected="click"),),
            idempotency=Idempotency.UNKNOWN,
            timeout_seconds=1,
        )
        result = await runtime.execute_action(action, final_action=True)
        self.assertIs(result.task_status, TaskStatus.UNKNOWN)
        self.assertEqual(len(simulator.dispatched_action_ids), 1)
        with self.assertRaises(ActionReplayError):
            await runtime.execute_action(action, final_action=True)
        self.assertEqual(len(simulator.dispatched_action_ids), 1)

    async def test_timeout_after_simulated_dispatch_becomes_unknown(self) -> None:
        target = make_target()
        environment = SimulatedComputerEnvironment((target,), facts={"window.ready": True})
        tasks = InMemoryTaskRepository()
        events = InMemoryEventStore()
        authority = AuthorizationContext(
            principal_id="user-1",
            user_intent_id="request-1",
            trust=TrustLevel.USER_INSTRUCTION,
            capabilities=frozenset({"simulator.computer"}),
        )
        task = TaskRecord.planned("Click Save", authorization=authority)
        tasks.save(task)
        registry = ToolRegistry()
        simulator = SimulatedComputerTool(
            environment,
            failure=SimulatorFailure.TIMEOUT_AFTER_DISPATCH,
        )
        registry.register(simulator)
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=registry,
            policy=PolicyEngine(),
            environment=environment,
            resources=ResourceManager(),
            verifier=FactVerifier(environment),
        )
        action = ActionContract(
            task_id=task.task_id,
            action_id="timeout-action",
            tool_name=simulator.spec.name,
            target=target.descriptor.identity,
            risk=RiskLevel.R1,
            authority=authority,
            parameters={"operation": "click"},
            preconditions=(Condition("window.ready", expected=True),),
            postconditions=(Condition("simulator.last_operation", expected="click"),),
            idempotency=Idempotency.UNKNOWN,
            timeout_seconds=0.05,
        )
        result = await runtime.execute_action(action, final_action=True)
        self.assertIs(result.task_status, TaskStatus.UNKNOWN)
        self.assertIn("timeout-action", simulator.dispatched_action_ids)

    async def test_cancellation_after_dispatch_marks_unknown_and_releases_resources(self) -> None:
        target = make_target()
        environment = SimulatedComputerEnvironment((target,), facts={"window.ready": True})
        tasks = InMemoryTaskRepository()
        events = InMemoryEventStore()
        authority = AuthorizationContext(
            principal_id="user-1",
            user_intent_id="request-1",
            trust=TrustLevel.USER_INSTRUCTION,
            capabilities=frozenset({"simulator.computer"}),
        )
        task = TaskRecord.planned("Click Save", authorization=authority)
        tasks.save(task)
        registry = ToolRegistry()
        simulator = SimulatedComputerTool(
            environment,
            failure=SimulatorFailure.TIMEOUT_AFTER_DISPATCH,
        )
        registry.register(simulator)
        resources = ResourceManager()
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=registry,
            policy=PolicyEngine(),
            environment=environment,
            resources=resources,
            verifier=FactVerifier(environment),
        )
        action = ActionContract(
            task_id=task.task_id,
            action_id="cancel-action",
            tool_name=simulator.spec.name,
            target=target.descriptor.identity,
            risk=RiskLevel.R1,
            authority=authority,
            parameters={"operation": "click"},
            preconditions=(Condition("window.ready", expected=True),),
            postconditions=(Condition("simulator.last_operation", expected="click"),),
            idempotency=Idempotency.UNKNOWN,
            timeout_seconds=1,
        )
        execution = asyncio.create_task(runtime.execute_action(action, final_action=True))
        for _ in range(100):
            if "cancel-action" in simulator.dispatched_action_ids:
                break
            await asyncio.sleep(0.001)
        self.assertIn("cancel-action", simulator.dispatched_action_ids)
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution
        self.assertIs(tasks.get(task.task_id).status, TaskStatus.UNKNOWN)
        self.assertIsNone(await resources.owner("simulator.pointer"))


if __name__ == "__main__":
    unittest.main()
