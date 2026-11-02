"""SQLite persistence: constraints, triggers, restart, transactions."""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from app.domain.application import DepotApplication
from app.domain.audit import AuditEvent
from app.domain.enums import ApplicationStatus as S
from app.domain.enums import AuditActorType, AuditOutcome
from app.domain.idempotency import IdempotencyRecord
from app.repositories.database import create_db_engine, create_session_factory
from app.repositories.interfaces import UnitOfWork
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork

NOW = datetime(2026, 11, 3, 9, 0, 0, 123456, tzinfo=UTC)
DIGEST = "c" * 64


def make_application(application_id: str = "app-1", **overrides: object) -> DepotApplication:
    values: dict[str, object] = {
        "application_id": application_id,
        "provider_id": "fixture-bank",
        "product_id": "depot-basic",
        "channel_id": "direct",
        "customer_ref": "opaque-id",
        "agent_actor_id": "agent-a",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return DepotApplication(**values)  # type: ignore[arg-type]


def make_event(application_id: str | None = "app-1", event_id: str = "evt-1") -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        occurred_at=NOW,
        correlation_id="corr.1",
        event_type="application.created",
        actor_type=AuditActorType.AGENT,
        actor_id="agent-a",
        application_id=application_id,
        outcome=AuditOutcome.SUCCESS,
        details={"k": "v"},
    )


def scalar(engine: Engine, sql: str) -> object:
    with engine.connect() as connection:
        return connection.execute(text(sql)).scalar_one()


def raw(engine: Engine, sql: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql))


# --- repositories round-trip ------------------------------------------------------------


def test_application_round_trips_with_timezone_and_microseconds(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    with uow_factory() as uow:
        uow.applications.add(make_application())
        uow.commit()
    with uow_factory() as uow:
        loaded = uow.applications.get("app-1")
    assert loaded == make_application()
    assert loaded is not None and loaded.created_at.tzinfo is not None
    assert loaded.created_at == NOW


def test_unknown_application_returns_none(uow_factory: Callable[[], UnitOfWork]) -> None:
    with uow_factory() as uow:
        assert uow.applications.get("nope") is None


def test_uncommitted_work_is_rolled_back(uow_factory: Callable[[], UnitOfWork]) -> None:
    with uow_factory() as uow:
        uow.applications.add(make_application())  # flushed but never committed
    with uow_factory() as uow:
        assert uow.applications.get("app-1") is None


def test_exception_inside_unit_of_work_rolls_back(uow_factory: Callable[[], UnitOfWork]) -> None:
    with pytest.raises(RuntimeError), uow_factory() as uow:
        uow.applications.add(make_application())
        raise RuntimeError("boom")
    with uow_factory() as uow:
        assert uow.applications.get("app-1") is None


def test_update_status_is_compare_and_set(uow_factory: Callable[[], UnitOfWork]) -> None:
    with uow_factory() as uow:
        uow.applications.add(make_application())
        uow.commit()
    args = {"from_status": S.DRAFT, "to_status": S.AWAITING_AUTHORIZATION, "resume_status": None}
    with uow_factory() as uow:
        assert uow.applications.update_status("app-1", expected_version=1, updated_at=NOW, **args)
        uow.commit()
    with uow_factory() as uow:
        # a second writer holding the stale version/status must lose
        assert not uow.applications.update_status(
            "app-1", expected_version=1, updated_at=NOW, **args
        )
        current = uow.applications.get("app-1")
    assert current is not None
    assert (current.status, current.version) == (S.AWAITING_AUTHORIZATION, 2)


# --- constraints ------------------------------------------------------------


def test_duplicate_application_id_is_rejected(uow_factory: Callable[[], UnitOfWork]) -> None:
    with uow_factory() as uow:
        uow.applications.add(make_application())
        uow.commit()
    with pytest.raises(IntegrityError), uow_factory() as uow:
        uow.applications.add(make_application())


def test_idempotency_key_is_unique_per_actor_operation_key(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    def record(application_id: str, actor: str = "agent-a") -> IdempotencyRecord:
        return IdempotencyRecord(
            actor_id=actor,
            operation="create_application",
            idempotency_key="k-1",
            request_hash=DIGEST,
            application_id=application_id,
            created_at=NOW,
        )

    with uow_factory() as uow:
        for app_id in ("app-1", "app-2", "app-3"):
            uow.applications.add(make_application(app_id))
        uow.idempotency.add(record("app-1"))
        uow.idempotency.add(record("app-2", actor="agent-b"))  # another actor: allowed
        uow.commit()
    with pytest.raises(IntegrityError), uow_factory() as uow:
        uow.idempotency.add(record("app-3"))  # same actor + operation + key


def test_idempotency_record_requires_an_existing_application(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    with pytest.raises(IntegrityError), uow_factory() as uow:
        uow.idempotency.add(
            IdempotencyRecord(
                actor_id="agent-a",
                operation="create_application",
                idempotency_key="k-1",
                request_hash=DIGEST,
                application_id="missing",
                created_at=NOW,
            )
        )


def test_check_constraints_reject_invalid_rows(engine: Engine) -> None:
    base = (
        "INSERT INTO applications (application_id, provider_id, product_id, channel_id, "
        "customer_ref, agent_actor_id, status, resume_status, version, created_at, updated_at) "
        "VALUES ('x', 'p', 'p', 'c', 'c', 'a', 'DRAFT', {resume}, {version}, "
        "'2026-11-03 09:00:00', '2026-11-03 09:00:00')"
    )
    with pytest.raises(IntegrityError):
        raw(engine, base.format(resume="NULL", version=0))  # version >= 1
    with pytest.raises(IntegrityError):
        raw(engine, base.format(resume="'KYC_PENDING'", version=1))  # resume only when parked


# --- triggers (defence in depth) ------------------------------------------------------------


def test_database_refuses_new_applications_that_do_not_start_as_draft(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="start as DRAFT"):
        raw(
            engine,
            "INSERT INTO applications (application_id, provider_id, product_id, channel_id, "
            "customer_ref, agent_actor_id, status, version, created_at, updated_at) VALUES "
            "('x', 'p', 'p', 'c', 'c', 'a', 'OPENED', 1, "
            "'2026-11-03 09:00:00', '2026-11-03 09:00:00')",
        )


def test_database_refuses_direct_opened_update_even_from_raw_sql(
    engine: Engine, uow_factory: Callable[[], UnitOfWork]
) -> None:
    with uow_factory() as uow:
        uow.applications.add(make_application())
        uow.commit()
    with pytest.raises(IntegrityError, match="OPENED is only reachable"):
        raw(engine, "UPDATE applications SET status = 'OPENED' WHERE application_id = 'app-1'")
    assert (
        scalar(engine, "SELECT status FROM applications WHERE application_id = 'app-1'") == "DRAFT"
    )


def test_database_allows_opened_from_submitted_and_then_freezes_it(
    engine: Engine, uow_factory: Callable[[], UnitOfWork]
) -> None:
    with uow_factory() as uow:
        uow.applications.add(make_application())
        uow.commit()
    # Reach SUBMITTED_TO_DEPOT_PROVIDER, then OPENED (the trigger only guards the OPENED edge).
    raw(
        engine,
        "UPDATE applications SET status='SUBMITTED_TO_DEPOT_PROVIDER' WHERE application_id='app-1'",
    )
    raw(engine, "UPDATE applications SET status='OPENED' WHERE application_id='app-1'")
    with pytest.raises(IntegrityError, match="terminal states cannot change"):
        raw(engine, "UPDATE applications SET status='DRAFT' WHERE application_id='app-1'")


def test_audit_events_are_append_only_at_database_level(
    engine: Engine, uow_factory: Callable[[], UnitOfWork]
) -> None:
    with uow_factory() as uow:
        uow.applications.add(make_application())
        uow.audit.append(make_event())
        uow.commit()
    with pytest.raises(IntegrityError, match="append-only"):
        raw(engine, "UPDATE audit_events SET outcome = 'success', event_type = 'tampered'")
    with pytest.raises(IntegrityError, match="append-only"):
        raw(engine, "DELETE FROM audit_events")
    assert scalar(engine, "SELECT COUNT(*) FROM audit_events") == 1


def test_audit_events_round_trip_in_insertion_order(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    with uow_factory() as uow:
        uow.applications.add(make_application())
        for i in range(3):
            uow.audit.append(make_event(event_id=f"evt-{i}"))
        uow.commit()
    with uow_factory() as uow:
        events = uow.audit.list_for_application("app-1")
    assert [e.event_id for e in events] == ["evt-0", "evt-1", "evt-2"]
    assert events[0].details == {"k": "v"}
    assert events[0].occurred_at == NOW
    assert not hasattr(uow.audit, "update") and not hasattr(uow.audit, "delete")


# --- restart ------------------------------------------------------------


def test_data_survives_a_new_engine_on_the_same_file(db_url: str, engine: Engine) -> None:
    with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
        uow.applications.add(make_application())
        uow.audit.append(make_event())
        uow.commit()
    engine.dispose()  # "stop the process"

    restarted = create_db_engine(db_url)  # "start it again"
    try:
        with SqlAlchemyUnitOfWork(create_session_factory(restarted)) as uow:
            assert uow.applications.get("app-1") == make_application()
            assert [e.event_id for e in uow.audit.list_for_application("app-1")] == ["evt-1"]
    finally:
        restarted.dispose()


def test_naive_datetimes_are_refused_by_the_storage_layer(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    from app.repositories.orm import UtcDateTime

    with pytest.raises(ValueError, match="naive datetime"):
        UtcDateTime().process_bind_param(datetime(2026, 11, 3), None)  # type: ignore[arg-type]
