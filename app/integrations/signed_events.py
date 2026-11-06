"""Signed provider callbacks: one verification for signing and identity-proofing providers.

A callback is a compact JWT signed by a key pinned in the bank's configuration. It names the issuer
and the bank, has a short lifetime and a one-time id. Nothing inside it is read before that holds.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.integrations.jose_tools import SignatureRejected, verify_jwt
from app.repositories.interfaces import UnitOfWork
from app.repositories.trust_orm import UsedJtiRow

MAX_EVENT_LIFETIME_SECONDS = 600
CLOCK_SKEW_SECONDS = 60


class EventRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SignedEvent:
    claims: Mapping[str, Any]
    jti: str
    expires_at: datetime


def pinned_jwks(raw: str | None) -> Mapping[str, Any]:
    if not raw:
        raise EventRejected("provider_not_configured")
    try:
        return json.loads(raw)
    except ValueError:
        raise EventRejected("provider_not_configured") from None


def verify_event(
    token: str,
    *,
    jwks: Mapping[str, Any],
    typ: str,
    issuer: str | None,
    audience: str,
    now: datetime,
) -> SignedEvent:
    if not issuer:
        raise EventRejected("provider_not_configured")
    try:
        _, claims = verify_jwt(token, jwks, typ=typ)
    except SignatureRejected as rejected:
        raise EventRejected(rejected.reason) from None
    if claims.get("iss") != issuer:
        raise EventRejected("wrong_issuer")
    if claims.get("aud") != audience:
        raise EventRejected("wrong_audience")
    issued, expires = claims.get("iat"), claims.get("exp")
    if type(issued) is not int or type(expires) is not int:
        raise EventRejected("times_missing")
    stamp = now.timestamp()
    if issued > stamp + CLOCK_SKEW_SECONDS:
        raise EventRejected("issued_in_the_future")
    if stamp >= expires or expires - issued > MAX_EVENT_LIFETIME_SECONDS:
        raise EventRejected("expired")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not 16 <= len(jti) <= 128:
        raise EventRejected("jti_invalid")
    return SignedEvent(claims, jti, datetime.fromtimestamp(expires, UTC))


def consume_jti(uow: UnitOfWork, namespace: str, jti: str, expires_at: datetime) -> bool:
    """False if this one-time value was already used. The caller commits."""
    if uow.trust.get(UsedJtiRow, (namespace, jti)) is not None:
        return False
    uow.trust.add(UsedJtiRow(namespace=namespace, jti=jti, expires_at=expires_at))
    try:
        uow.trust.flush()
    except IntegrityError:
        uow.trust.rollback()
        return False
    return True
