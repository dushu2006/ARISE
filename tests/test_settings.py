from __future__ import annotations

import unittest

from pydantic import ValidationError

from arise.config.settings import ApiSettings, ModelSettings, SecuritySettings


class SettingsValidationTests(unittest.TestCase):
    def test_api_bind_host_must_be_loopback(self) -> None:
        for host in ("0.0.0.0", "192.168.1.50", "example.com"):
            with self.subTest(host=host), self.assertRaises(ValidationError):
                ApiSettings(host=host)

    def test_request_body_timeout_is_bounded(self) -> None:
        for timeout in (0, -1, 301):
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                ApiSettings(request_body_timeout_seconds=timeout)

    def test_api_auth_token_is_bounded_visible_ascii(self) -> None:
        tokens = ("short", "x" * 32 + " ", "x" * 16 + " " + "x" * 16, "é" * 32, "x" * 513)
        for token in tokens:
            with self.subTest(token=token[:8]), self.assertRaises(ValidationError):
                ApiSettings(auth_token=token)

    def test_wildcard_origin_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            ApiSettings(trusted_origins=["*"])

    def test_production_requires_api_auth_and_forbids_environment_secrets(self) -> None:
        with self.assertRaises(ValidationError):
            SecuritySettings(environment="production", require_api_auth=False)
        with self.assertRaises(ValidationError):
            SecuritySettings(environment="production", allow_environment_secrets=True)

    def test_model_credentials_must_use_a_secret_reference(self) -> None:
        with self.assertRaises(ValidationError):
            ModelSettings(
                base_url="https://user:password@example.com/v1",
                model_id="model-a",
            )
        with self.assertRaises(ValidationError):
            ModelSettings(
                base_url="https://example.com/v1?api_key=secret",
                model_id="model-a",
            )


if __name__ == "__main__":
    unittest.main()
