"""Centralized risk policy and one-action, expiring human approvals."""

from __future__ import annotations

import hashlib
import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from arise.core.contracts import ActionContract, RiskLevel, TrustLevel, canonical_json, utc_now
from arise.core.ports import ToolSpec


class PolicyDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """Opaque, expiring authorization for one exact action contract."""

    token_id: str
    task_id: str
    action_id: str
    action_fingerprint: str
    approved_by: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not all(
            (self.token_id, self.task_id, self.action_id, self.action_fingerprint, self.approved_by)
        ):
            raise ValueError("approval fields cannot be empty")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("approval timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("approval expiry must follow issue time")


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    kind: PolicyDecisionKind
    effective_risk: RiskLevel
    reason: str
    confirmation_required: bool = False


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    auto_execute_through: RiskLevel = RiskLevel.R1
    confirmation_threshold: RiskLevel = RiskLevel.R2
    allow_privileged_actions: bool = False
    minimum_observation_confidence: float = 0.80
    maximum_approval_seconds: float = 120.0
    blocked_tools: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.minimum_observation_confidence)
            or not 0.0 <= self.minimum_observation_confidence <= 1.0
        ):
            raise ValueError("minimum_observation_confidence must be finite and in [0, 1]")
        if not isinstance(self.auto_execute_through, RiskLevel):
            raise ValueError("auto_execute_through must be a RiskLevel")
        if not isinstance(self.confirmation_threshold, RiskLevel):
            raise ValueError("confirmation_threshold must be a RiskLevel")
        if self.auto_execute_through > RiskLevel.R2:
            raise ValueError("automatic execution cannot be configured above R2")
        if self.confirmation_threshold < RiskLevel.R2:
            raise ValueError("confirmation threshold cannot be lower than R2")
        if not math.isfinite(self.maximum_approval_seconds) or self.maximum_approval_seconds <= 0:
            raise ValueError("maximum_approval_seconds must be positive")
        object.__setattr__(self, "blocked_tools", frozenset(self.blocked_tools))


class PolicyEngine:
    """Code-enforced policy. Model-provided risk can only be raised by tool metadata."""

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()
        self._lock = threading.RLock()
        self._issued: dict[str, ApprovalGrant] = {}

    @staticmethod
    def _effective_risk(action: ActionContract, tool: ToolSpec) -> RiskLevel:
        return RiskLevel(max(int(action.risk), int(tool.minimum_risk)))

    @staticmethod
    def _fingerprint(action: ActionContract, tool: ToolSpec, risk: RiskLevel) -> str:
        payload = action.approval_payload(risk, tool.version)
        payload["tool_policy"] = {
            "minimum_risk": int(tool.minimum_risk),
            "required_capabilities": sorted(tool.required_capabilities),
            "required_resources": list(tool.required_resources),
            "declared_side_effects": list(tool.declared_side_effects),
            "idempotency": tool.idempotency.value,
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def contract_fingerprint(self, action: ActionContract, tool: ToolSpec) -> str:
        """Return the current exact action+trusted-tool-policy digest."""

        return self._fingerprint(action, tool, self._effective_risk(action, tool))

    def evaluate(
        self,
        action: ActionContract,
        tool: ToolSpec,
        *,
        approval: ApprovalGrant | None = None,
        now: datetime | None = None,
    ) -> PolicyDecision:
        now = now or utc_now()
        if now.tzinfo is None:
            raise ValueError("policy evaluation time must be timezone-aware")
        with self._lock:
            expired = [
                token_id for token_id, grant in self._issued.items() if grant.expires_at <= now
            ]
            for token_id in expired:
                self._issued.pop(token_id, None)
        effective_risk = self._effective_risk(action, tool)

        if action.tool_name in self.config.blocked_tools:
            return PolicyDecision(
                PolicyDecisionKind.DENY, effective_risk, "tool is disabled by policy"
            )

        if tool.name != action.tool_name:
            return PolicyDecision(PolicyDecisionKind.DENY, effective_risk, "tool identity mismatch")

        if effective_risk > RiskLevel.R0 and action.authority.trust in {
            TrustLevel.MODEL_PROPOSAL,
            TrustLevel.UNTRUSTED_EXTERNAL,
            TrustLevel.VERIFIED_STATE,
        }:
            return PolicyDecision(
                PolicyDecisionKind.DENY,
                effective_risk,
                "untrusted or inferred content cannot authorize a side effect",
            )

        if effective_risk > RiskLevel.R0 and action.authority.trust is not TrustLevel.SYSTEM_POLICY:
            if action.authority.trust is not TrustLevel.USER_INSTRUCTION:
                return PolicyDecision(
                    PolicyDecisionKind.DENY,
                    effective_risk,
                    "a current user instruction is required for this action",
                )
            if not action.authority.user_intent_id:
                return PolicyDecision(
                    PolicyDecisionKind.DENY,
                    effective_risk,
                    "action is not linked to an explicit user intent",
                )

        missing_capabilities = tool.required_capabilities - action.authority.capabilities
        if missing_capabilities:
            names = ", ".join(sorted(missing_capabilities))
            return PolicyDecision(
                PolicyDecisionKind.DENY,
                effective_risk,
                f"required capability was not delegated: {names}",
            )

        if effective_risk >= RiskLevel.R2:
            if action.target is None or not action.target.has_semantic_anchor:
                return PolicyDecision(
                    PolicyDecisionKind.DENY,
                    effective_risk,
                    "consequential actions require a semantically identified target",
                )
            if not action.postconditions:
                return PolicyDecision(
                    PolicyDecisionKind.DENY,
                    effective_risk,
                    "consequential actions require explicit postconditions",
                )

        if effective_risk is RiskLevel.R4 and not self.config.allow_privileged_actions:
            return PolicyDecision(
                PolicyDecisionKind.DENY,
                effective_risk,
                "privileged/destructive actions are disabled by the active policy",
            )

        # External commitments and privileged actions always require explicit
        # action-scoped confirmation. User tuning may never make R3/R4 automatic.
        always_confirm = effective_risk >= RiskLevel.R3
        if not always_confirm and effective_risk <= self.config.auto_execute_through:
            return PolicyDecision(
                PolicyDecisionKind.ALLOW, effective_risk, "within automatic risk limit"
            )

        if not always_confirm and effective_risk < self.config.confirmation_threshold:
            return PolicyDecision(
                PolicyDecisionKind.ALLOW, effective_risk, "within configured risk limit"
            )

        if approval is not None and self._approval_is_valid(
            action, tool, effective_risk, approval, now
        ):
            return PolicyDecision(
                PolicyDecisionKind.ALLOW,
                effective_risk,
                "scoped user approval is valid",
                confirmation_required=True,
            )

        return PolicyDecision(
            PolicyDecisionKind.CONFIRM,
            effective_risk,
            "this action requires fresh, action-scoped user confirmation",
            confirmation_required=True,
        )

    def issue_approval(
        self,
        action: ActionContract,
        tool: ToolSpec,
        *,
        approved_by: str,
        ttl_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> ApprovalGrant:
        """Record a confirmation after an authenticated human approval surface.

        The UI/API that calls this method must authenticate the human and display
        the exact action scope. This method is not an LLM tool and must not be
        exposed directly to untrusted clients.
        """

        if not approved_by.strip():
            raise ValueError("approval requires an authenticated principal")
        if (
            action.authority.principal_id is not None
            and approved_by != action.authority.principal_id
        ):
            raise PermissionError("approval principal does not match the task owner")
        if (
            not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
            or ttl_seconds > self.config.maximum_approval_seconds
        ):
            raise ValueError("approval TTL is outside policy limits")
        now = now or utc_now()
        if now.tzinfo is None:
            raise ValueError("approval time must be timezone-aware")
        preliminary = self.evaluate(action, tool, now=now)
        if preliminary.kind is PolicyDecisionKind.DENY:
            raise PermissionError(preliminary.reason)
        if not preliminary.confirmation_required:
            raise ValueError("this action does not require a confirmation token")
        grant = ApprovalGrant(
            token_id=str(uuid.uuid4()),
            task_id=action.task_id,
            action_id=action.action_id,
            action_fingerprint=self._fingerprint(action, tool, preliminary.effective_risk),
            approved_by=approved_by,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        with self._lock:
            self._issued[grant.token_id] = grant
        return grant

    def consume_approval(
        self,
        action: ActionContract,
        tool: ToolSpec,
        approval: ApprovalGrant | None,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Consume a grant immediately before dispatch; each grant works once."""

        risk = self._effective_risk(action, tool)
        requires_confirmation = risk >= RiskLevel.R3 or (
            risk >= self.config.confirmation_threshold and risk > self.config.auto_execute_through
        )
        if approval is None:
            decision = self.evaluate(action, tool, now=now)
            return decision.kind is PolicyDecisionKind.ALLOW and not requires_confirmation
        now = now or utc_now()
        if now.tzinfo is None:
            raise ValueError("approval consumption time must be timezone-aware")
        if not requires_confirmation:
            return False
        with self._lock:
            if not self._approval_is_valid(action, tool, risk, approval, now):
                return False
            self._issued.pop(approval.token_id, None)
            return True

    def _approval_is_valid(
        self,
        action: ActionContract,
        tool: ToolSpec,
        risk: RiskLevel,
        approval: ApprovalGrant,
        now: datetime,
    ) -> bool:
        if approval.task_id != action.task_id or approval.action_id != action.action_id:
            return False
        if approval.expires_at <= now or approval.issued_at > now:
            return False
        if approval.action_fingerprint != self._fingerprint(action, tool, risk):
            return False
        with self._lock:
            stored = self._issued.get(approval.token_id)
            return stored == approval
