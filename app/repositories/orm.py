"""ORM (table) models. Internal to the persistence layer: never returned from the API.

Integrity is enforced in the database itself as well as in code:

* CHECK constraints on status values and on the `resume_status` / `version` invariants.
* UNIQUE (actor_id, operation, idempotency_key) makes duplicate creation impossible even
  under concurrent writers.
* Triggers (defence in depth, SQLite): new applications must start as DRAFT; OPENED can only
  be reached from SUBMITTED_TO_DEPOT_PROVIDER; terminal states are frozen; audit rows can
  never be updated or deleted (append-only).
* Phase 02: at most one *active* handoff per application (partial unique index); receipts and
  check results are append-only; a revoked mandate stays revoked; one consent per handoff.
* Phase 03: mandates of three kinds (bootstrap / onboarding / status), at most one per kind and
  application; their claims are immutable and a bootstrap's application binding is set once.
  Agent access tokens and customer sessions are stored as SHA-256 hashes only, with
  immutable claims and final revocation.
* Phase 04: check results are numbered attempts (a human-triggered re-evaluation appends a new
  attempt, never edits an old one); at most one ACTIVE review case per application and a
  closed case never reopens; the channel attribution is append-only; an application's
  provider, product, channel, customer and owner can never change after creation.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.domain.catalog import ChannelKind
from app.domain.enums import (
    ApplicationStatus,
    CheckOutcome,
    CheckType,
    MandateKind,
    ReviewCaseReason,
    ReviewCaseStatus,
)
from app.domain.workflow import RESUMABLE_STATES, TERMINAL_STATES
from app.repositories.database import Base


class UtcDateTime(TypeDecorator[datetime]):
    """Stores timezone-aware datetimes as naive UTC (SQLite has no tz) and restores them aware."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime rejected; use timezone-aware values")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def _sql_list(values: Any) -> str:
    return ", ".join(f"'{v.value}'" for v in sorted(values, key=lambda v: v.value))


_ALL_STATUSES = _sql_list(ApplicationStatus)
_PARKED = _sql_list(RESUMABLE_STATES)
_TERMINAL = _sql_list(TERMINAL_STATES)
_CHECK_TYPES = _sql_list(CheckType)
_CHECK_OUTCOMES = _sql_list(CheckOutcome)
_MANDATE_KINDS = _sql_list(MandateKind)
_CASE_STATUSES = _sql_list(ReviewCaseStatus)
_CASE_REASONS = _sql_list(ReviewCaseReason)
_CHANNEL_KINDS = _sql_list(ChannelKind)

# Created by the ORM for new databases AND by the schema upgrade for existing ones.
APPLICATION_IDENTITY_TRIGGER = """
    CREATE TRIGGER IF NOT EXISTS trg_applications_identity_is_final
    BEFORE UPDATE OF application_id, provider_id, product_id, channel_id, customer_ref,
                     agent_actor_id, created_at ON applications
    BEGIN SELECT RAISE(ABORT, 'provider, product, channel and parties of an application are final');
    END
    """


class ApplicationRow(Base):
    __tablename__ = "applications"

    application_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64))
    product_id: Mapped[str] = mapped_column(String(64))
    channel_id: Mapped[str] = mapped_column(String(64))
    customer_ref: Mapped[str] = mapped_column(String(64))
    agent_actor_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    resume_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())

    __table_args__ = (
        CheckConstraint(f"status IN ({_ALL_STATUSES})", name="ck_applications_status"),
        CheckConstraint(
            f"resume_status IS NULL OR resume_status IN ({_ALL_STATUSES})",
            name="ck_applications_resume_status",
        ),
        CheckConstraint(
            f"(status IN ({_PARKED})) = (resume_status IS NOT NULL)",
            name="ck_applications_resume_only_when_parked",
        ),
        CheckConstraint("version >= 1", name="ck_applications_version"),
    )


class IdempotencyRecordRow(Base):
    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(100))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())

    __table_args__ = (
        UniqueConstraint(
            "actor_id", "operation", "idempotency_key", name="uq_idempotency_actor_operation_key"
        ),
        UniqueConstraint("application_id", name="uq_idempotency_application"),
    )


class AuditEventRow(Base):
    """Append-only audit log. `seq` gives a stable total order of events."""

    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime())
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    application_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(20))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (Index("ix_audit_events_application_seq", "application_id", "seq"),)


class MandateRow(Base):
    """Authorization grant (mock claims): bootstrap, onboarding or status - see `MandateKind`.

    A bootstrap grant has no application until it creates one; every other kind is bound to
    its application from the start. At most one grant of each kind per application.
    """

    __tablename__ = "mandates"

    mandate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    application_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), nullable=True
    )
    agent_actor_id: Mapped[str] = mapped_column(String(64))
    customer_id: Mapped[str] = mapped_column(String(64))
    issuer: Mapped[str] = mapped_column(String(64))
    audience: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(JSON)
    issued_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    __table_args__ = (
        # NULL application ids (unused bootstrap grants) never collide in SQLite.
        UniqueConstraint("application_id", "kind", name="uq_mandates_application_kind"),
        CheckConstraint(f"kind IN ({_MANDATE_KINDS})", name="ck_mandates_kind"),
        CheckConstraint(
            "kind = 'bootstrap' OR application_id IS NOT NULL", name="ck_mandates_bound"
        ),
        CheckConstraint("expires_at > issued_at", name="ck_mandates_expiry"),
    )


class AgentAccessTokenRow(Base):
    """Opaque agent bearer token. Only the SHA-256 hash is stored, never the token itself."""

    __tablename__ = "agent_access_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    mandate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("mandates.mandate_id", ondelete="RESTRICT"), index=True
    )
    issuer: Mapped[str] = mapped_column(String(64))
    audience: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]] = mapped_column(JSON)
    issued_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint("length(token_hash) = 64", name="ck_agent_access_tokens_hash"),
        CheckConstraint("expires_at > issued_at", name="ck_agent_access_tokens_expiry"),
    )


class CustomerSessionRow(Base):
    """Server-side customer session (DEV-simulated sign-in). Secret stored as SHA-256 only."""

    __tablename__ = "customer_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint("length(secret_hash) = 64", name="ck_customer_sessions_hash"),
        CheckConstraint("expires_at > created_at", name="ck_customer_sessions_expiry"),
    )


class HandoffRow(Base):
    __tablename__ = "handoffs"

    handoff_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), index=True
    )
    nonce_hash: Mapped[str] = mapped_column(String(64))  # SHA-256 of the nonce, never the nonce
    action: Mapped[dict[str, Any]] = mapped_column(JSON)
    action_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    __table_args__ = (
        # Database-level guarantee: never two usable handoffs for one application.
        Index(
            "uq_handoffs_one_active_per_application",
            "application_id",
            unique=True,
            sqlite_where=text("consumed_at IS NULL AND revoked_at IS NULL"),
        ),
        CheckConstraint(
            "(consumed_at IS NULL) = (customer_id IS NULL)", name="ck_handoffs_bound_when_consumed"
        ),
    )


class ConsentReceiptRow(Base):
    __tablename__ = "consent_receipts"

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), index=True
    )
    customer_id: Mapped[str] = mapped_column(String(64))
    handoff_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("handoffs.handoff_id", ondelete="RESTRICT"), unique=True
    )
    action_digest: Mapped[str] = mapped_column(String(64))
    confirmed_at: Mapped[datetime] = mapped_column(UtcDateTime())


class DisclosureReceiptRow(Base):
    __tablename__ = "disclosure_receipts"

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), index=True
    )
    customer_id: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[str] = mapped_column(String(64))
    document_version: Mapped[str] = mapped_column(String(200))
    document_digest: Mapped[str] = mapped_column(String(64))
    acknowledged_at: Mapped[datetime] = mapped_column(UtcDateTime())
    # Phase 04 binding (NULL on receipts written before Phase 04).
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_version: Mapped[str | None] = mapped_column(String(200), nullable=True)
    terms_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


class CheckResultRow(Base):
    """Internal evidence that a mandatory check ran. Never exposed to the agent.

    One row per ATTEMPT: a re-evaluation after human review appends attempt n+1.
    """

    __tablename__ = "check_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT")
    )
    check_type: Mapped[str] = mapped_column(String(20))
    outcome: Mapped[str] = mapped_column(String(20))
    reference: Mapped[str] = mapped_column(String(200))
    mock: Mapped[bool] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    attempt: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        UniqueConstraint(
            "application_id", "check_type", "attempt", name="uq_check_results_attempt"
        ),
        CheckConstraint(f"check_type IN ({_CHECK_TYPES})", name="ck_check_results_type"),
        CheckConstraint(f"outcome IN ({_CHECK_OUTCOMES})", name="ck_check_results_outcome"),
        CheckConstraint("attempt >= 1", name="ck_check_results_attempt"),
    )


class ReviewCaseRow(Base):
    """Human-review case (Phase 04). Internal: never returned to the agent."""

    __tablename__ = "review_cases"

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), index=True
    )
    reason: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30))
    check_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    check_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    interrupted_step: Mapped[str | None] = mapped_column(String(40), nullable=True)
    opened_by: Mapped[str] = mapped_column(String(20))
    requested_information: Mapped[str | None] = mapped_column(String(40), nullable=True)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())
    version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        # Database-level guarantee: never two active review cases for one application.
        Index(
            "uq_review_cases_one_active_per_application",
            "application_id",
            unique=True,
            sqlite_where=text("status != 'CLOSED'"),
        ),
        CheckConstraint(f"status IN ({_CASE_STATUSES})", name="ck_review_cases_status"),
        CheckConstraint(f"reason IN ({_CASE_REASONS})", name="ck_review_cases_reason"),
        CheckConstraint(
            "(status = 'CLOSED') = (resolution IS NOT NULL)", name="ck_review_cases_resolution"
        ),
        CheckConstraint("version >= 1", name="ck_review_cases_version"),
    )


class ChannelAttributionRow(Base):
    """Verified distribution attribution, recorded with the customer's consent (append-only)."""

    __tablename__ = "channel_attributions"

    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), primary_key=True
    )
    provider_id: Mapped[str] = mapped_column(String(64))
    channel_id: Mapped[str] = mapped_column(String(64))
    channel_kind: Mapped[str] = mapped_column(String(20))
    partner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    registry_version: Mapped[str] = mapped_column(String(64))
    consent_receipt_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("consent_receipts.receipt_id", ondelete="RESTRICT"), unique=True
    )
    recorded_at: Mapped[datetime] = mapped_column(UtcDateTime())

    __table_args__ = (
        CheckConstraint(f"channel_kind IN ({_CHANNEL_KINDS})", name="ck_attributions_kind"),
        CheckConstraint(
            "(channel_kind = 'PARTNER_BANK') = (partner_id IS NOT NULL)",
            name="ck_attributions_partner",
        ),
    )


class DepotSubmissionRow(Base):
    __tablename__ = "depot_submissions"

    application_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("applications.application_id", ondelete="RESTRICT"), primary_key=True
    )
    provider_ref: Mapped[str] = mapped_column(String(64))
    submitted_at: Mapped[datetime] = mapped_column(UtcDateTime())
    confirmation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


def _trigger(table: Any, ddl: str) -> None:
    event.listen(table, "after_create", DDL(ddl).execute_if(dialect="sqlite"))


_trigger(
    ApplicationRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_applications_insert_only_draft
    BEFORE INSERT ON applications
    WHEN NEW.status != 'DRAFT'
    BEGIN SELECT RAISE(ABORT, 'new applications must start as DRAFT'); END
    """,
)
_trigger(
    ApplicationRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_applications_opened_only_from_submitted
    BEFORE UPDATE OF status ON applications
    WHEN NEW.status = 'OPENED' AND OLD.status != 'SUBMITTED_TO_DEPOT_PROVIDER'
    BEGIN SELECT RAISE(ABORT, 'OPENED is only reachable from SUBMITTED_TO_DEPOT_PROVIDER'); END
    """,
)
_trigger(
    ApplicationRow.__table__,
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_applications_terminal_frozen
    BEFORE UPDATE OF status ON applications
    WHEN OLD.status IN ({_TERMINAL}) AND NEW.status != OLD.status
    BEGIN SELECT RAISE(ABORT, 'terminal states cannot change'); END
    """,
)
_trigger(ApplicationRow.__table__, APPLICATION_IDENTITY_TRIGGER)
_trigger(
    AuditEventRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END
    """,
)
_trigger(
    AuditEventRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END
    """,
)


for _table, _name in (
    (ConsentReceiptRow.__table__, "consent_receipts"),
    (DisclosureReceiptRow.__table__, "disclosure_receipts"),
    (CheckResultRow.__table__, "check_results"),
    (ChannelAttributionRow.__table__, "channel_attributions"),
):
    for _op in ("UPDATE", "DELETE"):
        _trigger(
            _table,
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{_name}_no_{_op.lower()}
            BEFORE {_op} ON {_name}
            BEGIN SELECT RAISE(ABORT, '{_name} is append-only'); END
            """,
        )
_trigger(
    MandateRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mandates_revocation_is_final
    BEFORE UPDATE OF revoked_at ON mandates
    WHEN OLD.revoked_at IS NOT NULL
         AND (NEW.revoked_at IS NULL OR NEW.revoked_at != OLD.revoked_at)
    BEGIN SELECT RAISE(ABORT, 'a revoked mandate stays revoked'); END
    """,
)
# Phase 03: nobody - not even a buggy service - can widen a grant after it was issued.
_trigger(
    MandateRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mandates_claims_are_immutable
    BEFORE UPDATE OF mandate_id, jti, kind, agent_actor_id, customer_id, issuer, audience,
                     scopes, issued_at, expires_at ON mandates
    BEGIN SELECT RAISE(ABORT, 'mandate claims are immutable'); END
    """,
)
_trigger(
    MandateRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_mandates_binding_is_final
    BEFORE UPDATE OF application_id ON mandates
    WHEN OLD.application_id IS NOT NULL OR OLD.kind != 'bootstrap' OR OLD.revoked_at IS NOT NULL
         OR NEW.application_id IS NULL
    BEGIN SELECT RAISE(ABORT, 'only an unused bootstrap mandate can be bound, once'); END
    """,
)
for _table, _name, _label, _claims in (
    (
        AgentAccessTokenRow.__table__,
        "agent_access_tokens",
        "access token",
        "jti, token_hash, mandate_id, issuer, audience, scopes, issued_at, expires_at",
    ),
    (
        CustomerSessionRow.__table__,
        "customer_sessions",
        "customer session",
        "session_id, secret_hash, customer_id, created_at, expires_at",
    ),
):
    _trigger(
        _table,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_{_name}_revocation_is_final
        BEFORE UPDATE OF revoked_at ON {_name}
        WHEN OLD.revoked_at IS NOT NULL
             AND (NEW.revoked_at IS NULL OR NEW.revoked_at != OLD.revoked_at)
        BEGIN SELECT RAISE(ABORT, 'a revoked {_label} stays revoked'); END
        """,
    )
    _trigger(
        _table,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_{_name}_claims_are_immutable
        BEFORE UPDATE OF {_claims} ON {_name}
        BEGIN SELECT RAISE(ABORT, '{_label} claims are immutable'); END
        """,
    )
_trigger(
    ReviewCaseRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_review_cases_closed_is_final
    BEFORE UPDATE ON review_cases
    WHEN OLD.status = 'CLOSED'
    BEGIN SELECT RAISE(ABORT, 'a closed review case never changes'); END
    """,
)
_trigger(
    ReviewCaseRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_review_cases_facts_are_immutable
    BEFORE UPDATE OF case_id, application_id, reason, check_type, check_attempt,
                     request_category, interrupted_step, opened_by, created_at ON review_cases
    BEGIN SELECT RAISE(ABORT, 'why and where a review case was opened is immutable'); END
    """,
)
_trigger(
    ReviewCaseRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_review_cases_no_delete
    BEFORE DELETE ON review_cases
    BEGIN SELECT RAISE(ABORT, 'review cases are never deleted'); END
    """,
)
_trigger(
    DepotSubmissionRow.__table__,
    """
    CREATE TRIGGER IF NOT EXISTS trg_depot_submissions_confirm_once
    BEFORE UPDATE OF confirmed_at, confirmation_id, provider_ref ON depot_submissions
    WHEN OLD.confirmed_at IS NOT NULL
    BEGIN SELECT RAISE(ABORT, 'a confirmed depot submission cannot change'); END
    """,
)
