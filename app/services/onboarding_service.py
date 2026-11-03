"""Trusted onboarding orchestration (trust zone T2): the Phase 02 vertical slice.

    agent: request handoff -> customer: confirm -> bank: KYC, compliance, contract (MOCK)
    -> agent: submit -> depot provider (MOCK): signed confirmation -> OPENED

Every step goes through the persistent state machine (`ApplicationService.apply_transition`)
with the correct `TransitionAuthority`, inside one unit of work together with its audit event.
Nothing here reads an LLM output; every decision is a deterministic check. Denials are audited
in their own committed transaction and then raised, so a refused attempt leaves a trace but no
state change.

Phase 03: agent operations take a verified `AgentPrincipal` (scope + resource binding are
re-checked here, at the service boundary), and `submit` first resumes mandatory checks that an
interrupted process left pending - bounded, idempotent, never repeating a recorded step.
"""

import hashlib
import hmac
import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import (
    ActionDigestMismatchError,
    AppError,
    ConcurrentModificationError,
    ConfirmationRejectedError,
    CustomerMismatchError,
    HandoffExpiredError,
    HandoffInvalidError,
    HandoffNotActiveError,
    InvalidStateError,
    InvalidTransitionError,
    MandateInactiveError,
    NotFoundError,
    PrerequisitesNotMetError,
    ProviderUnavailableError,
    TransitionNotAuthorizedError,
    UnverifiedReferenceError,
)
from app.domain.application import DepotApplication
from app.domain.attribution import ChannelAttribution
from app.domain.base import DomainModel, OpaqueId
from app.domain.checks import CheckRecord, DepotSubmissionRecord
from app.domain.enums import (
    ApplicationStatus as S,
)
from app.domain.enums import (
    AuditActorType,
    AuditOutcome,
    CheckOutcome,
    CheckType,
    MandateScope,
    ReviewCaseReason,
    ReviewCaseStatus,
    ReviewDecision,
    ReviewEvidenceCategory,
)
from app.domain.handoff import ApprovedAction, Handoff
from app.domain.mandate import Mandate
from app.domain.receipts import ConsentReceipt, DisclosureReceipt
from app.domain.review import ManualReviewCase
from app.domain.workflow import TransitionAuthority as A
from app.domain.workflow import check_transition
from app.integrations.ports import (
    CompliancePort,
    ContractSigningPort,
    DepotProviderPort,
    KycPort,
    ProviderDiscoveryPort,
)
from app.repositories.interfaces import UnitOfWork
from app.services.application_service import ApplicationService
from app.services.authorization_service import AgentPrincipal
from app.services.catalog_policy import VerifiedOffer, resolve_offer
from app.services.policy import (
    PolicyDecision,
    evaluate_handoff_request,
    evaluate_submit,
    mandate_allows,
)

# Steps of the mandatory-check pipeline that a crashed process may leave behind.
_RESUMABLE_CHECK_STATES = frozenset({S.AUTHORIZED, S.KYC_PENDING, S.COMPLIANCE_PENDING})
logger = logging.getLogger("app.onboarding")


def _grant_allows(
    mandate: Mandate | None, principal: AgentPrincipal, scope: MandateScope, now: datetime
) -> bool:
    """The application's onboarding grant is active for `scope` AND is the very grant behind
    the presented token (a token of another grant never borrows this one's authority)."""
    return (
        mandate_allows(mandate, scope, principal.agent_actor_id, now)
        and mandate is not None
        and mandate.mandate_id == principal.mandate_id
    )


class ProviderConfirmation(DomainModel):
    """The depot provider's 'account opened' message after signature verification."""

    application_id: OpaqueId
    provider_ref: OpaqueId
    confirmation_id: OpaqueId
    outcome: Literal["OPENED"]


@dataclass(frozen=True)
class HandoffIssue:
    handoff: Handoff
    nonce: str  # shown once, to be delivered to the customer; only its hash is stored


@dataclass(frozen=True)
class HandoffReview:
    handoff: Handoff
    application: DepotApplication
    document_text: str


@dataclass(frozen=True)
class SubmitResult:
    application: DepotApplication
    replayed: bool


@dataclass(frozen=True)
class ProviderConfirmationResult:
    application: DepotApplication
    duplicate: bool


def _hash_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def check_case_reason(check: CheckType, outcome: CheckOutcome) -> ReviewCaseReason:
    """Internal case reason for a check that did not pass (never shown to agent or customer)."""
    if outcome is CheckOutcome.FAIL:
        return ReviewCaseReason.CHECK_FAILED
    if check is CheckType.KYC:
        return ReviewCaseReason.KYC_REVIEW_REQUIRED
    return ReviewCaseReason.COMPLIANCE_REVIEW_REQUIRED


class OnboardingService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        applications: ApplicationService,
        *,
        catalog: ProviderDiscoveryPort,
        kyc: KycPort,
        compliance: CompliancePort,
        contract: ContractSigningPort,
        depot: DepotProviderPort,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        handoff_ttl_seconds: int = 600,
    ) -> None:
        self._uow_factory = uow_factory
        self._apps = applications
        self._catalog = catalog
        self._kyc = kyc
        self._compliance = compliance
        self._contract = contract
        self._depot = depot
        self._clock = clock
        self._id_factory = id_factory
        self._nonce_factory = nonce_factory
        self._handoff_ttl = timedelta(seconds=handoff_ttl_seconds)

    # --- helpers ------------------------------------------------------------

    def _deny(
        self,
        uow: UnitOfWork,
        error: AppError,
        *,
        event_type: str,
        actor_type: AuditActorType,
        actor_id: str | None,
        application_id: str | None,
        correlation_id: str,
        details: dict[str, Any],
    ) -> AppError:
        """Discard the failed attempt's writes, record the refusal durably, return the error.

        Usage: ``raise self._deny(...)``. The caller's other writes are rolled back so a
        denied request can never leave partial state behind.
        """
        uow.rollback()
        uow.audit.append(
            self._apps.new_event(
                event_type=event_type,
                correlation_id=correlation_id,
                actor_type=actor_type,
                actor_id=actor_id,
                outcome=AuditOutcome.DENIED,
                application_id=application_id,
                details=details,
            )
        )
        uow.commit()
        return error

    def _offer(self, application: DepotApplication) -> VerifiedOffer:
        return resolve_offer(
            self._catalog,
            provider_id=application.provider_id,
            product_id=application.product_id,
            channel_id=application.channel_id,
            now=self._clock(),
        )

    def _approved_action(
        self, application: DepotApplication, offer: VerifiedOffer | None = None
    ) -> ApprovedAction:
        """Build the action from the VERIFIED catalog (never from agent-supplied text).

        Phase 04: it also binds the exact product terms (`terms_digest`) and the partner the
        registry attributes this channel to - so changed terms, a changed disclosure or a
        changed attribution make an earlier approval stale.
        """
        offer = offer or self._offer(application)
        product = offer.product
        return ApprovedAction(
            application_id=application.application_id,
            provider_id=application.provider_id,
            product_id=application.product_id,
            channel_id=application.channel_id,
            customer_ref=application.customer_ref,
            catalog_version=product.catalog_version,
            document_id=product.risk_document_id,
            document_version=product.risk_document_version,
            document_digest=product.risk_document_digest,
            terms_digest=product.terms_digest,
            partner_id=offer.channel.partner_id,
        )

    def _still_current(self, application: DepotApplication, handoff: Handoff) -> bool:
        """Is the action the customer approved still exactly what the live catalog offers?

        Approvals given before Phase 04 did not cover terms and partner; they are compared on
        the fields they did cover (their digest was computed without them)."""
        try:
            current = self._approved_action(application)
        except UnverifiedReferenceError:
            return False  # expired manifest, withdrawn product, ended partnership ...
        if handoff.action.terms_digest is None and handoff.action.partner_id is None:
            current = current.model_copy(update={"terms_digest": None, "partner_id": None})
        return hmac.compare_digest(current.digest(), handoff.action_digest)

    # --- 1. agent requests a customer handoff ------------------------------------------------

    def request_handoff(
        self, *, principal: AgentPrincipal, application_id: str, correlation_id: str
    ) -> HandoffIssue:
        """Create the one-time, expiring, action-bound handoff. The agent only *requests* it."""
        self._apps.require_scope(principal, MandateScope.REQUEST_HANDOFF, correlation_id)
        for attempt in (1, 2):
            try:
                return self._request_handoff_once(principal, application_id, correlation_id)
            except IntegrityError:
                # A concurrent request created an active handoff at the same moment (partial
                # unique index). Our transaction was rolled back; retry once against fresh data.
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def _request_handoff_once(
        self, principal: AgentPrincipal, application_id: str, correlation_id: str
    ) -> HandoffIssue:
        now = self._clock()
        actor_id = principal.agent_actor_id
        with self._uow_factory() as uow:
            application = self._apps.load_authorized(uow, principal, application_id, correlation_id)
            mandate = uow.mandates.get_for_application(application_id)
            decision = evaluate_handoff_request(application, mandate, actor_id, now)
            if decision.allowed and not _grant_allows(
                mandate, principal, MandateScope.REQUEST_HANDOFF, now
            ):
                decision = PolicyDecision(("mandate_inactive",))
            if not decision.allowed:
                error: AppError = (
                    MandateInactiveError()
                    if "mandate_inactive" in decision.missing
                    else InvalidStateError()
                )
                raise self._deny(
                    uow,
                    error,
                    event_type="handoff.request_denied",
                    actor_type=AuditActorType.AGENT,
                    actor_id=actor_id,
                    application_id=application_id,
                    correlation_id=correlation_id,
                    details={"missing": list(decision.missing)},
                )

            action = self._approved_action(application)
            nonce = self._nonce_factory()
            handoff = Handoff(
                handoff_id=f"hnd-{self._id_factory()}",
                application_id=application_id,
                nonce_hash=_hash_nonce(nonce),
                action=action,
                action_digest=action.digest(),
                expires_at=now + self._handoff_ttl,
                created_at=now,
            )
            superseded = uow.handoffs.revoke_active(application_id, now)
            uow.handoffs.add(handoff)
            if application.status is S.DRAFT:
                self._apps.apply_transition(
                    uow,
                    application,
                    S.AWAITING_AUTHORIZATION,
                    authority=A.SYSTEM_POLICY,
                    reason_code="handoff_created",
                    correlation_id=correlation_id,
                )
            uow.audit.append(
                self._apps.new_event(
                    event_type="handoff.created",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.AGENT,
                    actor_id=actor_id,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=application_id,
                    details={
                        "handoff_id": handoff.handoff_id,
                        "action_digest": handoff.action_digest,
                        "expires_at": handoff.expires_at.isoformat(),
                        "superseded_previous": superseded,
                    },
                )
            )
            uow.commit()
            return HandoffIssue(handoff, nonce)

    # --- challenge verification shared by the review page and the confirmation ---------------

    def _verified_handoff(
        self, uow: UnitOfWork, handoff_id: str, nonce: str, customer_id: str, correlation_id: str
    ) -> Handoff:
        """Return the handoff only if the challenge is right and it is still usable.

        Order matters: the nonce is checked first, so a caller who does not hold it learns
        nothing about the handoff's state.
        """
        now = self._clock()
        handoff = uow.handoffs.get(handoff_id)
        if handoff is None:
            raise HandoffInvalidError()

        def deny(error: AppError, reason: str) -> AppError:
            return self._deny(
                uow,
                error,
                event_type="handoff.rejected",
                actor_type=AuditActorType.CUSTOMER,
                actor_id=customer_id,
                application_id=handoff.application_id,
                correlation_id=correlation_id,
                details={"handoff_id": handoff_id, "reason": reason},
            )

        if not hmac.compare_digest(_hash_nonce(nonce), handoff.nonce_hash):
            raise deny(HandoffInvalidError(), "invalid_challenge")
        if handoff.consumed_at is not None or handoff.revoked_at is not None:
            raise deny(HandoffNotActiveError(), "consumed_or_superseded")
        if now >= handoff.expires_at:
            raise deny(HandoffExpiredError(), "expired")
        return handoff

    def record_customer_channel_denial(
        self,
        *,
        handoff_id: str,
        actor_type: AuditActorType,
        actor_id: str | None,
        reason: str,
        correlation_id: str,
    ) -> None:
        """Audit a refusal on the customer channel that never reached the ceremony itself
        (e.g. an agent credential presented, a failed CSRF check). Unknown handoff: no-op."""
        with self._uow_factory() as uow:
            handoff = uow.handoffs.get(handoff_id)
            if handoff is None:
                return
            uow.audit.append(
                self._apps.new_event(
                    event_type="handoff.rejected",
                    correlation_id=correlation_id,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    outcome=AuditOutcome.DENIED,
                    application_id=handoff.application_id,
                    details={"handoff_id": handoff_id, "reason": reason},
                )
            )
            uow.commit()

    # --- 2a. customer reviews (read-only) ----------------------------------------------------

    def review_handoff(
        self, *, customer_id: str, handoff_id: str, nonce: str, correlation_id: str
    ) -> HandoffReview:
        with self._uow_factory() as uow:
            handoff = self._verified_handoff(uow, handoff_id, nonce, customer_id, correlation_id)
            application = uow.applications.get(handoff.application_id)
            if application is None:  # pragma: no cover - FK guarantees it
                raise HandoffInvalidError()
            if application.customer_ref != customer_id:
                raise self._deny(
                    uow,
                    CustomerMismatchError(),
                    event_type="handoff.rejected",
                    actor_type=AuditActorType.CUSTOMER,
                    actor_id=customer_id,
                    application_id=application.application_id,
                    correlation_id=correlation_id,
                    details={"handoff_id": handoff_id, "reason": "customer_mismatch"},
                )
            text = self._catalog.get_document_text(
                handoff.action.document_id, handoff.action.document_version
            )
            return HandoffReview(handoff, application, text)

    # --- 2b. customer confirms (the bank-controlled ceremony) --------------------------------

    def confirm_handoff(
        self,
        *,
        customer_id: str,
        handoff_id: str,
        nonce: str,
        action_digest: str,
        correlation_id: str,
    ) -> DepotApplication:
        """Bind the authenticated customer to this application and this exact action.

        Fails closed - and changes nothing - unless the challenge, the customer identity, the
        presented digest, the live catalog and the mandate all agree. Success is atomic:
        handoff consumed + receipts + AWAITING_AUTHORIZATION -> AUTHORIZED. The deterministic
        checks then run (`advance_checks`).
        """
        now = self._clock()
        with self._uow_factory() as uow:
            handoff = self._verified_handoff(uow, handoff_id, nonce, customer_id, correlation_id)
            application = uow.applications.get(handoff.application_id)
            if application is None:  # pragma: no cover - FK guarantees it
                raise HandoffInvalidError()

            def deny(error: AppError, reason: str) -> AppError:
                return self._deny(
                    uow,
                    error,
                    event_type="handoff.rejected",
                    actor_type=AuditActorType.CUSTOMER,
                    actor_id=customer_id,
                    application_id=application.application_id,
                    correlation_id=correlation_id,
                    details={"handoff_id": handoff_id, "reason": reason},
                )

            if application.customer_ref != customer_id:
                raise deny(CustomerMismatchError(), "customer_mismatch")
            if application.status is not S.AWAITING_AUTHORIZATION:
                raise deny(InvalidStateError(), "state_not_awaiting_authorization")
            mandate = uow.mandates.get_for_application(application.application_id)
            if not mandate_allows(
                mandate, MandateScope.REQUEST_HANDOFF, application.agent_actor_id, now
            ):
                raise deny(MandateInactiveError(), "mandate_inactive")
            if not hmac.compare_digest(action_digest, handoff.action_digest):
                raise deny(ActionDigestMismatchError(), "presented_digest_differs")
            # The catalog may have changed since the handoff was issued (terms, disclosure
            # version, channel/partner, manifest validity): re-derive the action from the live
            # catalog and refuse if the customer would approve something no longer current.
            try:
                offer: VerifiedOffer | None = self._offer(application)
            except UnverifiedReferenceError:
                offer = None
            if (
                offer is None
                or self._approved_action(application, offer).digest() != handoff.action_digest
            ):
                raise deny(ActionDigestMismatchError(), "action_no_longer_current")
            if not uow.handoffs.consume(handoff_id, customer_id, now):
                raise deny(HandoffNotActiveError(), "lost_race_or_reused")

            action = handoff.action
            # Disclosure evidence bound to application, customer, provider, product (version
            # and terms), the exact document version and the approved action digest.
            uow.receipts.add_disclosure(
                DisclosureReceipt(
                    receipt_id=f"dsc-{self._id_factory()}",
                    application_id=application.application_id,
                    customer_id=customer_id,
                    document_id=action.document_id,
                    document_version=action.document_version,
                    document_digest=action.document_digest,
                    acknowledged_at=now,
                    provider_id=action.provider_id,
                    product_id=action.product_id,
                    product_version=action.catalog_version,
                    terms_digest=action.terms_digest,
                    action_digest=handoff.action_digest,
                )
            )
            consent = ConsentReceipt(
                receipt_id=f"cns-{self._id_factory()}",
                application_id=application.application_id,
                customer_id=customer_id,
                handoff_id=handoff_id,
                action_digest=handoff.action_digest,
                confirmed_at=now,
            )
            uow.receipts.add_consent(consent)
            # The verified distribution attribution, from the registry, fixed with the consent.
            uow.attributions.add(
                ChannelAttribution(
                    application_id=application.application_id,
                    provider_id=application.provider_id,
                    channel_id=offer.channel.channel_id,
                    channel_kind=offer.channel.kind,
                    partner_id=offer.channel.partner_id,
                    registry_version=offer.provider.manifest_digest,
                    consent_receipt_id=consent.receipt_id,
                    recorded_at=now,
                )
            )
            self._apps.apply_transition(
                uow,
                application,
                S.AUTHORIZED,
                authority=A.CUSTOMER_CHANNEL,
                reason_code="customer_confirmed",
                correlation_id=correlation_id,
                actor_id=customer_id,
            )
            uow.audit.append(
                self._apps.new_event(
                    event_type="handoff.confirmed",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.CUSTOMER,
                    actor_id=customer_id,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=application.application_id,
                    details={
                        "handoff_id": handoff_id,
                        "action_digest": handoff.action_digest,
                        "document_version": action.document_version,
                        "terms_digest": action.terms_digest,
                        "channel_kind": offer.channel.kind.value,
                        "partner_id": offer.channel.partner_id,
                    },
                )
            )
            uow.commit()
        return self.advance_checks(application.application_id, correlation_id)

    # --- 3. deterministic bank-side checks (MOCK providers) ----------------------------------

    def advance_checks(self, application_id: str, correlation_id: str) -> DepotApplication:
        """Run the pending mandatory steps until the workflow is ready or parked. Idempotent.

        AUTHORIZED -> KYC_PENDING -> COMPLIANCE_PENDING -> CONTRACT_PENDING (contract recorded).
        Each step is its own committed unit of work with its evidence, so an interruption
        leaves a well-defined state and re-running resumes where it stopped. Any outcome other
        than PASS parks the application in MANUAL_REVIEW (fail closed); it never advances.

        Bounded (a fixed number of steps) and safe to run concurrently: a provider is only
        called when no evidence for that check is recorded yet, and if another worker records
        the evidence or takes the transition first, this run rolls back and re-reads.
        """
        for _ in range(12):
            try:
                with self._uow_factory() as uow:
                    application = uow.applications.get(application_id)
                    if application is None:
                        raise NotFoundError()
                    if application.status is S.AUTHORIZED:
                        self._step(uow, application, S.KYC_PENDING, "kyc_started", correlation_id)
                    elif application.status is S.KYC_PENDING:
                        self._run_check(uow, application, CheckType.KYC, correlation_id)
                    elif application.status is S.COMPLIANCE_PENDING:
                        self._run_check(uow, application, CheckType.COMPLIANCE, correlation_id)
                    elif application.status is S.CONTRACT_PENDING:
                        self._record_contract(uow, application, correlation_id)
                        return uow.applications.get(application_id) or application
                    else:
                        return application
            except (IntegrityError, ConcurrentModificationError):
                continue  # lost a race with a concurrent run: re-read the committed state
        raise RuntimeError("advance_checks did not settle")  # pragma: no cover

    def _step(
        self, uow: UnitOfWork, application: DepotApplication, target: S, reason: str, cid: str
    ) -> None:
        self._apps.apply_transition(
            uow,
            application,
            target,
            authority=A.SYSTEM_POLICY,
            reason_code=reason,
            correlation_id=cid,
        )
        uow.commit()

    @staticmethod
    def _authorized_re_evaluation(
        uow: UnitOfWork, application_id: str, check: CheckType, latest: CheckRecord
    ) -> tuple[ReviewEvidenceCategory, ...] | None:
        """Evidence for a new attempt, if an operator closed the review of exactly the latest
        attempt with RE_EVALUATE; otherwise None (the recorded result stands)."""
        for case in uow.review_cases.list_for_application(application_id):
            if (
                case.check_type is check
                and case.check_attempt == latest.attempt
                and case.resolution is ReviewDecision.RE_EVALUATE
            ):
                return tuple(item.category for item in case.evidence)
        return None

    def _run_check(
        self, uow: UnitOfWork, application: DepotApplication, check: CheckType, cid: str
    ) -> None:
        """Run (or reuse) one mandatory check, then advance or park the application.

        A provider is called only if no result is recorded yet, or if an operator authorized a
        re-evaluation of the latest attempt (Phase 04): then attempt n+1 is called WITH the
        recorded review evidence and its result decides - nobody sets an outcome by hand.
        """
        app_id = application.application_id
        record = uow.checks.get(app_id, check)
        evidence = (
            None if record is None else self._authorized_re_evaluation(uow, app_id, check, record)
        )
        if record is None or evidence is not None:
            if evidence is None:  # first attempt: the original call, without review evidence
                result = (
                    self._kyc.verify_identity(application)
                    if check is CheckType.KYC
                    else self._compliance.assess(application)
                )
            else:
                result = (
                    self._kyc.verify_identity(application, evidence=evidence)
                    if check is CheckType.KYC
                    else self._compliance.assess(application, evidence=evidence)
                )
            record = CheckRecord(
                application_id=app_id,
                check_type=check,
                outcome=result.outcome,
                reference=result.reference,
                mock=result.mock,
                created_at=self._clock(),
                attempt=1 if record is None else record.attempt + 1,
            )
            uow.checks.add(record)  # UNIQUE(application, check, attempt): no double attempt
            if record.attempt > 1:
                uow.audit.append(
                    self._apps.new_event(
                        event_type="check.re_evaluated",
                        correlation_id=cid,
                        actor_type=AuditActorType.SYSTEM,
                        actor_id=None,
                        outcome=AuditOutcome.SUCCESS,
                        application_id=app_id,
                        details={"check_type": check.value, "attempt": record.attempt},
                    )
                )
        if record.outcome is CheckOutcome.PASS:
            target = S.COMPLIANCE_PENDING if check is CheckType.KYC else S.CONTRACT_PENDING
            reason = f"{check.value.lower()}_passed"
        else:
            target, reason = S.MANUAL_REVIEW, f"{check.value.lower()}_not_passed"
            self._open_check_review(uow, application, check, record, cid)
        self._step(uow, application, target, reason, cid)

    def _open_check_review(
        self,
        uow: UnitOfWork,
        application: DepotApplication,
        check: CheckType,
        record: CheckRecord,
        cid: str,
    ) -> None:
        """A check that did not pass parks the application WITH a durable review case.

        One active case per application: if the case for exactly this attempt is still open
        (the step was re-entered without a review decision), it is kept. Any other open case
        cannot hold this pause (its interrupted step is fixed), so it is closed as superseded
        - recorded in the audit trail - before the new case is opened.
        """
        app_id = application.application_id
        active = uow.review_cases.get_active_for_application(app_id)
        if active is not None:
            if (
                active.check_type is check
                and active.check_attempt == record.attempt
                and active.interrupted_step is application.status
            ):
                return
            now = self._clock()
            superseded = active.model_copy(
                update={
                    "status": ReviewCaseStatus.CLOSED,
                    "resolution": ReviewDecision.ACKNOWLEDGE,
                    "resolved_by": "system",
                    "resolved_at": now,
                }
            )
            if not uow.review_cases.update(superseded, expected_version=active.version, at=now):
                raise ConcurrentModificationError()
            uow.audit.append(
                self._apps.new_event(
                    event_type="review.case_superseded",
                    correlation_id=cid,
                    actor_type=AuditActorType.SYSTEM,
                    actor_id=None,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=app_id,
                    details={"case_id": active.case_id, "reason": active.reason.value},
                )
            )
        case = ManualReviewCase(
            case_id=f"rvw-{self._id_factory()}",
            application_id=application.application_id,
            reason=check_case_reason(check, record.outcome),
            check_type=check,
            check_attempt=record.attempt,
            interrupted_step=application.status,
            opened_by=AuditActorType.SYSTEM,
            created_at=self._clock(),
        )
        uow.review_cases.add(case)  # at most one active case per application (DB-enforced)
        uow.audit.append(
            self._apps.new_event(
                event_type="review.case_opened",
                correlation_id=cid,
                actor_type=AuditActorType.SYSTEM,
                actor_id=None,
                outcome=AuditOutcome.SUCCESS,
                application_id=application.application_id,
                details={
                    "case_id": case.case_id,
                    "reason": case.reason.value,
                    "check_type": check.value,
                    "attempt": record.attempt,
                    "interrupted_step": application.status.value,
                },
            )
        )

    def _record_contract(self, uow: UnitOfWork, application: DepotApplication, cid: str) -> None:
        if uow.checks.get(application.application_id, CheckType.CONTRACT) is not None:
            return
        consent = uow.receipts.get_consent(application.application_id)
        if consent is None:  # no customer confirmation: nothing to record, and submit stays blocked
            return
        signing = self._contract.record_signature(consent)
        uow.checks.add(
            CheckRecord(
                application_id=application.application_id,
                check_type=CheckType.CONTRACT,
                outcome=CheckOutcome.PASS if signing.signed else CheckOutcome.FAIL,
                reference=signing.contract_ref,
                mock=signing.mock,
                created_at=self._clock(),
            )
        )
        uow.audit.append(
            self._apps.new_event(
                event_type="contract.confirmation_recorded",
                correlation_id=cid,
                actor_type=AuditActorType.SYSTEM,
                actor_id=None,
                outcome=AuditOutcome.SUCCESS if signing.signed else AuditOutcome.FAILURE,
                application_id=application.application_id,
                details={"contract_ref": signing.contract_ref, "mock": signing.mock},
            )
        )
        uow.commit()

    # --- 4. agent submits after consent ------------------------------------------------------

    def _resume_checks(self, application_id: str, resume_from: S, correlation_id: str) -> None:
        """Run the unfinished mandatory checks once more. Fail closed: if a provider is still
        unavailable the attempt is audited and the policy gate (no evidence) refuses submit."""
        outcome = AuditOutcome.SUCCESS
        try:
            self.advance_checks(application_id, correlation_id)
        except AppError:
            raise
        except Exception:  # an external provider failed: leave the durable state as it is
            logger.warning(
                "resuming mandatory checks failed", exc_info=True, extra={"app": application_id}
            )
            outcome = AuditOutcome.FAILURE
        with self._uow_factory() as uow:
            uow.audit.append(
                self._apps.new_event(
                    event_type="workflow.resumed",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.SYSTEM,
                    actor_id=None,
                    outcome=outcome,
                    application_id=application_id,
                    details={"from": resume_from.value, "trigger": "submit"},
                )
            )
            uow.commit()

    def _pending_resume(
        self, principal: AgentPrincipal, application_id: str, correlation_id: str
    ) -> S | None:
        """The step to resume from, if the customer already approved but a crash left the
        mandatory checks unfinished. None if there is nothing (allowed) to resume."""
        now = self._clock()
        with self._uow_factory() as uow:
            application = self._apps.load_authorized(uow, principal, application_id, correlation_id)
            mandate = uow.mandates.get_for_application(application_id)
            if not _grant_allows(mandate, principal, MandateScope.SUBMIT_AFTER_CONSENT, now):
                return None  # the main path refuses (and audits) this request
            if uow.receipts.get_consent(application_id) is None:
                return None  # no stored customer approval: nothing may run on the agent's behalf
            if application.status in _RESUMABLE_CHECK_STATES or (
                application.status is S.CONTRACT_PENDING
                and uow.checks.get(application_id, CheckType.CONTRACT) is None
            ):
                return application.status
            return None

    def submit_application(
        self, *, principal: AgentPrincipal, application_id: str, correlation_id: str
    ) -> SubmitResult:
        """Hand the application to the depot provider - only if the policy gate allows it.

        Recovery: if the process stopped after the customer's confirmation but before the
        mandatory checks finished, they are resumed first (`advance_checks`: bounded,
        idempotent, recorded evidence is reused, never re-requested). The policy gate then
        decides on the evidence as usual; a check that does not pass still blocks submission.
        """
        self._apps.require_scope(principal, MandateScope.SUBMIT_AFTER_CONSENT, correlation_id)
        resume_from = self._pending_resume(principal, application_id, correlation_id)
        if resume_from is not None:
            self._resume_checks(application_id, resume_from, correlation_id)

        now = self._clock()
        actor_id = principal.agent_actor_id
        with self._uow_factory() as uow:
            application = self._apps.load_authorized(uow, principal, application_id, correlation_id)
            mandate = uow.mandates.get_for_application(application_id)
            if not _grant_allows(mandate, principal, MandateScope.SUBMIT_AFTER_CONSENT, now):
                raise self._deny(
                    uow,
                    MandateInactiveError(),
                    event_type="application.submit_denied",
                    actor_type=AuditActorType.AGENT,
                    actor_id=actor_id,
                    application_id=application_id,
                    correlation_id=correlation_id,
                    details={"missing": ["mandate_inactive"]},
                )
            if application.status is S.SUBMITTED_TO_DEPOT_PROVIDER:
                return SubmitResult(application, replayed=True)  # duplicate submit: no side effect

            consent = uow.receipts.get_consent(application_id)
            handoff = uow.handoffs.get(consent.handoff_id) if consent else None
            decision = evaluate_submit(
                application=application,
                mandate=mandate,
                actor_id=actor_id,
                now=now,
                handoff=handoff,
                consent=consent,
                disclosure=uow.receipts.get_disclosure(application_id),
                kyc=uow.checks.get(application_id, CheckType.KYC),
                compliance=uow.checks.get(application_id, CheckType.COMPLIANCE),
                contract=uow.checks.get(application_id, CheckType.CONTRACT),
            )
            if decision.allowed and (
                handoff is None or not self._still_current(application, handoff)
            ):
                # Phase 04: the approval covers exact terms, disclosure and attribution. If the
                # live catalog no longer offers exactly that (changed terms, expired manifest,
                # ended partnership), the old approval cannot be used to submit.
                decision = PolicyDecision(("approved_action_current",))
            if not decision.allowed:
                # The precise reason is audited; the agent only gets a generic refusal.
                raise self._deny(
                    uow,
                    PrerequisitesNotMetError(),
                    event_type="application.submit_denied",
                    actor_type=AuditActorType.AGENT,
                    actor_id=actor_id,
                    application_id=application_id,
                    correlation_id=correlation_id,
                    details={"missing": list(decision.missing)},
                )

            submission = self._depot.submit_application(application)  # idempotent per application
            if not submission.accepted:
                raise self._deny(
                    uow,
                    ProviderUnavailableError(),
                    event_type="application.submit_denied",
                    actor_type=AuditActorType.SYSTEM,
                    actor_id=None,
                    application_id=application_id,
                    correlation_id=correlation_id,
                    details={"missing": ["provider_accepted"]},
                )
            try:
                uow.depot_submissions.add(
                    DepotSubmissionRecord(
                        application_id=application_id,
                        provider_ref=submission.provider_ref,
                        submitted_at=now,
                    )
                )
                updated = self._apps.apply_transition(
                    uow,
                    application,
                    S.SUBMITTED_TO_DEPOT_PROVIDER,
                    authority=A.SYSTEM_POLICY,
                    reason_code="submitted_after_consent",
                    correlation_id=correlation_id,
                    actor_id=actor_id,
                )
                uow.audit.append(
                    self._apps.new_event(
                        event_type="application.submitted",
                        correlation_id=correlation_id,
                        actor_type=AuditActorType.AGENT,
                        actor_id=actor_id,
                        outcome=AuditOutcome.SUCCESS,
                        application_id=application_id,
                        details={"provider_ref": submission.provider_ref, "mock": submission.mock},
                    )
                )
                uow.commit()
                return SubmitResult(updated, replayed=False)
            except (IntegrityError, ConcurrentModificationError):
                # A concurrent duplicate submit won. Our writes were rolled back; report its result.
                uow.rollback()
                current = uow.applications.get(application_id)
                if current is not None and current.status is S.SUBMITTED_TO_DEPOT_PROVIDER:
                    return SubmitResult(current, replayed=True)
                raise

    # --- 5. trusted depot-provider confirmation -> OPENED ------------------------------------

    def handle_provider_confirmation(
        self, confirmation: ProviderConfirmation, correlation_id: str
    ) -> ProviderConfirmationResult:
        """Accept the provider's (already signature-verified) confirmation.

        The ONLY path to OPENED. It must match the application AND the provider reference we
        recorded at submission, arrive while the application is SUBMITTED_TO_DEPOT_PROVIDER, and
        carry a confirmation id not seen before. An exact repeat is acknowledged without any
        further effect.
        """
        now = self._clock()
        app_id = confirmation.application_id
        with self._uow_factory() as uow:

            def reject(reason: str, error: AppError | None = None) -> AppError:
                return self._deny(
                    uow,
                    error or ConfirmationRejectedError(),
                    event_type="provider.confirmation_rejected",
                    actor_type=AuditActorType.PROVIDER,
                    actor_id=None,
                    application_id=app_id,
                    correlation_id=correlation_id,
                    details={"reason": reason},
                )

            previous = uow.depot_submissions.get_by_confirmation_id(confirmation.confirmation_id)
            if previous is not None:
                if previous.application_id != app_id:
                    raise reject("confirmation_id_reused_for_other_application")
                duplicate_of = uow.applications.get(app_id)
                if duplicate_of is None:  # pragma: no cover - FK guarantees it
                    raise NotFoundError()
                return ProviderConfirmationResult(duplicate_of, duplicate=True)

            application = uow.applications.get(app_id)
            if application is None:
                raise NotFoundError()
            submission = uow.depot_submissions.get(app_id)
            if submission is None or not hmac.compare_digest(
                submission.provider_ref, confirmation.provider_ref
            ):
                raise reject("unknown_submission_or_provider_ref")
            try:
                check_transition(
                    application.status,
                    S.OPENED,
                    A.DEPOT_PROVIDER_CONFIRMATION,
                    application.resume_status,
                )
            except (InvalidTransitionError, TransitionNotAuthorizedError):
                raise reject("application_not_awaiting_confirmation") from None
            if not uow.depot_submissions.confirm(app_id, confirmation.confirmation_id, now):
                # Lost a race with an identical concurrent confirmation, or already confirmed.
                uow.rollback()
                again = uow.depot_submissions.get_by_confirmation_id(confirmation.confirmation_id)
                current = uow.applications.get(app_id)
                if again is not None and current is not None:
                    return ProviderConfirmationResult(current, duplicate=True)
                raise reject("already_confirmed")

            opened = self._apps.apply_transition(
                uow,
                application,
                S.OPENED,
                authority=A.DEPOT_PROVIDER_CONFIRMATION,
                reason_code="provider_confirmed_opening",
                correlation_id=correlation_id,
            )
            uow.audit.append(
                self._apps.new_event(
                    event_type="provider.confirmation_accepted",
                    correlation_id=correlation_id,
                    actor_type=AuditActorType.PROVIDER,
                    actor_id=None,
                    outcome=AuditOutcome.SUCCESS,
                    application_id=app_id,
                    details={
                        "confirmation_id": confirmation.confirmation_id,
                        "provider_ref": confirmation.provider_ref,
                        "mock": True,
                    },
                )
            )
            uow.commit()
            return ProviderConfirmationResult(opened, duplicate=False)
