from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.repositories.database import (
    SCHEMA_VERSION,
    Base,
    SchemaVersionError,
    create_db_engine,
    create_session_factory,
    init_database,
)


def test_sqlite_engine_and_session_work_in_memory() -> None:
    engine = create_db_engine("sqlite:///:memory:")
    with create_session_factory(engine)() as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    engine.dispose()


def test_creating_an_engine_creates_no_database_file(tmp_path: Path) -> None:
    db_file = tmp_path / "unused.db"
    engine = create_db_engine(f"sqlite:///{db_file.as_posix()}")  # lazy: nothing touched yet
    assert not db_file.exists()
    engine.dispose()


def test_init_database_creates_the_full_schema_and_is_idempotent(tmp_path: Path) -> None:
    db_file = tmp_path / "schema.db"
    engine = create_db_engine(f"sqlite:///{db_file.as_posix()}")
    init_database(engine)
    init_database(engine)  # running twice must be harmless

    tables = set(inspect(engine).get_table_names())
    assert tables == {
        # Phase 01
        "applications", "idempotency_records", "audit_events",
        # Phase 02
        "mandates", "handoffs", "consent_receipts", "disclosure_receipts",
        "check_results", "depot_submissions",
        # Phase 03
        "agent_access_tokens", "customer_sessions",
    }  # fmt: skip
    assert set(Base.metadata.tables) == tables
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA user_version")).scalar_one() == SCHEMA_VERSION
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
    engine.dispose()


def test_a_database_from_a_newer_version_is_refused(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'future.db').as_posix()}")
    with engine.begin() as connection:
        connection.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION + 1}"))
    with pytest.raises(SchemaVersionError):
        init_database(engine)
    engine.dispose()
