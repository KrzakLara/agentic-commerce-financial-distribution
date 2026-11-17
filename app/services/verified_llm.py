"""A model's proposal becomes an answer only through the bank's checks. The enforced pipeline.

    question -> sensitive-input screen -> proposer (model or offline rules, read-only tools)
    -> refused-tool check -> schema validation -> `CommunicationGuard.release` (verified source
    lookup and deterministic claim checks, then rendering from facts and fixed templates)
    -> `RegulatedAnswer`

The model's text never leaves. If it writes a correct sentence, that sentence is not released; if it
writes a false one, nothing changes. The server enforces this, and a prompt is not part of the
control: the proposal has no field for prose, and the answer is built from bank data only.

Reachable through the A2A skill `product-question` and `POST /v1/agent/questions`. It does not cover
a third party's agent, which holds its own model (see `docs/regulated-communication.md`).

Status with a real model: IMPLEMENTED BUT NOT LIVE-VALIDATED. No paid call has been made through
this pipeline, and the historical 20 of 20 evaluation did not include it and proves nothing about
it.
"""

import hashlib
import json
import threading
from dataclasses import dataclass

from pydantic import ValidationError

from app.agents.answer_proposer import AnswerProposer, ProposerRun, ProposerStatus
from app.integrations.agent_tools import (
    AgentToolCall,
    AgentToolName,
    AgentToolResult,
    AgentVisibleStatus,
)
from app.services.communication_contract import (
    AnswerOutcome,
    GeneratedBy,
    ModelProposal,
    NotEstablishedTopic,
    QuestionRequest,
    ReasonCode,
    RegulatedAnswer,
)
from app.services.regulated_communication import CommunicationGuard

_MODEL_FAILURES = {
    ProposerStatus.UNAVAILABLE: ReasonCode.MODEL_UNAVAILABLE,
    ProposerStatus.REFUSED: ReasonCode.MODEL_REFUSED,
    ProposerStatus.TRUNCATED: ReasonCode.MODEL_OUTPUT_TRUNCATED,
    ProposerStatus.ROUND_LIMIT: ReasonCode.MODEL_ROUND_LIMIT,
    ProposerStatus.INVALID_RESPONSE: ReasonCode.PROPOSAL_SCHEMA_INVALID,
}


class QuestionBudget:
    """How many paid model runs this process may make. It bounds cost; it is not a rate limit."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._used >= self._limit:
                return False
            self._used += 1
            return True


class VerifiedFactsGateway:
    """The two read-only tools, answered from verified facts. Nothing else is reachable here.

    It holds no customer data, so no question can bring any into model traffic. Narrative provider
    text stays out too: such a fact carries its identifier and no value.
    """

    def __init__(self, guard: CommunicationGuard) -> None:
        self._guard = guard
        self.executed: list[str] = []

    def execute(self, call: AgentToolCall) -> AgentToolResult:
        self.executed.append(call.tool.value)
        if call.tool is AgentToolName.DISCOVER_PROVIDER:
            return self._discover(call)
        if call.tool is AgentToolName.GET_OFFICIAL_PRODUCT:
            return self._product(call)
        return AgentToolResult(tool=call.tool, status=AgentVisibleStatus.DENIED)

    def _discover(self, call: AgentToolCall) -> AgentToolResult:
        provider_id = str(call.arguments["provider_id"])
        lookup = self._guard.facts_for_model(provider_id, None)
        if lookup.problem is not None:
            return AgentToolResult(
                tool=call.tool,
                status=AgentVisibleStatus.DENIED,
                data={"reason": lookup.problem.value},
            )
        return AgentToolResult(
            tool=call.tool,
            status=AgentVisibleStatus.OK,
            data={
                "provider_id": provider_id,
                "product_ids": self._guard.product_ids(provider_id),
                "facts": [fact.for_model() for fact in lookup.facts],
                "topics_that_may_be_unsupported": [topic.value for topic in NotEstablishedTopic],
            },
        )

    def _product(self, call: AgentToolCall) -> AgentToolResult:
        provider_id = str(call.arguments["provider_id"])
        product_id = str(call.arguments["product_id"])
        lookup = self._guard.facts_for_model(provider_id, product_id)
        if lookup.problem is not None:
            return AgentToolResult(
                tool=call.tool,
                status=AgentVisibleStatus.DENIED,
                data={"reason": lookup.problem.value},
            )
        return AgentToolResult(
            tool=call.tool,
            status=AgentVisibleStatus.OK,
            data={
                "product_id": product_id,
                "facts": [
                    fact.for_model() for fact in lookup.facts if fact.product_id == product_id
                ],
            },
        )


@dataclass(frozen=True)
class VerifiedRunResult:
    """The answer, and what the audit trail records about how it came about."""

    answer: RegulatedAnswer
    run: ProposerRun | None
    question_digest: str
    proposal_digest: str | None = None
    # Always true: a model's text is never what is released. Kept explicit for the audit trail.
    model_text_withheld: bool = True


class VerifiedLlmAgent:
    def __init__(
        self,
        guard: CommunicationGuard,
        proposer: AnswerProposer,
        *,
        budget: QuestionBudget | None = None,
    ) -> None:
        self._guard = guard
        self._proposer = proposer
        self._budget = budget

    def answer(self, request: QuestionRequest) -> VerifiedRunResult:
        question_digest = hashlib.sha256(request.question.encode("utf-8")).hexdigest()
        before_any_model = GeneratedBy(proposer=self._proposer.name)

        def refused(outcome: AnswerOutcome, reason: ReasonCode) -> RegulatedAnswer:
            return self._guard.refuse(
                outcome, reason, provider_id=request.provider_id, generated_by=before_any_model
            )

        screened = self._guard.screen_question(request.question)
        if screened is not None:
            return VerifiedRunResult(
                refused(AnswerOutcome.UNSUPPORTED_REQUEST, screened), None, question_digest
            )
        if self._proposer.billable and self._budget is not None and not self._budget.take():
            return VerifiedRunResult(
                refused(AnswerOutcome.REQUIRES_HUMAN_REVIEW, ReasonCode.MODEL_BUDGET_EXHAUSTED),
                None,
                question_digest,
            )
        run = self._proposer.propose(
            json.dumps(
                {
                    "customer_question": request.question,
                    "provider_id": request.provider_id,
                    "product_ids": list(request.product_ids),
                },
                ensure_ascii=False,
            )
        )
        generated_by = GeneratedBy(proposer=run.proposer, model=run.model)
        proposal_digest = (
            hashlib.sha256(run.proposal_text.encode("utf-8")).hexdigest()
            if run.proposal_text
            else None
        )

        def stopped(reason: ReasonCode) -> VerifiedRunResult:
            answer = self._guard.refuse(
                AnswerOutcome.REQUIRES_HUMAN_REVIEW,
                reason,
                provider_id=request.provider_id,
                generated_by=generated_by,
            )
            return VerifiedRunResult(answer, run, question_digest, proposal_digest)

        # A model that reached for a tool it was never offered is not trusted for this answer, even
        # if what it finally proposed looks correct.
        if run.rejected_tools:
            return stopped(ReasonCode.UNAUTHORIZED_TOOL_ATTEMPT)
        if run.status is not ProposerStatus.COMPLETED or run.proposal_text is None:
            return stopped(_MODEL_FAILURES.get(run.status, ReasonCode.PROPOSAL_SCHEMA_INVALID))
        try:
            proposal = ModelProposal.model_validate_json(run.proposal_text)
        except ValidationError:
            return stopped(ReasonCode.PROPOSAL_SCHEMA_INVALID)
        answer = self._guard.release(
            proposal,
            provider_id=request.provider_id,
            requested_product_ids=request.product_ids,
            generated_by=generated_by,
        )
        return VerifiedRunResult(answer, run, question_digest, proposal_digest)
