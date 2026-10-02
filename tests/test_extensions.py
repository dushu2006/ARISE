from __future__ import annotations

import unittest
from datetime import UTC, datetime

from arise.core.extensions import (
    MAX_AUDIO_CHUNK_BYTES,
    AudioChunk,
    ContextQuery,
    ContextSource,
    MemoryEntry,
    ResearchQuery,
    RetrievedContext,
    TranscriptSegment,
)


class ExtensionContractTests(unittest.TestCase):
    def test_audio_contract_bounds_chunk_size_and_format(self) -> None:
        valid = AudioChunk(
            sequence=0,
            codec="pcm_s16le",
            sample_rate_hz=16_000,
            channels=1,
            data=b"\x00\x00",
        )
        self.assertEqual(valid.sequence, 0)
        with self.assertRaises(ValueError):
            AudioChunk(
                sequence=1,
                codec="audio/pcm",
                sample_rate_hz=16_000,
                channels=1,
                data=b"\x00",
            )
        with self.assertRaises(ValueError):
            AudioChunk(
                sequence=1,
                codec="pcm_s16le",
                sample_rate_hz=16_000,
                channels=1,
                data=b"x" * (MAX_AUDIO_CHUNK_BYTES + 1),
            )

    def test_transcript_segments_validate_offsets_and_confidence(self) -> None:
        segment = TranscriptSegment(
            text="hello",
            start_offset_ms=0,
            end_offset_ms=500,
            confidence=0.95,
            is_final=True,
            locale="en-US",
        )
        self.assertTrue(segment.is_final)
        with self.assertRaises(ValueError):
            TranscriptSegment(
                text="hello",
                start_offset_ms=501,
                end_offset_ms=500,
                confidence=0.95,
                is_final=True,
            )

    def test_retrieval_contracts_are_bounded_and_provenance_tagged(self) -> None:
        query = ContextQuery(query="user preference", principal_id="user-1", limit=5)
        self.assertEqual(query.limit, 5)
        context = RetrievedContext(
            source=ContextSource.MEMORY,
            source_id="memory-1",
            text="An untrusted retrieved note.",
            provenance="local-memory:memory-1",
            retrieved_at=datetime.now(UTC),
            relevance=0.8,
        )
        self.assertIs(context.source, ContextSource.MEMORY)
        with self.assertRaises(ValueError):
            ContextQuery(query="search", principal_id="user-1", limit=0)
        with self.assertRaises(ValueError):
            RetrievedContext(
                source=ContextSource.WEB_RESEARCH,
                source_id="result-1",
                text="result",
                provenance="https://example.test",
                retrieved_at=datetime.now(),
            )

    def test_memory_write_requires_consent_reference_and_expiry(self) -> None:
        entry = MemoryEntry(
            principal_id="user-1",
            text="Preferred language: English.",
            consent_reference="consent-1",
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(entry.consent_reference, "consent-1")
        with self.assertRaises(ValueError):
            MemoryEntry(
                principal_id="user-1",
                text="memory",
                consent_reference="",
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            )

    def test_research_is_bounded_and_domain_allowlist_is_normalized(self) -> None:
        query = ResearchQuery(
            query="ARISE docs",
            max_results=3,
            allowed_domains=("Example.com", "example.com"),
        )
        self.assertEqual(query.allowed_domains, ("example.com",))
        with self.assertRaises(ValueError):
            ResearchQuery(query="unsafe", allowed_domains=("https://example.com",))
        with self.assertRaises(ValueError):
            ResearchQuery(query="too many", max_results=26)
