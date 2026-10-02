"""Versioned, provider-neutral contracts used by the ARISE runtime.

These types deliberately describe *proposals and evidence*, not OS operations.
An adapter is required to turn a validated action into an effect.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Any, TypeAlias


class ContractValidationError(ValueError):
    """Raised when an untrusted or malformed contract is rejected."""


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def validate_safe_token(value: str, label: str = "identifier") -> None:
    """Validate short data identifiers before they enter resource/event labels."""

    if not isinstance(value, str) or _SAFE_TOKEN.fullmatch(value) is None:
        raise ContractValidationError(f"{label} must be a bounded identifier")


class RiskLevel(IntEnum):
    """Action risk, ordered so policy can apply conservative floors."""

    R0 = 0  # observation
    R1 = 1  # harmless, local action
    R2 = 2  # reversible modification
    R3 = 3  # external side effect / commitment
    R4 = 4  # destructive, financial, privileged, or security-critical


class Idempotency(StrEnum):
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"
    UNKNOWN = "unknown"


class TrustLevel(StrEnum):
    """Provenance of the authority behind an action (not its evidence)."""

    SYSTEM_POLICY = "system_policy"
    USER_INSTRUCTION = "user_instruction"
    VERIFIED_STATE = "verified_state"
    MODEL_PROPOSAL = "model_proposal"
    UNTRUSTED_EXTERNAL = "untrusted_external"


class EvidenceSource(StrEnum):
    OBSERVED = "observed"
    RETRIEVED = "retrieved"
    USER_PROVIDED = "user_provided"
    MODEL_INFERRED = "model_inferred"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"


class ConditionOperator(StrEnum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    EXISTS = "exists"
    CONTAINS = "contains"


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A reference to a secret, never the secret value itself."""

    name: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ContractValidationError("secret reference name cannot be empty")


JSONScalar: TypeAlias = str | int | float | bool | None
FrozenJSON: TypeAlias = (
    JSONScalar | SecretRef | tuple["FrozenJSON", ...] | Mapping[str, "FrozenJSON"]
)


def freeze_json(value: Any, *, path: str = "value") -> FrozenJSON:
    """Validate and defensively freeze JSON-like data; bytes/objects are rejected."""

    if isinstance(value, SecretRef):
        return value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJSON] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractValidationError(f"{path} contains a non-string key")
            frozen[key] = freeze_json(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item, path=f"{path}[]") for item in value)
    raise ContractValidationError(f"{path} contains unsupported type {type(value).__name__}")


def thaw_json(value: Any) -> Any:
    """Return ordinary JSON-compatible containers for hashing/storage."""

    if isinstance(value, SecretRef):
        return {"$secret_ref": value.name}
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_json(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(thaw_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_byte_size(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    """Authority copied from the trusted request boundary, not model output."""

    principal_id: str | None
    user_intent_id: str | None
    trust: TrustLevel = TrustLevel.USER_INSTRUCTION
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.trust, TrustLevel):
            raise ContractValidationError("trust must be a TrustLevel")
        if self.principal_id is not None and not self.principal_id.strip():
            raise ContractValidationError("principal_id cannot be blank")
        if self.user_intent_id is not None and not self.user_intent_id.strip():
            raise ContractValidationError("user_intent_id cannot be blank")
        for capability in self.capabilities:
            validate_safe_token(capability, "capability name")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "user_intent_id": self.user_intent_id,
            "trust": self.trust.value,
            "capabilities": sorted(self.capabilities),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AuthorizationContext:
        return cls(
            principal_id=data.get("principal_id"),
            user_intent_id=data.get("user_intent_id"),
            trust=TrustLevel(data.get("trust", TrustLevel.UNTRUSTED_EXTERNAL.value)),
            capabilities=frozenset(data.get("capabilities", [])),
        )


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    """Logical target identity. Geometry is optional, ephemeral fallback data."""

    platform: str
    application: str | None = None
    process_id: int | None = None
    window_id: str | None = None
    browser_profile: str | None = None
    page_id: str | None = None
    account: str | None = None
    workspace: str | None = None
    container: str | None = None
    object_id: str | None = None
    role: str | None = None
    semantic_name: str | None = None
    stable_id: str | None = None
    locator: Mapping[str, FrozenJSON] = field(default_factory=dict)
    display_id: str | None = None
    bounds: tuple[float, float, float, float] | None = None
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.platform.strip():
            raise ContractValidationError("target platform cannot be blank")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ContractValidationError("target confidence must be in [0, 1]")
        if self.process_id is not None and self.process_id <= 0:
            raise ContractValidationError("process_id must be positive")
        if self.bounds is not None:
            if len(self.bounds) != 4 or not all(math.isfinite(value) for value in self.bounds):
                raise ContractValidationError("bounds must contain four finite numbers")
            object.__setattr__(self, "bounds", tuple(float(value) for value in self.bounds))
        frozen_locator = freeze_json(self.locator, path="target.locator")
        if not isinstance(frozen_locator, Mapping):
            raise ContractValidationError("target locator must be an object")
        if json_byte_size(frozen_locator) > 16_384:
            raise ContractValidationError("target locator exceeds the size limit")
        object.__setattr__(self, "locator", frozen_locator)

    @property
    def has_semantic_anchor(self) -> bool:
        return any(
            value
            for value in (
                self.object_id,
                self.stable_id,
                self.semantic_name,
                self.window_id,
                self.page_id,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "application": self.application,
            "process_id": self.process_id,
            "window_id": self.window_id,
            "browser_profile": self.browser_profile,
            "page_id": self.page_id,
            "account": self.account,
            "workspace": self.workspace,
            "container": self.container,
            "object_id": self.object_id,
            "role": self.role,
            "semantic_name": self.semantic_name,
            "stable_id": self.stable_id,
            "locator": thaw_json(self.locator),
            "display_id": self.display_id,
            "bounds": list(self.bounds) if self.bounds is not None else None,
            "confidence": self.confidence,
        }

    @property
    def fingerprint(self) -> str:
        """Fingerprint stable logical identity, not ephemeral screen geometry."""

        logical_identity = self.to_dict()
        for transient in ("bounds", "confidence", "display_id"):
            logical_identity.pop(transient, None)
        raw = canonical_json(logical_identity).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class Condition:
    """A small, deterministic fact predicate; never executable model code."""

    key: str
    operator: ConditionOperator = ConditionOperator.EQUALS
    expected: FrozenJSON = None
    description: str = ""

    def __post_init__(self) -> None:
        validate_safe_token(self.key, "condition key")
        if not isinstance(self.operator, ConditionOperator):
            raise ContractValidationError("operator must be a ConditionOperator")
        frozen_expected = freeze_json(self.expected, path=f"condition.{self.key}")
        if json_byte_size(frozen_expected) > 16_384:
            raise ContractValidationError("condition expectation exceeds the size limit")
        object.__setattr__(self, "expected", frozen_expected)
        if self.operator is ConditionOperator.EXISTS and self.expected is not None:
            raise ContractValidationError("EXISTS conditions do not take an expected value")

    def evaluate(self, facts: Mapping[str, Any]) -> bool | None:
        """Return True/False, or None when the observation lacks the fact."""

        present = self.key in facts
        if self.operator is ConditionOperator.EXISTS:
            return present
        if not present:
            return None
        actual = facts[self.key]
        expected = thaw_json(self.expected)
        actual_json = thaw_json(actual)
        if self.operator is ConditionOperator.EQUALS:
            return actual_json == expected
        if self.operator is ConditionOperator.NOT_EQUALS:
            return actual_json != expected
        if self.operator is ConditionOperator.CONTAINS:
            try:
                return expected in actual_json
            except TypeError:
                return False
        raise ContractValidationError(f"unsupported condition operator: {self.operator}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "operator": self.operator.value,
            "expected": thaw_json(self.expected),
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class ActionContract:
    """Typed action proposal. It is inert until policy and runtime authorize it."""

    task_id: str
    tool_name: str
    target: TargetIdentity | None
    risk: RiskLevel
    authority: AuthorizationContext
    parameters: Mapping[str, FrozenJSON] = field(default_factory=dict)
    preconditions: tuple[Condition, ...] = ()
    postconditions: tuple[Condition, ...] = ()
    required_resources: tuple[str, ...] = ()
    idempotency: Idempotency = Idempotency.UNKNOWN
    idempotency_key: str | None = None
    timeout_seconds: float = 30.0
    rollback_strategy: str | None = None
    verification_strategy: str = "observed_postconditions"
    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: int = 1

    def __post_init__(self) -> None:
        validate_safe_token(self.task_id, "task_id")
        validate_safe_token(self.tool_name, "tool_name")
        validate_safe_token(self.action_id, "action_id")
        if not isinstance(self.risk, RiskLevel):
            raise ContractValidationError("risk must be a RiskLevel")
        if not isinstance(self.authority, AuthorizationContext):
            raise ContractValidationError("authority must be an AuthorizationContext")
        if not isinstance(self.idempotency, Idempotency):
            raise ContractValidationError("idempotency must be an Idempotency value")
        if any(
            not isinstance(item, Condition) for item in (*self.preconditions, *self.postconditions)
        ):
            raise ContractValidationError("preconditions and postconditions must be Conditions")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ContractValidationError("timeout_seconds must be positive and finite")
        if self.timeout_seconds > 3600:
            raise ContractValidationError("timeout_seconds cannot exceed one hour")
        if self.schema_version < 1:
            raise ContractValidationError("schema_version must be positive")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ContractValidationError("idempotency_key cannot be blank")
        if self.target is not None and not isinstance(self.target, TargetIdentity):
            raise ContractValidationError("target must be a TargetIdentity or None")
        parameters = freeze_json(self.parameters, path="action.parameters")
        if not isinstance(parameters, Mapping):
            raise ContractValidationError("action.parameters must be an object")
        if json_byte_size(parameters) > 65_536:
            raise ContractValidationError("action parameters exceed the size limit")
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "preconditions", tuple(self.preconditions))
        object.__setattr__(self, "postconditions", tuple(self.postconditions))
        if len(self.preconditions) > 64 or len(self.postconditions) > 64:
            raise ContractValidationError("action condition count exceeds the limit")
        resources = tuple(sorted(set(self.required_resources)))
        for resource in resources:
            validate_safe_token(resource, "resource name")
        object.__setattr__(self, "required_resources", resources)

    def approval_payload(self, effective_risk: RiskLevel, tool_version: str) -> dict[str, Any]:
        """Canonical, exact scope for a one-action human approval."""

        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "action_id": self.action_id,
            "tool_name": self.tool_name,
            "tool_version": tool_version,
            "target": self.target.to_dict() if self.target is not None else None,
            "parameters": thaw_json(self.parameters),
            "risk": int(self.risk),
            "effective_risk": int(effective_risk),
            "authority": self.authority.to_dict(),
            "preconditions": [item.to_dict() for item in self.preconditions],
            "postconditions": [item.to_dict() for item in self.postconditions],
            "required_resources": list(self.required_resources),
            "idempotency": self.idempotency.value,
            "idempotency_key": self.idempotency_key,
            "timeout_seconds": self.timeout_seconds,
            "rollback_strategy": self.rollback_strategy,
            "verification_strategy": self.verification_strategy,
        }

    def approval_fingerprint(self, effective_risk: RiskLevel, tool_version: str) -> str:
        raw = canonical_json(self.approval_payload(effective_risk, tool_version)).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservationLease:
    """Short-lived environment evidence. Never reuse after expiry or state drift."""

    lease_id: str
    target_fingerprint: str | None
    state_hash: str
    created_at: datetime
    expires_at: datetime
    monotonic_deadline: float
    facts: Mapping[str, FrozenJSON]
    source: EvidenceSource = EvidenceSource.OBSERVED
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.lease_id or not self.state_hash:
            raise ContractValidationError("observation lease requires an id and state hash")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ContractValidationError("observation timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ContractValidationError("observation expiry must follow its creation")
        if not math.isfinite(self.monotonic_deadline):
            raise ContractValidationError("monotonic_deadline must be finite")
        if not isinstance(self.source, EvidenceSource):
            raise ContractValidationError("observation source must be an EvidenceSource")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ContractValidationError("observation confidence must be in [0, 1]")
        frozen = freeze_json(self.facts, path="observation.facts")
        if not isinstance(frozen, Mapping):
            raise ContractValidationError("observation facts must be an object")
        if json_byte_size(frozen) > 262_144:
            raise ContractValidationError("observation facts exceed the size limit")
        object.__setattr__(self, "facts", frozen)

    def is_valid(self, *, monotonic_now: float) -> bool:
        return monotonic_now < self.monotonic_deadline
