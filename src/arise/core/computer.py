"""Platform-neutral contracts for grounded, observable computer interaction.

These types describe observations and proposals only. They do not call an OS,
input device, browser, OCR engine, or model; adapters live outside ``core``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from arise.core.contracts import (
    ActionContract,
    ObservationLease,
    TargetIdentity,
    validate_safe_token,
)


class PerceptionSource(StrEnum):
    APPLICATION_API = "application_api"
    BROWSER_DOM = "browser_dom"
    UI_AUTOMATION = "ui_automation"
    ACCESSIBILITY = "accessibility"
    OCR_LAYOUT = "ocr_layout"
    VISION = "vision"
    COORDINATE = "coordinate"

    @property
    def priority(self) -> int:
        """Lower values are more deterministic and preferred for grounding."""

        return {
            PerceptionSource.APPLICATION_API: 0,
            PerceptionSource.BROWSER_DOM: 1,
            PerceptionSource.UI_AUTOMATION: 2,
            PerceptionSource.ACCESSIBILITY: 3,
            PerceptionSource.OCR_LAYOUT: 4,
            PerceptionSource.VISION: 5,
            PerceptionSource.COORDINATE: 6,
        }[self]


class CoordinateSpace(StrEnum):
    """Coordinate systems are never interchangeable without an explicit transform."""

    PHYSICAL_DESKTOP = "physical_desktop"
    MONITOR_LOGICAL = "monitor_logical"
    WINDOW_CLIENT = "window_client"
    BROWSER_VIEWPORT_CSS = "browser_viewport_css"
    IMAGE_PIXELS = "image_pixels"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class ComputerActionKind(StrEnum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    TYPE_TEXT = "type_text"
    KEY_PRESS = "key_press"
    HOTKEY = "hotkey"
    SCROLL = "scroll"
    DRAG = "drag"
    SELECT = "select"
    FOCUS = "focus"
    OPEN = "open"
    CLOSE = "close"
    NAVIGATE = "navigate"
    WAIT = "wait"
    EXTRACT = "extract"
    OBSERVE = "observe"


class ComputerFailureCode(StrEnum):
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    TARGET_STALE = "TARGET_STALE"
    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
    APPLICATION_NOT_FOUND = "APPLICATION_NOT_FOUND"
    APPLICATION_AMBIGUOUS = "APPLICATION_AMBIGUOUS"
    ACTIVATION_FAILED = "ACTIVATION_FAILED"
    OWNERSHIP_VERIFICATION_FAILED = "OWNERSHIP_VERIFICATION_FAILED"
    UIA_NOT_AVAILABLE = "UIA_NOT_AVAILABLE"
    ACTION_VERIFICATION_FAILED = "ACTION_VERIFICATION_FAILED"
    BROWSER_NOT_FOUND = "BROWSER_NOT_FOUND"
    ELEMENT_NOT_INTERACTABLE = "ELEMENT_NOT_INTERACTABLE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TIMEOUT = "TIMEOUT"
    USER_INTERFERENCE = "USER_INTERFERENCE"
    ENVIRONMENT_CHANGED = "ENVIRONMENT_CHANGED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    ACTION_UNKNOWN_OUTCOME = "ACTION_UNKNOWN_OUTCOME"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    POLICY_DENIED = "POLICY_DENIED"
    CANCELLED = "CANCELLED"
    INVALID_COORDINATE = "INVALID_COORDINATE"
    INVALID_TARGET = "INVALID_TARGET"
    INTERNAL_ADAPTER_ERROR = "INTERNAL_ADAPTER_ERROR"
    FOCUS_FAILED = "FOCUS_FAILED"
    LAUNCH_MODE_UNSUPPORTED = "LAUNCH_MODE_UNSUPPORTED"


class ReconciliationStatus(StrEnum):
    RESOLVED_SUCCEEDED = "resolved_succeeded"
    RESOLVED_FAILED = "resolved_failed"
    STILL_UNKNOWN = "still_unknown"


class SelectorQuality(StrEnum):
    EXACT_ACCESSIBLE_ROLE_NAME = "exact_accessible_role_name"
    LABEL = "label"
    PLACEHOLDER = "placeholder"
    TEST_ID = "test_id"
    EXACT_TEXT = "exact_text"
    STABLE_ATTRIBUTE = "stable_attribute"
    STRUCTURAL = "structural"
    VISUAL = "visual"
    COORDINATE = "coordinate"

    @property
    def score(self) -> float:
        return {
            SelectorQuality.EXACT_ACCESSIBLE_ROLE_NAME: 1.0,
            SelectorQuality.LABEL: 0.96,
            SelectorQuality.PLACEHOLDER: 0.92,
            SelectorQuality.TEST_ID: 0.90,
            SelectorQuality.EXACT_TEXT: 0.86,
            SelectorQuality.STABLE_ATTRIBUTE: 0.82,
            SelectorQuality.STRUCTURAL: 0.62,
            SelectorQuality.VISUAL: 0.55,
            SelectorQuality.COORDINATE: 0.35,
        }[self]


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise ValueError("point coordinates must be finite")


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis-aligned rectangle; x/y may be negative in virtual desktop space."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.x, self.y, self.width, self.height)):
            raise ValueError("rectangle coordinates must be finite")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("rectangle width and height must be positive")

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center(self) -> Point:
        return Point(self.x + self.width / 2, self.y + self.height / 2)

    def contains(self, point: Point, *, margin: float = 0.0) -> bool:
        return (
            self.x + margin <= point.x <= self.right - margin
            and self.y + margin <= point.y <= self.bottom - margin
        )

    def intersects(self, other: Rect) -> bool:
        return (
            self.x < other.right
            and self.right > other.x
            and self.y < other.bottom
            and self.bottom > other.y
        )

    def inset(self, fraction: float = 0.1) -> Rect:
        if not 0 <= fraction < 0.5:
            raise ValueError("inset fraction must be in [0, 0.5)")
        dx = self.width * fraction
        dy = self.height * fraction
        return Rect(self.x + dx, self.y + dy, self.width - 2 * dx, self.height - 2 * dy)


@dataclass(frozen=True, slots=True)
class DisplayGeometry:
    """Monitor geometry in physical virtual-desktop pixels; negative origins are valid."""

    display_id: str
    physical_bounds: Rect
    dpi_x: float
    dpi_y: float
    primary: bool
    dpi_available: bool = True
    dpi_source: str = "monitor_api"

    def __post_init__(self) -> None:
        if not self.display_id.strip() or len(self.display_id) > 256:
            raise ValueError("display_id must be non-empty and bounded")
        if not math.isfinite(self.dpi_x) or not math.isfinite(self.dpi_y):
            raise ValueError("display DPI values must be finite")
        if self.dpi_x <= 0 or self.dpi_y <= 0:
            raise ValueError("display DPI values must be positive")
        if not self.dpi_source.strip() or len(self.dpi_source) > 128:
            raise ValueError("dpi_source must be bounded")

    @property
    def scale_x(self) -> float:
        if not self.dpi_available:
            raise ValueError("monitor DPI was not measured; coordinate conversion is unsafe")
        return self.dpi_x / 96.0

    @property
    def scale_y(self) -> float:
        if not self.dpi_available:
            raise ValueError("monitor DPI was not measured; coordinate conversion is unsafe")
        return self.dpi_y / 96.0


class CoordinateMapper:
    """Explicit conversions to/from physical virtual-desktop pixel coordinates."""

    @staticmethod
    def monitor_logical_to_physical(point: Point, display: DisplayGeometry) -> Point:
        return Point(
            display.physical_bounds.x + point.x * display.scale_x,
            display.physical_bounds.y + point.y * display.scale_y,
        )

    @staticmethod
    def physical_to_monitor_logical(point: Point, display: DisplayGeometry) -> Point:
        return Point(
            (point.x - display.physical_bounds.x) / display.scale_x,
            (point.y - display.physical_bounds.y) / display.scale_y,
        )

    @staticmethod
    def window_client_to_physical(point: Point, client_origin: Point) -> Point:
        return Point(client_origin.x + point.x, client_origin.y + point.y)

    @staticmethod
    def browser_viewport_to_physical(
        point_css: Point,
        *,
        viewport_origin: Point,
        device_scale_factor: float,
    ) -> Point:
        if not math.isfinite(device_scale_factor) or device_scale_factor <= 0:
            raise ValueError("device_scale_factor must be positive and finite")
        return Point(
            viewport_origin.x + point_css.x * device_scale_factor,
            viewport_origin.y + point_css.y * device_scale_factor,
        )

    @staticmethod
    def image_to_physical(
        point: Point,
        *,
        image_width: int,
        image_height: int,
        capture_bounds: Rect,
    ) -> Point:
        if image_width <= 0 or image_height <= 0:
            raise ValueError("image dimensions must be positive")
        if not (0 <= point.x <= image_width and 0 <= point.y <= image_height):
            raise ValueError("image point is outside the captured image")
        return Point(
            capture_bounds.x + point.x * capture_bounds.width / image_width,
            capture_bounds.y + point.y * capture_bounds.height / image_height,
        )

    @staticmethod
    def physical_to_image(
        point: Point,
        *,
        image_width: int,
        image_height: int,
        capture_bounds: Rect,
    ) -> Point:
        if image_width <= 0 or image_height <= 0:
            raise ValueError("image dimensions must be positive")
        if not capture_bounds.contains(point):
            raise ValueError("physical point is outside the captured bounds")
        return Point(
            (point.x - capture_bounds.x) * image_width / capture_bounds.width,
            (point.y - capture_bounds.y) * image_height / capture_bounds.height,
        )

    @staticmethod
    def safe_click_point(
        bounds: Rect,
        *,
        unsafe_regions: tuple[Rect, ...] = (),
        edge_fraction: float = 0.12,
    ) -> Point:
        """Choose a point inside a validated target, avoiding edges and known hazards.

        The target still must be re-resolved and revalidated immediately before
        dispatch. This geometric helper grants no authority to click.
        """

        safe = bounds.inset(edge_fraction)
        fractions = (0.2, 0.35, 0.5, 0.65, 0.8)
        candidates = [
            Point(safe.x + safe.width * x_fraction, safe.y + safe.height * y_fraction)
            for x_fraction in fractions
            for y_fraction in fractions
        ]
        safe_candidates = [
            point
            for point in candidates
            if not any(region.contains(point) for region in unsafe_regions)
        ]
        if not safe_candidates:
            raise ValueError("target has no safe interior point outside unsafe regions")
        if not unsafe_regions:
            return safe.center

        def clearance(point: Point) -> float:
            distances: list[float] = []
            for region in unsafe_regions:
                dx = max(region.x - point.x, 0.0, point.x - region.right)
                dy = max(region.y - point.y, 0.0, point.y - region.bottom)
                distances.append(math.hypot(dx, dy))
            return min(distances, default=0.0)

        return max(safe_candidates, key=clearance)


@dataclass(frozen=True, slots=True)
class TargetDescriptor:
    """Ephemeral perception metadata associated with a stable domain identity."""

    identity: TargetIdentity
    source: PerceptionSource
    observed_at: datetime
    observation_id: str
    bounds: Rect | None = None
    coordinate_space: CoordinateSpace | None = None
    selector_quality: SelectorQuality | None = None
    visible: bool = True
    enabled: bool = True
    automation_id: str | None = None
    runtime_id: tuple[str | int, ...] = ()
    hierarchy: tuple[str, ...] = ()
    class_name: str | None = None
    framework_id: str | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("target observation time must be timezone-aware")
        validate_safe_token(self.observation_id, "observation_id")
        if self.bounds is not None and self.coordinate_space is None:
            raise ValueError("target bounds require an explicit coordinate space")
        if len(self.runtime_id) > 32 or any(
            not isinstance(item, (str, int)) or len(str(item)) > 128 for item in self.runtime_id
        ):
            raise ValueError("runtime_id must contain at most 32 bounded components")
        if len(self.hierarchy) > 64 or any(not item or len(item) > 256 for item in self.hierarchy):
            raise ValueError("target hierarchy must contain at most 64 bounded labels")


@dataclass(frozen=True, slots=True)
class TargetQuery:
    semantic_name: str
    role: str | None = None
    application: str | None = None
    process_id: int | None = None
    window_id: str | None = None
    page_id: str | None = None
    allowed_sources: tuple[PerceptionSource, ...] = tuple(PerceptionSource)
    allow_coordinate_fallback: bool = False
    minimum_confidence: float = 0.72
    ambiguity_margin: float = 0.08
    max_candidates: int = 32

    def __post_init__(self) -> None:
        if not isinstance(self.semantic_name, str) or len(self.semantic_name) > 512:
            raise ValueError("semantic_name must be bounded text")
        if self.role is not None and (not self.role.strip() or len(self.role) > 128):
            raise ValueError("role must be non-empty bounded text when supplied")
        if self.process_id is not None and self.process_id <= 0:
            raise ValueError("process_id must be positive")
        if not 0 <= self.minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be in [0, 1]")
        if not 0 <= self.ambiguity_margin <= 1:
            raise ValueError("ambiguity_margin must be in [0, 1]")
        if not 1 <= self.max_candidates <= 256:
            raise ValueError("max_candidates must be between 1 and 256")
        if not self.allowed_sources or any(
            not isinstance(source, PerceptionSource) for source in self.allowed_sources
        ):
            raise ValueError("allowed_sources must contain perception source values")
        object.__setattr__(self, "allowed_sources", tuple(dict.fromkeys(self.allowed_sources)))


@dataclass(frozen=True, slots=True)
class TargetCandidate:
    descriptor: TargetDescriptor
    confidence: float
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("candidate confidence must be in [0, 1]")
        if len(self.evidence) > 16 or any(not item or len(item) > 256 for item in self.evidence):
            raise ValueError("candidate evidence must contain at most 16 bounded entries")


@dataclass(frozen=True, slots=True)
class TargetResolution:
    status: ResolutionStatus
    candidates: tuple[TargetCandidate, ...]
    selected: TargetCandidate | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if len(self.candidates) > 256:
            raise ValueError("target resolution exceeds the candidate limit")
        if self.status is ResolutionStatus.RESOLVED:
            if self.selected is None or self.selected not in self.candidates:
                raise ValueError("resolved result must select one of its candidates")
        elif self.selected is not None:
            raise ValueError("ambiguous/not-found results cannot select a target")
        if self.status is ResolutionStatus.AMBIGUOUS and len(self.candidates) < 2:
            raise ValueError("ambiguous result requires at least two candidates")
        if self.status is ResolutionStatus.NOT_FOUND and self.candidates:
            raise ValueError("not-found result cannot contain candidates")
        if len(self.reason) > 1024:
            raise ValueError("resolution reason exceeds the size limit")


@dataclass(frozen=True, slots=True)
class DesktopEnvironmentSnapshot:
    """Detailed, explicit computer inventory for authenticated developer inspection."""

    snapshot_id: str
    captured_at: datetime
    operating_system: str
    version: str
    build: str | None
    architecture: str
    session_id: int | None
    displays: tuple[DisplayGeometry, ...]
    windows: tuple[WindowRecord, ...]
    foreground_window_id: str | None
    cursor_position: Point | None
    running_applications: tuple[RunningApplication, ...]
    installed_applications: tuple[RunningApplication, ...] = ()
    browsers: tuple[str, ...] = ()
    terminals: tuple[str, ...] = ()
    ide_names: tuple[str, ...] = ()
    available_features: tuple[str, ...] = ()
    unavailable_features: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        validate_safe_token(self.snapshot_id, "snapshot_id")
        if self.captured_at.tzinfo is None:
            raise ValueError("environment snapshot time must be timezone-aware")
        if not self.operating_system.strip() or not self.version.strip():
            raise ValueError("environment OS and version are required")
        if self.session_id is not None and self.session_id < 0:
            raise ValueError("session_id cannot be negative")
        for collection, limit, label in (
            (self.displays, 32, "displays"),
            (self.windows, 2048, "windows"),
            (self.running_applications, 2048, "running applications"),
            (self.installed_applications, 4096, "installed applications"),
        ):
            if len(collection) > limit:
                raise ValueError(f"environment {label} exceeds the result limit")
        for values, label in (
            (self.browsers, "browsers"),
            (self.terminals, "terminals"),
            (self.ide_names, "ide_names"),
            (self.available_features, "available_features"),
        ):
            if len(values) > 128 or any(not value or len(value) > 256 for value in values):
                raise ValueError(f"environment {label} exceeds the result limit")
        if len(self.unavailable_features) > 128:
            raise ValueError("unavailable feature count exceeds the limit")


@dataclass(frozen=True, slots=True)
class AccessibilityElement:
    target: TargetDescriptor
    control_type: str
    name: str
    value: str | None = None
    enabled: bool = True
    visible: bool = True
    focused: bool = False
    selected: bool | None = None
    expanded: bool | None = None
    toggle_state: str | None = None
    supported_patterns: tuple[str, ...] = ()
    parent_fingerprint: str | None = None
    child_count: int = 0
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.control_type or len(self.control_type) > 128:
            raise ValueError("control_type must be non-empty and bounded")
        if len(self.name) > 1024 or (self.value is not None and len(self.value) > 4096):
            raise ValueError("accessibility name/value exceeds the size limit")
        if len(self.supported_patterns) > 64:
            raise ValueError("supported_patterns exceeds the limit")
        if self.child_count < 0:
            raise ValueError("child_count cannot be negative")

    def safe_summary(self) -> dict[str, Any]:
        """Return diagnostics without exposing a sensitive control value."""

        return {
            "target_fingerprint": self.target.identity.fingerprint,
            "source": self.target.source.value,
            "control_type": self.control_type,
            "name": self.name,
            "value": None if self.sensitive else self.value,
            "sensitive": self.sensitive,
            "enabled": self.enabled,
            "visible": self.visible,
            "focused": self.focused,
            "selected": self.selected,
            "expanded": self.expanded,
            "toggle_state": self.toggle_state,
            "supported_patterns": list(self.supported_patterns),
            "parent_fingerprint": self.parent_fingerprint,
            "child_count": self.child_count,
        }


@dataclass(frozen=True, slots=True)
class CapturedImage:
    """Ephemeral image payload; adapters should keep it in memory and redact before sharing."""

    content: bytes
    content_type: str
    width: int
    height: int
    bounds: Rect
    captured_at: datetime
    sha256: str
    display_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes) or not 1 <= len(self.content) <= 8 * 1024 * 1024:
            raise ValueError("captured image must be between 1 byte and 8 MiB")
        if self.content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("captured image content type is unsupported")
        if not 1 <= self.width <= 16_384 or not 1 <= self.height <= 16_384:
            raise ValueError("captured image dimensions exceed the supported range")
        if self.captured_at.tzinfo is None:
            raise ValueError("capture timestamp must be timezone-aware")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", self.sha256):
            raise ValueError("captured image sha256 must be a 64-character hex digest")
        import hashlib

        if hashlib.sha256(self.content).hexdigest() != self.sha256.lower():
            raise ValueError("captured image sha256 does not match its bytes")


@dataclass(frozen=True, slots=True)
class RunningApplication:
    process_id: int
    name: str
    executable_path: str | None = None
    window_ids: tuple[str, ...] = ()
    package_family_name: str | None = None

    def __post_init__(self) -> None:
        if self.process_id <= 0 or not self.name.strip():
            raise ValueError("running application identity is invalid")
        if self.executable_path is not None and len(self.executable_path) > 4096:
            raise ValueError("executable path exceeds the size limit")
        if self.package_family_name is not None and len(self.package_family_name) > 256:
            raise ValueError("package family name exceeds the size limit")
        if len(self.window_ids) > 256:
            raise ValueError("application window count exceeds the limit")


@dataclass(frozen=True, slots=True)
class InstalledApplication:
    """Safe catalog summary for an installed app; contains no launch path."""

    identity: str
    name: str
    source: str
    activation_method: str

    def __post_init__(self) -> None:
        if not self.identity or len(self.identity) > 128:
            raise ValueError("installed application identity must be bounded")
        if not self.name.strip() or len(self.name) > 256:
            raise ValueError("installed application name must be bounded")
        if not self.source or len(self.source) > 64:
            raise ValueError("installed application source must be bounded")
        if not self.activation_method or len(self.activation_method) > 64:
            raise ValueError("installed application activation method must be bounded")


@dataclass(frozen=True, slots=True)
class WindowRecord:
    window_id: str
    process_id: int | None
    title: str
    application: str | None
    visible: bool
    minimized: bool
    maximized: bool
    foreground: bool
    bounds: Rect | None = None
    class_name: str | None = None
    executable_path: str | None = None
    package_family_name: str | None = None
    aumid: str | None = None

    def __post_init__(self) -> None:
        if not self.window_id.strip() or len(self.window_id) > 256:
            raise ValueError("window_id must be non-empty and bounded")
        if self.process_id is not None and self.process_id <= 0:
            raise ValueError("window process_id must be positive")
        if len(self.title) > 2048:
            raise ValueError("window title exceeds the size limit")
        if self.executable_path is not None and len(self.executable_path) > 4096:
            raise ValueError("window executable path exceeds the size limit")
        if self.package_family_name is not None and len(self.package_family_name) > 256:
            raise ValueError("window package family name exceeds the size limit")
        if self.aumid is not None and len(self.aumid) > 512:
            raise ValueError("window AUMID exceeds the size limit")


class FocusStrategy(StrEnum):
    """Which Windows activation route finally produced (or failed) foreground state."""

    NONE = "none"
    SET_FOREGROUND = "set_foreground"
    ATTACH_THREAD_INPUT = "attach_thread_input"
    ALT_NUDGE = "alt_nudge"


@dataclass(frozen=True, slots=True)
class WindowFocusEvidence:
    """Separable focus states; a focus request is never accepted as its own proof.

    ``window_exists``, ``window_visible``, ``foreground_window_id``,
    ``focus_requested`` and ``focus_verified`` are distinct facts. Only a fresh
    observation of the foreground window can set ``focus_verified``; an adapter
    that cannot observe it must leave the value ``False`` rather than infer it
    from a successful activation call.
    """

    window_id: str
    window_exists: bool = False
    window_visible: bool = False
    window_minimized: bool = False
    focus_requested: bool = False
    focus_error: str | None = None
    focus_strategy: FocusStrategy = FocusStrategy.NONE
    foreground_window_id: str | None = None
    foreground_process_id: int | None = None
    target_process_id: int | None = None
    attempts: int = 0

    def __post_init__(self) -> None:
        if not self.window_id.strip() or len(self.window_id) > 256:
            raise ValueError("window_id must be non-empty and bounded")
        if self.focus_error is not None and (
            not self.focus_error.strip() or len(self.focus_error) > 64
        ):
            raise ValueError("focus error code must be bounded")
        if not isinstance(self.focus_strategy, FocusStrategy):
            raise ValueError("focus_strategy must be a FocusStrategy")
        if self.attempts < 0 or self.attempts > 4096:
            raise ValueError("focus attempt count is out of range")

    @property
    def focus_verified(self) -> bool:
        """True only when a fresh observation saw this exact window in foreground."""

        return (
            self.focus_requested
            and self.focus_error is None
            and self.foreground_window_id is not None
            and self.foreground_window_id == self.window_id
        )

    @property
    def owner_matches_foreground(self) -> bool:
        """True when the foreground window belongs to the target's owning process.

        Chromium-style applications can present the intended top-level window
        from a different process than the one that owns a sibling window, so this
        is a weaker signal than :attr:`focus_verified` and is reported separately.
        """

        return (
            self.foreground_process_id is not None
            and self.target_process_id is not None
            and self.foreground_process_id == self.target_process_id
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "window_exists": self.window_exists,
            "window_visible": self.window_visible,
            "window_minimized": self.window_minimized,
            "focus_requested": self.focus_requested,
            "focus_verified": self.focus_verified,
            "focus_error": self.focus_error,
            "focus_strategy": self.focus_strategy.value,
            "foreground_window_id": self.foreground_window_id,
            "foreground_process_id": self.foreground_process_id,
            "target_process_id": self.target_process_id,
            "owner_matches_foreground": self.owner_matches_foreground,
            "attempts": self.attempts,
        }


@dataclass(frozen=True, slots=True)
class BrowserTabRecord:
    page_id: str
    browser: str
    title: str
    url: str
    active: bool
    profile_id: str
    process_id: int | None = None

    def __post_init__(self) -> None:
        for value, label in ((self.page_id, "page_id"), (self.profile_id, "profile_id")):
            validate_safe_token(value, label)
        if not self.browser.strip() or len(self.browser) > 128:
            raise ValueError("browser name must be bounded")
        if len(self.title) > 2048 or len(self.url) > 8192:
            raise ValueError("browser metadata exceeds the size limit")
        if self.process_id is not None and self.process_id <= 0:
            raise ValueError("browser process_id must be positive")


@dataclass(frozen=True, slots=True)
class OCRText:
    text: str
    bounds: Rect
    confidence: float
    language: str | None
    source: PerceptionSource = PerceptionSource.OCR_LAYOUT

    def __post_init__(self) -> None:
        if not self.text or len(self.text) > 4096:
            raise ValueError("OCR text must be non-empty and bounded")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("OCR confidence must be in [0, 1]")
        if self.source is not PerceptionSource.OCR_LAYOUT:
            raise ValueError("OCRText source must be OCR_LAYOUT")
        if self.language is not None and len(self.language) > 32:
            raise ValueError("OCR language tag exceeds the size limit")


@dataclass(frozen=True, slots=True)
class GroundingProposal:
    """Untrusted visual proposal; it never authorizes input or carries a click command."""

    target_description: str
    bounds: Rect
    confidence: float
    evidence: tuple[str, ...]
    observation_id: str
    source: PerceptionSource = PerceptionSource.VISION

    def __post_init__(self) -> None:
        if not self.target_description.strip() or len(self.target_description) > 512:
            raise ValueError("grounding description must be bounded text")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("grounding confidence must be in [0, 1]")
        if len(self.evidence) > 16 or any(not item or len(item) > 256 for item in self.evidence):
            raise ValueError("grounding evidence must be bounded")
        validate_safe_token(self.observation_id, "observation_id")
        if self.source is not PerceptionSource.VISION:
            raise ValueError("grounding proposals must be tagged as vision output")


@dataclass(frozen=True, slots=True)
class ComputerObservation:
    lease: ObservationLease
    targets: tuple[TargetCandidate, ...] = ()
    accessibility: tuple[AccessibilityElement, ...] = ()
    ocr: tuple[OCRText, ...] = ()
    screenshot_hash: str | None = None
    display_topology_hash: str | None = None

    def __post_init__(self) -> None:
        if len(self.targets) > 256:
            raise ValueError("observation target count exceeds the limit")
        if len(self.accessibility) > 4096:
            raise ValueError("accessibility tree exceeds the node limit")
        if len(self.ocr) > 4096:
            raise ValueError("OCR result count exceeds the limit")
        for value in (self.screenshot_hash, self.display_topology_hash):
            if value is not None and not re.fullmatch(r"[a-fA-F0-9]{16,128}", value):
                raise ValueError("observation hashes must be bounded hexadecimal values")


@dataclass(frozen=True, slots=True)
class EnvironmentFingerprint:
    foreground_window_id: str | None = None
    process_id: int | None = None
    browser_page_id: str | None = None
    browser_url_hash: str | None = None
    display_topology_hash: str | None = None
    accessibility_hash: str | None = None
    screenshot_hash: str | None = None
    cursor_position: Point | None = None

    @property
    def digest(self) -> str:
        import hashlib

        raw = "|".join(
            (
                self.foreground_window_id or "",
                str(self.process_id or ""),
                self.browser_page_id or "",
                self.browser_url_hash or "",
                self.display_topology_hash or "",
                self.accessibility_hash or "",
                self.screenshot_hash or "",
                (
                    ""
                    if self.cursor_position is None
                    else f"{self.cursor_position.x},{self.cursor_position.y}"
                ),
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HumanInterference:
    reason: str
    detected_at: datetime
    previous: EnvironmentFingerprint
    current: EnvironmentFingerprint

    def __post_init__(self) -> None:
        if not self.reason.strip() or len(self.reason) > 256:
            raise ValueError("interference reason must be bounded text")
        if self.detected_at.tzinfo is None:
            raise ValueError("interference timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    status: ReconciliationStatus
    action_id: str
    observation_id: str | None
    evidence_summary: str
    evidence_hash: str | None = None

    def __post_init__(self) -> None:
        validate_safe_token(self.action_id, "action_id")
        if self.observation_id is not None:
            validate_safe_token(self.observation_id, "observation_id")
        if len(self.evidence_summary) > 2048:
            raise ValueError("reconciliation evidence summary exceeds the size limit")
        if self.evidence_hash is not None and not re.fullmatch(
            r"[a-fA-F0-9]{16,128}", self.evidence_hash
        ):
            raise ValueError("reconciliation evidence_hash must be bounded hexadecimal")


@dataclass(frozen=True, slots=True)
class InteractionTelemetry:
    task_id: str
    action_id: str
    request_id: str
    adapter: str
    operation: str
    target_source: PerceptionSource | None
    resolution_method: str | None
    latency_ms: float
    success: bool
    verification_status: str | None
    failure_code: ComputerFailureCode | None
    retry_count: int = 0

    def __post_init__(self) -> None:
        for value, label in (
            (self.task_id, "task_id"),
            (self.action_id, "action_id"),
            (self.request_id, "request_id"),
            (self.adapter, "adapter"),
            (self.operation, "operation"),
        ):
            validate_safe_token(value, label)
        if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
            raise ValueError("latency_ms must be finite and non-negative")
        if self.retry_count < 0 or self.retry_count > 100:
            raise ValueError("retry_count must be between zero and one hundred")
        if self.verification_status is not None and len(self.verification_status) > 32:
            raise ValueError("verification_status is too long")


@dataclass(frozen=True, slots=True)
class ComputerActionSpec:
    """Computer operation metadata layered over the existing governed action contract."""

    kind: ComputerActionKind
    contract: ActionContract
    request_id: str
    provenance: str

    def __post_init__(self) -> None:
        if not isinstance(self.contract, ActionContract):
            raise ValueError("computer actions must wrap an ActionContract")
        validate_safe_token(self.request_id, "request_id")
        if not self.provenance.strip() or len(self.provenance) > 256:
            raise ValueError("action provenance must be bounded text")
        if (
            self.kind
            not in {
                ComputerActionKind.OPEN,
                ComputerActionKind.NAVIGATE,
                ComputerActionKind.WAIT,
                ComputerActionKind.OBSERVE,
            }
            and self.contract.target is None
        ):
            raise ValueError("this computer action requires a grounded target identity")

    @property
    def action_id(self) -> str:
        return self.contract.action_id

    @property
    def task_id(self) -> str:
        return self.contract.task_id

    @property
    def target(self) -> TargetIdentity | None:
        return self.contract.target
