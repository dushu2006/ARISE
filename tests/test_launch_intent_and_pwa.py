"""Pre-merge review regressions for the launch-intent flow and generic
browser-installed application (PWA) support.

Everything here is fake-backed: no Windows shell, COM, registry, UIA provider or
real browser is exercised. These tests prove the *logic* only; the real-Windows
behaviour they describe is listed as pending verification in
``docs/coherence-milestone-implementation.md``.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any

from arise.adapters.windows_app_discovery import (
    ActivationMethod,
    ApplicationDescriptor,
    normalize_application_name,
)
from arise.adapters.windows_app_launch import (
    DesktopSnapshot,
    ObservedProcess,
    Win32AppLaunchBackend,
    WindowsAppLaunchProvider,
    WindowsApplicationResolver,
    _bounded_name_match,
    _web_app_title_matches,
)
from arise.core.computer import ComputerFailureCode, FocusStrategy, WindowRecord
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import ActionContract, AuthorizationContext, RiskLevel


def _touch(path: Any) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"mock executable or shortcut")
    return path


class _StaticCatalog:
    def __init__(self, descriptors: tuple[ApplicationDescriptor, ...]) -> None:
        self.descriptors = descriptors

    def discover(self) -> tuple[ApplicationDescriptor, ...]:
        return self.descriptors


class _ShortcutBackend:
    """Backend that behaves like the real one: arguments require a direct launch."""

    def __init__(self, *, executable_path: str | None = None) -> None:
        self.executable_path = executable_path
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def activate_application(
        self, application: ApplicationDescriptor, *, arguments: Any = ()
    ) -> int | None:
        if arguments:
            if application.is_web_app:
                raise ComputerAdapterError(
                    ComputerFailureCode.LAUNCH_MODE_UNSUPPORTED,
                    "A browser-installed application is launched by its own link.",
                )
            if not application.executable_path:
                raise ComputerAdapterError(
                    ComputerFailureCode.LAUNCH_MODE_UNSUPPORTED,
                    "No executable can carry this launch mode.",
                )
            self.calls.append((application.executable_path, tuple(arguments)))
            return 4242
        self.calls.append((application.shortcut_path or application.executable_path or "", ()))
        return 4242


# ---------------------------------------------------------------------------
# 2. Launch-intent semantics survive every layer
# ---------------------------------------------------------------------------


class LaunchIntentFlowTests(unittest.IsolatedAsyncioTestCase):
    """The semantic launch intent must survive to the dispatch call unchanged."""

    def _backend(self, *, launched: list[tuple[str, tuple[str, ...]]]) -> Win32AppLaunchBackend:
        backend = Win32AppLaunchBackend()

        async def _launch(executable_path: str, arguments: Any = ()) -> int:
            launched.append((executable_path, tuple(arguments)))
            return 5555

        backend.launch_process = _launch  # type: ignore[method-assign]
        return backend

    async def test_advertised_switch_is_dispatched_to_the_executable(self) -> None:
        launched: list[tuple[str, tuple[str, ...]]] = []
        backend = self._backend(launched=launched)
        descriptor = ApplicationDescriptor(
            name="Google Chrome",
            executable_path=r"C:\Browsers\chrome.exe",
            process_names=("chrome.exe",),
            activation_method=ActivationMethod.START_MENU_SHORTCUT,
            shortcut_path=r"C:\Start Menu\Google Chrome.lnk",
            new_window_supported=True,
            new_window_arguments=("--new-window",),
        )
        pid = await backend.activate_application(descriptor, arguments=("--new-window",))
        self.assertEqual(pid, 5555)
        # A Start Menu link cannot carry switches, so the executable is launched
        # directly with exactly the advertised, validated switch.
        self.assertEqual(launched, [(r"C:\Browsers\chrome.exe", ("--new-window",))])

    async def test_browser_installed_app_never_receives_a_browser_switch(self) -> None:
        launched: list[tuple[str, tuple[str, ...]]] = []
        backend = self._backend(launched=launched)
        descriptor = ApplicationDescriptor(
            name="WhatsApp",
            executable_path=r"C:\Browsers\msedge_proxy.exe",
            process_names=("msedge_proxy.exe",),
            activation_method=ActivationMethod.START_MENU_SHORTCUT,
            shortcut_path=r"C:\Start Menu\WhatsApp.lnk",
            is_web_app=True,
        )
        with self.assertRaises(ComputerAdapterError) as caught:
            await backend.activate_application(descriptor, arguments=("--new-window",))
        self.assertIs(caught.exception.code, ComputerFailureCode.LAUNCH_MODE_UNSUPPORTED)
        self.assertEqual(launched, [])

    async def test_launch_mode_without_an_executable_fails_closed(self) -> None:
        launched: list[tuple[str, tuple[str, ...]]] = []
        backend = self._backend(launched=launched)
        descriptor = ApplicationDescriptor(
            name="Store App",
            activation_method=ActivationMethod.PACKAGED_AUMID,
            aumid="Vendor.App_abc!App",
            package_family_name="Vendor.App_abc",
        )
        with self.assertRaises(ComputerAdapterError) as caught:
            await backend.activate_application(descriptor, arguments=("--new-window",))
        self.assertIs(caught.exception.code, ComputerFailureCode.LAUNCH_MODE_UNSUPPORTED)
        self.assertEqual(launched, [])

    async def test_unsupported_launch_intent_never_dispatches(self) -> None:
        class _Backend:
            requires_visible_window = True
            dispatched: list[Any] = []

            async def list_running_processes(self) -> list[dict[str, Any]]:
                return []

            async def list_windows(self) -> list[WindowRecord]:
                return []

            async def activate_application(self, application: Any, *, arguments: Any = ()) -> int:
                _Backend.dispatched.append((application.name, tuple(arguments)))
                return 1

        descriptor = ApplicationDescriptor(
            name="Plain App",
            executable_path=r"C:\Apps\plain.exe",
            activation_method=ActivationMethod.EXECUTABLE,
        )
        provider = WindowsAppLaunchProvider(backend=_Backend())
        provider._resolver = WindowsApplicationResolver(catalog=_StaticCatalog((descriptor,)))
        for intent in ("force_new_window", "force_new_instance"):
            with self.subTest(intent=intent), self.assertRaises(ComputerAdapterError) as caught:
                await provider.launch_application(
                    "Plain App", timeout_seconds=2.0, launch_intent=intent
                )
            self.assertIs(caught.exception.code, ComputerFailureCode.LAUNCH_MODE_UNSUPPORTED)
        self.assertEqual(_Backend.dispatched, [])


# ---------------------------------------------------------------------------
# 6. Browser-installed applications (PWAs) are discovered generically
# ---------------------------------------------------------------------------


class BrowserInstalledAppDiscoveryTests(unittest.TestCase):
    def test_shortcut_arguments_are_discovered_as_a_distinct_entry_point(self) -> None:
        descriptor = ApplicationDescriptor(
            name="Mail",
            executable_path=r"C:\Browsers\msedge.exe",
            process_names=("msedge.exe",),
            activation_method=ActivationMethod.START_MENU_SHORTCUT,
            shortcut_path=r"C:\Start Menu\Mail.lnk",
            is_web_app=True,
            launch_arguments=("--profile-directory=Default", "--app-id=mail-pwa"),
        )
        self.assertTrue(descriptor.is_web_app)
        self.assertEqual(
            descriptor.launch_arguments,
            ("--profile-directory=Default", "--app-id=mail-pwa"),
        )
        self.assertTrue(descriptor.canonical_identity.startswith("webapp:"))

    def test_two_entry_points_of_one_executable_stay_distinct(self) -> None:
        browser = ApplicationDescriptor(
            name="Microsoft Edge",
            executable_path=r"C:\Browsers\msedge.exe",
            process_names=("msedge.exe",),
            activation_method=ActivationMethod.START_MENU_SHORTCUT,
            shortcut_path=r"C:\Start Menu\Microsoft Edge.lnk",
        )
        installed_app = ApplicationDescriptor(
            name="Mail",
            executable_path=r"C:\Browsers\msedge.exe",
            process_names=("msedge.exe",),
            activation_method=ActivationMethod.START_MENU_SHORTCUT,
            shortcut_path=r"C:\Start Menu\Mail.lnk",
            launch_arguments=("--app-id=mail-pwa",),
        )
        self.assertNotEqual(browser.canonical_identity, installed_app.canonical_identity)
        self.assertTrue(browser.canonical_identity.startswith("executable:"))
        self.assertTrue(installed_app.canonical_identity.startswith("shortcut-app:"))

    def test_the_same_entry_point_discovered_in_two_roots_deduplicates(self) -> None:
        first = ApplicationDescriptor(
            name="Mail",
            executable_path=r"C:\Browsers\msedge.exe",
            process_names=("msedge.exe",),
            activation_method=ActivationMethod.START_MENU_SHORTCUT,
            shortcut_path=r"C:\User Start Menu\Mail.lnk",
            launch_arguments=("--app-id=mail-pwa",),
        )
        second = replace(first, shortcut_path=r"C:\Common Start Menu\Mail.lnk")
        self.assertEqual(first.canonical_identity, second.canonical_identity)

    def test_web_app_window_verification_tolerates_the_sites_own_title(self) -> None:
        provider = WindowsAppLaunchProvider()
        web_app = ApplicationDescriptor(
            name="WhatsApp",
            executable_path=r"C:\Browsers\msedge_proxy.exe",
            process_names=("msedge_proxy.exe",),
            activation_method=ActivationMethod.START_MENU_SHORTCUT,
            shortcut_path=r"C:\Start Menu\WhatsApp.lnk",
            is_web_app=True,
        )
        window = WindowRecord(
            window_id="hwnd-pwa",
            process_id=77,
            title="WhatsApp",
            application="msedge_proxy.exe",
            visible=True,
            minimized=False,
            maximized=False,
            foreground=True,
            executable_path=r"C:\Browsers\msedge_proxy.exe",
        )
        snapshot = DesktopSnapshot(
            processes={
                77: ObservedProcess(77, "msedge_proxy.exe", r"C:\Browsers\msedge_proxy.exe")
            },
            windows={window.window_id: window},
            foreground_window_id=window.window_id,
        )
        # The site's own title, which is what a PWA window actually shows.
        dynamic = replace(window, title="WhatsApp Web")
        dynamic_snapshot = replace(
            snapshot, windows={dynamic.window_id: dynamic}, foreground_window_id=dynamic.window_id
        )
        self.assertTrue(provider._window_owner_verified(window, snapshot, web_app))
        self.assertTrue(provider._window_owner_verified(dynamic, dynamic_snapshot, web_app))

        # A different application in the same browser is still rejected.
        other = replace(window, title="Calendar")
        other_snapshot = replace(
            snapshot, windows={other.window_id: other}, foreground_window_id=other.window_id
        )
        self.assertFalse(provider._window_owner_verified(other, other_snapshot, web_app))

    def test_bounded_name_matching_does_not_become_fuzzy_search(self) -> None:
        self.assertTrue(_bounded_name_match("whatsappweb", "whatsapp"))
        self.assertTrue(_bounded_name_match("whatsapp", "whatsappweb"))
        self.assertFalse(_bounded_name_match("word", "wordpad"))
        self.assertFalse(_bounded_name_match("go", "googlechrome"))
        self.assertFalse(_bounded_name_match("excel", "excel"))
        self.assertFalse(_bounded_name_match("", "anything"))

    def test_web_app_title_matching_requires_a_bounded_match(self) -> None:
        self.assertTrue(_web_app_title_matches("WhatsApp Web", "whatsapp"))
        self.assertTrue(_web_app_title_matches("WhatsApp", "whatsapp"))
        self.assertFalse(_web_app_title_matches("Calendar", "whatsapp"))
        self.assertFalse(_web_app_title_matches("", "whatsapp"))

    def test_resolver_matches_a_shorter_installed_label(self) -> None:
        descriptor = ApplicationDescriptor(
            name="WhatsApp",
            executable_path=r"C:\Browsers\msedge_proxy.exe",
            process_names=("msedge_proxy.exe",),
            activation_method=ActivationMethod.START_MENU_SHORTCUT,
            shortcut_path=r"C:\Start Menu\WhatsApp.lnk",
            is_web_app=True,
        )
        resolver = WindowsApplicationResolver(
            catalog=_StaticCatalog((descriptor,)), cache_ttl_seconds=0.0
        )
        resolved = resolver.resolve("WhatsApp Web")
        self.assertEqual(resolved.name, "WhatsApp")
        candidates = resolver.last_diagnostic.get("candidates") or []
        self.assertTrue(
            any(item.get("matched_by") == "catalog_name_containment" for item in candidates)
        )

    def test_two_equally_plausible_containment_matches_stay_ambiguous(self) -> None:
        first = ApplicationDescriptor(
            name="WhatsApp Web",
            executable_path=r"C:\Browsers\a.exe",
            activation_method=ActivationMethod.EXECUTABLE,
        )
        second = ApplicationDescriptor(
            name="WhatsApp Business",
            executable_path=r"C:\Browsers\b.exe",
            activation_method=ActivationMethod.EXECUTABLE,
        )
        resolver = WindowsApplicationResolver(
            catalog=_StaticCatalog((first, second)), cache_ttl_seconds=0.0
        )
        with self.assertRaises(ComputerAdapterError) as caught:
            resolver.resolve("WhatsApp")
        self.assertIs(caught.exception.code, ComputerFailureCode.APPLICATION_AMBIGUOUS)

    def test_name_normalization_is_unchanged(self) -> None:
        self.assertEqual(normalize_application_name(" WhatsApp.exe "), "whatsapp")


class LaunchObservationFactTests(unittest.IsolatedAsyncioTestCase):
    """Launch observation must separate visibility, foreground and process state.

    The bounded desktop snapshot carries *visible* windows only, so a minimized
    window is deliberately not observable here: reporting it would invent a fact.
    That limitation is documented rather than approximated.
    """

    class _Backend:
        requires_visible_window = True

        def __init__(self, windows: list[WindowRecord]) -> None:
            self.windows = windows

        async def list_running_processes(self) -> list[dict[str, Any]]:
            return [{"pid": 4242, "name": "chrome.exe", "exe": r"C:\Browsers\chrome.exe"}]

        async def list_windows(self) -> list[WindowRecord]:
            return list(self.windows)

    def _window(self, *, minimized: bool, foreground: bool) -> WindowRecord:
        return WindowRecord(
            window_id="hwnd-1",
            process_id=4242,
            title="Google Chrome",
            application="chrome.exe",
            visible=not minimized,
            minimized=minimized,
            maximized=False,
            foreground=foreground,
            executable_path=r"C:\Browsers\chrome.exe",
        )

    async def _observe(self, window: WindowRecord) -> dict[str, Any]:
        descriptor = ApplicationDescriptor(
            name="Google Chrome",
            executable_path=r"C:\Browsers\chrome.exe",
            process_names=("chrome.exe",),
            activation_method=ActivationMethod.EXECUTABLE,
        )
        provider = WindowsAppLaunchProvider(backend=self._Backend([window]))
        provider._resolver = WindowsApplicationResolver(catalog=_StaticCatalog((descriptor,)))
        action = ActionContract(
            task_id="task-1",
            tool_name="system.app_launch",
            target=None,
            risk=RiskLevel.R0,
            authority=AuthorizationContext(principal_id="principal-1", user_intent_id="intent-1"),
            parameters={"application": "Google Chrome"},
        )
        lease = await provider.observe(action)
        return dict(lease.facts)

    async def test_a_visible_window_may_still_be_unfocused(self) -> None:
        facts = await self._observe(self._window(minimized=False, foreground=False))
        self.assertTrue(facts["window.open"])
        self.assertTrue(facts["window.visible"])
        self.assertFalse(facts["window.foreground"])
        self.assertFalse(facts["application.focused"])

    async def test_foreground_and_process_are_reported_separately(self) -> None:
        facts = await self._observe(self._window(minimized=False, foreground=True))
        self.assertTrue(facts["process.running"])
        self.assertTrue(facts["window.open"])
        self.assertTrue(facts["application.focused"])

    async def test_a_minimized_window_is_not_claimed_as_open(self) -> None:
        # The bounded snapshot carries visible windows only, so a minimized window
        # is reported as not open rather than being inferred from the process.
        facts = await self._observe(self._window(minimized=True, foreground=False))
        self.assertFalse(facts["window.open"])
        self.assertFalse(facts["window.foreground"])
        self.assertEqual(facts["window.count"], 0)


class FocusStrategyInputInjectionTests(unittest.TestCase):
    """The last activation strategy injects global input; it must be rate limited."""

    class _FakeUser32:
        def __init__(self) -> None:
            self.key_events = 0
            self.foreground_calls = 0

        def keybd_event(self, *args: Any) -> None:
            self.key_events += 1

        def SetForegroundWindow(self, hwnd: int) -> int:
            self.foreground_calls += 1
            return 1

        def GetForegroundWindow(self) -> int:
            return 0

        def GetWindowThreadProcessId(self, hwnd: int, pid: Any) -> int:
            return 0

        def BringWindowToTop(self, hwnd: int) -> int:
            return 1

    def test_the_alt_nudge_is_injected_at_most_once_per_interval(self) -> None:
        from arise.adapters import windows_uia

        user32 = self._FakeUser32()
        clock = {"now": 10_000.0}
        provider_cls = windows_uia.Win32UiaBackend

        provider_cls._apply_focus_strategy(
            user32, 4242, FocusStrategy.ALT_NUDGE, clock=lambda: clock["now"]
        )
        self.assertEqual(user32.key_events, 2)  # Alt down + Alt up
        self.assertEqual(user32.foreground_calls, 1)

        # A launch poll retries activation every fraction of a second; the second
        # and third attempts must not type Alt into the user's session again, yet
        # the ordinary activation request is still made.
        clock["now"] += 0.15
        provider_cls._apply_focus_strategy(
            user32, 4242, FocusStrategy.ALT_NUDGE, clock=lambda: clock["now"]
        )
        clock["now"] += 0.15
        provider_cls._apply_focus_strategy(
            user32, 4242, FocusStrategy.ALT_NUDGE, clock=lambda: clock["now"]
        )
        self.assertEqual(user32.key_events, 2)
        self.assertEqual(user32.foreground_calls, 3)

        # After the interval elapses the strategy may inject again.
        clock["now"] += windows_uia._ALT_NUDGE_MIN_INTERVAL_SECONDS
        provider_cls._apply_focus_strategy(
            user32, 4242, FocusStrategy.ALT_NUDGE, clock=lambda: clock["now"]
        )
        self.assertEqual(user32.key_events, 4)

    def test_ordinary_strategies_inject_nothing(self) -> None:
        from arise.adapters import windows_uia

        user32 = self._FakeUser32()
        for strategy in (FocusStrategy.SET_FOREGROUND, FocusStrategy.ATTACH_THREAD_INPUT):
            with self.subTest(strategy=strategy):
                windows_uia.Win32UiaBackend._apply_focus_strategy(user32, 4242, strategy)
        self.assertEqual(user32.key_events, 0)
        self.assertEqual(user32.foreground_calls, 2)


if __name__ == "__main__":
    unittest.main()
