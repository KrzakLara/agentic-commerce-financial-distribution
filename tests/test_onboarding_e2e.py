"""All five onboarding steps as one journey on the real routes, and what stops that journey.

Nothing here writes workflow state to the database or calls a service past its route. A test that
touches a row does so to simulate an attacker or a failure, and says so. Identity, signing and the
depot are synthetic: no regulated identification or qualified signature takes place anywhere.
"""

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from app.core.config import Environment, Settings
from app.domain.enums import ReviewDecision
from app.integrations.catalog_fixture import (
    DEFAULT_FIXTURE,
    FixtureProviderDiscovery,
    republish,
    tampered_copy,
)
from app.integrations.jose_tools import generate_key, sha256_hex
from app.integrations.provider_callback import build_signed_confirmation
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.application_service import ApplicationService
from app.services.customer_session_service import csrf_token
from app.services.regulatory_policy import POLICY_VERSION, Service, evaluate, service_applicability
from app.services.review_service import ReviewService, resolve_dev_operator
from external_agent.client import AgentError, artifact_of
from scripts import customer_steps as steps
from scripts.communication_eval_cases import claim, proposes, read_product
from scripts.eval_communication import PlannedClient
from tests.flow import CALLBACK_PATH, PROVIDER_SECRET
from tests.trust_harness import (
    AUTHORITY,
    BANK,
    OFFER,
    PROOFING_ID,
    TrustWorld,
    sign_event,
    trust_world,
)

PROOFING_TYPE = "identity-proofing-event+jwt"
CONTRACT_REFUSED = "prerequisites_not_met"


@pytest.fixture
def world():
    with trust_world() as value:
        yield value


def _submit(agent, started) -> dict:
    return artifact_of(
        agent.call(
            "depot-onboarding",
            started["tokens"]["onboarding_token"],
            action="submit",
            application_id=started["application_id"],
        )
    )


def _agent_state(agent, started) -> str:
    return artifact_of(
        agent.call(
            "depot-onboarding",
            started["tokens"]["status_token"],
            action="status",
            application_id=started["application_id"],
        )
    )["state"]


def _refused_submit(agent, started, world: TrustWorld) -> list[str]:
    with pytest.raises(AgentError) as refused:
        _submit(agent, started)
    assert refused.value.detail["data"]["code"] == CONTRACT_REFUSED
    denied = world.audit_details("application.submit_denied", started["application_id"])
    return denied[-1]["missing"]


def _handoff_parts(link: str) -> tuple[str, str]:
    return link.split("/")[3].split("?")[0], link.split("token=")[1]


def _direct_confirm(world: TrustWorld, browser: TestClient, link: str) -> httpx.Response:
    """A valid session and CSRF token, past whatever the page offers: the server must refuse."""
    handoff_id, nonce = _handoff_parts(link)
    digest = world.sql("SELECT action_digest FROM handoffs WHERE handoff_id = :h", h=handoff_id)[0][
        0
    ]
    secret = browser.cookies.get("trusteq_customer_api", path="/v1/customer")
    return browser.post(
        f"/v1/customer/handoffs/{handoff_id}/confirm",
        json={"nonce": nonce, "action_digest": digest},
        headers={"X-CSRF-Token": csrf_token(secret, "confirm", handoff_id)},
    )


def _prepared(world: TrustWorld, subject: str = "alice"):
    agent = world.agent()
    browser = world.login(subject)
    started = world.start_application(agent, subject)
    link = world.request_link(agent, started)
    return agent, browser, started, link


def _opened_events(world: TrustWorld, application_id: str) -> list[str]:
    return [e for e in world.event_types(application_id) if e == "provider.confirmation_accepted"]


def _review_service(world: TrustWorld) -> ReviewService:
    factory = world.app.state.session_factory
    return ReviewService(
        lambda: SqlAlchemyUnitOfWork(factory),
        ApplicationService(lambda: SqlAlchemyUnitOfWork(factory), clock=world.clock),
        clock=world.clock,
    )


def _case_of(world: TrustWorld, application_id: str) -> tuple:
    return world.sql(
        "SELECT case_id, reason, status FROM review_cases WHERE application_id = :a "
        "ORDER BY created_at DESC",
        a=application_id,
    )[0]


def _changed_catalog(world: TrustWorld) -> None:
    """A legitimate re-publication: the provider changes the terms the customer approved."""
    fixture = tampered_copy(DEFAULT_FIXTURE)
    fixture.manifests["fixture-bank"]["products"][0]["conditions"] = "Changed conditions (MOCK)."
    republish(fixture, "fixture-bank")
    world.app.state.container.catalog = FixtureProviderDiscovery(fixture, clock=world.clock)


# --- B6: one unified journey ----------------------------------------------------------------------


def test_the_five_steps_are_one_journey_from_discovery_to_opened(world) -> None:
    # 1. An independent agent discovers the provider through the list it was configured with.
    assert BANK in world.safe_http().get_json(f"{AUTHORITY}/list")
    # 2. The provider's identity is verified against the synthetic trust authority.
    trust = world.verifier().verify(BANK, world.clock().timestamp())
    assert trust.trust_anchor == AUTHORITY
    assert trust.regulatory_authorization == "UNKNOWN_NO_AUTHORITATIVE_LOOKUP"
    agent = world.agent()
    assert {"product-question", "depot-onboarding"} <= agent.skills()
    assert agent.verified_profile(trust)["regulatory_information"]["status"] == "NOT_ESTABLISHED"

    # 3. The agent retrieves approved product facts, each with its source.
    fact = artifact_of(
        agent.call(
            "product-information",
            provider_id=OFFER["provider_id"],
            product_id=OFFER["product_id"],
            question="FEE",
            subject="custody",
        )
    )
    assert fact["result"] == "VERIFIED_FACT" and fact["value"] == "0.00 EUR p.a."
    assert fact["claims"][0]["source_id"] == "pli-depot-basic"

    # 4. The customer authenticates with OIDC on the bank's channel, apart from the agent.
    browser = world.login("alice")
    customer_ref = world.customer_ref("alice")
    assert world.sql("SELECT customer_ref FROM authentication_events") == [(customer_ref,)]

    # 5. The customer grants a limited delegation on the bank's page.
    pushed = world.delegate(agent, browser=browser)
    assert agent.poll_token(pushed["device_code"]) == "issued"
    assert agent.grant["customer_ref"] == customer_ref

    # 6. The agent prepares the application. The customer comes from the delegation.
    created = artifact_of(
        agent.call("depot-onboarding", agent.access_token, action="start", **OFFER)
    )
    application_id = created["application_id"]
    started = {"application_id": application_id, "tokens": created["authorization"]}
    link = world.request_link(agent, started)
    assert world.status_of(application_id) == "AWAITING_AUTHORIZATION"

    # 7. FIVE STEPS (1): identity proofing, simulated and labelled so.
    assert world.complete_proofing(browser, link).status_code == 200
    assert world.sql(
        "SELECT status, regulatory_evidence, customer_ref FROM identity_proofing_cases "
        "WHERE application_id = :a",
        a=application_id,
    ) == [("VERIFIED", 0, customer_ref)]

    # 8. FIVE STEPS (2): tax information, entered by the customer on the bank's page.
    assert steps.post_tax(browser, link, steps.TAX_COMPLETE).status_code == 303
    assert world.sql(
        "SELECT customer_id, tax_residency_country FROM tax_information WHERE application_id = :a",
        a=application_id,
    ) == [(customer_ref, "DE")]

    # 9. FIVE STEPS (3): the questionnaire, assessed by fixed rules.
    answers = steps.ASSESSMENT_PROFILES["experienced"]
    assert steps.post_answers(browser, link, answers).status_code == 303
    assert world.sql(
        "SELECT outcome, rule_set_version FROM appropriateness_assessments "
        "WHERE application_id = :a",
        a=application_id,
    ) == [("ASSESSMENT_OK", "synthetic-2026.11")]

    # 10. The bank's policy evaluates what applies when the customer confirms the exact action.
    assert steps.post_confirmation(browser, link).status_code == 200
    assert world.status_of(application_id) == "CONTRACT_PENDING"
    decision = json.loads(
        world.sql(
            "SELECT result FROM policy_decisions WHERE application_id = :a AND stage = 'consent'",
            a=application_id,
        )[0][0]
    )
    assert decision["allowed"] is True and decision["policy_version"] == POLICY_VERSION
    assert decision["service_applicability"]["ACCOUNT_OPENING"]["appropriateness"] == (
        "NOT_TRIGGERED"
    )

    # 11. The agent provides verified product information. The bank renders it and audits it.
    answer = artifact_of(
        agent.call(
            "product-question",
            started["tokens"]["status_token"],
            question="What are the fees?",
            product_ids=[OFFER["product_id"]],
            application_id=application_id,
        )
    )
    assert answer["outcome"] == "VERIFIED_ANSWER"
    assert answer["generated_by"] == {
        "proposer": "deterministic-offline",
        "model": None,
        "model_prose_released": False,
    }
    released = world.audit_details("communication.answer_released", application_id)[-1]
    assert released["answer_digest"] == answer["answer_digest"]
    assert released["sources"][0]["source_id"] == "pli-depot-basic"

    # 12. The customer reviews the exact application and contract.
    form = world.contract_form(browser, application_id)
    package_hash = form["package_hash"]
    page = browser.get(f"/customer/applications/{application_id}/contract").text
    assert package_hash in page and OFFER["product_id"] in page
    document = json.loads(world.sql("SELECT canonical_json FROM contract_packages")[0][0])
    assert document["customer_ref"] == customer_ref
    assert document["service_context"] == "ACCOUNT_OPENING"
    assert [d["document_id"] for d in document["documents"]] == [
        "kid-depot-basic",
        "pli-depot-basic",
    ]

    # 13. The customer explicitly authorizes. 14. The test signer processes the package.
    reference = world.authorize_signing(browser, application_id)
    assert world.signer_outcome(reference, "SIGNED").status_code == 200
    assert world.sql("SELECT state, level, legal_effect, package_hash FROM signature_sessions") == [
        ("SIGNED", "TEST_NO_LEGAL_EFFECT", 0, package_hash)
    ]

    # 15. The bank's workflow hands over to the provider, which confirms with a signature.
    assert _submit(agent, started)["state"] == "IN_PROGRESS"
    assert world.status_of(application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert world.provider_opens(application_id).status_code == 200

    # 16. The synthetic depot is OPENED.
    assert world.status_of(application_id) == "OPENED"
    assert _agent_state(agent, started) == "OPENED"

    # 17. The audit trail holds what a reader needs to trace it.
    rows = world.sql(
        "SELECT correlation_id FROM audit_events WHERE application_id = :a", a=application_id
    )
    assert rows and all(row[0] for row in rows)
    confirmed = world.audit_details("handoff.confirmed", application_id)[0]
    assert confirmed["identity_proofing_status"] == "VERIFIED"
    assert confirmed["identity_proofing_regulatory_evidence"] is False
    assert confirmed["authentication"]["issuer"].startswith("https://idp.test")
    assert confirmed["delegation_id"] == pushed["delegation_id"]
    assert confirmed["tax_information"] == "LOCALLY_VALIDATED"
    assert confirmed["appropriateness_rule_set"] == "synthetic-2026.11"
    assert confirmed["policy_version"] == POLICY_VERSION
    for event in ("application.submitted", "provider.confirmation_accepted"):
        evidence = world.audit_details(event, application_id)[0]
        assert evidence["contract_package_hash"] == package_hash
        assert evidence["policy_version"] == POLICY_VERSION
        assert evidence["delegation_id"] == pushed["delegation_id"]
        assert evidence["identity_proofing_status"] == "VERIFIED"
        assert evidence["consent_receipt_id"].startswith("cns-")
        assert evidence["product_version"] == "2026.11"
        assert evidence["terms_digest"] == document["terms_digest"]
        assert {d["document_id"] for d in evidence["source_documents"]} == {
            "kid-depot-basic",
            "pli-depot-basic",
        }
        assert evidence["signature_level"] == "TEST_NO_LEGAL_EFFECT"
        assert evidence["signature_legal_effect"] is False


def test_the_journey_keeps_the_synthetic_labels_and_no_sensitive_value(world) -> None:
    agent = world.agent()
    state = world.to_contract_pending(agent)
    world.sign(state["browser"], state["application_id"])
    _submit(agent, state)
    world.provider_opens(state["application_id"])
    tables = world.sql("SELECT name FROM sqlite_master WHERE type = 'table'")
    everything = repr(
        [world.sql(f"SELECT * FROM {name}") for (name,) in tables]  # noqa: S608 - schema names
    )
    assert steps.SYNTHETIC_TAX_ID not in everything
    assert re.search(r"SYNTH-TAX-\d{6}", everything) is None
    assert "QES" not in repr(world.sql("SELECT level, evidence FROM signature_sessions"))
    assert world.sql("SELECT DISTINCT regulatory_evidence FROM identity_proofing_cases") == [(0,)]


# --- B7 and B1: identity ------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["REJECTED", "MANUAL_REVIEW", "EXPIRED"])
def test_an_invalid_identity_cannot_continue_at_any_later_gate(world, outcome) -> None:
    agent, browser, started, link = _prepared(world)
    application_id = started["application_id"]
    assert world.complete_proofing(browser, link, outcome).status_code == 200
    assert world.status_of(application_id) == "MANUAL_REVIEW"
    assert _direct_confirm(world, browser, link).status_code == 409
    assert _refused_submit(agent, started, world)
    assert world.provider_opens(application_id).status_code == 403
    assert world.status_of(application_id) == "MANUAL_REVIEW"
    assert _agent_state(agent, started) == "ACTION_REQUIRED"
    assert world.sql("SELECT COUNT(*) FROM consent_receipts") == [(0,)]


def test_no_information_is_enough_without_a_verified_identity(world) -> None:
    _, browser, started, link = _prepared(world)
    steps.complete_customer_information(browser, link)
    assert steps.page_form(browser.get(link).text, "/confirm") is None
    assert _direct_confirm(world, browser, link).status_code == 409
    denied = world.audit_details("handoff.rejected", started["application_id"])[-1]
    assert denied["missing"] == ["identity_proofing"]


def test_a_forged_identity_result_cannot_verify_anybody(world) -> None:
    _, browser, started, link = _prepared(world)
    form = steps.page_form(browser.get(link).text, "/identity-proofing")
    browser.post(form["_action"], data={"token": form["token"], "csrf_token": form["csrf_token"]})
    case = world.sql("SELECT case_id, provider_ref, customer_ref FROM identity_proofing_cases")[0]
    forged = sign_event(
        generate_key("proofing-1"),  # the right key id, the wrong key
        PROOFING_TYPE,
        {
            "iss": PROOFING_ID,
            "case_id": case[0],
            "provider_ref": case[1],
            "application_id": started["application_id"],
            "customer_ref": case[2],
            "status": "VERIFIED",
            "regulatory_evidence": False,
        },
        world.clock,
    )
    answer = world.bank.post("/internal/identity-proofing/callbacks", content=forged)
    assert answer.status_code == 403
    assert world.sql("SELECT status FROM identity_proofing_cases") == [("PENDING",)]
    assert _direct_confirm(world, browser, link).status_code == 409


def test_a_replayed_identity_result_changes_nothing(world) -> None:
    _, browser, started, link = _prepared(world)
    form = steps.page_form(browser.get(link).text, "/identity-proofing")
    browser.post(form["_action"], data={"token": form["token"], "csrf_token": form["csrf_token"]})
    case = world.sql("SELECT case_id, provider_ref, customer_ref FROM identity_proofing_cases")[0]
    event = sign_event(
        world.proofing_key,
        PROOFING_TYPE,
        {
            "iss": PROOFING_ID,
            "case_id": case[0],
            "provider_ref": case[1],
            "application_id": started["application_id"],
            "customer_ref": case[2],
            "status": "VERIFIED",
            "method": "SYNTHETIC_TEST",
            "assurance": "synthetic_no_assurance",
            "regulatory_evidence": False,
        },
        world.clock,
    )
    url = "/internal/identity-proofing/callbacks"
    assert world.bank.post(url, content=event).json() == {"result": "accepted"}
    assert world.bank.post(url, content=event).json() == {"result": "duplicate_ignored"}
    assert world.event_types(started["application_id"]).count("identity_proofing.result") == 1


def test_another_customers_identity_result_does_not_count(world) -> None:
    """A row moved to another customer, as a bug or an attacker with database access might do."""
    _, browser, started, link = _prepared(world)
    assert world.complete_proofing(browser, link).status_code == 200
    steps.complete_customer_information(browser, link)
    with world.app.state.engine.begin() as connection:
        connection.execute(text("UPDATE identity_proofing_cases SET customer_ref = 'cid-other'"))
    assert _direct_confirm(world, browser, link).status_code == 409
    denied = world.audit_details("handoff.rejected", started["application_id"])[-1]
    assert denied["missing"] == ["identity_proofing"]


def test_a_person_can_let_the_customer_retry_but_cannot_replace_the_identity_check(world) -> None:
    _, browser, started, link = _prepared(world)
    application_id = started["application_id"]
    world.complete_proofing(browser, link, "REJECTED")
    case_id = _case_of(world, application_id)[0]
    _review_service(world).decide(
        resolve_dev_operator("op-reviewer-1"), case_id, ReviewDecision.RESUME, "corr-op"
    )
    assert world.status_of(application_id) == "AWAITING_AUTHORIZATION"
    steps.complete_customer_information(browser, link)
    assert _direct_confirm(world, browser, link).status_code == 409  # a person is not a result
    assert world.complete_proofing(browser, link, "VERIFIED").status_code == 200
    assert steps.post_confirmation(browser, link).status_code == 200
    assert world.status_of(application_id) == "CONTRACT_PENDING"


# --- B7 and B2: tax information -----------------------------------------------------------------


def test_another_customer_cannot_change_the_tax_information_on_either_route(world) -> None:
    _, browser, started, link = _prepared(world)
    application_id = started["application_id"]
    steps.post_tax(browser, link, steps.TAX_COMPLETE)
    handoff_id, nonce = _handoff_parts(link)
    bob = world.login("bob")
    page_secret = bob.cookies.get("trusteq_customer_session", path="/customer")
    on_the_page = bob.post(
        f"/customer/handoffs/{handoff_id}/tax",
        data={
            **steps.TAX_COMPLETE,
            "tax_residency_country": "FR",
            "token": nonce,
            "csrf_token": csrf_token(page_secret, "tax", handoff_id),
        },
    )
    assert on_the_page.status_code == 403
    api_secret = bob.cookies.get("trusteq_customer_api", path="/v1/customer")
    over_the_api = bob.post(
        f"/v1/customer/handoffs/{handoff_id}/tax-information",
        json={
            "nonce": nonce,
            "tax_residency_country": "FR",
            "synthetic_tax_id": steps.SYNTHETIC_TAX_ID,
            "us_person_declared": False,
        },
        headers={"X-CSRF-Token": csrf_token(api_secret, "tax", handoff_id)},
    )
    assert over_the_api.status_code == 403
    assert world.sql(
        "SELECT tax_residency_country FROM tax_information WHERE application_id = :a",
        a=application_id,
    ) == [("DE",)]


def test_missing_tax_information_blocks_the_confirmation_and_the_submission(world) -> None:
    agent, browser, started, link = _prepared(world)
    world.complete_proofing(browser, link)
    steps.post_answers(browser, link, steps.ASSESSMENT_PROFILES["experienced"])
    assert _direct_confirm(world, browser, link).status_code == 409
    denied = world.audit_details("handoff.rejected", started["application_id"])[-1]
    assert denied["missing"] == ["tax_information"]
    assert _refused_submit(agent, started, world)
    assert world.status_of(started["application_id"]) == "AWAITING_AUTHORIZATION"


@pytest.mark.parametrize(
    "change",
    [
        {"synthetic_tax_id": steps.REAL_LOOKING_TAX_ID},
        {"synthetic_tax_id": ""},
        {"tax_residency_country": "germany"},
        {"us_person_declared": "perhaps"},
    ],
)
def test_invalid_tax_information_is_refused_and_keeps_the_gate_closed(world, change) -> None:
    _, browser, started, link = _prepared(world)
    world.complete_proofing(browser, link)
    steps.post_answers(browser, link, steps.ASSESSMENT_PROFILES["experienced"])
    response = steps.post_tax(browser, link, {**steps.TAX_COMPLETE, **change})
    assert response.status_code == 422
    assert world.sql("SELECT COUNT(*) FROM tax_information") == [(0,)]
    assert _direct_confirm(world, browser, link).status_code == 409


def test_a_replayed_tax_form_cannot_change_anything_after_the_confirmation(world) -> None:
    agent = world.agent()
    state = world.to_contract_pending(agent)
    handoff_id, nonce = _handoff_parts(state["link"])
    secret = state["browser"].cookies.get("trusteq_customer_session", path="/customer")
    replay = state["browser"].post(
        f"/customer/handoffs/{handoff_id}/tax",
        data={
            **steps.TAX_COMPLETE,
            "tax_residency_country": "FR",
            "token": nonce,
            "csrf_token": csrf_token(secret, "tax", handoff_id),
        },
    )
    assert replay.status_code == 409
    assert world.sql("SELECT tax_residency_country FROM tax_information") == [("DE",)]


def test_the_tax_step_has_no_agent_route_and_its_data_reaches_no_agent(world) -> None:
    agent, browser, started, link = _prepared(world)
    handoff_id, nonce = _handoff_parts(link)
    token = started["tokens"]["onboarding_token"]
    refused = world.bank.post(
        f"/v1/customer/handoffs/{handoff_id}/tax-information",
        json={"nonce": nonce, "synthetic_tax_id": steps.SYNTHETIC_TAX_ID},
        headers={"Authorization": f"DPoP {token}"},
    )
    assert refused.status_code == 401
    world.complete_proofing(browser, link)
    steps.complete_customer_information(browser, link)
    status = artifact_of(
        agent.call(
            "depot-onboarding",
            started["tokens"]["status_token"],
            action="status",
            application_id=started["application_id"],
        )
    )
    assert set(status) == {"application_id", "state"}


def test_a_tax_identifier_in_a_question_is_not_processed_stored_or_repeated(world) -> None:
    agent, _, started, _ = _prepared(world)
    answer = artifact_of(
        agent.call(
            "product-question",
            started["tokens"]["status_token"],
            question=f"My tax ID is {steps.SYNTHETIC_TAX_ID}. Is that right?",
            application_id=started["application_id"],
        )
    )
    assert answer["outcome"] == "UNSUPPORTED_REQUEST"
    assert answer["reason_code"] == "SENSITIVE_DATA_IN_QUESTION"
    assert steps.SYNTHETIC_TAX_ID not in json.dumps(answer)
    withheld = world.audit_details("communication.answer_withheld", started["application_id"])[-1]
    assert steps.SYNTHETIC_TAX_ID not in json.dumps(withheld)
    assert len(withheld["question_digest"]) == 64
    assert steps.SYNTHETIC_TAX_ID not in repr(world.sql("SELECT artifact FROM a2a_tasks"))


# --- B7 and B3: knowledge, experience, and which service is being asked for ---------------------


@pytest.mark.parametrize(
    ("profile", "outcome"),
    [("novice", "WARNING_REQUIRED"), ("incomplete", "INSUFFICIENT_INFORMATION")],
)
def test_a_warning_needs_the_customers_decision_and_does_not_reject_the_account(
    world, profile, outcome
) -> None:
    agent, browser, started, link = _prepared(world)
    application_id = started["application_id"]
    world.complete_proofing(browser, link)
    steps.post_tax(browser, link, steps.TAX_COMPLETE)
    steps.post_answers(browser, link, steps.ASSESSMENT_PROFILES[profile])
    page = browser.get(link).text
    assert "not investment advice" in page.lower()
    assert steps.page_form(page, "/confirm") is None
    assert _direct_confirm(world, browser, link).status_code == 409
    denied = world.audit_details("handoff.rejected", application_id)[-1]
    assert denied["missing"] == ["appropriateness_acknowledgement"]
    assert steps.post_acknowledgement(browser, link).status_code == 303
    assert steps.post_confirmation(browser, link).status_code == 200
    confirmed = world.audit_details("handoff.confirmed", application_id)[0]
    assert confirmed["appropriateness_outcome"] == outcome
    assert confirmed["appropriateness_acknowledged"] is True
    world.sign(browser, application_id)
    _submit(agent, started)
    assert world.provider_opens(application_id).status_code == 200
    assert world.status_of(application_id) == "OPENED"


def test_the_assessment_records_which_service_it_is_for(world) -> None:
    _, browser, started, link = _prepared(world)
    steps.post_answers(browser, link, steps.ASSESSMENT_PROFILES["novice"])
    recorded = world.audit_details(
        "customer_information.assessment_recorded", started["application_id"]
    )[-1]
    assert recorded["assessed_for_service"] == "ORDER_NON_ADVISED"
    assert recorded["account_opening_appropriateness"] == "NOT_TRIGGERED"
    assert recorded["rule_set_version"] == "synthetic-2026.11"


def test_each_service_has_its_own_decision() -> None:
    decisions = service_applicability({"assessment_outcome": "WARNING_REQUIRED"})
    opening = decisions[Service.ACCOUNT_OPENING.value]
    assert opening["offered"] is True and opening["appropriateness"] == "NOT_TRIGGERED"
    order = decisions[Service.ORDER_NON_ADVISED.value]
    assert order["offered"] is False and order["appropriateness"] == "REQUIRED"
    assert order["appropriateness_gate"] == "WARNING"
    execution_only = decisions[Service.ORDER_EXECUTION_ONLY.value]
    assert execution_only["appropriateness"] == "UNRESOLVED"
    assert execution_only["appropriateness_gate"] == "HUMAN"
    assert execution_only["legal_confirmation_required"] is True
    for service in (Service.INVESTMENT_ADVICE, Service.PORTFOLIO_MANAGEMENT):
        assert decisions[service.value]["suitability"] == "NOT_OFFERED"
        assert decisions[service.value]["offered"] is False
    assert evaluate(Service.ACCOUNT_OPENING, {"assessment_outcome": "WARNING_REQUIRED"})[
        "blocking"
    ] == ["AML-IDENTIFICATION", "SIGNATURE-LEVEL", "TAX-INFORMATION"]


@pytest.mark.parametrize(
    "service",
    [
        "INVESTMENT_ADVICE",
        "PORTFOLIO_MANAGEMENT",
        "ORDER_NON_ADVISED",
        "ORDER_EXECUTION_ONLY",
        "SOMETHING_ELSE",
    ],
)
def test_a_request_for_another_service_is_refused_not_turned_into_an_account(world, service):
    agent = world.agent()
    pushed = world.delegate(agent)
    assert agent.poll_token(pushed["device_code"]) == "issued"
    with pytest.raises(AgentError) as refused:
        agent.call("depot-onboarding", agent.access_token, action="start", service=service, **OFFER)
    assert refused.value.detail["data"]["code"] == "service_not_offered"
    assert world.sql("SELECT COUNT(*) FROM applications") == [(0,)]
    audited = world.audit_details("application.service_refused")[-1]
    assert audited["requested_service"] in {service, "UNKNOWN_SERVICE"}
    # The grant is untouched: the account opening itself is still possible.
    opened = artifact_of(
        agent.call(
            "depot-onboarding",
            agent.access_token,
            action="start",
            service="ACCOUNT_OPENING",
            **OFFER,
        )
    )
    assert opened["state"] == "DRAFT"


def test_no_agent_or_model_can_supply_or_override_the_assessment(world) -> None:
    agent, browser, started, link = _prepared(world)
    handoff_id, nonce = _handoff_parts(link)
    refused = world.bank.post(
        f"/v1/customer/handoffs/{handoff_id}/appropriateness",
        json={"nonce": nonce, "knows_capital_loss": True},
        headers={"Authorization": f"DPoP {started['tokens']['onboarding_token']}"},
    )
    assert refused.status_code == 401
    with pytest.raises(AgentError) as unknown:
        agent.call(
            "depot-onboarding",
            started["tokens"]["onboarding_token"],
            action="record_assessment",
            application_id=started["application_id"],
            outcome="ASSESSMENT_OK",
        )
    assert unknown.value.detail["code"] == -32602
    assert world.sql("SELECT COUNT(*) FROM appropriateness_assessments") == [(0,)]


# --- B7 and B4: customer authorization and the contract -----------------------------------------


def test_an_agent_cannot_reach_the_customers_authorization_by_any_route(world) -> None:
    agent = world.agent()
    state = world.to_contract_pending(agent)
    application_id = state["application_id"]
    form = world.contract_form(state["browser"], application_id)
    token = state["tokens"]["onboarding_token"]
    for path, kwargs in (
        (form["_action"], {"data": {"package_hash": form["package_hash"]}}),
        (
            f"/customer/applications/{application_id}/contract/cancel",
            {"data": {"csrf_token": "x" * 43}},
        ),
    ):
        answer = world.bank.post(path, headers={"Authorization": f"DPoP {token}"}, **kwargs)
        assert answer.status_code == 401
    with pytest.raises(AgentError) as unknown:
        agent.call(
            "depot-onboarding",
            token,
            action="authorize_signing",
            application_id=application_id,
            package_hash=form["package_hash"],
        )
    assert unknown.value.detail["code"] == -32602
    assert world.sql("SELECT COUNT(*) FROM signature_sessions") == [(0,)]
    assert "contract_signature" in _refused_submit(agent, state, world)


def test_a_change_of_terms_after_the_signature_invalidates_it_and_goes_to_a_person(world) -> None:
    agent = world.agent()
    state = world.to_contract_pending(agent)
    application_id = state["application_id"]
    world.sign(state["browser"], application_id)
    _changed_catalog(world)
    missing = _refused_submit(agent, state, world)
    assert missing == ["approved_action_current"]
    assert world.status_of(application_id) == "MANUAL_REVIEW"
    assert _case_of(world, application_id)[1:] == ("CHECK_FAILED", "OPEN")
    opened = world.audit_details("review.case_opened", application_id)[-1]
    assert opened["contract_problems"] == ["approved_action_current"]
    assert world.provider_opens(application_id).status_code == 403
    assert _agent_state(agent, state) == "ACTION_REQUIRED"


def test_a_person_can_only_reject_a_contract_that_no_longer_holds(world) -> None:
    agent = world.agent()
    state = world.to_contract_pending(agent)
    application_id = state["application_id"]
    world.sign(state["browser"], application_id)
    _changed_catalog(world)
    _refused_submit(agent, state, world)
    reviews = _review_service(world)
    case_id = _case_of(world, application_id)[0]
    with pytest.raises(Exception):  # noqa: B017, PT011 - the service refuses this decision
        reviews.decide(
            resolve_dev_operator("op-supervisor-1"), case_id, ReviewDecision.RESUME, "corr-op"
        )
    assert world.status_of(application_id) == "MANUAL_REVIEW"
    reviews.decide(
        resolve_dev_operator("op-supervisor-1"), case_id, ReviewDecision.REJECT, "corr-op"
    )
    assert world.status_of(application_id) == "REJECTED"
    assert world.provider_opens(application_id).status_code == 403


def test_terms_changed_before_the_package_exists_stop_it_from_being_built(world) -> None:
    agent = world.agent()
    state = world.to_contract_pending(agent)
    application_id = state["application_id"]
    _changed_catalog(world)
    page = state["browser"].get(f"/customer/applications/{application_id}/contract")
    assert page.status_code == 409
    assert world.sql("SELECT COUNT(*) FROM contract_packages") == [(0,)]
    assert world.status_of(application_id) == "MANUAL_REVIEW"


def test_a_package_rewritten_with_a_matching_hash_is_still_refused(world) -> None:
    """An attacker with database access rewrites the package, its hash and the signature's copy."""
    agent = world.agent()
    state = world.to_contract_pending(agent)
    application_id = state["application_id"]
    world.sign(state["browser"], application_id)
    original = json.loads(world.sql("SELECT canonical_json FROM contract_packages")[0][0])
    rewritten = json.dumps(
        {**original, "product_id": "depot-premium"}, sort_keys=True, separators=(",", ":")
    )
    digest = sha256_hex(rewritten.encode("utf-8"))
    with world.app.state.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_contract_packages_no_update"))
        connection.execute(
            text("UPDATE contract_packages SET canonical_json = :j, package_hash = :h"),
            {"j": rewritten, "h": digest},
        )
        connection.execute(text("UPDATE signature_sessions SET package_hash = :h"), {"h": digest})
    assert _refused_submit(agent, state, world) == ["contract_package_binding"]
    assert world.status_of(application_id) == "MANUAL_REVIEW"


def test_authorization_is_one_time_while_a_signature_is_running(world) -> None:
    agent = world.agent()
    state = world.to_contract_pending(agent)
    application_id = state["application_id"]
    form = world.contract_form(state["browser"], application_id)
    world.authorize_signing(state["browser"], application_id)
    again = state["browser"].post(
        form["_action"],
        data={"csrf_token": form["csrf_token"], "package_hash": form["package_hash"]},
    )
    assert again.status_code == 409
    assert world.sql("SELECT COUNT(*) FROM signature_sessions") == [(1,)]


def test_another_customer_cannot_authorize_or_cancel_the_signature(world) -> None:
    agent = world.agent()
    state = world.to_contract_pending(agent)
    application_id = state["application_id"]
    form = world.contract_form(state["browser"], application_id)
    bob = world.login("bob")
    secret = bob.cookies.get("trusteq_customer_session", path="/customer")
    answer = bob.post(
        form["_action"],
        data={
            "csrf_token": csrf_token(secret, "contract_authorize", application_id),
            "package_hash": form["package_hash"],
        },
    )
    assert answer.status_code == 404
    assert world.sql("SELECT COUNT(*) FROM signature_sessions") == [(0,)]


@pytest.mark.parametrize("outcome", ["DECLINED", "FAILED", "EXPIRED"])
def test_a_failed_signature_cannot_open_the_depot(world, outcome) -> None:
    agent = world.agent()
    state = world.to_contract_pending(agent)
    application_id = state["application_id"]
    world.sign(state["browser"], application_id, outcome)
    assert world.status_of(application_id) == "MANUAL_REVIEW"
    assert _refused_submit(agent, state, world)
    assert world.provider_opens(application_id).status_code == 403
    assert _case_of(world, application_id)[1] == "SIGNATURE_NOT_COMPLETED"


# --- B7 and B5: the depot -----------------------------------------------------------------------


def _at_submitted(world: TrustWorld):
    agent = world.agent()
    state = world.to_contract_pending(agent)
    world.sign(state["browser"], state["application_id"])
    assert _submit(agent, state)["state"] == "IN_PROGRESS"
    return agent, state


def _callback(world: TrustWorld, **overrides):
    application_id = overrides.pop("application_id")
    secret = overrides.pop("secret", PROVIDER_SECRET)
    signed = build_signed_confirmation(
        secret.encode(),
        application_id=application_id,
        provider_ref=overrides.pop("provider_ref", f"MOCK-DEPOT-{application_id}"),
        now=overrides.pop("now", world.clock().timestamp()),
        **overrides,
    )
    return TestClient(world.app, base_url=BANK).post(
        CALLBACK_PATH, content=signed.body, headers=signed.headers
    )


def test_a_forged_provider_callback_cannot_open_the_depot(world) -> None:
    _, state = _at_submitted(world)
    application_id = state["application_id"]
    wrong_secret = "another-mock-secret"  # noqa: S105 - a deliberately wrong test value
    assert _callback(world, application_id=application_id, secret=wrong_secret).status_code == 401
    old = world.clock().timestamp() - 3600
    assert _callback(world, application_id=application_id, now=old).status_code == 401
    assert world.status_of(application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"


def test_a_correctly_signed_but_wrong_callback_cannot_open_the_depot(world) -> None:
    _, state = _at_submitted(world)
    application_id = state["application_id"]
    assert _callback(world, application_id="app-unknown").status_code == 404
    assert (
        _callback(world, application_id=application_id, provider_ref="MOCK-DEPOT-other").status_code
        == 403
    )
    assert world.status_of(application_id) == "SUBMITTED_TO_DEPOT_PROVIDER"
    assert not _opened_events(world, application_id)


def test_a_duplicate_provider_callback_has_no_second_effect(world) -> None:
    _, state = _at_submitted(world)
    application_id = state["application_id"]
    first = _callback(world, application_id=application_id, confirmation_id="conf-1")
    second = _callback(world, application_id=application_id, confirmation_id="conf-1")
    assert first.json() == {"result": "accepted"}
    assert second.json() == {"result": "duplicate_ignored"}
    assert len(_opened_events(world, application_id)) == 1


def test_a_second_confirmation_id_cannot_reopen_it(world) -> None:
    _, state = _at_submitted(world)
    application_id = state["application_id"]
    assert _callback(world, application_id=application_id, confirmation_id="c-1").status_code == 200
    assert _callback(world, application_id=application_id, confirmation_id="c-2").status_code == 403
    assert len(_opened_events(world, application_id)) == 1


@pytest.mark.parametrize(
    "scenario",
    [
        "provider_unavailable",
        "provider_timeout",
        "malformed_response",
        "manipulated_opened",
        "v2_retired_no_fallback",
    ],
)
def test_provider_errors_fail_safely_and_create_no_opened(scenario) -> None:
    with trust_world(mock_depot_scenario=scenario) as world:
        agent = world.agent()
        state = world.to_contract_pending(agent)
        application_id = state["application_id"]
        world.sign(state["browser"], application_id)
        with pytest.raises(AgentError):
            _submit(agent, state)
        assert world.status_of(application_id) == "CONTRACT_PENDING"
        assert world.provider_opens(application_id).status_code == 403
        denied = world.audit_details("application.submit_denied", application_id)[-1]
        assert "provider_accepted" in denied["missing"]
        assert world.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]


def test_a_proofing_outage_changes_nothing(world) -> None:
    """The signing outage has its own test in tests/test_trust_contract_signing.py."""
    _, browser, started, link = _prepared(world)
    application_id = started["application_id"]
    container = world.app.state.container
    working = container.outbound_http

    def broken() -> httpx.Client:
        def fail(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("synthetic outage")

        return httpx.Client(transport=httpx.MockTransport(fail))

    form = steps.page_form(browser.get(link).text, "/identity-proofing")
    container.outbound_http = broken
    answer = browser.post(
        form["_action"], data={"token": form["token"], "csrf_token": form["csrf_token"]}
    )
    container.outbound_http = working
    assert answer.status_code == 502
    assert world.status_of(application_id) == "AWAITING_AUTHORIZATION"
    assert world.sql("SELECT status FROM identity_proofing_cases") == [("PENDING",)]
    assert _direct_confirm(world, browser, link).status_code == 409


def test_a_revoked_delegation_can_do_nothing_more_and_public_reading_stays(world) -> None:
    agent, browser, started, _ = _prepared(world)
    application_id = started["application_id"]
    secret = browser.cookies.get("trusteq_customer_session", path="/customer")
    token = csrf_token(secret, "delegation_revoke", started["delegation_id"])
    revoked = browser.post(
        f"/customer/delegations/{started['delegation_id']}/revoke", data={"csrf_token": token}
    )
    assert revoked.status_code == 200
    for name, kwargs in (
        ("depot-onboarding", {"action": "status", "application_id": application_id}),
        ("depot-onboarding", {"action": "submit", "application_id": application_id}),
        (
            "depot-onboarding",
            {"action": "request_customer_confirmation", "application_id": application_id},
        ),
        ("product-question", {"question": "Fees?", "application_id": application_id}),
    ):
        with pytest.raises(AgentError) as refused:
            agent.call(name, started["tokens"]["onboarding_token"], **kwargs)
        assert refused.value.detail["code"] == -32041
    assert world.status_of(application_id) == "AWAITING_AUTHORIZATION"
    public = artifact_of(agent.call("product-question", question="What are the fees?"))
    assert public["outcome"] == "VERIFIED_ANSWER"  # reading approved facts needs no delegation


def test_cases_for_a_person_stay_paused_until_a_person_acts(world) -> None:
    agent = world.agent()
    state = world.to_contract_pending(agent)
    application_id = state["application_id"]
    world.sign(state["browser"], application_id)
    secret = state["browser"].cookies.get("trusteq_customer_session", path="/customer")
    asked = state["browser"].post(
        f"/customer/applications/{application_id}/request-human",
        data={"csrf_token": csrf_token(secret, "human", application_id), "category": "OTHER"},
    )
    assert asked.status_code == 200
    assert world.status_of(application_id) == "MANUAL_REVIEW"
    assert _refused_submit(agent, state, world)
    assert world.provider_opens(application_id).status_code == 403
    assert world.status_of(application_id) == "MANUAL_REVIEW"
    case_id = _case_of(world, application_id)[0]
    _review_service(world).decide(
        resolve_dev_operator("op-reviewer-1"), case_id, ReviewDecision.RESUME, "corr-op"
    )
    assert world.status_of(application_id) == "CONTRACT_PENDING"
    assert _submit(agent, state)["state"] == "IN_PROGRESS"
    assert world.provider_opens(application_id).status_code == 200
    assert world.status_of(application_id) == "OPENED"


def test_partner_attribution_is_not_overwritten_at_any_later_step(world) -> None:
    """Each decision stays in the record. The partner comes from the register, never the agent."""
    agent = world.agent()
    offer = {**OFFER, "channel_id": "partner-1"}
    browser = world.login("alice")
    pushed = world.delegate(agent, offer=offer, browser=browser)
    assert agent.poll_token(pushed["device_code"]) == "issued"
    created = artifact_of(
        agent.call("depot-onboarding", agent.access_token, action="start", **offer)
    )
    started = {"application_id": created["application_id"], "tokens": created["authorization"]}
    link = world.request_link(agent, started)
    world.complete_proofing(browser, link)
    steps.complete_customer_information(browser, link)
    steps.post_confirmation(browser, link)
    application_id = started["application_id"]
    world.sign(browser, application_id)
    _submit(agent, started)
    world.provider_opens(application_id)
    assert world.status_of(application_id) == "OPENED"
    assert world.sql(
        "SELECT partner_id FROM channel_attributions WHERE application_id = :a", a=application_id
    ) == [("sampletown-coop-bank",)]
    decisions = world.sql(
        "SELECT sequence, partner_id FROM attribution_decisions WHERE application_id = :a "
        "ORDER BY sequence",
        a=application_id,
    )
    assert decisions[0] == (1, "sampletown-coop-bank")
    package = json.loads(world.sql("SELECT canonical_json FROM contract_packages")[0][0])
    assert package["partner_id"] == "sampletown-coop-bank"


# --- unsupported claims, with the configured model path, inside the journey ---------------------


def _llm_world(**extra):
    return trust_world(
        ai_execution_mode="llm",
        enable_llm=True,
        llm_provider="anthropic",
        llm_api_key=SecretStr("not-a-real-key"),
        **extra,
    )


def test_an_unsupported_claim_is_not_released_and_the_refusal_is_audited() -> None:
    with _llm_world() as world:
        agent, _, started, _ = _prepared(world)
        application_id = started["application_id"]
        world.app.state.container.answer_model_client = PlannedClient(
            (read_product(), proposes(claims=[claim("annual", value="25.00 EUR p.a.")]))
        )
        answer = artifact_of(
            agent.call(
                "product-question",
                started["tokens"]["status_token"],
                question="What is the annual fee?",
                application_id=application_id,
            )
        )
        assert answer["outcome"] == "INSUFFICIENT_VERIFIED_EVIDENCE"
        assert "25.00" not in json.dumps(answer)
        withheld = world.audit_details("communication.answer_withheld", application_id)[-1]
        assert withheld["rejected_reasons"] == ["UNKNOWN_CLAIM"]
        assert withheld["proposer"] == "anthropic"
        assert "25.00" not in json.dumps(withheld)


def test_in_llm_mode_a_question_needs_a_credential_so_nobody_spends_anonymously() -> None:
    with _llm_world() as world:
        client = PlannedClient((proposes(claims=[claim("order", value="4.90 EUR")]),))
        world.app.state.container.answer_model_client = client
        agent = world.agent()
        task = agent.call("product-question", question="What does an order cost?")
        assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
        assert artifact_of(task)["result"] == "DELEGATION_REQUIRED"
        assert client.messages.requests == []
        anonymous = world.bank.post("/v1/agent/questions", json={"question": "Fees?"})
        assert anonymous.status_code == 401
        assert client.messages.requests == []


def test_a_question_cannot_be_tied_to_another_customers_application() -> None:
    with _llm_world() as world:
        client = PlannedClient((proposes(claims=[claim("order", value="4.90 EUR")]),))
        world.app.state.container.answer_model_client = client
        _, _, started_a, _ = _prepared(world, "alice")
        agent_b, _, started_b, _ = _prepared(world, "bob")
        with pytest.raises(AgentError) as refused:
            agent_b.call(
                "product-question",
                started_b["tokens"]["status_token"],
                question="Fees?",
                application_id=started_a["application_id"],
            )
        assert refused.value.detail["data"]["code"] == "not_found"
        assert client.messages.requests == []  # refused before any model run


# --- configuration ------------------------------------------------------------------------------


def test_the_full_trust_profile_cannot_run_without_the_customers_information() -> None:
    with pytest.raises(ValueError, match="customer_information_required"):
        Settings(
            _env_file=None,
            environment=Environment.TEST,
            full_trust_profile=True,
            customer_information_required=False,
        )


def test_the_workflow_keeps_its_gates_even_if_that_setting_is_bypassed(world) -> None:
    """A copied settings object skips validators. The workflow's own gate still holds."""
    _, browser, started, link = _prepared(world)
    world.app.state.settings = world.app.state.settings.model_copy(
        update={"customer_information_required": False}
    )
    assert _direct_confirm(world, browser, link).status_code == 409
    denied = world.audit_details("handoff.rejected", started["application_id"])[-1]
    assert set(denied["missing"]) == {
        "tax_information",
        "appropriateness_assessment",
        "identity_proofing",
    }
