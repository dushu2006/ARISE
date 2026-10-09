"""Bounded Windows application discovery and native activation primitives.

Discovery is metadata-led: Start Menu shortcuts, Windows Start/AUMID entries,
installed-app metadata, App Paths, and PATH are considered by the resolver. This
module never scans an entire volume and never constructs a shell command from a
requested application name.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

_MAX_CATALOG_ENTRIES = 4096
_MAX_REGISTRY_ENTRIES = 2048
_MAX_SHORTCUTS = 4096
_MAX_SHORTCUT_DEPTH = 12
_MAX_SCAN_DIRECTORIES = 4096
_MAX_REGISTRY_SEARCH_FILES = 16_384
_MAX_START_APPS_BYTES = 1_000_000
# Launch-mode switches are capability metadata, never user input: a bounded
# switch spelling only, validated at the descriptor boundary.
_LAUNCH_SWITCH = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_MAX_LAUNCH_SWITCHES = 4
# Bounded Start Menu launch arguments kept only to distinguish entry points.
_MAX_LAUNCH_ARGUMENTS = 16
# Windows registers browsers in its own StartMenuInternet list. That registration
# is the generic signal that an application is a browser; nothing here names a
# vendor, and an application absent from the list simply advertises nothing.
_REGISTERED_BROWSER_ROOTS = ("SOFTWARE\\Clients\\StartMenuInternet",)
_REGISTERED_BROWSER_CACHE_SECONDS = 300.0
_registered_browser_cache: tuple[float, frozenset[str]] | None = None
_SHORTCUT_COMMAND_HOSTS = frozenset(
    {
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "wscript.exe",
        "cscript.exe",
        "mshta.exe",
        "rundll32.exe",
        "regsvr32.exe",
    }
)


class ActivationMethod(StrEnum):
    """Supported, non-shell-interpolated Windows activation routes."""

    EXECUTABLE = "executable"
    START_MENU_SHORTCUT = "start_menu_shortcut"
    PACKAGED_AUMID = "packaged_aumid"


def normalize_application_name(value: str) -> str:
    """Normalize a display/process name for exact matching, never fuzzy search."""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    if normalized.endswith(".exe"):
        normalized = normalized[:-4]
    return "".join(character for character in normalized if character.isalnum())


def normalized_executable_key(path: str | None) -> str:
    """Windows-aware, case-insensitive executable identity key."""
    if not path:
        return ""
    import ntpath

    cleaned = str(path).strip().strip('"')
    return ntpath.normcase(ntpath.normpath(cleaned)) if cleaned else ""


def _safe_label(value: Any, *, limit: int = 256) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:limit]


def _safe_aumid(value: Any) -> str | None:
    label = _safe_label(value, limit=512)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}![A-Za-z0-9._-]{1,200}", label):
        return None
    return label


def _executable_name(path: str | None) -> str:
    if not path:
        return ""
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class ApplicationDescriptor:
    """Normalized identity and safe activation metadata for an installed app.

    ``name``, ``executable_path``, ``process_names`` and ``allow_reuse`` retain
    the old resolved-application constructor shape. New discovery sources add
    activation and package/shortcut identity without requiring aliases.
    """

    name: str
    executable_path: str | None = None
    process_names: tuple[str, ...] = ()
    allow_reuse: bool = True
    activation_method: ActivationMethod | None = None
    package_family_name: str | None = None
    package_full_name: str | None = None
    aumid: str | None = None
    shortcut_path: str | None = None
    source: str = "resolved"
    aliases: tuple[str, ...] = ()
    is_web_app: bool = False
    # Launch arguments carried by a Start Menu shortcut. They distinguish two
    # entry points that share one executable (a browser and the browser-installed
    # applications launched through it), and they are never user input: they come
    # from the shortcut itself and are never shell-interpolated.
    launch_arguments: tuple[str, ...] = ()
    # Generic launch-mode capabilities. They are populated from discovered
    # metadata (for example Windows' own registered-browser list), never from an
    # application name, and a mode that is not advertised is refused rather than
    # approximated by reusing or re-launching normally.
    new_window_supported: bool = False
    new_instance_supported: bool = False
    new_window_arguments: tuple[str, ...] = ()
    new_instance_arguments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        clean_name = _safe_label(self.name, limit=256)
        if not clean_name:
            raise ValueError("application descriptor name must be non-empty")
        object.__setattr__(self, "name", clean_name)
        if self.executable_path is not None:
            path = str(self.executable_path).strip()
            if not path or len(path) > 4096 or "\x00" in path:
                raise ValueError("application executable path is invalid")
            object.__setattr__(self, "executable_path", path)
        if len(self.process_names) > 32 or any(
            not isinstance(name, str) or not name.strip() or len(name) > 256
            for name in self.process_names
        ):
            raise ValueError("application process names are invalid")
        if len(self.aliases) > 64 or any(
            not isinstance(alias, str) or not alias.strip() or len(alias) > 256
            for alias in self.aliases
        ):
            raise ValueError("application aliases are invalid")
        if self.shortcut_path is not None:
            shortcut = str(self.shortcut_path).strip()
            if not shortcut or len(shortcut) > 4096 or "\x00" in shortcut:
                raise ValueError("application shortcut path is invalid")
            object.__setattr__(self, "shortcut_path", shortcut)
        family = _safe_label(self.package_family_name, limit=256)
        if self.package_family_name is not None and not family:
            raise ValueError("package family name is invalid")
        if family:
            object.__setattr__(self, "package_family_name", family)
        if self.package_full_name is not None:
            full_name = _safe_label(self.package_full_name, limit=512)
            if not full_name:
                raise ValueError("package full name is invalid")
            object.__setattr__(self, "package_full_name", full_name)
        if self.aumid is not None:
            aumid = _safe_aumid(self.aumid)
            if aumid is None:
                raise ValueError("AUMID is invalid")
            object.__setattr__(self, "aumid", aumid)
        source = _safe_label(self.source, limit=64)
        if not source or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", source):
            raise ValueError("application descriptor source is invalid")
        object.__setattr__(self, "source", source)
        if not isinstance(self.is_web_app, bool):
            raise ValueError("is_web_app must be a boolean")
        if len(self.launch_arguments) > _MAX_LAUNCH_ARGUMENTS or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 256
            or "\x00" in item
            or item.strip() != item
            for item in self.launch_arguments
        ):
            raise ValueError("launch arguments must be bounded, trimmed strings")
        object.__setattr__(self, "launch_arguments", tuple(self.launch_arguments))
        for label, switches in (
            ("new_window_arguments", self.new_window_arguments),
            ("new_instance_arguments", self.new_instance_arguments),
        ):
            if len(switches) > _MAX_LAUNCH_SWITCHES or any(
                not isinstance(item, str) or _LAUNCH_SWITCH.fullmatch(item) is None
                for item in switches
            ):
                raise ValueError(f"{label} must be bounded launch switches")
        for label, supported, switches in (
            ("new_window", self.new_window_supported, self.new_window_arguments),
            ("new_instance", self.new_instance_supported, self.new_instance_arguments),
        ):
            if not isinstance(supported, bool):
                raise ValueError(f"{label}_supported must be a boolean")
            if supported and not switches:
                raise ValueError(f"{label}_supported requires its launch switches")
        method = self.activation_method
        if method is None:
            method = (
                ActivationMethod.PACKAGED_AUMID
                if self.aumid
                else ActivationMethod.START_MENU_SHORTCUT
                if self.shortcut_path
                else ActivationMethod.EXECUTABLE
            )
            object.__setattr__(self, "activation_method", method)
        elif not isinstance(method, ActivationMethod):
            method = ActivationMethod(method)
            object.__setattr__(self, "activation_method", method)
        if method is ActivationMethod.PACKAGED_AUMID and not self.aumid:
            raise ValueError("packaged activation requires an AUMID")
        if method is ActivationMethod.START_MENU_SHORTCUT and not self.shortcut_path:
            raise ValueError("shortcut activation requires a Start Menu shortcut")
        if method is ActivationMethod.EXECUTABLE and not self.executable_path:
            raise ValueError("executable activation requires an executable path")

    @property
    def display_name(self) -> str:
        return self.name

    @property
    def normalized_name(self) -> str:
        return normalize_application_name(self.name)

    @property
    def canonical_identity(self) -> str:
        if self.is_web_app:
            shortcut_key = normalized_executable_key(self.shortcut_path)
            return f"webapp:{self.normalized_name}:{shortcut_key}"
        if self.aumid:
            return f"aumid:{self.aumid.casefold()}"
        if self.package_family_name:
            return f"package:{self.package_family_name.casefold()}"
        executable_key = normalized_executable_key(self.executable_path)
        if executable_key:
            if (
                self.launch_arguments
                and self.activation_method is ActivationMethod.START_MENU_SHORTCUT
            ):
                return (
                    f"shortcut-app:{executable_key}:{_launch_arguments_key(self.launch_arguments)}"
                )
            return f"executable:{executable_key}"
        if self.shortcut_path:
            return f"shortcut:{normalized_executable_key(self.shortcut_path)}"
        return f"name:{self.normalized_name}:{self.source.casefold()}"

    @property
    def diagnostic_identity(self) -> str:
        """Stable opaque identity; paths and raw package metadata are not emitted."""
        return hashlib.sha256(self.canonical_identity.encode("utf-8")).hexdigest()[:16]

    def diagnostic_summary(self) -> dict[str, str]:
        safe_name = "".join(
            character if character.isalnum() or character in " _.-()" else "?"
            for character in self.name[:128]
        )
        return {
            "name": safe_name,
            "source": self.source,
            "activation_method": str(self.activation_method),
            "identity": self.diagnostic_identity,
        }


class WindowsApplicationCatalog:
    """Discover installed applications through bounded Windows metadata sources.

    The optional readers make the catalog deterministic in tests without pretending
    that those fakes exercise Win32, COM, PowerShell, or the Windows shell.
    """

    def __init__(
        self,
        *,
        is_windows: bool | None = None,
        start_menu_roots: Sequence[str | os.PathLike[str]] | None = None,
        start_apps_reader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        shortcut_reader: Callable[[str], tuple[str, str] | None] | None = None,
        registry_entries_reader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self._is_windows = sys.platform == "win32" if is_windows is None else is_windows
        self._start_menu_roots = (
            tuple(Path(root) for root in start_menu_roots)
            if start_menu_roots is not None
            else self._default_start_menu_roots()
        )
        self._start_apps_reader = start_apps_reader or self._read_start_apps
        self._shortcut_reader = shortcut_reader or _read_shell_link
        self._registry_entries_reader = registry_entries_reader or self._read_registry_entries

    @staticmethod
    def _default_start_menu_roots() -> tuple[Path, ...]:
        roots: list[Path] = []
        appdata = os.environ.get("APPDATA")
        programdata = os.environ.get("PROGRAMDATA")
        if appdata:
            roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        if programdata:
            roots.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        return tuple(roots)

    def discover(self) -> tuple[ApplicationDescriptor, ...]:
        if not self._is_windows:
            return ()
        discovered: list[ApplicationDescriptor] = []
        discovered.extend(self._discover_start_apps())
        discovered.extend(self._discover_start_menu_shortcuts())
        discovered.extend(self._discover_registry_apps())
        merged = _deduplicate_descriptors(discovered)[:_MAX_CATALOG_ENTRIES]
        browsers = registered_browser_executables()
        if not browsers:
            return merged
        return tuple(
            advertise_launch_capabilities(item, browser_executables=browsers) for item in merged
        )

    def _discover_start_apps(self) -> list[ApplicationDescriptor]:
        try:
            entries = self._start_apps_reader()
        except Exception:
            return []
        results: list[ApplicationDescriptor] = []
        for entry in tuple(entries)[:_MAX_CATALOG_ENTRIES]:
            if not isinstance(entry, Mapping):
                continue
            name = _safe_label(entry.get("Name") or entry.get("name"))
            aumid = _safe_aumid(entry.get("AppID") or entry.get("AppId") or entry.get("aumid"))
            if not name or not aumid:
                continue
            family = aumid.split("!", 1)[0]
            results.append(
                ApplicationDescriptor(
                    name=name,
                    process_names=(),
                    activation_method=ActivationMethod.PACKAGED_AUMID,
                    package_family_name=family,
                    aumid=aumid,
                    source="windows_start_apps",
                )
            )
        return results

    def _discover_start_menu_shortcuts(self) -> list[ApplicationDescriptor]:
        results: list[ApplicationDescriptor] = []
        scanned = 0
        visited_directories = 0
        for menu_root in self._start_menu_roots:
            if not menu_root.is_dir():
                continue
            for directory, subdirectories, filenames in os.walk(menu_root, followlinks=False):
                visited_directories += 1
                if visited_directories > _MAX_SCAN_DIRECTORIES:
                    return results
                depth = len(Path(directory).relative_to(menu_root).parts)
                subdirectories[:] = sorted(
                    dirname
                    for dirname in subdirectories
                    if depth < _MAX_SHORTCUT_DEPTH and not (Path(directory) / dirname).is_symlink()
                )
                for filename in sorted(filenames):
                    if not filename.casefold().endswith(".lnk"):
                        continue
                    scanned += 1
                    if scanned > _MAX_SHORTCUTS:
                        return results
                    shortcut_path = str(Path(directory) / filename)
                    try:
                        link = self._shortcut_reader(shortcut_path)
                    except Exception:
                        continue
                    if not link:
                        continue
                    target, arguments = link
                    label = Path(filename).stem
                    aumid = _aumid_from_shell_link(target, arguments)
                    if aumid:
                        results.append(
                            ApplicationDescriptor(
                                name=label,
                                activation_method=ActivationMethod.PACKAGED_AUMID,
                                package_family_name=aumid.split("!", 1)[0],
                                aumid=aumid,
                                shortcut_path=shortcut_path,
                                source="start_menu_shortcut",
                            )
                        )
                        continue
                    target_path = os.path.expandvars(str(target).strip().strip('"'))
                    if (
                        _executable_name(target_path).casefold() in _SHORTCUT_COMMAND_HOSTS
                        and str(arguments or "").strip()
                    ):
                        continue
                    if not target_path.casefold().endswith(".exe") or not os.path.isfile(
                        target_path
                    ):
                        continue
                    arguments_text = str(arguments or "").strip()
                    is_web_app = bool(
                        re.search(r"(?i)(?:--app-id=|--app=)", arguments_text)
                    ) or _launches_browser_installed_app(target_path, arguments_text)
                    results.append(
                        ApplicationDescriptor(
                            name=label,
                            executable_path=os.path.normpath(target_path),
                            process_names=(_executable_name(target_path),),
                            activation_method=ActivationMethod.START_MENU_SHORTCUT,
                            shortcut_path=shortcut_path,
                            source="start_menu_shortcut",
                            is_web_app=is_web_app,
                            launch_arguments=tuple(arguments_text.split())[:_MAX_LAUNCH_ARGUMENTS],
                        )
                    )
        return results

    def _discover_registry_apps(self) -> list[ApplicationDescriptor]:
        try:
            entries = self._registry_entries_reader()
        except Exception:
            return []
        results: list[ApplicationDescriptor] = []
        for entry in tuple(entries)[:_MAX_REGISTRY_ENTRIES]:
            if not isinstance(entry, Mapping):
                continue
            name = _safe_label(entry.get("DisplayName") or entry.get("name"))
            if not name:
                continue
            executable = _installed_executable(entry, name)
            if not executable:
                continue
            results.append(
                ApplicationDescriptor(
                    name=name,
                    executable_path=executable,
                    process_names=(_executable_name(executable),),
                    source="installed_registry",
                )
            )
        return results

    @staticmethod
    def _read_start_apps() -> Sequence[Mapping[str, Any]]:
        if sys.platform != "win32":
            return ()
        windows_dir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        candidates = (
            windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
            windows_dir / "Sysnative" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
            windows_dir / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        )
        powershell_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if powershell_path is None:
            return ()
        powershell = str(powershell_path)
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$ProgressPreference='SilentlyContinue';"
            "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
            "Get-StartApps | Select-Object -Property Name,AppID | ConvertTo-Json -Compress"
        )
        try:
            completed = subprocess.run(
                [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=6.0,
                check=False,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        output = completed.stdout
        if (
            completed.returncode != 0
            or len(output.encode("utf-8", errors="replace")) > _MAX_START_APPS_BYTES
        ):
            return ()
        try:
            payload = json.loads(output) if output.strip() else []
        except json.JSONDecodeError:
            return ()
        if isinstance(payload, Mapping):
            payload = [payload]
        if not isinstance(payload, list):
            return ()
        return tuple(item for item in payload[:_MAX_CATALOG_ENTRIES] if isinstance(item, Mapping))

    @staticmethod
    def _read_registry_entries() -> Sequence[Mapping[str, Any]]:
        if sys.platform != "win32":
            return ()
        try:
            import winreg
        except ImportError:
            return ()

        uninstall_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
        views = (
            winreg.KEY_READ,
            winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
        values: list[Mapping[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for registry_root in roots:
            for view in views:
                try:
                    with winreg.OpenKey(registry_root, uninstall_path, 0, view) as root_key:
                        count = min(winreg.QueryInfoKey(root_key)[0], _MAX_REGISTRY_ENTRIES)
                        for index in range(count):
                            try:
                                subkey_name = winreg.EnumKey(root_key, index)
                                with winreg.OpenKey(root_key, subkey_name) as subkey:
                                    item: dict[str, Any] = {}
                                    for value_name in (
                                        "DisplayName",
                                        "DisplayIcon",
                                        "InstallLocation",
                                    ):
                                        try:
                                            item[value_name] = winreg.QueryValueEx(
                                                subkey, value_name
                                            )[0]
                                        except OSError:
                                            continue
                                label = _safe_label(item.get("DisplayName"))
                                icon = _safe_label(item.get("DisplayIcon"), limit=4096)
                                install = _safe_label(item.get("InstallLocation"), limit=4096)
                                fingerprint = (
                                    normalize_application_name(label),
                                    icon.casefold(),
                                    install.casefold(),
                                )
                                if label and fingerprint not in seen:
                                    seen.add(fingerprint)
                                    values.append(item)
                                if len(values) >= _MAX_REGISTRY_ENTRIES:
                                    return tuple(values)
                            except OSError:
                                continue
                except OSError:
                    continue
        return tuple(values)


def _read_registered_browsers() -> Sequence[Mapping[str, Any]]:
    """Read the browsers Windows itself registers for the Start menu/Internet.

    This is an operating-system registration, not a vendor list: any application
    that registers here is treated as a browser for launch-capability purposes,
    and an application that does not register advertises nothing.
    """

    if sys.platform != "win32":
        return ()
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows host
        return ()
    results: list[dict[str, Any]] = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for base in _REGISTERED_BROWSER_ROOTS:
            try:
                container = winreg.OpenKey(root, base)
            except OSError:
                continue
            with container:
                index = 0
                while len(results) < 64:
                    try:
                        browser_key = winreg.EnumKey(container, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        command_key = winreg.OpenKey(
                            container, rf"{browser_key}\\shell\\open\\command"
                        )
                    except OSError:
                        continue
                    with command_key:
                        try:
                            value, _ = winreg.QueryValueEx(command_key, "")
                        except OSError:
                            continue
                    if isinstance(value, str) and value.strip():
                        results.append({"name": browser_key, "command": value})
    return results


def _command_executable(command: str) -> str | None:
    """Extract the executable from a registered open-command string."""

    text = str(command or "").strip()
    if not text:
        return None
    if text.startswith('"'):
        closing = text.find('"', 1)
        candidate = text[1:closing] if closing > 0 else text.strip('"')
    else:
        candidate = text.split(" ")[0]
    candidate = os.path.expandvars(candidate.strip().strip('"'))
    if candidate.casefold().endswith(".exe"):
        return os.path.normpath(candidate)
    return None


def registered_browser_executables(
    *,
    reader: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> frozenset[str]:
    """Return normalized executable keys of applications Windows registers as browsers."""

    global _registered_browser_cache
    source = reader or _read_registered_browsers
    now = clock()
    if _registered_browser_cache is not None:
        cached_at, cached = _registered_browser_cache
        if now - cached_at < _REGISTERED_BROWSER_CACHE_SECONDS:
            return cached
    keys: set[str] = set()
    try:
        entries = source()
    except Exception:
        entries = ()
    for entry in tuple(entries)[:64]:
        if not isinstance(entry, Mapping):
            continue
        executable = _command_executable(str(entry.get("command") or ""))
        if executable:
            key = normalized_executable_key(executable)
            if key:
                keys.add(key)
    result = frozenset(keys)
    _registered_browser_cache = (now, result)
    return result


def advertise_launch_capabilities(
    descriptor: ApplicationDescriptor,
    *,
    browser_executables: frozenset[str] | None = None,
) -> ApplicationDescriptor:
    """Attach launch-mode capabilities discovered from generic OS metadata.

    A browser that Windows registers as a Start-menu/Internet client is asked for
    a new window with the conventional ``--new-window`` switch. Nothing here
    inspects the application's name, so an unregistered application simply
    advertises no new-window capability and an explicit request fails closed.
    """

    if descriptor.new_window_supported or descriptor.is_web_app:
        return descriptor
    browsers = (
        browser_executables if browser_executables is not None else registered_browser_executables()
    )
    if not browsers:
        return descriptor
    key = normalized_executable_key(descriptor.executable_path)
    if key and key in browsers:
        return replace(
            descriptor,
            new_window_supported=True,
            new_window_arguments=("--new-window",),
        )
    return descriptor


def _launch_arguments_key(arguments: Sequence[str]) -> str:
    """Stable, opaque key for the launch arguments of one shortcut entry point."""

    joined = " ".join(str(item) for item in arguments)
    return hashlib.sha256(joined.encode("utf-8", "ignore")).hexdigest()[:16]


def _launches_browser_installed_app(target_path: str, arguments: str) -> bool:
    """True when a shortcut launches an application installed *through* a browser.

    Browser-installed applications (PWAs) and app-specific browser profiles are
    Start Menu shortcuts whose target is a browser executable plus launch
    arguments that select the application (an --app-id= switch, an --app= switch,
    a site identifier, ...). Both halves are checked generically: the browser test
    uses the operating system's own registered-browser list, and the application
    test only asks whether the arguments select something. No vendor is named
    here, and a plain browser shortcut (no selecting arguments) is still the
    browser.
    """

    text = str(arguments or "").strip()
    if not text:
        return False
    browsers = registered_browser_executables()
    if not browsers:
        return False
    executable_key = normalized_executable_key(target_path)
    if not executable_key or executable_key not in browsers:
        return False
    if re.search(r"(?i)--app(?:-id|-name|-url|-launch-url|-short-name)?[= ]", text):
        return True
    # A non-switch argument names the application or site to open.
    return any(not token.startswith("-") for token in text.split())


def _aumid_from_shell_link(target: str, arguments: str) -> str | None:
    joined = f"{target} {arguments}"
    match = re.search(
        r"(?i)shell:AppsFolder\\([A-Za-z0-9._-]{1,200}![A-Za-z0-9._-]{1,200})",
        joined,
    )
    return _safe_aumid(match.group(1)) if match else None


def _display_icon_executable(value: Any) -> str | None:
    icon = _safe_label(value, limit=4096)
    if not icon:
        return None
    if icon.startswith('"'):
        closing_quote = icon.find('"', 1)
        candidate = icon[1:closing_quote] if closing_quote > 0 else icon.strip('"')
    else:
        candidate = icon.split(",", 1)[0].strip()
    candidate = os.path.expandvars(candidate.strip().strip('"'))
    if candidate.casefold().endswith(".exe") and os.path.isfile(candidate):
        return os.path.normpath(candidate)
    return None


def _find_unique_named_executable(install_location: Any, display_name: str) -> str | None:
    raw_root = _safe_label(install_location, limit=4096)
    if not raw_root:
        return None
    root = Path(os.path.expandvars(raw_root.strip('"')))
    if not root.is_dir():
        return None
    wanted = normalize_application_name(display_name)
    if not wanted:
        return None
    matches: set[str] = set()
    inspected_files = 0
    inspected_directories = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and inspected_directories < _MAX_SCAN_DIRECTORIES:
        current, depth = stack.pop()
        inspected_directories += 1
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir() and depth < 6:
                    stack.append((entry, depth + 1))
                    continue
                inspected_files += 1
                if inspected_files > _MAX_REGISTRY_SEARCH_FILES:
                    return None
                if entry.is_file() and entry.suffix.casefold() == ".exe":
                    if normalize_application_name(entry.stem) == wanted:
                        matches.add(os.path.normpath(str(entry)))
                        if len(matches) > 1:
                            return None
            except OSError:
                continue
    return next(iter(matches)) if len(matches) == 1 else None


def _installed_executable(entry: Mapping[str, Any], display_name: str) -> str | None:
    icon = _display_icon_executable(entry.get("DisplayIcon"))
    if icon and normalize_application_name(Path(icon).stem) == normalize_application_name(
        display_name
    ):
        return icon
    return _find_unique_named_executable(entry.get("InstallLocation"), display_name)


def _deduplicate_descriptors(
    descriptors: Sequence[ApplicationDescriptor],
) -> tuple[ApplicationDescriptor, ...]:
    by_identity: dict[str, ApplicationDescriptor] = {}
    for descriptor in descriptors:
        identity = descriptor.canonical_identity
        current = by_identity.get(identity)
        if current is None:
            by_identity[identity] = descriptor
            continue
        aliases = tuple(dict.fromkeys((*current.aliases, descriptor.name, *descriptor.aliases)))
        # Prefer the metadata-rich, native activation record while retaining the
        # Start Menu link when it is the user's application-specific launcher/PWA.
        chosen = current
        if descriptor.activation_method is ActivationMethod.PACKAGED_AUMID:
            chosen = descriptor
        elif (
            current.activation_method is ActivationMethod.EXECUTABLE
            and descriptor.activation_method is ActivationMethod.START_MENU_SHORTCUT
        ):
            chosen = descriptor
        if chosen is current:
            by_identity[identity] = ApplicationDescriptor(
                name=current.name,
                executable_path=current.executable_path,
                process_names=tuple(
                    dict.fromkeys((*current.process_names, *descriptor.process_names))
                ),
                allow_reuse=current.allow_reuse and descriptor.allow_reuse,
                activation_method=current.activation_method,
                package_family_name=current.package_family_name or descriptor.package_family_name,
                package_full_name=current.package_full_name or descriptor.package_full_name,
                aumid=current.aumid or descriptor.aumid,
                shortcut_path=current.shortcut_path or descriptor.shortcut_path,
                source=current.source,
                aliases=aliases,
                is_web_app=current.is_web_app or descriptor.is_web_app,
                launch_arguments=current.launch_arguments or descriptor.launch_arguments,
                new_window_supported=(
                    current.new_window_supported or descriptor.new_window_supported
                ),
                new_instance_supported=(
                    current.new_instance_supported or descriptor.new_instance_supported
                ),
                new_window_arguments=(
                    current.new_window_arguments or descriptor.new_window_arguments
                ),
                new_instance_arguments=(
                    current.new_instance_arguments or descriptor.new_instance_arguments
                ),
            )
        else:
            by_identity[identity] = ApplicationDescriptor(
                name=chosen.name,
                executable_path=chosen.executable_path or current.executable_path,
                process_names=tuple(
                    dict.fromkeys((*current.process_names, *descriptor.process_names))
                ),
                allow_reuse=current.allow_reuse and descriptor.allow_reuse,
                activation_method=chosen.activation_method,
                package_family_name=chosen.package_family_name or current.package_family_name,
                package_full_name=chosen.package_full_name or current.package_full_name,
                aumid=chosen.aumid or current.aumid,
                shortcut_path=chosen.shortcut_path or current.shortcut_path,
                source=chosen.source,
                aliases=aliases,
                is_web_app=current.is_web_app or descriptor.is_web_app,
                launch_arguments=current.launch_arguments or descriptor.launch_arguments,
                new_window_supported=(
                    current.new_window_supported or descriptor.new_window_supported
                ),
                new_instance_supported=(
                    current.new_instance_supported or descriptor.new_instance_supported
                ),
                new_window_arguments=(
                    current.new_window_arguments or descriptor.new_window_arguments
                ),
                new_instance_arguments=(
                    current.new_instance_arguments or descriptor.new_instance_arguments
                ),
            )
    return tuple(
        sorted(
            by_identity.values(), key=lambda item: (item.normalized_name, item.canonical_identity)
        )
    )


def _read_shell_link(shortcut_path: str) -> tuple[str, str] | None:
    """Read an .lnk target through IShellLinkW without executing the shortcut."""
    if sys.platform != "win32":
        return None

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def make_guid(value: str) -> GUID:
        import uuid

        parsed = uuid.UUID(value)
        return GUID(
            parsed.time_low,
            parsed.time_mid,
            parsed.time_hi_version,
            (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:]),
        )

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    initialized_hr = int(ole32.CoInitializeEx(None, 0x2))  # COINIT_APARTMENTTHREADED
    initialized = initialized_hr in (0, 1)
    # RPC_E_CHANGED_MODE means COM is already initialized on this thread; calls may
    # still use that apartment, but this routine must not uninitialize it.
    if initialized_hr < 0 and initialized_hr != -2147417850:
        return None
    instance = ctypes.c_void_p()
    clsid = make_guid("00021401-0000-0000-C000-000000000046")
    iid = make_guid("000214F9-0000-0000-C000-000000000046")
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    try:
        hr = int(
            ole32.CoCreateInstance(
                ctypes.byref(clsid),
                None,
                1,  # CLSCTX_INPROC_SERVER
                ctypes.byref(iid),
                ctypes.byref(instance),
            )
        )
        if hr < 0 or not instance.value:
            return None
        vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        winfun = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        get_path = winfun(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar),
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        )(vtable[3])
        get_arguments = winfun(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar),
            ctypes.c_int,
        )(vtable[10])
        target_buffer = ctypes.create_unicode_buffer(4096)
        arguments_buffer = ctypes.create_unicode_buffer(4096)
        if get_path(instance, target_buffer, len(target_buffer), None, 0) < 0:
            return None
        if get_arguments(instance, arguments_buffer, len(arguments_buffer)) < 0:
            arguments_buffer.value = ""
        return target_buffer.value, arguments_buffer.value
    except Exception:
        return None
    finally:
        if instance.value:
            try:
                winfun = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
                vtable = ctypes.cast(
                    instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                ).contents
                release = winfun(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
                release(instance)
            except Exception:
                pass
        if initialized:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


def package_family_name_for_pid(process_id: int) -> str | None:
    """Query the OS package identity for a PID; never infer it from process names."""
    if sys.platform != "win32" or process_id <= 0:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetPackageFamilyName.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_wchar),
        ]
        kernel32.GetPackageFamilyName.restype = ctypes.c_long
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        process = kernel32.OpenProcess(0x1000, 0, int(process_id))  # QUERY_LIMITED_INFORMATION
        if not process:
            return None
        try:
            count = ctypes.c_uint32(0)
            result = int(kernel32.GetPackageFamilyName(process, ctypes.byref(count), None))
            if result != 122 or not 1 <= count.value <= 512:  # ERROR_INSUFFICIENT_BUFFER
                return None
            buffer = ctypes.create_unicode_buffer(count.value)
            result = int(kernel32.GetPackageFamilyName(process, ctypes.byref(count), buffer))
            return _safe_label(buffer.value, limit=256) if result == 0 else None
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        return None


def app_user_model_id_for_window(window_handle: int) -> str | None:
    """Read the OS AppUserModelID property for a top-level HWND, if present."""
    if sys.platform != "win32" or window_handle <= 0:
        return None
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class PropertyKey(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PropVariantUnion(ctypes.Union):
        _fields_ = [
            ("pwszVal", wintypes.LPWSTR),
            ("pointer", ctypes.c_void_p),
            ("llVal", ctypes.c_longlong),
            ("padding", ctypes.c_ubyte * 16),
        ]

    class PropVariant(ctypes.Structure):
        _fields_ = [
            ("vt", wintypes.USHORT),
            ("wReserved1", wintypes.USHORT),
            ("wReserved2", wintypes.USHORT),
            ("wReserved3", wintypes.USHORT),
            ("value", PropVariantUnion),
        ]

    def make_guid(value: str) -> GUID:
        import uuid

        parsed = uuid.UUID(value)
        return GUID(
            parsed.time_low,
            parsed.time_mid,
            parsed.time_hi_version,
            (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:]),
        )

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    initialized_hr = int(ole32.CoInitializeEx(None, 0x2))
    initialized = initialized_hr in (0, 1)
    if initialized_hr < 0 and initialized_hr != -2147417850:
        return None
    store = ctypes.c_void_p()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    get_store = shell32.SHGetPropertyStoreForWindow
    get_store.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    get_store.restype = ctypes.c_long
    iid = make_guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")  # IID_IPropertyStore
    prop_key = PropertyKey(make_guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5)
    variant = PropVariant()
    ole32.PropVariantClear.argtypes = [ctypes.POINTER(PropVariant)]
    ole32.PropVariantClear.restype = ctypes.c_long
    winfun = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    try:
        result = int(
            get_store(wintypes.HWND(window_handle), ctypes.byref(iid), ctypes.byref(store))
        )
        if result < 0 or not store.value:
            return None
        vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_value = winfun(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(PropertyKey),
            ctypes.POINTER(PropVariant),
        )(vtable[5])
        result = int(get_value(store, ctypes.byref(prop_key), ctypes.byref(variant)))
        if result < 0 or variant.vt not in (8, 31):  # VT_BSTR / VT_LPWSTR
            return None
        return _safe_aumid(variant.value.pwszVal)
    except Exception:
        return None
    finally:
        if store.value:
            try:
                vtable = ctypes.cast(
                    store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                ).contents
                release = winfun(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
                release(store)
            except Exception:
                pass
        try:
            ole32.PropVariantClear(ctypes.byref(variant))
        except Exception:
            pass
        if initialized:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


def activate_packaged_application(aumid: str) -> int:
    """Activate a registered package through IApplicationActivationManager."""
    if sys.platform != "win32":
        raise OSError("Packaged application activation requires Windows.")
    valid_aumid = _safe_aumid(aumid)
    if valid_aumid is None:
        raise ValueError("AUMID is invalid")

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def make_guid(value: str) -> GUID:
        import uuid

        parsed = uuid.UUID(value)
        return GUID(
            parsed.time_low,
            parsed.time_mid,
            parsed.time_hi_version,
            (ctypes.c_ubyte * 8).from_buffer_copy(parsed.bytes[8:]),
        )

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = ctypes.c_long
    initialized_hr = int(ole32.CoInitializeEx(None, 0x0))  # COINIT_MULTITHREADED
    initialized = initialized_hr in (0, 1)
    if initialized_hr < 0 and initialized_hr != -2147417850:
        raise OSError(f"COM initialization failed (HRESULT 0x{initialized_hr & 0xFFFFFFFF:08X}).")
    instance = ctypes.c_void_p()
    clsid = make_guid("45BA127D-10A8-46EA-8AB7-56EA9078943C")
    iid = make_guid("2E941141-7F97-4756-BA1D-9DECDE894A3D")
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(GUID),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    winfun = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    try:
        hr = int(
            ole32.CoCreateInstance(
                ctypes.byref(clsid),
                None,
                4,  # CLSCTX_LOCAL_SERVER
                ctypes.byref(iid),
                ctypes.byref(instance),
            )
        )
        if hr < 0 or not instance.value:
            raise OSError(f"Activation manager unavailable (HRESULT 0x{hr & 0xFFFFFFFF:08X}).")
        vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        activate = winfun(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        )(vtable[3])
        process_id = ctypes.c_uint32(0)
        hr = int(activate(instance, valid_aumid, None, 0, ctypes.byref(process_id)))
        if hr < 0:
            raise OSError(
                f"Windows rejected packaged app activation (HRESULT 0x{hr & 0xFFFFFFFF:08X})."
            )
        return int(process_id.value) if process_id.value > 0 else 0
    finally:
        if instance.value:
            try:
                vtable = ctypes.cast(
                    instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                ).contents
                release = winfun(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
                release(instance)
            except Exception:
                pass
        if initialized:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


def shell_execute_shortcut(shortcut_path: str) -> int | None:
    """Open a trusted Start Menu .lnk via ShellExecuteEx without cmd/shell text."""
    if sys.platform != "win32":
        raise OSError("Start Menu shortcut activation requires Windows.")
    path = str(shortcut_path).strip()
    if not path.casefold().endswith(".lnk") or not os.path.isfile(path):
        raise FileNotFoundError("Start Menu shortcut is unavailable.")
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    execute = shell32.ShellExecuteExW
    execute.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    execute.restype = wintypes.BOOL
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(ShellExecuteInfo)
    info.fMask = 0x00000040 | 0x00000100 | 0x00000400  # no-close-process, no-async, no-UI
    info.lpVerb = "open"
    info.lpFile = path
    info.nShow = 1
    if not execute(ctypes.byref(info)):
        error = ctypes.get_last_error()
        raise OSError(error, "Windows could not activate the Start Menu shortcut.")
    process_id: int | None = None
    try:
        if info.hProcess:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
            kernel32.GetProcessId.restype = wintypes.DWORD
            process_id = int(kernel32.GetProcessId(info.hProcess) or 0) or None
    finally:
        if info.hProcess:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(info.hProcess)
    return process_id


__all__ = [
    "ActivationMethod",
    "ApplicationDescriptor",
    "WindowsApplicationCatalog",
    "activate_packaged_application",
    "app_user_model_id_for_window",
    "normalize_application_name",
    "normalized_executable_key",
    "package_family_name_for_pid",
    "shell_execute_shortcut",
]
