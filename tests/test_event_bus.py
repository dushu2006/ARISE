from __future__ import annotations

import unittest

from arise.core.event_bus import EventBroker, PublishingEventStore
from arise.core.events import EventEnvelope, InMemoryEventStore


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_append_publishes_assigned_sequence(self) -> None:
        broker = EventBroker()
        subscription = broker.subscribe(max_queue_size=2)
        store = PublishingEventStore(InMemoryEventStore(), broker)
        event = store.append(EventEnvelope(event_type="TASK_ACCEPTED", task_id="task-a"))

        published = await subscription.queue.get()
        self.assertEqual(published.event_id, event.event_id)
        self.assertEqual(published.sequence, event.sequence)
        self.assertEqual(store.latest_sequence(), 1)
        self.assertEqual(store.replay_floor(), 0)

    async def test_failed_durable_append_is_not_published(self) -> None:
        class FailingStore:
            def append(self, event: EventEnvelope) -> EventEnvelope:
                del event
                raise OSError("injected event journal failure")

            def read_after(self, sequence=0, *, task_id=None, limit=500):
                del sequence, task_id, limit
                return []

            def latest_sequence(self) -> int:
                return 0

            def replay_floor(self) -> int:
                return 0

        broker = EventBroker()
        subscription = broker.subscribe()
        store = PublishingEventStore(FailingStore(), broker)
        with self.assertRaisesRegex(OSError, "injected event journal failure"):
            store.append(EventEnvelope(event_type="ACTION_STARTED"))
        self.assertTrue(subscription.queue.empty())

    async def test_event_payloads_are_recursively_redacted_before_persistence_and_publish(
        self,
    ) -> None:
        broker = EventBroker()
        subscription = broker.subscribe()
        store = PublishingEventStore(InMemoryEventStore(), broker)
        submitted = EventEnvelope(
            event_type="SENSITIVE_TEST",
            payload={
                "message": "Use api_key=sk-123456789012345678901234",
                "nested": {
                    "password": "hunter2",
                    "authorization": "opaque-authorization",
                    "access_token": "opaque-access-token",
                    "token": "opaque-token-value",
                    "bearer_message": "Bearer opaque-bearer-token",
                },
            },
        )

        persisted = store.append(submitted)
        published = await subscription.queue.get()
        for event in (submitted, persisted, published):
            self.assertEqual(event.payload["message"], "Use api_key=[REDACTED]")
            self.assertEqual(event.payload["nested"]["password"], "[REDACTED]")
            self.assertEqual(event.payload["nested"]["authorization"], "[REDACTED]")
            self.assertEqual(event.payload["nested"]["access_token"], "[REDACTED]")
            self.assertEqual(event.payload["nested"]["token"], "[REDACTED]")
            self.assertEqual(event.payload["nested"]["bearer_message"], "Bearer [REDACTED]")
            serialized = str(event.to_dict())
            for secret in (
                "sk-123456789012345678901234",
                "hunter2",
                "opaque-authorization",
                "opaque-access-token",
                "opaque-token-value",
                "opaque-bearer-token",
            ):
                self.assertNotIn(secret, serialized)

    async def test_slow_subscriber_is_marked_for_replay_and_detached(self) -> None:
        broker = EventBroker()
        subscription = broker.subscribe(max_queue_size=1)
        broker.publish(EventEnvelope(event_type="FIRST"))
        broker.publish(EventEnvelope(event_type="OVERFLOW"))

        self.assertTrue(subscription.overflowed)
        self.assertTrue(subscription.queue.full())
        broker.publish(EventEnvelope(event_type="NOT_DELIVERED"))
        self.assertTrue(subscription.queue.full())
        broker.unsubscribe(subscription)


if __name__ == "__main__":
    unittest.main()
