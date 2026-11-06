"""Customer-to-agent delegation: a LOCAL protocol built from published OAuth building blocks.

Status: LOCAL VERIFIED. This is not an OAuth 2.0 authorization server product and not FAPI 2.0
conformant. It uses the mechanisms, verified with `joserfc`:

* the agent's request is pushed to the bank first (shape of RFC 9126 PAR);
* the agent authenticates with a signed assertion of a registered key (RFC 7523, `private_key_jwt`);
* every token is bound to the agent's key by DPoP proofs (RFC 9449);
* the request names an exact action (shape of RFC 9396 `authorization_details`);
* the agent waits by polling with RFC 8628 error codes, so it needs no redirect listener.

FAPI 2.0 would require the authorization code flow, and it has no device grant. A contracted
authorization server replaces this module behind the same token verification (see
docs/identity-and-delegation.md).

Who decides what: the customer approves in the bank's channel, and the customer the delegation is
for
comes from the authenticated session, never from the agent. Every issued credential remains subject
to the bank's existing scope, ownership, state and policy checks.
"""

import base64
import hashlib
import json
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey
from sqlalchemy import select

from app.core.exceptions import (
    InvalidTokenError,
    NotFoundError,
    PolicyDeniedError,
    UnverifiedReferenceError,
)
from app.domain.audit import AuditEvent
from app.domain.enums import AuditActorType, AuditOutcome, MandateScope
from app.integrations.jose_tools import (
    ALGORITHMS,
    SignatureRejected,
    jwk_thumbprint,
    unverified_header,
    verify_jwt,
)
from app.integrations.ports import ProviderDiscoveryPort
from app.integrations.signed_events import consume_jti
from app.repositories.interfaces import UnitOfWork
from app.repositories.trust_orm import (
    DelegatedMandateRow,
    DelegationRow,
    RegisteredAgentRow,
)
from app.services.authorization_service import (
    AgentPrincipal,
    AuthorizationService,
    IssuedToken,
    hash_token,
)
from app.services.catalog_policy import resolve_offer

DELEGABLE_SCOPES = frozenset(
    {
        MandateScope.CREATE_DRAFT.value,
        MandateScope.REQUEST_HANDOFF.value,
        MandateScope.READ_STATUS.value,
        MandateScope.SUBMIT_AFTER_CONSENT.value,
    }
)
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"  # noqa: S105 - a grant type name
POLL_INTERVAL_SECONDS = 1
PROOF_WINDOW_SECONDS = 60
ASSERTION_MAX_LIFETIME_SECONDS = 300


class OAuthError(Exception):
    """An error in the shape of RFC 6749 section 5.2. Holds machine codes only."""

    def __init__(self, error: str, status_code: int = 400) -> None:
        super().__init__(error)
        self.error = error
        self.status_code = status_code


class DelegationNotActiveError(PolicyDeniedError):
    code = "delegation_not_active"

    @classmethod
    def default_message(cls) -> str:
        return "This delegation does not allow the action."


@dataclass(frozen=True)
class PushedDelegation:
    delegation_id: str
    request_uri: str
    device_code: str
    verification_uri_complete: str
    expires_in: int
    interval: int

    def __repr__(self) -> str:
        return f"PushedDelegation(delegation_id={self.delegation_id!r}, device_code=<hidden>)"


def verify_dpop_proof(
    proof: str | None,
    *,
    method: str,
    url: str,
    now: datetime,
    access_token: str | None = None,
) -> tuple[str, str]:
    """(key thumbprint, jti) of a valid RFC 9449 proof. Raises `SignatureRejected` otherwise."""
    if not proof:
        raise SignatureRejected("dpop_missing")
    header = unverified_header(proof)
    if header.get("typ") != "dpop+jwt" or header.get("alg") not in ALGORITHMS:
        raise SignatureRejected("dpop_header_invalid")
    jwk = header.get("jwk")
    thumbprint = jwk_thumbprint(jwk)
    try:
        claims = dict(jwt.decode(proof, ECKey.import_key(dict(jwk)), algorithms=ALGORITHMS).claims)
    except (ValueError, TypeError, KeyError, JoseError):
        raise SignatureRejected("dpop_bad_signature") from None
    issued = claims.get("iat")
    if type(issued) is not int or abs(now.timestamp() - issued) > PROOF_WINDOW_SECONDS:
        raise SignatureRejected("dpop_not_fresh")
    if claims.get("htm") != method or claims.get("htu") != url:
        raise SignatureRejected("dpop_wrong_request")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not 16 <= len(jti) <= 128:
        raise SignatureRejected("dpop_jti_invalid")
    if access_token is not None:
        digest = hashlib.sha256(access_token.encode("ascii")).digest()
        expected = _b64url(digest)
        if claims.get("ath") != expected:
            raise SignatureRejected("dpop_wrong_token")
    return thumbprint, jti


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def register_agent(
    uow: UnitOfWork,
    *,
    client_id: str,
    display_name: str,
    jwks: Mapping[str, Any],
    allowed_scopes: frozenset[str],
    now: datetime,
) -> None:
    """A trusted operator action: never reachable over HTTP, so no agent can register itself."""
    if not allowed_scopes <= DELEGABLE_SCOPES:
        raise ValueError("a registered agent cannot hold scopes the bank never delegates")
    uow.trust.add(
        RegisteredAgentRow(
            client_id=client_id,
            display_name=display_name,
            jwks=dict(jwks),
            allowed_scopes=sorted(allowed_scopes),
            active=True,
            created_at=now,
        )
    )


class DelegationService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        authorization: AuthorizationService,
        catalog: ProviderDiscoveryPort,
        public_base_url: str,
        delegation_ttl_seconds: int = 1800,
        request_ttl_seconds: int = 600,
        bootstrap_ttl_seconds: int = 900,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        secret_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        self._uow_factory = uow_factory
        self._authz = authorization
        self._catalog = catalog
        self._base = public_base_url
        self._ttl = timedelta(seconds=delegation_ttl_seconds)
        self._request_ttl = timedelta(seconds=request_ttl_seconds)
        self._bootstrap_ttl = timedelta(seconds=bootstrap_ttl_seconds)
        self._clock = clock
        self._id_factory = id_factory
        self._secret_factory = secret_factory

    # --- helpers --------------------------------------------------------------------------------

    @property
    def token_endpoint(self) -> str:
        return f"{self._base}/v1/delegation/token"

    @property
    def par_endpoint(self) -> str:
        return f"{self._base}/v1/delegation/par"

    def _event(
        self,
        event_type: str,
        *,
        correlation_id: str,
        actor_type: AuditActorType,
        actor_id: str | None,
        outcome: AuditOutcome,
        details: dict[str, Any],
        application_id: str | None = None,
    ) -> AuditEvent:
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

    def _audit_denied(self, reason: str, client_id: str | None, correlation_id: str) -> None:
        with self._uow_factory() as uow:
            uow.audit.append(
                self._event(
                    "delegation.denied",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.AGENT,
                    actor_id=client_id if client_id and len(client_id) <= 64 else None,
                    outcome=AuditOutcome.DENIED,
                    details={"reason": reason},
                )
            )
            uow.commit()

    def _authenticate_client(self, uow: UnitOfWork, assertion: str | None) -> RegisteredAgentRow:
        """A signed assertion by a registered, active agent, addressed to this bank. One use."""
        now = self._clock()
        if not assertion:
            raise OAuthError("invalid_client", 401)
        try:
            client_id = _unverified_subject(assertion)
            client = uow.trust.get(RegisteredAgentRow, client_id)
            if client is None or not client.active:
                raise OAuthError("invalid_client", 401)
            _, claims = verify_jwt(assertion, client.jwks, typ=None)
        except SignatureRejected:
            raise OAuthError("invalid_client", 401) from None
        expires = claims.get("exp")
        if (
            claims.get("iss") != client_id
            or claims.get("sub") != client_id
            or claims.get("aud") != self.token_endpoint
            or type(expires) is not int
            or not now.timestamp() < expires <= now.timestamp() + ASSERTION_MAX_LIFETIME_SECONDS
            or not isinstance(claims.get("jti"), str)
        ):
            raise OAuthError("invalid_client", 401)
        until = datetime.fromtimestamp(expires, UTC)
        if not consume_jti(uow, "client_assertion", claims["jti"][:128], until):
            raise OAuthError("invalid_client", 401)
        return client

    def _proof(
        self, uow: UnitOfWork, proof: str | None, method: str, url: str, token: str | None = None
    ) -> str:
        now = self._clock()
        thumbprint, jti = verify_dpop_proof(
            proof, method=method, url=url, now=now, access_token=token
        )
        until = now + timedelta(seconds=2 * PROOF_WINDOW_SECONDS)
        if not consume_jti(uow, "dpop", jti, until):
            raise SignatureRejected("dpop_replayed")
        return thumbprint

    # --- 1. the agent pushes its request ----------------------------------------------------------

    def push_request(
        self,
        *,
        client_assertion: str | None,
        dpop_proof: str | None,
        scope: str,
        authorization_details: Mapping[str, Any],
        correlation_id: str,
    ) -> PushedDelegation:
        now = self._clock()
        try:
            with self._uow_factory() as uow:
                client = self._authenticate_client(uow, client_assertion)
                client_id = client.client_id
                try:
                    jkt = self._proof(uow, dpop_proof, "POST", self.par_endpoint)
                except SignatureRejected as rejected:
                    raise OAuthError("invalid_dpop_proof") from rejected
                scopes = frozenset(scope.split())
                if (
                    MandateScope.CREATE_DRAFT.value not in scopes
                    or not scopes <= DELEGABLE_SCOPES
                    or not scopes <= frozenset(client.allowed_scopes)
                ):
                    raise OAuthError("invalid_scope")
                details = self._verified_details(authorization_details, now)
                delegation_id = f"dlg-{self._id_factory()}"
                device_code = self._secret_factory()
                request_token = self._secret_factory()
                uow.trust.add(
                    DelegationRow(
                        delegation_id=delegation_id,
                        client_id=client_id,
                        status="PENDING",
                        device_code_hash=hash_token(device_code),
                        request_token_hash=hash_token(request_token),
                        dpop_jkt=jkt,
                        scopes=sorted(scopes),
                        authorization_details=details,
                        created_at=now,
                        request_expires_at=now + self._request_ttl,
                    )
                )
                uow.audit.append(
                    self._event(
                        "delegation.requested",
                        correlation_id=correlation_id,
                        actor_type=AuditActorType.AGENT,
                        actor_id=client_id,
                        outcome=AuditOutcome.SUCCESS,
                        details={
                            "delegation_id": delegation_id,
                            "scopes": sorted(scopes),
                            "authorization_details": details,
                        },
                    )
                )
                uow.commit()
        except OAuthError as error:
            self._audit_denied(error.error, None, correlation_id)
            raise
        return PushedDelegation(
            delegation_id=delegation_id,
            request_uri=f"urn:trusteq:delegation:{delegation_id}",
            device_code=device_code,
            verification_uri_complete=(
                f"{self._base}/customer/delegations/authorize?request={request_token}"
            ),
            expires_in=int(self._request_ttl.total_seconds()),
            interval=POLL_INTERVAL_SECONDS,
        )

    def _verified_details(self, details: Mapping[str, Any], now: datetime) -> dict[str, str]:
        """The exact action, checked against the verified catalog. Unknown fields are refused."""
        keys = {"type", "provider_id", "product_id", "channel_id"}
        if not isinstance(details, Mapping) or set(details) != keys:
            raise OAuthError("invalid_authorization_details")
        if details["type"] != "depot_onboarding" or not all(
            isinstance(details[key], str) for key in keys
        ):
            raise OAuthError("invalid_authorization_details")
        try:
            resolve_offer(
                self._catalog,
                provider_id=details["provider_id"],
                product_id=details["product_id"],
                channel_id=details["channel_id"],
                now=now,
            )
        except UnverifiedReferenceError:
            raise OAuthError("invalid_authorization_details") from None
        return {key: details[key] for key in keys}

    # --- 2. the customer decides on the bank's page -----------------------------------------------

    def pending_request(self, request_token: str) -> DelegationRow:
        now = self._clock()
        with self._uow_factory() as uow:
            row = uow.trust.scalar(
                select(DelegationRow).where(
                    DelegationRow.request_token_hash == hash_token(request_token)
                )
            )
            if row is None or row.status != "PENDING" or now >= row.request_expires_at:
                raise NotFoundError()
            uow.trust.expunge(row)
            return row

    def display_name(self, client_id: str) -> str:
        with self._uow_factory() as uow:
            client = uow.trust.get(RegisteredAgentRow, client_id)
            return client.display_name if client else client_id

    def decide(
        self,
        *,
        customer_id: str,
        request_token: str,
        approve: bool,
        identity_assurance: str,
        correlation_id: str,
    ) -> DelegationRow:
        """The customer's decision. The customer comes from the session; a request decides once."""
        now = self._clock()
        with self._uow_factory() as uow:
            row = uow.trust.scalar(
                select(DelegationRow).where(
                    DelegationRow.request_token_hash == hash_token(request_token)
                )
            )
            if row is None or row.status != "PENDING" or now >= row.request_expires_at:
                raise NotFoundError()
            row.status = "APPROVED" if approve else "DENIED"
            row.customer_id = customer_id
            row.identity_assurance = identity_assurance
            row.decided_at = now
            if approve:
                row.expires_at = now + self._ttl
            uow.audit.append(
                self._event(
                    "delegation.approved" if approve else "delegation.declined",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.CUSTOMER,
                    actor_id=customer_id,
                    outcome=AuditOutcome.SUCCESS,
                    details={
                        "delegation_id": row.delegation_id,
                        "client_id": row.client_id,
                        "scopes": row.scopes,
                        "authorization_details": row.authorization_details,
                        "identity_assurance": identity_assurance,
                    },
                )
            )
            uow.commit()
            uow.trust.expunge(row)
            return row

    # --- 3. the agent collects the token ----------------------------------------------------------

    def exchange(
        self,
        *,
        grant_type: str | None,
        device_code: str | None,
        client_assertion: str | None,
        dpop_proof: str | None,
        correlation_id: str,
    ) -> tuple[IssuedToken, DelegationRow]:
        now = self._clock()
        if grant_type != DEVICE_GRANT or not device_code:
            raise OAuthError("unsupported_grant_type")
        try:
            with self._uow_factory() as uow:
                client = self._authenticate_client(uow, client_assertion)
                row = uow.trust.scalar(
                    select(DelegationRow).where(
                        DelegationRow.device_code_hash == hash_token(device_code)
                    )
                )
                if row is None or row.client_id != client.client_id:
                    raise OAuthError("invalid_grant")
                try:
                    jkt = self._proof(uow, dpop_proof, "POST", self.token_endpoint)
                except SignatureRejected as rejected:
                    raise OAuthError("invalid_dpop_proof") from rejected
                if jkt != row.dpop_jkt:
                    raise OAuthError("invalid_dpop_proof")
                if row.status == "PENDING":
                    error = self._pending_error(row, now)
                    uow.commit()  # the poll time and the consumed one-time values stay recorded
                    raise OAuthError(error)
                if row.status == "DENIED":
                    raise OAuthError("access_denied")
                if row.status != "APPROVED" or row.expires_at is None or now >= row.expires_at:
                    # A second collection of the same grant is treated as a stolen device code.
                    if row.status == "ISSUED":
                        self._revoke_row(uow, row, now, "device_code_replayed", correlation_id)
                        uow.commit()
                    raise OAuthError("invalid_grant")
                lifetime = min(self._bootstrap_ttl, row.expires_at - now)
                mandate, issued = self._authz.issue_delegated_bootstrap(
                    uow,
                    agent_actor_id=row.client_id,
                    customer_id=row.customer_id or "",
                    lifetime=lifetime,
                    now=now,
                )
                uow.trust.add(
                    DelegatedMandateRow(
                        mandate_id=mandate.mandate_id, delegation_id=row.delegation_id
                    )
                )
                row.status = "ISSUED"
                row.token_issued_at = now
                uow.audit.append(
                    self._event(
                        "delegation.token_issued",
                        correlation_id=correlation_id,
                        actor_type=AuditActorType.SYSTEM,
                        actor_id=None,
                        outcome=AuditOutcome.SUCCESS,
                        details={
                            "delegation_id": row.delegation_id,
                            "client_id": row.client_id,
                            "mandate_id": mandate.mandate_id,
                            "jti": issued.jti,
                            "dpop_bound": True,
                        },
                    )
                )
                uow.commit()
                uow.trust.expunge(row)
                return issued, row
        except OAuthError as error:
            if error.error not in {"authorization_pending", "slow_down"}:
                self._audit_denied(error.error, None, correlation_id)
            raise

    def _pending_error(self, row: DelegationRow, now: datetime) -> str:
        if now >= row.request_expires_at:
            return "expired_token"
        too_fast = (
            row.last_poll_at is not None
            and (now - row.last_poll_at).total_seconds() < POLL_INTERVAL_SECONDS
        )
        row.last_poll_at = now
        return "slow_down" if too_fast else "authorization_pending"

    # --- 4. every delegated request ---------------------------------------------------------------

    def check_request(
        self,
        principal: AgentPrincipal,
        *,
        scheme: str,
        raw_token: str,
        proof: str | None,
        method: str,
        url: str,
        correlation_id: str,
    ) -> AgentPrincipal:
        """Bind a request to its delegation. Credentials from other grants pass unchanged.

        A delegated token needs a fresh DPoP proof of the delegation's key, the delegation must be
        live, and the effective scopes are what the customer approved, whatever the token holds.
        """
        now = self._clock()
        with self._uow_factory() as uow:
            link = uow.trust.get(DelegatedMandateRow, principal.mandate_id)
            if link is None:
                if scheme != "bearer":
                    raise InvalidTokenError()
                return principal
            row = uow.trust.get(DelegationRow, link.delegation_id)
            reason = None
            if row is None or row.status != "ISSUED" or row.revoked_at is not None:
                reason = "delegation_revoked"
            elif row.expires_at is None or now >= row.expires_at:
                reason = "delegation_expired"
            elif scheme != "dpop":
                reason = "dpop_downgrade"
            if reason is None:
                try:
                    thumbprint = self._proof(uow, proof, method, url, raw_token)
                    if thumbprint != row.dpop_jkt:
                        reason = "dpop_wrong_key"
                except SignatureRejected as rejected:
                    reason = rejected.reason
            if reason is not None:
                uow.rollback()
                uow.audit.append(
                    self._event(
                        "authorization.denied",
                        correlation_id=correlation_id,
                        actor_type=AuditActorType.AGENT,
                        actor_id=principal.agent_actor_id,
                        outcome=AuditOutcome.DENIED,
                        application_id=principal.application_id,
                        details={"reason": reason, "jti": principal.jti},
                    )
                )
                uow.commit()
                raise InvalidTokenError()
            scopes = frozenset(MandateScope(s) for s in row.scopes)
            uow.commit()
            return replace(principal, scopes=principal.scopes & scopes)

    def enforce_action(
        self,
        principal: AgentPrincipal,
        *,
        provider_id: str,
        product_id: str,
        channel_id: str,
        correlation_id: str,
    ) -> None:
        """A delegated draft must be exactly the action the customer approved."""
        with self._uow_factory() as uow:
            link = uow.trust.get(DelegatedMandateRow, principal.mandate_id)
            if link is None:
                return
            row = uow.trust.get(DelegationRow, link.delegation_id)
            details = row.authorization_details if row else {}
            if (
                details.get("provider_id") == provider_id
                and details.get("product_id") == product_id
                and details.get("channel_id") == channel_id
            ):
                return
            uow.audit.append(
                self._event(
                    "authorization.denied",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.AGENT,
                    actor_id=principal.agent_actor_id,
                    outcome=AuditOutcome.DENIED,
                    details={"reason": "action_not_delegated", "jti": principal.jti},
                )
            )
            uow.commit()
        raise DelegationNotActiveError()

    def link_application_grants(
        self, *, bootstrap_mandate_id: str, mandate_ids: tuple[str, ...]
    ) -> None:
        """The application's grants stand behind the same delegation as the bootstrap grant."""
        with self._uow_factory() as uow:
            link = uow.trust.get(DelegatedMandateRow, bootstrap_mandate_id)
            if link is None:
                return
            for mandate_id in mandate_ids:
                uow.trust.add(
                    DelegatedMandateRow(mandate_id=mandate_id, delegation_id=link.delegation_id)
                )
            uow.commit()

    def delegation_of(self, mandate_id: str) -> DelegationRow | None:
        with self._uow_factory() as uow:
            link = uow.trust.get(DelegatedMandateRow, mandate_id)
            row = uow.trust.get(DelegationRow, link.delegation_id) if link else None
            if row is not None:
                uow.trust.expunge(row)
            return row

    # --- 5. revocation ----------------------------------------------------------------------------

    def _revoke_row(
        self, uow: UnitOfWork, row: DelegationRow, now: datetime, reason: str, correlation_id: str
    ) -> None:
        row.status = "REVOKED"
        row.revoked_at = now
        links = uow.trust.scalars(
            select(DelegatedMandateRow).where(
                DelegatedMandateRow.delegation_id == row.delegation_id
            )
        ).all()
        for link in links:
            uow.mandates.revoke(link.mandate_id, now)
            uow.agent_tokens.revoke_for_mandate(link.mandate_id, now)
        uow.audit.append(
            self._event(
                "delegation.revoked",
                correlation_id=correlation_id,
                actor_type=AuditActorType.CUSTOMER
                if reason == "customer_revoked"
                else AuditActorType.SYSTEM,
                actor_id=row.customer_id if reason == "customer_revoked" else None,
                outcome=AuditOutcome.SUCCESS,
                details={"delegation_id": row.delegation_id, "reason": reason},
            )
        )

    def revoke(self, *, customer_id: str, delegation_id: str, correlation_id: str) -> None:
        """Only the delegation's own customer. Anyone else is told it does not exist."""
        now = self._clock()
        with self._uow_factory() as uow:
            row = uow.trust.get(DelegationRow, delegation_id)
            if row is None or row.customer_id != customer_id:
                raise NotFoundError()
            if row.status in {"REVOKED", "DENIED"}:
                return
            self._revoke_row(uow, row, now, "customer_revoked", correlation_id)
            uow.commit()

    def list_for_customer(self, customer_id: str) -> list[DelegationRow]:
        with self._uow_factory() as uow:
            rows = list(
                uow.trust.scalars(
                    select(DelegationRow)
                    .where(DelegationRow.customer_id == customer_id)
                    .order_by(DelegationRow.created_at.desc())
                )
            )
            for row in rows:
                uow.trust.expunge(row)
            return rows


def _unverified_subject(assertion: str) -> str:
    """Which client's keys to verify with. Nothing is believed until that signature checks out."""
    try:
        payload = assertion.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, IndexError, TypeError):
        raise OAuthError("invalid_client", 401) from None
    subject = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(subject, str) or not 1 <= len(subject) <= 64:
        raise OAuthError("invalid_client", 401)
    return subject
