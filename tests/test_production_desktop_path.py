"""Composition inspection and FAKE-backend regressions, not live Windows evidence."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from test_planner_contract import EXECUTABLE_PLAN, ScriptedProvider
from test_windows_app_launch import FakeAppLaunchBackend
from test_windows_uia_and_perception import FakeUiaBackend

from arise.adapters.memory import InMemoryEnvironment, SetFactTool
from arise.adapters.secrets import MemorySecretProvider
from arise.adapters.windows_app_launch import (
    ResolvedApplication,
    Win32AppLaunchBackend,
    WindowsAppLaunchProvider,
    WindowsApplicationResolver,
)
from arise.adapters.windows_uia import (
    Win32UiaBackend,
    WindowsUiaProvider,
    register_windows_uia_tools,
)
from arise.config.settings import AppSettings, DatabaseSettings, DesktopSettings, SecuritySettings
from arise.core.computer import ComputerFailureCode, WindowRecord
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


def authority():
    return AuthorizationContext(
        principal_id="user",
        user_intent_id="intent",
        capabilities=frozenset({"desktop.ui_automation", "desktop.launch"}),
    )


def launch_fixture():
    backend = FakeAppLaunchBackend()
    backend.requires_visible_window = True
    resolver = WindowsApplicationResolver()
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
async def test_reuse_focus_failure_is_not_swallowed():
    backend, provider, _ = launch_fixture()
    backend.running_processes = [{"pid": 10, "name": "chrome.exe", "exe": r"C:\Chrome\chrome.exe"}]
    backend.windows = [window()]

    async def fail(_):
        raise ComputerAdapterError(ComputerFailureCode.WINDOW_NOT_FOUND, "Window disappeared.")

    backend.focus_window = fail
    with pytest.raises(ComputerAdapterError):
        await provider.launch_application("chrome")


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
    assert "adapter_unavailable" in runtime.tasks.get(action.task_id).steps[0].status_reason.lower()


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
    assert failure.value.code is ComputerFailureCode.TIMEOUT
    assert len(backend.launched_paths) == 1


@pytest.mark.asyncio
async def test_native_window_observation_failure_is_not_silently_empty():
    backend = Win32AppLaunchBackend()
    with pytest.raises(ComputerAdapterError) as failure:
        await backend.list_windows()
    assert failure.value.code is ComputerFailureCode.ADAPTER_UNAVAILABLE


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
