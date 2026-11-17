"""What the full-trust profile adds to the frozen onboarding workflow, at its existing policy gates.

No state and no transition is added. The workflow asks these gates at the customer's confirmation
and at the agent's submission, and they answer from recorded evidence.
"""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.domain.application import DepotApplication
from app.domain.enums import AuditActorType, AuditOutcome
from app.repositories.interfaces import UnitOfWork
from app.repositories.trust_orm import (
    AttributionDecisionRow,
    AuthenticationEventRow,
    DelegatedMandateRow,
    PolicyDecisionRow,
)
from app.services.application_service import ApplicationService
from app.services.contract_execution import (
    latest_session,
    open_contract_review,
    package_of,
    signature_gate,
)
from app.services.distribution_governance import (
    OUTCOME_PARTNER,
    OUTCOME_RESOLVED,
    DistributionGovernanceService,
)
from app.services.identity_proofing import latest_case, proofing_satisfied
from app.services.regulatory_policy import (
    POLICY_VERSION,
    Service,
    evaluate,
    service_applicability,
)

# What retrying cannot fix: the approved action or the signed package no longer holds.
CONTRACT_PROBLEMS = frozenset(
    {
        "approved_action_current",
        "contract_package_integrity",
        "contract_package_binding",
        "contract_signature_binding",
        "contract_signatory",
    }
)


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
    own_case = case is not None and case.customer_ref == application.customer_ref
    return {
        "stage": stage,
        "identity_proofing": case.status if own_case and case else None,
        "regulatory_evidence": bool(own_case and case and case.regulatory_evidence),
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
        facts = policy_facts(uow, application, stage)
        applicability = service_applicability(facts)
        result = {
            **evaluate(Service.ACCOUNT_OPENING, facts),
            "service_applicability": applicability,
        }
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
                    "service_context": Service.ACCOUNT_OPENING.value,
                    "policy_version": POLICY_VERSION,
                    "allowed": result["allowed"],
                    "blocking": result["blocking"],
                    "human_review": result["human_review"],
                    "unresolved": result["unresolved"],
                    "service_handling": {
                        service: decision["handling"] for service, decision in applicability.items()
                    },
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
            "authentication": _authentication(uow, application.customer_ref),
            "delegation_id": _delegation_of(uow, application.application_id),
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

    def escalate_contract_problem(
        self, uow: UnitOfWork, application_id: str, missing: Sequence[str], cid: str
    ) -> None:
        """A refused submission whose contract no longer holds goes to a person, and stays there."""
        problems = sorted(CONTRACT_PROBLEMS.intersection(missing))
        if not problems:
            return
        application = uow.applications.get(application_id)
        if application is None:  # pragma: no cover - the refused request has just read it
            return
        open_contract_review(
            uow,
            self._apps,
            application,
            problems=problems,
            case_id=f"rvw-{self._id_factory()}",
            now=self._clock(),
            cid=cid,
        )
        uow.commit()

    def submit_evidence(self, uow: UnitOfWork, application_id: str) -> dict[str, Any]:
        """Everything a reader needs to trace the application, in one audit event."""
        package = package_of(uow, application_id)
        session = latest_session(uow, application_id)
        case = latest_case(uow, application_id)
        consent = uow.receipts.get_consent(application_id)
        document = json.loads(package.canonical_json) if package else {}
        return {
            "contract_package_hash": package.package_hash if package else None,
            "signature_session": session.session_id if session else None,
            "signature_level": session.level if session else None,
            "signature_legal_effect": False,
            "policy_version": POLICY_VERSION,
            "service_context": Service.ACCOUNT_OPENING.value,
            "identity_proofing_status": case.status if case else "MISSING",
            "identity_proofing_regulatory_evidence": False,
            "delegation_id": _delegation_of(uow, application_id),
            "consent_receipt_id": consent.receipt_id if consent else None,
            "approved_action_digest": consent.action_digest if consent else None,
            "product_version": document.get("product_version"),
            "terms_digest": document.get("terms_digest"),
            "source_documents": document.get("documents", []),
        }


def _delegation_of(uow: UnitOfWork, application_id: str) -> str | None:
    mandate = uow.mandates.get_for_application(application_id)
    link = uow.trust.get(DelegatedMandateRow, mandate.mandate_id) if mandate else None
    return link.delegation_id if link else None


def _authentication(uow: UnitOfWork, customer_ref: str) -> dict[str, Any] | None:
    """How the customer last logged in. Authentication evidence, never identity proofing."""
    event = uow.trust.scalars(
        select(AuthenticationEventRow)
        .where(AuthenticationEventRow.customer_ref == customer_ref)
        .order_by(AuthenticationEventRow.recorded_at.desc())
    ).first()
    if event is None:
        return None
    return {
        "issuer": event.issuer,
        "acr": event.acr,
        "amr": list(event.amr),
        "auth_time": event.auth_time,
        "session_id": event.session_id,
    }
