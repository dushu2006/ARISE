from __future__ import annotations

import asyncio
import unittest

from arise.core.resources import ResourceAcquisitionTimeout, ResourceManager


class ResourceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_exclusive_resource_is_released_for_next_task(self) -> None:
        manager = ResourceManager()
        queued = asyncio.Event()
        acquired_by_second = asyncio.Event()

        async with manager.acquire_many("task-a", ["MOUSE", "WINDOW"], lease_seconds=2):
            self.assertEqual(await manager.owner("MOUSE"), "task-a")

            async def second_task() -> None:
                queued.set()
                async with manager.acquire_many(
                    "task-b", ["WINDOW", "MOUSE"], wait_timeout=1, lease_seconds=2
                ):
                    acquired_by_second.set()

            waiting = asyncio.create_task(second_task())
            await queued.wait()
            await asyncio.sleep(0)
            self.assertFalse(acquired_by_second.is_set())

        await asyncio.wait_for(acquired_by_second.wait(), timeout=1)
        await waiting
        self.assertIsNone(await manager.owner("MOUSE"))
        self.assertIsNone(await manager.owner("WINDOW"))

    async def test_wait_timeout_does_not_leave_a_waiter_or_lock(self) -> None:
        manager = ResourceManager()
        async with manager.acquire_many("owner", ["CLIPBOARD"], lease_seconds=2):
            with self.assertRaises(ResourceAcquisitionTimeout):
                async with manager.acquire_many(
                    "waiter", ["CLIPBOARD"], wait_timeout=0.01, lease_seconds=1
                ):
                    self.fail("waiter must not acquire a locked resource")
            self.assertEqual(await manager.owner("CLIPBOARD"), "owner")
        self.assertIsNone(await manager.owner("CLIPBOARD"))

    async def test_cancellation_releases_a_granted_lease(self) -> None:
        manager = ResourceManager()
        entered = asyncio.Event()

        async def hold() -> None:
            async with manager.acquire_many("cancelled-task", ["KEYBOARD"], lease_seconds=5):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        self.assertEqual(await manager.owner("KEYBOARD"), "cancelled-task")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(await manager.owner("KEYBOARD"))

    async def test_resources_are_acquired_as_a_sorted_atomic_set(self) -> None:
        manager = ResourceManager()
        async with manager.acquire_many("task", ["WINDOW", "MOUSE", "MOUSE"]):
            self.assertEqual(await manager.owner("MOUSE"), "task")
            self.assertEqual(await manager.owner("WINDOW"), "task")


if __name__ == "__main__":
    unittest.main()
