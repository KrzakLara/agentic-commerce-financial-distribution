"""Area 5: contract package, the customer's authorization, and the signed result."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Environment, Settings
from app.integrations.jose_tools import generate_key
from app.integrations.regulated_boundaries import SignatureLevel, SignatureRequest
from external_agent.client import AgentError, artifact_of
from tests.trust_harness import BANK, SIGNER_ID, TrustWorld, sign_event, trust_world

EVENT_TYPE = "signature-event+jwt"


@pytest.fixture
def world():
    with trust_world() as value:
        yield value


def _ready(world: TrustWorld):
    agent = world.agent()
    state = world.to_contract_pending(agent)
    assert world.status_of(state["application_id"]) == "CONTRACT_PENDING"
    return agent, state


def _session(world: TrustWorld, application_id: str):
    return world.sql(
        "SELECT session_id, provider_ref, package_hash, signer_ref, state, level, legal_effect "
        "FROM signature_sessions WHERE application_id = :a ORDER BY attempt DESC",
        a=application_id,
    )[0]


def _submit(agent, state) -> dict:
    return artifact_of(
        agent.call(
            "depot-onboarding",
            state["tokens"]["onboarding_token"],
            action="submit",
            application_id=state["application_id"],
        )
    )


def _event(world: TrustWorld, app_id: str, /, **overrides) -> str:
    session = _session(world, app_id)
    claims = {
        "iss": SIGNER_ID,
        "session_id": session[0],
        "provider_ref": session[1],
        "application_id": app_id,
        "package_hash": session[2],
        "signer_ref": session[3],
        "state": "SIGNED",
        "level": session[5],
        **overrides,
    }
    return sign_event(world.signer_key, EVENT_TYPE, claims, world.clock)


def _send(world: TrustWorld, token: str):
    return world.bank.post("/internal/signature-provider/callbacks", content=token)


# --- the package ------------------------------------------------------------------------------


def test_the_package_binds_the_confirmed_action_and_has_a_stable_hash(world) -> None:
    _, state = _ready(world)
    application_id = state["application_id"]
    browser = state["browser"]
    first = world.contract_form(browser, application_id)["package_hash"]
    assert world.contract_form(browser, application_id)["package_hash"] == first
    document = json.loads(world.sql("SELECT canonical_json FROM contract_packages")[0][0])
    assert document["product_id"] == "depot-basic"
    assert document["product_version"] == "2026.11"
    assert document["documents"][0]["document_id"] == "kid-depot-basic"
    assert document["customer_ref"] == world.customer_ref("alice")
    assert document["legal_effect"] is False
    assert document["approved_action_digest"]


def test_the_package_shows_the_customer_its_hash_and_says_there_is_no_legal_effect(world) -> None:
    _, state = _ready(world)
    page = state["browser"].get(f"/customer/applications/{state['application_id']}/contract").text
    assert "Package hash (SHA-256)" in page and "None. This is a synthetic package" in page
    assert "no qualified electronic signature" in page.replace("creates no", "no")


def test_the_package_cannot_be_changed_or_deleted(world) -> None:
    _, state = _ready(world)
    world.contract_form(state["browser"], state["application_id"])
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    for statement in (
        "UPDATE contract_packages SET package_hash = 'x'",
        "DELETE FROM contract_packages",
    ):
        with pytest.raises(IntegrityError), world.app.state.engine.begin() as connection:
            connection.execute(text(statement))


def test_another_customer_cannot_see_or_authorize_the_contract(world) -> None:
    _, state = _ready(world)
    bob = world.login("bob")
    path = f"/customer/applications/{state['application_id']}/contract"
    assert bob.get(path).status_code == 404
    assert world.sql("SELECT COUNT(*) FROM contract_packages") == [(0,)]


def test_an_anonymous_visitor_is_sent_to_the_login(world) -> None:
    _, state = _ready(world)
    anonymous = TestClient(world.app, base_url=BANK)
    answer = anonymous.get(
        f"/customer/applications/{state['application_id']}/contract", follow_redirects=False
    )
    assert answer.status_code == 303 and "/customer/oidc/login" in answer.headers["location"]


def test_the_page_is_refused_before_the_contract_step(world) -> None:
    agent = world.agent()
    browser = world.login("alice")
    started = world.start_application(agent)
    world.request_link(agent, started)
    answer = browser.get(f"/customer/applications/{started['application_id']}/contract")
    assert answer.status_code == 409


# --- the customer's authorization --------------------------------------------------------------


def test_an_agent_cannot_authorize_signing(world) -> None:
    _, state = _ready(world)
    form = world.contract_form(state["browser"], state["application_id"])
    answer = world.bank.post(
        form["_action"],
        data={"csrf_token": form["csrf_token"], "package_hash": form["package_hash"]},
        headers={"Authorization": f"DPoP {state['tokens']['onboarding_token']}"},
    )
    assert answer.status_code == 401
    assert world.sql("SELECT COUNT(*) FROM signature_sessions") == [(0,)]


def test_the_agent_has_no_action_for_the_contract(world) -> None:
    agent, state = _ready(world)
    with pytest.raises(AgentError) as refused:
        agent.call(
            "depot-onboarding",
            state["tokens"]["onboarding_token"],
            action="sign_contract",
            application_id=state["application_id"],
        )
    assert refused.value.detail["code"] == -32602


def test_authorization_needs_the_csrf_token_of_this_session(world) -> None:
    _, state = _ready(world)
    form = world.contract_form(state["browser"], state["application_id"])
    answer = state["browser"].post(
        form["_action"], data={"csrf_token": "x" * 43, "package_hash": form["package_hash"]}
    )
    assert answer.status_code == 403


def test_a_wrong_package_hash_is_refused(world) -> None:
    _, state = _ready(world)
    form = world.contract_form(state["browser"], state["application_id"])
    answer = state["browser"].post(
        form["_action"], data={"csrf_token": form["csrf_token"], "package_hash": "0" * 64}
    )
    assert answer.status_code == 409
    assert world.sql("SELECT COUNT(*) FROM signature_sessions") == [(0,)]
    assert world.audit_details("contract.authorization_refused")[0]["reason"] == (
        "package_hash_mismatch"
    )


# --- no signature, no depot --------------------------------------------------------------------


def test_the_agent_cannot_submit_before_the_signature(world) -> None:
    agent, state = _ready(world)
    with pytest.raises(AgentError) as refused:
        _submit(agent, state)
    assert refused.value.detail["data"]["code"] == "prerequisites_not_met"
    denied = world.audit_details("application.submit_denied", state["application_id"])[-1]
    assert "contract_package" in denied["missing"]
    assert world.status_of(state["application_id"]) == "CONTRACT_PENDING"


def test_the_whole_path_reaches_opened_with_traceable_evidence(world) -> None:
    agent, state = _ready(world)
    application_id = state["application_id"]
    world.sign(state["browser"], application_id)
    session = _session(world, application_id)
    assert session[4] == "SIGNED" and session[5] == "TEST_NO_LEGAL_EFFECT" and not session[6]
    assert _submit(agent, state)["state"] == "IN_PROGRESS"
    assert world.provider_opens(application_id).status_code == 200
    assert world.status_of(application_id) == "OPENED"
    submitted = world.audit_details("application.submitted", application_id)[0]
    assert submitted["contract_package_hash"] == session[2]
    assert submitted["signature_level"] == "TEST_NO_LEGAL_EFFECT"
    assert submitted["signature_legal_effect"] is False
    assert submitted["policy_version"]
    confirmed = world.audit_details("handoff.confirmed", application_id)[0]
    assert confirmed["attribution_outcome"] == "DIRECT_NO_PARTNER"
    assert confirmed["policy_version"] == submitted["policy_version"]
    assert confirmed["identity_proofing_status"] == "VERIFIED"


@pytest.mark.parametrize("outcome", ["DECLINED", "EXPIRED", "FAILED"])
def test_a_failed_signature_goes_to_a_person_and_never_opens(world, outcome) -> None:
    agent, state = _ready(world)
    application_id = state["application_id"]
    reference = world.authorize_signing(state["browser"], application_id)
    world.signer_outcome(reference, outcome)
    assert world.status_of(application_id) == "MANUAL_REVIEW"
    assert world.sql(
        "SELECT reason FROM review_cases WHERE application_id = :a", a=application_id
    ) == [("SIGNATURE_NOT_COMPLETED",)]
    with pytest.raises(AgentError):
        _submit(agent, state)
    assert world.provider_opens(application_id).status_code == 403
    assert world.status_of(application_id) == "MANUAL_REVIEW"


def test_a_person_can_let_the_customer_retry_and_the_gate_still_holds(world) -> None:
    from app.domain.enums import ReviewDecision
    from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
    from app.services.application_service import ApplicationService
    from app.services.review_service import ReviewService, resolve_dev_operator

    agent, state = _ready(world)
    application_id = state["application_id"]
    reference = world.authorize_signing(state["browser"], application_id)
    world.signer_outcome(reference, "DECLINED")
    factory = world.app.state.session_factory
    reviews = ReviewService(
        lambda: SqlAlchemyUnitOfWork(factory),
        ApplicationService(lambda: SqlAlchemyUnitOfWork(factory), clock=world.clock),
        clock=world.clock,
    )
    case_id = world.sql(
        "SELECT case_id FROM review_cases WHERE application_id = :a", a=application_id
    )[0][0]
    operator = resolve_dev_operator("op-reviewer-1")
    reviews.decide(operator, case_id, ReviewDecision.RESUME, "corr-op")
    assert world.status_of(application_id) == "CONTRACT_PENDING"
    with pytest.raises(AgentError):
        _submit(agent, state)  # a person's decision is not a signature
    world.sign(state["browser"], application_id)
    assert _submit(agent, state)["state"] == "IN_PROGRESS"


def test_cancellation_stops_the_signature_and_a_late_signed_message_is_refused(world) -> None:
    from app.services.customer_session_service import csrf_token

    _, state = _ready(world)
    application_id = state["application_id"]
    browser = state["browser"]
    world.authorize_signing(browser, application_id)
    late = _event(world, application_id)
    secret = browser.cookies.get("trusteq_customer_session", path="/customer")
    token = csrf_token(secret, "contract_cancel", application_id)
    answer = browser.post(
        f"/customer/applications/{application_id}/contract/cancel", data={"csrf_token": token}
    )
    assert answer.status_code == 200
    assert _session(world, application_id)[4] == "CANCELLED"
    assert _send(world, late).status_code == 403
    assert world.audit_details("contract.callback_rejected")[-1]["reason"] == (
        "session_already_decided"
    )
    world.sign(browser, application_id)  # the customer can begin again
    assert _session(world, application_id)[4] == "SIGNED"


def test_a_signed_message_after_expiry_is_refused_and_a_new_attempt_is_possible(world) -> None:
    _, state = _ready(world)
    application_id = state["application_id"]
    world.authorize_signing(state["browser"], application_id)
    world.clock.advance(seconds=world.settings.signature_ttl_seconds + 1)
    assert _send(world, _event(world, application_id)).status_code == 403
    assert world.audit_details("contract.callback_rejected")[-1]["reason"] == "session_expired"
    world.login("alice", browser=state["browser"])  # the customer's own session ended meanwhile
    world.sign(state["browser"], application_id)
    assert _session(world, application_id)[4] == "SIGNED"


def test_an_unavailable_provider_changes_nothing_and_can_be_retried(world) -> None:
    _, state = _ready(world)
    application_id = state["application_id"]
    container = world.app.state.container
    working = container.outbound_http

    def broken() -> httpx.Client:
        def fail(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("synthetic outage")

        return httpx.Client(transport=httpx.MockTransport(fail))

    form = world.contract_form(state["browser"], application_id)
    container.outbound_http = broken
    answer = state["browser"].post(
        form["_action"],
        data={"csrf_token": form["csrf_token"], "package_hash": form["package_hash"]},
        follow_redirects=False,
    )
    assert answer.status_code == 502
    assert _session(world, application_id)[4] == "PROVIDER_UNAVAILABLE"
    assert world.status_of(application_id) == "CONTRACT_PENDING"
    container.outbound_http = working
    world.sign(state["browser"], application_id)
    assert _session(world, application_id)[4] == "SIGNED"


# --- the provider's message --------------------------------------------------------------------


def test_a_duplicate_callback_is_harmless(world) -> None:
    _, state = _ready(world)
    application_id = state["application_id"]
    world.authorize_signing(state["browser"], application_id)
    token = _event(world, application_id)
    assert _send(world, token).json() == {"result": "accepted"}
    assert _send(world, token).json() == {"result": "duplicate_ignored"}
    assert world.event_types(application_id).count("contract.signature_result") == 1


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"package_hash": "0" * 64}, "package_hash_mismatch"),
        ({"signer_ref": "cid-someone-else"}, "signatory_mismatch"),
        ({"application_id": "app-other"}, "application_mismatch"),
        ({"provider_ref": "tsig-other"}, "provider_ref_mismatch"),
        ({"session_id": "sig-unknown"}, "unknown_session"),
        ({"state": "OPENED"}, "unknown_state"),
        ({"level": "QES"}, "level_mismatch"),
        ({"aud": "https://other.test"}, "wrong_audience"),
        ({"iss": "another-provider"}, "wrong_issuer"),
    ],
)
def test_a_correctly_signed_but_wrong_message_is_refused(world, overrides, reason) -> None:
    _, state = _ready(world)
    application_id = state["application_id"]
    world.authorize_signing(state["browser"], application_id)
    assert _send(world, _event(world, application_id, **overrides)).status_code == 403
    assert _session(world, application_id)[4] == "PENDING_CUSTOMER"
    assert world.audit_details("contract.callback_rejected")[-1]["reason"] == reason


def test_a_message_from_an_unknown_signer_key_is_refused(world) -> None:
    _, state = _ready(world)
    application_id = state["application_id"]
    world.authorize_signing(state["browser"], application_id)
    session = _session(world, application_id)
    forged = sign_event(
        generate_key("signer-1"),
        EVENT_TYPE,
        {
            "iss": SIGNER_ID,
            "session_id": session[0],
            "provider_ref": session[1],
            "application_id": application_id,
            "package_hash": session[2],
            "signer_ref": session[3],
            "state": "SIGNED",
            "level": session[5],
        },
        world.clock,
    )
    assert _send(world, forged).status_code == 403
    assert world.audit_details("contract.callback_rejected")[-1]["reason"] == "bad_signature"


def test_a_token_of_another_type_cannot_stand_in_for_a_signature_event(world) -> None:
    _, state = _ready(world)
    application_id = state["application_id"]
    world.authorize_signing(state["browser"], application_id)
    session = _session(world, application_id)
    token = sign_event(
        world.signer_key,
        "identity-proofing-event+jwt",
        {"iss": SIGNER_ID, "session_id": session[0], "state": "SIGNED"},
        world.clock,
    )
    assert _send(world, token).status_code == 403
    assert world.audit_details("contract.callback_rejected")[-1]["reason"] == "wrong_type"


def test_a_tampered_stored_package_stops_the_submission(world) -> None:
    from sqlalchemy import text

    agent, state = _ready(world)
    application_id = state["application_id"]
    world.sign(state["browser"], application_id)
    with world.app.state.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_contract_packages_no_update"))
        connection.execute(text("UPDATE contract_packages SET canonical_json = '{}'"))
    with pytest.raises(AgentError):
        _submit(agent, state)
    denied = world.audit_details("application.submit_denied", application_id)[-1]
    assert denied["missing"][0] == "contract_package_integrity"


# --- the signature level -----------------------------------------------------------------------


def test_nothing_can_claim_a_qualified_signature_without_a_named_qtsp() -> None:
    with pytest.raises(ValueError, match="real, named QTSP"):
        Settings(_env_file=None, environment=Environment.TEST, signature_level="QES")
    with pytest.raises(ValidationError):
        SignatureRequest(
            request_id="r",
            application_id="a",
            action_digest="0" * 64,
            level=SignatureLevel.QUALIFIED_VIA_QTSP,
            expires_at="2030-01-01T00:00:00+00:00",
        )


def test_the_test_signer_page_says_it_is_no_qes(world) -> None:
    _, state = _ready(world)
    reference = world.authorize_signing(state["browser"], state["application_id"])
    page = world.providers_http.get(f"/signing/sessions/{reference}").text
    assert "No qualified or legally valid electronic signature" in page


def test_the_evidence_never_says_qualified(world) -> None:
    _, state = _ready(world)
    world.sign(state["browser"], state["application_id"])
    stored = json.dumps(world.sql("SELECT level, legal_effect, evidence FROM signature_sessions"))
    assert "QES" not in stored and "QUALIFIED" not in stored.upper()
