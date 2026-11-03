"""DEV/TEST simulated CUSTOMER sign-in - NOT identity verification.

The real ceremony needs the customer to authenticate to the bank through a channel the bank
controls (eID / EUDI wallet / online banking), independently of the AI agent. None of that
exists in this prototype. This module provides a clearly labelled stand-in so the rest of the
ceremony (identity binding, digest binding, one-time challenge) can be built and tested.

Two DEV customer contexts, both switched off in production (`dev_customer_auth_active`):

* HTML pages (`/customer/...`): a SERVER-SIDE session (Phase 03). The dev sign-in form creates
  it; the browser gets a random secret in an HttpOnly, SameSite=Strict cookie (Secure on
  HTTPS), path ``/customer``; the server stores only its hash and enforces expiry. Forms also
  need a session-bound CSRF token (see `app.services.customer_session_service`).
* JSON ceremony (`POST /v1/customer/handoffs/{id}/confirm`): the ``X-Dev-Customer-Id`` header.

Separation from the agent: an agent's bearer token is never a customer credential. A request
that carries an ``Authorization`` header is refused on every customer route, and customer
secrets (``tqs_...``) are never accepted on agent routes.

LIMITATION (by design of the simulation): anyone who can complete the dev sign-in or send the
dev header can claim to be any customer. Preventing exactly that is the job of the real
identity channel, which is out of scope.
"""

import re
from dataclasses import dataclass

from fastapi import Request

from app.core.config import Settings
from app.services.customer_session_service import CustomerSessionService

DEV_CUSTOMER_HEADER = "X-Dev-Customer-Id"
SESSION_COOKIE = "trusteq_customer_session"
LOGIN_COOKIE = "trusteq_customer_login"  # pre-session CSRF secret of the sign-in form
COOKIE_PATH = "/customer"
LOGIN_COOKIE_MAX_AGE = 600
_CUSTOMER_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


@dataclass(frozen=True)
class CustomerContext:
    customer_id: str
    session_id: str | None = None  # None for the JSON header context
    session_secret: str | None = None  # raw cookie value, only to derive CSRF tokens
    is_dev_context: bool = True  # always True: there is no real customer authentication yet

    def __repr__(self) -> str:
        return f"CustomerContext(customer_id={self.customer_id!r}, session_id={self.session_id!r})"


def is_valid_customer_id(value: str | None) -> bool:
    return bool(value) and _CUSTOMER_ID.fullmatch(value or "") is not None


def agent_credentials_presented(request: Request) -> bool:
    """True if the request carries an Authorization header (an agent credential).

    The customer channel never accepts one - not alone, and not next to a customer context.
    """
    return request.headers.get("authorization") is not None


def header_customer(settings: Settings, header_value: str | None) -> CustomerContext | None:
    """The JSON API's DEV customer context, or None."""
    if not settings.dev_customer_auth_active or header_value is None:
        return None
    return CustomerContext(header_value) if is_valid_customer_id(header_value) else None


def session_customer(
    request: Request, settings: Settings, sessions: CustomerSessionService
) -> CustomerContext | None:
    """The signed-in customer of the HTML pages (active server-side session), or None."""
    if not settings.dev_customer_auth_active:
        return None
    secret = request.cookies.get(SESSION_COOKIE)
    session = sessions.resolve(secret)
    if session is None:
        return None
    return CustomerContext(session.customer_id, session.session_id, secret)


def cookie_is_secure(request: Request) -> bool:
    """Secure cookies whenever the page is served over HTTPS.

    Behind a TLS-terminating proxy run uvicorn with --proxy-headers so the scheme is right.
    """
    return request.url.scheme == "https"
