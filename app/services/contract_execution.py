"""Contract execution: the immutable package, the customer's authorization, the signed result.

Status: LOCAL VERIFIED with a labelled test signer, ADAPTER READY for a QTSP. No qualified
electronic
signature has ever been issued here. The level comes from the bank's configuration. A provider's
message claiming another level is refused, so a test signature cannot turn into a "QES".

Rules:

* the package is built from what the customer confirmed (the handoff's approved action), not from
  anything an agent sends, and it is append-only;
* only the application's own customer, on the bank's channel, can authorize signing;
* a signed callback must match the session, the provider's reference, the application, the signatory
  and the package hash, and it is valid once;
* no signature, no submission: `signature_gate` is part of the submit policy, so a failed, cancelled
  or expired signing can never lead to OPENED. It parks the application for a person instead.
"""

import contextlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    ProviderUnavailableError,
    ServiceDisabledError,
)
from app.domain.application import DepotApplication
from app.domain.enums import ApplicationStatus as S
from app.domain.enums import AuditActorType, AuditOutcome, ReviewCaseReason
from app.domain.review import ManualReviewCase
from app.domain.workflow import TransitionAuthority
from app.integrations.jose_tools import canonical_json, sha256_hex
from app.integrations.remote_providers import ProviderCallFailed, SignatureProviderPort
from app.integrations.signed_events import EventRejected, consume_jti, pinned_jwks, verify_event
from app.repositories.interfaces import UnitOfWork
from app.repositories.trust_orm import ContractPackageRow, SignatureSessionRow
from app.services.application_service import ApplicationService

EVENT_TYPE = "signature-event+jwt"
STATE_PENDING = "PENDING_CUSTOMER"
STATE_SIGNED = "SIGNED"
STATE_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
STATE_CANCELLED = "CANCELLED"
_FROM_PROVIDER = frozenset({STATE_SIGNED, "DECLINED", "EXPIRED", "FAILED", STATE_CANCELLED})
_NEEDS_A_PERSON = frozenset({"DECLINED", "EXPIRED", "FAILED"})
LEVEL_TEST = "TEST_NO_LEGAL_EFFECT"


class SignatureCallbackRejected(AppError):
    status_code = 403
    code = "callback_rejected"

    @classmethod
    def default_message(cls) -> str:
        return "The callback was refused."


class ContractHashMismatchError(ConflictError):
    code = "contract_hash_mismatch"

    @classmethod
    def default_message(cls) -> str:
        return "This is not the contract package that the bank prepared."


@dataclass(frozen=True)
class ContractPackage:
    package_id: str
    package_hash: str
    document: dict[str, Any]


def latest_session(uow: UnitOfWork, application_id: str) -> SignatureSessionRow | None:
    return uow.trust.scalars(
        select(SignatureSessionRow)
        .where(SignatureSessionRow.application_id == application_id)
        .order_by(SignatureSessionRow.attempt.desc())
    ).first()


def package_of(uow: UnitOfWork, application_id: str) -> ContractPackageRow | None:
    return uow.trust.scalar(
        select(ContractPackageRow).where(ContractPackageRow.application_id == application_id)
    )


def signature_gate(uow: UnitOfWork, application: DepotApplication) -> tuple[str, ...]:
    """What is missing before this application may be handed to the depot provider."""
    package = package_of(uow, application.application_id)
    if package is None:
        return ("contract_package",)
    if sha256_hex(package.canonical_json.encode("utf-8")) != package.package_hash:
        return ("contract_package_integrity",)
    session = latest_session(uow, application.application_id)
    if session is None or session.state != STATE_SIGNED:
        return ("contract_signature",)
    if session.package_hash != package.package_hash:
        return ("contract_signature_binding",)
    if session.signer_ref != application.customer_ref:
        return ("contract_signatory",)
    return ()


class ContractExecutionService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        applications: ApplicationService,
        *,
        provider: SignatureProviderPort | None,
        provider_id: str | None,
        provider_jwks: str | None,
        audience: str,
        level: str = LEVEL_TEST,
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
        self._level = level
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock
        self._id_factory = id_factory

    # --- the package ------------------------------------------------------------------------------

    def _own_application(self, uow: UnitOfWork, customer_id: str, app_id: str) -> DepotApplication:
        application = uow.applications.get(app_id)
        if application is None or application.customer_ref != customer_id:
            raise NotFoundError()  # another customer's application looks like a missing one
        return application

    def prepare_package(
        self,
        *,
        customer_id: str,
        application_id: str,
        attribution: dict[str, Any],
        policy: dict[str, Any],
        cid: str,
    ) -> ContractPackage:
        """Build the package once, from the confirmed action. Later calls return the same one."""
        now = self._clock()
        with self._uow_factory() as uow:
            application = self._own_application(uow, customer_id, application_id)
            existing = package_of(uow, application_id)
            if existing is not None:
                return ContractPackage(
                    existing.package_id, existing.package_hash, json.loads(existing.canonical_json)
                )
            if application.status is not S.CONTRACT_PENDING:
                raise ConflictError()
            consent = uow.receipts.get_consent(application_id)
            handoff = uow.handoffs.get(consent.handoff_id) if consent else None
            if handoff is None:
                raise ConflictError()
            action = handoff.action
            document: dict[str, Any] = {
                "package_type": "SYNTHETIC_DEPOT_CONTRACT_PACKAGE",
                "legal_effect": False,
                "application_id": application_id,
                "customer_ref": application.customer_ref,
                "provider_id": action.provider_id,
                "product_id": action.product_id,
                "product_version": action.catalog_version,
                "channel_id": action.channel_id,
                "partner_id": action.partner_id,
                "terms_digest": action.terms_digest,
                "documents": [
                    {
                        "document_id": action.document_id,
                        "document_version": action.document_version,
                        "document_digest": action.document_digest,
                    }
                ],
                "approved_action_digest": handoff.action_digest,
                "attribution_decision": attribution,
                "policy_decision": policy,
            }
            canonical = canonical_json(document).decode("utf-8")
            package = ContractPackageRow(
                package_id=f"pkg-{self._id_factory()}",
                application_id=application_id,
                customer_ref=application.customer_ref,
                canonical_json=canonical,
                package_hash=sha256_hex(canonical.encode("utf-8")),
                created_at=now,
            )
            uow.trust.add(package)
            uow.audit.append(
                self._apps.new_event(
                    event_type="contract.package_prepared",
                    correlation_id=cid,
                    actor_type=AuditActorType.SYSTEM,
                    actor_id=None,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=application_id,
                    details={
                        "package_id": package.package_id,
                        "package_hash": package.package_hash,
                        "product_version": action.catalog_version,
                        "legal_effect": False,
                    },
                )
            )
            uow.commit()
            return ContractPackage(package.package_id, package.package_hash, document)

    # --- the customer's authorization -------------------------------------------------------------

    def authorize_signing(
        self, *, customer_id: str, application_id: str, package_hash: str, cid: str
    ) -> str:
        """Record the customer's explicit authorization and start signing. Returns the signer
        URL."""
        if self._provider is None or not self._provider_id:
            raise ServiceDisabledError()
        now = self._clock()
        session_id = f"sig-{self._id_factory()}"
        expires_at = now + self._ttl
        with self._uow_factory() as uow:
            application = self._own_application(uow, customer_id, application_id)
            if application.status is not S.CONTRACT_PENDING:
                raise ConflictError()
            package = package_of(uow, application_id)
            if package is None or package.package_hash != package_hash:
                uow.audit.append(
                    self._apps.new_event(
                        event_type="contract.authorization_refused",
                        correlation_id=cid,
                        actor_type=AuditActorType.CUSTOMER,
                        actor_id=customer_id,
                        outcome=AuditOutcome.DENIED,
                        application_id=application_id,
                        details={"reason": "package_hash_mismatch"},
                    )
                )
                uow.commit()
                raise ContractHashMismatchError()
            current = latest_session(uow, application_id)
            if current is not None and current.state in {STATE_PENDING, STATE_SIGNED}:
                if current.state == STATE_SIGNED or now < current.expires_at:
                    raise ConflictError()
                current.state = "EXPIRED"
                current.updated_at = now
            uow.trust.add(
                SignatureSessionRow(
                    session_id=session_id,
                    application_id=application_id,
                    attempt=(current.attempt + 1) if current else 1,
                    package_hash=package.package_hash,
                    signer_ref=application.customer_ref,
                    state=STATE_PENDING,
                    provider_id=self._provider_id,
                    level=self._level,
                    legal_effect=False,
                    authorized_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                    evidence={},
                )
            )
            uow.audit.append(
                self._apps.new_event(
                    event_type="contract.signing_authorized",
                    correlation_id=cid,
                    actor_type=AuditActorType.CUSTOMER,
                    actor_id=customer_id,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=application_id,
                    details={
                        "session_id": session_id,
                        "package_hash": package.package_hash,
                        "provider_id": self._provider_id,
                        "level": self._level,
                    },
                )
            )
            uow.commit()
        try:
            started = self._provider.start_signing(
                session_id=session_id,
                application_id=application_id,
                package_hash=package_hash,
                signer_ref=customer_id,
                level=self._level,
                expires_at=int(expires_at.timestamp()),
            )
        except ProviderCallFailed:
            with self._uow_factory() as uow:
                row = uow.trust.get(SignatureSessionRow, session_id)
                row.state = STATE_UNAVAILABLE
                row.updated_at = self._clock()
                uow.commit()
            raise ProviderUnavailableError() from None
        with self._uow_factory() as uow:
            row = uow.trust.get(SignatureSessionRow, session_id)
            row.provider_ref = started.provider_ref
            uow.commit()
        return started.customer_url

    def signature_state(self, application_id: str) -> str | None:
        """What the customer's page shows. A session past its time counts as expired."""
        with self._uow_factory() as uow:
            session = latest_session(uow, application_id)
            if session is None:
                return None
            if session.state == STATE_PENDING and self._clock() >= session.expires_at:
                return "EXPIRED"
            return session.state

    def cancel(self, *, customer_id: str, application_id: str, cid: str) -> None:
        """The customer withdraws a running signature. The depot stays unopened."""
        now = self._clock()
        with self._uow_factory() as uow:
            self._own_application(uow, customer_id, application_id)
            session = latest_session(uow, application_id)
            if session is None or session.state != STATE_PENDING:
                raise ConflictError()
            session.state = STATE_CANCELLED
            session.updated_at = now
            reference = session.provider_ref
            uow.audit.append(
                self._apps.new_event(
                    event_type="contract.signing_cancelled",
                    correlation_id=cid,
                    actor_type=AuditActorType.CUSTOMER,
                    actor_id=customer_id,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=application_id,
                    details={"session_id": session.session_id},
                )
            )
            uow.commit()
        if reference and self._provider is not None:
            with contextlib.suppress(ProviderCallFailed):  # closed here: a late SIGNED is refused
                self._provider.cancel(reference)

    # --- the provider's signed result -------------------------------------------------------------

    def handle_callback(self, token: str, cid: str) -> str:
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
            raise SignatureCallbackRejected() from None
        claims = event.claims
        reason, application_id = None, None
        with self._uow_factory() as uow:
            row = uow.trust.get(SignatureSessionRow, str(claims.get("session_id")))
            if row is None:
                reason = "unknown_session"
            else:
                application_id = row.application_id
                if not consume_jti(uow, "signature_callback", event.jti, event.expires_at):
                    return "duplicate_ignored"
                reason = self._callback_problem(row, claims, now)
            if reason is None:
                row.state = str(claims["state"])
                row.updated_at = now
                row.evidence = {
                    "provider_event": event.jti,
                    "signed_document_hash": claims.get("package_hash"),
                    "signatory": row.signer_ref,
                    "level": row.level,
                    "legal_effect": False,
                }
                uow.audit.append(
                    self._apps.new_event(
                        event_type="contract.signature_result",
                        correlation_id=cid,
                        actor_type=AuditActorType.PROVIDER,
                        actor_id=self._provider_id,
                        outcome=AuditOutcome.SUCCESS
                        if row.state == STATE_SIGNED
                        else AuditOutcome.FAILURE,
                        application_id=row.application_id,
                        details={
                            "session_id": row.session_id,
                            "state": row.state,
                            "package_hash": row.package_hash,
                            "level": row.level,
                            "legal_effect": False,
                        },
                    )
                )
                if row.state in _NEEDS_A_PERSON:
                    self._park(uow, row, cid)
                uow.commit()
                return "accepted"
        self._audit_rejected(reason or "rejected", application_id, cid)
        raise SignatureCallbackRejected()

    def _callback_problem(
        self,
        row: SignatureSessionRow,
        claims: Any,
        now: datetime,  # noqa: ANN401
    ) -> str | None:
        state = claims.get("state")
        if state not in _FROM_PROVIDER:
            return "unknown_state"
        if row.provider_ref is None or claims.get("provider_ref") != row.provider_ref:
            return "provider_ref_mismatch"
        if claims.get("application_id") != row.application_id:
            return "application_mismatch"
        if claims.get("package_hash") != row.package_hash:
            return "package_hash_mismatch"
        if claims.get("signer_ref") != row.signer_ref:
            return "signatory_mismatch"
        if claims.get("level") != row.level:
            return "level_mismatch"  # e.g. a test signer claiming a qualified signature
        if row.state != STATE_PENDING:
            return "session_already_decided"
        if state == STATE_SIGNED and now >= row.expires_at:
            return "session_expired"
        return None

    def _park(self, uow: UnitOfWork, row: SignatureSessionRow, cid: str) -> None:
        application = uow.applications.get(row.application_id)
        if application is None or application.status is not S.CONTRACT_PENDING:
            return
        if uow.review_cases.get_active_for_application(row.application_id) is not None:
            return
        case = ManualReviewCase(
            case_id=f"rvw-{self._id_factory()}",
            application_id=row.application_id,
            reason=ReviewCaseReason.SIGNATURE_NOT_COMPLETED,
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
            reason_code="signature_not_completed",
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
                    "interrupted_step": S.CONTRACT_PENDING.value,
                },
            )
        )

    def _audit_rejected(self, reason: str, application_id: str | None, cid: str) -> None:
        with self._uow_factory() as uow:
            uow.audit.append(
                self._apps.new_event(
                    event_type="contract.callback_rejected",
                    correlation_id=cid,
                    actor_type=AuditActorType.PROVIDER,
                    actor_id=None,
                    outcome=AuditOutcome.DENIED,
                    application_id=application_id,
                    details={"reason": reason},
                )
            )
            uow.commit()
