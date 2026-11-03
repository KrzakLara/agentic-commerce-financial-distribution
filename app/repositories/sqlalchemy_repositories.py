"""Concrete SQLAlchemy repositories and unit of work.

ORM rows are converted to immutable domain models at this boundary; ORM objects never leave
the persistence layer.
"""

from datetime import datetime
from types import TracebackType
from typing import Self

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.domain.application import DepotApplication
from app.domain.audit import AuditEvent
from app.domain.enums import ApplicationStatus, AuditActorType, AuditOutcome
from app.domain.idempotency import IdempotencyRecord
from app.repositories.orm import ApplicationRow, AuditEventRow, IdempotencyRecordRow
from app.repositories.workflow_repositories import (
    SqlAlchemyAgentTokenRepository,
    SqlAlchemyChannelAttributionRepository,
    SqlAlchemyCheckRepository,
    SqlAlchemyCustomerSessionRepository,
    SqlAlchemyDepotSubmissionRepository,
    SqlAlchemyHandoffRepository,
    SqlAlchemyMandateRepository,
    SqlAlchemyReceiptRepository,
    SqlAlchemyReviewCaseRepository,
)


def _application_to_domain(row: ApplicationRow) -> DepotApplication:
    return DepotApplication(
        application_id=row.application_id,
        provider_id=row.provider_id,
        product_id=row.product_id,
        channel_id=row.channel_id,
        customer_ref=row.customer_ref,
        agent_actor_id=row.agent_actor_id,
        status=ApplicationStatus(row.status),
        resume_status=ApplicationStatus(row.resume_status) if row.resume_status else None,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyApplicationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, application: DepotApplication) -> None:
        self._session.add(
            ApplicationRow(
                application_id=application.application_id,
                provider_id=application.provider_id,
                product_id=application.product_id,
                channel_id=application.channel_id,
                customer_ref=application.customer_ref,
                agent_actor_id=application.agent_actor_id,
                status=application.status.value,
                resume_status=application.resume_status.value
                if application.resume_status
                else None,
                version=application.version,
                created_at=application.created_at,
                updated_at=application.updated_at,
            )
        )
        self._session.flush()  # surface constraint violations now, inside the caller's try

    def get(self, application_id: str) -> DepotApplication | None:
        # populate_existing: never serve a stale identity-map row after a compare-and-set UPDATE.
        row = self._session.execute(
            select(ApplicationRow)
            .where(ApplicationRow.application_id == application_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        return _application_to_domain(row) if row else None

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
        result = self._session.execute(
            update(ApplicationRow)
            .where(
                ApplicationRow.application_id == application_id,
                ApplicationRow.version == expected_version,
                ApplicationRow.status == from_status.value,
            )
            .values(
                status=to_status.value,
                resume_status=resume_status.value if resume_status else None,
                version=expected_version + 1,
                updated_at=updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1  # type: ignore[attr-defined]


class SqlAlchemyIdempotencyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: IdempotencyRecord) -> None:
        self._session.add(
            IdempotencyRecordRow(
                actor_id=record.actor_id,
                operation=record.operation,
                idempotency_key=record.idempotency_key,
                request_hash=record.request_hash,
                application_id=record.application_id,
                created_at=record.created_at,
            )
        )
        self._session.flush()

    def get(self, actor_id: str, operation: str, idempotency_key: str) -> IdempotencyRecord | None:
        row = self._session.execute(
            select(IdempotencyRecordRow).where(
                IdempotencyRecordRow.actor_id == actor_id,
                IdempotencyRecordRow.operation == operation,
                IdempotencyRecordRow.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return IdempotencyRecord(
            actor_id=row.actor_id,
            operation=row.operation,
            idempotency_key=row.idempotency_key,
            request_hash=row.request_hash,
            application_id=row.application_id,
            created_at=row.created_at,
        )


class SqlAlchemyAuditRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, event: AuditEvent) -> None:
        self._session.add(
            AuditEventRow(
                event_id=event.event_id,
                occurred_at=event.occurred_at,
                correlation_id=event.correlation_id,
                event_type=event.event_type,
                actor_type=event.actor_type.value,
                actor_id=event.actor_id,
                application_id=event.application_id,
                outcome=event.outcome.value,
                details=dict(event.details),
            )
        )
        self._session.flush()

    def list_for_application(self, application_id: str) -> list[AuditEvent]:
        rows = self._session.execute(
            select(AuditEventRow)
            .where(AuditEventRow.application_id == application_id)
            .order_by(AuditEventRow.seq)
        ).scalars()
        return [
            AuditEvent(
                event_id=r.event_id,
                occurred_at=r.occurred_at,
                correlation_id=r.correlation_id,
                event_type=r.event_type,
                actor_type=AuditActorType(r.actor_type),
                actor_id=r.actor_id,
                application_id=r.application_id,
                outcome=AuditOutcome(r.outcome),
                details=r.details,
            )
            for r in rows
        ]


class SqlAlchemyUnitOfWork:
    """One database transaction. Use as a context manager; uncommitted work is rolled back."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        self.applications = SqlAlchemyApplicationRepository(self._session)
        self.idempotency = SqlAlchemyIdempotencyRepository(self._session)
        self.audit = SqlAlchemyAuditRepository(self._session)
        self.mandates = SqlAlchemyMandateRepository(self._session)
        self.agent_tokens = SqlAlchemyAgentTokenRepository(self._session)
        self.customer_sessions = SqlAlchemyCustomerSessionRepository(self._session)
        self.handoffs = SqlAlchemyHandoffRepository(self._session)
        self.receipts = SqlAlchemyReceiptRepository(self._session)
        self.checks = SqlAlchemyCheckRepository(self._session)
        self.review_cases = SqlAlchemyReviewCaseRepository(self._session)
        self.attributions = SqlAlchemyChannelAttributionRepository(self._session)
        self.depot_submissions = SqlAlchemyDepotSubmissionRepository(self._session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self._session.rollback()  # no-op after a successful commit
        finally:
            self._session.close()

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()
