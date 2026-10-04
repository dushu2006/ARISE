"""Direct unit coverage for `CapabilityService` status truthfulness.

Before the Windows release gate this service had no direct test at all; it was only observed
through `/api/v1/capabilities`, where every fixture happened to register the optional adapters.
That let `browser.dom` and `desktop.ui_automation` report `available` from configuration alone:
an installed Playwright wheel with no Chromium binary, or registered UIA tools on a non-Windows
host, were advertised as live capabilities. These tests pin the gate in both directions so a
capability is never reported as available when the host cannot act on it.
"""

from __future__ import annotations

import unittest
from typing import Any

from arise.core.capabilities import CapabilityService
from arise.core.models import CapabilityStatus, HealthStatus, RiskLevel
from arise.core.ports import Idempotency, ToolRegistry, ToolSpec


class _FakeRouter:
    """Only `status()` is consulted by the capability listing."""

    def status(self) -> tuple[Any, ...]:
        return ()


class _StubTool:
    """Minimal `ActionTool` used only to make a tool name appear in the registry."""

    def __init__(self, name: str) -> None:
        self.spec = ToolSpec(
            name=name,
            version="1.0",
            description="test tool",
            minimum_risk=RiskLevel.R0,
            idempotency=Idempotency.IDEMPOTENT,
        )

    def validate_parameters(self, parameters: Any) -> None:
        return None

    async def execute(self, action: Any, observation: Any, resources: Any) -> Any:
        raise AssertionError("capability reporting must never execute a tool")


def _service(
    *,
    tool_names: tuple[str, ...] = (),
    browser_availability: Any = None,
    desktop_host_supported: Any = None,
) -> CapabilityService:
    registry = ToolRegistry()
    for name in tool_names:
        registry.register(_StubTool(name))
    return CapabilityService(
        router=_FakeRouter(),  # type: ignore[arg-type]
        tools=registry,
        database_available=True,
        browser_availability=browser_availability,
        desktop_host_supported=desktop_host_supported,
    )


def _capability(service: CapabilityService, name: str) -> Any:
    return next(capability for capability in service.list_capabilities() if capability.name == name)


class BrowserCapabilityTruthfulnessTests(unittest.TestCase):
    def test_wheel_without_binary_is_not_available(self) -> None:
        service = _service(
            tool_names=("browser.inspect",),
            browser_availability=lambda: {
                "playwright_installed": True,
                "chromium_installed": False,
                "isolated_adapter_reason": "no browser binary",
            },
        )
        capability = _capability(service, "browser.dom")
        self.assertIs(capability.status, CapabilityStatus.REQUIRES_CONFIGURATION)
        self.assertIs(capability.health, HealthStatus.UNAVAILABLE)
        self.assertEqual(capability.limitations, ("no browser binary",))

    def test_binary_without_wheel_is_not_available(self) -> None:
        service = _service(
            tool_names=("browser.inspect",),
            browser_availability=lambda: {
                "playwright_installed": False,
                "chromium_installed": True,
                "isolated_adapter_reason": "install the browser extra",
            },
        )
        capability = _capability(service, "browser.dom")
        self.assertIs(capability.status, CapabilityStatus.REQUIRES_CONFIGURATION)
        self.assertEqual(capability.limitations, ("install the browser extra",))

    def test_both_conditions_are_required_for_available(self) -> None:
        service = _service(
            tool_names=("browser.inspect",),
            browser_availability=lambda: {
                "playwright_installed": True,
                "chromium_installed": True,
                "isolated_adapter_reason": None,
            },
        )
        capability = _capability(service, "browser.dom")
        self.assertIs(capability.status, CapabilityStatus.AVAILABLE)
        self.assertIs(capability.health, HealthStatus.HEALTHY)

    def test_missing_report_is_degraded_not_available(self) -> None:
        capability = _capability(_service(tool_names=("browser.inspect",)), "browser.dom")
        self.assertIs(capability.status, CapabilityStatus.DEGRADED)
        self.assertNotEqual(capability.availability, "available")

    def test_failing_probe_fails_closed(self) -> None:
        def boom() -> dict[str, Any]:
            raise RuntimeError("probe exploded")

        capability = _capability(
            _service(tool_names=("browser.inspect",), browser_availability=boom),
            "browser.dom",
        )
        self.assertIs(capability.status, CapabilityStatus.UNAVAILABLE)
        self.assertIn("RuntimeError", capability.limitations[0])

    def test_absent_reason_still_reports_the_gap(self) -> None:
        service = _service(
            tool_names=("browser.inspect",),
            browser_availability=lambda: {
                "playwright_installed": True,
                "chromium_installed": False,
            },
        )
        capability = _capability(service, "browser.dom")
        self.assertIs(capability.status, CapabilityStatus.REQUIRES_CONFIGURATION)
        self.assertTrue(capability.limitations[0])

    def test_unregistered_browser_tools_stay_unavailable(self) -> None:
        service = _service(
            browser_availability=lambda: {
                "playwright_installed": True,
                "chromium_installed": True,
            }
        )
        capability = _capability(service, "browser.dom")
        self.assertIs(capability.status, CapabilityStatus.UNAVAILABLE)


class DesktopCapabilityTruthfulnessTests(unittest.TestCase):
    def test_non_windows_host_is_not_available(self) -> None:
        capability = _capability(
            _service(tool_names=("uia.focus",), desktop_host_supported=lambda: False),
            "desktop.ui_automation",
        )
        self.assertIs(capability.status, CapabilityStatus.REQUIRES_CONFIGURATION)
        self.assertIn("not running on a supported Windows desktop host", capability.limitations[0])

    def test_windows_host_is_available(self) -> None:
        capability = _capability(
            _service(tool_names=("uia.focus",), desktop_host_supported=lambda: True),
            "desktop.ui_automation",
        )
        self.assertIs(capability.status, CapabilityStatus.AVAILABLE)

    def test_missing_host_check_is_not_available(self) -> None:
        capability = _capability(_service(tool_names=("uia.focus",)), "desktop.ui_automation")
        self.assertIs(capability.status, CapabilityStatus.REQUIRES_CONFIGURATION)

    def test_raising_host_check_fails_closed(self) -> None:
        def boom() -> bool:
            raise RuntimeError("host check failed")

        capability = _capability(
            _service(tool_names=("uia.focus",), desktop_host_supported=boom),
            "desktop.ui_automation",
        )
        self.assertIs(capability.status, CapabilityStatus.REQUIRES_CONFIGURATION)

    def test_unregistered_uia_tools_stay_unavailable(self) -> None:
        capability = _capability(
            _service(desktop_host_supported=lambda: True), "desktop.ui_automation"
        )
        self.assertIs(capability.status, CapabilityStatus.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
