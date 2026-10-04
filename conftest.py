"""Pytest-only settings isolation; production dotenv loading is unchanged.

The configure hook runs before test-module imports (an autouse fixture alone is
too late for collection-time AppSettings calls). Each test also gets a clean
working directory so real backend subprocesses cannot load the repository .env.
Tests may deliberately use monkeypatch.setenv / patch.dict, or pass an explicit
AppSettings(_env_file=...) pointing at a synthetic dotenv file.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from arise.config.settings import AppSettings, get_settings

_PROVIDER_CREDENTIALS = frozenset(
    {
        "NVIDIA_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LOCAL_EMBEDDINGS_KEY",
        "BRAVE_SEARCH_API_KEY",
    }
)


def _clear_inherited_configuration(patch: pytest.MonkeyPatch) -> None:
    # Snapshot names only for deletion. Never log inherited settings or secrets.
    referenced_secrets = {
        value.upper()
        for name, value in os.environ.items()
        if name.upper().startswith("ARISE__") and name.upper().endswith("SECRET_NAME")
    }
    for name in tuple(os.environ):
        upper = name.upper()
        if (
            upper.startswith("ARISE_")
            or upper in _PROVIDER_CREDENTIALS
            or upper in referenced_secrets
        ):
            patch.delenv(name, raising=False)


def pytest_configure(config: pytest.Config) -> None:
    patch = pytest.MonkeyPatch()
    config.add_cleanup(patch.undo)
    config.add_cleanup(get_settings.cache_clear)
    _clear_inherited_configuration(patch)
    patch.setitem(AppSettings.model_config, "env_file", None)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_settings_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Reset settings/cache per test, including unittest.TestCase tests.

    Keep OS essentials such as PATH/SystemRoot for native subprocess tests.
    Test-owned environment overrides applied after fixture setup still work.
    Repository assets should be located relative to __file__, not the cwd.
    """
    _clear_inherited_configuration(monkeypatch)
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
