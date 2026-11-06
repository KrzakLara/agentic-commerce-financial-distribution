"""Controls that a layered test could not isolate: M74, M80 and M87.

Revocation and the signature gate each have two layers, so each layer is tested alone here. The
login's browser binding is reached with a browser that holds its own valid login flow.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services import regulatory_policy, trust_gates
from app.services.authorization_service import hash_token
from app.services.oidc_login import OidcLoginError, OidcLoginService
from external_agent.client import AgentError, artifact_of
from tests.test_trust_contract_signing import _ready, _submit
from tests.test_trust_identity_delegation import _begin, _session_cookie
from tests.trust_harness import BANK, TrustWorld, trust_world


@pytest.fixture
def world():
    with trust_world() as value:
        yield value


# --- M74: revocation has two layers, the delegation and the grants with their tokens -------------


def _status(world: TrustWorld, agent, started) -> dict:
    return artifact_of(
        agent.call(
            "depot-onboarding",
            started["tokens"]["status_token"],
            action="status",
            application_id=started["application_id"],
        )
    )


def _denial_reasons(world: TrustWorld) -> list[str]:
    return [detail["reason"] for detail in world.audit_details("authorization.denied")]


def test_a_live_delegation_and_live_tokens_are_accepted(world) -> None:
    agent = world.agent()
    started = world.start_application(agent)
    assert _status(world, agent, started)["application_id"] == started["application_id"]


@pytest.mark.parametrize(
    "assignment",
    [
        "status = 'REVOKED'",
        "revoked_at = '2026-11-03 09:00:00.000000'",
    ],
)
def test_the_delegation_layer_alone_refuses_while_the_tokens_are_still_live(
    world, assignment
) -> None:
    agent = world.agent()
    started = world.start_application(agent)
    with world.app.state.engine.begin() as connection:
        connection.exec_driver_sql(f"UPDATE delegations SET {assignment}")  # noqa: S608 - fixed
    live = world.sql("SELECT COUNT(*) FROM agent_access_tokens WHERE revoked_at IS NULL")[0][0]
    assert live >= 2  # the token layer would still let them through
    with pytest.raises(AgentError) as refused:
        _status(world, agent, started)
    assert refused.value.detail["code"] == -32041
    assert "delegation_revoked" in _denial_reasons(world)


def test_the_token_layer_alone_refuses_while_the_delegation_is_still_live(world) -> None:
    agent = world.agent()
    started = world.start_application(agent)
    jti = world.sql(
        "SELECT jti FROM agent_access_tokens WHERE token_hash = :h",
        h=hash_token(started["tokens"]["status_token"]),
    )[0][0]
    assert world.authz().revoke_token(jti, correlation_id="corr-test")
    assert world.sql("SELECT status FROM delegations") == [("ISSUED",)]
    with pytest.raises(AgentError) as refused:
        _status(world, agent, started)
    assert refused.value.detail["code"] == -32041
    assert "token_revoked" in _denial_reasons(world)


# --- M80: a signature is required by the contract gate and by the policy rule ------------------


def _missing(world: TrustWorld, application_id: str) -> list[str]:
    return world.audit_details("application.submit_denied", application_id)[-1]["missing"]


def _without_policy_blocks(service, facts):
    return {**regulatory_policy.evaluate(service, facts), "allowed": True, "blocking": []}


def test_with_a_package_and_no_signing_session_both_layers_refuse(world) -> None:
    agent, state = _ready(world)
    world.contract_form(state["browser"], state["application_id"])
    with pytest.raises(AgentError):
        _submit(agent, state)
    assert _missing(world, state["application_id"]) == [
        "contract_signature",
        "policy:SIGNATURE-LEVEL",
    ]


def test_a_pending_signature_is_not_a_signature_for_either_layer(world) -> None:
    agent, state = _ready(world)
    world.authorize_signing(state["browser"], state["application_id"])
    with pytest.raises(AgentError):
        _submit(agent, state)
    assert _missing(world, state["application_id"]) == [
        "contract_signature",
        "policy:SIGNATURE-LEVEL",
    ]


def test_the_contract_gate_alone_refuses_without_a_signature(world, monkeypatch) -> None:
    monkeypatch.setattr(trust_gates, "evaluate", _without_policy_blocks)
    agent, state = _ready(world)
    world.contract_form(state["browser"], state["application_id"])
    with pytest.raises(AgentError):
        _submit(agent, state)
    assert _missing(world, state["application_id"]) == ["contract_signature"]
    assert world.status_of(state["application_id"]) == "CONTRACT_PENDING"


def test_the_policy_rule_alone_refuses_without_a_signature(world, monkeypatch) -> None:
    monkeypatch.setattr(trust_gates, "signature_gate", lambda _uow, _application: ())
    agent, state = _ready(world)
    world.contract_form(state["browser"], state["application_id"])
    with pytest.raises(AgentError):
        _submit(agent, state)
    assert _missing(world, state["application_id"]) == ["policy:SIGNATURE-LEVEL"]
    assert world.status_of(state["application_id"]) == "CONTRACT_PENDING"


def test_a_signed_package_satisfies_both_layers(world) -> None:
    agent, state = _ready(world)
    world.sign(state["browser"], state["application_id"])
    assert _submit(agent, state)["state"] == "IN_PROGRESS"
    assert world.status_of(state["application_id"]) == "SUBMITTED_TO_DEPOT_PROVIDER"


# --- M87: a login's state belongs to the browser that began it ---------------------------------


def _login_service(world: TrustWorld) -> OidcLoginService:
    factory = world.app.state.session_factory
    return OidcLoginService(
        lambda: SqlAlchemyUnitOfWork(factory),
        world.settings,
        world.app.state.container.outbound_http,
        clock=world.clock,
    )


def test_a_browser_with_its_own_valid_flow_cannot_use_another_browsers_state(world) -> None:
    victim = TestClient(world.app, base_url=BANK)
    attacker = TestClient(world.app, base_url=BANK)
    victim_query = _begin(world, victim)
    _begin(world, attacker)  # the attacker holds a valid flow cookie of its own
    code = world.idp.issue_code(victim_query)
    denied = attacker.get(
        "/customer/oidc/callback", params={"code": code, "state": victim_query["state"]}
    )
    assert denied.status_code == 403
    assert _session_cookie(attacker) is None
    assert world.sql("SELECT COUNT(*) FROM customer_sessions") == [(0,)]


def test_a_refused_browser_does_not_use_up_the_real_browsers_login(world) -> None:
    victim = TestClient(world.app, base_url=BANK)
    attacker = TestClient(world.app, base_url=BANK)
    query = _begin(world, victim)
    _begin(world, attacker)
    code = world.idp.issue_code(query)
    attacker.get("/customer/oidc/callback", params={"code": code, "state": query["state"]})
    good = victim.get("/customer/oidc/callback", params={"code": code, "state": query["state"]})
    assert good.status_code == 200
    assert _session_cookie(victim)


def test_the_binding_check_is_what_refuses_a_valid_state_from_another_browser(world) -> None:
    service = _login_service(world)
    url, binding = service.begin()
    _, other_binding = service.begin()
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    code = world.idp.issue_code(query)
    with pytest.raises(OidcLoginError) as refused:
        service.complete(
            state=query["state"], code=code, issuer_parameter=None, browser_binding=other_binding
        )
    assert refused.value.reason == "browser_binding_invalid"
    verified = service.complete(
        state=query["state"], code=code, issuer_parameter=None, browser_binding=binding
    )
    assert verified.customer_ref == world.customer_ref(world.idp.next_subject)
