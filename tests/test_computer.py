from __future__ import annotations

import unittest
import uuid
from datetime import UTC, datetime, timedelta

from arise.core.computer import (
    ComputerActionKind,
    ComputerActionSpec,
    ComputerObservation,
    CoordinateMapper,
    CoordinateSpace,
    DisplayGeometry,
    EnvironmentFingerprint,
    PerceptionSource,
    Point,
    Rect,
    ResolutionStatus,
    SelectorQuality,
    TargetCandidate,
    TargetDescriptor,
    TargetQuery,
)
from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    EvidenceSource,
    ObservationLease,
    RiskLevel,
    TargetIdentity,
    TrustLevel,
)
from arise.core.grounding import TargetResolver
from arise.core.observation import (
    EnvironmentChangeDetector,
    InvalidationReason,
    ObservationCache,
)


def candidate(
    name: str,
    *,
    source: PerceptionSource = PerceptionSource.BROWSER_DOM,
    stable_id: str | None = None,
    role: str = "button",
    confidence: float = 0.98,
    quality: SelectorQuality = SelectorQuality.EXACT_ACCESSIBLE_ROLE_NAME,
    bounds: Rect | None = None,
    visible: bool = True,
    enabled: bool = True,
) -> TargetCandidate:
    observed = datetime.now(UTC)
    observation_id = str(uuid.uuid4())
    identity = TargetIdentity(
        platform="browser" if source is PerceptionSource.BROWSER_DOM else "windows",
        application="sample-app",
        process_id=1234,
        window_id="window-1",
        page_id="page-1" if source is PerceptionSource.BROWSER_DOM else None,
        object_id=stable_id,
        stable_id=stable_id,
        role=role,
        semantic_name=name,
    )
    descriptor = TargetDescriptor(
        identity=identity,
        source=source,
        observed_at=observed,
        observation_id=observation_id,
        bounds=bounds,
        coordinate_space=CoordinateSpace.BROWSER_VIEWPORT_CSS if bounds else None,
        selector_quality=quality,
        visible=visible,
        enabled=enabled,
        automation_id=stable_id,
    )
    return TargetCandidate(descriptor, confidence, evidence=("role and accessible name",))


def lease(*, lease_id: str | None = None, deadline: float = 105.0) -> ObservationLease:
    created = datetime.now(UTC)
    return ObservationLease(
        lease_id=lease_id or str(uuid.uuid4()),
        target_fingerprint="target-fingerprint",
        state_hash="state-hash",
        created_at=created,
        expires_at=created + timedelta(seconds=5),
        monotonic_deadline=deadline,
        facts={"window.foreground": True},
        source=EvidenceSource.OBSERVED,
    )


class CoordinateMappingTests(unittest.TestCase):
    def test_negative_origin_and_non_100_percent_dpi_round_trip(self) -> None:
        display = DisplayGeometry(
            display_id="left-monitor",
            physical_bounds=Rect(-1920, -120, 1920, 1080),
            dpi_x=144,
            dpi_y=120,
            primary=False,
        )
        physical = CoordinateMapper.monitor_logical_to_physical(Point(120, 72), display)
        self.assertEqual(physical, Point(-1740, -30))
        self.assertEqual(
            CoordinateMapper.physical_to_monitor_logical(physical, display), Point(120, 72)
        )

    def test_browser_and_capture_coordinates_require_explicit_transforms(self) -> None:
        screen = CoordinateMapper.browser_viewport_to_physical(
            Point(10, 20), viewport_origin=Point(-800, 100), device_scale_factor=2
        )
        self.assertEqual(screen, Point(-780, 140))
        capture = Rect(-100, 50, 400, 200)
        physical = CoordinateMapper.image_to_physical(
            Point(100, 50), image_width=200, image_height=100, capture_bounds=capture
        )
        self.assertEqual(physical, Point(100, 150))
        self.assertEqual(
            CoordinateMapper.physical_to_image(
                physical, image_width=200, image_height=100, capture_bounds=capture
            ),
            Point(100, 50),
        )
        with self.assertRaises(ValueError):
            CoordinateMapper.image_to_physical(
                Point(201, 50), image_width=200, image_height=100, capture_bounds=capture
            )

    def test_safe_click_point_stays_inside_target_and_avoids_known_unsafe_region(self) -> None:
        target = Rect(10, 20, 100, 40)
        unsafe = Rect(48, 32, 24, 16)
        point = CoordinateMapper.safe_click_point(target, unsafe_regions=(unsafe,))
        self.assertTrue(target.contains(point, margin=4))
        self.assertFalse(unsafe.contains(point))
        with self.assertRaises(ValueError):
            CoordinateMapper.safe_click_point(target, unsafe_regions=(target,))

    def test_rectangles_reject_non_finite_or_degenerate_geometry(self) -> None:
        with self.assertRaises(ValueError):
            Rect(0, 0, 0, 10)
        with self.assertRaises(ValueError):
            Point(float("nan"), 0)

    def test_unknown_monitor_dpi_fails_closed_for_coordinate_conversion(self) -> None:
        display = DisplayGeometry(
            display_id="unknown-dpi",
            physical_bounds=Rect(0, 0, 1920, 1080),
            dpi_x=96,
            dpi_y=96,
            primary=True,
            dpi_available=False,
            dpi_source="unavailable",
        )
        with self.assertRaisesRegex(ValueError, "DPI was not measured"):
            CoordinateMapper.monitor_logical_to_physical(Point(100, 100), display)
        with self.assertRaisesRegex(ValueError, "DPI was not measured"):
            CoordinateMapper.physical_to_monitor_logical(Point(100, 100), display)


class TargetResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = TargetResolver()
        self.query = TargetQuery(semantic_name="Sign In", role="button", application="sample-app")

    def test_ambiguous_semantic_targets_are_never_selected_by_array_order(self) -> None:
        first = candidate("Sign In", stable_id="signin-header")
        second = candidate("Sign In", stable_id="signin-modal")
        result = self.resolver.resolve(self.query, [first, second])
        self.assertIs(result.status, ResolutionStatus.AMBIGUOUS)
        self.assertIsNone(result.selected)
        self.assertEqual(len(result.candidates), 2)

    def test_hidden_or_disabled_targets_are_never_resolved(self) -> None:
        hidden = candidate("Sign In", stable_id="hidden", visible=False)
        disabled = candidate("Sign In", stable_id="disabled", enabled=False)
        result = self.resolver.resolve(self.query, [hidden, disabled])
        self.assertIs(result.status, ResolutionStatus.NOT_FOUND)
        self.assertIsNone(result.selected)

    def test_perception_hierarchy_prefers_dom_over_higher_confidence_vision(self) -> None:
        dom = candidate("Sign In", stable_id="signin", source=PerceptionSource.BROWSER_DOM)
        vision = candidate(
            "Sign In",
            stable_id="visual-signin",
            source=PerceptionSource.VISION,
            confidence=1.0,
            quality=SelectorQuality.VISUAL,
        )
        result = self.resolver.resolve(self.query, [vision, dom])
        self.assertIs(result.status, ResolutionStatus.RESOLVED)
        self.assertIsNotNone(result.selected)
        self.assertIs(result.selected.descriptor.source, PerceptionSource.BROWSER_DOM)

    def test_coordinate_fallback_requires_explicit_opt_in(self) -> None:
        coordinate = candidate(
            "Sign In",
            stable_id="visual-box",
            source=PerceptionSource.COORDINATE,
            quality=SelectorQuality.COORDINATE,
            bounds=Rect(10, 10, 100, 40),
        )
        self.assertIs(
            self.resolver.resolve(self.query, [coordinate]).status,
            ResolutionStatus.NOT_FOUND,
        )
        enabled = TargetQuery(
            semantic_name="Sign In",
            role="button",
            application="sample-app",
            allowed_sources=(PerceptionSource.COORDINATE,),
            allow_coordinate_fallback=True,
            minimum_confidence=0.7,
        )
        self.assertIs(
            self.resolver.resolve(enabled, [coordinate]).status,
            ResolutionStatus.RESOLVED,
        )

    def test_target_filters_prevent_cross_application_or_window_selection(self) -> None:
        other = candidate("Sign In", stable_id="signin")
        query = TargetQuery(
            semantic_name="Sign In",
            role="button",
            application="different-app",
            window_id="window-1",
        )
        self.assertIs(self.resolver.resolve(query, [other]).status, ResolutionStatus.NOT_FOUND)

    def test_low_confidence_and_untrusted_source_are_not_selected(self) -> None:
        low = candidate("Sign In", stable_id="low", confidence=0.1)
        result = self.resolver.resolve(self.query, [low])
        self.assertIs(result.status, ResolutionStatus.NOT_FOUND)


class ObservationFreshnessTests(unittest.TestCase):
    def test_expired_observation_is_removed_and_never_reissued(self) -> None:
        clock = [100.0]
        cache = ObservationCache(monotonic_clock=lambda: clock[0])
        item = ComputerObservation(lease=lease(deadline=101.0))
        cache.put(item)
        self.assertIs(cache.get(item.lease.lease_id), item)
        clock[0] = 101.0
        self.assertIsNone(cache.get(item.lease.lease_id))
        self.assertFalse(cache.is_current(item.lease.lease_id))

    def test_user_input_or_environment_change_invalidates_cached_observation(self) -> None:
        cache = ObservationCache(monotonic_clock=lambda: 100.0)
        item = ComputerObservation(lease=lease(deadline=105.0))
        cache.put(item)
        cache.invalidate(InvalidationReason.USER_INPUT, observation_id=item.lease.lease_id)
        self.assertIsNone(cache.get(item.lease.lease_id))

    def test_cache_is_bounded_and_evicts_oldest_observation(self) -> None:
        cache = ObservationCache(max_observations=2, monotonic_clock=lambda: 100.0)
        items = [ComputerObservation(lease=lease(deadline=105.0)) for _ in range(3)]
        for item in items:
            cache.put(item)
        self.assertIsNone(cache.get(items[0].lease.lease_id))
        self.assertIsNotNone(cache.get(items[1].lease.lease_id))
        self.assertIsNotNone(cache.get(items[2].lease.lease_id))

    def test_change_detector_distinguishes_window_navigation_and_user_interference(self) -> None:
        detector = EnvironmentChangeDetector()
        now = datetime.now(UTC)
        first = EnvironmentFingerprint(foreground_window_id="window-a", browser_page_id="page-a")
        self.assertIsNone(detector.update(first, detected_at=now))
        moved = EnvironmentFingerprint(foreground_window_id="window-b", browser_page_id="page-a")
        change = detector.update(moved, detected_at=now + timedelta(milliseconds=10))
        self.assertIsNotNone(change)
        self.assertIn("Foreground window", change.reason)
        user = EnvironmentFingerprint(
            foreground_window_id="window-b",
            browser_page_id="page-a",
            cursor_position=Point(20, 20),
        )
        interference = detector.update(
            user,
            detected_at=now + timedelta(milliseconds=20),
            user_input_observed=True,
        )
        self.assertIsNotNone(interference)
        self.assertIn("User input", interference.reason)


class ComputerActionContractTests(unittest.TestCase):
    def test_click_requires_grounded_target_and_records_provenance(self) -> None:
        authority = AuthorizationContext(
            principal_id="test-user",
            user_intent_id="request-1",
            trust=TrustLevel.USER_INSTRUCTION,
        )
        target = TargetIdentity(platform="windows", window_id="window-1", stable_id="save")
        contract = ActionContract(
            task_id="task-1",
            action_id="action-1",
            tool_name="desktop.uia.invoke",
            target=target,
            risk=RiskLevel.R1,
            authority=authority,
            required_resources=("desktop.window", "desktop.pointer"),
            timeout_seconds=5,
        )
        spec = ComputerActionSpec(
            kind=ComputerActionKind.CLICK,
            contract=contract,
            request_id="request-1",
            provenance="user instruction",
        )
        self.assertEqual(spec.target, target)
        self.assertEqual(spec.task_id, "task-1")
        self.assertEqual(spec.contract.required_resources, ("desktop.pointer", "desktop.window"))

        ungrounded = ActionContract(
            task_id="task-1",
            action_id="action-2",
            tool_name="desktop.uia.invoke",
            target=None,
            risk=RiskLevel.R1,
            authority=authority,
        )
        with self.assertRaises(ValueError):
            ComputerActionSpec(
                kind=ComputerActionKind.CLICK,
                contract=ungrounded,
                request_id="request-1",
                provenance="user instruction",
            )


if __name__ == "__main__":
    unittest.main()
