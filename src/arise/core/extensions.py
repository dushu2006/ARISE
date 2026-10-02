"""Typed contracts for future voice, memory, and research adapters.

These ports intentionally contain no device, storage, browser, or provider code. Data returned
through retrieval ports is context only: it carries no task authority and must remain untrusted.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from arise.core.contracts import validate_safe_token

MAX_AUDIO_CHUNK_BYTES = 256 * 1024
MAX_CONTEXT_TEXT_CHARS = 16_384
_AUDIO_FORMAT = re.compile(r"^[a-z0-9][a-z0-9.+_-]{0,63}$")
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """Bounded, ephemeral audio payload passed between voice adapters."""

    sequence: int
    codec: str
    sample_rate_hz: int
    channels: int
    data: bytes

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("audio sequence cannot be negative")
        if not _AUDIO_FORMAT.fullmatch(self.codec):
            raise ValueError("audio codec must be a short, lowercase format token")
        if not 8_000 <= self.sample_rate_hz <= 192_000:
            raise ValueError("audio sample rate must be between 8 kHz and 192 kHz")
        if not 1 <= self.channels <= 8:
            raise ValueError("audio channel count must be between one and eight")
        if not isinstance(self.data, bytes) or not 1 <= len(self.data) <= MAX_AUDIO_CHUNK_BYTES:
            raise ValueError("audio chunk must contain between 1 byte and 256 KiB")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """A temporary transcript segment; persistence is a separate, consent-gated decision."""

    text: str
    start_offset_ms: int
    end_offset_ms: int
    confidence: float
    is_final: bool
    locale: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or len(self.text) > 4096:
            raise ValueError("transcript segment must be text no longer than 4096 characters")
        if self.start_offset_ms < 0 or self.end_offset_ms < self.start_offset_ms:
            raise ValueError("transcript segment offsets are invalid")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("transcript confidence must be between zero and one")
        if self.locale is not None and len(self.locale) > 32:
            raise ValueError("transcript locale cannot exceed 32 characters")


class SpeechRecognitionPort(Protocol):
    """Convert an explicitly supplied audio stream to transcript segments."""

    def transcribe(
        self,
        audio: AsyncIterator[AudioChunk],
        *,
        locale: str | None = None,
        correlation_id: str,
    ) -> AsyncIterator[TranscriptSegment]: ...


class SpeechSynthesisPort(Protocol):
    """Synthesize bounded audio chunks; implementations must honor task cancellation."""

    def synthesize(
        self,
        text: str,
        *,
        locale: str | None = None,
        voice_id: str | None = None,
        correlation_id: str,
    ) -> AsyncIterator[AudioChunk]: ...


class ContextSource(StrEnum):
    MEMORY = "memory"
    WEB_RESEARCH = "web_research"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class ContextQuery:
    """Principal-scoped retrieval request. It does not grant access to perform actions."""

    query: str
    principal_id: str
    session_id: str | None = None
    task_id: str | None = None
    limit: int = 8

    def __post_init__(self) -> None:
        if not self.query.strip() or len(self.query) > MAX_CONTEXT_TEXT_CHARS:
            raise ValueError("context query must be non-empty and bounded")
        validate_safe_token(self.principal_id, "context principal_id")
        for name, value in (("session_id", self.session_id), ("task_id", self.task_id)):
            if value is not None:
                validate_safe_token(value, f"context {name}")
        if not 1 <= self.limit <= 50:
            raise ValueError("context result limit must be between one and fifty")


@dataclass(frozen=True, slots=True)
class RetrievedContext:
    """Provenance-tagged, untrusted text returned to a planner or model as data only."""

    source: ContextSource
    source_id: str
    text: str
    provenance: str
    retrieved_at: datetime
    relevance: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, ContextSource):
            raise ValueError("retrieved context source is invalid")
        if not self.source_id.strip() or len(self.source_id) > 512:
            raise ValueError("retrieved context source_id must be non-empty and bounded")
        if not isinstance(self.text, str) or len(self.text) > MAX_CONTEXT_TEXT_CHARS:
            raise ValueError("retrieved context text exceeds the configured limit")
        if not self.provenance.strip() or len(self.provenance) > 2048:
            raise ValueError("retrieved context provenance must be non-empty and bounded")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved context timestamp must be timezone-aware")
        if self.relevance is not None and not 0.0 <= self.relevance <= 1.0:
            raise ValueError("context relevance must be between zero and one")


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """A proposed memory write, requiring an explicit consent reference and expiry."""

    principal_id: str
    text: str
    consent_reference: str
    expires_at: datetime
    source_task_id: str | None = None

    def __post_init__(self) -> None:
        validate_safe_token(self.principal_id, "memory principal_id")
        validate_safe_token(self.consent_reference, "memory consent_reference")
        if not self.text.strip() or len(self.text) > MAX_CONTEXT_TEXT_CHARS:
            raise ValueError("memory text must be non-empty and bounded")
        if self.expires_at.tzinfo is None:
            raise ValueError("memory expiry must be timezone-aware")
        if self.source_task_id is not None:
            validate_safe_token(self.source_task_id, "memory source_task_id")


class MemoryConsentError(PermissionError):
    """A memory write lacks a current, principal-scoped, unused consent grant."""


class MemoryConsentPort(Protocol):
    """Atomically validate and consume a one-time memory-write consent reference.

    Implementations must verify the reference, principal, write scope, and consent expiry in
    the same atomic operation that marks the reference consumed. Consent must not be inferred
    from model output, a task result, or the presence of a non-empty reference string.
    """

    async def consume_write_consent(
        self,
        *,
        principal_id: str,
        consent_reference: str,
        now: datetime,
    ) -> bool: ...


async def require_memory_write_consent(
    entry: MemoryEntry,
    consent: MemoryConsentPort | None,
    *,
    now: datetime | None = None,
) -> None:
    """Reject expired proposals and require an atomic, one-time consent check.

    This guard does not persist or retrieve memories. Future memory adapters must call it
    immediately before a write and must not retry the write if its outcome is unknown.
    """

    checked_at = now or datetime.now(UTC)
    if checked_at.tzinfo is None:
        raise ValueError("memory consent check time must be timezone-aware")
    if entry.expires_at <= checked_at:
        raise MemoryConsentError("memory proposal has expired")
    if consent is None or not await consent.consume_write_consent(
        principal_id=entry.principal_id,
        consent_reference=entry.consent_reference,
        now=checked_at,
    ):
        raise MemoryConsentError("valid, unexpired, unused memory-write consent is required")


class MemoryPort(Protocol):
    """Principal-scoped memory adapter; retrieved content remains untrusted input."""

    async def retrieve(self, query: ContextQuery) -> Sequence[RetrievedContext]: ...

    async def store(self, entry: MemoryEntry) -> str: ...

    async def delete(self, *, principal_id: str, record_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ResearchQuery:
    """Bounded web-research request with an optional domain allowlist."""

    query: str
    max_results: int = 8
    allowed_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.query.strip() or len(self.query) > MAX_CONTEXT_TEXT_CHARS:
            raise ValueError("research query must be non-empty and bounded")
        if not 1 <= self.max_results <= 25:
            raise ValueError("research max_results must be between one and twenty-five")
        domains = tuple(
            dict.fromkeys(domain.lower().rstrip(".") for domain in self.allowed_domains)
        )
        for domain in domains:
            if (
                len(domain) > 253
                or not domain
                or any(not _DOMAIN_LABEL.fullmatch(label) for label in domain.split("."))
            ):
                raise ValueError("research allowlist entries must be plain DNS host names")
        object.__setattr__(self, "allowed_domains", domains)


class WebResearchPort(Protocol):
    """Return bounded, provenance-tagged results without granting browser/action authority."""

    async def search(self, query: ResearchQuery) -> Sequence[RetrievedContext]: ...
