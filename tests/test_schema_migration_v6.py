"""Schema version 6 only adds tables: a version 5 database keeps its rows and opens as before."""

import sqlite3
from pathlib import Path

from sqlalchemy import inspect

from app.repositories.database import SCHEMA_VERSION, create_db_engine, init_database
from app.repositories.trust_orm import Base as _TrustBase  # noqa: F401
from tests.flow import INTENT

_NEW_TABLES = {
    "registered_agents",
    "delegations",
    "delegated_mandates",
    "used_jtis",
    "oidc_login_states",
    "identity_bindings",
    "authentication_events",
    "identity_proofing_cases",
    "contract_packages",
    "signature_sessions",
    "partner_relationships",
    "attribution_claims",
    "attribution_decisions",
    "policy_decisions",
    "a2a_tasks",
}


def _as_v5(db: Path) -> None:
    with sqlite3.connect(db) as con:
        for table in _NEW_TABLES:
            con.execute(f"DROP TABLE {table}")  # noqa: S608 - fixed names
        con.execute("PRAGMA user_version = 5")


def test_a_version_5_database_gains_the_tables_and_keeps_its_rows(tmp_path: Path) -> None:
    db = tmp_path / "upgrade.db"
    url = f"sqlite:///{db.as_posix()}"
    engine = create_db_engine(url)
    init_database(engine)
    engine.dispose()
    _as_v5(db)
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO applications (application_id, provider_id, product_id, channel_id, "
            "customer_ref, agent_actor_id, status, version, created_at, updated_at) "
            "VALUES ('app-old', :p, :q, :c, :r, 'agent-a', 'DRAFT', 1, "
            "'2026-11-01 09:00:00.000000', '2026-11-01 09:00:00.000000')",
            {
                "p": INTENT.provider_id,
                "q": INTENT.product_id,
                "c": INTENT.channel_id,
                "r": INTENT.customer_ref,
            },
        )
        before = con.execute("SELECT * FROM applications").fetchall()
    engine = create_db_engine(url)
    init_database(engine)
    assert set(inspect(engine).get_table_names()) >= _NEW_TABLES
    engine.dispose()
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT * FROM applications").fetchall() == before
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6


def test_upgrading_twice_is_harmless(tmp_path: Path) -> None:
    db = tmp_path / "twice.db"
    url = f"sqlite:///{db.as_posix()}"
    for _ in range(2):
        engine = create_db_engine(url)
        init_database(engine)
        engine.dispose()
    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 6
