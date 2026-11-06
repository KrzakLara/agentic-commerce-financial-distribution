"""Tax information and the knowledge-and-experience questions, in the real onboarding journey.

Everything runs through the real routes with the step switched on, as in the default application.
"""

import html
import logging
import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.integrations.agent_tools import AgentToolName
from app.services.appropriateness import AppropriatenessOutcome as Outcome
from app.services.customer_session_service import csrf_token
from scripts import customer_steps as steps
from tests.flow import Flow, Started, api_session_secret, sign_in_browser

CUSTOMER = "cust-1"
CONSENT_COUNT = "SELECT COUNT(*) FROM consent_receipts"


@pytest.fixture
def gated(flow_factory: Callable[..., Flow]) -> Flow:
    return flow_factory(customer_information_required=True)


def link(started: Started) -> str:
    return steps.link_of(started.handoff_id, started.nonce)


def signed_in(flow: Flow) -> tuple[Started, str]:
    started = flow.start()
    steps.sign_in(flow.customer_http, link(started), CUSTOMER)
    return started, link(started)


def stored_tax(flow: Flow, application_id: str) -> list[tuple]:
    return flow.sql(
        "SELECT tax_residency_country, external_verification FROM tax_information "
        "WHERE application_id = :a",
        a=application_id,
    )


def stored_assessment(flow: Flow, application_id: str) -> list[tuple]:
    return flow.sql(
        "SELECT outcome, acknowledged_at FROM appropriateness_assessments "
        "WHERE application_id = :a",
        a=application_id,
    )


def whole_database(flow: Flow) -> str:
    tables = flow.sql("SELECT name FROM sqlite_master WHERE type = 'table'")
    return repr([flow.sql(f"SELECT * FROM {name}") for (name,) in tables])  # noqa: S608


def direct_confirm(flow: Flow, started: Started):  # noqa: ANN201
    """What a client could post without the page offering it: the real values, by hand."""
    return flow.customer_http.post(
        f"/customer/handoffs/{started.handoff_id}/confirm",
        data={
            "token": started.nonce,
            "action_digest": started.action_digest,
            "csrf_token": flow.page_csrf(started),
        },
    )


# --- the gate ------------------------------------------------------------------------------------


def test_the_step_is_on_by_default_in_the_application(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRUSTEQ_CUSTOMER_INFORMATION_REQUIRED")
    assert Settings(_env_file=None, environment=Environment.TEST).customer_information_required


def test_the_page_offers_no_confirmation_until_the_information_exists(gated: Flow) -> None:
    started, url = signed_in(gated)
    page = gated.customer_http.get(url).text
    assert "Confirm and authorize" not in page and 'name="action_digest"' not in page
    assert "waiting for your information and your confirmation" in page
    assert "Tax information (synthetic)" in page and "Knowledge and experience" in page


def test_the_bank_refuses_a_confirmation_without_the_information(gated: Flow) -> None:
    started, _ = signed_in(gated)
    response = direct_confirm(gated, started)
    assert response.status_code == 409
    assert gated.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    assert gated.sql(CONSENT_COUNT) == [(0,)]
    denied = [e.details for e in gated.audit(started.application_id) if e.outcome == "denied"]
    assert denied[-1]["reason"] == "customer_information_incomplete"
    assert denied[-1]["missing"] == ["tax_information", "appropriateness_assessment"]


@pytest.mark.parametrize(
    ("done", "missing"),
    [
        ("tax", ["appropriateness_assessment"]),
        ("answers", ["tax_information"]),
    ],
)
def test_one_of_the_two_is_not_enough(gated: Flow, done: str, missing: list[str]) -> None:
    started, url = signed_in(gated)
    if done == "tax":
        steps.post_tax(gated.customer_http, url, steps.TAX_COMPLETE)
    else:
        steps.post_answers(gated.customer_http, url, steps.ASSESSMENT_PROFILES["experienced"])
    assert direct_confirm(gated, started).status_code == 409
    denied = [e.details for e in gated.audit(started.application_id) if e.outcome == "denied"]
    assert denied[-1]["missing"] == missing


def test_the_agent_cannot_submit_past_the_gate(flow_factory: Callable[..., Flow]) -> None:
    """Defence in depth: a consent without the information is refused at submission too."""
    flow = flow_factory()  # consent recorded while the step was off ...
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    flow.app.state.settings = flow.app.state.settings.model_copy(
        update={"customer_information_required": True}
    )  # ... and the bank requires it now
    url = f"/v1/applications/{started.application_id}/submit"
    response = flow.agent_http.post(url, headers=flow.onboarding_headers(started.application_id))
    assert response.status_code == 409
    assert flow.status_of(started.application_id) == "CONTRACT_PENDING"
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]
    denied = [e for e in flow.audit(started.application_id) if e.outcome == "denied"]
    assert "tax_information" in denied[-1].details["missing"]


# --- tax information -----------------------------------------------------------------------------


def test_complete_tax_information_is_stored_without_the_identifier(
    gated: Flow, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    started, url = signed_in(gated)
    response = steps.post_tax(gated.customer_http, url, steps.TAX_COMPLETE)
    assert response.status_code == 303
    assert steps.SYNTHETIC_TAX_ID not in response.headers["location"]
    assert stored_tax(gated, started.application_id) == [("DE", "NOT_IMPLEMENTED")]

    page = gated.customer_http.get(url).text
    assert "Customer-provided on the bank&#x27;s page" in html.escape(html.unescape(page))
    assert "Not implemented: no tax authority is contacted" in page
    assert "Format accepted, value not stored" in page

    for where in (page, whole_database(gated), caplog.text, repr(response.headers)):
        assert steps.SYNTHETIC_TAX_ID not in where
    events = [e for e in gated.audit(started.application_id) if "tax" in e.event_type]
    assert events[-1].details["complete"] == "true"
    assert events[-1].details["synthetic_tax_id"] == "COLLECTED"


@pytest.mark.parametrize(
    ("change", "expected_label"),
    [
        ({"synthetic_tax_id": steps.REAL_LOOKING_TAX_ID}, "Invalid"),
        ({"synthetic_tax_id": ""}, "Missing"),
        ({"tax_residency_country": "germany"}, "Invalid"),
        ({"us_person_declared": "perhaps"}, "Invalid"),
        ({"us_person_declared": ""}, "Missing"),
        ({"exemption_order_amount_eur": "-5"}, "Invalid"),
        ({"church_tax_objection": "maybe"}, "Invalid"),
    ],
)
def test_incomplete_or_invalid_tax_information_is_named_and_not_stored(
    gated: Flow, caplog: pytest.LogCaptureFixture, change: dict[str, str], expected_label: str
) -> None:
    caplog.set_level(logging.DEBUG)
    started, url = signed_in(gated)
    response = steps.post_tax(gated.customer_http, url, {**steps.TAX_COMPLETE, **change})
    assert response.status_code == 422
    assert expected_label in response.text
    assert "Nothing was saved from this attempt" in response.text
    assert stored_tax(gated, started.application_id) == []
    assert "Confirm and authorize" not in response.text
    for value in (*change.values(), steps.SYNTHETIC_TAX_ID):
        if len(value) > 2:
            assert f'value="{value}"' not in response.text
    assert steps.REAL_LOOKING_TAX_ID not in whole_database(gated) + caplog.text + response.text
    assert gated.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


def test_an_invalid_attempt_does_not_replace_a_stored_record(gated: Flow) -> None:
    started, url = signed_in(gated)
    steps.post_tax(gated.customer_http, url, steps.TAX_COMPLETE)
    bad = steps.post_tax(gated.customer_http, url, {"tax_residency_country": "XX1"})
    assert bad.status_code == 422
    assert stored_tax(gated, started.application_id) == [("DE", "NOT_IMPLEMENTED")]


def test_a_client_cannot_state_its_own_source_or_verification(gated: Flow) -> None:
    started, url = signed_in(gated)
    forged = {
        **steps.TAX_COMPLETE,
        "source": "AGENT_OR_MODEL",
        "external_verification": "VERIFIED_BY_BZST",
    }
    assert steps.post_tax(gated.customer_http, url, forged).status_code == 303
    assert stored_tax(gated, started.application_id) == [("DE", "NOT_IMPLEMENTED")]
    assert gated.sql("SELECT source FROM tax_information") == [("CUSTOMER_ON_BANK_CHANNEL",)]


def api_headers(flow: Flow, started: Started, scope: str) -> dict[str, str]:
    secret = api_session_secret(flow.customer_http)
    return {"X-CSRF-Token": csrf_token(secret, scope, started.handoff_id)}


def test_the_json_endpoint_reports_statuses_and_never_values(gated: Flow) -> None:
    started, _ = signed_in(gated)
    url = f"/v1/customer/handoffs/{started.handoff_id}/tax-information"
    bad = gated.customer_http.post(
        url,
        json={"nonce": started.nonce, "synthetic_tax_id": steps.REAL_LOOKING_TAX_ID},
        headers=api_headers(gated, started, "tax"),
    )
    assert bad.status_code == 200 and bad.json()["complete"] is False
    assert bad.json()["field_statuses"]["synthetic_tax_id"] == "INVALID"
    assert bad.json()["field_statuses"]["tax_residency_country"] == "MISSING"
    assert steps.REAL_LOOKING_TAX_ID not in bad.text

    good = gated.customer_http.post(
        url,
        json={
            "nonce": started.nonce,
            "tax_residency_country": "DE",
            "synthetic_tax_id": steps.SYNTHETIC_TAX_ID,
            "us_person_declared": False,
        },
        headers=api_headers(gated, started, "tax"),
    )
    body = good.json()
    assert good.status_code == 200 and good.headers["cache-control"] == "no-store"
    assert body["complete"] is True and body["local_validation"] == "COMPLETE"
    assert body["external_verification"] == "NOT_IMPLEMENTED"
    assert body["data_source"] == "CUSTOMER_ON_BANK_CHANNEL"
    assert body["ready_for_confirmation"] is False  # the questions are still open
    assert steps.SYNTHETIC_TAX_ID not in good.text


def test_the_json_endpoint_refuses_unknown_fields_without_echoing_them(gated: Flow) -> None:
    started, _ = signed_in(gated)
    response = gated.customer_http.post(
        f"/v1/customer/handoffs/{started.handoff_id}/tax-information",
        json={"nonce": started.nonce, "synthetic_tax_id": 12345678901, "source": "AGENT_OR_MODEL"},
        headers=api_headers(gated, started, "tax"),
    )
    assert response.status_code == 422
    assert "12345678901" not in response.text and "AGENT_OR_MODEL" not in response.text
    assert stored_tax(gated, started.application_id) == []


# --- the knowledge-and-experience assessment ------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "outcome", "acknowledgement_needed", "shown"),
    [
        ("experienced", Outcome.ASSESSMENT_OK, False, "No warning from your answers"),
        ("novice", Outcome.WARNING_REQUIRED, True, "may not be appropriate"),
        ("incomplete", Outcome.INSUFFICIENT_INFORMATION, True, "Your answers are incomplete"),
    ],
)
def test_acceptance_a_b_c_each_result_reaches_the_customer_and_is_reproducible(
    gated: Flow, profile: str, outcome: Outcome, acknowledgement_needed: bool, shown: str
) -> None:
    started, url = signed_in(gated)
    browser = gated.customer_http
    steps.post_tax(browser, url, steps.TAX_COMPLETE)
    for _ in range(2):  # the same answers always give the same result
        assert (
            steps.post_answers(browser, url, steps.ASSESSMENT_PROFILES[profile]).status_code == 303
        )
        assert stored_assessment(gated, started.application_id) == [(outcome.value, None)]
    page = browser.get(url).text
    assert shown in html.unescape(page)
    assert "no result here stops the account opening" in page
    assert "not legally complete compliance" in page

    if acknowledgement_needed:
        assert "Confirm and authorize" not in page  # the customer reads the notice first
        assert direct_confirm(gated, started).status_code == 409
        assert steps.post_acknowledgement(browser, url).status_code == 303
        page = browser.get(url).text
    assert "Confirm and authorize" in page

    # No result rejects the customer: the journey continues to OPENED in all three cases.
    assert steps.post_confirmation(browser, url).status_code == 200
    assert gated.agent().continue_after_customer(started.application_id).stopped_at is None
    assert gated.provider_confirms(started.application_id).status_code == 200
    assert gated.status_of(started.application_id) == "OPENED"


def test_new_answers_replace_the_result_and_clear_the_acknowledgement(gated: Flow) -> None:
    started, url = signed_in(gated)
    browser = gated.customer_http
    steps.post_tax(browser, url, steps.TAX_COMPLETE)
    steps.post_answers(browser, url, steps.ASSESSMENT_PROFILES["novice"])
    steps.post_acknowledgement(browser, url)
    assert "Confirm and authorize" in browser.get(url).text
    steps.post_answers(browser, url, steps.ASSESSMENT_PROFILES["incomplete"])
    assert stored_assessment(gated, started.application_id) == [
        (Outcome.INSUFFICIENT_INFORMATION.value, None)
    ]
    assert "Confirm and authorize" not in browser.get(url).text


def test_an_acknowledgement_needs_a_result(gated: Flow) -> None:
    started, _ = signed_in(gated)
    response = gated.customer_http.post(
        f"/customer/handoffs/{started.handoff_id}/assessment",
        data={
            "token": started.nonce,
            "intent": "acknowledge",
            "csrf_token": csrf_token(
                api_session_secret(gated.customer_http), "assessment", started.handoff_id
            ),
        },
    )
    assert response.status_code == 409
    assert stored_assessment(gated, started.application_id) == []


@pytest.mark.parametrize(
    "tampered",
    [
        {"knows_capital_loss": "yes-of-course"},
        {"knows_no_guarantee": "1"},
        {"experience": "EXPERT"},
    ],
)
def test_acceptance_d_tampered_answers_are_rejected(gated: Flow, tampered: dict[str, str]) -> None:
    started, url = signed_in(gated)
    response = steps.post_answers(gated.customer_http, url, tampered)
    assert response.status_code == 400
    assert stored_assessment(gated, started.application_id) == []


def test_acceptance_d_a_submitted_result_or_source_has_no_effect(gated: Flow) -> None:
    started, url = signed_in(gated)
    forged = {
        **steps.ASSESSMENT_PROFILES["novice"],
        "outcome": "ASSESSMENT_OK",
        "source": "AGENT_OR_MODEL",
        "product_level": "BASIC",
        "acknowledged": "yes",
    }
    assert steps.post_answers(gated.customer_http, url, forged).status_code == 303
    assert stored_assessment(gated, started.application_id) == [
        (Outcome.WARNING_REQUIRED.value, None)
    ]


def test_the_json_assessment_refuses_a_stated_result(gated: Flow) -> None:
    started, _ = signed_in(gated)
    url = f"/v1/customer/handoffs/{started.handoff_id}/appropriateness"
    headers = api_headers(gated, started, "assessment")
    forged = gated.customer_http.post(
        url, json={"nonce": started.nonce, "outcome": "ASSESSMENT_OK"}, headers=headers
    )
    assert forged.status_code == 422
    good = gated.customer_http.post(
        url,
        json={
            "nonce": started.nonce,
            "knows_capital_loss": True,
            "knows_no_guarantee": True,
            "experience": "NONE",
        },
        headers=headers,
    )
    assert good.status_code == 200
    assert good.json()["outcome"] == "WARNING_REQUIRED"
    assert good.json()["ready_for_confirmation"] is False
    acknowledged = gated.customer_http.post(
        url, json={"nonce": started.nonce, "intent": "acknowledge"}, headers=headers
    )
    assert acknowledged.json()["acknowledged"] is True


# --- who may do this ------------------------------------------------------------------------------


@pytest.mark.parametrize("step", ["tax", "assessment"])
@pytest.mark.parametrize(
    ("probe", "status"),
    [
        ("no_session", 401),
        ("no_csrf", 403),
        ("csrf_of_another_step", 403),
        ("csrf_of_another_handoff", 403),
        ("cross_site", 403),
        ("agent_token", 401),
        ("self_asserted_header", 401),
        ("wrong_nonce", 403),
    ],
)
def test_acceptance_d_unauthorized_submissions_are_refused_and_store_nothing(
    gated: Flow, step: str, probe: str, status: int
) -> None:
    started, _ = signed_in(gated)
    secret = api_session_secret(gated.customer_http)
    other = "confirm" if step == "tax" else "tax"
    token, headers, client = started.nonce, {}, gated.customer_http
    csrf = csrf_token(secret, step, started.handoff_id)
    if probe == "no_session":
        client = TestClient(gated.app)
    elif probe == "no_csrf":
        csrf = ""
    elif probe == "csrf_of_another_step":
        csrf = csrf_token(secret, other, started.handoff_id)
    elif probe == "csrf_of_another_handoff":
        csrf = csrf_token(secret, step, "hnd-other")
    elif probe == "cross_site":
        headers = {"Origin": "https://evil.example"}
    elif probe == "agent_token":
        headers = gated.onboarding_headers(started.application_id)
    elif probe == "self_asserted_header":
        client = TestClient(gated.app)
        headers = {"X-Dev-Customer-Id": CUSTOMER}
    elif probe == "wrong_nonce":
        token = "A" * 43
    data = {
        **(steps.TAX_COMPLETE if step == "tax" else steps.ASSESSMENT_PROFILES["experienced"]),
        "token": token,
        "csrf_token": csrf,
        "intent": "answer",
    }
    response = client.post(
        f"/customer/handoffs/{started.handoff_id}/{step}",
        data=data,
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == status, response.text
    assert stored_tax(gated, started.application_id) == []
    assert stored_assessment(gated, started.application_id) == []
    assert gated.status_of(started.application_id) == "AWAITING_AUTHORIZATION"


def test_another_customer_cannot_supply_or_see_this_information(gated: Flow) -> None:
    started, url = signed_in(gated)
    steps.post_tax(gated.customer_http, url, steps.TAX_COMPLETE)
    other = TestClient(gated.app)
    sign_in_browser(other, "cust-2")
    secret = api_session_secret(other)
    response = other.post(
        f"/customer/handoffs/{started.handoff_id}/tax",
        data={
            **steps.TAX_COMPLETE,
            "tax_residency_country": "FR",
            "token": started.nonce,
            "csrf_token": csrf_token(secret, "tax", started.handoff_id),
        },
    )
    assert response.status_code == 403
    assert stored_tax(gated, started.application_id) == [("DE", "NOT_IMPLEMENTED")]
    page = other.get(url)
    assert page.status_code == 403 and "Tax information" not in page.text


def test_nothing_can_change_after_the_confirmation(gated: Flow) -> None:
    started, url = signed_in(gated)
    browser = gated.customer_http
    steps.complete_customer_information(browser, url)
    data = {
        **steps.TAX_COMPLETE,
        "tax_residency_country": "FR",
        "token": started.nonce,
        "csrf_token": csrf_token(api_session_secret(browser), "tax", started.handoff_id),
    }
    assert steps.post_confirmation(browser, url).status_code == 200
    refused = browser.post(f"/customer/handoffs/{started.handoff_id}/tax", data=data)
    assert refused.status_code == 409  # the link is used up
    assert stored_tax(gated, started.application_id) == [("DE", "NOT_IMPLEMENTED")]


def test_nothing_can_be_supplied_on_an_expired_link(gated: Flow) -> None:
    started, _ = signed_in(gated)
    browser = gated.customer_http
    secret = api_session_secret(browser)
    gated.clock.advance(seconds=601)
    expired = browser.post(
        f"/customer/handoffs/{started.handoff_id}/tax",
        data={
            **steps.TAX_COMPLETE,
            "token": started.nonce,
            "csrf_token": csrf_token(secret, "tax", started.handoff_id),
        },
    )
    assert expired.status_code == 410
    assert stored_tax(gated, started.application_id) == []


def test_a_paused_application_shows_no_forms_and_refuses_them(gated: Flow) -> None:
    started, url = signed_in(gated)
    browser = gated.customer_http
    secret = api_session_secret(browser)
    asked = browser.post(
        f"/customer/applications/{started.application_id}/request-human",
        data={
            "csrf_token": csrf_token(secret, "human", started.application_id),
            "category": "OTHER",
        },
    )
    assert asked.status_code == 200
    page = browser.get(url).text
    assert "Confirmation is not available at the moment" in page
    assert "Tax information (synthetic)" not in page
    refused = browser.post(
        f"/customer/handoffs/{started.handoff_id}/tax",
        data={
            **steps.TAX_COMPLETE,
            "token": started.nonce,
            "csrf_token": csrf_token(secret, "tax", started.handoff_id),
        },
    )
    assert refused.status_code == 409 and stored_tax(gated, started.application_id) == []
    denied = [e.details for e in gated.audit(started.application_id) if e.outcome == "denied"]
    assert denied[-1]["reason"] == "state_not_awaiting_authorization"


# --- what the agent gets --------------------------------------------------------------------


def test_the_agent_has_no_tool_or_view_of_this_information(gated: Flow) -> None:
    assert {tool.value for tool in AgentToolName} == {
        "discover_provider",
        "get_official_product",
        "create_draft",
        "request_handoff",
        "get_generic_status",
        "submit_after_consent",
    }
    started, url = signed_in(gated)
    steps.complete_customer_information(gated.customer_http, url, "novice")
    status = gated.agent_http.get(
        f"/v1/applications/{started.application_id}",
        headers=gated.status_headers(started.application_id),
    )
    assert status.json()["state"] == "AWAITING_CUSTOMER"
    for word in ("tax", "knows_", "WARNING", "experience", "assessment"):
        assert word not in status.text


def test_the_consent_audit_records_outcomes_and_never_a_value(gated: Flow) -> None:
    started, url = signed_in(gated)
    browser = gated.customer_http
    steps.complete_customer_information(browser, url, "novice")
    assert steps.post_confirmation(browser, url).status_code == 200
    confirmed = next(
        e for e in gated.audit(started.application_id) if e.event_type == "handoff.confirmed"
    )
    assert confirmed.details["tax_information"] == "LOCALLY_VALIDATED"
    assert confirmed.details["tax_external_verification"] == "NOT_IMPLEMENTED"
    assert confirmed.details["appropriateness_outcome"] == "WARNING_REQUIRED"
    assert confirmed.details["appropriateness_acknowledged"] is True
    assert steps.SYNTHETIC_TAX_ID not in repr(gated.audit(started.application_id))
    assert re.search(r"SYNTH-TAX-\d{6}", whole_database(gated)) is None


def test_the_customer_pages_keep_their_security_headers(gated: Flow) -> None:
    started, url = signed_in(gated)
    refused = steps.post_tax(gated.customer_http, url, {})
    page = gated.customer_http.get(url)
    for response in (refused, page):
        assert response.headers["cache-control"] == "no-store"
        assert "form-action 'self'" in response.headers["content-security-policy"]
        assert "<script" not in response.text.lower()
