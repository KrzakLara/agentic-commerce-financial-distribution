"""Customer-side confirmation (bank-controlled channel).

* ``POST /v1/customer/handoffs/{id}/confirm`` - the JSON ceremony from ADR section 05.
* ``/customer/...`` - the minimal HTML pages a customer actually sees (not part of the OpenAPI
  contract). They use the same service; the customer is the simulated DEV context.

The agent holds neither the customer header nor the sign-in cookie, and none of these routes
accept the agent's actor identity.
"""

import re
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Header, Path, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api import pages
from app.api.customer_auth import (
    DEV_CUSTOMER_COOKIE,
    DEV_CUSTOMER_HEADER,
    CurrentCustomer,
    is_valid_customer_id,
    resolve_customer,
    sign_session,
)
from app.api.deps import ContainerDep, CorrelationIdDep, OnboardingServiceDep, SettingsDep
from app.api.schemas.errors import ErrorResponse
from app.api.schemas.handoffs import HandoffConfirmRequest, HandoffConfirmResponse
from app.core.exceptions import AppError
from app.domain.visibility import to_agent_visible_state

router = APIRouter(tags=["customer"])

HandoffId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")]
_TOKEN = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_FORM_BYTES = 4096


@router.post(
    "/v1/customer/handoffs/{handoff_id}/confirm",
    response_model=HandoffConfirmResponse,
    summary="Customer confirms the exact approved action (bank-controlled ceremony)",
    description=(
        "Requires the DEV/TEST simulated customer context (`X-Dev-Customer-Id`), the one-time "
        "`nonce` and the `action_digest` the customer reviewed. The agent cannot call this: it "
        "has no customer context. Fails closed and changes nothing on any mismatch."
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
    customer: CurrentCustomer,
    service: OnboardingServiceDep,
    correlation_id: CorrelationIdDep,
) -> HandoffConfirmResponse:
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


# --- HTML pages ---------------------------------------------------------------------------------


async def _read_form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        return {}
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def _same_origin(request: Request) -> bool:
    """Cheap CSRF defence for the form posts: browsers always send Origin on POST."""
    origin = request.headers.get("origin")
    if origin is None:
        return True  # non-browser client (the demo, tests)
    return origin.rstrip("/") == str(request.base_url).rstrip("/")


def _error_page(exc: AppError, title: str) -> HTMLResponse:
    return pages.html_response(pages.result_page(title, exc.message, ok=False), exc.status_code)


@router.get(
    "/customer/handoffs/{handoff_id}", include_in_schema=False, name="customer_handoff_page"
)
async def handoff_page(
    handoff_id: HandoffId,
    request: Request,
    settings: SettingsDep,
    container: ContainerDep,
    service: OnboardingServiceDep,
    correlation_id: CorrelationIdDep,
    token: str = "",
    customer_header: Annotated[str | None, Header(alias=DEV_CUSTOMER_HEADER)] = None,
) -> HTMLResponse:
    if not _TOKEN.fullmatch(token):
        return pages.html_response(
            pages.result_page("Invalid link", "This confirmation link is not valid.", ok=False), 403
        )
    customer = resolve_customer(request, settings, customer_header)
    if customer is None:
        if not settings.dev_customer_auth_active:
            return pages.html_response(
                pages.result_page(
                    "Sign-in unavailable", "Customer sign-in is not available.", ok=False
                ),
                401,
            )
        return pages.html_response(pages.sign_in_page(handoff_id, token))
    try:
        review = await run_in_threadpool(
            service.review_handoff,
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
        )
    )


@router.post("/customer/dev-login", include_in_schema=False, response_model=None)
async def dev_login(request: Request, settings: SettingsDep) -> HTMLResponse | RedirectResponse:
    """DEV ONLY simulated sign-in. Sets a signed session cookie for the page flow."""
    if not settings.dev_customer_auth_active:
        return pages.html_response(
            pages.result_page("Sign-in unavailable", "Customer sign-in is not available.", False),
            401,
        )
    if not _same_origin(request):
        return pages.html_response(pages.result_page("Rejected", "Cross-site request.", False), 403)
    form = await _read_form(request)
    customer_id, handoff_id, token = (
        form.get("customer_id", "").strip(),
        form.get("handoff_id", ""),
        form.get("token", ""),
    )
    if not (is_valid_customer_id(customer_id) and _TOKEN.fullmatch(token) and handoff_id):
        return pages.html_response(
            pages.sign_in_page(handoff_id, token, "Please enter a valid customer ID."), 400
        )
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", handoff_id):
        return pages.html_response(pages.result_page("Rejected", "Invalid request.", False), 400)
    response = RedirectResponse(
        f"/customer/handoffs/{handoff_id}?token={token}", status_code=status.HTTP_303_SEE_OTHER
    )
    response.set_cookie(
        DEV_CUSTOMER_COOKIE,
        sign_session(request.app.state.dev_session_key, customer_id),
        httponly=True,
        samesite="strict",
        path="/customer",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/customer/handoffs/{handoff_id}/confirm", include_in_schema=False)
async def confirm_handoff_form(
    handoff_id: HandoffId,
    request: Request,
    settings: SettingsDep,
    service: OnboardingServiceDep,
    correlation_id: CorrelationIdDep,
    customer_header: Annotated[str | None, Header(alias=DEV_CUSTOMER_HEADER)] = None,
) -> HTMLResponse:
    if not _same_origin(request):
        return pages.html_response(pages.result_page("Rejected", "Cross-site request.", False), 403)
    customer = resolve_customer(request, settings, customer_header)
    if customer is None:
        return pages.html_response(
            pages.result_page("Not signed in", "Please sign in again.", ok=False), 401
        )
    form = await _read_form(request)
    token, digest = form.get("token", ""), form.get("action_digest", "")
    if not (_TOKEN.fullmatch(token) and _DIGEST.fullmatch(digest)):
        return pages.html_response(pages.result_page("Rejected", "Invalid request.", False), 400)
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
    return pages.html_response(
        pages.result_page(
            "Confirmation recorded",
            f"Thank you. Your confirmation for application {application.application_id} was "
            "recorded. The mandatory checks (MOCK) have run; the agent can now continue.",
            ok=True,
        )
    )
