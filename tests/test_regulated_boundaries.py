"""KYC and signing boundaries: MOCK only, and a mock result is never regulatory evidence."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.domain.application import DepotApplication
from app.domain.enums import CheckOutcome
from app.integrations.mock import MockKycProvider
from app.integrations.provider_callback import sign
from app.integrations.regulated_boundaries import (
    CallbackDecision,
    IdentityOutcome,
    IdentityVerificationRequest,
    IdentityVerificationResult,
    MockSignatureCallbacks,
    SignatureLevel,
    SignatureRequest,
    SignatureState,
    identity_result_from_check,
    new_signature_event,
)

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
SECRET = b"test-only-mock-signing-secret"
DIGEST = "a" * 64
APPLICATION = DepotApplication(
    application_id="app-1",
    provider_id="fixture-bank",
    product_id="depot-basic",
    channel_id="direct",
    customer_ref="cust-1",
    agent_actor_id="agent-a",
    created_at=NOW,
    updated_at=NOW,
)
IDENTITY_REQUEST = IdentityVerificationRequest(
    request_id="idv-1", application_id="app-1", customer_ref="cust-1"
)


@pytest.mark.parametrize(
    ("check", "expected", "human"),
    [
        (CheckOutcome.PASS, IdentityOutcome.VERIFIED, False),
        (CheckOutcome.REVIEW_REQUIRED, IdentityOutcome.REVIEW_REQUIRED, True),
        (CheckOutcome.FAIL, IdentityOutcome.NOT_VERIFIED, True),
    ],
)
def test_the_existing_mock_kyc_maps_onto_the_boundary_and_stays_a_mock(
    check: CheckOutcome, expected: IdentityOutcome, human: bool
) -> None:
    result = identity_result_from_check(
        IDENTITY_REQUEST, MockKycProvider(check).verify_identity(APPLICATION)
    )
    assert result.outcome is expected and result.human_required is human
    assert result.mock is True and result.is_regulatory_evidence is False


def test_a_provider_outage_needs_a_person_and_is_not_evidence() -> None:
    outage = IdentityVerificationResult(
        request_id="idv-1", outcome=IdentityOutcome.PROVIDER_UNAVAILABLE
    )
    assert outage.human_required is True and outage.is_regulatory_evidence is False


def signature_request(**overrides: object) -> SignatureRequest:
    return SignatureRequest(
        **{
            "request_id": "sig-1",
            "application_id": "app-1",
            "action_digest": DIGEST,
            "expires_at": NOW + timedelta(days=1),
            "state": SignatureState.PENDING_CUSTOMER,
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"level": SignatureLevel.QUALIFIED_VIA_QTSP},
        {"level": SignatureLevel.QUALIFIED_VIA_QTSP, "qtsp_id": "invented-qtsp"},
        {"level": SignatureLevel.QUALIFIED_VIA_QTSP, "mock": False},
    ],
)
def test_a_mock_cannot_claim_a_qualified_signature(overrides: dict) -> None:
    with pytest.raises(ValidationError, match="qualified signature"):
        signature_request(**overrides)
    assert signature_request().level is SignatureLevel.MOCK_NO_LEGAL_EFFECT


def deliver(
    callbacks: MockSignatureCallbacks, body: bytes, *, key: bytes = SECRET, age: int = 0
) -> CallbackDecision:
    now = NOW.timestamp()
    timestamp = str(int(now) - age)
    return callbacks.receive(
        timestamp=timestamp, signature=sign(key, timestamp, body), body=body, now=now
    )


def test_a_signed_callback_applies_once_and_a_replay_has_no_second_effect() -> None:
    callbacks = MockSignatureCallbacks(SECRET, signature_request())
    body = new_signature_event(callbacks.request, SignatureState.SIGNED)
    assert deliver(callbacks, body) is CallbackDecision.APPLIED
    assert callbacks.request.state is SignatureState.SIGNED
    assert deliver(callbacks, body) is CallbackDecision.DUPLICATE_IGNORED


def test_unsigned_stale_and_foreign_callbacks_change_nothing() -> None:
    request = signature_request()
    callbacks = MockSignatureCallbacks(SECRET, request)
    signed = new_signature_event(request, SignatureState.SIGNED)
    assert deliver(callbacks, signed, key=b"another-key") is CallbackDecision.REJECTED
    assert deliver(callbacks, signed, age=301) is CallbackDecision.REJECTED
    other_action = new_signature_event(
        request.model_copy(update={"action_digest": "b" * 64}), SignatureState.SIGNED
    )
    assert deliver(callbacks, other_action) is CallbackDecision.REJECTED
    other_request = new_signature_event(
        request.model_copy(update={"request_id": "sig-2"}), SignatureState.SIGNED
    )
    assert deliver(callbacks, other_request) is CallbackDecision.REJECTED
    for timestamp, signature in ((None, None), ("\xb2", "sha256=x"), ("1" * 5000, "\xe9")):
        decision = callbacks.receive(
            timestamp=timestamp, signature=signature, body=signed, now=NOW.timestamp()
        )
        assert decision is CallbackDecision.REJECTED
    assert callbacks.request.state is SignatureState.PENDING_CUSTOMER


def test_a_finished_signature_cannot_change_again() -> None:
    callbacks = MockSignatureCallbacks(SECRET, signature_request(state=SignatureState.SIGNED))
    body = new_signature_event(callbacks.request, SignatureState.DECLINED)
    assert deliver(callbacks, body) is CallbackDecision.REJECTED
    assert callbacks.request.state is SignatureState.SIGNED


@pytest.mark.parametrize(
    "state", [SignatureState.DECLINED, SignatureState.EXPIRED, SignatureState.PROVIDER_UNAVAILABLE]
)
def test_a_signature_that_did_not_complete_needs_a_person(state: SignatureState) -> None:
    callbacks = MockSignatureCallbacks(SECRET, signature_request())
    assert deliver(callbacks, new_signature_event(callbacks.request, state)) is (
        CallbackDecision.APPLIED
    )
    assert callbacks.request.human_required is True
