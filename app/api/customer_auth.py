"""DEV/TEST simulated CUSTOMER sign-in - NOT identity verification.

The real ceremony needs the customer to authenticate to the bank through a channel the bank
controls (eID / EUDI wallet / online banking), independently of the AI agent. None of that
exists in this prototype. This module only provides a clearly labelled stand-in so the rest of
the ceremony (identity binding, digest binding, one-time challenge) can be built and tested.

Properties that matter:

* It is a SEPARATE context from the agent's (`X-Dev-Actor-Id`). An agent request never counts
  as a customer, and a customer context never counts as an agent.
* A confirmation link or QR code alone authenticates nobody: the page requires this sign-in
  first, and the confirmation needs the challenge nonce AND the matching customer identity.
* It is switched off in production (`Settings.dev_customer_auth_active`): every customer route
  then answers 401, i.e. the ceremony fails closed.
* LIMITATION: anyone who can send the header / complete the dev sign-in form can claim to be
  any customer. That is exactly what a real identity channel must prevent.
"""

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from app.api.deps import SettingsDep
from app.core.exceptions import AuthenticationRequiredError

DEV_CUSTOMER_HEADER = "X-Dev-Customer-Id"
DEV_CUSTOMER_COOKIE = "trusteq_dev_customer"
_CUSTOMER_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

_dev_customer_header = APIKeyHeader(
    name=DEV_CUSTOMER_HEADER,
    scheme_name="DevCustomerHeader",
    description=(
        "DEV/TEST ONLY. Simulated customer sign-in for the bank-controlled confirmation. "
        "This is not identity verification and is disabled in production."
    ),
    auto_error=False,
)


@dataclass(frozen=True)
class CustomerContext:
    customer_id: str
    is_dev_context: bool = True  # always True: there is no real customer authentication yet


def is_valid_customer_id(value: str | None) -> bool:
    return bool(value) and _CUSTOMER_ID.fullmatch(value or "") is not None


def sign_session(key: bytes, customer_id: str) -> str:
    mac = hmac.new(key, customer_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{customer_id}.{mac}"


def read_session(key: bytes, cookie_value: str | None) -> str | None:
    """Return the customer id from a validly signed dev-session cookie, else None."""
    if not cookie_value or "." not in cookie_value:
        return None
    customer_id, _, mac = cookie_value.rpartition(".")
    expected = hmac.new(key, customer_id.encode("utf-8"), hashlib.sha256).hexdigest()
    if is_valid_customer_id(customer_id) and hmac.compare_digest(mac, expected):
        return customer_id
    return None


def resolve_customer(
    request: Request, settings: SettingsDep, header_value: str | None
) -> CustomerContext | None:
    """The simulated customer for this request, or None. Raises nothing (pages decide)."""
    if not settings.dev_customer_auth_active:
        return None
    if header_value is not None:
        return CustomerContext(header_value) if is_valid_customer_id(header_value) else None
    session_key: bytes = request.app.state.dev_session_key
    cookie_customer = read_session(session_key, request.cookies.get(DEV_CUSTOMER_COOKIE))
    return CustomerContext(cookie_customer) if cookie_customer else None


def get_current_customer(
    request: Request,
    settings: SettingsDep,
    header_value: Annotated[str | None, Depends(_dev_customer_header)],
) -> CustomerContext:
    customer = resolve_customer(request, settings, header_value)
    if customer is None:
        raise AuthenticationRequiredError()
    return customer


CurrentCustomer = Annotated[CustomerContext, Depends(get_current_customer)]
