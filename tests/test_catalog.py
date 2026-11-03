"""Verified provider discovery (fixture) and the "only verified references" rule."""

import copy
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.core.exceptions import CatalogIntegrityError, NotFoundError
from app.integrations.catalog_fixture import (
    ALLOWED_PROVIDER_IDS,
    DEFAULT_FIXTURE,
    FixtureProviderDiscovery,
    canonical_json,
    sha256_hex,
    tampered_copy,
)
from app.main import create_app
from tests.conftest import bootstrap_headers

BODY = {
    "provider_id": "fixture-bank",
    "product_id": "depot-basic",
    "channel_id": "direct",
    "customer_ref": "opaque-id",
}


def post(client: TestClient, body: dict[str, str]):  # noqa: ANN201
    return client.post(
        "/v1/applications",
        json=body,
        headers={**bootstrap_headers(client), "Idempotency-Key": str(uuid.uuid4())},
    )


# --- discovery endpoints ----------------------------------------------------------------------


def test_manifest_endpoint_returns_verified_fixture_metadata(client: TestClient) -> None:
    response = client.get("/v1/providers/fixture-bank/manifest")
    assert response.status_code == 200
    body = response.json()
    assert body["provider_id"] == "fixture-bank"
    assert body["legal_name"] == "Beispielbank Depot AG (MOCK)"
    assert body["role"] == "DEPOT_PROVIDER" and body["is_mock"] is True
    assert body["manifest_digest"] == sha256_hex(
        canonical_json(DEFAULT_FIXTURE.manifests["fixture-bank"])
    )
    assert {c["channel_id"] for c in body["channels"]} == {"direct", "partner-1"}
    assert body["product_ids"] == ["depot-basic"]
    # Honest labelling: this is a pinned local fixture, not a registry or BaFin verification.
    assert body["verification"] == "LOCAL_FIXTURE_DIGEST_PINNED"


def test_products_endpoint_returns_document_version_and_digest(client: TestClient) -> None:
    response = client.get("/v1/products", params={"provider_id": "fixture-bank"})
    assert response.status_code == 200
    (product,) = response.json()["products"]
    assert product["product_id"] == "depot-basic" and product["provider_id"] == "fixture-bank"
    assert product["document_id"] == "kid-depot-basic" and product["document_version"] == "1.0"
    assert len(product["document_digest"]) == 64
    assert set(product["channel_ids"]) == {"direct", "partner-1"}


def test_products_without_filter_lists_every_verified_provider(client: TestClient) -> None:
    ids = {p["provider_id"] for p in client.get("/v1/products").json()["products"]}
    assert ids == set(ALLOWED_PROVIDER_IDS)


def test_discovery_is_public_but_exposes_nothing_internal(client: TestClient) -> None:
    text = (
        client.get("/v1/providers/fixture-bank/manifest").text + client.get("/v1/products").text
    ).lower()
    for internal in ("customer", "application", "mandate", "kyc", "compliance"):
        assert internal not in text


def test_unknown_provider_is_not_found(client: TestClient) -> None:
    assert client.get("/v1/providers/rogue-bank/manifest").status_code == 404
    assert client.get("/v1/products", params={"provider_id": "rogue-bank"}).status_code == 404
    assert client.get("/v1/providers/bad id!/manifest").status_code in {404, 422}


# --- fixture integrity (deterministic validation) ----------------------------------------------


def test_default_fixture_loads_and_matches_its_pins() -> None:
    catalog = FixtureProviderDiscovery()
    assert [p.provider_id for p in catalog.list_providers()] == ["fixture-bank"]
    assert (
        DEFAULT_FIXTURE.pinned_manifest_digests["fixture-bank"]
        == catalog.get_provider("fixture-bank").manifest_digest
    )


def test_tampered_manifest_is_rejected() -> None:
    fixture = tampered_copy()
    fixture.manifests["fixture-bank"]["channels"][0]["label"] = "Evil Channel"
    with pytest.raises(CatalogIntegrityError, match="manifest"):
        FixtureProviderDiscovery(fixture)


def test_tampered_risk_text_is_rejected() -> None:
    fixture = tampered_copy()
    key = "kid-depot-basic@1.0"
    fixture.risk_documents[key] = fixture.risk_documents[key] + " Guaranteed returns!"
    with pytest.raises(CatalogIntegrityError, match="risk document"):
        FixtureProviderDiscovery(fixture)


def test_a_manifest_edited_and_repinned_still_needs_a_matching_document() -> None:
    fixture = tampered_copy()
    manifest = fixture.manifests["fixture-bank"]
    manifest["products"][0]["risk_document_digest"] = "0" * 64  # points at a different document
    object.__setattr__(
        fixture,
        "pinned_manifest_digests",
        {"fixture-bank": sha256_hex(canonical_json(manifest))},  # attacker re-pins the manifest
    )
    with pytest.raises(CatalogIntegrityError, match="document mismatch"):
        FixtureProviderDiscovery(fixture)


def test_a_provider_outside_the_allowlist_is_rejected_even_with_a_valid_digest() -> None:
    fixture = tampered_copy()
    manifest = copy.deepcopy(fixture.manifests["fixture-bank"])
    manifest["provider"]["provider_id"] = "rogue-bank"
    fixture.manifests["rogue-bank"] = manifest
    fixture.pinned_manifest_digests["rogue-bank"] = sha256_hex(canonical_json(manifest))
    with pytest.raises(CatalogIntegrityError, match="allowlist"):
        FixtureProviderDiscovery(fixture)


def test_a_manifest_without_a_pinned_digest_is_rejected() -> None:
    fixture = tampered_copy()
    fixture.pinned_manifest_digests.clear()
    with pytest.raises(CatalogIntegrityError):
        FixtureProviderDiscovery(fixture)


def test_unknown_lookups_raise_not_found() -> None:
    catalog = FixtureProviderDiscovery()
    with pytest.raises(NotFoundError):
        catalog.get_provider("rogue-bank")
    with pytest.raises(NotFoundError):
        catalog.get_document_text("kid-depot-basic", "9.9")


def test_a_tampered_catalog_means_the_application_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed at startup: a deployment with an edited catalog serves nothing at all."""
    import app.container as container_module

    fixture = tampered_copy()
    fixture.manifests["fixture-bank"]["provider"]["legal_name"] = "Fake Bank"
    monkeypatch.setattr(
        container_module, "FixtureProviderDiscovery", lambda: FixtureProviderDiscovery(fixture)
    )
    with pytest.raises(CatalogIntegrityError):
        create_app(Settings(_env_file=None, environment=Environment.TEST))


# --- only verified references may enter the workflow ---------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"provider_id": "rogue-bank"},
        {"product_id": "depot-premium"},
        {"channel_id": "partner-evil"},
        {"channel_id": "partner-2"},
    ],
)
def test_unverified_provider_product_or_channel_cannot_create_an_application(
    client: TestClient, bad: dict[str, str]
) -> None:
    response = post(client, {**BODY, **bad})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unverified_reference"
    with client.app.state.engine.connect() as connection:  # type: ignore[attr-defined]
        from sqlalchemy import text

        assert connection.execute(text("SELECT COUNT(*) FROM applications")).scalar_one() == 0


def test_a_registered_partner_channel_is_accepted(client: TestClient) -> None:
    assert post(client, {**BODY, "channel_id": "partner-1"}).status_code == 201
