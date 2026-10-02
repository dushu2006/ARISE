from __future__ import annotations

import asyncio
import dataclasses
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from arise.adapters.memory import InMemoryEnvironment
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
from arise.core.policy import PolicyConfig, PolicyDecisionKind, PolicyEngine
from arise.core.ports import (
    EvidenceRecord,
    ExecutionOutcome,
    ExecutionStatus,
    ToolRegistry,
    ToolSpec,
    VerificationResult,
    VerificationStatus,
)
from arise.core.resources import ResourceLease, ResourceLeaseLost, ResourceManager
from arise.core.runtime import ActionReplayError, AgentRuntime, FactVerifier
from arise.core.tasks import (
    InMemoryTaskRepository,
    InvalidTaskTransition,
    TaskRecord,
    TaskStatus,
)


class RecordingSetTool:
    def __init__(
        self,
        environment: InMemoryEnvironment,
        *,
        name: str = "test.set",
        risk: RiskLevel = RiskLevel.R1,
        hang_after_write: bool = False,
        minimum_capabilities: frozenset[str] = frozenset(),
    ) -> None:
        self.environment = environment
        self.calls = 0
        self.started = asyncio.Event()
        self.hang_after_write = hang_after_write
        self._spec = ToolSpec(
            name=name,
            version="1.0",
            description="test adapter",
            minimum_risk=risk,
            required_capabilities=minimum_capabilities,
            required_resources=("test-state",),
            idempotency=Idempotency.IDEMPOTENT,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def validate_parameters(self, parameters) -> None:
        if "key" not in parameters or "value" not in parameters:
            raise ValueError("key and value are required")

    async def execute(
        self,
        action: ActionContract,
        observation,
        resources: ResourceLease,
    ) -> ExecutionOutcome:
        del observation
        self.calls += 1
        self.started.set()
        await resources.ensure_valid()
        await self.environment.set_fact(str(action.parameters["key"]), action.parameters["value"])
        if self.hang_after_write:
            await asyncio.Event().wait()
        return ExecutionOutcome(ExecutionStatus.SUCCEEDED, "test action completed")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.target = TargetIdentity(
            platform="test",
            application="sample-app",
            object_id="sample-object",
            semantic_name="Sample object",
            confidence=1.0,
        )
        self.environment = InMemoryEnvironment(
            {"application.ready": True, "project.open": False, "message.sent": False},
            target=self.target,
        )
        self.tasks = InMemoryTaskRepository()
        self.events = InMemoryEventStore()
        self.policy = PolicyEngine()
        self.resources = ResourceManager()
        self.tools = ToolRegistry()
        self.tool = RecordingSetTool(self.environment)
        self.tools.register(self.tool)
        self.runtime = AgentRuntime(
            tasks=self.tasks,
            events=self.events,
            tools=self.tools,
            policy=self.policy,
            environment=self.environment,
            resources=self.resources,
            verifier=FactVerifier(self.environment),
        )
        self.authority = AuthorizationContext(
            principal_id="test-user",
            user_intent_id="intent-1",
            trust=TrustLevel.USER_INSTRUCTION,
        )
        self.task = TaskRecord.planned("Open the sample project", authorization=self.authority)
        self.tasks.save(self.task)

    def make_action(
        self,
        *,
        risk: RiskLevel = RiskLevel.R1,
        trust: TrustLevel = TrustLevel.USER_INSTRUCTION,
        tool_name: str = "test.set",
        key: str = "project.open",
        value: object = True,
        target: TargetIdentity | None = None,
        timeout: float = 1.0,
        preconditions: tuple[Condition, ...] | None = None,
        postconditions: tuple[Condition, ...] | None = None,
    ) -> ActionContract:
        return ActionContract(
            task_id=self.task.task_id,
            tool_name=tool_name,
            target=target or self.target,
            risk=risk,
            authority=(
                self.authority
                if trust is TrustLevel.USER_INSTRUCTION
                else AuthorizationContext(
                    principal_id="test-user",
                    user_intent_id="intent-1",
                    trust=trust,
                )
            ),
            parameters={"key": key, "value": value},
            preconditions=(
                preconditions
                if preconditions is not None
                else (Condition("application.ready", expected=True),)
            ),
            postconditions=(
                postconditions if postconditions is not None else (Condition(key, expected=value),)
            ),
            idempotency=Idempotency.IDEMPOTENT,
            timeout_seconds=timeout,
        )

    async def test_task_cannot_jump_from_running_to_completed(self) -> None:
        task = TaskRecord.planned("State machine check")
        task.transition_to(TaskStatus.RUNNING)
        with self.assertRaises(InvalidTaskTransition):
            task.transition_to(TaskStatus.COMPLETED)
        task.transition_to(TaskStatus.VERIFYING)
        with self.assertRaises(InvalidTaskTransition):
            task.transition_to(TaskStatus.COMPLETED)
        task.transition_to(TaskStatus.COMPLETED, verification_passed=True)
        self.assertEqual(task.status, TaskStatus.COMPLETED)

    async def test_verified_action_completes_only_after_postcondition(self) -> None:
        action = self.make_action()
        result = await self.runtime.execute_action(action, final_action=True)

        self.assertEqual(result.task_status, TaskStatus.COMPLETED)
        self.assertEqual(result.step_status.value, "succeeded")
        self.assertTrue(result.executed)
        self.assertIsNotNone(result.verification)
        self.assertEqual(result.verification.status.value, "passed")
        self.assertEqual(self.tool.calls, 1)

        event_types = [
            event.event_type for event in self.events.read_after(task_id=self.task.task_id)
        ]
        self.assertLess(
            event_types.index("VERIFICATION_PASSED"), event_types.index("TASK_COMPLETED")
        )
        self.assertIn("ACTION_AUTHORIZED", event_types)

    def test_verification_evidence_payloads_are_bounded(self) -> None:
        with self.assertRaises(ValueError):
            EvidenceRecord(
                source="observed",
                observation_id="observation-1",
                state_hash="hash-1",
                statement="x" * 4097,
            )

    async def test_verifier_cannot_claim_passed_without_grounded_evidence(self) -> None:
        class EvidenceFreeVerifier:
            async def verify(self, action: ActionContract) -> VerificationResult:
                del action
                return VerificationResult(
                    VerificationStatus.PASSED,
                    1,
                    "Unsubstantiated success claim.",
                )

        runtime = AgentRuntime(
            tasks=self.tasks,
            events=self.events,
            tools=self.tools,
            policy=self.policy,
            environment=self.environment,
            resources=self.resources,
            verifier=EvidenceFreeVerifier(),
        )
        result = await runtime.execute_action(self.make_action(), final_action=True)

        self.assertEqual(result.task_status, TaskStatus.UNKNOWN)
        self.assertEqual(result.verification.status.value, "unknown")
        self.assertNotEqual(self.tasks.get(self.task.task_id).status, TaskStatus.COMPLETED)
        self.assertEqual(self.tool.calls, 1)

    async def test_expired_resource_lease_after_dispatch_cannot_claim_success(self) -> None:
        class ExpiringLease:
            def __init__(self) -> None:
                self.validity_checks = 0

            async def ensure_valid(self) -> None:
                self.validity_checks += 1
                if self.validity_checks >= 2:
                    raise ResourceLeaseLost("injected expired resource lease")

            async def release(self) -> None:
                return None

        lease = ExpiringLease()

        class ExpiringResourceManager:
            @asynccontextmanager
            async def acquire_many(self, task_id, resources, **kwargs):
                del task_id, resources, kwargs
                try:
                    yield lease
                finally:
                    await lease.release()

        class WriteTool(RecordingSetTool):
            async def execute(self, action, observation, resources):
                del observation, resources
                self.calls += 1
                await self.environment.set_fact(
                    str(action.parameters["key"]), action.parameters["value"]
                )
                return ExecutionOutcome(ExecutionStatus.SUCCEEDED, "write completed")

        tool = WriteTool(self.environment)
        tools = ToolRegistry()
        tools.register(tool)
        runtime = AgentRuntime(
            tasks=self.tasks,
            events=self.events,
            tools=tools,
            policy=self.policy,
            environment=self.environment,
            resources=ExpiringResourceManager(),
            verifier=FactVerifier(self.environment),
        )
        result = await runtime.execute_action(self.make_action(), final_action=True)

        self.assertEqual(lease.validity_checks, 2)
        self.assertEqual(result.task_status, TaskStatus.UNKNOWN)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(self.tasks.get(self.task.task_id).status, TaskStatus.UNKNOWN)

    async def test_missing_precondition_blocks_before_dispatch(self) -> None:
        action = self.make_action(preconditions=(Condition("missing.fact", expected=True),))
        result = await self.runtime.execute_action(action, final_action=True)

        self.assertEqual(result.task_status, TaskStatus.BLOCKED)
        self.assertFalse(result.executed)
        self.assertEqual(self.tool.calls, 0)
        self.assertIn("not observable", result.message)

    async def test_wrong_target_is_blocked_without_tool_call(self) -> None:
        action = self.make_action(
            target=TargetIdentity(
                platform="test",
                application="different-app",
                object_id="other-object",
                semantic_name="Other object",
            )
        )
        result = await self.runtime.execute_action(action, final_action=True)
        self.assertEqual(result.task_status, TaskStatus.BLOCKED)
        self.assertEqual(self.tool.calls, 0)
        self.assertIn("target identity", result.message)

    async def test_untrusted_content_cannot_authorize_side_effect(self) -> None:
        action = self.make_action(trust=TrustLevel.UNTRUSTED_EXTERNAL)
        result = await self.runtime.execute_action(action, final_action=True)
        self.assertEqual(result.policy_decision.kind, PolicyDecisionKind.DENY)
        self.assertEqual(result.task_status, TaskStatus.BLOCKED)
        self.assertEqual(self.tool.calls, 0)

    async def test_action_cannot_grant_itself_new_capabilities(self) -> None:
        action = self.make_action()
        forged_authority = AuthorizationContext(
            principal_id="test-user",
            user_intent_id="intent-1",
            trust=TrustLevel.USER_INSTRUCTION,
            capabilities=frozenset({"system.admin"}),
        )
        forged_action = dataclasses.replace(action, authority=forged_authority)
        result = await self.runtime.execute_action(forged_action, final_action=True)
        self.assertEqual(result.policy_decision.kind, PolicyDecisionKind.DENY)
        self.assertIn("trusted request context", result.message)
        self.assertEqual(self.tool.calls, 0)

    async def test_unknown_outcome_is_not_retried(self) -> None:
        self.tool.hang_after_write = True
        action = self.make_action(timeout=0.02)
        result = await self.runtime.execute_action(action, final_action=True)

        self.assertEqual(result.task_status, TaskStatus.UNKNOWN)
        self.assertFalse(result.verification)
        self.assertEqual(self.tool.calls, 1)
        with self.assertRaises(ActionReplayError):
            await self.runtime.execute_action(action, final_action=True)
        self.assertEqual(self.tool.calls, 1)
        self.assertIsNone(await self.resources.owner("test-state"))

    async def test_cancellation_before_dispatch_marks_task_cancelled(self) -> None:
        action = self.make_action()
        async with self.resources.acquire_many("external-owner", ["test-state"], lease_seconds=5):
            execution = asyncio.create_task(self.runtime.execute_action(action, final_action=True))
            await asyncio.sleep(0.01)
            execution.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await execution
        stored = self.tasks.get(self.task.task_id)
        self.assertEqual(stored.status, TaskStatus.CANCELLED)
        self.assertEqual(stored.steps[0].status.value, "cancelled")
        self.assertEqual(self.tool.calls, 0)

    async def test_cancellation_after_dispatch_records_unknown(self) -> None:
        self.tool.hang_after_write = True
        action = self.make_action(timeout=5)
        execution = asyncio.create_task(self.runtime.execute_action(action, final_action=True))
        await asyncio.wait_for(self.tool.started.wait(), timeout=1)
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution
        stored = self.tasks.get(self.task.task_id)
        self.assertEqual(stored.status, TaskStatus.UNKNOWN)
        self.assertEqual(stored.steps[0].status.value, "unknown")
        self.assertIsNone(await self.resources.owner("test-state"))

    async def test_confirmation_is_exact_and_single_use(self) -> None:
        high_risk_tool = RecordingSetTool(self.environment, name="test.send", risk=RiskLevel.R3)
        self.tools = ToolRegistry()
        self.tools.register(high_risk_tool)
        self.runtime.tools = self.tools
        action = self.make_action(risk=RiskLevel.R1, tool_name="test.send", key="message.sent")

        first = await self.runtime.execute_action(action, final_action=True)
        self.assertEqual(first.task_status, TaskStatus.WAITING_USER)
        self.assertEqual(high_risk_tool.calls, 0)
        with self.assertRaises(PermissionError):
            self.policy.issue_approval(action, high_risk_tool.spec, approved_by="different-user")

        grant = self.policy.issue_approval(action, high_risk_tool.spec, approved_by="test-user")
        changed = dataclasses.replace(action, parameters={"key": "message.sent", "value": False})
        changed_decision = self.policy.evaluate(changed, high_risk_tool.spec, approval=grant)
        self.assertEqual(changed_decision.kind, PolicyDecisionKind.CONFIRM)

        second = await self.runtime.execute_action(action, approval=grant, final_action=True)
        self.assertEqual(second.task_status, TaskStatus.COMPLETED)
        self.assertEqual(high_risk_tool.calls, 1)
        used_again = self.policy.evaluate(action, high_risk_tool.spec, approval=grant)
        self.assertEqual(used_again.kind, PolicyDecisionKind.CONFIRM)

    async def test_approval_expires(self) -> None:
        high_risk_tool = RecordingSetTool(self.environment, name="test.external", risk=RiskLevel.R3)
        action = self.make_action(tool_name="test.external", key="message.sent")
        now = datetime.now(UTC)
        grant = self.policy.issue_approval(
            action,
            high_risk_tool.spec,
            approved_by="test-user",
            ttl_seconds=1,
            now=now,
        )
        expired = self.policy.evaluate(
            action,
            high_risk_tool.spec,
            approval=grant,
            now=now + timedelta(seconds=2),
        )
        self.assertEqual(expired.kind, PolicyDecisionKind.CONFIRM)

    async def test_policy_tool_floor_cannot_be_labeled_down_by_model(self) -> None:
        tool = ToolSpec(
            name="test.purchase",
            version="1",
            description="purchase simulation",
            minimum_risk=RiskLevel.R3,
        )
        action = self.make_action(risk=RiskLevel.R0, tool_name=tool.name)
        decision = self.policy.evaluate(action, tool)
        self.assertEqual(decision.effective_risk, RiskLevel.R3)
        self.assertEqual(decision.kind, PolicyDecisionKind.CONFIRM)

    async def test_r3_confirmation_cannot_be_disabled_by_relaxed_user_settings(self) -> None:
        relaxed_policy = PolicyEngine(
            PolicyConfig(
                auto_execute_through=RiskLevel.R2,
                confirmation_threshold=RiskLevel.R4,
            )
        )
        tool = ToolSpec(
            name="test.external",
            version="1",
            description="external side effect",
            minimum_risk=RiskLevel.R3,
        )
        action = self.make_action(risk=RiskLevel.R0, tool_name=tool.name)
        decision = relaxed_policy.evaluate(action, tool)
        self.assertEqual(decision.kind, PolicyDecisionKind.CONFIRM)
        self.assertFalse(relaxed_policy.consume_approval(action, tool, None))

    async def test_r4_is_disabled_even_if_a_user_attempts_to_approve(self) -> None:
        tool = ToolSpec(
            name="test.privileged",
            version="1",
            description="privileged test action",
            minimum_risk=RiskLevel.R4,
        )
        action = self.make_action(risk=RiskLevel.R4, tool_name=tool.name)
        decision = self.policy.evaluate(action, tool)
        self.assertEqual(decision.kind, PolicyDecisionKind.DENY)
        with self.assertRaises(PermissionError):
            self.policy.issue_approval(action, tool, approved_by="test-user")

    async def test_no_postconditions_cannot_claim_success(self) -> None:
        action = self.make_action(postconditions=())
        result = await self.runtime.execute_action(action, final_action=True)
        self.assertEqual(result.task_status, TaskStatus.UNKNOWN)
        self.assertEqual(result.verification.status.value, "unknown")


if __name__ == "__main__":
    unittest.main()
