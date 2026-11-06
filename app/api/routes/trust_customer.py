"""The customer's side of the trust integration, on the bank's channel, plus signed provider
callbacks.

Same rules as every customer route: an agent credential is refused, a state change needs an active
session and a CSRF token bound to that session and target, and cross-site posts are refused.
"""

import base64
import re
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.api import pages, trust_pages
from app.api.customer_auth import (
    SESSION_COOKIES,
    CustomerContext,
    agent_credentials_presented,
    cookie_is_secure,
    session_customer,
)
from app.api.deps import (
    ContractServiceDep,
    CorrelationIdDep,
    CustomerSessionServiceDep,
    DelegationServiceDep,
    GovernanceServiceDep,
    OidcLoginServiceDep,
    OnboardingServiceDep,
    ProofingServiceDep,
    SettingsDep,
)
from app.api.routes.customer import _cross_site, _error_page, _page, _read_form
from app.core.exceptions import AppError, ServiceDisabledError
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.repositories.trust_orm import PolicyDecisionRow
from app.services.customer_session_service import csrf_matches, csrf_token
from app.services.oidc_login import ASSURANCE_DEV, ASSURANCE_OIDC, OidcLoginError
from app.services.regulatory_policy import POLICY_VERSION

router = APIRouter(include_in_schema=False)
_FLOW_COOKIE = "trusteq_oidc_flow"
_FLOW_COOKIE_PATH = "/customer/oidc"
_RETURN_PATH = re.compile(
    r"^/customer/(?:handoffs|delegations|applications)(?:/[A-Za-z0-9_\-/]+)?(?:\?[A-Za-z0-9_\-=&]*)?$"
)
_MAX_CALLBACK_BYTES = 16 * 1024
_Id = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")]
CSRF_DELEGATION = "delegation"
CSRF_DELEGATION_REVOKE = "delegation_revoke"
CSRF_PROOFING = "proofing"
CSRF_CONTRACT_AUTHORIZE = "contract_authorize"
CSRF_CONTRACT_CANCEL = "contract_cancel"
CSRF_LOGOUT = "logout"


# --- login ----------------------------------------------------------------------------------------


def login_redirect(return_path: str) -> RedirectResponse:
    target = base64.urlsafe_b64encode(return_path.encode()).decode().rstrip("=")
    return RedirectResponse(
        f"/customer/oidc/login?next={target}", status_code=status.HTTP_303_SEE_OTHER
    )


def _decode_return(value: str) -> str | None:
    try:
        path = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
    except (ValueError, UnicodeError):
        return None
    return path if _RETURN_PATH.fullmatch(path) else None  # never an open redirect


@router.get("/customer/oidc/login", response_model=None)
def oidc_login(
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    login: OidcLoginServiceDep,
    next: Annotated[str, Query(max_length=600)] = "",  # noqa: A002 - the query parameter's name
) -> HTMLResponse | RedirectResponse:
    if not settings.oidc_login_active:
        return _page("Sign-in unavailable", "Customer sign-in is not available.", 401)
    if agent_credentials_presented(request):
        return _page("Not allowed", "Agent credentials are not accepted here.", 401)
    return_path = _decode_return(next)
    if return_path is None:
        return _page("Rejected", "Invalid request.", 400)
    current = session_customer(request, settings, sessions)
    try:
        url, binding = login.begin(current.customer_id if current else None)
    except OidcLoginError:
        return _page("Sign-in unavailable", "The sign-in service cannot be reached.", 503)
    response = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        _FLOW_COOKIE,
        f"{binding}.{next}",
        max_age=300,
        httponly=True,
        samesite="lax",  # it must come back with the provider's cross-site redirect
        secure=cookie_is_secure(request),
        path=_FLOW_COOKIE_PATH,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/customer/oidc/callback", response_model=None)
def oidc_callback(
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    login: OidcLoginServiceDep,
    correlation_id: CorrelationIdDep,
    state: Annotated[str | None, Query(max_length=200)] = None,
    code: Annotated[str | None, Query(max_length=2000)] = None,
    iss: Annotated[str | None, Query(max_length=300)] = None,
) -> HTMLResponse:
    if not settings.oidc_login_active:
        return _page("Sign-in unavailable", "Customer sign-in is not available.", 401)
    binding, _, encoded = (request.cookies.get(_FLOW_COOKIE) or "").partition(".")
    return_path = _decode_return(encoded)
    if return_path is None:
        return _page("Sign-in failed", "This sign-in could not be completed.", 403)
    try:
        verified = login.complete(
            state=state, code=code, issuer_parameter=iss, browser_binding=binding
        )
    except OidcLoginError:
        return _page("Sign-in failed", "This sign-in could not be completed.", 403)
    session, secret = sessions.start(
        customer_id=verified.customer_ref,
        replaces=None,
        correlation_id=correlation_id,
        simulated=False,
    )
    login.record(verified, session.session_id)
    response = pages.html_response(trust_pages.continue_page(return_path), 200)
    for name, path in SESSION_COOKIES:
        response.set_cookie(
            name,
            secret,
            max_age=settings.customer_session_ttl_seconds,
            httponly=True,
            samesite="strict",
            secure=cookie_is_secure(request),
            path=path,
        )
    response.delete_cookie(_FLOW_COOKIE, path=_FLOW_COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/customer/oidc/logout", response_model=None)
async def oidc_logout(
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    correlation_id: CorrelationIdDep,
) -> HTMLResponse:
    """Ends the bank's session. The provider's own session is that provider's concern."""
    if _cross_site(request):
        return _page("Rejected", "Cross-site request.", 403)
    customer = session_customer(request, settings, sessions)
    form = await _read_form(request)
    if customer is None or not csrf_matches(
        customer.session_secret, CSRF_LOGOUT, customer.customer_id, form.get("csrf_token")
    ):
        return _page("Rejected", "Invalid request.", 403)
    await run_in_threadpool(sessions.end, customer.session_secret, correlation_id=correlation_id)
    response = _page("Signed out", "You are signed out.", 200, ok=True)
    for name, path in SESSION_COOKIES:
        response.delete_cookie(name, path=path)
    return response


def _require_customer(
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    return_path: str,
) -> CustomerContext | HTMLResponse | RedirectResponse:
    if agent_credentials_presented(request):
        return _page("Not allowed", "Agent credentials are not accepted here.", 401)
    if not settings.customer_sign_in_active:
        return _page("Sign-in unavailable", "Customer sign-in is not available.", 401)
    customer = session_customer(request, settings, sessions)
    if customer is not None and customer.session_secret is not None:
        return customer
    if settings.oidc_login_active:
        return login_redirect(return_path)
    return _page("Not signed in", "Please sign in first.", 401)


def _post_guard(
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    purpose: str,
    binding: str,
    presented: str | None,
) -> CustomerContext | HTMLResponse:
    if agent_credentials_presented(request):
        return _page("Not allowed", "Agent credentials are not accepted here.", 401)
    if _cross_site(request):
        return _page("Rejected", "Cross-site request.", 403)
    customer = session_customer(request, settings, sessions)
    if customer is None or customer.session_secret is None:
        return _page("Not signed in", "Please sign in first.", 401)
    if not csrf_matches(customer.session_secret, purpose, binding, presented):
        return _page("Rejected", "This form is no longer valid.", 403)
    return customer


# --- delegation -----------------------------------------------------------------------------------


@router.get("/customer/delegations/authorize", response_model=None)
def delegation_page(
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    delegation: DelegationServiceDep,
    request_token: Annotated[str, Query(alias="request", min_length=20, max_length=80)],
) -> HTMLResponse | RedirectResponse:
    customer = _require_customer(
        request,
        settings,
        sessions,
        f"/customer/delegations/authorize?request={request_token}",
    )
    if not isinstance(customer, CustomerContext):
        return customer
    try:
        row = delegation.pending_request(request_token)
    except AppError as error:
        return _error_page(error, "Request cannot be shown")
    csrf = csrf_token(customer.session_secret or "", CSRF_DELEGATION, request_token)
    page = trust_pages.delegation_page(
        row, delegation.display_name(row.client_id), request_token, csrf
    )
    return pages.html_response(page)


@router.post("/customer/delegations/authorize", response_model=None)
async def delegation_decide(
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    delegation: DelegationServiceDep,
    correlation_id: CorrelationIdDep,
) -> HTMLResponse:
    form = await _read_form(request)
    request_token = form.get("request", "")
    customer = _post_guard(
        request, settings, sessions, CSRF_DELEGATION, request_token, form.get("csrf_token")
    )
    if not isinstance(customer, CustomerContext):
        return customer
    approve = form.get("decision") == "approve"
    try:
        await run_in_threadpool(
            delegation.decide,
            customer_id=customer.customer_id,
            request_token=request_token,
            approve=approve,
            identity_assurance=ASSURANCE_DEV if customer.is_dev_context else ASSURANCE_OIDC,
            correlation_id=correlation_id,
        )
    except AppError as error:
        return _error_page(error, "Decision not recorded")
    return _page(
        "Decision recorded",
        "The agent may now prepare your application. It cannot confirm or sign for you."
        if approve
        else "The agent was not allowed.",
        200,
        ok=True,
    )


@router.get("/customer/delegations", response_model=None)
def delegation_list(
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    delegation: DelegationServiceDep,
) -> HTMLResponse | RedirectResponse:
    customer = _require_customer(request, settings, sessions, "/customer/delegations")
    if not isinstance(customer, CustomerContext):
        return customer
    secret = customer.session_secret or ""
    rows = delegation.list_for_customer(customer.customer_id)
    return pages.html_response(
        trust_pages.delegation_list_page(
            rows, lambda item: csrf_token(secret, CSRF_DELEGATION_REVOKE, item)
        )
    )


@router.post("/customer/delegations/{delegation_id}/revoke", response_model=None)
async def delegation_revoke(
    delegation_id: _Id,
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    delegation: DelegationServiceDep,
    correlation_id: CorrelationIdDep,
) -> HTMLResponse:
    form = await _read_form(request)
    customer = _post_guard(
        request, settings, sessions, CSRF_DELEGATION_REVOKE, delegation_id, form.get("csrf_token")
    )
    if not isinstance(customer, CustomerContext):
        return customer
    try:
        await run_in_threadpool(
            delegation.revoke,
            customer_id=customer.customer_id,
            delegation_id=delegation_id,
            correlation_id=correlation_id,
        )
    except AppError as error:
        return _error_page(error, "Not withdrawn")
    return _page(
        "Delegation withdrawn", "This agent can no longer act on your application.", 200, ok=True
    )


# --- identity proofing ----------------------------------------------------------------------------


@router.post("/customer/handoffs/{handoff_id}/identity-proofing", response_model=None)
async def start_identity_proofing(
    handoff_id: _Id,
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    onboarding: OnboardingServiceDep,
    proofing: ProofingServiceDep,
    correlation_id: CorrelationIdDep,
) -> Response:
    form = await _read_form(request)
    customer = _post_guard(
        request, settings, sessions, CSRF_PROOFING, handoff_id, form.get("csrf_token")
    )
    if not isinstance(customer, CustomerContext):
        return customer

    def run() -> str:
        review = onboarding.review_handoff(
            customer_id=customer.customer_id,
            handoff_id=handoff_id,
            nonce=form.get("token", ""),
            correlation_id=correlation_id,
        )
        return proofing.start(
            customer_id=customer.customer_id,
            session_id=customer.session_id,
            handoff_application_id=review.application.application_id,
            cid=correlation_id,
        )

    try:
        url = await run_in_threadpool(run)
    except AppError as error:
        return _error_page(error, "Identity verification not started")
    return pages.html_response(
        trust_pages.provider_handover_page(
            "Identity verification",
            url,
            "You are leaving the bank's page for a synthetic test provider. "
            "Do not enter real data.",
        )
    )


# --- the contract ---------------------------------------------------------------------------------


def _policy_reference(request: Request, application_id: str) -> dict[str, str]:
    with SqlAlchemyUnitOfWork(request.app.state.session_factory) as uow:
        row = uow.trust.scalars(
            select(PolicyDecisionRow)
            .where(PolicyDecisionRow.application_id == application_id)
            .order_by(PolicyDecisionRow.decided_at.desc())
        ).first()
        return {
            "decision_id": row.decision_id if row else "",
            "policy_version": row.policy_version if row else POLICY_VERSION,
        }


@router.get("/customer/applications/{application_id}/contract", response_model=None)
def contract_review(
    application_id: _Id,
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    contract: ContractServiceDep,
    governance: GovernanceServiceDep,
    correlation_id: CorrelationIdDep,
) -> HTMLResponse | RedirectResponse:
    customer = _require_customer(
        request, settings, sessions, f"/customer/applications/{application_id}/contract"
    )
    if not isinstance(customer, CustomerContext):
        return customer
    if not settings.full_trust_profile:
        return _page("Not available", "This step is not active.", 404)
    attribution = governance.latest_decision(application_id)
    reference = (
        {
            "decision_id": attribution["decision_id"],
            "outcome": attribution["outcome"],
            "partner_id": attribution["partner_id"],
            "policy_version": attribution["policy_version"],
        }
        if attribution
        else {}
    )
    try:
        package = contract.prepare_package(
            customer_id=customer.customer_id,
            application_id=application_id,
            attribution=reference,
            policy=_policy_reference(request, application_id),
            cid=correlation_id,
        )
    except AppError as error:
        return _error_page(error, "Contract cannot be shown")
    state = contract.signature_state(application_id)
    secret = customer.session_secret or ""
    return pages.html_response(
        trust_pages.contract_page(
            application_id,
            package.package_hash,
            package.document,
            state,
            csrf_token(secret, CSRF_CONTRACT_AUTHORIZE, application_id),
            csrf_token(secret, CSRF_CONTRACT_CANCEL, application_id),
            attribution,
        )
    )


@router.post("/customer/applications/{application_id}/contract/authorize", response_model=None)
async def contract_authorize(
    application_id: _Id,
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    contract: ContractServiceDep,
    correlation_id: CorrelationIdDep,
) -> Response:
    form = await _read_form(request)
    customer = _post_guard(
        request, settings, sessions, CSRF_CONTRACT_AUTHORIZE, application_id, form.get("csrf_token")
    )
    if not isinstance(customer, CustomerContext):
        return customer
    try:
        url = await run_in_threadpool(
            contract.authorize_signing,
            customer_id=customer.customer_id,
            application_id=application_id,
            package_hash=form.get("package_hash", ""),
            cid=correlation_id,
        )
    except AppError as error:
        return _error_page(error, "Signing not started")
    return pages.html_response(
        trust_pages.provider_handover_page(
            "Signing",
            url,
            "Your authorization is recorded. You are leaving the bank's page for a test signer, "
            "which creates no qualified electronic signature.",
        )
    )


@router.post("/customer/applications/{application_id}/contract/cancel", response_model=None)
async def contract_cancel(
    application_id: _Id,
    request: Request,
    settings: SettingsDep,
    sessions: CustomerSessionServiceDep,
    contract: ContractServiceDep,
    correlation_id: CorrelationIdDep,
) -> HTMLResponse:
    form = await _read_form(request)
    customer = _post_guard(
        request, settings, sessions, CSRF_CONTRACT_CANCEL, application_id, form.get("csrf_token")
    )
    if not isinstance(customer, CustomerContext):
        return customer
    try:
        await run_in_threadpool(
            contract.cancel,
            customer_id=customer.customer_id,
            application_id=application_id,
            cid=correlation_id,
        )
    except AppError as error:
        return _error_page(error, "Not cancelled")
    return _page(
        "Signature cancelled", "The signature was cancelled. No depot was opened.", 200, ok=True
    )


# --- signed provider callbacks --------------------------------------------------------------------


async def _callback(
    request: Request, handler: Callable[[str, str], str], correlation_id: str
) -> dict[str, str]:
    body = await request.body()
    if len(body) > _MAX_CALLBACK_BYTES:
        raise ServiceDisabledError()
    result = await run_in_threadpool(handler, body.decode("ascii", "replace"), correlation_id)
    return {"result": result}


@router.post("/internal/signature-provider/callbacks")
async def signature_callback(
    request: Request, settings: SettingsDep, contract: ContractServiceDep, cid: CorrelationIdDep
) -> dict[str, str]:
    if not settings.full_trust_profile:
        raise ServiceDisabledError()
    return await _callback(request, contract.handle_callback, cid)


@router.post("/internal/identity-proofing/callbacks")
async def proofing_callback(
    request: Request, settings: SettingsDep, proofing: ProofingServiceDep, cid: CorrelationIdDep
) -> dict[str, str]:
    if not settings.full_trust_profile:
        raise ServiceDisabledError()
    return await _callback(request, proofing.handle_callback, cid)
