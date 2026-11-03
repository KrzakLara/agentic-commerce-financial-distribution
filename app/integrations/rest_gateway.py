"""`AgentToolGateway` implemented over the PUBLIC REST API.

The scripted (or, later, LLM) agent calls tools; this adapter turns them into HTTP requests
to ``/v1/...`` exactly as any external client would. It has no access to repositories,
services or the database, and sends the same credentials a real client would, so every
authorization, ownership, mandate and idempotency check on the server applies.

It sends ONLY the agent's own bearer tokens (Phase 03): the bootstrap token to create a draft,
the application's onboarding token for handoff/submit, its read-only status token for status
reads. It has no way to present a customer identity, to confirm a handoff, to call the
provider callback, to mint or widen a token, or to set a state.

It maps HTTP outcomes to the coarse `AgentVisibleStatus` vocabulary and never forwards
response bodies, error messages or internal detail back to the agent. Only validated, public
fields are copied into the tool result - never a token.
"""

from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.domain.visibility import AgentVisibleState
from app.integrations.agent_credentials import AgentCredentials, ApplicationTokens
from app.integrations.agent_tools import (
    AgentToolCall,
    AgentToolName,
    AgentToolResult,
    AgentVisibleStatus,
    CreateDraftArgs,
    DiscoverProviderArgs,
    GetGenericStatusArgs,
    GetOfficialProductArgs,
    RequestHandoffArgs,
    SubmitAfterConsentArgs,
)

_STATE_TO_STATUS: dict[AgentVisibleState, AgentVisibleStatus] = {
    AgentVisibleState.DRAFT: AgentVisibleStatus.OK,
    AgentVisibleState.AWAITING_CUSTOMER: AgentVisibleStatus.ACTION_REQUIRED,
    AgentVisibleState.IN_PROGRESS: AgentVisibleStatus.IN_PROGRESS,
    AgentVisibleState.ACTION_REQUIRED: AgentVisibleStatus.ACTION_REQUIRED,
    AgentVisibleState.OPENED: AgentVisibleStatus.OK,
    AgentVisibleState.DECLINED: AgentVisibleStatus.CLOSED,
    AgentVisibleState.CANCELLED: AgentVisibleStatus.CLOSED,
    AgentVisibleState.FAILED: AgentVisibleStatus.CLOSED,
}
_DENIED_HTTP = {400, 401, 403, 404, 409, 410, 422}


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ApplicationView(_Lenient):
    application_id: str
    state: AgentVisibleState


class _TokenView(_Lenient):
    access_token: str


class _AuthorizationView(_Lenient):
    onboarding: _TokenView
    status: _TokenView


class _CreatedView(_ApplicationView):
    authorization: _AuthorizationView | None = None  # null on an idempotent replay


class _ChannelView(_Lenient):
    channel_id: str


class _ManifestView(_Lenient):
    provider_id: str
    legal_name: str
    manifest_version: str
    manifest_digest: str
    is_mock: bool
    channels: list[_ChannelView]
    product_ids: list[str]


class _ProductView(_Lenient):
    product_id: str
    provider_id: str
    name: str
    catalog_version: str
    document_id: str
    document_version: str
    document_digest: str
    channel_ids: list[str]


class _ProductsView(_Lenient):
    products: list[_ProductView]


class _ActionView(_Lenient):
    provider_id: str
    product_id: str
    channel_id: str
    document_id: str
    document_version: str


class _HandoffView(_Lenient):
    handoff_id: str
    application_id: str
    state: AgentVisibleState
    expires_at: str
    action_digest: str
    action: _ActionView
    confirmation_url: str


def _unavailable(tool: AgentToolName, reason: str) -> AgentToolResult:
    return AgentToolResult(
        tool=tool, status=AgentVisibleStatus.UNAVAILABLE, data={"reason": reason}
    )


def _bearer(token: str | None) -> dict[str, str]:
    # No token: send none at all, and let the server refuse (fail closed on its side).
    return {"Authorization": f"Bearer {token}"} if token else {}


class RestAgentToolGateway:
    def __init__(self, client: httpx.Client, *, credentials: AgentCredentials) -> None:
        self._client = client
        self._credentials = credentials
        self._handlers: dict[AgentToolName, Callable[[AgentToolCall], AgentToolResult]] = {
            AgentToolName.DISCOVER_PROVIDER: self._discover_provider,
            AgentToolName.GET_OFFICIAL_PRODUCT: self._get_official_product,
            AgentToolName.CREATE_DRAFT: self._create_draft,
            AgentToolName.REQUEST_HANDOFF: self._request_handoff,
            AgentToolName.GET_GENERIC_STATUS: self._get_generic_status,
            AgentToolName.SUBMIT_AFTER_CONSENT: self._submit_after_consent,
        }

    def execute(self, call: AgentToolCall) -> AgentToolResult:
        handler = self._handlers.get(call.tool)
        if handler is None:
            return _unavailable(call.tool, "not_available")
        try:
            return handler(call)
        except (httpx.HTTPError, ValidationError, ValueError):
            return _unavailable(call.tool, "service_unreachable_or_invalid_response")

    # --- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def _failure(call: AgentToolCall, response: httpx.Response) -> AgentToolResult | None:
        """Map non-success HTTP to a body-less result; None means 'looks successful'."""
        if response.status_code in _DENIED_HTTP:
            return AgentToolResult(tool=call.tool, status=AgentVisibleStatus.DENIED)
        if response.status_code >= 400:
            return _unavailable(call.tool, "unexpected_response")
        return None

    def _application_result(
        self, call: AgentToolCall, response: httpx.Response, ok_codes: set[int], *, map_state: bool
    ) -> AgentToolResult:
        failure = self._failure(call, response)
        if failure is not None:
            return failure
        if response.status_code not in ok_codes:
            return _unavailable(call.tool, "unexpected_response")
        view = _ApplicationView.model_validate(response.json())
        status = _STATE_TO_STATUS[view.state] if map_state else AgentVisibleStatus.OK
        return AgentToolResult(
            tool=call.tool,
            status=status,
            data={"application_id": view.application_id, "state": view.state.value},
        )

    # --- tools -----------------------------------------------------------------------------------

    def _discover_provider(self, call: AgentToolCall) -> AgentToolResult:
        args = DiscoverProviderArgs.model_validate(call.arguments)
        response = self._client.get(f"/v1/providers/{args.provider_id}/manifest")
        failure = self._failure(call, response)
        if failure is not None:
            return failure
        view = _ManifestView.model_validate(response.json())
        if view.provider_id != args.provider_id:  # never trust a response for another provider
            return AgentToolResult(tool=call.tool, status=AgentVisibleStatus.DENIED)
        data: dict[str, Any] = {
            "provider_id": view.provider_id,
            "legal_name": view.legal_name,
            "manifest_version": view.manifest_version,
            "manifest_digest": view.manifest_digest,
            "is_mock": view.is_mock,
            "channel_ids": [c.channel_id for c in view.channels],
            "product_ids": view.product_ids,
        }
        return AgentToolResult(tool=call.tool, status=AgentVisibleStatus.OK, data=data)

    def _get_official_product(self, call: AgentToolCall) -> AgentToolResult:
        args = GetOfficialProductArgs.model_validate(call.arguments)
        response = self._client.get("/v1/products", params={"provider_id": args.provider_id})
        failure = self._failure(call, response)
        if failure is not None:
            return failure
        products = _ProductsView.model_validate(response.json()).products
        match = next(
            (
                p
                for p in products
                if p.product_id == args.product_id and p.provider_id == args.provider_id
            ),
            None,
        )
        if match is None:
            return AgentToolResult(tool=call.tool, status=AgentVisibleStatus.DENIED)
        return AgentToolResult(
            tool=call.tool, status=AgentVisibleStatus.OK, data=match.model_dump()
        )

    def _tokens(self, application_id: str) -> ApplicationTokens | None:
        return self._credentials.for_application(application_id)

    def _create_draft(self, call: AgentToolCall) -> AgentToolResult:
        args = CreateDraftArgs.model_validate(call.arguments)
        response = self._client.post(
            "/v1/applications",
            headers={
                **_bearer(self._credentials.bootstrap),
                "Idempotency-Key": str(args.idempotency_key),
            },
            json=args.model_dump(mode="json", exclude={"idempotency_key"}),
        )
        failure = self._failure(call, response)
        if failure is not None:
            return failure
        if response.status_code not in {200, 201}:
            return _unavailable(call.tool, "unexpected_response")
        view = _CreatedView.model_validate(response.json())
        if view.authorization is not None:  # first creation: keep the tokens in the runtime
            self._credentials.remember(
                view.application_id,
                ApplicationTokens(
                    onboarding=view.authorization.onboarding.access_token,
                    status=view.authorization.status.access_token,
                ),
            )
        return AgentToolResult(  # the tool result never contains a token
            tool=call.tool,
            status=AgentVisibleStatus.OK,
            data={"application_id": view.application_id, "state": view.state.value},
        )

    def _request_handoff(self, call: AgentToolCall) -> AgentToolResult:
        args = RequestHandoffArgs.model_validate(call.arguments)
        tokens = self._tokens(args.application_id)
        response = self._client.post(
            f"/v1/applications/{args.application_id}/handoff",
            headers=_bearer(tokens.onboarding if tokens else None),
        )
        failure = self._failure(call, response)
        if failure is not None:
            return failure
        if response.status_code != 201:
            return _unavailable(call.tool, "unexpected_response")
        view = _HandoffView.model_validate(response.json())
        return AgentToolResult(
            tool=call.tool,
            status=AgentVisibleStatus.OK,
            data={
                "handoff_id": view.handoff_id,
                "application_id": view.application_id,
                "state": view.state.value,
                "expires_at": view.expires_at,
                "action_digest": view.action_digest,
                "document_version": view.action.document_version,
                "confirmation_url": view.confirmation_url,
            },
        )

    def _get_generic_status(self, call: AgentToolCall) -> AgentToolResult:
        args = GetGenericStatusArgs.model_validate(call.arguments)
        tokens = self._tokens(args.application_id)
        response = self._client.get(  # least privilege: the read-only status token
            f"/v1/applications/{args.application_id}",
            headers=_bearer(tokens.status if tokens else None),
        )
        return self._application_result(call, response, {200}, map_state=True)

    def _submit_after_consent(self, call: AgentToolCall) -> AgentToolResult:
        args = SubmitAfterConsentArgs.model_validate(call.arguments)
        tokens = self._tokens(args.application_id)
        response = self._client.post(
            f"/v1/applications/{args.application_id}/submit",
            headers=_bearer(tokens.onboarding if tokens else None),
        )
        return self._application_result(call, response, {200, 202}, map_state=True)
