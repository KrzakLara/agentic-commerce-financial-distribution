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


class MandateScope(StrEnum):
    """Scopes of a short-lived onboarding mandate; mirrors the agent tool set (ADR-09)."""

    DISCOVER = "discover"
    CREATE_DRAFT = "create_draft"
    REQUEST_HANDOFF = "request_handoff"
    READ_STATUS = "read_status"
    SUBMIT_AFTER_CONSENT = "submit_after_consent"


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
    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    CLOSED = "CLOSED"


class ReviewCaseReason(StrEnum):
    COMPLIANCE_REVIEW_REQUIRED = "COMPLIANCE_REVIEW_REQUIRED"
    CUSTOMER_REQUESTED_HUMAN = "CUSTOMER_REQUESTED_HUMAN"


class CheckOutcome(StrEnum):
    """Outcome of a (MOCK) KYC / compliance check. Internal only."""

    PASS = "PASS"  # noqa: S105 - a check outcome, not a credential
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAIL = "FAIL"


class CheckType(StrEnum):
    KYC = "KYC"
    COMPLIANCE = "COMPLIANCE"
    CONTRACT = "CONTRACT"
