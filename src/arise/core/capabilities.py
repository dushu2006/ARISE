"""Truthful capability inventory; unimplemented adapters remain unavailable."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from arise.core.model_gateway import ModelRouter
from arise.core.models import (
    Capability,
    CapabilityStatus,
    HealthStatus,
    ProviderStatus,
)
from arise.core.ports import ToolRegistry


class CapabilityService:
    def __init__(
        self,
        *,
        router: ModelRouter,
        tools: ToolRegistry,
        database_available: bool | Callable[[], bool],
    ) -> None:
        self.router = router
        self.tools = tools
        self._database_probe = (
            database_available if callable(database_available) else lambda: database_available
        )

    def list_capabilities(self) -> tuple[Capability, ...]:
        providers = self.router.status()
        model_status = self._model_status(providers)
        try:
            database_available = bool(self._database_probe())
        except Exception:
            database_available = False
        capability_list = [
            Capability(
                name="task.orchestration",
                version="1.0",
                status=CapabilityStatus.AVAILABLE,
                availability="available",
                health=HealthStatus.HEALTHY,
                adapter="core.task-engine",
                limitations=("Plans require a configured model provider.",),
            ),
            Capability(
                name="storage.sqlite",
                version="1.0",
                status=(
                    CapabilityStatus.AVAILABLE
                    if database_available
                    else CapabilityStatus.UNAVAILABLE
                ),
                availability="available" if database_available else "unavailable",
                health=(HealthStatus.HEALTHY if database_available else HealthStatus.UNAVAILABLE),
                adapter="adapters.sqlite",
                limitations=() if database_available else ("Local database health check failed.",),
            ),
            Capability(
                name="model.planning",
                version="1.0",
                status=model_status,
                availability=model_status.value,
                health=(
                    HealthStatus.HEALTHY
                    if model_status is CapabilityStatus.AVAILABLE
                    else HealthStatus.DEGRADED
                    if model_status is CapabilityStatus.DEGRADED
                    else HealthStatus.UNAVAILABLE
                ),
                requirements=("Explicit provider configuration", "OS credential store"),
                adapter="core.model-router",
                limitations=(
                    ()
                    if model_status is CapabilityStatus.AVAILABLE
                    else ("No model call has yet established a healthy provider.",)
                    if model_status is CapabilityStatus.DEGRADED
                    else ("No eligible model provider is configured.",)
                ),
            ),
            self._unavailable(
                "desktop.ui_automation",
                "No live Windows UI Automation adapter is included in Phase 1.",
                requirements=("Windows UI Automation/accessibility adapter",),
            ),
            self._unavailable(
                "browser.dom",
                "No browser DOM/CDP/Playwright adapter is included in Phase 1.",
                requirements=("Browser adapter with scoped profiles and permissions",),
            ),
            self._unavailable(
                "vision.ocr",
                "No screenshot, OCR, or visual grounding adapter is included in Phase 1.",
                requirements=("Redacted capture and grounded visual adapter",),
            ),
            self._disabled("voice.asr", "Voice input is deferred; no microphone is accessed."),
            self._disabled("voice.tts", "Voice output is deferred; no audio is generated."),
            self._disabled("memory.semantic", "Advanced/semantic memory is deferred."),
            self._disabled("web.research", "Autonomous web research is deferred."),
        ]
        for tool in self.tools.list_specs():
            capability_list.append(
                Capability(
                    name=f"tool.{tool.name}",
                    version=tool.version,
                    status=CapabilityStatus.AVAILABLE,
                    availability="available",
                    health=HealthStatus.HEALTHY,
                    requirements=tuple(sorted(tool.required_capabilities)),
                    adapter=tool.name,
                    limitations=tool.declared_side_effects,
                )
            )
        return tuple(capability_list)

    def get(self, name: str) -> Capability | None:
        return next((item for item in self.list_capabilities() if item.name == name), None)

    @staticmethod
    def _model_status(providers: Sequence[ProviderStatus]) -> CapabilityStatus:
        if not providers:
            return CapabilityStatus.REQUIRES_CONFIGURATION
        if any(provider.status is CapabilityStatus.AVAILABLE for provider in providers):
            return CapabilityStatus.AVAILABLE
        return CapabilityStatus.DEGRADED

    @staticmethod
    def _unavailable(
        name: str,
        limitation: str,
        *,
        requirements: tuple[str, ...] = (),
    ) -> Capability:
        return Capability(
            name=name,
            version="0.1",
            status=CapabilityStatus.UNAVAILABLE,
            availability="unavailable",
            health=HealthStatus.UNAVAILABLE,
            requirements=requirements,
            limitations=(limitation,),
        )

    @staticmethod
    def _disabled(name: str, limitation: str) -> Capability:
        return Capability(
            name=name,
            version="0.1",
            status=CapabilityStatus.DISABLED,
            availability="deferred",
            health=HealthStatus.UNAVAILABLE,
            limitations=(limitation,),
        )
