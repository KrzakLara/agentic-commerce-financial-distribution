"""MOCK adapters for the regulated providers - synthetic, deterministic, no real operations.

Every class here is simulated and must never be described as real KYC, AML, signing or
bank integration. Scenario switching (e.g. PASS -> REVIEW_REQUIRED) is wired up in Phase 02+;
for now the outcome is a constructor argument.
"""

from typing import ClassVar

from app.domain.application import DepotApplication
from app.domain.receipts import ConsentReceipt
from app.integrations.results import CheckOutcome, CheckResult, ProviderSubmission, SigningResult


class MockKycProvider:
    IS_MOCK: ClassVar[bool] = True

    def __init__(self, outcome: CheckOutcome = CheckOutcome.PASS) -> None:
        self._outcome = outcome

    def verify_identity(self, application: DepotApplication) -> CheckResult:
        return CheckResult(
            outcome=self._outcome, reference=f"MOCK-KYC-{application.application_id}", mock=True
        )


class MockComplianceProvider:
    IS_MOCK: ClassVar[bool] = True

    def __init__(self, outcome: CheckOutcome = CheckOutcome.PASS) -> None:
        self._outcome = outcome

    def assess(self, application: DepotApplication) -> CheckResult:
        return CheckResult(
            outcome=self._outcome,
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
