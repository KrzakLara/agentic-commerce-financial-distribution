"""Agent authorization: issue, verify and revoke scoped opaque bearer tokens (Phase 03).

CONTROLLED DEVELOPMENT AUTHORIZATION - this is NOT an OAuth 2.0 authorization server, NOT a
FAPI 2.0 profile and NOT certified for production. It implements the properties the ADR asks
of a "short-lived scoped mandate" so that they can be enforced and tested end to end:

* tokens are 256-bit random opaque strings (``tqa_`` + 43 url-safe characters); only their
  SHA-256 hash is stored, so a database leak does not reveal a usable credential;
* every token record has a unique ``jti``, an issuer, an audience, ``issued_at``/``expires_at``,
  its scopes (never more than its grant) and server-side revocation;
* every grant (mandate) names the agent, the customer it acts for and - except an unused
  bootstrap grant - exactly one application.

Lifecycle
---------
1. A TRUSTED bootstrap (``scripts.dev_auth`` / the demo / tests - never an HTTP endpoint)
   issues a BOOTSTRAP grant: one agent, one customer, scope ``create_draft``, short TTL.
2. The agent creates an application with it. In the same transaction the bank binds the
   bootstrap grant to that application (single use) and issues an ONBOARDING grant
   (``request_handoff``, ``submit_after_consent``) and a STATUS grant (``read_status``),
   one token each (`ApplicationService.create_application`).
3. Every protected request: `authenticate` -> `AgentPrincipal`; the route requires a scope
   (`require_scope`); the service checks resource binding, mandate state and policy.
4. At any terminal state the bootstrap and onboarding grants and their tokens are revoked in
   the transition's own transaction. The read-only status grant is left to expire, so the agent
   can still learn the coarse outcome - and nothing else.
"""

import hashlib
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.exceptions import (
    AgentAuthenticationRequiredError,
    AppError,
    InsufficientScopeError,
    InvalidTokenError,
    MandateInactiveError,
    NotFoundError,
    ServiceDisabledError,
)
from app.domain.application import DepotApplication
from app.domain.audit import AuditEvent
from app.domain.enums import AuditActorType, AuditOutcome, MandateKind, MandateScope
from app.domain.mandate import (
    AGENT_API_AUDIENCE,
    AUTHORIZATION_ISSUER,
    BOOTSTRAP_SCOPES,
    ONBOARDING_SCOPES,
    SCOPES_BY_KIND,
    STATUS_SCOPES,
    AgentAccessToken,
    Mandate,
)
from app.domain.workflow import TERMINAL_STATES
from app.repositories.interfaces import UnitOfWork

TOKEN_PREFIX = "tqa_"  # noqa: S105 - a public prefix (helps secret scanners), not a secret
_TOKEN_FORMAT = re.compile(r"^tqa_[A-Za-z0-9_\-]{43}$")
# Grants that end with the workflow. STATUS is deliberately not in here (read-only outcome).
REVOKED_AT_COMPLETION: tuple[MandateKind, ...] = (MandateKind.BOOTSTRAP, MandateKind.ONBOARDING)
DEV_BOOTSTRAP_OPERATOR = "dev-bootstrap"


def hash_token(raw_token: str) -> str:
    """SHA-256 of a high-entropy token. (No slow hash needed: tokens are 256-bit random.)"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def looks_like_agent_token(value: str | None) -> bool:
    return bool(value) and _TOKEN_FORMAT.fullmatch(value or "") is not None


@dataclass(frozen=True)
class AgentPrincipal:
    """The verified caller of one agent request. Built only by `authenticate`."""

    agent_actor_id: str
    customer_id: str
    mandate_id: str
    mandate_kind: MandateKind
    jti: str
    scopes: frozenset[MandateScope]  # effective scopes: token AND grant
    application_id: str | None  # the one application this credential concerns (if any)
    expires_at: datetime


@dataclass(frozen=True)
class IssuedToken:
    """A freshly issued credential. `token` is shown exactly once; only its hash is stored."""

    token: str
    jti: str
    mandate_id: str
    scopes: frozenset[MandateScope]
    expires_at: datetime

    def __repr__(self) -> str:  # never let a raw token end up in a log line or traceback
        return f"IssuedToken(jti={self.jti!r}, scopes={sorted(self.scopes)}, token=<hidden>)"


@dataclass(frozen=True)
class ApplicationCredentials:
    """What the bank hands the agent together with a new application."""

    onboarding: IssuedToken
    status: IssuedToken


def _rejection_reason(
    token: AgentAccessToken, mandate: Mandate | None, audience: str, now: datetime
) -> str | None:
    """Why a KNOWN token may not be used (None: it may). Fail closed on anything unexpected."""
    if mandate is None:
        return "grant_missing"
    if token.revoked_at is not None:
        return "token_revoked"
    if mandate.revoked_at is not None:
        return "grant_revoked"
    if token.issuer != AUTHORIZATION_ISSUER or mandate.issuer != AUTHORIZATION_ISSUER:
        return "wrong_issuer"
    if token.audience != audience or mandate.audience != audience:
        return "wrong_audience"
    if now < token.issued_at or now < mandate.issued_at:
        return "not_yet_valid"
    if now >= token.expires_at:
        return "token_expired"
    if now >= mandate.expires_at:
        return "grant_expired"
    if not token.scopes <= mandate.scopes:
        return "scope_exceeds_grant"
    return None


class AuthorizationService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        secret_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        bootstrap_enabled: bool = False,
        bootstrap_ttl_seconds: int = 900,
        status_ttl_seconds: int = 86400,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._id_factory = id_factory
        self._secret_factory = secret_factory
        self._bootstrap_enabled = bootstrap_enabled
        self._bootstrap_ttl = timedelta(seconds=bootstrap_ttl_seconds)
        self._status_ttl = timedelta(seconds=status_ttl_seconds)

    # --- helpers --------------------------------------------------------------------------------

    def _event(
        self,
        event_type: str,
        *,
        correlation_id: str,
        actor_type: AuditActorType,
        actor_id: str | None,
        outcome: AuditOutcome,
        application_id: str | None,
        details: dict[str, Any],
    ) -> AuditEvent:
        # `details` never contains a token, a token hash or customer data.
        return AuditEvent(
            event_id=f"evt-{self._id_factory()}",
            occurred_at=self._clock(),
            correlation_id=correlation_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            application_id=application_id,
            outcome=outcome,
            details=details,
        )

    def _add_token(
        self, uow: UnitOfWork, mandate: Mandate, scopes: frozenset[MandateScope], now: datetime
    ) -> IssuedToken:
        if not scopes or not scopes <= mandate.scopes:
            raise ValueError("a token can never carry more than its grant")
        raw = TOKEN_PREFIX + self._secret_factory()
        if not _TOKEN_FORMAT.fullmatch(raw):
            raise ValueError("the secret factory must return 43 url-safe characters")
        token = AgentAccessToken(
            jti=f"jti-{self._id_factory()}",
            token_hash=hash_token(raw),
            mandate_id=mandate.mandate_id,
            issuer=mandate.issuer,
            audience=mandate.audience,
            scopes=scopes,
            issued_at=now,
            expires_at=mandate.expires_at,  # no refresh flow: a token lives as long as its grant
        )
        uow.agent_tokens.add(token)
        return IssuedToken(raw, token.jti, mandate.mandate_id, scopes, token.expires_at)

    def _grant(
        self,
        kind: MandateKind,
        *,
        agent_actor_id: str,
        customer_id: str,
        application_id: str | None,
        now: datetime,
        lifetime: timedelta,
    ) -> Mandate:
        return Mandate(
            mandate_id=f"mdt-{self._id_factory()}",
            jti=f"grt-{self._id_factory()}",
            kind=kind,
            application_id=application_id,
            agent_actor_id=agent_actor_id,
            customer_id=customer_id,
            issuer=AUTHORIZATION_ISSUER,
            audience=AGENT_API_AUDIENCE,
            scopes=SCOPES_BY_KIND[kind],
            issued_at=now,
            expires_at=now + lifetime,
        )

    # --- 1. trusted bootstrap (never reachable over HTTP) -------------------------------------

    def issue_bootstrap(
        self,
        *,
        agent_actor_id: str,
        customer_id: str,
        correlation_id: str,
        ttl_seconds: int | None = None,
    ) -> IssuedToken:
        """Issue the narrow starting grant: create ONE draft for ONE customer. DEV/TEST only."""
        if not self._bootstrap_enabled:
            raise ServiceDisabledError()
        now = self._clock()
        lifetime = timedelta(seconds=ttl_seconds) if ttl_seconds else self._bootstrap_ttl
        grant = self._grant(
            MandateKind.BOOTSTRAP,
            agent_actor_id=agent_actor_id,
            customer_id=customer_id,
            application_id=None,
            now=now,
            lifetime=lifetime,
        )
        with self._uow_factory() as uow:
            uow.mandates.add(grant)
            issued = self._add_token(uow, grant, BOOTSTRAP_SCOPES, now)
            uow.audit.append(
                self._event(
                    "authorization.bootstrap_issued",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.OPERATOR,
                    actor_id=DEV_BOOTSTRAP_OPERATOR,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=None,
                    details={
                        "mandate_id": grant.mandate_id,
                        "jti": issued.jti,
                        "agent_actor_id": agent_actor_id,
                        "scopes": sorted(BOOTSTRAP_SCOPES),
                        "expires_at": grant.expires_at.isoformat(),
                    },
                )
            )
            uow.commit()
        return issued

    def issue_delegated_bootstrap(
        self,
        uow: UnitOfWork,
        *,
        agent_actor_id: str,
        customer_id: str,
        lifetime: timedelta,
        now: datetime,
    ) -> tuple[Mandate, IssuedToken]:
        """The starting grant of a customer's delegation, in the caller's transaction.

        Not the DEV bootstrap: this one exists only because an authenticated customer approved a
        specific request. It has the same narrow scope, so it can create one draft.
        """
        grant = self._grant(
            MandateKind.BOOTSTRAP,
            agent_actor_id=agent_actor_id,
            customer_id=customer_id,
            application_id=None,
            now=now,
            lifetime=lifetime,
        )
        uow.mandates.add(grant)
        return grant, self._add_token(uow, grant, BOOTSTRAP_SCOPES, now)

    # --- 2. grants issued by the bank together with a new application -------------------------

    def grant_application_access(
        self,
        uow: UnitOfWork,
        application: DepotApplication,
        *,
        onboarding_ttl: timedelta,
        now: datetime,
    ) -> tuple[Mandate, Mandate, ApplicationCredentials]:
        """Add the ONBOARDING and STATUS grants (+ one token each) to the caller's unit of work.

        No commit: they become real together with the application, or not at all.
        """
        onboarding = self._grant(
            MandateKind.ONBOARDING,
            agent_actor_id=application.agent_actor_id,
            customer_id=application.customer_ref,
            application_id=application.application_id,
            now=now,
            lifetime=onboarding_ttl,
        )
        status = self._grant(
            MandateKind.STATUS,
            agent_actor_id=application.agent_actor_id,
            customer_id=application.customer_ref,
            application_id=application.application_id,
            now=now,
            lifetime=self._status_ttl,
        )
        uow.mandates.add(onboarding)
        uow.mandates.add(status)
        credentials = ApplicationCredentials(
            onboarding=self._add_token(uow, onboarding, ONBOARDING_SCOPES, now),
            status=self._add_token(uow, status, STATUS_SCOPES, now),
        )
        return onboarding, status, credentials

    # --- 3. verification of every protected request -----------------------------------------

    def authenticate(
        self, raw_token: str | None, *, audience: str, correlation_id: str
    ) -> AgentPrincipal:
        """Verify an opaque bearer token. Fails closed with ONE generic error for every reason;
        the precise reason is audited when the token is known (never the token itself)."""
        if not raw_token:
            raise AgentAuthenticationRequiredError()
        if not _TOKEN_FORMAT.fullmatch(raw_token):
            raise InvalidTokenError()
        now = self._clock()
        with self._uow_factory() as uow:
            token = uow.agent_tokens.get_by_hash(hash_token(raw_token))
            if token is None:
                raise InvalidTokenError()  # unknown or forged: nothing to attribute it to
            mandate = uow.mandates.get(token.mandate_id)
            reason = _rejection_reason(token, mandate, audience, now)
            if reason is not None or mandate is None:
                raise self._deny(
                    uow,
                    InvalidTokenError(),
                    reason=reason or "grant_missing",
                    jti=token.jti,
                    mandate=mandate,
                    correlation_id=correlation_id,
                )
            return AgentPrincipal(
                agent_actor_id=mandate.agent_actor_id,
                customer_id=mandate.customer_id,
                mandate_id=mandate.mandate_id,
                mandate_kind=mandate.kind,
                jti=token.jti,
                scopes=token.scopes & mandate.scopes,
                application_id=mandate.application_id,
                expires_at=min(token.expires_at, mandate.expires_at),
            )

    def require_scope(
        self, principal: AgentPrincipal, scope: MandateScope, *, correlation_id: str
    ) -> None:
        """Raise 403 `insufficient_scope` (audited) unless the credential carries `scope`."""
        if scope in principal.scopes:
            return
        with self._uow_factory() as uow:
            uow.audit.append(
                self._event(
                    "authorization.denied",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.AGENT,
                    actor_id=principal.agent_actor_id,
                    outcome=AuditOutcome.DENIED,
                    application_id=principal.application_id,
                    details={
                        "reason": "insufficient_scope",
                        "required_scope": scope.value,
                        "jti": principal.jti,
                        "grant_kind": principal.mandate_kind.value,
                    },
                )
            )
            uow.commit()
        raise InsufficientScopeError()

    def _deny(
        self,
        uow: UnitOfWork,
        error: AppError,
        *,
        reason: str,
        jti: str,
        mandate: Mandate | None,
        correlation_id: str,
    ) -> AppError:
        uow.rollback()
        uow.audit.append(
            self._event(
                "authorization.denied",
                correlation_id=correlation_id,
                actor_type=AuditActorType.AGENT,
                actor_id=mandate.agent_actor_id if mandate else None,
                outcome=AuditOutcome.DENIED,
                application_id=mandate.application_id if mandate else None,
                details={"reason": reason, "jti": jti},
            )
        )
        uow.commit()
        return error

    def identify_agent(self, raw_token: str | None) -> str | None:
        """Who holds this agent token, if anyone (for audit attribution only - NOT a check)."""
        if not looks_like_agent_token(raw_token):
            return None
        with self._uow_factory() as uow:
            token = uow.agent_tokens.get_by_hash(hash_token(raw_token or ""))
            mandate = uow.mandates.get(token.mandate_id) if token else None
            return mandate.agent_actor_id if mandate else None

    # --- 4. trusted operator actions (dev CLI / tests; never reachable over HTTP) -------------

    def reissue_for_application(
        self, *, application_id: str, correlation_id: str
    ) -> ApplicationCredentials:
        """Re-issue an application's tokens (e.g. lost, or created before Phase 03). DEV only.

        Never widens anything: it uses the application's existing, ACTIVE onboarding grant and
        only that grant's own kind of scopes. A missing status grant is created for a
        non-terminal application (pre-Phase-03 data has none).
        """
        if not self._bootstrap_enabled:
            raise ServiceDisabledError()
        now = self._clock()
        with self._uow_factory() as uow:
            application = uow.applications.get(application_id)
            if application is None:
                raise NotFoundError()
            onboarding = uow.mandates.get_for_application(application_id, MandateKind.ONBOARDING)
            if (
                application.status in TERMINAL_STATES
                or onboarding is None
                or not onboarding.is_active(now)
            ):
                raise MandateInactiveError()
            status = uow.mandates.get_for_application(application_id, MandateKind.STATUS)
            if status is None:
                status = self._grant(
                    MandateKind.STATUS,
                    agent_actor_id=application.agent_actor_id,
                    customer_id=application.customer_ref,
                    application_id=application_id,
                    now=now,
                    lifetime=self._status_ttl,
                )
                uow.mandates.add(status)
            if not status.is_active(now):
                raise MandateInactiveError()
            credentials = ApplicationCredentials(
                onboarding=self._add_token(
                    uow, onboarding, onboarding.scopes & ONBOARDING_SCOPES, now
                ),
                status=self._add_token(uow, status, STATUS_SCOPES, now),
            )
            uow.audit.append(
                self._event(
                    "authorization.tokens_reissued",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.OPERATOR,
                    actor_id=DEV_BOOTSTRAP_OPERATOR,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=application_id,
                    details={
                        "onboarding_jti": credentials.onboarding.jti,
                        "status_jti": credentials.status.jti,
                    },
                )
            )
            uow.commit()
        return credentials

    def revoke_token(self, jti: str, *, correlation_id: str) -> bool:
        """Server-side revocation of one token (operator action). True if it was live."""
        now = self._clock()
        with self._uow_factory() as uow:
            token = uow.agent_tokens.get(jti)
            if token is None or not uow.agent_tokens.revoke(jti, now):
                return False
            mandate = uow.mandates.get(token.mandate_id)
            uow.audit.append(
                self._event(
                    "authorization.token_revoked",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.OPERATOR,
                    actor_id=DEV_BOOTSTRAP_OPERATOR,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=mandate.application_id if mandate else None,
                    details={"jti": jti},
                )
            )
            uow.commit()
            return True
