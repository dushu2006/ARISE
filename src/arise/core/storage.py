"""Persistence ports for sessions and conversation turns."""

from __future__ import annotations

from typing import Protocol

from arise.core.models import ConversationTurn, Session


class SessionRepository(Protocol):
    def create(self, session: Session) -> Session: ...

    def get(self, session_id: str) -> Session | None: ...

    def list_recent(self, *, principal_id: str, limit: int = 50) -> list[Session]: ...

    def append_turn(self, turn: ConversationTurn) -> ConversationTurn: ...
