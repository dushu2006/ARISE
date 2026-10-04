"""Windows application launch adapter with strict deterministic resolution and safety gates.

This adapter resolves semantic application names to trusted executables on the host,
launches the process without shell interpolation, and verifies the launch through fresh
observation of the application's own top-level windows. Chromium-style multi-process
applications are associated with their browser window through observed executable
identity rather than the spawned launcher PID alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import ntpath
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol

import psutil

from arise.core.computer import (
    ComputerFailureCode,
    PerceptionSource,
    RunningApplication,
    WindowRecord,
)
from arise.core.computer_ports import ApplicationProvider, ComputerAdapterError
from arise.core.contracts import (
    ActionContract,
    EvidenceSource,
    Idempotency,
    ObservationLease,
    RiskLevel,
    canonical_json,
    utc_now,
)
from arise.core.ports import (
    ActionTool,
    EvidenceRecord,
    ExecutionOutcome,
    ExecutionStatus,
    ToolRegistry,
    ToolSpec,
    VerificationResult,
    VerificationStatus,
)
from arise.core.resources import ResourceLease

_SAFE_APP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:\-\(\)]{0,127}$")
_FORBIDDEN_CHARACTERS = frozenset(';&|><$`"\n\r\t\0%{}[]*?')
_MAX_OBSERVATIONS = 64

# Bounded desktop-snapshot capacities. The native window backend stops enumerating
# at 128 visible top-level windows, so a full inventory cannot be trusted to tell a
# new window from a pre-existing one beyond that point: launch observation fails closed.
_MAX_OBSERVED_PROCESSES = 4096
_MAX_OBSERVED_WINDOWS = 128
_LAUNCH_POLL_INTERVAL_SECONDS = 0.15
_LAUNCH_REQUIRED_STABLE_CHECKS = 2
_LAUNCH_EXIT_GRACE_CHECKS = 2
_MIN_LAUNCH_TIMEOUT_SECONDS = 2.0
_MAX_LAUNCH_TIMEOUT_SECONDS = 15.0


def _safe_basename(path: str) -> str:
    """Extract file name from both Windows and POSIX paths safely."""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _executable_key(path: str | None) -> str:
    """Normalized comparison key for an observed Windows executable path.

    An empty key means the executable identity is unknown, which never matches.
    """
    if not path:
        return ""
    cleaned = str(path).strip().strip('"')
    if not cleaned:
        return ""
    return ntpath.normcase(ntpath.normpath(cleaned))


def _window_area(window: WindowRecord) -> float:
    """Deterministic area of a window's observed bounds; unknown bounds sort last."""
    bounds = window.bounds
    if bounds is None:
        return 0.0
    return max(0.0, float(bounds.width)) * max(0.0, float(bounds.height))


@dataclass(frozen=True, slots=True)
class KnownAppAlias:
    name: str
    executables: tuple[str, ...]
    process_names: tuple[str, ...]
    allow_reuse: bool = True
    standard_windows_paths: tuple[str, ...] = ()


KNOWN_ALIASES: dict[str, KnownAppAlias] = {
    "chrome": KnownAppAlias(
        name="Google Chrome",
        executables=("chrome.exe", "chrome"),
        process_names=("chrome.exe", "chrome"),
        standard_windows_paths=(
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        ),
    ),
    "google chrome": KnownAppAlias(
        name="Google Chrome",
        executables=("chrome.exe", "chrome"),
        process_names=("chrome.exe", "chrome"),
        standard_windows_paths=(
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        ),
    ),
    "notepad": KnownAppAlias(
        name="Notepad",
        executables=("notepad.exe", "notepad"),
        process_names=("notepad.exe", "Notepad.exe", "notepad"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\notepad.exe",
            r"%SystemRoot%\notepad.exe",
        ),
    ),
    "notepad.exe": KnownAppAlias(
        name="Notepad",
        executables=("notepad.exe", "notepad"),
        process_names=("notepad.exe", "Notepad.exe", "notepad"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\notepad.exe",
            r"%SystemRoot%\notepad.exe",
        ),
    ),
    "calculator": KnownAppAlias(
        name="Calculator",
        executables=("calc.exe", "calc", "CalculatorApp.exe"),
        process_names=("calc.exe", "CalculatorApp.exe", "Calculator.exe"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\calc.exe",
            r"%SystemRoot%\calc.exe",
        ),
    ),
    "calc": KnownAppAlias(
        name="Calculator",
        executables=("calc.exe", "calc"),
        process_names=("calc.exe", "CalculatorApp.exe", "Calculator.exe"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\calc.exe",
            r"%SystemRoot%\calc.exe",
        ),
    ),
    "calc.exe": KnownAppAlias(
        name="Calculator",
        executables=("calc.exe", "calc"),
        process_names=("calc.exe", "CalculatorApp.exe", "Calculator.exe"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\calc.exe",
            r"%SystemRoot%\calc.exe",
        ),
    ),
    "vs code": KnownAppAlias(
        name="Visual Studio Code",
        executables=("Code.exe", "code.exe", "code.cmd", "code"),
        process_names=("Code.exe", "code.exe", "code"),
        standard_windows_paths=(
            r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
            r"%LocalAppData%\Programs\Microsoft VS Code\bin\code.cmd",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\bin\code.cmd",
            r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe",
        ),
    ),
    "vscode": KnownAppAlias(
        name="Visual Studio Code",
        executables=("Code.exe", "code.exe", "code.cmd", "code"),
        process_names=("Code.exe", "code.exe", "code"),
        standard_windows_paths=(
            r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
            r"%LocalAppData%\Programs\Microsoft VS Code\bin\code.cmd",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\bin\code.cmd",
            r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe",
        ),
    ),
    "visual studio code": KnownAppAlias(
        name="Visual Studio Code",
        executables=("Code.exe", "code.exe", "code.cmd", "code"),
        process_names=("Code.exe", "code.exe", "code"),
        standard_windows_paths=(
            r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
            r"%LocalAppData%\Programs\Microsoft VS Code\bin\code.cmd",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\bin\code.cmd",
            r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe",
        ),
    ),
    "code": KnownAppAlias(
        name="Visual Studio Code",
        executables=("Code.exe", "code.exe", "code.cmd", "code"),
        process_names=("Code.exe", "code.exe", "code"),
        standard_windows_paths=(
            r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
            r"%LocalAppData%\Programs\Microsoft VS Code\bin\code.cmd",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\bin\code.cmd",
        ),
    ),
    "powershell": KnownAppAlias(
        name="PowerShell",
        executables=("powershell.exe", "pwsh.exe", "powershell"),
        process_names=("powershell.exe", "pwsh.exe", "powershell"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe",
            r"%ProgramFiles%\PowerShell\7\pwsh.exe",
        ),
    ),
    "powershell.exe": KnownAppAlias(
        name="PowerShell",
        executables=("powershell.exe", "pwsh.exe", "powershell"),
        process_names=("powershell.exe", "pwsh.exe", "powershell"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe",
        ),
    ),
    "pwsh": KnownAppAlias(
        name="PowerShell",
        executables=("pwsh.exe", "powershell.exe", "pwsh"),
        process_names=("pwsh.exe", "powershell.exe", "pwsh"),
        standard_windows_paths=(
            r"%ProgramFiles%\PowerShell\7\pwsh.exe",
            r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe",
        ),
    ),
    "windows powershell": KnownAppAlias(
        name="PowerShell",
        executables=("powershell.exe", "pwsh.exe"),
        process_names=("powershell.exe", "pwsh.exe"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe",
        ),
    ),
    "terminal": KnownAppAlias(
        name="Windows Terminal",
        executables=("wt.exe", "WindowsTerminal.exe", "wt"),
        process_names=("WindowsTerminal.exe", "wt.exe", "wt"),
        standard_windows_paths=(
            r"%LocalAppData%\Microsoft\WindowsApps\wt.exe",
        ),
    ),
    "windows terminal": KnownAppAlias(
        name="Windows Terminal",
        executables=("wt.exe", "WindowsTerminal.exe", "wt"),
        process_names=("WindowsTerminal.exe", "wt.exe", "wt"),
        standard_windows_paths=(
            r"%LocalAppData%\Microsoft\WindowsApps\wt.exe",
        ),
    ),
    "edge": KnownAppAlias(
        name="Microsoft Edge",
        executables=("msedge.exe", "msedge"),
        process_names=("msedge.exe", "msedge"),
        standard_windows_paths=(
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    "microsoft edge": KnownAppAlias(
        name="Microsoft Edge",
        executables=("msedge.exe", "msedge"),
        process_names=("msedge.exe", "msedge"),
        standard_windows_paths=(
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    "msedge": KnownAppAlias(
        name="Microsoft Edge",
        executables=("msedge.exe", "msedge"),
        process_names=("msedge.exe", "msedge"),
        standard_windows_paths=(
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    "explorer": KnownAppAlias(
        name="File Explorer",
        executables=("explorer.exe", "explorer"),
        process_names=("explorer.exe",),
        standard_windows_paths=(
            r"%SystemRoot%\explorer.exe",
        ),
    ),
    "file explorer": KnownAppAlias(
        name="File Explorer",
        executables=("explorer.exe", "explorer"),
        process_names=("explorer.exe",),
        standard_windows_paths=(
            r"%SystemRoot%\explorer.exe",
        ),
    ),
    "paint": KnownAppAlias(
        name="Paint",
        executables=("mspaint.exe", "mspaint"),
        process_names=("mspaint.exe", "mspaint"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\mspaint.exe",
            r"%SystemRoot%\mspaint.exe",
        ),
    ),
    "mspaint": KnownAppAlias(
        name="Paint",
        executables=("mspaint.exe", "mspaint"),
        process_names=("mspaint.exe", "mspaint"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\mspaint.exe",
        ),
    ),
    "cmd": KnownAppAlias(
        name="Command Prompt",
        executables=("cmd.exe", "cmd"),
        process_names=("cmd.exe", "cmd"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\cmd.exe",
        ),
    ),
    "command prompt": KnownAppAlias(
        name="Command Prompt",
        executables=("cmd.exe", "cmd"),
        process_names=("cmd.exe", "cmd"),
        standard_windows_paths=(
            r"%SystemRoot%\System32\cmd.exe",
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class ResolvedApplication:
    """Deterministic, verified executable identity resolved on the host."""

    name: str
    executable_path: str
    process_names: tuple[str, ...]
    allow_reuse: bool = True


class WindowsApplicationResolver:
    """Safe, multi-tiered application resolution preventing command injection."""

    def __init__(self, *, custom_aliases: Mapping[str, ResolvedApplication] | None = None) -> None:
        self._custom_aliases: dict[str, ResolvedApplication] = dict(custom_aliases or {})

    def register_alias(self, alias_name: str, resolved: ResolvedApplication) -> None:
        self._custom_aliases[alias_name.strip().casefold()] = resolved

    def validate_name(self, app_name: str) -> str:
        """Strictly validate an untrusted application name proposal."""
        if not isinstance(app_name, str):
            raise ValueError("application name must be a string")
        stripped = app_name.strip()
        if not stripped:
            raise ValueError("application name cannot be blank")
        if len(stripped) > 128:
            raise ValueError("application name exceeds maximum length of 128 characters")
        if any(char in _FORBIDDEN_CHARACTERS for char in stripped):
            raise ValueError("application name contains forbidden shell characters")
        if ".." in stripped or "/../" in stripped or "\\..\\" in stripped:
            raise ValueError("application name contains path traversal tokens")

        parts = stripped.split()
        if len(parts) > 1:
            for part in parts[1:]:
                if part.startswith(("-", "/", "--")):
                    raise ValueError("application name contains forbidden command arguments")

        if not _SAFE_APP_NAME_RE.fullmatch(stripped):
            raise ValueError(f"application name '{stripped}' contains unsupported characters")
        return stripped

    def resolve(self, app_name: str) -> ResolvedApplication:
        """Resolve a semantic application name through the deterministic hierarchy."""
        clean_name = self.validate_name(app_name)
        key = clean_name.casefold()

        # 1. Custom / fixture aliases
        if key in self._custom_aliases:
            return self._custom_aliases[key]

        alias = KNOWN_ALIASES.get(key)
        canonical_name = alias.name if alias else clean_name
        process_names = alias.process_names if alias else (f"{clean_name}.exe", clean_name)
        allow_reuse = alias.allow_reuse if alias else True

        # 2. Check standard Windows paths if known alias
        if alias and sys.platform == "win32":
            for template in alias.standard_windows_paths:
                expanded = os.path.expandvars(template)
                if os.path.isfile(expanded):
                    return ResolvedApplication(
                        name=canonical_name,
                        executable_path=os.path.normpath(expanded),
                        process_names=process_names,
                        allow_reuse=allow_reuse,
                    )

        # 3. Windows Registry: App Paths
        if sys.platform == "win32":
            app_path = self._query_windows_app_paths(alias, clean_name)
            if app_path:
                return ResolvedApplication(
                    name=canonical_name,
                    executable_path=app_path,
                    process_names=process_names,
                    allow_reuse=allow_reuse,
                )

        # 4. Windows Registry: Uninstall entries DisplayName match
        if sys.platform == "win32":
            uninstall_path = self._query_windows_uninstall(clean_name)
            if uninstall_path:
                return ResolvedApplication(
                    name=canonical_name,
                    executable_path=uninstall_path,
                    process_names=process_names,
                    allow_reuse=allow_reuse,
                )

        # 5. PATH executable resolution via shutil.which
        candidates = (
            alias.executables
            if alias
            else (clean_name, f"{clean_name}.exe", f"{clean_name}.cmd", f"{clean_name}.bat")
        )
        for candidate in candidates:
            found = shutil.which(candidate)
            if found and os.path.isfile(found):
                resolved_proc = (_safe_basename(found), *process_names)
                return ResolvedApplication(
                    name=canonical_name,
                    executable_path=os.path.normpath(found),
                    process_names=resolved_proc,
                    allow_reuse=allow_reuse,
                )

        # If on non-Windows (e.g. Linux test environment with standard binaries)
        if sys.platform != "win32":
            found = shutil.which(clean_name.lower())
            if found and os.path.isfile(found):
                return ResolvedApplication(
                    name=canonical_name,
                    executable_path=os.path.normpath(found),
                    process_names=(_safe_basename(found), clean_name),
                    allow_reuse=allow_reuse,
                )

        raise ComputerAdapterError(
            ComputerFailureCode.APPLICATION_NOT_FOUND,
            f"Application '{app_name}' could not be resolved to an installed executable.",
            source=PerceptionSource.APPLICATION_API,
        )

    def _query_windows_app_paths(self, alias: KnownAppAlias | None, clean_name: str) -> str | None:
        try:
            import winreg
        except ImportError:
            return None

        candidates: list[str] = []
        if alias:
            candidates.extend(alias.executables)
        candidates.extend([f"{clean_name}.exe", clean_name])

        roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
        flags = (
            winreg.KEY_READ,
            winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )

        for candidate in candidates:
            subpath = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{candidate}"
            if not candidate.casefold().endswith(".exe"):
                subpath = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{candidate}.exe"
            for root in roots:
                for flag in flags:
                    try:
                        with winreg.OpenKey(root, subpath, 0, flag) as key:
                            val, _ = winreg.QueryValueEx(key, "")
                            if isinstance(val, str) and val.strip():
                                clean_val = val.strip().strip('"')
                                expanded = os.path.expandvars(clean_val)
                                if os.path.isfile(expanded):
                                    return os.path.normpath(expanded)
                    except OSError:
                        continue
        return None

    def _query_windows_uninstall(self, clean_name: str) -> str | None:
        try:
            import winreg
        except ImportError:
            return None

        roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
        flags = (
            winreg.KEY_READ,
            winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
        uninstall_subpath = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        target_name = clean_name.casefold()

        for root in roots:
            for flag in flags:
                try:
                    with winreg.OpenKey(root, uninstall_subpath, 0, flag) as uninstall_key:
                        num_subkeys = winreg.QueryInfoKey(uninstall_key)[0]
                        for idx in range(min(num_subkeys, 1024)):
                            try:
                                subkey_name = winreg.EnumKey(uninstall_key, idx)
                                with winreg.OpenKey(uninstall_key, subkey_name) as subkey:
                                    disp_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                                    if (
                                        not isinstance(disp_name, str)
                                        or target_name not in disp_name.casefold()
                                    ):
                                        continue
                                    try:
                                        icon, _ = winreg.QueryValueEx(subkey, "DisplayIcon")
                                        if isinstance(icon, str):
                                            icon_clean = icon.split(",")[0].strip().strip('"')
                                            expanded_icon = os.path.expandvars(icon_clean)
                                            if (
                                                os.path.isfile(expanded_icon)
                                                and expanded_icon.lower().endswith(".exe")
                                            ):
                                                return os.path.normpath(expanded_icon)
                                    except OSError:
                                        pass
                                    try:
                                        loc, _ = winreg.QueryValueEx(subkey, "InstallLocation")
                                        if isinstance(loc, str) and loc.strip():
                                            loc_clean = loc.strip().strip('"')
                                            expanded_loc = os.path.expandvars(loc_clean)
                                            if os.path.isdir(expanded_loc):
                                                for root_dir, _, files in os.walk(expanded_loc):
                                                    for file in files:
                                                        if file.lower().endswith(".exe"):
                                                            full_cand = os.path.join(
                                                                root_dir, file
                                                            )
                                                            if os.path.isfile(full_cand):
                                                                return os.path.normpath(full_cand)
                                    except OSError:
                                        pass
                            except OSError:
                                continue
                except OSError:
                    continue
        return None


class WindowsAppLaunchBackend(Protocol):
    """Port isolating process spawning and window query operations."""

    async def list_running_processes(self) -> Sequence[dict[str, Any]]: ...

    async def launch_process(self, executable_path: str) -> int: ...

    async def is_process_alive(self, pid: int) -> bool: ...

    async def list_windows(self) -> Sequence[WindowRecord]: ...

    async def focus_window(self, window_id: str) -> WindowRecord: ...


class Win32AppLaunchBackend:
    """Production backend using direct, safe OS subprocess spawning."""

    requires_visible_window = True

    def __init__(self, *, uia_backend: Any | None = None) -> None:
        self._uia_backend = uia_backend

    async def list_running_processes(self) -> Sequence[dict[str, Any]]:
        return await asyncio.to_thread(self._sync_list_processes)

    def _sync_list_processes(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        try:
            for proc in psutil.process_iter(["pid", "name", "exe"]):
                try:
                    info = proc.info
                    pid = int(info.get("pid") or proc.pid)
                    name = str(info.get("name") or proc.name())
                    exe = str(info.get("exe") or "")
                    if pid > 0 and name:
                        results.append({"pid": pid, "name": name, "exe": exe})
                except (psutil.Error, OSError, ValueError):
                    continue
        except (psutil.Error, OSError):
            pass
        return results

    async def launch_process(self, executable_path: str) -> int:
        return await asyncio.to_thread(self._sync_launch_process, executable_path)

    def _sync_launch_process(self, executable_path: str) -> int:
        if sys.platform != "win32":
            raise ComputerAdapterError(
                ComputerFailureCode.ADAPTER_UNAVAILABLE,
                "Native application launch requires a Windows desktop host.",
                source=PerceptionSource.APPLICATION_API,
            )
        norm_path = os.path.normpath(executable_path)
        if not os.path.isfile(norm_path):
            raise FileNotFoundError(f"Executable not found: {norm_path}")

        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)

        proc = subprocess.Popen(
            [norm_path],
            shell=False,
            close_fds=True,
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return int(proc.pid)

    async def is_process_alive(self, pid: int) -> bool:
        return await asyncio.to_thread(self._sync_is_alive, pid)

    def _sync_is_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            if not psutil.pid_exists(pid):
                return False
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except (psutil.Error, OSError, ValueError):
            return False

    async def list_windows(self) -> Sequence[WindowRecord]:
        if self._uia_backend is not None and hasattr(self._uia_backend, "list_windows"):
            return await self._uia_backend.list_windows()
        raise ComputerAdapterError(
            ComputerFailureCode.ADAPTER_UNAVAILABLE,
            "Application window observation backend is unavailable.",
            source=PerceptionSource.APPLICATION_API,
        )

    async def focus_window(self, window_id: str) -> WindowRecord:
        if self._uia_backend is not None and hasattr(self._uia_backend, "focus_window"):
            return await self._uia_backend.focus_window(window_id)
        raise ComputerAdapterError(
            ComputerFailureCode.ADAPTER_UNAVAILABLE,
            "Window focus backend is unavailable.",
            source=PerceptionSource.APPLICATION_API,
        )


@dataclass(frozen=True, slots=True)
class ObservedProcess:
    """One observed process.

    The executable path is the only trusted application identity; a process name
    alone never proves that a process belongs to the resolved application.
    """

    process_id: int
    name: str
    executable_path: str

    @property
    def executable_key(self) -> str:
        return _executable_key(self.executable_path)


@dataclass(frozen=True, slots=True)
class DesktopSnapshot:
    """One bounded, read-only observation of processes and visible top-level windows.

    Windows are keyed by window id in enumeration order. Only visible top-level
    windows are retained because hidden windows and child controls cannot prove
    that an application was launched or activated.
    """

    processes: Mapping[int, ObservedProcess]
    windows: Mapping[str, WindowRecord]
    foreground_window_id: str | None


class WindowEvidenceKind(StrEnum):
    """How an observed application window can be explained by a dispatch."""

    NEW_WINDOW = "new_window"
    ACTIVATED_EXISTING_WINDOW = "activated_existing_window"


@dataclass(frozen=True, slots=True)
class WindowEvidence:
    """A visible application window that appeared or was activated after dispatch."""

    window: WindowRecord
    owner_process_id: int
    owner_executable_key: str
    kind: WindowEvidenceKind

    @property
    def window_id(self) -> str:
        return self.window.window_id


def _window_evidence_sort_key(evidence: WindowEvidence) -> tuple[int, int, float, str]:
    """Deterministic preference among simultaneous candidates.

    A newly created window is stronger evidence than a re-activated window, then
    the foreground window wins, then the larger window, then the lowest window id.
    """

    return (
        0 if evidence.kind is WindowEvidenceKind.NEW_WINDOW else 1,
        0 if evidence.window.foreground else 1,
        -_window_area(evidence.window),
        evidence.window.window_id,
    )


class WindowsAppLaunchProvider(ApplicationProvider):
    """Orchestrates application resolution, launching, idempotency, and verification.

    Launch verification is window-based rather than launcher-PID-based:

    1. A bounded baseline of the application's processes and their visible
       top-level windows is captured before any dispatch.
    2. The resolved executable is dispatched with ``shell=False``.
    3. Post-dispatch polling accepts only windows that did not exist in the
       baseline ("new") or that the dispatch freshly activated ("activated").
    4. Window ownership is proven by observed executable identity. Process names
       are never accepted, and unknown or conflicting ownership fails closed.

    Chromium-style applications are supported because the visible browser window
    can be owned by a process other than the spawned launcher PID, and because a
    launcher can hand the request to an already running browser process.
    """

    def __init__(
        self,
        *,
        backend: WindowsAppLaunchBackend | None = None,
        resolver: WindowsApplicationResolver | None = None,
    ) -> None:
        self._backend = backend or Win32AppLaunchBackend()
        self._resolver = resolver or WindowsApplicationResolver()
        self._observations: OrderedDict[str, ObservationLease] = OrderedDict()
        self._launch_diagnostic: dict[str, Any] = {}

    @property
    def launch_diagnostic(self) -> dict[str, Any]:
        """Bounded dispatch metadata, not verification evidence; no paths or UI text."""
        return dict(self._launch_diagnostic)

    @property
    def resolver(self) -> WindowsApplicationResolver:
        return self._resolver

    @property
    def backend(self) -> WindowsAppLaunchBackend:
        return self._backend

    @property
    def _requires_window(self) -> bool:
        # Native desktop launches require visible-window evidence. Alternate application
        # backends may intentionally implement process-only applications.
        return bool(getattr(self._backend, "requires_visible_window", False))

    # ------------------------------------------------------------------
    # Observed identity helpers
    # ------------------------------------------------------------------

    def _matches(self, proc: Mapping[str, Any], resolved: ResolvedApplication) -> bool:
        try:
            process_id = int(proc.get("pid") or 0)
        except (TypeError, ValueError):
            process_id = 0
        return self._process_matches(
            ObservedProcess(
                process_id=process_id,
                name=str(proc.get("name") or ""),
                executable_path=str(proc.get("exe") or ""),
            ),
            resolved,
        )

    def _process_matches(self, process: ObservedProcess, resolved: ResolvedApplication) -> bool:
        """Native launches require the resolved executable's observed path."""
        if self._requires_window:
            expected = _executable_key(resolved.executable_path)
            return bool(expected) and process.executable_key == expected
        names = {_safe_basename(name).casefold() for name in resolved.process_names}
        return (
            _safe_basename(process.name).casefold() in names
            or _safe_basename(process.executable_path).casefold() in names
        )

    def _window_owner_identity(
        self, window: WindowRecord, snapshot: DesktopSnapshot
    ) -> tuple[int | None, list[str]]:
        """Observed executable identity of a window's owning process.

        The window record can carry the owner's executable path (recorded by the
        native window backend while enumerating the HWND); the process snapshot is
        an independent observation of the same owner. Both are compared when present.
        """
        owner_pid = window.process_id if window.process_id and window.process_id > 0 else None
        identities: list[str] = []
        record_key = _executable_key(window.executable_path)
        if record_key:
            identities.append(record_key)
        observed = snapshot.processes.get(owner_pid) if owner_pid is not None else None
        if observed is not None and observed.executable_key:
            identities.append(observed.executable_key)
        return owner_pid, identities

    def _window_owner_verified(
        self, window: WindowRecord, snapshot: DesktopSnapshot, resolved: ResolvedApplication
    ) -> bool:
        """Prove that a window belongs to the resolved application's executable.

        Ownership is established only from observed executable paths: a missing
        identity fails closed, and a conflicting identity also fails closed.
        """
        expected = _executable_key(resolved.executable_path)
        if not expected:
            return False
        owner_pid, identities = self._window_owner_identity(window, snapshot)
        if owner_pid is None or not identities:
            return False
        return all(identity == expected for identity in identities)

    @staticmethod
    def _window_is_active(window: WindowRecord) -> bool:
        """A window that is visible, restored, and currently foreground."""
        return bool(window.visible and not window.minimized and window.foreground)

    def _application_process_ids(
        self, snapshot: DesktopSnapshot, resolved: ResolvedApplication
    ) -> set[int]:
        return {
            process_id
            for process_id, process in snapshot.processes.items()
            if self._process_matches(process, resolved)
        }

    def _application_windows(
        self,
        snapshot: DesktopSnapshot,
        resolved: ResolvedApplication,
        application_process_ids: set[int] | None = None,
    ) -> dict[str, WindowRecord]:
        """Visible top-level windows that provably belong to the resolved application."""
        if self._requires_window:
            return {
                window_id: window
                for window_id, window in snapshot.windows.items()
                if self._window_owner_verified(window, snapshot, resolved)
            }
        process_ids = (
            application_process_ids
            if application_process_ids is not None
            else self._application_process_ids(snapshot, resolved)
        )
        return {
            window_id: window
            for window_id, window in snapshot.windows.items()
            if window.process_id is not None and window.process_id in process_ids
        }

    async def _snapshot_desktop(self) -> DesktopSnapshot:
        """Take one bounded read-only desktop snapshot; oversized inventories fail closed."""
        processes = await self._backend.list_running_processes()
        windows = await self._backend.list_windows()
        if len(processes) > _MAX_OBSERVED_PROCESSES:
            raise ComputerAdapterError(
                ComputerFailureCode.ENVIRONMENT_CHANGED,
                "The observed process inventory exceeds the bounded snapshot capacity.",
                source=PerceptionSource.APPLICATION_API,
            )
        if len(windows) >= _MAX_OBSERVED_WINDOWS:
            raise ComputerAdapterError(
                ComputerFailureCode.ENVIRONMENT_CHANGED,
                "The observed visible-window inventory exceeds the bounded snapshot capacity.",
                source=PerceptionSource.APPLICATION_API,
            )

        observed_processes: dict[int, ObservedProcess] = {}
        for proc in processes:
            try:
                process_id = int(proc.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if process_id <= 0 or process_id in observed_processes:
                continue
            observed_processes[process_id] = ObservedProcess(
                process_id=process_id,
                name=str(proc.get("name") or ""),
                executable_path=str(proc.get("exe") or ""),
            )

        observed_windows: dict[str, WindowRecord] = {}
        for window in windows:
            if not window.visible or window.window_id in observed_windows:
                continue
            observed_windows[window.window_id] = window
        foreground_window_id = next(
            (window_id for window_id, window in observed_windows.items() if window.foreground),
            None,
        )
        return DesktopSnapshot(
            processes=observed_processes,
            windows=observed_windows,
            foreground_window_id=foreground_window_id,
        )

    # ------------------------------------------------------------------
    # Launch orchestration
    # ------------------------------------------------------------------

    @staticmethod
    def _bounded_timeout(timeout_seconds: float) -> float:
        return min(
            max(float(timeout_seconds), _MIN_LAUNCH_TIMEOUT_SECONDS), _MAX_LAUNCH_TIMEOUT_SECONDS
        )

    def _select_existing_window(
        self, windows: Mapping[str, WindowRecord], *, prefer_foreground: bool
    ) -> WindowRecord | None:
        """Deterministically choose the intended existing application window."""
        if not windows:
            return None
        if not prefer_foreground:
            # Process-only alternate backends keep the historical first-window behavior.
            return next(iter(windows.values()))
        return min(
            windows.values(),
            key=lambda window: (
                not window.foreground,
                window.minimized,
                -_window_area(window),
                window.window_id,
            ),
        )

    async def launch_application(
        self, application_id: str, *, timeout_seconds: float = 10.0
    ) -> RunningApplication:
        self._launch_diagnostic = {"stage": "resolution", "mode": "not_dispatched"}
        resolved = self._resolver.resolve(application_id)
        self._launch_diagnostic.update(
            stage="baseline_observation",
            executable_identity=hashlib.sha256(
                _executable_key(resolved.executable_path).encode("utf-8")
            ).hexdigest(),
        )

        deadline = time.monotonic() + self._bounded_timeout(timeout_seconds)
        baseline = await self._snapshot_desktop()
        baseline_processes = self._application_process_ids(baseline, resolved)
        baseline_windows = self._application_windows(baseline, resolved, baseline_processes)
        self._launch_diagnostic.update(
            baseline_application_processes=len(baseline_processes),
            baseline_application_windows=len(baseline_windows),
            baseline_foreground_window_observed=baseline.foreground_window_id is not None,
        )

        # A background process is not an open application. Reuse only an observed
        # window belonging to the resolved executable, and do not swallow focus errors.
        if resolved.allow_reuse:
            reused = await self._reuse_existing_window(resolved, baseline_windows)
            if reused is not None:
                return reused

        dispatched_pid = await self._dispatch_process(resolved)
        if not self._requires_window:
            return await self._await_process_only_launch(resolved, dispatched_pid, deadline)

        # Polling and confirmation share one deadline: a window that cannot be
        # re-observed after the focus attempt is never reported as a success.
        rejected_window_ids: set[str] = set()
        while True:
            evidence = await self._poll_window_evidence(
                resolved,
                baseline_windows,
                dispatched_pid,
                deadline,
                excluded_window_ids=frozenset(rejected_window_ids),
            )
            confirmed = await self._confirm_window_evidence(resolved, evidence, dispatched_pid)
            if confirmed is not None:
                return confirmed
            rejected_window_ids.add(evidence.window_id)
            if time.monotonic() >= deadline:
                raise ComputerAdapterError(
                    ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                    f"The window observed for '{resolved.name}' could not be confirmed by a fresh "
                    "observation; the external effect is unknown.",
                    source=PerceptionSource.APPLICATION_API,
                )

    async def _dispatch_process(self, resolved: ResolvedApplication) -> int:
        self._launch_diagnostic.update(stage="process_dispatch", mode="spawn_attempt")
        try:
            pid = await self._backend.launch_process(resolved.executable_path)
        except ComputerAdapterError:
            raise
        except Exception as exc:
            raise ComputerAdapterError(
                ComputerFailureCode.INTERNAL_ADAPTER_ERROR,
                f"Failed to launch '{resolved.name}': {type(exc).__name__}: {exc}",
                source=PerceptionSource.APPLICATION_API,
            ) from exc

        if pid <= 0:
            raise ComputerAdapterError(
                ComputerFailureCode.INTERNAL_ADAPTER_ERROR,
                f"Failed to launch '{resolved.name}': invalid PID returned.",
                source=PerceptionSource.APPLICATION_API,
            )
        return pid

    async def _reuse_existing_window(
        self, resolved: ResolvedApplication, baseline_windows: Mapping[str, WindowRecord]
    ) -> RunningApplication | None:
        """Reuse a visible application window, or return None so the caller dispatches.

        Reuse is idempotent: no process is spawned. The intended window is selected
        deterministically and must be confirmed by a *fresh* observation after the
        focus attempt; the record returned by ``focus_window`` is never evidence. If
        activation cannot be confirmed, the caller dispatches the executable so the
        application's own single-instance logic can activate its window.
        """
        target = self._select_existing_window(
            baseline_windows, prefer_foreground=self._requires_window
        )
        if target is None or target.process_id is None or target.process_id <= 0:
            return None

        self._launch_diagnostic.update(stage="focus_existing_window", mode="reuse_attempt")
        if not self._requires_window:
            # Alternate backends keep the existing behavior: a focus error propagates.
            await self._backend.focus_window(target.window_id)
            self._launch_diagnostic.update(
                stage="focus_existing_window", mode="reuse", process_id=target.process_id
            )
            return RunningApplication(
                process_id=target.process_id,
                name=resolved.name,
                executable_path=resolved.executable_path,
                window_ids=(target.window_id,),
            )

        focus_error: str | None = None
        try:
            await self._backend.focus_window(target.window_id)
        except ComputerAdapterError as exc:
            focus_error = exc.code.value
        except Exception as exc:
            focus_error = type(exc).__name__

        confirmation = await self._snapshot_desktop()
        confirmed = confirmation.windows.get(target.window_id)
        if (
            focus_error is None
            and confirmed is not None
            and self._window_is_active(confirmed)
            and self._window_owner_verified(confirmed, confirmation, resolved)
        ):
            self._launch_diagnostic.update(
                stage="focus_existing_window",
                mode="reuse",
                process_id=int(confirmed.process_id or target.process_id),
                focus_verified=True,
            )
            return RunningApplication(
                process_id=int(confirmed.process_id or target.process_id),
                name=resolved.name,
                executable_path=resolved.executable_path,
                window_ids=(target.window_id,),
            )

        # Not swallowed: the failed or unconfirmed activation is recorded, and the
        # caller proceeds to a real dispatch whose window evidence is observed fresh.
        self._launch_diagnostic.update(
            stage="focus_existing_window",
            mode="reuse_unconfirmed",
            reuse_focus_error=focus_error or "activation_not_observed",
        )
        return None

    async def _await_process_only_launch(
        self, resolved: ResolvedApplication, pid: int, deadline: float
    ) -> RunningApplication:
        """Bounded liveness loop for alternate backends that do not require a window."""
        self._launch_diagnostic.update(stage="window_poll", mode="spawn", dispatched_process_id=pid)
        stable_checks = 0
        window_ids: list[str] = []

        while time.monotonic() < deadline:
            alive = await self._backend.is_process_alive(pid)
            if not alive:
                running_now = await self._backend.list_running_processes()
                child_matched = next(
                    (int(p.get("pid", 0)) for p in running_now if self._matches(p, resolved)),
                    None,
                )
                if child_matched:
                    pid = child_matched
                    alive = True
                else:
                    raise ComputerAdapterError(
                        ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                        f"Process '{resolved.name}' (PID {pid}) exited immediately after launch.",
                        source=PerceptionSource.APPLICATION_API,
                    )

            running_now = await self._backend.list_running_processes()
            matching_pids = {
                int(p.get("pid", 0)) for p in running_now if self._matches(p, resolved)
            }
            windows = await self._backend.list_windows()
            window_ids = [
                win.window_id
                for win in windows
                if win.process_id in matching_pids and win.visible and not win.minimized
            ]
            stable_checks = stable_checks + 1 if alive else 0
            if stable_checks >= _LAUNCH_REQUIRED_STABLE_CHECKS:
                if window_ids:
                    await self._backend.focus_window(window_ids[0])
                break

            await asyncio.sleep(_LAUNCH_POLL_INTERVAL_SECONDS)

        if stable_checks < _LAUNCH_REQUIRED_STABLE_CHECKS:
            raise ComputerAdapterError(
                ComputerFailureCode.TIMEOUT,
                f"Timed out verifying launch for '{resolved.name}'.",
                source=PerceptionSource.APPLICATION_API,
            )

        return RunningApplication(
            process_id=pid,
            name=resolved.name,
            executable_path=resolved.executable_path,
            window_ids=tuple(window_ids),
        )

    def _window_evidence_candidates(
        self,
        resolved: ResolvedApplication,
        snapshot: DesktopSnapshot,
        baseline_windows: Mapping[str, WindowRecord],
        excluded_window_ids: frozenset[str] = frozenset(),
    ) -> list[WindowEvidence]:
        """Windows that this dispatch can explain, in deterministic preference order.

        ``new_window`` means the window id did not exist in the bounded baseline.
        ``activated_existing_window`` means a pre-existing application window is
        foreground now and was not foreground (or was minimized) in the baseline.

        An unchanged pre-existing application window is never evidence, so an
        unrelated Chrome window cannot satisfy verification for this launch.
        """
        candidates: list[WindowEvidence] = []
        for window_id, window in snapshot.windows.items():
            if window_id in excluded_window_ids:
                continue
            if not window.visible or window.minimized:
                continue
            if not self._window_owner_verified(window, snapshot, resolved):
                continue
            owner_pid, identities = self._window_owner_identity(window, snapshot)
            if owner_pid is None:
                continue
            prior = baseline_windows.get(window_id)
            if prior is None:
                kind = WindowEvidenceKind.NEW_WINDOW
            elif window.foreground and (not prior.foreground or prior.minimized):
                kind = WindowEvidenceKind.ACTIVATED_EXISTING_WINDOW
            else:
                continue
            candidates.append(
                WindowEvidence(
                    window=window,
                    owner_process_id=owner_pid,
                    owner_executable_key=identities[0],
                    kind=kind,
                )
            )
        candidates.sort(key=_window_evidence_sort_key)
        return candidates

    async def _poll_window_evidence(
        self,
        resolved: ResolvedApplication,
        baseline_windows: Mapping[str, WindowRecord],
        dispatched_pid: int,
        deadline: float,
        *,
        excluded_window_ids: frozenset[str] = frozenset(),
    ) -> WindowEvidence:
        """Poll for a new or newly activated visible window of the resolved application.

        The spawned PID is not the association anchor: for Chromium-style
        applications the visible browser window may be owned by a different
        process, and the launcher may exit after handing the request to an already
        running browser process. A window must be observed twice consecutively
        before it is accepted.
        """
        self._launch_diagnostic.update(
            stage="window_poll", mode="spawn", dispatched_process_id=dispatched_pid
        )
        stable_window_id: str | None = None
        stable_checks = 0
        exit_checks = 0

        while time.monotonic() < deadline:
            dispatched_alive = await self._backend.is_process_alive(dispatched_pid)
            snapshot = await self._snapshot_desktop()
            candidates = self._window_evidence_candidates(
                resolved, snapshot, baseline_windows, excluded_window_ids
            )
            if candidates:
                exit_checks = 0
                selected = candidates[0]
                if selected.window_id == stable_window_id:
                    stable_checks += 1
                else:
                    stable_window_id = selected.window_id
                    stable_checks = 1
                if stable_checks >= _LAUNCH_REQUIRED_STABLE_CHECKS:
                    return selected
            else:
                stable_window_id = None
                stable_checks = 0
                if not dispatched_alive and not self._application_process_ids(snapshot, resolved):
                    exit_checks += 1
                    if exit_checks >= _LAUNCH_EXIT_GRACE_CHECKS:
                        raise ComputerAdapterError(
                            ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                            f"Process '{resolved.name}' (PID {dispatched_pid}) exited without "
                            "leaving an identifiable application process or visible window.",
                            source=PerceptionSource.APPLICATION_API,
                        )
                else:
                    exit_checks = 0
            await asyncio.sleep(_LAUNCH_POLL_INTERVAL_SECONDS)

        raise ComputerAdapterError(
            ComputerFailureCode.TIMEOUT,
            f"Timed out verifying launch for '{resolved.name}': no new or newly activated "
            "visible application window was observed.",
            source=PerceptionSource.APPLICATION_API,
        )

    async def _confirm_window_evidence(
        self, resolved: ResolvedApplication, evidence: WindowEvidence, dispatched_pid: int
    ) -> RunningApplication | None:
        """Bring the observed window forward and confirm it with a fresh observation.

        The record returned by ``focus_window`` is never trusted as evidence. A
        focus failure is recorded (never swallowed) and cannot by itself invalidate
        a window that was newly created by this dispatch, but an activated
        pre-existing window must still be foreground in the fresh observation.

        Returns ``None`` when the observed window cannot be confirmed; the caller
        keeps polling until the bounded deadline and then fails closed.
        """
        window_id = evidence.window_id
        self._launch_diagnostic.update(
            stage="window_confirmation",
            mode="spawn",
            dispatched_process_id=dispatched_pid,
            window_evidence=evidence.kind.value,
            window_owner_process_id=evidence.owner_process_id,
            focus_failed=False,
            focus_error=None,
            focus_verified=False,
            confirmation_failed=False,
        )
        try:
            await self._backend.focus_window(window_id)
        except Exception as exc:
            self._launch_diagnostic.update(
                focus_failed=True,
                focus_error=(
                    exc.code.value if isinstance(exc, ComputerAdapterError) else type(exc).__name__
                ),
            )

        confirmation = await self._snapshot_desktop()
        confirmed = confirmation.windows.get(window_id)
        if (
            confirmed is None
            or not confirmed.visible
            or confirmed.minimized
            or not self._window_owner_verified(confirmed, confirmation, resolved)
        ):
            self._launch_diagnostic.update(confirmation_failed=True)
            return None
        if (
            evidence.kind is WindowEvidenceKind.ACTIVATED_EXISTING_WINDOW
            and not confirmed.foreground
        ):
            self._launch_diagnostic.update(confirmation_failed=True)
            return None

        self._launch_diagnostic.update(focus_verified=bool(confirmed.foreground))
        return RunningApplication(
            process_id=int(confirmed.process_id or evidence.owner_process_id),
            name=resolved.name,
            executable_path=resolved.executable_path,
            window_ids=(window_id,),
        )

    async def running_applications(self) -> Sequence[RunningApplication]:
        processes = await self._backend.list_running_processes()
        windows = await self._backend.list_windows()
        windows_by_pid: dict[int, list[str]] = {}
        for win in windows:
            if win.process_id is not None:
                windows_by_pid.setdefault(win.process_id, []).append(win.window_id)

        results: list[RunningApplication] = []
        seen_pids: set[int] = set()
        for p in processes:
            pid = int(p.get("pid", 0))
            if pid <= 0 or pid in seen_pids:
                continue
            seen_pids.add(pid)
            name = str(p.get("name", ""))
            exe = str(p.get("exe") or "") or None
            win_ids = tuple(windows_by_pid.get(pid, []))
            results.append(
                RunningApplication(
                    process_id=pid,
                    name=name,
                    executable_path=exe,
                    window_ids=win_ids,
                )
            )
        return tuple(results)

    async def installed_applications(self, *, limit: int = 512) -> Sequence[RunningApplication]:
        apps: list[RunningApplication] = []
        for name in KNOWN_ALIASES:
            if len(apps) >= limit:
                break
            try:
                resolved = self._resolver.resolve(name)
                apps.append(
                    RunningApplication(
                        process_id=0,
                        name=resolved.name,
                        executable_path=resolved.executable_path,
                    )
                )
            except Exception:
                continue
        return tuple(apps)

    async def observe(self, action: ActionContract) -> ObservationLease:
        app_name = (
            action.parameters.get("application")
            or action.parameters.get("app_name")
            or action.parameters.get("name")
            or (action.target.application if action.target else None)
            or ""
        )
        resolved_name = app_name
        is_running = False
        pid: int | None = None
        window_ids: list[str] = []
        observation_error: str | None = None

        if app_name:
            try:
                resolved = self._resolver.resolve(str(app_name))
                resolved_name = resolved.name
                snapshot = await self._snapshot_desktop()
                application_process_ids = self._application_process_ids(snapshot, resolved)
                is_running = bool(application_process_ids)
                pid = min(application_process_ids) if application_process_ids else None
                application_windows = self._application_windows(
                    snapshot, resolved, application_process_ids
                )
                window_ids = [
                    window_id
                    for window_id, window in application_windows.items()
                    if not window.minimized
                ]
            except Exception as exc:
                observation_error = (
                    exc.code.value if isinstance(exc, ComputerAdapterError) else type(exc).__name__
                )

        facts: dict[str, Any] = {
            "application": str(app_name),
            "application.name": str(resolved_name),
            "application.running": is_running,
            "process.running": is_running,
            "process_id": pid,
            "window.open": len(window_ids) > 0,
            "window_ids": list(window_ids),
            "domain": "system.application",
            "observation.error": observation_error,
        }
        serialized = canonical_json(facts)
        state_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        lease_id = f"obs-launch-{uuid.uuid4()}"
        now = utc_now()
        lease = ObservationLease(
            lease_id=lease_id,
            target_fingerprint=action.target.fingerprint if action.target is not None else None,
            state_hash=state_hash,
            created_at=now,
            expires_at=now + timedelta(seconds=min(action.timeout_seconds + 30.0, 300.0)),
            monotonic_deadline=time.monotonic() + min(action.timeout_seconds + 30.0, 300.0),
            facts=facts,
            source=EvidenceSource.OBSERVED,
            confidence=1.0,
        )
        self._observations[lease_id] = lease
        if len(self._observations) > _MAX_OBSERVATIONS:
            self._observations.popitem(last=False)
        return lease

    async def is_current(self, lease: ObservationLease) -> bool:
        pid = lease.facts.get("process_id")
        if pid is not None and isinstance(pid, int) and pid > 0:
            return await self._backend.is_process_alive(pid)
        return lease.is_valid(monotonic_now=time.monotonic())

    async def verify(
        self, action: ActionContract, outcome: ExecutionOutcome | None = None
    ) -> VerificationResult:
        del outcome
        try:
            observation = await self.observe(action)
        except Exception as exc:
            return VerificationResult(
                status=VerificationStatus.UNKNOWN,
                level=0,
                summary=f"Could not reobserve application state ({type(exc).__name__}).",
            )

        app_name = (
            observation.facts.get("application.name")
            or observation.facts.get("application")
            or "Application"
        )
        is_running = bool(
            observation.facts.get("application.running") or observation.facts.get("process.running")
        )
        pid = observation.facts.get("process_id")

        if (
            not is_running
            or not pid
            or (self._requires_window and not observation.facts.get("window.open"))
        ):
            return VerificationResult(
                status=VerificationStatus.FAILED,
                level=1,
                summary="Resolved process and required visible window were not observed.",
                evidence=(
                    EvidenceRecord(
                        source="observed",
                        observation_id=observation.lease_id,
                        state_hash=observation.state_hash,
                        statement="Required process/window evidence was not observed.",
                    ),
                ),
            )

        failed_conditions: list[str] = []
        for condition in action.postconditions:
            if not condition.evaluate(observation.facts):
                failed_conditions.append(condition.description or condition.key)

        if failed_conditions:
            return VerificationResult(
                status=VerificationStatus.FAILED,
                level=1,
                summary=f"Unmet application postconditions: {', '.join(failed_conditions)}",
                evidence=(
                    EvidenceRecord(
                        source="observed",
                        observation_id=observation.lease_id,
                        state_hash=observation.state_hash,
                        statement="Application postcondition check failed.",
                    ),
                ),
            )

        observed_summary = (
            f"Resolved application process (PID {pid}) and visible window were observed."
            if self._requires_window
            else f"Application '{app_name}' verified running (PID {pid})."
        )
        return VerificationResult(
            status=VerificationStatus.PASSED,
            level=2,
            summary=observed_summary,
            evidence=(
                EvidenceRecord(
                    source="observed",
                    observation_id=observation.lease_id,
                    state_hash=observation.state_hash,
                    statement=observed_summary,
                ),
            ),
        )


class AppLaunchTool(ActionTool):
    """ActionTool port exposing generic application launch to TaskEngine and PolicyEngine."""

    def __init__(self, provider: WindowsAppLaunchProvider) -> None:
        self.provider = provider
        self._spec = ToolSpec(
            name="system.app_launch",
            version="1.0.0",
            description=(
                "Launch or activate an installed desktop application by its semantic name "
                "(e.g. Chrome, Notepad, VS Code, Calculator, PowerShell)."
            ),
            minimum_risk=RiskLevel.R1,
            required_capabilities=frozenset({"desktop.launch"}),
            required_resources=(),
            declared_side_effects=("Starts or activates a desktop application process.",),
            idempotency=Idempotency.IDEMPOTENT,
            max_result_bytes=4096,
            parameter_names=("application",),
            target_scope=None,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def resources_for(self, action: ActionContract) -> tuple[str, ...]:
        app_name = (
            action.parameters.get("application")
            or action.parameters.get("app_name")
            or action.parameters.get("name")
        )
        if app_name and isinstance(app_name, str):
            clean = re.sub(r"[^A-Za-z0-9_.-]", "_", app_name.strip().lower())[:64]
            return ("desktop.launch", f"desktop.app.{clean}")
        return ("desktop.launch",)

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        if (
            "application" not in parameters
            and "app_name" not in parameters
            and "name" not in parameters
        ):
            raise ValueError("system.app_launch requires an 'application' parameter")
        app_name = (
            parameters.get("application")
            or parameters.get("app_name")
            or parameters.get("name")
        )
        if not isinstance(app_name, str) or not app_name.strip():
            raise ValueError("application must be a non-empty string")
        if len(app_name) > 128:
            raise ValueError("application name exceeds maximum length of 128 characters")
        self.provider.resolver.validate_name(app_name)

    async def execute(
        self,
        action: ActionContract,
        observation: ObservationLease,
        resources: ResourceLease,
    ) -> ExecutionOutcome:
        started_at = utc_now()
        if resources is not None:
            await resources.ensure_valid()
        app_name = (
            action.parameters.get("application")
            or action.parameters.get("app_name")
            or action.parameters.get("name")
            or (action.target.application if action.target else None)
        )
        if not app_name or not isinstance(app_name, str):
            raise ComputerAdapterError(
                ComputerFailureCode.INVALID_TARGET,
                "No application name was provided.",
                source=PerceptionSource.APPLICATION_API,
            )
        app = await self.provider.launch_application(
            app_name, timeout_seconds=action.timeout_seconds
        )
        finished_at = utc_now()
        return ExecutionOutcome(
            status=ExecutionStatus.SUCCEEDED,
            summary=f"Application '{app.name}' is running (PID {app.process_id}).",
            result_metadata={
                "application": app.name,
                "process_id": app.process_id,
                "executable_path": app.executable_path or "",
                "window_ids": list(app.window_ids),
            },
            started_at=started_at,
            finished_at=finished_at,
        )


def register_app_launch_tools(
    registry: ToolRegistry, provider: WindowsAppLaunchProvider
) -> None:
    registry.register(AppLaunchTool(provider))


__all__ = [
    "AppLaunchTool",
    "DesktopSnapshot",
    "KnownAppAlias",
    "KNOWN_ALIASES",
    "ObservedProcess",
    "ResolvedApplication",
    "WindowEvidence",
    "WindowEvidenceKind",
    "Win32AppLaunchBackend",
    "WindowsAppLaunchBackend",
    "WindowsAppLaunchProvider",
    "WindowsApplicationResolver",
    "register_app_launch_tools",
]
