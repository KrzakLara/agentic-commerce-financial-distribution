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
