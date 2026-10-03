"""Auditable event envelopes and event-store port."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from arise.core.contracts import FrozenJSON, freeze_json, json_byte_size, thaw_json, utc_now
from arise.core.redaction import DEFAULT_REDACTOR

_PROCESS_RUNTIME_ID = str(uuid.uuid4())


class EventSeverity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SECURITY = "security"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_type: str
    task_id: str | None = None
    step_id: str | None = None
    parent_event_id: str | None = None
    session_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    source: str = "agent-runtime"
    severity: EventSeverity = EventSeverity.INFO
    payload: Mapping[str, FrozenJSON] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=utc_now)
    monotonic_timestamp_ns: int = field(default_factory=time.monotonic_ns)
    runtime_id: str = field(default_factory=lambda: _PROCESS_RUNTIME_ID)
    schema_version: int = 1
    sequence: int | None = None

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("event_type cannot be blank")
        if not self.event_id.strip():
            raise ValueError("event_id cannot be blank")
        if self.timestamp.tzinfo is None:
            raise ValueError("event timestamp must be timezone-aware")
        if self.monotonic_timestamp_ns < 0:
            raise ValueError("monotonic timestamp cannot be negative")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")
        if self.sequence is not None and self.sequence < 1:
            raise ValueError("event sequence must be positive when assigned")
        if not isinstance(self.severity, EventSeverity):
            raise ValueError("severity must be an EventSeverity")
        frozen = freeze_json(self.payload, path="event.payload")
        if not isinstance(frozen, Mapping):
            raise ValueError("event payload must be an object")
        safe_payload = freeze_json(DEFAULT_REDACTOR.redact_object(frozen), path="event.payload")
        if not isinstance(safe_payload, Mapping):
            raise ValueError("event payload must be an object")
        if json_byte_size(safe_payload) > 65_536:
            raise ValueError("event payload exceeds the size limit")
        object.__setattr__(self, "payload", safe_payload)

    def with_sequence(self, sequence: int) -> EventEnvelope:
        if sequence < 1:
            raise ValueError("event sequence must be positive")
        return replace(self, sequence=sequence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "monotonic_timestamp_ns": self.monotonic_timestamp_ns,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "parent_event_id": self.parent_event_id,
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "source": self.source,
            "severity": self.severity.value,
            "payload": thaw_json(self.payload),
            "runtime_id": self.runtime_id,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EventEnvelope:
        return cls(
            event_id=str(data["event_id"]),
            event_type=str(data["event_type"]),
            timestamp=datetime.fromisoformat(str(data["timestamp"])),
            monotonic_timestamp_ns=int(data["monotonic_timestamp_ns"]),
            task_id=data.get("task_id"),
            step_id=data.get("step_id"),
            parent_event_id=data.get("parent_event_id"),
            session_id=data.get("session_id"),
            correlation_id=data.get("correlation_id"),
            causation_id=data.get("causation_id"),
            source=str(data.get("source", "unknown")),
            severity=EventSeverity(data.get("severity", EventSeverity.INFO.value)),
            payload=data.get("payload", {}),
            runtime_id=str(data.get("runtime_id", "unknown-runtime")),
            schema_version=int(data.get("schema_version", 1)),
            sequence=int(data["sequence"]) if data.get("sequence") is not None else None,
        )


class EventStore(Protocol):
    """Append-only event persistence contract."""

    def append(self, event: EventEnvelope) -> EventEnvelope: ...

    def read_after(
        self,
        sequence: int = 0,
        *,
        task_id: str | None = None,
        limit: int = 500,
    ) -> list[EventEnvelope]: ...

    def latest_sequence(self) -> int: ...

    def replay_floor(self) -> int:
        """Largest pruned sequence; cursors below it must refresh state before reconnecting."""
        ...


class DuplicateEventError(RuntimeError):
    """An event ID was replayed with content different from its original append."""


def _same_event_content(first: EventEnvelope, second: EventEnvelope) -> bool:
    first_data = first.to_dict()
    second_data = second.to_dict()
    first_data.pop("sequence", None)
    second_data.pop("sequence", None)
    return first_data == second_data


class InMemoryEventStore:
    """Deterministic event store for local development and tests."""

    def __init__(self) -> None:
        self._events: list[EventEnvelope] = []
        self._events_by_id: dict[str, EventEnvelope] = {}
        self._lock = threading.RLock()

    def append(self, event: EventEnvelope) -> EventEnvelope:
        with self._lock:
            existing = self._events_by_id.get(event.event_id)
            if existing is not None:
                if not _same_event_content(existing, event):
                    raise DuplicateEventError("event_id was reused with different event content")
                return existing
            stored = event.with_sequence(len(self._events) + 1)
            self._events.append(stored)
            self._events_by_id[event.event_id] = stored
            return stored

    def read_after(
        self,
        sequence: int = 0,
        *,
        task_id: str | None = None,
        limit: int = 500,
    ) -> list[EventEnvelope]:
        if sequence < 0:
            raise ValueError("sequence cannot be negative")
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            return [
                event
                for event in self._events
                if (event.sequence or 0) > sequence
                and (task_id is None or event.task_id == task_id)
            ][:limit]

    def latest_sequence(self) -> int:
        with self._lock:
            return len(self._events)

    def replay_floor(self) -> int:
        return 0


class NullEventStore:
    """Explicit no-op sink for callers that deliberately disable persistence."""

    def append(self, event: EventEnvelope) -> EventEnvelope:
        return event

    def read_after(
        self,
        sequence: int = 0,
        *,
        task_id: str | None = None,
        limit: int = 500,
    ) -> list[EventEnvelope]:
        return []

    def latest_sequence(self) -> int:
        return 0

    def replay_floor(self) -> int:
        return 0
