"""Identity proofing: a vendor-neutral boundary, separate from authentication.

Status: ADAPTER READY for German eID, video identification or an EUDI wallet, and LOCAL VERIFIED
with
a clearly labelled synthetic provider. Real proofing needs a contract and credentials this project
does not have, so `regulatory_evidence` is always false here. An OIDC login never sets any of this.

The bank stores status, method and assurance labels only: no document images and no biometric data.
Evidence fields outside the fixed list are refused, so a provider cannot add them by accident.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    PolicyDeniedError,
    ProviderUnavailableError,
    ServiceDisabledError,
)
from app.domain.application import DepotApplication
from app.domain.enums import ApplicationStatus as S
from app.domain.enums import AuditActorType, AuditOutcome, ReviewCaseReason
from app.domain.review import ManualReviewCase
from app.domain.workflow import TransitionAuthority
from app.integrations.remote_providers import IdentityProofingPort, ProviderCallFailed
from app.integrations.signed_events import (
    EventRejected,
    consume_jti,
    pinned_jwks,
    verify_event,
)
from app.repositories.interfaces import UnitOfWork
from app.repositories.trust_orm import IdentityProofingRow
from app.services.application_service import ApplicationService
from app.services.oidc_login import session_was_authenticated

EVENT_TYPE = "identity-proofing-event+jwt"
STATUS_PENDING = "PENDING"
STATUS_VERIFIED = "VERIFIED"
_FINAL = frozenset({STATUS_VERIFIED, "REJECTED", "EXPIRED", "MANUAL_REVIEW"})
_NEEDS_A_PERSON = frozenset({"REJECTED", "EXPIRED", "MANUAL_REVIEW"})
_EVENT_FIELDS = frozenset(
    {
        "iss",
        "aud",
        "iat",
        "exp",
        "jti",
        "case_id",
        "provider_ref",
        "application_id",
        "customer_ref",
        "status",
        "method",
        "assurance",
        "regulatory_evidence",
    }
)


class ProofingCallbackRejected(AppError):
    status_code = 403
    code = "callback_rejected"

    @classmethod
    def default_message(cls) -> str:
        return "The callback was refused."


class ProofingRequiresAuthenticationError(PolicyDeniedError):
    code = "verified_login_required"

    @classmethod
    def default_message(cls) -> str:
        return "Identity verification needs a verified login. The DEV sign-in is not enough."


def latest_case(uow: UnitOfWork, application_id: str) -> IdentityProofingRow | None:
    return uow.trust.scalars(
        select(IdentityProofingRow)
        .where(IdentityProofingRow.application_id == application_id)
        .order_by(IdentityProofingRow.attempt.desc())
    ).first()


def proofing_satisfied(uow: UnitOfWork, application: DepotApplication, now: datetime) -> bool:
    case = latest_case(uow, application.application_id)
    return (
        case is not None
        and case.status == STATUS_VERIFIED
        and case.customer_ref == application.customer_ref
    )


class IdentityProofingService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        applications: ApplicationService,
        *,
        provider: IdentityProofingPort | None,
        provider_id: str | None,
        provider_jwks: str | None,
        audience: str,
        ttl_seconds: int = 900,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._uow_factory = uow_factory
        self._apps = applications
        self._provider = provider
        self._provider_id = provider_id
        self._jwks = provider_jwks
        self._audience = audience
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock
        self._id_factory = id_factory

    def start(
        self, *, customer_id: str, session_id: str | None, handoff_application_id: str, cid: str
    ) -> str:
        """Open a case for the customer's own application. Returns the provider's customer URL."""
        if self._provider is None or not self._provider_id:
            raise ServiceDisabledError()
        now = self._clock()
        case_id = f"idp-{self._id_factory()}"
        expires_at = now + self._ttl
        with self._uow_factory() as uow:
            application = uow.applications.get(handoff_application_id)
            if application is None or application.customer_ref != customer_id:
                raise NotFoundError()
            if not session_was_authenticated(uow, session_id, customer_id):
                raise ProofingRequiresAuthenticationError()
            if application.status is not S.AWAITING_AUTHORIZATION:
                raise ConflictError()
            existing = latest_case(uow, application.application_id)
            if existing is not None and existing.status == STATUS_VERIFIED:
                raise ConflictError()
            uow.trust.add(
                IdentityProofingRow(
                    case_id=case_id,
                    application_id=application.application_id,
                    attempt=(existing.attempt + 1) if existing else 1,
                    customer_ref=customer_id,
                    provider_id=self._provider_id,
                    status=STATUS_PENDING,
                    regulatory_evidence=False,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                )
            )
            uow.audit.append(
                self._apps.new_event(
                    event_type="identity_proofing.started",
                    correlation_id=cid,
                    actor_type=AuditActorType.CUSTOMER,
                    actor_id=customer_id,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=application.application_id,
                    details={"case_id": case_id, "provider_id": self._provider_id},
                )
            )
            uow.commit()
        try:
            started = self._provider.start_case(
                case_id=case_id,
                application_id=handoff_application_id,
                customer_ref=customer_id,
                expires_at=int(expires_at.timestamp()),
            )
        except ProviderCallFailed:
            raise ProviderUnavailableError() from None
        with self._uow_factory() as uow:
            row = uow.trust.get(IdentityProofingRow, case_id)
            row.provider_ref = started.provider_ref
            uow.commit()
        return started.customer_url

    def handle_callback(self, token: str, cid: str) -> str:
        """Apply a provider's signed result. The answer is `accepted` or `duplicate_ignored`."""
        now = self._clock()
        try:
            event = verify_event(
                token,
                jwks=pinned_jwks(self._jwks),
                typ=EVENT_TYPE,
                issuer=self._provider_id,
                audience=self._audience,
                now=now,
            )
        except EventRejected as rejected:
            self._audit_rejected(rejected.reason, None, cid)
            raise ProofingCallbackRejected() from None
        claims = event.claims
        if set(claims) - _EVENT_FIELDS:
            self._audit_rejected("undeclared_evidence_field", None, cid)
            raise ProofingCallbackRejected()
        reason, application_id = None, None
        with self._uow_factory() as uow:
            row = uow.trust.get(IdentityProofingRow, str(claims.get("case_id")))
            status = claims.get("status")
            if row is None:
                reason = "unknown_case"
            else:
                application_id = row.application_id
                if not consume_jti(uow, "proofing_callback", event.jti, event.expires_at):
                    return "duplicate_ignored"
                if (
                    claims.get("application_id") != row.application_id
                    or claims.get("customer_ref") != row.customer_ref
                    or claims.get("provider_ref") != row.provider_ref
                ):
                    reason = "binding_mismatch"
                elif claims.get("regulatory_evidence") is not False:
                    # A provider without a contract cannot deliver regulatory evidence.
                    reason = "regulatory_claim_not_accepted"
                elif status not in _FINAL:
                    reason = "unknown_status"
                elif row.status != STATUS_PENDING:
                    reason = "case_already_decided"
                elif now >= row.expires_at and status == STATUS_VERIFIED:
                    reason = "case_expired"
            if reason is None:
                row.status = str(status)
                row.method = str(claims.get("method", ""))[:40] or None
                row.assurance = str(claims.get("assurance", ""))[:60] or None
                row.updated_at = now
                uow.audit.append(
                    self._apps.new_event(
                        event_type="identity_proofing.result",
                        correlation_id=cid,
                        actor_type=AuditActorType.PROVIDER,
                        actor_id=self._provider_id,
                        outcome=AuditOutcome.SUCCESS,
                        application_id=row.application_id,
                        details={
                            "case_id": row.case_id,
                            "status": row.status,
                            "method": row.method,
                            "assurance": row.assurance,
                            "regulatory_evidence": False,
                            "kyc_status_set": False,
                        },
                    )
                )
                if row.status in _NEEDS_A_PERSON:
                    self._park(uow, row, cid)
                uow.commit()
                return "accepted"
        self._audit_rejected(reason or "rejected", application_id, cid)
        raise ProofingCallbackRejected()

    def _park(self, uow: UnitOfWork, row: IdentityProofingRow, cid: str) -> None:
        application = uow.applications.get(row.application_id)
        if application is None or application.status is not S.AWAITING_AUTHORIZATION:
            return
        if uow.review_cases.get_active_for_application(row.application_id) is not None:
            return
        case = ManualReviewCase(
            case_id=f"rvw-{self._id_factory()}",
            application_id=row.application_id,
            reason=ReviewCaseReason.IDENTITY_PROOFING_NOT_COMPLETED,
            interrupted_step=application.status,
            opened_by=AuditActorType.SYSTEM,
            created_at=self._clock(),
        )
        uow.review_cases.add(case)
        self._apps.apply_transition(
            uow,
            application,
            S.MANUAL_REVIEW,
            authority=TransitionAuthority.SYSTEM_POLICY,
            reason_code="identity_proofing_not_completed",
            correlation_id=cid,
        )
        uow.audit.append(
            self._apps.new_event(
                event_type="review.case_opened",
                correlation_id=cid,
                actor_type=AuditActorType.SYSTEM,
                actor_id=None,
                outcome=AuditOutcome.SUCCESS,
                application_id=row.application_id,
                details={
                    "case_id": case.case_id,
                    "reason": case.reason.value,
                    "interrupted_step": S.AWAITING_AUTHORIZATION.value,
                },
            )
        )

    def _audit_rejected(self, reason: str, application_id: str | None, cid: str) -> None:
        with self._uow_factory() as uow:
            uow.audit.append(
                self._apps.new_event(
                    event_type="identity_proofing.callback_rejected",
                    correlation_id=cid,
                    actor_type=AuditActorType.PROVIDER,
                    actor_id=None,
                    outcome=AuditOutcome.DENIED,
                    application_id=application_id,
                    details={"reason": reason},
                )
            )
            uow.commit()
