"""Human review (Phase 04): the customer's request for a person and the operator workflow.

One state machine: a review parks the application in the existing MANUAL_REVIEW state, which
remembers the interrupted step (`resume_status`). Leaving MANUAL_REVIEW is only possible
towards that step (OPERATOR) or to REJECTED (OPERATOR); OPENED stays reachable only through
the depot provider's signed callback. A human therefore cannot bypass KYC, AML, consent,
signing or the provider:

* operators choose from a fixed list of decisions (`ReviewDecision`) - there is no "approve";
* RE_EVALUATE re-runs the interrupted check as a NEW attempt with the recorded, permitted
  evidence; the deterministic (MOCK) provider result decides, exactly as the first time;
* RESUME (customer-requested pauses only) re-enters the interrupted step; recorded results are
  reused and pending checks run normally;
* REJECT is a business decision of a SUPERVISOR; ACKNOWLEDGE closes a case that did not pause.

What leaves this module towards the customer is a generic acknowledgement - the same whether a
case is new, already open, or opened by compliance (no tipping-off). The agent only ever sees
the coarse state ACTION_REQUIRED.
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import (
    AppError,
    ConcurrentModificationError,
    InvalidStateError,
    NotFoundError,
    PolicyDeniedError,
)
from app.domain.application import DepotApplication
from app.domain.attribution import ChannelAttribution
from app.domain.audit import AuditEvent
from app.domain.checks import CheckRecord
from app.domain.enums import (
    ApplicationStatus as S,
)
from app.domain.enums import (
    AuditActorType,
    AuditOutcome,
    HumanRequestCategory,
    OperatorRole,
    ReviewCaseReason,
    ReviewCaseStatus,
    ReviewDecision,
    ReviewEvidenceCategory,
)
from app.domain.review import ManualReviewCase, ReviewEvidence
from app.domain.workflow import TERMINAL_STATES
from app.domain.workflow import TransitionAuthority as A
from app.repositories.interfaces import UnitOfWork
from app.services.application_service import ApplicationService

# Steps a customer request may pause. Not after submission (the depot provider decides), not
# while already parked, not on a terminal application: then the case is opened WITHOUT pausing.
PAUSABLE_STEPS = frozenset(
    {
        S.DRAFT,
        S.AWAITING_AUTHORIZATION,
        S.AUTHORIZED,
        S.KYC_PENDING,
        S.COMPLIANCE_PENDING,
        S.CONTRACT_PENDING,
    }
)
EVIDENCE_REFERENCE = re.compile(r"^SYN-[A-Z0-9][A-Z0-9\-]{2,40}$")  # synthetic store refs only
MAX_EVIDENCE_ITEMS = 10
HUMAN_REQUEST_MESSAGE = "Your request to speak with a person has been recorded."

# DEV/TEST synthetic operator directory. Operators are bank staff: they are never agents,
# customers or providers, hold no agent token and never call the agent API.
DEV_OPERATORS: dict[str, OperatorRole] = {
    "op-reviewer-1": OperatorRole.REVIEWER,
    "op-supervisor-1": OperatorRole.SUPERVISOR,
}


@dataclass(frozen=True)
class OperatorPrincipal:
    operator_id: str
    role: OperatorRole


def resolve_dev_operator(operator_id: str) -> OperatorPrincipal | None:
    """The synthetic operator with this id, or None (unknown ids, tokens, agents, customers)."""
    role = DEV_OPERATORS.get(operator_id)
    return OperatorPrincipal(operator_id, role) if role else None


class OperatorNotPermittedError(PolicyDeniedError):
    code = "operator_not_permitted"

    @classmethod
    def default_message(cls) -> str:
        return "This review action is not permitted."


@dataclass(frozen=True)
class HumanRequestAck:
    """The customer's (generic) acknowledgement. Deliberately carries no case detail."""

    application_id: str
    message: str = HUMAN_REQUEST_MESSAGE


@dataclass(frozen=True)
class CaseDetails:
    """Safe synthetic context for an operator (internal: never shown to agent or customer)."""

    case: ManualReviewCase
    application: DepotApplication
    checks: list[CheckRecord]
    consent_recorded: bool
    disclosure_recorded: bool
    attribution: ChannelAttribution | None
    audit: list[AuditEvent]
    allowed_decisions: frozenset[ReviewDecision]


def allowed_decisions(
    case: ManualReviewCase, application: DepotApplication
) -> frozenset[ReviewDecision]:
    """The deterministic decision policy. Nothing here can lead to OPENED."""
    if not case.active:
        return frozenset()
    holds_workflow = application.status is S.MANUAL_REVIEW and case.interrupted_step is not None
    if not holds_workflow:
        return frozenset({ReviewDecision.ACKNOWLEDGE})
    if case.reason is ReviewCaseReason.CUSTOMER_REQUESTED_HUMAN:
        return frozenset({ReviewDecision.RESUME})
    decisions = {ReviewDecision.REJECT}
    if case.check_attempt is not None and case.status is not ReviewCaseStatus.AWAITING_INFORMATION:
        decisions.add(ReviewDecision.RE_EVALUATE)
    return frozenset(decisions)


class ReviewService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        applications: ApplicationService,
        *,
        advance_checks: Callable[[str, str], DepotApplication] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._uow_factory = uow_factory
        self._apps = applications
        self._advance_checks = advance_checks
        self._clock = clock
        self._id_factory = id_factory

    def _event(
        self,
        event_type: str,
        *,
        actor_type: AuditActorType,
        actor_id: str | None,
        application_id: str | None,
        correlation_id: str,
        details: dict[str, object],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
    ) -> AuditEvent:
        return self._apps.new_event(
            event_type=event_type,
            correlation_id=correlation_id,
            actor_type=actor_type,
            actor_id=actor_id,
            outcome=outcome,
            application_id=application_id,
            details=details,
        )

    # --- the customer asks for a person (bank-controlled customer channel) -----------------

    def record_request_denial(
        self,
        *,
        application_id: str,
        actor_type: AuditActorType,
        actor_id: str | None,
        reason: str,
        correlation_id: str,
    ) -> None:
        """Audit a refused request-human attempt BEFORE the workflow is reached (agent
        credential, cross-site request, no active customer session, wrong CSRF token).
        The reason is a machine code; nothing about review cases or checks is recorded."""
        with self._uow_factory() as uow:
            known = uow.applications.get(application_id) is not None
            uow.audit.append(
                self._event(
                    "review.human_request_denied",
                    actor_type=actor_type,
                    actor_id=actor_id,
                    application_id=application_id if known else None,
                    correlation_id=correlation_id,
                    details={"reason": reason},
                    outcome=AuditOutcome.DENIED,
                )
            )
            uow.commit()

    def record_agent_attempt(
        self, *, application_id: str, agent_id: str | None, correlation_id: str
    ) -> None:
        """Audit an agent credential presented to the request-human route (always refused)."""
        self.record_request_denial(
            application_id=application_id,
            actor_type=AuditActorType.AGENT,
            actor_id=agent_id,
            reason="agent_credentials_presented",
            correlation_id=correlation_id,
        )

    def request_human(
        self,
        *,
        customer_id: str,
        application_id: str,
        category: HumanRequestCategory,
        correlation_id: str,
    ) -> HumanRequestAck:
        """Open (or join) the application's review case and, if possible, pause the workflow.

        Only the application's own customer may ask (anyone else: 404, audited). The answer is
        identical in every successful case, so it reveals nothing about existing cases.
        """
        for attempt in (1, 2):
            try:
                return self._request_human_once(
                    customer_id, application_id, category, correlation_id
                )
            except (IntegrityError, ConcurrentModificationError):
                # A concurrent request or check opened a case / moved the application first.
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def _request_human_once(
        self,
        customer_id: str,
        application_id: str,
        category: HumanRequestCategory,
        correlation_id: str,
    ) -> HumanRequestAck:
        now = self._clock()
        with self._uow_factory() as uow:
            application = uow.applications.get(application_id)
            if application is None or application.customer_ref != customer_id:
                uow.audit.append(
                    self._event(
                        "review.human_request_denied",
                        actor_type=AuditActorType.CUSTOMER,
                        actor_id=customer_id,
                        application_id=application_id if application else None,
                        correlation_id=correlation_id,
                        details={
                            "reason": "not_the_customer" if application else "unknown_application"
                        },
                        outcome=AuditOutcome.DENIED,
                    )
                )
                uow.commit()
                raise NotFoundError()

            active = uow.review_cases.get_active_for_application(application_id)
            if active is not None:  # never a second active case: the request joins this one
                uow.audit.append(
                    self._event(
                        "review.human_requested",
                        actor_type=AuditActorType.CUSTOMER,
                        actor_id=customer_id,
                        application_id=application_id,
                        correlation_id=correlation_id,
                        details={
                            "case_id": active.case_id,
                            "category": category.value,
                            "joined_existing_case": True,
                        },
                    )
                )
                uow.commit()
                return HumanRequestAck(application_id)

            pauses = application.status in PAUSABLE_STEPS
            if pauses:
                interrupted: S | None = application.status
            elif application.status is S.MANUAL_REVIEW:  # parked without a case (pre-Phase 04)
                interrupted = application.resume_status
            else:
                interrupted = None
            case = ManualReviewCase(
                case_id=f"rvw-{self._id_factory()}",
                application_id=application_id,
                reason=ReviewCaseReason.CUSTOMER_REQUESTED_HUMAN,
                request_category=category,
                interrupted_step=interrupted,
                opened_by=AuditActorType.CUSTOMER,
                created_at=now,
            )
            uow.review_cases.add(case)
            if pauses:
                self._apps.apply_transition(
                    uow,
                    application,
                    S.MANUAL_REVIEW,
                    authority=A.CUSTOMER_CHANNEL,
                    reason_code="customer_requested_human",
                    correlation_id=correlation_id,
                    actor_id=customer_id,
                )
            uow.audit.append(
                self._event(
                    "review.case_opened",
                    actor_type=AuditActorType.CUSTOMER,
                    actor_id=customer_id,
                    application_id=application_id,
                    correlation_id=correlation_id,
                    details={
                        "case_id": case.case_id,
                        "reason": case.reason.value,
                        "category": category.value,
                        "paused_workflow": pauses,
                        "interrupted_step": interrupted.value if interrupted else None,
                    },
                )
            )
            uow.commit()
            return HumanRequestAck(application_id)

    # --- operator workflow (trusted dev CLI; no HTTP route) --------------------------------

    def list_active(self, operator: OperatorPrincipal) -> list[ManualReviewCase]:
        self._require_operator(operator)
        with self._uow_factory() as uow:
            return uow.review_cases.list_active()

    def inspect(self, operator: OperatorPrincipal, case_id: str) -> CaseDetails:
        self._require_operator(operator)
        with self._uow_factory() as uow:
            case = uow.review_cases.get(case_id)
            if case is None:
                raise NotFoundError()
            application = uow.applications.get(case.application_id)
            if application is None:  # pragma: no cover - FK guarantees it
                raise NotFoundError()
            app_id = application.application_id
            return CaseDetails(
                case=case,
                application=application,
                checks=uow.checks.list_for_application(app_id),
                consent_recorded=uow.receipts.get_consent(app_id) is not None,
                disclosure_recorded=uow.receipts.get_disclosure(app_id) is not None,
                attribution=uow.attributions.get(app_id),
                audit=uow.audit.list_for_application(app_id),
                allowed_decisions=allowed_decisions(case, application),
            )

    def request_information(
        self,
        operator: OperatorPrincipal,
        case_id: str,
        category: ReviewEvidenceCategory,
        correlation_id: str,
    ) -> ManualReviewCase:
        """Ask for one permitted kind of evidence. RE_EVALUATE waits until it is recorded."""
        return self._update_case(
            operator,
            case_id,
            correlation_id,
            "review.information_requested",
            lambda case: {
                "status": ReviewCaseStatus.AWAITING_INFORMATION,
                "requested_information": category,
            },
            {"category": category.value},
        )

    def record_evidence(
        self,
        operator: OperatorPrincipal,
        case_id: str,
        category: ReviewEvidenceCategory,
        reference: str,
        correlation_id: str,
    ) -> ManualReviewCase:
        """Record a permitted evidence item: a category and a SYNTHETIC reference, no content."""
        if not EVIDENCE_REFERENCE.fullmatch(reference):
            raise ValueError("evidence references are synthetic ids like SYN-SOF-0001")

        def change(case: ManualReviewCase) -> dict[str, object]:
            if len(case.evidence) >= MAX_EVIDENCE_ITEMS:
                raise InvalidStateError("This case already holds the maximum amount of evidence.")
            item = ReviewEvidence(
                category=category,
                reference=reference,
                recorded_by=operator.operator_id,
                recorded_at=self._clock(),
            )
            still_waiting = (
                None if case.requested_information is category else case.requested_information
            )
            return {
                # Only the requested category ends the wait for information.
                "status": ReviewCaseStatus.IN_REVIEW
                if still_waiting is None
                else ReviewCaseStatus.AWAITING_INFORMATION,
                "evidence": (*case.evidence, item),
                "requested_information": still_waiting,
            }

        return self._update_case(
            operator,
            case_id,
            correlation_id,
            "review.evidence_recorded",
            change,
            {"category": category.value, "reference": reference},
        )

    def decide(
        self,
        operator: OperatorPrincipal,
        case_id: str,
        decision: ReviewDecision,
        correlation_id: str,
    ) -> DepotApplication:
        """Close the case with one allowed decision and let the deterministic workflow continue."""
        self._require_operator(operator)
        now = self._clock()
        with self._uow_factory() as uow:
            case = uow.review_cases.get(case_id)
            if case is None:
                raise NotFoundError()
            application = uow.applications.get(case.application_id)
            if application is None:  # pragma: no cover - FK guarantees it
                raise NotFoundError()
            allowed = allowed_decisions(case, application)
            reason = None
            if decision not in allowed:
                reason = "decision_not_allowed_for_case"
            elif decision is ReviewDecision.REJECT and operator.role is not OperatorRole.SUPERVISOR:
                reason = "supervisor_required"
            elif (
                decision in (ReviewDecision.RE_EVALUATE, ReviewDecision.RESUME)
                and application.resume_status is not case.interrupted_step
            ):
                reason = "case_does_not_match_workflow"  # defensive: never resume elsewhere
            if reason is not None:
                raise self._deny(uow, operator, case, decision, reason, correlation_id)

            closed = case.model_copy(
                update={
                    "status": ReviewCaseStatus.CLOSED,
                    "resolution": decision,
                    "resolved_by": operator.operator_id,
                    "resolved_at": now,
                }
            )
            if not uow.review_cases.update(closed, expected_version=case.version, at=now):
                uow.rollback()
                raise ConcurrentModificationError()
            result = application
            if decision is ReviewDecision.REJECT:
                result = self._apps.apply_transition(
                    uow,
                    application,
                    S.REJECTED,
                    authority=A.OPERATOR,
                    reason_code="review_rejected",
                    correlation_id=correlation_id,
                    actor_id=operator.operator_id,
                )
            elif decision in (ReviewDecision.RE_EVALUATE, ReviewDecision.RESUME):
                if case.interrupted_step is None:  # pragma: no cover - allowed_decisions
                    raise InvalidStateError()
                result = self._apps.apply_transition(
                    uow,
                    application,
                    case.interrupted_step,  # the remembered step - the only way out
                    authority=A.OPERATOR,
                    reason_code=f"review_{decision.value.lower()}",
                    correlation_id=correlation_id,
                    actor_id=operator.operator_id,
                )
            uow.audit.append(
                self._event(
                    "review.decided",
                    actor_type=AuditActorType.OPERATOR,
                    actor_id=operator.operator_id,
                    application_id=application.application_id,
                    correlation_id=correlation_id,
                    details={
                        "case_id": case_id,
                        "decision": decision.value,
                        "role": operator.role.value,
                        "evidence_items": len(case.evidence),
                    },
                )
            )
            uow.commit()

        if decision in (ReviewDecision.RE_EVALUATE, ReviewDecision.RESUME) and (
            self._advance_checks is not None and result.status not in TERMINAL_STATES
        ):
            try:  # the bank's deterministic pipeline continues; a provider outage fails closed
                result = self._advance_checks(result.application_id, correlation_id)
            except AppError:
                raise
            except Exception:  # the durable state stays as it is; the next submit resumes
                with self._uow_factory() as uow:
                    current = uow.applications.get(result.application_id)
                result = current or result
        return result

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _require_operator(operator: OperatorPrincipal) -> None:
        if resolve_dev_operator(operator.operator_id) != operator:
            raise OperatorNotPermittedError()

    def _deny(
        self,
        uow: UnitOfWork,
        operator: OperatorPrincipal,
        case: ManualReviewCase,
        decision: ReviewDecision,
        reason: str,
        correlation_id: str,
    ) -> AppError:
        uow.rollback()
        uow.audit.append(
            self._event(
                "review.decision_denied",
                actor_type=AuditActorType.OPERATOR,
                actor_id=operator.operator_id,
                application_id=case.application_id,
                correlation_id=correlation_id,
                details={"case_id": case.case_id, "decision": decision.value, "reason": reason},
                outcome=AuditOutcome.DENIED,
            )
        )
        uow.commit()
        # Operators are trusted bank staff (no HTTP route raises this): name the reason.
        return OperatorNotPermittedError(f"This review action is not permitted ({reason}).")

    def _update_case(
        self,
        operator: OperatorPrincipal,
        case_id: str,
        correlation_id: str,
        event_type: str,
        change: Callable[[ManualReviewCase], dict[str, object]],
        details: dict[str, object],
    ) -> ManualReviewCase:
        self._require_operator(operator)
        now = self._clock()
        with self._uow_factory() as uow:
            case = uow.review_cases.get(case_id)
            if case is None:
                raise NotFoundError()
            if not case.active:
                raise InvalidStateError("This review case is closed.")
            updated = ManualReviewCase.model_validate(
                {**case.model_dump(), **change(case), "version": case.version + 1}
            )
            if not uow.review_cases.update(updated, expected_version=case.version, at=now):
                raise ConcurrentModificationError()
            uow.audit.append(
                self._event(
                    event_type,
                    actor_type=AuditActorType.OPERATOR,
                    actor_id=operator.operator_id,
                    application_id=case.application_id,
                    correlation_id=correlation_id,
                    details={"case_id": case_id, **details},
                )
            )
            uow.commit()
            return updated
