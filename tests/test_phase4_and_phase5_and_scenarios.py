"""Comprehensive tests for Phase 4, Phase 5, Supervision, and Critical Scenarios 1–8."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from arise.adapters.brave_research import (
    detect_conflicting_sources,
)
from arise.adapters.browser_playwright import (
    PlaywrightBrowserProvider,
    register_playwright_tools,
)
from arise.adapters.memory import InMemoryEnvironment
from arise.adapters.openai_compatible import OpenAICompatibleProvider
from arise.adapters.secrets import MemorySecretProvider
from arise.adapters.sqlite import (
    SQLiteDatabase,
    SQLiteMemoryRepository,
)
from arise.adapters.windows_uia import (
    RawUiaNode,
    WindowsUiaProvider,
)
from arise.config.settings import AppSettings, SecuritySettings
from arise.core.computer import (
    DisplayGeometry,
    Point,
    Rect,
    WindowRecord,
)
from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    Condition,
    Idempotency,
    ObservationLease,
    RiskLevel,
    TrustLevel,
)
from arise.core.engine import TaskEngine
from arise.core.events import InMemoryEventStore
from arise.core.extensions import (
    AudioChunk,
    ContextQuery,
    ContextSource,
    MemoryDisabledError,
    MemoryEntry,
    MemoryGovernanceError,
    MemoryKind,
    RetrievedContext,
    validate_memory_write_governance,
)
from arise.core.model_gateway import ModelRouter
from arise.core.models import (
    ActionProposal,
    ConditionModel,
    ConversationTurn,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelSelectionRequest,
    PlanStep,
    StepFallbackPolicy,
    StepRetryPolicy,
    TargetModel,
    TaskPlan,
    UserRequest,
    VoiceState,
)
from arise.core.personalization import (
    LocalDeterministicEmbeddingAdapter,
    PersonalizationStore,
    ProceduralMemoryStore,
    ShortTermConversationMemory,
    WorkingMemoryStore,
)
from arise.core.policy import PolicyConfig, PolicyEngine
from arise.core.ports import (
    ExecutionOutcome,
    ExecutionStatus,
    ToolRegistry,
    ToolSpec,
)
from arise.core.resources import ResourceLease, ResourceManager
from arise.core.runtime import AgentRuntime, FactVerifier
from arise.core.tasks import InMemoryTaskRepository, TaskRecord, TaskStatus
from arise.core.voice import (
    AudioDevice,
    AudioHub,
    LiveEvent,
    LiveEventType,
    LiveSessionConfig,
    LiveToolCall,
    VoiceActivity,
    VoiceConfig,
    WakeWordDetection,
)
from arise.core.voice_bridge import TaskEngineVoiceAdapter, VoiceConversationBridge
from arise.server import create_app
from arise.supervisor import (
    BackendSupervisor,
    SupervisorConfig,
    build_sidecar,
    resolve_backend_command,
    sidecar_binary_name,
    target_triple,
)


class ConfigurableSetTool:
    """Test tool backed by InMemoryEnvironment with configurable failure count and resources."""

    def __init__(
        self,
        environment: InMemoryEnvironment,
        *,
        name: str = "test.set",
        risk: RiskLevel = RiskLevel.R1,
        resources: tuple[str, ...] = ("state-a",),
        idempotency: Idempotency = Idempotency.IDEMPOTENT,
        fail_times: int = 0,
        unknown_on_call: bool = False,
        delay_seconds: float = 0.0,
    ) -> None:
        self.environment = environment
        self.calls = 0
        self.fail_times = fail_times
        self.unknown_on_call = unknown_on_call
        self.delay_seconds = delay_seconds
        self.concurrent_active = 0
        self.max_concurrent_active = 0
        self._spec = ToolSpec(
            name=name,
            version="1.0",
            description=f"Configurable tool {name}",
            minimum_risk=risk,
            required_resources=resources,
            idempotency=idempotency,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def validate_parameters(self, parameters: Mapping[str, object]) -> None:
        if "key" not in parameters or "value" not in parameters:
            raise ValueError("key and value are required")

    async def execute(
        self,
        action: ActionContract,
        observation: ObservationLease,
        resources: ResourceLease,
    ) -> ExecutionOutcome:
        del observation
        await resources.ensure_valid()
        self.calls += 1
        self.concurrent_active += 1
        self.max_concurrent_active = max(self.max_concurrent_active, self.concurrent_active)
        try:
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)
            if self.unknown_on_call:
                return ExecutionOutcome(
                    status=ExecutionStatus.UNKNOWN,
                    summary="Simulated crash after side effect may have started.",
                    side_effect_may_have_occurred=True,
                )
            if self.calls <= self.fail_times:
                return ExecutionOutcome(
                    status=ExecutionStatus.FAILED,
                    summary="Transient pre-effect failure.",
                    side_effect_may_have_occurred=False,
                )
            await self.environment.set_fact(
                str(action.parameters["key"]), action.parameters["value"]
            )
            return ExecutionOutcome(
                status=ExecutionStatus.SUCCEEDED,
                summary="Fact updated.",
                side_effect_may_have_occurred=True,
            )
        finally:
            self.concurrent_active -= 1


class StaticPlanCompiler:
    def __init__(self, plan_builder) -> None:
        self._builder = plan_builder

    async def create_plan(self, request: UserRequest, task: TaskRecord) -> TaskPlan:
        return self._builder(request, task)


class Phase4PlanningAndModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for_terminal(self, engine: TaskEngine, task_id: str) -> TaskRecord:
        for _ in range(200):
            record = engine.tasks.get(task_id)
            in_flight = task_id in engine._active
            if record is not None and (
                record.status
                in {
                    TaskStatus.WAITING_USER,
                    TaskStatus.REQUIRES_USER_INPUT,
                }
                or (
                    not in_flight
                    and record.status
                    in {
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                        TaskStatus.BLOCKED,
                        TaskStatus.UNKNOWN,
                    }
                )
            ):
                return record
            await asyncio.sleep(0.01)
        self.fail(f"Task {task_id} did not settle in time")

    async def test_conditional_step_skips_when_false_and_executes_when_true(self) -> None:
        env = InMemoryEnvironment({"feature.enabled": False})
        tool = ConfigurableSetTool(env, name="test.set")
        tools = ToolRegistry()
        tools.register(tool)
        tasks = InMemoryTaskRepository()
        events = InMemoryEventStore()
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=tools,
            policy=PolicyEngine(),
            environment=env,
            resources=ResourceManager(),
            verifier=FactVerifier(env),
        )

        def build_plan(_req: UserRequest, task: TaskRecord) -> TaskPlan:
            step1 = PlanStep(
                step_id="step-cond-false",
                title="Run only when feature.enabled is true",
                condition=ConditionModel(key="feature.enabled", expected=True),
                skip_when_condition_false=True,
                action=ActionProposal(
                    action_id="act-cond-1",
                    tool_name="test.set",
                    risk=RiskLevel.R1,
                    parameters={"key": "skipped.fact", "value": "should-not-run"},
                    postconditions=(ConditionModel(key="skipped.fact", expected="should-not-run"),),
                ),
            )
            step2 = PlanStep(
                step_id="step-cond-true",
                title="Run when feature.enabled is false",
                depends_on=("step-cond-false",),
                condition=ConditionModel(key="feature.enabled", expected=False),
                action=ActionProposal(
                    action_id="act-cond-2",
                    tool_name="test.set",
                    risk=RiskLevel.R1,
                    parameters={"key": "executed.fact", "value": "ran"},
                    postconditions=(ConditionModel(key="executed.fact", expected="ran"),),
                ),
            )
            # Check per-step contract properties
            self.assertEqual(step2.tool_name, "test.set")
            self.assertEqual(step2.risk_level, RiskLevel.R1)
            self.assertEqual(step2.verification_method, "observed_postconditions")
            return TaskPlan(
                task_id=task.task_id,
                goal=task.goal,
                steps=(step1, step2),
                planner_id="test/conditional",
            )

        engine = TaskEngine(
            tasks=tasks,
            events=events,
            runtime=runtime,
            tools=tools,
            policy=PolicyEngine(),
            planner=StaticPlanCompiler(build_plan),
        )
        try:
            submitted = await engine.submit(
                UserRequest(text="Run conditional steps"), principal_id="user-1"
            )
            settled = await self.wait_for_terminal(engine, submitted.task_id)
            self.assertIs(settled.status, TaskStatus.COMPLETED)
            self.assertEqual(tool.calls, 1)
            event_types = [e.event_type for e in events.read_after(task_id=submitted.task_id)]
            self.assertIn("STEP_CONDITION_SKIPPED", event_types)
        finally:
            await engine.close()

    async def test_parallel_safe_steps_and_per_step_retry_and_fallback(self) -> None:
        env = InMemoryEnvironment({})
        tool_a = ConfigurableSetTool(
            env, name="test.parallel_a", resources=("res-a",), delay_seconds=0.03
        )
        tool_b = ConfigurableSetTool(
            env, name="test.parallel_b", resources=("res-b",), delay_seconds=0.03
        )
        flaky_tool = ConfigurableSetTool(env, name="test.flaky", resources=("res-c",), fail_times=1)
        always_fail_tool = ConfigurableSetTool(
            env, name="test.primary_fail", resources=("res-d",), fail_times=5
        )
        fallback_tool = ConfigurableSetTool(env, name="test.fallback_ok", resources=("res-d",))
        tools = ToolRegistry()
        for t in (tool_a, tool_b, flaky_tool, always_fail_tool, fallback_tool):
            tools.register(t)
        tasks = InMemoryTaskRepository()
        events = InMemoryEventStore()
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=tools,
            policy=PolicyEngine(),
            environment=env,
            resources=ResourceManager(),
            verifier=FactVerifier(env),
        )

        def build_plan(_req: UserRequest, task: TaskRecord) -> TaskPlan:
            p1 = PlanStep(
                step_id="p1",
                title="Parallel step A",
                parallel_safe=True,
                action=ActionProposal(
                    action_id="act-p1",
                    tool_name="test.parallel_a",
                    risk=RiskLevel.R1,
                    parameters={"key": "p1.done", "value": True},
                    postconditions=(ConditionModel(key="p1.done", expected=True),),
                ),
            )
            p2 = PlanStep(
                step_id="p2",
                title="Parallel step B",
                parallel_safe=True,
                action=ActionProposal(
                    action_id="act-p2",
                    tool_name="test.parallel_b",
                    risk=RiskLevel.R1,
                    parameters={"key": "p2.done", "value": True},
                    postconditions=(ConditionModel(key="p2.done", expected=True),),
                ),
            )
            retry_step = PlanStep(
                step_id="s-retry",
                title="Step with retry policy",
                depends_on=("p1", "p2"),
                retry_policy=StepRetryPolicy(max_attempts=2, backoff_seconds=0.01),
                action=ActionProposal(
                    action_id="act-retry",
                    tool_name="test.flaky",
                    risk=RiskLevel.R1,
                    parameters={"key": "retry.done", "value": True},
                    postconditions=(ConditionModel(key="retry.done", expected=True),),
                ),
            )
            fb_step = PlanStep(
                step_id="s-fallback",
                title="Step with fallback policy",
                depends_on=("s-retry",),
                fallback_policy=StepFallbackPolicy(
                    strategy="fallback_action",
                    fallback_action=ActionProposal(
                        action_id="act-fallback-1",
                        tool_name="test.fallback_ok",
                        risk=RiskLevel.R1,
                        parameters={"key": "fb.done", "value": True},
                        postconditions=(ConditionModel(key="fb.done", expected=True),),
                    ),
                ),
                action=ActionProposal(
                    action_id="act-primary-fail",
                    tool_name="test.primary_fail",
                    risk=RiskLevel.R1,
                    parameters={"key": "fb.done", "value": True},
                    postconditions=(ConditionModel(key="fb.done", expected=True),),
                ),
            )
            return TaskPlan(
                task_id=task.task_id,
                goal=task.goal,
                steps=(p1, p2, retry_step, fb_step),
                planner_id="test/parallel-retry-fallback",
            )

        engine = TaskEngine(
            tasks=tasks,
            events=events,
            runtime=runtime,
            tools=tools,
            policy=PolicyEngine(),
            planner=StaticPlanCompiler(build_plan),
        )
        try:
            submitted = await engine.submit(
                UserRequest(text="Run parallel, retry, and fallback steps"),
                principal_id="user-1",
            )
            settled = await self.wait_for_terminal(engine, submitted.task_id)
            self.assertIs(settled.status, TaskStatus.COMPLETED)
            self.assertEqual(tool_a.calls, 1)
            self.assertEqual(tool_b.calls, 1)
            self.assertEqual(flaky_tool.calls, 2)
            self.assertEqual(always_fail_tool.calls, 1)
            self.assertEqual(fallback_tool.calls, 1)
            event_types = [e.event_type for e in events.read_after(task_id=submitted.task_id)]
            self.assertIn("STEP_RETRY_SCHEDULED", event_types)
            self.assertIn("STEP_FALLBACK_EXECUTED", event_types)
        finally:
            await engine.close()

    async def test_model_router_escalation_streaming_and_backpressure(self) -> None:
        events = InMemoryEventStore()
        router = ModelRouter(
            max_concurrent_requests=1,
            max_queued_requests=1,
            events=events,
        )

        class DeepProvider:
            provider_id = "deep-reasoner-local"
            model_ids = ("deep-v1",)
            is_cloud = False
            max_concurrent_requests = 1
            supports_streaming = True

            def supports(self, role: ModelRole, modalities: frozenset[str]) -> bool:
                return role is ModelRole.DEEP_REASONER and modalities <= {"text"}

            async def complete(self, request: ModelRequest) -> ModelResponse:
                return ModelResponse(
                    request_id=request.request_id,
                    provider_id=self.provider_id,
                    model_id="deep-v1",
                    content="deep-escalated-result",
                    latency_ms=2.0,
                )

        router.register(DeepProvider())
        req = ModelRequest(
            role=ModelRole.PLANNER,
            messages=(ModelMessage(role="user", content="Complex multi-app synthesis"),),
            stream=False,
        )
        sel = ModelSelectionRequest(
            role=ModelRole.PLANNER,
            task_type="complex_synthesis",
            complexity="high",
            privacy="local_only",
        )
        escalated = await router.complete_with_escalation(req, selection=sel)
        self.assertEqual(escalated.provider_id, "deep-reasoner-local")
        self.assertEqual(escalated.content, "deep-escalated-result")

        # Test OpenAICompatibleProvider SSE streaming via ModelRouter.stream
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hello "},"finish_reason":null}]}\n\n',
            'data: {"choices":[{"delta":{"content":"world!"},"finish_reason":"stop"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content="".join(sse_lines).encode("utf-8"))
            )
        )
        stream_provider = OpenAICompatibleProvider(
            provider_id="sse-local",
            base_url="http://127.0.0.1:11434/v1",
            model_id="llama-local",
            api_key_secret_name="LOCAL_KEY",
            secret_provider=MemorySecretProvider(),
            is_cloud=False,
            supports_streaming=True,
            client=client,
        )
        router.register(stream_provider)
        try:
            chunks = []
            async for chunk in router.stream(req):
                chunks.append(chunk)
            text = "".join(c.text_delta for c in chunks)
            self.assertEqual(text, "Hello world!")
            self.assertTrue(any(c.is_final for c in chunks))
        finally:
            await client.aclose()

    async def test_web_research_relevance_filtering_and_conflict_detection(self) -> None:
        now = datetime.now(UTC)
        c1 = RetrievedContext(
            source=ContextSource.WEB_RESEARCH,
            source_id="https://alpha.example.com/spec",
            text="The ARISE latency budget is 150 ms and cloud mode is enabled by default.",
            provenance="Alpha Spec",
            retrieved_at=now,
            relevance=0.9,
        )
        c2 = RetrievedContext(
            source=ContextSource.WEB_RESEARCH,
            source_id="https://beta.example.com/spec",
            text="The ARISE latency budget is 500 ms and cloud mode is not enabled by default.",
            provenance="Beta Spec",
            retrieved_at=now,
            relevance=0.85,
        )
        checked = detect_conflicting_sources((c1, c2))
        self.assertIn("https://beta.example.com/spec", checked[0].conflicts_with)
        self.assertIn("https://alpha.example.com/spec", checked[1].conflicts_with)


class Phase5MemoryPersonalizationAndSupervisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_governance_working_shortterm_procedural_and_personalization(
        self,
    ) -> None:
        # 1. Governance checks reject payment card numbers and credential-only strings
        with self.assertRaises(MemoryGovernanceError):
            validate_memory_write_governance("card_number=4532015112830366")
        with self.assertRaises(MemoryGovernanceError):
            validate_memory_write_governance("api_key=[REDACTED]")

        # 2. WorkingMemoryStore and ShortTermConversationMemory
        wm = WorkingMemoryStore()
        snap = wm.upsert(
            task_id="task-101",
            principal_id="user-a",
            goal="Compare GPU specs",
            current_step_id="step-1",
            observations={"active_app": "msedge.exe"},
            note="Opened comparison tab",
        )
        self.assertEqual(snap.current_step_id, "step-1")
        self.assertIsNone(wm.get("task-101", principal_id="user-b"))

        stm = ShortTermConversationMemory(max_turns_per_session=2)
        for i in range(3):
            stm.append_turn(
                principal_id="user-a",
                turn=ConversationTurn(session_id="sess-1", speaker="user", text=f"Turn {i}"),
            )
        recent = stm.recent_turns(principal_id="user-a", session_id="sess-1")
        self.assertEqual([t.text for t in recent], ["Turn 1", "Turn 2"])

        # 3. SQLiteMemoryRepository with LocalDeterministicEmbeddingAdapter, edit, disable, episodic
        db = SQLiteDatabase(":memory:")
        try:
            embedder = LocalDeterministicEmbeddingAdapter()
            repo = SQLiteMemoryRepository(db, embedding=embedder)
            now = datetime.now(UTC)
            entry = MemoryEntry(
                principal_id="user-a",
                text="Prefer dark theme in Windows Terminal.",
                consent_reference="pending",
                expires_at=now + timedelta(days=30),
                kind=MemoryKind.PREFERENCE,
                confidence=0.95,
                sensitivity="personal",
                expiration_policy="ttl",
            )
            ref, _ = await repo.issue_write_consent(entry, now=now)
            rec_id = await repo.store(replace(entry, consent_reference=ref))
            stored = repo.get_record(principal_id="user-a", record_id=rec_id)
            assert stored is not None
            self.assertEqual(stored.id, rec_id)
            self.assertEqual(stored.type, "preference")
            self.assertEqual(stored.content, "Prefer dark theme in Windows Terminal.")
            self.assertAlmostEqual(stored.confidence, 0.95)
            self.assertEqual(stored.sensitivity, "personal")
            self.assertEqual(stored.expiration_policy, "ttl")

            # Edit memory record
            edited = await repo.update_record(
                principal_id="user-a",
                record_id=rec_id,
                text="Prefer solarized dark theme in Windows Terminal.",
                confidence=0.99,
            )
            self.assertIn("solarized", edited.text)
            self.assertAlmostEqual(edited.confidence, 0.99)

            # Episodic task summary
            auth = AuthorizationContext(
                principal_id="user-a",
                user_intent_id="intent-ep",
                trust=TrustLevel.USER_INSTRUCTION,
            )
            completed_task = TaskRecord.planned("Organize Downloads folder", authorization=auth)
            completed_task.transition_to(TaskStatus.RUNNING)
            completed_task.transition_to(TaskStatus.VERIFYING)
            completed_task.transition_to(TaskStatus.COMPLETED, verification_passed=True)
            ep_id = await repo.record_episodic_task_summary(completed_task, now=now)
            ep_rec = repo.get_record(principal_id="user-a", record_id=ep_id)
            assert ep_rec is not None
            self.assertIs(ep_rec.kind, MemoryKind.EPISODIC)

            # Disable memory for principal
            repo.set_enabled(principal_id="user-a", enabled=False)
            self.assertFalse(repo.is_enabled(principal_id="user-a"))
            self.assertEqual(
                await repo.retrieve(ContextQuery(query="solarized", principal_id="user-a")),
                (),
            )
            with self.assertRaises(MemoryDisabledError):
                await repo.store(replace(entry, consent_reference=ref))
            repo.set_enabled(principal_id="user-a", enabled=True)

            # 4. ProceduralMemoryStore & PersonalizationStore
            proc = ProceduralMemoryStore(db)
            step = PlanStep(
                step_id="wf-step-1",
                title="Focus Settings window",
                action=ActionProposal(
                    action_id="wf-act-1",
                    tool_name="uia.focus",
                    target=TargetModel(
                        platform="windows",
                        application="SystemSettings.exe",
                        window_id="hwnd-1001",
                        role="window",
                        semantic_name="Settings",
                    ),
                    risk=RiskLevel.R1,
                    parameters={},
                    preconditions=(
                        ConditionModel(key="uia.window.Settings.exists", expected=True),
                    ),
                    postconditions=(
                        ConditionModel(key="uia.active_window_id", expected="hwnd-1001"),
                    ),
                ),
            )
            wf = proc.save_workflow(
                principal_id="user-a",
                name="Open Windows Settings",
                description="Focus the Windows Settings window and verify focus.",
                goal_pattern="open windows settings",
                steps=(step,),
                provenance_task_id=completed_task.task_id,
                approved_by_user=True,
            )
            matched = proc.match_workflow(
                principal_id="user-a",
                request_text="Please open Windows Settings now",
            )
            assert matched is not None
            self.assertEqual(matched.workflow_id, wf.workflow_id)

            # Detect stale workflow steps when precondition fails
            stale = proc.detect_stale_workflow_steps(wf, {"uia.window.Settings.exists": False})
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0][0], "wf-step-1")

            # Adapt workflow to a new task plan with re-grounded target
            adapted_plan = proc.adapt_workflow_to_task_plan(
                wf,
                task_id="task-adapted-1",
                goal="Open Windows Settings",
                target_overrides={
                    "wf-step-1": TargetModel(
                        platform="windows",
                        application="SystemSettings.exe",
                        window_id="hwnd-2002",
                        role="window",
                        semantic_name="Settings",
                    )
                },
            )
            self.assertEqual(adapted_plan.steps[0].target.window_id, "hwnd-2002")

            # Personalization profile CRUD
            pstore = PersonalizationStore(db)
            profile = pstore.update_profile(
                principal_id="user-a",
                preferred_browser="msedge",
                preferred_apps={"terminal": "wt.exe"},
                preferred_response_style="concise",
                preferred_tts_voice="en-US-JennyNeural",
                preferred_tts_speed=1.15,
                approved_workflows=(wf.workflow_id,),
            )
            self.assertEqual(profile.preferred_browser, "msedge")
            self.assertEqual(profile.preferred_apps["terminal"], "wt.exe")
            self.assertEqual(profile.preferred_response_style, "concise")
        finally:
            db.close()

    async def test_supervisor_and_sidecar_packaging_contract(self) -> None:
        self.assertIn("windows", target_triple(system="Windows", machine="AMD64"))
        self.assertTrue(sidecar_binary_name(system="Windows", machine="AMD64").endswith(".exe"))
        cmd = resolve_backend_command()
        self.assertTrue(len(cmd) >= 1)

        with tempfile.TemporaryDirectory() as tmp:
            dest = build_sidecar(
                repo_root=Path(__file__).resolve().parents[1],
                output_dir=Path(tmp),
                dry_run=True,
            )
            self.assertEqual(dest.parent, Path(tmp))

        # Test BackendSupervisor startup handshake, crash recovery, and clean shutdown
        spawn_calls = 0

        class FakeSupervisedProcess:
            def __init__(self, *, crash_immediately: bool = False) -> None:
                self._returncode: int | None = 1 if crash_immediately else None
                self.stdin_closed = False
                self.killed = False

            @property
            def returncode(self) -> int | None:
                return self._returncode

            async def wait_ready(self, timeout_seconds: float) -> bool:
                del timeout_seconds
                return True

            async def wait_exit(self, timeout_seconds: float | None = None) -> int | None:
                del timeout_seconds
                return self._returncode

            async def close_stdin(self) -> None:
                self.stdin_closed = True
                self._returncode = 0

            def terminate_forcefully(self) -> None:
                self.killed = True
                self._returncode = -9

        processes: list[FakeSupervisedProcess] = []

        def spawn_fake() -> FakeSupervisedProcess:
            nonlocal spawn_calls
            spawn_calls += 1
            # First process crashes after startup; second stays alive
            proc = FakeSupervisedProcess(crash_immediately=(spawn_calls == 1))
            processes.append(proc)
            return proc

        supervisor = BackendSupervisor(
            SupervisorConfig(
                startup_timeout_seconds=1.0,
                shutdown_grace_seconds=0.5,
                max_restarts=2,
                restart_backoff_seconds=0.01,
                health_check_interval_seconds=0.02,
            ),
            spawner=spawn_fake,
            health_probe=lambda: True,
        )
        await supervisor.start()
        try:
            for _ in range(50):
                if supervisor.restart_count >= 1 and supervisor.is_running:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(supervisor.restart_count, 1)
            self.assertTrue(supervisor.is_running)
        finally:
            await supervisor.stop()
        self.assertTrue(processes[-1].stdin_closed)


class CriticalEndToEndAcceptanceScenariosTests(unittest.IsolatedAsyncioTestCase):
    """Tests all 8 critical end-to-end acceptance scenarios required by Section 10.5."""

    async def wait_for_task(self, engine: TaskEngine, task_id: str) -> TaskRecord:
        for _ in range(200):
            record = engine.tasks.get(task_id)
            in_flight = task_id in engine._active or engine.queued_count > 0
            if record is not None:
                if record.status is TaskStatus.WAITING_USER and bool(
                    engine.pending_confirmations(task_id)
                ):
                    return record
                if record.status is TaskStatus.REQUIRES_USER_INPUT and engine.can_accept_input(
                    task_id
                ):
                    return record
                if not in_flight and record.status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.BLOCKED,
                    TaskStatus.UNKNOWN,
                }:
                    return record
            await asyncio.sleep(0.01)
        self.fail(f"Task {task_id} did not settle")

    async def test_scenarios_1_through_8_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings(
                data_dir=Path(tmp),
                security=SecuritySettings(
                    environment="test",
                    require_api_auth=False,
                ),
            )
            app = create_app(settings)
            with TestClient(app) as client:
                services = app.state.services

                # Register a fast reasoner for Scenario 1 question answering
                class FastReasoner:
                    provider_id = "local-fast"
                    model_ids = ("fast-qna",)
                    is_cloud = False
                    max_concurrent_requests = 2
                    supports_streaming = False

                    def supports(self, role: ModelRole, modalities: frozenset[str]) -> bool:
                        return role is ModelRole.FAST_REASONER and modalities <= {"text"}

                    async def complete(self, request: ModelRequest) -> ModelResponse:
                        return ModelResponse(
                            request_id=request.request_id,
                            provider_id=self.provider_id,
                            model_id="fast-qna",
                            content=(
                                "Direct answer: Windows UI Automation uses semantic control trees."
                            ),
                            latency_ms=1.0,
                        )

                services.router.register(FastReasoner())

                # --- SCENARIO 1: Spoken / Text Question Answering ---
                resp1 = client.post(
                    "/api/v1/interactions",
                    json={"text": "What is Windows UI Automation?"},
                )
                self.assertEqual(resp1.status_code, 200)
                body1 = resp1.json()
                self.assertEqual(body1["outcome"], "answer")
                self.assertEqual(body1["intent"], "question")
                self.assertIsNone(body1["task"])
                self.assertIn("Direct answer", body1["answer"])

                # --- Verify Phase 5 REST endpoints (/personalization, /workflows, /memory) ---
                p_resp = client.put(
                    "/api/v1/personalization",
                    json={
                        "preferred_browser": "msedge",
                        "preferred_response_style": "concise",
                    },
                )
                self.assertEqual(p_resp.status_code, 200)
                self.assertEqual(p_resp.json()["preferred_browser"], "msedge")

                m_set = client.get("/api/v1/memory/settings")
                self.assertEqual(m_set.status_code, 200)
                self.assertTrue(m_set.json()["enabled"])

        # --- SCENARIO 2: Spoken Browser Task (VoiceBridge -> TaskEngine -> Browser -> Verifier) ---
        class ScenarioLocator:
            def __init__(self) -> None:
                self.fill_value: str | None = None

            async def count(self) -> int:
                return 1

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def fill(self, value: str, *, timeout: int) -> None:
                del timeout
                self.fill_value = value

        class ScenarioPage:
            def __init__(self, rows: list[dict[str, object]]) -> None:
                self.rows = rows
                self.url = "https://www.google.com/"
                self.locator_instance = ScenarioLocator()

            async def title(self) -> str:
                return "Google"

            async def evaluate(self, _script: str, limit: int) -> list[dict[str, object]]:
                return self.rows[:limit]

            def get_by_test_id(self, _test_id: str) -> ScenarioLocator:
                return self.locator_instance

            def get_by_role(self, _role: str, *, name: str, exact: bool) -> ScenarioLocator:
                del name, exact
                return self.locator_instance

            def is_closed(self) -> bool:
                return False

        class ScenarioContext:
            def __init__(self, p: ScenarioPage) -> None:
                self.pages = [p]

        row = {
            "role": "textbox",
            "name": "Search",
            "tag": "input",
            "id": "q",
            "test_id": "search-box",
            "label": "Search",
            "placeholder": "",
            "text": "",
            "input_type": "text",
            "sensitive": False,
            "contenteditable": False,
            "visible": True,
            "enabled": True,
            "checked": None,
            "selected_index": None,
            "hierarchy": ["Google"],
            "bounds": [20, 40, 240, 32],
        }
        page = ScenarioPage([row])
        browser_provider = PlaywrightBrowserProvider(
            allowed_domains=("google.com", "www.google.com"),
            dns_resolver=lambda _host: ("142.250.190.4",),
        )
        browser_provider._context = ScenarioContext(page)
        page_id = browser_provider._register_page(page)
        browser_provider._default_page_id = page_id

        tools = ToolRegistry()
        register_playwright_tools(tools, browser_provider)
        tasks = InMemoryTaskRepository()
        events = InMemoryEventStore()
        browser_policy = PolicyEngine(
            PolicyConfig(
                auto_execute_through=RiskLevel.R2,
                confirmation_threshold=RiskLevel.R3,
            )
        )
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=tools,
            policy=browser_policy,
            environment=browser_provider,
            resources=ResourceManager(),
            verifier=browser_provider,
        )

        async def plan_browser_task(_req: UserRequest, task: TaskRecord) -> TaskPlan:
            candidates = await browser_provider.inspect(page_id)
            search_box = next(
                c for c in candidates if c.descriptor.identity.semantic_name == "Search"
            )
            step = PlanStep(
                step_id="fill-search",
                title="Type NVIDIA into Search box",
                action=ActionProposal(
                    action_id="act-browser-fill",
                    tool_name="browser.fill",
                    target=TargetModel.model_validate(search_box.descriptor.identity.to_dict()),
                    risk=RiskLevel.R2,
                    parameters={"text": "NVIDIA"},
                    postconditions=(
                        ConditionModel(
                            key="browser.page_id",
                            expected=page_id,
                        ),
                    ),
                ),
            )
            return TaskPlan(
                task_id=task.task_id,
                goal=task.goal,
                steps=(step,),
                planner_id="test/browser-planner",
            )

        class AsyncPlanAdapter:
            async def create_plan(self, req: UserRequest, task: TaskRecord) -> TaskPlan:
                return await plan_browser_task(req, task)

        engine = TaskEngine(
            tasks=tasks,
            events=events,
            runtime=runtime,
            tools=tools,
            policy=browser_policy,
            planner=AsyncPlanAdapter(),
            capability_grants=lambda _p: frozenset({"browser.control"}),
        )
        bridge = VoiceConversationBridge(TaskEngineVoiceAdapter(engine))
        try:
            call_res = await bridge.handle_tool_call(
                LiveToolCall("call-s2", "execute_task", {"text": "Open Chrome and search NVIDIA"}),
                principal_id="user-1",
                session_id="voice-s2",
                user_text="Open Chrome and search NVIDIA",
            )
            self.assertEqual(call_res["status"], "accepted")
            self.assertFalse(call_res["verified"])
            settled = await self.wait_for_task(engine, call_res["task_id"])
            self.assertIs(settled.status, TaskStatus.COMPLETED)
            self.assertEqual(page.locator_instance.fill_value, "NVIDIA")
            status_res = await bridge.handle_tool_call(
                LiveToolCall("call-s2-status", "get_task_status", {"task_id": settled.task_id}),
                principal_id="user-1",
                session_id="voice-s2",
            )
            self.assertEqual(status_res["state"], "completed")
            self.assertTrue(status_res["verified"])
        finally:
            await engine.close()

        # --- SCENARIO 3: Voice Barge-In (User interrupts speech mid-response) ---
        import time as _time

        def _audio_frame(seq: int) -> AudioChunk:
            return AudioChunk(
                sequence=seq,
                codec="pcm_s16le",
                sample_rate_hz=16_000,
                channels=1,
                data=b"\x10\x00" * 320,
                captured_at_monotonic_ns=_time.monotonic_ns(),
            )

        class ScenarioMic:
            def __init__(self) -> None:
                self.devices = (AudioDevice("mic-1", "Test Mic", True),)
                self.queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue()

            async def list_devices(self) -> Sequence[AudioDevice]:
                return self.devices

            async def capture(self, device_id: str) -> AsyncIterator[AudioChunk]:
                assert device_id == "mic-1"
                while True:
                    c = await self.queue.get()
                    if c is None:
                        return
                    yield c

            async def close(self) -> None:
                self.queue.put_nowait(None)

        class ScenarioVAD:
            async def analyze(self, _chunk: AudioChunk) -> VoiceActivity:
                return VoiceActivity(True, 0.99)

        class ScenarioWake:
            def __init__(self) -> None:
                self.first = True

            async def accept(self, _chunk: AudioChunk) -> WakeWordDetection:
                if self.first:
                    self.first = False
                    return WakeWordDetection(matched=True)
                return WakeWordDetection(matched=False)

            async def end_utterance(self) -> WakeWordDetection:
                return WakeWordDetection(matched=False)

            async def reset(self) -> None:
                return None

        class ScenarioPlayback:
            def __init__(self) -> None:
                self.played: list[AudioChunk] = []
                self.stops = 0

            async def play(self, chunk: AudioChunk) -> None:
                self.played.append(chunk)

            async def stop(self) -> None:
                self.stops += 1

            async def close(self) -> None:
                return None

        class ScenarioLiveSession:
            def __init__(self) -> None:
                self.sent_audio: list[AudioChunk] = []
                self.events: asyncio.Queue[LiveEvent | None] = asyncio.Queue()
                self.output_generation = 0
                self.interruptions = 0

            async def send_audio(self, chunk: AudioChunk) -> None:
                self.sent_audio.append(chunk)

            async def interrupt(self, first_user_audio: AudioChunk) -> int:
                self.interruptions += 1
                self.output_generation += 1
                await self.send_audio(first_user_audio)
                return self.output_generation

            async def send_text(self, text: str) -> None:
                del text

            async def send_tool_response(
                self, call: LiveToolCall, response: dict[str, object]
            ) -> None:
                del call, response

            async def receive(self) -> AsyncIterator[LiveEvent]:
                while True:
                    ev = await self.events.get()
                    if ev is None:
                        return
                    yield ev

            async def close(self) -> None:
                self.events.put_nowait(None)

        class ScenarioLiveProvider:
            provider_id = "scenario-live"

            def __init__(self) -> None:
                self.session = ScenarioLiveSession()

            async def connect(self, _config: LiveSessionConfig) -> ScenarioLiveSession:
                return self.session

            async def close(self) -> None:
                return None

        mic = ScenarioMic()
        sink = ScenarioPlayback()
        provider3 = ScenarioLiveProvider()
        hub3 = AudioHub(
            microphone=mic,
            vad=ScenarioVAD(),
            wake_word_detector=ScenarioWake(),
            provider=provider3,
            playback=sink,
            config=VoiceConfig(wake_word="hey arise"),
        )
        await hub3.start()
        try:
            await hub3.process_chunk(_audio_frame(1))
            for _ in range(50):
                if hub3.state is VoiceState.LISTENING:
                    break
                await asyncio.sleep(0.01)
            await hub3._handle_live_event(
                provider3.session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=_audio_frame(2),
                    text="Speaking assistant response",
                    generation_id=0,
                ),
            )
            self.assertIs(hub3.state, VoiceState.SPEAKING)
            self.assertEqual(len(sink.played), 1)
            # User barges in with speech chunk while assistant is speaking
            await hub3.process_chunk(_audio_frame(3))
            self.assertGreaterEqual(sink.stops, 1)
            self.assertEqual(provider3.session.interruptions, 1)
            # Stale audio from generation 0 is gated and discarded
            await hub3._handle_live_event(
                provider3.session,
                LiveEvent(
                    type=LiveEventType.OUTPUT_AUDIO,
                    audio=_audio_frame(4),
                    text="Stale chunk",
                    generation_id=0,
                ),
            )
            self.assertEqual(len(sink.played), 1)
        finally:
            await hub3.close()

        # --- SCENARIO 4: Risky Action Confirmation (R3 waits for user, single-use grant) ---
        target4 = TargetModel(
            platform="windows",
            application="outlook.exe",
            window_id="hwnd-mail-1",
            role="button",
            semantic_name="Send",
        )
        env4 = InMemoryEnvironment({"email.sent": False}, target=target4.to_domain())
        send_tool = ConfigurableSetTool(env4, name="email.send", risk=RiskLevel.R3)
        tools4 = ToolRegistry()
        tools4.register(send_tool)
        tasks4 = InMemoryTaskRepository()
        events4 = InMemoryEventStore()
        policy4 = PolicyEngine()
        runtime4 = AgentRuntime(
            tasks=tasks4,
            events=events4,
            tools=tools4,
            policy=policy4,
            environment=env4,
            resources=ResourceManager(),
            verifier=FactVerifier(env4),
        )
        engine4 = TaskEngine(
            tasks=tasks4,
            events=events4,
            runtime=runtime4,
            tools=tools4,
            policy=policy4,
            planner=StaticPlanCompiler(
                lambda _r, t: TaskPlan(
                    task_id=t.task_id,
                    goal=t.goal,
                    steps=(
                        PlanStep(
                            step_id="send-step",
                            title="Send external email",
                            action=ActionProposal(
                                action_id="act-send",
                                tool_name="email.send",
                                target=target4,
                                risk=RiskLevel.R3,
                                parameters={"key": "email.sent", "value": True},
                                postconditions=(ConditionModel(key="email.sent", expected=True),),
                            ),
                        ),
                    ),
                    planner_id="test/r3",
                )
            ),
        )
        try:
            t4 = await engine4.submit(
                UserRequest(text="Send the quarterly report email"), principal_id="user-1"
            )
            waiting = await self.wait_for_task(engine4, t4.task_id)
            self.assertIs(waiting.status, TaskStatus.WAITING_USER)
            self.assertEqual(send_tool.calls, 0)
            confirmations = engine4.pending_confirmations(t4.task_id)
            self.assertEqual(len(confirmations), 1)
            await engine4.approve(
                task_id=t4.task_id,
                confirmation_id=confirmations[0].confirmation_id,
                approved_by="user-1",
            )
            done4 = await self.wait_for_task(engine4, t4.task_id)
            self.assertIs(done4.status, TaskStatus.COMPLETED)
            self.assertEqual(send_tool.calls, 1)
        finally:
            await engine4.close()

        # --- SCENARIO 5: Target Drift / Environment Change (Blocks stale, re-grounds semantic) ---
        class ScenarioUiaBackend:
            def __init__(self) -> None:
                self.windows = [
                    WindowRecord(
                        window_id="hwnd-1001",
                        process_id=4100,
                        title="Notepad",
                        application="notepad.exe",
                        visible=True,
                        minimized=False,
                        maximized=False,
                        foreground=True,
                        bounds=Rect(100, 100, 800, 600),
                        class_name="Notepad",
                    )
                ]
                self.nodes = [
                    RawUiaNode(
                        node_id="uia-btn-save",
                        window_id="hwnd-1001",
                        process_id=4100,
                        application="notepad.exe",
                        control_type="Button",
                        role="button",
                        name="Save",
                        automation_id="SaveButton",
                        enabled=True,
                        visible=True,
                        focused=False,
                        supported_patterns=("InvokePattern",),
                        bounds=Rect(120, 140, 80, 28),
                        hierarchy=("Notepad", "Toolbar"),
                    )
                ]

            async def list_displays(self) -> Sequence[DisplayGeometry]:
                return (
                    DisplayGeometry(
                        display_id="display-1",
                        physical_bounds=Rect(0, 0, 1920, 1080),
                        dpi_x=96.0,
                        dpi_y=96.0,
                        primary=True,
                        dpi_available=True,
                        dpi_source="GetDpiForMonitor",
                    ),
                )

            async def list_windows(self, *, include_hidden: bool = False) -> Sequence[WindowRecord]:
                del include_hidden
                return tuple(self.windows)

            async def foreground_window(self) -> WindowRecord | None:
                return self.windows[0]

            async def focus_window(self, window_id: str) -> WindowRecord:
                del window_id
                return self.windows[0]

            async def inspect_window_nodes(
                self,
                window_id: str,
                *,
                max_depth: int = 16,
                max_nodes: int = 1024,
            ) -> Sequence[RawUiaNode]:
                del window_id, max_depth, max_nodes
                return tuple(self.nodes)

            async def cursor_position(self) -> Point | None:
                return Point(150.0, 150.0)

            async def user_input_observed_since(self, monotonic_seconds: float) -> bool:
                del monotonic_seconds
                return False

            async def invoke_node(
                self, window_id: str, node: RawUiaNode, *, click_point: Point | None = None
            ) -> None:
                del window_id, node, click_point

            async def set_node_value(self, window_id: str, node: RawUiaNode, value: str) -> None:
                del window_id, node, value

            async def focus_node(self, window_id: str, node: RawUiaNode) -> None:
                del window_id, node

            async def send_keys_to_node(self, window_id: str, node: RawUiaNode, keys: str) -> None:
                del window_id, node, keys

        uia_backend = ScenarioUiaBackend()
        uia_provider = WindowsUiaProvider(backend=uia_backend)
        cands5 = await uia_provider.inspect("hwnd-1001")
        save_cand = next(c for c in cands5 if c.descriptor.identity.semantic_name == "Save")
        # Simulate UIA node_id / layout drift (window rebuilt control tree)
        uia_backend.nodes = [
            replace(
                uia_backend.nodes[0],
                node_id="uia-btn-save-rebuilt",
                bounds=Rect(220, 140, 80, 28),
            )
        ]
        regrounded = await uia_provider.reground_stale_target(save_cand.descriptor.identity)
        self.assertEqual(regrounded.descriptor.identity.object_id, "SaveButton")
        assert regrounded.descriptor.bounds is not None
        self.assertEqual(regrounded.descriptor.bounds.x, 220.0)

        # --- SCENARIO 6: Ambiguous Request -> Clarification -> Completion ---
        env6 = InMemoryEnvironment({"message.sent": False})
        msg_tool = ConfigurableSetTool(env6, name="msg.send", risk=RiskLevel.R1)
        tools6 = ToolRegistry()
        tools6.register(msg_tool)
        tasks6 = InMemoryTaskRepository()
        events6 = InMemoryEventStore()
        runtime6 = AgentRuntime(
            tasks=tasks6,
            events=events6,
            tools=tools6,
            policy=PolicyEngine(),
            environment=env6,
            resources=ResourceManager(),
            verifier=FactVerifier(env6),
        )

        def plan_ambiguous(req: UserRequest, task: TaskRecord) -> TaskPlan:
            if "john.smith@example.com" not in req.text.lower():
                return TaskPlan(
                    task_id=task.task_id,
                    goal=task.goal,
                    steps=(),
                    planner_id="test/clarify",
                    needs_clarification=True,
                    clarification_question="Which John should receive the message?",
                )
            return TaskPlan(
                task_id=task.task_id,
                goal=task.goal,
                steps=(
                    PlanStep(
                        step_id="send-john",
                        title="Send message to john.smith@example.com",
                        action=ActionProposal(
                            action_id="act-john",
                            tool_name="msg.send",
                            risk=RiskLevel.R1,
                            parameters={"key": "message.sent", "value": True},
                            postconditions=(ConditionModel(key="message.sent", expected=True),),
                        ),
                    ),
                ),
                planner_id="test/clarify",
            )

        engine6 = TaskEngine(
            tasks=tasks6,
            events=events6,
            runtime=runtime6,
            tools=tools6,
            policy=PolicyEngine(),
            planner=StaticPlanCompiler(plan_ambiguous),
        )
        try:
            t6 = await engine6.submit(UserRequest(text="Send that to John"), principal_id="user-1")
            clarify_state = await self.wait_for_task(engine6, t6.task_id)
            self.assertIs(clarify_state.status, TaskStatus.REQUIRES_USER_INPUT)
            self.assertEqual(msg_tool.calls, 0)
            await engine6.provide_input(
                t6.task_id,
                "Send to john.smith@example.com",
                principal_id="user-1",
            )
            done6 = await self.wait_for_task(engine6, t6.task_id)
            self.assertIs(done6.status, TaskStatus.COMPLETED)
            self.assertEqual(msg_tool.calls, 1)
        finally:
            await engine6.close()

        # --- SCENARIO 7: Adversarial Web Page (Prompt Injection Denied by Policy) ---
        policy7 = PolicyEngine()
        untrusted_auth = AuthorizationContext(
            principal_id="user-1",
            user_intent_id="intent-7",
            trust=TrustLevel.UNTRUSTED_EXTERNAL,
        )
        injected_action = ActionContract(
            task_id="task-7",
            action_id="act-injected",
            tool_name="msg.send",
            target=None,
            risk=RiskLevel.R1,
            authority=untrusted_auth,
            parameters={"key": "exfiltrate", "value": "secrets"},
            postconditions=(Condition("exfiltrate", expected="secrets"),),
        )
        decision7 = policy7.evaluate(injected_action, msg_tool.spec)
        self.assertEqual(decision7.kind.value, "deny")

        # --- SCENARIO 8: Interrupted / Unknown Outcome (Never Retried Automatically) ---
        env8 = InMemoryEnvironment({"file.written": False})
        crash_tool = ConfigurableSetTool(
            env8,
            name="file.write",
            risk=RiskLevel.R1,
            idempotency=Idempotency.IDEMPOTENT,
            unknown_on_call=True,
        )
        tools8 = ToolRegistry()
        tools8.register(crash_tool)
        tasks8 = InMemoryTaskRepository()
        events8 = InMemoryEventStore()
        runtime8 = AgentRuntime(
            tasks=tasks8,
            events=events8,
            tools=tools8,
            policy=PolicyEngine(),
            environment=env8,
            resources=ResourceManager(),
            verifier=FactVerifier(env8),
        )
        engine8 = TaskEngine(
            tasks=tasks8,
            events=events8,
            runtime=runtime8,
            tools=tools8,
            policy=PolicyEngine(),
            planner=StaticPlanCompiler(
                lambda _r, t: TaskPlan(
                    task_id=t.task_id,
                    goal=t.goal,
                    steps=(
                        PlanStep(
                            step_id="step-unknown",
                            title="Write file with crash mid-dispatch",
                            retry_policy=StepRetryPolicy(max_attempts=3),
                            action=ActionProposal(
                                action_id="act-unknown",
                                tool_name="file.write",
                                risk=RiskLevel.R1,
                                parameters={"key": "file.written", "value": True},
                                postconditions=(ConditionModel(key="file.written", expected=True),),
                            ),
                        ),
                    ),
                    planner_id="test/unknown",
                )
            ),
        )
        try:
            t8 = await engine8.submit(
                UserRequest(text="Write critical file"), principal_id="user-1"
            )
            settled8 = await self.wait_for_task(engine8, t8.task_id)
            self.assertIs(settled8.status, TaskStatus.UNKNOWN)
            # Even with retry_policy(max_attempts=3), UNKNOWN outcomes are NEVER retried!
            self.assertEqual(crash_tool.calls, 1)
        finally:
            await engine8.close()


class ProductionRuntimeCompositionAuditTests(unittest.TestCase):
    """Verify that create_app(settings) composes Voice+ASR+TTS+UIA+Browser+Perception+TaskEngine."""

    def test_create_app_composes_voice_tts_browser_uia_perception_and_routes_voice_utterances(
        self,
    ) -> None:
        import json as _json
        import time as _time
        from unittest.mock import AsyncMock, patch

        from fastapi.testclient import TestClient

        from arise.adapters.audio_local import LazyKokoroSpeechSynthesis
        from arise.config.settings import (
            AppSettings,
            BrowserSettings,
            DatabaseSettings,
            DesktopSettings,
            EmbeddingSettings,
            ModelSettings,
            PerceptionSettings,
            SecuritySettings,
            VoiceSettings,
        )
        from arise.core.computer import (
            CoordinateSpace,
            PerceptionSource,
            SelectorQuality,
            TargetCandidate,
            TargetDescriptor,
        )
        from arise.core.contracts import TargetIdentity, utc_now
        from arise.core.models import ModelResponse, ModelRole
        from arise.server import create_app

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vosk_dir = root / "vosk-model"
            vosk_dir.mkdir(parents=True, exist_ok=True)
            kokoro_model = root / "kokoro.onnx"
            kokoro_voices = root / "voices.bin"
            kokoro_model.write_bytes(b"fake-onnx")
            kokoro_voices.write_bytes(b"fake-voices")

            settings = AppSettings(
                data_dir=root,
                database=DatabaseSettings(path=root / "prod-audit.sqlite3"),
                desktop=DesktopSettings(enabled=True),
                browser=BrowserSettings(enabled=True, allowed_domains=["example.com"]),
                perception=PerceptionSettings(
                    enabled=True,
                    allow_coordinate_fallback=True,
                    unsafe_regions=((0, 0, 10, 10),),
                ),
                embeddings=EmbeddingSettings(use_local_fallback=True),
                model=ModelSettings(
                    provider_id="local-llm",
                    base_url="http://127.0.0.1:11434/v1",
                    model_id="local-model",
                ),
                voice=VoiceSettings(
                    enabled=True,
                    microphone_enabled=True,
                    local_model_path=vosk_dir,
                    kokoro_model_path=kokoro_model,
                    kokoro_voices_path=kokoro_voices,
                    tts_voice="af_heart",
                    allow_cloud=True,
                ),
                security=SecuritySettings(
                    environment="test",
                    require_api_auth=True,
                    allow_cloud_models=True,
                ),
            )

            with patch(
                "arise.server._voice_gemini_prerequisites",
                return_value=(True, True, True),
            ):
                app = create_app(settings)

            services = app.state.services
            # 1. Verify production composition created all subsystems without manual test wiring
            self.assertIsNotNone(services.voice_hub)
            self.assertIsNotNone(services.voice_hub.speech_recognizer)
            self.assertIsNotNone(services.voice_hub.wake_word_detector)
            self.assertIsNotNone(services.voice_hub.vad)
            self.assertIsInstance(services.voice_hub.speech_synthesizer, LazyKokoroSpeechSynthesis)
            self.assertIsNotNone(services.voice_hub.playback)
            self.assertIsNotNone(services.voice_hub.conversation_bridge)
            self.assertIsNotNone(services.uia_provider)
            assert services.uia_provider is not None
            self.assertTrue(services.uia_provider.allow_coordinate_fallback)
            self.assertEqual(services.uia_provider.unsafe_regions, (Rect(0, 0, 10, 10),))
            self.assertIsNotNone(services.browser_provider)
            self.assertIsNotNone(services.perception)
            assert services.perception is not None
            self.assertEqual(services.perception.unsafe_regions, (Rect(0, 0, 10, 10),))
            self.assertIsNotNone(services.screen_capture)
            self.assertIsNotNone(services.working_memory)
            self.assertIsNotNone(services.short_term_memory)

            # Verify tools and capabilities registered in production app
            tool_names = {spec.name for spec in services.tools.list_specs()}
            self.assertIn("uia.invoke", tool_names)
            self.assertIn("browser.navigate", tool_names)
            self.assertIn("browser.fill", tool_names)

            # Register a deterministic local model provider on services.router for planning & Q&A
            # and a lightweight test tool to verify single-step and multi-step execution
            env_state = InMemoryEnvironment({"chrome.opened": False, "chrome.searched": False})
            open_tool = ConfigurableSetTool(env_state, name="app.open_chrome", risk=RiskLevel.R1)
            search_tool = ConfigurableSetTool(
                env_state, name="app.search_chrome", risk=RiskLevel.R1
            )
            services.tools.register(open_tool)
            services.tools.register(search_tool)
            services.engine.runtime.environment = env_state
            services.engine.runtime.verifier = FactVerifier(env_state)

            class LocalProdModelProvider:
                provider_id = "local-prod-provider"
                model_ids = ("local-model",)
                is_cloud = False
                max_concurrent_requests = 4
                supports_streaming = False

                def supports(self, role: ModelRole, modalities: frozenset[str]) -> bool:
                    return role in {ModelRole.FAST_REASONER, ModelRole.PLANNER} and (
                        modalities <= {"text"}
                    )

                async def complete(self, request: ModelRequest) -> ModelResponse:
                    if request.role is ModelRole.FAST_REASONER:
                        return ModelResponse(
                            request_id=request.request_id,
                            provider_id=self.provider_id,
                            model_id="local-model",
                            content="To open Chrome, click its desktop icon or press Win+R.",
                            latency_ms=1,
                        )
                    user_goal = request.messages[1].content.lower()
                    if "and then search" in user_goal:
                        plan_dict = {
                            "steps": [
                                {
                                    "step_id": "step-open",
                                    "title": "Open Chrome",
                                    "action": {
                                        "action_id": "act-open-1",
                                        "tool_name": "app.open_chrome",
                                        "risk": 1,
                                        "parameters": {
                                            "key": "chrome.opened",
                                            "value": True,
                                        },
                                        "postconditions": [
                                            {"key": "chrome.opened", "expected": True}
                                        ],
                                    },
                                },
                                {
                                    "step_id": "step-search",
                                    "title": "Search for X in Chrome",
                                    "depends_on": ["step-open"],
                                    "action": {
                                        "action_id": "act-search-2",
                                        "tool_name": "app.search_chrome",
                                        "risk": 1,
                                        "parameters": {
                                            "key": "chrome.searched",
                                            "value": True,
                                        },
                                        "postconditions": [
                                            {"key": "chrome.searched", "expected": True}
                                        ],
                                    },
                                },
                            ]
                        }
                    else:
                        plan_dict = {
                            "steps": [
                                {
                                    "step_id": "step-open-only",
                                    "title": "Open Chrome",
                                    "action": {
                                        "action_id": "act-open-only",
                                        "tool_name": "app.open_chrome",
                                        "risk": 1,
                                        "parameters": {
                                            "key": "chrome.opened",
                                            "value": True,
                                        },
                                        "postconditions": [
                                            {"key": "chrome.opened", "expected": True}
                                        ],
                                    },
                                }
                            ]
                        }
                    return ModelResponse(
                        request_id=request.request_id,
                        provider_id=self.provider_id,
                        model_id="local-model",
                        content=_json.dumps(plan_dict),
                        latency_ms=1,
                    )

            # Replace unstarted OpenAICompatibleProvider with in-process provider on the same router
            services.router._providers.clear()
            services.router.register(LocalProdModelProvider())

            # Inject fake Kokoro model factory and recording playback on the composed voice_hub
            class FakeKokoroEngine:
                def create_stream(self, text: str, *, voice: str, lang: str):
                    del text, voice, lang

                    async def _gen():
                        yield [0.1, -0.1] * 160, 16_000

                    return _gen()

            services.voice_hub.speech_synthesizer._kokoro_factory = lambda _m, _v: (
                FakeKokoroEngine()
            )
            played_chunks: list[AudioChunk] = []

            class RecordingPlayback:
                async def play(self, chunk: AudioChunk) -> None:
                    played_chunks.append(chunk)

                async def stop(self) -> None:
                    return None

                async def close(self) -> None:
                    return None

            services.voice_hub.playback = RecordingPlayback()

            with TestClient(app) as client:
                headers = {"Authorization": f"Bearer {services.api_token}"}

                # Verify capabilities report desktop.ui_automation, browser.dom, vision.ocr
                caps = {
                    c["name"]: c for c in client.get("/api/v1/capabilities", headers=headers).json()
                }
                self.assertEqual(caps["desktop.ui_automation"]["status"], "available")
                self.assertEqual(caps["browser.dom"]["status"], "available")
                self.assertEqual(caps["vision.ocr"]["status"], "available")
                self.assertEqual(caps["memory.semantic"]["status"], "available")

                # A. "Tell me how to open Chrome" -> answered without task admission + TTS played
                q_res = client.post(
                    "/api/v1/voice/utterance",
                    headers=headers,
                    json={"text": "Tell me how to open Chrome", "speak_response": True},
                )
                self.assertEqual(q_res.status_code, 200, q_res.text)
                q_data = q_res.json()
                self.assertEqual(q_data["status"], "answered")
                self.assertEqual(q_data["intent"], "question")
                self.assertIsNone(q_data["task_id"])
                self.assertIn("To open Chrome", q_data["spoken_response"])
                self.assertEqual(client.get("/api/v1/tasks", headers=headers).json(), [])
                self.assertGreaterEqual(len(played_chunks), 1)

                # B. "Open Chrome" -> travels through production voice path into TaskEngine
                c_res = client.post(
                    "/api/v1/voice/utterance",
                    headers=headers,
                    json={"text": "Open Chrome", "speak_response": True},
                )
                self.assertEqual(c_res.status_code, 200, c_res.text)
                c_data = c_res.json()
                self.assertEqual(c_data["status"], "accepted")
                self.assertEqual(c_data["intent"], "command")
                single_task_id = c_data["task_id"]
                for _ in range(100):
                    detail = client.get(f"/api/v1/tasks/{single_task_id}", headers=headers).json()[
                        "task"
                    ]
                    if detail["state"] == "completed":
                        break
                    _time.sleep(0.01)
                self.assertEqual(detail["state"], "completed")
                self.assertEqual(len(detail["steps"]), 1)

                # C. "Open Chrome and then search for X" -> creates a real multi-step task
                env_state._facts["chrome.opened"] = False
                env_state._facts["chrome.searched"] = False
                m_res = client.post(
                    "/api/v1/voice/utterance",
                    headers=headers,
                    json={"text": "Open Chrome and then search for X", "speak_response": True},
                )
                self.assertEqual(m_res.status_code, 200, m_res.text)
                m_data = m_res.json()
                self.assertEqual(m_data["status"], "accepted")
                self.assertEqual(m_data["intent"], "multi_step_task")
                multi_task_id = m_data["task_id"]
                for _ in range(100):
                    m_detail = client.get(f"/api/v1/tasks/{multi_task_id}", headers=headers).json()[
                        "task"
                    ]
                    if m_detail["state"] == "completed":
                        break
                    _time.sleep(0.01)
                self.assertEqual(m_detail["state"], "completed")
                self.assertEqual(len(m_detail["steps"]), 2)
                self.assertEqual(
                    [s["status"] for s in m_detail["steps"]],
                    ["succeeded", "succeeded"],
                )

                # D. Production perception asks UIA before lower-confidence visual fallback,
                # returns only an untrusted proposal, and never admits a task by itself.
                foreground = WindowRecord(
                    window_id="hwnd-perception-test",
                    process_id=5200,
                    title="Test window",
                    application="test-app.exe",
                    visible=True,
                    minimized=False,
                    maximized=False,
                    foreground=True,
                    bounds=Rect(0, 0, 640, 480),
                )
                ui_candidate = TargetCandidate(
                    descriptor=TargetDescriptor(
                        identity=TargetIdentity(
                            platform="windows",
                            window_id=foreground.window_id,
                            role="button",
                            semantic_name="Submit",
                            stable_id="submit-button",
                        ),
                        source=PerceptionSource.UI_AUTOMATION,
                        observed_at=utc_now(),
                        observation_id="uia-perception-test",
                        bounds=Rect(100, 100, 80, 32),
                        coordinate_space=CoordinateSpace.PHYSICAL_DESKTOP,
                        selector_quality=SelectorQuality.EXACT_ACCESSIBLE_ROLE_NAME,
                    ),
                    confidence=0.99,
                    evidence=("test UIA semantic match",),
                )
                with (
                    patch.object(
                        services.uia_provider,
                        "foreground_window",
                        new=AsyncMock(return_value=foreground),
                    ),
                    patch.object(
                        services.uia_provider,
                        "inspect",
                        new=AsyncMock(return_value=(ui_candidate,)),
                    ),
                ):
                    p_res = client.post(
                        "/api/v1/perception/resolve",
                        headers=headers,
                        json={"query": "Submit", "capture_screen_if_needed": False},
                    )
                self.assertEqual(p_res.status_code, 200, p_res.text)
                self.assertEqual(p_res.json()["status"], "resolved")
                self.assertEqual(p_res.json()["target"]["semantic_name"], "Submit")
                self.assertEqual(
                    p_res.json()["authority"],
                    "untrusted_grounding_requires_policy_and_verifier",
                )
                self.assertEqual(len(client.get("/api/v1/tasks", headers=headers).json()), 2)


class WorkflowAdaptEndpointTests(unittest.TestCase):
    """`POST /api/v1/workflows/{id}/adapt` must use the real store contract and stay untrusted."""

    def _settings(self, tmp: Path) -> AppSettings:
        return AppSettings(
            data_dir=tmp,
            security=SecuritySettings(environment="test", require_api_auth=False),
        )

    def test_adapt_endpoint_returns_stale_steps_and_an_untrusted_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            app = create_app(self._settings(Path(raw)))
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/workflows",
                    json={
                        "name": "Evening summary",
                        "description": "Summarise the open project before shutdown",
                        "goal_pattern": "summarise the project",
                        "steps": [
                            {
                                "title": "focus the editor window",
                                "action": {
                                    "tool_name": "uia.focus",
                                    "risk": 1,
                                    "preconditions": [
                                        {
                                            "key": "uia.window.Editor.exists",
                                            "operator": "equals",
                                            "expected": True,
                                        }
                                    ],
                                },
                            },
                            {
                                "title": "read the legacy status bar",
                                "action": {"tool_name": "legacy.removed_tool", "risk": 1},
                            },
                        ],
                        "approved_by_user": True,
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                workflow_id = created.json()["workflow_id"]

                adapted = client.post(
                    f"/api/v1/workflows/{workflow_id}/adapt",
                    json={
                        "goal": "summarise the project tonight",
                        "observed_facts": {"uia.window.Editor.exists": False},
                    },
                )
                self.assertEqual(adapted.status_code, 200, adapted.text)
                body = adapted.json()
                self.assertEqual(body["workflow_id"], workflow_id)
                self.assertEqual(
                    [item["reason"] for item in body["stale_steps"]],
                    ["Precondition 'uia.window.Editor.exists' drifted from expected value."],
                )
                self.assertEqual(
                    body["authority"], "untrusted_proposal_requires_policy_and_verifier"
                )
                plan = body["plan"]
                self.assertEqual(len(plan["steps"]), 2)
                # Adapted steps keep their verification checkpoints and stay preview-only.
                self.assertTrue(all(step["verification_checkpoint"] for step in plan["steps"]))
                self.assertTrue(plan["task_id"].startswith("preview-"))
                self.assertEqual(plan["planner_id"], f"procedural/{workflow_id}@v1")
                self.assertEqual(len(client.get("/api/v1/tasks").json()), 0)

                # No body still works: an empty observation set reports no drift.
                bare = client.post(f"/api/v1/workflows/{workflow_id}/adapt")
                self.assertEqual(bare.status_code, 200, bare.text)
                self.assertEqual(bare.json()["stale_steps"], [])

                # Unknown workflow and oversized observation sets fail closed.
                self.assertEqual(client.post("/api/v1/workflows/nope/adapt").status_code, 404)
                oversized = {f"fact.{index}": True for index in range(65)}
                self.assertEqual(
                    client.post(
                        f"/api/v1/workflows/{workflow_id}/adapt",
                        json={"observed_facts": oversized},
                    ).status_code,
                    422,
                )
                self.assertEqual(len(client.get("/api/v1/tasks").json()), 0)


class ConversationMemoryWiringTests(unittest.TestCase):
    """The production text path must actually populate bounded in-process memory.

    Regression lock for the release-gate defect where `POST /api/v1/interactions` called
    `ShortTermConversationMemory.append_turn(session_id=..., role=..., content=...)` and
    `WorkingMemoryStore.put(WorkingMemorySnapshot(...))`. Neither signature exists, so the real
    route raised inside a broad `except Exception` that only logged a warning, leaving short-term
    and working memory permanently empty while every store-level unit test stayed green.
    """

    def _settings(self, tmp: Path) -> AppSettings:
        return AppSettings(
            data_dir=tmp,
            security=SecuritySettings(environment="test", require_api_auth=False),
        )

    def test_question_and_command_paths_record_short_term_and_working_memory(self) -> None:
        import logging

        with tempfile.TemporaryDirectory() as raw:
            app = create_app(self._settings(Path(raw)))
            with TestClient(app) as client:
                services = app.state.services
                with self.assertNoLogs("arise.api", level=logging.WARNING):
                    question = client.post(
                        "/api/v1/interactions",
                        json={
                            "text": "what is the capital of France?",
                            "session_id": "sess-memory-1",
                        },
                    )
                self.assertEqual(question.status_code, 200, question.text)
                turns = services.short_term_memory.recent_turns(
                    principal_id=services.principal_id,
                    session_id="sess-memory-1",
                )
                self.assertTrue(turns, "short-term memory stayed empty for a question")
                self.assertEqual(turns[0].speaker, "user")
                self.assertEqual(turns[0].text, "what is the capital of France?")

                with self.assertNoLogs("arise.api", level=logging.WARNING):
                    command = client.post(
                        "/api/v1/interactions",
                        json={"text": "open the Notepad window", "session_id": "sess-memory-2"},
                    )
                self.assertEqual(command.status_code, 200, command.text)
                body = command.json()
                self.assertEqual(body["outcome"], "task")
                task_id = body["task"]["task_id"]
                command_turns = services.short_term_memory.recent_turns(
                    principal_id=services.principal_id,
                    session_id="sess-memory-2",
                )
                self.assertTrue(
                    any(turn.task_id == task_id for turn in command_turns),
                    "the admitted task's user turn was not mirrored into short-term memory",
                )
                snapshot = services.working_memory.get(task_id, principal_id=services.principal_id)
                self.assertIsNotNone(snapshot, "working memory was never populated by the route")
                assert snapshot is not None
                self.assertEqual(snapshot.goal, "open the Notepad window")
                self.assertIsNotNone(snapshot.expires_at)
                self.assertGreater(snapshot.expires_at, datetime.now(UTC))
                self.assertEqual(snapshot.observations.get("session_id"), "sess-memory-2")

    def test_working_memory_expiry_and_short_term_bounds_are_enforced(self) -> None:
        store = WorkingMemoryStore(default_ttl_seconds=60)
        live = store.upsert(
            task_id="task-live", principal_id="local-user", goal="keep this context"
        )
        self.assertIsNotNone(live.expires_at)
        expired = store.upsert(
            task_id="task-expired",
            principal_id="local-user",
            goal="stale context",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        self.assertTrue(expired.is_expired(datetime.now(UTC)))
        self.assertIsNone(store.get("task-expired", principal_id="local-user"))
        self.assertIsNotNone(store.get("task-live", principal_id="local-user"))
        # An expired entry never inherits its observations into a fresh upsert.
        revived = store.upsert(
            task_id="task-expired",
            principal_id="local-user",
            goal="fresh context",
            observations={"stale": True},
        )
        self.assertEqual(revived.observations, {"stale": True})
        self.assertEqual(store.purge_expired(), 0)
        past = datetime.now(UTC) + timedelta(seconds=30)
        store.upsert(task_id="task-soon", principal_id="local-user", goal="brief", expires_at=past)
        self.assertEqual(store.purge_expired(now=past + timedelta(seconds=1)), 1)
        with self.assertRaises(ValueError):
            store.upsert(
                task_id="task-naive",
                principal_id="local-user",
                goal="reject naive expiry",
                expires_at=datetime(2030, 1, 1),
            )

        bounded = ShortTermConversationMemory(max_turns_per_session=2, max_chars_per_turn=16)
        long_turn = ConversationTurn(
            turn_id="turn-1",
            session_id="sess-bound",
            speaker="user",
            text="abcdefghijklmnopqrstuvwxyz",
        )
        stored = bounded.append_turn(principal_id="local-user", turn=long_turn)
        self.assertEqual(len(stored.text), 16)
        for index in range(3):
            bounded.append_turn(
                principal_id="local-user",
                turn=ConversationTurn(
                    turn_id=f"turn-{index + 2}",
                    session_id="sess-bound",
                    speaker="user",
                    text=f"turn {index}",
                ),
            )
        recent = bounded.recent_turns(principal_id="local-user", session_id="sess-bound", limit=10)
        self.assertEqual(len(recent), 2)
        self.assertEqual([turn.text for turn in recent], ["turn 1", "turn 2"])


if __name__ == "__main__":
    unittest.main()
