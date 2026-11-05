"""PROTOTYPE demonstration of a knowledge-and-experience assessment. Synthetic data only.

This is not legally complete MiFID II or WpHG compliance, not advice and not a suitability
assessment. It is an isolated component: the frozen onboarding state machine does not call it,
because the assessment belongs to a securities ORDER, and this prototype opens an ACCOUNT and
executes no order. Intended integration point: a separate order step on the bank's customer
channel, after OPENED, before an order is accepted.

The rules are plain code and no model takes part: no agent tool reaches this module, and answers
that did not come from the customer on the bank's channel do not count. Whether a statutory duty
applies is decided by the bank's compliance function, never by an LLM.

Execution-only (WpHG section 63(11)) is deliberately not an outcome. `review_execution_only` can
only say that an exemption may be considered by compliance, or that the assessment remains
required: no fund or service is assumed to be exempt.
"""

from enum import StrEnum

from app.domain.base import DomainModel

RULE_SET_VERSION = "synthetic-2026.11"
PROTOTYPE_NOTICE = "Prototype demonstration with synthetic data: not legally complete compliance."


class ServiceContext(StrEnum):
    ACCOUNT_OPENING = "ACCOUNT_OPENING"
    ORDER_EXECUTION = "ORDER_EXECUTION"


class ProductKnowledgeLevel(StrEnum):
    """A synthetic label for how much a product asks of the customer. Not a legal classification."""

    BASIC = "BASIC"
    ADVANCED = "ADVANCED"


class AnswerSource(StrEnum):
    CUSTOMER_ON_BANK_CHANNEL = "CUSTOMER_ON_BANK_CHANNEL"
    AGENT_OR_MODEL = "AGENT_OR_MODEL"


class ExperienceBand(StrEnum):
    NONE = "NONE"
    ONE_TO_FIVE = "ONE_TO_FIVE"
    MORE_THAN_FIVE = "MORE_THAN_FIVE"


class AppropriatenessOutcome(StrEnum):
    ASSESSMENT_OK = "ASSESSMENT_OK"
    WARNING_REQUIRED = "WARNING_REQUIRED"
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"
    NOT_TRIGGERED_BY_ACCOUNT_OPENING = "NOT_TRIGGERED_BY_ACCOUNT_OPENING"


class ExecutionOnlyReview(StrEnum):
    MAY_BE_CONSIDERED_BY_COMPLIANCE = "MAY_BE_CONSIDERED_BY_COMPLIANCE"
    ASSESSMENT_REMAINS_REQUIRED = "ASSESSMENT_REMAINS_REQUIRED"


QUESTIONNAIRE: tuple[tuple[str, str], ...] = (
    ("knows_capital_loss", "True or false: the value of a fund can fall below what you paid."),
    ("knows_no_guarantee", "True or false: past returns guarantee future returns."),
    ("experience", "How many securities or fund transactions did you make in the last 3 years?"),
)


class Answers(DomainModel):
    """The questionnaire. None means not answered; a wrong answer is still an answer."""

    source: AnswerSource
    knows_capital_loss: bool | None = None  # correct answer: True
    knows_no_guarantee: bool | None = None  # correct answer: False
    experience: ExperienceBand | None = None


class AppropriatenessResult(DomainModel):
    outcome: AppropriatenessOutcome
    reasons: tuple[str, ...] = ()
    customer_notice: str | None = None
    # The customer is told and decides; the system neither accepts nor rejects them silently.
    requires_customer_acknowledgement: bool = False
    rejects_customer: bool = False
    rule_set_version: str = RULE_SET_VERSION


_WARNING = (
    "Based on your answers, this product may not be appropriate for your knowledge and "
    "experience. You may still decide to continue. This is not investment advice."
)
_NO_ASSESSMENT = (
    "Your answers are incomplete, so we cannot assess whether this product is appropriate for "
    "you. You may complete them or decide how to continue. This is not investment advice."
)


def assess(
    context: ServiceContext, product: ProductKnowledgeLevel, answers: Answers
) -> AppropriatenessResult:
    """Deterministic: the same input always gives the same result."""
    if context is ServiceContext.ACCOUNT_OPENING:
        return AppropriatenessResult(
            outcome=AppropriatenessOutcome.NOT_TRIGGERED_BY_ACCOUNT_OPENING,
            reasons=("assessment_belongs_to_the_order_step",),
        )
    if answers.source is not AnswerSource.CUSTOMER_ON_BANK_CHANNEL:
        return _insufficient(("answers_not_from_the_customer_channel",))
    missing = tuple(
        f"unanswered_{name}"
        for name in ("knows_capital_loss", "knows_no_guarantee", "experience")
        if getattr(answers, name) is None
    )
    if missing:
        return _insufficient(missing)

    gaps: list[str] = []
    if answers.knows_capital_loss is not True:
        gaps.append("knowledge_capital_loss")
    if answers.knows_no_guarantee is not False:
        gaps.append("knowledge_no_guarantee")
    if product is ProductKnowledgeLevel.ADVANCED and answers.experience is ExperienceBand.NONE:
        gaps.append("experience_none_for_advanced_product")
    if gaps:
        return AppropriatenessResult(
            outcome=AppropriatenessOutcome.WARNING_REQUIRED,
            reasons=tuple(gaps),
            customer_notice=_WARNING,
            requires_customer_acknowledgement=True,
        )
    return AppropriatenessResult(outcome=AppropriatenessOutcome.ASSESSMENT_OK)


def _insufficient(reasons: tuple[str, ...]) -> AppropriatenessResult:
    return AppropriatenessResult(
        outcome=AppropriatenessOutcome.INSUFFICIENT_INFORMATION,
        reasons=reasons,
        customer_notice=_NO_ASSESSMENT,
        requires_customer_acknowledgement=True,
    )


class ExecutionOnlyFacts(DomainModel):
    """Facts recorded by the bank, not by an agent. None means not established."""

    non_complex_per_compliance: bool | None = None
    initiated_by_customer: bool | None = None
    customer_informed_no_assessment: bool | None = None
    conflict_rules_met: bool | None = None


def review_execution_only(facts: ExecutionOnlyFacts) -> ExecutionOnlyReview:
    """All conditions established: compliance MAY consider an exemption. Never "exempt"."""
    if all(value is True for value in facts.model_dump().values()):
        return ExecutionOnlyReview.MAY_BE_CONSIDERED_BY_COMPLIANCE
    return ExecutionOnlyReview.ASSESSMENT_REMAINS_REQUIRED


SYNTHETIC_PROFILES: dict[str, Answers] = {
    "synthetic-experienced": Answers(
        source=AnswerSource.CUSTOMER_ON_BANK_CHANNEL,
        knows_capital_loss=True,
        knows_no_guarantee=False,
        experience=ExperienceBand.MORE_THAN_FIVE,
    ),
    "synthetic-novice": Answers(
        source=AnswerSource.CUSTOMER_ON_BANK_CHANNEL,
        knows_capital_loss=True,
        knows_no_guarantee=True,
        experience=ExperienceBand.NONE,
    ),
    "synthetic-incomplete": Answers(
        source=AnswerSource.CUSTOMER_ON_BANK_CHANNEL, knows_capital_loss=True
    ),
}
