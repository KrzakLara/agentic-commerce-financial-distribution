from pydantic import AwareDatetime

from app.domain.base import DomainModel, OpaqueId, Sha256Hex, ShortText
from app.domain.enums import ProviderRole


class Provider(DomainModel):
    """A legal entity in the catalog. Roles are distinct (fund provider != depot provider)."""

    provider_id: OpaqueId
    legal_name: ShortText
    role: ProviderRole
    manifest_version: ShortText
    manifest_digest: Sha256Hex
    # True for every fixture in the MVP; real trust anchors are a production concern.
    is_mock: bool = True


class Product(DomainModel):
    """Approved, versioned product metadata (no free-form marketing claims)."""

    product_id: OpaqueId
    provider_id: OpaqueId
    name: ShortText
    catalog_version: ShortText
    risk_document_id: OpaqueId
    risk_document_version: ShortText
    risk_document_digest: Sha256Hex
    valid_until: AwareDatetime | None = None


class Channel(DomainModel):
    """A registered distribution channel. Only registered channels may enter the workflow."""

    channel_id: OpaqueId
    provider_id: OpaqueId
    label: ShortText
    is_partner: bool = False
