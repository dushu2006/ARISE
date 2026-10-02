"""Failure-injectable computer simulator for tests and demos only.

This module performs no host I/O and is never registered by the production API.
It models observations and action outcomes so the guarded runtime can be tested
without pretending that Linux or mocks are Windows execution.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from typing import Any

from arise.core.computer import (
    ComputerFailureCode,
    PerceptionSource,
    TargetCandidate,
    TargetDescriptor,
    TargetQuery,
    TargetResolution,
)
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import (
    ActionContract,
    EvidenceSource,
    Idempotency,
    ObservationLease,
    RiskLevel,
    canonical_json,
    freeze_json,
    thaw_json,
    utc_now,
)
from arise.core.grounding import TargetResolver
from arise.core.ports import (
    EnvironmentPort,
    ExecutionOutcome,
    ExecutionStatus,
    ToolSpec,
)
from arise.core.resources import ResourceLease


class SimulatorFailure(StrEnum):
    TARGET_DISAPPEARS = "target_disappears"
    TARGET_MOVES = "target_moves"
    USER_INTERFERENCE = "user_interference"
    TIMEOUT_AFTER_DISPATCH = "timeout_after_dispatch"
    UNKNOWN_AFTER_DISPATCH = "unknown_after_dispatch"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"


class SimulatedComputerEnvironment(EnvironmentPort):
    """Mutable, explicit test fixture whose every mutation changes its state hash."""

    def __init__(
        self,
        candidates: tuple[TargetCandidate, ...] = (),
        *,
        lease_seconds: float = 2.0,
        facts: Mapping[str, Any] | None = None,
    ) -> None:
        if not 0.05 <= lease_seconds <= 60:
            raise ValueError("simulator lease_seconds must be between 0.05 and 60")
        self.lease_seconds = lease_seconds
        self._facts = dict(freeze_json(facts or {}, path="simulator.facts"))
        self._candidates = list(candidates)
        self._generation = 0
        self._user_interference = False
        self._lock = asyncio.Lock()
        self._resolver = TargetResolver()

    async def observe(self, action: ActionContract) -> ObservationLease:
        async with self._lock:
            observation_id = str(uuid.uuid4())
            now = utc_now()
            deadline = time.monotonic() + self.lease_seconds
            target_fingerprint = action.target.fingerprint if action.target else None
            facts = self._facts_for(action.target.fingerprint if action.target else None)
            state_hash = self._state_hash(facts)
            return ObservationLease(
                lease_id=observation_id,
                target_fingerprint=target_fingerprint,
                state_hash=state_hash,
                created_at=now,
                expires_at=now + timedelta(seconds=self.lease_seconds),
                monotonic_deadline=deadline,
                facts=facts,
                source=EvidenceSource.OBSERVED,
                confidence=1.0,
            )

    async def is_current(self, observation: ObservationLease) -> bool:
        async with self._lock:
            if not observation.is_valid(monotonic_now=time.monotonic()):
                return False
            current = self._facts_for(observation.target_fingerprint)
            return observation.state_hash == self._state_hash(current)

    async def resolve(self, query: TargetQuery) -> TargetResolution:
        async with self._lock:
            return self._resolver.resolve(query, tuple(self._candidates))

    async def remove_target(self, fingerprint: str) -> None:
        async with self._lock:
            self._candidates = [
                candidate
                for candidate in self._candidates
                if candidate.descriptor.identity.fingerprint != fingerprint
            ]
            self._generation += 1

    async def move_target(self, fingerprint: str, bounds) -> None:
        async with self._lock:
            moved: list[TargetCandidate] = []
            found = False
            for candidate in self._candidates:
                if candidate.descriptor.identity.fingerprint != fingerprint:
                    moved.append(candidate)
                    continue
                found = True
                descriptor = candidate.descriptor
                moved.append(
                    TargetCandidate(
                        descriptor=TargetDescriptor(
                            identity=descriptor.identity,
                            source=descriptor.source,
                            observed_at=utc_now(),
                            observation_id=str(uuid.uuid4()),
                            bounds=bounds,
                            coordinate_space=descriptor.coordinate_space,
                            selector_quality=descriptor.selector_quality,
                            visible=descriptor.visible,
                            enabled=descriptor.enabled,
                            automation_id=descriptor.automation_id,
                            runtime_id=descriptor.runtime_id,
                            hierarchy=descriptor.hierarchy,
                            class_name=descriptor.class_name,
                            framework_id=descriptor.framework_id,
                        ),
                        confidence=candidate.confidence,
                        evidence=candidate.evidence,
                    )
                )
            if not found:
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_NOT_FOUND,
                    "The simulated target no longer exists.",
                )
            self._candidates = moved
            self._generation += 1

    async def mark_user_interference(self) -> None:
        async with self._lock:
            self._user_interference = True
            self._generation += 1

    async def clear_user_interference(self) -> None:
        async with self._lock:
            self._user_interference = False
            self._generation += 1

    async def set_fact(self, key: str, value: Any) -> None:
        frozen = freeze_json(value, path=f"simulator.facts.{key}")
        async with self._lock:
            self._facts[key] = frozen
            self._generation += 1

    async def _snapshot(self) -> tuple[list[TargetCandidate], dict[str, Any]]:
        async with self._lock:
            return list(self._candidates), dict(self._facts)

    def _facts_for(self, target_fingerprint: str | None) -> dict[str, Any]:
        target_present = target_fingerprint is None or any(
            candidate.descriptor.identity.fingerprint == target_fingerprint
            and candidate.descriptor.source is not PerceptionSource.COORDINATE
            for candidate in self._candidates
        )
        return {
            **thaw_json(self._facts),
            "computer.generation": self._generation,
            "computer.target_present": target_present,
            "computer.user_interference": self._user_interference,
            "computer.target_fingerprints": [
                candidate.descriptor.identity.fingerprint for candidate in self._candidates
            ],
        }

    def _state_hash(self, facts: Mapping[str, Any]) -> str:
        serialized = canonical_json(
            {
                "generation": self._generation,
                "facts": thaw_json(facts),
                "candidates": [
                    {
                        "fingerprint": item.descriptor.identity.fingerprint,
                        "bounds": (
                            None
                            if item.descriptor.bounds is None
                            else [
                                item.descriptor.bounds.x,
                                item.descriptor.bounds.y,
                                item.descriptor.bounds.width,
                                item.descriptor.bounds.height,
                            ]
                        ),
                        "visible": item.descriptor.visible,
                        "enabled": item.descriptor.enabled,
                    }
                    for item in self._candidates
                ],
            }
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SimulatedComputerTool:
    """One guarded simulated action with dispatch/unknown-outcome injection."""

    def __init__(
        self,
        environment: SimulatedComputerEnvironment,
        *,
        failure: SimulatorFailure | None = None,
    ) -> None:
        self.environment = environment
        self.failure = failure
        self.dispatched_action_ids: set[str] = set()
        self._spec = ToolSpec(
            name="simulator.computer_action",
            version="1.0.0",
            description="Perform an explicitly simulated computer interaction.",
            minimum_risk=RiskLevel.R1,
            required_capabilities=frozenset({"simulator.computer"}),
            required_resources=("simulator.desktop", "simulator.pointer", "simulator.keyboard"),
            declared_side_effects=("mutates isolated simulator state only",),
            idempotency=Idempotency.UNKNOWN,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        operation = parameters.get("operation")
        if operation not in {"click", "type_text", "focus", "navigate", "observe"}:
            raise ValueError("unsupported simulated computer operation")

    async def execute(
        self,
        action: ActionContract,
        observation: ObservationLease,
        resources: ResourceLease,
    ) -> ExecutionOutcome:
        await resources.ensure_valid()
        if not await self.environment.is_current(observation):
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "The simulated observation expired or the computer state changed.",
            )
        if action.action_id in self.dispatched_action_ids:
            return ExecutionOutcome(
                status=ExecutionStatus.UNKNOWN,
                summary="This simulated action ID was already dispatched.",
                side_effect_may_have_occurred=True,
            )
        target = action.target
        if target is not None and not any(
            candidate.descriptor.identity.fingerprint == target.fingerprint
            for candidate in (await self.environment._snapshot())[0]
        ):
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_NOT_FOUND,
                "The simulated target disappeared before dispatch.",
            )

        if self.failure is SimulatorFailure.TARGET_DISAPPEARS and target is not None:
            await self.environment.remove_target(target.fingerprint)
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "The simulated target disappeared before dispatch.",
            )
        if self.failure is SimulatorFailure.TARGET_MOVES and target is not None:
            candidates, _ = await self.environment._snapshot()
            current = next(
                candidate
                for candidate in candidates
                if candidate.descriptor.identity.fingerprint == target.fingerprint
            )
            bounds = current.descriptor.bounds
            if bounds is not None:
                from arise.core.computer import Rect

                await self.environment.move_target(
                    target.fingerprint,
                    Rect(bounds.x + 120, bounds.y + 70, bounds.width, bounds.height),
                )
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "The simulated target moved before dispatch.",
            )
        if self.failure is SimulatorFailure.USER_INTERFERENCE:
            await self.environment.mark_user_interference()
            raise ComputerAdapterError(
                ComputerFailureCode.USER_INTERFERENCE,
                "Simulated user input invalidated the observation.",
            )
        if self.failure is SimulatorFailure.ADAPTER_UNAVAILABLE:
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "The simulator injected an unavailable adapter.",
            )

        self.dispatched_action_ids.add(action.action_id)
        started_at = utc_now()
        operation = str(action.parameters.get("operation", "observe"))
        if self.failure is not SimulatorFailure.VERIFICATION_UNAVAILABLE:
            await self.environment.set_fact(f"simulator.action.{action.action_id}.dispatched", True)
            await self.environment.set_fact("simulator.last_operation", operation)

        if self.failure is SimulatorFailure.TIMEOUT_AFTER_DISPATCH:
            await asyncio.Event().wait()
        if self.failure is SimulatorFailure.UNKNOWN_AFTER_DISPATCH:
            return ExecutionOutcome(
                status=ExecutionStatus.UNKNOWN,
                summary="Dispatch occurred; simulator cannot establish the external outcome.",
                side_effect_may_have_occurred=True,
                started_at=started_at,
                finished_at=utc_now(),
            )
        return ExecutionOutcome(
            status=ExecutionStatus.SUCCEEDED,
            summary="Simulated interaction dispatched; independent verification is still required.",
            side_effect_may_have_occurred=True,
            result_metadata={"operation": operation},
            started_at=started_at,
            finished_at=utc_now(),
        )
