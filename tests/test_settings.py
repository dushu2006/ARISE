from __future__ import annotations

import unittest

from pydantic import ValidationError

from arise.config.settings import (
    ApiSettings,
    AppSettings,
    EmbeddingSettings,
    MemorySettings,
    ModelSettings,
    RuntimeSettings,
    SecuritySettings,
    VoiceSettings,
)


class SettingsValidationTests(unittest.TestCase):
    def test_api_bind_host_must_be_loopback(self) -> None:
        for host in ("0.0.0.0", "192.168.1.50", "example.com"):
            with self.subTest(host=host), self.assertRaises(ValidationError):
                ApiSettings(host=host)

    def test_request_body_timeout_is_bounded(self) -> None:
        for timeout in (0, -1, 301):
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                ApiSettings(request_body_timeout_seconds=timeout)

    def test_task_history_retention_is_optional_and_bounded(self) -> None:
        self.assertIsNone(RuntimeSettings().task_history_retention_days)
        self.assertEqual(
            RuntimeSettings(task_history_retention_days=365).task_history_retention_days, 365
        )
        for days in (0, -1, 3651):
            with self.subTest(days=days), self.assertRaises(ValidationError):
                RuntimeSettings(task_history_retention_days=days)

    def test_websocket_replay_limit_is_bounded(self) -> None:
        self.assertEqual(ApiSettings().websocket_replay_limit, 5000)
        for limit in (0, 99, 100_001):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                ApiSettings(websocket_replay_limit=limit)

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

    def test_embedding_provider_requires_model_endpoint_and_explicit_cloud_memory_opt_ins(
        self,
    ) -> None:
        with self.assertRaises(ValidationError):
            EmbeddingSettings(enabled=True, base_url="http://127.0.0.1:1234/v1")
        with self.assertRaises(ValidationError):
            AppSettings(
                embeddings=EmbeddingSettings(
                    enabled=True,
                    base_url="https://embeddings.example.org/v1",
                    model_id="embed-v1",
                    allow_cloud=True,
                ),
                security=SecuritySettings(environment="test", allow_cloud_models=True),
            )
        settings = AppSettings(
            embeddings=EmbeddingSettings(
                enabled=True,
                base_url="https://embeddings.example.org/v1",
                model_id="embed-v1",
                allow_cloud=True,
            ),
            memory=MemorySettings(allow_cloud_embeddings=True),
            security=SecuritySettings(environment="test", allow_cloud_models=True),
        )
        self.assertTrue(settings.embeddings.enabled)

    def test_voice_defaults_are_dormant_and_local_voice_does_not_require_cloud(self) -> None:
        self.assertFalse(VoiceSettings().enabled)
        self.assertEqual(VoiceSettings().inactivity_timeout_seconds, 30)
        local_voice = AppSettings(voice=VoiceSettings(enabled=True))
        self.assertTrue(local_voice.voice.enabled)
        with self.assertRaises(ValidationError):
            AppSettings(voice=VoiceSettings(enabled=True, allow_cloud=True))
        with self.assertRaises(ValidationError):
            AppSettings(
                voice=VoiceSettings(enabled=True, allow_cloud=True),
                security=SecuritySettings(allow_cloud_models=False),
            )
        enabled = AppSettings(
            voice=VoiceSettings(enabled=True, allow_cloud=True),
            security=SecuritySettings(allow_cloud_models=True),
        )
        self.assertTrue(enabled.voice.allow_cloud)

    def test_microphone_requires_separate_voice_opt_in_and_user_model_path(self) -> None:
        with self.assertRaises(ValidationError):
            VoiceSettings(microphone_enabled=True, local_model_path="/models/vosk")
        with self.assertRaises(ValidationError):
            VoiceSettings(enabled=True, microphone_enabled=True)
        enabled = VoiceSettings(
            enabled=True,
            microphone_enabled=True,
            local_model_path="/models/vosk-en",
        )
        self.assertTrue(enabled.microphone_enabled)
        self.assertEqual(enabled.local_model_path.as_posix(), "/models/vosk-en")

    def test_voice_timeout_secret_name_and_model_are_bounded(self) -> None:
        for timeout in (4, 3601):
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                VoiceSettings(inactivity_timeout_seconds=timeout)
        for name in ("bad-name", "", "KEY=VALUE"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                VoiceSettings(api_key_secret_name=name)
        with self.assertRaises(ValidationError):
            VoiceSettings(model_id=" ")


if __name__ == "__main__":
    unittest.main()
