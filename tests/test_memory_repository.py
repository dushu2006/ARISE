from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from arise.adapters.sqlite import SQLiteDatabase, SQLiteMemoryRepository
from arise.core.extensions import (
    ContextQuery,
    ContextSource,
    EmbeddingResult,
    MemoryConsentError,
    MemoryEntry,
    MemoryGovernanceError,
    MemoryKind,
)


class DeterministicEmbeddingFixture:
    """Fake-only semantic vectors for ranking tests; not registered in production."""

    async def embed(self, text: str, *, correlation_id: str) -> EmbeddingResult:
        del correlation_id
        lowered = text.casefold()
        if "apple" in lowered or "fruit" in lowered or "unrelated generic retrieval" in lowered:
            vector = (0.98, 0.02)
        else:
            vector = (0.02, 0.98)
        return EmbeddingResult("fixture-embedding-v1", vector)


class SQLiteMemoryRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database = SQLiteDatabase(":memory:")
        self.repository = SQLiteMemoryRepository(self.database)
        self.now = datetime.now(UTC)

    async def asyncTearDown(self) -> None:
        self.database.close()

    def entry(
        self,
        *,
        text: str = "I prefer concise status updates.",
        principal_id: str = "user-a",
        consent_reference: str = "pending-consent",
        kind: MemoryKind = MemoryKind.PREFERENCE,
        expires_at: datetime | None = None,
    ) -> MemoryEntry:
        return MemoryEntry(
            principal_id=principal_id,
            text=text,
            consent_reference=consent_reference,
            expires_at=expires_at or self.now + timedelta(days=30),
            kind=kind,
        )

    async def grant(self, entry: MemoryEntry) -> MemoryEntry:
        reference, expiry = await self.repository.issue_write_consent(
            entry,
            now=self.now,
        )
        self.assertGreater(expiry, self.now)
        return replace(entry, consent_reference=reference)

    async def test_payment_card_numbers_are_rejected_before_any_storage(self) -> None:
        """Redaction masks a PAN, so governance must still reject the pre-redaction proposal."""

        for text in (
            "my card is 4111111111111111",
            "credit card: 4111 1111 1111 1111",
            "card_number=4532015112830366",
        ):
            with self.subTest(text=text):
                proposal = self.entry(text=text)
                scoped = await self.grant(proposal)
                with self.assertRaises(MemoryGovernanceError):
                    await self.repository.store(scoped)
                self.assertEqual(len(self.repository.list_records(principal_id="user-a")), 0)

    async def test_write_is_persistent_principal_scoped_and_redacted(self) -> None:
        proposal = self.entry(
            text="I prefer concise status updates; api_key=sk-12345678901234567890."
        )
        scoped = await self.grant(proposal)
        record_id = await self.repository.store(scoped)

        stored = self.repository.get_record(principal_id="user-a", record_id=record_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertIn("api_key=[REDACTED]", stored.text)
        self.assertNotIn("sk-12345678901234567890", stored.text)
        self.assertIsNone(self.repository.get_record(principal_id="user-b", record_id=record_id))
        self.assertFalse(self.repository.delete(principal_id="user-b", record_id=record_id))

        retrieved = await self.repository.retrieve(
            ContextQuery(query="concise status updates", principal_id="user-a")
        )
        self.assertTrue(retrieved)
        self.assertEqual(retrieved[0].source, ContextSource.MEMORY)
        self.assertEqual(retrieved[0].source_id, record_id)
        self.assertEqual(
            await self.repository.retrieve(
                ContextQuery(query="concise status updates", principal_id="user-b")
            ),
            (),
        )

    async def test_one_time_grant_is_bound_to_exact_text_kind_and_retention(self) -> None:
        proposed = self.entry()
        scoped = await self.grant(proposed)
        wrong_text = replace(scoped, text="I prefer verbose updates.")
        with self.assertRaises(MemoryConsentError):
            await self.repository.store(wrong_text)

        wrong_kind = replace(scoped, kind=MemoryKind.SEMANTIC)
        with self.assertRaises(MemoryConsentError):
            await self.repository.store(wrong_kind)

        record_id = await self.repository.store(scoped)
        with self.assertRaises(MemoryConsentError):
            await self.repository.store(scoped)
        self.assertIsNotNone(self.repository.get_record(principal_id="user-a", record_id=record_id))

    async def test_semantic_vectors_persist_and_rank_without_word_overlap(self) -> None:
        repository = SQLiteMemoryRepository(
            self.database,
            embedding=DeterministicEmbeddingFixture(),
        )
        entries = (
            self.entry(text="I enjoy apples and pears."),
            self.entry(text="I repair cars and buses."),
        )
        record_ids: list[str] = []
        for entry in entries:
            reference, _ = await repository.issue_write_consent(entry, now=self.now)
            record_ids.append(await repository.store(replace(entry, consent_reference=reference)))

        records = repository.list_records(principal_id="user-a")
        self.assertTrue(
            all(record.embedding_model_id == "fixture-embedding-v1" for record in records)
        )
        self.assertTrue(all(record.embedding is not None for record in records))
        results = await repository.retrieve(
            ContextQuery(
                query="unrelated generic retrieval",
                principal_id="user-a",
                limit=2,
            )
        )
        self.assertEqual(results[0].source_id, record_ids[0])
        self.assertIn("apples", results[0].text)
        self.assertGreater(results[0].relevance or 0, results[1].relevance or 0)

    async def test_invalid_consent_is_rejected_before_embedding_egress(self) -> None:
        class RecordingEmbedding:
            calls = 0

            async def embed(self, text: str, *, correlation_id: str) -> EmbeddingResult:
                del text, correlation_id
                self.calls += 1
                return EmbeddingResult("recording-model", (1.0, 0.0))

        embedding = RecordingEmbedding()
        repository = SQLiteMemoryRepository(self.database, embedding=embedding)
        with self.assertRaises(MemoryConsentError):
            await repository.store(self.entry(consent_reference="unissued-consent"))
        self.assertEqual(embedding.calls, 0)
        self.assertEqual(repository.list_records(principal_id="user-a"), [])

    async def test_embedding_failure_falls_back_to_lexical_memory_search(self) -> None:
        class FailingEmbedding:
            async def embed(self, text: str, *, correlation_id: str) -> EmbeddingResult:
                del text, correlation_id
                raise RuntimeError("provider details must not escape")

        repository = SQLiteMemoryRepository(self.database, embedding=FailingEmbedding())
        entry = self.entry(text="I prefer concise summaries.")
        reference, _ = await repository.issue_write_consent(entry, now=self.now)
        record_id = await repository.store(replace(entry, consent_reference=reference))
        results = await repository.retrieve(
            ContextQuery(query="concise summaries", principal_id="user-a")
        )
        self.assertEqual(results[0].source_id, record_id)
        record = repository.get_record(principal_id="user-a", record_id=record_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNone(record.embedding)

    async def test_expiry_and_clear_remove_records_and_outstanding_grants(self) -> None:
        scoped = await self.grant(self.entry())
        record_id = await self.repository.store(scoped)
        self.assertEqual(len(self.repository.list_records(principal_id="user-a")), 1)

        removed = self.repository.purge_expired(now=self.now + timedelta(days=31))
        self.assertEqual(removed, 1)
        self.assertEqual(self.repository.list_records(principal_id="user-a"), [])
        self.assertIsNone(self.repository.get_record(principal_id="user-a", record_id=record_id))

        proposed = self.entry(text="A second approved memory.")
        reference, _ = await self.repository.issue_write_consent(proposed, now=self.now)
        self.assertTrue(reference)
        self.assertEqual(self.repository.delete_all(principal_id="user-a"), 0)
        with self.database.locked() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM memory_consents WHERE principal_id = ?", ("user-a",)
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_expired_grant_is_not_consumed_or_written(self) -> None:
        proposal = self.entry()
        reference, _ = await self.repository.issue_write_consent(
            proposal,
            ttl_seconds=1,
            now=self.now - timedelta(seconds=2),
        )
        with self.assertRaises(MemoryConsentError):
            await self.repository.store(replace(proposal, consent_reference=reference))
        self.assertEqual(self.repository.list_records(principal_id="user-a"), [])


if __name__ == "__main__":
    unittest.main()
