from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, OpaqueId
from app.domain.enums import MandateScope


class Mandate(DomainModel):
    """Short-lived, scoped delegation from a customer to an agent actor (MOCK claims).

    Claim names (`audience`, `expires_at`, `jti`) follow the ADR's mandate model; this is
    not a FAPI-certified token. Phase 02 issues one per application, checks it before agent
    operations and revokes it on any terminal state. Token-level verification (audience, jti
    replay, per-request ownership) is Phase 03.
    """

    mandate_id: OpaqueId
    jti: OpaqueId
    application_id: OpaqueId | None = None  # onboarding mandates are bound to one application
    agent_actor_id: OpaqueId
    customer_id: OpaqueId
    audience: OpaqueId
    scopes: frozenset[MandateScope] = Field(min_length=1)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _expiry_after_issue(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        return self
