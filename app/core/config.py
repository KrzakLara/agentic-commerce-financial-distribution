"""Environment-based application settings (Pydantic Settings).

All variables use the ``TRUSTEQ_`` prefix and may be provided through the process
environment or a local, git-ignored ``.env`` file. Defaults are safe and never
require an external API key: the AI execution mode defaults to ``scripted``.
The one exception to the prefix is the provider's usual ``ANTHROPIC_API_KEY``.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AIExecutionMode(StrEnum):
    """How the customer-side agent decides on tool calls (ADR-09)."""

    SCRIPTED = "scripted"  # deterministic, no API key (the default)
    LLM = "llm"  # a real model, only behind an explicit flag (Phase 05)


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRUSTEQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
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
    llm_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("TRUSTEQ_LLM_API_KEY", "ANTHROPIC_API_KEY")
    )
    # Bounds of one LLM agent run: thinking depth, tokens per model request, model requests per
    # run, seconds per request, and the SDK's retries of one request (no other retry loop exists).
    llm_effort: Literal["low", "medium", "high"] = "medium"
    llm_max_output_tokens: int = Field(default=4096, ge=256, le=16000)
    llm_max_tool_rounds: int = Field(default=8, ge=1, le=20)
    llm_timeout_seconds: float = Field(default=60.0, ge=5.0, le=300.0)
    llm_max_retries: int = Field(default=1, ge=0, le=3)

    # DEV/TEST ONLY: the trusted bootstrap (`python -m scripts.dev_auth`, the demo, tests) may
    # issue an agent's first, narrow grant. There is NO HTTP endpoint for it. Agents always
    # authenticate with `Authorization: Bearer <token>`; the Phase 02 X-Dev-Actor-Id header is
    # gone. Never active in production (see `dev_agent_bootstrap_active`): production has no
    # way to issue agent tokens, so every protected /v1 route answers 401 (fail closed).
    dev_agent_bootstrap_enabled: bool = True
    # DEV/TEST ONLY simulated *customer* sign-in (the dev sign-in page).
    # Separate from the agent credentials above. Not digital identity verification; never
    # active in production.
    dev_customer_auth_enabled: bool = True
    # DEV/TEST ONLY trusted operator CLI for human review (`python -m scripts.dev_operator`).
    # Synthetic operator identities, separate from agents, customers and providers; there is
    # NO operator HTTP endpoint. Never active in production.
    dev_operator_cli_enabled: bool = True

    # Lifetimes (seconds): customer handoff challenge, onboarding grant (+ its token), agent
    # bootstrap grant, read-only status grant, customer session on the bank-controlled pages.
    handoff_ttl_seconds: int = Field(default=600, ge=30, le=3600)
    mandate_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    agent_bootstrap_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    agent_status_ttl_seconds: int = Field(default=86400, ge=300, le=604800)
    customer_session_ttl_seconds: int = Field(default=900, ge=60, le=3600)

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
            for flag in (
                "dev_agent_bootstrap_enabled",
                "dev_customer_auth_enabled",
                "dev_operator_cli_enabled",
            ):
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
    def dev_agent_bootstrap_active(self) -> bool:
        """True only outside production, no matter how the flag is configured."""
        return self.dev_agent_bootstrap_enabled and self.environment is not Environment.PRODUCTION

    @property
    def dev_customer_auth_active(self) -> bool:
        return self.dev_customer_auth_enabled and self.environment is not Environment.PRODUCTION

    @property
    def dev_operator_cli_active(self) -> bool:
        return self.dev_operator_cli_enabled and self.environment is not Environment.PRODUCTION

    @property
    def docs_enabled(self) -> bool:
        return self.environment is not Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings. Tests build `Settings(_env_file=None)` directly instead."""
    return Settings()
