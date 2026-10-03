"""On-demand, read-only host facts used by the authenticated diagnostics surface.

The default collector avoids screen capture, window enumeration, and audio streams. On Windows,
small Win32 probes collect display/foreground metadata; optional PortAudio discovery only asks
for the device list and never opens a device. Process and installed-app inventories are bounded
and omit command lines, environment variables, executable paths, and registry install locations.
"""

from __future__ import annotations

import importlib
import os
import platform
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import psutil

from arise.core.models import (
    ActiveWindowInfo,
    ApplicationInfo,
    CapabilityStatus,
    DisplayInfo,
    EnvironmentSnapshot,
)
from arise.core.redaction import DEFAULT_REDACTOR

_MAX_RUNNING_APPS = 512
_MAX_INSTALLED_APPS = 512
_MAX_AUDIO_DEVICES = 64
_MAX_NAME_LENGTH = 256
_MAX_WINDOW_TITLE_LENGTH = 2048

_BROWSER_LABELS: tuple[tuple[str, str], ...] = (
    ("chrome", "Google Chrome"),
    ("msedge", "Microsoft Edge"),
    ("edge", "Microsoft Edge"),
    ("firefox", "Mozilla Firefox"),
    ("brave", "Brave"),
    ("chromium", "Chromium"),
    ("opera", "Opera"),
    ("vivaldi", "Vivaldi"),
)
_TERMINAL_LABELS: tuple[tuple[str, str], ...] = (
    ("windowsterminal", "Windows Terminal"),
    ("windows terminal", "Windows Terminal"),
    ("powershell", "PowerShell"),
    ("pwsh", "PowerShell"),
    ("cmd.exe", "Command Prompt"),
    ("command prompt", "Command Prompt"),
    ("bash", "Bash"),
    ("zsh", "Zsh"),
    ("gnome-terminal", "GNOME Terminal"),
    ("konsole", "Konsole"),
    ("terminal", "Terminal"),
)


class WindowsHostProbe:
    """Bounded Win32 inventory calls; construction and imports are Windows-only."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows host probes are available only on Windows.")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        try:
            self._shcore = ctypes.WinDLL("shcore", use_last_error=True)
        except OSError:
            self._shcore = None

    def gpu_names(self) -> tuple[str, ...]:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class DisplayDevice(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128),
            ]

        enum_devices = self._user32.EnumDisplayDevicesW
        enum_devices.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(DisplayDevice),
            wintypes.DWORD,
        ]
        enum_devices.restype = wintypes.BOOL
        attached_to_desktop = 0x00000001
        names: list[str] = []
        for index in range(128):
            device = DisplayDevice()
            device.cb = ctypes.sizeof(device)
            if not enum_devices(None, index, ctypes.byref(device), 0):
                break
            name = _safe_name(device.DeviceString)
            if device.StateFlags & attached_to_desktop and name:
                names.append(name)
        return _dedupe(names, limit=32)

    def displays(self) -> tuple[DisplayInfo, ...]:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class Rect(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class MonitorInfoEx(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", Rect),
                ("rcWork", Rect),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]

        monitor_callback = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(Rect),
            wintypes.LPARAM,
        )
        get_monitor_info = self._user32.GetMonitorInfoW
        get_monitor_info.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfoEx)]
        get_monitor_info.restype = wintypes.BOOL
        dpi_function = getattr(self._shcore, "GetDpiForMonitor", None)
        if dpi_function is not None:
            dpi_function.argtypes = [
                wintypes.HMONITOR,
                ctypes.c_int,
                ctypes.POINTER(wintypes.UINT),
                ctypes.POINTER(wintypes.UINT),
            ]
            dpi_function.restype = ctypes.c_long

        results: list[DisplayInfo] = []

        @monitor_callback
        def collect(handle: Any, _dc: Any, _rect: Any, _data: int) -> bool:
            info = MonitorInfoEx()
            info.cbSize = ctypes.sizeof(info)
            if not get_monitor_info(handle, ctypes.byref(info)):
                return True
            bounds = info.rcMonitor
            dpi_x = wintypes.UINT()
            dpi_y = wintypes.UINT()
            measured_dpi_x: int | None = None
            measured_dpi_y: int | None = None
            scale: float | None = None
            if (
                dpi_function is not None
                and dpi_function(handle, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0
                and dpi_x.value > 0
                and dpi_y.value > 0
            ):
                measured_dpi_x = int(dpi_x.value)
                measured_dpi_y = int(dpi_y.value)
                scale = ((dpi_x.value / 96.0) + (dpi_y.value / 96.0)) / 2.0
            display_id = _safe_name(info.szDevice) or f"monitor-{len(results) + 1}"
            results.append(
                DisplayInfo(
                    display_id=display_id,
                    width=max(0, int(bounds.right - bounds.left)),
                    height=max(0, int(bounds.bottom - bounds.top)),
                    scale=scale,
                    dpi_x=measured_dpi_x,
                    dpi_y=measured_dpi_y,
                    primary=bool(info.dwFlags & 1),
                    availability=CapabilityStatus.AVAILABLE,
                )
            )
            return len(results) < 32

        enum_monitors = self._user32.EnumDisplayMonitors
        enum_monitors.argtypes = [wintypes.HDC, ctypes.c_void_p, monitor_callback, wintypes.LPARAM]
        enum_monitors.restype = wintypes.BOOL
        if not enum_monitors(None, None, collect, 0):
            raise OSError(ctypes.get_last_error(), "Could not enumerate Windows displays.")
        return tuple(results)

    def foreground_window(self) -> ActiveWindowInfo:
        wintypes = self._wintypes
        user32 = self._user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        handle = user32.GetForegroundWindow()
        if not handle:
            return ActiveWindowInfo(available=False, reason_unavailable="NO_FOREGROUND_WINDOW")
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes_int = self._ctypes.c_int
        length = max(0, min(int(user32.GetWindowTextLengthW(handle)), _MAX_WINDOW_TITLE_LENGTH))
        title_buffer = self._ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes_int]
        user32.GetWindowTextW.restype = ctypes_int
        user32.GetWindowTextW(handle, title_buffer, length + 1)
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            self._ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId(handle, self._ctypes.byref(process_id))
        pid = int(process_id.value) or None
        application: str | None = None
        if pid is not None:
            try:
                application = _safe_process_name(psutil.Process(pid).name()) or None
            except (psutil.Error, OSError, ValueError):
                pass
        title = DEFAULT_REDACTOR.redact(title_buffer.value[:_MAX_WINDOW_TITLE_LENGTH]) or None
        return ActiveWindowInfo(
            available=True,
            title=title,
            application=application,
            process_id=pid,
            window_id=f"0x{int(handle):x}",
        )

    def installed_applications(
        self, *, limit: int = _MAX_INSTALLED_APPS
    ) -> tuple[ApplicationInfo, ...]:
        if sys.platform != "win32":
            return ()
        import winreg

        roots = (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        )
        flags = (
            winreg.KEY_READ,
            winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
        found: dict[str, ApplicationInfo] = {}
        readable_roots = 0
        for root, path in roots:
            for access in flags:
                try:
                    with winreg.OpenKey(root, path, 0, access) as uninstall_key:
                        key_count = winreg.QueryInfoKey(uninstall_key)[0]
                        readable_roots += 1
                        count = min(key_count, 4096)
                        for index in range(count):
                            try:
                                subkey_name = winreg.EnumKey(uninstall_key, index)
                                with winreg.OpenKey(uninstall_key, subkey_name) as subkey:
                                    display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                            except OSError:
                                continue
                            name = _safe_name(display_name)
                            if name:
                                found.setdefault(
                                    name.casefold(),
                                    ApplicationInfo(name=name, source="installed_registry"),
                                )
                            if len(found) >= limit:
                                return tuple(found.values())
                except OSError:
                    continue
        if readable_roots == 0:
            raise OSError("Installed-application registry keys were unavailable.")
        return tuple(found.values())


class EnvironmentDiscovery:
    """Collect bounded host facts without capturing screens, audio, or process arguments."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        windows_probe: Any | None = None,
        process_iter: Callable[..., Iterable[Any]] | None = None,
        audio_module: Any | None = None,
    ) -> None:
        self.platform_name = platform_name or platform.system() or "unknown"
        self._windows_probe = windows_probe
        if self._windows_probe is None and self.platform_name.lower() == "windows":
            try:
                self._windows_probe = WindowsHostProbe()
            except Exception:
                self._windows_probe = None
        self._process_iter = process_iter or psutil.process_iter
        self._audio_module = audio_module

    def collect(self) -> EnvironmentSnapshot:
        unavailable: set[str] = {"network_status"}
        windows = self.platform_name.lower() == "windows"
        displays: tuple[DisplayInfo, ...] = ()
        gpu_names: tuple[str, ...] = ()
        active_window: ActiveWindowInfo | None = None
        installed_applications: tuple[ApplicationInfo, ...] = ()

        if self._windows_probe is None:
            unavailable.update(
                ("gpu_names", "displays", "display_dpi", "active_window", "installed_applications")
            )
        else:
            gpu_names, gpu_available = self._probe_tuple(self._windows_probe.gpu_names)
            displays, displays_available = self._probe_tuple(self._windows_probe.displays)
            active_window, active_available = self._probe_value(
                self._windows_probe.foreground_window
            )
            if active_window is not None:
                active_window = _sanitize_active_window(active_window)
            installed_applications, installed_available = self._probe_tuple(
                self._windows_probe.installed_applications
            )
            if not gpu_available:
                unavailable.add("gpu_names")
            if not displays_available or not displays:
                unavailable.add("displays")
            if (
                not displays_available
                or not displays
                or any(display.scale is None for display in displays)
            ):
                unavailable.add("display_dpi")
            if not active_available:
                unavailable.add("active_window")
            if not installed_available:
                unavailable.add("installed_applications")

        running_applications, running_available = self._running_applications()
        if not running_available:
            unavailable.add("running_applications")
        audio_inputs, audio_outputs, audio_available = self._audio_devices()
        if not audio_available:
            unavailable.update(("audio_input_devices", "audio_output_devices"))

        named_apps = tuple(app.name for app in (*running_applications, *installed_applications))
        browsers = _classify_names(named_apps, _BROWSER_LABELS)
        terminals = _classify_names(named_apps, _TERMINAL_LABELS)
        try:
            memory_total: int | None = int(psutil.virtual_memory().total)
        except (psutil.Error, OSError, RuntimeError, TypeError, ValueError):
            memory_total = None
            unavailable.add("total_memory_bytes")
        try:
            cpu_count = psutil.cpu_count(logical=True) or 1
        except (psutil.Error, OSError, RuntimeError):
            cpu_count = 1
            unavailable.add("cpu_count")

        if not windows:
            unavailable.update(
                ("gpu_names", "displays", "display_dpi", "active_window", "installed_applications")
            )
        return EnvironmentSnapshot(
            operating_system=self.platform_name,
            os_version=platform.release() or "unknown",
            architecture=platform.machine() or "unknown",
            cpu_count=max(1, int(cpu_count)),
            total_memory_bytes=memory_total,
            gpu_names=gpu_names,
            displays=displays,
            active_window=active_window,
            running_applications=running_applications,
            installed_applications=installed_applications,
            browsers=browsers,
            terminals=terminals,
            audio_input_devices=audio_inputs,
            audio_output_devices=audio_outputs,
            network_status="unknown",
            unavailable_fields=tuple(sorted(unavailable)),
        )

    @staticmethod
    def _probe_tuple(probe: Callable[..., Sequence[Any]]) -> tuple[Any, bool]:
        try:
            return tuple(probe()), True
        except Exception:
            return (), False

    @staticmethod
    def _probe_value(probe: Callable[..., Any]) -> tuple[Any | None, bool]:
        try:
            return probe(), True
        except Exception:
            return None, False

    def _running_applications(self) -> tuple[tuple[ApplicationInfo, ...], bool]:
        found: dict[int, ApplicationInfo] = {}
        try:
            processes = self._process_iter(["pid", "name"])
            for process in processes:
                try:
                    info = getattr(process, "info", None)
                    pid = int(info.get("pid") if isinstance(info, dict) else process.pid)
                    raw_name = info.get("name") if isinstance(info, dict) else process.name()
                    name = _safe_process_name(raw_name)
                except (psutil.Error, OSError, RuntimeError, TypeError, ValueError, AttributeError):
                    continue
                if pid > 0 and name:
                    found.setdefault(pid, ApplicationInfo(name=name, process_id=pid))
                if len(found) >= _MAX_RUNNING_APPS:
                    break
        except (psutil.Error, OSError, RuntimeError, TypeError, ValueError):
            return (), False
        current_pid = os.getpid()
        if current_pid not in found:
            try:
                current_name = _safe_process_name(psutil.Process(current_pid).name())
            except (psutil.Error, OSError, RuntimeError, ValueError):
                current_name = ""
            if current_name:
                if len(found) >= _MAX_RUNNING_APPS:
                    found.pop(max(found))
                found[current_pid] = ApplicationInfo(name=current_name, process_id=current_pid)
        return tuple(found[key] for key in sorted(found)), True

    def _audio_devices(self) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
        module = self._audio_module
        if module is None:
            try:
                module = importlib.import_module("sounddevice")
            except Exception:
                return (), (), False
        query_devices = getattr(module, "query_devices", None)
        if not callable(query_devices):
            return (), (), False
        try:
            devices = query_devices()
            inputs: list[str] = []
            outputs: list[str] = []
            for device in devices:
                if not isinstance(device, dict):
                    continue
                name = _safe_name(device.get("name"))
                if not name:
                    continue
                try:
                    input_channels = int(device.get("max_input_channels", 0))
                    output_channels = int(device.get("max_output_channels", 0))
                except (TypeError, ValueError):
                    continue
                if input_channels > 0 and len(inputs) < _MAX_AUDIO_DEVICES:
                    inputs.append(name)
                if output_channels > 0 and len(outputs) < _MAX_AUDIO_DEVICES:
                    outputs.append(name)
            return (
                _dedupe(inputs, limit=_MAX_AUDIO_DEVICES),
                _dedupe(outputs, limit=_MAX_AUDIO_DEVICES),
                True,
            )
        except Exception:
            return (), (), False


def _sanitize_active_window(window: ActiveWindowInfo) -> ActiveWindowInfo:
    title = DEFAULT_REDACTOR.redact(window.title or "")[:_MAX_WINDOW_TITLE_LENGTH] or None
    application = _safe_name(window.application) or None
    return ActiveWindowInfo(
        available=window.available,
        title=title,
        application=application,
        process_id=window.process_id,
        window_id=window.window_id,
        reason_unavailable=window.reason_unavailable,
    )


def _safe_process_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _safe_name(value.replace("\\", "/").rsplit("/", maxsplit=1)[-1])


def _safe_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    name = DEFAULT_REDACTOR.redact(value[:4096].replace("\x00", " ").strip())
    name = re.sub(r"\s+", " ", name)
    return name[:_MAX_NAME_LENGTH]


def _dedupe(values: Iterable[str], *, limit: int) -> tuple[str, ...]:
    result: dict[str, str] = {}
    for value in values:
        safe = _safe_name(value)
        if safe:
            result.setdefault(safe.casefold(), safe)
        if len(result) >= limit:
            break
    return tuple(result.values())


def _classify_names(names: Sequence[str], labels: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    normalized = tuple(_safe_name(name).casefold() for name in names)
    return tuple(
        dict.fromkeys(
            label for needle, label in labels if any(needle in name for name in normalized)
        )
    )
