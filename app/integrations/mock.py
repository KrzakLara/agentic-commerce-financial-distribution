"""MOCK adapters for the regulated providers - synthetic, deterministic, no real operations.

Every class here is simulated and must never be described as real KYC, AML, signing or
bank integration. The outcome is a constructor argument; in addition (Phase 04) there is one
SYNTHETIC scenario switch, like a payment sandbox's test card number:

* `MockComplianceProvider` answers REVIEW_REQUIRED for any customer reference that starts with
  ``synthetic-review-`` (used by ``scripts/demo_manual_review.py``).

Re-evaluation after human review: a REVIEW_REQUIRED outcome is resolved (PASS) only if the
recorded evidence contains the category that this check asks for; FAIL is never resolved by
evidence. Deterministic: the same application and evidence always give the same result.
"""

from collections.abc import Sequence
from typing import ClassVar

from app.domain.application import DepotApplication
from app.domain.enums import ReviewEvidenceCategory
from app.domain.receipts import ConsentReceipt
from app.integrations.results import CheckOutcome, CheckResult, ProviderSubmission, SigningResult

REVIEW_SCENARIO_PREFIX = "synthetic-review-"
KYC_RESOLVING_EVIDENCE = ReviewEvidenceCategory.IDENTITY_REVERIFICATION
COMPLIANCE_RESOLVING_EVIDENCE = ReviewEvidenceCategory.SOURCE_OF_FUNDS_CONFIRMATION


def _resolve(
    outcome: CheckOutcome,
    evidence: Sequence[ReviewEvidenceCategory],
    resolving: ReviewEvidenceCategory,
) -> CheckOutcome:
    if outcome is CheckOutcome.REVIEW_REQUIRED and resolving in evidence:
        return CheckOutcome.PASS
    return outcome


class MockKycProvider:
    IS_MOCK: ClassVar[bool] = True

    def __init__(self, outcome: CheckOutcome = CheckOutcome.PASS) -> None:
        self._outcome = outcome

    def verify_identity(
        self, application: DepotApplication, evidence: Sequence[ReviewEvidenceCategory] = ()
    ) -> CheckResult:
        return CheckResult(
            outcome=_resolve(self._outcome, evidence, KYC_RESOLVING_EVIDENCE),
            reference=f"MOCK-KYC-{application.application_id}",
            mock=True,
        )


class MockComplianceProvider:
    IS_MOCK: ClassVar[bool] = True

    def __init__(self, outcome: CheckOutcome = CheckOutcome.PASS) -> None:
        self._outcome = outcome

    def assess(
        self, application: DepotApplication, evidence: Sequence[ReviewEvidenceCategory] = ()
    ) -> CheckResult:
        outcome = self._outcome
        if application.customer_ref.startswith(REVIEW_SCENARIO_PREFIX):
            outcome = CheckOutcome.REVIEW_REQUIRED  # the synthetic manual-review scenario
        return CheckResult(
            outcome=_resolve(outcome, evidence, COMPLIANCE_RESOLVING_EVIDENCE),
            reference=f"MOCK-COMPLIANCE-{application.application_id}",
            mock=True,
        )


class MockContractSigner:
    IS_MOCK: ClassVar[bool] = True

    def record_signature(self, consent: ConsentReceipt) -> SigningResult:
        return SigningResult(
            signed=True, contract_ref=f"MOCK-CONTRACT-{consent.application_id}", mock=True
        )


class MockDepotProvider:
    IS_MOCK: ClassVar[bool] = True

    def submit_application(self, application: DepotApplication) -> ProviderSubmission:
        return ProviderSubmission(
            provider_ref=f"MOCK-DEPOT-{application.application_id}", accepted=True, mock=True
        )
