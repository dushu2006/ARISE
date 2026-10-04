"""Planner port and planner-specific domain errors.

An ``InvalidPlan`` always carries a bounded, sanitized failure category so that
task status reasons and audit events can explain *which* contract rule rejected
a model proposal without ever persisting model output, prompts, or credentials.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from arise.core.contracts import ContractValidationError, validate_safe_token
from arise.core.models import TaskPlan, UserRequest
from arise.core.tasks import TaskRecord

MAX_PLAN_DIAGNOSTIC_LENGTH = 512


class PlanFailureCategory(StrEnum):
    """Bounded, sanitized categories for a rejected planner proposal."""

    # Planner/model-output contract failures.
    RESPONSE_NOT_JSON = "response_not_json"
    RESPONSE_NOT_OBJECT = "response_not_object"
    RESPONSE_TOO_LARGE = "response_too_large"
    SCHEMA_INVALID = "schema_invalid"

    # Task-engine structural validation failures.
    UNTYPED_PLAN = "untyped_plan"
    TASK_MISMATCH = "task_mismatch"
    GOAL_MISMATCH = "goal_mismatch"
    STEP_LIMIT_EXCEEDED = "step_limit_exceeded"
    MISSING_AUTHORITY = "missing_authority"
    DUPLICATE_ACTION_ID = "duplicate_action_id"
    DUPLICATE_STEP_ID = "duplicate_step_id"
    STEP_SELF_DEPENDENCY = "step_self_dependency"
    UNKNOWN_DEPENDENCY = "unknown_dependency"
    DEPENDENCY_CYCLE = "dependency_cycle"


class PlannerUnavailable(RuntimeError):
    """Raised when no planning model or provider is available."""


class InvalidPlan(ValueError):
    """Raised when a planner returns a malformed, stale, or unsafe plan.

    ``category`` is one of the bounded :class:`PlanFailureCategory` values and
    ``detail`` is a short, sanitized description of the rejected field paths and
    schema rule types (never model text, prompt text, or credentials).
    """

    def __init__(
        self,
        message: str,
        *,
        category: PlanFailureCategory = PlanFailureCategory.SCHEMA_INVALID,
        detail: str = "",
        attempts: int = 1,
    ) -> None:
        if not isinstance(category, PlanFailureCategory):
            raise ContractValidationError("plan failure category must be a bounded category")
        if attempts < 1 or attempts > 8:
            raise ContractValidationError("plan failure attempt count is out of range")
        super().__init__(message)
        self.category = category
        self.detail = _sanitize_detail(detail)
        self.attempts = attempts

    @property
    def diagnostic_code(self) -> str:
        """Return a stable, loggable code such as ``InvalidPlan:schema_invalid``."""

        return f"{type(self).__name__}:{self.category.value}"


def _sanitize_detail(detail: str) -> str:
    """Bound and strip a diagnostic to safe characters before it is persisted."""

    if not isinstance(detail, str):
        return ""
    safe = "".join(
        character
        for character in detail
        if character.isascii() and (character.isalnum() or character in " ._:,;()<>=-")
    )
    return safe.strip()[:MAX_PLAN_DIAGNOSTIC_LENGTH]


def validate_plan_diagnostic_token(value: str, label: str = "plan diagnostic") -> str:
    """Validate a short diagnostic token that may be stored in audit metadata."""

    validate_safe_token(value, label)
    return value


class TaskPlanner(Protocol):
    async def create_plan(self, request: UserRequest, task: TaskRecord) -> TaskPlan: ...
