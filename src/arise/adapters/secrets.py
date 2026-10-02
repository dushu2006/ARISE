"""Credential-provider ports and opt-in secret-store adapters."""

from __future__ import annotations

import os
from typing import Protocol


class SecretUnavailable(LookupError):
    pass


class SecretProvider(Protocol):
    def get_secret(self, name: str) -> str: ...


class MemorySecretProvider:
    """Test-only secret provider; values are never serialized by ARISE."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get_secret(self, name: str) -> str:
        value = self._values.get(name)
        if not value:
            raise SecretUnavailable("secret is not configured")
        return value


class EnvironmentSecretProvider:
    """Environment lookup is disabled unless an explicit development flag is set."""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled

    def get_secret(self, name: str) -> str:
        if not self.enabled:
            raise SecretUnavailable("environment secret lookup is disabled")
        value = os.environ.get(name)
        if not value:
            raise SecretUnavailable("secret is not configured")
        return value


class KeyringSecretProvider:
    """Optional OS credential-store adapter (install the secure-secrets extra)."""

    def __init__(self, *, service_name: str = "ARISE") -> None:
        self.service_name = service_name

    def get_secret(self, name: str) -> str:
        try:
            import keyring
        except ImportError as exc:
            raise SecretUnavailable("OS keyring support is not installed") from exc
        try:
            value = keyring.get_password(self.service_name, name)
        except Exception as exc:
            raise SecretUnavailable("OS keyring lookup failed") from exc
        if not value:
            raise SecretUnavailable("secret is not configured in the OS keyring")
        return value


class CompositeSecretProvider:
    """Try the OS credential store, then an explicitly enabled dev environment."""

    def __init__(
        self,
        *,
        keyring_provider: SecretProvider | None = None,
        environment_provider: SecretProvider | None = None,
    ) -> None:
        self.keyring_provider = keyring_provider or KeyringSecretProvider()
        self.environment_provider = environment_provider or EnvironmentSecretProvider()

    def get_secret(self, name: str) -> str:
        try:
            return self.keyring_provider.get_secret(name)
        except SecretUnavailable:
            return self.environment_provider.get_secret(name)
