"""Regression tests for the shared best-effort secret redactor.

These are REAL deterministic unit tests of `arise.core.redaction`, the single
boundary used by event envelopes, persisted conversation turns, task goals,
planner input, research queries, diagnostics, and browser/perception metadata.
"""

from __future__ import annotations

import unittest

from arise.core.redaction import DEFAULT_REDACTOR, SecretRedactor


class CredentialShapeRedactionTests(unittest.TestCase):
    def test_labelled_credentials_bearer_and_provider_keys_are_redacted(self) -> None:
        cases = {
            "login with password: UNIQPASSCOLON001": "login with password: [REDACTED]",
            "set api_key=UNIQAPIKEYEQUAL003": "set api_key=[REDACTED]",
            "key is sk-proj-UNIQOPENAISTYLE00004": "key is [REDACTED]",
            "use Bearer eyJUNIQBEARERTOKEN0005 now": "use Bearer [REDACTED] now",
            "token: AKIAUNIQAWSKEY000006": "token: [REDACTED]",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(DEFAULT_REDACTOR.redact(source), expected)
                # Redaction is idempotent: no residual secret-shaped value survives.
                self.assertEqual(DEFAULT_REDACTOR.redact(expected), expected)

    def test_payment_card_numbers_are_redacted_in_every_common_format(self) -> None:
        for source in (
            "card 4111111111111111 expires",
            "card 4111 1111 1111 1111 expires",
            "card 4111-1111-1111-1111 expires",
            "credit card: 4111 1111 1111 1111",
            "amex 378282246310005 on file",
            "my cvv is 4111111111111111 for the record",
        ):
            with self.subTest(source=source):
                redacted = DEFAULT_REDACTOR.redact(source)
                self.assertIn("[REDACTED]", redacted)
                self.assertNotIn("4111111111111111", redacted)
                self.assertNotIn("4111 1111 1111 1111", redacted)
                self.assertNotIn("378282246310005", redacted)

    def test_non_card_digit_runs_are_preserved(self) -> None:
        for source in (
            "order 1234567890123456 please",
            "set timeout to 4111111111111 and open the app",
            "id 123456789012345678",
            "timestamp 1561962224760 ns",
            "my phone is +1 555 123 4567",
            "invoice 2026-10-04 number 42",
        ):
            with self.subTest(source=source):
                self.assertEqual(DEFAULT_REDACTOR.redact(source), source)

    def test_redaction_is_stable_across_repeated_passes(self) -> None:
        once = DEFAULT_REDACTOR.redact("card 4111-1111-1111-1111 and password: abc123")
        self.assertEqual(DEFAULT_REDACTOR.redact(once), once)
        self.assertEqual(once, "card [REDACTED] and password: [REDACTED]")

    def test_redact_object_recurses_and_masks_secret_shaped_keys(self) -> None:
        redactor = SecretRedactor()
        payload = {
            "authorization": "Bearer opaque-token-value",
            "nested": {"notes": "card 4111111111111111", "count": 7},
            "items": [{"value": "password: hunter2"}],
        }
        safe = redactor.redact_object(payload)
        self.assertEqual(safe["authorization"], "[REDACTED]")
        self.assertEqual(safe["nested"]["notes"], "card [REDACTED]")
        self.assertEqual(safe["nested"]["count"], 7)
        self.assertEqual(safe["items"][0]["value"], "password: [REDACTED]")

    def test_non_text_input_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            DEFAULT_REDACTOR.redact(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
