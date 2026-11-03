"""Public schemas for provider discovery (verified fixture metadata).

Product information is the provider's approved, versioned, NON-PERSONALIZED information: no
suitability assessment, no recommendation, no claim of MiFID II / WpHG compliance.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

from app.domain.catalog import Channel, Fee, Product, Provider, SourceReference
from app.integrations.catalog_fixture import SIGNATURE_ALGORITHM, VERIFICATION_CHECKS

PRODUCT_NOTICE = (
    "Official, non-personalized product information from the provider's verified catalog "
    "(MOCK). It is not investment advice and not a suitability assessment."
)


class ChannelView(BaseModel):
    """A registered, ACTIVE distribution channel. The partner comes from the bank's registry."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    label: str
    is_partner: bool
    kind: str
    partner_id: str | None
    partner_name: str | None

    @classmethod
    def from_domain(cls, channel: Channel) -> "ChannelView":
        return cls(
            channel_id=channel.channel_id,
            label=channel.label,
            is_partner=channel.is_partner,
            kind=channel.kind.value,
            partner_id=channel.partner_id,
            partner_name=channel.partner_name,
        )


class FeeView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    label: str
    amount: str

    @classmethod
    def from_domain(cls, fee: Fee) -> "FeeView":
        return cls(code=fee.code, label=fee.label, amount=fee.amount)


class SourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    title: str
    document_id: str
    document_version: str
    document_digest: str

    @classmethod
    def from_domain(cls, source: SourceReference) -> "SourceView":
        return cls(**source.model_dump())


class ProductView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    provider_id: str
    name: str
    catalog_version: str
    document_id: str
    document_version: str
    document_digest: str
    channel_ids: list[str]
    # Phase 04: approved terms, risk information, sources and their integrity.
    fees: list[FeeView] = []
    conditions: str | None = None
    risk_class: str | None = None
    risk_summary: str | None = None
    sources: list[SourceView] = []
    effective_from: date | None = None
    published_at: date | None = None
    terms_digest: str | None = None
    notice: str = PRODUCT_NOTICE

    @classmethod
    def from_domain(cls, product: Product, channels: list[Channel]) -> "ProductView":
        return cls(
            product_id=product.product_id,
            provider_id=product.provider_id,
            name=product.name,
            catalog_version=product.catalog_version,
            document_id=product.risk_document_id,
            document_version=product.risk_document_version,
            document_digest=product.risk_document_digest,
            channel_ids=[c.channel_id for c in channels if c.serves(product.product_id)],
            fees=[FeeView.from_domain(f) for f in product.fees],
            conditions=product.conditions,
            risk_class=product.risk_class,
            risk_summary=product.risk_summary,
            sources=[SourceView.from_domain(s) for s in product.sources],
            effective_from=product.effective_from,
            published_at=product.published_at,
            terms_digest=product.terms_digest,
        )


class ProviderManifestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str
    legal_name: str
    role: str
    manifest_version: str
    manifest_digest: str
    is_mock: bool
    verification: str
    channels: list[ChannelView]
    product_ids: list[str]
    # Phase 04: what the bank verified (all against its own register - see the docs).
    registry_reference: str | None = None
    verified_domain: str | None = None
    api_endpoint: str | None = None
    capabilities: list[str] = []
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    signature_algorithm: str = SIGNATURE_ALGORITHM
    trust_anchor: str | None = None
    verification_checks: list[str] = list(VERIFICATION_CHECKS)

    @classmethod
    def build(
        cls, provider: Provider, channels: list[Channel], products: list[Product]
    ) -> "ProviderManifestResponse":
        return cls(
            provider_id=provider.provider_id,
            legal_name=provider.legal_name,
            role=provider.role.value,
            manifest_version=provider.manifest_version,
            manifest_digest=provider.manifest_digest,
            is_mock=provider.is_mock,
            verification="LOCAL_FIXTURE_DIGEST_PINNED",  # not a BaFin or registry check
            channels=[ChannelView.from_domain(c) for c in channels if c.active],
            product_ids=[p.product_id for p in products],
            registry_reference=provider.registry_reference,
            verified_domain=provider.verified_domain,
            api_endpoint=provider.api_endpoint,
            capabilities=sorted(provider.capabilities),
            issued_at=provider.issued_at,
            expires_at=provider.expires_at,
            trust_anchor=provider.trust_anchor,
        )


class ProductListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: list[ProductView]
