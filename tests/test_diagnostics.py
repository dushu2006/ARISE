from __future__ import annotations

import os
import unittest

from arise.adapters.diagnostics import EnvironmentDiscovery
from arise.core.models import (
    ActiveWindowInfo,
    ApplicationInfo,
    CapabilityStatus,
    DisplayInfo,
)


class _Process:
    def __init__(self, pid: int, name: str) -> None:
        self.info = {"pid": pid, "name": name}


class _AudioDevices:
    @staticmethod
    def query_devices() -> list[dict[str, object]]:
        return [
            {"name": "USB microphone", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "Studio output", "max_input_channels": 0, "max_output_channels": 2},
            {
                "name": "Shared api_key=sk-12345678901234567890 device",
                "max_input_channels": 1,
                "max_output_channels": 1,
            },
        ]


class _WindowsProbe:
    @staticmethod
    def gpu_names() -> tuple[str, ...]:
        return ("NVIDIA GeForce RTX",)

    @staticmethod
    def displays() -> tuple[DisplayInfo, ...]:
        return (
            DisplayInfo(
                display_id="\\\\.\\DISPLAY1",
                width=2560,
                height=1440,
                scale=1.5,
                dpi_x=144,
                dpi_y=144,
                primary=True,
                availability=CapabilityStatus.AVAILABLE,
            ),
        )

    @staticmethod
    def foreground_window() -> ActiveWindowInfo:
        return ActiveWindowInfo(
            available=True,
            title="Dashboard api_key=sk-12345678901234567890",
            application="msedge.exe",
            process_id=20,
            window_id="0x100",
        )

    @staticmethod
    def installed_applications() -> tuple[ApplicationInfo, ...]:
        return (ApplicationInfo(name="Mozilla Firefox", source="installed_registry"),)


class EnvironmentDiscoveryTests(unittest.TestCase):
    def test_windows_snapshot_integrates_bounded_host_and_optional_audio_facts(self) -> None:
        requested_attrs: list[list[str]] = []

        def process_iter(attrs: list[str]) -> list[_Process]:
            requested_attrs.append(attrs)
            return [
                _Process(10, r"C:\\Users\\private-user\\chrome.exe"),
                _Process(20, "msedge.exe"),
            ]

        snapshot = EnvironmentDiscovery(
            platform_name="Windows",
            windows_probe=_WindowsProbe(),
            process_iter=process_iter,
            audio_module=_AudioDevices(),
        ).collect()

        self.assertEqual(requested_attrs, [["pid", "name"]])
        self.assertEqual(snapshot.gpu_names, ("NVIDIA GeForce RTX",))
        self.assertEqual(snapshot.displays[0].dpi_x, 144)
        self.assertEqual(snapshot.displays[0].dpi_y, 144)
        self.assertEqual(snapshot.displays[0].scale, 1.5)
        self.assertTrue(snapshot.displays[0].primary)
        self.assertTrue(snapshot.active_window.available)
        self.assertIn("[REDACTED]", snapshot.active_window.title or "")
        self.assertNotIn("12345678901234567890", snapshot.active_window.title or "")
        running_names = {app.name for app in snapshot.running_applications}
        self.assertTrue({"chrome.exe", "msedge.exe"} <= running_names)
        self.assertNotIn("private-user", " ".join(running_names))
        self.assertIn(os.getpid(), {app.process_id for app in snapshot.running_applications})
        self.assertEqual(snapshot.installed_applications[0].source, "installed_registry")
        self.assertEqual(
            set(snapshot.browsers), {"Google Chrome", "Microsoft Edge", "Mozilla Firefox"}
        )
        self.assertEqual(
            snapshot.audio_input_devices, ("USB microphone", "Shared api_key=[REDACTED] device")
        )
        self.assertEqual(
            snapshot.audio_output_devices, ("Studio output", "Shared api_key=[REDACTED] device")
        )
        self.assertNotIn("displays", snapshot.unavailable_fields)
        self.assertNotIn("display_dpi", snapshot.unavailable_fields)
        self.assertNotIn("audio_input_devices", snapshot.unavailable_fields)
        self.assertIn("network_status", snapshot.unavailable_fields)

    def test_real_process_inventory_includes_the_discovery_host(self) -> None:
        snapshot = EnvironmentDiscovery(
            platform_name="Linux",
            audio_module=_AudioDevices(),
        ).collect()
        running_pids = {app.process_id for app in snapshot.running_applications}
        self.assertIn(os.getpid(), running_pids)
        self.assertLessEqual(len(snapshot.running_applications), 512)
        self.assertNotIn("running_applications", snapshot.unavailable_fields)

    def test_non_windows_snapshot_reports_unsupported_desktop_fields_truthfully(self) -> None:
        snapshot = EnvironmentDiscovery(
            platform_name="Linux",
            process_iter=lambda _attrs: [_Process(1, "bash"), _Process(2, "python")],
            audio_module=_AudioDevices(),
        ).collect()

        self.assertEqual(snapshot.operating_system, "Linux")
        self.assertEqual(snapshot.terminals, ("Bash",))
        self.assertEqual(snapshot.audio_input_devices[0], "USB microphone")
        self.assertIsNone(snapshot.active_window)
        self.assertIn("gpu_names", snapshot.unavailable_fields)
        self.assertIn("displays", snapshot.unavailable_fields)
        self.assertIn("display_dpi", snapshot.unavailable_fields)
        self.assertIn("active_window", snapshot.unavailable_fields)
        self.assertIn("installed_applications", snapshot.unavailable_fields)

    def test_native_discovery_failure_is_reported_without_fabricated_values(self) -> None:
        class FailingWindowsProbe(_WindowsProbe):
            @staticmethod
            def gpu_names() -> tuple[str, ...]:
                raise OSError("native probe failed")

            @staticmethod
            def displays() -> tuple[DisplayInfo, ...]:
                raise OSError("native probe failed")

            @staticmethod
            def foreground_window() -> ActiveWindowInfo:
                raise OSError("native probe failed")

            @staticmethod
            def installed_applications() -> tuple[ApplicationInfo, ...]:
                raise OSError("native probe failed")

        snapshot = EnvironmentDiscovery(
            platform_name="Windows",
            windows_probe=FailingWindowsProbe(),
            process_iter=lambda _attrs: [],
            audio_module=_AudioDevices(),
        ).collect()

        self.assertEqual(snapshot.gpu_names, ())
        self.assertEqual(snapshot.displays, ())
        self.assertIsNone(snapshot.active_window)
        self.assertEqual(snapshot.installed_applications, ())
        self.assertTrue(
            {"gpu_names", "displays", "display_dpi", "active_window", "installed_applications"}
            <= set(snapshot.unavailable_fields)
        )


if __name__ == "__main__":
    unittest.main()
