"""Small health/diagnostics service over injected runtime and persistence probes."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from arise.core.capabilities import CapabilityService
from arise.core.models import (
    CapabilityStatus,
    DiagnosticsSnapshot,
    HealthSnapshot,
    HealthStatus,
)


class TaskEngineHealthPort(Protocol):
    @property
    def active_count(self) -> int: ...

    @property
    def queued_count(self) -> int: ...


class EnvironmentProbe(Protocol):
    def collect(self): ...


class HealthService:
    def __init__(
        self,
        *,
        app_name: str,
        app_version: str,
        capability_service: CapabilityService,
        task_engine: TaskEngineHealthPort,
        environment: EnvironmentProbe,
        database_schema_version: int,
        database_path_kind: str,
        database_probe: Callable[[], bool],
    ) -> None:
        self.app_name = app_name
        self.app_version = app_version
        self.capability_service = capability_service
        self.task_engine = task_engine
        self.environment = environment
        self.database_schema_version = database_schema_version
        self.database_path_kind = database_path_kind
        self.database_probe = database_probe
        self._started_at = time.monotonic()

    def health(self) -> HealthSnapshot:
        database_available = self._database_available()
        capabilities = self.capability_service.list_capabilities()
        model = next(item for item in capabilities if item.name == "model.planning")
        database_status = (
            CapabilityStatus.AVAILABLE if database_available else CapabilityStatus.UNAVAILABLE
        )
        status = HealthStatus.HEALTHY
        degraded: list[str] = []
        if not database_available:
            status = HealthStatus.UNAVAILABLE
            degraded.append("local database check failed")
        if model.status is not CapabilityStatus.AVAILABLE:
            if status is HealthStatus.HEALTHY:
                status = HealthStatus.DEGRADED
            degraded.append(f"model planning is {model.status.value}")
        return HealthSnapshot(
            status=status,
            app_name=self.app_name,
            app_version=self.app_version,
            uptime_seconds=max(0.0, time.monotonic() - self._started_at),
            database_status=database_status,
            model_status=model.status,
            active_tasks=self.task_engine.active_count,
            queued_tasks=self.task_engine.queued_count,
            degraded_reasons=tuple(degraded),
        )

    def diagnostics(self) -> DiagnosticsSnapshot:
        return DiagnosticsSnapshot(
            environment=self.environment.collect(),
            capabilities=self.capability_service.list_capabilities(),
            providers=self.capability_service.router.status(),
            database_schema_version=self.database_schema_version,
            database_path_kind=self.database_path_kind,
        )

    def _database_available(self) -> bool:
        try:
            return bool(self.database_probe())
        except Exception:
            return False
