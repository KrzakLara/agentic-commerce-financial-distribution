from pydantic import AwareDatetime

from app.domain.base import DomainModel, OpaqueId, Sha256Hex, ShortText


class ConsentReceipt(DomainModel):
    """Evidence that a specific customer approved a specific action via the trusted channel."""

    receipt_id: OpaqueId
    application_id: OpaqueId
    customer_id: OpaqueId
    handoff_id: OpaqueId
    action_digest: Sha256Hex
    confirmed_at: AwareDatetime


class DisclosureReceipt(DomainModel):
    """Acknowledgement of a mandatory disclosure on the trusted customer channel.

    Ties the acknowledgement to the exact document version and digest. Acknowledgement is
    not evidence of understanding (ADR section 09). Phase 04 also binds it to provider,
    product (version and terms) and the approved action digest. Receipts written before Phase
    04 have no such fields (None).
    """

    receipt_id: OpaqueId
    application_id: OpaqueId
    customer_id: OpaqueId
    document_id: OpaqueId
    document_version: ShortText
    document_digest: Sha256Hex
    acknowledged_at: AwareDatetime
    provider_id: OpaqueId | None = None
    product_id: OpaqueId | None = None
    product_version: ShortText | None = None
    terms_digest: Sha256Hex | None = None
    action_digest: Sha256Hex | None = None
