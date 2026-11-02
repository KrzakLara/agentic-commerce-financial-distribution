"""The deterministic policy gate: pure functions, exhaustive on the submit prerequisites."""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.application import DepotApplication
from app.domain.checks import CheckRecord
from app.domain.enums import ApplicationStatus as S
from app.domain.enums import CheckOutcome, CheckType, MandateScope
from app.domain.handoff import ApprovedAction, Handoff
from app.domain.mandate import Mandate
from app.domain.receipts import ConsentReceipt, DisclosureReceipt
from app.services.policy import evaluate_handoff_request, evaluate_submit, mandate_allows

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
DIGEST = "e" * 64
ACTION = ApprovedAction(
    application_id="app-1", provider_id="fixture-bank", product_id="depot-basic",
    channel_id="direct", customer_ref="cust-1", catalog_version="2026.11",
    document_id="kid-depot-basic", document_version="1.0", document_digest=DIGEST,
)  # fmt: skip


def application(status: S = S.CONTRACT_PENDING) -> DepotApplication:
    return DepotApplication(
        application_id="app-1", provider_id="fixture-bank", product_id="depot-basic",
        channel_id="direct", customer_ref="cust-1", agent_actor_id="agent-a", status=status,
        resume_status=S.KYC_PENDING if status in {S.MANUAL_REVIEW, S.ACTION_REQUIRED} else None,
        created_at=NOW, updated_at=NOW,
    )  # fmt: skip


def mandate(**overrides: object) -> Mandate:
    values: dict[str, object] = {
        "mandate_id": "m-1", "jti": "j-1", "application_id": "app-1", "agent_actor_id": "agent-a",
        "customer_id": "cust-1", "audience": "trusteq-onboarding-api",
        "scopes": frozenset(MandateScope), "issued_at": NOW, "expires_at": NOW + timedelta(hours=1),
    }  # fmt: skip
    values.update(overrides)
    return Mandate(**values)  # type: ignore[arg-type]


def handoff(**overrides: object) -> Handoff:
    values: dict[str, object] = {
        "handoff_id": "h-1", "application_id": "app-1", "nonce_hash": DIGEST, "action": ACTION,
        "action_digest": ACTION.digest(), "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=10), "customer_id": "cust-1", "consumed_at": NOW,
    }  # fmt: skip
    values.update(overrides)
    return Handoff(**values)  # type: ignore[arg-type]


def consent(**overrides: object) -> ConsentReceipt:
    values: dict[str, object] = {
        "receipt_id": "c-1", "application_id": "app-1", "customer_id": "cust-1",
        "handoff_id": "h-1", "action_digest": ACTION.digest(), "confirmed_at": NOW,
    }  # fmt: skip
    values.update(overrides)
    return ConsentReceipt(**values)  # type: ignore[arg-type]


def disclosure(**overrides: object) -> DisclosureReceipt:
    values: dict[str, object] = {
        "receipt_id": "d-1", "application_id": "app-1", "customer_id": "cust-1",
        "document_id": "kid-depot-basic", "document_version": "1.0", "document_digest": DIGEST,
        "acknowledged_at": NOW,
    }  # fmt: skip
    values.update(overrides)
    return DisclosureReceipt(**values)  # type: ignore[arg-type]


def check(kind: CheckType, outcome: CheckOutcome = CheckOutcome.PASS) -> CheckRecord:
    return CheckRecord(
        application_id="app-1", check_type=kind, outcome=outcome, reference="MOCK-REF",
        mock=True, created_at=NOW,
    )  # fmt: skip


def submit(**overrides: object):  # noqa: ANN201
    values: dict[str, object] = {
        "application": application(), "mandate": mandate(), "actor_id": "agent-a", "now": NOW,
        "handoff": handoff(), "consent": consent(), "disclosure": disclosure(),
        "kyc": check(CheckType.KYC), "compliance": check(CheckType.COMPLIANCE),
        "contract": check(CheckType.CONTRACT),
    }  # fmt: skip
    values.update(overrides)
    return evaluate_submit(**values)  # type: ignore[arg-type]


def test_submit_is_allowed_only_when_every_prerequisite_is_present() -> None:
    decision = submit()
    assert decision.allowed and decision.missing == ()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"consent": None}, "customer_consent"),
        ({"handoff": None}, "customer_consent"),
        ({"consent": consent(action_digest="f" * 64)}, "customer_consent"),
        ({"consent": consent(customer_id="someone-else")}, "customer_consent"),
        ({"handoff": handoff(consumed_at=None, customer_id=None)}, "customer_consent"),
        ({"handoff": handoff(customer_id="someone-else")}, "customer_consent"),
        ({"disclosure": None}, "disclosure_acknowledgement"),
        ({"disclosure": disclosure(document_version="0.9")}, "disclosure_acknowledgement"),
        ({"disclosure": disclosure(document_digest="a" * 64)}, "disclosure_acknowledgement"),
        ({"kyc": None}, "kyc"),
        ({"kyc": check(CheckType.KYC, CheckOutcome.REVIEW_REQUIRED)}, "kyc"),
        ({"compliance": None}, "compliance"),
        ({"compliance": check(CheckType.COMPLIANCE, CheckOutcome.FAIL)}, "compliance"),
        ({"contract": None}, "contract"),
        ({"contract": check(CheckType.KYC)}, "contract"),  # wrong kind of evidence
        ({"mandate": None}, "mandate_inactive"),
        ({"mandate": mandate(revoked_at=NOW)}, "mandate_inactive"),
        (
            {"mandate": mandate(issued_at=NOW - timedelta(hours=1), expires_at=NOW)},
            "mandate_inactive",
        ),
        ({"mandate": mandate(agent_actor_id="agent-b")}, "mandate_inactive"),
        (
            {"mandate": mandate(scopes=frozenset({MandateScope.READ_STATUS}))},
            "mandate_inactive",
        ),
    ],
)
def test_each_missing_prerequisite_blocks_submission_and_is_named(
    overrides: dict[str, object], expected: str
) -> None:
    decision = submit(**overrides)
    assert not decision.allowed
    assert expected in decision.missing


@pytest.mark.parametrize("status", [s for s in S if s is not S.CONTRACT_PENDING])
def test_submission_requires_exactly_the_contract_pending_state(status: S) -> None:
    decision = submit(application=application(status))
    assert not decision.allowed and "state_not_contract_pending" in decision.missing


def test_everything_missing_is_reported_together() -> None:
    decision = submit(
        application=application(S.DRAFT), mandate=None, handoff=None, consent=None,
        disclosure=None, kyc=None, compliance=None, contract=None,
    )  # fmt: skip
    assert set(decision.missing) == {
        "mandate_inactive", "state_not_contract_pending", "customer_consent",
        "disclosure_acknowledgement", "kyc", "compliance", "contract",
    }  # fmt: skip


def test_handoff_request_needs_a_mandate_and_an_eligible_state() -> None:
    assert evaluate_handoff_request(application(S.DRAFT), mandate(), "agent-a", NOW).allowed
    assert evaluate_handoff_request(
        application(S.AWAITING_AUTHORIZATION), mandate(), "agent-a", NOW
    ).allowed
    for status in (S.AUTHORIZED, S.KYC_PENDING, S.CONTRACT_PENDING, S.OPENED, S.CANCELLED):
        decision = evaluate_handoff_request(application(status), mandate(), "agent-a", NOW)
        assert decision.missing == ("state_not_eligible_for_handoff",)
    assert (
        "mandate_inactive"
        in evaluate_handoff_request(application(S.DRAFT), None, "agent-a", NOW).missing
    )


def test_mandate_allows_checks_owner_scope_expiry_and_revocation() -> None:
    scope = MandateScope.SUBMIT_AFTER_CONSENT
    assert mandate_allows(mandate(), scope, "agent-a", NOW)
    assert not mandate_allows(mandate(), scope, "agent-b", NOW)
    assert not mandate_allows(mandate(), scope, "agent-a", NOW + timedelta(hours=1))
    assert not mandate_allows(mandate(revoked_at=NOW), scope, "agent-a", NOW)
    assert not mandate_allows(None, scope, "agent-a", NOW)
