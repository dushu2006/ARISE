"""Comprehensive tests for Windows application launch capability, resolver, and security gates."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from arise.adapters.windows_app_discovery import WindowsApplicationCatalog
from arise.adapters.windows_app_launch import (
    AppLaunchTool,
    ResolvedApplication,
    WindowsAppLaunchProvider,
    WindowsApplicationResolver,
    _safe_basename,
    register_app_launch_tools,
)
from arise.config.settings import (
    AppSettings,
    DatabaseSettings,
    DesktopSettings,
    SecuritySettings,
)
from arise.core.computer import ComputerFailureCode, WindowRecord
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    Condition,
    Idempotency,
    RiskLevel,
    TargetIdentity,
)
from arise.core.events import InMemoryEventStore
from arise.core.models import (
    ModelRequest,
    ModelResponse,
    ModelRole,
    UserRequest,
)
from arise.core.planner import GatewayTaskPlanner
from arise.core.planner_contract import build_system_prompt
from arise.core.policy import PolicyDecisionKind, PolicyEngine
from arise.core.ports import ExecutionStatus, ToolRegistry, VerificationStatus
from arise.core.resources import ResourceManager
from arise.core.runtime import AgentRuntime
from arise.core.tasks import InMemoryTaskRepository, TaskRecord, TaskStatus
from arise.server import CompositeEnvironment, CompositeVerifier, create_app


def _empty_catalog_resolver() -> WindowsApplicationResolver:
    return WindowsApplicationResolver(catalog=WindowsApplicationCatalog(is_windows=False))


class FakeAppLaunchBackend:
    """Deterministic simulated backend for unit and integration testing."""

    def __init__(self) -> None:
        self.running_processes: list[dict[str, Any]] = []
        self.windows: list[WindowRecord] = []
        self.launched_paths: list[str] = []
        self.focused_windows: list[str] = []
        self.next_pid = 1000
        self.fail_launch: Exception | None = None
        self.exit_immediately = False
        self.delay_alive = 0.0

    async def list_running_processes(self) -> Sequence[dict[str, Any]]:
        return list(self.running_processes)

    async def launch_process(self, executable_path: str) -> int:
        if self.fail_launch is not None:
            raise self.fail_launch
        self.launched_paths.append(executable_path)
        pid = self.next_pid
        self.next_pid += 1
        name = _safe_basename(executable_path)
        if not self.exit_immediately:
            self.running_processes.append({"pid": pid, "name": name, "exe": executable_path})
        return pid

    async def is_process_alive(self, pid: int) -> bool:
        if self.delay_alive > 0:
            await asyncio.sleep(self.delay_alive)
        if self.exit_immediately:
            return False
        return any(p["pid"] == pid for p in self.running_processes)

    async def list_windows(self) -> Sequence[WindowRecord]:
        return list(self.windows)

    async def focus_window(self, window_id: str) -> WindowRecord:
        self.focused_windows.append(window_id)
        for win in self.windows:
            if win.window_id == window_id:
                return win
        return WindowRecord(
            window_id=window_id,
            process_id=1,
            title="Focused Window",
            application="App",
            visible=True,
            minimized=False,
            maximized=False,
            foreground=True,
        )


class ApplicationResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = _empty_catalog_resolver()

    def test_known_aliases_resolve_deterministically(self) -> None:
        aliases_to_test = [
            ("Chrome", "Google Chrome"),
            ("google chrome", "Google Chrome"),
            ("Notepad", "Notepad"),
            ("notepad.exe", "Notepad"),
            ("Calculator", "Calculator"),
            ("calc", "Calculator"),
            ("calc.exe", "Calculator"),
            ("VS Code", "Visual Studio Code"),
            ("vscode", "Visual Studio Code"),
            ("code", "Visual Studio Code"),
            ("PowerShell", "PowerShell"),
            ("powershell.exe", "PowerShell"),
            ("pwsh", "PowerShell"),
            ("Edge", "Microsoft Edge"),
            ("msedge", "Microsoft Edge"),
            ("Terminal", "Windows Terminal"),
            ("cmd", "Command Prompt"),
            ("Paint", "Paint"),
            ("File Explorer", "File Explorer"),
        ]
        for query, expected_name in aliases_to_test:
            self.resolver.register_alias(
                query,
                ResolvedApplication(
                    name=expected_name,
                    executable_path=f"C:\\Program Files\\{expected_name}\\app.exe",
                    process_names=(f"{expected_name.lower()}.exe",),
                ),
            )
            resolved = self.resolver.resolve(query)
            self.assertEqual(resolved.name, expected_name)

    def test_name_validation_rejects_empty_or_whitespace(self) -> None:
        with self.assertRaises(ValueError):
            self.resolver.validate_name("")
        with self.assertRaises(ValueError):
            self.resolver.validate_name("   ")

    def test_name_validation_rejects_excessive_length(self) -> None:
        with self.assertRaises(ValueError):
            self.resolver.validate_name("a" * 129)

    def test_name_validation_rejects_forbidden_shell_characters(self) -> None:
        forbidden = [
            "notepad.exe; calc.exe",
            "notepad.exe && calc.exe",
            "notepad.exe | calc.exe",
            "notepad.exe > out.txt",
            "notepad.exe < in.txt",
            "`calc.exe`",
            "$(calc.exe)",
            "notepad.exe\ncalc.exe",
            "notepad.exe\0calc.exe",
            'notepad.exe"calc.exe',
            "notepad.exe$VAR",
            "notepad.exe%VAR%",
            "calc.exe{1}",
        ]
        for malicious in forbidden:
            with self.assertRaises(ValueError, msg=f"Should reject: {malicious}"):
                self.resolver.validate_name(malicious)

    def test_name_validation_rejects_path_traversal(self) -> None:
        traversals = [
            "../../Windows/System32/calc.exe",
            "..\\..\\evil.exe",
            "/../evil",
        ]
        for malicious in traversals:
            with self.assertRaises(ValueError, msg=f"Should reject traversal: {malicious}"):
                self.resolver.validate_name(malicious)

    def test_name_validation_rejects_command_line_arguments(self) -> None:
        argument_attempts = [
            "notepad.exe /p file.txt",
            "calc.exe --version",
            "cmd.exe /c calc.exe",
            "powershell.exe -Command calc.exe",
            "app.exe -exec evil",
            "app.exe --all",
        ]
        for attempt in argument_attempts:
            with self.assertRaises(ValueError, msg=f"Should reject command arguments: {attempt}"):
                self.resolver.validate_name(attempt)

    def test_unknown_application_raises_application_not_found(self) -> None:
        with self.assertRaises(ComputerAdapterError) as caught:
            self.resolver.resolve("NonExistentApplication_123456789")
        self.assertEqual(caught.exception.code, ComputerFailureCode.APPLICATION_NOT_FOUND)

    def test_custom_registered_alias_takes_precedence(self) -> None:
        custom = ResolvedApplication(
            name="Custom App",
            executable_path="/usr/bin/custom-app",
            process_names=("custom-app",),
        )
        self.resolver.register_alias("my_custom_app", custom)
        resolved = self.resolver.resolve("my_custom_app")
        self.assertEqual(resolved.name, "Custom App")
        self.assertEqual(resolved.executable_path, "/usr/bin/custom-app")


class AppLaunchToolSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeAppLaunchBackend()
        self.resolver = _empty_catalog_resolver()
        self.provider = WindowsAppLaunchProvider(backend=self.backend, resolver=self.resolver)
        self.tool = AppLaunchTool(self.provider)

    def test_tool_spec_attributes(self) -> None:
        spec = self.tool.spec
        self.assertEqual(spec.name, "system.app_launch")
        self.assertEqual(spec.minimum_risk, RiskLevel.R1)
        self.assertEqual(spec.required_capabilities, frozenset({"desktop.launch"}))
        self.assertEqual(spec.idempotency, Idempotency.IDEMPOTENT)
        self.assertEqual(spec.parameter_names, ("application", "launch_intent"))
        self.assertIsNone(spec.target_scope)

    def test_parameter_validation(self) -> None:
        self.tool.validate_parameters({"application": "Chrome"})
        self.tool.validate_parameters({"app_name": "Notepad"})
        self.tool.validate_parameters({"name": "Calculator"})

        with self.assertRaises(ValueError):
            self.tool.validate_parameters({})
        with self.assertRaises(ValueError):
            self.tool.validate_parameters({"application": ""})
        with self.assertRaises(ValueError):
            self.tool.validate_parameters({"application": "notepad; calc"})

    def test_dynamic_resources_for_action(self) -> None:
        action = ActionContract(
            task_id="task-1",
            tool_name="system.app_launch",
            target=None,
            risk=RiskLevel.R1,
            authority=AuthorizationContext(
                principal_id="user-1",
                user_intent_id="intent-1",
                capabilities=frozenset({"desktop.launch"}),
            ),
            parameters={"application": "Google Chrome"},
        )
        resources = self.tool.resources_for(action)
        self.assertIn("desktop.launch", resources)
        self.assertIn("desktop.app.google_chrome", resources)


class AppLaunchExecutionAndVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.backend = FakeAppLaunchBackend()
        self.resolver = _empty_catalog_resolver()
        self.resolver.register_alias(
            "chrome",
            ResolvedApplication(
                name="Google Chrome",
                executable_path="C:\\Program Files\\Google\\Chrome\\chrome.exe",
                process_names=("chrome.exe", "chrome"),
                allow_reuse=True,
            ),
        )
        self.resolver.register_alias(
            "notepad",
            ResolvedApplication(
                name="Notepad",
                executable_path="C:\\Windows\\notepad.exe",
                process_names=("notepad.exe", "notepad"),
                allow_reuse=True,
            ),
        )
        self.provider = WindowsAppLaunchProvider(backend=self.backend, resolver=self.resolver)
        self.tool = AppLaunchTool(self.provider)
        self.authority = AuthorizationContext(
            principal_id="user-1",
            user_intent_id="intent-1",
            capabilities=frozenset({"desktop.launch"}),
        )

    def make_action(self, app_name: str = "Chrome") -> ActionContract:
        return ActionContract(
            task_id="task-launch-1",
            tool_name="system.app_launch",
            target=TargetIdentity(platform="windows", application=app_name),
            risk=RiskLevel.R1,
            authority=self.authority,
            parameters={"application": app_name},
        )

    async def test_successful_application_launch(self) -> None:
        action = self.make_action("Chrome")
        obs = await self.provider.observe(action)
        self.assertFalse(obs.facts["application.running"])

        outcome = await self.tool.execute(action, obs, None)  # type: ignore[arg-type]
        self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(outcome.result_metadata["application"], "Google Chrome")
        pid = outcome.result_metadata["process_id"]
        self.assertGreater(pid, 0)
        self.assertEqual(len(self.backend.launched_paths), 1)

        verification = await self.provider.verify(action)
        self.assertEqual(verification.status, VerificationStatus.PASSED)
        self.assertEqual(verification.level, 2)
        self.assertTrue(len(verification.evidence) > 0)

    async def test_idempotent_launch_reuses_already_running_application(self) -> None:
        self.backend.running_processes.append(
            {"pid": 4567, "name": "chrome.exe", "exe": "chrome.exe"}
        )
        self.backend.windows.append(
            WindowRecord(
                window_id="0xwin1",
                process_id=4567,
                title="Google Chrome",
                application="Google Chrome",
                visible=True,
                minimized=False,
                maximized=False,
                foreground=True,
            )
        )
        action = self.make_action("Chrome")
        obs = await self.provider.observe(action)
        self.assertTrue(obs.facts["application.running"])
        self.assertEqual(obs.facts["process_id"], 4567)

        outcome = await self.tool.execute(action, obs, None)  # type: ignore[arg-type]
        self.assertEqual(outcome.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(outcome.result_metadata["process_id"], 4567)
        self.assertEqual(len(self.backend.launched_paths), 0)
        self.assertIn("0xwin1", self.backend.focused_windows)

        verification = await self.provider.verify(action)
        self.assertEqual(verification.status, VerificationStatus.PASSED)

    async def test_launch_failure_raises_adapter_error(self) -> None:
        self.backend.fail_launch = OSError("Permission denied")
        action = self.make_action("Chrome")
        obs = await self.provider.observe(action)
        with self.assertRaises(ComputerAdapterError) as caught:
            await self.tool.execute(action, obs, None)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, ComputerFailureCode.ACTIVATION_FAILED)

    async def test_process_exits_immediately_fails_verification(self) -> None:
        self.backend.exit_immediately = True
        action = self.make_action("Chrome")
        obs = await self.provider.observe(action)
        with self.assertRaises(ComputerAdapterError) as caught:
            await self.tool.execute(action, obs, None)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, ComputerFailureCode.ACTION_UNKNOWN_OUTCOME)

    async def test_verification_detects_not_running(self) -> None:
        action = self.make_action("Chrome")
        verification = await self.provider.verify(action)
        self.assertEqual(verification.status, VerificationStatus.FAILED)

    async def test_explicit_postconditions_verified(self) -> None:
        self.backend.running_processes.append(
            {"pid": 9999, "name": "notepad.exe", "exe": "notepad.exe"}
        )
        action = ActionContract(
            task_id="task-launch-2",
            tool_name="system.app_launch",
            target=TargetIdentity(platform="windows", application="Notepad"),
            risk=RiskLevel.R1,
            authority=self.authority,
            parameters={"application": "Notepad"},
            postconditions=(
                Condition("application.running", expected=True),
                Condition("process.running", expected=True),
            ),
        )
        verification = await self.provider.verify(action)
        self.assertEqual(verification.status, VerificationStatus.PASSED)

    async def test_unmet_explicit_postconditions_fails(self) -> None:
        self.backend.running_processes.append(
            {"pid": 9999, "name": "notepad.exe", "exe": "notepad.exe"}
        )
        action = ActionContract(
            task_id="task-launch-3",
            tool_name="system.app_launch",
            target=TargetIdentity(platform="windows", application="Notepad"),
            risk=RiskLevel.R1,
            authority=self.authority,
            parameters={"application": "Notepad"},
            postconditions=(Condition("nonexistent_fact", expected=True),),
        )
        verification = await self.provider.verify(action)
        self.assertEqual(verification.status, VerificationStatus.FAILED)


class EndToEndRuntimeAndPolicyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.backend = FakeAppLaunchBackend()
        self.resolver = _empty_catalog_resolver()
        self.resolver.register_alias(
            "chrome",
            ResolvedApplication(
                name="Google Chrome",
                executable_path="C:\\Program Files\\Google\\Chrome\\chrome.exe",
                process_names=("chrome.exe",),
            ),
        )
        self.provider = WindowsAppLaunchProvider(backend=self.backend, resolver=self.resolver)
        self.tools = ToolRegistry()
        register_app_launch_tools(self.tools, self.provider)
        self.tasks = InMemoryTaskRepository()
        self.events = InMemoryEventStore()
        self.policy = PolicyEngine()
        self.environment = CompositeEnvironment(app_launch=self.provider)
        self.verifier = CompositeVerifier(self.environment, app_launch=self.provider)
        self.runtime = AgentRuntime(
            tasks=self.tasks,
            events=self.events,
            tools=self.tools,
            policy=self.policy,
            environment=self.environment,
            resources=ResourceManager(),
            verifier=self.verifier,
        )

    async def test_runtime_executes_launch_and_completes_task(self) -> None:
        authority = AuthorizationContext(
            principal_id="test-user",
            user_intent_id="intent-launch-1",
            capabilities=frozenset({"desktop.launch"}),
        )
        task = TaskRecord.planned("Open Chrome", authorization=authority)
        self.tasks.save(task)

        action = ActionContract(
            task_id=task.task_id,
            tool_name="system.app_launch",
            target=TargetIdentity(platform="windows", application="Chrome"),
            risk=RiskLevel.R1,
            authority=authority,
            parameters={"application": "Chrome"},
        )

        result = await self.runtime.execute_action(action, final_action=True)
        self.assertEqual(result.task_status, TaskStatus.COMPLETED)
        self.assertEqual(result.step_status.value, "succeeded")
        self.assertEqual(result.verification.status, VerificationStatus.PASSED)
        self.assertEqual(len(self.backend.launched_paths), 1)

    async def test_policy_denies_when_capability_is_missing(self) -> None:
        authority_without_cap = AuthorizationContext(
            principal_id="test-user",
            user_intent_id="intent-launch-2",
            capabilities=frozenset(),
        )
        task = TaskRecord.planned("Open Chrome", authorization=authority_without_cap)
        self.tasks.save(task)

        action = ActionContract(
            task_id=task.task_id,
            tool_name="system.app_launch",
            target=TargetIdentity(platform="windows", application="Chrome"),
            risk=RiskLevel.R1,
            authority=authority_without_cap,
            parameters={"application": "Chrome"},
        )

        result = await self.runtime.execute_action(action, final_action=True)
        self.assertEqual(result.task_status, TaskStatus.BLOCKED)
        self.assertEqual(result.policy_decision.kind, PolicyDecisionKind.DENY)
        self.assertIn("required capability was not delegated", result.policy_decision.reason)
        self.assertEqual(len(self.backend.launched_paths), 0)


class MockPlannerProvider:
    provider_id = "test-planner"
    model_ids = ("planner-model",)
    is_cloud = False
    max_concurrent_requests = 1
    supports_streaming = False

    def __init__(self, plan_json: str) -> None:
        self.plan_json = plan_json
        self.calls = 0

    def supports(self, role: ModelRole, modalities: frozenset[str]) -> bool:
        return role is ModelRole.PLANNER and modalities == frozenset({"text"})

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            request_id=request.request_id,
            provider_id=self.provider_id,
            model_id="planner-model",
            content=self.plan_json,
            latency_ms=1,
        )


class ProductionCompositionAndPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_composition_registers_launch_tool_and_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test-composition.sqlite3"
            settings = AppSettings(
                database=DatabaseSettings(path=db_path),
                desktop=DesktopSettings(enabled=True),
                security=SecuritySettings(environment="development", require_api_auth=False),
            )
            app = create_app(settings)
            services = app.state.services
            try:
                tool_names = [spec.name for spec in services.tools.list_specs()]
                self.assertIn("system.app_launch", tool_names)
                self.assertIn("uia.invoke", tool_names)

                caps = {c.name: c for c in services.health.capability_service.list_capabilities()}
                self.assertIn("tool.system.app_launch", caps)
                self.assertIn("desktop.launch", caps)

                prompt = build_system_prompt(services.tools.list_specs())
                self.assertIn("system.app_launch", prompt)
                self.assertIn("desktop.launch", prompt)
            finally:
                await services.engine.close()
                await services.router.close()
                services.database.close()

    async def test_planner_proposes_launch_and_engine_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test-planner.sqlite3"
            settings = AppSettings(
                database=DatabaseSettings(path=db_path),
                desktop=DesktopSettings(enabled=True),
                security=SecuritySettings(environment="development", require_api_auth=False),
            )
            app = create_app(settings)
            services = app.state.services
            try:
                fake_backend = FakeAppLaunchBackend()
                services.app_launch_provider._backend = fake_backend
                fake_resolver = _empty_catalog_resolver()
                services.app_launch_provider._resolver = fake_resolver
                services.uia_provider.application_resolver = fake_resolver
                fake_resolver.register_alias(
                    "chrome",
                    ResolvedApplication(
                        name="Google Chrome",
                        executable_path="C:\\Program Files\\Google\\Chrome\\chrome.exe",
                        process_names=("chrome.exe",),
                    ),
                )

                plan_content = (
                    '{"needs_clarification":false,"steps":['
                    '{"step_id":"step-1","title":"Open Chrome","action":'
                    '{"tool_name":"system.app_launch","risk":1,"parameters":{"application":"Chrome"}}}'
                    "]}"
                )
                planner_provider = MockPlannerProvider(plan_content)
                services.router.register(planner_provider)
                services.engine.planner = GatewayTaskPlanner(
                    services.router,
                    services.tools,
                    model_id="planner-model",
                    privacy="local_only",
                )

                request = UserRequest(text="Open Chrome")
                task_record = await services.engine.submit(request, principal_id="local-user")

                for _ in range(500):
                    current = services.tasks.get(task_record.task_id)
                    if current is not None:
                        task_record = current
                        if current.status in {
                            TaskStatus.COMPLETED,
                            TaskStatus.FAILED,
                            TaskStatus.BLOCKED,
                        }:
                            break
                    await asyncio.sleep(0.02)

                self.assertEqual(
                    task_record.status,
                    TaskStatus.COMPLETED,
                    task_record.status_reason,
                )
                self.assertEqual(len(fake_backend.launched_paths), 1)
            finally:
                await services.engine.close()
                await services.router.close()
                services.database.close()


if __name__ == "__main__":
    unittest.main()
