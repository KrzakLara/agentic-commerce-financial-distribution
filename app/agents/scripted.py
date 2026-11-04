"""Deterministic scripted customer agent. No LLM, no API key, no network of its own.

It performs a fixed tool-call sequence through the `AgentToolGateway` and stops at the
first step the server does not answer acceptably. It cannot confirm a handoff, approve KYC,
sign, or set OPENED: those tools do not exist in its vocabulary, and the backend would reject
them anyway. It never reads or writes a repository.

Phases of the journey (the customer's confirmation happens in between, elsewhere):

1. ``open_depot``            discover -> product -> draft -> request handoff -> status
2. (customer confirms on the bank-controlled page; the agent is not involved)
3. ``continue_after_customer``  status -> submit_after_consent -> status
4. (the depot provider confirms; the agent is not involved)
5. ``read_status``           the final status as reported by the backend
"""

from typing import Any

from app.agents.base import AgentRunResult, OpenDepotIntent, draft_idempotency_key
from app.integrations.agent_tools import (
    AgentToolCall,
    AgentToolGateway,
    AgentToolName,
    AgentToolResult,
    AgentVisibleStatus,
)

_OK_ONLY = frozenset({AgentVisibleStatus.OK})
_PROGRESS_STATUSES = frozenset(
    {AgentVisibleStatus.OK, AgentVisibleStatus.IN_PROGRESS, AgentVisibleStatus.ACTION_REQUIRED}
)


class _Run:
    """Executes steps through the gateway and records every result."""

    def __init__(self, gateway: AgentToolGateway) -> None:
        self._gateway = gateway
        self.steps: list[AgentToolResult] = []

    def call(
        self,
        tool: AgentToolName,
        arguments: dict[str, Any],
        accepted: frozenset[AgentVisibleStatus] = _OK_ONLY,
    ) -> AgentToolResult | None:
        result = self._gateway.execute(AgentToolCall(tool=tool, arguments=arguments))
        self.steps.append(result)
        return result if result.status in accepted else None

    def stopped(self, tool: AgentToolName, reason: str | None = None) -> AgentRunResult:
        return AgentRunResult(
            steps=tuple(self.steps),
            stopped_at=tool,
            last_status=self.steps[-1].status if self.steps else None,
            stop_reason=reason,
        )

    def finished(self) -> AgentRunResult:
        return AgentRunResult(steps=tuple(self.steps), last_status=self.steps[-1].status)


class ScriptedCustomerAgent:
    mode = "scripted"

    def __init__(self, gateway: AgentToolGateway) -> None:
        self._gateway = gateway

    def _create_draft(
        self, run: _Run, intent: OpenDepotIntent, provider_id: str, product_id: str, channel_id: str
    ) -> AgentToolResult | None:
        draft = run.call(
            AgentToolName.CREATE_DRAFT,
            {
                "provider_id": provider_id,
                "product_id": product_id,
                "channel_id": channel_id,
                "customer_ref": intent.customer_ref,
                "idempotency_key": str(draft_idempotency_key(intent)),
            },
        )
        if draft is None or not isinstance(draft.data.get("application_id"), str):
            return None
        return draft

    def draft_application(self, intent: OpenDepotIntent) -> AgentRunResult:
        """Phase 01 slice: create a draft, then read back its generic status."""
        run = _Run(self._gateway)
        draft = self._create_draft(
            run, intent, intent.provider_id, intent.product_id, intent.channel_id
        )
        if draft is None:
            return run.stopped(AgentToolName.CREATE_DRAFT)
        status = run.call(
            AgentToolName.GET_GENERIC_STATUS,
            {"application_id": draft.data["application_id"]},
            _PROGRESS_STATUSES,
        )
        if status is None:
            return run.stopped(AgentToolName.GET_GENERIC_STATUS)
        return run.finished()

    def open_depot(self, intent: OpenDepotIntent) -> AgentRunResult:
        """Discover, validate, draft, and ask for the customer's approval (handoff).

        The application is prepared ONLY from values the server's verified catalog returned:
        a provider, product or channel that discovery does not offer ends the run.
        """
        run = _Run(self._gateway)

        provider = run.call(AgentToolName.DISCOVER_PROVIDER, {"provider_id": intent.provider_id})
        if provider is None:
            return run.stopped(AgentToolName.DISCOVER_PROVIDER)
        if provider.data.get(
            "provider_id"
        ) != intent.provider_id or intent.product_id not in provider.data.get("product_ids", []):
            return run.stopped(AgentToolName.DISCOVER_PROVIDER, "product_not_offered")

        product = run.call(
            AgentToolName.GET_OFFICIAL_PRODUCT,
            {"provider_id": intent.provider_id, "product_id": intent.product_id},
        )
        if product is None:
            return run.stopped(AgentToolName.GET_OFFICIAL_PRODUCT)
        if intent.channel_id not in product.data.get("channel_ids", []) or not product.data.get(
            "document_version"
        ):
            return run.stopped(AgentToolName.GET_OFFICIAL_PRODUCT, "channel_not_offered")

        draft = self._create_draft(
            run,
            intent,
            provider_id=provider.data["provider_id"],
            product_id=product.data["product_id"],
            channel_id=intent.channel_id,
        )
        if draft is None:
            return run.stopped(AgentToolName.CREATE_DRAFT)

        application_args = {"application_id": draft.data["application_id"]}
        if run.call(AgentToolName.REQUEST_HANDOFF, application_args) is None:
            return run.stopped(AgentToolName.REQUEST_HANDOFF)
        # "Customer action still required" is the expected status after a handoff request.
        if run.call(AgentToolName.GET_GENERIC_STATUS, application_args, _PROGRESS_STATUSES) is None:
            return run.stopped(AgentToolName.GET_GENERIC_STATUS)
        return run.finished()

    def continue_after_customer(self, application_id: str) -> AgentRunResult:
        """Resume after the customer's SEPARATE confirmation: submit, then read the status.

        If the server still reports that the customer has to act, the agent does not even
        try to submit. If the backend refuses the submit (consent or checks missing), the run
        stops with the server's generic denial - the agent cannot work around it.
        """
        run = _Run(self._gateway)
        args = {"application_id": application_id}
        before = run.call(AgentToolName.GET_GENERIC_STATUS, args, _PROGRESS_STATUSES)
        if before is None:
            return run.stopped(AgentToolName.GET_GENERIC_STATUS)
        if before.data.get("state") in {"AWAITING_CUSTOMER", "DRAFT"}:
            return run.stopped(AgentToolName.GET_GENERIC_STATUS, "waiting_for_customer")
        if run.call(AgentToolName.SUBMIT_AFTER_CONSENT, args, _PROGRESS_STATUSES) is None:
            return run.stopped(AgentToolName.SUBMIT_AFTER_CONSENT)
        if run.call(AgentToolName.GET_GENERIC_STATUS, args, _PROGRESS_STATUSES) is None:
            return run.stopped(AgentToolName.GET_GENERIC_STATUS)
        return run.finished()

    def read_status(self, application_id: str) -> AgentRunResult:
        """Report exactly what the backend says - including that nothing is opened yet."""
        run = _Run(self._gateway)
        if (
            run.call(
                AgentToolName.GET_GENERIC_STATUS,
                {"application_id": application_id},
                _PROGRESS_STATUSES | {AgentVisibleStatus.CLOSED},
            )
            is None
        ):
            return run.stopped(AgentToolName.GET_GENERIC_STATUS)
        return run.finished()
