"""Agent-facing application endpoints (ADR section 05).

create draft, read generic state, request a customer handoff, submit after consent.

There is intentionally NO endpoint that sets an application's state. `handoff` and `submit` are
*requests*: the trusted core checks mandate, ownership, prerequisites and policy, and performs
any state change itself with its own authority (`OnboardingService`).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Path, Request, Response, status

from app.api.auth import CurrentAgentActor
from app.api.deps import ApplicationServiceDep, CorrelationIdDep, OnboardingServiceDep
from app.api.schemas.applications import ApplicationResponse, CreateApplicationRequest
from app.api.schemas.errors import ErrorResponse
from app.api.schemas.handoffs import HandoffResponse

router = APIRouter(prefix="/v1/applications", tags=["applications"])

_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing or invalid (dev) actor identity."},
    404: {
        "model": ErrorResponse,
        "description": "Unknown application, or not owned by the caller.",
    },
}


@router.post(
    "",
    response_model=ApplicationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a draft application (idempotent)",
    description=(
        "Creates a DRAFT application owned by the calling actor. Requires an `Idempotency-Key` "
        "(UUID). The same actor + key + identical body returns the original application "
        "(HTTP 200, `Idempotent-Replayed: true`); the same key with a different body is "
        "rejected with 409. All body fields are untrusted proposals."
    ),
    responses={
        200: {
            "model": ApplicationResponse,
            "description": "Idempotent replay of an earlier request.",
        },
        401: _ERRORS[401],
        409: {
            "model": ErrorResponse,
            "description": "Idempotency-Key reused with a different body.",
        },
    },
)
def create_application(
    body: CreateApplicationRequest,
    response: Response,
    actor: CurrentAgentActor,
    service: ApplicationServiceDep,
    correlation_id: CorrelationIdDep,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> ApplicationResponse:
    result = service.create_application(
        actor_id=actor.agent_actor_id,
        command=body.to_command(),
        idempotency_key=str(idempotency_key),
        correlation_id=correlation_id,
    )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    return ApplicationResponse.from_domain(result.application)


@router.get(
    "/{application_id}",
    response_model=ApplicationResponse,
    summary="Get the generic state of your own application",
    description=(
        "Returns only the caller's own application with a coarse state. Another actor's "
        "application is indistinguishable from a missing one (404)."
    ),
    responses={401: _ERRORS[401], 404: _ERRORS[404]},
)
def get_application(
    application_id: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")],
    actor: CurrentAgentActor,
    service: ApplicationServiceDep,
    correlation_id: CorrelationIdDep,
) -> ApplicationResponse:
    application = service.get_application(
        actor_id=actor.agent_actor_id,
        application_id=application_id,
        correlation_id=correlation_id,
    )
    return ApplicationResponse.from_domain(application)


_AppId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")]


@router.post(
    "/{application_id}/handoff",
    response_model=HandoffResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request direct customer approval (handoff)",
    description=(
        "Creates a one-time, expiring challenge bound to this application and to the exact "
        "action (provider, product, channel, disclosure document version). The agent only "
        "*requests* it. The customer must separately sign in to the bank-controlled page and "
        "confirm; the returned `confirmation_url` is a pointer, not an authorization. A new "
        "handoff supersedes any earlier unused one."
    ),
    responses={
        401: _ERRORS[401],
        403: {"model": ErrorResponse, "description": "Mandate inactive."},
        404: _ERRORS[404],
        409: {"model": ErrorResponse, "description": "Application not in a state for a handoff."},
    },
)
def request_handoff(
    application_id: _AppId,
    request: Request,
    response: Response,
    actor: CurrentAgentActor,
    service: OnboardingServiceDep,
    correlation_id: CorrelationIdDep,
) -> HandoffResponse:
    issue = service.request_handoff(
        actor_id=actor.agent_actor_id, application_id=application_id, correlation_id=correlation_id
    )
    response.headers["Cache-Control"] = "no-store"  # the body carries the one-time challenge
    base = str(request.base_url).rstrip("/")
    url = f"{base}/customer/handoffs/{issue.handoff.handoff_id}?token={issue.nonce}"
    return HandoffResponse.build(issue.handoff, url)


@router.post(
    "/{application_id}/submit",
    response_model=ApplicationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit after consent",
    description=(
        "Hands the application to the depot provider, but only if the deterministic policy "
        "gate finds verified customer consent, passed mandatory checks and an active mandate. "
        "Otherwise 409 `prerequisites_not_met` with no further detail. The response never says "
        "OPENED: only the provider's trusted confirmation does that. A repeated submit returns "
        "200 without a second hand-over."
    ),
    responses={
        200: {"model": ApplicationResponse, "description": "Already submitted (idempotent)."},
        401: _ERRORS[401],
        403: {"model": ErrorResponse, "description": "Mandate inactive."},
        404: _ERRORS[404],
        409: {"model": ErrorResponse, "description": "Prerequisites not met."},
    },
)
def submit_application(
    application_id: _AppId,
    response: Response,
    actor: CurrentAgentActor,
    service: OnboardingServiceDep,
    correlation_id: CorrelationIdDep,
) -> ApplicationResponse:
    result = service.submit_application(
        actor_id=actor.agent_actor_id, application_id=application_id, correlation_id=correlation_id
    )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return ApplicationResponse.from_domain(result.application)
