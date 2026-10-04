"""Single, validated configuration boundary for the Python runtime."""

from __future__ import annotations

import ipaddress
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_api_token(value: str) -> str:
    """Validate the URL-safe bearer-token format shared with the desktop bridge."""

    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 512
        or not value.isascii()
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        raise ValueError("API tokens must contain 32 to 512 visible ASCII characters")
    return value


def default_data_dir() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "ARISE"
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "arise"


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_concurrent_tasks: int = Field(default=2, ge=1, le=16)
    max_queued_tasks: int = Field(default=64, ge=1, le=10_000)
    task_timeout_seconds: float = Field(default=900.0, gt=0, le=86_400)
    resource_wait_timeout_seconds: float = Field(default=15.0, ge=0, le=3600)
    task_history_limit: int = Field(default=500, ge=1, le=100_000)
    task_history_retention_days: int | None = Field(default=None, ge=1, le=3650)


class ApiSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=0, le=65_535)
    trusted_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "tauri://localhost",
            "http://tauri.localhost",
            "https://tauri.localhost",
        ]
    )
    websocket_heartbeat_seconds: float = Field(default=25.0, gt=0, le=300)
    websocket_client_queue_size: int = Field(default=128, ge=8, le=4096)
    websocket_replay_limit: int = Field(default=5000, ge=100, le=100_000)
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    request_body_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    auth_token: SecretStr | None = None

    @field_validator("host")
    @classmethod
    def validate_host(cls, host: str) -> str:
        host = host.strip()
        if host.lower() == "localhost":
            return host
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError as exc:
            raise ValueError("the local API must bind to localhost or a loopback IP") from exc
        if not loopback:
            raise ValueError("the local API must bind to localhost or a loopback IP")
        return host

    @field_validator("auth_token")
    @classmethod
    def validate_auth_token(cls, token: SecretStr | None) -> SecretStr | None:
        if token is not None:
            validate_api_token(token.get_secret_value())
        return token

    @field_validator("trusted_origins")
    @classmethod
    def validate_origins(cls, origins: list[str]) -> list[str]:
        if not origins or any(not origin.strip() for origin in origins):
            raise ValueError("at least one non-empty trusted origin is required")
        if "*" in origins:
            raise ValueError("wildcard origins are forbidden for the local control API")
        return list(dict.fromkeys(origin.strip().rstrip("/") for origin in origins))


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: Path | None = None
    busy_timeout_ms: int = Field(default=5000, ge=100, le=60_000)


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider_id: str | None = None
    base_url: AnyHttpUrl | None = None
    model_id: str | None = None
    api_key_secret_name: str = "NVIDIA_API_KEY"
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    request_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_concurrent_requests: int = Field(default=4, ge=1, le=128)
    allow_cloud: bool = False

    @field_validator("api_key_secret_name")
    @classmethod
    def validate_secret_name(cls, name: str) -> str:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("secret name must be an environment/keyring identifier")
        return name

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> ModelSettings:
        if (self.base_url is None) != (self.model_id is None):
            raise ValueError("model base_url and model_id must be configured together")
        if self.base_url is not None and any(
            value is not None
            for value in (
                self.base_url.username,
                self.base_url.password,
                self.base_url.query,
                self.base_url.fragment,
            )
        ):
            raise ValueError("model base_url cannot contain credentials, query, or fragment data")
        for name, value in (("provider_id", self.provider_id), ("model_id", self.model_id)):
            if value is not None and (not value.strip() or len(value) > 256):
                raise ValueError(f"model {name} must be non-empty and at most 256 characters")
        if self.provider_id is not None and self.base_url is None:
            raise ValueError("model provider_id requires a base_url and model_id")
        return self


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    microphone_enabled: bool = False
    local_model_path: Path | None = None
    kokoro_model_path: Path | None = None
    kokoro_voices_path: Path | None = None
    tts_voice: str = "af_heart"
    enable_local_tts_acknowledgement: bool = True
    microphone_device_id: str | None = None
    playback_device_id: str | None = None
    wake_word: str = "ARISE"
    wake_word_min_confidence: float = Field(default=0.72, ge=0, le=1)
    minimum_asr_confidence: float = Field(default=0.65, ge=0, le=1)
    vad_aggressiveness: int = Field(default=2, ge=0, le=3)
    locale: str = "en"
    model_id: str = "gemini-3.8-live"
    api_key_secret_name: str = "GEMINI_API_KEY"
    allow_cloud: bool = False
    inactivity_timeout_seconds: int = Field(default=30, ge=5, le=3600)
    connect_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    max_reconnect_attempts: int = Field(default=2, ge=0, le=10)
    reconnect_backoff_seconds: float = Field(default=0.5, ge=0, le=30)

    @field_validator("api_key_secret_name")
    @classmethod
    def validate_secret_name(cls, name: str) -> str:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("secret name must be an environment/keyring identifier")
        return name

    @field_validator("model_id", "tts_voice")
    @classmethod
    def validate_model_id(cls, model_id: str) -> str:
        if not model_id.strip() or len(model_id) > 256:
            raise ValueError("voice model_id and tts_voice must be non-empty and at most 256 chars")
        return model_id.strip()

    @field_validator("wake_word", "locale")
    @classmethod
    def validate_voice_tokens(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 32:
            raise ValueError("voice wake word and locale must contain 1 to 32 characters")
        return normalized

    @field_validator("microphone_device_id", "playback_device_id")
    @classmethod
    def validate_device_id(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 256):
            raise ValueError("audio device identifier must be non-empty and bounded")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def validate_microphone_opt_in(self) -> VoiceSettings:
        if self.microphone_enabled and not self.enabled:
            raise ValueError("microphone activation requires voice.enabled")
        if self.microphone_enabled and self.local_model_path is None:
            raise ValueError("microphone activation requires a user-supplied local Vosk model path")
        if (self.kokoro_model_path is None) != (self.kokoro_voices_path is None):
            raise ValueError("kokoro_model_path and kokoro_voices_path must be configured together")
        return self


class DesktopSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    max_tree_depth: int = Field(default=8, ge=1, le=20)
    max_nodes: int = Field(default=250, ge=10, le=2000)


class BrowserSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    allowed_domains: list[str] = Field(default_factory=list)
    allow_private_network: bool = False


class PerceptionSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    allow_coordinate_fallback: bool = False
    minimum_vision_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    # Physical virtual-desktop rectangles: (x, y, width, height).
    unsafe_regions: tuple[tuple[float, float, float, float], ...] = ()

    @field_validator("unsafe_regions")
    @classmethod
    def validate_unsafe_regions(
        cls, regions: tuple[tuple[float, float, float, float], ...]
    ) -> tuple[tuple[float, float, float, float], ...]:
        if len(regions) > 64:
            raise ValueError("at most 64 coordinate fallback unsafe regions may be configured")
        validated: list[tuple[float, float, float, float]] = []
        for region in regions:
            if len(region) != 4:
                raise ValueError("each unsafe region must be (x, y, width, height)")
            values = tuple(float(value) for value in region)
            if not all(math.isfinite(value) for value in values):
                raise ValueError("unsafe region coordinates must be finite")
            if values[2] <= 0 or values[3] <= 0:
                raise ValueError("unsafe region width and height must be positive")
            validated.append(values)
        return tuple(validated)


class EmbeddingSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    use_local_fallback: bool = False
    base_url: AnyHttpUrl | None = None
    model_id: str | None = None
    api_key_secret_name: str = "LOCAL_EMBEDDINGS_KEY"
    allow_cloud: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    @field_validator("api_key_secret_name")
    @classmethod
    def validate_secret_name(cls, name: str) -> str:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("secret name must be an environment/keyring identifier")
        return name

    @model_validator(mode="after")
    def validate_endpoint(self) -> EmbeddingSettings:
        if (self.base_url is None) != (self.model_id is None):
            raise ValueError("embedding base_url and model_id must be configured together")
        if self.model_id is not None and (not self.model_id.strip() or len(self.model_id) > 256):
            raise ValueError("embedding model_id must be non-empty and at most 256 characters")
        if self.base_url is not None and any(
            value is not None
            for value in (
                self.base_url.username,
                self.base_url.password,
                self.base_url.query,
                self.base_url.fragment,
            )
        ):
            raise ValueError("embedding base_url cannot contain credentials, query, or fragment")
        if self.allow_cloud and not self.enabled:
            raise ValueError("cloud embeddings require embedding.enabled")
        return self


class ResearchSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    provider: Literal["brave"] = "brave"
    api_key_secret_name: str = "BRAVE_SEARCH_API_KEY"
    max_results: int = Field(default=8, ge=1, le=25)
    max_source_fetches: int = Field(default=5, ge=0, le=10)
    timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    max_source_bytes: int = Field(default=524_288, ge=1024, le=4_194_304)
    fetch_pages: bool = True

    @field_validator("api_key_secret_name")
    @classmethod
    def validate_secret_name(cls, name: str) -> str:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("secret name must be an environment/keyring identifier")
        return name


class MemorySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    allow_cloud_context: bool = False
    allow_cloud_embeddings: bool = False
    max_records_per_principal: int = Field(default=1000, ge=1, le=100_000)
    default_retention_days: int = Field(default=365, ge=1, le=3650)
    max_retention_days: int = Field(default=3650, ge=1, le=3650)
    consent_lifetime_seconds: int = Field(default=120, ge=1, le=600)

    @model_validator(mode="after")
    def validate_retention_bounds(self) -> MemorySettings:
        if self.default_retention_days > self.max_retention_days:
            raise ValueError("memory default retention cannot exceed the maximum")
        return self


class SecuritySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    require_api_auth: bool = True
    allow_environment_secrets: bool = False
    allow_cloud_models: bool = False
    allow_web_research: bool = False
    confirmation_timeout_seconds: float = Field(default=90.0, gt=0, le=600)

    @model_validator(mode="after")
    def constrain_development_shortcuts(self) -> SecuritySettings:
        if self.allow_environment_secrets and self.environment == "production":
            raise ValueError("environment-variable secret lookup is development-only")
        if self.environment == "production" and not self.require_api_auth:
            raise ValueError("API authentication cannot be disabled in production")
        return self


class LoggingSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class AppSettings(BaseSettings):
    """Application settings; nested environment names use ``ARISE__``."""

    model_config = SettingsConfigDict(
        env_prefix="ARISE__",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "ARISE"
    app_version: str = "0.1.0"
    data_dir: Path = Field(default_factory=default_data_dir)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    model: ModelSettings = Field(default_factory=ModelSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    desktop: DesktopSettings = Field(default_factory=DesktopSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    perception: PerceptionSettings = Field(default_factory=PerceptionSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @model_validator(mode="after")
    def require_explicit_cloud_opt_ins(self) -> AppSettings:
        if self.voice.allow_cloud and not (self.voice.enabled and self.security.allow_cloud_models):
            raise ValueError(
                "Gemini Live requires voice.enabled and both voice/security cloud opt-ins"
            )
        if self.embeddings.allow_cloud and not (
            self.security.allow_cloud_models and self.memory.allow_cloud_embeddings
        ):
            raise ValueError(
                "Cloud embeddings require security.allow_cloud_models and "
                "memory.allow_cloud_embeddings"
            )
        return self

    @property
    def database_path(self) -> Path:
        configured = self.database.path
        if configured is not None:
            return configured.expanduser()
        return self.data_dir.expanduser() / "arise.sqlite3"


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Load configuration once. Tests should inject settings rather than mutate env."""

    return AppSettings()
