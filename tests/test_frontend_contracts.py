from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import get_args

from arise.core import models
from arise.core.events import EventEnvelope, EventSeverity
from arise.core.models import CapabilityStatus, HealthStatus, TaskSnapshot, TaskStepSnapshot
from arise.core.protocol import ServerFrame
from arise.core.tasks import StepStatus, TaskStatus


class FrontendBackendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        frontend_source = Path(__file__).parents[1] / "frontend" / "src"
        cls.source = (frontend_source / "types.ts").read_text(encoding="utf-8")
        cls.api_source = (frontend_source / "api.ts").read_text(encoding="utf-8")
        cls.app_source = (frontend_source / "App.tsx").read_text(encoding="utf-8")
        backend_root = Path(__file__).parents[1] / "src" / "arise"
        cls.server_source = (backend_root / "server.py").read_text(encoding="utf-8")
        cls.models_source = (backend_root / "core" / "models.py").read_text(encoding="utf-8")

    def _type_expression_values(self, expression: str, seen: set[str] | None = None) -> set[str]:
        seen = set() if seen is None else seen
        values = set(re.findall(r"'([^']+)'", expression))
        for alias in re.findall(r"\b([A-Z][A-Za-z0-9_]*)\b", expression):
            if alias in seen:
                continue
            match = re.search(
                rf"export type {re.escape(alias)}\s*=\s*(.*?);",
                self.source,
                flags=re.DOTALL,
            )
            if match is not None:
                values |= self._type_expression_values(match.group(1), seen | {alias})
        return values

    def union_values(self, name: str) -> set[str]:
        match = re.search(
            rf"export type {re.escape(name)}\s*=\s*(.*?);",
            self.source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match, f"TypeScript union {name} is missing")
        return self._type_expression_values(match.group(1))

    def interface_property_union(self, interface: str, property_name: str) -> set[str]:
        match = re.search(
            rf"export interface {re.escape(interface)}\s*\{{(.*?)\n\}}",
            self.source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match, f"TypeScript interface {interface} is missing")
        property_match = re.search(
            rf"\b{re.escape(property_name)}\s*:\s*(.*?);",
            match.group(1),
            flags=re.DOTALL,
        )
        self.assertIsNotNone(property_match, f"{interface}.{property_name} contract is missing")
        return self._type_expression_values(property_match.group(1))

    def interface_properties(self, interface: str) -> set[str]:
        match = re.search(
            rf"export interface {re.escape(interface)}\s*\{{(.*?)\n\}}",
            self.source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match, f"TypeScript interface {interface} is missing")
        return set(re.findall(r"^  ([A-Za-z_][A-Za-z0-9_]*)\??:", match.group(1), re.MULTILINE))

    def inline_array_object_properties(self, interface: str, property_name: str) -> set[str]:
        match = re.search(
            rf"export interface {re.escape(interface)}\s*\{{(.*?)\n\}}",
            self.source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match, f"TypeScript interface {interface} is missing")
        property_match = re.search(
            rf"\b{re.escape(property_name)}\s*:\s*Array<\{{(.*?)\}}\s*>;",
            match.group(1),
            flags=re.DOTALL,
        )
        self.assertIsNotNone(property_match, f"{interface}.{property_name} contract is missing")
        return set(
            re.findall(
                r"^    ([A-Za-z_][A-Za-z0-9_]*)\s*:",
                property_match.group(1),
                re.MULTILINE,
            )
        )

    def test_task_and_step_status_unions_match_backend_enums(self) -> None:
        self.assertEqual(self.union_values("TaskState"), {state.value for state in TaskStatus})
        self.assertEqual(
            self.interface_property_union("TaskStep", "status"),
            {state.value for state in StepStatus},
        )
        self.assertIs(TaskSnapshot.model_fields["state"].annotation, TaskStatus)
        self.assertIs(TaskStepSnapshot.model_fields["status"].annotation, StepStatus)
        verification_values = {item.value for item in models.VerificationStatus}
        self.assertEqual(
            self.interface_property_union("TaskStep", "verification_status"),
            verification_values,
        )

    def test_frontend_response_fields_match_backend_contracts(self) -> None:
        mappings = {
            "Capability": models.Capability,
            "DisplayInfo": models.DisplayInfo,
            "ActiveWindowInfo": models.ActiveWindowInfo,
            "ApplicationInfo": models.ApplicationInfo,
            "EnvironmentSnapshot": models.EnvironmentSnapshot,
            "DiagnosticsSnapshot": models.DiagnosticsSnapshot,
            "HealthSnapshot": models.HealthSnapshot,
            "VoiceStatusSnapshot": models.VoiceStatusSnapshot,
            "VoiceMetricSnapshot": models.VoiceMetricSnapshot,
            "ConfirmationRequest": models.ConfirmationRequest,
            "Session": models.Session,
            "TaskStep": models.TaskStepSnapshot,
            "TaskSnapshot": models.TaskSnapshot,
            "TaskDetail": models.TaskDetail,
            "ServerFrame": ServerFrame,
        }
        for frontend_name, backend_model in mappings.items():
            with self.subTest(contract=frontend_name):
                self.assertEqual(
                    self.interface_properties(frontend_name),
                    set(backend_model.model_fields),
                )
        self.assertEqual(
            self.interface_properties("EventRecord"),
            set(EventEnvelope.__dataclass_fields__) | {"sequence"},
        )
        self.assertEqual(
            self.inline_array_object_properties("Session", "turns"),
            set(models.ConversationTurn.model_fields),
        )
        self.assertEqual(
            self.inline_array_object_properties("DiagnosticsSnapshot", "providers"),
            set(models.ProviderStatus.model_fields),
        )

    def test_capability_health_and_event_unions_match_backend_contracts(self) -> None:
        self.assertEqual(
            self.union_values("CapabilityStatus"),
            {item.value for item in CapabilityStatus},
        )
        backend_availability = set(
            get_args(models.Capability.model_fields["availability"].annotation)
        )
        self.assertEqual(
            backend_availability,
            {item.value for item in CapabilityStatus} | {"deferred"},
        )
        self.assertEqual(
            self.interface_property_union("Capability", "availability"),
            {item.value for item in CapabilityStatus} | {"deferred"},
        )
        self.assertEqual(
            self.interface_property_union("Capability", "status"),
            {item.value for item in CapabilityStatus},
        )
        self.assertEqual(
            self.interface_property_union("HealthSnapshot", "status"),
            {item.value for item in HealthStatus},
        )
        self.assertEqual(
            self.interface_property_union("VoiceStatusSnapshot", "state"),
            {item.value for item in models.VoiceState},
        )
        self.assertEqual(
            self.interface_property_union("VoiceStatusSnapshot", "microphone_status"),
            {item.value for item in models.MicrophoneStatus},
        )
        self.assertEqual(
            self.interface_property_union("VoiceStatusSnapshot", "provider_status"),
            {item.value for item in models.VoiceProviderStatus},
        )
        self.assertEqual(
            self.interface_property_union("EventRecord", "severity"),
            {item.value for item in EventSeverity},
        )
        self.assertEqual(
            self.interface_property_union("ApplicationInfo", "source"),
            set(get_args(models.ApplicationInfo.model_fields["source"].annotation)),
        )
        self.assertEqual(
            self.interface_property_union("EnvironmentSnapshot", "network_status"),
            set(get_args(models.EnvironmentSnapshot.model_fields["network_status"].annotation)),
        )

    def test_core_imports_remain_independent_of_frameworks_and_adapters(self) -> None:
        core_dir = Path(__file__).parents[1] / "src" / "arise" / "core"
        forbidden = {
            "fastapi",
            "httpx",
            "openai",
            "playwright",
            "selenium",
            "sqlite3",
            "tauri",
            "win32api",
            "win32gui",
            "pywinauto",
        }
        imports: list[tuple[str, str]] = []
        for path in core_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend((path.name, alias.name.split(".")[0]) for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append((path.name, node.module.split(".")[0]))
        violations = sorted(
            (filename, module) for filename, module in imports if module in forbidden
        )
        self.assertEqual(violations, [])

    def test_frontend_recovers_expired_and_bounded_event_replays(self) -> None:
        self.assertIn("EVENT_CURSOR_EXPIRED", self.api_source)
        self.assertIn("replayCursor = floor", self.api_source)
        self.assertIn("EVENT_REPLAY_LIMIT", self.api_source)
        self.assertIn("callbacks.onReplayReset?.()", self.api_source)
        self.assertIn("Older event history was pruned", self.app_source)
        self.assertIn("void refreshTasks(api)", self.app_source)

    def test_text_composer_uses_intent_routed_interaction_contract(self) -> None:
        self.assertIn("async interact(", self.api_source)
        self.assertIn("'/interactions'", self.api_source)
        self.assertIn("api.interact(", self.app_source)
        self.assertNotIn("api.submitTask(", self.app_source)
        self.assertIn("TextInteractionResponse", self.source)
        self.assertIn("outcome: TextInteractionOutcome", self.source)
        self.assertIn("NO TASK CREATED", self.app_source)

    def test_parent_child_task_api_contract_is_typed(self) -> None:
        self.assertIn("parent_task_id: string | null", self.source)
        self.assertIn("parent_task_id: str | None", self.models_source)
        self.assertIn("async listChildTasks(", self.api_source)
        self.assertIn("async submitChildTask(", self.api_source)
        self.assertIn("/children", self.api_source)
        self.assertIn("/api/v1/tasks/{parent_task_id}/children", self.server_source)

    def test_environment_discovery_is_typed_and_user_requested(self) -> None:
        self.assertIn("audio_input_devices: string[]", self.source)
        self.assertIn("audio_output_devices: string[]", self.source)
        self.assertIn("async diagnostics(): Promise<DiagnosticsSnapshot>", self.api_source)
        self.assertIn("api.diagnostics()", self.app_source)
        self.assertIn("<EnvironmentDiagnosticsPanel snapshot={diagnostics} />", self.app_source)
        self.assertIn("Refresh status &amp; local facts", self.app_source)

    def test_protocol_frame_is_explicitly_versioned_as_v1(self) -> None:
        self.assertRegex(self.source, r"protocol_version\s*:\s*1\s*;")


if __name__ == "__main__":
    unittest.main()
