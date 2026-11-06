"""v4 -> v5: two new tables, existing rows untouched, a newer schema still refused."""

import sqlite3
from pathlib import Path

import pytest

from app.repositories.database import (
    SCHEMA_VERSION,
    SchemaVersionError,
    create_db_engine,
    init_database,
)
from tests.flow import Flow

NEW_TABLES = {"tax_information", "appropriateness_assessments"}


def upgrade(db: Path) -> None:
    engine = create_db_engine(f"sqlite:///{db.as_posix()}")
    init_database(engine)
    engine.dispose()


def test_a_v4_database_gains_the_new_tables_and_keeps_its_data(flow: Flow, db_url: str) -> None:
    started = flow.run_to_opened()
    db = Path(db_url.removeprefix("sqlite:///"))
    before = flow.sql("SELECT * FROM applications"), flow.sql("SELECT * FROM audit_events")
    flow.app.state.engine.dispose()
    with sqlite3.connect(db) as con:  # what a Phase 04 database looks like
        for table in NEW_TABLES:
            con.execute(f"DROP TABLE {table}")
        con.execute("PRAGMA user_version = 4")

    upgrade(db)
    upgrade(db)  # idempotent

    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names >= NEW_TABLES
        for table in NEW_TABLES:
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)  # noqa: S608
        status = con.execute(
            "SELECT status FROM applications WHERE application_id = ?", (started.application_id,)
        ).fetchone()
    assert status == ("OPENED",)
    assert flow.sql("SELECT * FROM applications") == before[0]
    assert flow.sql("SELECT * FROM audit_events") == before[1]


def test_the_tax_table_has_no_column_for_the_identifier(flow: Flow) -> None:
    columns = {row[1] for row in flow.sql("PRAGMA table_info(tax_information)")}
    assert "tax_residency_country" in columns
    assert not {c for c in columns if "tax_id" in c or c == "identifier"}


def test_a_newer_schema_is_still_refused(tmp_path: Path) -> None:
    db = tmp_path / "newer.db"
    upgrade(db)
    with sqlite3.connect(db) as con:
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(SchemaVersionError):
        upgrade(db)
