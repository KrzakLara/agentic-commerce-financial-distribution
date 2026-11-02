"""The onboarding state machine: an explicit, deterministic transition table (ADR-05).

Pure functions, no I/O. The table lists every allowed edge together with the *authority*
that may take it. Anything not listed is rejected, so there is no generic ``set_status``.

Key rules:

* The untrusted external agent (`TransitionAuthority.AGENT`) appears on no edge. It can
  only *request* things through the API; the trusted core performs the transition.
* ``OPENED`` has exactly one incoming edge: SUBMITTED_TO_DEPOT_PROVIDER -> OPENED, taken only
  with ``DEPOT_PROVIDER_CONFIRMATION`` (an authenticated provider callback, wired up in Phase 02).
* ``ACTION_REQUIRED`` and ``MANUAL_REVIEW`` remember the step they interrupted
  (``resume_status``). Leaving them can only re-enter that step, so a review is never a
  bypass of mandatory checks (ADR section 05).
* Terminal states have no outgoing edges.
"""

from enum import StrEnum

from app.core.exceptions import InvalidTransitionError, TransitionNotAuthorizedError
from app.domain.enums import ApplicationStatus as S


class TransitionAuthority(StrEnum):
    """Who is causing a transition. Set by trusted code, never taken from a request body."""

    AGENT = "AGENT"  # untrusted external agent: may never change state
    SYSTEM_POLICY = "SYSTEM_POLICY"  # deterministic bank-side policy / workflow
    CUSTOMER_CHANNEL = "CUSTOMER_CHANNEL"  # authenticated, bank-controlled customer channel
    OPERATOR = "OPERATOR"  # bank staff in the trusted channel
    DEPOT_PROVIDER_CONFIRMATION = "DEPOT_PROVIDER_CONFIRMATION"  # authenticated provider callback


A = TransitionAuthority

TERMINAL_STATES: frozenset[S] = frozenset({S.OPENED, S.REJECTED, S.CANCELLED, S.FAILED})
# States that park the workflow and remember where to continue.
RESUMABLE_STATES: frozenset[S] = frozenset({S.ACTION_REQUIRED, S.MANUAL_REVIEW})

_MAIN_STEPS = (
    S.AWAITING_AUTHORIZATION,
    S.AUTHORIZED,
    S.KYC_PENDING,
    S.COMPLIANCE_PENDING,
    S.CONTRACT_PENDING,
)
# Steps a workflow can be interrupted in (ACTION_REQUIRED / MANUAL_REVIEW can only follow them).
_REVIEWABLE = (S.DRAFT, *_MAIN_STEPS, S.SUBMITTED_TO_DEPOT_PROVIDER)
_NON_TERMINAL = frozenset(S) - TERMINAL_STATES


def _build_static_transitions() -> dict[tuple[S, S], frozenset[A]]:
    t: dict[tuple[S, S], frozenset[A]] = {}

    def edge(src: S, dst: S, *authorities: A) -> None:
        t[(src, dst)] = frozenset(authorities)

    # Happy path. Each step is taken by trusted code only.
    edge(S.DRAFT, S.AWAITING_AUTHORIZATION, A.SYSTEM_POLICY)  # after a handoff is created
    edge(S.AWAITING_AUTHORIZATION, S.AUTHORIZED, A.CUSTOMER_CHANNEL)  # customer confirms
    edge(S.AUTHORIZED, S.KYC_PENDING, A.SYSTEM_POLICY)
    edge(S.KYC_PENDING, S.COMPLIANCE_PENDING, A.SYSTEM_POLICY)
    edge(S.COMPLIANCE_PENDING, S.CONTRACT_PENDING, A.SYSTEM_POLICY)
    edge(S.CONTRACT_PENDING, S.SUBMITTED_TO_DEPOT_PROVIDER, A.SYSTEM_POLICY)
    # The one and only way to OPENED.
    edge(S.SUBMITTED_TO_DEPOT_PROVIDER, S.OPENED, A.DEPOT_PROVIDER_CONFIRMATION)

    # Side outcomes.
    for src in _MAIN_STEPS:
        edge(src, S.ACTION_REQUIRED, A.SYSTEM_POLICY)
    for src in _REVIEWABLE:
        edge(src, S.MANUAL_REVIEW, A.SYSTEM_POLICY, A.CUSTOMER_CHANNEL)  # incl. request-human
    for src in (S.KYC_PENDING, S.COMPLIANCE_PENDING):
        edge(src, S.REJECTED, A.SYSTEM_POLICY)
    edge(S.MANUAL_REVIEW, S.REJECTED, A.OPERATOR)
    edge(S.SUBMITTED_TO_DEPOT_PROVIDER, S.REJECTED, A.DEPOT_PROVIDER_CONFIRMATION)
    for src in _NON_TERMINAL - {S.SUBMITTED_TO_DEPOT_PROVIDER}:
        edge(src, S.CANCELLED, A.CUSTOMER_CHANNEL, A.SYSTEM_POLICY)  # not after submission
    for src in _NON_TERMINAL:
        edge(src, S.FAILED, A.SYSTEM_POLICY)
    return t


STATIC_TRANSITIONS: dict[tuple[S, S], frozenset[A]] = _build_static_transitions()

# Leaving a parked state is only possible towards the remembered step.
RESUME_AUTHORITIES: dict[S, frozenset[A]] = {
    S.ACTION_REQUIRED: frozenset({A.SYSTEM_POLICY, A.CUSTOMER_CHANNEL}),
    S.MANUAL_REVIEW: frozenset({A.OPERATOR}),
}


def allowed_authorities(
    current: S, target: S, resume_status: S | None = None
) -> frozenset[A] | None:
    """Authorities allowed on the edge ``current -> target``; ``None`` if there is no such edge."""
    static = STATIC_TRANSITIONS.get((current, target))
    if static is not None:
        return static
    if current in RESUMABLE_STATES and resume_status is not None and target == resume_status:
        return RESUME_AUTHORITIES[current]
    return None


def check_transition(current: S, target: S, authority: A, resume_status: S | None = None) -> None:
    """Raise unless ``authority`` may move an application from ``current`` to ``target``.

    * `InvalidTransitionError` (409): there is no such edge.
    * `TransitionNotAuthorizedError` (403): the edge exists, but not for this authority.
    """
    authorities = allowed_authorities(current, target, resume_status)
    if authorities is None:
        raise InvalidTransitionError(
            f"Transition {current.value} -> {target.value} is not allowed."
        )
    if authority not in authorities:
        raise TransitionNotAuthorizedError(
            f"{authority.value} may not perform {current.value} -> {target.value}."
        )


def next_resume_status(current: S, target: S) -> S | None:
    """The `resume_status` to persist after taking ``current -> target``."""
    if target in RESUMABLE_STATES:
        return current  # remember the interrupted step
    return None  # any other target clears it
