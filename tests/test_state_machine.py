"""Pure state-machine rules (no database): ADR-05 / plan Phase 01."""

import itertools
from pathlib import Path

import pytest

from app.core.exceptions import InvalidTransitionError, TransitionNotAuthorizedError
from app.domain.enums import ApplicationStatus as S
from app.domain.workflow import (
    RESUMABLE_STATES,
    STATIC_TRANSITIONS,
    TERMINAL_STATES,
    TransitionAuthority,
    allowed_authorities,
    check_transition,
    next_resume_status,
)

A = TransitionAuthority
DOC = Path(__file__).resolve().parent.parent / "docs" / "state-machine.md"

HAPPY_PATH = [
    (S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY),
    (S.AUTHORIZED, A.CUSTOMER_CHANNEL),
    (S.KYC_PENDING, A.SYSTEM_POLICY),
    (S.COMPLIANCE_PENDING, A.SYSTEM_POLICY),
    (S.CONTRACT_PENDING, A.SYSTEM_POLICY),
    (S.SUBMITTED_TO_DEPOT_PROVIDER, A.SYSTEM_POLICY),
    (S.OPENED, A.DEPOT_PROVIDER_CONFIRMATION),
]


def test_every_state_named_in_the_plan_exists() -> None:
    assert {s.value for s in S} == {
        "DRAFT", "AWAITING_AUTHORIZATION", "AUTHORIZED", "KYC_PENDING", "COMPLIANCE_PENDING",
        "CONTRACT_PENDING", "SUBMITTED_TO_DEPOT_PROVIDER", "OPENED", "ACTION_REQUIRED",
        "MANUAL_REVIEW", "REJECTED", "CANCELLED", "FAILED",
    }  # fmt: skip


def test_happy_path_is_allowed_edge_by_edge() -> None:
    current = S.DRAFT
    for target, authority in HAPPY_PATH:
        check_transition(current, target, authority)
        current = target
    assert current is S.OPENED


@pytest.mark.parametrize("current", sorted(TERMINAL_STATES))
def test_terminal_states_have_no_outgoing_edges(current: S) -> None:
    for target, authority in itertools.product(S, A):
        with pytest.raises(InvalidTransitionError):
            check_transition(current, target, authority, resume_status=S.KYC_PENDING)


def test_every_combination_outside_the_table_is_rejected() -> None:
    """Exhaustive: 13 x 13 x 5 combinations; allowed exactly when the table says so."""
    for current, target, authority in itertools.product(S, S, A):
        permitted = authority in STATIC_TRANSITIONS.get((current, target), frozenset())
        if permitted:
            check_transition(current, target, authority)
        else:
            with pytest.raises((InvalidTransitionError, TransitionNotAuthorizedError)):
                check_transition(current, target, authority)


def test_an_edge_with_the_wrong_authority_is_forbidden_not_invalid() -> None:
    with pytest.raises(TransitionNotAuthorizedError):
        check_transition(S.AWAITING_AUTHORIZATION, S.AUTHORIZED, A.SYSTEM_POLICY)
    with pytest.raises(InvalidTransitionError):
        check_transition(S.DRAFT, S.CONTRACT_PENDING, A.SYSTEM_POLICY)  # skipping steps


def test_the_external_agent_can_take_no_edge_at_all() -> None:
    for authorities in STATIC_TRANSITIONS.values():
        assert A.AGENT not in authorities
    for resume_authorities in (frozenset({A.SYSTEM_POLICY, A.CUSTOMER_CHANNEL}), {A.OPERATOR}):
        assert A.AGENT not in resume_authorities
    for current, target in itertools.product(S, S):
        with pytest.raises((InvalidTransitionError, TransitionNotAuthorizedError)):
            check_transition(current, target, A.AGENT, resume_status=S.AUTHORIZED)


def test_opened_has_exactly_one_incoming_edge_and_one_authority() -> None:
    incoming = {k: v for k, v in STATIC_TRANSITIONS.items() if k[1] is S.OPENED}
    assert incoming == {(S.SUBMITTED_TO_DEPOT_PROVIDER, S.OPENED): {A.DEPOT_PROVIDER_CONFIRMATION}}


@pytest.mark.parametrize("current", [s for s in S if s is not S.SUBMITTED_TO_DEPOT_PROVIDER])
def test_opened_is_unreachable_from_any_other_state_with_any_authority(current: S) -> None:
    for authority in A:
        with pytest.raises((InvalidTransitionError, TransitionNotAuthorizedError)):
            check_transition(
                current, S.OPENED, authority, resume_status=S.SUBMITTED_TO_DEPOT_PROVIDER
            )


@pytest.mark.parametrize("authority", [a for a in A if a is not A.DEPOT_PROVIDER_CONFIRMATION])
def test_only_the_provider_confirmation_can_open_a_depot(authority: A) -> None:
    with pytest.raises(TransitionNotAuthorizedError):
        check_transition(S.SUBMITTED_TO_DEPOT_PROVIDER, S.OPENED, authority)


def test_rejected_is_not_reachable_from_early_or_unrelated_states() -> None:
    for current in (S.DRAFT, S.AWAITING_AUTHORIZATION, S.AUTHORIZED, S.CONTRACT_PENDING):
        assert allowed_authorities(current, S.REJECTED) is None


def test_cancellation_is_not_possible_after_submission_to_the_depot_provider() -> None:
    assert allowed_authorities(S.SUBMITTED_TO_DEPOT_PROVIDER, S.CANCELLED) is None


@pytest.mark.parametrize("parked", sorted(RESUMABLE_STATES))
def test_a_parked_state_can_only_resume_the_interrupted_step(parked: S) -> None:
    resume_authority = A.OPERATOR if parked is S.MANUAL_REVIEW else A.SYSTEM_POLICY
    check_transition(parked, S.KYC_PENDING, resume_authority, resume_status=S.KYC_PENDING)
    # ... but not skip ahead to a different step (a review is no bypass of mandatory checks)
    with pytest.raises(InvalidTransitionError):
        check_transition(parked, S.CONTRACT_PENDING, resume_authority, resume_status=S.KYC_PENDING)
    with pytest.raises(InvalidTransitionError):
        check_transition(parked, S.KYC_PENDING, resume_authority, resume_status=None)


def test_manual_review_can_be_continued_by_an_operator_only() -> None:
    for authority in (A.AGENT, A.SYSTEM_POLICY, A.CUSTOMER_CHANNEL, A.DEPOT_PROVIDER_CONFIRMATION):
        with pytest.raises(TransitionNotAuthorizedError):
            check_transition(
                S.MANUAL_REVIEW, S.COMPLIANCE_PENDING, authority, resume_status=S.COMPLIANCE_PENDING
            )


def test_resume_status_is_remembered_and_cleared() -> None:
    assert next_resume_status(S.COMPLIANCE_PENDING, S.MANUAL_REVIEW) is S.COMPLIANCE_PENDING
    assert next_resume_status(S.KYC_PENDING, S.ACTION_REQUIRED) is S.KYC_PENDING
    assert next_resume_status(S.MANUAL_REVIEW, S.COMPLIANCE_PENDING) is None
    assert next_resume_status(S.DRAFT, S.AWAITING_AUTHORIZATION) is None


def test_documentation_lists_every_edge_of_the_table() -> None:
    text = DOC.read_text(encoding="utf-8")
    for (current, target), authorities in STATIC_TRANSITIONS.items():
        names = ", ".join(sorted(a.value for a in authorities))
        row = f"| {current.value} | {target.value} | {names} |"
        assert row in text, f"docs/state-machine.md is out of date: missing {row}"
