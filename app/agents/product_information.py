"""Deterministic product answers for an agent, built only from the bank's approved claims.

Shows what the bank actually controls: verified data, structured responses and official claims. The
answer comes from the `approved_claims` of the existing `get_official_product` tool, so it cannot
state a fact the bank did not approve. A fact the source does not contain is declined, and a request
for a guarantee or a personal judgment goes to a person.

This is the scripted path. The LLM agent receives the same approved claims in its tool results, but
its own free-form text is not checked against them, and the bank cannot control what a customer's
personal agent writes.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.integrations.agent_tools import (
    AgentToolCall,
    AgentToolGateway,
    AgentToolName,
    AgentToolResult,
    AgentVisibleStatus,
)


class ProductQuestion(StrEnum):
    FEE = "FEE"
    PROVIDER_IDENTITY = "PROVIDER_IDENTITY"
    PARTNER = "PARTNER"
    # Narrative or not in the structured facts: the bank's own page shows these.
    CONDITIONS_OR_RISK = "CONDITIONS_OR_RISK"
    # Sensitive: never answered by an agent.
    GUARANTEE = "GUARANTEE"
    PERSONAL_RECOMMENDATION = "PERSONAL_RECOMMENDATION"
    APPROVAL_OUTCOME = "APPROVAL_OUTCOME"


class AnswerKind(StrEnum):
    APPROVED_FACT = "APPROVED_FACT"
    NOT_IN_APPROVED_SOURCES = "NOT_IN_APPROVED_SOURCES"
    SEE_BANK_PAGE = "SEE_BANK_PAGE"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    UNAVAILABLE = "UNAVAILABLE"


class ProductAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AnswerKind
    statement: str
    provider_id: str
    product_id: str
    product_version: str | None = None
    source: dict[str, str] | None = None


class FeeComparisonRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: str
    answer: ProductAnswer


class FeeComparison(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    comparable: bool
    fee_code: str
    rows: tuple[FeeComparisonRow, ...]
    note: str


_SENSITIVE = frozenset(
    {
        ProductQuestion.GUARANTEE,
        ProductQuestion.PERSONAL_RECOMMENDATION,
        ProductQuestion.APPROVAL_OUTCOME,
    }
)
_ASK_A_PERSON = (
    "I can't confirm that from the bank's approved information. You can ask for a person on the "
    "bank's customer page."
)
_ON_THE_BANK_PAGE = (
    "The bank shows this text on its own customer page, with the exact document version."
)
_NOT_IN_SOURCES = "The bank's approved information does not contain that."
_UNAVAILABLE = "The bank's approved information could not be read, so I can't state anything."


class ProductInformationAgent:
    def __init__(self, gateway: AgentToolGateway) -> None:
        self._gateway = gateway

    def _product(self, provider_id: str, product_id: str) -> AgentToolResult:
        return self._gateway.execute(
            AgentToolCall(
                tool=AgentToolName.GET_OFFICIAL_PRODUCT,
                arguments={"provider_id": provider_id, "product_id": product_id},
            )
        )

    def answer(
        self,
        provider_id: str,
        product_id: str,
        question: ProductQuestion,
        subject: str | None = None,
    ) -> ProductAnswer:
        def reply(kind: AnswerKind, statement: str) -> ProductAnswer:
            return ProductAnswer(
                kind=kind, statement=statement, provider_id=provider_id, product_id=product_id
            )

        if question in _SENSITIVE:  # no tool is even called: a fact could not answer this
            return reply(AnswerKind.ESCALATE_TO_HUMAN, _ASK_A_PERSON)
        if question is ProductQuestion.CONDITIONS_OR_RISK:
            return reply(AnswerKind.SEE_BANK_PAGE, _ON_THE_BANK_PAGE)
        result = self._product(provider_id, product_id)
        if result.status is not AgentVisibleStatus.OK:
            return reply(AnswerKind.UNAVAILABLE, _UNAVAILABLE)
        claim = _matching_claim(result.data.get("approved_claims", []), question, subject)
        if claim is None:
            return reply(AnswerKind.NOT_IN_APPROVED_SOURCES, _NOT_IN_SOURCES)
        return ProductAnswer(
            kind=AnswerKind.APPROVED_FACT,
            statement=claim["statement"],
            provider_id=provider_id,
            product_id=product_id,
            product_version=claim.get("product_version"),
            source=claim["source"],
        )

    def compare_fees(
        self, provider_id: str, product_ids: tuple[str, ...], fee_code: str
    ) -> FeeComparison:
        """Side by side, from approved facts only, with no ranking and no recommendation."""
        rows = tuple(
            FeeComparisonRow(
                product_id=product_id,
                answer=self.answer(provider_id, product_id, ProductQuestion.FEE, fee_code),
            )
            for product_id in product_ids
        )
        facts = [row for row in rows if row.answer.kind is AnswerKind.APPROVED_FACT]
        comparable = len(facts) >= 2 and len(facts) == len(rows)
        return FeeComparison(
            comparable=comparable,
            fee_code=fee_code,
            rows=rows,
            note=(
                "Approved facts side by side. This is not a recommendation."
                if comparable
                else "The approved information does not support this comparison."
            ),
        )


def _matching_claim(
    claims: list[dict[str, Any]], question: ProductQuestion, subject: str | None
) -> dict[str, Any] | None:
    kinds = {
        ProductQuestion.FEE: "FEE",
        ProductQuestion.PROVIDER_IDENTITY: "PROVIDER_IDENTITY",
        ProductQuestion.PARTNER: "PARTNER_ATTRIBUTION",
    }
    return next(
        (
            claim
            for claim in claims
            if claim["kind"] == kinds[question]
            and (question is ProductQuestion.PROVIDER_IDENTITY or claim["subject"] == subject)
        ),
        None,
    )
