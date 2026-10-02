from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from arise.core.extensions import (
    MemoryConsentError,
    MemoryEntry,
    require_memory_write_consent,
)


class FakeConsentRegistry:
    """Test-only atomic consent port; this is not a memory store."""

    def __init__(self, grants: dict[str, tuple[str, datetime]]) -> None:
        self.grants = grants
        self.consumed: set[str] = set()
        self._lock = asyncio.Lock()

    async def consume_write_consent(
        self,
        *,
        principal_id: str,
        consent_reference: str,
        now: datetime,
    ) -> bool:
        async with self._lock:
            grant = self.grants.get(consent_reference)
            if (
                grant is None
                or grant[0] != principal_id
                or grant[1] <= now
                or consent_reference in self.consumed
            ):
                return False
            self.consumed.add(consent_reference)
            return True


class MemoryConsentGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 2, tzinfo=UTC)

    def entry(self, reference: str = "consent-a", *, principal: str = "user-a") -> MemoryEntry:
        return MemoryEntry(
            principal_id=principal,
            text="The user's preferred language is English.",
            consent_reference=reference,
            expires_at=self.now + timedelta(days=1),
        )

    async def test_missing_invalid_expired_and_wrong_principal_consents_are_rejected(self) -> None:
        with self.assertRaises(MemoryConsentError):
            await require_memory_write_consent(self.entry(), None, now=self.now)

        registry = FakeConsentRegistry(
            {
                "expired": ("user-a", self.now - timedelta(seconds=1)),
                "other-user": ("user-b", self.now + timedelta(days=1)),
            }
        )
        with self.assertRaises(MemoryConsentError):
            await require_memory_write_consent(
                self.entry("unknown-reference"), registry, now=self.now
            )
        with self.assertRaises(MemoryConsentError):
            await require_memory_write_consent(self.entry("expired"), registry, now=self.now)
        with self.assertRaises(MemoryConsentError):
            await require_memory_write_consent(self.entry("other-user"), registry, now=self.now)

    async def test_proposal_expiry_is_checked_even_with_a_live_consent_grant(self) -> None:
        registry = FakeConsentRegistry(
            {"consent-a": ("user-a", self.now + timedelta(days=1))}
        )
        expired_entry = self.entry().__class__(
            principal_id="user-a",
            text="The user's preferred language is English.",
            consent_reference="consent-a",
            expires_at=self.now,
        )
        with self.assertRaises(MemoryConsentError):
            await require_memory_write_consent(expired_entry, registry, now=self.now)
        self.assertFalse(registry.consumed)

    async def test_consent_reference_is_consumed_once_and_replay_is_rejected(self) -> None:
        registry = FakeConsentRegistry(
            {"consent-a": ("user-a", self.now + timedelta(days=1))}
        )
        await require_memory_write_consent(self.entry(), registry, now=self.now)
        with self.assertRaises(MemoryConsentError):
            await require_memory_write_consent(self.entry(), registry, now=self.now)
        self.assertEqual(registry.consumed, {"consent-a"})

    async def test_concurrent_replay_allows_exactly_one_consumer(self) -> None:
        registry = FakeConsentRegistry(
            {"consent-a": ("user-a", self.now + timedelta(days=1))}
        )
        results = await asyncio.gather(
            require_memory_write_consent(self.entry(), registry, now=self.now),
            require_memory_write_consent(self.entry(), registry, now=self.now),
            return_exceptions=True,
        )
        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(sum(isinstance(result, MemoryConsentError) for result in results), 1)


if __name__ == "__main__":
    unittest.main()
