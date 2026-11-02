"""External (public) schemas for the application endpoints.

These are NOT the domain models. They expose only what an agent may see: its own opaque
application id, the values it proposed, a coarse state and timestamps. Internal fields
(customer_ref, owner id, fine-grained workflow state, resume state, version, audit data,
compliance detail) are never part of the response.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.application import DepotApplication
from app.domain.base import OpaqueId
from app.domain.visibility import AgentVisibleState, to_agent_visible_state
from app.services.application_service import CreateApplicationCommand


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
