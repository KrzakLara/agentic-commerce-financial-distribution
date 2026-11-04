"""Customer-side confirmation (bank-controlled channel).

* ``POST /v1/customer/handoffs/{id}/confirm`` - the JSON ceremony from ADR section 05 (DEV
  customer context: ``X-Dev-Customer-Id``).
* ``POST /v1/customer/applications/{id}/request-human`` - the customer asks for a person
  (Phase 04): requires the customer's SERVER-SIDE session (cookie ``trusteq_customer_api``) and
  its CSRF token (``X-CSRF-Token``); the self-asserted DEV header is not accepted here.
* ``/customer/...`` - the minimal HTML pages a customer actually sees (not part of the OpenAPI
  contract), with a server-side session and CSRF tokens (Phase 03), including the "Speak to a
  person" button (``POST /customer/applications/{id}/request-human``, Phase 04).

None of these routes accepts an agent credential: a request carrying an ``Authorization``
header is refused (and audited), whatever else it carries. Possessing the handoff link (or its
nonce) is never authentication: the page shows nothing until the customer is signed in.
"""

import re
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Header, Path, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import APIKeyHeader

from app.api import pages
from app.api.customer_auth import (
    API_SESSION_COOKIE,
    COOKIE_PATH,
    CSRF_HEADER,
    DEV_CUSTOMER_HEADER,
    LOGIN_COOKIE,
    LOGIN_COOKIE_MAX_AGE,
    SESSION_COOKIE,
    SESSION_COOKIES,
    CustomerContext,
    agent_credentials_presented,
    cookie_is_secure,
    header_customer,
    is_valid_customer_id,
    session_customer,
)
from app.api.deps import (
    AuthorizationServiceDep,
    ContainerDep,
    CorrelationIdDep,
    CustomerSessionServiceDep,
    OnboardingServiceDep,
    ReviewServiceDep,
    SettingsDep,
)
from app.api.schemas.errors import ErrorResponse
from app.api.schemas.handoffs import HandoffConfirmRequest, HandoffConfirmResponse
from app.api.schemas.review import HumanRequestBody, HumanRequestResponse
from app.core.config import Settings
from app.core.exceptions import (
    AppError,
    AuthenticationRequiredError,
    CrossSiteRequestError,
    CsrfTokenInvalidError,
)
from app.domain.enums import AuditActorType, HumanRequestCategory
from app.domain.visibility import to_agent_visible_state
from app.services.authorization_service import AuthorizationService
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

_dev_customer_header = APIKeyHeader(
    name=DEV_CUSTOMER_HEADER,
    scheme_name="DevCustomerHeader",
    description=(
        "DEV/TEST ONLY. Simulated customer sign-in for the bank-controlled confirmation. "
        "This is not identity verification and is disabled in production."
    ),
    auto_error=False,
)


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
        "Requires the DEV/TEST simulated customer context (`X-Dev-Customer-Id`), the one-time "
        "`nonce` and the `action_digest` the customer reviewed. Agent credentials are never "
        "accepted here: a request with an `Authorization` header is refused. Fails closed and "
        "changes nothing on any mismatch."
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
    authorization: AuthorizationServiceDep,
    correlation_id: CorrelationIdDep,
    customer_header: Annotated[str | None, Depends(_dev_customer_header)],
) -> HandoffConfirmResponse:
    if agent_credentials_presented(request):
        _refuse_agent_credentials(request, handoff_id, service, authorization, correlation_id)
        raise AuthenticationRequiredError()
    customer = header_customer(settings, customer_header)
    if customer is None:
        raise AuthenticationRequiredError()
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
        review = service.review_handoff(
            customer_id=customer.customer_id,
            handoff_id=handoff_id,
            nonce=token,
            correlation_id=correlation_id,
        )
    except AppError as exc:
        return _error_page(exc, "Request cannot be shown")
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
    return pages.html_response(
        pages.review_page(
            review=review,
            customer_id=customer.customer_id,
            token=token,
            provider_name=provider.legal_name,
            product_name=product.name,
            channel_label=channel.label,
            csrf_token=csrf_token(customer.session_secret, CSRF_CONFIRM, handoff_id),
            product=product,
            channel=channel,
            human_csrf_token=csrf_token(customer.session_secret, CSRF_HUMAN, action.application_id),
        )
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
        f"Thank you. Your confirmation for application {application.application_id} was "
        "recorded. The mandatory checks (MOCK) have run; the agent can now continue.",
        ok=True,
        human_request=(
            application.application_id,
            csrf_token(customer.session_secret, CSRF_HUMAN, application.application_id),
        ),
        step=pages.STEP_CONFIRMED,
    )
    return pages.html_response(page, 200)


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
