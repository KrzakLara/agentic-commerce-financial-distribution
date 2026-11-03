"""Scoped delegation (mandates) and the opaque bearer tokens that carry them (Phase 03).

A *mandate* is the authorization grant: which agent may do what, for which customer and which
application, until when. An *access token* is the credential an agent presents for one
mandate: a random opaque string whose SHA-256 hash - never the token - is stored server-side.

Claim names (issuer, audience, issued/expires, jti) follow OAuth 2.0 / RFC 7519 vocabulary,
but this is a CONTROLLED DEVELOPMENT authorization implementation: no OAuth server, no FAPI
profile, no sender-constrained tokens. See docs/trust-boundaries.md.
"""

from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, OpaqueId, Sha256Hex
from app.domain.enums import MandateKind, MandateScope

# The single issuer this deployment trusts, and the audience of the agent-facing API.
AUTHORIZATION_ISSUER = "trusteq-dev-authorization"
AGENT_API_AUDIENCE = "trusteq-onboarding-api"

# Least privilege by construction: each kind of grant carries exactly these scopes.
BOOTSTRAP_SCOPES: frozenset[MandateScope] = frozenset({MandateScope.CREATE_DRAFT})
ONBOARDING_SCOPES: frozenset[MandateScope] = frozenset(
    {MandateScope.REQUEST_HANDOFF, MandateScope.SUBMIT_AFTER_CONSENT}
)
STATUS_SCOPES: frozenset[MandateScope] = frozenset({MandateScope.READ_STATUS})
SCOPES_BY_KIND: dict[MandateKind, frozenset[MandateScope]] = {
    MandateKind.BOOTSTRAP: BOOTSTRAP_SCOPES,
    MandateKind.ONBOARDING: ONBOARDING_SCOPES,
    MandateKind.STATUS: STATUS_SCOPES,
}


class Mandate(DomainModel):
    """Short-lived, scoped delegation from a customer to an agent actor (MOCK claims).

    `kind` says what the grant is for (see `MandateKind`). Bootstrap grants start without an
    application and are bound to the one they create; onboarding and status grants are bound
    to their application from the start. Grants issued before Phase 03 were migrated as
    ONBOARDING grants and may still list their Phase 02 scope set.
    """

    mandate_id: OpaqueId
    jti: OpaqueId  # identifier of the grant itself
    kind: MandateKind = MandateKind.ONBOARDING
    application_id: OpaqueId | None = None
    agent_actor_id: OpaqueId
    customer_id: OpaqueId
    issuer: OpaqueId = AUTHORIZATION_ISSUER
    audience: OpaqueId
    scopes: frozenset[MandateScope] = Field(min_length=1)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if self.kind is not MandateKind.ONBOARDING and self.scopes != SCOPES_BY_KIND[self.kind]:
            raise ValueError(f"a {self.kind.value} mandate carries exactly its own scopes")
        if self.kind is MandateKind.STATUS and self.application_id is None:
            raise ValueError("a status mandate is bound to one application")
        return self

    def is_active(self, now: AwareDatetime) -> bool:
        return self.revoked_at is None and self.issued_at <= now < self.expires_at


class AgentAccessToken(DomainModel):
    """Server-side record of one opaque bearer token. The raw token is NEVER stored.

    `scopes` must be a subset of the mandate's scopes (checked at issue and on every use).
    """

    jti: OpaqueId  # unique token identifier: what audit events and revocations refer to
    token_hash: Sha256Hex
    mandate_id: OpaqueId
    issuer: OpaqueId
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
