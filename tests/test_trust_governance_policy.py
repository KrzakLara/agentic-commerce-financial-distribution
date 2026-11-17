"""Areas 8 and 9: distribution attribution and the regulatory applicability matrix."""

from datetime import date
from itertools import product as cartesian

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.domain.enums import AuditActorType
from app.integrations.jose_tools import generate_key
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.repositories.trust_orm import PartnerRelationshipRow
from app.services.application_service import ApplicationService
from app.services.distribution_governance import (
    OUTCOME_CONFLICT,
    OUTCOME_DIRECT,
    OUTCOME_PARTNER,
    OUTCOME_RESOLVED,
    POLICY,
    ROLES,
    DistributionGovernanceService,
    commission_for,
    decide_attribution,
)
from app.services.regulatory_policy import (
    POLICY_VERSION,
    Applies,
    Gate,
    Service,
    applicability_matrix,
    evaluate,
)
from external_agent.client import AgentError, artifact_of
from scripts import customer_steps as steps
from tests.trust_harness import ALL_SCOPES, OFFER, TrustWorld, sign_referral, trust_world


@pytest.fixture
def world():
    with trust_world() as value:
        yield value


def _governance(world: TrustWorld) -> DistributionGovernanceService:
    factory = world.app.state.session_factory
    return DistributionGovernanceService(
        lambda: SqlAlchemyUnitOfWork(factory),
        ApplicationService(lambda: SqlAlchemyUnitOfWork(factory), clock=world.clock),
        world.app.state.container.catalog,
        referral_jwks=world.settings.partner_referral_jwks,
        audience=world.settings.public_base_url,
        clock=world.clock,
    )


def _started(world: TrustWorld, channel: str = "direct"):
    agent = world.agent()
    offer = {**OFFER, "channel_id": channel}
    pushed = world.delegate(agent, offer=offer)
    assert agent.poll_token(pushed["device_code"]) == "issued"
    started = artifact_of(
        agent.call("depot-onboarding", agent.access_token, action="start", **offer)
    )
    return agent, {
        "application_id": started["application_id"],
        "tokens": started["authorization"],
    }


def _refer(world, agent, started, token) -> dict:
    return agent.submit_referral(
        started["application_id"], started["tokens"]["onboarding_token"], token
    )


def _claims(world, application_id):
    return world.sql(
        "SELECT partner_id, source, status, reason FROM attribution_claims "
        "WHERE application_id = :a ORDER BY received_at, claim_id",
        a=application_id,
    )


def _latest(world, application_id):
    return world.sql(
        "SELECT sequence, outcome, partner_id, policy_version FROM attribution_decisions "
        "WHERE application_id = :a ORDER BY sequence DESC",
        a=application_id,
    )[0]


def _customer_finishes(world, agent, started, *, browser=None):
    browser = browser or world.browser("alice")
    link = world.request_link(agent, started)
    assert world.complete_proofing(browser, link).status_code == 200
    steps.complete_customer_information(browser, link)
    assert steps.post_confirmation(browser, link).status_code == 200
    return browser


# --- the example cases -------------------------------------------------------------------------


def test_new_customer_discovers_the_provider_directly(world) -> None:
    agent, started = _started(world)
    _customer_finishes(world, agent, started)
    application_id = started["application_id"]
    assert _latest(world, application_id)[1:3] == (OUTCOME_DIRECT, None)
    decision = _governance(world).latest_decision(application_id)
    assert decision["commission"]["status"] == "NOT_APPLICABLE"
    assert any("direct route" in line for line in decision["explanation"])


def test_existing_partner_customer_starts_through_an_ai_agent(world) -> None:
    """A verified relationship counts. The agent neither stated nor could state it."""
    world.login("alice")
    with SqlAlchemyUnitOfWork(world.app.state.session_factory) as uow:
        uow.trust.add(
            PartnerRelationshipRow(
                customer_ref=world.customer_ref("alice"),
                partner_id="sampletown-coop-bank",
                verified_source="synthetic partner register",
                created_at=world.clock(),
            )
        )
        uow.commit()
    agent, started = _started(world)
    _customer_finishes(world, agent, started)
    application_id = started["application_id"]
    assert _latest(world, application_id)[1:3] == (OUTCOME_PARTNER, "sampletown-coop-bank")
    assert ("sampletown-coop-bank", "VERIFIED_RELATIONSHIP", "ACCEPTED",
            "verified_existing_relationship") in _claims(world, application_id)  # fmt: skip


def test_a_partner_channel_chosen_by_the_customer_is_attributed(world) -> None:
    agent, started = _started(world, "partner-1")
    _customer_finishes(world, agent, started)
    decision = _governance(world).latest_decision(started["application_id"])
    assert decision["outcome"] == OUTCOME_PARTNER
    assert decision["commission"]["status"] == "CALCULATED_DEMONSTRATION_ONLY"
    assert decision["commission"]["amount_eur_cents"] == 1250
    assert decision["commission"]["inducement_review_required"] is True
    assert decision["commission"]["payable"] is False


def test_a_valid_signed_referral_is_attributed_with_its_evidence(world) -> None:
    agent, started = _started(world)
    answer = _refer(world, agent, started, sign_referral(world, "sampletown-coop-bank"))
    assert answer["outcome"] == OUTCOME_PARTNER
    row = world.sql("SELECT proof_digest, terms_version FROM attribution_claims")[0]
    assert len(row[0]) == 64 and row[1] == "sample-2026.11"


def test_referral_metadata_changing_halfway_does_not_silently_overwrite(world) -> None:
    agent, started = _started(world, "partner-1")
    application_id = started["application_id"]
    _customer_finishes(world, agent, started)
    first = _latest(world, application_id)
    assert first[1:3] == (OUTCOME_PARTNER, "sampletown-coop-bank")
    answer = _refer(world, agent, started, sign_referral(world, "riverside-sample-bank"))
    assert answer["outcome"] == OUTCOME_CONFLICT
    rows = world.sql(
        "SELECT sequence, outcome, partner_id FROM attribution_decisions "
        "WHERE application_id = :a ORDER BY sequence",
        a=application_id,
    )
    assert rows[0][1:] == (OUTCOME_PARTNER, "sampletown-coop-bank")  # the old decision is intact
    assert rows[-1][1:] == (OUTCOME_CONFLICT, None)


def test_two_partners_claiming_means_a_person_decides_and_nobody_is_paid(world) -> None:
    agent, started = _started(world)
    application_id = started["application_id"]
    _refer(world, agent, started, sign_referral(world, "sampletown-coop-bank"))
    _refer(world, agent, started, sign_referral(world, "riverside-sample-bank"))
    governance = _governance(world)
    decision = governance.latest_decision(application_id)
    assert decision["outcome"] == OUTCOME_CONFLICT
    assert decision["partner_id"] is None  # not in favour of the provider, and not of a partner
    assert decision["commission"] == {"status": "WITHHELD", "reason": "attribution_conflict"}

    with pytest.raises(Exception, match="not found"):  # noqa: PT011 - a person cannot invent one
        governance.resolve_by_person(
            operator_id="op-supervisor-1",
            application_id=application_id,
            partner_id="invented-partner",
            cid="c",
        )
    resolved = governance.resolve_by_person(
        operator_id="op-supervisor-1",
        application_id=application_id,
        partner_id="riverside-sample-bank",
        cid="c",
    )
    assert resolved["outcome"] == OUTCOME_RESOLVED
    assert resolved["decided_by"] == "op-supervisor-1"
    assert resolved["commission"] == {
        "status": "NOT_ELIGIBLE",
        "reason": "product_not_in_the_contract",
    }


def test_a_conflict_does_not_stop_the_customers_account(world) -> None:
    agent, started = _started(world)
    application_id = started["application_id"]
    _refer(world, agent, started, sign_referral(world, "sampletown-coop-bank"))
    _refer(world, agent, started, sign_referral(world, "riverside-sample-bank"))
    browser = _customer_finishes(world, agent, started)
    world.sign(browser, application_id)
    artifact_of(
        agent.call(
            "depot-onboarding",
            started["tokens"]["onboarding_token"],
            action="submit",
            application_id=application_id,
        )
    )
    assert world.provider_opens(application_id).status_code == 200
    assert world.status_of(application_id) == "OPENED"
    assert _latest(world, application_id)[1] == OUTCOME_CONFLICT


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (
            lambda w: sign_referral(w, "sampletown-coop-bank", key=generate_key("partner-a")),
            "forged_referral",
        ),
        (
            lambda w: sign_referral(w, "not-a-partner", key=generate_key("x")),
            "unknown_partner",
        ),
        (
            lambda w: sign_referral(w, "sampletown-coop-bank", terms="sample-2024.01"),
            "terms_outdated",
        ),
        (
            lambda w: sign_referral(w, "sampletown-coop-bank", lifetime=-10),
            "referral_expired",
        ),
        (
            lambda w: sign_referral(w, "sampletown-coop-bank", product="depot-premium"),
            "wrong_product",
        ),
        (
            lambda w: sign_referral(w, "example-valley-savings-bank", terms="sample-2025.01"),
            "no_current_contract",
        ),
    ],
)
def test_an_invalid_referral_is_rejected_and_recorded(world, build, reason) -> None:
    agent, started = _started(world)
    answer = _refer(world, agent, started, build(world))
    assert answer["outcome"] == OUTCOME_DIRECT
    claim = _claims(world, started["application_id"])[0]
    assert claim[2:] == ("REJECTED", reason)


def test_a_replayed_referral_is_rejected(world) -> None:
    agent, started = _started(world)
    token = sign_referral(world, "sampletown-coop-bank")
    _refer(world, agent, started, token)
    _refer(world, agent, started, token)
    assert {c[2:] for c in _claims(world, started["application_id"])} == {
        ("ACCEPTED", "valid_signed_referral"),
        ("REJECTED", "referral_replayed"),
    }


def test_an_agent_cannot_name_a_partner_or_a_rule(world) -> None:
    agent, started = _started(world)
    url = f"https://bank.test/v1/applications/{started['application_id']}/referrals"
    token = started["tokens"]["onboarding_token"]
    answer = world.bank.post(
        f"/v1/applications/{started['application_id']}/referrals",
        json={"partner_id": "my-partner", "commission": 100},
        headers={"Authorization": f"DPoP {token}", "DPoP": agent._dpop("POST", url, token)},
    )
    assert answer.status_code == 422
    assert world.sql("SELECT COUNT(*) FROM attribution_claims") == [(0,)]


def test_a_status_token_cannot_carry_a_referral(world) -> None:
    agent, started = _started(world)
    with pytest.raises(Exception):  # noqa: B017, PT011 - the bank's 403 surfaces as an error
        agent.submit_referral(
            started["application_id"],
            started["tokens"]["status_token"],
            sign_referral(world, "sampletown-coop-bank"),
        )
    assert world.sql("SELECT COUNT(*) FROM attribution_claims") == [(0,)]


def test_a_referral_for_another_customers_application_looks_missing(world) -> None:
    agent_a, started_a = _started(world)
    agent_b = world.agent()
    pushed = world.delegate(agent_b, "bob")
    assert agent_b.poll_token(pushed["device_code"]) == "issued"
    started_b = artifact_of(
        agent_b.call("depot-onboarding", agent_b.access_token, action="start", **OFFER)
    )
    with pytest.raises(AgentError):
        agent_b.submit_referral(
            started_a["application_id"],
            started_b["authorization"]["onboarding_token"],
            sign_referral(world, "sampletown-coop-bank"),
        )


def test_decisions_and_claims_are_append_only(world) -> None:
    agent, started = _started(world)
    _refer(world, agent, started, sign_referral(world, "sampletown-coop-bank"))
    for statement in (
        "UPDATE attribution_decisions SET partner_id = 'x'",
        "DELETE FROM attribution_decisions",
        "UPDATE attribution_claims SET status = 'ACCEPTED'",
    ):
        with pytest.raises(IntegrityError), world.app.state.engine.begin() as connection:
            connection.execute(text(statement))


def test_each_decision_records_its_inputs_and_policy_version(world) -> None:
    agent, started = _started(world)
    _refer(world, agent, started, sign_referral(world, "sampletown-coop-bank"))
    assert _latest(world, started["application_id"])[3] == POLICY.version
    details = world.audit_details("attribution.decided", started["application_id"])[0]
    assert details["policy_version"] == POLICY.version


# --- the pure rules ----------------------------------------------------------------------------


TODAY = date(2026, 11, 3)


def _claim(partner, source="SIGNED_REFERRAL", status="ACCEPTED", reason="ok"):
    return {"partner_id": partner, "source": source, "status": status, "reason": reason}


def test_the_decision_is_deterministic_and_order_independent() -> None:
    claims = [_claim("a"), _claim("b"), _claim(None, status="REJECTED", reason="x")]
    first = decide_attribution(claims=claims, product_id="p", policy=POLICY, today=TODAY)
    second = decide_attribution(
        claims=list(reversed(claims)), product_id="p", policy=POLICY, today=TODAY
    )
    assert first["outcome"] == second["outcome"] == OUTCOME_CONFLICT
    assert first["partner_id"] == second["partner_id"] is None


def test_no_combination_of_claims_ever_attributes_without_an_accepted_claim() -> None:
    options = [
        _claim("a"),
        _claim("b"),
        _claim("a", status="REJECTED"),
        _claim(None, status="REJECTED"),
    ]
    for size in range(5):
        for claims in cartesian(options, repeat=size):
            result = decide_attribution(
                claims=list(claims), product_id="depot-basic", policy=POLICY, today=TODAY
            )
            accepted = {c["partner_id"] for c in claims if c["status"] == "ACCEPTED"}
            if result["partner_id"] is not None:
                assert accepted == {result["partner_id"]}
            elif len(accepted) > 1:
                assert result["commission"]["status"] == "WITHHELD"


def test_commission_is_a_labelled_demonstration_from_a_current_contract() -> None:
    assert commission_for("sampletown-coop-bank", "depot-basic", POLICY, TODAY)["payable"] is False
    assert (
        commission_for("sampletown-coop-bank", "other", POLICY, TODAY)["status"] == "NOT_ELIGIBLE"
    )
    assert (
        commission_for("example-valley-savings-bank", "depot-basic", POLICY, TODAY)["reason"]
        == "no_current_contract"
    )
    assert commission_for("nobody", "depot-basic", POLICY, TODAY)["status"] == "NOT_ELIGIBLE"


def test_the_roles_stay_separate() -> None:
    assert set(ROLES) == {
        "product_provider",
        "depot_operator",
        "distributor_or_partner_bank",
        "advising_party",
        "agent_technology_provider",
        "customer",
    }
    assert "Never receives attribution" in ROLES["agent_technology_provider"]


# --- the regulatory matrix ---------------------------------------------------------------------


def _rule(result: dict, rule_id: str) -> dict:
    return next(rule for rule in result["rules"] if rule["rule_id"] == rule_id)


def test_opening_an_account_does_not_trigger_the_appropriateness_test() -> None:
    result = evaluate(Service.ACCOUNT_OPENING, {})
    rule = _rule(result, "APPROPRIATENESS")
    assert rule["applies"] == Applies.NOT_TRIGGERED
    assert rule["gate"] == Gate.NONE
    assert "WpHG section 63(10)" in rule["source"]


def test_a_non_advised_order_needs_the_test_but_warns_and_never_rejects() -> None:
    result = evaluate(Service.ORDER_NON_ADVISED, {"assessment_outcome": "WARNING_REQUIRED"})
    rule = _rule(result, "APPROPRIATENESS")
    assert rule["applies"] == Applies.REQUIRED and rule["gate"] == Gate.WARNING
    assert result["allowed"] is True and "APPROPRIATENESS" in result["warnings"]


def test_execution_only_is_never_assumed() -> None:
    all_stated = {
        "non_complex_instrument": True,
        "customer_initiative": True,
        "prior_notice": True,
        "conflicts": True,
    }
    rule = _rule(evaluate(Service.ORDER_EXECUTION_ONLY, all_stated), "APPROPRIATENESS")
    assert rule["applies"] == Applies.UNRESOLVED and rule["gate"] == Gate.HUMAN
    assert rule["legal_confirmation_required"] is True
    assert rule["reason_code"] == "execution_only_conditions_stated_compliance_must_confirm"
    partial = _rule(evaluate(Service.ORDER_EXECUTION_ONLY, {}), "APPROPRIATENESS")
    assert partial["reason_code"] == "execution_only_conditions_not_all_stated"


@pytest.mark.parametrize("service", [Service.INVESTMENT_ADVICE, Service.PORTFOLIO_MANAGEMENT])
def test_advice_is_separate_and_not_offered(service) -> None:
    result = evaluate(service, {})
    rule = _rule(result, "SUITABILITY")
    assert rule["applies"] == Applies.NOT_OFFERED and result["allowed"] is False


def test_identification_is_a_real_gate_for_account_opening_only() -> None:
    missing = evaluate(Service.ACCOUNT_OPENING, {"tax_information": True, "stage": "consent"})
    assert missing["blocking"] == ["AML-IDENTIFICATION"] and not missing["allowed"]
    rule = _rule(evaluate(Service.PRODUCT_INFORMATION, {}), "AML-IDENTIFICATION")
    assert rule["applies"] == Applies.NOT_TRIGGERED


def test_a_synthetic_result_is_never_labelled_as_real_evidence() -> None:
    facts = {
        "identity_proofing": "VERIFIED",
        "regulatory_evidence": False,
        "tax_information": True,
        "contract_signature": "SIGNED",
    }
    result = evaluate(Service.ACCOUNT_OPENING, facts)
    assert result["allowed"] is True
    assert _rule(result, "AML-IDENTIFICATION")["evidence"] == "SATISFIED_SYNTHETIC"
    assert _rule(result, "SIGNATURE-LEVEL")["evidence"] == "SATISFIED_SYNTHETIC"
    assert "SIGNATURE-LEVEL" in result["unresolved"]
    assert "not legal advice" in result["statement"]


def test_a_model_as_the_decider_is_refused() -> None:
    result = evaluate(Service.ACCOUNT_OPENING, {"decision_maker": "llm"})
    assert "NO-AI-COMPLIANCE-DECISION" in result["blocking"]


def test_attribution_with_a_partner_goes_to_an_inducement_review() -> None:
    result = evaluate(Service.ACCOUNT_OPENING, {"partner_attributed": True})
    rule = _rule(result, "INDUCEMENTS")
    assert rule["applies"] == Applies.UNRESOLVED and rule["gate"] == Gate.HUMAN
    assert "INDUCEMENTS" in result["human_review"]
    assert "WpHG section 70" in rule["source"]


def test_the_evaluation_is_deterministic_and_every_rule_names_a_source() -> None:
    facts = {"identity_proofing": "VERIFIED"}
    assert evaluate(Service.ACCOUNT_OPENING, facts) == evaluate(Service.ACCOUNT_OPENING, facts)
    for row in applicability_matrix():
        assert row["policy_version"] == POLICY_VERSION
        for rule in row["rules"]:
            assert rule["source"] and rule["reason_code"]


def test_each_service_is_in_the_matrix() -> None:
    assert {row["service"] for row in applicability_matrix()} == {s.value for s in Service}


def test_the_journey_records_policy_decisions_with_their_version(world) -> None:
    agent, started = _started(world)
    application_id = started["application_id"]
    browser = _customer_finishes(world, agent, started)
    world.sign(browser, application_id)
    artifact_of(
        agent.call(
            "depot-onboarding",
            started["tokens"]["onboarding_token"],
            action="submit",
            application_id=application_id,
        )
    )
    rows = world.sql(
        "SELECT stage, policy_version, allowed FROM policy_decisions "
        "WHERE application_id = :a ORDER BY decided_at",
        a=application_id,
    )
    assert [row[0] for row in rows] == ["consent", "submit"]
    assert {row[1] for row in rows} == {POLICY_VERSION}
    assert all(row[2] for row in rows)


def test_a_missing_mandatory_result_refuses_the_submission(world) -> None:
    """The submit gate reads recorded evidence. A deleted result fails closed."""
    agent, started = _started(world)
    application_id = started["application_id"]
    browser = _customer_finishes(world, agent, started)
    world.sign(browser, application_id)
    with world.app.state.engine.begin() as connection:
        connection.execute(text("DELETE FROM identity_proofing_cases"))
    with pytest.raises(AgentError) as refused:
        agent.call(
            "depot-onboarding",
            started["tokens"]["onboarding_token"],
            action="submit",
            application_id=application_id,
        )
    assert refused.value.detail["data"]["code"] == "prerequisites_not_met"
    denied = world.audit_details("application.submit_denied", application_id)[-1]
    assert "identity_proofing" in denied["missing"]


def test_policy_decisions_are_append_only(world) -> None:
    agent, started = _started(world)
    _customer_finishes(world, agent, started)
    with pytest.raises(IntegrityError), world.app.state.engine.begin() as connection:
        connection.execute(text("UPDATE policy_decisions SET allowed = 1"))


def test_the_scope_list_of_the_harness_matches_the_delegable_set() -> None:
    from app.services.delegation_service import DELEGABLE_SCOPES

    assert frozenset(ALL_SCOPES.split()) == DELEGABLE_SCOPES
    assert AuditActorType.AGENT.value == "agent"


# --- the business example of the presentation ---------------------------------------------------


def test_the_channel_conflict_business_example_end_to_end(world) -> None:
    """A partner customer arrives through her own agent, a second partner claims her, the account
    continues, a forgery and a machine are refused, and only a person settles the attribution."""
    from app.services.review_service import OperatorNotPermittedError

    # 1. Alice has an existing relationship with Sampletown Cooperative Bank (synthetic register).
    world.login("alice")
    with SqlAlchemyUnitOfWork(world.app.state.session_factory) as uow:
        uow.trust.add(
            PartnerRelationshipRow(
                customer_ref=world.customer_ref("alice"),
                partner_id="sampletown-coop-bank",
                verified_source="synthetic partner register",
                created_at=world.clock(),
            )
        )
        uow.commit()
    # 2. Later her independent agent opens the depot through the direct digital channel.
    agent, started = _started(world)
    application_id = started["application_id"]
    _customer_finishes(world, agent, started)
    assert _latest(world, application_id)[1:3] == (OUTCOME_PARTNER, "sampletown-coop-bank")

    # 3. A forged referral naming Sampletown is rejected and recorded; it changes nothing.
    forged = sign_referral(world, "sampletown-coop-bank", key=generate_key("forger"))
    assert _refer(world, agent, started, forged)["outcome"] == OUTCOME_PARTNER
    # 4. The agent then carries a valid signed referral of a second partner: a genuine conflict.
    conflict = _refer(world, agent, started, sign_referral(world, "riverside-sample-bank"))
    assert conflict["outcome"] == OUTCOME_CONFLICT
    governance = _governance(world)
    decision = governance.latest_decision(application_id)
    assert decision["partner_id"] is None
    assert decision["commission"] == {"status": "WITHHELD", "reason": "attribution_conflict"}
    claims = _claims(world, application_id)
    assert ("sampletown-coop-bank", "ACCEPTED") in {(c[0], c[2]) for c in claims}
    assert ("riverside-sample-bank", "ACCEPTED") in {(c[0], c[2]) for c in claims}
    assert ("sampletown-coop-bank", "REJECTED", "forged_referral") in {
        (c[0], c[2], c[3]) for c in claims
    }
    # 5. The conflict does not stop the customer's account.
    assert world.status_of(application_id) == "CONTRACT_PENDING"

    # 6. Neither a model nor the agent can settle it; a person cannot invent a partner.
    for machine in ("claude-sonnet-5-5", "personal-agent-demo"):
        with pytest.raises(OperatorNotPermittedError):
            governance.resolve_by_person(
                operator_id=machine,
                application_id=application_id,
                partner_id="sampletown-coop-bank",
                cid="c",
            )
    with pytest.raises(Exception, match="not found"):  # noqa: PT011
        governance.resolve_by_person(
            operator_id="op-supervisor-1",
            application_id=application_id,
            partner_id="invented-partner",
            cid="c",
        )
    # 7. A person decides among the accepted claims: the existing relationship.
    resolved = governance.resolve_by_person(
        operator_id="op-supervisor-1",
        application_id=application_id,
        partner_id="sampletown-coop-bank",
        cid="c",
    )
    assert resolved["outcome"] == OUTCOME_RESOLVED
    assert resolved["decided_by"] == "op-supervisor-1"
    # 8. Technical attribution is not a commission entitlement: nothing becomes payable.
    assert resolved["commission"]["status"] == "CALCULATED_DEMONSTRATION_ONLY"
    assert resolved["commission"]["payable"] is False
    assert resolved["commission"]["inducement_review_required"] is True

    # 9. The evidence stays: every decision in order, each with its policy version.
    history = world.sql(
        "SELECT outcome, policy_version FROM attribution_decisions "
        "WHERE application_id = :a ORDER BY sequence",
        a=application_id,
    )
    assert [row[0] for row in history][-3:] == [OUTCOME_PARTNER, OUTCOME_CONFLICT, OUTCOME_RESOLVED]
    assert all(row[1] for row in history)
    decided = world.sql(
        "SELECT COUNT(*) FROM audit_events "
        "WHERE application_id = :a AND event_type = 'attribution.decided'",
        a=application_id,
    )[0][0]
    assert decided >= 3
