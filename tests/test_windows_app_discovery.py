"""Deterministic fake-backed application discovery and activation regressions.

These tests validate catalog ranking and verification logic only; they do not
exercise the Windows shell, COM, AppX activation, or a live UIA provider.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from arise.adapters.windows_app_discovery import (
    ActivationMethod,
    ApplicationDescriptor,
    WindowsApplicationCatalog,
    normalize_application_name,
)
from arise.adapters.windows_app_launch import (
    DesktopSnapshot,
    ObservedProcess,
    Win32AppLaunchBackend,
    WindowsAppLaunchProvider,
    WindowsApplicationResolver,
)
from arise.core.computer import ComputerFailureCode, WindowRecord
from arise.core.computer_ports import ComputerAdapterError


class StaticCatalog:
    def __init__(self, descriptors: tuple[ApplicationDescriptor, ...]) -> None:
        self.descriptors = descriptors
        self.calls = 0

    def discover(self) -> tuple[ApplicationDescriptor, ...]:
        self.calls += 1
        return self.descriptors


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"mock executable or shortcut")
    return path


def test_catalog_discovers_aumids_start_menu_pwas_and_exact_installed_metadata(tmp_path):
    start_menu = tmp_path / "Start Menu" / "Programs"
    start_menu.mkdir(parents=True)
    whatsapp_link = _touch(start_menu / "WhatsApp.lnk")
    unsafe_link = _touch(start_menu / "Shell Launcher.lnk")
    chrome_exe = _touch(tmp_path / "Browser" / "chrome.exe")
    shell_exe = _touch(tmp_path / "Windows" / "System32" / "cmd.exe")
    portable_location = tmp_path / "Portable Editor"
    editor_exe = _touch(portable_location / "Portable Editor.exe")
    _touch(portable_location / "uninstall-helper.exe")
    ambiguous_location = tmp_path / "Ambiguous App"
    _touch(ambiguous_location / "Ambiguous App.exe")
    _touch(ambiguous_location / "nested" / "Ambiguous App.exe")
    mystery_location = tmp_path / "Mystery App"
    _touch(mystery_location / "unrelated.exe")

    def read_shortcut(path: str) -> tuple[str, str] | None:
        if path == str(whatsapp_link):
            return str(chrome_exe), "--profile-directory=Default --app-id=mocked-pwa"
        if path == str(unsafe_link):
            return str(shell_exe), "/c echo untrusted"
        return None

    catalog = WindowsApplicationCatalog(
        is_windows=True,
        start_menu_roots=(start_menu,),
        start_apps_reader=lambda: (
            {"Name": "Camera", "AppID": "Microsoft.WindowsCamera_8wekyb3d8bbwe!App"},
            {"Name": "Unlaunchable Start Item", "AppID": "not-an-aumid"},
        ),
        shortcut_reader=read_shortcut,
        registry_entries_reader=lambda: (
            {
                "DisplayName": "Portable Editor",
                "DisplayIcon": f'"{editor_exe}",0',
                "InstallLocation": str(portable_location),
            },
            {
                "DisplayName": "Ambiguous App",
                "InstallLocation": str(ambiguous_location),
            },
            {
                "DisplayName": "Mystery App",
                "InstallLocation": str(mystery_location),
            },
        ),
    )

    applications = catalog.discover()
    by_name = {application.name: application for application in applications}

    assert set(by_name) == {"Camera", "Portable Editor", "WhatsApp"}
    assert by_name["Camera"].activation_method is ActivationMethod.PACKAGED_AUMID
    assert by_name["Camera"].package_family_name == "Microsoft.WindowsCamera_8wekyb3d8bbwe"
    assert by_name["WhatsApp"].activation_method is ActivationMethod.START_MENU_SHORTCUT
    assert by_name["WhatsApp"].is_web_app is True
    assert by_name["WhatsApp"].shortcut_path == str(whatsapp_link)
    assert by_name["WhatsApp"].executable_path == str(chrome_exe)
    assert by_name["Portable Editor"].executable_path == str(editor_exe)
    assert all("uninstall-helper" not in (app.executable_path or "") for app in applications)


def test_name_normalization_is_exact_and_locale_independent():
    assert normalize_application_name(" WhatsApp.exe ") == "whatsapp"
    assert normalize_application_name("Ｍｉｃｒｏｓｏｆｔ Camera") == "microsoftcamera"
    assert normalize_application_name("Visual Studio Code") != normalize_application_name(
        "Visual Studio"
    )


def test_catalog_match_beats_a_registered_alias_and_is_cache_bounded():
    package_app = ApplicationDescriptor(
        name="WhatsApp",
        activation_method=ActivationMethod.PACKAGED_AUMID,
        package_family_name="Vendor.WhatsApp_abc123",
        aumid="Vendor.WhatsApp_abc123!App",
        source="windows_start_apps",
    )
    legacy = ApplicationDescriptor(
        name="Legacy WhatsApp",
        executable_path=r"C:\Old\whatsapp.exe",
        process_names=("whatsapp.exe",),
        source="fixture_alias",
    )
    catalog = StaticCatalog((package_app,))
    resolver = WindowsApplicationResolver(catalog=catalog, cache_ttl_seconds=30)
    resolver.register_alias("WhatsApp", legacy)

    assert resolver.resolve("WhatsApp") is package_app
    assert resolver.resolve("WhatsApp") is package_app
    assert catalog.calls == 1


def test_ambiguous_exact_candidates_fail_closed_with_path_free_diagnostics():
    catalog = StaticCatalog(
        (
            ApplicationDescriptor(
                name="Editor",
                executable_path=r"C:\Apps\One\editor.exe",
                process_names=("editor.exe",),
                source="start_menu_shortcut",
            ),
            ApplicationDescriptor(
                name="Editor",
                executable_path=r"D:\Apps\Two\editor.exe",
                process_names=("editor.exe",),
                source="installed_registry",
            ),
        )
    )
    resolver = WindowsApplicationResolver(catalog=catalog)

    with pytest.raises(ComputerAdapterError) as failure:
        resolver.resolve("Editor")

    assert failure.value.code is ComputerFailureCode.APPLICATION_AMBIGUOUS
    diagnostic = resolver.last_diagnostic
    assert diagnostic["requested_application"] == "Editor"
    assert diagnostic["normalized_query"] == "editor"
    assert diagnostic["outcome"] == "ambiguous"
    assert diagnostic["candidate_count"] == 2
    assert "C:\\Apps" not in json.dumps(diagnostic)
    assert "D:\\Apps" not in json.dumps(diagnostic)


def test_uninstall_metadata_does_not_choose_an_arbitrary_executable(tmp_path):
    install = tmp_path / "Mystery Product"
    _touch(install / "setup-helper.exe")
    catalog = WindowsApplicationCatalog(
        is_windows=True,
        start_menu_roots=(),
        start_apps_reader=lambda: (),
        registry_entries_reader=lambda: (
            {"DisplayName": "Mystery Product", "InstallLocation": str(install)},
        ),
    )

    assert catalog.discover() == ()


def test_shell_script_and_command_host_path_entries_are_not_executable_fallbacks():
    resolver = WindowsApplicationResolver(catalog=StaticCatalog(()))
    with (
        patch("arise.adapters.windows_app_launch.sys.platform", "win32"),
        patch(
            "arise.adapters.windows_app_launch.shutil.which", return_value=r"C:\Tools\unknown.cmd"
        ),
        patch("arise.adapters.windows_app_launch.os.path.isfile", return_value=True),
    ):
        with pytest.raises(ComputerAdapterError) as failure:
            resolver.resolve("unknown")
    assert failure.value.code is ComputerFailureCode.APPLICATION_NOT_FOUND


@pytest.mark.asyncio
async def test_windows_backend_selects_native_packaged_and_shortcut_activation():
    backend = Win32AppLaunchBackend()
    packaged = ApplicationDescriptor(
        name="Camera",
        activation_method=ActivationMethod.PACKAGED_AUMID,
        package_family_name="Microsoft.WindowsCamera_8wekyb3d8bbwe",
        aumid="Microsoft.WindowsCamera_8wekyb3d8bbwe!App",
    )
    shortcut = ApplicationDescriptor(
        name="WhatsApp",
        executable_path=r"C:\Browser\chrome.exe",
        process_names=("chrome.exe",),
        activation_method=ActivationMethod.START_MENU_SHORTCUT,
        shortcut_path=r"C:\Start Menu\WhatsApp.lnk",
        is_web_app=True,
    )
    with (
        patch(
            "arise.adapters.windows_app_launch.activate_packaged_application", return_value=701
        ) as activate_package,
        patch(
            "arise.adapters.windows_app_launch.shell_execute_shortcut", return_value=702
        ) as activate_shortcut,
    ):
        assert await backend.activate_application(packaged) == 701
        assert await backend.activate_application(shortcut) == 702
    activate_package.assert_called_once_with(packaged.aumid)
    activate_shortcut.assert_called_once_with(shortcut.shortcut_path)


def test_executable_dispatch_remains_shell_false():
    backend = Win32AppLaunchBackend()
    with (
        patch("arise.adapters.windows_app_launch.sys.platform", "win32"),
        patch("arise.adapters.windows_app_launch.os.path.isfile", return_value=True),
        patch("arise.adapters.windows_app_launch.subprocess.Popen") as spawn,
    ):
        spawn.return_value.pid = 123
        assert backend._sync_launch_process(r"C:\Apps\editor.exe") == 123
    assert spawn.call_args.args[0] == [r"C:\Apps\editor.exe"]
    assert spawn.call_args.kwargs["shell"] is False


@pytest.mark.asyncio
async def test_installed_application_inventory_comes_from_catalog_not_alias_table():
    descriptor = ApplicationDescriptor(
        name="Portable Tool",
        executable_path=r"C:\Portable\portable-tool.exe",
        process_names=("portable-tool.exe",),
        source="path_executable",
    )
    provider = WindowsAppLaunchProvider(
        backend=PackageActivationBackend("Vendor.App_abc123"),
        resolver=WindowsApplicationResolver(catalog=StaticCatalog((descriptor,))),
    )

    applications = await provider.installed_applications(limit=10)

    assert len(applications) == 1
    assert applications[0].name == "Portable Tool"
    assert applications[0].identity == descriptor.diagnostic_identity
    assert applications[0].activation_method == ActivationMethod.EXECUTABLE.value
    assert not hasattr(applications[0], "executable_path")


@pytest.mark.asyncio
async def test_resolution_failure_is_diagnosed_before_activation_without_paths():
    provider = WindowsAppLaunchProvider(
        resolver=WindowsApplicationResolver(catalog=StaticCatalog(()))
    )

    with pytest.raises(ComputerAdapterError) as failure:
        await provider.launch_application("Missing App")

    assert failure.value.code is ComputerFailureCode.APPLICATION_NOT_FOUND
    diagnostic = provider.launch_diagnostic
    assert diagnostic["stage"] == "resolution"
    assert diagnostic["mode"] == "not_dispatched"
    assert diagnostic["failure_code"] == ComputerFailureCode.APPLICATION_NOT_FOUND.value
    assert diagnostic["requested_application"] == "Missing App"
    assert diagnostic["normalized_query"] == "missingapp"
    assert diagnostic["candidate_count"] == 0
    assert "executable_path" not in json.dumps(diagnostic)


class PackageActivationBackend:
    requires_visible_window = True

    def __init__(self, family: str) -> None:
        self.family = family
        self.processes: list[dict[str, Any]] = []
        self.windows: list[WindowRecord] = []
        self.activated: list[str] = []
        self.focused: list[str] = []

    async def list_running_processes(self):
        return list(self.processes)

    async def activate_application(self, application: ApplicationDescriptor) -> int:
        assert application.aumid is not None
        self.activated.append(application.aumid)
        pid = 4444
        executable = r"C:\Program Files\WindowsApps\Camera\Camera.exe"
        self.processes = [
            {
                "pid": pid,
                "name": "Camera.exe",
                "exe": executable,
                "package_family_name": self.family,
            }
        ]
        self.windows = [
            WindowRecord(
                window_id="hwnd-camera",
                process_id=pid,
                title="Camera",
                application="ApplicationFrameHost.exe",
                visible=True,
                minimized=False,
                maximized=False,
                foreground=False,
                executable_path=executable,
                package_family_name=self.family,
                aumid=f"{self.family}!App",
            )
        ]
        return pid

    async def is_process_alive(self, pid: int) -> bool:
        return any(process["pid"] == pid for process in self.processes)

    async def list_windows(self):
        return list(self.windows)

    async def focus_window(self, window_id: str) -> WindowRecord:
        self.focused.append(window_id)
        self.windows = [
            replace(window, foreground=window.window_id == window_id) for window in self.windows
        ]
        return next(window for window in self.windows if window.window_id == window_id)


@pytest.mark.asyncio
async def test_packaged_activation_requires_package_owner_and_fresh_visible_window():
    family = "Microsoft.WindowsCamera_8wekyb3d8bbwe"
    descriptor = ApplicationDescriptor(
        name="Camera",
        activation_method=ActivationMethod.PACKAGED_AUMID,
        package_family_name=family,
        aumid=f"{family}!App",
        source="windows_start_apps",
    )
    backend = PackageActivationBackend(family)
    resolver = WindowsApplicationResolver(catalog=StaticCatalog((descriptor,)))
    provider = WindowsAppLaunchProvider(backend=backend, resolver=resolver)

    application = await provider.launch_application("Camera", timeout_seconds=2)

    assert backend.activated == [descriptor.aumid]
    assert backend.focused == ["hwnd-camera"]
    assert application.name == "Camera"
    assert application.package_family_name == family
    assert application.window_ids == ("hwnd-camera",)
    assert provider.launch_diagnostic["activation_method"] == ActivationMethod.PACKAGED_AUMID.value
    assert provider.launch_diagnostic["focus_verified"] is True


def test_package_identity_can_be_confirmed_by_native_window_aumid():
    family = "Vendor.Camera_abc123"
    aumid = f"{family}!App"
    descriptor = ApplicationDescriptor(
        name="Camera",
        activation_method=ActivationMethod.PACKAGED_AUMID,
        package_family_name=family,
        aumid=aumid,
    )
    window = WindowRecord(
        window_id="hwnd-frame",
        process_id=52,
        title="Camera",
        application="ApplicationFrameHost.exe",
        visible=True,
        minimized=False,
        maximized=False,
        foreground=True,
        executable_path=r"C:\Windows\System32\ApplicationFrameHost.exe",
        aumid=aumid,
    )
    snapshot = DesktopSnapshot(
        processes={
            52: ObservedProcess(
                52,
                "ApplicationFrameHost.exe",
                r"C:\Windows\System32\ApplicationFrameHost.exe",
            )
        },
        windows={window.window_id: window},
        foreground_window_id=window.window_id,
    )
    provider = WindowsAppLaunchProvider()
    assert provider._window_owner_verified(window, snapshot, descriptor) is True

    wrong_window = replace(window, aumid=f"{family}!AnotherApp")
    wrong_snapshot = replace(
        snapshot,
        windows={wrong_window.window_id: wrong_window},
        foreground_window_id=wrong_window.window_id,
    )
    assert provider._window_owner_verified(wrong_window, wrong_snapshot, descriptor) is False


def test_package_mismatch_and_webapp_title_mismatch_fail_owner_verification():
    expected_family = "Vendor.Camera_abc123"
    descriptor = ApplicationDescriptor(
        name="Camera",
        activation_method=ActivationMethod.PACKAGED_AUMID,
        package_family_name=expected_family,
        aumid=f"{expected_family}!App",
    )
    window = WindowRecord(
        window_id="hwnd-wrong-package",
        process_id=50,
        title="Camera",
        application="Camera.exe",
        visible=True,
        minimized=False,
        maximized=False,
        foreground=True,
        executable_path=r"C:\WindowsApps\Other\Camera.exe",
        package_family_name="Vendor.Other_abc123",
        aumid="Vendor.Other_abc123!App",
    )
    snapshot = DesktopSnapshot(
        processes={
            50: ObservedProcess(
                50,
                "Camera.exe",
                r"C:\WindowsApps\Other\Camera.exe",
                "Vendor.Other_abc123",
            )
        },
        windows={window.window_id: window},
        foreground_window_id=window.window_id,
    )
    provider = WindowsAppLaunchProvider()
    assert provider._window_owner_verified(window, snapshot, descriptor) is False

    web_app = ApplicationDescriptor(
        name="WhatsApp",
        executable_path=r"C:\Browser\chrome.exe",
        process_names=("chrome.exe",),
        activation_method=ActivationMethod.START_MENU_SHORTCUT,
        shortcut_path=r"C:\Start Menu\WhatsApp.lnk",
        is_web_app=True,
    )
    chrome_window = WindowRecord(
        window_id="hwnd-chrome",
        process_id=51,
        title="Google Chrome",
        application="chrome.exe",
        visible=True,
        minimized=False,
        maximized=False,
        foreground=True,
        executable_path=r"C:\Browser\chrome.exe",
    )
    chrome_snapshot = DesktopSnapshot(
        processes={51: ObservedProcess(51, "chrome.exe", r"C:\Browser\chrome.exe")},
        windows={chrome_window.window_id: chrome_window},
        foreground_window_id=chrome_window.window_id,
    )
    assert provider._window_owner_verified(chrome_window, chrome_snapshot, web_app) is False
    whatsapp_window = replace(chrome_window, title="WhatsApp")
    whatsapp_snapshot = replace(
        chrome_snapshot,
        windows={whatsapp_window.window_id: whatsapp_window},
        foreground_window_id=whatsapp_window.window_id,
    )
    assert provider._window_owner_verified(whatsapp_window, whatsapp_snapshot, web_app) is True
