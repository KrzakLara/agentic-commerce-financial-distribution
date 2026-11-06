"""Customer-side confirmation (bank-controlled channel).

* ``POST /v1/customer/handoffs/{id}/confirm`` - the JSON ceremony from ADR section 05: requires
  the customer's SERVER-SIDE session (cookie ``trusteq_customer_api``) and its CSRF token
  (``X-CSRF-Token``), like the form on the page. The self-asserted DEV header that this route
  used to accept (finding F-1) is gone.
* ``POST /v1/customer/applications/{id}/request-human`` - the customer asks for a person
  (Phase 04): requires the customer's SERVER-SIDE session (cookie ``trusteq_customer_api``) and
  its CSRF token (``X-CSRF-Token``); the self-asserted DEV header is not accepted here.
* ``POST /v1/customer/handoffs/{id}/tax-information`` and ``.../appropriateness`` - what the
  customer supplies before confirming, with the same session, CSRF and one-time challenge.
* ``/customer/...`` - the minimal HTML pages a customer actually sees (not part of the OpenAPI
  contract), with a server-side session and CSRF tokens (Phase 03), including the "Speak to a
  person" button (``POST /customer/applications/{id}/request-human``, Phase 04) and the tax and
  assessment forms (``POST /customer/handoffs/{id}/tax`` and ``.../assessment``).

None of these routes accepts an agent credential: a request carrying an ``Authorization``
header is refused (and audited), whatever else it carries. Possessing the handoff link (or its
nonce) is never authentication: the page shows nothing until the customer is signed in.
"""

import re
from collections.abc import Mapping
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Header, Path, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api import pages
from app.api.customer_auth import (
    API_SESSION_COOKIE,
    COOKIE_PATH,
    CSRF_HEADER,
    LOGIN_COOKIE,
    LOGIN_COOKIE_MAX_AGE,
    SESSION_COOKIE,
    SESSION_COOKIES,
    CustomerContext,
    agent_credentials_presented,
    cookie_is_secure,
    is_valid_customer_id,
    session_customer,
)
from app.api.deps import (
    AuthorizationServiceDep,
    ContainerDep,
    CorrelationIdDep,
    CustomerInformationServiceDep,
    CustomerSessionServiceDep,
    OnboardingServiceDep,
    ReviewServiceDep,
    SettingsDep,
)
from app.api.schemas.customer_information import (
    AssessmentBody,
    AssessmentResponse,
    TaxInformationBody,
    TaxInformationResponse,
)
from app.api.schemas.errors import ErrorResponse
from app.api.schemas.handoffs import HandoffConfirmRequest, HandoffConfirmResponse
from app.api.schemas.review import HumanRequestBody, HumanRequestResponse
from app.container import AppContainer
from app.core.config import Settings
from app.core.exceptions import (
    AppError,
    AuthenticationRequiredError,
    CrossSiteRequestError,
    CsrfTokenInvalidError,
)
from app.domain.enums import AuditActorType, HumanRequestCategory
from app.domain.visibility import to_agent_visible_state
from app.services.appropriateness import PROTOTYPE_NOTICE, ExperienceBand, customer_notice_for
from app.services.authorization_service import AuthorizationService
from app.services.customer_information_service import CustomerInformationService
from app.services.customer_session_service import (
    CustomerSessionService,
    csrf_matches,
    csrf_token,
    is_pre_session_secret,
    new_pre_session_secret,
)
from app.services.onboarding_service import OnboardingService
from app.services.review_service import ReviewService

router = APIRouter(tags=["customer"])

HandoffId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")]
ApplicationId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")]
_TOKEN = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_HANDOFF_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_MAX_FORM_BYTES = 4096
CSRF_CONFIRM = "confirm"
CSRF_LOGIN = "login"
CSRF_HUMAN = "human"  # bound to (session, application)
CSRF_TAX = "tax"  # bound to (session, handoff)
CSRF_ASSESSMENT = "assessment"  # bound to (session, handoff)


def _presented_agent_id(request: Request, authorization: AuthorizationService) -> str | None:
    """Which agent (if any) the presented bearer token belongs to - for the audit trail only."""
    header = request.headers.get("authorization") or ""
    scheme, _, raw = header.partition(" ")
    return authorization.identify_agent(raw.strip()) if scheme.lower() == "bearer" else None


def _refuse_agent_credentials(
    request: Request,
    handoff_id: str,
    service: OnboardingService,
    authorization: AuthorizationService,
    correlation_id: str,
) -> None:
    """Audit an agent credential presented to the customer channel (e.g. self-approval)."""
    service.record_customer_channel_denial(
        handoff_id=handoff_id,
        actor_type=AuditActorType.AGENT,
        actor_id=_presented_agent_id(request, authorization),
        reason="agent_credentials_presented",
        correlation_id=correlation_id,
    )


@router.post(
    "/v1/customer/handoffs/{handoff_id}/confirm",
    response_model=HandoffConfirmResponse,
    summary="Customer confirms the exact approved action (bank-controlled ceremony)",
    description=(
        "Requires the customer's server-side session (HttpOnly cookie `trusteq_customer_api`, "
        "set by the bank's - in this prototype SIMULATED - sign-in), the session's CSRF token "
        "for this handoff in `X-CSRF-Token`, the one-time `nonce` and the `action_digest` the "
        "customer reviewed. The self-asserted `X-Dev-Customer-Id` header is not accepted; agent "
        "credentials are refused and audited. Fails closed and changes nothing on any mismatch."
    ),
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        410: {"model": ErrorResponse},
    },
)
def confirm_handoff(
    handoff_id: HandoffId,
    body: HandoffConfirmRequest,
    request: Request,
    settings: SettingsDep,
    service: OnboardingServiceDep,
    sessions: CustomerSessionServiceDep,
    authorization: AuthorizationServiceDep,
    correlation_id: CorrelationIdDep,
    csrf_header: Annotated[
        str | None,
        Header(
            alias=CSRF_HEADER,
            description="The session's CSRF token for this handoff (from the bank page).",
        ),
    ] = None,
) -> HandoffConfirmResponse:
    if agent_credentials_presented(request):
        _refuse_agent_credentials(request, handoff_id, service, authorization, correlation_id)
        raise AuthenticationRequiredError()
    if _cross_site(request):
        raise CrossSiteRequestError()
    customer = session_customer(request, settings, sessions, API_SESSION_COOKIE)
    if customer is None or customer.session_secret is None:
        raise AuthenticationRequiredError()  # missing, invalid, expired or revoked session
    if not csrf_matches(customer.session_secret, CSRF_CONFIRM, handoff_id, csrf_header):
        service.record_customer_channel_denial(
            handoff_id=handoff_id,
            actor_type=AuditActorType.CUSTOMER,
            actor_id=customer.customer_id,
            reason="csrf_token_invalid",
            correlation_id=correlation_id,
        )
        raise CsrfTokenInvalidError()
    application = service.confirm_handoff(
        customer_id=customer.customer_id,
        handoff_id=handoff_id,
        nonce=body.nonce,
        action_digest=body.action_digest,
        correlation_id=correlation_id,
    )
    return HandoffConfirmResponse(
        handoff_id=handoff_id,
        application_id=application.application_id,
        state=to_agent_visible_state(application.status),
    )


def _human_request_customer(
    request: Request,
    application_id: str,
    *,
    settings: Settings,
    sessions: CustomerSessionService,
    reviews: ReviewService,
    authorization: AuthorizationService,
    correlation_id: str,
    cookie: str,
    presented_csrf: str | None,
) -> CustomerContext:
    """The ONE gate of both request-human routes (customer JSON API and the page button).

    In order: no agent credential; not a cross-site browser request; an ACTIVE server-side
    customer session (the server decides expiry and revocation); a CSRF token bound to this
    session AND this application. Every refusal is audited with a machine reason only. The
    self-asserted DEV customer header is never a credential here. Which customer may act on
    which application is decided afterwards by `ReviewService.request_human` (404 otherwise).
    """

    def deny(actor_type: AuditActorType, actor_id: str | None, reason: str) -> None:
        reviews.record_request_denial(
            application_id=application_id,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            correlation_id=correlation_id,
        )

    if agent_credentials_presented(request):
        deny(
            AuditActorType.AGENT,
            _presented_agent_id(request, authorization),
            "agent_credentials_presented",
        )
        raise AuthenticationRequiredError()
    if _cross_site(request):
        deny(AuditActorType.CUSTOMER, None, "cross_site_request")
        raise CrossSiteRequestError()
    customer = session_customer(request, settings, sessions, cookie)
    if customer is None or customer.session_secret is None:
        deny(AuditActorType.CUSTOMER, None, "no_customer_session")  # missing/expired/revoked
        raise AuthenticationRequiredError()
    if not csrf_matches(customer.session_secret, CSRF_HUMAN, application_id, presented_csrf):
        deny(AuditActorType.CUSTOMER, customer.customer_id, "csrf_token_invalid")
        raise CsrfTokenInvalidError()
    return customer


@router.post(
    "/v1/customer/applications/{application_id}/request-human",
    response_model=HumanRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Customer asks to speak with a person (bank-controlled customer channel)",
    description=(
        "Requires the customer's server-side session (HttpOnly cookie `trusteq_customer_api`, "
        "set by the bank's - in this prototype SIMULATED - sign-in) and the session's CSRF token "
        "for this application in the `X-CSRF-Token` header. The self-asserted "
        "`X-Dev-Customer-Id` header is not accepted; agent credentials are refused and audited. "
        "Only the application's own customer may ask (404 otherwise). Opens - or joins - the "
        "application's single review case and, where possible, pauses the workflow at its "
        "current step. The acknowledgement is generic: it never reveals review reasons or check "
        "results."
    ),
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
def request_human(
    application_id: ApplicationId,
    body: HumanRequestBody,
    request: Request,
    response: Response,
    settings: SettingsDep,
    reviews: ReviewServiceDep,
    sessions: CustomerSessionServiceDep,
    authorization: AuthorizationServiceDep,
    correlation_id: CorrelationIdDep,
    csrf_header: Annotated[
        str | None,
        Header(
            alias=CSRF_HEADER,
            description="The session's CSRF token for this application (from the bank page).",
        ),
    ] = None,
) -> HumanRequestResponse:
    customer = _human_request_customer(
        request,
        application_id,
        settings=settings,
        sessions=sessions,
        reviews=reviews,
        authorization=authorization,
        correlation_id=correlation_id,
        cookie=API_SESSION_COOKIE,
        presented_csrf=csrf_header,
    )
    ack = reviews.request_human(
        customer_id=customer.customer_id,
        application_id=application_id,
        category=body.category,
        correlation_id=correlation_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return HumanRequestResponse(application_id=ack.application_id, message=ack.message)


def _customer_for_handoff(
    request: Request,
    handoff_id: str,
    *,
    settings: Settings,
    sessions: CustomerSessionService,
    service: OnboardingService,
    authorization: AuthorizationService,
    correlation_id: str,
    cookie: str,
    scope: str,
    presented_csrf: str | None,
) -> CustomerContext:
    """The ONE gate of the tax and assessment routes, in the order of the confirmation's checks.

    No agent credential; not a cross-site browser request; an ACTIVE server-side customer session;
    a CSRF token bound to this session, this handoff and this purpose. Whether the handoff is live
    and belongs to this customer is decided next, by the service, with the one-time challenge.
    """
    if agent_credentials_presented(request):
        _refuse_agent_credentials(request, handoff_id, service, authorization, correlation_id)
        raise AuthenticationRequiredError()
    if _cross_site(request):
        raise CrossSiteRequestError()
    customer = session_customer(request, settings, sessions, cookie)
    if customer is None or customer.session_secret is None:
        raise AuthenticationRequiredError()
    if not csrf_matches(customer.session_secret, scope, handoff_id, presented_csrf):
        service.record_customer_channel_denial(
            handoff_id=handoff_id,
            actor_type=AuditActorType.CUSTOMER,
            actor_id=customer.customer_id,
            reason="csrf_token_invalid",
            correlation_id=correlation_id,
        )
        raise CsrfTokenInvalidError()
    return customer


@router.post(
    "/v1/customer/handoffs/{handoff_id}/tax-information",
    response_model=TaxInformationResponse,
    summary="Customer supplies synthetic tax information (bank-controlled customer channel)",
    description=(
        "Requires the customer's server-side session (cookie `trusteq_customer_api`), the "
        "session's CSRF token for this handoff in `X-CSRF-Token` and the one-time `nonce`. Agent "
        "credentials are refused and audited. Validation is local and deterministic: the answer "
        "says which fields are collected, missing or invalid and never echoes a value. Nothing "
        "is checked with a tax authority. Synthetic data only: a real tax ID is refused."
    ),
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        410: {"model": ErrorResponse},
    },
)
def submit_tax_information(
    handoff_id: HandoffId,
    body: TaxInformationBody,
    request: Request,
    response: Response,
    settings: SettingsDep,
    service: OnboardingServiceDep,
    sessions: CustomerSessionServiceDep,
    authorization: AuthorizationServiceDep,
    information: CustomerInformationServiceDep,
    correlation_id: CorrelationIdDep,
    csrf_header: Annotated[
        str | None,
        Header(alias=CSRF_HEADER, description="The session's CSRF token for this handoff."),
    ] = None,
) -> TaxInformationResponse:
    customer = _customer_for_handoff(
        request,
        handoff_id,
        settings=settings,
        sessions=sessions,
        service=service,
        authorization=authorization,
        correlation_id=correlation_id,
        cookie=API_SESSION_COOKIE,
        scope=CSRF_TAX,
        presented_csrf=csrf_header,
    )
    result = information.submit_tax(
        customer_id=customer.customer_id,
        handoff_id=handoff_id,
        nonce=body.nonce,
        submitted=body.model_dump(exclude={"nonce"}),
        correlation_id=correlation_id,
    )
    application = service.review_handoff(
        customer_id=customer.customer_id,
        handoff_id=handoff_id,
        nonce=body.nonce,
        correlation_id=correlation_id,
    ).application
    response.headers["Cache-Control"] = "no-store"
    return TaxInformationResponse(
        application_id=application.application_id,
        complete=result.complete,
        field_statuses={name: status.value for name, status in result.statuses.items()},
        data_source=result.source.value,
        local_validation="COMPLETE" if result.complete else "INCOMPLETE",
        ready_for_confirmation=information.status(application).ready,
    )


@router.post(
    "/v1/customer/handoffs/{handoff_id}/appropriateness",
    response_model=AssessmentResponse,
    summary="Customer answers the synthetic knowledge-and-experience questions",
    description=(
        "Same customer-channel requirements as the tax information. `intent=answer` records the "
        "deterministic result of the answers; `intent=acknowledge` records that the customer read "
        "a warning or the missing-answers notice and wants to continue. No result stops the "
        "account opening. A prototype demonstration with synthetic data: not legally complete "
        "compliance, not advice and not a suitability assessment."
    ),
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        410: {"model": ErrorResponse},
    },
)
def submit_assessment(
    handoff_id: HandoffId,
    body: AssessmentBody,
    request: Request,
    response: Response,
    settings: SettingsDep,
    service: OnboardingServiceDep,
    sessions: CustomerSessionServiceDep,
    authorization: AuthorizationServiceDep,
    information: CustomerInformationServiceDep,
    correlation_id: CorrelationIdDep,
    csrf_header: Annotated[
        str | None,
        Header(alias=CSRF_HEADER, description="The session's CSRF token for this handoff."),
    ] = None,
) -> AssessmentResponse:
    customer = _customer_for_handoff(
        request,
        handoff_id,
        settings=settings,
        sessions=sessions,
        service=service,
        authorization=authorization,
        correlation_id=correlation_id,
        cookie=API_SESSION_COOKIE,
        scope=CSRF_ASSESSMENT,
        presented_csrf=csrf_header,
    )
    if body.intent == "acknowledge":
        record = information.acknowledge_assessment(
            customer_id=customer.customer_id,
            handoff_id=handoff_id,
            nonce=body.nonce,
            correlation_id=correlation_id,
        )
    else:
        record = information.submit_assessment(
            customer_id=customer.customer_id,
            handoff_id=handoff_id,
            nonce=body.nonce,
            knows_capital_loss=body.knows_capital_loss,
            knows_no_guarantee=body.knows_no_guarantee,
            experience=body.experience,
            correlation_id=correlation_id,
        )
    application = service.review_handoff(
        customer_id=customer.customer_id,
        handoff_id=handoff_id,
        nonce=body.nonce,
        correlation_id=correlation_id,
    ).application
    response.headers["Cache-Control"] = "no-store"
    return AssessmentResponse(
        application_id=record.application_id,
        outcome=record.outcome,
        customer_notice=customer_notice_for(record.outcome),
        acknowledged=record.acknowledged_at is not None,
        rule_set_version=record.rule_set_version,
        ready_for_confirmation=information.status(application).ready,
        notice=PROTOTYPE_NOTICE,
    )


# --- HTML pages ---------------------------------------------------------------------------------


async def _read_form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        return {}
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _cross_site(request: Request) -> bool:
    """Defence in depth next to the CSRF token: refuse form posts a browser marks as coming
    from another site. Non-browser clients send neither header and rely on the CSRF token."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in {"same-origin", "none"}:
        return True
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return origin.rstrip("/") != str(request.base_url).rstrip("/")  # includes "null"


def _page(title: str, message: str, status_code: int, ok: bool = False) -> HTMLResponse:
    return pages.html_response(pages.result_page(title, message, ok=ok), status_code)


def _error_page(exc: AppError, title: str) -> HTMLResponse:
    return _page(title, exc.message, exc.status_code)


def _sign_in_response(
    request: Request, handoff_id: str, token: str, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    """The sign-in form with a login-CSRF token bound to a pre-session cookie."""
    pre_session = request.cookies.get(LOGIN_COOKIE)
    fresh = not is_pre_session_secret(pre_session)
    secret = new_pre_session_secret() if fresh else str(pre_session)
    response = pages.html_response(
        pages.sign_in_page(
            handoff_id, token, error, login_csrf=csrf_token(secret, CSRF_LOGIN, handoff_id)
        ),
        status_code,
    )
    if fresh:
        response.set_cookie(
            LOGIN_COOKIE,
            secret,
            max_age=LOGIN_COOKIE_MAX_AGE,
            httponly=True,
            samesite="strict",
            secure=cookie_is_secure(request),
            path=COOKIE_PATH,
        )
    return response


@router.get(
    "/customer/handoffs/{handoff_id}", include_in_schema=False, name="customer_handoff_page"
)
def handoff_page(
    handoff_id: HandoffId,
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    service: OnboardingServiceDep,
    sessions: CustomerSessionServiceDep,
    authorization: AuthorizationServiceDep,
    information: CustomerInformationServiceDep,
    correlation_id: CorrelationIdDep,
    token: str = "",
) -> HTMLResponse:
    if not _TOKEN.fullmatch(token):
        return _page("Invalid link", "This confirmation link is not valid.", 403)
    if not settings.dev_customer_auth_active:
        return _page("Sign-in unavailable", "Customer sign-in is not available.", 401)
    if agent_credentials_presented(request):
        _refuse_agent_credentials(request, handoff_id, service, authorization, correlation_id)
        return _page("Not allowed", "Agent credentials are not accepted here.", 401)
    customer = session_customer(request, settings, sessions)
    if customer is None or customer.session_secret is None:
        return _sign_in_response(request, handoff_id, token)  # the link alone shows nothing
    try:
        return _review_response(
            customer, handoff_id, token, settings, container, service, information, correlation_id
        )
    except AppError as exc:
        return _error_page(exc, "Request cannot be shown")


def _review_response(
    customer: CustomerContext,
    handoff_id: str,
    token: str,
    settings: Settings,
    container: AppContainer,
    service: OnboardingService,
    information: CustomerInformationService,
    correlation_id: str,
    tax_statuses: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """The review page. `tax_statuses`: the field results of a tax form that was just refused."""
    secret = customer.session_secret or ""
    review = service.review_handoff(
        customer_id=customer.customer_id,
        handoff_id=handoff_id,
        nonce=token,
        correlation_id=correlation_id,
    )
    action = review.handoff.action
    provider = container.catalog.get_provider(action.provider_id)
    product = next(
        p
        for p in container.catalog.list_products(action.provider_id)
        if p.product_id == action.product_id
    )
    channel = next(
        c
        for c in container.catalog.list_channels(action.provider_id)
        if c.channel_id == action.channel_id
    )
    panel = None
    if settings.customer_information_required:
        current = information.status(review.application)
        panel = pages.InformationPanel(
            handoff_id=handoff_id,
            token=token,
            tax_csrf=csrf_token(secret, CSRF_TAX, handoff_id),
            assessment_csrf=csrf_token(secret, CSRF_ASSESSMENT, handoff_id),
            tax_record=current.tax,
            assessment=current.assessment,
            ready=current.ready,
            tax_statuses=tax_statuses,
        )
    return pages.html_response(
        pages.review_page(
            review=review,
            customer_id=customer.customer_id,
            token=token,
            provider_name=provider.legal_name,
            product_name=product.name,
            channel_label=channel.label,
            csrf_token=csrf_token(secret, CSRF_CONFIRM, handoff_id),
            product=product,
            channel=channel,
            human_csrf_token=csrf_token(secret, CSRF_HUMAN, action.application_id),
            information=panel,
        ),
        status_code,
    )


@router.post("/customer/dev-login", include_in_schema=False, response_model=None)
async def dev_login(
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    correlation_id: CorrelationIdDep,
) -> HTMLResponse | RedirectResponse:
    """DEV ONLY simulated sign-in: creates a server-side session (rotating any previous one)."""
    if not settings.dev_customer_auth_active:
        return _page("Sign-in unavailable", "Customer sign-in is not available.", 401)
    if _cross_site(request):
        return _page("Rejected", "Cross-site request.", 403)
    if agent_credentials_presented(request):
        return _page("Not allowed", "Agent credentials are not accepted here.", 401)
    form = await _read_form(request)
    customer_id = form.get("customer_id", "").strip()
    handoff_id, token = form.get("handoff_id", ""), form.get("token", "")
    if not _HANDOFF_ID.fullmatch(handoff_id):
        return _page("Rejected", "Invalid request.", 400)
    if not csrf_matches(
        request.cookies.get(LOGIN_COOKIE), CSRF_LOGIN, handoff_id, form.get("login_csrf")
    ):
        return _page("Rejected", "This sign-in form is no longer valid. Open the link again.", 403)
    if not (is_valid_customer_id(customer_id) and _TOKEN.fullmatch(token)):
        return _sign_in_response(
            request, handoff_id, token, "Please enter a valid customer ID.", 400
        )
    _, secret = await run_in_threadpool(
        sessions.start,
        customer_id=customer_id,
        replaces=request.cookies.get(SESSION_COOKIE),
        correlation_id=correlation_id,
    )
    response = RedirectResponse(
        f"/customer/handoffs/{handoff_id}?token={token}", status_code=status.HTTP_303_SEE_OTHER
    )
    for name, path in SESSION_COOKIES:  # the pages and the customer JSON API: one session
        response.set_cookie(
            name,
            secret,
            max_age=settings.customer_session_ttl_seconds,
            httponly=True,
            samesite="strict",
            secure=cookie_is_secure(request),
            path=path,
        )
    response.delete_cookie(LOGIN_COOKIE, path=COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/customer/handoffs/{handoff_id}/confirm", include_in_schema=False)
async def confirm_handoff_form(
    handoff_id: HandoffId,
    request: Request,
    settings: SettingsDep,
    service: OnboardingServiceDep,
    sessions: CustomerSessionServiceDep,
    authorization: AuthorizationServiceDep,
    correlation_id: CorrelationIdDep,
) -> HTMLResponse:
    if _cross_site(request):
        return _page("Rejected", "Cross-site request.", 403)
    if agent_credentials_presented(request):
        await run_in_threadpool(
            _refuse_agent_credentials, request, handoff_id, service, authorization, correlation_id
        )
        return _page("Not allowed", "Agent credentials are not accepted here.", 401)
    customer = await run_in_threadpool(session_customer, request, settings, sessions)
    if customer is None or customer.session_secret is None:
        return _page("Not signed in", "Please sign in again.", 401)
    form = await _read_form(request)
    token, digest = form.get("token", ""), form.get("action_digest", "")
    if not (_TOKEN.fullmatch(token) and _DIGEST.fullmatch(digest)):
        return _page("Rejected", "Invalid request.", 400)
    if not csrf_matches(customer.session_secret, CSRF_CONFIRM, handoff_id, form.get("csrf_token")):
        await run_in_threadpool(
            service.record_customer_channel_denial,
            handoff_id=handoff_id,
            actor_type=AuditActorType.CUSTOMER,
            actor_id=customer.customer_id,
            reason="csrf_token_invalid",
            correlation_id=correlation_id,
        )
        return _page("Rejected", "This form is no longer valid. Please reload the page.", 403)
    try:
        application = await run_in_threadpool(
            service.confirm_handoff,
            customer_id=customer.customer_id,
            handoff_id=handoff_id,
            nonce=token,
            action_digest=digest,
            correlation_id=correlation_id,
        )
    except AppError as exc:
        return _error_page(exc, "Confirmation failed")
    page = pages.result_page(
        "Confirmation recorded",
        f"Your authorization for application {application.application_id} has been recorded. "
        "Further processing is subject to the bank's required checks. "
        "Your account is not open yet.",
        ok=True,
        human_request=(
            application.application_id,
            csrf_token(customer.session_secret, CSRF_HUMAN, application.application_id),
        ),
        step=pages.STEP_CONFIRMED,
    )
    return pages.html_response(page, 200)


def _yes_no(value: str) -> object:
    """A form's yes or no. Anything else stays as it is, so that validation calls it invalid."""
    return {"yes": True, "no": False}.get(value, value)


def _tax_fields(form: Mapping[str, str]) -> dict[str, object]:
    amount = form.get("exemption_order_amount_eur", "").strip()
    return {
        "tax_residency_country": form.get("tax_residency_country", "").strip(),
        "synthetic_tax_id": form.get("synthetic_tax_id", "").strip(),
        "us_person_declared": _yes_no(form.get("us_person_declared", "")),
        "exemption_order_amount_eur": int(amount)
        if amount.isascii() and amount.isdigit()
        else amount,
        "church_tax_objection": _yes_no(form.get("church_tax_objection", "")),
    }


def _reviewed_page_redirect(handoff_id: str, token: str, anchor: str) -> RedirectResponse:
    return RedirectResponse(
        f"/customer/handoffs/{handoff_id}?token={token}#{anchor}",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/customer/handoffs/{handoff_id}/tax", include_in_schema=False, response_model=None)
async def tax_form(
    handoff_id: HandoffId,
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    service: OnboardingServiceDep,
    sessions: CustomerSessionServiceDep,
    authorization: AuthorizationServiceDep,
    information: CustomerInformationServiceDep,
    correlation_id: CorrelationIdDep,
) -> HTMLResponse | RedirectResponse:
    """A complete form goes back to the page. A refused one shows the field statuses, no values."""
    form = await _read_form(request)
    token = form.get("token", "")
    if not _TOKEN.fullmatch(token):
        return _page("Rejected", "Invalid request.", 400)

    def run() -> HTMLResponse | RedirectResponse:
        customer = _customer_for_handoff(
            request,
            handoff_id,
            settings=settings,
            sessions=sessions,
            service=service,
            authorization=authorization,
            correlation_id=correlation_id,
            cookie=SESSION_COOKIE,
            scope=CSRF_TAX,
            presented_csrf=form.get("csrf_token"),
        )
        result = information.submit_tax(
            customer_id=customer.customer_id,
            handoff_id=handoff_id,
            nonce=token,
            submitted=_tax_fields(form),
            correlation_id=correlation_id,
        )
        if result.complete:
            return _reviewed_page_redirect(handoff_id, token, pages.ANCHOR_TAX)
        statuses = {name: status.value for name, status in result.statuses.items()}
        return _review_response(
            customer, handoff_id, token, settings, container, service, information,
            correlation_id, tax_statuses=statuses, status_code=422,
        )  # fmt: skip

    try:
        return await run_in_threadpool(run)
    except AppError as exc:
        return _error_page(exc, "Request refused")


def _assessment_answer(value: str) -> bool | None:
    """Unanswered is a valid answer. Anything but true or false is a tampered form."""
    if value == "":
        return None
    answers = {"true": True, "false": False}
    if value not in answers:
        raise ValueError(value)
    return answers[value]


@router.post(
    "/customer/handoffs/{handoff_id}/assessment", include_in_schema=False, response_model=None
)
async def assessment_form(
    handoff_id: HandoffId,
    request: Request,
    settings: SettingsDep,
    service: OnboardingServiceDep,
    sessions: CustomerSessionServiceDep,
    authorization: AuthorizationServiceDep,
    information: CustomerInformationServiceDep,
    correlation_id: CorrelationIdDep,
) -> HTMLResponse | RedirectResponse:
    form = await _read_form(request)
    token = form.get("token", "")
    if not _TOKEN.fullmatch(token):
        return _page("Rejected", "Invalid request.", 400)

    def run() -> RedirectResponse:
        customer = _customer_for_handoff(
            request,
            handoff_id,
            settings=settings,
            sessions=sessions,
            service=service,
            authorization=authorization,
            correlation_id=correlation_id,
            cookie=SESSION_COOKIE,
            scope=CSRF_ASSESSMENT,
            presented_csrf=form.get("csrf_token"),
        )
        if form.get("intent") == "acknowledge":
            information.acknowledge_assessment(
                customer_id=customer.customer_id,
                handoff_id=handoff_id,
                nonce=token,
                correlation_id=correlation_id,
            )
        else:
            experience = form.get("experience", "")
            information.submit_assessment(
                customer_id=customer.customer_id,
                handoff_id=handoff_id,
                nonce=token,
                knows_capital_loss=_assessment_answer(form.get("knows_capital_loss", "")),
                knows_no_guarantee=_assessment_answer(form.get("knows_no_guarantee", "")),
                experience=ExperienceBand(experience) if experience else None,
                correlation_id=correlation_id,
            )
        return _reviewed_page_redirect(handoff_id, token, pages.ANCHOR_KNOWLEDGE)

    try:
        return await run_in_threadpool(run)
    except AppError as exc:
        return _error_page(exc, "Request refused")
    except ValueError:
        return _page("Rejected", "Invalid request.", 400)


@router.post("/customer/applications/{application_id}/request-human", include_in_schema=False)
async def request_human_form(
    application_id: ApplicationId,
    request: Request,
    settings: SettingsDep,
    reviews: ReviewServiceDep,
    sessions: CustomerSessionServiceDep,
    authorization: AuthorizationServiceDep,
    correlation_id: CorrelationIdDep,
) -> HTMLResponse:
    """The "Speak to a person" button: the page session + its CSRF form field, through
    the same gate and workflow as the JSON route; answers with the same neutral text."""
    form = await _read_form(request)
    try:
        customer = await run_in_threadpool(
            lambda: _human_request_customer(
                request,
                application_id,
                settings=settings,
                sessions=sessions,
                reviews=reviews,
                authorization=authorization,
                correlation_id=correlation_id,
                cookie=SESSION_COOKIE,
                presented_csrf=form.get("csrf_token"),
            )
        )
        try:
            category = HumanRequestCategory(form.get("category", ""))
        except ValueError:
            return _page("Rejected", "Invalid request.", 400)
        ack = await run_in_threadpool(
            lambda: reviews.request_human(
                customer_id=customer.customer_id,
                application_id=application_id,
                category=category,
                correlation_id=correlation_id,
            )
        )
    except AppError as exc:
        return _error_page(exc, "Request not possible")
    return pages.html_response(pages.human_request_ack_page(ack.message), 200)
