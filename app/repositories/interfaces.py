"""Repository and unit-of-work interfaces. Services depend on these, never on SQLAlchemy.

Repositories never commit. The unit of work owns the transaction, so a business operation
(e.g. status change + audit event) is atomic.
"""

from datetime import datetime
from types import TracebackType
from typing import Protocol, Self

from app.domain.application import DepotApplication
from app.domain.audit import AuditEvent
from app.domain.checks import CheckRecord, DepotSubmissionRecord
from app.domain.enums import ApplicationStatus, CheckType
from app.domain.handoff import Handoff
from app.domain.idempotency import IdempotencyRecord
from app.domain.mandate import Mandate
from app.domain.receipts import ConsentReceipt, DisclosureReceipt


class ApplicationRepository(Protocol):
    def add(self, application: DepotApplication) -> None:
        """Insert a new application. Raises `sqlalchemy.exc.IntegrityError` on a duplicate id."""
        ...

    def get(self, application_id: str) -> DepotApplication | None: ...

    def update_status(
        self,
        application_id: str,
        *,
        expected_version: int,
        from_status: ApplicationStatus,
        to_status: ApplicationStatus,
        resume_status: ApplicationStatus | None,
        updated_at: datetime,
    ) -> bool:
        """Compare-and-set the status. Returns False if the row no longer matches
        (`expected_version` / `from_status`), i.e. another writer got there first."""
        ...


class IdempotencyRepository(Protocol):
    def add(self, record: IdempotencyRecord) -> None:
        """Insert a record. Raises `IntegrityError` if (actor, operation, key) already exists."""
        ...

    def get(
        self, actor_id: str, operation: str, idempotency_key: str
    ) -> IdempotencyRecord | None: ...


class AuditRepository(Protocol):
    """Append-only: there is deliberately no update or delete method."""

    def append(self, event: AuditEvent) -> None: ...

    def list_for_application(self, application_id: str) -> list[AuditEvent]: ...


class MandateRepository(Protocol):
    def add(self, mandate: Mandate) -> None: ...

    def get_for_application(self, application_id: str) -> Mandate | None: ...

    def revoke(self, application_id: str, at: datetime) -> bool:
        """Mark the mandate of the application revoked. False if absent or already revoked."""
        ...


class HandoffRepository(Protocol):
    def add(self, handoff: Handoff) -> None:
        """Insert. Raises `IntegrityError` if the application already has an active handoff."""
        ...

    def get(self, handoff_id: str) -> Handoff | None: ...

    def revoke_active(self, application_id: str, at: datetime) -> int:
        """Supersede every usable handoff of the application; returns how many."""
        ...

    def consume(self, handoff_id: str, customer_id: str, at: datetime) -> bool:
        """Single-use compare-and-set: bind the customer and mark consumed, only if the
        handoff is still unconsumed and unrevoked. False: someone else got there first."""
        ...


class ReceiptRepository(Protocol):
    def add_consent(self, receipt: ConsentReceipt) -> None: ...

    def add_disclosure(self, receipt: DisclosureReceipt) -> None: ...

    def get_consent(self, application_id: str) -> ConsentReceipt | None: ...

    def get_disclosure(self, application_id: str) -> DisclosureReceipt | None: ...


class CheckRepository(Protocol):
    def add(self, record: CheckRecord) -> None:
        """Insert. Raises `IntegrityError` if this check already has a result."""
        ...

    def get(self, application_id: str, check_type: CheckType) -> CheckRecord | None: ...


class DepotSubmissionRepository(Protocol):
    def add(self, record: DepotSubmissionRecord) -> None: ...

    def get(self, application_id: str) -> DepotSubmissionRecord | None: ...

    def get_by_confirmation_id(self, confirmation_id: str) -> DepotSubmissionRecord | None: ...

    def confirm(self, application_id: str, confirmation_id: str, at: datetime) -> bool:
        """Compare-and-set: record the provider confirmation once. False if already done."""
        ...


class UnitOfWork(Protocol):
    applications: ApplicationRepository
    idempotency: IdempotencyRepository
    audit: AuditRepository
    mandates: MandateRepository
    handoffs: HandoffRepository
    receipts: ReceiptRepository
    checks: CheckRepository
    depot_submissions: DepotSubmissionRepository

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Rolls back anything not committed."""
        ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
