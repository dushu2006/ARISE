"""Planner port and planner-specific domain errors."""

from __future__ import annotations

from typing import Protocol

from arise.core.models import TaskPlan, UserRequest
from arise.core.tasks import TaskRecord


class PlannerUnavailable(RuntimeError):
    """Raised when no planning model or provider is available."""


class InvalidPlan(ValueError):
    """Raised when a planner returns a malformed, stale, or unsafe plan."""


class TaskPlanner(Protocol):
    async def create_plan(self, request: UserRequest, task: TaskRecord) -> TaskPlan: ...
