"""Truthful capability inventory; unimplemented adapters remain unavailable."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence

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
        voice_enabled: bool = False,
        voice_microphone_enabled: bool = False,
        voice_runtime_composed: bool = False,
        gemini_cloud_opted_in: bool = False,
        gemini_secret_configured: bool = False,
        gemini_sdk_available: bool = False,
        memory_enabled: bool = True,
        research_enabled: bool = False,
        research_secret_configured: bool = False,
        embeddings_enabled: bool = False,
        perception_enabled: bool = False,
        browser_availability: Callable[[], Mapping[str, object]] | None = None,
        desktop_host_supported: bool | Callable[[], bool] | None = None,
    ) -> None:
        self.router = router
        self.tools = tools
        self.voice_enabled = voice_enabled
        self.voice_microphone_enabled = voice_microphone_enabled
        self.voice_runtime_composed = voice_runtime_composed
        self.gemini_cloud_opted_in = gemini_cloud_opted_in
        self.gemini_secret_configured = gemini_secret_configured
        self.gemini_sdk_available = gemini_sdk_available
        self.memory_enabled = memory_enabled
        self.research_enabled = research_enabled
        self.research_secret_configured = research_secret_configured
        self.embeddings_enabled = embeddings_enabled
        self.perception_enabled = perception_enabled
        self._browser_availability = browser_availability
        self._desktop_host_supported = desktop_host_supported
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
            (
                self._desktop_capability()
                if any(spec.name.startswith("uia.") for spec in self.tools.list_specs())
                else self._unavailable(
                    "desktop.ui_automation",
                    "No live Windows UI Automation adapter is included or registered.",
                    requirements=("Windows UI Automation/accessibility adapter",),
                )
            ),
            (
                self._app_launch_capability()
                if any(spec.name == "system.app_launch" for spec in self.tools.list_specs())
                else self._unavailable(
                    "desktop.launch",
                    "No live application-launch adapter is included or registered.",
                    requirements=("Windows application launch adapter",),
                )
            ),
            (
                self._browser_capability()
                if any(spec.name.startswith("browser.") for spec in self.tools.list_specs())
                else self._unavailable(
                    "browser.dom",
                    "The optional Playwright adapter is experimental, not API-registered, "
                    "and not validated against a real browser.",
                    requirements=("Browser adapter with scoped profiles and permissions",),
                )
            ),
            (
                Capability(
                    name="vision.ocr",
                    version="1.0",
                    status=CapabilityStatus.AVAILABLE,
                    availability="available",
                    health=HealthStatus.HEALTHY,
                    requirements=("Redacted capture and grounded visual adapter",),
                    adapter="adapters.perception.PerceptionHierarchyPipeline",
                    limitations=(),
                )
                if self.perception_enabled
                else self._unavailable(
                    "vision.ocr",
                    "No screenshot, OCR, or visual grounding adapter is included in Phase 1.",
                    requirements=("Redacted capture and grounded visual adapter",),
                )
            ),
            *self._voice_capabilities(),
            self._memory_capability(database_available),
            self._semantic_memory_capability(database_available),
            self._research_capability(),
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

    def _semantic_memory_capability(self, database_available: bool) -> Capability:
        if not self.memory_enabled:
            status = CapabilityStatus.DISABLED
            health = HealthStatus.UNAVAILABLE
            limitation = "Persistent memory is disabled by configuration."
        elif not database_available:
            status = CapabilityStatus.UNAVAILABLE
            health = HealthStatus.UNAVAILABLE
            limitation = "The local database health check failed."
        elif not self.embeddings_enabled:
            status = CapabilityStatus.UNAVAILABLE
            health = HealthStatus.UNAVAILABLE
            limitation = (
                "No explicitly configured embedding endpoint is available; persistent memory "
                "continues to use local lexical ranking."
            )
        else:
            status = CapabilityStatus.AVAILABLE
            health = HealthStatus.DEGRADED
            limitation = (
                "An embedding endpoint is configured. Live inference is checked on use; "
                "ranking falls back to lexical search if it is unavailable."
            )
        return Capability(
            name="memory.semantic",
            version="1.0",
            status=status,
            availability=status.value,
            health=health,
            requirements=("Configured embedding endpoint", "Explicit cloud-memory egress opt-ins"),
            adapter="adapters.openai_embeddings.OpenAICompatibleEmbeddingAdapter"
            if self.embeddings_enabled
            else None,
            limitations=(limitation,),
        )

    def _research_capability(self) -> Capability:
        if not self.research_enabled:
            status = CapabilityStatus.DISABLED
            health = HealthStatus.UNAVAILABLE
            limitation = (
                "Web egress is disabled. Enable both the research adapter and explicit "
                "security opt-in to make user-initiated searches available."
            )
        elif not self.research_secret_configured:
            status = CapabilityStatus.REQUIRES_CONFIGURATION
            health = HealthStatus.DEGRADED
            limitation = "The Brave Search OS-keyring credential is unavailable."
        else:
            status = CapabilityStatus.AVAILABLE
            health = HealthStatus.DEGRADED
            limitation = (
                "User-initiated Brave searches and bounded public-page extraction are enabled; "
                "network success is checked per request and all results remain untrusted."
            )
        return Capability(
            name="web.research",
            version="1.0",
            status=status,
            availability=status.value,
            health=health,
            requirements=(
                "ARISE__RESEARCH__ENABLED=true",
                "ARISE__SECURITY__ALLOW_WEB_RESEARCH=true",
                "Brave Search credential in the OS keyring",
            ),
            adapter="adapters.brave_research.BraveWebResearchAdapter"
            if self.research_enabled
            else None,
            limitations=(limitation,),
        )

    def _memory_capability(self, database_available: bool) -> Capability:
        if not self.memory_enabled:
            status = CapabilityStatus.DISABLED
            limitation = "Persistent memory is disabled by configuration."
        elif not database_available:
            status = CapabilityStatus.UNAVAILABLE
            limitation = "The local database health check failed."
        else:
            status = CapabilityStatus.AVAILABLE
            limitation = (
                "Local, consent-gated storage and retrieval are available; embeddings are optional "
                "and lexical ranking remains the fallback. Model output cannot create records."
            )
        return Capability(
            name="memory.local",
            version="1.0",
            status=status,
            availability=status.value,
            health=(
                HealthStatus.HEALTHY
                if status is CapabilityStatus.AVAILABLE
                else HealthStatus.UNAVAILABLE
            ),
            requirements=("Explicit user consent for each write", "Local SQLite database"),
            adapter="adapters.sqlite.SQLiteMemoryRepository",
            limitations=(limitation,),
        )

    def _voice_capabilities(self) -> tuple[Capability, ...]:
        if not self.voice_enabled:
            local_status = CapabilityStatus.DISABLED
            local_limitation = "Voice is disabled; no microphone or playback adapter is started."
            model_status = CapabilityStatus.DISABLED
            model_limitation = "Voice is disabled; configure local model paths before use."
        elif not self.voice_microphone_enabled:
            local_status = CapabilityStatus.REQUIRES_CONFIGURATION
            local_limitation = (
                "Voice is configured, but microphone use requires the explicit local microphone "
                "setting and a user-initiated start command."
            )
            model_status = CapabilityStatus.REQUIRES_CONFIGURATION
            model_limitation = "Configure a user-supplied local Vosk model before use."
        elif self.voice_runtime_composed:
            local_status = CapabilityStatus.UNAVAILABLE
            local_limitation = (
                "AudioHub and local adapters are composed behind an authenticated start/stop "
                "control; a real device/session must pass runtime checks before availability."
            )
            model_status = CapabilityStatus.UNAVAILABLE
            model_limitation = (
                "The configured local Vosk model is checked before microphone capture; live "
                "inference and Windows device behavior remain unverified."
            )
        else:
            local_status = CapabilityStatus.UNAVAILABLE
            local_limitation = (
                "Microphone activation was requested, but Gemini credentials, the optional SDK, "
                "or local voice prerequisites are unavailable."
            )
            model_status = CapabilityStatus.REQUIRES_CONFIGURATION
            model_limitation = "Configure a user-supplied local Vosk model before use."

        if not self.voice_enabled:
            gemini_status = CapabilityStatus.DISABLED
            gemini_limitation = "Voice and Gemini Live are disabled by configuration."
        elif not self.gemini_cloud_opted_in:
            gemini_status = CapabilityStatus.REQUIRES_CONFIGURATION
            gemini_limitation = (
                "Gemini Live requires the explicit voice and security cloud opt-ins."
            )
        elif not self.gemini_secret_configured:
            gemini_status = CapabilityStatus.REQUIRES_CONFIGURATION
            gemini_limitation = "The configured OS-keyring credential is not available."
        elif not self.gemini_sdk_available:
            gemini_status = CapabilityStatus.REQUIRES_CONFIGURATION
            gemini_limitation = "Install the optional google-genai dependency to enable Live."
        else:
            gemini_status = CapabilityStatus.UNAVAILABLE
            gemini_limitation = (
                "Configuration and the optional adapter are present; an authenticated user start "
                "is required, and no real Live session has been runtime-verified."
            )

        def capability(
            name: str,
            status: CapabilityStatus,
            limitation: str,
            requirements: tuple[str, ...],
        ) -> Capability:
            return Capability(
                name=name,
                version="0.1",
                status=status,
                availability=status.value,
                health=(
                    HealthStatus.DEGRADED
                    if status is CapabilityStatus.REQUIRES_CONFIGURATION
                    else HealthStatus.UNAVAILABLE
                ),
                requirements=requirements,
                limitations=(limitation,),
            )

        local_requirements = (
            "Explicit microphone/playback consent",
            "Windows device and permission validation",
        )
        return (
            capability("voice.runtime", local_status, local_limitation, local_requirements),
            capability("voice.audio_capture", local_status, local_limitation, local_requirements),
            capability("voice.audio_playback", local_status, local_limitation, local_requirements),
            capability(
                "voice.local_vad",
                local_status,
                "Install/configure WebRTC VAD and validate it with synthetic and live audio."
                if self.voice_enabled
                else local_limitation,
                ("Optional voice-local dependency", "Windows live-audio validation"),
            ),
            capability(
                "voice.wake_word",
                local_status,
                "Configure a user-supplied Vosk model and validate wake/false-wake behavior."
                if self.voice_enabled
                else local_limitation,
                ("User-supplied Vosk model", "Local-only wake validation"),
            ),
            capability(
                "voice.asr",
                model_status,
                model_limitation,
                ("User-supplied Vosk model", "Streaming partial/final validation"),
            ),
            capability(
                "voice.tts",
                model_status,
                model_limitation,
                ("User-supplied Kokoro model/voice files", "Local inference validation"),
            ),
            capability(
                "voice.gemini_live",
                gemini_status,
                gemini_limitation,
                (
                    "Explicit CLI opt-in and cloud consent",
                    "OS-keyring credential and optional SDK",
                    "Local wake gate and live Windows validation",
                ),
            ),
            capability(
                "voice.task_admission",
                CapabilityStatus.UNAVAILABLE,
                "The narrow TaskEngine bridge is implemented; admission remains unavailable until "
                "the configured microphone/provider session passes runtime checks.",
                ("Explicit safe tool registration through TaskEngine/PolicyEngine",),
            ),
        )

    @staticmethod
    def _model_status(providers: Sequence[ProviderStatus]) -> CapabilityStatus:
        if not providers:
            return CapabilityStatus.REQUIRES_CONFIGURATION
        if any(provider.status is CapabilityStatus.AVAILABLE for provider in providers):
            return CapabilityStatus.AVAILABLE
        return CapabilityStatus.DEGRADED

    def _desktop_capability(self) -> Capability:
        """Registration is not host support: UIA tools only work on a Windows desktop host.

        The adapter itself fails closed at execution time; this keeps the reported inventory
        honest as well, so a non-Windows host never advertises live UI Automation.
        """

        requirements = ("Windows UI Automation/accessibility adapter",)
        probe = self._desktop_host_supported
        supported: bool | None = None
        if probe is not None:
            try:
                supported = bool(probe() if callable(probe) else probe)
            except Exception:
                supported = False
        if supported:
            return Capability(
                name="desktop.ui_automation",
                version="1.0",
                status=CapabilityStatus.AVAILABLE,
                availability="available",
                health=HealthStatus.HEALTHY,
                requirements=requirements,
                adapter="adapters.windows_uia.WindowsUiaProvider",
                limitations=(
                    "Native inspection is limited to Win32 HWND controls, not the full UIA tree. "
                    "Chrome omnibox accessibility is not established. Host availability does not "
                    "prove execution; actions require fresh observation and verification.",
                ),
            )
        reason = (
            "The UIA adapter is registered, but this process is not running on a supported "
            "Windows desktop host."
            if supported is False
            else "The UIA adapter is registered, but no host-support check is configured, so "
            "live Windows automation cannot be reported as available."
        )
        return Capability(
            name="desktop.ui_automation",
            version="1.0",
            status=CapabilityStatus.REQUIRES_CONFIGURATION,
            availability="requires_configuration",
            health=HealthStatus.UNAVAILABLE,
            requirements=requirements,
            adapter="adapters.windows_uia.WindowsUiaProvider",
            limitations=(reason,),
        )

    def _app_launch_capability(self) -> Capability:
        requirements = ("Windows application launch adapter",)
        probe = self._desktop_host_supported
        supported: bool | None = None
        if probe is not None:
            try:
                supported = bool(probe() if callable(probe) else probe)
            except Exception:
                supported = False
        if supported or (supported is None and sys.platform == "win32"):
            return Capability(
                name="desktop.launch",
                version="1.0",
                status=CapabilityStatus.AVAILABLE,
                availability="available",
                health=HealthStatus.HEALTHY,
                requirements=requirements,
                adapter="adapters.windows_app_launch.WindowsAppLaunchProvider",
                limitations=(),
            )
        reason = (
            "The application-launch tool is registered, but this process is not running on a "
            "supported Windows desktop host."
            if supported is False or sys.platform != "win32"
            else "The application-launch tool is registered, but host support is unverified."
        )
        return Capability(
            name="desktop.launch",
            version="1.0",
            status=CapabilityStatus.REQUIRES_CONFIGURATION,
            availability="requires_configuration",
            health=HealthStatus.UNAVAILABLE,
            requirements=requirements,
            adapter="adapters.windows_app_launch.WindowsAppLaunchProvider",
            limitations=(reason,),
        )

    def _browser_capability(self) -> Capability:
        """Report browser.dom only when a launchable browser actually exists.

        Registration proves configuration, not capability: the Playwright wheel can be
        installed with no Chromium binary, and `provider.start()` would then fail. When an
        availability probe is supplied, its per-condition result gates the status.
        """

        requirements = ("Browser adapter with scoped profiles and permissions",)
        if self._browser_availability is None:
            return Capability(
                name="browser.dom",
                version="1.0",
                status=CapabilityStatus.DEGRADED,
                availability="degraded",
                health=HealthStatus.DEGRADED,
                requirements=requirements,
                adapter="adapters.browser_playwright.PlaywrightBrowserProvider",
                limitations=("No browser availability probe is configured for this process.",),
            )
        try:
            report = dict(self._browser_availability())
        except Exception as exc:
            return Capability(
                name="browser.dom",
                version="1.0",
                status=CapabilityStatus.UNAVAILABLE,
                availability="unavailable",
                health=HealthStatus.UNAVAILABLE,
                requirements=requirements,
                adapter="adapters.browser_playwright.PlaywrightBrowserProvider",
                limitations=(f"Browser availability probe failed: {type(exc).__name__}.",),
            )
        if report.get("playwright_installed") and report.get("chromium_installed"):
            return Capability(
                name="browser.dom",
                version="1.0",
                status=CapabilityStatus.AVAILABLE,
                availability="available",
                health=HealthStatus.HEALTHY,
                requirements=requirements,
                adapter="adapters.browser_playwright.PlaywrightBrowserProvider",
                limitations=("Not validated against a real browser on this host.",),
            )
        reason = str(
            report.get("isolated_adapter_reason")
            or "No launchable browser is available for the Playwright adapter."
        )
        return Capability(
            name="browser.dom",
            version="1.0",
            status=CapabilityStatus.REQUIRES_CONFIGURATION,
            availability="requires_configuration",
            health=HealthStatus.UNAVAILABLE,
            requirements=requirements,
            adapter="adapters.browser_playwright.PlaywrightBrowserProvider",
            limitations=(reason,),
        )

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
