"""Phase 04: trusted product information, disclosure evidence bound to the exact version and
action, invalidation of stale approvals - and verified partner-channel attribution."""

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from app.domain.handoff import ApprovedAction
from app.integrations.catalog_fixture import (
    CatalogFixture,
    FixtureProviderDiscovery,
    republish,
    tampered_copy,
)
from tests.conftest import bootstrap_headers
from tests.flow import INTENT, Flow, Started

Manifest = dict[str, Any]
PARTNER_INTENT = INTENT.model_copy(update={"channel_id": "partner-1"})


def catalog_with(edit: Callable[[Manifest], None]) -> FixtureProviderDiscovery:
    """A LEGITIMATELY re-published catalog (signed by the provider, pinned by the bank)."""
    fixture: CatalogFixture = tampered_copy()
    edit(fixture.manifests["fixture-bank"])
    republish(fixture, "fixture-bank")
    return FixtureProviderDiscovery(fixture)


def raise_fee(m: Manifest) -> None:
    m["products"][0]["fees"][1]["amount"] = "9.90 EUR"


def end_partnership(m: Manifest) -> None:
    next(c for c in m["channels"] if c["channel_id"] == "partner-1")["active"] = False


def handoff_action(flow: Flow, application_id: str) -> dict[str, Any]:
    (row,) = flow.sql(
        "SELECT action FROM handoffs WHERE application_id = :a AND revoked_at IS NULL",
        a=application_id,
    )
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def submit(flow: Flow, application_id: str):  # noqa: ANN201
    return flow.agent_http.post(
        f"/v1/applications/{application_id}/submit",
        headers=flow.onboarding_headers(application_id),
    )


def last_submit_denial(flow: Flow, application_id: str) -> dict[str, Any]:
    return [
        e.details for e in flow.audit(application_id) if e.event_type == "application.submit_denied"
    ][-1]


def confirmed(flow: Flow, intent=INTENT) -> Started:  # noqa: ANN001
    started = flow.start(intent=intent)
    assert flow.customer_confirm(started).status_code == 200
    return started


# --- trusted product information ----------------------------------------------------------------


def test_product_information_is_complete_versioned_and_non_personalized(flow: Flow) -> None:
    response = flow.agent_http.get("/v1/products", params={"provider_id": "fixture-bank"})
    (product,) = response.json()["products"]
    assert product["product_id"] == "depot-basic" and product["provider_id"] == "fixture-bank"
    assert product["catalog_version"] == "2026.11"
    assert [f["code"] for f in product["fees"]] == ["custody", "order"]
    assert product["conditions"] and product["risk_class"] and product["risk_summary"]
    (source,) = product["sources"]
    price_list = flow.container.catalog.get_document_text(
        source["document_id"], source["document_version"]
    )
    assert hashlib.sha256(price_list.encode()).hexdigest() == source["document_digest"]
    assert product["effective_from"] == "2026-11-01" and product["published_at"] == "2026-10-15"
    assert len(product["terms_digest"]) == 64
    assert "not investment advice" in product["notice"]
    lowered = response.text.lower()
    for claim in ("mifid", "wphg", "bafin", "recommended for you", "suitable for you"):
        assert claim not in lowered


def test_the_terms_digest_covers_every_term() -> None:
    base = FixtureProviderDiscovery().list_products("fixture-bank")[0].terms_digest
    edits: list[Callable[[Manifest], None]] = [
        raise_fee,
        lambda m: m["products"][0].update(conditions="Changed conditions (MOCK)."),
        lambda m: m["products"][0].update(risk_summary="Changed risk text."),
        lambda m: m["products"][0].update(risk_class="5 of 7 (synthetic scale)"),
        lambda m: m["products"][0].update(effective_from="2026-11-02"),
        lambda m: m["products"][0]["sources"][0].update(title="Other title (MOCK)"),
    ]
    digests = {catalog_with(e).list_products("fixture-bank")[0].terms_digest for e in edits}
    assert base not in digests and len(digests) == len(edits)


# --- the approval binds the exact terms, disclosure and attribution -----------------------------


def test_the_approved_action_binds_the_terms_and_the_registered_partner(flow: Flow) -> None:
    terms = flow.container.catalog.list_products("fixture-bank")[0].terms_digest
    direct = flow.start()
    partner = flow.start(actor_id="agent-b", intent=PARTNER_INTENT.model_copy(
        update={"customer_ref": "cust-2"}))  # fmt: skip
    direct_action = handoff_action(flow, direct.application_id)
    partner_action = handoff_action(flow, partner.application_id)
    assert direct_action["terms_digest"] == partner_action["terms_digest"] == terms
    assert direct_action["partner_id"] is None
    assert partner_action["partner_id"] == "sampletown-coop-bank"  # from the registry


def test_the_disclosure_receipt_is_bound_to_the_exact_version_and_action(flow: Flow) -> None:
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    (receipt,) = flow.sql(
        "SELECT application_id, customer_id, provider_id, product_id, product_version, "
        "document_id, document_version, document_digest, terms_digest, action_digest, "
        "acknowledged_at FROM disclosure_receipts"
    )
    (consent,) = flow.sql("SELECT action_digest, confirmed_at FROM consent_receipts")
    product = flow.container.catalog.list_products("fixture-bank")[0]
    assert receipt[:8] == (
        started.application_id, "cust-1", "fixture-bank", "depot-basic", "2026.11",
        "kid-depot-basic", "1.0", product.risk_document_digest,
    )  # fmt: skip
    assert receipt[8] == product.terms_digest
    assert receipt[9] == consent[0] == started.action_digest
    assert receipt[10] == consent[1]  # the confirmation time


def test_changed_terms_after_the_handoff_invalidate_the_old_approval(flow: Flow) -> None:
    started = flow.start()
    flow.container.catalog = catalog_with(raise_fee)  # the provider re-publishes its prices
    response = flow.customer_confirm(started)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "action_digest_mismatch"
    assert flow.sql("SELECT COUNT(*) FROM disclosure_receipts") == [(0,)]
    reasons = [e.details.get("reason") for e in flow.audit(started.application_id)]
    assert "action_no_longer_current" in reasons
    # A fresh handoff carries the new terms - and then the customer can approve them.
    fresh = flow.agent_http.post(
        f"/v1/applications/{started.application_id}/handoff",
        headers=flow.onboarding_headers(started.application_id),
    ).json()
    new_terms = flow.container.catalog.list_products("fixture-bank")[0].terms_digest
    assert fresh["action"]["terms_digest"] == new_terms


@pytest.mark.parametrize(
    "edit",
    [
        raise_fee,
        lambda m: m["products"][0].update(conditions="New conditions (MOCK)."),
        end_partnership,
    ],
    ids=["fee-changed", "conditions-changed", "partnership-ended"],
)
def test_changes_after_consent_block_the_submission(
    flow: Flow, edit: Callable[[Manifest], None]
) -> None:
    started = confirmed(flow, PARTNER_INTENT)
    app_id = started.application_id
    assert flow.status_of(app_id) == "CONTRACT_PENDING"
    flow.container.catalog = catalog_with(edit)
    response = submit(flow, app_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "prerequisites_not_met"
    assert last_submit_denial(flow, app_id) == {"missing": ["approved_action_current"]}
    assert flow.status_of(app_id) == "CONTRACT_PENDING"
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]


def test_an_unchanged_catalog_lets_the_consented_application_submit(flow: Flow) -> None:
    started = confirmed(flow, PARTNER_INTENT)
    flow.container.catalog = catalog_with(lambda m: None)  # re-published, same content
    assert submit(flow, started.application_id).status_code == 202


def test_approvals_from_before_phase_04_keep_their_digest() -> None:
    """Unset terms/partner are left out of the digest, so a stored pre-Phase 04 approval still
    verifies exactly as it did when the customer confirmed it."""
    legacy = {
        "action_type": "OPEN_DEPOT_ACCOUNT", "application_id": "app-1", "provider_id": "p",
        "product_id": "q", "channel_id": "direct", "customer_ref": "cust-1",
        "catalog_version": "1", "document_id": "doc", "document_version": "1.0",
        "document_digest": "a" * 64,
    }  # fmt: skip
    phase03_digest = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert ApprovedAction(**legacy).digest() == phase03_digest
    bound = ApprovedAction(**legacy, terms_digest="b" * 64, partner_id="sampletown-coop-bank")
    assert bound.digest() != phase03_digest


# --- verified partner-channel attribution -------------------------------------------------------


def test_a_partner_attribution_is_recorded_from_the_registry_with_the_consent(flow: Flow) -> None:
    started = confirmed(flow, PARTNER_INTENT)
    (row,) = flow.sql(
        "SELECT application_id, provider_id, channel_id, channel_kind, partner_id, "
        "registry_version, consent_receipt_id FROM channel_attributions"
    )
    (consent_id,) = flow.sql("SELECT receipt_id FROM consent_receipts")[0]
    manifest_digest = flow.container.catalog.get_provider("fixture-bank").manifest_digest
    assert row == (
        started.application_id, "fixture-bank", "partner-1", "PARTNER_BANK",
        "sampletown-coop-bank", manifest_digest, consent_id,
    )  # fmt: skip
    # three distinct roles: depot provider, partner bank - and the partner is not the provider
    assert row[1] != row[4]


def test_a_direct_channel_attributes_no_partner(flow: Flow) -> None:
    confirmed(flow)
    assert flow.sql("SELECT channel_kind, partner_id FROM channel_attributions") == [
        ("DIRECT", None)
    ]


@pytest.mark.parametrize(
    "extra",
    [{"partner_id": "agent-friendly-bank"}, {"commission_recipient": "agent-a"}],
)
def test_the_agent_cannot_choose_a_commission_recipient(flow: Flow, extra: dict[str, str]) -> None:
    from fastapi.testclient import TestClient

    client: TestClient = flow.agent_http
    response = client.post(
        "/v1/applications",
        json={**INTENT.model_dump(), **extra},
        headers={
            **bootstrap_headers(client, customer_id="cust-1"),
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 422
    assert flow.sql("SELECT COUNT(*) FROM applications") == [(0,)]


@pytest.mark.parametrize("channel_id", ["partner-2", "partner-evil", "PARTNER-1", "partner_1"])
def test_inactive_unknown_or_fabricated_channels_are_refused(flow: Flow, channel_id: str) -> None:
    response = flow.agent_http.post(
        "/v1/applications",
        json={**INTENT.model_dump(), "channel_id": channel_id},
        headers={
            **bootstrap_headers(flow.agent_http, customer_id="cust-1"),
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 422


def test_a_channel_that_does_not_serve_the_product_is_refused(flow: Flow) -> None:
    def second_product_only_via_partner(m: Manifest) -> None:
        extra = {**m["products"][0], "product_id": "depot-plus", "name": "Depot Plus (MOCK)"}
        m["products"].append(extra)
        partner = next(c for c in m["channels"] if c["channel_id"] == "partner-1")
        partner["product_ids"] = ["depot-plus"]

    flow.container.catalog = catalog_with(second_product_only_via_partner)
    response = flow.agent_http.post(
        "/v1/applications",
        json={**PARTNER_INTENT.model_dump()},  # depot-basic through partner-1: not offered
        headers={
            **bootstrap_headers(flow.agent_http, customer_id="cust-1"),
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 422


def test_an_ended_partnership_stops_a_pending_approval(flow: Flow) -> None:
    started = flow.start(intent=PARTNER_INTENT)
    flow.container.catalog = catalog_with(end_partnership)
    response = flow.customer_confirm(started)
    assert response.status_code == 409
    assert flow.sql("SELECT COUNT(*) FROM channel_attributions") == [(0,)]


def test_the_attribution_cannot_change_after_consent(flow: Flow) -> None:
    started = confirmed(flow, PARTNER_INTENT)
    for statement in (
        "UPDATE channel_attributions SET partner_id = 'example-valley-savings-bank'",
        "DELETE FROM channel_attributions",
        "UPDATE applications SET channel_id = 'direct'",
        "UPDATE applications SET provider_id = 'rogue-bank'",
    ):
        with pytest.raises(DBAPIError):
            flow.execute(statement)
    with pytest.raises(DBAPIError):  # one attribution per application
        flow.execute("INSERT INTO channel_attributions SELECT * FROM channel_attributions")
    assert flow.sql("SELECT channel_id, partner_id FROM channel_attributions") == [
        ("partner-1", "sampletown-coop-bank")
    ]
    assert flow.sql(
        "SELECT channel_id FROM applications WHERE application_id = :a", a=started.application_id
    ) == [("partner-1",)]
