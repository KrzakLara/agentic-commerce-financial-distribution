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
)
from app.domain.handoff import ApprovedAction, Handoff
from app.domain.mandate import Mandate
from app.domain.receipts import ConsentReceipt, DisclosureReceipt
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

    def _approved_action(self, application: DepotApplication) -> ApprovedAction:
        """Build the action from the VERIFIED catalog (never from agent-supplied text)."""
        try:
            products = {
                p.product_id: p for p in self._catalog.list_products(application.provider_id)
            }
            channels = {c.channel_id for c in self._catalog.list_channels(application.provider_id)}
        except NotFoundError:
            raise UnverifiedReferenceError() from None
        product = products.get(application.product_id)
        if product is None or application.channel_id not in channels:
            raise UnverifiedReferenceError()
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
        )

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
            # The catalog may have changed since the handoff was issued: re-derive the action
            # and refuse if the customer would approve something that is no longer current.
            if self._approved_action(application).digest() != handoff.action_digest:
                raise deny(ActionDigestMismatchError(), "action_no_longer_current")
            if not uow.handoffs.consume(handoff_id, customer_id, now):
                raise deny(HandoffNotActiveError(), "lost_race_or_reused")

            action = handoff.action
            uow.receipts.add_disclosure(
                DisclosureReceipt(
                    receipt_id=f"dsc-{self._id_factory()}",
                    application_id=application.application_id,
                    customer_id=customer_id,
                    document_id=action.document_id,
                    document_version=action.document_version,
                    document_digest=action.document_digest,
                    acknowledged_at=now,
                )
            )
            uow.receipts.add_consent(
                ConsentReceipt(
                    receipt_id=f"cns-{self._id_factory()}",
                    application_id=application.application_id,
                    customer_id=customer_id,
                    handoff_id=handoff_id,
                    action_digest=handoff.action_digest,
                    confirmed_at=now,
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

    def _run_check(
        self, uow: UnitOfWork, application: DepotApplication, check: CheckType, cid: str
    ) -> None:
        record = uow.checks.get(application.application_id, check)
        if record is None:
            result = (
                self._kyc.verify_identity(application)
                if check is CheckType.KYC
                else self._compliance.assess(application)
            )
            record = CheckRecord(
                application_id=application.application_id,
                check_type=check,
                outcome=result.outcome,
                reference=result.reference,
                mock=result.mock,
                created_at=self._clock(),
            )
            uow.checks.add(record)
        if record.outcome is CheckOutcome.PASS:
            target = S.COMPLIANCE_PENDING if check is CheckType.KYC else S.CONTRACT_PENDING
            reason = f"{check.value.lower()}_passed"
        else:
            target, reason = S.MANUAL_REVIEW, f"{check.value.lower()}_not_passed"
        self._step(uow, application, target, reason, cid)

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
