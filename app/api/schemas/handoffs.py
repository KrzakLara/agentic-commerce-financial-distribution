"""Schemas for the customer handoff and the provider callback."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.domain.base import Sha256Hex
from app.domain.handoff import ApprovedAction, Handoff
from app.domain.visibility import AgentVisibleState

Nonce = Annotated[
    str, StringConstraints(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_\-]+$")
]


class ApprovedActionView(BaseModel):
    """What the customer will be asked to approve. Contains no customer data."""

    model_config = ConfigDict(extra="forbid")

    action_type: str
    application_id: str
    provider_id: str
    product_id: str
    channel_id: str
    catalog_version: str
    document_id: str
    document_version: str
    document_digest: str
    # Phase 04: digest of the approved product terms, and the registered partner (if any) the
    # onboarding is attributed to - both from the bank's registry, never from the agent.
    terms_digest: str | None = None
    partner_id: str | None = None

    @classmethod
    def from_domain(cls, action: ApprovedAction) -> "ApprovedActionView":
        data = action.model_dump()
        data.pop("customer_ref")
        return cls(**data)


class HandoffResponse(BaseModel):
    """Returned to the AGENT. The `confirmation_url` is a pointer, not an authorization:
    opening it still requires the customer's own (simulated) sign-in."""

    model_config = ConfigDict(extra="forbid")

    handoff_id: str
    application_id: str
    state: AgentVisibleState
    expires_at: datetime
    action_digest: str
    action: ApprovedActionView
    confirmation_url: str

    @classmethod
    def build(cls, handoff: Handoff, url: str) -> "HandoffResponse":
        return cls(
            handoff_id=handoff.handoff_id,
            application_id=handoff.application_id,
            state=AgentVisibleState.AWAITING_CUSTOMER,
            expires_at=handoff.expires_at,
            action_digest=handoff.action_digest,
            action=ApprovedActionView.from_domain(handoff.action),
            confirmation_url=url,
        )


class HandoffConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: Nonce
    action_digest: Sha256Hex


class HandoffConfirmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handoff_id: str
    application_id: str
    state: AgentVisibleState
