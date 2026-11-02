"""Verified provider discovery backed by a local, SYNTHETIC fixture (Phase 02).

Everything here is fictional and labelled MOCK. There is no BaFin lookup and no network:
trust comes from deterministic checks that run every time the fixture is loaded -

* the provider must be on a hard-coded allowlist,
* the manifest's SHA-256 digest must equal a digest *pinned in code*,
* every risk document's SHA-256 must equal both the pinned value and the value embedded in
  the manifest.

If anything differs (tampered, edited, unknown provider) loading raises
`CatalogIntegrityError` and the catalog is unavailable: fail closed. A register entry is not
proof of endpoint ownership (ADR section 09); real signed manifests are a production concern.
"""

import copy
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import CatalogIntegrityError, NotFoundError
from app.domain.catalog import Channel, Product, Provider
from app.domain.enums import ProviderRole

ALLOWED_PROVIDER_IDS = frozenset({"fixture-bank"})

_RISK_TEXT = (
    "MOCK RISK DISCLOSURE (synthetic text, not a real KID). A securities account holds "
    "securities in custody; the value of securities can fall as well as rise, and you may get "
    "back less than you invested. No real product, price or return is described here."
)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CatalogFixture:
    """Raw fixture content plus the digests pinned for it."""

    manifests: dict[str, dict[str, Any]]
    risk_documents: dict[str, str]  # "<document_id>@<version>" -> text
    pinned_manifest_digests: dict[str, str]
    pinned_risk_digests: dict[str, str]


# Pinned in code on purpose: editing the fixture without re-pinning makes loading fail.
_PINNED_MANIFEST_DIGEST = "5441d9a65ed6db77fdbb083ddce99029c3614d6bf546d4b65cbda39c6309c032"
_PINNED_RISK_DIGEST = "f67a5d990a3aa46af4c54e3dd7e319f81f63e77a6952cb559a42e935976c7ad4"


def _build_default() -> CatalogFixture:
    risk_digest = sha256_hex(_RISK_TEXT.encode("utf-8"))
    manifest = {
        "provider": {
            "provider_id": "fixture-bank",
            "legal_name": "Beispielbank Depot AG (MOCK)",
            "role": "DEPOT_PROVIDER",
            "manifest_version": "2026-11-01",
        },
        "channels": [
            {"channel_id": "direct", "label": "Direktvertrieb (MOCK)", "is_partner": False},
            {"channel_id": "partner-1", "label": "Hausbank Musterstadt (MOCK)", "is_partner": True},
        ],
        "products": [
            {
                "product_id": "depot-basic",
                "name": "Wertpapierdepot Basis (MOCK)",
                "catalog_version": "2026.11",
                "risk_document_id": "kid-depot-basic",
                "risk_document_version": "1.0",
                "risk_document_digest": risk_digest,
            }
        ],
    }
    return CatalogFixture(
        manifests={"fixture-bank": manifest},
        risk_documents={"kid-depot-basic@1.0": _RISK_TEXT},
        pinned_manifest_digests={"fixture-bank": _PINNED_MANIFEST_DIGEST},
        pinned_risk_digests={"kid-depot-basic@1.0": _PINNED_RISK_DIGEST},
    )


DEFAULT_FIXTURE = _build_default()


def tampered_copy(fixture: CatalogFixture = DEFAULT_FIXTURE) -> CatalogFixture:
    """A deep copy that tests can modify to prove tampering is detected."""
    return CatalogFixture(
        manifests=copy.deepcopy(fixture.manifests),
        risk_documents=dict(fixture.risk_documents),
        pinned_manifest_digests=dict(fixture.pinned_manifest_digests),
        pinned_risk_digests=dict(fixture.pinned_risk_digests),
    )


class FixtureProviderDiscovery:
    """`ProviderDiscoveryPort` over a verified fixture. Verification runs in the constructor."""

    def __init__(self, fixture: CatalogFixture = DEFAULT_FIXTURE) -> None:
        self._providers: dict[str, Provider] = {}
        self._channels: dict[str, tuple[Channel, ...]] = {}
        self._products: dict[str, tuple[Product, ...]] = {}
        self._documents = dict(fixture.risk_documents)
        self._verify_and_load(fixture)

    # --- verification ------------------------------------------------------------

    def _verify_and_load(self, fixture: CatalogFixture) -> None:
        for key, text in fixture.risk_documents.items():
            pinned = fixture.pinned_risk_digests.get(key)
            if pinned is None or sha256_hex(text.encode("utf-8")) != pinned:
                raise CatalogIntegrityError(f"risk document {key} failed its integrity check")

        for provider_id, doc in fixture.manifests.items():
            if provider_id not in ALLOWED_PROVIDER_IDS:
                raise CatalogIntegrityError(f"provider {provider_id} is not on the allowlist")
            pinned = fixture.pinned_manifest_digests.get(provider_id)
            digest = sha256_hex(canonical_json(doc))
            if pinned is None or digest != pinned:
                raise CatalogIntegrityError(f"manifest of {provider_id} failed its integrity check")
            if doc["provider"]["provider_id"] != provider_id:
                raise CatalogIntegrityError(f"manifest key mismatch for {provider_id}")

            self._providers[provider_id] = Provider(
                provider_id=provider_id,
                legal_name=doc["provider"]["legal_name"],
                role=ProviderRole(doc["provider"]["role"]),
                manifest_version=doc["provider"]["manifest_version"],
                manifest_digest=digest,
                is_mock=True,
            )
            self._channels[provider_id] = tuple(
                Channel(provider_id=provider_id, **channel) for channel in doc["channels"]
            )
            products: list[Product] = []
            for item in doc["products"]:
                doc_key = f"{item['risk_document_id']}@{item['risk_document_version']}"
                text = fixture.risk_documents.get(doc_key)
                if text is None or sha256_hex(text.encode("utf-8")) != item["risk_document_digest"]:
                    raise CatalogIntegrityError(f"product {item['product_id']} document mismatch")
                products.append(Product(provider_id=provider_id, **item))
            self._products[provider_id] = tuple(products)

    # --- ProviderDiscoveryPort ------------------------------------------------------------

    def get_provider(self, provider_id: str) -> Provider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise NotFoundError()
        return provider

    def list_products(self, provider_id: str) -> Sequence[Product]:
        self.get_provider(provider_id)
        return self._products[provider_id]

    def list_channels(self, provider_id: str) -> Sequence[Channel]:
        self.get_provider(provider_id)
        return self._channels[provider_id]

    def get_document_text(self, document_id: str, document_version: str) -> str:
        text = self._documents.get(f"{document_id}@{document_version}")
        if text is None:
            raise NotFoundError()
        return text

    def list_providers(self) -> Sequence[Provider]:
        return tuple(self._providers.values())
