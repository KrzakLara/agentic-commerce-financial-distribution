# Onboarding state machine (Phase 01)

Implemented in `app/domain/workflow.py` (pure rules) and `ApplicationService.transition`
(atomic, audited). Source: ADR-001 v1.1 section 05 and plan v2 Phase 1.
**There is no `set_status`.** An edge either exists in the table below for the caller's
*authority*, or the change is rejected.

## Authorities

| Authority | Meaning |
|---|---|
| `AGENT` | The untrusted external AI agent. **Appears on no edge** - it can only request things. |
| `SYSTEM_POLICY` | Deterministic bank-side workflow / policy (T2). |
| `CUSTOMER_CHANNEL` | The authenticated customer in the bank-controlled channel (Phase 02/03). |
| `OPERATOR` | Bank staff in the trusted channel. |
| `DEPOT_PROVIDER_CONFIRMATION` | Authenticated depot-provider callback (Phase 02). The only way to `OPENED`. |

The authority is chosen by trusted server code. It is never read from a request body, so no
API client can claim a stronger one.

## Happy path

`DRAFT -> AWAITING_AUTHORIZATION -> AUTHORIZED -> KYC_PENDING -> COMPLIANCE_PENDING ->
CONTRACT_PENDING -> SUBMITTED_TO_DEPOT_PROVIDER -> OPENED`

Side outcomes: `ACTION_REQUIRED`, `MANUAL_REVIEW`, `REJECTED`, `CANCELLED`, `FAILED`.
Terminal states (no outgoing edges): `OPENED`, `REJECTED`, `CANCELLED`, `FAILED`.

## Allowed transitions

| From | To | Allowed authority |
|---|---|---|
| DRAFT | AWAITING_AUTHORIZATION | SYSTEM_POLICY |
| DRAFT | MANUAL_REVIEW | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| DRAFT | CANCELLED | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| DRAFT | FAILED | SYSTEM_POLICY |
| AWAITING_AUTHORIZATION | AUTHORIZED | CUSTOMER_CHANNEL |
| AWAITING_AUTHORIZATION | ACTION_REQUIRED | SYSTEM_POLICY |
| AWAITING_AUTHORIZATION | MANUAL_REVIEW | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| AWAITING_AUTHORIZATION | CANCELLED | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| AWAITING_AUTHORIZATION | FAILED | SYSTEM_POLICY |
| AUTHORIZED | KYC_PENDING | SYSTEM_POLICY |
| AUTHORIZED | ACTION_REQUIRED | SYSTEM_POLICY |
| AUTHORIZED | MANUAL_REVIEW | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| AUTHORIZED | CANCELLED | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| AUTHORIZED | FAILED | SYSTEM_POLICY |
| KYC_PENDING | COMPLIANCE_PENDING | SYSTEM_POLICY |
| KYC_PENDING | ACTION_REQUIRED | SYSTEM_POLICY |
| KYC_PENDING | MANUAL_REVIEW | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| KYC_PENDING | REJECTED | SYSTEM_POLICY |
| KYC_PENDING | CANCELLED | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| KYC_PENDING | FAILED | SYSTEM_POLICY |
| COMPLIANCE_PENDING | CONTRACT_PENDING | SYSTEM_POLICY |
| COMPLIANCE_PENDING | ACTION_REQUIRED | SYSTEM_POLICY |
| COMPLIANCE_PENDING | MANUAL_REVIEW | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| COMPLIANCE_PENDING | REJECTED | SYSTEM_POLICY |
| COMPLIANCE_PENDING | CANCELLED | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| COMPLIANCE_PENDING | FAILED | SYSTEM_POLICY |
| CONTRACT_PENDING | SUBMITTED_TO_DEPOT_PROVIDER | SYSTEM_POLICY |
| CONTRACT_PENDING | ACTION_REQUIRED | SYSTEM_POLICY |
| CONTRACT_PENDING | MANUAL_REVIEW | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| CONTRACT_PENDING | CANCELLED | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| CONTRACT_PENDING | FAILED | SYSTEM_POLICY |
| SUBMITTED_TO_DEPOT_PROVIDER | MANUAL_REVIEW | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| SUBMITTED_TO_DEPOT_PROVIDER | OPENED | DEPOT_PROVIDER_CONFIRMATION |
| SUBMITTED_TO_DEPOT_PROVIDER | REJECTED | DEPOT_PROVIDER_CONFIRMATION |
| SUBMITTED_TO_DEPOT_PROVIDER | FAILED | SYSTEM_POLICY |
| ACTION_REQUIRED | CANCELLED | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| ACTION_REQUIRED | FAILED | SYSTEM_POLICY |
| MANUAL_REVIEW | REJECTED | OPERATOR |
| MANUAL_REVIEW | CANCELLED | CUSTOMER_CHANNEL, SYSTEM_POLICY |
| MANUAL_REVIEW | FAILED | SYSTEM_POLICY |

## Parked states: `ACTION_REQUIRED` and `MANUAL_REVIEW`

Entering one of them stores the interrupted step in `resume_status`. Leaving them can only
re-enter that same step, so a review never skips a mandatory check (ADR section 05):

| From | To | Allowed authority |
|---|---|---|
| ACTION_REQUIRED | the stored `resume_status` | SYSTEM_POLICY, CUSTOMER_CHANNEL |
| MANUAL_REVIEW | the stored `resume_status` | OPERATOR |

`CANCELLED`, `FAILED` (and `REJECTED` from `MANUAL_REVIEW` by an operator) are listed in the
main table. A negative appropriateness result is **not** automatically `REJECTED` (ADR).

## Error semantics

* No such edge -> `InvalidTransitionError` -> HTTP 409 `invalid_transition`.
* Edge exists, wrong authority -> `TransitionNotAuthorizedError` -> HTTP 403 `transition_not_authorized`.
* Another writer won the race -> `ConcurrentModificationError` -> HTTP 409 `concurrent_modification` (safe to retry).

Every rejected attempt is recorded as an `application.transition_rejected` audit event.

## Defence in depth in the database

Besides the Python rules, SQLite triggers refuse (even for raw SQL): inserting a non-`DRAFT`
application, moving to `OPENED` from anything but `SUBMITTED_TO_DEPOT_PROVIDER`, and changing a
terminal state. A CHECK constraint ties `resume_status` to the parked states.

## Agent-visible states

(Unchanged in Phase 02: the agent still never sees KYC vs compliance vs review.)

The agent never sees the internal state, only a coarse one (`app/domain/visibility.py`):

| Internal state | Agent sees |
|---|---|
| DRAFT | DRAFT |
| AWAITING_AUTHORIZATION | AWAITING_CUSTOMER |
| AUTHORIZED, KYC_PENDING, COMPLIANCE_PENDING, CONTRACT_PENDING, SUBMITTED_TO_DEPOT_PROVIDER | IN_PROGRESS |
| ACTION_REQUIRED, MANUAL_REVIEW | ACTION_REQUIRED |
| OPENED | OPENED |
| REJECTED | DECLINED |
| CANCELLED | CANCELLED |
| FAILED | FAILED |

## Who drives each transition in Phase 02

| Transition | Driven by |
|---|---|
| DRAFT -> AWAITING_AUTHORIZATION | `OnboardingService.request_handoff` (agent request, `SYSTEM_POLICY`) |
| AWAITING_AUTHORIZATION -> AUTHORIZED | `confirm_handoff` after challenge, identity, digest, catalog and mandate checks (`CUSTOMER_CHANNEL`) |
| AUTHORIZED -> KYC_PENDING -> COMPLIANCE_PENDING -> CONTRACT_PENDING | `advance_checks`, one committed step each, with a `check_results` row (`SYSTEM_POLICY`) |
| any non-PASS check -> MANUAL_REVIEW | `advance_checks`, fail closed (`SYSTEM_POLICY`) |
| CONTRACT_PENDING -> SUBMITTED_TO_DEPOT_PROVIDER | `submit_application`, only if `evaluate_submit` allows (`SYSTEM_POLICY`) |
| SUBMITTED_TO_DEPOT_PROVIDER -> OPENED | `handle_provider_confirmation` after signature, identity, state and replay checks (`DEPOT_PROVIDER_CONFIRMATION`) |

Each runs through `ApplicationService.apply_transition`, inside the flow's own unit of work: the
state change, its audit event, the mandate revocation (terminal states) and the flow's other writes
commit together or not at all.

## Not yet implemented

No endpoint triggers transitions yet; the handoff, customer confirmation, mock checks and the
provider callback that drive them arrive in Phases 02-03. Phase 01 defines and tests the rules.
