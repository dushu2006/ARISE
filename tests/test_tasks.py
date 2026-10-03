from __future__ import annotations

import unittest

from arise.core.contracts import AuthorizationContext, TrustLevel
from arise.core.tasks import (
    InMemoryTaskRepository,
    InvalidTaskTransition,
    TaskRecord,
    TaskStatus,
)


class TaskStateTransitionTests(unittest.TestCase):
    def test_in_memory_task_listing_is_scoped_to_authenticated_principal(self) -> None:
        tasks = InMemoryTaskRepository()
        own = TaskRecord.new(
            "Own task",
            authorization=AuthorizationContext(
                principal_id="principal-a",
                user_intent_id="request-a",
                trust=TrustLevel.USER_INSTRUCTION,
            ),
            request_id="request-a",
            session_id="session-a",
        )
        other = TaskRecord.new(
            "Other task",
            authorization=AuthorizationContext(
                principal_id="principal-b",
                user_intent_id="request-b",
                trust=TrustLevel.USER_INSTRUCTION,
            ),
            request_id="request-b",
            session_id="session-b",
        )
        tasks.create_or_get(own)
        tasks.create_or_get(other)

        self.assertEqual(
            [task.task_id for task in tasks.list_for_principal(principal_id="principal-a")],
            [own.task_id],
        )

    def test_interrupted_task_can_be_reconciled_or_requeued(self) -> None:
        task = TaskRecord.new("Recover this task")
        task.transition_to(TaskStatus.QUEUED)
        task.transition_to(TaskStatus.INTERRUPTED, reason="process restarted")
        task.transition_to(TaskStatus.REQUIRES_USER_INPUT, reason="reconciliation required")
        self.assertEqual(task.status, TaskStatus.REQUIRES_USER_INPUT)
        task.transition_to(TaskStatus.QUEUED, reason="user provided a fresh instruction")
        self.assertEqual(task.status, TaskStatus.QUEUED)

    def test_waiting_user_can_expire_into_explicit_input_required_state(self) -> None:
        task = TaskRecord.planned("Approve an action")
        task.transition_to(TaskStatus.WAITING_USER)
        task.transition_to(TaskStatus.REQUIRES_USER_INPUT, reason="approval expired")
        task.transition_to(TaskStatus.QUEUED, reason="replan after user input")
        self.assertEqual(task.status, TaskStatus.QUEUED)

    def test_invalid_interrupted_transition_is_still_rejected(self) -> None:
        task = TaskRecord.planned("State machine check")
        task.transition_to(TaskStatus.INTERRUPTED)
        with self.assertRaises(InvalidTaskTransition):
            task.transition_to(TaskStatus.COMPLETED, verification_passed=True)
        self.assertEqual(task.status, TaskStatus.INTERRUPTED)

    def test_running_task_cannot_skip_verification(self) -> None:
        task = TaskRecord.planned("State machine check")
        task.transition_to(TaskStatus.RUNNING)
        with self.assertRaises(InvalidTaskTransition):
            task.transition_to(TaskStatus.COMPLETED, verification_passed=True)
