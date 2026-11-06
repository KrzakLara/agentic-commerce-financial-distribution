"""Schema version 6: delegation, identity, contract execution, attribution and policy evidence.

New tables only, so an older database gains them through `create_all` and no row changes. The
evidence tables are append-only by trigger, like the older receipts. That protects against a buggy
service, not against somebody with database access (finding F-6).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.repositories.database import Base
from app.repositories.orm import UtcDateTime


class RegisteredAgentRow(Base):
    """An external agent the bank knows: its public keys and the most it may ever ask for."""

    __tablename__ = "registered_agents"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    jwks: Mapped[dict[str, Any]] = mapped_column(JSON)
    allowed_scopes: Mapped[list[str]] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class DelegationRow(Base):
    """One customer-to-agent delegation, from the agent's pushed request to its revocation."""

    __tablename__ = "delegations"

    delegation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("registered_agents.client_id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(20))
    device_code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    request_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    dpop_jkt: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(JSON)
    authorization_details: Mapped[dict[str, Any]] = mapped_column(JSON)
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    identity_assurance: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    request_expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    token_issued_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class DelegatedMandateRow(Base):
    """Which grants a delegation stands behind. A grant listed here needs a DPoP proof."""

    __tablename__ = "delegated_mandates"

    mandate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mandates.mandate_id", ondelete="RESTRICT"), primary_key=True
    )
    delegation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("delegations.delegation_id", ondelete="RESTRICT"), index=True
    )


class UsedJtiRow(Base):
    """Replay protection: one row per accepted one-time proof, assertion or callback."""

    __tablename__ = "used_jtis"

    namespace: Mapped[str] = mapped_column(String(40), primary_key=True)
    jti: Mapped[str] = mapped_column(String(128), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())


class OidcLoginStateRow(Base):
    """A login in progress. State and browser binding are stored only as hashes."""

    __tablename__ = "oidc_login_states"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_binding_hash: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(128))
    code_verifier: Mapped[str] = mapped_column(String(128))
    signed_in_customer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class IdentityBindingRow(Base):
    """A verified (issuer, subject) and the bank's own pseudonymous customer reference for it."""

    __tablename__ = "identity_bindings"
    __table_args__ = (UniqueConstraint("issuer", "subject_hash", name="uq_identity_subject"),)

    customer_ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(300))
    subject_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class AuthenticationEventRow(Base):
    """Evidence of one authentication. It is authentication only, never identity proofing."""

    __tablename__ = "authentication_events"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_ref: Mapped[str] = mapped_column(String(64), index=True)
    issuer: Mapped[str] = mapped_column(String(300))
    acr: Mapped[str | None] = mapped_column(String(100), nullable=True)
    amr: Mapped[list[str]] = mapped_column(JSON)
    auth_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UtcDateTime())


class IdentityProofingRow(Base):
    """A case at a vendor-neutral identity-proofing provider. No document or biometric data."""

    __tablename__ = "identity_proofing_cases"

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer)
    customer_ref: Mapped[str] = mapped_column(String(64))
    provider_id: Mapped[str] = mapped_column(String(64))
    provider_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    assurance: Mapped[str | None] = mapped_column(String(60), nullable=True)
    regulatory_evidence: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())


class ContractPackageRow(Base):
    """The exact package the customer reviewed. Immutable by trigger."""

    __tablename__ = "contract_packages"

    package_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), unique=True
    )
    customer_ref: Mapped[str] = mapped_column(String(64))
    canonical_json: Mapped[str] = mapped_column(Text)
    package_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class SignatureSessionRow(Base):
    """One signing attempt for one package, one signatory and one provider session."""

    __tablename__ = "signature_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer)
    package_hash: Mapped[str] = mapped_column(String(64))
    signer_ref: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))
    provider_id: Mapped[str] = mapped_column(String(64))
    provider_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    level: Mapped[str] = mapped_column(String(40))
    legal_effect: Mapped[bool] = mapped_column(Boolean, default=False)
    authorized_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)


class PartnerRelationshipRow(Base):
    """A synthetic, verified existing customer-to-partner relationship. Never agent-supplied."""

    __tablename__ = "partner_relationships"

    customer_ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    partner_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    verified_source: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class AttributionClaimRow(Base):
    """Every claim as it arrived, accepted or not. Append-only."""

    __tablename__ = "attribution_claims"

    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), index=True
    )
    partner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(60))
    proof_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    terms_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime())


class AttributionDecisionRow(Base):
    """A deterministic decision with its inputs. A later decision never overwrites this one."""

    __tablename__ = "attribution_decisions"
    __table_args__ = (UniqueConstraint("application_id", "sequence", name="uq_attribution_seq"),)

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(40))
    partner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(40))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON)
    explanation: Mapped[list[str]] = mapped_column(JSON)
    commission: Mapped[dict[str, Any]] = mapped_column(JSON)
    decided_by: Mapped[str] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(UtcDateTime())


class PolicyDecisionRow(Base):
    """A regulatory-policy evaluation: which version decided what, with which reasons."""

    __tablename__ = "policy_decisions"

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), index=True
    )
    stage: Mapped[str] = mapped_column(String(30))
    service_context: Mapped[str] = mapped_column(String(40))
    policy_version: Mapped[str] = mapped_column(String(40))
    allowed: Mapped[bool] = mapped_column(Boolean)
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    decided_at: Mapped[datetime] = mapped_column(UtcDateTime())


class A2aTaskRow(Base):
    """An A2A task. Never holds a token, a one-time link or customer data."""

    __tablename__ = "a2a_tasks"
    __table_args__ = (Index("ix_a2a_tasks_owner", "owner"),)

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    context_id: Mapped[str] = mapped_column(String(64))
    owner: Mapped[str] = mapped_column(String(64))
    skill: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(40))
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


def _append_only(table: Any, name: str) -> None:  # noqa: ANN401 - a SQLAlchemy table
    for operation in ("UPDATE", "DELETE"):
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER IF NOT EXISTS trg_{name}_no_{operation.lower()} "
                f"BEFORE {operation} ON {name} "
                f"BEGIN SELECT RAISE(ABORT, '{name} is append-only'); END"
            ).execute_if(dialect="sqlite"),
        )


for _row in (
    ContractPackageRow,
    AttributionClaimRow,
    AttributionDecisionRow,
    PolicyDecisionRow,
    AuthenticationEventRow,
):
    _append_only(_row.__table__, _row.__tablename__)
