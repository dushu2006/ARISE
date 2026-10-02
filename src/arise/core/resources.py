"""Fair, cancellation-safe resource leases for shared desktop resources."""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from arise.core.contracts import validate_safe_token


class ResourceAcquisitionTimeout(TimeoutError):
    pass


class ResourceLeaseLost(RuntimeError):
    pass


@dataclass(slots=True)
class _Owner:
    lease_id: str
    task_id: str
    expires_at: float


@dataclass(slots=True)
class _Waiter:
    task_id: str
    resources: tuple[str, ...]
    priority: int
    sequence: int


class ResourceLease:
    """Lease object passed to tools; release is idempotent and cancellation-safe."""

    def __init__(
        self,
        manager: ResourceManager,
        *,
        lease_id: str,
        task_id: str,
        resources: tuple[str, ...],
        expires_at: float,
    ) -> None:
        self._manager = manager
        self.lease_id = lease_id
        self.task_id = task_id
        self.resources = resources
        self.expires_at = expires_at
        self._released = False

    async def ensure_valid(self) -> None:
        if self._released or not await self._manager._lease_is_valid(self):
            raise ResourceLeaseLost(f"resource lease for task {self.task_id} is no longer valid")

    async def renew(self, seconds: float) -> None:
        if self._released:
            raise ResourceLeaseLost("cannot renew a released lease")
        self.expires_at = await self._manager._renew(self, seconds)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._manager._release(self)

    async def __aenter__(self) -> ResourceLease:
        await self.ensure_valid()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.release()


class ResourceManager:
    """Atomically acquires resource sets in a stable order.

    Acquisition is all-or-nothing, so tasks cannot deadlock by taking the mouse
    and a window in opposite orders. Expired leases are reclaimed lazily whenever
    an operation enters the manager. Callers must still release explicitly in a
    ``finally`` block (the async context manager does this automatically).
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._owners: dict[str, _Owner] = {}
        self._waiters: list[_Waiter] = []
        self._sequence = 0

    @staticmethod
    def _normalize(resources: Iterable[str]) -> tuple[str, ...]:
        supplied = tuple(resources)
        if any(not isinstance(resource, str) for resource in supplied):
            raise ValueError("resource names must be strings")
        normalized = tuple(sorted(set(supplied)))
        for resource in normalized:
            validate_safe_token(resource, "resource name")
        return normalized

    def _purge_expired_locked(self, now: float) -> None:
        expired = [resource for resource, owner in self._owners.items() if owner.expires_at <= now]
        for resource in expired:
            self._owners.pop(resource, None)

    def _can_grant_locked(self, waiter: _Waiter) -> bool:
        wanted = set(waiter.resources)
        if any(resource in self._owners for resource in wanted):
            return False

        ordered = sorted(self._waiters, key=lambda item: (-item.priority, item.sequence))
        for earlier in ordered:
            if earlier is waiter:
                break
            # Preserve FIFO/priority fairness for overlapping sets while allowing
            # independent resources to proceed concurrently.
            if wanted.intersection(earlier.resources):
                return False
        return True

    @asynccontextmanager
    async def acquire_many(
        self,
        task_id: str,
        resources: Iterable[str],
        *,
        priority: int = 0,
        wait_timeout: float | None = None,
        lease_seconds: float = 60.0,
    ) -> AsyncIterator[ResourceLease]:
        if not task_id.strip():
            raise ValueError("task_id cannot be blank")
        normalized = self._normalize(resources)
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive and finite")
        if wait_timeout is not None and (not math.isfinite(wait_timeout) or wait_timeout < 0):
            raise ValueError("wait_timeout must be non-negative and finite")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_timeout if wait_timeout is not None else None
        lease_id = str(uuid.uuid4())
        acquired = False
        expires_at = loop.time() + lease_seconds

        async with self._condition:
            self._sequence += 1
            waiter = _Waiter(task_id, normalized, priority, self._sequence)
            self._waiters.append(waiter)
            try:
                while True:
                    now = loop.time()
                    self._purge_expired_locked(now)
                    if self._can_grant_locked(waiter):
                        self._waiters.remove(waiter)
                        expires_at = now + lease_seconds
                        for resource in normalized:
                            self._owners[resource] = _Owner(lease_id, task_id, expires_at)
                        acquired = True
                        self._condition.notify_all()
                        break

                    remaining = deadline - now if deadline is not None else None
                    if remaining is not None and remaining <= 0:
                        raise ResourceAcquisitionTimeout(
                            f"timed out waiting for resources: {', '.join(normalized)}"
                        )
                    expiries = [owner.expires_at - now for owner in self._owners.values()]
                    next_expiry = min((value for value in expiries if value > 0), default=None)
                    wait_for = remaining
                    if next_expiry is not None:
                        wait_for = next_expiry if wait_for is None else min(wait_for, next_expiry)
                    try:
                        if wait_for is None:
                            await self._condition.wait()
                        else:
                            await asyncio.wait_for(
                                self._condition.wait(), timeout=max(wait_for, 0.001)
                            )
                    except TimeoutError:
                        continue
            finally:
                if not acquired and waiter in self._waiters:
                    self._waiters.remove(waiter)
                    self._condition.notify_all()

        lease = ResourceLease(
            self,
            lease_id=lease_id,
            task_id=task_id,
            resources=normalized,
            expires_at=expires_at,
        )
        try:
            yield lease
        finally:
            # Shield the cleanup from task cancellation so input/device locks do
            # not remain held after the agent is interrupted.
            await asyncio.shield(lease.release())

    async def owner(self, resource: str) -> str | None:
        async with self._condition:
            self._purge_expired_locked(asyncio.get_running_loop().time())
            owner = self._owners.get(resource)
            return owner.task_id if owner else None

    async def _lease_is_valid(self, lease: ResourceLease) -> bool:
        async with self._condition:
            now = asyncio.get_running_loop().time()
            self._purge_expired_locked(now)
            return all(
                (owner := self._owners.get(resource)) is not None
                and owner.lease_id == lease.lease_id
                and owner.task_id == lease.task_id
                for resource in lease.resources
            )

    async def _renew(self, lease: ResourceLease, seconds: float) -> float:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("lease extension must be positive and finite")
        async with self._condition:
            now = asyncio.get_running_loop().time()
            self._purge_expired_locked(now)
            if not all(
                (owner := self._owners.get(resource)) is not None
                and owner.lease_id == lease.lease_id
                and owner.task_id == lease.task_id
                for resource in lease.resources
            ):
                raise ResourceLeaseLost("resource lease expired before renewal")
            expiry = now + seconds
            for resource in lease.resources:
                self._owners[resource].expires_at = expiry
            self._condition.notify_all()
            return expiry

    async def _release(self, lease: ResourceLease) -> None:
        async with self._condition:
            for resource in lease.resources:
                owner = self._owners.get(resource)
                # An old lease must never release a newer owner's lock.
                if owner is not None and owner.lease_id == lease.lease_id:
                    self._owners.pop(resource, None)
            self._condition.notify_all()
