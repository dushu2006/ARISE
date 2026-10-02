from __future__ import annotations

import unittest

from arise.core.contracts import (
    ActionContract,
    AuthorizationContext,
    Condition,
    ConditionOperator,
    ContractValidationError,
    RiskLevel,
    SecretRef,
    TargetIdentity,
    TrustLevel,
    canonical_json,
)


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = AuthorizationContext(
            principal_id="test-user",
            user_intent_id="intent-1",
            trust=TrustLevel.USER_INSTRUCTION,
        )

    def test_action_parameters_are_defensively_frozen_and_secret_refs_stay_references(self) -> None:
        original = {"options": {"safe": True}, "credential": SecretRef("MAIL_TOKEN")}
        action = ActionContract(
            task_id="task-1",
            action_id="action-1",
            tool_name="browser.send",
            target=TargetIdentity(platform="test", object_id="draft-1"),
            risk=RiskLevel.R3,
            authority=self.authority,
            parameters=original,
        )
        original["options"]["safe"] = False
        self.assertTrue(action.parameters["options"]["safe"])
        with self.assertRaises(TypeError):
            action.parameters["options"]["safe"] = False
        serialized = canonical_json(action.parameters)
        self.assertIn('"$secret_ref":"MAIL_TOKEN"', serialized)
        self.assertNotIn("actual-secret-value", serialized)

    def test_condition_is_three_valued_and_has_no_executable_expression(self) -> None:
        equals = Condition("page.ready", expected=True)
        self.assertIsNone(equals.evaluate({}))
        self.assertTrue(equals.evaluate({"page.ready": True}))
        self.assertFalse(equals.evaluate({"page.ready": False}))
        exists = Condition("button.send", operator=ConditionOperator.EXISTS)
        self.assertFalse(exists.evaluate({}))
        self.assertTrue(exists.evaluate({"button.send": None}))

    def test_target_fingerprint_ignores_ephemeral_geometry(self) -> None:
        first = TargetIdentity(
            platform="windows",
            application="mail",
            object_id="draft-1",
            semantic_name="Send",
            bounds=(10, 10, 40, 20),
            confidence=0.9,
        )
        moved = TargetIdentity(
            platform="windows",
            application="mail",
            object_id="draft-1",
            semantic_name="Send",
            bounds=(50, 70, 40, 20),
            confidence=1.0,
        )
        self.assertEqual(first.fingerprint, moved.fingerprint)

    def test_identifiers_and_parameter_sizes_are_bounded(self) -> None:
        with self.assertRaises(ContractValidationError):
            ActionContract(
                task_id="task-1",
                action_id="action-1",
                tool_name="tool\nforged",
                target=None,
                risk=RiskLevel.R0,
                authority=self.authority,
            )
        with self.assertRaises(ContractValidationError):
            ActionContract(
                task_id="task-1",
                action_id="action-1",
                tool_name="test.read",
                target=None,
                risk=RiskLevel.R0,
                authority=self.authority,
                parameters={"large": "x" * 70_000},
            )


if __name__ == "__main__":
    unittest.main()
