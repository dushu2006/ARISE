"""Ports that isolate the agent runtime from automation/provider implementations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from arise.core.contracts import (
    ActionContract,
    EvidenceSource,
    FrozenJSON,
    Idempotency,
    ObservationLease,
    RiskLevel,
    freeze_json,
    json_byte_size,
    thaw_json,
    utc_now,
    validate_safe_token,
)
from arise.core.resources import ResourceLease


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Trusted, adapter-owned metadata used by policy and scheduling."""

    name: str
    version: str
    description: str
    minimum_risk: RiskLevel
    required_capabilities: frozenset[str] = frozenset()
    required_resources: tuple[str, ...] = ()
    declared_side_effects: tuple[str, ...] = ()
    idempotency: Idempotency = Idempotency.UNKNOWN
    max_result_bytes: int = 65_536
    # Exact accepted `action.parameters` keys. Trusted adapter metadata used to
    # describe the tool to a planner; it never authorizes a parameter by itself.
    parameter_names: tuple[str, ...] = ()
    # Required target scope, e.g. "windows.window_id" or "browser.page_id".
    target_scope: str | None = None

    def __post_init__(self) -> None:
        validate_safe_token(self.name, "tool name")
        validate_safe_token(self.version, "tool version")
        if not isinstance(self.minimum_risk, RiskLevel):
            raise ValueError("minimum_risk must be a RiskLevel")
        if not isinstance(self.idempotency, Idempotency):
            raise ValueError("idempotency must be an Idempotency value")
        if self.max_result_bytes < 1:
            raise ValueError("max_result_bytes must be positive")
        for capability in self.required_capabilities:
            validate_safe_token(capability, "tool capability name")
        resources = tuple(sorted(set(self.required_resources)))
        for resource in resources:
            validate_safe_token(resource, "tool resource name")
        parameter_names = tuple(sorted(set(self.parameter_names)))
        for parameter_name in parameter_names:
            validate_safe_token(parameter_name, "tool parameter name")
        if self.target_scope is not None:
            validate_safe_token(self.target_scope, "tool target scope")
        object.__setattr__(self, "required_capabilities", frozenset(self.required_capabilities))
        object.__setattr__(self, "required_resources", resources)
        object.__setattr__(self, "declared_side_effects", tuple(self.declared_side_effects))
        object.__setattr__(self, "parameter_names", parameter_names)


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    status: ExecutionStatus
    summary: str
    side_effect_may_have_occurred: bool = True
    result_metadata: Mapping[str, FrozenJSON] = field(default_factory=dict)
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExecutionStatus):
            raise ValueError("status must be an ExecutionStatus")
        if not isinstance(self.summary, str) or len(self.summary) > 4096:
            raise ValueError("summary must be text no longer than 4096 characters")
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("execution outcome timestamps must be timezone-aware")
        frozen = freeze_json(self.result_metadata, path="execution.result_metadata")
        if not isinstance(frozen, Mapping):
            raise ValueError("result_metadata must be an object")
        if json_byte_size(frozen) > 1_048_576:
            raise ValueError("result_metadata exceeds the hard size limit")
        object.__setattr__(self, "result_metadata", frozen)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    source: str
    observation_id: str | None
    state_hash: str | None
    statement: str
    captured_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not 1 <= len(self.source) <= 64:
            raise ValueError("evidence source must contain 1 to 64 characters")
        if self.observation_id is not None and (
            not isinstance(self.observation_id, str) or len(self.observation_id) > 128
        ):
            raise ValueError("evidence observation_id must be at most 128 characters")
        if self.state_hash is not None and (
            not isinstance(self.state_hash, str) or len(self.state_hash) > 256
        ):
            raise ValueError("evidence state_hash must be at most 256 characters")
        if not isinstance(self.statement, str) or not 1 <= len(self.statement) <= 4096:
            raise ValueError("evidence statement must contain 1 to 4096 characters")
        if self.captured_at.tzinfo is None:
            raise ValueError("evidence timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: VerificationStatus
    level: int
    summary: str
    evidence: tuple[EvidenceRecord, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.status, VerificationStatus):
            raise ValueError("status must be a VerificationStatus")
        if not isinstance(self.summary, str) or len(self.summary) > 4096:
            raise ValueError("verification summary must be text no longer than 4096 characters")
        if not isinstance(self.level, int) or not 0 <= self.level <= 5:
            raise ValueError("verification level must be an integer between V0 and V5")
        evidence = tuple(self.evidence)
        if len(evidence) > 128 or any(not isinstance(item, EvidenceRecord) for item in evidence):
            raise ValueError("verification evidence must contain at most 128 typed records")
        if self.status is VerificationStatus.PASSED and (
            self.level < 1
            or not any(
                item.source in {EvidenceSource.OBSERVED.value, EvidenceSource.RETRIEVED.value}
                and bool(item.observation_id or item.state_hash)
                and bool(item.statement.strip())
                for item in evidence
            )
        ):
            raise ValueError("passed verification requires grounded, traceable evidence")
        object.__setattr__(self, "evidence", evidence)


class ActionTool(Protocol):
    """Deterministic executor for one registered tool capability."""

    @property
    def spec(self) -> ToolSpec: ...

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None: ...

    async def execute(
        self,
        action: ActionContract,
        observation: ObservationLease,
        resources: ResourceLease,
    ) -> ExecutionOutcome: ...


class DynamicResourceProvider(Protocol):
    """Optional adapter hook for identity-derived, action-specific resource locks."""

    def resources_for(self, action: ActionContract) -> tuple[str, ...]: ...


class EnvironmentPort(Protocol):
    """Read and revalidate current application/desktop state."""

    async def observe(self, action: ActionContract) -> ObservationLease: ...

    async def is_current(self, observation: ObservationLease) -> bool: ...


class VerifierPort(Protocol):
    """Independent post-action verification, not a model self-assessment."""

    async def verify(self, action: ActionContract) -> VerificationResult: ...


class ToolNotFoundError(LookupError):
    pass


class ToolRegistry:
    """In-process registry; production discovery belongs in an adapter layer."""

    def __init__(self) -> None:
        self._tools: dict[str, ActionTool] = {}

    def register(self, tool: ActionTool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> ActionTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(name) from exc

    def list_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec for tool in self._tools.values())


def metadata_to_dict(metadata: Mapping[str, FrozenJSON]) -> dict[str, Any]:
    """Convert bounded tool metadata for storage/telemetry adapters."""

    return thaw_json(metadata)
