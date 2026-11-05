"""Finding F-1: the customer JSON confirmation needs the session, not a self-asserted header.

The ceremony's bindings (customer, nonce, digest, single use) are covered in `test_handoff.py`.
This file holds the attack from Phase 06 and each credential a caller could present instead of
the real session + CSRF token. The last test pins what this fix does NOT change: the DEV sign-in
itself still lets any HTTP client claim to be any synthetic customer.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.main import create_app
from tests.flow import (
    Flow,
    Started,
    confirm_csrf_headers,
    customer_headers,
    sign_in_browser,
)


def url_of(started: Started) -> str:
    return f"/v1/customer/handoffs/{started.handoff_id}/confirm"


def body_of(started: Started) -> dict[str, str]:
    return {"nonce": started.nonce, "action_digest": started.action_digest}


def assert_nothing_happened(flow: Flow, started: Started) -> None:
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    assert flow.sql("SELECT COUNT(*) FROM consent_receipts") == [(0,)]
    assert flow.sql("SELECT consumed_at FROM handoffs") == [(None,)]


def rejection_reasons(flow: Flow, started: Started) -> list[str]:
    return [
        e.details["reason"]
        for e in flow.audit(started.application_id)
        if e.event_type == "handoff.rejected"
    ]


def test_the_phase_06_attack_values_the_agent_holds_plus_the_header_is_refused(flow: Flow) -> None:
    started = flow.start()
    response = TestClient(flow.app).post(
        url_of(started), json=body_of(started), headers=customer_headers("cust-1")
    )
    assert response.status_code == 401
    assert_nothing_happened(flow, started)
    submit = flow.agent_http.post(
        f"/v1/applications/{started.application_id}/submit",
        headers=flow.onboarding_headers(started.application_id),
    )
    assert submit.status_code == 409  # no consent, so the policy gate stops the agent
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]


def test_a_missing_customer_session_is_refused(flow: Flow) -> None:
    started = flow.start()
    response = TestClient(flow.app).post(url_of(started), json=body_of(started))
    assert response.status_code == 401
    assert_nothing_happened(flow, started)


@pytest.mark.parametrize("cookie", ["tqs_" + "A" * 43, "not-a-session", "tqa_" + "A" * 43])
def test_an_invalid_customer_session_is_refused(flow: Flow, cookie: str) -> None:
    started = flow.start()
    browser = TestClient(flow.app)
    browser.cookies.set("trusteq_customer_api", cookie, path="/v1/customer")
    response = browser.post(url_of(started), json=body_of(started))
    assert response.status_code == 401
    assert_nothing_happened(flow, started)


def test_an_expired_session_is_refused(flow: Flow) -> None:
    started = flow.start()
    browser = flow.customer_browser("cust-1")
    headers = confirm_csrf_headers(browser, started.handoff_id)
    flow.clock.advance(seconds=901)  # the session lifetime is 900 s; the server enforces it
    response = browser.post(url_of(started), json=body_of(started), headers=headers)
    assert response.status_code == 401  # 401 from the session check, before any handoff check
    assert_nothing_happened(flow, started)


def test_a_header_for_the_right_customer_does_not_help_the_wrong_session(flow: Flow) -> None:
    started = flow.start()
    other = flow.customer_browser("cust-2")
    response = other.post(
        url_of(started),
        json=body_of(started),
        headers={
            **customer_headers("cust-1"),
            **confirm_csrf_headers(other, started.handoff_id),
        },
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "customer_mismatch"
    assert_nothing_happened(flow, started)
    assert rejection_reasons(flow, started) == ["customer_mismatch"]
    # The stranger's attempt did not burn the real customer's challenge.
    assert flow.customer_confirm(started).status_code == 200


@pytest.mark.parametrize("forged", [None, "", "A" * 43, "not-a-token", "<script>"])
def test_a_missing_or_invalid_csrf_token_is_refused_and_audited(
    flow: Flow, forged: str | None
) -> None:
    started = flow.start()
    browser = flow.customer_browser("cust-1")
    headers = {} if forged is None else {"X-CSRF-Token": forged}
    response = browser.post(url_of(started), json=body_of(started), headers=headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == "csrf_token_invalid"
    assert_nothing_happened(flow, started)
    assert rejection_reasons(flow, started) == ["csrf_token_invalid"]


def test_a_csrf_token_of_another_handoff_or_session_is_refused(flow: Flow) -> None:
    started = flow.start()
    browser = flow.customer_browser("cust-1")
    other_handoff = confirm_csrf_headers(browser, "hnd-another")
    assert (
        browser.post(url_of(started), json=body_of(started), headers=other_handoff).status_code
        == 403
    )
    stranger = TestClient(flow.app)
    sign_in_browser(stranger, "cust-1")  # a second session of the same customer
    foreign = confirm_csrf_headers(stranger, started.handoff_id)
    assert browser.post(url_of(started), json=body_of(started), headers=foreign).status_code == 403
    assert_nothing_happened(flow, started)


def test_an_agent_token_is_refused_even_next_to_a_valid_session_and_csrf_token(flow: Flow) -> None:
    started = flow.start()
    browser = flow.customer_browser("cust-1")
    response = browser.post(
        url_of(started),
        json=body_of(started),
        headers={
            **flow.onboarding_headers(started.application_id),
            **confirm_csrf_headers(browser, started.handoff_id),
        },
    )
    assert response.status_code == 401
    assert_nothing_happened(flow, started)
    assert rejection_reasons(flow, started) == ["agent_credentials_presented"]


def test_a_replayed_challenge_creates_no_second_consent(flow: Flow) -> None:
    started = flow.start()
    browser = flow.customer_browser("cust-1")
    headers = confirm_csrf_headers(browser, started.handoff_id)
    first = browser.post(url_of(started), json=body_of(started), headers=headers)
    replay = browser.post(url_of(started), json=body_of(started), headers=headers)
    assert first.status_code == 200
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "handoff_not_active"
    assert flow.sql("SELECT COUNT(*) FROM consent_receipts") == [(1,)]


def test_a_wrong_challenge_is_refused_with_the_valid_session(flow: Flow) -> None:
    started = flow.start()
    browser = flow.customer_browser("cust-1")
    response = browser.post(
        url_of(started),
        json={**body_of(started), "nonce": "A" * 43},
        headers=confirm_csrf_headers(browser, started.handoff_id),
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "handoff_invalid"
    assert_nothing_happened(flow, started)


def test_a_tampered_digest_is_refused(flow: Flow) -> None:
    started = flow.start()
    browser = flow.customer_browser("cust-1")
    response = browser.post(
        url_of(started),
        json={**body_of(started), "action_digest": "0" * 64},
        headers=confirm_csrf_headers(browser, started.handoff_id),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "action_digest_mismatch"
    assert_nothing_happened(flow, started)


@pytest.mark.parametrize(
    "browser_headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_a_cross_origin_request_is_refused(flow: Flow, browser_headers: dict[str, str]) -> None:
    started = flow.start()
    browser = flow.customer_browser("cust-1")
    response = browser.post(
        url_of(started),
        json=body_of(started),
        headers={**browser_headers, **confirm_csrf_headers(browser, started.handoff_id)},
    )
    assert response.status_code == 403
    assert_nothing_happened(flow, started)


def test_the_valid_customer_confirms_and_opened_still_needs_the_provider(flow: Flow) -> None:
    started = flow.start()
    response = flow.customer_confirm(started)
    assert response.status_code == 200 and response.json()["state"] == "IN_PROGRESS"
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"
    confirmed = [
        e for e in flow.audit(started.application_id) if e.event_type == "handoff.confirmed"
    ]
    assert (confirmed[0].actor_type.value, confirmed[0].actor_id) == ("customer", "cust-1")

    assert flow.agent().continue_after_customer(started.application_id).stopped_at is None
    assert flow.status_of(started.application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    unsigned = flow.provider_http.post(
        "/internal/depot-provider/confirmations", content=b'{"outcome":"OPENED"}'
    )
    assert unsigned.status_code == 401
    assert flow.status_of(started.application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert flow.provider_confirms(started.application_id).status_code == 200
    assert flow.status_of(started.application_id) == "OPENED"


def test_the_openapi_contract_no_longer_offers_the_header(flow: Flow) -> None:
    document = flow.agent_http.get("/openapi.json").json()
    assert "DevCustomerHeader" not in document["components"].get("securitySchemes", {})
    operation = document["paths"]["/v1/customer/handoffs/{handoff_id}/confirm"]["post"]
    assert "security" not in operation
    headers = {p["name"] for p in operation["parameters"] if p["in"] == "header"}
    assert headers == {"X-CSRF-Token"}


def test_in_production_there_is_no_session_and_the_confirmation_fails_closed(db_url: str) -> None:
    settings = Settings(_env_file=None, environment=Environment.PRODUCTION, database_url=db_url)
    with TestClient(create_app(settings)) as prod:
        prod.cookies.set("trusteq_customer_api", "tqs_" + "A" * 43, path="/v1/customer")
        response = prod.post(
            "/v1/customer/handoffs/hnd-1/confirm",
            json={"nonce": "n" * 20, "action_digest": "a" * 64},
            headers={"X-Dev-Customer-Id": "cust-1", "X-CSRF-Token": "A" * 43},
        )
    assert response.status_code == 401


def test_residual_limit_any_http_client_can_still_script_the_dev_sign_in(flow: Flow) -> None:
    """NOT a security guarantee, but a pin of what remains: the DEV identity boundary.

    With the values an agent holds and the customer reference, a client that speaks HTTP itself
    can use the sign-in form and record a consent. The audit trail shows a simulated sign-in, and
    OPENED still needs the provider's signature. Only a real bank sign-in closes this.
    """
    started = flow.start()
    rogue = TestClient(flow.app)
    link = f"/customer/handoffs/{started.handoff_id}"
    form = rogue.get(link, params={"token": started.nonce}).text
    login = rogue.post(
        "/customer/dev-login",
        data={
            "customer_id": "cust-1",
            "handoff_id": started.handoff_id,
            "token": started.nonce,
            "login_csrf": re.search(r'name="login_csrf" value="([^"]+)"', form).group(1),
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert flow.customer_confirm(started, client=rogue).status_code == 200
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"  # the limit is real
    assert flow.sql(
        "SELECT COUNT(*) FROM audit_events WHERE event_type = 'customer.session_started' "
        "AND details LIKE '%\"simulated_sign_in\": true%'"
    ) == [(1,)]

    flow.agent().continue_after_customer(started.application_id)
    forged = flow.provider_http.post(
        "/internal/depot-provider/confirmations", content=b'{"outcome":"OPENED"}'
    )
    assert forged.status_code == 401
    assert flow.status_of(started.application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
