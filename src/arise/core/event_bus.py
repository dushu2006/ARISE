"""Bounded in-process live-event fanout backed by a durable event-store port."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from arise.core.events import EventEnvelope, EventStore


@dataclass(eq=False, slots=True)
class EventSubscription:
    task_id: str | None
    queue: asyncio.Queue[EventEnvelope]
    overflowed: bool = False


class EventBroker:
    """Slow clients are disconnected; they can replay from the durable cursor."""

    def __init__(self) -> None:
        self._subscriptions: set[EventSubscription] = set()

    def subscribe(
        self, *, task_id: str | None = None, max_queue_size: int = 128
    ) -> EventSubscription:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be positive")
        subscription = EventSubscription(
            task_id=task_id,
            queue=asyncio.Queue(maxsize=max_queue_size),
        )
        self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        self._subscriptions.discard(subscription)

    def publish(self, event: EventEnvelope) -> None:
        for subscription in tuple(self._subscriptions):
            if subscription.task_id is not None and subscription.task_id != event.task_id:
                continue
            try:
                subscription.queue.put_nowait(event)
            except asyncio.QueueFull:
                subscription.overflowed = True
                self._subscriptions.discard(subscription)


class PublishingEventStore(EventStore):
    """Durably append first, then publish the assigned sequence to subscribers."""

    def __init__(self, store: EventStore, broker: EventBroker) -> None:
        self.store = store
        self.broker = broker

    def append(self, event: EventEnvelope) -> EventEnvelope:
        stored = self.store.append(event)
        self.broker.publish(stored)
        return stored

    def read_after(self, sequence: int = 0, *, task_id: str | None = None, limit: int = 500):
        return self.store.read_after(sequence, task_id=task_id, limit=limit)

    def latest_sequence(self) -> int:
        return self.store.latest_sequence()
