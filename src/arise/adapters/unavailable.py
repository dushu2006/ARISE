"""Fail-closed adapters for capabilities not implemented in this phase."""

from __future__ import annotations

from arise.core.contracts import ActionContract, ObservationLease
from arise.core.errors import CapabilityUnavailableError


class UnavailableEnvironment:
    """Never fabricates observations when platform perception is not installed."""

    async def observe(self, action: ActionContract) -> ObservationLease:
        del action
        raise CapabilityUnavailableError(
            "No live desktop/browser environment adapter is installed.",
            component="environment",
            operation="observe",
        )

    async def is_current(self, observation: ObservationLease) -> bool:
        del observation
        return False
