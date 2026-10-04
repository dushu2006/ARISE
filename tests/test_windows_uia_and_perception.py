from __future__ import annotations

import json
import unittest
from collections.abc import Sequence
from typing import Any

from arise.adapters.perception import (
    CoordinateFallbackSafetyGate,
    OcrPerceptionAdapter,
    PerceptionHierarchyPipeline,
    ScreenCaptureAdapter,
    ScreenshotDeduplicator,
    VisionGroundingAdapter,
    crop_captured_image,
    decode_rgba_png,
    downscale_captured_image,
    make_captured_image_from_rgba,
)
from arise.adapters.secrets import MemorySecretProvider
from arise.adapters.windows_uia import (
    RawUiaNode,
    WindowsUiaProvider,
    register_windows_uia_tools,
)
from arise.core.computer import (
    CapturedImage,
    ComputerFailureCode,
    CoordinateSpace,
    DisplayGeometry,
    GroundingProposal,
    OCRText,
    PerceptionSource,
    Point,
    Rect,
    ResolutionStatus,
    SelectorQuality,
    TargetCandidate,
    TargetDescriptor,
    TargetQuery,
    WindowRecord,
)
from arise.core.computer_ports import ComputerAdapterError
from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    Condition,
    RiskLevel,
    SecretRef,
    TargetIdentity,
    TrustLevel,
    utc_now,
)
from arise.core.model_gateway import ModelRouter
from arise.core.models import ModelRequest, ModelResponse, ModelRole
from arise.core.policy import PolicyEngine
from arise.core.ports import ToolRegistry, VerificationStatus
from arise.core.resources import ResourceManager
from arise.core.runtime import AgentRuntime
from arise.core.tasks import InMemoryTaskRepository, TaskRecord, TaskStatus


class FakeUiaBackend:
    def __init__(self) -> None:
        self.displays: list[DisplayGeometry] = [
            DisplayGeometry(
                display_id="display-primary",
                physical_bounds=Rect(0, 0, 1920, 1080),
                dpi_x=144.0,  # 150% DPI
                dpi_y=144.0,
                primary=True,
                dpi_available=True,
                dpi_source="GetDpiForMonitor",
            ),
            DisplayGeometry(
                display_id="display-secondary",
                physical_bounds=Rect(-1920, 0, 1920, 1080),
                dpi_x=96.0,
                dpi_y=96.0,
                primary=False,
                dpi_available=True,
                dpi_source="GetDpiForMonitor",
            ),
        ]
        self.windows: list[WindowRecord] = [
            WindowRecord(
                window_id="hwnd-1001",
                process_id=4200,
                title="Settings - token=sk-secret12345678901234567890",
                application="settings.exe",
                visible=True,
                minimized=False,
                maximized=False,
                foreground=True,
                bounds=Rect(100, 100, 800, 600),
                class_name="ApplicationFrameWindow",
            ),
            WindowRecord(
                window_id="hwnd-1002",
                process_id=4300,
                title="Background Terminal",
                application="wt.exe",
                visible=True,
                minimized=False,
                maximized=False,
                foreground=False,
                bounds=Rect(200, 200, 700, 500),
                class_name="CASCADIA_HOSTING_WINDOW_CLASS",
            ),
        ]
        self.nodes: list[RawUiaNode] = [
            RawUiaNode(
                node_id="node-save",
                window_id="hwnd-1001",
                process_id=4200,
                application="settings.exe",
                control_type="Button",
                role="button",
                name="Save Changes",
                automation_id="btnSave",
                enabled=True,
                visible=True,
                focused=False,
                supported_patterns=("InvokePattern",),
                bounds=Rect(150, 220, 120, 40),
                hierarchy=("Settings", "Footer"),
            ),
            RawUiaNode(
                node_id="node-username",
                window_id="hwnd-1001",
                process_id=4200,
                application="settings.exe",
                control_type="Edit",
                role="textbox",
                name="Username",
                automation_id="txtUsername",
                value="alice",
                enabled=True,
                visible=True,
                focused=True,
                supported_patterns=("ValuePattern",),
                bounds=Rect(150, 140, 240, 32),
                hierarchy=("Settings", "Form"),
            ),
            RawUiaNode(
                node_id="node-password",
                window_id="hwnd-1001",
                process_id=4200,
                application="settings.exe",
                control_type="Edit",
                role="textbox",
                name="Password",
                automation_id="txtPassword",
                value="raw-password-must-not-leak",
                sensitive=True,
                enabled=True,
                visible=True,
                focused=False,
                supported_patterns=("ValuePattern",),
                bounds=Rect(150, 180, 240, 32),
                hierarchy=("Settings", "Form"),
            ),
        ]
        self.cursor = Point(200.0, 240.0)
        self.user_input_detected = False
        self.invoked_nodes: list[str] = []
        self.values_set: dict[str, str] = {}
        self.focused_nodes: list[str] = []
        self.keys_sent: list[tuple[str, str]] = []

    async def list_displays(self) -> Sequence[DisplayGeometry]:
        return tuple(self.displays)

    async def list_windows(self, *, include_hidden: bool = False) -> Sequence[WindowRecord]:
        if include_hidden:
            return tuple(self.windows)
        return tuple(w for w in self.windows if w.visible)

    async def foreground_window(self) -> WindowRecord | None:
        return next((w for w in self.windows if w.foreground), None)

    async def focus_window(self, window_id: str) -> WindowRecord:
        updated: list[WindowRecord] = []
        target: WindowRecord | None = None
        for w in self.windows:
            is_fg = w.window_id == window_id
            rec = WindowRecord(
                window_id=w.window_id,
                process_id=w.process_id,
                title=w.title,
                application=w.application,
                visible=w.visible,
                minimized=w.minimized,
                maximized=w.maximized,
                foreground=is_fg,
                bounds=w.bounds,
                class_name=w.class_name,
            )
            if is_fg:
                target = rec
            updated.append(rec)
        if target is None:
            raise ComputerAdapterError(
                ComputerFailureCode.WINDOW_NOT_FOUND,
                "Window not found.",
                source=PerceptionSource.UI_AUTOMATION,
            )
        self.windows = updated
        return target

    async def inspect_window_nodes(
        self,
        window_id: str,
        *,
        max_depth: int = 16,
        max_nodes: int = 1024,
    ) -> Sequence[RawUiaNode]:
        del max_depth
        return tuple(n for n in self.nodes if n.window_id == window_id)[:max_nodes]

    async def cursor_position(self) -> Point | None:
        return self.cursor

    async def user_input_observed_since(self, monotonic_seconds: float) -> bool:
        del monotonic_seconds
        return self.user_input_detected

    async def invoke_node(
        self, window_id: str, node: RawUiaNode, *, click_point: Point | None = None
    ) -> None:
        del window_id, click_point
        self.invoked_nodes.append(node.node_id)

    async def set_node_value(self, window_id: str, node: RawUiaNode, value: str) -> None:
        del window_id
        self.values_set[node.node_id] = value
        self.nodes = [
            RawUiaNode(
                node_id=n.node_id,
                window_id=n.window_id,
                process_id=n.process_id,
                application=n.application,
                control_type=n.control_type,
                role=n.role,
                name=n.name,
                automation_id=n.automation_id,
                class_name=n.class_name,
                framework_id=n.framework_id,
                value=value if n.node_id == node.node_id else n.value,
                enabled=n.enabled,
                visible=n.visible,
                focused=n.focused,
                selected=n.selected,
                expanded=n.expanded,
                toggle_state=n.toggle_state,
                sensitive=n.sensitive,
                supported_patterns=n.supported_patterns,
                runtime_id=n.runtime_id,
                hierarchy=n.hierarchy,
                bounds=n.bounds,
                coordinate_space=n.coordinate_space,
                child_count=n.child_count,
            )
            for n in self.nodes
        ]

    async def focus_node(self, window_id: str, node: RawUiaNode) -> None:
        del window_id
        self.focused_nodes.append(node.node_id)

    async def send_keys(self, window_id: str, node: RawUiaNode | None, key: str) -> None:
        self.keys_sent.append((window_id, key))
        del node


class WindowsUiaProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.backend = FakeUiaBackend()
        self.secrets = MemorySecretProvider({"uia.password": "S3cretPass!99"})
        self.provider = WindowsUiaProvider(
            backend=self.backend,
            secret_provider=self.secrets,
        )

    async def test_window_and_control_tree_discovery_redact_secrets(self) -> None:
        windows = await self.provider.list_windows()
        self.assertEqual(len(windows), 2)
        self.assertNotIn("sk-secret12345678901234567890", windows[0].title)

        tree = await self.provider.inspect_tree("hwnd-1001")
        self.assertEqual(len(tree), 3)
        by_name = {el.name: el for el in tree}
        self.assertTrue(by_name["Password"].sensitive)
        self.assertIsNone(by_name["Password"].value)
        self.assertIsNone(by_name["Password"].safe_summary()["value"])
        self.assertEqual(by_name["Username"].value, "alice")
        self.assertEqual(by_name["Save Changes"].target.automation_id, "btnSave")

    async def test_semantic_resolution_and_invoke_focus_keyboard_and_secret_fill(self) -> None:
        res = await self.provider.resolve(
            TargetQuery(semantic_name="Save Changes", role="button", window_id="hwnd-1001")
        )
        self.assertIs(res.status, ResolutionStatus.RESOLVED)
        assert res.selected is not None
        await self.provider.invoke(res.selected)
        self.assertEqual(self.backend.invoked_nodes, ["node-save"])

        candidates = await self.provider.inspect("hwnd-1001")
        by_name = {c.descriptor.identity.semantic_name: c for c in candidates}
        await self.provider.set_value(by_name["Username"], "bob")
        self.assertEqual(self.backend.values_set["node-username"], "bob")

        # Sensitive field rejects plaintext
        candidates = await self.provider.inspect("hwnd-1001")
        by_name = {c.descriptor.identity.semantic_name: c for c in candidates}
        with self.assertRaises(ComputerAdapterError) as ctx:
            await self.provider.set_value(by_name["Password"], "plaintext-forbidden")
        self.assertIs(ctx.exception.code, ComputerFailureCode.PERMISSION_DENIED)

        # Sensitive field accepts SecretRef
        await self.provider.set_value(by_name["Password"], SecretRef("uia.password"))
        self.assertEqual(self.backend.values_set["node-password"], "S3cretPass!99")

        # Focus and keyboard press
        candidates = await self.provider.inspect("hwnd-1001")
        by_name = {c.descriptor.identity.semantic_name: c for c in candidates}
        await self.provider.focus(by_name["Username"])
        self.assertIn("node-username", self.backend.focused_nodes)
        await self.provider.press(by_name["Username"], "Enter")
        self.assertEqual(self.backend.keys_sent, [("hwnd-1001", "Enter")])

    async def test_mixed_dpi_normalization_and_round_trip(self) -> None:
        displays = await self.provider.displays()
        primary = displays[0]  # 144 DPI = 1.5x scale
        secondary = displays[1]  # -1920 origin, 96 DPI = 1.0x scale
        self.assertEqual(await self.provider.dpi_for_window("hwnd-1001"), (144.0, 144.0))

        logical = self.provider.normalize_bounds_to_logical(Rect(150, 300, 180, 60), primary)
        self.assertEqual(logical, Rect(100.0, 200.0, 120.0, 40.0))
        physical = self.provider.logical_bounds_to_physical(logical, primary)
        self.assertEqual(physical, Rect(150.0, 300.0, 180.0, 60.0))

        neg_logical = self.provider.normalize_bounds_to_logical(
            Rect(-1800, 50, 200, 100), secondary
        )
        self.assertEqual(neg_logical, Rect(120.0, 50.0, 200.0, 100.0))

    async def test_display_change_focus_change_and_human_interference_rejected(self) -> None:
        # 1. Display change between observation and invoke
        candidates = await self.provider.inspect("hwnd-1001")
        save_btn = next(
            c for c in candidates if c.descriptor.identity.semantic_name == "Save Changes"
        )
        self.backend.displays[0] = DisplayGeometry(
            display_id="display-primary",
            physical_bounds=Rect(0, 0, 2560, 1440),
            dpi_x=192.0,
            dpi_y=192.0,
            primary=True,
        )
        with self.assertRaises(ComputerAdapterError) as disp_err:
            await self.provider.invoke(save_btn)
        self.assertIs(disp_err.exception.code, ComputerFailureCode.ENVIRONMENT_CHANGED)

        # 2. Focus change between observation and invoke
        candidates = await self.provider.inspect("hwnd-1001")
        save_btn = next(
            c for c in candidates if c.descriptor.identity.semantic_name == "Save Changes"
        )
        await self.backend.focus_window("hwnd-1002")
        with self.assertRaises(ComputerAdapterError) as focus_err:
            await self.provider.invoke(save_btn)
        self.assertIs(focus_err.exception.code, ComputerFailureCode.ENVIRONMENT_CHANGED)
        await self.backend.focus_window("hwnd-1001")

        # 3. Human interference via cursor drift or input tick
        candidates = await self.provider.inspect("hwnd-1001")
        save_btn = next(
            c for c in candidates if c.descriptor.identity.semantic_name == "Save Changes"
        )
        self.backend.cursor = Point(600.0, 600.0)
        with self.assertRaises(ComputerAdapterError) as cursor_err:
            await self.provider.invoke(save_btn)
        self.assertIs(cursor_err.exception.code, ComputerFailureCode.USER_INTERFERENCE)

        candidates = await self.provider.inspect("hwnd-1001")
        save_btn = next(
            c for c in candidates if c.descriptor.identity.semantic_name == "Save Changes"
        )
        self.backend.user_input_detected = True
        with self.assertRaises(ComputerAdapterError) as input_err:
            await self.provider.invoke(save_btn)
        self.assertIs(input_err.exception.code, ComputerFailureCode.USER_INTERFERENCE)

    async def test_stale_regrounding_succeeds_for_semantic_target_and_rejects_stale_coordinate(
        self,
    ) -> None:
        candidates = await self.provider.inspect("hwnd-1001")
        save_btn = next(
            c for c in candidates if c.descriptor.identity.semantic_name == "Save Changes"
        )
        # Mutate unrelated sibling control -> state_hash changes, but save_btn identity is intact
        await self.backend.set_node_value("hwnd-1001", self.backend.nodes[1], "charlie")
        await self.provider.invoke(save_btn)
        self.assertEqual(self.provider.reground_count, 1)
        self.assertEqual(self.backend.invoked_nodes, ["node-save"])

        # Coordinate candidate with moved bounds is rejected even when re-grounding is enabled
        coord_candidate = TargetCandidate(
            descriptor=TargetDescriptor(
                identity=save_btn.descriptor.identity,
                source=PerceptionSource.COORDINATE,
                observed_at=utc_now(),
                observation_id=save_btn.descriptor.observation_id,
                bounds=Rect(10, 10, 50, 20),
                coordinate_space=CoordinateSpace.PHYSICAL_DESKTOP,
                selector_quality=SelectorQuality.COORDINATE,
            ),
            confidence=0.8,
            evidence=("coordinate fallback",),
        )
        with self.assertRaises(ComputerAdapterError) as coord_err:
            await self.provider.invoke(coord_candidate)
        self.assertIs(coord_err.exception.code, ComputerFailureCode.TARGET_STALE)

    async def test_uia_action_tool_integrates_with_agent_runtime_and_verifier(self) -> None:
        registry = ToolRegistry()
        register_windows_uia_tools(registry, self.provider)
        candidates = await self.provider.inspect("hwnd-1001")
        username_cand = next(
            c for c in candidates if c.descriptor.identity.semantic_name == "Username"
        )
        authority = AuthorizationContext(
            principal_id="user-1",
            user_intent_id="intent-uia-1",
            trust=TrustLevel.USER_INSTRUCTION,
            capabilities=frozenset({"desktop.ui_automation"}),
        )
        tasks = InMemoryTaskRepository()
        task = TaskRecord.planned("Update username in Settings", authorization=authority)
        tasks.save(task)
        from arise.core.events import InMemoryEventStore

        events = InMemoryEventStore()
        runtime = AgentRuntime(
            tasks=tasks,
            events=events,
            tools=registry,
            policy=PolicyEngine(),
            environment=self.provider,
            resources=ResourceManager(),
            verifier=self.provider,
        )
        action = ActionContract(
            task_id=task.task_id,
            action_id="uia-fill-username",
            tool_name="uia.fill",
            target=username_cand.descriptor.identity,
            risk=RiskLevel.R2,
            authority=authority,
            parameters={"text": "updated_user"},
            postconditions=(Condition("uia.element.edit.Username.value", expected="updated_user"),),
        )
        # R2 requires confirmation first
        policy = runtime.policy
        tool_spec = registry.get("uia.fill").spec
        first = await runtime.execute_action(action, final_action=True)
        self.assertIs(first.task_status, TaskStatus.WAITING_USER)
        grant = policy.issue_approval(action, tool_spec, approved_by="user-1")
        second = await runtime.execute_action(action, approval=grant, final_action=True)
        self.assertTrue(second.executed)
        self.assertEqual(second.step_status.value, "succeeded")
        assert second.verification is not None
        self.assertIs(second.verification.status, VerificationStatus.PASSED)
        self.assertIs(second.task_status, TaskStatus.COMPLETED)


class PerceptionAndHierarchyTests(unittest.IsolatedAsyncioTestCase):
    def _sample_rgba(self, width: int, height: int, color: tuple[int, int, int, int]) -> bytes:
        return bytes(color) * (width * height)

    async def test_png_encode_decode_crop_downscale_and_deduplication(self) -> None:
        rgba = self._sample_rgba(40, 20, (20, 120, 220, 255))
        image = make_captured_image_from_rgba(
            40,
            20,
            rgba,
            bounds=Rect(100, 200, 400, 200),
            display_id="display-primary",
        )
        w, h, decoded = decode_rgba_png(image.content)
        self.assertEqual((w, h), (40, 20))
        self.assertEqual(decoded, rgba)

        cropped = crop_captured_image(image, Rect(10, 5, 20, 10))
        self.assertEqual((cropped.width, cropped.height), (20, 10))
        self.assertEqual(cropped.bounds, Rect(200.0, 250.0, 200.0, 100.0))

        scaled = downscale_captured_image(image, max_dimension=20)
        self.assertEqual((scaled.width, scaled.height), (20, 10))
        self.assertEqual(scaled.bounds, image.bounds)

        dedup = ScreenshotDeduplicator()
        first, dup1 = dedup.record_or_reuse("window:1", image)
        second, dup2 = dedup.record_or_reuse("window:1", image)
        self.assertFalse(dup1)
        self.assertTrue(dup2)
        self.assertIs(first, second)
        self.assertEqual(dedup.dedup_hits, 1)

    async def test_screen_capture_adapter_active_window_and_region(self) -> None:
        class FakeCaptureBackend:
            async def capture_rect(
                self, bounds: Rect, *, display_id: str | None = None
            ) -> tuple[int, int, bytes]:
                del display_id
                w = max(1, min(100, int(bounds.width)))
                h = max(1, min(80, int(bounds.height)))
                return w, h, bytes((10, 20, 30, 255)) * (w * h)

        fg_win = WindowRecord(
            window_id="hwnd-active",
            process_id=100,
            title="Editor",
            application="code.exe",
            visible=True,
            minimized=False,
            maximized=False,
            foreground=True,
            bounds=Rect(50, 60, 80, 40),
        )

        async def get_fg() -> WindowRecord:
            return fg_win

        async def get_wins() -> Sequence[WindowRecord]:
            return (fg_win,)

        adapter = ScreenCaptureAdapter(
            backend=FakeCaptureBackend(),
            windows_fn=get_wins,
            foreground_window_fn=get_fg,
        )
        win_cap = await adapter.capture_active_window(max_dimension=40)
        self.assertEqual((win_cap.width, win_cap.height), (40, 20))
        self.assertEqual(win_cap.bounds, Rect(50, 60, 80, 40))

        region_cap = await adapter.capture_region(Rect(10, 10, 30, 20))
        self.assertEqual((region_cap.width, region_cap.height), (30, 20))

    async def test_ocr_and_vision_hierarchy_and_coordinate_fail_closed(self) -> None:
        rgba = self._sample_rgba(100, 50, (255, 255, 255, 255))
        screenshot = make_captured_image_from_rgba(
            100, 50, rgba, bounds=Rect(0, 0, 1000, 500), display_id="display-primary"
        )

        class FakeOcrBackend:
            async def extract_text(
                self, image: CapturedImage, *, language: str | None = None
            ) -> Sequence[OCRText]:
                del image
                return (
                    OCRText(
                        text="Submit Order",
                        bounds=Rect(20, 10, 30, 10),
                        confidence=0.94,
                        language=language or "en",
                    ),
                )

        class FakeVisionProvider:
            provider_id = "vision-local"
            model_ids = ("vl-model",)
            is_cloud = False
            max_concurrent_requests = 2

            def supports(self, role: ModelRole, modalities: frozenset[str]) -> bool:
                return role is ModelRole.VISION and "image" in modalities

            async def complete(self, request: ModelRequest) -> ModelResponse:
                return ModelResponse(
                    request_id=request.request_id,
                    provider_id=self.provider_id,
                    model_id="vl-model",
                    content=json.dumps(
                        {
                            "target_description": "Gear Icon Settings",
                            "bounds": [40, 20, 20, 15],
                            "confidence": 0.91,
                            "evidence": ["distinct gear icon in toolbar"],
                        }
                    ),
                    latency_ms=5.0,
                )

        router = ModelRouter()
        router.register(FakeVisionProvider())
        ocr_adapter = OcrPerceptionAdapter(backend=FakeOcrBackend())
        vision_adapter = VisionGroundingAdapter(model_router=router, minimum_confidence=0.75)
        pipeline = PerceptionHierarchyPipeline(ocr=ocr_adapter, vision=vision_adapter)

        # 1. OCR resolves when structural candidates are empty
        ocr_res = await pipeline.resolve_hierarchical(
            TargetQuery(semantic_name="Submit Order", role="text"),
            screenshot=screenshot,
        )
        self.assertIs(ocr_res.status, ResolutionStatus.RESOLVED)
        assert ocr_res.selected is not None
        self.assertIs(ocr_res.selected.descriptor.source, PerceptionSource.OCR_LAYOUT)
        self.assertEqual(ocr_res.selected.descriptor.bounds, Rect(200.0, 100.0, 300.0, 100.0))

        # 2. Vision resolves when OCR does not match
        vis_res = await pipeline.resolve_hierarchical(
            TargetQuery(semantic_name="Gear Icon Settings", role="button"),
            screenshot=screenshot,
        )
        self.assertIs(vis_res.status, ResolutionStatus.RESOLVED)
        assert vis_res.selected is not None
        self.assertIs(vis_res.selected.descriptor.source, PerceptionSource.VISION)
        self.assertEqual(vis_res.selected.descriptor.bounds, Rect(400.0, 200.0, 200.0, 150.0))

        # 3. Low-confidence vision proposal is rejected
        low_proposal = GroundingProposal(
            target_description="Gear Icon Settings",
            bounds=Rect(40, 20, 20, 15),
            confidence=0.52,
            evidence=("uncertain",),
            observation_id="obs-low",
        )
        with self.assertRaises(ComputerAdapterError) as low_err:
            vision_adapter.verify_and_build_candidate(
                low_proposal,
                image=screenshot,
                query=TargetQuery(semantic_name="Gear Icon Settings"),
            )
        self.assertIs(low_err.exception.code, ComputerFailureCode.VERIFICATION_FAILED)

        # 4. Coordinate fallback fails closed unless all safety gates pass
        coord_cand = TargetCandidate(
            descriptor=TargetDescriptor(
                identity=TargetIdentity(
                    platform="windows",
                    window_id="hwnd-1",
                    role="button",
                    semantic_name="OK",
                ),
                source=PerceptionSource.COORDINATE,
                observed_at=utc_now(),
                observation_id="obs-coord",
                bounds=Rect(100, 100, 80, 40),
                coordinate_space=CoordinateSpace.PHYSICAL_DESKTOP,
                selector_quality=SelectorQuality.COORDINATE,
            ),
            confidence=0.85,
            evidence=("coordinate fallback",),
        )
        unsafe_gate = CoordinateFallbackSafetyGate(
            allow_coordinate_fallback=True,
            observation_current=True,
            dpi_verified=False,  # DPI unverified -> must fail closed!
            focus_verified=True,
            no_human_interference=True,
        )
        with self.assertRaises(ComputerAdapterError) as gate_err:
            unsafe_gate.validate_or_raise(coord_cand)
        self.assertIs(gate_err.exception.code, ComputerFailureCode.INVALID_COORDINATE)

        safe_gate = CoordinateFallbackSafetyGate(
            allow_coordinate_fallback=True,
            observation_current=True,
            dpi_verified=True,
            focus_verified=True,
            no_human_interference=True,
        )
        safe_pt = safe_gate.validate_or_raise(coord_cand)
        self.assertTrue(Rect(100, 100, 80, 40).contains(safe_pt))

    async def test_win32_screen_capture_backend_and_win32_uia_backend_contracts(self) -> None:
        import ctypes

        from arise.adapters.perception import Win32ScreenCaptureBackend
        from arise.adapters.windows_uia import Win32UiaBackend

        # 1. Win32ScreenCaptureBackend converts BGRA to RGBA and releases GDI handles
        released: list[str] = []

        class FakeUser32Gdi:
            def GetDC(self, hwnd: int) -> int:
                assert hwnd == 0
                return 101

            def ReleaseDC(self, hwnd: int, dc: int) -> int:
                assert hwnd == 0 and dc == 101
                released.append("ReleaseDC")
                return 1

        class FakeGdi32:
            def CreateCompatibleDC(self, dc: int) -> int:
                assert dc == 101
                return 202

            def CreateCompatibleBitmap(self, dc: int, w: int, h: int) -> int:
                assert dc == 101 and w == 2 and h == 1
                return 303

            def SelectObject(self, dc: int, obj: int) -> int:
                assert dc == 202
                released.append(f"SelectObject:{obj}")
                return 404

            def BitBlt(self, *args: object) -> int:
                del args
                return 1

            def GetDIBits(
                self,
                dc: int,
                bmp: int,
                start: int,
                lines: int,
                buf: Any,
                header_ptr: Any,
                usage: int,
            ) -> int:
                assert dc == 202 and bmp == 303 and start == 0 and lines == 1 and usage == 0
                del header_ptr
                # Pixel 0: BGRA=(10, 20, 30, 255), Pixel 1: BGRA=(40, 50, 60, 255)
                data = bytes([10, 20, 30, 255, 40, 50, 60, 255])
                ctypes.memmove(buf, data, len(data))
                return 1

            def DeleteObject(self, obj: int) -> int:
                assert obj == 303
                released.append("DeleteObject")
                return 1

            def DeleteDC(self, dc: int) -> int:
                assert dc == 202
                released.append("DeleteDC")
                return 1

        gdi_backend = Win32ScreenCaptureBackend(user32=FakeUser32Gdi(), gdi32=FakeGdi32())
        w, h, rgba = await gdi_backend.capture_rect(Rect(0, 0, 2, 1))
        self.assertEqual((w, h), (2, 1))
        self.assertEqual(rgba, bytes([30, 20, 10, 255, 60, 50, 40, 255]))
        self.assertEqual(
            released,
            ["SelectObject:303", "SelectObject:404", "DeleteObject", "DeleteDC", "ReleaseDC"],
        )

        # 2. Win32UiaBackend child control walk, user input check, and control dispatch
        sent_messages: list[tuple[int, int, int, object]] = []
        posted_messages: list[tuple[int, int, int, int]] = []

        class FakeWin32User32:
            def GetForegroundWindow(self) -> int:
                return 1000

            def IsWindow(self, hwnd: int) -> bool:
                return hwnd in {1000, 1001, 1002}

            def GetWindowTextLengthW(self, hwnd: int) -> int:
                return len({1000: "Settings", 1001: "Save", 1002: "secret123"}.get(hwnd, ""))

            def GetWindowTextW(self, hwnd: int, buf: Any, max_len: int) -> int:
                del max_len
                val = {1000: "Settings", 1001: "Save", 1002: "secret123"}.get(hwnd, "")
                buf.value = val
                return len(val)

            def GetWindowThreadProcessId(self, hwnd: int, pid_ptr: Any) -> int:
                del hwnd
                ctypes.cast(pid_ptr, ctypes.POINTER(ctypes.c_ulong)).contents.value = 0
                return 1

            def GetWindowRect(self, hwnd: int, rect_ptr: Any) -> bool:
                coords = {
                    1000: (0, 0, 800, 600),
                    1001: (20, 40, 120, 80),
                    1002: (20, 100, 220, 130),
                }[hwnd]
                r = ctypes.cast(rect_ptr, ctypes.POINTER(ctypes.c_long * 4)).contents
                r[0], r[1], r[2], r[3] = coords
                return True

            def IsWindowVisible(self, hwnd: int) -> bool:
                del hwnd
                return True

            def IsWindowEnabled(self, hwnd: int) -> bool:
                del hwnd
                return True

            def IsIconic(self, hwnd: int) -> bool:
                del hwnd
                return False

            def IsZoomed(self, hwnd: int) -> bool:
                del hwnd
                return False

            def EnumChildWindows(self, parent_hwnd: int, cb: Any, lparam: int) -> bool:
                assert parent_hwnd == 1000
                cb(1001, lparam)
                cb(1002, lparam)
                return True

            def GetParent(self, hwnd: int) -> int:
                del hwnd
                return 1000

            def GetClassNameW(self, hwnd: int, buf: Any, max_len: int) -> int:
                del max_len
                cls_name = {1001: "Button", 1002: "Edit"}[hwnd]
                buf.value = cls_name
                return len(cls_name)

            def GetDlgCtrlID(self, hwnd: int) -> int:
                return {1001: 10, 1002: 20}[hwnd]

            def GetWindowLongW(self, hwnd: int, idx: int) -> int:
                assert idx == -16
                return 0x0020 if hwnd == 1002 else 0  # ES_PASSWORD on 1002

            def SetForegroundWindow(self, hwnd: int) -> bool:
                return hwnd == 1000

            def SendMessageW(self, hwnd: int, msg: int, wparam: int, lparam: object) -> int:
                sent_messages.append((hwnd, msg, wparam, lparam))
                return 1

            def PostMessageW(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
                posted_messages.append((hwnd, msg, wparam, lparam))
                return 1

        uia_win32 = Win32UiaBackend(user32=FakeWin32User32())
        nodes = await uia_win32.inspect_window_nodes("hwnd-1000")
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[1].role, "button")
        self.assertEqual(nodes[1].name, "Save")
        self.assertEqual(nodes[1].automation_id, "ctrl-10")
        self.assertEqual(nodes[2].role, "textbox")
        self.assertTrue(nodes[2].sensitive)
        self.assertEqual(nodes[2].value, "")

        await uia_win32.invoke_node("hwnd-1000", nodes[1])
        await uia_win32.set_node_value("hwnd-1000", nodes[2], "new-val")
        await uia_win32.focus_node("hwnd-1000", nodes[1])
        await uia_win32.send_keys("hwnd-1000", nodes[1], "Enter")
        self.assertEqual(sent_messages[0], (1001, 0x00F5, 0, 0))
        self.assertEqual(sent_messages[1], (1002, 0x000C, 0, "new-val"))
        self.assertEqual(sent_messages[2], (1001, 0x0007, 0, 0))
        self.assertEqual(posted_messages, [(1001, 0x0100, 0x0D, 0), (1001, 0x0101, 0x0D, 0)])
