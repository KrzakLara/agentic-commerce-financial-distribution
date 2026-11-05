"""The synthetic knowledge-and-experience assessment: deterministic, separate from the flow."""

import inspect
from pathlib import Path

import pytest

from app.integrations.agent_tools import AgentToolName
from app.services import appropriateness
from app.services.appropriateness import (
    SYNTHETIC_PROFILES,
    Answers,
    AnswerSource,
    AppropriatenessOutcome,
    ExecutionOnlyFacts,
    ExecutionOnlyReview,
    ExperienceBand,
    ProductKnowledgeLevel,
    ServiceContext,
    assess,
    review_execution_only,
)
from tests.flow import Flow

ORDER = ServiceContext.ORDER_EXECUTION
OK, WARN, INSUFFICIENT = (
    AppropriatenessOutcome.ASSESSMENT_OK,
    AppropriatenessOutcome.WARNING_REQUIRED,
    AppropriatenessOutcome.INSUFFICIENT_INFORMATION,
)


@pytest.mark.parametrize(
    ("profile", "product", "expected"),
    [
        ("synthetic-experienced", ProductKnowledgeLevel.BASIC, OK),
        ("synthetic-experienced", ProductKnowledgeLevel.ADVANCED, OK),
        ("synthetic-novice", ProductKnowledgeLevel.BASIC, WARN),
        ("synthetic-novice", ProductKnowledgeLevel.ADVANCED, WARN),
        ("synthetic-incomplete", ProductKnowledgeLevel.BASIC, INSUFFICIENT),
    ],
)
def test_the_synthetic_profiles(
    profile: str, product: ProductKnowledgeLevel, expected: AppropriatenessOutcome
) -> None:
    first = assess(ORDER, product, SYNTHETIC_PROFILES[profile])
    assert first.outcome is expected
    assert first == assess(ORDER, product, SYNTHETIC_PROFILES[profile])  # deterministic


def test_experience_matters_only_for_the_advanced_product() -> None:
    correct_no_experience = Answers(
        source=AnswerSource.CUSTOMER_ON_BANK_CHANNEL,
        knows_capital_loss=True,
        knows_no_guarantee=False,
        experience=ExperienceBand.NONE,
    )
    assert assess(ORDER, ProductKnowledgeLevel.BASIC, correct_no_experience).outcome is OK
    advanced = assess(ORDER, ProductKnowledgeLevel.ADVANCED, correct_no_experience)
    assert advanced.outcome is WARN
    assert advanced.reasons == ("experience_none_for_advanced_product",)


@pytest.mark.parametrize("profile", ["synthetic-novice", "synthetic-incomplete"])
def test_a_warning_is_a_separate_outcome_not_a_silent_accept_or_reject(profile: str) -> None:
    result = assess(ORDER, ProductKnowledgeLevel.BASIC, SYNTHETIC_PROFILES[profile])
    assert result.outcome is not OK
    assert result.customer_notice and "not investment advice" in result.customer_notice
    assert result.requires_customer_acknowledgement is True
    assert result.rejects_customer is False


def test_account_opening_is_not_the_order_step() -> None:
    for answers in SYNTHETIC_PROFILES.values():
        result = assess(ServiceContext.ACCOUNT_OPENING, ProductKnowledgeLevel.ADVANCED, answers)
        assert result.outcome is AppropriatenessOutcome.NOT_TRIGGERED_BY_ACCOUNT_OPENING
        assert result.requires_customer_acknowledgement is False


def test_answers_from_an_agent_or_a_model_never_count_however_good_they_look() -> None:
    perfect = SYNTHETIC_PROFILES["synthetic-experienced"].model_copy(
        update={"source": AnswerSource.AGENT_OR_MODEL}
    )
    result = assess(ORDER, ProductKnowledgeLevel.BASIC, perfect)
    assert result.outcome is INSUFFICIENT
    assert result.reasons == ("answers_not_from_the_customer_channel",)


@pytest.mark.parametrize(
    "missing",
    [
        "non_complex_per_compliance",
        "initiated_by_customer",
        "customer_informed_no_assessment",
        "conflict_rules_met",
    ],
)
def test_execution_only_needs_every_condition(missing: str) -> None:
    facts = {
        "non_complex_per_compliance": True,
        "initiated_by_customer": True,
        "customer_informed_no_assessment": True,
        "conflict_rules_met": True,
    }
    assert (
        review_execution_only(ExecutionOnlyFacts(**facts))
        is ExecutionOnlyReview.MAY_BE_CONSIDERED_BY_COMPLIANCE
    )
    for value in (False, None):
        assert (
            review_execution_only(ExecutionOnlyFacts(**{**facts, missing: value}))
            is ExecutionOnlyReview.ASSESSMENT_REMAINS_REQUIRED
        )


def test_no_outcome_declares_anything_exempt() -> None:
    names = [o.value for o in (*ExecutionOnlyReview, *AppropriatenessOutcome)]
    assert not any(name == "EXEMPT" or name.startswith("EXEMPT") for name in names)


def test_no_agent_tool_or_model_code_reaches_the_assessment(flow: Flow) -> None:
    assert not any("assess" in tool.value for tool in AgentToolName)
    app_root = Path(inspect.getfile(appropriateness)).parent.parent
    source = Path(inspect.getfile(appropriateness)).read_text(encoding="utf-8")
    assert "import anthropic" not in source and "app.agents" not in source
    for module in ("agents/llm.py", "agents/scripted.py", "integrations/rest_gateway.py"):
        assert "appropriateness" not in (app_root / module).read_text(encoding="utf-8")

    # The frozen journey does not depend on it: the account opens without an assessment step.
    started = flow.run_to_opened()
    assert flow.status_of(started.application_id) == "OPENED"
    assert not any("appropriateness" in event for event in flow.event_types(started.application_id))
