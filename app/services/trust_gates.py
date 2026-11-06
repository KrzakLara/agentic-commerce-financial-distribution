"""What the full-trust profile adds to the frozen onboarding workflow, at its existing policy gates.

No state and no transition is added. The workflow asks these gates at the customer's confirmation
and at the agent's submission, and they answer from recorded evidence.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.domain.application import DepotApplication
from app.domain.enums import AuditActorType, AuditOutcome
from app.repositories.interfaces import UnitOfWork
from app.repositories.trust_orm import AttributionDecisionRow, PolicyDecisionRow
from app.services.application_service import ApplicationService
from app.services.contract_execution import latest_session, package_of, signature_gate
from app.services.distribution_governance import (
    OUTCOME_PARTNER,
    OUTCOME_RESOLVED,
    DistributionGovernanceService,
)
from app.services.identity_proofing import latest_case, proofing_satisfied
from app.services.regulatory_policy import POLICY_VERSION, Service, evaluate


def policy_facts(uow: UnitOfWork, application: DepotApplication, stage: str) -> dict[str, Any]:
    """The evidence the rules read. Statuses only: never a tax value or a personal detail."""
    app_id = application.application_id
    case = latest_case(uow, app_id)
    session = latest_session(uow, app_id)
    assessment = uow.customer_information.get_assessment(app_id)
    decision = uow.trust.scalars(
        select(AttributionDecisionRow)
        .where(AttributionDecisionRow.application_id == app_id)
        .order_by(AttributionDecisionRow.sequence.desc())
    ).first()
    return {
        "stage": stage,
        "identity_proofing": case.status if case else None,
        "regulatory_evidence": bool(case and case.regulatory_evidence),
        "tax_information": uow.customer_information.get_tax(app_id) is not None,
        "assessment_outcome": assessment.outcome if assessment else None,
        "contract_signature": session.state if session else None,
        "partner_attributed": bool(
            decision
            and decision.outcome in {OUTCOME_PARTNER, OUTCOME_RESOLVED}
            and decision.partner_id
        ),
        "decision_maker": "bank_policy",
    }


class TrustGates:
    def __init__(
        self,
        applications: ApplicationService,
        governance: DistributionGovernanceService,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._apps = applications
        self._governance = governance
        self._clock = clock
        self._id_factory = id_factory

    def confirm_missing(self, uow: UnitOfWork, application: DepotApplication) -> tuple[str, ...]:
        """Before the customer may confirm: a recorded, verified identity-proofing result."""
        if proofing_satisfied(uow, application, self._clock()):
            return ()
        return ("identity_proofing",)

    def _evaluate(
        self, uow: UnitOfWork, application: DepotApplication, stage: str, cid: str
    ) -> dict[str, Any]:
        result = evaluate(Service.ACCOUNT_OPENING, policy_facts(uow, application, stage))
        row = PolicyDecisionRow(
            decision_id=f"pol-{self._id_factory()}",
            application_id=application.application_id,
            stage=stage,
            service_context=Service.ACCOUNT_OPENING.value,
            policy_version=POLICY_VERSION,
            allowed=result["allowed"],
            result=result,
            decided_at=self._clock(),
        )
        uow.trust.add(row)
        uow.audit.append(
            self._apps.new_event(
                event_type="policy.evaluated",
                correlation_id=cid,
                actor_type=AuditActorType.SYSTEM,
                actor_id=None,
                outcome=AuditOutcome.SUCCESS if result["allowed"] else AuditOutcome.DENIED,
                application_id=application.application_id,
                details={
                    "decision_id": row.decision_id,
                    "stage": stage,
                    "policy_version": POLICY_VERSION,
                    "allowed": result["allowed"],
                    "blocking": result["blocking"],
                    "human_review": result["human_review"],
                    "unresolved": result["unresolved"],
                },
            )
        )
        return {**result, "decision_id": row.decision_id}

    def consent_evidence(
        self, uow: UnitOfWork, application: DepotApplication, cid: str
    ) -> dict[str, Any]:
        """Fixed with the consent: the attribution decision and the policy version that applied."""
        decision = self._governance.decide_in(uow, application, cid)
        policy = self._evaluate(uow, application, "consent", cid)
        case = latest_case(uow, application.application_id)
        return {
            "identity_proofing_status": case.status if case else "MISSING",
            "identity_proofing_regulatory_evidence": False,
            "attribution_decision_id": decision["decision_id"],
            "attribution_outcome": decision["outcome"],
            "policy_decision_id": policy["decision_id"],
            "policy_version": policy["policy_version"],
        }

    def submit_missing(
        self, uow: UnitOfWork, application: DepotApplication, cid: str
    ) -> tuple[str, ...]:
        """Before the depot provider: the signed package, and no unmet mandatory policy gate."""
        missing = signature_gate(uow, application)
        policy = self._evaluate(uow, application, "submit", cid)
        return (*missing, *(f"policy:{rule}" for rule in policy["blocking"]))

    def submit_evidence(self, uow: UnitOfWork, application_id: str) -> dict[str, Any]:
        package = package_of(uow, application_id)
        session = latest_session(uow, application_id)
        return {
            "contract_package_hash": package.package_hash if package else None,
            "signature_session": session.session_id if session else None,
            "signature_level": session.level if session else None,
            "signature_legal_effect": False,
            "policy_version": POLICY_VERSION,
        }
