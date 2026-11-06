"""Enumerations taken from ADR-001 v1.1 and the implementation plan.

Listing a state here does NOT define allowed transitions; the transition table and
state machine are Phase 01.
"""

from enum import StrEnum


class TrustZone(StrEnum):
    """ADR section 03: trust is granted by evidence and controls, not by zone labels alone."""

    T0A_CUSTOMER = "T0a"  # human customer, legal decision-maker
    T0B_EXTERNAL_AGENT = "T0b"  # external AI software, always untrusted
    T1_EDGE = "T1"  # verifies requests and provider claims
    T2_CORE = "T2"  # policy and account-opening authority
    T3_REGULATED_PROVIDER = "T3"  # contracted regulated providers


class ApplicationStatus(StrEnum):
    DRAFT = "DRAFT"
    AWAITING_AUTHORIZATION = "AWAITING_AUTHORIZATION"
    AUTHORIZED = "AUTHORIZED"
    KYC_PENDING = "KYC_PENDING"
    COMPLIANCE_PENDING = "COMPLIANCE_PENDING"
    CONTRACT_PENDING = "CONTRACT_PENDING"
    SUBMITTED_TO_DEPOT_PROVIDER = "SUBMITTED_TO_DEPOT_PROVIDER"
    OPENED = "OPENED"  # only a trusted depot-provider confirmation may set this
    # Side outcomes
    ACTION_REQUIRED = "ACTION_REQUIRED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class ProviderRole(StrEnum):
    """Distinct legal roles (ADR ADR-08): a fund provider is not automatically a depot provider."""

    FUND_PROVIDER = "FUND_PROVIDER"
    DISTRIBUTOR = "DISTRIBUTOR"
    DEPOT_PROVIDER = "DEPOT_PROVIDER"
    PARTNER_BANK = "PARTNER_BANK"  # distributes the depot (Hausbank); the commission recipient


class MandateScope(StrEnum):
    """Operation-specific scopes; names mirror the agent tool set (ADR-09).

    `DISCOVER` is never granted: catalog discovery is public (ADR section 05).
    """

    DISCOVER = "discover"
    CREATE_DRAFT = "create_draft"  # create ONE draft application (bootstrap grant only)
    REQUEST_HANDOFF = "request_handoff"  # ask for the customer's approval of one application
    READ_STATUS = "read_status"  # read the coarse status of one application
    SUBMIT_AFTER_CONSENT = "submit_after_consent"  # submit one customer-approved application


class MandateKind(StrEnum):
    """What a mandate (authorization grant) is for. Phase 03.

    * BOOTSTRAP  - issued by the trusted dev/test bootstrap: may create ONE draft for ONE
                   customer; bound to the created application on first use.
    * ONBOARDING - issued by the bank with the draft: handoff + submit for that application;
                   revoked when the workflow reaches a terminal state.
    * STATUS     - issued by the bank with the draft: read-only coarse status of that
                   application; deliberately outlives the workflow until it expires, so the
                   agent can learn the outcome (ADR section 03, step 5). It can change nothing.
    """

    BOOTSTRAP = "bootstrap"
    ONBOARDING = "onboarding"
    STATUS = "status"


class AuditActorType(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    OPERATOR = "operator"
    PROVIDER = "provider"
    SYSTEM = "system"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILURE = "failure"


class ReviewCaseStatus(StrEnum):
    OPEN = "OPEN"  # waiting for an operator
    AWAITING_INFORMATION = "AWAITING_INFORMATION"  # operator asked for more evidence
    IN_REVIEW = "IN_REVIEW"  # evidence recorded, ready for a decision
    CLOSED = "CLOSED"  # resolved (see ReviewDecision); a closed case never reopens


class ReviewCaseReason(StrEnum):
    COMPLIANCE_REVIEW_REQUIRED = "COMPLIANCE_REVIEW_REQUIRED"
    KYC_REVIEW_REQUIRED = "KYC_REVIEW_REQUIRED"
    CHECK_FAILED = "CHECK_FAILED"  # a mandatory check returned FAIL: a human decides, not code
    CUSTOMER_REQUESTED_HUMAN = "CUSTOMER_REQUESTED_HUMAN"
    IDENTITY_PROOFING_NOT_COMPLETED = "IDENTITY_PROOFING_NOT_COMPLETED"
    SIGNATURE_NOT_COMPLETED = "SIGNATURE_NOT_COMPLETED"


class HumanRequestCategory(StrEnum):
    """Why a customer asks for a person. A fixed list: no free text, no personal data."""

    QUESTION_ABOUT_PRODUCT = "QUESTION_ABOUT_PRODUCT"
    HELP_WITH_PROCESS = "HELP_WITH_PROCESS"
    DISAGREE_WITH_AUTOMATED_RESULT = "DISAGREE_WITH_AUTOMATED_RESULT"
    OTHER = "OTHER"


class ReviewEvidenceCategory(StrEnum):
    """Permitted kinds of review evidence (synthetic references only in the prototype)."""

    SOURCE_OF_FUNDS_CONFIRMATION = "SOURCE_OF_FUNDS_CONFIRMATION"
    IDENTITY_REVERIFICATION = "IDENTITY_REVERIFICATION"
    ADDRESS_CONFIRMATION = "ADDRESS_CONFIRMATION"
    CUSTOMER_CLARIFICATION = "CUSTOMER_CLARIFICATION"


class ReviewDecision(StrEnum):
    """What an operator may decide. Deliberately NO 'approve', 'pass' or 'open'."""

    RE_EVALUATE = "RE_EVALUATE"  # re-run the interrupted check with the recorded evidence
    RESUME = "RESUME"  # continue a customer-requested pause at the interrupted step
    REJECT = "REJECT"  # business decision REJECTED (supervisor only)
    ACKNOWLEDGE = "ACKNOWLEDGE"  # close a case that did not pause the workflow


class OperatorRole(StrEnum):
    REVIEWER = "REVIEWER"
    SUPERVISOR = "SUPERVISOR"  # may additionally REJECT


class CheckOutcome(StrEnum):
    """Outcome of a (MOCK) KYC / compliance check. Internal only."""

    PASS = "PASS"  # noqa: S105 - a check outcome, not a credential
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAIL = "FAIL"


class CheckType(StrEnum):
    KYC = "KYC"
    COMPLIANCE = "COMPLIANCE"
    CONTRACT = "CONTRACT"
