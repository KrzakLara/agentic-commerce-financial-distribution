import hashlib
import json
from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from app.domain.base import DomainModel, OpaqueId, Sha256Hex, ShortText


class ApprovedAction(DomainModel):
    """The exact action the customer is asked to approve; its digest is what gets bound.

    Everything a decision depends on is inside this object: who offers what, through which
    channel, for which customer reference, and which disclosure document (id, version, digest)
    applies. Changing any field changes the digest, so a confirmation for one action can never
    be replayed for another.
    """

    action_type: Literal["OPEN_DEPOT_ACCOUNT"] = "OPEN_DEPOT_ACCOUNT"
    application_id: OpaqueId
    provider_id: OpaqueId
    product_id: OpaqueId
    channel_id: OpaqueId
    customer_ref: OpaqueId
    catalog_version: ShortText
    document_id: OpaqueId
    document_version: ShortText
    document_digest: Sha256Hex

    def digest(self) -> str:
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Handoff(DomainModel):
    """Request-bound, one-time customer handoff (ADR section 03, session substitution).

    Only a hash of the nonce is stored. `action_digest` fixes the immutable proposed
    action; `customer_id` is bound only when the authenticated customer confirms.
    A handoff is usable while it is neither consumed, revoked (superseded) nor expired.
    """

    handoff_id: OpaqueId
    application_id: OpaqueId
    nonce_hash: Sha256Hex
    action: ApprovedAction
    action_digest: Sha256Hex
    expires_at: AwareDatetime
    created_at: AwareDatetime
    customer_id: OpaqueId | None = None
    consumed_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.action.application_id != self.application_id:
            raise ValueError("action belongs to a different application")
        if self.action.digest() != self.action_digest:
            raise ValueError("action_digest does not match the action")
        return self
