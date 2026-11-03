"""Trusted customer handoff: creation, binding, single use, expiry, identity, digest."""

import hashlib
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.domain.enums import ApplicationStatus as S
from app.domain.handoff import ApprovedAction
from app.domain.workflow import TransitionAuthority as A
from app.integrations.catalog_fixture import (
    CatalogFixture,
    FixtureProviderDiscovery,
    canonical_json,
    sha256_hex,
    sign_manifest,
    tampered_copy,
)
from tests.flow import INTENT, Flow, customer_headers


def new_catalog_version() -> FixtureProviderDiscovery:
    """A *legitimately re-published* catalog: new disclosure text/version, consistently pinned."""
    fixture: CatalogFixture = tampered_copy()
    text = fixture.risk_documents.pop("kid-depot-basic@1.0") + " Revised wording."
    digest = sha256_hex(text.encode())
    fixture.risk_documents["kid-depot-basic@2.0"] = text
    fixture.pinned_risk_digests.pop("kid-depot-basic@1.0")
    fixture.pinned_risk_digests["kid-depot-basic@2.0"] = digest
    product = fixture.manifests["fixture-bank"]["products"][0]
    product.update(risk_document_version="2.0", risk_document_digest=digest)
    fixture.pinned_manifest_digests["fixture-bank"] = sha256_hex(
        canonical_json(fixture.manifests["fixture-bank"])
    )
    fixture.signatures["fixture-bank"] = sign_manifest(fixture.manifests["fixture-bank"])
    return FixtureProviderDiscovery(fixture)


def handoff_rows(flow: Flow) -> list[tuple]:
    return flow.sql(
        "SELECT handoff_id, nonce_hash, action_digest, customer_id, consumed_at, revoked_at "
        "FROM handoffs ORDER BY created_at"
    )


# --- creating the handoff --------------------------------------------------


def test_handoff_is_created_persisted_and_bound_to_the_exact_action(flow: Flow) -> None:
    started = flow.start()

    (row,) = handoff_rows(flow)
    handoff_id, nonce_hash, digest, customer_id, consumed_at, revoked_at = row
    assert handoff_id == started.handoff_id
    assert (customer_id, consumed_at, revoked_at) == (None, None, None)  # bound at confirm only
    assert nonce_hash == hashlib.sha256(started.nonce.encode()).hexdigest()
    assert digest == started.action_digest

    # The digest covers provider, product, channel, customer reference and disclosure version.
    (action_json,) = flow.sql("SELECT action FROM handoffs")[0]
    action = ApprovedAction.model_validate_json(action_json)
    assert action.digest() == digest
    assert (action.provider_id, action.product_id, action.channel_id) == (
        "fixture-bank",
        "depot-basic",
        "direct",
    )
    assert (action.document_id, action.document_version) == ("kid-depot-basic", "1.0")
    assert action.customer_ref == "cust-1" and action.application_id == started.application_id


def test_the_nonce_itself_is_never_stored_anywhere(flow: Flow) -> None:
    started = flow.start()
    flow.customer_confirm(started)
    for table in ("handoffs", "consent_receipts", "audit_events", "applications", "mandates"):
        dump = repr(flow.sql(f"SELECT * FROM {table}"))  # noqa: S608
        assert started.nonce not in dump, table


def test_handoff_expires_after_the_configured_ttl_and_moves_the_draft_forward(flow: Flow) -> None:
    started = flow.start()
    ((expires_at, created_at),) = flow.sql("SELECT expires_at, created_at FROM handoffs")
    ttl = datetime.fromisoformat(expires_at) - datetime.fromisoformat(created_at)
    assert ttl.total_seconds() == 600
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    types = flow.event_types(started.application_id)
    assert types == ["application.created", "application.transition", "handoff.created"]
    assert flow.audit(started.application_id)[1].details["reason_code"] == "handoff_created"


def test_agent_response_contains_the_pointer_but_no_customer_data(flow: Flow) -> None:
    started = flow.start()
    response = flow.agent_http.post(
        f"/v1/applications/{started.application_id}/handoff",
        headers=flow.onboarding_headers(started.application_id),
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["state"] == "AWAITING_CUSTOMER"
    assert "customer_ref" not in body["action"] and "cust-1" not in response.text
    assert body["action"]["document_version"] == "1.0"
    assert "/customer/handoffs/" in body["confirmation_url"]


def test_a_new_handoff_supersedes_the_old_one(flow: Flow) -> None:
    first = flow.start()
    second_response = flow.agent_http.post(
        f"/v1/applications/{first.application_id}/handoff",
        headers=flow.onboarding_headers(first.application_id),
    )
    assert second_response.status_code == 201
    new_nonce = second_response.json()["confirmation_url"].split("token=")[1]
    new_id = second_response.json()["handoff_id"]

    old = flow.customer_confirm(first)
    assert old.status_code == 409 and old.json()["error"]["code"] == "handoff_not_active"
    assert flow.status_of(first.application_id) == "AWAITING_AUTHORIZATION"

    ok = flow.customer_http.post(
        f"/v1/customer/handoffs/{new_id}/confirm",
        json={"nonce": new_nonce, "action_digest": first.action_digest},
        headers=customer_headers("cust-1"),
    )
    assert ok.status_code == 200
    active = flow.sql(
        "SELECT COUNT(*) FROM handoffs WHERE consumed_at IS NULL AND revoked_at IS NULL"
    )
    assert active == [(0,)]


def test_database_allows_only_one_active_handoff_per_application(flow: Flow) -> None:
    started = flow.start()
    with pytest.raises(IntegrityError, match="UNIQUE"):
        flow.execute(  # a second *active* row must be impossible even for raw SQL
            "INSERT INTO handoffs (handoff_id, application_id, nonce_hash, action, action_digest, "
            "created_at, expires_at) SELECT 'hnd-dup', application_id, nonce_hash, action, "
            "action_digest, created_at, expires_at FROM handoffs WHERE handoff_id = :h",
            h=started.handoff_id,
        )


def test_another_agent_cannot_request_a_handoff_for_my_application(flow: Flow) -> None:
    started = flow.start()
    intruder = flow.start(
        actor_id="agent-b", intent=INTENT.model_copy(update={"customer_ref": "c9"})
    )
    response = flow.agent_http.post(  # agent-b's VALID onboarding token, but for its own app
        f"/v1/applications/{started.application_id}/handoff",
        headers=flow.onboarding_headers(intruder.application_id, "agent-b"),
    )
    assert response.status_code == 404
    assert "application.access_denied" in flow.event_types(started.application_id)
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


def test_handoff_is_refused_without_an_active_mandate(flow: Flow) -> None:
    app_id = flow.draft(INTENT)
    flow.execute(  # an operator revokes the onboarding grant directly in the database
        "UPDATE mandates SET revoked_at = '2026-11-03 09:00:00' "
        "WHERE application_id = :a AND kind = 'onboarding'",
        a=app_id,
    )
    response = flow.agent_http.post(
        f"/v1/applications/{app_id}/handoff", headers=flow.onboarding_headers(app_id)
    )
    # Phase 03: a token whose grant is revoked is itself invalid (RFC 6750: 401 invalid_token).
    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_token"
    assert flow.status_of(app_id) == "DRAFT"
    reasons = [e.details.get("reason") for e in flow.audit(app_id)]
    assert "grant_revoked" in reasons


def test_handoff_is_refused_once_the_mandate_has_expired(flow: Flow) -> None:
    app_id = flow.draft(INTENT)
    flow.clock.advance(hours=2)
    response = flow.agent_http.post(
        f"/v1/applications/{app_id}/handoff", headers=flow.onboarding_headers(app_id)
    )
    # The onboarding token expires with its grant (no refresh): 401, not a business decision.
    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_token"
    assert flow.status_of(app_id) == "DRAFT"
    assert "token_expired" in [e.details.get("reason") for e in flow.audit(app_id)]


def test_handoff_is_refused_for_an_application_that_is_past_that_stage(flow: Flow) -> None:
    started = flow.start()
    flow.service().transition(
        started.application_id,
        S.CANCELLED,
        authority=A.CUSTOMER_CHANNEL,
        reason_code="customer_cancelled",
        correlation_id="corr-t",
    )
    response = flow.agent_http.post(
        f"/v1/applications/{started.application_id}/handoff",
        headers=flow.onboarding_headers(started.application_id),
    )
    # The cancellation (terminal state) revoked the onboarding grant AND its tokens.
    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_token"
    assert flow.status_of(started.application_id) == "CANCELLED"


# --- the customer's confirmation --------------------------------------------------


def test_customer_confirmation_authorizes_and_runs_the_mock_checks(flow: Flow) -> None:
    started = flow.start()
    response = flow.customer_confirm(started)
    assert response.status_code == 200
    assert response.json() == {
        "handoff_id": started.handoff_id,
        "application_id": started.application_id,
        "state": "IN_PROGRESS",
    }
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"

    transitions = [
        (e.details["from"], e.details["to"], e.details["authority"], e.details["reason_code"])
        for e in flow.audit(started.application_id)
        if e.event_type == "application.transition"
    ]
    assert transitions == [
        ("DRAFT", "AWAITING_AUTHORIZATION", "SYSTEM_POLICY", "handoff_created"),
        ("AWAITING_AUTHORIZATION", "AUTHORIZED", "CUSTOMER_CHANNEL", "customer_confirmed"),
        ("AUTHORIZED", "KYC_PENDING", "SYSTEM_POLICY", "kyc_started"),
        ("KYC_PENDING", "COMPLIANCE_PENDING", "SYSTEM_POLICY", "kyc_passed"),
        ("COMPLIANCE_PENDING", "CONTRACT_PENDING", "SYSTEM_POLICY", "compliance_passed"),
    ]


def test_confirmation_records_receipts_bound_to_this_customer_and_action(flow: Flow) -> None:
    started = flow.start()
    flow.customer_confirm(started)

    handoff = handoff_rows(flow)[0]
    assert handoff[3] == "cust-1" and handoff[4] is not None  # customer bound, consumed
    (consent,) = flow.sql(
        "SELECT customer_id, handoff_id, action_digest "
        "FROM consent_receipts WHERE application_id = :a",
        a=started.application_id,
    )
    assert consent == ("cust-1", started.handoff_id, started.action_digest)
    (disclosure,) = flow.sql(
        "SELECT customer_id, document_id, document_version FROM disclosure_receipts"
    )
    assert disclosure == ("cust-1", "kid-depot-basic", "1.0")


def test_confirmation_is_audited_with_the_customer_as_actor(flow: Flow) -> None:
    started = flow.start()
    flow.customer_confirm(started)
    confirmed = [
        e for e in flow.audit(started.application_id) if e.event_type == "handoff.confirmed"
    ]
    assert len(confirmed) == 1
    assert (confirmed[0].actor_type.value, confirmed[0].actor_id) == ("customer", "cust-1")
    assert started.nonce not in repr(confirmed[0].details)


# --- the agent cannot approve its own handoff -------------------------------------------


def test_the_agents_credentials_cannot_confirm_a_handoff(flow: Flow) -> None:
    started = flow.start()
    app_id = started.application_id
    for headers in (
        flow.onboarding_headers(app_id),  # it even holds the valid nonce and digest
        flow.status_headers(app_id),
        # ... and an agent token next to a (simulated) customer context does not count either
        {**flow.onboarding_headers(app_id), **customer_headers("cust-1")},
    ):
        response = flow.agent_http.post(
            f"/v1/customer/handoffs/{started.handoff_id}/confirm",
            json={"nonce": started.nonce, "action_digest": started.action_digest},
            headers=headers,
        )
        assert response.status_code == 401
    assert flow.status_of(app_id) == "AWAITING_AUTHORIZATION"
    assert handoff_rows(flow)[0][4] is None  # not consumed
    refusals = [e for e in flow.audit(app_id) if e.event_type == "handoff.rejected"]
    assert [(e.details["reason"], e.actor_type.value, e.actor_id) for e in refusals] == [
        ("agent_credentials_presented", "agent", "agent-a")
    ] * 3


def test_confirmation_without_any_customer_context_is_refused(flow: Flow) -> None:
    started = flow.start()
    response = flow.customer_http.post(
        f"/v1/customer/handoffs/{started.handoff_id}/confirm",
        json={"nonce": started.nonce, "action_digest": started.action_digest},
    )
    assert response.status_code == 401
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


def test_a_customer_context_does_not_work_on_agent_routes(flow: Flow) -> None:
    started = flow.start()
    response = flow.customer_http.get(
        f"/v1/applications/{started.application_id}", headers=customer_headers("cust-1")
    )
    assert response.status_code == 401


def test_the_scripted_agent_never_sends_customer_credentials_or_touches_customer_routes(
    flow: Flow,
) -> None:
    """Record every request the agent's HTTP client makes during a full journey."""
    sent: list[tuple[str, str, dict[str, str]]] = []

    class RecordingClient(TestClient):
        def request(self, method, url, **kwargs):  # type: ignore[no-untyped-def]  # noqa: ANN001, ANN003, ANN201
            sent.append((method, str(url), dict(kwargs.get("headers") or {})))
            return super().request(method, url, **kwargs)

    flow.agent_http = RecordingClient(flow.app)
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    flow.agent().continue_after_customer(started.application_id)
    flow.provider_confirms(started.application_id)
    flow.agent().read_status(started.application_id)

    assert sent, "the agent made no requests?"
    for method, url, headers in sent:
        assert "/customer/" not in url and "/internal/" not in url, (method, url)
        names = {h.lower() for h in headers}
        assert "x-dev-customer-id" not in names and "x-dev-actor-id" not in names
        assert not any(k.lower().startswith("x-provider") for k in headers)
        if url.startswith("/v1/applications"):  # every protected call carries a bearer token
            assert headers["Authorization"].startswith("Bearer tqa_"), (method, url)


def test_link_alone_is_not_authentication_the_page_shows_nothing_without_sign_in(
    flow: Flow,
) -> None:
    started = flow.start()
    page = flow.customer_http.get(
        f"/customer/handoffs/{started.handoff_id}", params={"token": started.nonce}
    )
    assert page.status_code == 200
    assert "Sign in" in page.text
    for detail in (
        "Example Depot Bank",
        "depot-basic",
        started.application_id,
        started.action_digest,
    ):
        assert detail not in page.text  # nothing about the request is revealed to an unknown viewer
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


# --- wrong identity / digest / challenge ------------------------------------------------


def test_wrong_customer_identity_is_rejected_and_the_handoff_stays_usable(flow: Flow) -> None:
    started = flow.start()
    response = flow.customer_confirm(started, customer_id="cust-2")
    assert response.status_code == 403 and response.json()["error"]["code"] == "customer_mismatch"
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    assert flow.sql("SELECT COUNT(*) FROM consent_receipts") == [(0,)]
    rejected = [e for e in flow.audit(started.application_id) if e.event_type == "handoff.rejected"]
    assert rejected[0].details["reason"] == "customer_mismatch" and rejected[0].actor_id == "cust-2"
    # A stranger's failed attempt must not burn the real customer's handoff.
    assert flow.customer_confirm(started, customer_id="cust-1").status_code == 200


def test_a_changed_action_digest_blocks_confirmation(flow: Flow) -> None:
    started = flow.start()
    response = flow.customer_confirm(started, action_digest="0" * 64)
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "action_digest_mismatch"
    )
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    assert handoff_rows(flow)[0][4] is None
    # ... and the digest of a *different* application's action is no better.
    other = flow.start(
        actor_id="agent-b",
        intent=INTENT.model_copy(update={"customer_ref": "cust-1", "channel_id": "partner-1"}),
    )
    assert flow.customer_confirm(started, action_digest=other.action_digest).status_code == 409


def test_wrong_or_unknown_challenge_is_rejected_without_revealing_which(flow: Flow) -> None:
    started = flow.start()
    wrong = flow.customer_confirm(started, nonce="A" * 43)
    unknown = flow.customer_http.post(
        "/v1/customer/handoffs/hnd-doesnotexist/confirm",
        json={"nonce": started.nonce, "action_digest": started.action_digest},
        headers=customer_headers("cust-1"),
    )
    assert wrong.status_code == unknown.status_code == 403
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "handoff_invalid"
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


def test_an_expired_handoff_is_rejected(flow: Flow) -> None:
    started = flow.start()
    flow.clock.advance(seconds=599)
    assert (
        flow.customer_http.get(  # still valid one second before the end
            f"/customer/handoffs/{started.handoff_id}", params={"token": started.nonce}
        ).status_code
        == 200
    )
    flow.clock.advance(seconds=1)  # exactly at expiry: closed
    response = flow.customer_confirm(started)
    assert response.status_code == 410 and response.json()["error"]["code"] == "handoff_expired"
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    assert flow.sql("SELECT COUNT(*) FROM consent_receipts") == [(0,)]


def test_a_reused_handoff_is_rejected_and_creates_no_second_consent(flow: Flow) -> None:
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    again = flow.customer_confirm(started)
    assert again.status_code == 409 and again.json()["error"]["code"] == "handoff_not_active"
    assert flow.sql("SELECT COUNT(*) FROM consent_receipts") == [(1,)]
    assert flow.sql("SELECT COUNT(*) FROM disclosure_receipts") == [(1,)]
    assert flow.event_types(started.application_id).count("handoff.confirmed") == 1


def test_confirmation_is_refused_when_the_catalog_changed_since_the_handoff(flow: Flow) -> None:
    started = flow.start()
    flow.container.catalog = new_catalog_version()  # disclosure document is now version 2.0
    response = flow.customer_confirm(started)
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "action_digest_mismatch"
    )
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    reasons = [e.details.get("reason") for e in flow.audit(started.application_id)]
    assert "action_no_longer_current" in reasons
    # A fresh handoff shows the customer the new version, and then it works.
    fresh = flow.agent_http.post(
        f"/v1/applications/{started.application_id}/handoff",
        headers=flow.onboarding_headers(started.application_id),
    ).json()
    assert fresh["action"]["document_version"] == "2.0"


def test_confirmation_is_refused_after_the_mandate_expired(
    flow_factory: Callable[..., Flow],
) -> None:
    flow = flow_factory(handoff_ttl_seconds=3600, mandate_ttl_seconds=60)
    started = flow.start()
    flow.clock.advance(seconds=120)
    response = flow.customer_confirm(started)
    assert response.status_code == 403 and response.json()["error"]["code"] == "mandate_inactive"
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


def test_confirmation_is_refused_when_the_application_is_no_longer_awaiting(flow: Flow) -> None:
    started = flow.start()
    flow.service().transition(
        started.application_id, S.FAILED, authority=A.SYSTEM_POLICY,
        reason_code="timeout", correlation_id="corr-t",
    )  # fmt: skip
    response = flow.customer_confirm(started)
    assert response.status_code in {403, 409}
    assert flow.status_of(started.application_id) == "FAILED"


def test_confirmation_request_body_is_strict(flow: Flow) -> None:
    started = flow.start()
    url = f"/v1/customer/handoffs/{started.handoff_id}/confirm"
    base = {"nonce": started.nonce, "action_digest": started.action_digest}
    headers = customer_headers("cust-1")
    assert (
        flow.customer_http.post(url, json={**base, "status": "OPENED"}, headers=headers).status_code
        == 422
    )
    assert (
        flow.customer_http.post(
            url, json={**base, "customer_id": "cust-9"}, headers=headers
        ).status_code
        == 422
    )
    assert (
        flow.customer_http.post(url, json={**base, "nonce": "short"}, headers=headers).status_code
        == 422
    )
    assert (
        flow.customer_http.post(
            url, json={**base, "action_digest": "XYZ"}, headers=headers
        ).status_code
        == 422
    )
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


# --- concurrency --------------------------------------------------


def test_two_simultaneous_confirmations_of_one_handoff_let_exactly_one_win(flow: Flow) -> None:
    started = flow.start()
    clients = [TestClient(flow.app), TestClient(flow.app)]
    barrier = threading.Barrier(2)

    def attempt(i: int) -> int:
        barrier.wait()
        return flow.customer_confirm(started, client=clients[i]).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(attempt, range(2)))
    assert codes == [200, 409]
    assert flow.sql("SELECT COUNT(*) FROM consent_receipts") == [(1,)]
    assert flow.event_types(started.application_id).count("handoff.confirmed") == 1
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"
