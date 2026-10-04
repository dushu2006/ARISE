from __future__ import annotations

import unittest

from arise.adapters.browser_playwright import (
    _DOM_SNAPSHOT_SCRIPT,
    PlaywrightActionTool,
    PlaywrightBrowserProvider,
    redact_browser_url,
    register_playwright_tools,
    validate_browser_url,
)
from arise.adapters.secrets import MemorySecretProvider
from arise.core.computer import ComputerFailureCode
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import SecretRef
from arise.core.ports import ToolRegistry


class FakeLocator:
    def __init__(self) -> None:
        self.fill_value: str | None = None
        self.click_count = 0
        self.scroll_count = 0
        self.press_keys: list[str] = []

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def fill(self, value: str, *, timeout: int) -> None:
        del timeout
        self.fill_value = value

    async def click(self, *, timeout: int) -> None:
        del timeout
        self.click_count += 1

    async def press(self, key: str, *, timeout: int) -> None:
        del timeout
        self.press_keys.append(key)

    async def select_option(self, *, value: str, timeout: int) -> None:
        del value, timeout

    async def scroll_into_view_if_needed(self, *, timeout: int) -> None:
        del timeout
        self.scroll_count += 1


class FakePage:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.url = "https://example.test/login?access_token=do-not-store#private"
        self.locator_instance = FakeLocator()

    async def title(self) -> str:
        return "Sign in"

    async def evaluate(self, script: str, limit: int) -> list[dict[str, object]]:
        assert "input_type" in script
        assert "sensitive" in script
        return self.rows[:limit]

    def get_by_test_id(self, test_id: str) -> FakeLocator:
        assert test_id == "password-field"
        return self.locator_instance

    def get_by_role(self, role: str, *, name: str, exact: bool) -> FakeLocator:
        del role, name, exact
        return self.locator_instance

    def get_by_label(self, label: str, *, exact: bool) -> FakeLocator:
        del label, exact
        return self.locator_instance

    def get_by_placeholder(self, placeholder: str, *, exact: bool) -> FakeLocator:
        del placeholder, exact
        return self.locator_instance

    def get_by_text(self, text: str, *, exact: bool) -> FakeLocator:
        del text, exact
        return self.locator_instance

    def locator(self, selector: str) -> FakeLocator:
        del selector
        return self.locator_instance

    def is_closed(self) -> bool:
        return False


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]


class FakeRoute:
    def __init__(self) -> None:
        self.action: str | None = None

    async def continue_(self) -> None:
        self.action = "continue"

    async def abort(self, error_code: str) -> None:
        self.action = f"abort:{error_code}"


class FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class PlaywrightBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.row: dict[str, object] = {
            "role": "textbox",
            "name": "Password",
            "tag": "input",
            "id": "password-input",
            "test_id": "password-field",
            "label": "Password",
            "placeholder": "",
            "text": "",
            "input_type": "password",
            "sensitive": True,
            "contenteditable": False,
            "visible": True,
            "enabled": True,
            "checked": None,
            "selected": None,
            "hierarchy": ["Sign in"],
            "bounds": [20, 40, 180, 32],
        }
        self.page = FakePage([self.row])
        self.provider = PlaywrightBrowserProvider(
            secret_provider=MemorySecretProvider({"login.password": "test-secret-value"})
        )
        self.provider._context = FakeContext(self.page)
        self.page_id = self.provider._register_page(self.page)
        self.provider._default_page_id = self.page_id

    async def test_dom_snapshot_preserves_input_type_and_sensitive_metadata(self) -> None:
        candidates = await self.provider.inspect(self.page_id)
        self.assertIn("input_type", _DOM_SNAPSHOT_SCRIPT)
        self.assertIn("sensitive", _DOM_SNAPSHOT_SCRIPT)
        self.assertEqual(len(candidates), 1)
        identity = candidates[0].descriptor.identity
        self.assertEqual(identity.locator["input_type"], "password")
        self.assertIs(identity.locator["sensitive"], True)
        self.assertTrue(candidates[0].descriptor.visible)
        self.assertTrue(candidates[0].descriptor.enabled)
        self.assertNotIn("test-secret-value", repr(identity.to_dict()))

    async def test_plain_text_is_rejected_for_sensitive_fields(self) -> None:
        candidate = (await self.provider.inspect(self.page_id))[0]
        with self.assertRaises(ComputerAdapterError) as caught:
            await self.provider.fill(candidate, "not-a-secret-ref", timeout_seconds=1)
        self.assertIs(caught.exception.code, ComputerFailureCode.PERMISSION_DENIED)
        self.assertIsNone(self.page.locator_instance.fill_value)

    async def test_secret_reference_is_resolved_only_at_dispatch(self) -> None:
        candidate = (await self.provider.inspect(self.page_id))[0]
        result = await self.provider.fill(candidate, SecretRef("login.password"), timeout_seconds=1)
        self.assertEqual(self.page.locator_instance.fill_value, "test-secret-value")
        self.assertNotIn("test-secret-value", result)
        self.assertNotIn("test-secret-value", repr(candidate.descriptor.identity.to_dict()))

    async def test_tool_reports_sensitive_plain_text_as_pre_dispatch_failure(self) -> None:
        from arise.core.contracts import (
            ActionContract,
            AuthorizationContext,
            RiskLevel,
            TrustLevel,
        )
        from arise.core.ports import ExecutionStatus
        from arise.core.resources import ResourceManager

        candidate = (await self.provider.inspect(self.page_id))[0]
        authority = AuthorizationContext(
            principal_id="test-user",
            user_intent_id="intent-1",
            trust=TrustLevel.USER_INSTRUCTION,
            capabilities=frozenset({"browser.control"}),
        )
        action = ActionContract(
            task_id="browser-task",
            action_id="browser-fill-test",
            tool_name="browser.fill",
            target=candidate.descriptor.identity,
            risk=RiskLevel.R2,
            authority=authority,
            parameters={"text": "plaintext-must-not-be-entered"},
        )
        tool = PlaywrightActionTool(self.provider, "fill")
        observation = await self.provider.observe(action)
        resources = ResourceManager()
        async with resources.acquire_many(
            action.task_id, tool.resources_for(action), lease_seconds=2
        ) as resource_lease:
            outcome = await tool.execute(action, observation, resource_lease)
        self.assertIs(outcome.status, ExecutionStatus.FAILED)
        self.assertFalse(outcome.side_effect_may_have_occurred)
        self.assertIsNone(self.page.locator_instance.fill_value)

    async def test_dom_change_invalidates_observed_target_before_click(self) -> None:
        candidate = (await self.provider.inspect(self.page_id))[0]
        self.page.rows[0] = {**self.row, "name": "Different password control"}
        with self.assertRaises(ComputerAdapterError) as caught:
            await self.provider.click(candidate, timeout_seconds=1)
        self.assertIs(caught.exception.code, ComputerFailureCode.TARGET_STALE)
        self.assertEqual(self.page.locator_instance.click_count, 0)

    async def test_scroll_dispatches_only_after_fresh_target_validation(self) -> None:
        candidate = (await self.provider.inspect(self.page_id))[0]
        await self.provider.scroll_into_view(candidate, timeout_seconds=1)
        self.assertEqual(self.page.locator_instance.scroll_count, 1)
        self.page.rows[0] = {**self.row, "name": "New password control"}
        with self.assertRaises(ComputerAdapterError) as caught:
            await self.provider.scroll_into_view(candidate, timeout_seconds=1)
        self.assertIs(caught.exception.code, ComputerFailureCode.TARGET_STALE)
        self.assertEqual(self.page.locator_instance.scroll_count, 1)

    async def test_popup_pages_are_registered_as_scoped_tabs(self) -> None:
        popup = FakePage([self.row])
        self.provider._context.pages.append(popup)
        tabs = await self.provider.list_tabs()
        self.assertEqual(len(tabs), 2)
        popup_record = next(tab for tab in tabs if tab.page_id != self.page_id)
        self.assertEqual(popup_record.title, "Sign in")
        self.assertNotIn("private", popup_record.url)
        self.assertNotEqual(popup_record.page_id, self.page_id)

    def test_browser_url_policy_blocks_unsafe_schemes_and_private_hosts(self) -> None:
        self.assertEqual(
            validate_browser_url("https://example.com/account"), "https://example.com/account"
        )
        for url in (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://user:pass@example.com/",
            "http://127.0.0.1/",
            "http://0x7f000001/",
            "http://localhost/",
            "http://%31%32%37.0.0.1/",
            "https:\\\\example.com\\\\path",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_browser_url(url)
        self.assertEqual(
            validate_browser_url("http://127.0.0.1:8080/", allow_private_network=True),
            "http://127.0.0.1:8080/",
        )

    async def test_browser_route_guards_private_subrequests_and_redirect_targets(self) -> None:
        public = FakeRoute()
        await self.provider._guard_route(public, FakeRequest("https://example.com/asset.js"))
        self.assertEqual(public.action, "continue")
        private = FakeRoute()
        await self.provider._guard_route(private, FakeRequest("http://10.0.0.1/private"))
        self.assertEqual(private.action, "abort:blockedbyclient")
        inline_data = FakeRoute()
        await self.provider._guard_route(inline_data, FakeRequest("data:text/plain,ok"))
        self.assertEqual(inline_data.action, "continue")

    def test_url_redaction_drops_query_fragment_and_opaque_secret_paths(self) -> None:
        safe = redact_browser_url(
            "https://example.com/reset/password/opaque-value?access_token=very-secret#fragment"
        )
        self.assertEqual(safe, "https://example.com/reset/[REDACTED]/[REDACTED]")
        for secret in ("very-secret", "opaque-value", "access_token", "fragment"):
            self.assertNotIn(secret, safe)

    def test_action_tools_have_risk_floors_and_register_separately(self) -> None:
        registry = ToolRegistry()
        tools = register_playwright_tools(registry, self.provider)
        self.assertEqual(len(tools), 7)
        specs = {spec.name: spec for spec in registry.list_specs()}
        self.assertEqual(specs["browser.click"].minimum_risk, 3)
        self.assertEqual(specs["browser.fill"].minimum_risk, 2)
        self.assertEqual(specs["browser.fill_secret"].minimum_risk, 3)
        self.assertIn("browser.control", specs["browser.click"].required_capabilities)
        click_tool = registry.get("browser.click")
        action_target = self.provider.page_identity(self.page_id)
        action = type("Action", (), {"target": action_target})()
        self.assertEqual(click_tool.resources_for(action), (f"browser.page.{self.page_id}",))
        self.assertIsInstance(PlaywrightActionTool(self.provider, "scroll"), PlaywrightActionTool)

    async def test_stale_browser_target_regrounding_and_crash_navigation_recovery(self) -> None:
        submit_row = {
            **self.row,
            "role": "button",
            "name": "Submit",
            "tag": "button",
            "id": "submit-btn",
            "test_id": "submit-btn",
            "input_type": "",
            "sensitive": False,
        }
        self.page.rows.append(submit_row)
        candidates = await self.provider.inspect(self.page_id)
        submit_cand = next(c for c in candidates if c.descriptor.identity.semantic_name == "Submit")
        # Unrelated DOM row changes -> state_hash changes
        self.page.rows[0] = {**self.row, "placeholder": "Updated placeholder"}
        regrounded = await self.provider.reground_stale_target(submit_cand)
        self.assertEqual(
            regrounded.descriptor.identity.fingerprint,
            submit_cand.descriptor.identity.fingerprint,
        )
        self.assertEqual(self.provider.reground_count, 1)

        # Crash recovery restores a fresh page
        replacement_page = FakePage([submit_row])
        replacement_ctx = FakeContext(replacement_page)
        new_page_id = await self.provider.recover_after_crash(replacement_context=replacement_ctx)
        self.assertEqual(self.provider.crash_recovery_count, 1)
        self.assertTrue(new_page_id)

        # Navigation recovery clears stale leases and navigates to fallback
        async def fake_goto(url: str, *, wait_until: str, timeout: int) -> None:
            del wait_until, timeout
            replacement_page.url = url

        replacement_page.goto = fake_goto  # type: ignore[attr-defined]
        tab = await self.provider.recover_navigation(
            new_page_id, fallback_url="https://example.com/home"
        )
        self.assertEqual(self.provider.navigation_recovery_count, 1)
        self.assertEqual(tab.url, "https://example.com/home")

    async def test_dns_rebinding_and_websocket_egress_controls(self) -> None:
        from arise.adapters.browser_playwright import (
            validate_browser_egress_url,
            verify_browser_dns_binding,
        )

        # Plaintext ws:// is blocked on public network; wss:// to public host is allowed
        with self.assertRaises(ValueError):
            validate_browser_egress_url("ws://example.com/socket")
        self.assertEqual(
            validate_browser_egress_url(
                "wss://api.example.com/socket", allowed_domains=("example.com",)
            ),
            "wss://api.example.com/socket",
        )
        with self.assertRaises(ValueError):
            validate_browser_egress_url("wss://evil.test/socket", allowed_domains=("example.com",))

        # DNS rebinding from public IP to 127.0.0.1 is blocked
        pinned: dict[str, frozenset[str]] = {}
        ips = verify_browser_dns_binding(
            "https://rebind.example.com/",
            dns_resolver=lambda _host: ["93.184.216.34"],
            pinned_hosts=pinned,
        )
        self.assertEqual(ips, ("93.184.216.34",))
        with self.assertRaises(ValueError):
            verify_browser_dns_binding(
                "https://rebind.example.com/",
                dns_resolver=lambda _host: ["127.0.0.1"],
                pinned_hosts=pinned,
            )
        with self.assertRaises(ValueError):
            verify_browser_dns_binding(
                "https://rebind.example.com/",
                dns_resolver=lambda _host: ["198.51.100.42"],
                pinned_hosts=pinned,
            )

        # Route guard enforces DNS resolver and WebSocket egress
        guarded_provider = PlaywrightBrowserProvider(
            allowed_domains=("example.com",),
            dns_resolver=lambda host: ["127.0.0.1"] if "rebind" in host else ["93.184.216.34"],
        )
        ws_private = FakeRoute()
        await guarded_provider._guard_route(ws_private, FakeRequest("ws://127.0.0.1:8765/ws/v1"))
        self.assertEqual(ws_private.action, "abort:blockedbyclient")
        rebind_route = FakeRoute()
        await guarded_provider._guard_route(
            rebind_route, FakeRequest("https://rebind.example.com/api")
        )
        self.assertEqual(rebind_route.action, "abort:blockedbyclient")
        valid_wss = FakeRoute()
        await guarded_provider._guard_route(valid_wss, FakeRequest("wss://sub.example.com/stream"))
        self.assertEqual(valid_wss.action, "continue")

        discovery = guarded_provider.discover_browsers()
        self.assertIn("playwright_installed", discovery)
        self.assertIn("browsers", discovery)
        # A browser is only reported when a launchable binary exists; an installed wheel
        # without downloaded browsers must never look like an available browser.
        self.assertIsInstance(discovery["chromium_installed"], bool)
        self.assertEqual(
            "Chromium (Playwright)" in discovery["browsers"],
            discovery["chromium_installed"],
        )
        self.assertEqual(
            discovery["isolated_adapter"] == "PlaywrightBrowserProvider",
            discovery["chromium_installed"],
        )
        if not discovery["chromium_installed"]:
            self.assertTrue(discovery["isolated_adapter_reason"])

    def test_browser_discovery_reports_a_missing_chromium_binary_truthfully(self) -> None:
        import os
        import tempfile
        from pathlib import Path

        from arise.adapters.browser_playwright import discover_available_browsers

        previous = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
            try:
                empty = discover_available_browsers(collector=object())
                self.assertFalse(empty["chromium_installed"])
                self.assertNotIn("Chromium (Playwright)", empty["browsers"])
                self.assertIsNone(empty["isolated_adapter"])
                self.assertTrue(empty["isolated_adapter_reason"])

                browser = root / "chromium-1243" / "chrome-linux64"
                browser.mkdir(parents=True)
                (browser / "chrome").write_bytes(b"#!/bin/sh\n")
                filled = discover_available_browsers(collector=object())
                self.assertTrue(filled["chromium_installed"])
                self.assertIn("Chromium (Playwright)", filled["browsers"])
                self.assertEqual(filled["isolated_adapter"], "PlaywrightBrowserProvider")
                self.assertIsNone(filled["isolated_adapter_reason"])
            finally:
                if previous is None:
                    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
                else:
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = previous


class _DynamicResourceTool:
    def __init__(self, resources) -> None:
        from arise.core.contracts import Idempotency, RiskLevel
        from arise.core.ports import ToolSpec

        self.resources = resources
        self.resource_name = "browser.page.page-test"
        self.owned_during_dispatch: str | None = None
        self._spec = ToolSpec(
            name="browser.dynamic_test",
            version="1.0.0",
            description="test dynamic page resource locking",
            minimum_risk=RiskLevel.R1,
            required_resources=(),
            idempotency=Idempotency.IDEMPOTENT,
        )

    @property
    def spec(self):
        return self._spec

    def validate_parameters(self, parameters) -> None:
        if parameters:
            raise ValueError("no parameters expected")

    def resources_for(self, action) -> tuple[str, ...]:
        return (f"browser.page.{action.target.page_id}",)

    async def execute(self, action, observation, resources):
        from arise.core.ports import ExecutionOutcome, ExecutionStatus

        del observation
        await resources.ensure_valid()
        self.owned_during_dispatch = await self.resources.owner(self.resource_name)
        return ExecutionOutcome(ExecutionStatus.SUCCEEDED, "safe fake operation")


class DynamicResourceWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_acquires_adapter_computed_page_resource(self) -> None:
        from arise.adapters.memory import InMemoryEnvironment
        from arise.core.contracts import (
            ActionContract,
            AuthorizationContext,
            Condition,
            RiskLevel,
            TargetIdentity,
            TrustLevel,
        )
        from arise.core.events import InMemoryEventStore
        from arise.core.policy import PolicyEngine
        from arise.core.ports import ToolRegistry
        from arise.core.resources import ResourceManager
        from arise.core.runtime import AgentRuntime, FactVerifier
        from arise.core.tasks import InMemoryTaskRepository, TaskRecord, TaskStatus

        target = TargetIdentity(
            platform="browser",
            page_id="page-test",
            object_id="page",
            stable_id="page-test",
            semantic_name="Browser page",
        )
        authority = AuthorizationContext(
            principal_id="test-user",
            user_intent_id="intent-browser",
            trust=TrustLevel.USER_INSTRUCTION,
        )
        environment = InMemoryEnvironment({"browser.ready": True}, target=target)
        tasks = InMemoryTaskRepository()
        task = TaskRecord.planned("test browser page lock", authorization=authority)
        tasks.save(task)
        events = InMemoryEventStore()
        resources = ResourceManager()
        tool = _DynamicResourceTool(resources)
        registry = ToolRegistry()
        registry.register(tool)
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=registry,
            policy=PolicyEngine(),
            environment=environment,
            resources=resources,
            verifier=FactVerifier(environment),
        )
        action = ActionContract(
            task_id=task.task_id,
            tool_name=tool.spec.name,
            target=target,
            risk=RiskLevel.R1,
            authority=authority,
            postconditions=(Condition("browser.ready", expected=True),),
            action_id="browser-lock-test",
        )
        result = await runtime.execute_action(action, final_action=True)
        self.assertIs(result.task_status, TaskStatus.COMPLETED)
        self.assertEqual(tool.owned_during_dispatch, task.task_id)
        self.assertIsNone(await resources.owner(tool.resource_name))
