from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, OpaqueId
from app.domain.enums import ApplicationStatus
from app.domain.workflow import RESUMABLE_STATES


class DepotApplication(DomainModel):
    """A securities-account (Wertpapierdepot) onboarding application.

    Agent-provided fields are untrusted proposals. `status` is changed only through the
    state machine (`app.domain.workflow` + `ApplicationService.transition`); there is
    intentionally no `set_status` helper.
    """

    application_id: OpaqueId
    provider_id: OpaqueId
    product_id: OpaqueId
    channel_id: OpaqueId
    customer_ref: OpaqueId
    agent_actor_id: OpaqueId
    status: ApplicationStatus = ApplicationStatus.DRAFT
    # The step interrupted by ACTION_REQUIRED / MANUAL_REVIEW; set only in those states.
    resume_status: ApplicationStatus | None = None
    # Optimistic-lock counter, incremented by every status change.
    version: int = Field(default=1, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _resume_status_matches_state(self) -> Self:
        if (self.status in RESUMABLE_STATES) != (self.resume_status is not None):
            raise ValueError("resume_status must be set exactly when status is a parked state")
        return self
