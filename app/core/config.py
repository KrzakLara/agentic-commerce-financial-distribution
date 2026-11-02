"""Environment-based application settings (Pydantic Settings).

All variables use the ``TRUSTEQ_`` prefix and may be provided through the process
environment or a local, git-ignored ``.env`` file. Defaults are safe and never
require an external API key: the AI execution mode defaults to ``scripted``.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AIExecutionMode(StrEnum):
    """How the customer-side agent decides on tool calls (ADR-09)."""

    SCRIPTED = "scripted"  # deterministic, no API key
    LLM = "llm"  # optional, behind an explicit flag; not implemented in Phase 00


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRUSTEQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "TRUSTEQ Secure Agentic Depot Onboarding"
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    api_version: str = "v1"

    database_url: str = "sqlite:///./trusteq.db"

    log_level: LogLevel = "INFO"
    log_json: bool = True

    ai_execution_mode: AIExecutionMode = AIExecutionMode.SCRIPTED
    # Real model calls require this explicit flag in addition to the mode (ADR-09).
    enable_llm: bool = False
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None

    # DEV/TEST ONLY stand-in for real scoped authentication (planned for Phase 03). Lets a
    # caller name its own actor via the X-Dev-Actor-Id header. It is never honoured in
    # production (see `dev_actor_auth_active`); without it every /v1 route answers 401.
    dev_actor_auth_enabled: bool = True
    # DEV/TEST ONLY simulated *customer* sign-in (X-Dev-Customer-Id header / dev sign-in page).
    # Separate from the agent context above. Not digital identity verification; never active
    # in production.
    dev_customer_auth_enabled: bool = True

    # Lifetimes (seconds) of the customer handoff challenge and the onboarding mandate.
    handoff_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    mandate_ttl_seconds: int = Field(default=3600, ge=60, le=86400)

    # Shared secret for the MOCK depot provider's signed confirmation callback. Unset (the
    # default) disables the callback endpoint. Refused in production: the mock provider is a
    # prototype stand-in, not a trusted production counterparty.
    mock_provider_callback_secret: SecretStr | None = None

    allowed_origins: Annotated[list[str], Field(max_length=20)] = [
        "http://localhost:3000",
        "http://localhost:5173",
    ]

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator(
        "llm_provider",
        "llm_model",
        "llm_base_url",
        "llm_api_key",
        "mock_provider_callback_secret",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        # ".env.example" ships blank placeholders; treat them as "not configured".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.environment is Environment.PRODUCTION:
            if self.debug:
                raise ValueError("debug must be false in production")
            if "*" in self.allowed_origins:
                raise ValueError("wildcard CORS origin is not allowed in production")
            for flag in ("dev_actor_auth_enabled", "dev_customer_auth_enabled"):
                if getattr(self, flag) and flag in self.model_fields_set:
                    raise ValueError(f"{flag} must not be set in production")
            if self.mock_provider_callback_secret is not None:
                raise ValueError(
                    "the mock depot-provider callback must not be enabled in production"
                )
        if self.ai_execution_mode is AIExecutionMode.LLM:
            if not self.enable_llm:
                raise ValueError("ai_execution_mode=llm requires enable_llm=true")
            if self.llm_provider is None:
                raise ValueError("ai_execution_mode=llm requires llm_provider")
        return self

    @property
    def dev_actor_auth_active(self) -> bool:
        """True only outside production, no matter how the flag is configured."""
        return self.dev_actor_auth_enabled and self.environment is not Environment.PRODUCTION

    @property
    def dev_customer_auth_active(self) -> bool:
        return self.dev_customer_auth_enabled and self.environment is not Environment.PRODUCTION

    @property
    def docs_enabled(self) -> bool:
        return self.environment is not Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings. Tests build `Settings(_env_file=None)` directly instead."""
    return Settings()
