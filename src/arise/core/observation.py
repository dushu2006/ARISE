"""Freshness cache and deterministic environment-change detection."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from arise.core.computer import ComputerObservation, EnvironmentFingerprint, HumanInterference


class InvalidationReason(StrEnum):
    USER_INPUT = "user_input"
    FOREGROUND_WINDOW_CHANGED = "foreground_window_changed"
    BROWSER_PAGE_CHANGED = "browser_page_changed"
    NAVIGATION = "navigation"
    ACCESSIBILITY_TREE_CHANGED = "accessibility_tree_changed"
    DISPLAY_TOPOLOGY_CHANGED = "display_topology_changed"
    SCREEN_CHANGED = "screen_changed"
    TARGET_MOVED = "target_moved"
    ADAPTER_RESTARTED = "adapter_restarted"
    EXPLICIT = "explicit"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class CachedComputerObservation:
    observation: ComputerObservation
    cached_at_monotonic: float


class ObservationCache:
    """Bounded cache that never returns expired or explicitly invalidated state."""

    def __init__(
        self,
        *,
        max_observations: int = 64,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= max_observations <= 4096:
            raise ValueError("max_observations must be between 1 and 4096")
        self.max_observations = max_observations
        self._clock = monotonic_clock
        self._observations: OrderedDict[str, CachedComputerObservation] = OrderedDict()
        self._invalidated: set[str] = set()
        self._lock = threading.RLock()

    def put(self, observation: ComputerObservation) -> None:
        if not observation.lease.is_valid(monotonic_now=self._clock()):
            raise ValueError("expired observations cannot be cached")
        with self._lock:
            self._purge_expired_locked()
            observation_id = observation.lease.lease_id
            self._invalidated.discard(observation_id)
            self._observations[observation_id] = CachedComputerObservation(
                observation=observation,
                cached_at_monotonic=self._clock(),
            )
            self._observations.move_to_end(observation_id)
            while len(self._observations) > self.max_observations:
                evicted_id, _ = self._observations.popitem(last=False)
                self._invalidated.discard(evicted_id)

    def get(self, observation_id: str) -> ComputerObservation | None:
        with self._lock:
            if observation_id in self._invalidated:
                return None
            self._purge_expired_locked()
            cached = self._observations.get(observation_id)
            if cached is None:
                return None
            self._observations.move_to_end(observation_id)
            return cached.observation

    def is_current(
        self,
        observation_id: str,
        *,
        state_hash: str | None = None,
        target_fingerprint: str | None = None,
    ) -> bool:
        observation = self.get(observation_id)
        if observation is None:
            return False
        lease = observation.lease
        return (state_hash is None or lease.state_hash == state_hash) and (
            target_fingerprint is None or lease.target_fingerprint == target_fingerprint
        )

    def invalidate(
        self,
        reason: InvalidationReason,
        *,
        observation_id: str | None = None,
    ) -> None:
        if not isinstance(reason, InvalidationReason):
            raise ValueError("reason must be an InvalidationReason")
        with self._lock:
            if observation_id is None:
                self._invalidated.update(self._observations)
                self._observations.clear()
                return
            self._observations.pop(observation_id, None)
            self._invalidated.add(observation_id)

    def invalidate_target(self, target_fingerprint: str, reason: InvalidationReason) -> int:
        with self._lock:
            matching = [
                key
                for key, cached in self._observations.items()
                if cached.observation.lease.target_fingerprint == target_fingerprint
            ]
            for key in matching:
                self._observations.pop(key, None)
                self._invalidated.add(key)
            return len(matching)

    def clear(self) -> None:
        self.invalidate(InvalidationReason.EXPLICIT)

    def _purge_expired_locked(self) -> None:
        now = self._clock()
        expired = [
            key
            for key, cached in self._observations.items()
            if not cached.observation.lease.is_valid(monotonic_now=now)
        ]
        for key in expired:
            self._observations.pop(key, None)
            self._invalidated.add(key)
        # Bound tombstones independently from cache churn.
        if len(self._invalidated) > self.max_observations * 4:
            self._invalidated = set(list(self._invalidated)[-self.max_observations * 2 :])


class EnvironmentChangeDetector:
    """Compares deterministic state fingerprints; adapters supply actual signals."""

    def __init__(self) -> None:
        self._previous: EnvironmentFingerprint | None = None
        self._lock = threading.RLock()

    def update(
        self,
        current: EnvironmentFingerprint,
        *,
        detected_at: datetime,
        user_input_observed: bool = False,
    ) -> HumanInterference | None:
        if detected_at.tzinfo is None:
            raise ValueError("detected_at must be timezone-aware")
        with self._lock:
            previous, self._previous = self._previous, current
        if previous is None or previous.digest == current.digest:
            return None
        if user_input_observed:
            reason = "User input was observed while an action held computer resources."
        elif previous.foreground_window_id != current.foreground_window_id:
            reason = "Foreground window changed during the observation lease."
        elif previous.browser_page_id != current.browser_page_id or (
            previous.browser_url_hash != current.browser_url_hash
        ):
            reason = "Browser page identity or URL changed during the observation lease."
        elif previous.display_topology_hash != current.display_topology_hash:
            reason = "Display topology or DPI changed during the observation lease."
        elif previous.accessibility_hash != current.accessibility_hash:
            reason = "Accessibility tree changed during the observation lease."
        elif previous.screenshot_hash != current.screenshot_hash:
            reason = "Screen content changed during the observation lease."
        else:
            reason = "Computer environment changed during the observation lease."
        return HumanInterference(reason, detected_at, previous, current)

    def reset(self) -> None:
        with self._lock:
            self._previous = None
