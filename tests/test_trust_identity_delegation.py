"""Areas 3 and 4: customer login, identity proofing, and delegation to an external agent."""

import base64
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.integrations.jose_tools import generate_key
from external_agent.client import AgentError, artifact_of
from external_agent.trust import new_client_key
from scripts import customer_steps as steps
from tests.trust_harness import (
    ALL_SCOPES,
    BANK,
    IDP,
    OFFER,
    PROOFING_ID,
    TrustWorld,
    provider_reference,
    sign_event,
    trust_world,
)

PROOFING_TYPE = "identity-proofing-event+jwt"


@pytest.fixture
def world():
    with trust_world() as value:
        yield value


def _begin(world: TrustWorld, client: TestClient) -> dict[str, str]:
    encoded = base64.urlsafe_b64encode(b"/customer/delegations").decode().rstrip("=")
    start = client.get(f"/customer/oidc/login?next={encoded}", follow_redirects=False)
    assert start.status_code == 303
    return {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}


def _session_cookie(client: TestClient) -> str | None:
    return client.cookies.get("trusteq_customer_session", path="/customer")


# --- OpenID Connect login ---------------------------------------------------------------------


def test_the_authorization_request_uses_code_pkce_state_and_nonce(world) -> None:
    query = _begin(world, TestClient(world.app, base_url=BANK))
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) == 43
    assert query["state"] and query["nonce"]
    assert query["client_id"] == "bank-client"


def test_a_verified_login_creates_a_session_for_a_derived_reference(world) -> None:
    client = world.login("alice")
    assert _session_cookie(client)
    rows = world.sql("SELECT customer_ref, issuer, acr FROM authentication_events")
    assert rows == [(world.customer_ref("alice"), IDP, "urn:test")]
    assert "alice" not in rows[0][0]  # pseudonymous


def test_no_login_ever_sets_a_proofing_or_kyc_result(world) -> None:
    world.login("alice")
    assert world.sql("SELECT COUNT(*) FROM identity_proofing_cases") == [(0,)]
    assert world.sql("SELECT COUNT(*) FROM check_results") == [(0,)]


def test_the_state_works_once_and_only_for_the_browser_that_began(world) -> None:
    client = TestClient(world.app, base_url=BANK)
    query = _begin(world, client)
    code = world.idp.issue_code(query)
    other = TestClient(world.app, base_url=BANK)
    denied = other.get("/customer/oidc/callback", params={"code": code, "state": query["state"]})
    assert denied.status_code == 403
    assert (
        client.get("/customer/oidc/callback", params={"code": code, "state": "x" * 43}).status_code
        == 403
    )
    good = client.get("/customer/oidc/callback", params={"code": code, "state": query["state"]})
    assert good.status_code == 200
    query_again = {"code": world.idp.issue_code(query), "state": query["state"]}
    client.cookies.set("trusteq_oidc_flow", "x.y", path="/customer/oidc")
    assert client.get("/customer/oidc/callback", params=query_again).status_code == 403


@pytest.mark.parametrize(
    "tamper",
    [
        {"iss": "https://evil.test"},
        {"aud": "another-client"},
        {"nonce": "wrong-nonce"},
        {"exp": 1},
        {"iat": 9_999_999_999},
        {"sub": ""},
    ],
)
def test_a_wrong_id_token_is_refused(world, tamper) -> None:
    world.idp.tamper = tamper
    client = TestClient(world.app, base_url=BANK)
    query = _begin(world, client)
    done = client.get(
        "/customer/oidc/callback",
        params={"code": world.idp.issue_code(query), "state": query["state"]},
    )
    assert done.status_code == 403
    assert _session_cookie(client) is None


def test_a_token_signed_by_another_key_is_refused(world) -> None:
    world.idp.sign_with = generate_key("idp-1")
    client = TestClient(world.app, base_url=BANK)
    query = _begin(world, client)
    done = client.get(
        "/customer/oidc/callback",
        params={"code": world.idp.issue_code(query), "state": query["state"]},
    )
    assert done.status_code == 403


def test_a_wrong_pkce_verifier_is_refused_by_the_provider(world) -> None:
    client = TestClient(world.app, base_url=BANK)
    query = _begin(world, client)
    code = world.idp.issue_code({**query, "code_challenge": "A" * 43})
    done = client.get("/customer/oidc/callback", params={"code": code, "state": query["state"]})
    assert done.status_code == 403


def test_an_issuer_parameter_from_another_provider_is_refused(world) -> None:
    client = TestClient(world.app, base_url=BANK)
    query = _begin(world, client)
    done = client.get(
        "/customer/oidc/callback",
        params={
            "code": world.idp.issue_code(query),
            "state": query["state"],
            "iss": "https://evil.test",
        },
    )
    assert done.status_code == 403


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer": "https://evil.test"},
        {"token_endpoint": "https://169.254.169.254/token"},
        {"jwks_uri": "https://evil.test/jwks"},
        {"authorization_endpoint": "http://idp.test/authorize"},
    ],
)
def test_changed_discovery_metadata_cannot_redirect_the_bank(world, overrides) -> None:
    world.idp.discovery_overrides = overrides
    client = TestClient(world.app, base_url=BANK)
    encoded = base64.urlsafe_b64encode(b"/customer/delegations").decode().rstrip("=")
    assert client.get(f"/customer/oidc/login?next={encoded}").status_code == 503


@pytest.mark.parametrize(
    "target", ["https://evil.test/", "//evil.test", "/docs", "/customer/../admin"]
)
def test_the_return_path_cannot_be_an_open_redirect(world, target) -> None:
    client = TestClient(world.app, base_url=BANK)
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    response = client.get(f"/customer/oidc/login?next={encoded}", follow_redirects=False)
    assert response.status_code == 400


def test_an_agent_credential_is_refused_at_the_login(world) -> None:
    client = TestClient(world.app, base_url=BANK)
    encoded = base64.urlsafe_b64encode(b"/customer/delegations").decode().rstrip("=")
    response = client.get(
        f"/customer/oidc/login?next={encoded}", headers={"Authorization": "DPoP abc"}
    )
    assert response.status_code == 401


def test_a_signed_in_browser_cannot_switch_to_another_account_without_signing_out(world) -> None:
    client = world.login("alice")
    world.idp.next_subject = "bob"
    query = _begin(world, client)
    done = client.get(
        "/customer/oidc/callback",
        params={"code": world.idp.issue_code(query), "state": query["state"]},
    )
    assert done.status_code == 403
    assert world.sql("SELECT COUNT(*) FROM authentication_events") == [(1,)]


def test_logout_ends_the_session_on_the_server(world) -> None:
    from app.services.customer_session_service import csrf_token

    client = world.login("alice")
    secret = _session_cookie(client)
    assert client.get("/customer/delegations").status_code == 200
    refused = client.post("/customer/oidc/logout", data={"csrf_token": "x" * 43})
    assert refused.status_code == 403
    token = csrf_token(secret, "logout", world.customer_ref("alice"))
    assert client.post("/customer/oidc/logout", data={"csrf_token": token}).status_code == 200
    stale = TestClient(world.app, base_url=BANK)
    stale.cookies.set("trusteq_customer_session", secret, path="/customer")
    response = stale.get("/customer/delegations", follow_redirects=False)
    assert response.status_code == 303 and "/customer/oidc/login" in response.headers["location"]


def test_a_session_expires(world) -> None:
    client = world.login("alice")
    world.clock.advance(seconds=world.settings.customer_session_ttl_seconds + 1)
    response = client.get("/customer/delegations", follow_redirects=False)
    assert response.status_code == 303


def test_the_dev_sign_in_is_off_when_a_real_login_is_configured(world) -> None:
    client = TestClient(world.app, base_url=BANK)
    answer = client.post(
        "/customer/dev-login",
        data={"customer_id": "anyone", "handoff_id": "hnd-x", "token": "t" * 24, "login_csrf": "x"},
    )
    assert answer.status_code == 401
    assert world.sql("SELECT COUNT(*) FROM customer_sessions") == [(0,)]


def test_oidc_mode_without_a_complete_provider_still_fails_closed() -> None:
    settings = Settings(_env_file=None, environment=Environment.TEST, customer_identity_mode="oidc")
    assert not settings.oidc_login_active and not settings.customer_sign_in_active


def test_plain_http_issuers_are_refused_outside_the_local_setting(world) -> None:
    from app.integrations.safe_http import UnsafeUrlError, check_url

    with pytest.raises(UnsafeUrlError):
        check_url(
            "http://idp.test/x", allowed_origins=frozenset({"http://idp.test"}),
            allow_insecure_local=False,
        )  # fmt: skip
    with pytest.raises(UnsafeUrlError):
        check_url(
            "http://10.0.0.8/x", allowed_origins=frozenset({"http://10.0.0.8"}),
            allow_insecure_local=True,
        )  # fmt: skip


def test_production_refuses_the_insecure_local_setting_and_a_regulated_claim() -> None:
    with pytest.raises(ValueError, match="oidc_allow_insecure_local"):
        Settings(_env_file=None, environment=Environment.PRODUCTION, oidc_allow_insecure_local=True)
    with pytest.raises(ValueError, match="regulated identity proofing"):
        Settings(_env_file=None, environment=Environment.TEST, proofing_provider_regulated=True)


# --- identity proofing ------------------------------------------------------------------------


def _proofing_app(world: TrustWorld, agent=None):
    agent = agent or world.agent()
    browser = world.login("alice")
    started = world.start_application(agent)
    link = world.request_link(agent, started)
    return agent, browser, started, link


def _case(world: TrustWorld, application_id: str):
    return world.sql(
        "SELECT case_id, provider_ref, status, regulatory_evidence, customer_ref "
        "FROM identity_proofing_cases WHERE application_id = :a ORDER BY attempt DESC",
        a=application_id,
    )[0]


def test_confirmation_needs_a_verified_proofing_result(world) -> None:
    _, browser, started, link = _proofing_app(world)
    steps.complete_customer_information(browser, link)
    page = browser.get(link).text
    assert steps.page_form(page, "/confirm") is None
    assert "Identity verification (synthetic)" in page
    world.complete_proofing(browser, link)
    page = browser.get(link).text
    assert steps.page_form(page, "/confirm") is not None
    assert "no legal or regulatory value" in page
    details = world.audit_details("identity_proofing.result", started["application_id"])
    assert details[0]["regulatory_evidence"] is False and details[0]["kyc_status_set"] is False


def test_the_json_confirmation_is_refused_without_proofing(world) -> None:
    from app.services.customer_session_service import csrf_token

    _, browser, started, link = _proofing_app(world)
    steps.complete_customer_information(browser, link)
    handoff_id = link.split("/")[3].split("?")[0]
    nonce = link.split("token=")[1]
    digest = world.sql("SELECT action_digest FROM handoffs WHERE handoff_id = :h", h=handoff_id)[0][
        0
    ]
    secret = browser.cookies.get("trusteq_customer_api", path="/v1/customer")
    answer = browser.post(
        f"/v1/customer/handoffs/{handoff_id}/confirm",
        json={"nonce": nonce, "action_digest": digest},
        headers={"X-CSRF-Token": csrf_token(secret, "confirm", handoff_id)},
    )
    assert answer.status_code == 409
    assert world.status_of(started["application_id"]) == "AWAITING_AUTHORIZATION"


def test_dev_sign_in_alone_cannot_start_proofing(tmp_path) -> None:
    """The DEV fallback stays usable for the old flows, but it cannot reach regulated identity."""
    with trust_world(customer_identity_mode="dev_simulated") as world:
        from tests.flow import sign_in_browser

        agent = world.agent()
        browser = TestClient(world.app, base_url=BANK)
        sign_in_browser(browser, "cust-dev")
        pushed = agent.request_delegation(scope=ALL_SCOPES, **OFFER)
        world.decide(pushed, browser=browser)
        rows = world.sql("SELECT identity_assurance FROM delegations")
        assert rows == [("dev_simulated",)]
        world.clock.advance(seconds=2)
        assert agent.poll_token(pushed["device_code"]) == "issued"
        started = artifact_of(
            agent.call("depot-onboarding", agent.access_token, action="start", **OFFER)
        )
        link = world.request_link(agent, {"tokens": started["authorization"],
                                          "application_id": started["application_id"]})  # fmt: skip
        form = steps.page_form(browser.get(link).text, "/identity-proofing")
        answer = browser.post(
            form["_action"], data={"token": form["token"], "csrf_token": form["csrf_token"]}
        )
        assert answer.status_code == 403
        assert "DEV sign-in is not enough" in answer.text
        assert world.sql("SELECT COUNT(*) FROM identity_proofing_cases") == [(0,)]


def _forge(world, key, **claims) -> str:
    return sign_event(key, PROOFING_TYPE, {"iss": PROOFING_ID, **claims}, world.clock)


def _start_case(world):
    agent, browser, started, link = _proofing_app(world)
    form = steps.page_form(browser.get(link).text, "/identity-proofing")
    response = browser.post(
        form["_action"],
        data={"token": form["token"], "csrf_token": form["csrf_token"]},
        follow_redirects=False,
    )
    assert response.status_code == 200
    return browser, started, link, provider_reference(response.text)


def test_a_callback_signed_by_another_key_is_refused(world) -> None:
    _, started, _, reference = _start_case(world)
    case = _case(world, started["application_id"])
    token = _forge(
        world,
        generate_key("proofing-1"),
        case_id=case[0],
        provider_ref=reference,
        application_id=started["application_id"],
        customer_ref=case[4],
        status="VERIFIED",
        regulatory_evidence=False,
    )
    answer = world.bank.post("/internal/identity-proofing/callbacks", content=token)
    assert answer.status_code == 403
    assert _case(world, started["application_id"])[2] == "PENDING"
    assert (
        world.audit_details("identity_proofing.callback_rejected")[0]["reason"] == "bad_signature"
    )


def test_a_garbage_callback_is_refused(world) -> None:
    assert (
        world.bank.post("/internal/identity-proofing/callbacks", content="not-a-token").status_code
        == 403
    )


@pytest.mark.parametrize("outcome", ["REJECTED", "MANUAL_REVIEW", "EXPIRED"])
def test_a_failed_identification_goes_to_a_person_and_never_opens(world, outcome) -> None:
    agent, browser, started, link = _proofing_app(world)
    world.complete_proofing(browser, link, outcome)
    application_id = started["application_id"]
    assert world.status_of(application_id) == "MANUAL_REVIEW"
    assert world.sql(
        "SELECT reason, status FROM review_cases WHERE application_id = :a", a=application_id
    ) == [("IDENTITY_PROOFING_NOT_COMPLETED", "OPEN")]
    status = artifact_of(
        agent.call(
            "depot-onboarding",
            started["tokens"]["status_token"],
            action="status",
            application_id=application_id,
        )
    )
    assert status["state"] == "ACTION_REQUIRED"
    assert "identity" not in json.dumps(status).lower()
    page = browser.get(link).text
    assert steps.page_form(page, "/confirm") is None


def _event(world: TrustWorld, started: dict, reference: str, **overrides) -> str:
    case = _case(world, started["application_id"])
    claims = {
        "case_id": case[0],
        "provider_ref": reference,
        "application_id": started["application_id"],
        "customer_ref": case[4],
        "status": "VERIFIED",
        "method": "SYNTHETIC_TEST",
        "assurance": "synthetic_no_assurance",
        "regulatory_evidence": False,
        **overrides,
    }
    return _forge(world, world.proofing_key, **claims)


def _send_proofing(world: TrustWorld, token: str):
    return world.bank.post("/internal/identity-proofing/callbacks", content=token)


def test_a_valid_callback_applies_and_an_exact_replay_changes_nothing(world) -> None:
    _, started, _, reference = _start_case(world)
    token = _event(world, started, reference)
    assert _send_proofing(world, token).json() == {"result": "accepted"}
    assert _case(world, started["application_id"])[2] == "VERIFIED"
    assert _send_proofing(world, token).json() == {"result": "duplicate_ignored"}
    assert _case(world, started["application_id"])[2] == "VERIFIED"


def test_a_decided_case_cannot_be_changed_by_a_new_event(world) -> None:
    _, started, _, reference = _start_case(world)
    assert _send_proofing(world, _event(world, started, reference)).status_code == 200
    late = _event(world, started, reference, status="REJECTED")
    assert _send_proofing(world, late).status_code == 403
    assert _case(world, started["application_id"])[2] == "VERIFIED"
    assert world.audit_details("identity_proofing.callback_rejected")[-1]["reason"] == (
        "case_already_decided"
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"application_id": "app-other"}, "binding_mismatch"),
        ({"customer_ref": "cid-somebody-else"}, "binding_mismatch"),
        ({"provider_ref": "tprf-other"}, "binding_mismatch"),
        ({"regulatory_evidence": True}, "regulatory_claim_not_accepted"),
        ({"status": "KYC_VERIFIED"}, "unknown_status"),
        ({"document_image": "base64..."}, "undeclared_evidence_field"),
        ({"biometric_template": "..."}, "undeclared_evidence_field"),
    ],
)
def test_a_correctly_signed_but_wrong_callback_is_refused(world, overrides, reason) -> None:
    _, started, _, reference = _start_case(world)
    token = _event(world, started, reference, **overrides)
    assert _send_proofing(world, token).status_code == 403
    assert _case(world, started["application_id"])[2] == "PENDING"
    assert world.audit_details("identity_proofing.callback_rejected")[-1]["reason"] == reason


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"aud": "https://other-bank.test"}, "wrong_audience"),
        ({"iss": "another-provider"}, "wrong_issuer"),
        ({"exp": 1000}, "expired"),
        ({"jti": "short"}, "jti_invalid"),
    ],
)
def test_an_event_outside_its_envelope_is_refused(world, overrides, reason) -> None:
    _, started, _, reference = _start_case(world)
    token = _event(world, started, reference, **overrides)
    assert _send_proofing(world, token).status_code == 403
    assert world.audit_details("identity_proofing.callback_rejected")[-1]["reason"] == reason


def test_a_verified_result_after_the_cases_expiry_is_refused(world) -> None:
    _, started, _, reference = _start_case(world)
    world.clock.advance(seconds=world.settings.proofing_ttl_seconds + 1)
    assert _send_proofing(world, _event(world, started, reference)).status_code == 403
    assert _case(world, started["application_id"])[2] == "PENDING"


def test_an_authenticated_customer_cannot_see_or_approve_someone_elses_application(world) -> None:
    _, _, started, link = _proofing_app(world)
    bob = world.login("bob")
    page = bob.get(link)
    assert "Tax information" not in page.text
    assert page.status_code in {403, 404}
    assert world.status_of(started["application_id"]) == "AWAITING_AUTHORIZATION"


# --- delegation -------------------------------------------------------------------------------


def _token(world: TrustWorld, agent, subject: str = "alice", **kwargs) -> dict:
    pushed = world.delegate(agent, subject, **kwargs)
    assert agent.poll_token(pushed["device_code"]) == "issued"
    return pushed


def test_a_valid_delegation_gives_the_agent_a_bound_token_and_no_customer_secret(world) -> None:
    agent = world.agent()
    pushed = _token(world, agent)
    grant = agent.grant
    assert grant["token_type"] == "DPoP"  # noqa: S105 - a token type name
    assert grant["customer_ref"] == world.customer_ref("alice")
    assert set(grant["scope"].split()) == set(ALL_SCOPES.split())
    assert world.sql(
        "SELECT status, identity_assurance FROM delegations WHERE delegation_id = :d",
        d=pushed["delegation_id"],
    ) == [("ISSUED", "oidc_authenticated_not_proofed")]
    assert "alice" not in json.dumps(grant)


def test_the_customer_page_shows_the_exact_request_and_escapes_the_agents_name(world) -> None:
    agent = world.agent()
    pushed = agent.request_delegation(scope=ALL_SCOPES, **OFFER)
    client = world.login("alice")
    link = urlsplit(pushed["verification_uri_complete"])
    page = client.get(f"{link.path}?{link.query}").text
    assert "Personal agent &lt;test&gt;" in page and "<test>" not in page
    assert "depot-basic" in page and "cannot confirm or sign" in page


def test_a_declined_delegation_gives_no_token(world) -> None:
    agent = world.agent()
    pushed = world.delegate(agent, approve=False)
    with pytest.raises(AgentError) as refused:
        agent.poll_token(pushed["device_code"])
    assert refused.value.detail == "access_denied"


def test_the_agent_cannot_decide_its_own_delegation(world) -> None:
    agent = world.agent()
    pushed = agent.request_delegation(scope=ALL_SCOPES, **OFFER)
    token = parse_qs(urlsplit(pushed["verification_uri_complete"]).query)["request"][0]
    anonymous = TestClient(world.app, base_url=BANK)
    answer = anonymous.post(
        "/customer/delegations/authorize",
        data={"request": token, "csrf_token": "x" * 43, "decision": "approve"},
    )
    assert answer.status_code == 401
    bearer = anonymous.post(
        "/customer/delegations/authorize",
        data={"request": token, "decision": "approve"},
        headers={"Authorization": "DPoP tqa_abc"},
    )
    assert bearer.status_code == 401
    assert world.sql("SELECT status FROM delegations") == [("PENDING",)]


def test_a_wrong_csrf_token_cannot_approve(world) -> None:
    agent = world.agent()
    pushed = agent.request_delegation(scope=ALL_SCOPES, **OFFER)
    token = parse_qs(urlsplit(pushed["verification_uri_complete"]).query)["request"][0]
    client = world.login("alice")
    answer = client.post(
        "/customer/delegations/authorize",
        data={"request": token, "csrf_token": "x" * 43, "decision": "approve"},
    )
    assert answer.status_code == 403
    assert world.sql("SELECT status FROM delegations") == [("PENDING",)]


def test_a_cross_site_post_cannot_approve(world) -> None:
    agent = world.agent()
    pushed = agent.request_delegation(scope=ALL_SCOPES, **OFFER)
    link = urlsplit(pushed["verification_uri_complete"])
    client = world.login("alice")
    form = steps.page_form(client.get(f"{link.path}?{link.query}").text, "/delegations/authorize")
    answer = client.post(
        "/customer/delegations/authorize",
        data={"request": form["request"], "csrf_token": form["csrf_token"], "decision": "approve"},
        headers={"Origin": "https://evil.test", "Sec-Fetch-Site": "cross-site"},
    )
    assert answer.status_code == 403


def test_an_unregistered_agent_cannot_ask(world) -> None:
    stranger = new_client_key("stranger")
    agent = world.agent(client_id="not-registered", client_key=stranger)
    with pytest.raises(AgentError) as refused:
        agent.request_delegation(scope=ALL_SCOPES, **OFFER)
    assert refused.value.detail == "invalid_client"


def test_a_registered_id_with_another_key_cannot_ask(world) -> None:
    agent = world.agent(client_key=new_client_key("agent-key-1"))
    with pytest.raises(AgentError) as refused:
        agent.request_delegation(scope=ALL_SCOPES, **OFFER)
    assert refused.value.detail == "invalid_client"


@pytest.mark.parametrize(
    "scope",
    [
        "create_draft read_status approve_consent",
        "create_draft open_account",
        "read_status",
    ],
)
def test_scope_escalation_at_the_request_fails(world, scope) -> None:
    with pytest.raises(AgentError) as refused:
        world.agent().request_delegation(scope=scope, **OFFER)
    assert refused.value.detail == "invalid_scope"


def test_a_scope_outside_the_agents_registration_fails() -> None:
    with trust_world(allowed_scopes="create_draft read_status") as world:
        with pytest.raises(AgentError) as refused:
            world.agent().request_delegation(scope=ALL_SCOPES, **OFFER)
        assert refused.value.detail == "invalid_scope"


@pytest.mark.parametrize(
    "offer",
    [
        {"provider_id": "rogue-bank", "product_id": "depot-basic", "channel_id": "direct"},
        {"provider_id": "fixture-bank", "product_id": "other", "channel_id": "direct"},
        {"provider_id": "fixture-bank", "product_id": "depot-basic", "channel_id": "partner-2"},
    ],
)
def test_a_delegation_for_an_unverified_action_is_refused(world, offer) -> None:
    with pytest.raises(AgentError) as refused:
        world.agent().request_delegation(scope=ALL_SCOPES, **offer)
    assert refused.value.detail == "invalid_authorization_details"


def test_the_token_works_only_for_the_approved_action(world) -> None:
    agent = world.agent()
    _token(world, agent)
    with pytest.raises(AgentError) as refused:
        agent.call(
            "depot-onboarding",
            agent.access_token,
            action="start",
            provider_id="fixture-bank",
            product_id="depot-basic",
            channel_id="partner-1",
        )
    assert refused.value.detail["data"]["code"] == "delegation_not_active"
    assert world.sql("SELECT COUNT(*) FROM applications") == [(0,)]


def test_the_customer_comes_from_the_delegation_not_from_the_agent(world) -> None:
    agent = world.agent()
    _token(world, agent)
    url = f"{BANK}/v1/applications"
    response = world.bank.post(
        "/v1/applications",
        json={**OFFER, "customer_ref": world.customer_ref("bob")},
        headers={
            "Authorization": f"DPoP {agent.access_token}",
            "DPoP": agent._dpop("POST", url, agent.access_token),
            "Idempotency-Key": "11111111-1111-4111-8111-111111111111",
        },
    )
    assert response.status_code == 403
    assert world.sql("SELECT COUNT(*) FROM applications") == [(0,)]


def test_a_delegated_token_is_useless_without_the_proof(world) -> None:
    agent = world.agent()
    _token(world, agent)
    url = "/v1/applications"
    body = {**OFFER, "customer_ref": agent.grant["customer_ref"]}
    idem = {"Idempotency-Key": "11111111-1111-4111-8111-111111111111"}
    plain = world.bank.post(
        url, json=body, headers={"Authorization": f"Bearer {agent.access_token}", **idem}
    )
    assert plain.status_code == 401
    missing = world.bank.post(
        url, json=body, headers={"Authorization": f"DPoP {agent.access_token}", **idem}
    )
    assert missing.status_code == 401
    assert world.sql("SELECT COUNT(*) FROM applications") == [(0,)]
    reasons = [d["reason"] for d in world.audit_details("authorization.denied")]
    assert "dpop_downgrade" in reasons and "dpop_missing" in reasons


def test_a_proof_from_another_key_fails(world) -> None:
    agent = world.agent()
    _token(world, agent)
    other = world.agent()
    url = f"{BANK}/v1/applications"
    answer = world.bank.post(
        "/v1/applications",
        json={**OFFER, "customer_ref": agent.grant["customer_ref"]},
        headers={
            "Authorization": f"DPoP {agent.access_token}",
            "DPoP": other._dpop("POST", url, agent.access_token),
            "Idempotency-Key": "11111111-1111-4111-8111-111111111111",
        },
    )
    assert answer.status_code == 401
    assert "dpop_wrong_key" in [d["reason"] for d in world.audit_details("authorization.denied")]


@pytest.mark.parametrize(
    ("method", "url", "token_override", "reason"),
    [
        ("GET", f"{BANK}/v1/applications", None, "dpop_wrong_request"),
        ("POST", f"{BANK}/v1/other", None, "dpop_wrong_request"),
        ("POST", f"{BANK}/v1/applications", "tqa_" + "B" * 43, "dpop_wrong_token"),
    ],
)
def test_a_proof_for_another_request_or_token_fails(world, method, url, token_override, reason):
    agent = world.agent()
    _token(world, agent)
    proof = agent._dpop(method, url, token_override or agent.access_token)
    answer = world.bank.post(
        "/v1/applications",
        json={**OFFER, "customer_ref": agent.grant["customer_ref"]},
        headers={
            "Authorization": f"DPoP {agent.access_token}",
            "DPoP": proof,
            "Idempotency-Key": "11111111-1111-4111-8111-111111111111",
        },
    )
    assert answer.status_code == 401
    assert reason in [d["reason"] for d in world.audit_details("authorization.denied")]


def test_a_replayed_proof_fails(world) -> None:
    agent = world.agent()
    started = world.start_application(agent)
    token = started["tokens"]["status_token"]
    path = f"/v1/applications/{started['application_id']}"
    proof = agent._dpop("GET", f"{BANK}{path}", token)
    headers = {"Authorization": f"DPoP {token}", "DPoP": proof}
    assert world.bank.get(path, headers=headers).status_code == 200
    assert world.bank.get(path, headers=headers).status_code == 401
    assert "dpop_replayed" in [d["reason"] for d in world.audit_details("authorization.denied")]


def test_a_stale_proof_fails(world) -> None:
    agent = world.agent()
    started = world.start_application(agent)
    token = started["tokens"]["status_token"]
    path = f"/v1/applications/{started['application_id']}"
    proof = agent._dpop("GET", f"{BANK}{path}", token)
    world.clock.advance(seconds=120)
    answer = world.bank.get(path, headers={"Authorization": f"DPoP {token}", "DPoP": proof})
    assert answer.status_code == 401


def test_a_replayed_client_assertion_fails(world) -> None:
    agent = world.agent()
    assertion = agent._client_assertion()
    form = {
        "client_assertion": assertion,
        "scope": ALL_SCOPES,
        "authorization_details": json.dumps({"type": "depot_onboarding", **OFFER}),
    }
    url = f"{BANK}/v1/delegation/par"
    first = world.bank.post(
        "/v1/delegation/par", data=form, headers={"DPoP": agent._dpop("POST", url)}
    )
    assert first.status_code == 201
    second = world.bank.post(
        "/v1/delegation/par", data=form, headers={"DPoP": agent._dpop("POST", url)}
    )
    assert second.status_code == 401


def test_a_token_is_collected_once_and_a_second_try_revokes_the_grant(world) -> None:
    agent = world.agent()
    pushed = _token(world, agent)
    with pytest.raises(AgentError) as refused:
        agent.poll_token(pushed["device_code"])
    assert refused.value.detail == "invalid_grant"
    assert world.sql("SELECT status FROM delegations") == [("REVOKED",)]
    task = agent.call("human-escalation")  # public reading still works
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    with pytest.raises(AgentError):
        agent.call("depot-onboarding", agent.access_token, action="start", **OFFER)


def test_another_agent_cannot_collect_the_token(world) -> None:
    from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
    from app.services.delegation_service import register_agent

    second = new_client_key("agent-2-key")
    with SqlAlchemyUnitOfWork(world.app.state.session_factory) as uow:
        register_agent(
            uow,
            client_id="agent-2",
            display_name="Other agent",
            jwks={"keys": [second.as_dict(private=False)]},
            allowed_scopes=frozenset(ALL_SCOPES.split()),
            now=world.clock(),
        )
        uow.commit()
    agent = world.agent()
    pushed = world.delegate(agent)
    thief = world.agent(client_id="agent-2", client_key=second)
    with pytest.raises(AgentError) as refused:
        thief.poll_token(pushed["device_code"])
    assert refused.value.detail == "invalid_grant"
    assert agent.poll_token(pushed["device_code"]) == "issued"


def test_the_device_code_needs_the_requests_dpop_key(world) -> None:
    agent = world.agent()
    pushed = world.delegate(agent)
    agent._dpop_key = new_client_key("another-dpop-key")
    with pytest.raises(AgentError) as refused:
        agent.poll_token(pushed["device_code"])
    assert refused.value.detail == "invalid_dpop_proof"


def test_polling_too_fast_is_slowed_and_an_expired_request_ends(world) -> None:
    agent = world.agent()
    pushed = agent.request_delegation(scope=ALL_SCOPES, **OFFER)
    assert agent.poll_token(pushed["device_code"]) == "pending"
    assert agent.poll_token(pushed["device_code"]) == "pending"  # slow_down, also not a token
    world.clock.advance(seconds=world.settings.delegation_request_ttl_seconds + 1)
    with pytest.raises(AgentError) as refused:
        agent.poll_token(pushed["device_code"])
    assert refused.value.detail == "expired_token"


def test_an_expired_delegation_fails(world) -> None:
    agent = world.agent()
    started = world.start_application(agent)
    world.clock.advance(seconds=world.settings.delegation_ttl_seconds + 1)
    with pytest.raises(AgentError) as refused:
        agent.call(
            "depot-onboarding",
            started["tokens"]["status_token"],
            action="status",
            application_id=started["application_id"],
        )
    assert refused.value.detail["code"] == -32041
    assert "delegation_expired" in [
        d["reason"] for d in world.audit_details("authorization.denied")
    ]


def test_a_revoked_delegation_fails_for_every_token_at_once(world) -> None:
    from app.services.customer_session_service import csrf_token

    agent = world.agent()
    started = world.start_application(agent)
    client = world.browser("alice")
    secret = client.cookies.get("trusteq_customer_session", path="/customer")
    page = client.get("/customer/delegations").text
    assert started["delegation_id"][:12] in page
    token = csrf_token(secret, "delegation_revoke", started["delegation_id"])
    answer = client.post(
        f"/customer/delegations/{started['delegation_id']}/revoke", data={"csrf_token": token}
    )
    assert answer.status_code == 200
    for name, action in (("onboarding_token", "submit"), ("status_token", "status")):
        with pytest.raises(AgentError) as refused:
            agent.call(
                "depot-onboarding",
                started["tokens"][name],
                action=action,
                application_id=started["application_id"],
            )
        assert refused.value.detail["code"] == -32041
    assert world.status_of(started["application_id"]) == "DRAFT"
    assert world.audit_details("delegation.revoked")[0]["reason"] == "customer_revoked"


def test_another_customer_cannot_revoke_it(world) -> None:
    from app.services.customer_session_service import csrf_token

    agent = world.agent()
    started = world.start_application(agent)
    bob = world.login("bob")
    secret = bob.cookies.get("trusteq_customer_session", path="/customer")
    token = csrf_token(secret, "delegation_revoke", started["delegation_id"])
    answer = bob.post(
        f"/customer/delegations/{started['delegation_id']}/revoke", data={"csrf_token": token}
    )
    assert answer.status_code == 404
    assert world.sql("SELECT status FROM delegations") == [("ISSUED",)]


def test_cross_customer_a_delegated_token_cannot_read_another_application(world) -> None:
    agent_a = world.agent()
    started_a = world.start_application(agent_a, "alice")
    agent_b = world.agent()
    started_b = world.start_application(agent_b, "bob")
    token = started_a["tokens"]["status_token"]
    with pytest.raises(AgentError) as refused:
        agent_a.call(
            "depot-onboarding",
            token,
            action="status",
            application_id=started_b["application_id"],
        )
    assert refused.value.detail["data"]["code"] == "not_found"


def test_a_narrower_delegation_narrows_the_issued_tokens() -> None:
    """The customer approved no submission: the application's token cannot submit either."""
    with trust_world() as world:
        agent = world.agent()
        pushed = world.delegate(agent, scope="create_draft request_handoff read_status")
        assert agent.poll_token(pushed["device_code"]) == "issued"
        started = artifact_of(
            agent.call("depot-onboarding", agent.access_token, action="start", **OFFER)
        )
        with pytest.raises(AgentError) as refused:
            agent.call(
                "depot-onboarding",
                started["authorization"]["onboarding_token"],
                action="submit",
                application_id=started["application_id"],
            )
        assert refused.value.detail["data"]["code"] == "insufficient_scope"


@pytest.mark.parametrize(
    "path",
    [
        "/customer/handoffs/{handoff}/confirm",
        "/v1/customer/handoffs/{handoff}/confirm",
        "/customer/handoffs/{handoff}/tax",
        "/customer/handoffs/{handoff}/identity-proofing",
    ],
)
def test_the_agents_token_is_refused_on_customer_routes(world, path) -> None:
    agent = world.agent()
    started = world.start_application(agent)
    link = world.request_link(agent, started)
    handoff = re.search(r"/customer/handoffs/([^?]+)", link).group(1)
    nonce = link.split("token=")[1]
    kwargs = (
        {"json": {"nonce": nonce, "action_digest": "0" * 64}}
        if path.startswith("/v1/")
        else {"data": {"token": nonce}}
    )
    answer = world.bank.post(
        path.format(handoff=handoff),
        headers={"Authorization": f"DPoP {started['tokens']['onboarding_token']}"},
        **kwargs,
    )
    assert answer.status_code == 401
    assert world.sql("SELECT COUNT(*) FROM consent_receipts") == [(0,)]


def test_the_agent_cannot_submit_before_the_customer_and_gets_no_reason(world) -> None:
    agent = world.agent()
    started = world.start_application(agent)
    with pytest.raises(AgentError) as refused:
        agent.call(
            "depot-onboarding",
            started["tokens"]["onboarding_token"],
            action="submit",
            application_id=started["application_id"],
        )
    assert refused.value.detail["data"]["code"] == "prerequisites_not_met"
    assert world.status_of(started["application_id"]) == "DRAFT"


def test_a_delegation_row_holds_no_raw_secret(world) -> None:
    agent = world.agent()
    pushed = _token(world, agent)
    stored = json.dumps(world.sql("SELECT * FROM delegations"), default=str)
    assert pushed["device_code"] not in stored
    assert agent.access_token not in stored


def test_undelegated_tokens_keep_working_as_plain_bearer_tokens_only(world) -> None:
    token = (
        world.authz()
        .issue_bootstrap(agent_actor_id="agent-old", customer_id="cust-old", correlation_id="c")
        .token
    )
    body = {**OFFER, "customer_ref": "cust-old"}
    idem = {"Idempotency-Key": "22222222-2222-4222-8222-222222222222"}
    as_dpop = world.bank.post(
        "/v1/applications", json=body, headers={"Authorization": f"DPoP {token}", **idem}
    )
    assert as_dpop.status_code == 401
    as_bearer = world.bank.post(
        "/v1/applications", json=body, headers={"Authorization": f"Bearer {token}", **idem}
    )
    assert as_bearer.status_code == 201
