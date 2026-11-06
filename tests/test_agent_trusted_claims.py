"""Approved product facts at the agent's tool boundary, and what they do not cover."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from app.agents import ScriptedCustomerAgent
from app.agents.product_information import AnswerKind, ProductInformationAgent, ProductQuestion
from app.integrations.agent_credentials import AgentCredentials
from app.integrations.agent_tools import AgentToolCall, AgentToolName
from app.integrations.catalog_fixture import (
    DEFAULT_FIXTURE,
    FixtureProviderDiscovery,
    prompt_injection_fixture,
    republish,
    tampered_copy,
)
from app.integrations.rest_gateway import RestAgentToolGateway
from app.services.bank_claims import SAFE_RESPONSE, approved_claims
from tests.flow import Flow

ROOT = Path(__file__).resolve().parent.parent
PROVIDER, PRODUCT = "fixture-bank", "depot-basic"
CLAIMS = f"/v1/providers/{PROVIDER}/products/{PRODUCT}/approved-claims"
PRICE_LIST = "pli-depot-basic"


def gateway(flow: Flow) -> RestAgentToolGateway:
    return RestAgentToolGateway(flow.agent_http, credentials=AgentCredentials())


def agent(flow: Flow) -> ProductInformationAgent:
    return ProductInformationAgent(gateway(flow))


def by_kind(flow: Flow) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for claim in flow.agent_http.get(CLAIMS).json()["claims"]:
        grouped.setdefault(claim["kind"], []).append(claim)
    return grouped


def with_catalog(flow: Flow, change: Callable[[dict], None] | None = None) -> None:
    fixture = tampered_copy(DEFAULT_FIXTURE)
    if change is not None:
        change(fixture.manifests[PROVIDER])
    republish(fixture, PROVIDER)
    flow.container.catalog = FixtureProviderDiscovery(fixture)


# --- the bank's published claims ------------------------------------------------------------------


def test_every_published_claim_cites_its_source_and_the_product_version(flow: Flow) -> None:
    claims = by_kind(flow)
    assert set(claims) == {
        "PROVIDER_IDENTITY", "PARTNER_ATTRIBUTION", "RISK_SUMMARY", "FEE", "CONDITIONS",
    }  # fmt: skip
    custody = next(c for c in claims["FEE"] if c["subject"] == "custody")
    assert custody["value"] == "0.00 EUR p.a."
    assert custody["product_version"] == "2026.11"
    assert custody["source"]["document_id"] == PRICE_LIST
    assert len(custody["source"]["document_digest"]) == 64
    assert custody["decision"] == "APPROVED" and custody["mock"] is True
    assert "verified synthetic price list" in custody["statement"]
    assert "the custody fee of depot-basic is 0.00 EUR p.a." in custody["statement"]
    identity = claims["PROVIDER_IDENTITY"][0]
    assert identity["source"]["document_id"] == "provider-manifest"
    assert {c["subject"] for c in claims["PARTNER_ATTRIBUTION"]} == {"partner-1"}


def test_an_unknown_product_has_no_claims_and_nothing_is_invented(flow: Flow) -> None:
    missing = flow.agent_http.get(f"/v1/providers/{PROVIDER}/products/depot-gold/approved-claims")
    assert missing.status_code == 404
    assert approved_claims(flow.container.catalog, "no-such-bank", PRODUCT) == []
    assert approved_claims(flow.container.catalog, PROVIDER, "depot-gold") == []


def test_hostile_catalog_text_never_becomes_an_approved_claim(flow: Flow) -> None:
    flow.container.catalog = FixtureProviderDiscovery(prompt_injection_fixture())
    claims = by_kind(flow)
    assert "CONDITIONS" not in claims and "RISK_SUMMARY" not in claims
    for group in claims.values():
        for claim in group:
            assert "SYSTEM" not in claim["statement"]
            assert "ignore" not in claim["statement"].lower()


def test_an_expired_manifest_publishes_no_claims(flow: Flow) -> None:
    flow.clock.advance(days=4000)
    assert flow.agent_http.get(CLAIMS).status_code == 503
    fee = {
        "kind": "FEE",
        "provider_id": PROVIDER,
        "product_id": PRODUCT,
        "subject": "custody",
        "text": "0.00 EUR p.a.",
        "source": flow_source(flow),
    }
    assert flow.agent_http.post("/v1/claims/verify", json=fee).json()["decision"] == (
        "ESCALATE_TO_HUMAN"
    )


def flow_source(flow: Flow) -> dict:
    product = next(iter(flow.container.catalog.list_products(PROVIDER)))
    return {
        "document_id": product.sources[0].document_id,
        "document_version": product.sources[0].document_version,
        "document_digest": product.sources[0].document_digest,
    }


# --- the claim check ------------------------------------------------------------------------------


def check(flow: Flow, **claim: object) -> dict:
    response = flow.agent_http.post(
        "/v1/claims/verify", json={"provider_id": PROVIDER, "product_id": PRODUCT, **claim}
    )
    assert response.status_code == 200
    return response.json()


def test_an_exact_structured_claim_is_approved(flow: Flow) -> None:
    verdict = check(
        flow, kind="FEE", subject="custody", text="0.00 EUR p.a.", source=flow_source(flow)
    )
    assert verdict == {"decision": "APPROVED", "reason": "matches_the_verified_catalog",
                       "safe_response": None}  # fmt: skip


def test_a_changed_value_a_missing_source_or_an_unknown_fee_is_not_approved(flow: Flow) -> None:
    source = flow_source(flow)
    assert check(flow, kind="FEE", subject="custody", text="0 EUR", source=source)["decision"] == (
        "UNSUPPORTED"
    )
    assert check(flow, kind="FEE", subject="custody", text="0.00 EUR p.a.")["decision"] == (
        "UNSUPPORTED"
    )
    forged = {**source, "document_digest": "0" * 64}
    assert (
        check(flow, kind="FEE", subject="custody", text="0.00 EUR p.a.", source=forged)["decision"]
        == "UNSUPPORTED"
    )
    assert check(flow, kind="FEE", subject="closing", text="9 EUR", source=source)["decision"] == (
        "ESCALATE_TO_HUMAN"
    )


def test_the_examples_from_the_brief_are_not_approved(flow: Flow) -> None:
    for text in (
        "This investment is guaranteed to be safe.",
        "This bank will definitely approve your account.",
    ):
        for kind in ("FREE_TEXT", "CONDITIONS", "FEE"):
            verdict = check(flow, kind=kind, text=text, source=flow_source(flow))
            assert verdict["decision"] == "ESCALATE_TO_HUMAN", (kind, text)
            assert verdict["safe_response"] == SAFE_RESPONSE


def test_free_text_is_never_an_official_claim(flow: Flow) -> None:
    verdict = check(flow, kind="FREE_TEXT", text="The custody fee is 0.00 EUR p.a.")
    assert verdict["decision"] == "UNSUPPORTED"


def test_the_safe_answer_promises_no_contact(flow: Flow) -> None:
    assert "will contact" not in SAFE_RESPONSE and "can help you" not in SAFE_RESPONSE
    assert (
        flow.agent_http.post(
            "/v1/claims/verify",
            json={"kind": "FEE", "provider_id": PROVIDER, "text": "x", "unknown": "field"},
        ).status_code
        == 422
    )


# --- the agent's tool boundary --------------------------------------------------------------------


def test_the_tool_result_carries_structured_claims_and_no_narrative(flow: Flow) -> None:
    result = gateway(flow).execute(
        AgentToolCall(
            tool=AgentToolName.GET_OFFICIAL_PRODUCT,
            arguments={"provider_id": PROVIDER, "product_id": PRODUCT},
        )
    )
    claims = result.data["approved_claims"]
    assert {c["kind"] for c in claims} == {"FEE", "PROVIDER_IDENTITY", "PARTNER_ATTRIBUTION"}
    assert not {"conditions", "risk_summary"} & result.data.keys()
    dumped = json.dumps(claims)
    assert "no minimum deposit" not in dumped and "can lose value" not in dumped
    assert all(c["source"]["document_digest"] and c["decision"] == "APPROVED" for c in claims)


def test_the_answer_comes_from_the_approved_claim(flow: Flow) -> None:
    answer = agent(flow).answer(PROVIDER, PRODUCT, ProductQuestion.FEE, "custody")
    assert answer.kind is AnswerKind.APPROVED_FACT
    assert answer.product_version == "2026.11"
    assert answer.source is not None and answer.source["document_id"] == PRICE_LIST
    assert "0.00 EUR p.a." in answer.statement


def test_partner_attribution_and_identity_are_answered_with_their_source(flow: Flow) -> None:
    partner = agent(flow).answer(PROVIDER, PRODUCT, ProductQuestion.PARTNER, "partner-1")
    assert partner.kind is AnswerKind.APPROVED_FACT
    assert "Sampletown Cooperative Bank eG (MOCK)" in partner.statement
    assert partner.source is not None and partner.source["document_id"] == "provider-manifest"
    identity = agent(flow).answer(PROVIDER, PRODUCT, ProductQuestion.PROVIDER_IDENTITY)
    assert "Example Depot Bank AG (MOCK)" in identity.statement


def test_a_fact_the_source_does_not_contain_is_declined(flow: Flow) -> None:
    for question, subject in (
        (ProductQuestion.FEE, "account-closing"),
        (ProductQuestion.PARTNER, "partner-evil"),
    ):
        answer = agent(flow).answer(PROVIDER, PRODUCT, question, subject)
        assert answer.kind is AnswerKind.NOT_IN_APPROVED_SOURCES
        assert answer.source is None


class _CountingGateway:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, call: AgentToolCall) -> None:  # pragma: no cover - must never run
        self.calls += 1
        raise AssertionError(call)


def test_sensitive_questions_go_to_a_person_without_any_tool_call() -> None:
    counting = _CountingGateway()
    information = ProductInformationAgent(counting)  # type: ignore[arg-type]
    for question in (
        ProductQuestion.GUARANTEE,
        ProductQuestion.PERSONAL_RECOMMENDATION,
        ProductQuestion.APPROVAL_OUTCOME,
    ):
        answer = information.answer(PROVIDER, PRODUCT, question)
        assert answer.kind is AnswerKind.ESCALATE_TO_HUMAN
        assert "ask for a person" in answer.statement
    assert counting.calls == 0


def test_narrative_text_is_left_to_the_bank_page(flow: Flow) -> None:
    answer = agent(flow).answer(PROVIDER, PRODUCT, ProductQuestion.CONDITIONS_OR_RISK)
    assert answer.kind is AnswerKind.SEE_BANK_PAGE


def test_a_comparison_needs_two_approved_facts_and_ranks_nothing(flow: Flow) -> None:
    single = agent(flow).compare_fees(PROVIDER, (PRODUCT,), "custody")
    assert single.comparable is False and "does not support" in single.note

    def second_product(manifest: dict) -> None:
        extra = {
            **manifest["products"][0],
            "product_id": "depot-plus",
            "name": "Depot Plus (MOCK)",
            "fees": [
                {"code": "custody", "label": "Custody fee (MOCK)", "amount": "12.00 EUR p.a."}
            ],
        }
        manifest["products"].append(extra)

    with_catalog(flow, second_product)
    both = agent(flow).compare_fees(PROVIDER, (PRODUCT, "depot-plus"), "custody")
    assert both.comparable is True and "not a recommendation" in both.note
    amounts = [row.answer.statement for row in both.rows]
    assert "0.00 EUR p.a." in amounts[0] and "12.00 EUR p.a." in amounts[1]
    missing = agent(flow).compare_fees(PROVIDER, (PRODUCT, "depot-plus"), "closing")
    assert missing.comparable is False


def test_the_scripted_journey_still_works_with_claims_in_the_tool_result(flow: Flow) -> None:
    started = flow.start()
    assert started.application_id
    assert isinstance(flow.agent(), ScriptedCustomerAgent)


def test_the_llm_agent_module_is_still_the_evaluated_one() -> None:
    """Approved claims reach the LLM through a tool result, so the recorded digest must not move."""
    recorded = json.loads(
        (ROOT / "docs/evaluation/phase-05-live-results.json").read_text(encoding="utf-8")
    )
    digest = hashlib.sha256((ROOT / "app/agents/llm.py").read_bytes()).hexdigest()
    assert digest in json.dumps(recorded)
