"""Synthetic developer configuration regressions; no real credentials are used."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from arise.config.settings import AppSettings, VoiceSettings, get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_DOTENV = """\
ARISE__VOICE__ENABLED=true
ARISE__VOICE__ALLOW_CLOUD=true
ARISE__SECURITY__ALLOW_CLOUD_MODELS=true
ARISE__MODEL__ALLOW_CLOUD=true
ARISE__API__AUTH_TOKEN=synthetic-test-token-not-a-real-credential
NVIDIA_API_KEY=synthetic-test-key-not-a-real-credential
"""


def assert_local_defaults(settings: AppSettings) -> None:
    assert settings.voice.enabled is False
    assert settings.voice.allow_cloud is False
    assert settings.security.allow_cloud_models is False
    assert settings.model.allow_cloud is False
    assert settings.api.auth_token is None


def test_implicit_dotenv_is_ignored_but_explicit_test_dotenv_can_be_loaded(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(SYNTHETIC_DOTENV, encoding="utf-8")
    assert_local_defaults(AppSettings())
    assert_local_defaults(get_settings())
    with pytest.raises(ValidationError, match="Gemini Live requires"):
        AppSettings(voice=VoiceSettings(enabled=True, allow_cloud=True))

    explicit = AppSettings(_env_file=dotenv)
    assert explicit.voice.enabled is True
    assert explicit.voice.allow_cloud is True
    assert explicit.security.allow_cloud_models is True


def test_intentional_environment_overrides_and_security_validation_still_work(monkeypatch):
    monkeypatch.setenv("ARISE__VOICE__ENABLED", "true")
    monkeypatch.setenv("ARISE__VOICE__ALLOW_CLOUD", "true")
    with pytest.raises(ValidationError, match="Gemini Live requires"):
        AppSettings()
    monkeypatch.setenv("ARISE__SECURITY__ALLOW_CLOUD_MODELS", "true")
    assert get_settings().voice.allow_cloud is True
    # Leave the cache populated: the autouse fixture must clear it at teardown.


def test_defaults_and_cache_start_clean_for_each_test():
    assert get_settings.cache_info().currsize == 0
    assert_local_defaults(get_settings())


def test_plain_python_still_loads_production_dotenv(tmp_path):
    """A fresh interpreter does not inherit pytest's in-process settings patch."""
    (tmp_path / ".env").write_text(SYNTHETIC_DOTENV, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from arise.config.settings import AppSettings; "
            "s = AppSettings(); "
            "assert AppSettings.model_config['env_file'] == '.env'; "
            "assert s.voice.enabled and s.voice.allow_cloud; "
            "assert s.security.allow_cloud_models and s.model.allow_cloud; "
            "print('production dotenv loading unchanged')",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == "production dotenv loading unchanged"


def test_pytest_isolated_before_collection_and_in_backend_subprocesses(tmp_path):
    """Run actual pytest under both a polluted shell and synthetic developer .env."""
    project = tmp_path / "polluted-project"
    project.mkdir()
    (project / ".env").write_text(SYNTHETIC_DOTENV, encoding="utf-8")
    (project / "conftest.py").write_text(
        (REPO_ROOT / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (project / "test_probe.py").write_text(
        """\
import os
import subprocess
import sys
import unittest
from pathlib import Path
from pydantic import ValidationError
from arise.config.settings import AppSettings, VoiceSettings, get_settings

# Must already be isolated during collection, before any autouse fixture runs.
s = get_settings()
assert not s.voice.enabled and not s.voice.allow_cloud
assert not s.security.allow_cloud_models and not s.model.allow_cloud
assert s.api.auth_token is None
assert not any(k.upper().startswith("ARISE_") for k in os.environ)
assert "NVIDIA_API_KEY" not in os.environ
assert "SYNTHETIC_PROVIDER_CREDENTIAL" not in os.environ

class TestUnittestIsolation(unittest.TestCase):
    def test_validation_and_child_process(self):
        assert get_settings.cache_info().currsize == 0
        assert not Path(".env").exists()
        with self.assertRaises(ValidationError):
            AppSettings(voice=VoiceSettings(enabled=True, allow_cloud=True))
        subprocess.run([
            sys.executable, "-c",
            "from arise.config.settings import AppSettings; "
            "s = AppSettings(); "
            "assert not s.voice.enabled and not s.voice.allow_cloud; "
            "assert not s.security.allow_cloud_models; "
            "assert s.api.auth_token is None"
        ], check=True, timeout=30)

def test_explicit_environment(monkeypatch):
    monkeypatch.setenv("ARISE__VOICE__ENABLED", "true")
    assert AppSettings().voice.enabled
""",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.update(
        {
            "ARISE__VOICE__ENABLED": "true",
            "ARISE__VOICE__ALLOW_CLOUD": "true",
            "ARISE__SECURITY__ALLOW_CLOUD_MODELS": "true",
            "ARISE__MODEL__ALLOW_CLOUD": "true",
            "ARISE__MODEL__API_KEY_SECRET_NAME": "SYNTHETIC_PROVIDER_CREDENTIAL",
            "SYNTHETIC_PROVIDER_CREDENTIAL": "synthetic-private-marker",
            "NVIDIA_API_KEY": "synthetic-private-marker",
            # Pydantic settings are case-insensitive, even on case-sensitive hosts.
            "arise__api__auth_token": "synthetic-test-token-not-a-real-credential",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "test_probe.py"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert "synthetic-private-marker" not in result.stdout + result.stderr
