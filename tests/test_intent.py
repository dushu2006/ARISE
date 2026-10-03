from __future__ import annotations

import unittest

from arise.core.intent import IntentClassifier, IntentKind


class IntentClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = IntentClassifier()

    def test_question_and_task_requests_are_distinguished(self) -> None:
        self.assertEqual(
            self.classifier.classify("What is NVIDIA's latest GPU?").kind,
            IntentKind.QUESTION,
        )
        self.assertEqual(
            self.classifier.classify("Can you open Chrome?").kind,
            IntentKind.COMMAND,
        )
        self.assertEqual(
            self.classifier.classify("Will you open Chrome?").kind,
            IntentKind.COMMAND,
        )
        self.assertEqual(
            self.classifier.classify("How do I open Chrome?").kind,
            IntentKind.QUESTION,
        )

    def test_information_requests_containing_action_verbs_are_not_executable_commands(self) -> None:
        for text in (
            "Tell me how to open Chrome",
            "Please tell me how to open Chrome",
            "Please explain how to open Chrome",
            "Show me the steps to open Chrome",
        ):
            with self.subTest(text=text):
                classification = self.classifier.classify(text)
                self.assertEqual(classification.kind, IntentKind.QUESTION)
                self.assertFalse(classification.may_require_runtime_task)
        self.assertEqual(
            self.classifier.classify("Please open Chrome").kind,
            IntentKind.COMMAND,
        )

    def test_multi_step_follow_up_status_and_cancellation(self) -> None:
        self.assertEqual(
            self.classifier.classify("Open Chrome and then search NVIDIA").kind,
            IntentKind.MULTI_STEP_TASK,
        )
        self.assertEqual(
            self.classifier.classify("Also compare the first three sources").kind,
            IntentKind.FOLLOW_UP,
        )
        self.assertEqual(
            self.classifier.classify("What is the task status?").kind,
            IntentKind.STATUS_REQUEST,
        )
        self.assertEqual(self.classifier.classify("Stop").kind, IntentKind.CANCELLATION)

    def test_heuristics_are_advisory_and_do_not_assign_authority(self) -> None:
        result = self.classifier.classify("I want you to open Chrome")
        self.assertEqual(result.kind, IntentKind.TASK)
        self.assertTrue(result.may_require_runtime_task)
        self.assertFalse(hasattr(result, "authorization"))

    def test_empty_input_requests_clarification(self) -> None:
        self.assertEqual(self.classifier.classify("   ").kind, IntentKind.CLARIFICATION)


if __name__ == "__main__":
    unittest.main()
