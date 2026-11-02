"""SQLAlchemy repositories for the onboarding workflow tables (Phase 02).

Mandates, handoffs, receipts, check evidence and depot submissions. Like the Phase 01
repositories they never commit (the unit of work does) and convert rows to immutable
domain models at this boundary.
"""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.domain.checks import CheckRecord, DepotSubmissionRecord
from app.domain.enums import CheckOutcome, CheckType, MandateScope
from app.domain.handoff import ApprovedAction, Handoff
from app.domain.mandate import Mandate
from app.domain.receipts import ConsentReceipt, DisclosureReceipt
from app.repositories.orm import (
    CheckResultRow,
    ConsentReceiptRow,
    DepotSubmissionRow,
    DisclosureReceiptRow,
    HandoffRow,
    MandateRow,
)


def _rowcount(result: object) -> int:
    return int(result.rowcount)  # type: ignore[attr-defined]


class SqlAlchemyMandateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, mandate: Mandate) -> None:
        self._session.add(
            MandateRow(
                mandate_id=mandate.mandate_id,
                jti=mandate.jti,
                application_id=mandate.application_id,
                agent_actor_id=mandate.agent_actor_id,
                customer_id=mandate.customer_id,
                audience=mandate.audience,
                scopes=sorted(scope.value for scope in mandate.scopes),
                issued_at=mandate.issued_at,
                expires_at=mandate.expires_at,
                revoked_at=mandate.revoked_at,
            )
        )
        self._session.flush()

    def get_for_application(self, application_id: str) -> Mandate | None:
        row = self._session.execute(
            select(MandateRow)
            .where(MandateRow.application_id == application_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None:
            return None
        return Mandate(
            mandate_id=row.mandate_id,
            jti=row.jti,
            application_id=row.application_id,
            agent_actor_id=row.agent_actor_id,
            customer_id=row.customer_id,
            audience=row.audience,
            scopes=frozenset(MandateScope(v) for v in row.scopes),
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )

    def revoke(self, application_id: str, at: datetime) -> bool:
        result = self._session.execute(
            update(MandateRow)
            .where(MandateRow.application_id == application_id, MandateRow.revoked_at.is_(None))
            .values(revoked_at=at)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1


def _handoff_to_domain(row: HandoffRow) -> Handoff:
    return Handoff(
        handoff_id=row.handoff_id,
        application_id=row.application_id,
        nonce_hash=row.nonce_hash,
        action=ApprovedAction.model_validate(row.action),
        action_digest=row.action_digest,
        expires_at=row.expires_at,
        created_at=row.created_at,
        customer_id=row.customer_id,
        consumed_at=row.consumed_at,
        revoked_at=row.revoked_at,
    )


class SqlAlchemyHandoffRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, handoff: Handoff) -> None:
        self._session.add(
            HandoffRow(
                handoff_id=handoff.handoff_id,
                application_id=handoff.application_id,
                nonce_hash=handoff.nonce_hash,
                action=handoff.action.model_dump(),
                action_digest=handoff.action_digest,
                created_at=handoff.created_at,
                expires_at=handoff.expires_at,
                customer_id=handoff.customer_id,
                consumed_at=handoff.consumed_at,
                revoked_at=handoff.revoked_at,
            )
        )
        self._session.flush()

    def get(self, handoff_id: str) -> Handoff | None:
        row = self._session.execute(
            select(HandoffRow)
            .where(HandoffRow.handoff_id == handoff_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        return _handoff_to_domain(row) if row else None

    def revoke_active(self, application_id: str, at: datetime) -> int:
        result = self._session.execute(
            update(HandoffRow)
            .where(
                HandoffRow.application_id == application_id,
                HandoffRow.consumed_at.is_(None),
                HandoffRow.revoked_at.is_(None),
            )
            .values(revoked_at=at)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result)

    def consume(self, handoff_id: str, customer_id: str, at: datetime) -> bool:
        result = self._session.execute(
            update(HandoffRow)
            .where(
                HandoffRow.handoff_id == handoff_id,
                HandoffRow.consumed_at.is_(None),
                HandoffRow.revoked_at.is_(None),
            )
            .values(consumed_at=at, customer_id=customer_id)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1


class SqlAlchemyReceiptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_consent(self, receipt: ConsentReceipt) -> None:
        self._session.add(ConsentReceiptRow(**receipt.model_dump()))
        self._session.flush()

    def add_disclosure(self, receipt: DisclosureReceipt) -> None:
        self._session.add(DisclosureReceiptRow(**receipt.model_dump()))
        self._session.flush()

    def get_consent(self, application_id: str) -> ConsentReceipt | None:
        row = self._session.execute(
            select(ConsentReceiptRow).where(ConsentReceiptRow.application_id == application_id)
        ).scalar_one_or_none()
        return ConsentReceipt.model_validate(row, from_attributes=True) if row else None

    def get_disclosure(self, application_id: str) -> DisclosureReceipt | None:
        row = self._session.execute(
            select(DisclosureReceiptRow).where(
                DisclosureReceiptRow.application_id == application_id
            )
        ).scalar_one_or_none()
        return DisclosureReceipt.model_validate(row, from_attributes=True) if row else None


class SqlAlchemyCheckRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: CheckRecord) -> None:
        self._session.add(
            CheckResultRow(
                application_id=record.application_id,
                check_type=record.check_type.value,
                outcome=record.outcome.value,
                reference=record.reference,
                mock=record.mock,
                created_at=record.created_at,
            )
        )
        self._session.flush()

    def get(self, application_id: str, check_type: CheckType) -> CheckRecord | None:
        row = self._session.execute(
            select(CheckResultRow).where(
                CheckResultRow.application_id == application_id,
                CheckResultRow.check_type == check_type.value,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return CheckRecord(
            application_id=row.application_id,
            check_type=CheckType(row.check_type),
            outcome=CheckOutcome(row.outcome),
            reference=row.reference,
            mock=row.mock,
            created_at=row.created_at,
        )


class SqlAlchemyDepotSubmissionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _to_domain(row: DepotSubmissionRow) -> DepotSubmissionRecord:
        return DepotSubmissionRecord(
            application_id=row.application_id,
            provider_ref=row.provider_ref,
            submitted_at=row.submitted_at,
            confirmation_id=row.confirmation_id,
            confirmed_at=row.confirmed_at,
        )

    def add(self, record: DepotSubmissionRecord) -> None:
        self._session.add(DepotSubmissionRow(**record.model_dump()))
        self._session.flush()

    def get(self, application_id: str) -> DepotSubmissionRecord | None:
        row = self._session.execute(
            select(DepotSubmissionRow)
            .where(DepotSubmissionRow.application_id == application_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        return self._to_domain(row) if row else None

    def get_by_confirmation_id(self, confirmation_id: str) -> DepotSubmissionRecord | None:
        row = self._session.execute(
            select(DepotSubmissionRow).where(DepotSubmissionRow.confirmation_id == confirmation_id)
        ).scalar_one_or_none()
        return self._to_domain(row) if row else None

    def confirm(self, application_id: str, confirmation_id: str, at: datetime) -> bool:
        result = self._session.execute(
            update(DepotSubmissionRow)
            .where(
                DepotSubmissionRow.application_id == application_id,
                DepotSubmissionRow.confirmed_at.is_(None),
            )
            .values(confirmation_id=confirmation_id, confirmed_at=at)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(result) == 1
