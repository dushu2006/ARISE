"""Windows application launch adapter with strict deterministic resolution and safety gates.

This adapter resolves semantic application names to trusted executables on the host,
launches the process without shell interpolation, verifies process liveness and window
appearance through bounded polling, and integrates with the ARISE action/policy/security model.
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


def _safe_basename(path: str) -> str:
    """Extract file name from both Windows and POSIX paths safely."""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


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


class WindowsAppLaunchProvider(ApplicationProvider):
    """Orchestrates application resolution, launching, idempotency, and verification."""

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
        # Native desktop launches require visible evidence. Alternate application
        # backends may intentionally implement process-only applications.
        return bool(getattr(self._backend, "requires_visible_window", False))

    def _matches(self, proc: Mapping[str, Any], resolved: ResolvedApplication) -> bool:
        if self._requires_window:
            return ntpath.normcase(str(proc.get("exe") or "")) == ntpath.normcase(
                resolved.executable_path
            )
        names = {_safe_basename(p).casefold() for p in resolved.process_names}
        return (
            _safe_basename(str(proc.get("name", ""))).casefold() in names
            or _safe_basename(str(proc.get("exe", ""))).casefold() in names
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

    async def launch_application(
        self, application_id: str, *, timeout_seconds: float = 10.0
    ) -> RunningApplication:
        self._launch_diagnostic = {"stage": "resolution", "mode": "not_dispatched"}
        resolved = self._resolver.resolve(application_id)
        self._launch_diagnostic.update(
            stage="existing_window_observation",
            executable_identity=hashlib.sha256(
                ntpath.normcase(resolved.executable_path).encode("utf-8")
            ).hexdigest(),
        )

        # A background process is not an open application. Reuse only an observed
        # window belonging to the resolved executable, and do not swallow focus errors.
        running = await self._backend.list_running_processes()
        matching_pids = {
            int(proc.get("pid", 0)) for proc in running if self._matches(proc, resolved)
        }
        if resolved.allow_reuse:
            windows = await self._backend.list_windows()
            for win in windows:
                if win.process_id in matching_pids and win.visible:
                    self._launch_diagnostic.update(
                        stage="focus_existing_window", mode="reuse", process_id=win.process_id
                    )
                    focused = await self._backend.focus_window(win.window_id)
                    if self._requires_window and (not focused.foreground or focused.minimized):
                        raise ComputerAdapterError(
                            ComputerFailureCode.ACTION_UNKNOWN_OUTCOME,
                            "Existing application window could not be made visible and foreground.",
                            source=PerceptionSource.APPLICATION_API,
                        )
                    return RunningApplication(
                        process_id=win.process_id,
                        name=resolved.name,
                        executable_path=resolved.executable_path,
                        window_ids=(win.window_id,),
                    )

        # 2. Launch process safely
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

        self._launch_diagnostic.update(stage="window_poll", mode="spawn", dispatched_process_id=pid)
        # 3. Bounded polling verification
        poll_deadline = time.monotonic() + min(max(timeout_seconds, 2.0), 15.0)
        poll_interval = 0.15
        stable_checks = 0
        required_stable_checks = 2
        window_ids = []

        while time.monotonic() < poll_deadline:
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
            stable_checks = (
                stable_checks + 1 if alive and (window_ids or not self._requires_window) else 0
            )
            if stable_checks >= required_stable_checks:
                if window_ids:
                    await self._backend.focus_window(window_ids[0])
                break

            await asyncio.sleep(poll_interval)

        if stable_checks < required_stable_checks:
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
                running = await self._backend.list_running_processes()
                matching_pids = {
                    int(p.get("pid", 0))
                    for p in running
                    if self._matches(p, resolved) and int(p.get("pid", 0)) > 0
                }
                is_running = bool(matching_pids)
                pid = min(matching_pids) if matching_pids else None
                windows = await self._backend.list_windows()
                window_ids = [
                    win.window_id
                    for win in windows
                    if win.process_id in matching_pids and win.visible and not win.minimized
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
    "KnownAppAlias",
    "KNOWN_ALIASES",
    "ResolvedApplication",
    "Win32AppLaunchBackend",
    "WindowsAppLaunchBackend",
    "WindowsAppLaunchProvider",
    "WindowsApplicationResolver",
    "register_app_launch_tools",
]
