"""Policy-gated, resource-aware action runtime with independent verification."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import (
    ActionContract,
    EvidenceSource,
    ObservationLease,
    RiskLevel,
    json_byte_size,
    validate_safe_token,
)
from arise.core.events import EventEnvelope, EventSeverity, EventStore
from arise.core.policy import ApprovalGrant, PolicyDecision, PolicyDecisionKind, PolicyEngine
from arise.core.ports import (
    EnvironmentPort,
    EvidenceRecord,
    ExecutionOutcome,
    ExecutionStatus,
    ToolNotFoundError,
    ToolRegistry,
    VerificationResult,
    VerifierPort,
)
from arise.core.ports import (
    VerificationStatus as PortVerificationStatus,
)
from arise.core.resources import ResourceAcquisitionTimeout, ResourceLeaseLost, ResourceManager
from arise.core.tasks import (
    WAITING_STATUSES,
    ActionStep,
    DuplicateActionError,
    InvalidTaskTransition,
    StepStatus,
    TaskNotFoundError,
    TaskRecord,
    TaskRepository,
    TaskStatus,
)
from arise.core.tasks import (
    VerificationStatus as TaskVerificationStatus,
)


class ActionReplayError(RuntimeError):
    """Refuse to re-dispatch an action that has already started or terminated."""


class TaskNotRunnableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ActionRunResult:
    task_id: str
    action_id: str
    task_status: TaskStatus
    step_status: StepStatus
    policy_decision: PolicyDecision
    message: str
    executed: bool = False
    verification: VerificationResult | None = None
    bound_action: ActionContract | None = None


class FactVerifier:
    """Verify declared postconditions using a fresh environment observation.

    This verifier intentionally ignores tool/model assertions about success. A
    platform adapter may provide a stronger verifier (filesystem/API/provider
    receipt); this generic implementation reports only local observed facts.
    """

    def __init__(self, environment: EnvironmentPort) -> None:
        self.environment = environment

    async def verify(self, action: ActionContract) -> VerificationResult:
        if not action.postconditions:
            return VerificationResult(
                PortVerificationStatus.UNKNOWN,
                0,
                "No postconditions were defined, so completion cannot be verified.",
            )
        try:
            observation = await self.environment.observe(action)
        except Exception:
            return VerificationResult(
                PortVerificationStatus.UNKNOWN,
                0,
                "A fresh observation was unavailable.",
            )
        if not isinstance(observation, ObservationLease):
            return VerificationResult(
                PortVerificationStatus.UNKNOWN,
                0,
                "The environment adapter returned a malformed verification observation.",
            )
        if not observation.is_valid(monotonic_now=time.monotonic()):
            return VerificationResult(
                PortVerificationStatus.UNKNOWN,
                0,
                "The verification observation expired.",
            )
        if observation.source not in {EvidenceSource.OBSERVED, EvidenceSource.RETRIEVED}:
            return VerificationResult(
                PortVerificationStatus.UNKNOWN,
                0,
                "Verification evidence is inferred or otherwise not authoritative.",
            )
        expected_target = action.target.fingerprint if action.target is not None else None
        if observation.target_fingerprint != expected_target:
            return VerificationResult(
                PortVerificationStatus.UNKNOWN,
                0,
                "The observed target does not match the action target.",
            )
        try:
            current = await self.environment.is_current(observation)
        except Exception:
            current = False
        if not current:
            return VerificationResult(
                PortVerificationStatus.UNKNOWN,
                0,
                "The environment changed during verification.",
            )

        evidence: list[EvidenceRecord] = []
        for condition in action.postconditions:
            matched = condition.evaluate(observation.facts)
            if matched is None:
                evidence.append(
                    self._evidence(observation, f"Required fact '{condition.key}' was absent.")
                )
                return VerificationResult(
                    PortVerificationStatus.UNKNOWN,
                    0,
                    "A required postcondition could not be observed.",
                    tuple(evidence),
                )
            if not matched:
                evidence.append(
                    self._evidence(observation, f"Postcondition '{condition.key}' did not match.")
                )
                return VerificationResult(
                    PortVerificationStatus.FAILED,
                    1,
                    "Observed state does not satisfy the declared postconditions.",
                    tuple(evidence),
                )
            evidence.append(
                self._evidence(observation, f"Postcondition '{condition.key}' was observed.")
            )
        return VerificationResult(
            PortVerificationStatus.PASSED,
            1,
            "All declared postconditions were observed in current local state.",
            tuple(evidence),
        )

    @staticmethod
    def _evidence(observation: ObservationLease, statement: str) -> EvidenceRecord:
        return EvidenceRecord(
            source=observation.source.value,
            observation_id=observation.lease_id,
            state_hash=observation.state_hash,
            statement=statement,
            captured_at=observation.created_at,
        )


class AgentRuntime:
    """Executes one typed action as a guarded transaction.

    The runtime does not plan natural language and does not provide a Windows or
    browser implementation. It accepts already-structured contracts, enforces
    policy/target/resource checks, dispatches a registered adapter, then
    independently verifies postconditions.
    """

    _REPLAYABLE_STEP_STATES = {
        StepStatus.PLANNED,
        StepStatus.WAITING_USER,
        StepStatus.WAITING_RESOURCE,
    }
    _RUNNABLE_TASK_STATES = {
        TaskStatus.READY,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_USER,
        TaskStatus.WAITING_RESOURCE,
        TaskStatus.PARTIALLY_COMPLETED,
        TaskStatus.RECOVERING,
    }

    def __init__(
        self,
        *,
        tasks: TaskRepository,
        events: EventStore,
        tools: ToolRegistry,
        policy: PolicyEngine,
        environment: EnvironmentPort,
        resources: ResourceManager,
        verifier: VerifierPort,
        runtime_id: str | None = None,
    ) -> None:
        self.tasks = tasks
        self.events = events
        self.tools = tools
        self.policy = policy
        self.environment = environment
        self.resources = resources
        self.verifier = verifier
        self.runtime_id = runtime_id or str(uuid.uuid4())
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._task_lock_users: dict[str, int] = {}

    async def execute_action(
        self,
        action: ActionContract,
        *,
        approval: ApprovalGrant | None = None,
        final_action: bool = False,
        resource_wait_timeout: float = 15.0,
    ) -> ActionRunResult:
        """Run one action. `final_action` is trusted plan-controller metadata.

        A final step still cannot complete the task until its own postconditions
        pass. Callers must not populate `final_action` from a model proposal.
        """

        task_lock = self._task_locks.get(action.task_id)
        if task_lock is None:
            task_lock = asyncio.Lock()
            self._task_locks[action.task_id] = task_lock
        self._task_lock_users[action.task_id] = self._task_lock_users.get(action.task_id, 0) + 1
        try:
            async with task_lock:
                return await self._execute_locked(
                    action,
                    approval=approval,
                    final_action=final_action,
                    resource_wait_timeout=resource_wait_timeout,
                )
        finally:
            remaining = self._task_lock_users[action.task_id] - 1
            if remaining == 0:
                self._task_lock_users.pop(action.task_id, None)
                if not task_lock.locked():
                    self._task_locks.pop(action.task_id, None)
            else:
                self._task_lock_users[action.task_id] = remaining

    async def _execute_locked(
        self,
        action: ActionContract,
        *,
        approval: ApprovalGrant | None,
        final_action: bool,
        resource_wait_timeout: float,
    ) -> ActionRunResult:
        task = self.tasks.get(action.task_id)
        if task is None:
            raise TaskNotFoundError(action.task_id)
        existing_step = task.find_step(action.action_id)
        if existing_step is not None and existing_step.status not in self._REPLAYABLE_STEP_STATES:
            raise ActionReplayError(
                f"action {action.action_id} is already {existing_step.status.value}; "
                "reconcile or replan instead of retrying"
            )
        if task.status not in self._RUNNABLE_TASK_STATES:
            raise TaskNotRunnableError(f"task is {task.status.value}, not runnable")
        if task.status is TaskStatus.WAITING_USER and (
            existing_step is None or existing_step.status is not StepStatus.WAITING_USER
        ):
            raise TaskNotRunnableError("task is waiting for a different user decision")

        try:
            tool = self.tools.get(action.tool_name)
        except ToolNotFoundError:
            return self._block_unavailable_tool(task, action)

        original_fingerprint = self.policy.contract_fingerprint(action, tool.spec)
        if existing_step is not None and existing_step.contract_fingerprint != original_fingerprint:
            raise DuplicateActionError("action_id was reused with a different action contract")
        grounding_error: ComputerAdapterError | None = None
        ground_action = getattr(tool, "ground_action", None)
        if callable(ground_action) and task.authorization == action.authority:
            try:
                action = await asyncio.wait_for(ground_action(action), action.timeout_seconds)
            except ComputerAdapterError as exc:
                grounding_error = exc
            except TimeoutError:
                from arise.core.computer import ComputerFailureCode

                grounding_error = ComputerAdapterError(
                    ComputerFailureCode.TIMEOUT, "Semantic target observation timed out."
                )

        effective_risk = RiskLevel(max(int(action.risk), int(tool.spec.minimum_risk)))
        fingerprint = self.policy.contract_fingerprint(action, tool.spec)
        step = task.find_step(action.action_id)
        if (
            step is not None
            and step.status is StepStatus.PLANNED
            and grounding_error is None
            and callable(ground_action)
        ):
            # Only refine an undispatched proposal whose original contract matched.
            # Approval is issued later against this observed identity, never the proposal.
            step.contract_fingerprint = fingerprint
            self.tasks.save(task)
        if step is not None:
            if step.contract_fingerprint != fingerprint:
                raise DuplicateActionError("action_id was reused with a different action contract")
            if step.status not in self._REPLAYABLE_STEP_STATES:
                raise ActionReplayError(
                    f"action {action.action_id} is already {step.status.value}; "
                    "reconcile or replan instead of retrying"
                )
        else:
            step = task.add_step(
                ActionStep(
                    action_id=action.action_id,
                    contract_fingerprint=fingerprint,
                    tool_name=action.tool_name,
                    risk=effective_risk,
                )
            )
            self.tasks.save(task)
            self._emit(
                "STEP_CREATED",
                task,
                step,
                {"tool": tool.spec.name, "risk": int(effective_risk)},
            )

        if task.authorization is None or action.authority != task.authorization:
            decision = PolicyDecision(
                PolicyDecisionKind.DENY,
                effective_risk,
                "action authority does not match the task's trusted request context",
            )
            self._emit(
                "ACTION_DENIED",
                task,
                step,
                {"reason": decision.reason, "risk": int(effective_risk)},
                severity=EventSeverity.SECURITY,
            )
            return self._block(task, step, reason=decision.reason, policy_decision=decision)

        provider = getattr(tool, "provider", None)
        backend = getattr(provider, "_backend", None)
        self._emit(
            "EXECUTION_PATH",
            task,
            step,
            {
                "tool": tool.spec.name,
                "provider": type(provider).__name__ if provider is not None else None,
                "backend": type(backend).__name__ if backend is not None else None,
                "target_fingerprint": action.target.fingerprint if action.target else None,
                "process_id": action.target.process_id if action.target else None,
                "window_id": action.target.window_id if action.target else None,
                "postcondition_count": len(action.postconditions),
                "postcondition_keys": [
                    item.key
                    if item.key
                    in {
                        "window.id",
                        "window.foreground",
                        "window.focused_element",
                        "window.open",
                        "window.visible",
                        "window.count",
                        "application.running",
                        "application.focused",
                        "process.running",
                        "process_id",
                        "window.element_count",
                        "uia.state_hash",
                    }
                    else "dynamic-key-withheld"
                    for item in action.postconditions
                ],
                "postcondition_operators": [item.operator.value for item in action.postconditions],
                "postcondition_expected_types": [
                    type(item.expected).__name__ for item in action.postconditions
                ],
                "required_resources": list(action.required_resources),
            },
        )

        if grounding_error is not None:
            return self._block(
                task,
                step,
                reason=f"Target grounding failed: {grounding_error.code.value}. {grounding_error}",
                policy_decision=PolicyDecision(
                    PolicyDecisionKind.DENY, effective_risk, "target observation unavailable"
                ),
            )

        try:
            tool.validate_parameters(action.parameters)
        except Exception as exc:
            return self._block(
                task,
                step,
                reason=f"Tool parameters failed validation: {type(exc).__name__}.",
                policy_decision=PolicyDecision(
                    PolicyDecisionKind.DENY,
                    effective_risk,
                    "tool parameter validation failed",
                ),
            )

        # Adapters may derive page/window locks from a validated target identity.
        # This remains optional so Phase 1 tools keep their static ToolSpec contract.
        dynamic_resources: tuple[str, ...] = ()
        resource_resolver = getattr(tool, "resources_for", None)
        if callable(resource_resolver):
            try:
                supplied_resources = resource_resolver(action)
                if not isinstance(supplied_resources, tuple):
                    raise ValueError("dynamic resources must be returned as a tuple")
                for resource in supplied_resources:
                    validate_safe_token(resource, "dynamic resource name")
                dynamic_resources = tuple(sorted(set(supplied_resources)))
            except Exception as exc:
                return self._block(
                    task,
                    step,
                    reason=(
                        f"Tool resource requirements failed validation: {exc.code.value}."
                        if isinstance(exc, ComputerAdapterError)
                        else f"Tool resource requirements failed validation: {type(exc).__name__}."
                    ),
                    policy_decision=PolicyDecision(
                        PolicyDecisionKind.DENY,
                        effective_risk,
                        "tool resource requirements are invalid",
                    ),
                )

        self._emit(
            "ACTION_PROPOSED",
            task,
            step,
            {"tool": tool.spec.name, "risk": int(effective_risk)},
        )
        decision = self.policy.evaluate(action, tool.spec, approval=approval)
        if decision.kind is PolicyDecisionKind.DENY:
            self._emit(
                "ACTION_DENIED",
                task,
                step,
                {"reason": decision.reason, "risk": int(decision.effective_risk)},
                severity=EventSeverity.SECURITY,
            )
            return self._block(task, step, reason=decision.reason, policy_decision=decision)
        if decision.kind is PolicyDecisionKind.CONFIRM:
            self._set_step_status(step, StepStatus.WAITING_USER, decision.reason)
            self.tasks.save(task)
            self._set_task_status(task, TaskStatus.WAITING_USER, decision.reason)
            self._emit(
                "ACTION_CONFIRMATION_REQUIRED",
                task,
                step,
                {"risk": int(decision.effective_risk), "action_id": action.action_id},
                severity=EventSeverity.WARNING,
            )
            return ActionRunResult(
                task.task_id,
                action.action_id,
                task.status,
                step.status,
                decision,
                "Waiting for approval of this exact action.",
                bound_action=action,
            )

        if task.status is TaskStatus.WAITING_USER:
            self._set_task_status(task, TaskStatus.READY, "Scoped user approval received.")
        if task.status in {
            TaskStatus.READY,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.RECOVERING,
            TaskStatus.WAITING_RESOURCE,
        }:
            self._set_task_status(task, TaskStatus.RUNNING, "Action execution started.")

        approval_consumed = False
        resource_names = (
            set(action.required_resources)
            | set(tool.spec.required_resources)
            | set(dynamic_resources)
        )
        self._set_step_status(step, StepStatus.WAITING_RESOURCE, "Acquiring required resources.")
        self.tasks.save(task)
        self._set_task_status(task, TaskStatus.WAITING_RESOURCE, "Waiting for exclusive resources.")
        self._emit(
            "RESOURCE_WAITING",
            task,
            step,
            {"resources": sorted(resource_names)},
        )

        resource_acquired = False
        try:
            async with self.resources.acquire_many(
                task.task_id,
                resource_names,
                priority=10,
                wait_timeout=resource_wait_timeout,
                lease_seconds=action.timeout_seconds + 10.0,
            ) as resource_lease:
                resource_acquired = True
                self._set_task_status(task, TaskStatus.RUNNING, "Required resources acquired.")
                self._emit(
                    "RESOURCE_ACQUIRED",
                    task,
                    step,
                    {"resources": sorted(resource_names)},
                )
                prepared = await self._prepare_environment(task, step, action, decision)
                if isinstance(prepared, ActionRunResult):
                    return prepared
                observation = prepared

                # Check the observation lease as close to dispatch as possible.
                try:
                    await resource_lease.ensure_valid()
                    still_current = observation.is_valid(monotonic_now=time.monotonic())
                    if still_current:
                        still_current = await self.environment.is_current(observation)
                except Exception:
                    still_current = False
                if not still_current:
                    return self._block(
                        task,
                        step,
                        reason=(
                            "The environment changed before the action could be "
                            "dispatched; reobserve first."
                        ),
                        policy_decision=decision,
                    )

                if decision.confirmation_required:
                    consumed = self.policy.consume_approval(action, tool.spec, approval)
                    if not consumed:
                        reason = (
                            "The scoped approval expired or was already used; "
                            "confirmation is required again."
                        )
                        self._set_step_status(step, StepStatus.WAITING_USER, reason)
                        self.tasks.save(task)
                        self._set_task_status(task, TaskStatus.WAITING_USER, reason)
                        return ActionRunResult(
                            task.task_id,
                            action.action_id,
                            task.status,
                            step.status,
                            PolicyDecision(
                                PolicyDecisionKind.CONFIRM,
                                decision.effective_risk,
                                reason,
                                confirmation_required=True,
                            ),
                            "Waiting for fresh approval.",
                            bound_action=action,
                        )
                    approval_consumed = True

                self._set_step_status(step, StepStatus.RUNNING, "Action dispatch started.")
                self.tasks.save(task)
                self._emit(
                    "ACTION_AUTHORIZED",
                    task,
                    step,
                    {
                        "tool": tool.spec.name,
                        "risk": int(decision.effective_risk),
                        "approval_used": approval_consumed,
                    },
                )
                self._emit("ACTION_STARTED", task, step, {"tool": tool.spec.name})

                try:
                    outcome = await asyncio.wait_for(
                        tool.execute(action, observation, resource_lease),
                        timeout=action.timeout_seconds,
                    )
                except asyncio.CancelledError:
                    self._mark_unknown(task, step, "Execution was cancelled after dispatch began.")
                    raise
                except TimeoutError:
                    return self._mark_unknown_result(
                        task,
                        step,
                        decision,
                        "The tool timed out after dispatch; the external effect is unknown.",
                    )
                except Exception as exc:
                    code = (
                        exc.code.value
                        if isinstance(exc, ComputerAdapterError)
                        else type(exc).__name__
                    )
                    return self._mark_unknown_result(
                        task,
                        step,
                        decision,
                        f"The tool failed after dispatch ({code}); the external effect is unknown.",
                    )

                if not isinstance(outcome, ExecutionOutcome):
                    return self._mark_unknown_result(
                        task,
                        step,
                        decision,
                        "The tool returned a malformed outcome after dispatch; "
                        "the external effect is unknown.",
                    )
                self._emit(
                    "EXECUTION_RESULT",
                    task,
                    step,
                    {
                        "status": outcome.status.value,
                        "side_effect_may_have_occurred": outcome.side_effect_may_have_occurred,
                    },
                )
                if json_byte_size(outcome.result_metadata) > tool.spec.max_result_bytes:
                    self._emit(
                        "TOOL_RESULT_DISCARDED",
                        task,
                        step,
                        {"reason": "result exceeded the registered tool size limit"},
                        severity=EventSeverity.WARNING,
                    )
                    outcome = ExecutionOutcome(
                        status=outcome.status,
                        summary=(
                            "Tool metadata was discarded because it exceeded the configured limit."
                        ),
                        side_effect_may_have_occurred=outcome.side_effect_may_have_occurred,
                        started_at=outcome.started_at,
                        finished_at=outcome.finished_at,
                    )
                if outcome.status is ExecutionStatus.UNKNOWN or (
                    outcome.status is ExecutionStatus.FAILED
                    and outcome.side_effect_may_have_occurred
                ):
                    return self._mark_unknown_result(
                        task,
                        step,
                        decision,
                        "The tool could not establish whether its side effect occurred.",
                    )
                if outcome.status is ExecutionStatus.FAILED:
                    return self._mark_failed(
                        task,
                        step,
                        decision,
                        "The tool reported a definite failure before any side effect.",
                    )
                try:
                    await resource_lease.ensure_valid()
                except ResourceLeaseLost:
                    return self._mark_unknown_result(
                        task,
                        step,
                        decision,
                        "The resource lease expired after dispatch; the external outcome "
                        "is unknown.",
                    )

                self._set_step_status(
                    step, StepStatus.VERIFYING, "Checking declared postconditions."
                )
                self.tasks.save(task)
                self._set_task_status(
                    task, TaskStatus.VERIFYING, "Independent verification started."
                )
                self._emit(
                    "VERIFICATION_STARTED", task, step, {"strategy": action.verification_strategy}
                )
                try:
                    verification = await self.verifier.verify(action)
                except asyncio.CancelledError:
                    self._mark_unknown(task, step, "Verification was cancelled after execution.")
                    raise
                except Exception as exc:
                    verification = VerificationResult(
                        PortVerificationStatus.UNKNOWN,
                        0,
                        f"Verifier failed ({type(exc).__name__}).",
                    )
                if not isinstance(verification, VerificationResult):
                    verification = VerificationResult(
                        PortVerificationStatus.UNKNOWN,
                        0,
                        "Verifier returned malformed evidence.",
                    )
                try:
                    await resource_lease.ensure_valid()
                except ResourceLeaseLost:
                    return self._mark_unknown_result(
                        task,
                        step,
                        decision,
                        "The resource lease expired before verification completed.",
                    )

                self._emit(
                    "VERIFICATION_EVIDENCE",
                    task,
                    step,
                    {
                        "status": verification.status.value,
                        "level": verification.level,
                        "evidence": [
                            {
                                "source": item.source,
                                "observation_id": item.observation_id,
                                "state_hash": item.state_hash,
                            }
                            for item in verification.evidence[:16]
                        ],
                    },
                )
                if verification.status is PortVerificationStatus.PASSED:
                    step.verification_status = TaskVerificationStatus.PASSED
                    self._set_step_status(step, StepStatus.SUCCEEDED, "Postconditions verified.")
                    self.tasks.save(task)
                    self._emit(
                        "VERIFICATION_PASSED",
                        task,
                        step,
                        {"level": verification.level, "evidence_count": len(verification.evidence)},
                    )
                    if final_action:
                        self._set_task_status(
                            task,
                            TaskStatus.COMPLETED,
                            "Verified final action.",
                            verification_passed=True,
                        )
                        self._emit(
                            "TASK_COMPLETED", task, step, {"verification_level": verification.level}
                        )
                        message = "The action completed and its postconditions were verified."
                    else:
                        self._set_task_status(
                            task,
                            TaskStatus.PARTIALLY_COMPLETED,
                            "This step was verified; the plan has not been declared complete.",
                        )
                        message = "This step is verified; the task remains partially complete."
                    return ActionRunResult(
                        task.task_id,
                        action.action_id,
                        task.status,
                        step.status,
                        decision,
                        message,
                        executed=True,
                        verification=verification,
                    )

                if verification.status is PortVerificationStatus.FAILED:
                    step.verification_status = TaskVerificationStatus.FAILED
                    self._set_step_status(
                        step, StepStatus.FAILED, "Postcondition verification failed."
                    )
                    self.tasks.save(task)
                    self._set_task_status(
                        task, TaskStatus.FAILED, "Postconditions were not satisfied."
                    )
                    self._emit(
                        "VERIFICATION_FAILED",
                        task,
                        step,
                        {"level": verification.level},
                        severity=EventSeverity.ERROR,
                    )
                    return ActionRunResult(
                        task.task_id,
                        action.action_id,
                        task.status,
                        step.status,
                        decision,
                        (
                            "The action ran, but verification found that its "
                            "postconditions were not satisfied."
                        ),
                        executed=True,
                        verification=verification,
                    )

                step.verification_status = TaskVerificationStatus.UNKNOWN
                self._set_step_status(step, StepStatus.UNKNOWN, "Verification outcome is unknown.")
                self.tasks.save(task)
                self._set_task_status(
                    task, TaskStatus.UNKNOWN, "The result could not be independently verified."
                )
                self._emit(
                    "VERIFICATION_UNKNOWN",
                    task,
                    step,
                    {"level": verification.level},
                    severity=EventSeverity.WARNING,
                )
                return ActionRunResult(
                    task.task_id,
                    action.action_id,
                    task.status,
                    step.status,
                    decision,
                    (
                        "The action may have run, but its result is unknown. "
                        "It will not be retried blindly."
                    ),
                    executed=True,
                    verification=verification,
                )
        except asyncio.CancelledError:
            if step.status in {
                StepStatus.PLANNED,
                StepStatus.WAITING_USER,
                StepStatus.WAITING_RESOURCE,
            }:
                self._set_step_status(
                    step, StepStatus.CANCELLED, "Cancelled before action dispatch."
                )
                self.tasks.save(task)
                if task.status is not TaskStatus.CANCELLED:
                    self._set_task_status(
                        task, TaskStatus.CANCELLED, "Cancelled before action dispatch."
                    )
                self._emit("STEP_CANCELLED", task, step, {"dispatched": False})
            raise
        except ResourceAcquisitionTimeout:
            return self._block(
                task,
                step,
                reason="Required resources remained busy until the wait deadline expired.",
                policy_decision=decision,
            )
        finally:
            # ResourceManager releases the lease first; this event contains no
            # target data or tool result and is safe to retain in audit history.
            if resource_acquired:
                self._emit("RESOURCE_RELEASED", task, step, {"resources": sorted(resource_names)})

    async def _prepare_environment(
        self,
        task: TaskRecord,
        step: ActionStep,
        action: ActionContract,
        decision: PolicyDecision,
    ) -> ObservationLease | ActionRunResult:
        try:
            observation = await self.environment.observe(action)
        except Exception:
            return self._block(
                task,
                step,
                reason="The current environment could not be observed safely.",
                policy_decision=decision,
            )
        if not isinstance(observation, ObservationLease):
            return self._block(
                task,
                step,
                reason="The environment adapter returned a malformed observation.",
                policy_decision=decision,
            )
        expected_target = action.target.fingerprint if action.target is not None else None
        if observation.target_fingerprint != expected_target:
            return self._block(
                task,
                step,
                reason="Observed target identity does not match the requested target.",
                policy_decision=decision,
            )
        if not observation.is_valid(monotonic_now=time.monotonic()):
            return self._block(
                task,
                step,
                reason="The observation lease was already expired.",
                policy_decision=decision,
            )
        if observation.source not in {EvidenceSource.OBSERVED, EvidenceSource.RETRIEVED}:
            return self._block(
                task,
                step,
                reason="The grounding source is inferred or otherwise untrusted.",
                policy_decision=decision,
            )
        if (
            decision.effective_risk >= RiskLevel.R2
            and observation.confidence < self.policy.config.minimum_observation_confidence
        ):
            return self._block(
                task,
                step,
                reason="Target grounding confidence is below policy minimum.",
                policy_decision=decision,
            )

        for condition in action.preconditions:
            result = condition.evaluate(observation.facts)
            if result is not True:
                state = "not satisfied" if result is False else "not observable"
                return self._block(
                    task,
                    step,
                    reason=f"Precondition '{condition.key}' is {state}; no action was dispatched.",
                    policy_decision=decision,
                )
        self._emit(
            "OBSERVATION_CREATED",
            task,
            step,
            {
                "observation_id": observation.lease_id,
                "state_hash": observation.state_hash,
                "target_fingerprint": observation.target_fingerprint,
            },
        )
        return observation

    def _block_unavailable_tool(self, task: TaskRecord, action: ActionContract) -> ActionRunResult:
        decision = PolicyDecision(PolicyDecisionKind.DENY, action.risk, "tool is not registered")
        step = task.find_step(action.action_id)
        if step is None:
            step = task.add_step(
                ActionStep(
                    action_id=action.action_id,
                    contract_fingerprint=action.approval_fingerprint(action.risk, "unavailable"),
                    tool_name=action.tool_name,
                    risk=action.risk,
                )
            )
            self.tasks.save(task)
        return self._block(task, step, reason=decision.reason, policy_decision=decision)

    def _block(
        self,
        task: TaskRecord,
        step: ActionStep,
        *,
        reason: str,
        policy_decision: PolicyDecision,
    ) -> ActionRunResult:
        self._set_step_status(step, StepStatus.BLOCKED, reason)
        self.tasks.save(task)
        if task.status is not TaskStatus.BLOCKED:
            self._set_task_status(task, TaskStatus.BLOCKED, reason)
        self._emit("ACTION_BLOCKED", task, step, {"reason": reason}, severity=EventSeverity.WARNING)
        return ActionRunResult(
            task.task_id,
            step.action_id,
            task.status,
            step.status,
            policy_decision,
            reason,
        )

    def _mark_failed(
        self,
        task: TaskRecord,
        step: ActionStep,
        decision: PolicyDecision,
        reason: str,
    ) -> ActionRunResult:
        self._set_step_status(step, StepStatus.FAILED, reason)
        self.tasks.save(task)
        self._set_task_status(task, TaskStatus.FAILED, reason)
        self._emit("STEP_FAILED", task, step, {"reason": reason}, severity=EventSeverity.ERROR)
        return ActionRunResult(
            task.task_id, step.action_id, task.status, step.status, decision, reason, executed=True
        )

    def _mark_unknown_result(
        self,
        task: TaskRecord,
        step: ActionStep,
        decision: PolicyDecision,
        reason: str,
    ) -> ActionRunResult:
        self._mark_unknown(task, step, reason)
        return ActionRunResult(
            task.task_id,
            step.action_id,
            task.status,
            step.status,
            decision,
            (
                "The external outcome is unknown. Reconcile current state "
                "before taking another action."
            ),
            executed=True,
        )

    def _mark_unknown(self, task: TaskRecord, step: ActionStep, reason: str) -> None:
        step.verification_status = TaskVerificationStatus.UNKNOWN
        self._set_step_status(step, StepStatus.UNKNOWN, reason)
        self.tasks.save(task)
        self._set_task_status(task, TaskStatus.UNKNOWN, reason)
        self._emit("STEP_UNKNOWN", task, step, {"reason": reason}, severity=EventSeverity.WARNING)

    def _set_step_status(self, step: ActionStep, status: StepStatus, reason: str) -> None:
        step.set_status(status, reason=reason)

    # ------------------------------------------------------------------
    # Waiting-state sink used by tools that wait for external completion
    # ------------------------------------------------------------------

    def begin_wait(self, task_id: str, status: TaskStatus, *, waiting_reason: str) -> None:
        """Record *what* a task is waiting for instead of a bare "running"."""

        task = self.tasks.get(task_id)
        if task is None:
            return
        try:
            task.begin_wait(status, waiting_reason=waiting_reason)
        except InvalidTaskTransition:
            return
        self.tasks.save(task)
        self._emit(
            "TASK_WAITING",
            task,
            None,
            {"status": status.value, "waiting_reason": waiting_reason},
        )

    def end_wait(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            return
        resumed = task.status in WAITING_STATUSES
        task.end_wait()
        self.tasks.save(task)
        if resumed:
            self._emit(
                "TASK_WAIT_ENDED",
                task,
                None,
                {"status": task.status.value, "resume_count": task.resume_count},
            )

    def _set_task_status(
        self,
        task: TaskRecord,
        status: TaskStatus,
        reason: str,
        *,
        verification_passed: bool = False,
    ) -> None:
        old_status = task.status
        task.transition_to(status, reason=reason, verification_passed=verification_passed)
        if task.status is old_status:
            self.tasks.save(task)
            return
        self.tasks.save(task)
        self._emit(
            f"TASK_{status.value.upper()}",
            task,
            None,
            {"from": old_status.value, "to": status.value, "reason": reason},
            severity=EventSeverity.ERROR
            if status in {TaskStatus.FAILED, TaskStatus.UNKNOWN}
            else EventSeverity.INFO,
        )

    def _emit(
        self,
        event_type: str,
        task: TaskRecord,
        step: ActionStep | None,
        payload: Mapping[str, Any],
        *,
        severity: EventSeverity = EventSeverity.INFO,
    ) -> None:
        self.events.append(
            EventEnvelope(
                event_type=event_type,
                task_id=task.task_id,
                step_id=step.action_id if step is not None else None,
                session_id=task.session_id,
                correlation_id=task.correlation_id,
                causation_id=task.request_id,
                source="agent-runtime",
                severity=severity,
                payload=payload,
                runtime_id=self.runtime_id,
            )
        )
