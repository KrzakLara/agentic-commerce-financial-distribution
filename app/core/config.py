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
    # The bank's own question answering, in LLM mode: a question needs a verified credential, and
    # one process makes at most this many paid model runs. Scripted mode is free and public.
    llm_questions_require_credential: bool = True
    llm_max_questions_per_process: int = Field(default=200, ge=1, le=100_000)

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
    # How the MOCK depot provider's versioned API behaves (see `provider_contract.SCENARIOS`). An
    # unknown value stops the application from starting.
    mock_depot_scenario: str = "current_v2"

    # The customer must supply tax information and answer the knowledge-and-experience questions
    # on the bank's page before the bank accepts a confirmation. Off only for the three original
    # P0 scenarios and their tests, which predate this step and say so.
    customer_information_required: bool = True

    # Who signs the customer in. `oidc` is a boundary only: no identity provider is configured for
    # this project, so that mode switches the DEV sign-in off and every customer page fails closed
    # until a real provider and a token verifier exist (`app.integrations.customer_identity`).
    customer_identity_mode: Literal["dev_simulated", "oidc"] = "dev_simulated"
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_redirect_uri: str | None = None
    oidc_jwks_uri: str | None = None
    # A plain-http, loopback-only OIDC provider for the local integration environment. Refused in
    # production. Every other issuer must use HTTPS.
    oidc_allow_insecure_local: bool = False

    # The origin this deployment publishes in discovery metadata and checks DPoP proofs against.
    # Fixed by configuration, never taken from the request's Host header.
    public_base_url: str = "http://127.0.0.1:8000"
    # The provider's own signing key (a private JWK in JSON). Unset outside production: a fresh key
    # per process. It proves that metadata is unchanged, not who the provider is or what it may do.
    provider_signing_jwk: SecretStr | None = None
    # The federation superior named in `authority_hints` (a synthetic authority in this project).
    trust_superior_entity: str | None = None

    # The nine-area integrated journey: identity proofing, contract signing, attribution and
    # policy evidence become mandatory gates. Off by default, so the earlier scenarios are
    # unchanged. It needs `customer_identity_mode=oidc`: the DEV sign-in never runs next to it.
    full_trust_profile: bool = False
    delegation_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    delegation_request_ttl_seconds: int = Field(default=600, ge=30, le=3600)

    # Vendor-neutral boundaries. Each provider is named by configuration with its pinned public
    # keys; callbacks signed by anything else are refused.
    signature_provider_id: str | None = None
    signature_provider_url: str | None = None
    signature_provider_jwks: str | None = None
    signature_provider_api_key: SecretStr | None = None
    signature_level: Literal["TEST_NO_LEGAL_EFFECT", "QES"] = "TEST_NO_LEGAL_EFFECT"
    qtsp_id: str | None = None  # a real QTSP on an EU trusted list. None exists for this project.
    signature_ttl_seconds: int = Field(default=900, ge=60, le=86400)

    proofing_provider_id: str | None = None
    proofing_provider_url: str | None = None
    proofing_provider_jwks: str | None = None
    proofing_provider_api_key: SecretStr | None = None
    proofing_provider_regulated: bool = False  # True needs a contract no prototype can have
    proofing_ttl_seconds: int = Field(default=900, ge=60, le=86400)

    # {"partner_id": {"keys": [...]}}: the public keys of the partners' referral proofs (synthetic).
    partner_referral_jwks: str | None = None

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
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret",
        "oidc_redirect_uri",
        "oidc_jwks_uri",
        "provider_signing_jwk",
        "trust_superior_entity",
        "signature_provider_id",
        "signature_provider_url",
        "signature_provider_jwks",
        "signature_provider_api_key",
        "qtsp_id",
        "proofing_provider_id",
        "proofing_provider_url",
        "proofing_provider_jwks",
        "proofing_provider_api_key",
        "partner_referral_jwks",
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
            if self.oidc_allow_insecure_local:
                raise ValueError("oidc_allow_insecure_local must not be set in production")
            if self.provider_signing_jwk is None and self.full_trust_profile:
                raise ValueError("full_trust_profile in production needs provider_signing_jwk")
        if self.full_trust_profile and not self.customer_information_required:
            # The profile's gates read the customer's information, and the pages collect it only
            # with this on. Off, a customer could never get past the gates.
            raise ValueError("full_trust_profile requires customer_information_required")
        if self.full_trust_profile and self.customer_identity_mode != "oidc":
            # The profile's gates rest on a verified login. Next to the DEV sign-in, a typed
            # customer ID could approve an agent's delegation and supply the customer's information.
            raise ValueError("full_trust_profile requires customer_identity_mode=oidc")
        if self.signature_level == "QES" and self.qtsp_id is None:
            raise ValueError("a qualified signature needs a real, named QTSP")
        if self.proofing_provider_regulated and self.environment is not Environment.PRODUCTION:
            raise ValueError("regulated identity proofing cannot be claimed outside production")
        self.public_base_url = self.public_base_url.rstrip("/")
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
        return (
            self.dev_customer_auth_enabled
            and self.environment is not Environment.PRODUCTION
            and self.customer_identity_mode == "dev_simulated"
            # Also when a copied settings object skipped the validation above.
            and not self.full_trust_profile
        )

    @property
    def oidc_login_active(self) -> bool:
        """A real authorization-code login, only with a complete provider configuration."""
        return self.customer_identity_mode == "oidc" and all(
            value is not None
            for value in (
                self.oidc_issuer,
                self.oidc_client_id,
                self.oidc_client_secret,
                self.oidc_redirect_uri,
                self.oidc_jwks_uri,
            )
        )

    @property
    def customer_sign_in_active(self) -> bool:
        """Some sign-in exists. `oidc` without a provider has none: customer pages fail closed."""
        return self.dev_customer_auth_active or self.oidc_login_active

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
