from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, model_validator

from app.domain.base import DomainModel, OpaqueId, Sha256Hex, ShortText
from app.domain.enums import ProviderRole


class ChannelKind(StrEnum):
    DIRECT = "DIRECT"  # the provider's own channel
    PARTNER_BANK = "PARTNER_BANK"  # a registered partner bank (Hausbank) distributes


class Provider(DomainModel):
    """A legal entity in the catalog. Roles are distinct (fund provider != depot provider).

    Phase 04: the provider-to-domain-to-endpoint binding comes from the bank's TRUSTED catalog
    and a signed, expiring manifest; a registry reference (LEI) is informational only.
    """

    provider_id: OpaqueId
    legal_name: ShortText
    role: ProviderRole
    manifest_version: ShortText
    manifest_digest: Sha256Hex
    # True for every fixture in the MVP; real trust anchors are a production concern.
    is_mock: bool = True
    registry_reference: ShortText | None = None  # fictional LEI - NOT proof of anything
    verified_domain: ShortText | None = None
    api_endpoint: ShortText | None = None
    capabilities: frozenset[str] = frozenset()
    trust_anchor: OpaqueId | None = None  # key id the manifest signature verified against
    issued_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None


class Fee(DomainModel):
    code: OpaqueId
    label: ShortText
    amount: ShortText  # display text from the approved price list (synthetic)


class SourceReference(DomainModel):
    """An official document of the (synthetic) catalog that a product statement relies on."""

    source_id: OpaqueId
    title: ShortText
    document_id: OpaqueId
    document_version: ShortText
    document_digest: Sha256Hex


class Product(DomainModel):
    """Approved, versioned product metadata (no free-form marketing claims).

    `catalog_version` is the product version. `terms_digest` is the SHA-256 of the canonical
    terms (fees, conditions, risk information, sources, dates); it is part of the action a
    customer approves, so changed terms invalidate an earlier approval.
    """

    product_id: OpaqueId
    provider_id: OpaqueId
    name: ShortText
    catalog_version: ShortText
    risk_document_id: OpaqueId
    risk_document_version: ShortText
    risk_document_digest: Sha256Hex
    valid_until: AwareDatetime | None = None
    fees: tuple[Fee, ...] = ()
    conditions: ShortText | None = None
    risk_class: ShortText | None = None
    risk_summary: ShortText | None = None
    sources: tuple[SourceReference, ...] = ()
    effective_from: date | None = None
    published_at: date | None = None
    terms_digest: Sha256Hex | None = None


class Channel(DomainModel):
    """A registered distribution channel. Only registered, ACTIVE channels of the provider
    for the product may enter the workflow; the partner (commission recipient) comes from
    this registry, never from the agent."""

    channel_id: OpaqueId
    provider_id: OpaqueId
    label: ShortText
    is_partner: bool = False
    kind: ChannelKind = ChannelKind.DIRECT
    partner_id: OpaqueId | None = None
    partner_name: ShortText | None = None
    active: bool = True
    product_ids: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.is_partner != (self.kind is ChannelKind.PARTNER_BANK):
            raise ValueError("is_partner must match the channel kind")
        if (self.kind is ChannelKind.PARTNER_BANK) != (self.partner_id is not None):
            raise ValueError("exactly the partner channels name a partner")
        return self

    def serves(self, product_id: str) -> bool:
        return self.active and (not self.product_ids or product_id in self.product_ids)
