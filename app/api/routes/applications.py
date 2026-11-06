"""Agent-facing application endpoints (ADR section 05).

create draft, read generic state, request a customer handoff, submit after consent.

Every route requires `Authorization: Bearer <token>` with one operation-specific scope:

=================================== =======================
POST /v1/applications               ``create_draft``  (bootstrap grant, single use)
GET  /v1/applications/{id}          ``read_status``   (status token of that application)
POST /v1/applications/{id}/handoff  ``request_handoff`` (onboarding token of that application)
POST /v1/applications/{id}/submit   ``submit_after_consent`` (onboarding token)
=================================== =======================

There is intentionally NO endpoint that sets an application's state or issues/extends tokens.
`handoff` and `submit` are *requests*: the trusted core checks token, binding, mandate,
prerequisites and policy, and performs any state change itself with its own authority.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Request, Response, status

from app.api.auth import CanCreateDraft, CanReadStatus, CanRequestHandoff, CanSubmit
from app.api.deps import (
    ApplicationServiceDep,
    CorrelationIdDep,
    DelegationServiceDep,
    OnboardingServiceDep,
)
from app.api.schemas.applications import (
    ApplicationAuthorizationView,
    ApplicationCreatedResponse,
    ApplicationResponse,
    CreateApplicationRequest,
)
from app.api.schemas.errors import ErrorResponse
from app.api.schemas.handoffs import HandoffResponse

router = APIRouter(prefix="/v1/applications", tags=["applications"])

_ERRORS = {
    401: {
        "model": ErrorResponse,
        "description": "Missing, invalid, expired or revoked bearer token.",
    },
    403: {
        "model": ErrorResponse,
        "description": "Token lacks the scope (`insufficient_scope`) or the mandate is inactive.",
    },
    404: {
        "model": ErrorResponse,
        "description": "Unknown application, or not covered by the caller's token.",
    },
}
_AppId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post(
    "",
    response_model=ApplicationCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft application (idempotent, consumes the bootstrap grant)",
    description=(
        "Requires a bootstrap token (scope `create_draft`) that names this agent and this "
        "`customer_ref`; it can create exactly ONE application. Requires an `Idempotency-Key` "
        "(UUID). The 201 response carries the application-bound tokens - shown once: an "
        "`onboarding` token (`request_handoff submit_after_consent`) and a read-only `status` "
        "token (`read_status`). The same agent + key + identical body returns the original "
        "application (HTTP 200, `Idempotent-Replayed: true`, `authorization: null`); the same "
        "key with a different body is rejected with 409. All body fields are untrusted "
        "proposals."
    ),
    responses={
        200: {
            "model": ApplicationCreatedResponse,
            "description": "Idempotent replay of an earlier request (no new tokens).",
        },
        **_ERRORS,
        409: {
            "model": ErrorResponse,
            "description": "Idempotency-Key reused with a different body.",
        },
    },
)
def create_application(
    body: CreateApplicationRequest,
    response: Response,
    principal: CanCreateDraft,
    service: ApplicationServiceDep,
    delegation: DelegationServiceDep,
    correlation_id: CorrelationIdDep,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApplicationCreatedResponse:
    delegation.enforce_action(
        principal,
        provider_id=body.provider_id,
        product_id=body.product_id,
        channel_id=body.channel_id,
        correlation_id=correlation_id,
    )
    result = service.create_application(
        principal=principal,
        command=body.to_command(),
        idempotency_key=str(idempotency_key),
        correlation_id=correlation_id,
    )
    _no_store(response)  # the body may carry credentials
    if result.credentials is not None:
        delegation.link_application_grants(
            bootstrap_mandate_id=principal.mandate_id,
            mandate_ids=(
                result.credentials.onboarding.mandate_id,
                result.credentials.status.mandate_id,
            ),
        )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    view = ApplicationResponse.from_domain(result.application)
    return ApplicationCreatedResponse(
        **view.model_dump(),
        authorization=(
            ApplicationAuthorizationView.from_credentials(result.credentials)
            if result.credentials
            else None
        ),
    )


@router.get(
    "/{application_id}",
    response_model=ApplicationResponse,
    summary="Get the generic state of your own application",
    description=(
        "Requires the application's `read_status` token. Returns only a coarse state. Another "
        "actor's application - or one your token is not bound to - is indistinguishable from "
        "a missing one (404)."
    ),
    responses=_ERRORS,
)
def get_application(
    application_id: _AppId,
    principal: CanReadStatus,
    service: ApplicationServiceDep,
    correlation_id: CorrelationIdDep,
) -> ApplicationResponse:
    application = service.get_application(
        principal=principal, application_id=application_id, correlation_id=correlation_id
    )
    return ApplicationResponse.from_domain(application)


@router.post(
    "/{application_id}/handoff",
    response_model=HandoffResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request direct customer approval (handoff)",
    description=(
        "Requires the application's onboarding token (scope `request_handoff`). Creates a "
        "one-time, expiring challenge bound to this application and to the exact action "
        "(provider, product, channel, disclosure document version). The agent only *requests* "
        "it. The customer must separately sign in to the bank-controlled page and confirm; the "
        "returned `confirmation_url` is a pointer, not an authorization. A new handoff "
        "supersedes any earlier unused one."
    ),
    responses={
        **_ERRORS,
        409: {"model": ErrorResponse, "description": "Application not in a state for a handoff."},
    },
)
def request_handoff(
    application_id: _AppId,
    request: Request,
    response: Response,
    principal: CanRequestHandoff,
    service: OnboardingServiceDep,
    correlation_id: CorrelationIdDep,
) -> HandoffResponse:
    issue = service.request_handoff(
        principal=principal, application_id=application_id, correlation_id=correlation_id
    )
    _no_store(response)  # the body carries the one-time challenge
    base = str(request.base_url).rstrip("/")
    url = f"{base}/customer/handoffs/{issue.handoff.handoff_id}?token={issue.nonce}"
    return HandoffResponse.build(issue.handoff, url)


@router.post(
    "/{application_id}/submit",
    response_model=ApplicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit after consent",
    description=(
        "Requires the application's onboarding token (scope `submit_after_consent`). Hands the "
        "application to the depot provider, but only if the deterministic policy gate finds "
        "verified customer consent, passed mandatory checks and an active mandate (checks a "
        "crash left unfinished are resumed first). Otherwise 409 `prerequisites_not_met` with "
        "no further detail. The response never says OPENED: only the provider's trusted "
        "confirmation does that. A repeated submit returns 200 without a second hand-over."
    ),
    responses={
        200: {"model": ApplicationResponse, "description": "Already submitted (idempotent)."},
        **_ERRORS,
        409: {"model": ErrorResponse, "description": "Prerequisites not met."},
    },
)
def submit_application(
    application_id: _AppId,
    response: Response,
    principal: CanSubmit,
    service: OnboardingServiceDep,
    correlation_id: CorrelationIdDep,
) -> ApplicationResponse:
    result = service.submit_application(
        principal=principal, application_id=application_id, correlation_id=correlation_id
    )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return ApplicationResponse.from_domain(result.application)
