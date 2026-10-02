"""Deterministic in-memory adapters for tests, demos, and replay fixtures only."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from arise.core.contracts import (
    ActionContract,
    EvidenceSource,
    Idempotency,
    ObservationLease,
    RiskLevel,
    TargetIdentity,
    canonical_json,
    freeze_json,
    thaw_json,
    utc_now,
)
from arise.core.ports import (
    ExecutionOutcome,
    ExecutionStatus,
    ToolSpec,
)
from arise.core.resources import ResourceLease


class InMemoryEnvironment:
    """Mutable test environment with state-hash based observation leases."""

    def __init__(
        self,
        facts: Mapping[str, Any] | None = None,
        *,
        target: TargetIdentity | None = None,
        lease_seconds: float = 5.0,
        confidence: float = 1.0,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._facts = dict(freeze_json(facts or {}, path="in_memory.facts"))
        self.target = target
        self.lease_seconds = lease_seconds
        self.confidence = confidence
        self._lock = asyncio.Lock()

    async def observe(self, action: ActionContract) -> ObservationLease:
        del action  # The adapter observes its own state; it does not echo a proposal.
        async with self._lock:
            now = utc_now()
            monotonic_now = time.monotonic()
            facts = dict(self._facts)
            target_fingerprint = self.target.fingerprint if self.target is not None else None
            state_hash = self._state_hash(facts, target_fingerprint)
            return ObservationLease(
                lease_id=str(uuid.uuid4()),
                target_fingerprint=target_fingerprint,
                state_hash=state_hash,
                created_at=now,
                expires_at=now + timedelta(seconds=self.lease_seconds),
                monotonic_deadline=monotonic_now + self.lease_seconds,
                facts=facts,
                source=EvidenceSource.OBSERVED,
                confidence=self.confidence,
            )

    async def is_current(self, observation: ObservationLease) -> bool:
        async with self._lock:
            if time.monotonic() >= observation.monotonic_deadline:
                return False
            target_fingerprint = self.target.fingerprint if self.target is not None else None
            return observation.state_hash == self._state_hash(self._facts, target_fingerprint)

    async def set_fact(self, key: str, value: Any) -> None:
        if not key:
            raise ValueError("fact key cannot be empty")
        safe_value = freeze_json(value, path=f"fact.{key}")
        async with self._lock:
            self._facts[key] = safe_value

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return thaw_json(self._facts)

    @staticmethod
    def _state_hash(facts: Mapping[str, Any], target_fingerprint: str | None) -> str:
        serialized = canonical_json(
            {"facts": thaw_json(facts), "target_fingerprint": target_fingerprint}
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SetFactTool:
    """A deliberately simulated state-writing tool; never touches the host OS."""

    def __init__(self, environment: InMemoryEnvironment) -> None:
        self.environment = environment
        self._spec = ToolSpec(
            name="simulator.set_fact",
            version="1.0.0",
            description="Set one fact in the in-memory test environment.",
            minimum_risk=RiskLevel.R1,
            required_capabilities=frozenset({"simulator.write"}),
            required_resources=("simulator-state",),
            declared_side_effects=("mutates in-memory simulator state",),
            idempotency=Idempotency.IDEMPOTENT,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        if not isinstance(parameters.get("key"), str) or not parameters["key"].strip():
            raise ValueError("'key' must be a non-empty string")
        if "value" not in parameters:
            raise ValueError("'value' is required")

    async def execute(
        self,
        action: ActionContract,
        observation: ObservationLease,
        resources: ResourceLease,
    ) -> ExecutionOutcome:
        del observation
        started_at = utc_now()
        await resources.ensure_valid()
        key = str(action.parameters["key"])
        value = thaw_json(action.parameters["value"])
        await self.environment.set_fact(key, value)
        return ExecutionOutcome(
            status=ExecutionStatus.SUCCEEDED,
            summary="Updated simulated state.",
            side_effect_may_have_occurred=True,
            result_metadata={"changed_key": key},
            started_at=started_at,
            finished_at=utc_now(),
        )
