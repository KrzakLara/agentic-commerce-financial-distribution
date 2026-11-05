"""Typed boundaries a regulated KYC provider and a signing provider would plug into. MOCK ONLY.

The existing `KycPort` and `ContractSigningPort` stay as they are, and the onboarding flow does not
call this module. They answer synchronously with PASS or signed, which is enough for the mocks but
cannot express a provider outage or a signature that the customer completes later. These contracts
state what a real integration has to express: who owns the request, what the outcome means, how a
signed callback is checked and when a person must step in.

Nothing here is identity verification or an electronic signature. No QTSP or eIDAS service is
integrated, and a mock result is never regulatory evidence.
"""

import json
import uuid
from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, model_validator

from app.domain.base import DomainModel, OpaqueId, Sha256Hex, ShortText
from app.domain.enums import CheckOutcome
from app.integrations.provider_callback import verify
from app.integrations.results import CheckResult


class IdentityOutcome(StrEnum):
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class IdentityVerificationRequest(DomainModel):
    """Owned by the bank: created by the backend, never by the agent or from its text."""

    request_id: OpaqueId
    application_id: OpaqueId
    customer_ref: OpaqueId


class IdentityVerificationResult(DomainModel):
    """Owned by the provider, recorded by the bank."""

    request_id: OpaqueId
    outcome: IdentityOutcome
    provider_ref: ShortText | None = None
    mock: bool = True

    @property
    def is_regulatory_evidence(self) -> bool:
        """A mock result never is. Real providers need a contract and a compliance sign-off."""
        return False if self.mock else self.outcome is IdentityOutcome.VERIFIED

    @property
    def human_required(self) -> bool:
        return self.outcome is not IdentityOutcome.VERIFIED


_FROM_CHECK = {
    CheckOutcome.PASS: IdentityOutcome.VERIFIED,
    CheckOutcome.REVIEW_REQUIRED: IdentityOutcome.REVIEW_REQUIRED,
    CheckOutcome.FAIL: IdentityOutcome.NOT_VERIFIED,
}


def identity_result_from_check(
    request: IdentityVerificationRequest, check: CheckResult
) -> IdentityVerificationResult:
    """Map an existing mock `CheckResult` onto this boundary. `mock` is carried over, not set."""
    return IdentityVerificationResult(
        request_id=request.request_id,
        outcome=_FROM_CHECK[check.outcome],
        provider_ref=check.reference,
        mock=check.mock,
    )


class SignatureLevel(StrEnum):
    MOCK_NO_LEGAL_EFFECT = "MOCK_NO_LEGAL_EFFECT"
    QUALIFIED_VIA_QTSP = "QUALIFIED_VIA_QTSP"  # names the production target; not implemented


class SignatureState(StrEnum):
    REQUESTED = "REQUESTED"
    PENDING_CUSTOMER = "PENDING_CUSTOMER"
    SIGNED = "SIGNED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


_NEXT: dict[SignatureState, frozenset[SignatureState]] = {
    SignatureState.REQUESTED: frozenset(
        {SignatureState.PENDING_CUSTOMER, SignatureState.PROVIDER_UNAVAILABLE}
    ),
    SignatureState.PENDING_CUSTOMER: frozenset(
        {
            SignatureState.SIGNED,
            SignatureState.DECLINED,
            SignatureState.EXPIRED,
            SignatureState.PROVIDER_UNAVAILABLE,
        }
    ),
    SignatureState.PROVIDER_UNAVAILABLE: frozenset({SignatureState.PENDING_CUSTOMER}),
}
_HUMAN_REQUIRED = {
    SignatureState.DECLINED,
    SignatureState.EXPIRED,
    SignatureState.PROVIDER_UNAVAILABLE,
}


class SignatureRequest(DomainModel):
    """Owned by the bank. Binds the exact approved action; the customer signs, never the agent."""

    request_id: OpaqueId
    application_id: OpaqueId
    action_digest: Sha256Hex
    state: SignatureState = SignatureState.REQUESTED
    level: SignatureLevel = SignatureLevel.MOCK_NO_LEGAL_EFFECT
    qtsp_id: OpaqueId | None = None
    expires_at: AwareDatetime
    mock: bool = True

    @model_validator(mode="after")
    def _no_fake_qualified_signature(self) -> Self:
        qualified = self.level is SignatureLevel.QUALIFIED_VIA_QTSP
        if qualified and (self.mock or self.qtsp_id is None):
            raise ValueError("a qualified signature needs a real, named QTSP; a mock is not one")
        return self

    @property
    def human_required(self) -> bool:
        return self.state in _HUMAN_REQUIRED


class SignatureEvent(DomainModel):
    """The body of the provider's signed callback."""

    event_id: OpaqueId
    request_id: OpaqueId
    action_digest: Sha256Hex
    state: SignatureState


class CallbackDecision(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE_IGNORED = "DUPLICATE_IGNORED"
    REJECTED = "REJECTED"


class MockSignatureCallbacks:
    """Signature, binding, single use and legal state changes of a provider's callback.

    Reuses the HMAC check of the depot callback, a stand-in for a provider's real signed messages
    or mutual TLS. Keeps state in memory because it is a standalone mock.
    """

    def __init__(self, secret: bytes, request: SignatureRequest) -> None:
        self._secret = secret
        self.request = request
        self._applied: set[str] = set()

    def receive(
        self, *, timestamp: str | None, signature: str | None, body: bytes, now: float
    ) -> CallbackDecision:
        try:
            authentic = verify(
                self._secret, timestamp=timestamp, signature=signature, body=body, now=now
            )
            event = SignatureEvent.model_validate(json.loads(body)) if authentic else None
        except (ValueError, TypeError, OverflowError):  # malformed input; see finding F-5
            return CallbackDecision.REJECTED
        if event is None or event.request_id != self.request.request_id:
            return CallbackDecision.REJECTED
        if event.action_digest != self.request.action_digest:
            return CallbackDecision.REJECTED
        if event.event_id in self._applied:
            return CallbackDecision.DUPLICATE_IGNORED
        if event.state not in _NEXT.get(self.request.state, frozenset()):
            return CallbackDecision.REJECTED
        self._applied.add(event.event_id)
        self.request = self.request.model_copy(update={"state": event.state})
        return CallbackDecision.APPLIED


def new_signature_event(request: SignatureRequest, state: SignatureState) -> bytes:
    """A provider's callback body, for the mock and its tests."""
    event = SignatureEvent(
        event_id=uuid.uuid4().hex,
        request_id=request.request_id,
        action_digest=request.action_digest,
        state=state,
    )
    return event.model_dump_json().encode("utf-8")
