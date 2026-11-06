"""Schema version 2 (Phase 02) -> 3 (Phase 03): existing data must survive, nothing silently.

`tests/fixtures/schema_v2.sql` is the exact DDL the Phase 02 code produced (captured from commit
ef0e16d). Each test builds a real v2 database with Phase 02 rows, then runs the CURRENT
`init_database` on it.
"""

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.config import Environment, Settings
from app.domain.mandate import AUTHORIZATION_ISSUER
from app.main import create_app
from app.repositories.database import (
    SCHEMA_VERSION,
    create_db_engine,
    create_session_factory,
    init_database,
)
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.authorization_service import AuthorizationService

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "schema_v2.sql"
PHASE02_SCOPES = '["create_draft", "read_status", "request_handoff", "submit_after_consent"]'


def ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def build_v2_database(path: Path) -> dict[str, str]:
    """A Phase 02 database: one OPENED application (revoked mandate), one in flight."""
    now = datetime.now(UTC)
    opened, in_flight = f"app-{uuid.uuid4().hex}", f"app-{uuid.uuid4().hex}"
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text(encoding="utf-8"))
    for app_id in (opened, in_flight):
        con.execute(
            "INSERT INTO applications (application_id, provider_id, product_id, channel_id, "
            "customer_ref, agent_actor_id, status, resume_status, version, created_at, "
            "updated_at) VALUES (?, 'fixture-bank', 'depot-basic', 'direct', 'cust-1', "
            "'agent-a', 'DRAFT', NULL, 1, ?, ?)",
            (app_id, ts(now), ts(now)),
        )
    con.execute(  # Phase 02 path to OPENED (raw SQL, as the triggers allow)
        "UPDATE applications SET status = 'SUBMITTED_TO_DEPOT_PROVIDER', version = 7 "
        "WHERE application_id = ?",
        (opened,),
    )
    con.execute(
        "UPDATE applications SET status = 'OPENED', version = 8 WHERE application_id = ?", (opened,)
    )
    con.execute(
        "UPDATE applications SET status = 'AWAITING_AUTHORIZATION', version = 2 "
        "WHERE application_id = ?",
        (in_flight,),
    )
    for app_id, revoked in ((opened, ts(now)), (in_flight, None)):
        con.execute(
            "INSERT INTO mandates (mandate_id, jti, application_id, agent_actor_id, customer_id, "
            "audience, scopes, issued_at, expires_at, revoked_at) VALUES (?, ?, ?, 'agent-a', "
            "'cust-1', 'trusteq-onboarding-api', ?, ?, ?, ?)",
            (
                f"mdt-{app_id}",
                f"jti-{app_id}",
                app_id,
                PHASE02_SCOPES,
                ts(now - timedelta(minutes=5)),
                ts(now + timedelta(hours=1)),
                revoked,
            ),
        )
    con.execute(
        "INSERT INTO audit_events (event_id, occurred_at, correlation_id, event_type, actor_type, "
        "actor_id, application_id, outcome, details) VALUES ('evt-1', ?, 'corr-1', "
        "'application.created', 'agent', 'agent-a', ?, 'success', '{}')",
        (ts(now), opened),
    )
    con.commit()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    con.close()
    return {"opened": opened, "in_flight": in_flight}


def objects(path: Path, kind: str) -> set[str]:
    with sqlite3.connect(path) as con:
        return {n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def upgrade(path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    try:
        init_database(engine)
    finally:
        engine.dispose()


def test_a_phase02_database_is_upgraded_in_place_without_losing_data(tmp_path: Path) -> None:
    db = tmp_path / "v2.db"
    ids = build_v2_database(db)
    with sqlite3.connect(db) as con:
        before = {
            table: con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608
            for table in ("applications", "audit_events")
        }
        old_mandates = con.execute("SELECT * FROM mandates ORDER BY mandate_id").fetchall()

    upgrade(db)

    with sqlite3.connect(db) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        for table, rows in before.items():
            assert con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows  # noqa: S608
        con.row_factory = sqlite3.Row
        mandates = con.execute("SELECT * FROM mandates ORDER BY mandate_id").fetchall()
    assert len(mandates) == len(old_mandates) == 2
    for new, old in zip(mandates, old_mandates, strict=True):
        assert (new["kind"], new["issuer"]) == ("onboarding", AUTHORIZATION_ISSUER)
        assert (new["mandate_id"], new["jti"], new["application_id"]) == old[:3]
        assert (new["agent_actor_id"], new["customer_id"], new["audience"]) == old[3:6]
        assert json.loads(new["scopes"]) == json.loads(old[6])
        assert (new["issued_at"], new["expires_at"], new["revoked_at"]) == old[7:10]
    assert {"agent_access_tokens", "customer_sessions", "mandates"} <= objects(db, "table")
    assert "_mandates_v2" not in objects(db, "table")
    assert {
        "trg_mandates_revocation_is_final",
        "trg_mandates_claims_are_immutable",
        "trg_mandates_binding_is_final",
        "trg_agent_access_tokens_revocation_is_final",
        "trg_customer_sessions_revocation_is_final",
    } <= objects(db, "trigger")
    assert {m["application_id"] for m in mandates} == set(ids.values())


def test_the_upgraded_mandates_table_enforces_the_phase03_rules(tmp_path: Path) -> None:
    db = tmp_path / "v2.db"
    ids = build_v2_database(db)
    upgrade(db)
    engine = create_db_engine(f"sqlite:///{db.as_posix()}")
    try:
        with engine.begin() as con, pytest.raises(IntegrityError, match="stays revoked"):
            con.exec_driver_sql(
                "UPDATE mandates SET revoked_at = NULL WHERE application_id = ?", (ids["opened"],)
            )
        with engine.begin() as con, pytest.raises(IntegrityError, match="immutable"):
            con.exec_driver_sql(
                "UPDATE mandates SET scopes = '[\"create_draft\"]' WHERE application_id = ?",
                (ids["in_flight"],),
            )
        with engine.begin() as con, pytest.raises(IntegrityError, match="UNIQUE"):
            con.exec_driver_sql(  # a second onboarding grant for the same application
                "INSERT INTO mandates (mandate_id, jti, kind, application_id, agent_actor_id, "
                "customer_id, issuer, audience, scopes, issued_at, expires_at) SELECT 'mdt-dup', "
                "'jti-dup', kind, application_id, agent_actor_id, customer_id, issuer, audience, "
                "scopes, issued_at, expires_at FROM mandates WHERE application_id = ?",
                (ids["in_flight"],),
            )
    finally:
        engine.dispose()


def test_upgrading_twice_is_harmless(tmp_path: Path) -> None:
    db = tmp_path / "v2.db"
    build_v2_database(db)
    upgrade(db)
    with sqlite3.connect(db) as con:
        first = con.execute("SELECT * FROM mandates ORDER BY 1").fetchall()
    upgrade(db)
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT * FROM mandates ORDER BY 1").fetchall() == first
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_an_upgrade_interrupted_after_the_first_step_resumes(tmp_path: Path) -> None:
    db = tmp_path / "v2.db"
    build_v2_database(db)
    with sqlite3.connect(db) as con:  # what a crash right after step 1 leaves behind
        con.execute("DROP TRIGGER trg_mandates_revocation_is_final")
        con.execute("ALTER TABLE mandates RENAME TO _mandates_v2")
        con.commit()
    assert "mandates" not in objects(db, "table")
    upgrade(db)
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*), MIN(kind) FROM mandates").fetchone() == (
            2,
            "onboarding",
        )
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert "_mandates_v2" not in objects(db, "table")
    assert "trg_mandates_revocation_is_final" in objects(db, "trigger")


def test_a_phase02_in_flight_application_can_be_continued_after_reissuing_tokens(
    tmp_path: Path,
) -> None:
    db = tmp_path / "v2.db"
    ids = build_v2_database(db)
    settings = Settings(
        _env_file=None, environment=Environment.TEST, database_url=f"sqlite:///{db.as_posix()}"
    )
    with TestClient(create_app(settings)) as client:  # startup runs the upgrade
        factory = create_session_factory(client.app.state.engine)  # type: ignore[attr-defined]
        operator = AuthorizationService(
            lambda: SqlAlchemyUnitOfWork(factory), bootstrap_enabled=True
        )
        credentials = operator.reissue_for_application(
            application_id=ids["in_flight"], correlation_id="corr-op"
        )
        status = {"Authorization": f"Bearer {credentials.status.token}"}
        onboarding = {"Authorization": f"Bearer {credentials.onboarding.token}"}
        view = client.get(f"/v1/applications/{ids['in_flight']}", headers=status)
        assert view.status_code == 200 and view.json()["state"] == "AWAITING_CUSTOMER"
        assert (
            client.post(
                f"/v1/applications/{ids['in_flight']}/handoff", headers=onboarding
            ).status_code
            == 201
        )
        # The legacy grant's old create_draft scope is NOT carried into the new token.
        assert credentials.onboarding.scopes == {"request_handoff", "submit_after_consent"}
        # An OPENED (revoked) Phase 02 application cannot be revived this way.
        with pytest.raises(Exception, match="not active"):
            operator.reissue_for_application(application_id=ids["opened"], correlation_id="c")
