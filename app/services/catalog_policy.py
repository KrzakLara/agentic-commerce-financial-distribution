"""The one "only verified references" rule (Phase 04), used at create, handoff, confirm and submit.

A provider, product and channel may only be used if all of them come from the VERIFIED catalog
and are usable *now*:

* the provider's manifest is inside its validity window (re-checked on every use - a manifest
  that was valid at startup can expire while the server runs);
* the product is effective and not withdrawn;
* the channel is registered for this provider, ACTIVE (including the bank's partner register)
  and serves this product.

The partner a channel attributes the onboarding to comes from the registry here - an agent can
only name a channel id, never a partner or commission recipient.
"""

from dataclasses import dataclass
from datetime import datetime

from app.core.exceptions import CatalogIntegrityError, NotFoundError, UnverifiedReferenceError
from app.domain.catalog import Channel, Product, Provider
from app.integrations.ports import ProviderDiscoveryPort


@dataclass(frozen=True)
class VerifiedOffer:
    provider: Provider
    product: Product
    channel: Channel


def provider_is_current(provider: Provider, now: datetime) -> bool:
    if provider.issued_at is not None and now < provider.issued_at:
        return False
    return provider.expires_at is None or now < provider.expires_at


def product_is_current(product: Product, now: datetime) -> bool:
    if product.effective_from is not None and now.date() < product.effective_from:
        return False
    return product.valid_until is None or now < product.valid_until


def resolve_offer(
    catalog: ProviderDiscoveryPort,
    *,
    provider_id: str,
    product_id: str,
    channel_id: str,
    now: datetime,
) -> VerifiedOffer:
    """The verified (provider, product, channel), or `UnverifiedReferenceError` (422)."""
    try:
        provider = catalog.get_provider(provider_id)
        product = next(
            (p for p in catalog.list_products(provider_id) if p.product_id == product_id), None
        )
        channel = next(
            (c for c in catalog.list_channels(provider_id) if c.channel_id == channel_id), None
        )
    except (NotFoundError, CatalogIntegrityError):
        raise UnverifiedReferenceError() from None
    if (
        product is None
        or channel is None
        or not provider_is_current(provider, now)
        or not product_is_current(product, now)
        or not channel.serves(product_id)
    ):
        raise UnverifiedReferenceError()
    return VerifiedOffer(provider, product, channel)
