"""Bounded, event-driven waiting with adaptive polling as the fallback.

Long-running work must not be modelled as "sleep 60 seconds and assume the result".
The hierarchy ARISE prefers is:

    1. a real event (process/window/DOM/UIA/provider/job event),
    2. a provider job/task identifier status,
    3. application-level state,
    4. controlled polling with adaptive backoff.

``WaitCoordinator`` implements that hierarchy for anything that can be expressed as
an awaitable predicate: it subscribes to the durable event broker when one is
supplied, re-checks the predicate on every relevant event, and otherwise falls back
to polling with an interval that grows from ``min_interval`` to ``max_interval``.
Polling never happens every second unless the caller asks for that explicitly.

The coordinator owns no authority and performs no side effects: predicates are
read-only observations supplied by callers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

DEFAULT_MIN_INTERVAL_SECONDS = 0.5
DEFAULT_MAX_INTERVAL_SECONDS = 15.0
DEFAULT_BACKOFF_FACTOR = 1.7
_MAX_CONSECUTIVE_PREDICATE_ERRORS = 8


class WaitOutcome(StrEnum):
    RESOLVED = "resolved"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PREDICATE_UNAVAILABLE = "predicate_unavailable"


@dataclass(frozen=True, slots=True)
class WaitResult:
    outcome: WaitOutcome
    elapsed_seconds: float
    attempts: int
    event_wakeups: int
    poll_wakeups: int
    final_interval_seconds: float
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.outcome is WaitOutcome.RESOLVED


class EventSubscriptionPort(Protocol):
    """The narrow slice of :class:`EventBroker` the coordinator depends on."""

    def subscribe(self, *, task_id: str | None = None, max_queue_size: int = 128) -> Any: ...

    def unsubscribe(self, subscription: Any) -> None: ...


class WaitCoordinator:
    """Wait for an observable condition using events first, backoff polling second."""

    def __init__(
        self,
        *,
        broker: EventSubscriptionPort | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._broker = broker
        self._clock = clock
        self._sleeper = sleeper

    async def wait_until(
        self,
        predicate: Callable[[], Awaitable[bool] | bool],
        *,
        timeout_seconds: float,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        max_interval_seconds: float = DEFAULT_MAX_INTERVAL_SECONDS,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        task_id: str | None = None,
        event_filter: Callable[[Any], bool] | None = None,
        description: str = "",
    ) -> WaitResult:
        """Wait until ``predicate`` is true, the deadline passes, or cancellation.

        The predicate is checked once immediately, then after every matching event
        and otherwise after a bounded, exponentially spaced interval. Cancellation
        propagates so a task can be interrupted while it waits.
        """

        started = self._clock()
        deadline = started + max(0.0, float(timeout_seconds))
        interval = _bounded_interval(
            min_interval_seconds, min_interval_seconds, max_interval_seconds
        )
        attempts = 0
        event_wakeups = 0
        poll_wakeups = 0
        consecutive_errors = 0
        subscription: Any = None
        queue: asyncio.Queue[Any] | None = None
        if self._broker is not None:
            try:
                subscription = self._broker.subscribe(task_id=task_id)
                queue = getattr(subscription, "queue", None)
            except Exception:
                subscription = None
                queue = None
        try:
            while True:
                attempts += 1
                try:
                    ready = predicate()
                    if asyncio.iscoroutine(ready) or isinstance(ready, Awaitable):
                        ready = await ready
                except asyncio.CancelledError:
                    raise
                except Exception:
                    consecutive_errors += 1
                    if consecutive_errors >= _MAX_CONSECUTIVE_PREDICATE_ERRORS:
                        return WaitResult(
                            WaitOutcome.PREDICATE_UNAVAILABLE,
                            self._clock() - started,
                            attempts,
                            event_wakeups,
                            poll_wakeups,
                            interval,
                            "The wait condition could not be observed repeatedly.",
                        )
                    ready = False
                else:
                    consecutive_errors = 0
                if ready:
                    return WaitResult(
                        WaitOutcome.RESOLVED,
                        self._clock() - started,
                        attempts,
                        event_wakeups,
                        poll_wakeups,
                        interval,
                        description,
                    )
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return WaitResult(
                        WaitOutcome.TIMEOUT,
                        self._clock() - started,
                        attempts,
                        event_wakeups,
                        poll_wakeups,
                        interval,
                        description,
                    )
                wait_for = min(interval, remaining)
                if queue is not None:
                    woke_on_event = await self._wait_for_event(
                        queue, wait_for, event_filter=event_filter
                    )
                    if woke_on_event:
                        event_wakeups += 1
                        # A relevant event means state may have just changed: check
                        # immediately and reset the backoff.
                        interval = _bounded_interval(
                            min_interval_seconds, min_interval_seconds, max_interval_seconds
                        )
                        continue
                else:
                    await self._sleeper(wait_for)
                poll_wakeups += 1
                interval = _bounded_interval(
                    interval * max(1.0, float(backoff_factor)),
                    min_interval_seconds,
                    max_interval_seconds,
                )
        except asyncio.CancelledError:
            return WaitResult(
                WaitOutcome.CANCELLED,
                self._clock() - started,
                attempts,
                event_wakeups,
                poll_wakeups,
                interval,
                description,
            )
        finally:
            if subscription is not None and self._broker is not None:
                try:
                    self._broker.unsubscribe(subscription)
                except Exception:
                    pass

    async def _wait_for_event(
        self,
        queue: asyncio.Queue[Any],
        timeout: float,
        *,
        event_filter: Callable[[Any], bool] | None,
    ) -> bool:
        """Return True when a *relevant* event arrived before the timeout."""

        try:
            event = await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError:
            return False
        if event_filter is None:
            return True
        try:
            return bool(event_filter(event))
        except Exception:
            return True


def _bounded_interval(value: float, minimum: float, maximum: float) -> float:
    lower = max(0.05, float(minimum))
    upper = max(lower, float(maximum))
    return min(max(float(value), lower), upper)


def wait_summary(result: WaitResult, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Bounded, secret-free diagnostic payload for events and task state."""

    payload: dict[str, Any] = {
        "outcome": result.outcome.value,
        "elapsed_seconds": round(float(result.elapsed_seconds), 3),
        "attempts": int(result.attempts),
        "event_wakeups": int(result.event_wakeups),
        "poll_wakeups": int(result.poll_wakeups),
        "final_interval_seconds": round(float(result.final_interval_seconds), 3),
    }
    if result.detail:
        payload["description"] = result.detail[:128]
    if extra:
        payload.update(extra)
    return payload


__all__ = [
    "WaitCoordinator",
    "WaitOutcome",
    "WaitResult",
    "wait_summary",
]
