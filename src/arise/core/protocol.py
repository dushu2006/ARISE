"""Versioned, typed WebSocket frames for frontend/backend communication."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, TypeAdapter

from arise.core.models import ContractModel, UserRequest

PROTOCOL_VERSION = 1


class ClientFrameBase(ContractModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=128)


class ClientHello(ClientFrameBase):
    type: Literal["client.hello"]
    auth_token: str = Field(default="", max_length=512)
    last_event_sequence: int = Field(default=0, ge=0)


class TaskSubmitFrame(ClientFrameBase):
    type: Literal["task.submit"]
    request: UserRequest
    session_id: str | None = None


class TaskCancelFrame(ClientFrameBase):
    type: Literal["task.cancel"]
    task_id: str


class TaskApproveFrame(ClientFrameBase):
    type: Literal["task.approve"]
    task_id: str
    confirmation_id: str


class TaskRespondFrame(ClientFrameBase):
    type: Literal["task.respond"]
    task_id: str
    text: str = Field(min_length=1, max_length=16_384)


class EventSubscribeFrame(ClientFrameBase):
    type: Literal["events.subscribe"]
    after_sequence: int = Field(default=0, ge=0)
    task_id: str | None = None


class PingFrame(ClientFrameBase):
    type: Literal["ping"]


class ClientGoodbyeFrame(ClientFrameBase):
    type: Literal["client.goodbye"]


ClientFrame: TypeAlias = Annotated[
    ClientHello
    | TaskSubmitFrame
    | TaskCancelFrame
    | TaskApproveFrame
    | TaskRespondFrame
    | EventSubscribeFrame
    | PingFrame
    | ClientGoodbyeFrame,
    Field(discriminator="type"),
]

CLIENT_FRAME_ADAPTER = TypeAdapter(ClientFrame)


class ServerFrame(ContractModel):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str = Field(min_length=1, max_length=64)
    payload: dict[str, object] = Field(default_factory=dict)

    def to_wire(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def parse_client_frame(data: object) -> ClientFrame:
    return CLIENT_FRAME_ADAPTER.validate_python(data)
