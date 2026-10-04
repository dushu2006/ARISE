"""Windows UI Automation (UIA) adapter with strict semantic targeting and safety gates.

All UIA operations require a fresh observation lease, semantic target resolution,
focus/display-topology/human-interference checks, and explicit secret-reference
protection for password/sensitive controls.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from arise.adapters.secrets import SecretProvider, SecretUnavailable
from arise.core.computer import (
    AccessibilityElement,
    ComputerFailureCode,
    CoordinateMapper,
    CoordinateSpace,
    DisplayGeometry,
    EnvironmentFingerprint,
    PerceptionSource,
    Point,
    Rect,
    ResolutionStatus,
    SelectorQuality,
    TargetCandidate,
    TargetDescriptor,
    TargetQuery,
    TargetResolution,
    WindowRecord,
)
from arise.core.computer_ports import ComputerAdapterError, SensitiveText
from arise.core.contracts import (
    ActionContract,
    EvidenceSource,
    Idempotency,
    ObservationLease,
    RiskLevel,
    SecretRef,
    TargetIdentity,
    canonical_json,
    utc_now,
    validate_safe_token,
)
from arise.core.grounding import TargetResolver
from arise.core.observation import EnvironmentChangeDetector
from arise.core.ports import (
    EvidenceRecord,
    ExecutionOutcome,
    ExecutionStatus,
    ToolRegistry,
    ToolSpec,
    VerificationResult,
    VerificationStatus,
)
from arise.core.redaction import DEFAULT_REDACTOR
from arise.core.resources import ResourceLease, ResourceLeaseLost

_MAX_OBSERVATIONS = 64
_MAX_TREE_NODES = 1024
_MAX_TREE_DEPTH = 16
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9+_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class RawUiaNode:
    """Raw control node returned by the platform UIA backend before normalization."""

    node_id: str
    window_id: str
    process_id: int
    application: str
    control_type: str
    role: str
    name: str
    automation_id: str | None = None
    class_name: str | None = None
    framework_id: str | None = None
    value: str | None = None
    enabled: bool = True
    visible: bool = True
    focused: bool = False
    selected: bool | None = None
    expanded: bool | None = None
    toggle_state: str | None = None
    sensitive: bool = False
    supported_patterns: tuple[str, ...] = ("InvokePattern",)
    runtime_id: tuple[str | int, ...] = ()
    hierarchy: tuple[str, ...] = ()
    bounds: Rect | None = None
    coordinate_space: CoordinateSpace = CoordinateSpace.PHYSICAL_DESKTOP
    child_count: int = 0


class WindowsUiaBackend(Protocol):
    """Low-level backend interface isolating Win32/COM calls from safety logic."""

    async def list_displays(self) -> Sequence[DisplayGeometry]: ...

    async def list_windows(self, *, include_hidden: bool = False) -> Sequence[WindowRecord]: ...

    async def foreground_window(self) -> WindowRecord | None: ...

    async def focus_window(self, window_id: str) -> WindowRecord: ...

    async def inspect_window_nodes(
        self,
        window_id: str,
        *,
        max_depth: int = _MAX_TREE_DEPTH,
        max_nodes: int = _MAX_TREE_NODES,
    ) -> Sequence[RawUiaNode]: ...

    async def cursor_position(self) -> Point | None: ...

    async def user_input_observed_since(self, monotonic_seconds: float) -> bool: ...

    async def invoke_node(
        self, window_id: str, node: RawUiaNode, *, click_point: Point | None = None
    ) -> None: ...

    async def set_node_value(self, window_id: str, node: RawUiaNode, value: str) -> None: ...

    async def focus_node(self, window_id: str, node: RawUiaNode) -> None: ...

    async def send_keys(self, window_id: str, node: RawUiaNode | None, key: str) -> None: ...


_WIN32_CLASS_ROLE_MAP: Mapping[str, tuple[str, str]] = {
    "button": ("button", "Button"),
    "edit": ("textbox", "Edit"),
    "richedit20w": ("textbox", "Document"),
    "combobox": ("combobox", "ComboBox"),
    "listbox": ("list", "List"),
    "syslistview32": ("list", "List"),
    "systreeview32": ("tree", "Tree"),
    "systabcontrol32": ("tab", "Tab"),
    "static": ("text", "Text"),
    "scrollbar": ("scrollbar", "ScrollBar"),
    "msctls_trackbar32": ("slider", "Slider"),
    "msctls_progress32": ("progressbar", "ProgressBar"),
}


def _parse_hwnd(token: str) -> int:
    cleaned = token.strip().lower()
    if cleaned.startswith("hwnd-"):
        cleaned = cleaned[5:]
    try:
        hwnd = int(cleaned, 0)
    except ValueError as exc:
        raise ComputerAdapterError(
            ComputerFailureCode.WINDOW_NOT_FOUND,
            "Invalid Windows HWND identifier.",
            source=PerceptionSource.UI_AUTOMATION,
        ) from exc
    if hwnd <= 0:
        raise ComputerAdapterError(
            ComputerFailureCode.WINDOW_NOT_FOUND,
            "Invalid Windows HWND identifier.",
            source=PerceptionSource.UI_AUTOMATION,
        )
    return hwnd


class Win32UiaBackend:
    """Native Win32 user32/shcore backend for window/display/control inspection and dispatch."""

    def __init__(
        self,
        *,
        user32: Any | None = None,
        shcore: Any | None = None,
        kernel32: Any | None = None,
    ) -> None:
        self._user32 = user32
        self._shcore = shcore
        self._kernel32 = kernel32

    def _require_windows(self) -> None:
        if sys.platform != "win32" and self._user32 is None:
            raise ComputerAdapterError(
                ComputerFailureCode.UIA_NOT_AVAILABLE,
                "Windows UI Automation requires a supported Windows desktop host.",
                source=PerceptionSource.UI_AUTOMATION,
            )

    def desktop_available(self) -> bool:
        """Probe an accessible input desktop; OS name alone is not availability."""
        if sys.platform != "win32":
            return False
        from ctypes import wintypes

        try:
            user32 = self._get_user32()
            user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            user32.OpenInputDesktop.restype = wintypes.HANDLE
            user32.CloseDesktop.argtypes = [wintypes.HANDLE]
            user32.CloseDesktop.restype = wintypes.BOOL
            desktop = user32.OpenInputDesktop(0, False, 0x0001)
            if not desktop:
                return False
            try:
                return bool(user32.GetForegroundWindow())
            finally:
                user32.CloseDesktop(desktop)
        except (OSError, AttributeError):
            return False

    def _get_user32(self) -> Any:
        if self._user32 is not None:
            return self._user32
        import ctypes

        return ctypes.WinDLL("user32", use_last_error=True)

    def _get_shcore(self) -> Any | None:
        if self._shcore is not None:
            return self._shcore
        if sys.platform != "win32":
            return None
        import ctypes

        try:
            return ctypes.WinDLL("shcore", use_last_error=True)
        except OSError:
            return None

    def _get_kernel32(self) -> Any:
        if self._kernel32 is not None:
            return self._kernel32
        import ctypes

        return ctypes.WinDLL("kernel32", use_last_error=True)

    async def list_displays(self) -> Sequence[DisplayGeometry]:
        self._require_windows()
        return await asyncio.to_thread(self._sync_list_displays)

    def _sync_list_displays(self) -> Sequence[DisplayGeometry]:
        import ctypes
        from ctypes import wintypes

        user32 = self._get_user32()
        shcore = self._get_shcore()

        class WinRect(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class MonitorInfoEx(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", WinRect),
                ("rcWork", WinRect),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]

        results: list[DisplayGeometry] = []
        enum_monitors = getattr(user32, "EnumDisplayMonitors", None)
        get_monitor_info = getattr(user32, "GetMonitorInfoW", None)
        dpi_fn = getattr(shcore, "GetDpiForMonitor", None) if shcore is not None else None

        if enum_monitors is not None and get_monitor_info is not None:
            cb_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
            monitor_cb_type = cb_factory(
                wintypes.BOOL,
                wintypes.HMONITOR,
                wintypes.HDC,
                ctypes.POINTER(WinRect),
                wintypes.LPARAM,
            )

            @monitor_cb_type
            def _collect(hmon: Any, _hdc: Any, _lprect: Any, _lparam: int) -> bool:
                info = MonitorInfoEx()
                info.cbSize = ctypes.sizeof(MonitorInfoEx)
                if not get_monitor_info(hmon, ctypes.byref(info)):
                    return True
                rc = info.rcMonitor
                w = max(1, int(rc.right - rc.left))
                h = max(1, int(rc.bottom - rc.top))
                dpi_x_val = 96.0
                dpi_y_val = 96.0
                dpi_ok = False
                dpi_src = "default_96"
                if dpi_fn is not None:
                    dx = wintypes.UINT(0)
                    dy = wintypes.UINT(0)
                    try:
                        if (
                            dpi_fn(hmon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0
                            and dx.value > 0
                        ):
                            dpi_x_val = float(dx.value)
                            dpi_y_val = float(dy.value or dx.value)
                            dpi_ok = True
                            dpi_src = "GetDpiForMonitor"
                    except Exception:
                        dpi_ok = False
                raw_dev = "".join(
                    ch for ch in str(info.szDevice) if ch.isalnum() or ch in {"-", "_"}
                )
                dev_id = raw_dev or f"display-{len(results) + 1}"
                results.append(
                    DisplayGeometry(
                        display_id=dev_id[:64],
                        physical_bounds=Rect(float(rc.left), float(rc.top), float(w), float(h)),
                        dpi_x=dpi_x_val,
                        dpi_y=dpi_y_val,
                        primary=bool(info.dwFlags & 1),
                        dpi_available=dpi_ok,
                        dpi_source=dpi_src,
                    )
                )
                return len(results) < 16

            try:
                enum_monitors(None, None, _collect, 0)
            except Exception:
                results.clear()

        if results:
            return tuple(results)

        width = max(1, int(user32.GetSystemMetrics(0)))
        height = max(1, int(user32.GetSystemMetrics(1)))
        dpi = 96.0
        dpi_available = False
        try:
            dpi_val = int(user32.GetDpiForSystem())
            if dpi_val > 0:
                dpi = float(dpi_val)
                dpi_available = True
        except Exception:
            dpi_available = False
        return (
            DisplayGeometry(
                display_id="display-primary",
                physical_bounds=Rect(0, 0, width, height),
                dpi_x=dpi,
                dpi_y=dpi,
                primary=True,
                dpi_available=dpi_available,
                dpi_source="GetDpiForSystem" if dpi_available else "unavailable",
            ),
        )

    def _window_record_from_hwnd(self, hwnd: int, *, fg_hwnd: int) -> WindowRecord | None:
        import ctypes
        from ctypes import wintypes

        import psutil

        user32 = self._get_user32()
        if not hwnd or not bool(user32.IsWindow(hwnd)):
            return None
        length = max(0, min(2048, int(user32.GetWindowTextLengthW(hwnd))))
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = DEFAULT_REDACTOR.redact(buf.value.strip())[:2048]
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wintypes.RECT()
        bounds: Rect | None = None
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            w = float(rect.right - rect.left)
            h = float(rect.bottom - rect.top)
            if w > 0 and h > 0:
                bounds = Rect(float(rect.left), float(rect.top), w, h)
        visible = bool(user32.IsWindowVisible(hwnd))
        minimized = bool(user32.IsIconic(hwnd))
        maximized = bool(user32.IsZoomed(hwnd))
        process_id = int(pid.value) if int(pid.value) > 0 else None
        app_name = "windows-app"
        owner_executable: str | None = None
        package_family_name: str | None = None
        window_aumid: str | None = None
        if process_id is not None:
            try:
                owner_process = psutil.Process(process_id)
                raw_name = owner_process.name()
                cleaned = "".join(ch for ch in raw_name if ch.isalnum() or ch in {"-", "_", "."})
                if cleaned:
                    app_name = cleaned[:128]
                # The owner's executable path is observed here, on the HWND's PID,
                # so an application launch can prove window ownership by executable
                # identity instead of a process name. Access failures stay None and
                # therefore cannot be treated as a match.
                raw_executable = owner_process.exe()
                if raw_executable:
                    owner_executable = str(raw_executable)[:4096]
                from arise.adapters.windows_app_discovery import (
                    app_user_model_id_for_window,
                    package_family_name_for_pid,
                )

                package_family_name = package_family_name_for_pid(process_id)
                window_aumid = app_user_model_id_for_window(hwnd)
            except Exception:
                app_name = "windows-app"
        return WindowRecord(
            window_id=f"hwnd-{hwnd}",
            process_id=process_id,
            title=title,
            application=app_name,
            visible=visible,
            minimized=minimized,
            maximized=maximized,
            foreground=(hwnd == fg_hwnd),
            bounds=bounds,
            executable_path=owner_executable,
            package_family_name=package_family_name,
            aumid=window_aumid,
        )

    async def list_windows(self, *, include_hidden: bool = False) -> Sequence[WindowRecord]:
        self._require_windows()
        return await asyncio.to_thread(self._sync_list_windows, include_hidden=include_hidden)

    def _sync_list_windows(self, *, include_hidden: bool = False) -> Sequence[WindowRecord]:
        import ctypes
        from ctypes import wintypes

        user32 = self._get_user32()
        fg_hwnd = int(user32.GetForegroundWindow() or 0)
        enum_windows = getattr(user32, "EnumWindows", None)
        records: list[WindowRecord] = []

        if enum_windows is not None:
            cb_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
            wnd_cb_type = cb_factory(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            @wnd_cb_type
            def _collect_wnd(hwnd: Any, _lparam: int) -> bool:
                rec = self._window_record_from_hwnd(int(hwnd), fg_hwnd=fg_hwnd)
                if rec is None:
                    return True
                if not include_hidden and not rec.visible:
                    return True
                records.append(rec)
                return len(records) < 128

            try:
                enum_windows(_collect_wnd, 0)
            except Exception:
                records.clear()

        if records:
            return tuple(records)

        if fg_hwnd:
            rec = self._window_record_from_hwnd(fg_hwnd, fg_hwnd=fg_hwnd)
            if rec is not None and (include_hidden or rec.visible):
                return (rec,)
        return ()

    async def foreground_window(self) -> WindowRecord | None:
        self._require_windows()
        return await asyncio.to_thread(self._sync_foreground_window)

    def _sync_foreground_window(self) -> WindowRecord | None:
        user32 = self._get_user32()
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        return self._window_record_from_hwnd(hwnd, fg_hwnd=hwnd)

    async def focus_window(self, window_id: str) -> WindowRecord:
        self._require_windows()
        return await asyncio.to_thread(self._sync_focus_window, window_id)

    def _sync_focus_window(self, window_id: str) -> WindowRecord:
        user32 = self._get_user32()
        hwnd = _parse_hwnd(window_id)
        if not bool(user32.IsWindow(hwnd)):
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Requested Windows UIA window handle does not exist.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        if bool(user32.IsIconic(hwnd)):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        fg_hwnd = int(user32.GetForegroundWindow() or 0)
        rec = self._window_record_from_hwnd(hwnd, fg_hwnd=fg_hwnd)
        if rec is None or fg_hwnd != hwnd:
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Requested Windows UIA window could not be brought to foreground.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        return rec

    @staticmethod
    def _focused_hwnd_for_window(user32: Any, window_hwnd: int) -> int | None:
        """Return the GUI thread's keyboard-focused HWND when Windows reports one.

        Foreground-window state is not keyboard focus. GetGUIThreadInfo is used
        instead of GetFocus because this inspection runs on a worker thread. The
        result can only identify a native HWND; custom-drawn controls that are
        not separate HWNDs remain unidentifiable and will not be guessed.
        """

        import ctypes
        from ctypes import wintypes

        get_thread_id = getattr(user32, "GetWindowThreadProcessId", None)
        get_gui_thread_info = getattr(user32, "GetGUIThreadInfo", None)
        if get_thread_id is None or get_gui_thread_info is None:
            return None

        class GuiThreadInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT),
            ]

        process_id = wintypes.DWORD(0)
        try:
            thread_id = int(get_thread_id(window_hwnd, ctypes.byref(process_id)) or 0)
            if thread_id <= 0:
                return None
            info = GuiThreadInfo()
            info.cbSize = ctypes.sizeof(GuiThreadInfo)
            if not get_gui_thread_info(thread_id, ctypes.byref(info)):
                return None
            focused_hwnd = int(info.hwndFocus or 0)
        except Exception:
            return None
        return focused_hwnd or None

    async def inspect_window_nodes(
        self,
        window_id: str,
        *,
        max_depth: int = _MAX_TREE_DEPTH,
        max_nodes: int = _MAX_TREE_NODES,
    ) -> Sequence[RawUiaNode]:
        self._require_windows()
        return await asyncio.to_thread(
            self._sync_inspect_window_nodes,
            window_id,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )

    def _sync_inspect_window_nodes(
        self,
        window_id: str,
        *,
        max_depth: int = _MAX_TREE_DEPTH,
        max_nodes: int = _MAX_TREE_NODES,
    ) -> Sequence[RawUiaNode]:
        import ctypes
        from ctypes import wintypes

        user32 = self._get_user32()
        root_hwnd = _parse_hwnd(window_id)
        if not bool(user32.IsWindow(root_hwnd)):
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Target window handle is no longer valid.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        fg_hwnd = int(user32.GetForegroundWindow() or 0)
        focused_hwnd = self._focused_hwnd_for_window(user32, root_hwnd)
        root_rec = self._window_record_from_hwnd(root_hwnd, fg_hwnd=fg_hwnd)
        if root_rec is None:
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Target window record could not be inspected.",
                source=PerceptionSource.UI_AUTOMATION,
            )

        root_pid = int(root_rec.process_id or 0)
        root_app = root_rec.application
        nodes: list[RawUiaNode] = [
            RawUiaNode(
                node_id=f"hwnd-{root_hwnd}",
                window_id=f"hwnd-{root_hwnd}",
                process_id=root_pid,
                application=root_app,
                role="window",
                name=root_rec.title,
                automation_id=f"hwnd-{root_hwnd}",
                class_name="Window",
                control_type="Window",
                framework_id="Win32",
                enabled=True,
                visible=root_rec.visible,
                focused=(focused_hwnd == root_hwnd),
                hierarchy=(f"hwnd-{root_hwnd}",),
                bounds=root_rec.bounds,
            )
        ]
        if max_nodes <= 1 or max_depth < 1:
            return tuple(nodes)

        hierarchy_by_hwnd: dict[int, tuple[str, ...]] = {root_hwnd: (f"hwnd-{root_hwnd}",)}
        enum_child = getattr(user32, "EnumChildWindows", None)
        if enum_child is None:
            return tuple(nodes)

        cb_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        child_cb_type = cb_factory(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @child_cb_type
        def _collect_child(child_hwnd_raw: Any, _lparam: int) -> bool:
            if len(nodes) >= max_nodes:
                return False
            child_hwnd = int(child_hwnd_raw)
            parent_hwnd = int(user32.GetParent(child_hwnd) or root_hwnd)
            parent_path = hierarchy_by_hwnd.get(parent_hwnd, (f"hwnd-{root_hwnd}",))
            if len(parent_path) > max_depth:
                return True
            child_path = (*parent_path, f"hwnd-{child_hwnd}")
            hierarchy_by_hwnd[child_hwnd] = child_path

            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child_hwnd, cls_buf, 256)
            class_name = cls_buf.value.strip()[:128] or "Control"
            role, ctrl_type = _WIN32_CLASS_ROLE_MAP.get(class_name.lower(), ("control", class_name))

            txt_len = max(0, min(1024, int(user32.GetWindowTextLengthW(child_hwnd))))
            txt_buf = ctypes.create_unicode_buffer(txt_len + 1)
            user32.GetWindowTextW(child_hwnd, txt_buf, txt_len + 1)
            raw_text = DEFAULT_REDACTOR.redact(txt_buf.value.strip())[:512]

            ctrl_id = int(user32.GetDlgCtrlID(child_hwnd) or 0)
            auto_id = f"ctrl-{ctrl_id}" if ctrl_id > 0 else f"hwnd-{child_hwnd}"

            rect = wintypes.RECT()
            bounds: Rect | None = None
            if user32.GetWindowRect(child_hwnd, ctypes.byref(rect)):
                w = float(rect.right - rect.left)
                h = float(rect.bottom - rect.top)
                if w > 0 and h > 0:
                    bounds = Rect(float(rect.left), float(rect.top), w, h)

            visible = bool(user32.IsWindowVisible(child_hwnd))
            enabled = bool(user32.IsWindowEnabled(child_hwnd))
            style = int(user32.GetWindowLongW(child_hwnd, -16) or 0)  # GWL_STYLE
            es_password = 0x0020
            is_pwd = role == "textbox" and bool(style & es_password)
            patterns = (
                ("ValuePattern", "InvokePattern") if role == "textbox" else ("InvokePattern",)
            )

            nodes.append(
                RawUiaNode(
                    node_id=f"hwnd-{child_hwnd}",
                    window_id=f"hwnd-{root_hwnd}",
                    process_id=root_pid,
                    application=root_app,
                    role=role,
                    name=raw_text,
                    automation_id=auto_id,
                    class_name=class_name,
                    control_type=ctrl_type,
                    framework_id="Win32",
                    value="" if is_pwd else raw_text,
                    enabled=enabled,
                    visible=visible,
                    focused=(focused_hwnd == child_hwnd),
                    sensitive=is_pwd,
                    supported_patterns=patterns,
                    runtime_id=(child_hwnd,),
                    hierarchy=child_path,
                    bounds=bounds,
                )
            )
            return len(nodes) < max_nodes

        enum_child(root_hwnd, _collect_child, 0)
        return tuple(nodes)

    async def cursor_position(self) -> Point | None:
        if sys.platform != "win32" and self._user32 is None:
            return None
        import ctypes
        from ctypes import wintypes

        user32 = self._get_user32()
        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            return Point(float(pt.x), float(pt.y))
        return None

    async def user_input_observed_since(self, monotonic_seconds: float) -> bool:
        if sys.platform != "win32" and self._user32 is None:
            return False
        import ctypes
        from ctypes import wintypes

        class LastInputInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("dwTime", wintypes.DWORD),
            ]

        user32 = self._get_user32()
        kernel32 = self._get_kernel32()
        get_last_input = getattr(user32, "GetLastInputInfo", None)
        get_tick_count = getattr(kernel32, "GetTickCount", None)
        if get_last_input is None or get_tick_count is None:
            return False
        lii = LastInputInfo()
        lii.cbSize = ctypes.sizeof(LastInputInfo)
        if not get_last_input(ctypes.byref(lii)):
            return False
        now_tick = int(get_tick_count()) & 0xFFFFFFFF
        last_tick = int(lii.dwTime) & 0xFFFFFFFF
        elapsed_input_ms = (now_tick - last_tick) & 0xFFFFFFFF
        elapsed_observation_ms = max(0.0, (time.monotonic() - monotonic_seconds) * 1000.0)
        return float(elapsed_input_ms) < elapsed_observation_ms

    async def invoke_node(
        self, window_id: str, node: RawUiaNode, *, click_point: Point | None = None
    ) -> None:
        self._require_windows()
        await asyncio.to_thread(self._sync_invoke_node, window_id, node, click_point=click_point)

    def _sync_invoke_node(
        self, window_id: str, node: RawUiaNode, *, click_point: Point | None = None
    ) -> None:
        user32 = self._get_user32()
        target_hwnd = _parse_hwnd(node.node_id if node.node_id.startswith("hwnd-") else window_id)
        if not bool(user32.IsWindow(target_hwnd)):
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_NOT_FOUND,
                "Target Win32 control handle no longer exists.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        if click_point is not None:
            user32.SetCursorPos(int(round(click_point.x)), int(round(click_point.y)))
            user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
            return
        bm_click = 0x00F5
        user32.SendMessageW(target_hwnd, bm_click, 0, 0)

    async def set_node_value(self, window_id: str, node: RawUiaNode, value: str) -> None:
        self._require_windows()
        await asyncio.to_thread(self._sync_set_node_value, window_id, node, value)

    def _sync_set_node_value(self, window_id: str, node: RawUiaNode, value: str) -> None:
        user32 = self._get_user32()
        target_hwnd = _parse_hwnd(node.node_id if node.node_id.startswith("hwnd-") else window_id)
        if not bool(user32.IsWindow(target_hwnd)):
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_NOT_FOUND,
                "Target Win32 control handle no longer exists.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        wm_settext = 0x000C
        if not user32.SendMessageW(target_hwnd, wm_settext, 0, value):
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_FAILED,
                "Win32 WM_SETTEXT failed on target control.",
                source=PerceptionSource.UI_AUTOMATION,
            )

    async def focus_node(self, window_id: str, node: RawUiaNode) -> None:
        self._require_windows()
        await asyncio.to_thread(self._sync_focus_node, window_id, node)

    def _sync_focus_node(self, window_id: str, node: RawUiaNode) -> None:
        user32 = self._get_user32()
        root_hwnd = _parse_hwnd(window_id)
        target_hwnd = _parse_hwnd(node.node_id if node.node_id.startswith("hwnd-") else window_id)
        if not bool(user32.IsWindow(target_hwnd)):
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_NOT_FOUND,
                "Target Win32 control handle no longer exists.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        user32.SetForegroundWindow(root_hwnd)
        wm_setfocus = 0x0007
        user32.SendMessageW(target_hwnd, wm_setfocus, 0, 0)

    async def send_keys(self, window_id: str, node: RawUiaNode | None, key: str) -> None:
        self._require_windows()
        await asyncio.to_thread(self._sync_send_keys, window_id, node, key)

    def _sync_send_keys(self, window_id: str, node: RawUiaNode | None, key: str) -> None:
        user32 = self._get_user32()
        target_hwnd = _parse_hwnd(
            node.node_id if node is not None and node.node_id.startswith("hwnd-") else window_id
        )
        if not bool(user32.IsWindow(target_hwnd)):
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_NOT_FOUND,
                "Target Win32 control handle no longer exists.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        vk_map = {
            "enter": 0x0D,
            "return": 0x0D,
            "tab": 0x09,
            "escape": 0x1B,
            "esc": 0x1B,
            "space": 0x20,
            "backspace": 0x08,
            "delete": 0x2E,
            "up": 0x26,
            "down": 0x28,
            "left": 0x25,
            "right": 0x27,
        }
        wm_keydown = 0x0100
        wm_keyup = 0x0101
        wm_char = 0x0102
        normalized = key.strip().lower()
        if normalized in vk_map:
            vk = vk_map[normalized]
            user32.PostMessageW(target_hwnd, wm_keydown, vk, 0)
            user32.PostMessageW(target_hwnd, wm_keyup, vk, 0)
            return
        for ch in key:
            user32.PostMessageW(target_hwnd, wm_char, ord(ch), 0)


@dataclass(slots=True)
class _WindowObservation:
    window_id: str
    process_id: int | None
    target_fingerprint: str | None
    state_hash: str
    display_topology_hash: str
    foreground_window_id: str | None
    cursor_position: Point | None
    observed_monotonic: float
    expires_at: float
    elements: tuple[AccessibilityElement, ...]
    candidates: tuple[TargetCandidate, ...]
    raw_nodes: Mapping[str, RawUiaNode]


class WindowsUiaProvider:
    """Grounded Windows UI Automation provider implementing observation, resolution, and actions."""

    def __init__(
        self,
        *,
        backend: WindowsUiaBackend | None = None,
        secret_provider: SecretProvider | None = None,
        resolver: TargetResolver | None = None,
        application_resolver: Any | None = None,
        observation_lease_seconds: float = 5.0,
        default_timeout_seconds: float = 10.0,
        max_tree_nodes: int = _MAX_TREE_NODES,
        max_tree_depth: int = _MAX_TREE_DEPTH,
        allow_stale_regrounding: bool = True,
        allow_coordinate_fallback: bool = False,
        unsafe_regions: tuple[Rect, ...] = (),
    ) -> None:
        if not 0.2 <= observation_lease_seconds <= 60.0:
            raise ValueError("observation_lease_seconds must be between 0.2 and 60")
        if not 0.2 <= default_timeout_seconds <= 120.0:
            raise ValueError("default_timeout_seconds must be between 0.2 and 120")
        if not 1 <= max_tree_nodes <= _MAX_TREE_NODES:
            raise ValueError(f"max_tree_nodes must be between 1 and {_MAX_TREE_NODES}")
        if not 1 <= max_tree_depth <= _MAX_TREE_DEPTH:
            raise ValueError(f"max_tree_depth must be between 1 and {_MAX_TREE_DEPTH}")
        self._backend: WindowsUiaBackend = backend or Win32UiaBackend()
        self._secrets = secret_provider
        self._resolver = resolver or TargetResolver()
        self.application_resolver = application_resolver
        self.observation_lease_seconds = observation_lease_seconds
        self.default_timeout_seconds = default_timeout_seconds
        if any(not isinstance(region, Rect) for region in unsafe_regions):
            raise ValueError("unsafe_regions must contain only Rect values")
        self.max_tree_nodes = max_tree_nodes
        self.max_tree_depth = max_tree_depth
        self.allow_stale_regrounding = allow_stale_regrounding
        self.allow_coordinate_fallback = allow_coordinate_fallback
        self.unsafe_regions = tuple(unsafe_regions)
        self._observations: OrderedDict[str, _WindowObservation] = OrderedDict()
        self._change_detector = EnvironmentChangeDetector()
        self._reground_count = 0

    @property
    def reground_count(self) -> int:
        return self._reground_count

    async def displays(self) -> Sequence[DisplayGeometry]:
        displays = tuple(await self._backend.list_displays())
        if not displays:
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "No Windows display geometry is available.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        return displays

    async def dpi_for_display(self, display_id: str) -> tuple[float, float]:
        for display in await self.displays():
            if display.display_id == display_id:
                if not display.dpi_available:
                    raise ComputerAdapterError(
                        ComputerFailureCode.INVALID_COORDINATE,
                        "Monitor DPI was not measured; coordinate conversion is unsafe.",
                        source=PerceptionSource.UI_AUTOMATION,
                    )
                return display.dpi_x, display.dpi_y
        raise ComputerAdapterError(
            ComputerFailureCode.INVALID_TARGET,
            "Requested display_id was not found in active display topology.",
            source=PerceptionSource.UI_AUTOMATION,
        )

    async def dpi_for_window(self, window_id: str) -> tuple[float, float]:
        windows = await self.list_windows(include_hidden=True)
        target_win = next((w for w in windows if w.window_id == window_id), None)
        if target_win is None:
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Requested window_id was not found.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        displays = await self.displays()
        if target_win.bounds is not None:
            center = target_win.bounds.center
            for display in displays:
                if display.physical_bounds.contains(center) or display.physical_bounds.intersects(
                    target_win.bounds
                ):
                    if not display.dpi_available:
                        raise ComputerAdapterError(
                            ComputerFailureCode.INVALID_COORDINATE,
                            "Monitor DPI for window is unavailable; coordinate math is unsafe.",
                            source=PerceptionSource.UI_AUTOMATION,
                        )
                    return display.dpi_x, display.dpi_y
        primary = next((d for d in displays if d.primary), displays[0])
        if not primary.dpi_available:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_COORDINATE,
                "Primary monitor DPI is unavailable; coordinate math is unsafe.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        return primary.dpi_x, primary.dpi_y

    def normalize_bounds_to_logical(self, bounds: Rect, display: DisplayGeometry) -> Rect:
        """Normalize physical virtual-desktop bounds into monitor-logical DPI-aware bounds."""
        try:
            top_left = CoordinateMapper.physical_to_monitor_logical(
                Point(bounds.x, bounds.y), display
            )
            scale_x = display.scale_x
            scale_y = display.scale_y
        except ValueError as exc:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_COORDINATE,
                str(exc),
                source=PerceptionSource.UI_AUTOMATION,
            ) from exc
        return Rect(top_left.x, top_left.y, bounds.width / scale_x, bounds.height / scale_y)

    def logical_bounds_to_physical(self, bounds: Rect, display: DisplayGeometry) -> Rect:
        """Convert monitor-logical bounds into physical virtual-desktop pixel bounds."""
        try:
            top_left = CoordinateMapper.monitor_logical_to_physical(
                Point(bounds.x, bounds.y), display
            )
            scale_x = display.scale_x
            scale_y = display.scale_y
        except ValueError as exc:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_COORDINATE,
                str(exc),
                source=PerceptionSource.UI_AUTOMATION,
            ) from exc
        return Rect(top_left.x, top_left.y, bounds.width * scale_x, bounds.height * scale_y)

    async def list_windows(self, *, include_hidden: bool = False) -> Sequence[WindowRecord]:
        raw_windows = await self._backend.list_windows(include_hidden=include_hidden)
        sanitized: list[WindowRecord] = []
        for win in raw_windows:
            if not include_hidden and not win.visible:
                continue
            sanitized.append(
                WindowRecord(
                    window_id=win.window_id,
                    process_id=win.process_id,
                    title=DEFAULT_REDACTOR.redact(win.title)[:2048],
                    application=win.application,
                    visible=win.visible,
                    minimized=win.minimized,
                    maximized=win.maximized,
                    foreground=win.foreground,
                    bounds=win.bounds,
                    class_name=win.class_name,
                    executable_path=None,
                    package_family_name=win.package_family_name,
                    aumid=win.aumid,
                )
            )
        return tuple(sanitized)

    async def foreground_window(self) -> WindowRecord | None:
        win = await self._backend.foreground_window()
        if win is None:
            return None
        return WindowRecord(
            window_id=win.window_id,
            process_id=win.process_id,
            title=DEFAULT_REDACTOR.redact(win.title)[:2048],
            application=win.application,
            visible=win.visible,
            minimized=win.minimized,
            maximized=win.maximized,
            foreground=win.foreground,
            bounds=win.bounds,
            class_name=win.class_name,
            executable_path=None,
            package_family_name=win.package_family_name,
            aumid=win.aumid,
        )

    async def focus_window(self, window_id: str) -> WindowRecord:
        validate_safe_token(window_id, "window_id")
        return await self._backend.focus_window(window_id)

    async def inspect_tree(
        self,
        window_id: str,
        *,
        max_depth: int = _MAX_TREE_DEPTH,
        max_nodes: int = _MAX_TREE_NODES,
    ) -> Sequence[AccessibilityElement]:
        validate_safe_token(window_id, "window_id")
        observation_id = f"uia-obs-{uuid.uuid4().hex[:16]}"
        record = await self._capture_window(
            window_id,
            observation_id,
            max_depth=max_depth,
            max_nodes=max_nodes,
            remember=True,
        )
        return record.elements

    async def inspect(
        self,
        window_id: str,
        *,
        max_depth: int = _MAX_TREE_DEPTH,
        max_nodes: int = _MAX_TREE_NODES,
    ) -> Sequence[TargetCandidate]:
        validate_safe_token(window_id, "window_id")
        observation_id = f"uia-obs-{uuid.uuid4().hex[:16]}"
        record = await self._capture_window(
            window_id,
            observation_id,
            max_depth=max_depth,
            max_nodes=max_nodes,
            remember=True,
        )
        return record.candidates

    async def resolve(self, query: TargetQuery) -> TargetResolution:
        window_id = query.window_id
        if window_id is None:
            fg = await self.foreground_window()
            if fg is None:
                return TargetResolution(
                    ResolutionStatus.NOT_FOUND,
                    (),
                    reason="No foreground Windows UIA window is available.",
                )
            window_id = fg.window_id
        candidates = await self.inspect(window_id)
        return self._resolver.resolve(query, candidates)

    async def reground_stale_target(self, identity: TargetIdentity) -> TargetCandidate:
        """Re-inspect the target window and re-resolve a stale UIA target by semantic anchor."""

        window_id = identity.window_id
        if window_id is None:
            fg = await self.foreground_window()
            window_id = fg.window_id if fg is not None else None
        if window_id is None:
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Cannot reground stale UIA target without an active window.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        candidates = await self.inspect(window_id)
        exact = [
            item
            for item in candidates
            if item.descriptor.identity.fingerprint == identity.fingerprint
            and item.descriptor.visible
            and item.descriptor.enabled
        ]
        if len(exact) == 1:
            return exact[0]
        if identity.stable_id:
            candidates = [
                item
                for item in candidates
                if item.descriptor.identity.stable_id == identity.stable_id
                and item.descriptor.visible
                and item.descriptor.enabled
                and (identity.role is None or item.descriptor.identity.role == identity.role)
            ]
            if not identity.semantic_name and len(candidates) == 1:
                return candidates[0]
        if identity.semantic_name:
            resolution = self._resolver.resolve(
                TargetQuery(
                    semantic_name=identity.semantic_name,
                    role=identity.role,
                    window_id=window_id,
                    allowed_sources=(PerceptionSource.UI_AUTOMATION,),
                ),
                candidates,
            )
            if resolution.status is ResolutionStatus.RESOLVED and resolution.selected is not None:
                return resolution.selected
            if resolution.status is ResolutionStatus.AMBIGUOUS:
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_AMBIGUOUS,
                    "Stale UIA target is ambiguous after re-observation.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
        raise ComputerAdapterError(
            ComputerFailureCode.TARGET_STALE,
            "Semantic control was not found in the observed Win32 HWND tree; "
            "custom accessibility controls (including Chrome omnibox) may not be exposed.",
            source=PerceptionSource.UI_AUTOMATION,
        )

    async def observe(self, action: ActionContract) -> ObservationLease:
        target = action.target
        window_id = target.window_id if target is not None else None
        if window_id is None:
            fg = await self.foreground_window()
            if fg is None:
                raise ComputerAdapterError(
                    ComputerFailureCode.WINDOW_NOT_FOUND,
                    "No foreground window is available to observe.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            window_id = fg.window_id
        validate_safe_token(window_id, "window_id")
        lease_id = f"uia-lease-{uuid.uuid4().hex[:16]}"
        record = await self._capture_window(
            window_id,
            lease_id,
            max_depth=self.max_tree_depth,
            max_nodes=self.max_tree_nodes,
            remember=False,
        )
        target_fingerprint = target.fingerprint if target is not None else window_id
        record.target_fingerprint = target_fingerprint
        self._remember(lease_id, record)
        now = utc_now()
        # This fact is based on the backend's observed keyboard-focus state, not
        # the foreground-window flag. Backends that cannot identify a focused
        # named element report None; the verifier never infers focus from a click.
        focused_names = [el.name for el in record.elements if el.focused and el.name]
        facts: dict[str, Any] = {
            "window.id": window_id,
            "window.foreground": record.foreground_window_id == window_id,
            "window.element_count": len(record.elements),
            "window.focused_element": focused_names[0] if focused_names else None,
            "display.topology_hash": record.display_topology_hash,
            "uia.state_hash": record.state_hash,
        }
        for el in record.elements:
            if el.name:
                key = f"uia.element.{el.control_type.lower()}.{el.name}"
                facts[key] = True
                if not el.sensitive and el.value is not None:
                    facts[f"{key}.value"] = el.value
        return ObservationLease(
            lease_id=lease_id,
            target_fingerprint=target_fingerprint,
            state_hash=record.state_hash,
            created_at=now,
            expires_at=now + timedelta(seconds=self.observation_lease_seconds),
            monotonic_deadline=record.expires_at,
            facts=facts,
            source=EvidenceSource.OBSERVED,
        )

    async def is_current(self, observation: ObservationLease) -> bool:
        record = self._observations.get(observation.lease_id)
        deadline = (
            min(record.expires_at, observation.monotonic_deadline) if record is not None else 0.0
        )
        if record is None or time.monotonic() >= deadline:
            return False
        if record.state_hash != observation.state_hash:
            return False
        try:
            if await self._backend.user_input_observed_since(record.observed_monotonic):
                return False
            current_displays = await self.displays()
            if self._hash_displays(current_displays) != record.display_topology_hash:
                return False
            fg = await self.foreground_window()
            if fg is None or fg.window_id != record.window_id:
                return False
            fresh = await self._capture_window(
                record.window_id,
                observation.lease_id,
                max_depth=self.max_tree_depth,
                max_nodes=self.max_tree_nodes,
                remember=False,
            )
        except ComputerAdapterError:
            return False
        return fresh.state_hash == record.state_hash

    def candidates_for_observation(
        self, observation: ObservationLease
    ) -> tuple[TargetCandidate, ...]:
        record = self._observations.get(observation.lease_id)
        if record is None or time.monotonic() >= record.expires_at:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "The Windows UIA observation lease is missing or expired.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        return record.candidates

    async def invoke(self, target: TargetCandidate, *, timeout_seconds: float = 5.0) -> str:
        window_id, fresh_candidate, raw_node, display = await self._resolve_fresh(
            target, timeout_seconds=timeout_seconds
        )
        click_point: Point | None = None
        if "InvokePattern" not in raw_node.supported_patterns:
            if not self.allow_coordinate_fallback:
                raise ComputerAdapterError(
                    ComputerFailureCode.POLICY_DENIED,
                    "Coordinate click fallback is disabled; configure explicit opt-in first.",
                    source=PerceptionSource.COORDINATE,
                )
            if fresh_candidate.descriptor.bounds is None:
                raise ComputerAdapterError(
                    ComputerFailureCode.INVALID_COORDINATE,
                    "Control does not support InvokePattern and has no verified bounds.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            if not display.dpi_available:
                raise ComputerAdapterError(
                    ComputerFailureCode.INVALID_COORDINATE,
                    "Monitor DPI is unverified; refusing coordinate click fallback.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            try:
                click_point = CoordinateMapper.safe_click_point(
                    fresh_candidate.descriptor.bounds,
                    unsafe_regions=self.unsafe_regions,
                )
            except ValueError as exc:
                raise ComputerAdapterError(
                    ComputerFailureCode.INVALID_COORDINATE,
                    str(exc),
                    source=PerceptionSource.UI_AUTOMATION,
                ) from exc
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._backend.invoke_node(window_id, raw_node, click_point=click_point)
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Windows UIA invoke timed out after dispatch; outcome is unknown.",
                retryable=False,
                source=PerceptionSource.UI_AUTOMATION,
            ) from None
        except ComputerAdapterError:
            raise
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Windows UIA invoke failed during execution; outcome is unknown.",
                retryable=False,
                source=PerceptionSource.UI_AUTOMATION,
            ) from None
        return "Windows UIA control invoked; postconditions must verify the result."

    async def click(self, target: TargetCandidate, *, timeout_seconds: float = 5.0) -> str:
        return await self.invoke(target, timeout_seconds=timeout_seconds)

    async def set_value(
        self,
        target: TargetCandidate,
        value: SensitiveText,
        *,
        timeout_seconds: float = 5.0,
    ) -> str:
        window_id, _fresh_candidate, raw_node, _display = await self._resolve_fresh(
            target, timeout_seconds=timeout_seconds
        )
        is_sensitive = bool(
            raw_node.sensitive or target.descriptor.identity.locator.get("sensitive", False)
        )
        if is_sensitive and not isinstance(value, SecretRef):
            raise ComputerAdapterError(
                ComputerFailureCode.PERMISSION_DENIED,
                "Plaintext input is forbidden for sensitive/password UIA controls; use SecretRef.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        if isinstance(value, SecretRef) and not is_sensitive:
            raise ComputerAdapterError(
                ComputerFailureCode.PERMISSION_DENIED,
                "SecretRef may only be entered into a sensitive/password UIA control.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        resolved_text = self._resolve_sensitive_text(value)
        if "ValuePattern" not in raw_node.supported_patterns:
            raise ComputerAdapterError(
                ComputerFailureCode.ELEMENT_NOT_INTERACTABLE,
                "The target UIA control does not support ValuePattern text entry.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._backend.set_node_value(window_id, raw_node, resolved_text)
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Windows UIA set_value timed out after dispatch; outcome is unknown.",
                retryable=False,
                source=PerceptionSource.UI_AUTOMATION,
            ) from None
        except ComputerAdapterError:
            raise
        except Exception:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Windows UIA set_value failed during dispatch; outcome is unknown.",
                retryable=False,
                source=PerceptionSource.UI_AUTOMATION,
            ) from None
        return "Windows UIA value set; postconditions must verify the result."

    async def focus(self, target: TargetCandidate, *, timeout_seconds: float = 5.0) -> str:
        window_id, _fresh_candidate, raw_node, _display = await self._resolve_fresh(
            target, timeout_seconds=timeout_seconds, require_foreground=False
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._backend.focus_window(window_id)
                await self._backend.focus_node(window_id, raw_node)
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.TIMEOUT,
                "Windows UIA focus operation timed out.",
                retryable=True,
                source=PerceptionSource.UI_AUTOMATION,
            ) from None
        return "Windows UIA focus updated."

    async def press(
        self, target: TargetCandidate, key: str, *, timeout_seconds: float = 5.0
    ) -> str:
        if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Windows UIA key descriptor must be a bounded key or chord name.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        window_id, _fresh_candidate, raw_node, _display = await self._resolve_fresh(
            target, timeout_seconds=timeout_seconds
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._backend.send_keys(window_id, raw_node, key)
        except TimeoutError:
            raise ComputerAdapterError(
                ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                "Windows UIA key press timed out after dispatch; outcome is unknown.",
                retryable=False,
                source=PerceptionSource.UI_AUTOMATION,
            ) from None
        return "Windows UIA key press dispatched; postconditions must verify the result."

    async def verify(
        self, action: ActionContract, outcome: ExecutionOutcome | None = None
    ) -> VerificationResult:
        del outcome
        try:
            observation = await self.observe(action)
        except ComputerAdapterError as exc:
            return VerificationResult(
                status=VerificationStatus.UNKNOWN,
                level=0,
                summary=f"Could not reobserve Windows UIA state ({exc.code.value}).",
            )
        missing: list[str] = []
        for condition in action.postconditions:
            if not condition.evaluate(observation.facts):
                missing.append(condition.description or condition.key)
        if missing:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                level=1,
                summary=f"Unmet Windows UIA postconditions: {', '.join(missing)}",
                evidence=(
                    EvidenceRecord(
                        source="observed",
                        observation_id=observation.lease_id,
                        state_hash=observation.state_hash,
                        statement="Windows UIA postcondition check failed.",
                    ),
                ),
            )
        return VerificationResult(
            status=VerificationStatus.PASSED,
            level=2,
            summary="All Windows UIA postconditions were verified from fresh observation.",
            evidence=(
                EvidenceRecord(
                    source="observed",
                    observation_id=observation.lease_id,
                    state_hash=observation.state_hash,
                    statement="All Windows UIA postconditions matched observed control state.",
                ),
            ),
        )

    def _resolve_sensitive_text(self, value: SensitiveText) -> str:
        if isinstance(value, SecretRef):
            if self._secrets is None:
                raise ComputerAdapterError(
                    ComputerFailureCode.PERMISSION_DENIED,
                    "No secret provider is configured for UIA SecretRef resolution.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            try:
                secret = self._secrets.get_secret(value.name)
            except SecretUnavailable:
                raise ComputerAdapterError(
                    ComputerFailureCode.PERMISSION_DENIED,
                    "Referenced UIA secret is unavailable.",
                    source=PerceptionSource.UI_AUTOMATION,
                ) from None
            except Exception:
                raise ComputerAdapterError(
                    ComputerFailureCode.PERMISSION_DENIED,
                    "Referenced UIA secret could not be resolved.",
                    source=PerceptionSource.UI_AUTOMATION,
                ) from None
            if not isinstance(secret, str) or not secret or len(secret) > 16_384:
                raise ComputerAdapterError(
                    ComputerFailureCode.PERMISSION_DENIED,
                    "Referenced UIA secret is empty or exceeds the maximum length.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            return secret
        if not isinstance(value, str) or len(value) > 16_384:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "UIA text must be a bounded string or SecretRef.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        return value

    async def _resolve_fresh(
        self,
        candidate: TargetCandidate,
        *,
        timeout_seconds: float,
        require_foreground: bool = True,
    ) -> tuple[str, TargetCandidate, RawUiaNode, DisplayGeometry]:
        identity = candidate.descriptor.identity
        window_id = identity.window_id
        if identity.platform != "windows" or not window_id:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Windows UIA actions require a window-scoped Windows TargetIdentity.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        record = self._observations.get(candidate.descriptor.observation_id)
        if record is None or time.monotonic() >= record.expires_at or record.window_id != window_id:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "The Windows UIA target observation lease is missing or expired.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        if await self._backend.user_input_observed_since(record.observed_monotonic):
            self._observations.pop(candidate.descriptor.observation_id, None)
            raise ComputerAdapterError(
                ComputerFailureCode.USER_INTERFERENCE,
                "Human mouse or keyboard input was detected after observation; "
                "aborting UIA dispatch.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        current_cursor = await self._backend.cursor_position()
        if (
            record.cursor_position is not None
            and current_cursor is not None
            and (
                abs(current_cursor.x - record.cursor_position.x) > 6.0
                or abs(current_cursor.y - record.cursor_position.y) > 6.0
            )
        ):
            self._observations.pop(candidate.descriptor.observation_id, None)
            raise ComputerAdapterError(
                ComputerFailureCode.USER_INTERFERENCE,
                "Cursor moved unexpectedly since observation; aborting UIA dispatch.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        displays = await self.displays()
        current_display_hash = self._hash_displays(displays)
        if current_display_hash != record.display_topology_hash:
            self._observations.pop(candidate.descriptor.observation_id, None)
            raise ComputerAdapterError(
                ComputerFailureCode.ENVIRONMENT_CHANGED,
                "Display topology or DPI scaling changed since target observation.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        fg = await self.foreground_window()
        if require_foreground and (fg is None or fg.window_id != window_id):
            raise ComputerAdapterError(
                ComputerFailureCode.ENVIRONMENT_CHANGED,
                "Target window is no longer the foreground window; focus changed.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        async with asyncio.timeout(timeout_seconds):
            fresh_record = await self._capture_window(
                window_id,
                candidate.descriptor.observation_id,
                max_depth=self.max_tree_depth,
                max_nodes=self.max_tree_nodes,
                remember=False,
            )
        if (
            record.process_id is not None
            and fresh_record.process_id is not None
            and fresh_record.process_id != record.process_id
        ):
            raise ComputerAdapterError(
                ComputerFailureCode.ENVIRONMENT_CHANGED,
                "Window process identity changed since observation.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        if fresh_record.state_hash != record.state_hash:
            if (
                candidate.descriptor.source is PerceptionSource.COORDINATE
                or not self.allow_stale_regrounding
            ):
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_STALE,
                    "Windows UIA control tree changed after observation; "
                    "coordinate/strict target is stale.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            fresh_match = self._reground_candidate(candidate, fresh_record)
        else:
            matches = [
                item
                for item in fresh_record.candidates
                if item.descriptor.identity.fingerprint == identity.fingerprint
            ]
            if not matches:
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_NOT_FOUND,
                    "The grounded Windows UIA element no longer exists.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            if len(matches) != 1:
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_AMBIGUOUS,
                    "The grounded Windows UIA identity matches multiple controls.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            fresh_match = matches[0]

        if not fresh_match.descriptor.visible or not fresh_match.descriptor.enabled:
            raise ComputerAdapterError(
                ComputerFailureCode.ELEMENT_NOT_INTERACTABLE,
                "The grounded Windows UIA control is hidden or disabled.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        if (
            candidate.descriptor.source is PerceptionSource.COORDINATE
            and candidate.descriptor.bounds != fresh_match.descriptor.bounds
        ):
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_STALE,
                "Coordinate target bounds moved after observation; rejecting stale coordinates.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        raw_node = fresh_record.raw_nodes.get(fresh_match.descriptor.identity.fingerprint)
        if raw_node is None:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_NOT_FOUND,
                "The native UIA node handle is no longer present.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        primary_display = next((d for d in displays if d.primary), displays[0])
        return window_id, fresh_match, raw_node, primary_display

    def _reground_candidate(
        self, stale_candidate: TargetCandidate, fresh_record: _WindowObservation
    ) -> TargetCandidate:
        identity = stale_candidate.descriptor.identity
        exact_matches = [
            item
            for item in fresh_record.candidates
            if item.descriptor.identity.fingerprint == identity.fingerprint
            and item.descriptor.visible
            and item.descriptor.enabled
        ]
        if len(exact_matches) == 1:
            if (
                stale_candidate.descriptor.bounds is not None
                and exact_matches[0].descriptor.bounds != stale_candidate.descriptor.bounds
                and stale_candidate.descriptor.selector_quality is SelectorQuality.COORDINATE
            ):
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_STALE,
                    "Stale coordinate target cannot be re-grounded after bounds change.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            self._reground_count += 1
            return exact_matches[0]
        if len(exact_matches) > 1:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_AMBIGUOUS,
                "Stale UIA target matches multiple controls on re-grounding.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        raise ComputerAdapterError(
            ComputerFailureCode.TARGET_STALE,
            "The Windows UIA control tree changed and the target could not "
            "be uniquely re-grounded.",
            source=PerceptionSource.UI_AUTOMATION,
        )

    async def _capture_window(
        self,
        window_id: str,
        observation_id: str,
        *,
        max_depth: int,
        max_nodes: int,
        remember: bool,
    ) -> _WindowObservation:
        displays = await self.displays()
        display_hash = self._hash_displays(displays)
        primary_display = next((d for d in displays if d.primary), displays[0])
        windows = await self.list_windows(include_hidden=True)
        win = next((w for w in windows if w.window_id == window_id), None)
        if win is None:
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Target Windows UIA window does not exist.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        fg = await self.foreground_window()
        cursor = await self._backend.cursor_position()
        raw_nodes = tuple(
            await self._backend.inspect_window_nodes(
                window_id, max_depth=max_depth, max_nodes=max_nodes
            )
        )[:max_nodes]
        elements: list[AccessibilityElement] = []
        candidates: list[TargetCandidate] = []
        raw_map: dict[str, RawUiaNode] = {}
        digest_rows: list[dict[str, Any]] = []
        observed_at = utc_now()
        for node in raw_nodes:
            element, candidate, digest_row = self._normalize_node(
                node,
                observation_id=observation_id,
                observed_at=observed_at,
                display=primary_display,
            )
            elements.append(element)
            candidates.append(candidate)
            raw_map[candidate.descriptor.identity.fingerprint] = node
            digest_rows.append(digest_row)
        state_payload = {
            "window_id": window_id,
            "process_id": win.process_id,
            "title": win.title,
            "bounds": (
                [win.bounds.x, win.bounds.y, win.bounds.width, win.bounds.height]
                if win.bounds is not None
                else None
            ),
            "display_hash": display_hash,
            "nodes": digest_rows,
        }
        state_hash = hashlib.sha256(canonical_json(state_payload).encode("utf-8")).hexdigest()
        now_mono = time.monotonic()
        record = _WindowObservation(
            window_id=window_id,
            process_id=win.process_id,
            target_fingerprint=None,
            state_hash=state_hash,
            display_topology_hash=display_hash,
            foreground_window_id=fg.window_id if fg is not None else None,
            cursor_position=cursor,
            observed_monotonic=now_mono,
            expires_at=now_mono + self.observation_lease_seconds,
            elements=tuple(elements),
            candidates=tuple(candidates),
            raw_nodes=raw_map,
        )
        self._change_detector.update(
            EnvironmentFingerprint(
                foreground_window_id=record.foreground_window_id,
                process_id=win.process_id,
                display_topology_hash=display_hash,
                accessibility_hash=state_hash,
                cursor_position=cursor,
            ),
            detected_at=observed_at,
        )
        if remember:
            self._remember(observation_id, record)
        return record

    def _normalize_node(
        self,
        node: RawUiaNode,
        *,
        observation_id: str,
        observed_at: datetime,
        display: DisplayGeometry,
    ) -> tuple[AccessibilityElement, TargetCandidate, dict[str, Any]]:
        safe_name = DEFAULT_REDACTOR.redact(node.name).strip()[:512]
        safe_role = (node.role or node.control_type or "control").strip().lower()[:64]
        safe_control_type = (node.control_type or "Control").strip()[:128]
        safe_auto_id = (
            DEFAULT_REDACTOR.redact(node.automation_id).strip()[:256]
            if node.automation_id
            else None
        )
        is_sensitive = bool(
            node.sensitive
            or "password" in safe_name.casefold()
            or (safe_auto_id and "password" in safe_auto_id.casefold())
        )
        safe_value = (
            None
            if is_sensitive or node.value is None
            else DEFAULT_REDACTOR.redact(node.value).strip()[:1024]
        )
        physical_bounds = node.bounds
        if physical_bounds is not None and node.coordinate_space is CoordinateSpace.MONITOR_LOGICAL:
            physical_bounds = self.logical_bounds_to_physical(physical_bounds, display)
        if safe_role and safe_name:
            quality = SelectorQuality.EXACT_ACCESSIBLE_ROLE_NAME
        elif safe_auto_id:
            quality = SelectorQuality.STABLE_ATTRIBUTE
        elif safe_name:
            quality = SelectorQuality.EXACT_TEXT
        else:
            quality = SelectorQuality.STRUCTURAL
        stable_id = safe_auto_id or node.node_id
        locator: dict[str, Any] = {
            "role": safe_role,
            "name": safe_name,
            "automation_id": safe_auto_id,
            "control_type": safe_control_type,
            "class_name": node.class_name,
            "sensitive": is_sensitive,
            "patterns": list(node.supported_patterns),
        }
        identity = TargetIdentity(
            platform="windows",
            application=node.application,
            process_id=node.process_id,
            window_id=node.window_id,
            object_id=stable_id,
            role=safe_role,
            semantic_name=safe_name or None,
            stable_id=stable_id,
            locator=locator,
        )
        descriptor = TargetDescriptor(
            identity=identity,
            source=PerceptionSource.UI_AUTOMATION,
            observed_at=observed_at,
            observation_id=observation_id,
            bounds=physical_bounds,
            coordinate_space=CoordinateSpace.PHYSICAL_DESKTOP if physical_bounds else None,
            selector_quality=quality,
            visible=bool(node.visible),
            enabled=bool(node.enabled),
            automation_id=safe_auto_id,
            runtime_id=node.runtime_id,
            hierarchy=tuple(DEFAULT_REDACTOR.redact(h)[:128] for h in node.hierarchy[:16] if h),
            class_name=node.class_name,
            framework_id=node.framework_id,
        )
        element = AccessibilityElement(
            target=descriptor,
            control_type=safe_control_type,
            name=safe_name,
            value=safe_value,
            enabled=bool(node.enabled),
            visible=bool(node.visible),
            focused=bool(node.focused),
            selected=node.selected,
            expanded=node.expanded,
            toggle_state=node.toggle_state,
            supported_patterns=node.supported_patterns,
            parent_fingerprint=None,
            child_count=max(0, node.child_count),
            sensitive=is_sensitive,
        )
        candidate = TargetCandidate(
            descriptor=descriptor,
            confidence=quality.score,
            evidence=("windows UIA control tree", "sensitive values redacted"),
        )
        digest_row = {
            "node_id": node.node_id,
            "role": safe_role,
            "name": safe_name,
            "automation_id": safe_auto_id,
            "enabled": bool(node.enabled),
            "visible": bool(node.visible),
            "focused": bool(node.focused),
            "value": safe_value,
            "bounds": (
                [
                    physical_bounds.x,
                    physical_bounds.y,
                    physical_bounds.width,
                    physical_bounds.height,
                ]
                if physical_bounds is not None
                else None
            ),
        }
        return element, candidate, digest_row

    @staticmethod
    def _hash_displays(displays: Sequence[DisplayGeometry]) -> str:
        payload = [
            {
                "id": d.display_id,
                "bounds": [
                    d.physical_bounds.x,
                    d.physical_bounds.y,
                    d.physical_bounds.width,
                    d.physical_bounds.height,
                ],
                "dpi_x": d.dpi_x,
                "dpi_y": d.dpi_y,
                "primary": d.primary,
                "dpi_available": d.dpi_available,
            }
            for d in displays
        ]
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def _remember(self, observation_id: str, record: _WindowObservation) -> None:
        self._observations[observation_id] = record
        self._observations.move_to_end(observation_id)
        now = time.monotonic()
        for key, value in tuple(self._observations.items()):
            if value.expires_at <= now:
                self._observations.pop(key, None)
        while len(self._observations) > _MAX_OBSERVATIONS:
            self._observations.popitem(last=False)


class WindowsUiaActionTool:
    """Policy-facing tool for one operation on the Windows UI Automation adapter."""

    _POLICY = {
        "invoke": (RiskLevel.R2, Idempotency.UNKNOWN, "invokes a grounded Windows UIA control"),
        "click": (RiskLevel.R3, Idempotency.UNKNOWN, "clicks a grounded Windows UIA control"),
        "fill": (RiskLevel.R2, Idempotency.UNKNOWN, "sets non-secret text in a UIA control"),
        "fill_secret": (
            RiskLevel.R3,
            Idempotency.UNKNOWN,
            "sets a referenced secret in a UIA control",
        ),
        "focus": (RiskLevel.R1, Idempotency.IDEMPOTENT, "focuses a grounded Windows UIA control"),
        "press": (RiskLevel.R3, Idempotency.UNKNOWN, "sends a bounded key chord to a UIA control"),
    }
    # Trusted parameter keys per operation, mirrored from validate_parameters.
    _PARAMETERS = {
        "invoke": (),
        "click": (),
        "fill": ("text",),
        "fill_secret": ("text",),
        "focus": (),
        "press": ("key",),
    }

    def __init__(self, provider: WindowsUiaProvider, operation: str) -> None:
        if operation not in self._POLICY:
            raise ValueError("unsupported Windows UIA operation")
        self.provider = provider
        self.operation = operation
        risk, idempotency, effect = self._POLICY[operation]
        self._spec = ToolSpec(
            name=f"uia.{operation}",
            version="1.0.0",
            description=(
                f"{operation.title()} a grounded control via Windows UI Automation. The native "
                "runtime grounds semantic application/control targets before locking and approval; "
                "never invent window IDs. The backend can verify keyboard focus only for a "
                "named HWND in the inspected tree; custom-drawn controls may not be identifiable."
            ),
            minimum_risk=risk,
            required_capabilities=frozenset({"desktop.ui_automation"}),
            required_resources=(),
            declared_side_effects=(effect,),
            idempotency=idempotency,
            max_result_bytes=4096,
            parameter_names=self._PARAMETERS[operation],
            target_scope="windows.window_id",
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def ground_action(self, action: ActionContract) -> ActionContract:
        """Bind a semantic proposal to observed identity before locks and approval.

        Never pick an arbitrary foreground window for an application-scoped request.
        Native HWND-only inspection fails closed for inaccessible custom controls.
        """
        target = action.target
        if target is None or target.platform != "windows" or not target.has_semantic_anchor:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Windows action requires a semantic Windows target.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        from arise.adapters.windows_app_discovery import normalize_application_name
        from arise.adapters.windows_app_launch import (
            KNOWN_ALIASES,
            _executable_key,
            _safe_basename,
        )

        resolved_application = None
        alias = KNOWN_ALIASES.get(target.application.casefold()) if target.application else None
        if target.application and self.provider.application_resolver is not None:
            try:
                resolved_application = await asyncio.to_thread(
                    self.provider.application_resolver.resolve, target.application
                )
            except ComputerAdapterError as exc:
                if exc.code is ComputerFailureCode.APPLICATION_AMBIGUOUS:
                    raise

        windows = await self.provider.list_windows()
        matches = []
        for window in windows:
            if target.window_id is not None and window.window_id != target.window_id:
                continue
            if target.process_id is not None and window.process_id != target.process_id:
                continue
            if target.application:
                requested = normalize_application_name(target.application)
                if resolved_application is not None:
                    if resolved_application.package_family_name:
                        app_matches = normalize_application_name(
                            window.package_family_name or ""
                        ) == normalize_application_name(
                            resolved_application.package_family_name
                        ) or bool(
                            resolved_application.aumid
                            and window.aumid
                            and window.aumid.casefold() == resolved_application.aumid.casefold()
                        )
                    elif resolved_application.is_web_app:
                        app_matches = normalize_application_name(
                            window.title
                        ) == resolved_application.normalized_name and _executable_key(
                            window.executable_path
                        ) == _executable_key(resolved_application.executable_path)
                    else:
                        app_matches = bool(
                            resolved_application.executable_path
                            and _executable_key(window.executable_path)
                            == _executable_key(resolved_application.executable_path)
                        )
                else:
                    names = {requested}
                    if alias is not None:
                        names.update(
                            normalize_application_name(name) for name in alias.process_names
                        )
                        names.add(normalize_application_name(alias.name))
                    observed = {
                        normalize_application_name(window.application or ""),
                        normalize_application_name(_safe_basename(window.executable_path or "")),
                    }
                    app_matches = bool(names.intersection(observed))
                if not app_matches:
                    continue
            if window.visible:
                matches.append(window)
        if len(matches) != 1:
            raise ComputerAdapterError(
                ComputerFailureCode.TARGET_AMBIGUOUS
                if matches
                else ComputerFailureCode.WINDOW_NOT_FOUND,
                "Semantic target needs exactly one observed matching application window.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        candidate = await self.provider.reground_stale_target(
            replace(target, window_id=matches[0].window_id)
        )
        return replace(action, target=candidate.descriptor.identity)

    def resources_for(self, action: ActionContract) -> tuple[str, ...]:
        target = action.target
        if target is None or target.platform != "windows" or target.window_id is None:
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "Windows UIA action requires a window-scoped target identity.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        validate_safe_token(target.window_id, "window_id")
        return (f"desktop.window.{target.window_id}", "desktop.focus", "desktop.input")

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        allowed = {
            "invoke": set(),
            "click": set(),
            "fill": {"text"},
            "fill_secret": {"text"},
            "focus": set(),
            "press": {"key"},
        }[self.operation]
        if set(parameters) != allowed:
            raise ValueError("Windows UIA parameters do not match the operation schema")
        if self.operation == "fill":
            text = parameters["text"]
            if not isinstance(text, str) or len(text) > 16_384:
                raise ValueError("uia.fill requires bounded non-secret text")
        elif self.operation == "fill_secret":
            if not isinstance(parameters["text"], SecretRef):
                raise ValueError("uia.fill_secret requires a SecretRef")
        elif self.operation == "press":
            key = parameters["key"]
            if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
                raise ValueError("uia.press requires a bounded key descriptor")

    async def execute(
        self,
        action: ActionContract,
        observation: ObservationLease,
        resources: ResourceLease,
    ) -> ExecutionOutcome:
        started_at = utc_now()
        try:
            await resources.ensure_valid()
        except ResourceLeaseLost:
            return self._pre_dispatch_failure("RESOURCE_LEASE_LOST", started_at)
        try:
            target = action.target
            if target is None or target.window_id is None:
                raise ComputerAdapterError(
                    ComputerFailureCode.INVALID_TARGET,
                    "Windows UIA action lacks an explicit window target.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            candidates = self.provider.candidates_for_observation(observation)
            matches = [
                item
                for item in candidates
                if item.descriptor.identity.fingerprint == target.fingerprint
            ]
            if not matches:
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_STALE,
                    "The action target was not present in its source UIA observation.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            if len(matches) != 1:
                raise ComputerAdapterError(
                    ComputerFailureCode.TARGET_AMBIGUOUS,
                    "The action target matches multiple observed UIA elements.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
            candidate = matches[0]
            await resources.ensure_valid()
            if self.operation in {"invoke", "click"}:
                summary = await self.provider.invoke(
                    candidate, timeout_seconds=action.timeout_seconds
                )
            elif self.operation in {"fill", "fill_secret"}:
                summary = await self.provider.set_value(
                    candidate,
                    action.parameters["text"],
                    timeout_seconds=action.timeout_seconds,
                )
            elif self.operation == "focus":
                summary = await self.provider.focus(
                    candidate, timeout_seconds=action.timeout_seconds
                )
            elif self.operation == "press":
                summary = await self.provider.press(
                    candidate,
                    str(action.parameters["key"]),
                    timeout_seconds=action.timeout_seconds,
                )
            else:
                raise ComputerAdapterError(
                    ComputerFailureCode.CAPABILITY_UNAVAILABLE,
                    "Unsupported UIA operation.",
                    source=PerceptionSource.UI_AUTOMATION,
                )
        except ResourceLeaseLost:
            return self._pre_dispatch_failure("RESOURCE_LEASE_LOST", started_at)
        except ComputerAdapterError as exc:
            if exc.code is ComputerFailureCode.ACTION_UNKNOWN_OUTCOME:
                raise
            return self._pre_dispatch_failure(exc.code.value, started_at)

        await resources.ensure_valid()
        return ExecutionOutcome(
            status=ExecutionStatus.SUCCEEDED,
            summary=summary,
            side_effect_may_have_occurred=self.operation != "focus",
            result_metadata={},
            started_at=started_at,
            finished_at=utc_now(),
        )

    @staticmethod
    def _pre_dispatch_failure(code: str, started_at: datetime) -> ExecutionOutcome:
        return ExecutionOutcome(
            status=ExecutionStatus.FAILED,
            summary=f"Windows UIA action was not dispatched ({code}).",
            side_effect_may_have_occurred=False,
            result_metadata={"failure_code": code},
            started_at=started_at,
            finished_at=utc_now(),
        )


def register_windows_uia_tools(
    registry: ToolRegistry, provider: WindowsUiaProvider
) -> tuple[WindowsUiaActionTool, ...]:
    """Register separately risk-rated Windows UIA operations in a tool registry."""

    tools = tuple(
        WindowsUiaActionTool(provider, op)
        for op in ("invoke", "click", "fill", "fill_secret", "focus", "press")
    )
    for tool in tools:
        registry.register(tool)
    return tools


__all__ = [
    "RawUiaNode",
    "Win32UiaBackend",
    "WindowsUiaActionTool",
    "WindowsUiaBackend",
    "WindowsUiaProvider",
    "register_windows_uia_tools",
]
