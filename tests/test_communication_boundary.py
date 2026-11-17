"""The communication boundary: a model proposes, the bank's server decides and writes the answer.

Everything runs through the real routes and the real adapter. A fake client plays only the model, so
these tests fail if the bank's own checks fail. No paid model is called anywhere here.
"""

import ast
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.lib._parse._transform import transform_schema
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.agents.answer_proposer import DeterministicProposer, UnavailableProposer
from app.agents.factory import answer_model_client, build_answer_proposer
from app.agents.llm import PINNED_MODEL
from app.agents.llm_answer_proposer import AnswerProposalAgent, LlmAnswerProposer
from app.container import build_default_container
from app.core.config import Environment, Settings
from app.integrations.agent_tools import AgentToolCall, AgentToolName, AgentVisibleStatus
from app.integrations.catalog_fixture import (
    HOSTILE_INSTRUCTIONS,
    FixtureProviderDiscovery,
    ManifestRejected,
    prompt_injection_fixture,
)
from app.main import create_app
from app.services.communication_contract import (
    AnswerOutcome,
    ClaimType,
    GeneratedBy,
    Intent,
    ModelProposal,
    NotEstablishedTopic,
    ProcessStep,
    QuestionRequest,
    ReasonCode,
    proposal_json_schema,
)
from app.services.regulated_communication import (
    NOT_ESTABLISHED_TOPICS,
    PROCESS_EXPLANATIONS,
    CommunicationGuard,
)
from app.services.verified_facts import build_fact_lookup, fee_is_stated_in
from app.services.verified_llm import QuestionBudget, VerifiedFactsGateway, VerifiedLlmAgent
from external_agent.client import AgentError, artifact_of
from scripts.communication_eval_cases import (
    PRICE_LIST,
    build_catalog_fixture,
    call_tool,
    claim,
    proposes,
    read_product,
    read_provider,
)
from scripts.eval_communication import NOW, PlannedClient
from tests.flow import FakeClock
from tests.trust_harness import trust_world

ROOT = Path(__file__).resolve().parent.parent
GENERATED = GeneratedBy(proposer="test")
PROVIDER = "fixture-bank"
PRODUCT = "depot-basic"


def _guard(catalog: str = "default", days_later: int = 0) -> CommunicationGuard:
    fixture = build_catalog_fixture(catalog)
    return CommunicationGuard(
        FixtureProviderDiscovery(fixture, clock=lambda: NOW),
        lambda: NOW + timedelta(days=days_later),
    )


def _pipeline(plan: tuple[Any, ...], catalog: str = "default", days_later: int = 0, budget=None):
    guard = _guard(catalog, days_later)
    client = PlannedClient(plan)
    gateway = VerifiedFactsGateway(guard)
    proposer = LlmAnswerProposer(
        AnswerProposalAgent(client, gateway, proposal_schema=proposal_json_schema())
    )
    return VerifiedLlmAgent(guard, proposer, budget=budget), client, gateway


def _ask(agent: VerifiedLlmAgent, question: str = "How much is an order?"):
    return agent.answer(
        QuestionRequest(question=question, provider_id=PROVIDER, product_ids=(PRODUCT,))
    )


def _proposal(**fields: Any) -> ModelProposal:  # noqa: ANN401
    return ModelProposal.model_validate_json(json.dumps(proposes(**fields)[1]))


# --- the structure: nothing but the guard writes an answer ----------------------------------------


def _python_files(*folders: str) -> list[Path]:
    return [path for folder in folders for path in (ROOT / folder).rglob("*.py")]


def test_only_the_guard_builds_a_regulated_answer() -> None:
    builders = []
    for path in _python_files("app", "scripts"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "RegulatedAnswer"
            ):
                builders.append(path.name)
    assert builders == ["regulated_communication.py"]


def test_no_route_can_reach_a_model_except_through_the_pipeline() -> None:
    forbidden = {
        "app.agents.llm",
        "app.agents.llm_answer_proposer",
        "app.agents.answer_proposer",
        "anthropic",
    }
    for path in _python_files("app/api"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            assert not forbidden.intersection(modules), (path.name, modules)


def test_the_proposal_has_no_field_a_sentence_could_go_into() -> None:
    schema = proposal_json_schema()
    assert set(schema["properties"]) == {
        "intent",
        "product_ids",
        "claims",
        "process_step",
        "not_established_topics",
        "document_id",
        "document_version",
    }
    fields = schema["properties"]["claims"]["items"]["properties"]
    assert set(fields) == {
        "claim_id",
        "product_id",
        "claim_type",
        "field",
        "value",
        "source_id",
        "source_version",
        "source_digest",
    }
    assert schema["additionalProperties"] is False
    assert schema["properties"]["claims"]["items"]["additionalProperties"] is False


def test_the_schema_uses_only_what_the_api_accepts_and_matches_the_pydantic_model() -> None:
    schema = proposal_json_schema()
    assert transform_schema(schema) == schema
    text = json.dumps(schema)
    for keyword in ("minLength", "maxLength", "minimum", "maximum", "maxItems", "pattern"):
        assert keyword not in text
    assert set(ModelProposal.model_fields) == set(schema["required"])
    example = proposes(claims=[claim("order", value="4.90 EUR")])[1]
    assert ModelProposal.model_validate(example).claims[0].value == "4.90 EUR"


def test_a_proposal_with_any_added_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelProposal.model_validate({**proposes()[1], "answer_text": "Hello"})
    broken = {**claim("order"), "note": "guaranteed"}
    with pytest.raises(ValidationError):
        ModelProposal.model_validate(proposes(claims=[broken])[1])


# --- the adapter: the real class, only the API client is fake -------------------------------------


def test_the_adapter_requests_the_pinned_model_read_only_tools_and_the_proposal_format() -> None:
    agent, client, _ = _pipeline((proposes(claims=[claim("order", value="4.90 EUR")]),))
    _ask(agent)
    request = client.messages.requests[0]
    assert request["model"] == PINNED_MODEL
    assert {tool["name"] for tool in request["tools"]} == {
        "discover_provider",
        "get_official_product",
    }
    assert all(tool["strict"] is True for tool in request["tools"])
    assert request["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["output_config"]["format"]["schema"] == proposal_json_schema()
    question = json.loads(request["messages"][0]["content"])
    assert question["customer_question"] == "How much is an order?"


def test_a_tool_outside_the_list_is_refused_by_the_adapter_and_again_by_the_gateway() -> None:
    plan = (
        call_tool("submit_after_consent", application_id="app-1"),
        proposes(claims=[claim("order", value="4.90 EUR")]),
    )
    agent, client, gateway = _pipeline(plan)
    result = _ask(agent)
    assert result.answer.outcome is AnswerOutcome.REQUIRES_HUMAN_REVIEW
    assert result.answer.reason_code is ReasonCode.UNAUTHORIZED_TOOL_ATTEMPT
    assert result.run is not None and result.run.rejected_tools == ("submit_after_consent",)
    assert gateway.executed == []
    tool_result = client.messages.requests[1]["messages"][-1]["content"][0]
    assert "tool_not_allowed_on_this_surface" in tool_result["content"]
    direct = gateway.execute(
        AgentToolCall(
            tool=AgentToolName.SUBMIT_AFTER_CONSENT, arguments={"application_id": "app-1"}
        )
    )
    assert direct.status is AgentVisibleStatus.DENIED


@pytest.mark.parametrize(
    ("plan", "reason"),
    [
        ((("stop", "refusal"),), ReasonCode.MODEL_REFUSED),
        ((("stop", "max_tokens"),), ReasonCode.MODEL_OUTPUT_TRUNCATED),
        ((("error", "timeout"),), ReasonCode.MODEL_UNAVAILABLE),
        (tuple(read_product() for _ in range(8)), ReasonCode.MODEL_ROUND_LIMIT),
        ((("stop", "pause_turn"),), ReasonCode.PROPOSAL_SCHEMA_INVALID),
    ],
)
def test_a_model_that_does_not_finish_normally_gets_a_fixed_answer(plan, reason) -> None:
    agent, _, _ = _pipeline(plan)
    answer = _ask(agent).answer
    assert answer.outcome is AnswerOutcome.REQUIRES_HUMAN_REVIEW
    assert answer.reason_code is reason
    assert not answer.claims and "cut off" not in " ".join(answer.statements)


def test_the_budget_stops_paid_runs_before_the_model_is_called() -> None:
    agent, client, _ = _pipeline(
        (proposes(claims=[claim("order", value="4.90 EUR")]),), budget=QuestionBudget(1)
    )
    assert _ask(agent).answer.outcome is AnswerOutcome.VERIFIED_ANSWER
    second = _ask(agent).answer
    assert second.reason_code is ReasonCode.MODEL_BUDGET_EXHAUSTED
    assert len(client.messages.requests) == 1


# --- which proposer the configuration gives -------------------------------------------------------


def _settings(**values: Any) -> Settings:  # noqa: ANN401
    return Settings(_env_file=None, environment=Environment.TEST, **values)


def _llm(**values: Any) -> Settings:  # noqa: ANN401
    return _settings(ai_execution_mode="llm", enable_llm=True, llm_provider="anthropic", **values)


def test_scripted_mode_uses_the_offline_rules_and_needs_no_key() -> None:
    gateway = VerifiedFactsGateway(_guard())
    proposer = build_answer_proposer(_settings(), gateway, proposal_json_schema())
    assert isinstance(proposer, DeterministicProposer) and proposer.billable is False


def test_llm_mode_with_a_key_uses_the_existing_client_builder() -> None:
    """Only builds the SDK client, which opens no connection until it is used."""
    settings = _llm(llm_api_key=SecretStr("sk-ant-test-not-a-real-key"))
    client = answer_model_client(settings)
    assert client.__class__.__name__ == "Anthropic"
    gateway = VerifiedFactsGateway(_guard())
    proposer = build_answer_proposer(settings, gateway, proposal_json_schema(), client)
    assert isinstance(proposer, LlmAnswerProposer) and proposer.billable is True


def test_llm_mode_without_a_key_never_falls_back_to_the_rules() -> None:
    agent = VerifiedLlmAgent(
        _guard(),
        build_answer_proposer(_llm(), VerifiedFactsGateway(_guard()), proposal_json_schema()),
    )
    answer = _ask(agent).answer
    assert answer.outcome is AnswerOutcome.REQUIRES_HUMAN_REVIEW
    assert answer.reason_code is ReasonCode.MODEL_UNAVAILABLE
    assert not answer.claims


def test_llm_mode_with_another_provider_or_no_sdk_reports_itself_unavailable(monkeypatch) -> None:
    gateway = VerifiedFactsGateway(_guard())
    other = _settings(ai_execution_mode="llm", enable_llm=True, llm_provider="other")
    assert isinstance(
        build_answer_proposer(other, gateway, proposal_json_schema()), UnavailableProposer
    )
    monkeypatch.setitem(sys.modules, "app.agents.llm_answer_proposer", None)
    settings = _llm(llm_api_key=SecretStr("sk-ant-test-not-a-real-key"))
    proposer = build_answer_proposer(settings, gateway, proposal_json_schema())
    assert isinstance(proposer, UnavailableProposer)


def test_scripted_mode_imports_no_model_sdk() -> None:
    """The route must work on a machine without the optional SDK."""
    code = (
        "import sys\n"
        "sys.modules['anthropic'] = None\n"
        "from app.api.deps import get_verified_llm_agent\n"
        "from app.main import create_app\n"
        "print('ok')\n"
    )
    import subprocess

    result = subprocess.run(  # noqa: S603 - our own interpreter
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PYTHONPATH": str(ROOT),
            "TRUSTEQ_DATABASE_URL": "sqlite://",
            "SYSTEMROOT": "C:\\Windows",
        },
    )
    assert result.stdout.strip() == "ok", result.stderr[-800:]


# --- model traffic: what a model can and cannot read ---------------------------------------------


def test_the_gateway_gives_structured_values_and_never_narrative_text() -> None:
    gateway = VerifiedFactsGateway(_guard())
    result = gateway.execute(
        AgentToolCall(
            tool=AgentToolName.GET_OFFICIAL_PRODUCT,
            arguments={"provider_id": PROVIDER, "product_id": PRODUCT},
        )
    )
    facts = {fact["claim_id"]: fact for fact in result.data["facts"]}
    assert facts[f"{PROVIDER}/{PRODUCT}/FEE/order"]["value"] == "4.90 EUR"
    assert facts[f"{PROVIDER}/{PRODUCT}/CONTRACT_TERM/conditions"]["value"] is None
    dumped = json.dumps(result.data)
    assert "no minimum deposit" not in dumped and "lose value" not in dumped


@pytest.mark.parametrize("catalog", ["hostile_terms", "hostile_name", "hostile_document"])
def test_hostile_catalog_text_never_enters_model_traffic(catalog) -> None:
    agent, client, _ = _pipeline(
        (read_provider(), read_product(), proposes(claims=[claim("order", value="4.90 EUR")])),
        catalog,
    )
    _ask(agent)
    traffic = json.dumps(
        [request["messages"] for request in client.messages.requests], default=str
    ).lower()
    for marker in ("approve_consent", "ignore previous", "ignore all previous", "<script"):
        assert marker not in traffic


def test_sensitive_input_never_reaches_the_model() -> None:
    agent, client, _ = _pipeline((proposes(),))
    for question in (
        "My tax ID is SYNTH-TAX-700421.",
        "My tax number is 12345678901.",
        "Use DE12 3456 7890 1234 5678 90.",
        "Write to alice@example.org please.",
    ):
        result = _ask(agent, question)
        assert result.answer.reason_code is ReasonCode.SENSITIVE_DATA_IN_QUESTION
        assert result.answer.outcome is AnswerOutcome.UNSUPPORTED_REQUEST
        assert question not in json.dumps(result.answer.as_artifact())
    assert client.messages.requests == []


# --- citations must support their claims ---------------------------------------------------------


def _facts(catalog: str = "default", product: str | None = PRODUCT):
    guard = _guard(catalog)
    return guard.facts_for_model(PROVIDER, product)


def test_every_fact_has_all_the_fields_the_contract_requires() -> None:
    for fact in _facts().facts:
        assert fact.claim_id and fact.claim_type and fact.field
        assert fact.canonical_value and fact.statement
        assert fact.source.source_id and fact.source.source_version
        assert fact.source.source_timestamp
        if fact.claim_type not in {ClaimType.REGULATORY_STATUS, ClaimType.SERVICE_SCOPE}:
            assert fact.source.integrity_digest


def test_the_required_kinds_of_fact_exist_for_the_default_product() -> None:
    assert {fact.claim_type for fact in _facts().facts} >= {
        ClaimType.PROVIDER_IDENTITY,
        ClaimType.PRODUCT_IDENTITY,
        ClaimType.PRODUCT_AVAILABILITY,
        ClaimType.FEE,
        ClaimType.RISK_CLASSIFICATION,
        ClaimType.RISK_DISCLOSURE,
        ClaimType.DOCUMENT_VERSION,
        ClaimType.CONTRACT_TERM,
        ClaimType.PRODUCT_LIMITATION,
        ClaimType.REGULATORY_STATUS,
        ClaimType.SERVICE_SCOPE,
    }
    with_rules = _facts("with_eligibility")
    assert ClaimType.ELIGIBILITY_RULE in {fact.claim_type for fact in with_rules.facts}


def test_a_fee_cites_the_document_that_really_states_it() -> None:
    catalog = FixtureProviderDiscovery()
    product = catalog.list_products(PROVIDER)[0]
    text = catalog.get_document_text(PRICE_LIST, "2026.11")
    custody, order = product.fees
    assert fee_is_stated_in(custody, text) and fee_is_stated_in(order, text)
    swapped = order.model_copy(update={"amount": custody.amount})
    assert not fee_is_stated_in(swapped, text)


def test_a_fee_the_price_list_does_not_state_is_not_a_fact() -> None:
    lookup = _facts("fee_not_in_price_list")
    assert f"{PROVIDER}/{PRODUCT}/FEE/order" not in lookup.by_id()
    assert lookup.withheld[f"{PROVIDER}/{PRODUCT}/FEE/order"] is (
        ReasonCode.SOURCE_DOES_NOT_SUPPORT_CLAIM
    )
    assert f"{PROVIDER}/{PRODUCT}/FEE/custody" in lookup.by_id()


def test_narrative_fields_cite_the_manifest_that_contains_them() -> None:
    facts = _facts().by_id()
    for name in ("RISK_DISCLOSURE/summary", "CONTRACT_TERM/conditions"):
        fact = facts[f"{PROVIDER}/{PRODUCT}/{name}"]
        assert fact.source.source_id == "provider-manifest"
        assert fact.narrative is True
        assert "manifest" in fact.statement


def test_a_quoted_sentence_must_be_literally_in_the_cited_document() -> None:
    fixture = build_catalog_fixture("default")
    catalog = FixtureProviderDiscovery(fixture, clock=lambda: NOW)
    original = catalog.get_document_text

    def changed(document_id: str, version: str) -> str:
        text = original(document_id, version)
        return text.replace("you may get back less than you invested", "you may profit")

    catalog.get_document_text = changed  # type: ignore[method-assign]
    lookup = build_fact_lookup(catalog, PROVIDER, PRODUCT, NOW)
    # The digest no longer matches, so nothing that rests on this document's text is stated.
    assert f"{PROVIDER}/{PRODUCT}/RISK_DISCLOSURE/capital_loss" not in lookup.by_id()
    assert f"{PROVIDER}/{PRODUCT}/DOCUMENT_VERSION/document:kid-depot-basic" in lookup.withheld


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"value": "4.91 EUR"}, ReasonCode.VALUE_MISMATCH),
        ({"source_id": "kid-depot-basic"}, ReasonCode.SOURCE_MISMATCH),
        ({"source_version": "2025.01"}, ReasonCode.VERSION_MISMATCH),
        ({"source_digest": "0" * 64}, ReasonCode.DIGEST_MISMATCH),
        ({"product": "depot-gold"}, ReasonCode.UNKNOWN_PRODUCT),
    ],
)
def test_any_changed_field_refuses_the_whole_answer(kwargs, reason) -> None:
    answer = _guard().release(
        _proposal(claims=[claim("custody", value="0.00 EUR p.a."), claim("order", **kwargs)]),
        provider_id=PROVIDER,
        generated_by=GENERATED,
    )
    assert answer.outcome is AnswerOutcome.INSUFFICIENT_VERIFIED_EVIDENCE
    assert answer.reason_code is reason
    assert answer.claims == ()
    assert "0.00" not in " ".join(answer.statements)


def test_a_claim_named_with_another_products_id_is_refused() -> None:
    wrong_owner = {**claim("custody", value="0.00 EUR p.a."), "product_id": "depot-plus"}
    answer = _guard("two_products").release(
        _proposal(claims=[wrong_owner]), provider_id=PROVIDER, generated_by=GENERATED
    )
    assert answer.reason_code is ReasonCode.PRODUCT_MISMATCH


def test_a_claim_with_a_mislabelled_type_or_field_is_refused() -> None:
    relabelled = {
        **claim("custody", value="0.00 EUR p.a."),
        "claim_type": ClaimType.RISK_CLASSIFICATION.value,
    }
    answer = _guard().release(
        _proposal(claims=[relabelled]), provider_id=PROVIDER, generated_by=GENERATED
    )
    assert answer.reason_code is ReasonCode.CLAIM_TYPE_MISMATCH
    other_field = {**claim("custody", value="0.00 EUR p.a."), "field": "order"}
    answer = _guard().release(
        _proposal(claims=[other_field]), provider_id=PROVIDER, generated_by=GENERATED
    )
    assert answer.reason_code is ReasonCode.FIELD_MISMATCH


def test_a_refused_claim_echoes_nothing_the_model_wrote() -> None:
    answer = _guard().release(
        _proposal(claims=[claim("x", value="Guaranteed 9% for life", claim_id="a very long lie")]),
        provider_id=PROVIDER,
        generated_by=GENERATED,
    )
    dumped = json.dumps(answer.as_artifact())
    assert "Guaranteed 9%" not in dumped and "a very long lie" not in dumped
    assert answer.rejected_claims[0].known_claim_id is None


def test_a_correct_proposal_releases_the_fact_with_its_whole_citation() -> None:
    answer = _guard().release(
        _proposal(claims=[claim("order", value="4.90 EUR", source_id=PRICE_LIST)]),
        provider_id=PROVIDER,
        generated_by=GENERATED,
    )
    assert answer.outcome is AnswerOutcome.VERIFIED_ANSWER
    released = answer.claims[0]
    assert released.canonical_value == "4.90 EUR"
    assert released.source_id == PRICE_LIST and released.source_version == "2026.11"
    assert released.source_timestamp == "2026-10-15"
    assert len(released.integrity_digest or "") == 64
    assert released.validation_outcome == "VERIFIED" and released.reason_code is None
    assert released.support.value == "VALUE_IN_DOCUMENT_TEXT"
    assert answer.generated_by.model_prose_released is False
    assert len(answer.answer_digest) == 64


def test_the_rendered_text_is_checked_last_with_the_pattern_check() -> None:
    """A signed catalog value can pass every other check and still say something unverified."""
    from scripts.communication_eval_cases import _with_product_field

    guard = CommunicationGuard(
        FixtureProviderDiscovery(
            _with_product_field(conditions="Synthetic: a 5% loyalty bonus."), clock=lambda: NOW
        ),
        lambda: NOW,
    )
    answer = guard.release(
        _proposal(claims=[claim("conditions", claim_type="CONTRACT_TERM")]),
        provider_id=PROVIDER,
        generated_by=GENERATED,
    )
    assert answer.outcome is AnswerOutcome.REQUIRES_HUMAN_REVIEW
    assert answer.reason_code is ReasonCode.RENDERED_TEXT_FAILED_CHECK
    assert "5%" not in json.dumps(answer.as_artifact())


# --- fixed texts ---------------------------------------------------------------------------------


def test_every_process_step_and_topic_has_a_fixed_text() -> None:
    assert set(PROCESS_EXPLANATIONS) == set(ProcessStep)
    assert set(NOT_ESTABLISHED_TOPICS) == set(NotEstablishedTopic)


def test_the_fixed_texts_promise_nothing_and_say_what_is_simulated() -> None:
    joined = " ".join(PROCESS_EXPLANATIONS.values()).lower()
    for promise in ("will contact", "member of staff", "guaranteed", "you are approved"):
        assert promise not in joined
    assert "synthetic test service" in PROCESS_EXPLANATIONS[ProcessStep.IDENTITY_VERIFICATION]
    signing = PROCESS_EXPLANATIONS[ProcessStep.CONTRACT_SIGNING]
    assert "no qualified electronic signature" in signing and "no legal effect" in signing
    assert "mock" in PROCESS_EXPLANATIONS[ProcessStep.DEPOT_OPENING]


@pytest.mark.parametrize(
    ("intent", "outcome", "reason"),
    [
        (
            Intent.PERSONAL_ADVICE,
            AnswerOutcome.REQUIRES_HUMAN_REVIEW,
            ReasonCode.PERSONAL_ADVICE_NOT_OFFERED,
        ),
        (
            Intent.OUTCOME_PREDICTION,
            AnswerOutcome.REQUIRES_HUMAN_REVIEW,
            ReasonCode.OUTCOME_DECIDED_BY_THE_BANK,
        ),
        (
            Intent.ACTION_REQUEST,
            AnswerOutcome.UNSUPPORTED_REQUEST,
            ReasonCode.ACTION_ONLY_ON_BANK_CHANNEL,
        ),
        (
            Intent.PRIVATE_DATA_REQUEST,
            AnswerOutcome.UNSUPPORTED_REQUEST,
            ReasonCode.PRIVATE_DATA_NOT_AVAILABLE,
        ),
        (Intent.OUT_OF_SCOPE, AnswerOutcome.UNSUPPORTED_REQUEST, ReasonCode.OUT_OF_SCOPE),
    ],
)
def test_these_intents_have_a_fixed_answer_whatever_else_the_model_proposes(
    intent, outcome, reason
) -> None:
    answer = _guard().release(
        _proposal(intent=intent.value, claims=[claim("order", value="4.90 EUR")]),
        provider_id=PROVIDER,
        generated_by=GENERATED,
    )
    assert answer.outcome is outcome and answer.reason_code is reason
    assert answer.claims == () and "4.90" not in " ".join(answer.statements)


def test_not_established_is_said_only_if_the_sources_do_not_mention_the_topic() -> None:
    plain = _guard().release(
        _proposal(intent="SOURCE_LIMIT", not_established_topics=["GUARANTEE"]),
        provider_id=PROVIDER,
        generated_by=GENERATED,
    )
    assert plain.outcome is AnswerOutcome.NOT_ESTABLISHED_BY_SOURCES
    assert {source.source_id for source in plain.sources_consulted} >= {"provider-manifest"}
    hostile = _guard("hostile_document").release(
        _proposal(intent="SOURCE_LIMIT", not_established_topics=["GUARANTEE"]),
        provider_id=PROVIDER,
        generated_by=GENERATED,
    )
    assert hostile.outcome is AnswerOutcome.REQUIRES_HUMAN_REVIEW
    assert hostile.reason_code is ReasonCode.TOPIC_PRESENT_IN_SOURCES


def test_an_eligibility_topic_is_not_called_absent_when_rules_are_published() -> None:
    answer = _guard("with_eligibility").release(
        _proposal(intent="SOURCE_LIMIT", not_established_topics=["ELIGIBILITY_RULES"]),
        provider_id=PROVIDER,
        generated_by=GENERATED,
    )
    assert answer.reason_code is ReasonCode.TOPIC_PRESENT_IN_SOURCES


# --- the catalog itself ---------------------------------------------------------------------------


def test_a_withdrawn_product_in_a_signed_manifest_is_really_withdrawn() -> None:
    catalog = FixtureProviderDiscovery(
        build_catalog_fixture("withdrawn_product"), clock=lambda: NOW
    )
    assert catalog.list_products(PROVIDER)[0].valid_until is not None
    lookup = build_fact_lookup(catalog, PROVIDER, PRODUCT, NOW)
    assert lookup.problem is ReasonCode.PRODUCT_NOT_CURRENT and lookup.facts == ()


def test_a_validity_without_a_timezone_fails_the_manifest() -> None:
    from scripts.communication_eval_cases import _with_product_field

    with pytest.raises(ManifestRejected):
        FixtureProviderDiscovery(
            _with_product_field(valid_until="2030-01-01T00:00:00"), clock=lambda: NOW
        )


# --- structured requests through the guard --------------------------------------------------------


def test_a_structured_answer_repeats_no_text_a_caller_typed() -> None:
    answer = _guard().verified_answer(
        provider_id="Guaranteed returns of 10%",
        product_id="We recommend this",
        question="The bank guarantees 10%",
        subject=None,
    )
    dumped = json.dumps(answer)
    assert "Guaranteed" not in dumped and "recommend" not in dumped and "10%" not in dumped
    assert answer["question"] == "OTHER" and answer["outcome"] == "INSUFFICIENT_VERIFIED_EVIDENCE"


def test_a_comparison_of_a_product_with_itself_is_no_comparison() -> None:
    answer = _guard().compare_fees(
        provider_id=PROVIDER, product_ids=[PRODUCT, PRODUCT], fee_code="custody"
    )
    assert answer["outcome"] == "INSUFFICIENT_VERIFIED_EVIDENCE" and answer["rows"] == []


def test_a_structured_comparison_of_two_products_shows_both_and_ranks_neither() -> None:
    answer = _guard("two_products").compare_fees(
        provider_id=PROVIDER, product_ids=[PRODUCT, "depot-plus"], fee_code="custody"
    )
    assert answer["outcome"] == "COMPARED_NOT_RANKED"
    assert [row["value"] for row in answer["rows"]] == ["0.00 EUR p.a.", "12.00 EUR p.a."]
    assert "not a recommendation" in answer["note"]
    for word in ("cheaper", "better", "best"):
        assert word not in json.dumps(answer).lower()


def test_conditions_and_risk_structured_answers_cite_the_manifest() -> None:
    for question, text in (("CONDITIONS", "no minimum deposit"), ("RISK", "can lose value")):
        answer = _guard().verified_answer(
            provider_id=PROVIDER, product_id=PRODUCT, question=question, subject=None
        )
        assert answer["result"] == "VERIFIED_FACT" and text in answer["statement"]
        assert answer["evidence"]["source"]["document_id"] == "provider-manifest"
    risk_class = _guard().verified_answer(
        provider_id=PROVIDER, product_id=PRODUCT, question="RISK_CLASS", subject=None
    )
    assert risk_class["value"] == "3 of 7 (synthetic scale)"


# --- documents ------------------------------------------------------------------------------------


def test_a_current_document_is_served_as_data_with_its_digest() -> None:
    document = _guard().document(provider_id=PROVIDER, document_id=PRICE_LIST, version="2026.11")
    assert document["result"] == "DOCUMENT_AVAILABLE"
    assert "4.90 EUR" in document["text"] and document["treat_as"].startswith("data")
    assert document["source_timestamp"] == "2026-10-15"


@pytest.mark.parametrize(
    ("catalog", "days", "document_id", "version", "result", "reason"),
    [
        ("default", 0, PRICE_LIST, "2025.01", "NOT_FOUND", "VERSION_MISMATCH"),
        ("default", 0, "pli-other", "1.0", "NOT_FOUND", "UNKNOWN_DOCUMENT"),
        ("default", 800, PRICE_LIST, "2026.11", "EXPIRED", "SOURCE_EXPIRED"),
        ("withdrawn_product", 0, PRICE_LIST, "2026.11", "EXPIRED", "PRODUCT_NOT_CURRENT"),
        (
            "hostile_document",
            0,
            "kid-depot-basic",
            "1.0",
            "REQUIRES_HUMAN_REVIEW",
            "SOURCE_TEXT_UNSAFE",
        ),
    ],
)
def test_a_document_that_is_not_current_or_not_safe_is_not_served(
    catalog, days, document_id, version, result, reason
) -> None:
    answer = _guard(catalog, days).document(
        provider_id=PROVIDER, document_id=document_id, version=version
    )
    assert answer["result"] == result and answer["reason_code"] == reason
    assert "text" not in answer


@pytest.fixture
def world():
    with trust_world() as value:
        yield value


def test_the_document_routes_use_the_same_checks(world) -> None:
    good = world.bank.get(f"/v1/agent/documents/{PRICE_LIST}", params={"version": "2026.11"})
    assert good.status_code == 200 and good.json()["result"] == "DOCUMENT_AVAILABLE"
    old = world.bank.get(f"/v1/agent/documents/{PRICE_LIST}", params={"version": "2025.01"})
    assert old.status_code == 404
    agent = world.agent()
    missing = artifact_of(
        agent.call("document-retrieval", document_id=PRICE_LIST, version="2025.01")
    )
    assert missing["reason_code"] == "VERSION_MISMATCH" and "text" not in missing
    world.clock.advance(days=800)
    expired = world.bank.get(f"/v1/agent/documents/{PRICE_LIST}", params={"version": "2026.11"})
    assert expired.status_code == 409 and expired.json()["reason_code"] == "SOURCE_EXPIRED"


# --- the routes -----------------------------------------------------------------------------------


def _app(plan: tuple[Any, ...] | None = None, **settings: Any) -> tuple[TestClient, Any]:  # noqa: ANN401
    values = {
        "environment": Environment.TEST,
        "database_url": "sqlite://",
        "log_level": "ERROR",
        **settings,
    }
    configured = Settings(_env_file=None, **values)
    container = build_default_container(configured)
    container.clock = FakeClock()
    client = PlannedClient(plan) if plan is not None else None
    container.answer_model_client = client
    return TestClient(create_app(configured, container), base_url="https://bank.test"), client


def test_the_rest_route_answers_like_the_a2a_skill() -> None:
    http, _ = _app()
    with http:
        answer = http.post(
            "/v1/agent/questions", json={"question": "What are the fees?", "product_id": PRODUCT}
        )
        assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
        body = answer.json()
        assert body["outcome"] == "VERIFIED_ANSWER"
        assert body["contract_version"] == "regulated-answer/1"
        assert body["generated_by"]["proposer"] == "deterministic-offline"
        assert [claim_["field"] for claim_ in body["claims"]] == ["custody", "order"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "x" * 1001},
        {"question": 7},
        {"question": "Fees?", "product_ids": "depot-basic"},
        {"question": "Fees?", "product_ids": ["<script>"]},
        {"question": "Fees?", "provider_id": "../etc"},
        [],
    ],
)
def test_an_invalid_question_is_refused_and_repeated_nowhere(body) -> None:
    http, _ = _app()
    with http:
        answer = http.post("/v1/agent/questions", json=body)
        assert answer.status_code == 422
        assert answer.json() == {"error": {"code": "validation_error"}}


def test_an_oversized_question_body_is_refused() -> None:
    http, _ = _app()
    with http:
        assert http.post("/v1/agent/questions", content=b"x" * 40000).status_code == 413


def test_the_rest_route_refuses_a_bad_credential(world) -> None:
    answer = world.bank.post(
        "/v1/agent/questions",
        json={"question": "Fees?"},
        headers={"Authorization": "Bearer tqa_" + "A" * 43},
    )
    assert answer.status_code == 401


def test_a_fake_model_takes_the_real_route_in_llm_mode() -> None:
    http, client = _app(
        (read_product(), proposes(claims=[claim("order", value="4.90 EUR")])),
        ai_execution_mode="llm",
        enable_llm=True,
        llm_provider="anthropic",
        llm_api_key=SecretStr("not-a-real-key"),
        llm_questions_require_credential=False,
    )
    with http:
        body = http.post("/v1/agent/questions", json={"question": "Order fee?"}).json()
    assert body["outcome"] == "VERIFIED_ANSWER"
    assert body["generated_by"]["proposer"] == "anthropic"
    assert body["generated_by"]["model"] == "fake-model-no-api-call"
    assert len(client.messages.requests) == 2


def test_the_public_question_stores_nothing(world) -> None:
    before = world.sql("SELECT COUNT(*) FROM audit_events")
    agent = world.agent()
    task = agent.call("product-question", question="What are the fees?")
    assert artifact_of(task)["outcome"] == "VERIFIED_ANSWER"
    assert world.sql("SELECT COUNT(*) FROM audit_events") == before
    assert world.sql("SELECT COUNT(*) FROM a2a_tasks") == [(0,)]


def test_a_question_needs_text_on_the_a2a_skill_too(world) -> None:
    agent = world.agent()
    with pytest.raises(AgentError) as refused:
        agent.call("product-question", question="")
    assert refused.value.detail["code"] == -32602


def test_an_authenticated_task_keeps_a_summary_and_never_restates_an_old_fact(world) -> None:
    agent = world.agent()
    started = world.start_application(agent)
    token = started["tokens"]["status_token"]
    task = agent.call("product-question", token, question="What are the fees?")
    assert "4.90 EUR" in json.dumps(artifact_of(task))
    assert "4.90" not in repr(world.sql("SELECT artifact FROM a2a_tasks"))
    world.clock.advance(seconds=30)

    def as_owner(method: str, params: dict) -> dict:
        url = agent.interface["url"]
        return agent.rpc(
            method,
            params,
            headers={
                "A2A-Version": "1.0",
                "Authorization": f"DPoP {token}",
                "DPoP": agent._dpop("POST", url, token),
            },
        )

    stored = as_owner("GetTask", {"id": task["id"]})["artifacts"][0]["parts"][0]["data"]
    assert stored["summary_only"] is True and stored["outcome"] == "VERIFIED_ANSWER"
    assert "statements" not in stored and "4.90" not in json.dumps(stored)


def test_a_stored_structured_answer_is_a_summary_too(world) -> None:
    agent = world.agent()
    started = world.start_application(agent)
    agent.call(
        "product-information",
        started["tokens"]["status_token"],
        provider_id=PROVIDER,
        product_id=PRODUCT,
        question="FEE",
        subject="order",
    )
    agent.call(
        "document-retrieval",
        started["tokens"]["status_token"],
        document_id=PRICE_LIST,
        version="2026.11",
    )
    stored = repr(world.sql("SELECT artifact FROM a2a_tasks"))
    assert "4.90" not in stored and "MOCK PRICE LIST" not in stored
    assert "summary_only" in stored


def test_the_public_text_check_says_it_is_no_statement_of_the_bank(world) -> None:
    """It still passes misleading prose: it reads no meaning. So the answer labels itself."""
    answer = world.bank.post(
        "/v1/communication/verify",
        json={"text": "Most people find this account pays for itself very quickly."},
    ).json()
    assert answer["verdict"] == "RELEASE"
    assert answer["bank_statement"] is False
    assert answer["semantic_check"] == "NOT_PERFORMED"
    assert "does not say that the text is true" in answer["note"]


def test_misleading_prose_passes_that_check_but_cannot_reach_a_customer() -> None:
    prose = "Most people find this account pays for itself very quickly."
    guard = _guard()
    assert guard.check(prose, provider_id=PROVIDER, product_id=PRODUCT).verdict.value == "RELEASE"
    agent, _, _ = _pipeline((("text", prose),))
    result = _ask(agent)
    assert result.answer.outcome is AnswerOutcome.REQUIRES_HUMAN_REVIEW
    assert prose not in json.dumps(result.answer.as_artifact())


def test_the_hostile_fixture_constant_is_what_these_tests_think_it_is() -> None:
    catalog = FixtureProviderDiscovery(prompt_injection_fixture())
    assert catalog.list_products(PROVIDER)[0].conditions == HOSTILE_INSTRUCTIONS
    assert SimpleNamespace(value=HOSTILE_INSTRUCTIONS).value.startswith("IGNORE PREVIOUS")
