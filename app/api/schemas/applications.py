"""External (public) schemas for the application endpoints.

These are NOT the domain models. They expose only what an agent may see: its own opaque
application id, the values it proposed, a coarse state and timestamps. Internal fields
(customer_ref, owner id, fine-grained workflow state, resume state, version, audit data,
compliance detail) are never part of the response.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.application import DepotApplication
from app.domain.base import OpaqueId
from app.domain.visibility import AgentVisibleState, to_agent_visible_state
from app.services.application_service import CreateApplicationCommand
from app.services.authorization_service import ApplicationCredentials, IssuedToken


class CreateApplicationRequest(BaseModel):
    """Agent-provided fields are untrusted proposals; unknown fields (`status`...) are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_id: OpaqueId = Field(examples=["fixture-bank"])
    product_id: OpaqueId = Field(examples=["depot-basic"])
    channel_id: OpaqueId = Field(examples=["direct"])
    customer_ref: OpaqueId = Field(
        description="Opaque customer reference; no personal data.", examples=["opaque-id"]
    )

    def to_command(self) -> CreateApplicationCommand:
        return CreateApplicationCommand(**self.model_dump())


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    provider_id: str
    product_id: str
    channel_id: str
    state: AgentVisibleState
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, application: DepotApplication) -> "ApplicationResponse":
        return cls(
            application_id=application.application_id,
            provider_id=application.provider_id,
            product_id=application.product_id,
            channel_id=application.channel_id,
            state=to_agent_visible_state(application.status),
            created_at=application.created_at,
            updated_at=application.updated_at,
        )


class AgentTokenView(BaseModel):
    """One application-bound bearer token, shown ONCE (the bank keeps only its hash)."""

    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - OAuth token type name, not a secret
    scope: str = Field(description="Space-delimited scopes (RFC 6749 section 3.3).")
    expires_at: datetime

    @classmethod
    def from_issued(cls, issued: IssuedToken) -> "AgentTokenView":
        return cls(
            access_token=issued.token,
            scope=" ".join(sorted(scope.value for scope in issued.scopes)),
            expires_at=issued.expires_at,
        )


class ApplicationAuthorizationView(BaseModel):
    """The credentials the bank grants for exactly this application (Phase 03)."""

    model_config = ConfigDict(extra="forbid")

    onboarding: AgentTokenView = Field(
        description="`request_handoff submit_after_consent`; revoked at OPENED / terminal end."
    )
    status: AgentTokenView = Field(
        description="`read_status` only; outlives the onboarding until it expires."
    )

    @classmethod
    def from_credentials(
        cls, credentials: ApplicationCredentials
    ) -> "ApplicationAuthorizationView":
        return cls(
            onboarding=AgentTokenView.from_issued(credentials.onboarding),
            status=AgentTokenView.from_issued(credentials.status),
        )


class ApplicationCreatedResponse(ApplicationResponse):
    """Create response: the application plus - on first creation only - its tokens.

    An idempotent replay returns `authorization: null`: tokens are never minted twice.
    """

    authorization: ApplicationAuthorizationView | None
