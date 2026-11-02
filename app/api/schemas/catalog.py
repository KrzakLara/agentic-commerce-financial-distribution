"""Public schemas for provider discovery (verified fixture metadata)."""

from pydantic import BaseModel, ConfigDict

from app.domain.catalog import Channel, Product, Provider


class ChannelView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str
    label: str
    is_partner: bool

    @classmethod
    def from_domain(cls, channel: Channel) -> "ChannelView":
        return cls(
            channel_id=channel.channel_id, label=channel.label, is_partner=channel.is_partner
        )


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
            channel_ids=[c.channel_id for c in channels],
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
            channels=[ChannelView.from_domain(c) for c in channels],
            product_ids=[p.product_id for p in products],
        )


class ProductListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: list[ProductView]
