"""Result types shared by the regulated-provider ports (T3)."""

from app.domain.base import DomainModel, OpaqueId, ShortText
from app.domain.enums import CheckOutcome

__all__ = ["CheckOutcome", "CheckResult", "ProviderSubmission", "SigningResult"]


class CheckResult(DomainModel):
    """Outcome of a KYC or compliance check. Internal only: never shown to the agent raw."""

    outcome: CheckOutcome
    reference: ShortText
    mock: bool  # explicit MOCK flag: True for every simulated provider


class SigningResult(DomainModel):
    signed: bool
    contract_ref: ShortText
    mock: bool


class ProviderSubmission(DomainModel):
    """Result of handing an application to the depot provider.

    Deliberately has no `opened` field: OPENED is only ever set from the provider's
    authenticated callback, never from a synchronous call result (ADR section 05).
    """

    provider_ref: OpaqueId
    accepted: bool
    mock: bool
    # Set by the versioned adapter, for the audit trail. Machine codes only, no provider content.
    contract_version: str | None = None
    fallback_from: str | None = None
    refusal: str | None = None

    def audit_details(self) -> dict[str, str]:
        details = {
            "contract_version": self.contract_version,
            "fallback_from": self.fallback_from,
            "refusal": self.refusal,
        }
        return {name: value for name, value in details.items() if value is not None}
