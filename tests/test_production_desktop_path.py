"""Composition inspection and FAKE-backend regressions, not live Windows evidence."""

import json
import os
from collections.abc import Callable, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
from test_planner_contract import EXECUTABLE_PLAN, ScriptedProvider
from test_windows_app_launch import FakeAppLaunchBackend
from test_windows_uia_and_perception import FakeUiaBackend

from arise.adapters.memory import InMemoryEnvironment, SetFactTool
from arise.adapters.secrets import MemorySecretProvider
from arise.adapters.windows_app_discovery import (
    WindowsApplicationCatalog,
    normalized_executable_key,
)
from arise.adapters.windows_app_launch import (
    ResolvedApplication,
    Win32AppLaunchBackend,
    WindowsAppLaunchProvider,
    WindowsApplicationResolver,
    register_app_launch_tools,
)
from arise.adapters.windows_uia import (
    Win32UiaBackend,
    WindowsUiaProvider,
    register_windows_uia_tools,
)
from arise.config.settings import AppSettings, DatabaseSettings, DesktopSettings, SecuritySettings
from arise.core.computer import (
    ComputerFailureCode,
    PerceptionSource,
    Rect,
    WindowRecord,
)
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    Condition,
    RiskLevel,
    TargetIdentity,
)
from arise.core.engine import TaskEngine
from arise.core.events import InMemoryEventStore
from arise.core.model_gateway import ModelRouter
from arise.core.models import ActionProposal, PlanStep, TaskPlan, UserRequest
from arise.core.planner import GatewayTaskPlanner
from arise.core.planning import InvalidPlan
from arise.core.policy import PolicyDecisionKind, PolicyEngine
from arise.core.ports import ToolRegistry, VerificationStatus
from arise.core.resources import ResourceManager
from arise.core.runtime import AgentRuntime
from arise.core.tasks import ActionStep, InMemoryTaskRepository, StepStatus, TaskRecord, TaskStatus
from arise.server import CompositeEnvironment, CompositeVerifier, create_app


def _empty_catalog_resolver() -> WindowsApplicationResolver:
    return WindowsApplicationResolver(catalog=WindowsApplicationCatalog(is_windows=False))


def authority():
    return AuthorizationContext(
        principal_id="user",
        user_intent_id="intent",
        capabilities=frozenset({"desktop.ui_automation", "desktop.launch"}),
    )


def launch_fixture():
    backend = FakeAppLaunchBackend()
    backend.requires_visible_window = True
    resolver = _empty_catalog_resolver()
    resolver.register_alias(
        "chrome",
        ResolvedApplication(
            name="Google Chrome",
            executable_path=r"C:\Chrome\chrome.exe",
            process_names=("chrome.exe",),
            allow_reuse=True,
        ),
    )
    provider = WindowsAppLaunchProvider(backend=backend, resolver=resolver)
    action = ActionContract(
        task_id="launch",
        target=None,
        tool_name="system.app_launch",
        risk=RiskLevel.R1,
        authority=authority(),
        parameters={"application": "chrome"},
    )
    return backend, provider, action


def window(pid=10):
    return WindowRecord("hwnd-10", pid, "Chrome", "chrome.exe", True, False, False, True)


@pytest.mark.asyncio
async def test_native_contract_requires_visible_window_and_exact_executable():
    backend, provider, action = launch_fixture()
    backend.running_processes = [{"pid": 10, "name": "chrome.exe", "exe": r"C:\Chrome\chrome.exe"}]
    assert (await provider.verify(action)).status is VerificationStatus.FAILED
    backend.windows = [window()]
    assert (await provider.verify(action)).status is VerificationStatus.PASSED
    backend.running_processes[0]["exe"] = r"C:\Impostor\chrome.exe"
    assert (await provider.verify(action)).status is VerificationStatus.FAILED
    backend.running_processes = []
    assert (await provider.verify(action)).status is VerificationStatus.FAILED


@pytest.mark.asyncio
async def test_background_process_does_not_short_circuit_launch():
    backend, provider, _ = launch_fixture()
    backend.running_processes = [{"pid": 10, "name": "chrome.exe", "exe": r"C:\Chrome\chrome.exe"}]
    original = backend.launch_process

    async def launch(path):
        pid = await original(path)
        backend.windows = [window(pid)]
        return pid

    backend.launch_process = launch
    await provider.launch_application("chrome")
    assert backend.launched_paths == [r"C:\Chrome\chrome.exe"]
    assert backend.focused_windows == ["hwnd-10"]
    assert provider.launch_diagnostic["mode"] == "spawn"
    assert provider.launch_diagnostic["dispatched_process_id"] == 1000
    assert "Chrome" not in json.dumps(provider.launch_diagnostic)


@pytest.mark.asyncio
async def test_reuse_focus_failure_is_recorded_and_never_duplicates_the_application():
    """Windows can refuse foreground without the window being closed.

    "The application is open" and "the application took focus" are separate facts.
    The window is still observed visible and ownership-verified after the refused
    activation, so an ordinary "open" request reuses it instead of spawning a
    second instance; the refused focus is recorded rather than swallowed.
    """
    backend, provider, _ = launch_fixture()
    backend.running_processes = [{"pid": 10, "name": "chrome.exe", "exe": r"C:\Chrome\chrome.exe"}]
    backend.windows = [window()]

    async def fail(_):
        raise ComputerAdapterError(ComputerFailureCode.FOCUS_FAILED, "Foreground change refused.")

    backend.focus_window = fail
    app = await provider.launch_application("chrome", timeout_seconds=2)

    assert backend.launched_paths == []
    assert app.window_ids == ("hwnd-10",)
    diagnostic = provider.launch_diagnostic
    assert diagnostic["mode"] == "reuse"
    assert diagnostic["focus_verified"] is False
    assert diagnostic["focus_error"] == ComputerFailureCode.FOCUS_FAILED.value


# ---------------------------------------------------------------------------------
# Chromium-style multi-process association: the launcher PID is not the window owner.
# These are FAKE-backend regressions; they are not Windows verification evidence.
# ---------------------------------------------------------------------------------

CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class MultiProcessLaunchBackend:
    """Deterministic Chromium-like backend where the window owner is not the launcher.

    ``launch_process`` models Chrome's launcher semantics: the spawned process is not
    automatically the browser-window owner, and an already running browser process can
    handle the request. Tests install ``on_launch`` to model the resulting desktop
    mutation deterministically instead of depending on real Windows behavior.
    """

    requires_visible_window = True

    def __init__(self, *, keep_launcher_alive: bool = False) -> None:
        self.keep_launcher_alive = keep_launcher_alive
        self.processes: list[dict[str, Any]] = []
        self.windows: list[WindowRecord] = []
        self.launched_paths: list[str] = []
        self.focus_calls: list[str] = []
        self.focus_error: Exception | None = None
        self.focus_reports_success_without_change = False
        self.on_launch: Callable[[int], None] | None = None
        self._next_pid = 5000

    def add_process(self, pid: int, executable_path: str, *, name: str = "chrome.exe") -> int:
        self.processes.append({"pid": pid, "name": name, "exe": executable_path})
        return pid

    def add_window(
        self,
        window_id: str,
        process_id: int,
        *,
        executable_path: str | None = CHROME_EXE,
        foreground: bool = False,
        minimized: bool = False,
    ) -> WindowRecord:
        record = WindowRecord(
            window_id=window_id,
            process_id=process_id,
            title="Chrome",
            application="chrome.exe",
            visible=True,
            minimized=minimized,
            maximized=False,
            foreground=foreground,
            bounds=Rect(0.0, 0.0, 1280.0, 800.0),
            executable_path=executable_path,
        )
        self.windows.append(record)
        return record

    def set_foreground(self, window_id: str) -> None:
        self.windows = [
            replace(window, foreground=window.window_id == window_id) for window in self.windows
        ]

    async def list_running_processes(self) -> Sequence[dict[str, Any]]:
        return [dict(process) for process in self.processes]

    async def launch_process(self, executable_path: str) -> int:
        self.launched_paths.append(executable_path)
        launcher_pid = self._next_pid
        self._next_pid += 1
        if self.keep_launcher_alive:
            self.add_process(launcher_pid, executable_path)
        if self.on_launch is not None:
            self.on_launch(launcher_pid)
        return launcher_pid

    async def is_process_alive(self, pid: int) -> bool:
        return any(int(process["pid"]) == pid for process in self.processes)

    async def list_windows(self) -> Sequence[WindowRecord]:
        return list(self.windows)

    async def focus_window(self, window_id: str) -> WindowRecord:
        self.focus_calls.append(window_id)
        if self.focus_error is not None:
            raise self.focus_error
        target = next((window for window in self.windows if window.window_id == window_id), None)
        if target is None:
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Requested window does not exist.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        if not self.focus_reports_success_without_change:
            self.set_foreground(window_id)
        return replace(target, foreground=True)


def multi_process_fixture(*, keep_launcher_alive: bool = False, allow_reuse: bool = True):
    backend = MultiProcessLaunchBackend(keep_launcher_alive=keep_launcher_alive)
    resolver = _empty_catalog_resolver()
    resolver.register_alias(
        "chrome",
        ResolvedApplication(
            name="Google Chrome",
            executable_path=CHROME_EXE,
            process_names=("chrome.exe",),
            allow_reuse=allow_reuse,
        ),
    )
    provider = WindowsAppLaunchProvider(backend=backend, resolver=resolver)
    action = ActionContract(
        task_id="launch-multiprocess",
        target=None,
        tool_name="system.app_launch",
        risk=RiskLevel.R1,
        authority=authority(),
        parameters={"application": "chrome"},
    )
    return backend, provider, action


@pytest.mark.asyncio
async def test_launcher_pid_is_associated_with_the_browser_window_owner():
    backend, provider, _ = multi_process_fixture()
    browser_pid = backend.add_process(7001, CHROME_EXE)

    def on_launch(launcher_pid: int) -> None:
        del launcher_pid
        # Chrome's launcher hands the request off and exits; the browser process
        # (different PID, same installed executable) owns the new top-level window.
        backend.add_window("hwnd-browser", browser_pid)

    backend.on_launch = on_launch
    app = await provider.launch_application("chrome")

    assert backend.launched_paths == [CHROME_EXE]
    assert app.process_id == browser_pid
    assert app.window_ids == ("hwnd-browser",)
    diagnostic = provider.launch_diagnostic
    assert diagnostic["mode"] == "spawn"
    assert diagnostic["dispatched_process_id"] == 5000
    assert diagnostic["window_owner_process_id"] == browser_pid
    assert diagnostic["window_evidence"] == "new_window"
    assert backend.focus_calls == ["hwnd-browser"]


@pytest.mark.asyncio
async def test_child_process_window_owner_is_resolved_by_executable_identity():
    backend, provider, _ = multi_process_fixture(keep_launcher_alive=True)

    def on_launch(launcher_pid: int) -> None:
        child_pid = backend.add_process(launcher_pid + 1, CHROME_EXE)
        backend.add_window("hwnd-child-owned", child_pid)

    backend.on_launch = on_launch
    app = await provider.launch_application("chrome")

    assert app.process_id == 5001
    assert app.process_id != provider.launch_diagnostic["dispatched_process_id"]
    assert app.window_ids == ("hwnd-child-owned",)


@pytest.mark.asyncio
async def test_same_name_window_owner_with_a_different_executable_is_rejected():
    backend, provider, _ = multi_process_fixture()

    def on_launch(launcher_pid: int) -> None:
        del launcher_pid
        impostor_pid = backend.add_process(7002, r"C:\Impostor\chrome.exe")
        backend.add_window("hwnd-impostor", impostor_pid, executable_path=r"C:\Impostor\chrome.exe")

    backend.on_launch = on_launch
    with pytest.raises(ComputerAdapterError) as failure:
        await provider.launch_application("chrome", timeout_seconds=2)

    # No executable-verified application process or window remains: fail closed.
    assert failure.value.code is ComputerFailureCode.ACTION_UNKNOWN_OUTCOME
    assert backend.focus_calls == []


@pytest.mark.asyncio
async def test_window_without_an_observed_owner_identity_fails_closed():
    backend, provider, _ = multi_process_fixture()

    def on_launch(launcher_pid: int) -> None:
        del launcher_pid
        # A visible window appears, but nothing observed proves which application owns it.
        backend.add_window("hwnd-unidentified", 7003, executable_path=None)

    backend.on_launch = on_launch
    with pytest.raises(ComputerAdapterError) as failure:
        await provider.launch_application("chrome", timeout_seconds=2)

    assert failure.value.code is ComputerFailureCode.ACTION_UNKNOWN_OUTCOME
    assert backend.focus_calls == []


@pytest.mark.asyncio
async def test_conflicting_window_owner_identity_is_rejected():
    backend, provider, _ = multi_process_fixture()
    browser_pid = backend.add_process(7004, CHROME_EXE)

    def on_launch(launcher_pid: int) -> None:
        del launcher_pid
        # The window record and the process snapshot disagree about the owner executable.
        backend.add_window("hwnd-conflict", browser_pid, executable_path=r"C:\Other\chrome.exe")

    backend.on_launch = on_launch
    with pytest.raises(ComputerAdapterError) as failure:
        await provider.launch_application("chrome", timeout_seconds=2)

    assert failure.value.code is ComputerFailureCode.OWNERSHIP_VERIFICATION_FAILED
    assert backend.focus_calls == []


@pytest.mark.asyncio
async def test_preexisting_application_window_is_not_evidence_of_a_new_launch():
    backend, provider, _ = multi_process_fixture(allow_reuse=False)
    browser_pid = backend.add_process(7100, CHROME_EXE)
    backend.add_window("hwnd-existing-chrome", browser_pid)
    other_pid = backend.add_process(7200, r"C:\Other\other.exe", name="other.exe")
    backend.add_window(
        "hwnd-other", other_pid, executable_path=r"C:\Other\other.exe", foreground=True
    )

    with pytest.raises(ComputerAdapterError) as failure:
        await provider.launch_application("chrome", timeout_seconds=2)

    # A pre-existing Chrome process/window and an unrelated foreground window prove nothing.
    assert failure.value.code is ComputerFailureCode.ACTION_VERIFICATION_FAILED
    assert backend.launched_paths == [CHROME_EXE]
    assert "hwnd-existing-chrome" not in backend.focus_calls


# ---------------------------------------------------------------------------------
# Host-state emulation.
#
# The fallback resolution path reads OS metadata: environment variables, the App
# Paths registry, ``PATH``, and the list of applications Windows registers as
# browsers. Tests that touch it must not depend on whether Chrome is installed on
# the machine running the suite, so the metadata is pinned here. These are fakes:
# they do not exercise Win32, COM, PowerShell or the registry, and they are not
# Windows evidence -- they only make the outcome host-independent.
# ---------------------------------------------------------------------------------

_LAUNCH_MODULE = "arise.adapters.windows_app_launch"


@contextmanager
def _emulate_browser_host():
    """Emulate a Windows host on which the browser above is installed and registered."""

    real_isfile, real_expandvars = os.path.isfile, os.path.expandvars

    def _expandvars(value: str) -> str:
        expanded = (
            value.replace("%ProgramFiles(x86)%", r"C:\Program Files (x86)")
            .replace("%ProgramFiles%", r"C:\Program Files")
            .replace("%LocalAppData%", r"C:\Users\tester\AppData\Local")
        )
        return real_expandvars(expanded)

    def _isfile(value: str) -> bool:
        return value == CHROME_EXE or real_isfile(value)

    with ExitStack() as stack:
        stack.enter_context(patch(f"{_LAUNCH_MODULE}.sys.platform", "win32"))
        stack.enter_context(patch(f"{_LAUNCH_MODULE}.os.path.isfile", _isfile))
        stack.enter_context(patch(f"{_LAUNCH_MODULE}.os.path.expandvars", _expandvars))
        stack.enter_context(patch(f"{_LAUNCH_MODULE}.shutil.which", lambda _candidate: None))
        stack.enter_context(
            patch(
                f"{_LAUNCH_MODULE}.registered_browser_executables",
                lambda: frozenset({normalized_executable_key(CHROME_EXE)}),
            )
        )
        stack.enter_context(
            patch.object(
                WindowsApplicationResolver,
                "_query_windows_app_paths_candidates",
                lambda _self, _alias, _name: (CHROME_EXE,),
            )
        )
        yield


@pytest.mark.asyncio
async def test_registered_alias_keeps_its_configuration_when_host_registers_a_browser():
    """An explicitly registered alias outranks OS path fallbacks on any host.

    Capability advertising (``--new-window`` for applications Windows registers as
    browsers) rebuilds the fallback descriptors. That rebuild must not demote a
    registered alias to the same score as a path fallback: the fallback carries the
    alias defaults and would silently override the registered configuration. This
    is host-independent by construction, not Windows evidence.
    """

    with _emulate_browser_host():
        resolver = _empty_catalog_resolver()
        resolver.register_alias(
            "chrome",
            ResolvedApplication(
                name="Google Chrome",
                executable_path=CHROME_EXE,
                process_names=("chrome.exe",),
                allow_reuse=False,
            ),
        )
        descriptor = resolver.resolve("chrome")

    assert descriptor.allow_reuse is False
    assert descriptor.executable_path == CHROME_EXE
    # Generic capability advertising still applies; only the alias' priority changed.
    assert descriptor.new_window_supported is True


@pytest.mark.asyncio
async def test_preexisting_window_is_not_reused_when_host_registers_a_browser():
    """Same contract as the reuse-refusal test above, with the host pinned.

    The reusable behaviour of an application is configuration, not ambient host
    state: a host that registers the same executable as a browser must not turn a
    ``allow_reuse=False`` alias back into a reuse.
    """

    backend, provider, _ = multi_process_fixture(allow_reuse=False)
    browser_pid = backend.add_process(7100, CHROME_EXE)
    backend.add_window("hwnd-existing-chrome", browser_pid)

    with _emulate_browser_host():
        with pytest.raises(ComputerAdapterError) as failure:
            await provider.launch_application("chrome", timeout_seconds=2)

    assert failure.value.code is ComputerFailureCode.ACTION_VERIFICATION_FAILED
    assert backend.launched_paths == [CHROME_EXE]
    assert "hwnd-existing-chrome" not in backend.focus_calls


@pytest.mark.asyncio
async def test_existing_visible_window_reuse_is_idempotent_and_freshly_confirmed():
    backend, provider, _ = multi_process_fixture()
    browser_pid = backend.add_process(7100, CHROME_EXE)
    backend.add_window("hwnd-existing-chrome", browser_pid)

    app = await provider.launch_application("chrome")

    assert backend.launched_paths == []
    assert backend.focus_calls == ["hwnd-existing-chrome"]
    assert app.process_id == browser_pid
    assert app.window_ids == ("hwnd-existing-chrome",)
    diagnostic = provider.launch_diagnostic
    assert diagnostic["mode"] == "reuse"
    assert diagnostic["focus_verified"] is True


@pytest.mark.asyncio
async def test_unfocusable_existing_window_is_reused_with_unverified_focus():
    """A window that is open but cannot be focused is reused, not duplicated.

    The focus call reports success while Windows never changes the foreground
    window. The fresh observation therefore shows a visible, ownership-verified
    window that is *not* foreground: reuse succeeds, and the report says focus was
    not verified instead of claiming it.
    """
    backend, provider, _ = multi_process_fixture()
    browser_pid = backend.add_process(7100, CHROME_EXE)
    backend.add_window("hwnd-existing-chrome", browser_pid)
    # The focus call reports success but Windows never changes the foreground window.
    backend.focus_reports_success_without_change = True

    app = await provider.launch_application("chrome")

    assert backend.launched_paths == []
    assert app.process_id == browser_pid
    assert app.window_ids == ("hwnd-existing-chrome",)
    diagnostic = provider.launch_diagnostic
    assert diagnostic["mode"] == "reuse"
    assert diagnostic["focus_verified"] is False


@pytest.mark.asyncio
async def test_minimized_window_that_cannot_be_restored_is_not_reused():
    """A window ARISE cannot bring back is not a usable existing instance."""

    backend, provider, _ = multi_process_fixture()
    browser_pid = backend.add_process(7100, CHROME_EXE)
    existing = backend.add_window("hwnd-existing-chrome", browser_pid, minimized=True)
    backend.focus_reports_success_without_change = True

    def on_launch(launcher_pid: int) -> None:
        del launcher_pid
        backend.windows = [replace(existing, minimized=False, foreground=True)]

    backend.on_launch = on_launch
    app = await provider.launch_application("chrome")

    # The dispatch restored and activated the pre-existing window, which is a fresh,
    # independently observed transition.
    assert backend.launched_paths == [CHROME_EXE]
    assert app.window_ids == ("hwnd-existing-chrome",)
    assert provider.launch_diagnostic["window_evidence"] == "activated_existing_window"


@pytest.mark.asyncio
@pytest.mark.parametrize("focus_mode", ["raises", "claims_success_without_change"])
async def test_runtime_completes_open_chrome_when_chrome_reuses_its_browser_process(focus_mode):
    """Regression for the reported task-level failure.

    A background sidecar cannot change the foreground window, so the native focus call
    either raises or returns a record Windows did not honor. The previous provider
    propagated the first form as UNKNOWN ("The tool failed after dispatch
    (WINDOW_NOT_FOUND)") and accepted the second as a false success without any
    activation. With a bounded baseline and a fresh observation the already open,
    ownership-verified window is reused, the action completes, and focus is reported
    as unverified rather than assumed.
    """
    backend, provider, _ = multi_process_fixture()
    browser_pid = backend.add_process(7100, CHROME_EXE)
    backend.add_window("hwnd-existing-chrome", browser_pid)
    if focus_mode == "raises":
        backend.focus_error = ComputerAdapterError(
            ComputerFailureCode.FOCUS_FAILED,
            "Foreground change refused.",
            source=PerceptionSource.UI_AUTOMATION,
        )
    else:
        backend.focus_reports_success_without_change = True

    tasks = InMemoryTaskRepository()
    task = TaskRecord.planned("Open Chrome", authorization=authority())
    tasks.save(task)
    tools = ToolRegistry()
    register_app_launch_tools(tools, provider)
    policy = PolicyEngine()
    environment = CompositeEnvironment(app_launch=provider)
    runtime = AgentRuntime(
        tasks=tasks,
        events=InMemoryEventStore(),
        tools=tools,
        policy=policy,
        environment=environment,
        resources=ResourceManager(),
        verifier=CompositeVerifier(environment, app_launch=provider),
    )
    action = ActionContract(
        task_id=task.task_id,
        target=TargetIdentity(platform="windows", application="Chrome"),
        tool_name="system.app_launch",
        risk=RiskLevel.R1,
        authority=task.authorization,
        parameters={"application": "Chrome"},
    )

    result = await runtime.execute_action(action, final_action=True)

    assert result.step_status is StepStatus.SUCCEEDED
    assert result.verification.status is VerificationStatus.PASSED
    assert result.task_status is TaskStatus.COMPLETED
    # No duplicate instance: the already open window satisfies "open Chrome".
    assert backend.launched_paths == []
    assert provider.launch_diagnostic["focus_verified"] is False


@pytest.mark.asyncio
async def test_no_visible_window_fails_launch_and_rejects_process_only_success():
    backend, provider, action = multi_process_fixture(keep_launcher_alive=True)

    with pytest.raises(ComputerAdapterError) as failure:
        await provider.launch_application("chrome", timeout_seconds=2)
    assert failure.value.code is ComputerFailureCode.WINDOW_NOT_FOUND
    assert backend.launched_paths == [CHROME_EXE]

    observation = await provider.observe(action)
    assert observation.facts["process.running"] is True
    assert observation.facts["window.open"] is False
    verification = await provider.verify(action)
    assert verification.status is VerificationStatus.FAILED


@pytest.mark.asyncio
async def test_focus_failure_does_not_hide_a_newly_created_window():
    backend, provider, _ = multi_process_fixture(keep_launcher_alive=True)
    backend.focus_error = ComputerAdapterError(
        ComputerFailureCode.WINDOW_NOT_FOUND,
        "Foreground change refused.",
        source=PerceptionSource.UI_AUTOMATION,
    )

    def on_launch(launcher_pid: int) -> None:
        # The launch itself created a new visible window; only the focus attempt failed.
        backend.add_window("hwnd-new-browser", launcher_pid)

    backend.on_launch = on_launch
    app = await provider.launch_application("chrome")

    assert app.window_ids == ("hwnd-new-browser",)
    diagnostic = provider.launch_diagnostic
    # The failure is surfaced; no foreground claim is made on its behalf.
    assert diagnostic["focus_failed"] is True
    assert diagnostic["focus_error"] == ComputerFailureCode.WINDOW_NOT_FOUND.value
    assert diagnostic["focus_verified"] is False


@pytest.mark.asyncio
async def test_reuse_focus_failure_without_fresh_evidence_fails_closed():
    """Only an *unusable* window falls through to dispatch, which still fails closed.

    The pre-existing window is minimized and activation is refused, so reuse cannot
    claim an open application. The dispatch produces no visible window either, and
    the launch fails closed instead of reporting the unusable window as success.
    """
    backend, provider, _ = multi_process_fixture(keep_launcher_alive=True)
    browser_pid = backend.add_process(7100, CHROME_EXE)
    backend.add_window("hwnd-existing-chrome", browser_pid, minimized=True)
    backend.focus_error = ComputerAdapterError(
        ComputerFailureCode.FOCUS_FAILED,
        "Foreground change refused.",
        source=PerceptionSource.UI_AUTOMATION,
    )

    with pytest.raises(ComputerAdapterError) as failure:
        await provider.launch_application("chrome", timeout_seconds=2)

    assert failure.value.code is ComputerFailureCode.WINDOW_NOT_FOUND
    assert provider.launch_diagnostic["mode"] == "spawn"
    assert provider.launch_diagnostic["focus_error"] == ComputerFailureCode.FOCUS_FAILED.value


@pytest.mark.asyncio
async def test_window_that_disappears_before_confirmation_fails_closed():
    backend, provider, _ = multi_process_fixture(keep_launcher_alive=True)

    def on_launch(launcher_pid: int) -> None:
        backend.add_window("hwnd-transient", launcher_pid)

    async def focus_then_close(window_id: str) -> WindowRecord:
        del window_id
        backend.windows = []
        raise ComputerAdapterError(ComputerFailureCode.WINDOW_NOT_FOUND, "Window closed.")

    backend.on_launch = on_launch
    backend.focus_window = focus_then_close  # type: ignore[method-assign]
    with pytest.raises(ComputerAdapterError) as failure:
        await provider.launch_application("chrome", timeout_seconds=2)

    assert failure.value.code is ComputerFailureCode.ACTION_VERIFICATION_FAILED
    assert provider.launch_diagnostic["confirmation_failed"] is True


@pytest.mark.asyncio
async def test_window_selection_is_deterministic_when_several_windows_appear():
    backend, provider, _ = multi_process_fixture(keep_launcher_alive=True)
    browser_pid = backend.add_process(7005, CHROME_EXE)

    def on_launch(launcher_pid: int) -> None:
        del launcher_pid
        backend.add_window("hwnd-small-helper", browser_pid, foreground=False)
        backend.add_window("hwnd-large-foreground", browser_pid, foreground=True)

    backend.on_launch = on_launch
    app = await provider.launch_application("chrome")

    assert app.window_ids == ("hwnd-large-foreground",)
    assert backend.focus_calls == ["hwnd-large-foreground"]


def test_production_composition_selects_shared_native_backend(tmp_path):
    services = create_app(
        AppSettings(
            database=DatabaseSettings(path=tmp_path / "composition.sqlite3"),
            desktop=DesktopSettings(enabled=True),
            security=SecuritySettings(environment="development", require_api_auth=False),
        )
    ).state.services
    try:
        launch = services.tools.get("system.app_launch")
        assert isinstance(launch.provider.backend, Win32AppLaunchBackend)
        assert launch.provider.backend.requires_visible_window
        native = services.uia_provider._backend
        assert isinstance(native, Win32UiaBackend)
        assert launch.provider.backend._uia_backend is native
        for operation in ("invoke", "click", "fill", "fill_secret", "focus", "press"):
            tool = services.tools.get(f"uia.{operation}")
            assert tool.provider is services.uia_provider
        assert services.engine.runtime.environment.uia is services.uia_provider
        assert services.engine.runtime.verifier.app_launch is launch.provider
    finally:
        services.database.close()


def test_native_spawn_is_shell_false():
    backend = Win32AppLaunchBackend()
    with (
        patch("arise.adapters.windows_app_launch.sys.platform", "win32"),
        patch("arise.adapters.windows_app_launch.os.path.isfile", return_value=True),
        patch("arise.adapters.windows_app_launch.subprocess.Popen") as popen,
    ):
        popen.return_value.pid = 10
        assert backend._sync_launch_process("chrome.exe") == 10
        assert popen.call_args.kwargs["shell"] is False
        assert popen.call_args.args == (["chrome.exe"],)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value,diagnostic",
    [
        ("action_id", "contains spaces", "action_id must be a bounded identifier"),
        ("required_resources", ["desktop mouse"], "resource name must be a bounded identifier"),
        ("idempotency_key", " ", "idempotency_key cannot be blank"),
        (
            "postconditions",
            [{"key": "window.open", "operator": "exists", "expected": True}],
            "postconditions.0: EXISTS conditions do not take an expected value",
        ),
    ],
)
async def test_planner_domain_rejection_is_bounded_and_field_specific(field, value, diagnostic):
    payload = json.loads(json.dumps(EXECUTABLE_PLAN))
    payload["steps"][0]["action"][field] = value
    router = ModelRouter()
    provider = ScriptedProvider([json.dumps(payload)] * 2)
    router.register(provider)
    tools = ToolRegistry()
    tools.register(SetFactTool(InMemoryEnvironment({})))
    planner = GatewayTaskPlanner(router, tools)
    task = TaskRecord.new("test", authorization=authority())
    try:
        with pytest.raises(InvalidPlan) as failure:
            await planner.create_plan(UserRequest(text="test"), task)
        assert diagnostic in failure.value.detail
        assert "steps.0.action" in failure.value.detail
        assert not planner.last_diagnostic().accepted
        assert planner.last_diagnostic().attempts == 2
    finally:
        await router.close()


def uia_fixture():
    backend = FakeUiaBackend()
    provider = WindowsUiaProvider(backend=backend, secret_provider=MemorySecretProvider())
    tools = ToolRegistry()
    register_windows_uia_tools(tools, provider)
    tasks = InMemoryTaskRepository()
    task = TaskRecord.new("click Save", authorization=authority())
    task.transition_to(TaskStatus.UNDERSTANDING)
    task.transition_to(TaskStatus.PLANNING)
    task.transition_to(TaskStatus.READY)
    tasks.save(task)
    policy = PolicyEngine()
    environment = CompositeEnvironment(uia=provider)
    runtime = AgentRuntime(
        tasks=tasks,
        events=InMemoryEventStore(),
        tools=tools,
        policy=policy,
        environment=environment,
        resources=ResourceManager(),
        verifier=CompositeVerifier(environment, uia=provider),
    )
    action = ActionContract(
        task_id=task.task_id,
        tool_name="uia.click",
        risk=RiskLevel.R3,
        authority=task.authorization,
        target=TargetIdentity(
            platform="windows",
            application="settings.exe",
            semantic_name="Save Changes",
            role="button",
        ),
        postconditions=(Condition("window.focused_element", expected="Save Changes"),),
    )
    return backend, provider, runtime, action


@pytest.mark.asyncio
async def test_semantic_grounding_resources_approval_dispatch_and_failed_focus_verification():
    backend, provider, runtime, action = uia_fixture()
    task = runtime.tasks.get(action.task_id)
    tool = runtime.tools.get(action.tool_name)
    # TaskEngine pre-registers planned steps with the original proposal fingerprint.
    task.add_step(
        ActionStep(
            action.action_id,
            runtime.policy.contract_fingerprint(action, tool.spec),
            action.tool_name,
            RiskLevel.R3,
        )
    )
    runtime.tasks.save(task)
    result = await runtime.execute_action(action)
    assert result.policy_decision.kind is PolicyDecisionKind.CONFIRM
    assert backend.invoked_nodes == []
    bound = result.bound_action
    assert bound.target.window_id == "hwnd-1001"
    assert tool.resources_for(bound) == (
        "desktop.window.hwnd-1001",
        "desktop.focus",
        "desktop.input",
    )
    grant = runtime.policy.issue_approval(bound, tool.spec, approved_by="user")
    result = await runtime.execute_action(bound, approval=grant, final_action=True)
    assert backend.invoked_nodes == ["node-save"]
    # This fake invoke does NOT change observed focus: dispatch cannot prove success.
    assert result.verification.status is VerificationStatus.FAILED
    assert result.step_status is StepStatus.FAILED


@pytest.mark.asyncio
async def test_unavailable_native_backend_is_blocked_not_missing_resource():
    _, provider, runtime, action = uia_fixture()
    provider._backend = Win32UiaBackend()
    with patch("arise.adapters.windows_uia.sys.platform", "linux"):
        result = await runtime.execute_action(action)
    assert result.step_status is StepStatus.BLOCKED
    assert "uia_not_available" in runtime.tasks.get(action.task_id).steps[0].status_reason.lower()


@pytest.mark.asyncio
async def test_engine_confirmation_binds_observed_identity_not_semantic_proposal():
    _, _, runtime, action = uia_fixture()
    engine = TaskEngine(
        runtime=runtime,
        tasks=runtime.tasks,
        events=runtime.events,
        tools=runtime.tools,
        policy=runtime.policy,
    )
    proposal = ActionProposal(tool_name="uia.click", risk=RiskLevel.R3, action_id=action.action_id)
    step = PlanStep(title="Click", action=proposal)
    engine._plans[action.task_id] = (
        TaskPlan(task_id=action.task_id, goal="click", steps=(step,), planner_id="fake"),
        [(step, action)],
    )
    result = await runtime.execute_action(action)
    engine._register_confirmation(action.task_id, action, result)
    assert engine._actions[(action.task_id, action.action_id)] == result.bound_action
    assert engine._plans[action.task_id][1][0][1] == result.bound_action


@pytest.mark.asyncio
async def test_wrong_application_and_missing_control_never_dispatch():
    backend, _, runtime, action = uia_fixture()
    action = replace(action, target=replace(action.target, application="chrome"))
    result = await runtime.execute_action(action)
    assert result.step_status is StepStatus.BLOCKED
    assert backend.invoked_nodes == []


@pytest.mark.asyncio
async def test_unexposed_control_reports_hwnd_observation_limitation():
    backend, _, runtime, action = uia_fixture()
    action = replace(action, target=replace(action.target, semantic_name="Address bar"))
    result = await runtime.execute_action(action)
    assert result.step_status is StepStatus.BLOCKED
    assert "Win32 HWND tree" in runtime.tasks.get(action.task_id).steps[0].status_reason
    assert backend.invoked_nodes == []


@pytest.mark.asyncio
async def test_ambiguous_windows_are_not_silently_selected():
    backend, _, runtime, action = uia_fixture()
    backend.windows.append(replace(backend.windows[0], window_id="hwnd-999"))
    result = await runtime.execute_action(action)
    assert result.step_status is StepStatus.BLOCKED
    assert "target_ambiguous" in runtime.tasks.get(action.task_id).steps[0].status_reason.lower()
    assert backend.invoked_nodes == []


@pytest.mark.asyncio
async def test_changed_grounded_target_cannot_reuse_approval():
    backend, _, runtime, action = uia_fixture()
    result = await runtime.execute_action(action)
    bound = result.bound_action
    grant = runtime.policy.issue_approval(
        bound, runtime.tools.get(action.tool_name).spec, approved_by="user"
    )
    # Original target disappears and a same-name replacement acquires a different ID.
    backend.nodes[0] = replace(backend.nodes[0], automation_id="replacement")
    result = await runtime.execute_action(bound, approval=grant)
    assert result.step_status is StepStatus.BLOCKED
    assert backend.invoked_nodes == []


def test_domain_diagnostic_does_not_echo_malicious_json_keys():
    from arise.core.contracts import ContractValidationError

    proposal = ActionProposal(
        tool_name="uia.fill",
        risk=RiskLevel.R2,
        parameters={"key contains SECRET-MARKER": float("nan")},
    )
    with pytest.raises(ContractValidationError) as failure:
        proposal.to_domain(task_id="task", authority=authority())
    assert "SECRET-MARKER" not in failure.value.diagnostic
    assert "non-finite" in failure.value.diagnostic


@pytest.mark.asyncio
async def test_native_backend_without_visible_window_times_out_instead_of_succeeding():
    backend, provider, _ = launch_fixture()
    with pytest.raises(ComputerAdapterError) as failure:
        await provider.launch_application("chrome", timeout_seconds=2)
    assert failure.value.code is ComputerFailureCode.WINDOW_NOT_FOUND
    assert len(backend.launched_paths) == 1


@pytest.mark.asyncio
async def test_native_window_observation_failure_is_not_silently_empty():
    backend = Win32AppLaunchBackend()
    with pytest.raises(ComputerAdapterError) as failure:
        await backend.list_windows()
    assert failure.value.code is ComputerFailureCode.UIA_NOT_AVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["fallback", "condition"])
async def test_planner_validates_fallbacks_and_step_conditions_through_domain(location):
    payload = json.loads(json.dumps(EXECUTABLE_PLAN))
    bad = {"key": "window.open", "operator": "exists", "expected": True}
    if location == "condition":
        payload["steps"][0]["condition"] = bad
        expected_path = "steps.0.condition"
    else:
        fallback = dict(payload["steps"][0]["action"], postconditions=[bad])
        payload["steps"][0]["fallback_policy"] = {
            "strategy": "fallback_action",
            "fallback_action": fallback,
        }
        expected_path = "steps.0.fallback_policy.fallback_action"
    planner = GatewayTaskPlanner(ModelRouter(), ToolRegistry())
    task = TaskRecord.new("test", authorization=authority())
    with pytest.raises(InvalidPlan) as failure:
        planner._validate_response(
            json.dumps(payload), task, raw_content="", provider_id="fake", model_id="fake"
        )
    assert expected_path in failure.value.detail
    assert "EXISTS conditions do not take an expected value" in failure.value.detail


def test_live_script_never_labels_backend_completion_as_real_verification(capsys):
    import runpy
    from pathlib import Path

    script = runpy.run_path(str(Path(__file__).parents[1] / "scripts/live_nvidia_planner_check.py"))
    code = script["_print_outcome"]({"state": "completed"}, {"accepted": True}, ())
    output = capsys.readouterr().out
    assert code == 0
    assert "BACKEND REPORTS COMPLETED" in output
    assert "EXECUTION VERIFIED" not in output
    assert "waiting_user" in script["TERMINAL_STATES"]
