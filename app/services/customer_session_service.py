"""Customer sessions and CSRF tokens for the bank-controlled pages (Phase 03).

The sign-in that creates a session is still a DEVELOPMENT SIMULATION (anyone can type a
customer id); what is real here is everything *around* it:

* the session is a 256-bit random secret in an HttpOnly, SameSite=Strict cookie; the server
  stores only its SHA-256 hash and decides expiry and revocation itself;
* signing in again rotates the session (the previous one is revoked);
* state-changing forms carry a CSRF token derived from the session secret and the form's
  target (HMAC-SHA256), so it is bound to ONE session and ONE handoff and is useless anywhere
  else. The sign-in form, which has no session yet, uses the same scheme with a short-lived
  pre-session cookie (login CSRF);
* customer secrets (``tqs_``) and agent tokens (``tqa_``) live in different tables and are
  never accepted in each other's place.
"""

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.domain.audit import AuditEvent
from app.domain.customer_session import CustomerSession
from app.domain.enums import AuditActorType, AuditOutcome
from app.repositories.interfaces import UnitOfWork

SESSION_PREFIX = "tqs_"
_SESSION_FORMAT = re.compile(r"^tqs_[A-Za-z0-9_\-]{43}$")
_PRE_SESSION_FORMAT = re.compile(r"^[A-Za-z0-9_\-]{43}$")
_CSRF_FORMAT = re.compile(r"^[A-Za-z0-9_\-]{43}$")


def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_pre_session_secret() -> str:
    """Random value for the pre-session (sign-in form) cookie."""
    return secrets.token_urlsafe(32)


def is_pre_session_secret(value: str | None) -> bool:
    return bool(value) and _PRE_SESSION_FORMAT.fullmatch(value or "") is not None


def csrf_token(secret: str, purpose: str, binding: str) -> str:
    """HMAC-SHA256(secret, purpose|binding) as 43 url-safe characters.

    `secret` is the raw session (or pre-session) secret the browser presents in its cookie;
    the server never stores it, it re-derives the token on every request.
    """
    mac = hmac.new(secret.encode("utf-8"), f"csrf|{purpose}|{binding}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode("ascii")


def csrf_matches(secret: str | None, purpose: str, binding: str, presented: str | None) -> bool:
    """Constant-time check. Missing secret, missing token or malformed token: False."""
    if not secret or not presented or not _CSRF_FORMAT.fullmatch(presented):
        return False
    return hmac.compare_digest(csrf_token(secret, purpose, binding), presented)


class CustomerSessionService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        secret_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        ttl_seconds: int = 900,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._id_factory = id_factory
        self._secret_factory = secret_factory
        self._ttl = timedelta(seconds=ttl_seconds)

    def end(self, secret: str | None, *, correlation_id: str) -> bool:
        """Log out: revoke this browser's session on the server. True if it was active."""
        session = self.resolve(secret)
        if session is None:
            return False
        now = self._clock()
        with self._uow_factory() as uow:
            uow.customer_sessions.revoke(session.session_id, now)
            uow.audit.append(
                AuditEvent(
                    event_id=f"evt-{self._id_factory()}",
                    occurred_at=now,
                    correlation_id=correlation_id,
                    event_type="customer.session_ended",
                    actor_type=AuditActorType.CUSTOMER,
                    actor_id=session.customer_id,
                    application_id=None,
                    outcome=AuditOutcome.SUCCESS,
                    details={"session_id": session.session_id},
                )
            )
            uow.commit()
        return True

    def start(
        self,
        *,
        customer_id: str,
        replaces: str | None,
        correlation_id: str,
        simulated: bool = True,
    ) -> tuple[CustomerSession, str]:
        """Create a session after the (simulated) sign-in; revoke the browser's previous one.

        Returns the stored record and the raw secret for the cookie (shown once).
        """
        now = self._clock()
        secret = SESSION_PREFIX + self._secret_factory()
        if not _SESSION_FORMAT.fullmatch(secret):
            raise ValueError("the secret factory must return 43 url-safe characters")
        session = CustomerSession(
            session_id=f"ses-{self._id_factory()}",
            secret_hash=hash_secret(secret),
            customer_id=customer_id,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._uow_factory() as uow:
            replaced = False
            if replaces and _SESSION_FORMAT.fullmatch(replaces):
                previous = uow.customer_sessions.get_by_hash(hash_secret(replaces))
                if previous is not None:
                    replaced = uow.customer_sessions.revoke(previous.session_id, now)
            uow.customer_sessions.add(session)
            uow.audit.append(
                AuditEvent(
                    event_id=f"evt-{self._id_factory()}",
                    occurred_at=now,
                    correlation_id=correlation_id,
                    event_type="customer.session_started",
                    actor_type=AuditActorType.CUSTOMER,
                    actor_id=customer_id,
                    application_id=None,
                    outcome=AuditOutcome.SUCCESS,
                    details={
                        "session_id": session.session_id,
                        "simulated_sign_in": simulated,
                        "expires_at": session.expires_at.isoformat(),
                        "replaced_previous_session": replaced,
                    },
                )
            )
            uow.commit()
        return session, secret

    def resolve(self, secret: str | None) -> CustomerSession | None:
        """The ACTIVE session for this cookie value, else None (unknown, expired, revoked)."""
        if not secret or not _SESSION_FORMAT.fullmatch(secret):
            return None
        now = self._clock()
        with self._uow_factory() as uow:
            session = uow.customer_sessions.get_by_hash(hash_secret(secret))
        return session if session is not None and session.is_active(now) else None
