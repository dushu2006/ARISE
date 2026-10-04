"""Centralized retry/circuit-breaker primitives for safe idempotent I/O only."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from arise.core.errors import ErrorInfo, classify_exception

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 0.2
    max_delay_seconds: float = 4.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.initial_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max delay cannot be less than initial delay")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be in [0, 1]")

    def delay_for_attempt(self, attempt: int, *, random_value: float | None = None) -> float:
        if attempt < 1:
            raise ValueError("attempt numbers start at one")
        base = min(self.max_delay_seconds, self.initial_delay_seconds * (2 ** (attempt - 1)))
        sample = random.random() if random_value is None else random_value
        sample = min(1.0, max(0.0, sample))
        factor = 1.0 + self.jitter_ratio * (2.0 * sample - 1.0)
        return max(0.0, base * factor)


async def run_with_retry(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    classify: Callable[[BaseException], ErrorInfo] = classify_exception,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Callable[[int, ErrorInfo, float], None] | None = None,
) -> T:
    """Retry only failures classified retryable; cancellation is never retried.

    Do not use this helper for an action whose external side effect may have
    started. Such an action must be reconciled and represented as UNKNOWN.
    """

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            info = classify(exc)
            if not info.retryable or attempt >= policy.max_attempts:
                raise
            delay = policy.delay_for_attempt(attempt)
            if on_retry is not None:
                on_retry(attempt, info, delay)
            await sleep(delay)
    raise AssertionError("unreachable")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    """Small provider-health breaker; callers own the request semaphore."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        half_open_max_calls: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or half_open_max_calls < 1 or recovery_seconds <= 0:
            raise ValueError("invalid circuit breaker settings")
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.half_open_max_calls = half_open_max_calls
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and self._clock() - self._opened_at >= self.recovery_seconds
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0
        return self._state

    def allow_request(self) -> bool:
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.OPEN:
            return False
        if self._half_open_calls >= self.half_open_max_calls:
            return False
        self._half_open_calls += 1
        return True

    def record_cancelled(self) -> None:
        """Release an abandoned half-open probe without claiming health or failure."""
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_calls = max(0, self._half_open_calls - 1)

    def record_success(self) -> None:
        self._failure_count = 0
        self._opened_at = None
        self._half_open_calls = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failure_count += 1
        if self._state is CircuitState.HALF_OPEN or self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            self._half_open_calls = 0
