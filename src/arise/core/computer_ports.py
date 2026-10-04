"""Platform-neutral ports for desktop, browser, perception, and reconciliation adapters.

``ActionTool``, ``EnvironmentPort``, and ``VerifierPort`` from Phase 1 remain the
runtime integration points; the aliases below make their Phase 2 roles explicit
without introducing a parallel execution lifecycle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, TypeAlias

from arise.core.computer import (
    AccessibilityElement,
    BrowserTabRecord,
    CapturedImage,
    ComputerFailureCode,
    DisplayGeometry,
    EnvironmentFingerprint,
    GroundingProposal,
    InstalledApplication,
    InteractionTelemetry,
    OCRText,
    PerceptionSource,
    Point,
    Rect,
    RunningApplication,
    TargetCandidate,
    TargetQuery,
    TargetResolution,
    WindowRecord,
)
from arise.core.contracts import ActionContract, ObservationLease, SecretRef
from arise.core.ports import ActionTool, EnvironmentPort, VerificationResult, VerifierPort

EnvironmentProvider: TypeAlias = EnvironmentPort
ActionExecutor: TypeAlias = ActionTool
ActionVerifier: TypeAlias = VerifierPort
SensitiveText: TypeAlias = str | SecretRef


class WindowProvider(Protocol):
    async def list_windows(self, *, include_hidden: bool = False) -> Sequence[WindowRecord]: ...

    async def foreground_window(self) -> WindowRecord | None: ...

    async def focus_window(self, window_id: str) -> WindowRecord: ...


class ApplicationProvider(Protocol):
    async def running_applications(self) -> Sequence[RunningApplication]: ...

    async def installed_applications(
        self, *, limit: int = 512
    ) -> Sequence[InstalledApplication]: ...

    async def launch_application(
        self, application_id: str, *, timeout_seconds: float
    ) -> RunningApplication: ...


class BrowserProvider(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def list_tabs(self) -> Sequence[BrowserTabRecord]: ...

    async def inspect(
        self, page_id: str, *, max_elements: int = 512
    ) -> Sequence[TargetCandidate]: ...

    async def navigate(
        self,
        page_id: str,
        url: str,
        *,
        timeout_seconds: float,
        expected_observation: ObservationLease | None = None,
    ) -> BrowserTabRecord: ...

    async def resolve(self, page_id: str, query: TargetQuery) -> TargetResolution: ...

    async def click(self, candidate: TargetCandidate, *, timeout_seconds: float) -> str: ...

    async def fill(
        self,
        candidate: TargetCandidate,
        text: SensitiveText,
        *,
        timeout_seconds: float,
    ) -> str: ...

    async def press(
        self, candidate: TargetCandidate, key: str, *, timeout_seconds: float
    ) -> str: ...

    async def select_option(
        self, candidate: TargetCandidate, value: str, *, timeout_seconds: float
    ) -> str: ...

    async def scroll_into_view(
        self, candidate: TargetCandidate, *, timeout_seconds: float
    ) -> str: ...


class AccessibilityProvider(Protocol):
    async def inspect_tree(
        self,
        window_id: str,
        *,
        max_depth: int = 16,
        max_nodes: int = 1024,
    ) -> Sequence[AccessibilityElement]: ...

    async def resolve(self, query: TargetQuery) -> TargetResolution: ...

    async def invoke(self, target: TargetCandidate, *, timeout_seconds: float) -> str: ...

    async def set_value(
        self,
        target: TargetCandidate,
        value: SensitiveText,
        *,
        timeout_seconds: float,
    ) -> str: ...


class ScreenCaptureProvider(Protocol):
    async def capture_desktop(self, *, max_dimension: int = 4096) -> CapturedImage: ...

    async def capture_display(
        self, display_id: str, *, max_dimension: int = 4096
    ) -> CapturedImage: ...

    async def capture_window(
        self, window_id: str, *, max_dimension: int = 4096
    ) -> CapturedImage: ...

    async def capture_region(
        self,
        bounds: Rect,
        *,
        display_id: str | None = None,
        max_dimension: int = 4096,
    ) -> CapturedImage: ...


class DisplayProvider(Protocol):
    async def displays(self) -> Sequence[DisplayGeometry]: ...


class DPIProvider(Protocol):
    async def dpi_for_display(self, display_id: str) -> tuple[float, float]: ...

    async def dpi_for_window(self, window_id: str) -> tuple[float, float]: ...


class OCRProvider(Protocol):
    async def recognize(
        self,
        image: CapturedImage,
        *,
        language: str | None = None,
        region: Rect | None = None,
    ) -> Sequence[OCRText]: ...


class VisionProvider(Protocol):
    async def ground(
        self,
        image: CapturedImage,
        *,
        question: str,
        candidates: Sequence[TargetCandidate] = (),
        timeout_seconds: float = 30.0,
    ) -> GroundingProposal: ...


class InputProvider(Protocol):
    """Lowest-level input port; callers must pass freshly validated targets/points."""

    async def move_pointer(self, point: Point) -> None: ...

    async def click(self, point: Point, *, button: str = "left", clicks: int = 1) -> None: ...

    async def type_text(self, text: SensitiveText, *, interval_seconds: float = 0.0) -> None: ...

    async def press_keys(self, keys: Sequence[str]) -> None: ...

    async def scroll(self, delta_x: int, delta_y: int) -> None: ...


class TargetResolverPort(Protocol):
    def resolve(
        self, query: TargetQuery, candidates: Sequence[TargetCandidate]
    ) -> TargetResolution: ...


class GroundingProvider(Protocol):
    async def ground(
        self,
        query: TargetQuery,
        *,
        image: CapturedImage,
        candidates: Sequence[TargetCandidate],
    ) -> GroundingProposal: ...


class InteractionObserver(Protocol):
    async def snapshots(self) -> AsyncIterator[EnvironmentFingerprint]: ...

    async def stop(self) -> None: ...


class ReconciliationProvider(Protocol):
    async def reconcile(
        self,
        action: ActionContract,
        *,
        last_observation: ObservationLease | None,
        timeout_seconds: float,
    ) -> VerificationResult: ...


class InteractionTelemetrySink(Protocol):
    async def record(self, telemetry: InteractionTelemetry) -> None: ...


class TerminalCommandProvider(Protocol):
    """Future-safe command boundary; intentionally no arbitrary-code implementation here."""

    async def execute(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        approved: bool,
        correlation_id: str,
    ) -> tuple[int, str, str]: ...


class FilesystemProvider(Protocol):
    """Future filesystem boundary; destructive methods require explicit policy upstream."""

    async def list_entries(self, path: str, *, limit: int = 256) -> Sequence[dict[str, str]]: ...

    async def read_text(self, path: str, *, max_bytes: int = 1_048_576) -> str: ...


class ComputerAdapterError(RuntimeError):
    """Adapter error with a stable, safe failure category."""

    def __init__(
        self,
        code: ComputerFailureCode,
        message: str,
        *,
        retryable: bool = False,
        source: PerceptionSource | None = None,
    ) -> None:
        if not isinstance(code, ComputerFailureCode):
            raise ValueError("code must be a ComputerFailureCode")
        if not message or len(message) > 512:
            raise ValueError("adapter error message must be non-empty and bounded")
        self.code = code
        self.retryable = retryable
        self.source = source
        super().__init__(message)
