"""Bounded, event-driven "wait until the environment says X" action.

Long-running work (an external service generating code, a download finishing, an
application reaching a state) must not become "sleep and assume". This tool waits
on an *observable condition* using :class:`WaitCoordinator`: relevant events first,
adaptive polling only as the fallback, always bounded by the action timeout.

It performs no side effect and claims no completion of anything it did not do: the
condition it waits for is a plain fact predicate expressed with the same
``ConditionModel`` shape used for preconditions and postconditions, and the task
still cannot complete until the runtime's independent verifier observes the
declared postconditions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from arise.core.computer import ComputerFailureCode, PerceptionSource
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import (
    ActionContract,
    Condition,
    ConditionOperator,
    Idempotency,
    ObservationLease,
    RiskLevel,
    utc_now,
)
from arise.core.ports import (
    ExecutionOutcome,
    ExecutionStatus,
    ToolRegistry,
    ToolSpec,
)
from arise.core.resources import ResourceLease
from arise.core.tasks import TaskStatus
from arise.core.waits import WaitCoordinator, WaitOutcome, WaitResult, wait_summary

MAX_WAIT_SECONDS = 900.0
MIN_WAIT_SECONDS = 1.0
_MAX_POLL_INTERVAL_SECONDS = 60.0
_MIN_POLL_INTERVAL_SECONDS = 0.25

WAIT_TARGET_STATUSES: dict[str, TaskStatus] = {
    "application": TaskStatus.WAITING_FOR_APPLICATION,
    "browser": TaskStatus.WAITING_FOR_BROWSER,
    "verification": TaskStatus.WAITING_FOR_VERIFICATION,
    "external": TaskStatus.WAITING_FOR_EXTERNAL_RESULT,
}


class EnvironmentObserver(Protocol):
    """One read-only source of current environment facts."""

    async def __call__(self, action: ActionContract) -> Mapping[str, Any]: ...


class TaskStatusSink(Protocol):
    """Lets a waiting tool publish *what* the task is waiting for."""

    def begin_wait(self, task_id: str, status: TaskStatus, *, waiting_reason: str) -> None: ...

    def end_wait(self, task_id: str) -> None: ...


def wait_condition_from_parameters(parameters: Mapping[str, Any]) -> Condition:
    """Build a typed fact predicate from validated tool parameters."""

    key = str(parameters.get("condition_key") or "").strip()
    operator_value = str(parameters.get("operator") or ConditionOperator.EQUALS.value)
    try:
        operator = ConditionOperator(operator_value)
    except ValueError as exc:
        raise ValueError("operator is not a supported condition operator") from exc
    expected = parameters.get("expected")
    description = str(parameters.get("description") or "")[:256]
    return Condition(
        key=key,
        operator=operator,
        expected=expected,
        description=description or f"Wait until {key} {operator.value}",
    )


@dataclass(slots=True)
class EnvironmentWaitTool:
    """Wait for an observed environment condition, with bounded adaptive polling."""

    observers: tuple[EnvironmentObserver, ...]
    coordinator: WaitCoordinator | None = None
    _status_sink: TaskStatusSink | None = None
    _last_result: WaitResult | None = None

    def __post_init__(self) -> None:
        if not self.observers:
            raise ValueError("wait tool requires at least one environment observer")
        if self.coordinator is None:
            self.coordinator = WaitCoordinator()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="system.wait_for_condition",
            version="1.0.0",
            description=(
                "Wait (bounded, event-driven, adaptive polling fallback) until an "
                "observed environment condition becomes true. Use it to wait for "
                "long-running external work, an application state, a browser state, "
                "or a verification result. It never performs the work itself."
            ),
            minimum_risk=RiskLevel.R0,
            required_capabilities=frozenset(),
            required_resources=(),
            declared_side_effects=(),
            idempotency=Idempotency.IDEMPOTENT,
            max_result_bytes=4096,
            parameter_names=(
                "condition_key",
                "operator",
                "expected",
                "timeout_seconds",
                "poll_interval_seconds",
                "wait_target",
                "description",
            ),
            target_scope=None,
        )

    @property
    def last_result(self) -> WaitResult | None:
        return self._last_result

    def set_status_sink(self, sink: TaskStatusSink | None) -> None:
        self._status_sink = sink

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        key = parameters.get("condition_key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("condition_key is required")
        if len(key) > 128:
            raise ValueError("condition_key exceeds the maximum length")
        operator_value = str(parameters.get("operator") or ConditionOperator.EQUALS.value)
        if operator_value not in {item.value for item in ConditionOperator}:
            raise ValueError("operator is not a supported condition operator")
        if operator_value != ConditionOperator.EXISTS.value and "expected" not in parameters:
            raise ValueError("expected is required for this operator")
        timeout = parameters.get("timeout_seconds", 60.0)
        try:
            timeout_seconds = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout_seconds must be a number") from exc
        if not MIN_WAIT_SECONDS <= timeout_seconds <= MAX_WAIT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be between {MIN_WAIT_SECONDS} and {MAX_WAIT_SECONDS}"
            )
        interval = parameters.get("poll_interval_seconds", 2.0)
        try:
            interval_seconds = float(interval)
        except (TypeError, ValueError) as exc:
            raise ValueError("poll_interval_seconds must be a number") from exc
        if not _MIN_POLL_INTERVAL_SECONDS <= interval_seconds <= _MAX_POLL_INTERVAL_SECONDS:
            raise ValueError("poll_interval_seconds is out of range")
        target = str(parameters.get("wait_target") or "external")
        if target not in WAIT_TARGET_STATUSES:
            raise ValueError("wait_target is not a supported wait target")
        # Build the condition so malformed predicates fail before dispatch.
        wait_condition_from_parameters(parameters)

    async def gather_facts(self, action: ActionContract) -> dict[str, Any]:
        """Merge the latest facts from every observer; later observers win."""

        facts: dict[str, Any] = {}
        for observer in self.observers:
            try:
                observed = await observer(action)
            except Exception:
                continue
            if isinstance(observed, Mapping):
                facts.update(dict(observed))
        return facts

    async def execute(
        self,
        action: ActionContract,
        observation: ObservationLease,
        resources: ResourceLease,
    ) -> ExecutionOutcome:
        started_at = utc_now()
        del observation
        if resources is not None:
            await resources.ensure_valid()
        try:
            condition = wait_condition_from_parameters(action.parameters)
        except ValueError as exc:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                f"Wait condition is invalid: {exc}",
                source=PerceptionSource.APPLICATION_API,
            ) from exc
        timeout_seconds = min(
            float(action.parameters.get("timeout_seconds", 60.0)),
            float(action.timeout_seconds),
            MAX_WAIT_SECONDS,
        )
        poll_interval = float(action.parameters.get("poll_interval_seconds", 2.0))
        target = str(action.parameters.get("wait_target") or "external")
        status = WAIT_TARGET_STATUSES[target]
        waiting_reason = f"{condition.key} {condition.operator.value}"
        sink = self._status_sink
        if sink is not None:
            try:
                sink.begin_wait(action.task_id, status, waiting_reason=waiting_reason)
            except Exception:
                sink = None
        try:
            result = await self.coordinator.wait_until(
                self._predicate(action, condition),
                timeout_seconds=timeout_seconds,
                min_interval_seconds=poll_interval,
                max_interval_seconds=min(max(poll_interval * 8.0, 5.0), _MAX_POLL_INTERVAL_SECONDS),
                task_id=action.task_id,
                description=waiting_reason,
            )
        finally:
            if sink is not None:
                try:
                    sink.end_wait(action.task_id)
                except Exception:
                    pass
        self._last_result = result
        finished_at = utc_now()
        metadata = wait_summary(result, extra={"condition_key": condition.key})
        if result.outcome is WaitOutcome.RESOLVED:
            return ExecutionOutcome(
                status=ExecutionStatus.SUCCEEDED,
                summary=f"Observed {condition.key} {condition.operator.value} after "
                f"{result.elapsed_seconds:.1f}s "
                f"({result.attempts} checks, {result.event_wakeups} event wake-ups).",
                side_effect_may_have_occurred=False,
                result_metadata=metadata,
                started_at=started_at,
                finished_at=finished_at,
            )
        if result.outcome is WaitOutcome.TIMEOUT:
            return ExecutionOutcome(
                status=ExecutionStatus.FAILED,
                summary=(
                    f"{condition.key} {condition.operator.value} was not observed within "
                    f"{timeout_seconds:.0f}s. No completion is claimed."
                ),
                side_effect_may_have_occurred=False,
                result_metadata=metadata,
                started_at=started_at,
                finished_at=finished_at,
            )
        if result.outcome is WaitOutcome.PREDICATE_UNAVAILABLE:
            return ExecutionOutcome(
                status=ExecutionStatus.UNKNOWN,
                summary="The wait condition could not be observed reliably.",
                side_effect_may_have_occurred=False,
                result_metadata=metadata,
                started_at=started_at,
                finished_at=finished_at,
            )
        # Cancelled: propagate so the task engine records an interrupted wait.
        raise asyncio.CancelledError()

    def _predicate(self, action: ActionContract, condition: Condition) -> Any:
        async def _evaluate() -> bool:
            facts = await self.gather_facts(action)
            return bool(condition.evaluate(facts))

        return _evaluate


def register_environment_wait_tools(
    registry: ToolRegistry,
    observers: Sequence[EnvironmentObserver],
    *,
    coordinator: WaitCoordinator | None = None,
) -> None:
    if not observers:
        return
    registry.register(EnvironmentWaitTool(tuple(observers), coordinator))


__all__ = [
    "EnvironmentObserver",
    "EnvironmentWaitTool",
    "MAX_WAIT_SECONDS",
    "TaskStatusSink",
    "register_environment_wait_tools",
    "wait_condition_from_parameters",
]
