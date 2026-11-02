"""Deterministic policy gate for the onboarding flow (ADR-03: policy decides, never the LLM).

Pure functions over already-loaded facts - no I/O, no randomness, no model output. Each
decision returns *which prerequisites are missing* so the service can audit the precise reason
while the agent only ever receives a generic denial.
"""

from dataclasses import dataclass
from datetime import datetime

from app.domain.application import DepotApplication
from app.domain.checks import CheckRecord
from app.domain.enums import ApplicationStatus as S
from app.domain.enums import CheckOutcome, CheckType, MandateScope
from app.domain.handoff import Handoff
from app.domain.mandate import Mandate
from app.domain.receipts import ConsentReceipt, DisclosureReceipt

HANDOFF_ALLOWED_STATES = frozenset({S.DRAFT, S.AWAITING_AUTHORIZATION})


@dataclass(frozen=True)
class PolicyDecision:
    missing: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.missing


def mandate_allows(
    mandate: Mandate | None, scope: MandateScope, actor_id: str, now: datetime
) -> bool:
    """The mandate must exist, be this actor's, unexpired, unrevoked and carry the scope."""
    return (
        mandate is not None
        and mandate.agent_actor_id == actor_id
        and mandate.revoked_at is None
        and now < mandate.expires_at
        and scope in mandate.scopes
    )


def evaluate_handoff_request(
    application: DepotApplication, mandate: Mandate | None, actor_id: str, now: datetime
) -> PolicyDecision:
    missing: list[str] = []
    if not mandate_allows(mandate, MandateScope.REQUEST_HANDOFF, actor_id, now):
        missing.append("mandate_inactive")
    if application.status not in HANDOFF_ALLOWED_STATES:
        missing.append("state_not_eligible_for_handoff")
    return PolicyDecision(tuple(missing))


def evaluate_submit(
    *,
    application: DepotApplication,
    mandate: Mandate | None,
    actor_id: str,
    now: datetime,
    handoff: Handoff | None,
    consent: ConsentReceipt | None,
    disclosure: DisclosureReceipt | None,
    kyc: CheckRecord | None,
    compliance: CheckRecord | None,
    contract: CheckRecord | None,
) -> PolicyDecision:
    """Submission needs ALL of: active mandate, customer consent bound to the exact approved
    action, the matching disclosure acknowledgement, and passed KYC, compliance and contract
    steps - evidence in the database, not just a state label."""
    missing: list[str] = []
    if not mandate_allows(mandate, MandateScope.SUBMIT_AFTER_CONSENT, actor_id, now):
        missing.append("mandate_inactive")
    if application.status is not S.CONTRACT_PENDING:
        missing.append("state_not_contract_pending")

    consent_ok = (
        consent is not None
        and handoff is not None
        and handoff.consumed_at is not None
        and handoff.customer_id == consent.customer_id == application.customer_ref
        and consent.handoff_id == handoff.handoff_id
        and consent.action_digest == handoff.action_digest == handoff.action.digest()
    )
    if not consent_ok:
        missing.append("customer_consent")

    disclosure_ok = (
        disclosure is not None
        and handoff is not None
        and disclosure.document_id == handoff.action.document_id
        and disclosure.document_version == handoff.action.document_version
        and disclosure.document_digest == handoff.action.document_digest
    )
    if not disclosure_ok:
        missing.append("disclosure_acknowledgement")

    for name, check_type, record in (
        ("kyc", CheckType.KYC, kyc),
        ("compliance", CheckType.COMPLIANCE, compliance),
        ("contract", CheckType.CONTRACT, contract),
    ):
        if (
            record is None
            or record.check_type is not check_type
            or record.outcome is not CheckOutcome.PASS
        ):
            missing.append(name)
    return PolicyDecision(tuple(missing))
