"""An LLM's text passes the bank's deterministic checks before anyone reads it.

`app/agents/llm.py` is unchanged, because its digest is recorded evidence of the Phase 05
evaluation.
This wrapper is bank code at the agent's output boundary (the agent package itself stays behind the
tool gateway): whatever the model writes, the customer receives either that text, verified, or a
fixed answer that says the evidence is insufficient. It covers an agent run through this boundary,
not
a third party's agent, which can use `POST /v1/communication/verify` but cannot be forced to.

The historical 20 of 20 evaluation did not include this wrapper and proves nothing about it. No paid
model call has been made with it. Its tests use a fake model client.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from app.services.regulated_communication import CommunicationGuard, GuardResult, Verdict


class _RunsLlm(Protocol):
    def run(self, request: str, *, customer_ref: str | None = None) -> Any: ...  # noqa: ANN401


@dataclass(frozen=True)
class VerifiedRunResult:
    run: Any  # an `LlmRunResult`; its `final_text` is what may be shown
    guard: GuardResult
    model_text_withheld: bool


class VerifiedLlmAgent:
    def __init__(
        self,
        agent: _RunsLlm,
        guard: CommunicationGuard,
        *,
        provider_id: str,
        product_id: str | None,
    ) -> None:
        self._agent = agent
        self._guard = guard
        self._provider_id = provider_id
        self._product_id = product_id

    def run(self, request: str, *, customer_ref: str | None = None) -> VerifiedRunResult:
        result = self._agent.run(request, customer_ref=customer_ref)
        verdict = self._guard.check(
            result.final_text, provider_id=self._provider_id, product_id=self._product_id
        )
        withheld = verdict.verdict is not Verdict.RELEASE
        released = result.model_copy(update={"final_text": verdict.released_text})
        return VerifiedRunResult(released, verdict, withheld)
